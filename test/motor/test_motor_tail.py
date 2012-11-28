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

import functools
import threading
import time

import motor
if not motor.requirements_satisfied:
    from nose.plugins.skip import SkipTest
    raise SkipTest("Tornado or greenlet not installed")

from tornado import ioloop, gen

from test.test_connection import host, port
from test.motor import MotorTest, async_test_engine, AssertRaises
from pymongo.errors import OperationFailure

# TODO: test tailing empty collection

class MotorTailTest(MotorTest):
    def setUp(self):
        super(MotorTailTest, self).setUp()
        self.sync_db.capped.drop()
        # autoIndexId catches test bugs that try to insert duplicate _id's
        self.sync_db.create_collection(
            'capped', capped=True, size=1000, autoIndexId=True)

        self.sync_db.uncapped.drop()
        self.sync_db.uncapped.insert({}, safe=True)

    def start_insertion_thread(self, pauses):
        """A thread that gradually inserts documents into a capped collection
        """
        def add_docs():
            i = 0
            for pause in pauses:
                if pause == 'drop':
                    self.sync_db.capped.drop()
                else:
                    time.sleep(pause)
                    self.sync_db.capped.insert({'_id': i}, safe=True)
                    i += 1

        t = threading.Thread(target=add_docs)
        t.start()
        return t

    # Used by test_tail, test_tail_drop_collection, etc.
    def each(self, results, n_expected, callback, result, error):
        if error:
            results.append(type(error))
        elif result:
            results.append(result)
            if len(results) != n_expected:
                # Continue
                return

        # Cancel iteration
        callback()
        return False

    # Need a longish pause to ensure tail() recovers when cursor times out and
    # returns None
    tail_pauses = (
        1, 0, 1, 0, 0, 0, 5, 0, 0, 0, 0, 0, 0, 0, 0.1, 0.1, 0, 0)

    @async_test_engine(timeout_sec=sum(tail_pauses) + 1)
    def test_tail(self, done):
        t = self.start_insertion_thread(self.tail_pauses)
        results = []
        each = functools.partial(
            self.each, results, len(self.tail_pauses),
            (yield gen.Callback('done')))

        test_db = self.motor_connection(host, port).test
        capped = test_db.capped

        # Note we do *not* pass tailable or await_data to find(), the
        # convenience method handles it for us.
        capped.find().tail(each)
        yield gen.Wait('done')
        self.assertEqual(
            results,
            [{'_id': i} for i in range(len(self.tail_pauses))])

        t.join()
        done()

    drop_collection_pauses = (0, 0, 1, 'drop', 1, 0, 0)

    @async_test_engine(timeout_sec=10)
    def test_tail_drop_collection(self, done):
        # Ensure tail() throws error when its collection is dropped
        t = self.start_insertion_thread(self.drop_collection_pauses)

        results = []
        each = functools.partial(
            self.each, results, len(self.drop_collection_pauses),
            (yield gen.Callback('done')))

        test_db = self.motor_connection(host, port).test
        capped = test_db.capped
        capped.find().tail(each)
        yield gen.Wait('done')

        # Don't assume that the first 3 results before the drop will be
        # recorded -- dropping a collection kills the cursor even if not
        # fully iterated.
        self.assertTrue(OperationFailure in results)
        self.assertFalse('cancelled' in results)
        t.join()
        done()

    @async_test_engine()
    def test_tail_uncapped_collection(self, done):
        test_db = self.motor_connection(host, port).test
        uncapped = test_db.uncapped

        yield AssertRaises(
            OperationFailure,
            uncapped.find().tail)

        done()

    @async_test_engine()
    def test_tail_nonempty_collection(self, done):
        self.sync_db.capped.insert([{'_id': -2}, {'_id': -1}], safe=True)

        pauses = (0, 0, 1, 0, 0)
        t = self.start_insertion_thread(pauses)

        results = []
        each = functools.partial(
            self.each, results, len(pauses) + 2, (yield gen.Callback('done')))

        test_db = self.motor_connection(host, port).test
        capped = test_db.capped
        capped.find().tail(each)
        yield gen.Wait('done')
        self.assertEqual(
            [{'_id': i} for i in range(-2, len(pauses))],
            results)

        t.join()
        done()

    @async_test_engine()
    def test_tail_gen(self, done):
        pauses = (1, 0.5, 1, 0, 0)
        t = self.start_insertion_thread(pauses)

        loop = ioloop.IOLoop.instance()
        results = []

        test_db = self.motor_connection(host, port).test
        capped = test_db.capped
        cursor = capped.find(tailable=True, await_data=True)
        while len(results) < len(pauses):
            if not cursor.alive:
                # While collection is empty, tailable cursor dies immediately
                yield gen.Task(loop.add_timeout, time.time() + 0.1)
                cursor = capped.find(tailable=True)

            if (yield cursor.fetch_next):
                results.append(cursor.next_object())
            else:
                yield gen.Task(loop.add_timeout, time.time() + 0.1)

        t.join()
        self.assertEqual([{'_id': i} for i in range(len(pauses))], results)
        yield motor.Op(cursor.close)
        done()
