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

import motor
if not motor.requirements_satisfied:
    from nose.plugins.skip import SkipTest
    raise SkipTest("Tornado or greenlet not installed")

import tornado
from tornado import ioloop, gen

import pymongo

from motor.motortest import (
    MotorTest, async_test_engine, host, port, AssertRaises, AssertEqual,
    puritanical)
from pymongo.errors import (
    InvalidOperation, ConfigurationError, ConnectionFailure, AutoReconnect)
from test.utils import server_is_master_with_slave, delay


class MotorConnectionTest(MotorTest):
    @async_test_engine()
    def test_connection(self):
        cx = motor.MotorConnection(host, port)

        # Can't access databases before connecting
        self.assertRaises(
            pymongo.errors.InvalidOperation,
            lambda: cx.some_database_name
        )

        self.assertRaises(
            pymongo.errors.InvalidOperation,
            lambda: cx['some_database_name']
        )

        result = yield motor.Op(cx.open)
        self.assertEqual(result, cx)
        self.assertTrue(cx.connected)

    def test_connection_callback(self):
        cx = motor.MotorConnection(host, port)
        self.check_optional_callback(cx.open)

    @async_test_engine()
    def test_sync_connection(self):
        class DictSubclass(dict):
            pass

        args = host, port
        kwargs = dict(
            connectTimeoutMS=1000, socketTimeoutMS=1500, max_pool_size=23,
            document_class=DictSubclass, tz_aware=True)

        cx = yield motor.Op(motor.MotorConnection(*args, **kwargs).open)
        sync_cx = cx.sync_connection()
        self.assertTrue(isinstance(sync_cx, pymongo.connection.Connection))
        self.assertEqual(host, sync_cx.host)
        self.assertEqual(port, sync_cx.port)
        self.assertEqual(1000, sync_cx._Connection__conn_timeout * 1000.0)
        self.assertEqual(1500, sync_cx._Connection__net_timeout * 1000.0)
        self.assertEqual(23, sync_cx._Connection__max_pool_size)
        self.assertEqual(True, sync_cx._Connection__tz_aware)
        self.assertEqual(DictSubclass, sync_cx._Connection__document_class)

        # Make sure sync connection works
        self.assertEqual(
            {'_id': 5, 's': hex(5)},
            sync_cx.test.test_collection.find_one({'_id': 5}))

    @async_test_engine()
    def test_open_sync(self):
        loop = ioloop.IOLoop.instance()
        cx = motor.MotorConnection(host, port)
        self.assertFalse(cx.connected)

        # open_sync() creates a special IOLoop just to run the connection
        # code to completion
        self.assertEqual(cx, cx.open_sync())
        self.assertTrue(cx.connected)

        # IOLoop was restored?
        self.assertEqual(loop, cx.io_loop)

        # Really connected?
        result = yield motor.Op(cx.admin.command, "buildinfo")
        self.assertEqual(int, type(result['bits']))

        yield motor.Op(cx.test.test_collection.insert,
            {'_id': 'test_open_sync'})
        doc = yield motor.Op(
            cx.test.test_collection.find({'_id': 'test_open_sync'}).next)
        self.assertEqual('test_open_sync', doc['_id'])

    def test_open_sync_custom_io_loop(self):
        # Check that we can create a MotorConnection with a custom IOLoop, then
        # call open_sync(), which uses a new loop, and the custom loop is
        # restored.
        loop = puritanical.PuritanicalIOLoop()
        cx = motor.MotorConnection(host, port, io_loop=loop)
        self.assertEqual(cx, cx.open_sync())
        self.assertTrue(cx.connected)

        # Custom loop restored?
        self.assertEqual(loop, cx.io_loop)

        @async_test_engine(io_loop=loop)
        def test(self):
            # Custom loop works?
            yield AssertEqual(
                {'_id': 17, 's': hex(17)},
                cx.test.test_collection.find({'_id': 17}).next)

            yield AssertEqual(
                {'_id': 37, 's': hex(37)},
                cx.test.test_collection.find({'_id': 37}).next)

        test(self)

    def test_custom_io_loop(self):
        self.assertRaises(
            TypeError,
            lambda: motor.MotorConnection(host, port, io_loop='foo')
        )

        loop = puritanical.PuritanicalIOLoop()

        @async_test_engine(io_loop=loop)
        def test(self):
            # Make sure we can do async things with the custom loop
            cx = motor.MotorConnection(host, port, io_loop=loop)
            yield AssertEqual(cx, cx.open)
            self.assertTrue(cx.connected)
            doc = yield motor.Op(
                cx.test.test_collection.find({'_id': 17}).next)
            self.assertEqual({'_id': 17, 's': hex(17)}, doc)

        test(self)

    def test_database_named_delegate(self):
        cx = self.motor_connection(host, port)
        self.assertTrue(isinstance(cx.delegate, pymongo.connection.Connection))
        self.assertTrue(isinstance(cx['delegate'], motor.MotorDatabase))

    def test_copy_db_argument_checking(self):
        cx = self.motor_connection(host, port)

        self.assertRaises(TypeError, cx.copy_database, 4, "foo")
        self.assertRaises(TypeError, cx.copy_database, "foo", 4)

        self.assertRaises(
            pymongo.errors.InvalidName, cx.copy_database, "foo", "$foo")

    @async_test_engine(timeout_sec=60)
    def test_copy_db(self):
        # 1. Drop old test DBs
        # 2. Copy a test DB N times at once (we need to do it many times at
        #   once to make sure that GreenletPool's start_request() is properly
        #   isolating operations from each other)
        # 3. Create a username and password
        # 4. Copy a database using name and password
        is_ms = server_is_master_with_slave(self.sync_cx)
        ncopies = 20
        nrange = list(range(ncopies))
        test_db_names = ['pymongo_test%s' % i for i in nrange]
        cx = self.motor_connection(host, port)

        def check_copydb_results():
            db_names = self.sync_cx.database_names()
            for test_db_name in test_db_names:
                self.assertTrue(test_db_name in db_names)
                result = self.sync_cx[test_db_name].test.find_one()
                self.assertTrue(result, "No results in %s" % test_db_name)
                self.assertEqual("bar", result.get("foo"),
                    "Wrong result from %s: %s" % (test_db_name, result))

        def drop_all():
            for test_db_name in test_db_names:
                # Setup code has configured a short timeout, and the copying
                # has put Mongo under enough load that we risk timeouts here
                # unless we override.
                self.sync_cx[test_db_name]['$cmd'].find_one(
                    {'dropDatabase': 1}, network_timeout=30)

            if not is_ms:
                # Due to SERVER-2329, databases may not disappear from a master in a
                # master-slave pair
                db_names = self.sync_cx.database_names()
                for test_db_name in test_db_names:
                    self.assertFalse(
                        test_db_name in db_names,
                        "%s not dropped" % test_db_name)

        # 1. Drop old test DBs
        yield motor.Op(cx.drop_database, 'pymongo_test')
        drop_all()

        # 2. Copy a test DB N times at once
        yield motor.Op(cx.pymongo_test.test.insert, {"foo": "bar"})
        for test_db_name in test_db_names:
            cx.copy_database("pymongo_test", test_db_name,
                callback=(yield gen.Callback(key=test_db_name)))

        yield motor.WaitAllOps(test_db_names)
        check_copydb_results()

        drop_all()

        # 3. Create a username and password
        yield motor.Op(cx.pymongo_test.add_user, "mike", "password")

        yield AssertRaises(
            pymongo.errors.OperationFailure,
            cx.copy_database, "pymongo_test", "pymongo_test0",
            username="foo", password="bar")

        yield AssertRaises(
            pymongo.errors.OperationFailure, cx.copy_database,
            "pymongo_test", "pymongo_test0",
            username="mike", password="bar")

        # 4. Copy a database using name and password
        for test_db_name in test_db_names:
            cx.copy_database(
                "pymongo_test", test_db_name,
                username="mike", password="password",
                callback=(yield gen.Callback(test_db_name)))

        yield motor.WaitAllOps(test_db_names)
        check_copydb_results()

        drop_all()

    def test_timeout(self):
        # Launch two slow find_ones. The one with a timeout should get an error
        loop = ioloop.IOLoop.instance()
        no_timeout = self.motor_connection(host, port)
        timeout = self.motor_connection(host, port, socketTimeoutMS=100)

        results = []
        query = {
            '$where': delay(0.5),
            '_id': 1,
        }

        def callback(result, error):
            results.append({'result': result, 'error': error})

        no_timeout.test.test_collection.find_one(query, callback=callback)
        timeout.test.test_collection.find_one(query, callback=callback)

        self.assertEventuallyEqual(
            True,
            lambda: isinstance(
                results[0]['error'],
                pymongo.errors.AutoReconnect
            ) and results[0]['error'].message == 'timed out'
        )

        self.assertEventuallyEqual(
            {'_id':1, 's':hex(1)},
            lambda: results[1]['result']
        )

        loop.start()

        # Make sure the delay completes before we call tearDown() and try to
        # drop the collection
        time.sleep(0.5)

    @async_test_engine()
    def test_connection_failure(self):
        exc = None
        try:
            # Assuming there isn't anything actually running on this port
            yield motor.Op(motor.MotorConnection('localhost', 8765).open)
        except Exception, e:
            exc = e

        self.assertTrue(isinstance(exc, ConnectionFailure))

        # Tornado 2.3 IOStream stores the error that closed it
        if tornado.version_info >= (2, 3):
            # Are these assumptions valid on Windows?
            self.assertTrue('Errno 61' in exc.message)
            self.assertTrue('Connection refused' in exc.message)
        else:
            self.assertTrue('error' in exc.message)

    @async_test_engine()
    def test_connection_timeout(self):
        exc = None
        start = time.time()
        try:
            # Assuming asdf.com isn't running an open mongod on this port
            yield motor.Op(motor.MotorConnection(
                'asdf.com', 8765, connectTimeoutMS=1000).open)
        except Exception, e:
            exc = e

        self.assertTrue(isinstance(exc, AutoReconnect))
        connection_duration = time.time() - start
        self.assertAlmostEqual(1, connection_duration, delta=0.25)

    @async_test_engine()
    def test_max_pool_size_validation(self):
        cx = motor.MotorConnection(host=host, port=port, max_pool_size=-1)
        yield AssertRaises(ConfigurationError, cx.open)

        cx = motor.MotorConnection(host=host, port=port, max_pool_size='foo')
        yield AssertRaises(ConfigurationError, cx.open)

        c = motor.MotorConnection(host=host, port=port, max_pool_size=100)
        yield motor.Op(c.open)
        self.assertEqual(c.max_pool_size, 100)

    def test_requests(self):
        cx = self.motor_connection(host, port)
        for method in 'start_request', 'in_request', 'end_request':
            self.assertRaises(NotImplementedError, getattr(cx, method))

    def test_high_concurrency(self):
        loop = ioloop.IOLoop.instance()
        self.sync_db.insert_collection.drop()
        self.assertEqual(200, self.sync_coll.count())
        cx = self.motor_connection(host, port).open_sync()
        collection = cx.test.test_collection

        concurrency = 150
        ndocs = [0]
        ninserted = [0]

        def each(result, error):
            if error:
                raise error

            # Final call to each() has result None
            if result:
                ndocs[0] += 1

                # Part-way through, start an insert
                if ndocs[0] == (200 * concurrency) / 3:
                    cx.test.insert_collection.insert(
                        {'foo': 'bar'}, callback=inserted)

        for _ in range(concurrency):
            collection.find().each(each)

        def inserted(result, error):
            if error:
                raise error

            ninserted[0] += 1
            if ninserted[0] < 100:
                cx.test.insert_collection.insert(
                    {'foo': 'bar'}, callback=inserted)

        self.assertEventuallyEqual(
            200 * concurrency, lambda: ndocs[0], timeout_sec=60)

        self.assertEventuallyEqual(
            100, lambda: ninserted[0], timeout_sec=60)

        loop.start()

        self.assertEqual(100, self.sync_db.insert_collection.count())
        self.sync_db.insert_collection.drop()


if __name__ == '__main__':
    unittest.main()
