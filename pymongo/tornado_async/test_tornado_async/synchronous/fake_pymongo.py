from collections import deque
import inspect
import os
import time
from tornado.ioloop import IOLoop

# TODO doc WTF this module does
# TODO sometimes I refer to things as 'async', sometimes as 'tornado' -- maybe
# everything should be called 'motor'?

import pymongo as sync_pymongo
from pymongo.tornado_async import async
from pymongo.errors import ConnectionFailure, TimeoutError, OperationFailure

# So that synchronous unittests can import these names from fake_pymongo,
# thinking it's really pymongo
from pymongo import ASCENDING, DESCENDING, GEO2D, GEOHAYSTACK, ReadPreference


__all__ = [
    'ASCENDING', 'DESCENDING', 'GEO2D', 'GEOHAYSTACK', 'ReadPreference',
    'Connection', 'ReplicaSetConnection', 'Database', 'Collection',
    'Cursor',
]


timeout_sec = float(os.environ.get('TIMEOUT_SEC', 5))

#
#class StopAndFail(object):
#    # TODO: doc
#    # TODO: unnecessary with IOLoop.remove_timeout()?
#    def __init__(self, exc):
#        self.exc = exc
#        self.abort = False
#
#    def __call__(self, *args, **kwargs):
#        if not self.abort:
#            IOLoop.instance().stop()
#            raise self.exc
#

# TODO: better name or iface, document
def loop_timeout(kallable, exc=None, seconds=timeout_sec):
    loop = IOLoop.instance()
    outcome = {}

    def raise_timeout_err():
        loop.stop()
        outcome['error'] = exc or TimeoutError("timeout")

    timeout = loop.add_timeout(time.time() + seconds, raise_timeout_err)

    def callback(result, error):
        loop.stop()
        loop.remove_timeout(timeout)
        outcome['result'] = result
        outcome['error'] = error

    kallable(callback)
    IOLoop.instance().start()
    if outcome.get('error'):
        raise outcome['error']

    return outcome['result']


# Methods that don't take a 'safe' argument
methods_without_safe_arg = {}

for klass in (
    sync_pymongo.connection.Connection,
    sync_pymongo.database.Database,
    sync_pymongo.collection.Collection,
):
    for method_name, method in inspect.getmembers(
        klass,
        inspect.ismethod
    ):
        if 'safe' not in inspect.getargspec(method).args:
            methods_without_safe_arg.setdefault(
                'Tornado' + klass.__name__, set()
            ).add(method.func_name)


def synchronize(async_method):
    """
    @param async_method:  An asynchronous method defined on a TornadoConnection,
                          TornadoDatabase, etc.
    @return:              A synchronous wrapper around the method
    """
    def synchronized_method(*args, **kwargs):
        assert 'callback' not in kwargs
        class_name = async_method.im_self.__class__.__name__
        has_safe_arg = (
            async_method.func_name not in methods_without_safe_arg[class_name]
        )

        if 'safe' not in kwargs and has_safe_arg:
            # By default, Motor passes safe=True if there's a callback, but
            # we don't want that, so we explicitly override.
            kwargs['safe'] = False

        rv = None
        try:
            rv = loop_timeout(
                lambda cb: async_method(*args, callback=cb, **kwargs),
            )
        except OperationFailure:
            # Ignore OperationFailure for unsafe writes; synchronous pymongo
            # wouldn't have known the operation failed.
            if kwargs.get('safe') or not has_safe_arg:
                raise

        return rv

    return synchronized_method


class Fake(object):
    """
    Wraps a TornadoConnection, TornadoDatabase, or TornadoCollection and
    makes it act like the synchronous pymongo equivalent
    """
    def __getattr__(self, name):
        async_obj = getattr(self, self.async_attr, None)

        # This if-else seems to replicate the logic of getattr(), except,
        # weirdly, for non-ASCII names like in
        # TestCollection.test_messages_with_unicode_collection_names().
        if name in dir(async_obj):
            async_attr = getattr(async_obj, name)
        else:
            async_attr = async_obj.__getattr__(name)

        if name in self.async_ops:
            # async_attr is an async method on a TornadoConnection or something
            return synchronize(async_attr)
        else:
            # If this is like connection.db, or db.test, then wrap the
            # outgoing object in a Fake
            if isinstance(async_attr, async.TornadoDatabase):
                return Database(self, name)
            elif isinstance(async_attr, async.TornadoCollection):
                if isinstance(self, Collection):
                    # Dotted access, like db.test.mike
                    return Collection(self.database, self.name + '.' + name)
                else:
                    return Collection(self, name)
            else:
                # Non-socket operation on a pymongo Database, like
                # database.system_js or _fix_outgoing()
                return async_attr

    __getitem__ = __getattr__


class Connection(Fake):
    async_attr = '_tconn'
    async_connection_class = async.TornadoConnection
    async_ops = async.TornadoConnection.async_ops

    def __init__(self, host=None, port=None, *args, **kwargs):
        self.host = host
        self.port = port
        self._tconn = self.async_connection_class(host, port, *args, **kwargs)

        # Try to connect the TornadoConnection before continuing
        exc = ConnectionFailure(
            "fake_pymongo.Connection: Can't connect to %s:%s" % (host, port)
        )
        loop_timeout(kallable=self._tconn.open, exc=exc)

        # For unittests that examine this attribute
        self.__pool = self._tconn.sync_connection._Connection__pool

    def drop_database(self, name_or_database):
        # Special case, since pymongo.Connection.drop_database does
        # isinstance(name_or_database, database.Database)
        if isinstance(name_or_database, Database):
            name_or_database = name_or_database._tdb.name

        drop = super(Connection, self).__getattr__('drop_database')
        return drop(name_or_database)

class ReplicaSetConnection(Connection):
    # fake_pymongo.ReplicaSetConnection is just like fake_pymongo.Connection,
    # except it wraps a TornadoReplicaSetConnection instead of a
    # TornadoConnection.
    async_connection_class = async.TornadoReplicaSetConnection


class Database(Fake):
    async_attr = '_tdb'
    async_ops = async.TornadoDatabase.async_ops

    def __init__(self, connection, name):
        assert isinstance(connection, Connection), (
            "Expected Connection, got %s" % repr(connection)
        )
        self.connection = connection

        # Get a TornadoDatabase
        self._tdb = getattr(connection._tconn, name)
        assert isinstance(self._tdb, async.TornadoDatabase)

class Collection(Fake):
    # If async_ops changes, we'll need to update this
    assert 'find' not in async.TornadoCollection.async_ops

    async_attr = '_tcoll'
    async_ops = async.TornadoCollection.async_ops.union(set([
        'find', 'map_reduce'
    ]))

    def __init__(self, database, name):
        assert isinstance(database, Database)
        # Get a TornadoCollection
        self._tcoll = database._tdb.__getattr__(name)
        assert isinstance(self._tcoll, async.TornadoCollection)

        self.database = database

    def find(self, *args, **kwargs):
        # Return a fake Cursor that wraps the call to TornadoCollection.find()
        return Cursor(self._tcoll, *args, **kwargs)

    def map_reduce(self, *args, **kwargs):
        # We need to override map_reduce specially, because we have to wrap the
        # TornadoCollection it returns in a fake Collection.
        fake_map_reduce = super(Collection, self).__getattr__('map_reduce')
        rv = fake_map_reduce(*args, **kwargs)
        if isinstance(rv, async.TornadoCollection):
            return Collection(self.database, rv.name)
        else:
            return rv

    def __cmp__(self, other):
        return cmp(self._tcoll, other._tcoll)

    # Delegate to TornadoCollection's uuid_subtype property -- TornadoCollection
    # in turn delegates to pymongo.collection.Collection. This is all to get
    # test_uuid_subtype to pass.
    def __get_uuid_subtype(self):
        return self._tcoll.uuid_subtype

    def __set_uuid_subtype(self, subtype):
        self._tcoll.uuid_subtype = subtype

    uuid_subtype = property(__get_uuid_subtype, __set_uuid_subtype)

class Cursor(object):
    def __init__(self, tornado_coll, *args, **kwargs):
        self.tornado_coll = tornado_coll
        self.args = args
        self.kwargs = kwargs
        self.tornado_cursor = None
        self.data = deque()

    def __iter__(self):
        return self

    def _next_batch(self):
        outcome = {}

        if not self.tornado_cursor:
            # Start the query
            self.tornado_cursor = loop_timeout(
                kallable=lambda callback: self.tornado_coll.find(
                    *self.args, callback=callback, **self.kwargs
                ),
                outcome=outcome,
            )
        else:
            if not self.tornado_cursor.alive:
                raise StopIteration

            # Continue the query
            loop_timeout(
                kallable=self.tornado_cursor.get_more,
                outcome=outcome,
            )

        IOLoop.instance().start()

        if outcome.get('error'):
            raise outcome['error']

        self.data += outcome['result']

    def next(self):
        if self.data:
            return self.data.popleft()

        self._next_batch()

        if len(self.data):
            return self.data.popleft()
        else:
            raise StopIteration

    def count(self):
        command = {"query": self.spec, "fields": self.fields}
        outcome = {}
        loop_timeout(
            kallable=lambda callback: self.tornado_coll._tdb.command(
                "count", self.tornado_coll.name,
                allowable_errors=["ns missing"],
                callback=callback,
                **command
            ),
            outcome=outcome
        )

        IOLoop.instance().start()

        if outcome.get('error'):
            raise outcome['error']

        # TODO: remove?:
#        if outcome['result'].get("errmsg", "") == "ns missing":
#            return 0
        return int(outcome['result']['n'])

    def distinct(self, key):
        command = {"query": self.spec, "fields": self.fields, "key": key}
        outcome = {}
        loop_timeout(
            kallable=lambda callback: self.tornado_coll._tdb.command(
                "distinct", self.tornado_coll.name,
                callback=callback,
                **command
            ),
            outcome=outcome
        )

        IOLoop.instance().start()

        if outcome.get('error'):
            raise outcome['error']

        return outcome['result']['values']

    def explain(self):
        if '$query' not in self.spec:
            spec = {'$query': self.spec}
        else:
            spec = self.spec

        spec['$explain'] = True

        return synchronize(self.tornado_coll.find)(spec)[0]
