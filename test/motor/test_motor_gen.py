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

from nose.plugins.skip import SkipTest

from pymongo.errors import DuplicateKeyError
import motor

if not motor.requirements_satisfied:
    raise SkipTest("Tornado or greenlet not installed")

from tornado import gen
from test.motor import MotorTest, host, port, async_test_engine


class MotorGenTest(MotorTest):
    def tearDown(self):
        self.sync_db.test_collection2.drop()
        super(MotorGenTest, self).tearDown()

    @async_test_engine()
    def test_op(self):
        cx = self.motor_connection(host, port)
        collection = cx.test.test_collection
        doc = {'_id': 'jesse'}
        _id = yield motor.Op(collection.insert, doc)
        self.assertEqual('jesse', _id)
        result = yield motor.Op(collection.find_one, doc)
        self.assertEqual(doc, result)

        error = None
        try:
            yield motor.Op(collection.insert, doc)
        except Exception, e:
            error = e

        self.assertTrue(isinstance(error, DuplicateKeyError))

    @async_test_engine()
    def test_wait_op(self):
        cx = self.motor_connection(host, port)
        collection = cx.test.test_collection
        doc = {'_id': 'jesse'}
        collection.insert(doc, callback=(yield gen.Callback('insert_a')))
        _id = yield motor.WaitOp('insert_a')
        self.assertEqual('jesse', _id)
        collection.find_one(doc, callback=(yield gen.Callback('find_one')))
        result = yield motor.WaitOp('find_one')
        self.assertEqual(doc, result)

        # The DuplicateKeyError isn't raised here
        collection.insert(doc, callback=(yield gen.Callback('insert_b')))

        error = None
        try:
            # Error here
            yield motor.WaitOp('insert_b')
        except Exception, e:
            error = e

        self.assertTrue(isinstance(error, DuplicateKeyError))

    @async_test_engine()
    def test_wait_all_ops(self):
        cx = self.motor_connection(host, port)
        collection = cx.test.test_collection2
        collection.insert(
            {'_id': 'b'}, callback=(yield gen.Callback('insert_b')))
        collection.insert(
            {'_id': 'a'}, callback=(yield gen.Callback('insert_a')))
        ids = yield motor.WaitAllOps(['insert_b', 'insert_a'])
        self.assertEqual(['b', 'a'], ids)

        collection.find_one(
            {'_id': 'a'}, callback=(yield gen.Callback('find_one0')))
        collection.find_one(
            {'_id': 'b'}, callback=(yield gen.Callback('find_one1')))
        docs = yield motor.WaitAllOps(['find_one0', 'find_one1'])
        self.assertEqual([{'_id': 'a'}, {'_id': 'b'}], docs)

        collection.insert(
            {'_id': 'c'}, callback=(yield gen.Callback('insert_c')))

        # The DuplicateKeyError isn't raised here
        collection.insert(
            {'_id': 'b'}, callback=(yield gen.Callback('dupe_insert_b')))

        error = None
        try:
            # Error here
            yield motor.WaitAllOps(['insert_c', 'dupe_insert_b'])
        except Exception, e:
            error = e

        self.assertTrue(isinstance(error, DuplicateKeyError))


if __name__ == '__main__':
    unittest.main()
