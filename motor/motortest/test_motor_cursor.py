# Copyright 2012 10gen, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test Motor, an asynchronous driver for MongoDB and Tornado."""

import unittest
import time

import motor
if not motor.requirements_satisfied:
    from nose.plugins.skip import SkipTest
    raise SkipTest("Tornado or greenlet not installed")

from tornado import ioloop, gen

from motor.motortest import (
    MotorTest, async_test_engine, host, port, AssertEqual)
import pymongo


class MotorCursorTest(MotorTest):
    def test_cursor(self):
        cx = self.motor_connection(host, port)
        coll = cx.test.foo
        cursor = coll.find()
        self.assert_(isinstance(cursor, motor.MotorCursor))
        self.assertFalse(cursor.started, "Cursor shouldn't start immediately")

    @async_test_engine()
    def test_count(self):
        coll = self.motor_connection(host, port).test.test_collection
        yield AssertEqual(200, coll.find().count)
        yield AssertEqual(100, coll.find({'_id': {'$gt': 99}}).count)
        where = 'this._id % 2 == 0 && this._id >= 50'
        yield AssertEqual(75, coll.find({'$where': where}).count)
        yield AssertEqual(75, coll.find().where(where).count)
        yield AssertEqual(
            25,
            coll.find({'_id': {'$lt': 100}}).where(where).count)
        yield AssertEqual(
            25,
            coll.find({'_id': {'$lt': 100}, '$where': where}).count)

    @async_test_engine()
    def test_next(self):
        coll = self.motor_connection(host, port).test.test_collection
        cursor = coll.find({}, {'_id': 1}).sort([('_id', pymongo.ASCENDING)])
        yield AssertEqual({'_id': 0}, cursor.next)
        self.assertTrue(cursor.alive)

        # Dereferencing the cursor eventually closes it on the server; yielding
        # clears the engine Runner's reference to the cursor.
        loop = ioloop.IOLoop.instance()
        yield gen.Task(loop.add_callback)
        del cursor

        while self.get_open_cursors() > self.open_cursors:
            yield gen.Task(loop.add_timeout, time.time() + 0.5)

    def test_each(self):
        coll = self.motor_connection(host, port).test.test_collection
        cursor = coll.find({}, {'_id': 1}).sort([('_id', pymongo.ASCENDING)])

        results = []
        def callback(result, error):
            if error:
                raise error

            results.append(result)

        cursor.each(callback)
        expected = [{'_id': i} for i in range(200)] + [None]
        self.assertEventuallyEqual(expected, lambda: results)
        ioloop.IOLoop.instance().start()

    @async_test_engine()
    def test_to_list(self):
        coll = self.motor_connection(host, port).test.test_collection
        cursor = coll.find({}, {'_id': 1}).sort([('_id', pymongo.ASCENDING)])
        expected = [{'_id': i} for i in range(200)]
        yield AssertEqual(expected, cursor.to_list)
        yield motor.Op(cursor.close)

    @async_test_engine()
    def test_limit_zero(self):
        # Limit of 0 is a weird case that PyMongo handles specially, make sure
        # Motor does too. cursor.limit(0) means "remove limit", but cursor[:0]
        # sets a limit of 0.
        coll = self.motor_connection(host, port).test.test_collection

        # Make sure our setup code made some documents
        results = yield motor.Op(coll.find().to_list)
        self.assertTrue(len(results) > 0)
        yield AssertEqual(None, coll.find()[:0].next)
        yield AssertEqual(None, coll.find()[:0].each)
        yield AssertEqual([], coll.find()[:0].to_list)
        self.wait_for_cursors()

    def test_cursor_close(self):
        # The flow here is complex; we're testing that a cursor can be
        # explicitly closed.
        # 1. Create a cursor on the server by running find()
        # 2. In the find() callback, start closing the cursor
        # 3. Wait a little to make sure the cursor closes
        # 4. Stop the IOLoop so we can exit test_cursor_close()
        # 5. In MotorTest.tearDown(), we'll assert all cursors have closed.
        cx = self.motor_connection(host, port)
        loop = ioloop.IOLoop.instance()

        def found(result, error):
            if error:
                raise error

            self.assertFalse(cursor.delegate._Cursor__killed)
            cursor.close(callback=closed)

            # Cancel iteration, so the cursor isn't exhausted
            return False

        def closed(result, error):
            # Cursor reports it's alive because it has buffered data, even
            # though it's killed on the server
            self.assertTrue(cursor.alive)
            self.assertTrue(cursor.delegate._Cursor__killed)
            loop.stop()
            if error:
                raise error

        cursor = cx.test.test_collection.find()
        cursor.each(callback=found)

        # Start the find(), the callback will close the cursor
        loop.start()
        self.assertEqual(self.open_cursors, self.get_open_cursors())

    def test_each_cancel(self):
        loop = ioloop.IOLoop.instance()
        cx = self.motor_connection(host, port)
        collection = cx.test.test_collection
        results = []

        def cancel(result, error):
            if error:
                loop.stop()
                raise error

            results.append(result)
            loop.add_callback(canceled)
            return False # Cancel iteration

        def canceled():
            try:
                self.assertFalse(cursor.delegate._Cursor__killed)
                self.assertTrue(cursor.alive)

                # Resume iteration
                cursor.each(each)
            except Exception:
                loop.stop()
                raise

        def each(result, error):
            if error:
                loop.stop()
                raise error

            if result:
                results.append(result)
            else:
                # Complete
                loop.stop()

        cursor = collection.find()
        cursor.each(cancel)
        loop.start()

        self.assertEqual(self.sync_coll.count(), len(results))

    def test_cursor_slice_argument_checking(self):
        cx = self.motor_connection(host, port)
        collection = cx.test.test_collection

        for arg in '', None, {}, []:
            self.assertRaises(TypeError, lambda: collection.find()[arg])

        self.assertRaises(IndexError, lambda: collection.find()[-1])

    @async_test_engine()
    def test_cursor_slice(self):
        # This is an asynchronous copy of PyMongo's test_getitem_slice_index in
        # test_cursor.py

        cx = self.motor_connection(host, port)

        # test_collection was filled out in setUp()
        coll = cx.test.test_collection

        self.assertRaises(IndexError, lambda: coll.find()[-1])
        self.assertRaises(IndexError, lambda: coll.find()[1:2:2])
        self.assertRaises(IndexError, lambda: coll.find()[2:1])

        result = yield motor.Op(coll.find()[0:].to_list)
        self.assertEqual(200, len(result))

        result = yield motor.Op(coll.find()[20:].to_list)
        self.assertEqual(180, len(result))

        result = yield motor.Op(coll.find()[99:].to_list)
        self.assertEqual(101, len(result))

        result = yield motor.Op(coll.find()[1000:].to_list)
        self.assertEqual(0, len(result))

        result = yield motor.Op(coll.find()[20:25].to_list)
        self.assertEqual(5, len(result))

        # Any slice overrides all previous slices
        result = yield motor.Op(coll.find()[20:25][20:].to_list)
        self.assertEqual(180, len(result))

        result = yield motor.Op(coll.find()[20:25].limit(0).skip(20).to_list)
        self.assertEqual(180, len(result))

        result = yield motor.Op(coll.find().limit(0).skip(20)[20:25].to_list)
        self.assertEqual(5, len(result))

        result = yield motor.Op(coll.find()[:1].to_list)
        self.assertEqual(1, len(result))

        result = yield motor.Op(coll.find()[:5].to_list)
        self.assertEqual(5, len(result))


if __name__ == '__main__':
    unittest.main()
