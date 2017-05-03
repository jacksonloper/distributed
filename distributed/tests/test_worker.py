from __future__ import print_function, division, absolute_import

from concurrent.futures import ThreadPoolExecutor
import logging
from numbers import Integral, Number
from operator import add
import os
import re
import shutil
import sys
import traceback

from dask import delayed
import pytest
from toolz import pluck, sliding_window
import tornado
from tornado import gen
from tornado.ioloop import TimeoutError

import distributed
from distributed.core import rpc, connect
from distributed.client import _wait
from distributed.scheduler import Scheduler
from distributed.metrics import time
from distributed.protocol import to_serialize
from distributed.protocol.pickle import dumps, loads
from distributed.sizeof import sizeof
from distributed.worker import Worker, error_message, logger, TOTAL_MEMORY
from distributed.utils import ignoring, tmpfile
from distributed.utils_test import (loop, inc, mul, gen_cluster, div, dec,
        slow, slowinc, throws, gen_test, readone)



def test_worker_ncores():
    from distributed.worker import _ncores
    w = Worker('127.0.0.1', 8019)
    try:
        assert w.executor._max_workers == _ncores
    finally:
        shutil.rmtree(w.local_dir)


@gen_cluster()
def test_str(s, a, b):
    assert a.address in str(a)
    assert a.address in repr(a)
    assert str(a.ncores) in str(a)
    assert str(a.ncores) in repr(a)
    assert str(len(a.executing)) in repr(a)


def test_identity():
    w = Worker('127.0.0.1', 8019)
    ident = w.identity(None)
    assert 'Worker' in ident['type']
    assert ident['scheduler'] == 'tcp://127.0.0.1:8019'
    assert isinstance(ident['ncores'], int)
    assert isinstance(ident['memory_limit'], Number)


def test_health():
    w = Worker('127.0.0.1', 8019)
    d = w.host_health()
    assert isinstance(d, dict)
    d = w.host_health()
    try:
        import psutil
    except ImportError:
        pass
    else:
        try:
            psutil.disk_io_counters()
        except RuntimeError:
            pass
        else:
            assert 'disk-read' in d
            assert 'disk-write' in d
        assert 'network-recv' in d
        assert 'network-send' in d


@gen_cluster(client=True)
def test_worker_bad_args(c, s, a, b):
    class NoReprObj(object):
        """ This object cannot be properly represented as a string. """
        def __str__(self):
            raise ValueError("I have no str representation.")
        def __repr__(self):
            raise ValueError("I have no repr representation.")

    x = c.submit(NoReprObj, workers=a.address)
    yield _wait(x)
    assert not a.executing
    assert a.data

    def bad_func(*args, **kwargs):
        1 / 0

    class MockLoggingHandler(logging.Handler):
        """Mock logging handler to check for expected logs."""

        def __init__(self, *args, **kwargs):
            self.reset()
            logging.Handler.__init__(self, *args, **kwargs)

        def emit(self, record):
            self.messages[record.levelname.lower()].append(record.getMessage())

        def reset(self):
            self.messages = {
                'debug': [],
                'info': [],
                'warning': [],
                'error': [],
                'critical': [],
            }

    hdlr = MockLoggingHandler()
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(hdlr)
    y = c.submit(bad_func, x, k=x, workers=b.address)
    yield _wait(y)

    assert not b.executing
    assert y.status == 'error'
    # Make sure job died because of bad func and not because of bad
    # argument.
    with pytest.raises(ZeroDivisionError):
        yield y._result()

    if sys.version_info[0] >= 3:
        tb = yield y._traceback()
        assert any('1 / 0' in line
                  for line in pluck(3, traceback.extract_tb(tb))
                  if line)
    assert "Compute Failed" in hdlr.messages['warning'][0]
    logger.setLevel(old_level)

    # Now we check that both workers are still alive.

    xx = c.submit(add, 1, 2, workers=a.address)
    yy = c.submit(add, 3, 4, workers=b.address)

    results = yield c._gather([xx, yy])

    assert tuple(results) == (3, 7)


@gen_cluster()
def dont_test_workers_update_center(s, a, b):
    aa = rpc(ip=a.ip, port=a.port)

    response = yield aa.update_data(data={'x': dumps(1), 'y': dumps(2)})
    assert response['status'] == 'OK'
    assert response['nbytes'] == {'x': sizeof(1), 'y': sizeof(2)}

    assert a.data == {'x': 1, 'y': 2}
    assert s.who_has == {'x': {a.address},
                         'y': {a.address}}
    assert s.has_what[a.address] == {'x', 'y'}

    yield aa.delete_data(keys=['x'], close=True)
    assert not s.who_has['x']
    assert all('x' not in s for s in c.has_what.values())

    aa.close_rpc()


@slow
@gen_cluster()
def dont_test_delete_data_with_missing_worker(c, a, b):
    bad = '127.0.0.1:9001'  # this worker doesn't exist
    c.who_has['z'].add(bad)
    c.who_has['z'].add(a.address)
    c.has_what[bad].add('z')
    c.has_what[a.address].add('z')
    a.data['z'] = 5

    cc = rpc(ip=c.ip, port=c.port)

    yield cc.delete_data(keys=['z'])  # TODO: this hangs for a while
    assert 'z' not in a.data
    assert not c.who_has['z']
    assert not c.has_what[bad]
    assert not c.has_what[a.address]

    cc.close_rpc()


@gen_cluster(client=True)
def test_upload_file(c, s, a, b):
    assert not os.path.exists(os.path.join(a.local_dir, 'foobar.py'))
    assert not os.path.exists(os.path.join(b.local_dir, 'foobar.py'))
    assert a.local_dir != b.local_dir

    aa = rpc(a.address)
    bb = rpc(b.address)
    yield [aa.upload_file(filename='foobar.py', data=b'x = 123'),
           bb.upload_file(filename='foobar.py', data='x = 123')]

    assert os.path.exists(os.path.join(a.local_dir, 'foobar.py'))
    assert os.path.exists(os.path.join(b.local_dir, 'foobar.py'))

    def g():
        import foobar
        return foobar.x

    future = c.submit(g, workers=a.address)
    result = yield future._result()
    assert result == 123

    yield a._close()
    yield b._close()
    aa.close_rpc()
    bb.close_rpc()
    assert not os.path.exists(os.path.join(a.local_dir, 'foobar.py'))


@gen_cluster(client=True)
def test_upload_egg(c, s, a, b):
    eggname = 'mytestegg-1.0.0-py3.4.egg'
    local_file = __file__.replace('test_worker.py', eggname)
    assert not os.path.exists(os.path.join(a.local_dir, eggname))
    assert not os.path.exists(os.path.join(b.local_dir, eggname))
    assert a.local_dir != b.local_dir

    aa = rpc(a.address)
    bb = rpc(b.address)
    with open(local_file, 'rb') as f:
        payload = f.read()
    yield [aa.upload_file(filename=eggname, data=payload),
           bb.upload_file(filename=eggname, data=payload)]

    assert os.path.exists(os.path.join(a.local_dir, eggname))
    assert os.path.exists(os.path.join(b.local_dir, eggname))

    def g(x):
        import testegg
        return testegg.inc(x)

    future = c.submit(g, 10, workers=a.address)
    result = yield future._result()
    assert result == 10 + 1

    yield a._close()
    yield b._close()
    aa.close_rpc()
    bb.close_rpc()
    assert not os.path.exists(os.path.join(a.local_dir, eggname))


@gen_cluster()
def test_broadcast(s, a, b):
    with rpc(s.address) as cc:
        results = yield cc.broadcast(msg={'op': 'ping'})
        assert results == {a.address: b'pong', b.address: b'pong'}


@gen_test()
def test_worker_with_port_zero():
    s = Scheduler()
    s.start(8007)
    w = Worker(s.ip, s.port)
    yield w._start()
    assert isinstance(w.port, int)
    assert w.port > 1024


@slow
def test_worker_waits_for_center_to_come_up(loop):
    @gen.coroutine
    def f():
        w = Worker('127.0.0.1', 8007)
        yield w._start()

    try:
        loop.run_sync(f, timeout=4)
    except TimeoutError:
        pass


@gen_cluster(client=True, ncores=[('127.0.0.1', 1)])
def test_worker_task_data(c, s, w):
    x = delayed(2)
    xx = c.persist(x)
    yield _wait(xx)
    assert w.data[x.key] == 2


def test_error_message():
    class MyException(Exception):
        def __init__(self, a, b):
            self.args = (a + b,)
        def __str__(self):
            return "MyException(%s)" % self.args

    msg = error_message(MyException('Hello', 'World!'))
    assert 'Hello' in str(msg['exception'])


@gen_cluster()
def test_gather(s, a, b):
    b.data['x'] = 1
    b.data['y'] = 2
    with rpc(a.address) as aa:
        resp = yield aa.gather(who_has={'x': [b.address], 'y': [b.address]})
        assert resp['status'] == 'OK'

        assert a.data['x'] == b.data['x']
        assert a.data['y'] == b.data['y']


def test_io_loop(loop):
    s = Scheduler(loop=loop)
    s.listen(0)
    assert s.io_loop is loop
    w = Worker(s.address, loop=loop)
    assert w.io_loop is loop


@gen_cluster(client=True, ncores=[])
def test_spill_to_disk(e, s):
    np = pytest.importorskip('numpy')
    w = Worker(s.address, loop=s.loop, memory_limit=1000)
    yield w._start()

    x = e.submit(np.random.randint, 0, 255, size=500, dtype='u1', key='x')
    yield _wait(x)
    y = e.submit(np.random.randint, 0, 255, size=500, dtype='u1', key='y')
    yield _wait(y)

    assert set(w.data) == {x.key, y.key}
    assert set(w.data.fast) == {x.key, y.key}

    z = e.submit(np.random.randint, 0, 255, size=500, dtype='u1', key='z')
    yield _wait(z)
    assert set(w.data) == {x.key, y.key, z.key}
    assert set(w.data.fast) == {y.key, z.key}
    assert set(w.data.slow) == {x.key} or set(w.data.slow) == {x.key, y.key}

    yield x._result()
    assert set(w.data.fast) == {x.key, z.key}
    assert set(w.data.slow) == {y.key} or set(w.data.slow) == {x.key, y.key}
    yield w._close()


@gen_cluster(client=True)
def test_access_key(c, s, a, b):
    def f(i):
        from distributed.worker import thread_state
        return thread_state.key

    futures = [c.submit(f, i, key='x-%d' % i) for i in range(20)]
    results = yield c._gather(futures)
    assert list(results) == ['x-%d' % i for i in range(20)]


@gen_cluster(client=True)
def test_run_dask_worker(c, s, a, b):
    def f(dask_worker=None):
        return dask_worker.id

    response = yield c._run(f)
    assert response == {a.address: a.id, b.address: b.id}


@gen_cluster(client=True)
def test_run_coroutine_dask_worker(c, s, a, b):
    if sys.version_info < (3,) and tornado.version_info < (4, 5):
        pytest.skip("test needs Tornado 4.5+ on Python 2.7")

    @gen.coroutine
    def f(dask_worker=None):
        yield gen.sleep(0.001)
        raise gen.Return(dask_worker.id)

    response = yield c._run_coroutine(f)
    assert response == {a.address: a.id, b.address: b.id}


@gen_cluster(client=True, ncores=[])
def test_Executor(c, s):
    with ThreadPoolExecutor(2) as e:
        w = Worker(s.ip, s.port, executor=e)
        assert w.executor is e
        yield w._start()

        future = c.submit(inc, 1)
        result = yield future._result()
        assert result == 2

        assert e._threads  # had to do some work

        yield w._close()


@gen_cluster(client=True, ncores=[('127.0.0.1', 1)], timeout=30)
def test_spill_by_default(c, s, w):
    da = pytest.importorskip('dask.array')
    x = da.ones(int(TOTAL_MEMORY * 0.7), chunks=10000000, dtype='u1')
    y = c.persist(x)
    yield _wait(y)
    assert len(w.data.slow)  # something is on disk


@gen_cluster(ncores=[('127.0.0.1', 1)],
             worker_kwargs={'reconnect': False})
def test_close_on_disconnect(s, w):
    yield s.close()

    start = time()
    while w.status != 'closed':
        yield gen.sleep(0.01)
        assert time() < start + 5


def test_memory_limit_auto():
    a = Worker('127.0.0.1', 8099, ncores=1)
    b = Worker('127.0.0.1', 8099, ncores=2)
    c = Worker('127.0.0.1', 8099, ncores=100)
    d = Worker('127.0.0.1', 8099, ncores=200)

    assert isinstance(a.memory_limit, Number)
    assert isinstance(b.memory_limit, Number)

    assert a.memory_limit < b.memory_limit

    assert c.memory_limit == d.memory_limit


@gen_cluster(client=True)
def test_inter_worker_communication(c, s, a, b):
    [x, y] = yield c._scatter([1, 2], workers=a.address)

    future = c.submit(add, x, y, workers=b.address)
    result = yield future._result()
    assert result == 3


@gen_cluster(client=True)
def test_clean(c, s, a, b):
    x = c.submit(inc, 1, workers=a.address)
    y = c.submit(inc, x, workers=b.address)

    yield y._result()

    collections = [a.tasks, a.task_state, a.startstops, a.data, a.nbytes,
                   a.durations, a.priorities, a.types, a.threads]
    for c in collections:
        assert c

    x.release()
    y.release()

    while x.key in a.task_state:
        yield gen.sleep(0.01)

    for c in collections:
        assert not c


@pytest.mark.skipif(sys.version_info[:2] == (3, 4), reason="mul bytes fails")
@gen_cluster(client=True)
def test_message_breakup(c, s, a, b):
    n = 100000
    a.target_message_size = 10 * n
    b.target_message_size = 10 * n
    xs = [c.submit(mul, b'%d' % i, n, workers=a.address) for i in range(30)]
    y = c.submit(lambda *args: None, xs, workers=b.address)
    yield y._result()

    assert 2 <= len(b.incoming_transfer_log) <= 20
    assert 2 <= len(a.outgoing_transfer_log) <= 20

    assert all(msg['who'] == b.address for msg in a.outgoing_transfer_log)
    assert all(msg['who'] == a.address for msg in a.incoming_transfer_log)


@gen_cluster(client=True)
def test_types(c, s, a, b):
    assert not a.types
    assert not b.types
    x = c.submit(inc, 1, workers=a.address)
    yield _wait(x)
    assert a.types[x.key] == int

    y = c.submit(inc, x, workers=b.address)
    yield _wait(y)
    assert b.types == {x.key: int, y.key: int}

    yield c._cancel(y)

    start = time()
    while y.key in b.data:
        yield gen.sleep(0.01)
        assert time() < start + 5

    assert y.key not in b.types


@gen_cluster()
def test_system_monitor(s, a, b):
    assert b.monitor
    b.monitor.update()



@gen_cluster(client=True, ncores=[('127.0.0.1', 2, {'resources': {'A': 1}}),
                                  ('127.0.0.1', 1)])
def test_restrictions(c, s, a, b):
    # Resource restrictions
    x = c.submit(inc, 1, resources={'A': 1})
    yield x._result()
    assert a.resource_restrictions == {x.key: {'A': 1}}
    yield c._cancel(x)

    while x.key in a.task_state:
        yield gen.sleep(0.01)

    assert a.resource_restrictions == {}


@pytest.mark.xfail
@gen_cluster(client=True)
def test_clean_nbytes(c, s, a, b):
    L = [delayed(inc)(i) for i in range(10)]
    for i in range(5):
        L = [delayed(add)(x, y) for x, y in sliding_window(2, L)]
    total = delayed(sum)(L)

    future = c.compute(total)
    yield _wait(future)

    yield gen.sleep(1)
    assert len(a.nbytes) + len(b.nbytes) == 1


@gen_cluster(client=True, ncores=[('127.0.0.1', 1)] * 20)
def test_gather_many_small(c, s, a, *workers):
    a.total_connections = 2
    futures = yield c._scatter(list(range(100)))

    assert all(w.data for w in workers)

    def f(*args):
        return 10

    future = c.submit(f, *futures, workers=a.address)
    yield _wait(future)

    types = list(pluck(0, a.log))
    req = [i for i, t in enumerate(types) if t == 'request-dep']
    recv = [i for i, t in enumerate(types) if t == 'receive-dep']
    assert min(recv) > max(req)

    assert a.comm_nbytes == 0


@gen_cluster(client=True, ncores=[('127.0.0.1', 1)] * 3)
def test_multiple_transfers(c, s, w1, w2, w3):
    x = c.submit(inc, 1, workers=w1.address)
    y = c.submit(inc, 2, workers=w2.address)
    z = c.submit(add, x, y, workers=w3.address)

    yield _wait(z)

    r = w3.startstops[z.key]
    transfers = [t for t in r if t[0] == 'transfer']
    assert len(transfers) == 2


@gen_cluster(client=True, ncores=[('127.0.0.1', 1)] * 3)
def test_share_communication(c, s, w1, w2, w3):
    x = c.submit(mul, b'1', int(w3.target_message_size + 1), workers=w1.address)
    y = c.submit(mul, b'2', int(w3.target_message_size + 1), workers=w2.address)
    yield _wait([x, y])
    yield c._replicate([x, y], workers=[w1.address, w2.address])
    z = c.submit(add, x, y, workers=w3.address)
    yield _wait(z)
    assert len(w3.incoming_transfer_log) == 2
    assert w1.outgoing_transfer_log
    assert w2.outgoing_transfer_log


@gen_cluster(client=True)
def test_dont_overlap_communications_to_same_worker(c, s, a, b):
    x = c.submit(mul, b'1', int(b.target_message_size + 1), workers=a.address)
    y = c.submit(mul, b'2', int(b.target_message_size + 1), workers=a.address)
    yield _wait([x, y])
    z = c.submit(add, x, y, workers=b.address)
    yield _wait(z)
    assert len(b.incoming_transfer_log) == 2
    l1, l2 = b.incoming_transfer_log

    assert l1['stop'] < l2['start']


@pytest.mark.avoid_travis
@gen_cluster(client=True)
def test_log_exception_on_failed_task(c, s, a, b):
    with tmpfile() as fn:
        fh = logging.FileHandler(fn)
        try:
            from distributed.worker import logger
            logger.addHandler(fh)

            future = c.submit(div, 1, 0)
            yield _wait(future)

            yield gen.sleep(0.1)
            fh.flush()
            with open(fn) as f:
                text = f.read()

            assert "ZeroDivisionError" in text
            assert "Exception" in text
        finally:
            logger.removeHandler(fh)


@gen_cluster(client=True)
def test_clean_up_dependencies(c, s, a, b):
    x = delayed(inc)(1)
    y = delayed(inc)(2)
    xx = delayed(inc)(x)
    yy = delayed(inc)(y)
    z = delayed(add)(xx, yy)

    zz = c.persist(z)
    yield _wait(zz)

    start = time()
    while len(a.data) + len(b.data) > 1:
        yield gen.sleep(0.01)
        assert time() < start + 2

    assert set(a.data) | set(b.data) == {zz.key}


@gen_cluster(client=True)
def test_hold_onto_dependents(c, s, a, b):
    x = c.submit(inc, 1, workers=a.address)
    y = c.submit(inc, x, workers=b.address)
    yield _wait(y)

    assert x.key in b.data

    yield c._cancel(y)
    yield gen.sleep(0.1)

    assert x.key in b.data


@slow
@gen_test()
def test_worker_death_timeout():
    w = Worker('127.0.0.1', 38848, death_timeout=1)
    yield w._start()

    yield gen.sleep(3)
    assert w.status == 'closed'


@gen_cluster(client=True)
def test_stop_doing_unnecessary_work(c, s, a, b):
    futures = c.map(slowinc, range(1000), delay=0.01)
    yield gen.sleep(0.1)

    del futures

    start = time()
    while a.executing:
        yield gen.sleep(0.01)
        assert time() - start < 0.5


@gen_cluster(client=True, ncores=[('127.0.0.1', 1)])
def test_priorities(c, s, w):
    a = delayed(slowinc)(1, dask_key_name='a', delay=0.05)
    b = delayed(slowinc)(2, dask_key_name='b', delay=0.05)
    a1 = delayed(slowinc)(a, dask_key_name='a1', delay=0.05)
    a2 = delayed(slowinc)(a1, dask_key_name='a2', delay=0.05)
    b1 = delayed(slowinc)(b, dask_key_name='b1', delay=0.05)

    z = delayed(add)(a2, b1)
    future = yield c.compute(z)._result()

    log = [t for t in w.log if t[1] == 'executing' and t[2] == 'memory']
    assert [t[0] for t in log[:5]] == ['a', 'b', 'a1', 'b1', 'a2']


@gen_cluster(client=True, ncores=[('127.0.0.1', 1)])
def test_priorities_2(c, s, w):
    values = []
    for i in range(10):
        a = delayed(slowinc)(i, dask_key_name='a-%d' % i, delay=0.01)
        a1 = delayed(inc)(a, dask_key_name='a1-%d' % i)
        a2 = delayed(inc)(a1, dask_key_name='a2-%d' % i)
        b1 = delayed(dec)(a, dask_key_name='b1-%d' % i)  # <<-- least favored

        values.append(a2)
        values.append(b1)

    futures = c.compute(values)
    yield _wait(futures)

    log = [t[0] for t in w.log
                if t[1] == 'executing'
                and t[2] == 'memory'
                and not t[0].startswith('finalize')]

    assert any(key.startswith('b1') for key in log[:len(log) // 2])


@gen_cluster(client=True, worker_kwargs={'heartbeat_interval': 0.020})
def test_heartbeats(c, s, a, b):
    pytest.importorskip('psutil')
    start = time()
    while not all(s.worker_info[w].get('memory-rss') for w in s.workers):
        yield gen.sleep(0.01)
        assert time() < start + 2
