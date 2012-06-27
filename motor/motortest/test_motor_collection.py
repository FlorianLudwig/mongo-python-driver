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

import time
import unittest

from tornado import gen, ioloop

import pymongo
import motor
if not motor.requirements_satisfied:
    from nose.plugins.skip import SkipTest
    raise SkipTest("Tornado or greenlet not installed")

from motor.motortest import (
    MotorTest, async_test_engine, host, port, AssertEqual, AssertRaises)
import bson
from bson.objectid import ObjectId
from pymongo.errors import DuplicateKeyError
from test.utils import delay


class MotorCollectionTest(MotorTest):
    @async_test_engine()
    def test_collection(self):
        # Test that we can create a collection directly, not just from
        # MotorConnection's accessors
        db = self.motor_connection(host, port).test
        collection = motor.MotorCollection(db, 'test_collection')

        # Make sure we got the right collection and it can do an operation
        doc = yield motor.Op(collection.find_one, {'_id': 1})
        self.assertEqual(1, doc['_id'])

    @async_test_engine()
    def test_dotted_collection_name(self):
        # Ensure that remove, insert, and find work on collections with dots
        # in their names.
        cx = self.motor_connection(host, port)
        for coll in (
            cx.test.foo,
            cx.test.foo.bar,
            cx.test.foo.bar.baz.quux
        ):
            yield motor.Op(coll.remove)
            yield AssertEqual('xyzzy', coll.insert, {'_id':'xyzzy'})
            result = yield motor.Op(coll.find_one, {'_id':'xyzzy'})
            self.assertEqual(result['_id'], 'xyzzy')
            yield motor.Op(coll.remove)
            yield AssertEqual(None, coll.find_one, {'_id':'xyzzy'})

    @async_test_engine()
    def test_next(self):
        # 1. Open a connection.
        #
        # 2. test_collection has docs inserted in setUp(). Query for documents
        # with _id 0 through 13, in batches of 5: 0-4, 5-9, 10-13.
        #
        # 3. For each document, check if the cursor has been closed. I expect
        # it to remain open until we've retrieved doc with _id 10. Mongo doesn't
        # close the cursor and return cursor_id 0 if the final batch exactly
        # contains the last document -- the last batch size has to go one *past*
        # the final document in order to close the cursor.
        connection = self.motor_connection(host, port)

        cursor = connection.test.test_collection.find(
            {'_id': {'$lt':14}},
            {'s': False}, # exclude 's' field
            sort=[('_id', 1)],
        ).batch_size(5)

        results = []
        while True:
            doc = yield motor.Op(cursor.next)

            if doc:
                results.append(doc['_id'])
                if doc['_id'] < 10:
                    self.assertEqual(
                        1 + self.open_cursors,
                        self.get_open_cursors()
                    )
            else:
                break

        self.assertEqual(range(14), results)
        self.assertEqual(
            self.open_cursors,
            self.get_open_cursors()
        )

    def test_each(self):
        # 1. Open a connection.
        #
        # 2. test_collection has docs inserted in setUp(). Query for documents
        # with _id 0 through 13, in batches of 5: 0-4, 5-9, 10-13.
        #
        # 3. For each document, check if the cursor has been closed. I expect
        # it to remain open until we've retrieved doc with _id 10. Oddly, Mongo
        # doesn't close the cursor and return cursor_id 0 if the final batch
        # exactly contains the last document -- the last batch size has to go
        # one *past* the final document in order to close the cursor.
        connection = self.motor_connection(host, port)

        cursor = connection.test.test_collection.find(
            {'_id': {'$lt':14}},
            {'s': False}, # exclude 's' field
            sort=[('_id', 1)],
        ).batch_size(5)

        def callback(doc, error):
            if error:
                raise error

            if doc:
                results.append(doc['_id'])

            if doc and doc['_id'] < 10:
                self.assertEqual(
                    1 + self.open_cursors,
                    self.get_open_cursors()
                )
            else:
                self.assertEqual(
                    self.open_cursors,
                    self.get_open_cursors()
                )

        results = []
        cursor.each(callback)

        self.assertEventuallyEqual(range(14), lambda: results)

    @async_test_engine()
    def test_find_where(self):
        # Check that $where clauses work
        coll = self.motor_connection(host, port).test.test_collection
        res = yield motor.Op(coll.find().to_list)
        self.assertEqual(200, len(res))

        # Get the one doc with _id of 8
        where = 'this._id == 2 * 4'
        res0 = yield motor.Op(coll.find({'$where': where}).to_list)
        self.assertEqual(1, len(res0))
        self.assertEqual(8, res0[0]['_id'])

        res1 = yield motor.Op(coll.find().where(where).to_list)
        self.assertEqual(res0, res1)

    def test_find_callback(self):
        cx = self.motor_connection(host, port)
        cursor = cx.test.test_collection.find()
        self.check_required_callback(cursor.next)
        self.check_required_callback(cursor.each)
        self.check_required_callback(cursor.to_list)

        # Ensure tearDown doesn't complain about open cursors
        self.wait_for_cursors()

    def test_find_is_async(self):
        # Confirm find() is async by launching two operations which will finish
        # out of order. Also test that MotorConnection doesn't reuse sockets
        # incorrectly.
        cx = self.motor_connection(host, port)

        results = []

        def callback(doc, error):
            if error:
                raise error
            if doc:
                results.append(doc)

        # Launch find operations for _id's 1 and 2 which will finish in order
        # 2, then 1.
        loop = ioloop.IOLoop.instance()

        now = time.time()

        # This find() takes 0.5 seconds
        loop.add_timeout(
            now + 0.1,
            lambda: cx.test.test_collection.find(
                {'_id': 1, '$where': delay(0.5)},
                fields={'s': True, '_id': False},
            ).each(callback)
        )

        # Very fast lookup
        loop.add_timeout(
            now + 0.2,
            lambda: cx.test.test_collection.find(
                {'_id': 2},
                fields={'s': True, '_id': False},
            ).each(callback)
        )

        # Results were appended in order 2, 1
        self.assertEventuallyEqual(
            [{'s': hex(s)} for s in (2, 1)],
            lambda: results,
            timeout_sec=2
        )

        ioloop.IOLoop.instance().start()

    def test_find_and_cancel(self):
        cx = self.motor_connection(host, port)
        results = []

        def callback(doc, error):
            if error:
                raise error

            results.append(doc)

            if len(results) == 2:
                # cancel iteration
                return False

        cursor = cx.test.test_collection.find(sort=[('s', 1)])
        cursor.each(callback)
        self.assertEventuallyEqual(
            [
                {'_id': 0, 's': hex(0)},
                {'_id': 1, 's': hex(1)},
            ],
            lambda: results,
        )

        ioloop.IOLoop.instance().start()

        # There are 200 docs, but we canceled after 2
        self.assertEqual(2, len(results))

        cursor.close()
        self.wait_for_cursors()

    @async_test_engine()
    def test_find_to_list(self):
        yield AssertEqual(
            [{'_id': i} for i in range(200)],
            self.motor_connection(host, port).test.test_collection.find(
                sort=[('_id', 1)], fields=['_id']
            ).to_list
        )

    @async_test_engine()
    def test_find_one(self):
        cx = self.motor_connection(host, port)
        yield AssertEqual(
            {'_id': 1, 's': hex(1)},
            self.motor_connection(host, port).test.test_collection.find_one,
            {'_id': 1})

    def test_find_one_callback(self):
        cx = self.motor_connection(host, port)
        self.check_required_callback(cx.test.test_collection.find_one)

    def test_find_one_is_async(self):
        # Confirm find_one() is async by launching two operations which will
        # finish out of order.
        results = []

        def callback(result, error):
            if error:
                raise error
            results.append(result)

        # Launch 2 find_one operations for _id's 1 and 2, which will finish in
        # order 2 then 1.
        loop = ioloop.IOLoop.instance()

        cx = self.motor_connection(host, port)

        # This find_one() takes half a second
        loop.add_timeout(
            time.time() + 0.1,
            lambda: cx.test.test_collection.find_one(
                {'_id': 1, '$where': delay(1)},
                fields={'s': True, '_id': False},
                callback=callback
            )
        )

        # Very fast lookup
        loop.add_timeout(
            time.time() + 0.2,
            lambda: cx.test.test_collection.find_one(
                {'_id': 2},
                fields={'s': True, '_id': False},
                callback=callback
            )
        )

        # Results were appended in order 2, 1
        self.assertEventuallyEqual(
            [{'s': hex(s)} for s in (2, 1)],
            lambda: results
        )

        ioloop.IOLoop.instance().start()

    @async_test_engine()
    def test_update(self):
        cx = self.motor_connection(host, port)
        result = yield motor.Op(cx.test.test_collection.update,
            {'_id': 5},
            {'$set': {'foo': 'bar'}},
        )

        self.assertEqual(1, result['ok'])
        self.assertEqual(True, result['updatedExisting'])
        self.assertEqual(1, result['n'])
        self.assertEqual(None, result['err'])

    @async_test_engine()
    def test_update_bad(self):
        # Violate a unique index, make sure we handle error well
        results = []

        def callback(result, error):
            self.assert_(isinstance(error, DuplicateKeyError))
            self.assertEqual(None, result)
            results.append(result)

        cx = self.motor_connection(host, port)

        try:
            # There's already a document with s: hex(4)
            yield motor.Op(
                cx.test.test_collection.update,
                {'_id': 5},
                {'$set': {'s': hex(4)}},
            )
        except DuplicateKeyError:
            pass
        except Exception, e:
            self.fail("Expected DuplicateKeyError, got %s" % repr(e))
        else:
            self.fail("DuplicateKeyError not raised")

    def test_update_callback(self):
        cx = self.motor_connection(host, port)
        self.check_optional_callback(cx.test.test_collection.update, {}, {})

    @async_test_engine()
    def test_insert(self):
        yield AssertEqual(
            201,
            self.motor_connection(host, port).test.test_collection.insert,
            {'_id': 201}
        )

    @async_test_engine()
    def test_insert_many(self):
        yield AssertEqual(
            range(201, 211),
            self.motor_connection(host, port).test.test_collection.insert,
            [{'_id': i, 's': hex(i)} for i in range(201, 211)]
        )

    @async_test_engine()
    def test_insert_bad(self):
        # Violate a unique index, make sure we handle error well
        yield AssertRaises(
            DuplicateKeyError,
            self.motor_connection(host, port).test.test_collection.insert,
            {'s': hex(4)} # There's already a document with s: hex(4)
        )

    def test_insert_many_one_bad(self):
        # Violate a unique index in one of many updates, handle error
        result = yield AssertRaises(
            DuplicateKeyError,
            self.motor_connection(host, port).test.test_collection.insert,
            [
                {'_id': 201, 's': hex(201)},
                {'_id': 202, 's': hex(4)}, # Already exists
                {'_id': 203, 's': hex(203)},
            ]
        )

        # Even though first insert succeeded, an exception was raised and
        # result is None
        self.assertEqual(None, result)

        # First insert should've succeeded
        yield AssertEqual(
            [{'_id': 201, 's': hex(201)}],
            self.sync_db.test_collection.find({'_id': 201}).to_list
        )

        # Final insert didn't execute, since second failed
        yield AssertEqual(
            [],
            self.sync_db.test_collection.find({'_id': 203}).to_list
        )

    def test_save_callback(self):
        cx = self.motor_connection(host, port)
        self.check_optional_callback(cx.test.test_collection.save, {})

    @async_test_engine()
    def test_save_with_id(self):
        # save() returns the _id, in this case 5
        yield AssertEqual(
            5,
            self.motor_connection(host, port).test.test_collection.save,
            {'_id': 5}
        )

    @async_test_engine()
    def test_save_without_id(self):
        result = yield motor.Op(
            self.motor_connection(host, port).test.test_collection.save,
            {'fiddle': 'faddle'}
        )

        # save() returns the new _id
        self.assertTrue(isinstance(result, ObjectId))

    @async_test_engine()
    def test_save_bad(self):
        # Violate a unique index, make sure we handle error well
        yield AssertRaises(
            DuplicateKeyError,
            self.motor_connection(host, port).test.test_collection.save,
            {'_id': 5, 's': hex(4)} # There's already a document with s: hex(4)
        )

    @async_test_engine()
    def test_save_multiple(self):
        # TODO: what are we testing here really?
        cx = self.motor_connection(host, port)

        for i in range(10):
            cx.test.test_collection.save(
                {'_id': i + 500, 's': hex(i + 500)},
                callback=(yield gen.Callback(key=i))
            )

        results = yield motor.WaitAllOps(range(10))

        # TODO: doc that result, error are in result.args for all gen.engine
        # yields

        # Once all saves complete, results will be a list of _id's, from 500 to
        # 509, but not necessarily in that order since we're motor
        self.assertEqual(range(500, 510), sorted(results))

    @async_test_engine()
    def test_remove(self):
        # Remove a document twice, check that we get a success response first
        # time and an error the second time.
        cx = self.motor_connection(host, port)
        result = yield motor.Op(cx.test.test_collection.remove, {'_id': 1})

        # First time we remove, n = 1
        self.assertEqual(1, result['n'])
        self.assertEqual(1, result['ok'])
        self.assertEqual(None, result['err'])

        result = yield motor.Op(cx.test.test_collection.remove, {'_id': 1})

        # Second time, document is already gone, n = 0
        self.assertEqual(0, result['n'])
        self.assertEqual(1, result['ok'])
        self.assertEqual(None, result['err'])

    def test_remove_callback(self):
        cx = self.motor_connection(host, port)
        self.check_optional_callback(cx.test.test_collection.remove)

    @async_test_engine()
    def test_unsafe_remove(self):
        # Test that unsafe removes with no callback still work
        self.assertEqual(1, self.sync_coll.find({'_id': 117}).count(),
            msg="Test setup should have a document with _id 117")

        coll = self.motor_connection(host, port).test.test_collection
        yield motor.Op(coll.remove, {'_id': 117})
        yield AssertEqual(0, coll.find({'_id': 117}).count)

    def test_unsafe_insert(self):
        # Test that unsafe inserts with no callback still work

        # id 201 not present
        self.assertEqual(0, self.sync_coll.find({'_id': 201}).count())

        # insert id 201 without a callback or safe=True
        self.motor_connection(host, port).test.test_collection.insert(
            {'_id': 201})

        # the insert is eventually executed
        self.assertEventuallyEqual(
            1,
            lambda: len(list(self.sync_db.test_collection.find({'_id': 201})))
        )

        ioloop.IOLoop.instance().start()

    def test_unsafe_save(self):
        # Test that unsafe saves with no callback still work
        self.motor_connection(host, port).test.test_collection.save(
            {'_id': 201})

        self.assertEventuallyEqual(
            1,
            lambda: len(list(self.sync_db.test_collection.find({'_id': 201})))
        )

        ioloop.IOLoop.instance().start()

    def test_nested_callbacks(self):
        cx = self.motor_connection(host, port)
        results = [0]

        def callback(result, error):
            if error:
                raise error

            if not result:
                # Done iterating
                return

            results[0] += 1
            if results[0] < 1000:
                cx.test.test_collection.find(
                    {'_id': 1},
                    {'s': False},
                ).each(callback)

        cx.test.test_collection.find(
            {'_id': 1},
            {'s': False},
        ).each(callback)

        self.assertEventuallyEqual(
            [1000],
            lambda: results,
            timeout_sec=30
        )

        ioloop.IOLoop.instance().start()

    def test_nested_callbacks_2(self):
        cx = motor.MotorConnection(host, port)
        results = []

        def connected(cx, error):
            if error:
                raise error

            cx.test.test.insert({'_id': 201}, callback=inserted)

        def inserted(result, error):
            if error:
                raise error

            cx.test.test.find_one({'_id': 201}, callback=found)

        def found(result, error):
            if error:
                raise error

            cx.test.test.remove({'_id': 201}, callback=removed)

        def removed(result, error):
            results.append('done')

        cx.open(connected)

        self.assertEventuallyEqual(
            ['done'],
            lambda: results
        )

        ioloop.IOLoop.instance().start()

    @async_test_engine()
    def test_map_reduce(self):
        # Count number of documents with even and odd _id
        expected_result = [{'_id': 0, 'value': 100}, {'_id': 1, 'value': 100}]
        map = bson.Code('function map() { emit(this._id % 2, 1); }')
        reduce = bson.Code('''
        function reduce(key, values) {
            r = 0;
            values.forEach(function(value) { r += value; });
            return r;
        }''')

        cx = self.motor_connection(host, port)
        yield motor.Op(cx.test.tmp_mr.drop)

        # First do a standard mapreduce, should return MotorCollection
        tmp_mr = yield motor.Op(cx.test.test_collection.map_reduce,
            map, reduce, 'tmp_mr')

        self.assertTrue(isinstance(tmp_mr, motor.MotorCollection),
            'map_reduce should return MotorCollection, not %s' % tmp_mr)

        result = yield motor.Op(tmp_mr.find().sort([('_id', 1)]).to_list)
        self.assertEqual(expected_result, result)

        # Standard mapreduce with full response
        yield motor.Op(cx.test.tmp_mr.drop)
        response = yield motor.Op(cx.test.test_collection.map_reduce,
            map, reduce, 'tmp_mr', full_response=True)

        self.assertTrue(isinstance(response, dict),
            'map_reduce should return dict, not %s' % response)

        self.assertEqual('tmp_mr', response['result'])
        result = yield motor.Op(tmp_mr.find().sort([('_id', 1)]).to_list)
        self.assertEqual(expected_result, result)

        # Inline mapreduce
        yield motor.Op(cx.test.tmp_mr.drop)
        result = yield motor.Op(cx.test.test_collection.inline_map_reduce,
            map, reduce)

        result.sort(key=lambda doc: doc['_id'])
        self.assertEqual(expected_result, result)

    @async_test_engine()
    def test_get_last_error_options(self):
        cx = motor.MotorConnection(host, port)

        # An implementation quirk of Motor, can't access properties until
        # connected
        self.assertRaises(pymongo.errors.InvalidOperation, getattr, cx, 'safe')

        yield motor.Op(cx.open)
        self.assertFalse(cx.safe)
        self.assertEqual({}, cx.get_lasterror_options())

        # TODO: once PyMongo 'safe' behavior is fixed, test that
        # MotorConnection's 'safe' is True with a GLE option and safe=False
        # PYTHON-358
        for safe, gle_options in [
            (True,  {}),
            (True, {'w': 2}),
            (True, {'wtimeout': 1000}),
            (True, {'j': True}),
        ]:
            cx = motor.MotorConnection(host, port, safe=safe, **gle_options)
            yield motor.Op(cx.open)
            expected_safe = bool(safe or gle_options)
            self.assertEqual(expected_safe, cx.safe,
                "Expected safe %s with safe=%s and options %s" % (
                    expected_safe, safe, gle_options
                ))
            self.assertEqual(gle_options, cx.get_lasterror_options())

            db = cx.test
            self.assertEqual(expected_safe, db.safe)
            self.assertEqual(gle_options, db.get_lasterror_options())

            test_collection = db.test_collection
            self.assertEqual(expected_safe, test_collection.safe)
            self.assertEqual(
                gle_options, test_collection.get_lasterror_options())

    @async_test_engine()
    def test_indexes(self):
        cx = self.motor_connection(host, port)
        test_collection = cx.test.test_collection

        # Create an index
        idx_name = yield motor.Op(test_collection.create_index, [('foo', 1)])
        index_info = yield motor.Op(test_collection.index_information)
        self.assertEqual([('foo', 1)], index_info[idx_name]['key'])

        # Ensure the same index, test that callback is executed
        result = yield motor.Op(test_collection.ensure_index, [('foo', 1)])
        self.assertEqual(None, result)
        result2 = yield motor.Op(test_collection.ensure_index, [('foo', 1)])
        self.assertEqual(None, result2)

        # Ensure an index that doesn't exist, test it's created
        yield motor.Op(test_collection.ensure_index, [('bar', 1)])
        index_info = yield motor.Op(test_collection.index_information)
        self.assertTrue(any([
            info['key'] == [('bar', 1)] for info in index_info.values()
        ]))

        # Don't test drop_index or drop_indexes -- Synchro tests them


if __name__ == '__main__':
    unittest.main()
