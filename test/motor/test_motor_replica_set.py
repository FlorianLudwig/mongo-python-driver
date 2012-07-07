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
import socket
import unittest

import motor
from test.motor import puritanical

if not motor.requirements_satisfied:
    from nose.plugins.skip import SkipTest
    raise SkipTest("Tornado or greenlet not installed")

from tornado import ioloop, iostream

from test.motor import (
    MotorTest, async_test_engine, AssertEqual, AssertRaises, host, port )
import pymongo.errors
import pymongo.replica_set_connection
from test.test_replica_set_connection import TestConnectionReplicaSetBase


class MotorReplicaSetTest(MotorTest, TestConnectionReplicaSetBase):
    def setUp(self):
        # TODO: Make TestConnectionReplicaSetBase cooperative
        TestConnectionReplicaSetBase.setUp(self)
        MotorTest.setUp(self)

    @async_test_engine()
    def test_replica_set_connection(self):
        cx = motor.MotorReplicaSetConnection(host, port, replicaSet='repl0')

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
        self.assertTrue(isinstance(cx.delegate._ReplicaSetConnection__monitor,
            motor.MotorReplicaSetMonitor))
        self.assertEqual(ioloop.IOLoop.instance(),
            cx.delegate._ReplicaSetConnection__monitor.io_loop)

    def test_connection_callback(self):
        cx = motor.MotorReplicaSetConnection(host, port, replicaSet='repl0')
        self.check_optional_callback(cx.open)

    @async_test_engine()
    def test_open_sync(self):
        loop = ioloop.IOLoop.instance()
        cx = motor.MotorReplicaSetConnection(host, port, replicaSet='repl0')
        self.assertFalse(cx.connected)

        # open_sync() creates a special IOLoop just to run the connection
        # code to completion
        self.assertEqual(cx, cx.open_sync())
        self.assertTrue(cx.connected)

        # IOLoop was restored?
        self.assertEqual(loop, cx.io_loop)
        self.assertTrue(isinstance(cx.delegate._ReplicaSetConnection__monitor,
            motor.MotorReplicaSetMonitor))
        self.assertEqual(loop,
            cx.delegate._ReplicaSetConnection__monitor.io_loop)

        # Really connected?
        result = yield motor.Op(cx.admin.command, "buildinfo")
        self.assertEqual(int, type(result['bits']))

        yield motor.Op(cx.test.test_collection.insert,
            {'_id': 'test_open_sync'})
        doc = yield motor.Op(
            cx.test.test_collection.find({'_id': 'test_open_sync'}).next)
        self.assertEqual('test_open_sync', doc['_id'])

    def test_open_sync_custom_io_loop(self):
        # Check that we can create a MotorReplicaSetConnection with a custom
        # IOLoop, then call open_sync(), which uses a new loop, and the custom
        # loop is restored.
        loop = puritanical.PuritanicalIOLoop()
        cx = motor.MotorReplicaSetConnection(
            host, port, replicaSet='repl0', io_loop=loop)
        self.assertEqual(cx, cx.open_sync())
        self.assertTrue(cx.connected)

        # Custom loop restored?
        self.assertEqual(loop, cx.io_loop)
        self.assertTrue(isinstance(cx.delegate._ReplicaSetConnection__monitor,
            motor.MotorReplicaSetMonitor))
        self.assertEqual(loop,
            cx.delegate._ReplicaSetConnection__monitor.io_loop)

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
            lambda: motor.MotorReplicaSetConnection(
                host, port, replicaSet='repl0', io_loop='foo')
        )

        loop = puritanical.PuritanicalIOLoop()

        @async_test_engine(io_loop=loop)
        def test(self):
            # Make sure we can do async things with the custom loop
            cx = motor.MotorReplicaSetConnection(
                host, port, replicaSet='repl0', io_loop=loop)
            yield AssertEqual(cx, cx.open)
            self.assertTrue(cx.connected)
            self.assertTrue(isinstance(
                cx.delegate._ReplicaSetConnection__monitor,
                motor.MotorReplicaSetMonitor))
            self.assertEqual(loop,
                cx.delegate._ReplicaSetConnection__monitor.io_loop)

            doc = yield motor.Op(
                cx.test.test_collection.find({'_id': 17}).next)
            self.assertEqual({'_id': 17, 's': hex(17)}, doc)

        test(self)

    @async_test_engine()
    def test_sync_connection(self):
        class DictSubclass(dict):
            pass

        args = ['localhost:27017']
        kwargs = dict(
            connectTimeoutMS=1000, socketTimeoutMS=1500, max_pool_size=23,
            document_class=DictSubclass, tz_aware=True, replicaSet='repl0')

        cx = yield motor.Op(motor.MotorReplicaSetConnection(
            *args, **kwargs).open)
        sync_cx = cx.sync_connection()
        self.assertTrue(isinstance(
            sync_cx, pymongo.replica_set_connection.ReplicaSetConnection))
        self.assertFalse(isinstance(sync_cx._ReplicaSetConnection__monitor,
            motor.MotorReplicaSetMonitor))
        self.assertEqual(1000,
            sync_cx._ReplicaSetConnection__conn_timeout * 1000.0)
        self.assertEqual(1500,
            sync_cx._ReplicaSetConnection__net_timeout * 1000.0)
        self.assertEqual(23, sync_cx.max_pool_size)
        self.assertEqual(True, sync_cx._ReplicaSetConnection__tz_aware)
        self.assertEqual(DictSubclass,
            sync_cx._ReplicaSetConnection__document_class)

        # Make sure sync connection works
        self.assertEqual(
            {'_id': 5, 's': hex(5)},
            sync_cx.test.test_collection.find_one({'_id': 5}))

    @async_test_engine()
    def test_auto_reconnect_exception_when_read_preference_is_secondary(self):
        cx = motor.MotorReplicaSetConnection(
            'localhost:27017', replicaSet='repl0')

        yield motor.Op(cx.open)
        db = cx.pymongo_test

        def raise_socket_error(self, data, callback):
            ioloop.IOLoop.instance().add_callback(
                functools.partial(callback, None, socket.error('foo')))

        old_write = iostream.IOStream.write
        iostream.IOStream.write = raise_socket_error

        try:
            cursor = db.test.find(
                read_preference=pymongo.ReadPreference.SECONDARY)

            yield AssertRaises(pymongo.errors.AutoReconnect, cursor.each)
        finally:
            iostream.IOStream.write = old_write


if __name__ == '__main__':
    unittest.main()
