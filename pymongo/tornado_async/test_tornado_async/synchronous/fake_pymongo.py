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

"""Fake PyMongo implementation built on top of Motor, for the sole purpose of
checking that Motor passes the same unittests as PyMongo.

DO NOT USE THIS MODULE.
"""

import functools
import inspect
import os
import time
from tornado.ioloop import IOLoop

# TODO doc WTF this module does
# TODO sometimes I refer to things as 'async', sometimes as 'tornado' -- maybe
# everything should be called 'motor'?

from pymongo import son_manipulator, common
from pymongo.tornado_async import async
from pymongo.errors import ConnectionFailure, TimeoutError, OperationFailure


# So that synchronous unittests can import these names from fake_pymongo,
# thinking it's really pymongo
from pymongo import (
    ASCENDING, DESCENDING, GEO2D, GEOHAYSTACK, ReadPreference,
    ALL, helpers, OFF, SLOW_ONLY
)

from pymongo.replica_set_connection import _partition_node

__all__ = [
    'ASCENDING', 'DESCENDING', 'GEO2D', 'GEOHAYSTACK', 'ReadPreference',
    'Connection', 'ReplicaSetConnection', 'Database', 'Collection',
    'Cursor', 'ALL', 'helpers', 'OFF', 'SLOW_ONLY', '_partition_node'
]


# TODO: doc
timeout_sec = float(os.environ.get('TIMEOUT_SEC', 5))


# TODO: better name or iface, document
# TODO: maybe just get rid of this and put it all in synchronize()?
def loop_timeout(kallable, exc=None, seconds=timeout_sec, name="<anon>"):
    loop = IOLoop.instance()
    loop._callbacks[:] = []
    outcome = {}

    def raise_timeout_err():
        loop.stop()
        outcome['error'] = (exc or TimeoutError("timeout"))

    timeout = loop.add_timeout(time.time() + seconds, raise_timeout_err)

    def callback(result, error):
        loop.stop()
        loop.remove_timeout(timeout)
        outcome['result'] = result
        outcome['error'] = error

        # Special case: return False to stop iteration if this callback is
        # being used in Motor's find().each()
        return False

    kallable(callback=callback)
    assert not loop.running(), "Loop already running in method %s" % name
    loop.start()
    if outcome.get('error'):
        raise outcome['error']

    return outcome['result']


class Sync(object):
    def __init__(self, name, has_safe_arg):
        self.name = name
        self.has_safe_arg = has_safe_arg

    def __get__(self, obj, objtype):
        async_method = getattr(obj.delegate, self.name)
        return synchronize(obj, async_method, has_safe_arg=self.has_safe_arg)


def synchronize(self, async_method, has_safe_arg):
    """
    @param self:                A Fake object, e.g. fake_pymongo Connection
    @param async_method:        Bound method of a TornadoConnection,
                                TornadoDatabase, etc.
    @param has_safe_arg:        Whether the method takes a 'safe' argument
    @return:                    A synchronous wrapper around the method
    """
    assert isinstance(self, Fake)

    @functools.wraps(async_method)
    def synchronized_method(*args, **kwargs):
        assert 'callback' not in kwargs

        # TODO: is this right?
        safe_arg_passed = (
            'safe' in kwargs or 'w' in kwargs or 'j' in kwargs
            or 'wtimeout' in kwargs or getattr(self.delegate, 'safe', False)
        )
        
        if not safe_arg_passed and has_safe_arg:
            # By default, Motor passes safe=True if there's a callback, but
            # we're emulating PyMongo, which defaults safe to False, so we
            # explicitly override.
            kwargs['safe'] = False

        rv = None
        try:
            # TODO: document that all Motor methods accept a callback, but only
            # some require them. Get that into Sphinx somehow.
            rv = loop_timeout(
                lambda callback: async_method(*args, callback=callback, **kwargs),
                name=async_method.func_name
            )
        except OperationFailure:
            # Ignore OperationFailure for unsafe writes; synchronous pymongo
            # wouldn't have known the operation failed.
            if safe_arg_passed or not has_safe_arg:
                raise

        return rv

    return synchronized_method


class FakeWrapReturnValue(async.DelegateProperty):
    def __get__(self, obj, objtype):
        # self.name is set by FakeMeta
        method = getattr(obj.delegate, self.name)

        def wrap_return_value(*args, **kwargs):
            rv = method(*args, **kwargs)
            # TODO: check for the other Motor classes and wrap them
            # in appropriate Fakes
            if isinstance(rv, async.TornadoCursor):
                return Cursor(rv)
            else:
                return rv

        return wrap_return_value


# TODO: change name to "Synchronized"? then the current Synchronized will need
# to be renamed. But "Fake" sounds too general for what this does, which is
# specifically to synchronize Motor. I like "Synchro" to extend the "Motor"
# naming scheme.
class FakeMeta(type):
    def __new__(cls, name, bases, attrs):
        # Create the class.
        new_class = type.__new__(cls, name, bases, attrs)

        delegate_class = new_class.__delegate_class__

        if delegate_class:
            delegated_attrs = {}

            for klass in reversed(inspect.getmro(delegate_class)):
                delegated_attrs.update(klass.__dict__)

            for attrname, delegate_attr in delegated_attrs.items():
                # If attrname is in attrs, it means the Fake has overridden
                # this attribute, e.g. Database.create_collection which is
                # special-cased.
                if attrname not in attrs:
                    if isinstance(delegate_attr, async.Async):
                        # Re-synchronize the method
                        sync_method = Sync(attrname, delegate_attr.has_safe_arg)
                        setattr(new_class, attrname, sync_method)
                    elif isinstance(delegate_attr, async.CallAndReturnSelf):
                        # Wrap Motor objects return from functions in Fakes
                        wrapper = FakeWrapReturnValue()
                        wrapper.name = attrname
                        setattr(new_class, attrname, wrapper)
                    elif isinstance(delegate_attr, async.DelegateProperty):
                        # Delegate the property from Fake to Motor
                        setattr(new_class, attrname, delegate_attr)

        # Set DelegateProperties' names
        for name, attr in attrs.items():
            if isinstance(attr, async.DelegateProperty):
                attr.name = name

        return new_class


class Fake(object):
    """
    Wraps a TornadoConnection, TornadoDatabase, or TornadoCollection and
    makes it act like the synchronous pymongo equivalent
    """
    __metaclass__ = FakeMeta
    __delegate_class__ = None

    def __cmp__(self, other):
        return cmp(self.delegate, other.delegate)


class Connection(Fake):
    HOST = 'localhost'
    PORT = 27017

    __delegate_class__ = async.TornadoConnection

    def __init__(self, host=None, port=None, *args, **kwargs):
        # Motor doesn't implement auto_start_request
        kwargs.pop('auto_start_request', None)

        # So that TestConnection.test_constants and test_types work
        self.host = host if host is not None else self.HOST
        self.port = port if port is not None else self.PORT
        self.delegate = self.__delegate_class__(
            self.host, self.port, *args, **kwargs
        )

        # Try to connect the TornadoConnection before continuing; raise
        # ConnectionFailure if it doesn't work.
        exc = ConnectionFailure(
            "fake_pymongo.Connection: Can't connect to %s:%s" % (host, port)
        )

        loop_timeout(kallable=self.delegate.open, exc=exc)

    def drop_database(self, name_or_database):
        # Special case, since pymongo Connection.drop_database does
        # isinstance(name_or_database, database.Database)
        if isinstance(name_or_database, Database):
            name_or_database = name_or_database.delegate

        synchronize(self, self.delegate.drop_database, has_safe_arg=False)(name_or_database)

    def start_request(self):
        self.request = self.delegate.start_request()
        self.request.__enter__()

    def end_request(self):
        if self.request:
            self.request.__exit__(None, None, None)

    # TODO: document how this is implemented by Motor; use Motor's
    # is_locked(callback) instead of current_op(). Can we just delete this
    # property and it will Just Work?
    @property
    def is_locked(self):
        ops = self.admin.current_op()
        return bool(ops.get('fsyncLock', 0))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.delegate.disconnect()

    def __getattr__(self, name):
        # If this is like connection.db, then wrap the outgoing object in a Fake
        return Database(self, name)

    __getitem__ = __getattr__

class ReplicaSetConnection(Connection):
    # fake_pymongo.ReplicaSetConnection is just like fake_pymongo.Connection,
    # except it wraps a TornadoReplicaSetConnection instead of a
    # TornadoConnection.
    __delegate_class__ = async.TornadoReplicaSetConnection


class Database(Fake):
    __delegate_class__ = async.TornadoDatabase

    def __init__(self, connection, name):
        assert isinstance(connection, Connection), (
            "Expected Connection, got %s" % repr(connection)
        )
        self.connection = connection

        self.delegate = connection.delegate[name]
        assert isinstance(self.delegate, async.TornadoDatabase)

    def add_son_manipulator(self, manipulator):
        if isinstance(manipulator, son_manipulator.AutoReference):
            db = manipulator.database
            if isinstance(db, Database):
                manipulator.database = db.delegate.delegate

        self.delegate.add_son_manipulator(manipulator)

    # TODO: refactor, maybe something like fix_incoming or fix_outgoing?
    def drop_collection(self, name_or_collection):
        # Special case, since pymongo Database.drop_collection does
        # isinstance(name_or_collection, collection.Collection)
        if isinstance(name_or_collection, Collection):
            name_or_collection = name_or_collection.delegate

        return synchronize(self, self.delegate.drop_collection, has_safe_arg=False)(name_or_collection)

    # TODO: refactor
    def validate_collection(self, name_or_collection, *args, **kwargs):
        # Special case, since pymongo Database.validate_collection does
        # isinstance(name_or_collection, collection.Collection)
        if isinstance(name_or_collection, Collection):
            name_or_collection = name_or_collection.delegate

        return synchronize(self, self.delegate.validate_collection, has_safe_arg=False)(
            name_or_collection, *args, **kwargs
        )

    # TODO: refactor
    def create_collection(self, name, *args, **kwargs):
        # Special case, since TornadoDatabase.create_collection returns a
        # TornadoCollection
        collection = synchronize(self, self.delegate.create_collection, has_safe_arg=False)(
            name, *args, **kwargs
        )

        if isinstance(collection, async.TornadoCollection):
            collection = Collection(self, name)

        return collection

    def __getattr__(self, name):
        return Collection(self, name)

    __getitem__ = __getattr__


class Collection(Fake):
    __delegate_class__ = async.TornadoCollection

    def __init__(self, database, name):
        assert isinstance(database, Database)
        self.database = database

        self.delegate = database.delegate[name]
        assert isinstance(self.delegate, async.TornadoCollection)

    def find(self, *args, **kwargs):
        # Return a fake Cursor that wraps the TornadoCursor
        return Cursor(self.delegate.find(*args, **kwargs))

    # TODO: refactor
    def map_reduce(self, *args, **kwargs):
        # We need to override map_reduce specially, because we have to wrap the
        # TornadoCollection it returns in a fake Collection.
        rv = loop_timeout(
            lambda callback: self.delegate.map_reduce(*args, callback=callback, **kwargs)
        )

        if isinstance(rv, async.TornadoCollection):
            return Collection(self.database, rv.name)
        else:
            return rv

    def __getattr__(self, name):
        # Access to collections with dotted names, like db.test.mike
        return Collection(self.database, self.name + '.' + name)

    __getitem__ = __getattr__

    
class Cursor(Fake):
    __delegate_class__ = async.TornadoCursor

    close                               = async.ReadOnlyDelegateProperty()
    rewind                              = FakeWrapReturnValue()
    clone                               = FakeWrapReturnValue()

    def __init__(self, tornado_cursor):
        self.delegate = tornado_cursor

    def __iter__(self):
        return self

    def next(self):
        rv = loop_timeout(self.delegate.each)
        if rv is not None:
            return rv
        else:
            raise StopIteration

    # TODO: refactor these
    def where(self, code):
        self.delegate.where(code)
        return self

    def sort(self, *args, **kwargs):
        self.delegate.sort(*args, **kwargs)
        return self

    def explain(self):
        return loop_timeout(self.delegate.explain)

    def __getitem__(self, item):
        rv = loop_timeout(self.delegate.to_list)
        return rv[item]
