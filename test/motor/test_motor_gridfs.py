# -*- coding: utf-8 -*-
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

"""Test GridFS with Motor, an asynchronous driver for MongoDB and Tornado."""

import motor
if not motor.requirements_satisfied:
    from nose.plugins.skip import SkipTest
    raise SkipTest("Tornado or greenlet not installed")

import unittest
from functools import partial

from bson.py3compat import b, StringIO
from gridfs.errors import FileExists, NoFile
from pymongo.errors import AutoReconnect
from pymongo.read_preferences import ReadPreference

from test.test_replica_set_connection import TestConnectionReplicaSetBase
from test.motor import (
    MotorTest, async_test_engine, host, port, AssertEqual, AssertRaises, puritanical)


class MotorGridfsTest(MotorTest):
    def _reset(self):
        self.sync_db.drop_collection("fs.files")
        self.sync_db.drop_collection("fs.chunks")
        self.sync_db.drop_collection("alt.files")
        self.sync_db.drop_collection("alt.chunks")

    def setUp(self):
        super(MotorGridfsTest, self).setUp()
        self._reset()
        self.db = self.motor_connection(host, port).open_sync().test

    def tearDown(self):
        self._reset()
        super(MotorGridfsTest, self).tearDown()

    @async_test_engine()
    def test_gridfs(self):
        self.assertRaises(TypeError, motor.MotorGridFS, "foo")
        self.assertRaises(TypeError, motor.MotorGridFS, 5)
        fs = yield motor.Op(motor.MotorGridFS(self.db).open)

        # new_file should be an already-open MotorGridIn.
        gin = yield motor.Op(fs.new_file, _id=1, filename='foo')
        self.assertTrue(gin.delegate)
        yield motor.Op(gin.write, 'a') # No error
        yield motor.Op(gin.close)

        # get, get_version, and get_last_version should be already-open
        # MotorGridOut instances
        gout = yield motor.Op(fs.get, 1)
        self.assertTrue(gout.delegate)
        gout = yield motor.Op(fs.get_version, 'foo')
        self.assertTrue(gout.delegate)
        gout = yield motor.Op(fs.get_last_version, 'foo')
        self.assertTrue(gout.delegate)

    @async_test_engine()
    def test_gridfs_callback(self):
        fs = motor.MotorGridFS(self.db)
        self.check_callback_handling(fs.open, False)

        fs = yield motor.Op(motor.MotorGridFS(self.db).open)
        self.check_callback_handling(fs.new_file, True)
        self.check_callback_handling(fs.get, True)
        self.check_callback_handling(fs.get_version, True)
        self.check_callback_handling(fs.get_last_version, True)
        self.check_callback_handling(partial(fs.put, 'a'), False)
        self.check_callback_handling(partial(fs.delete, 1), False)
        self.check_callback_handling(fs.list, True)
        self.check_callback_handling(fs.exists, True)

    def test_custom_io_loop(self):
        loop = puritanical.PuritanicalIOLoop()

        @async_test_engine(io_loop=loop)
        def test(self):
            cx = motor.MotorConnection(host, port, io_loop=loop)
            yield motor.Op(cx.open)

            fs = motor.MotorGridFS(cx.test)
            self.assertFalse(fs.delegate)

            # Make sure we can do async things with the custom loop
            yield motor.Op(fs.open)
            self.assertTrue(fs.delegate)
            file_id0 = yield motor.Op(fs.put, 'foo')

            gridin1 = yield motor.Op(fs.new_file)
            yield motor.Op(gridin1.write, 'bar')
            yield motor.Op(gridin1.close)
            file_id1 = gridin1._id

            gridin2 = motor.MotorGridIn(cx.test.fs, filename='fn')
            yield motor.Op(gridin2.open)
            yield motor.Op(gridin2.write, 'baz')
            yield motor.Op(gridin2.close)

            gridout0 = yield motor.Op(fs.get, file_id0)
            yield AssertEqual('foo', gridout0.read)

            gridout1 = motor.MotorGridOut(cx.test.fs, file_id1)
            yield motor.Op(gridout1.open)
            yield AssertEqual('bar', gridout1.read)

            gridout2 = yield motor.Op(fs.get_last_version, 'fn')
            yield AssertEqual('baz', gridout2.read)

        test(self)

    @async_test_engine()
    def test_basic(self):
        fs = yield motor.Op(motor.MotorGridFS(self.db).open)
        oid = yield motor.Op(fs.put, b("hello world"))
        out = yield motor.Op(fs.get, oid)
        yield AssertEqual(b("hello world"), out.read)
        yield AssertEqual(1, self.db.fs.files.count)
        yield AssertEqual(1, self.db.fs.chunks.count)

        yield motor.Op(fs.delete, oid)
        yield AssertRaises(NoFile, fs.get, oid)
        yield AssertEqual(0, self.db.fs.files.count)
        yield AssertEqual(0, self.db.fs.chunks.count)

        yield AssertRaises(NoFile, fs.get, "foo")
        yield AssertEqual("foo", fs.put, b("hello world"), _id="foo")
        gridout = yield motor.Op(fs.get, "foo")
        yield AssertEqual(b("hello world"), gridout.read)

    @async_test_engine()
    def test_list(self):
        fs = yield motor.Op(motor.MotorGridFS(self.db).open)
        self.assertEqual([], (yield motor.Op(fs.list)))
        yield motor.Op(fs.put, b("hello world"))
        self.assertEqual([], (yield motor.Op(fs.list)))

        yield motor.Op(fs.put, b(""), filename="mike")
        yield motor.Op(fs.put, b("foo"), filename="test")
        yield motor.Op(fs.put, b(""), filename="hello world")

        self.assertEqual(set(["mike", "test", "hello world"]),
                         set((yield motor.Op(fs.list))))

    @async_test_engine()
    def test_alt_collection(self):
        alt = yield motor.Op(motor.MotorGridFS(self.db, 'alt').open)
        oid = yield motor.Op(alt.put, b("hello world"))
        gridout = yield motor.Op(alt.get, oid)
        self.assertEqual(b("hello world"), (yield motor.Op(gridout.read)))
        self.assertEqual(1, (yield motor.Op(self.db.alt.files.count)))
        self.assertEqual(1, (yield motor.Op(self.db.alt.chunks.count)))

        alt.delete(oid)
        yield AssertRaises(NoFile, alt.get, oid)
        self.assertEqual(0, (yield motor.Op(self.db.alt.files.count)))
        self.assertEqual(0, (yield motor.Op(self.db.alt.chunks.count)))

        yield AssertRaises(NoFile, alt.get, "foo")
        oid = yield motor.Op(alt.put, b("hello world"), _id="foo")
        self.assertEqual("foo", oid)
        gridout = yield motor.Op(alt.get, "foo")
        self.assertEqual(b("hello world"), (yield motor.Op(gridout.read)))

        yield motor.Op(alt.put, b(""), filename="mike")
        yield motor.Op(alt.put, b("foo"), filename="test")
        yield motor.Op(alt.put, b(""), filename="hello world")

        self.assertEqual(set(["mike", "test", "hello world"]),
                         set((yield motor.Op(alt.list))))

    @async_test_engine()
    def test_put_filelike(self):
        fs = yield motor.Op(motor.MotorGridFS(self.db).open)
        oid = yield motor.Op(fs.put, StringIO(b("hello world")), chunk_size=1)
        self.assertEqual(11, (yield motor.Op(self.db.fs.chunks.count)))
        gridout = yield motor.Op(fs.get, oid)
        self.assertEqual(b("hello world"), (yield motor.Op(gridout.read)))

    @async_test_engine()
    def test_put_duplicate(self):
        fs = yield motor.Op(motor.MotorGridFS(self.db).open)
        oid = yield motor.Op(fs.put, b("hello"))
        yield AssertRaises(FileExists, fs.put, "world", _id=oid)


class TestGridfsReplicaSet(MotorTest, TestConnectionReplicaSetBase):
    @async_test_engine()
    def test_gridfs_replica_set(self):
        rsc = motor.MotorReplicaSetConnection(
            host=host, port=port,
            w=self.w, wtimeout=5000,
            read_preference=ReadPreference.SECONDARY,
            replicaSet=self.name
        ).open_sync()

        try:
            fs = yield motor.Op(motor.MotorGridFS(rsc.pymongo_test).open)
            oid = yield motor.Op(fs.put, b('foo'))
            gridout = yield motor.Op(fs.get, oid)
            content = yield motor.Op(gridout.read)
            self.assertEqual(b('foo'), content)
        finally:
            rsc.close()

    @async_test_engine()
    def test_gridfs_secondary(self):
        primary_host, primary_port = self.primary
        primary_connection = motor.MotorConnection(
            primary_host, primary_port).open_sync()

        secondary_host, secondary_port = self.secondaries[0]
        for secondary_connection in [
            motor.MotorConnection(
                secondary_host, secondary_port, slave_okay=True).open_sync(),
            motor.MotorConnection(secondary_host, secondary_port,
                read_preference=ReadPreference.SECONDARY).open_sync(),
        ]:
            yield motor.Op(
                primary_connection.pymongo_test.drop_collection, "fs.files")
            yield motor.Op(
                primary_connection.pymongo_test.drop_collection, "fs.chunks")

            # Should detect it's connected to secondary and not attempt to
            # create index
            fs = yield motor.Op(
                motor.MotorGridFS(secondary_connection.pymongo_test).open)

            # This won't detect secondary, raises error
            yield AssertRaises(AutoReconnect, fs.put, b('foo'))

    def tearDown(self):
        rsc = self._get_connection()
        rsc.pymongo_test.drop_collection('fs.files')
        rsc.pymongo_test.drop_collection('fs.chunks')
        rsc.close()


if __name__ == "__main__":
    unittest.main()
