"""A new follower's SYNC must hand it every write exactly once: in the snapshot or
down the live stream, never both and never neither. The snapshot must also be taken
under the engine lock, so it is never half of a transaction."""

import asyncio
import io
import json
import threading

import pytest
from rich.console import Console

from kedis_python.parser import CommandParser
from test_stream_server import serve  # shared wire helper


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # server.py builds its global store in the cwd on import
    from kedis_python import server

    server.console = Console(file=io.StringIO(), width=200)
    server.global_store.flushall()
    server.stream_waiters.clear()
    monkeypatch.setattr(server, "server_role", "master")
    monkeypatch.setattr(server, "connected_replicas", [])
    yield server
    server.stream_waiters.clear()
    server.global_store.flushall()


async def start_sync(connect):
    f = await connect()
    f.w.write(b"SYNC\n")
    await f.w.drain()
    return f


async def read_snapshot(f):
    assert (await asyncio.wait_for(f.r.readline(), 10)) == b"A1\n"
    n = int((await f.r.readline())[1:-1])
    return json.loads((await f.r.readexactly(n + 1))[:-1])["data"]


async def read_stream(f, settle=0.4):
    """The commands the leader sent after the snapshot."""
    await asyncio.sleep(settle)
    try:
        data = await asyncio.wait_for(f.r.read(1 << 20), 1)
    except asyncio.TimeoutError:
        return []
    out = []
    while data:
        tokens, used = CommandParser.parse(data)
        if not tokens:
            break
        out.append(tokens)
        data = data[used:]
    return out


def times(snapshot, stream, key):
    """How often a one-element write to `key` reaches the follower."""
    held = snapshot.get(key)
    if isinstance(held, dict):
        held = len(held["data"])
    else:
        held = 1 if held is not None else 0
    return held + sum(1 for c in stream if len(c) > 1 and c[1] == key)


def test_a_quiet_sync_sends_the_data_once_and_then_the_stream(srv):
    async def scenario(connect, errors):
        w = await connect()
        await w.call("SET", "before", "1")
        f = await start_sync(connect)
        snap = await read_snapshot(f)
        await w.call("SET", "after", "1")
        stream = await read_stream(f)
        assert times(snap, stream, "before") == 1
        assert times(snap, stream, "after") == 1

    assert serve(srv, scenario) == []


def test_a_write_made_while_the_snapshot_is_being_sent_is_not_lost(srv, monkeypatch):
    async def scenario(connect, errors):
        reached, release = asyncio.Event(), asyncio.Event()
        armed = {"on": False}
        real_state = srv.global_store.get_snapshot_state
        real_drain = srv.drain_within_deadline

        def state():
            armed["on"] = True
            return real_state()

        async def drain(writer):
            if armed["on"]:  # the first send after the snapshot was taken
                armed["on"] = False
                reached.set()
                await release.wait()
            await real_drain(writer)

        monkeypatch.setattr(srv.global_store, "get_snapshot_state", state)
        monkeypatch.setattr(srv, "drain_within_deadline", drain)

        w = await connect()
        await w.call("SET", "before", "1")
        f = await start_sync(connect)
        await asyncio.wait_for(reached.wait(), 10)
        await w.call("SET", "during", "1")  # applied after the snapshot, while it is in flight
        release.set()
        snap = await read_snapshot(f)
        await w.call("SET", "after", "1")
        stream = await read_stream(f)
        assert times(snap, stream, "before") == 1
        assert times(snap, stream, "during") == 1
        assert times(snap, stream, "after") == 1

    assert serve(srv, scenario) == []


def test_a_write_applied_before_the_snapshot_is_not_sent_again(srv, monkeypatch):
    async def scenario(connect, errors):
        applied, release = threading.Event(), threading.Event()
        real = srv.global_handler.execute_records

        def held(tokens, sock=None):
            out = real(tokens, sock)
            if tokens[:2] == ["RPUSH", "l"]:  # applied; not yet on its way to followers
                applied.set()
                release.wait(10)
            return out

        monkeypatch.setattr(srv.global_handler, "execute_records", held)

        w = await connect()
        pending = asyncio.create_task(w.call("RPUSH", "l", "1"))
        while not applied.is_set():
            await asyncio.sleep(0.01)
        f = await start_sync(connect)
        await asyncio.sleep(0.4)  # let the SYNC run as far as it can
        release.set()
        snap = await read_snapshot(f)
        await pending
        stream = await read_stream(f)
        assert times(snap, stream, "l") == 1

    assert serve(srv, scenario) == []


def test_the_snapshot_is_taken_under_the_engine_lock(srv, monkeypatch):
    seen = []
    real_state = srv.global_store.get_snapshot_state

    def state():
        seen.append(srv.global_handler._engine_lock.locked())
        return real_state()

    monkeypatch.setattr(srv.global_store, "get_snapshot_state", state)

    async def scenario(connect, errors):
        f = await start_sync(connect)
        await read_snapshot(f)

    assert serve(srv, scenario) == []
    assert seen == [True]


# --- the barrier on its own ---------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def test_writes_do_not_wait_for_each_other():
    from kedis_python.server import SyncBarrier

    async def scenario():
        b = SyncBarrier()
        async with b.write():
            async with b.write():
                assert b.inflight == 2

    run(scenario())


def test_a_snapshot_waits_for_writes_in_flight():
    from kedis_python.server import SyncBarrier

    async def scenario():
        b, log = SyncBarrier(), []

        async def write():
            async with b.write():
                log.append("write applied")
                await asyncio.sleep(0.1)
                log.append("write buffered")

        async def sync():
            async with b.snapshot():
                log.append("snapshot")

        w = asyncio.create_task(write())
        await asyncio.sleep(0.02)
        await sync()
        await w
        assert log == ["write applied", "write buffered", "snapshot"]

    run(scenario())


def test_a_write_waits_for_a_snapshot_in_progress():
    from kedis_python.server import SyncBarrier

    async def scenario():
        b, log = SyncBarrier(), []

        async def sync():
            async with b.snapshot():
                log.append("snapshot start")
                await asyncio.sleep(0.1)
                log.append("snapshot end")

        async def write():
            async with b.write():
                log.append("write")

        s = asyncio.create_task(sync())
        await asyncio.sleep(0.02)
        await write()
        await s
        assert log == ["snapshot start", "snapshot end", "write"]

    run(scenario())


def test_snapshots_run_one_at_a_time():
    from kedis_python.server import SyncBarrier

    async def scenario():
        b, log = SyncBarrier(), []

        async def sync(name):
            async with b.snapshot():
                log.append(name + " in")
                await asyncio.sleep(0.05)
                log.append(name + " out")

        await asyncio.gather(sync("a"), sync("b"))
        assert log in (["a in", "a out", "b in", "b out"], ["b in", "b out", "a in", "a out"])

    run(scenario())


def test_a_write_that_fails_still_releases_the_barrier():
    from kedis_python.server import SyncBarrier

    async def scenario():
        b = SyncBarrier()
        with pytest.raises(RuntimeError):
            async with b.write():
                raise RuntimeError("boom")
        assert b.inflight == 0
        async with b.snapshot():  # would hang if the write still counted
            pass

    run(scenario())


def test_a_cancelled_waiting_write_does_not_wedge_the_barrier():
    from kedis_python.server import SyncBarrier

    async def scenario():
        b = SyncBarrier()
        async with b.snapshot():
            waiting = asyncio.create_task(b.write().__aenter__())
            await asyncio.sleep(0.02)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        async with b.write():
            assert b.inflight == 1
        assert b.inflight == 0

    run(scenario())


# --- a snapshot that cannot be delivered ---------------------------------------------------------


def test_a_follower_that_never_reads_its_snapshot_is_dropped_and_writes_go_on(srv, monkeypatch):
    monkeypatch.setattr(srv, "WRITE_DEADLINE", 0.5)
    monkeypatch.setattr(srv, "WRITE_MIN_RATE", 1e15)  # queued size adds no time
    big = "x" * (8 * 1024 * 1024)  # more than the kernel buffers on both ends
    srv.global_store.set("big", big)

    async def scenario(connect, errors):
        stalled = await start_sync(connect)  # asks for the snapshot and never reads it
        w = await connect()
        assert await w.call("SET", "k", "v") == "OK"  # a stalled follower holds no writes up
        for _ in range(100):
            if not srv.connected_replicas:
                break
            await asyncio.sleep(0.05)
        assert srv.connected_replicas == []
        assert await w.call("SET", "k2", "v") == "OK"
        stalled.close()

    assert serve(srv, scenario) == []


def test_a_block_applied_before_the_snapshot_is_not_sent_again(srv, monkeypatch):
    async def scenario(connect, errors):
        applied, release = threading.Event(), threading.Event()
        real = srv.global_handler.execute_batch

        def held(batch, sock=None):
            out = real(batch, sock)
            applied.set()  # applied; not yet on its way to followers
            release.wait(10)
            return out

        monkeypatch.setattr(srv.global_handler, "execute_batch", held)

        w = await connect()
        await w.call("MULTI")
        await w.call("RPUSH", "l", "1")
        await w.call("RPUSH", "l", "2")
        pending = asyncio.create_task(w.call("EXEC"))
        while not applied.is_set():
            await asyncio.sleep(0.01)
        f = await start_sync(connect)
        await asyncio.sleep(0.4)
        release.set()
        snap = await read_snapshot(f)
        await pending
        stream = await read_stream(f)
        assert snap["l"]["data"] == ["1", "2"]
        assert [c for c in stream if c[0] == "RPUSH"] == []  # nothing sent twice

    assert serve(srv, scenario) == []


def test_a_snapshot_that_cannot_be_sent_unregisters_the_follower_at_once(srv, monkeypatch):
    monkeypatch.setattr(srv, "WRITE_DEADLINE", 0.5)
    monkeypatch.setattr(srv, "WRITE_MIN_RATE", 1e15)
    srv.global_store.set("big", "x" * (8 * 1024 * 1024))

    async def scenario(connect, errors):
        stalled = await start_sync(connect)  # never reads; no write follows to drop it
        for _ in range(100):
            if not srv.connected_replicas:
                break
            await asyncio.sleep(0.05)
        assert srv.connected_replicas == []
        stalled.close()

    serve(srv, scenario)


# --- consumer groups: XREADGROUP is under the barrier too ----------------------------------------


def group_frames(stream):
    return [c for c in stream if c[0] == "XGAPPLY"]


async def make_group(w, start):
    await w.call("XGROUP", "CREATE", "s", "g", start, "MKSTREAM")


def test_a_read_applied_before_the_snapshot_is_not_forwarded_again(srv, monkeypatch):
    async def scenario(connect, errors):
        applied, release = threading.Event(), threading.Event()
        real = srv.global_handler.execute_records

        def held(tokens, sock=None):
            out = real(tokens, sock)
            if tokens[0] == "XREADGROUP":  # applied; not yet on its way to followers
                applied.set()
                release.wait(10)
            return out

        monkeypatch.setattr(srv.global_handler, "execute_records", held)

        w = await connect()
        await make_group(w, "0")
        await w.call("XADD", "s", "1-0", "k", "v")
        pending = asyncio.create_task(
            w.call("XREADGROUP", "GROUP", "g", "c", "COUNT", "1", "STREAMS", "s", ">")
        )
        while not applied.is_set():
            await asyncio.sleep(0.01)
        f = await start_sync(connect)
        await asyncio.sleep(0.4)
        release.set()
        await read_snapshot(f)
        await pending
        stream = await read_stream(f)
        assert group_frames(stream) == []  # the snapshot already holds the delivery

    assert serve(srv, scenario) == []


def test_a_blocking_read_applied_before_the_snapshot_is_not_forwarded_again(srv, monkeypatch):
    async def scenario(connect, errors):
        applied, release = threading.Event(), threading.Event()
        real = srv.global_handler.xreadgroup_attempt

        def held(*a, **k):
            out = real(*a, **k)
            if out[1]:  # an attempt that changed a group
                applied.set()
                release.wait(10)
            return out

        monkeypatch.setattr(srv.global_handler, "xreadgroup_attempt", held)

        w = await connect()
        await make_group(w, "0")
        await w.call("XADD", "s", "1-0", "k", "v")
        pending = asyncio.create_task(
            w.call("XREADGROUP", "GROUP", "g", "c", "BLOCK", "2000", "STREAMS", "s", ">")
        )
        while not applied.is_set():
            await asyncio.sleep(0.01)
        f = await start_sync(connect)
        await asyncio.sleep(0.4)
        release.set()
        await read_snapshot(f)
        await pending
        stream = await read_stream(f)
        assert group_frames(stream) == []

    assert serve(srv, scenario) == []


def test_a_blocked_reads_new_consumer_reaches_followers_while_it_waits(srv):
    async def scenario(connect, errors):
        w = await connect()
        await make_group(w, "$")
        f = await start_sync(connect)
        await read_snapshot(f)
        blocked = asyncio.create_task(  # nothing to read: parks for 5 s
            w.call("XREADGROUP", "GROUP", "g", "c", "BLOCK", "5000", "STREAMS", "s", ">")
        )
        stream = await read_stream(f, settle=1.0)
        kinds = [c[1] for c in group_frames(stream)]
        assert "XGCONS" in kinds  # not held back until the read ends
        assert not blocked.done()
        blocked.cancel()

    serve(srv, scenario)


def test_a_late_read_cannot_move_a_followers_group_cursor_backwards(srv, monkeypatch):
    async def scenario(connect, errors):
        applied, release = threading.Event(), threading.Event()
        real = srv.global_handler.execute_records

        def held(tokens, sock=None):
            out = real(tokens, sock)
            if tokens[0] == "XREADGROUP" and tokens[3] == "c1":
                applied.set()  # c1 holds 1-0; its records are not yet forwarded
                release.wait(10)
            return out

        monkeypatch.setattr(srv.global_handler, "execute_records", held)

        a, b = await connect(), await connect()
        await make_group(a, "0")
        await a.call("XADD", "s", "1-0", "k", "v")
        await a.call("XADD", "s", "2-0", "k", "v")
        slow = asyncio.create_task(
            a.call("XREADGROUP", "GROUP", "g", "c1", "COUNT", "1", "STREAMS", "s", ">")
        )
        while not applied.is_set():
            await asyncio.sleep(0.01)
        got = await b.call("XREADGROUP", "GROUP", "g", "c2", "COUNT", "1", "STREAMS", "s", ">")
        assert got[0][1][0][0] == "2-0"  # the group cursor is now 2-0
        f = await start_sync(connect)
        await asyncio.sleep(0.4)
        release.set()
        await read_snapshot(f)
        await slow
        stream = await read_stream(f)
        cursors = [c[-1] for c in group_frames(stream) if c[1] == "XGLAST"]
        assert "1-0" not in cursors  # the snapshot's 2-0 must not be rolled back to 1-0

    assert serve(srv, scenario) == []


class DeadFollower:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        raise ConnectionResetError("gone")


def test_a_group_read_drops_a_follower_that_cannot_be_written_to(srv):
    dead = DeadFollower()

    async def scenario(connect, errors):
        w = await connect()
        await make_group(w, "0")
        await w.call("XADD", "s", "1-0", "k", "v")
        srv.connected_replicas.append(dead)  # only the read below can meet it
        got = await w.call("XREADGROUP", "GROUP", "g", "c", "COUNT", "1", "STREAMS", "s", ">")
        assert got[0][1][0][0] == "1-0"  # the reader is answered whatever happens to followers
        assert dead.buf  # the follower was sent the group changes...
        assert srv.connected_replicas == []  # ...and dropped when it could not take them

    assert serve(srv, scenario) == []


# --- the order followers hear writes in ----------------------------------------------------------


class RecordingFollower:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        pass

    def commands(self):
        data, out = bytes(self.buf), []
        while data:
            tokens, used = CommandParser.parse(data)
            if not tokens:
                break
            out.append(tokens)
            data = data[used:]
        return out


def test_followers_hear_writes_in_the_order_they_were_applied(srv, monkeypatch):
    follower = RecordingFollower()
    srv.connected_replicas.append(follower)

    async def scenario(connect, errors):
        go = asyncio.Event()
        calls = []
        real_to_thread = asyncio.to_thread

        async def slow_first(fn, *a, **k):
            calls.append(1)
            first = len(calls) == 1
            out = await real_to_thread(fn, *a, **k)  # the write is applied by now
            if first:
                await go.wait()  # ...but its report back to the event loop is late
            return out

        monkeypatch.setattr(asyncio, "to_thread", slow_first)

        slow, fast = await connect(), await connect()
        first = asyncio.create_task(slow.call("RPUSH", "l", "a"))
        while not calls:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)  # a is applied
        await fast.call("RPUSH", "l", "b")  # applied after a; reports back first
        go.set()
        await first
        assert srv.global_store.get("l") is not None
        heard = [c[2] for c in follower.commands() if c[:2] == ["RPUSH", "l"]]
        assert heard == ["a", "b"], f"master applied a then b; follower heard {heard}"

    assert serve(srv, scenario) == []


def test_a_write_that_stalls_after_applying_is_not_overtaken(srv, monkeypatch):
    follower = RecordingFollower()
    srv.connected_replicas.append(follower)

    async def scenario(connect, errors):
        applied, release = threading.Event(), threading.Event()
        real = srv.global_handler.execute_records

        def held(tokens, sock=None):
            out = real(tokens, sock)
            if tokens[:3] == ["RPUSH", "l", "a"]:
                applied.set()  # applied; this thread now stalls before reporting back
                release.wait(10)
            return out

        monkeypatch.setattr(srv.global_handler, "execute_records", held)

        slow, fast = await connect(), await connect()
        first = asyncio.create_task(slow.call("RPUSH", "l", "a"))
        while not applied.is_set():
            await asyncio.sleep(0.01)
        second = asyncio.create_task(fast.call("RPUSH", "l", "b"))
        await asyncio.sleep(0.3)  # b gets as far as it can while a is stalled
        release.set()
        await asyncio.gather(first, second)
        heard = [c[2] for c in follower.commands() if c[:2] == ["RPUSH", "l"]]
        assert heard == ["a", "b"], f"master applied a then b; follower heard {heard}"

    assert serve(srv, scenario) == []


def test_a_block_is_heard_in_the_order_it_was_applied(srv, monkeypatch):
    follower = RecordingFollower()
    srv.connected_replicas.append(follower)

    async def scenario(connect, errors):
        go = asyncio.Event()
        calls = []
        real_to_thread = asyncio.to_thread

        async def slow_first(fn, *a, **k):
            calls.append(1)
            first = len(calls) == 1
            out = await real_to_thread(fn, *a, **k)
            if first:
                await go.wait()
            return out

        monkeypatch.setattr(asyncio, "to_thread", slow_first)

        slow, fast = await connect(), await connect()
        await slow.call("MULTI")
        await slow.call("RPUSH", "l", "a")
        first = asyncio.create_task(slow.call("EXEC"))  # the first thing to use a worker thread
        while not calls:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        await fast.call("RPUSH", "l", "b")
        go.set()
        await first
        heard = [c[2] for c in follower.commands() if c[:2] == ["RPUSH", "l"]]
        assert heard == ["a", "b"], f"master applied a then b; follower heard {heard}"

    assert serve(srv, scenario) == []


# --- a client that goes away in the middle of a write ----------------------------------------------


def handler_tasks():
    return [
        t for t in asyncio.all_tasks()
        if getattr(t.get_coro(), "__name__", "") == "handle_connection"
    ]


def stalled_write(srv, monkeypatch, key="l", value="a"):
    """Makes the first write to `key` stall in its worker thread after applying."""
    applied, release = threading.Event(), threading.Event()
    real = srv.global_handler.execute_records

    def held(tokens, sock=None):
        out = real(tokens, sock)
        if tokens[:3] == ["RPUSH", key, value]:
            applied.set()
            release.wait(10)
        return out

    monkeypatch.setattr(srv.global_handler, "execute_records", held)
    return applied, release


def test_a_client_that_hangs_up_mid_write_still_has_its_write_sent_once(srv, monkeypatch):
    follower = RecordingFollower()
    srv.connected_replicas.append(follower)
    applied, release = stalled_write(srv, monkeypatch)

    async def scenario(connect, errors):
        c = await connect()
        c.w.write(b"A3\nS5\nRPUSH\nS1\nl\nS1\na\n")
        await c.w.drain()
        while not applied.is_set():
            await asyncio.sleep(0.01)
        c.close()  # gone before it can be answered
        await asyncio.sleep(0.2)
        release.set()
        await asyncio.sleep(0.5)
        heard = [x[2] for x in follower.commands() if x[:2] == ["RPUSH", "l"]]
        assert heard == ["a"]
        assert srv.global_store.get("l") is not None
        assert srv.replication_barrier.inflight == 0

    assert serve(srv, scenario) == []


def test_a_cancelled_handler_keeps_the_barrier_until_its_write_is_done(srv, monkeypatch):
    follower = RecordingFollower()
    srv.connected_replicas.append(follower)
    applied, release = stalled_write(srv, monkeypatch)

    async def scenario(connect, errors):
        c = await connect()
        c.w.write(b"A3\nS5\nRPUSH\nS1\nl\nS1\na\n")
        await c.w.drain()
        while not applied.is_set():
            await asyncio.sleep(0.01)
        for t in handler_tasks():
            t.cancel()  # the server task dies while the worker thread is still mid-write
        await asyncio.sleep(0.3)
        # the thread is still applying: a SYNC must not be able to run yet
        assert srv.replication_barrier.inflight == 1
        release.set()
        await asyncio.sleep(0.5)
        heard = [x[2] for x in follower.commands() if x[:2] == ["RPUSH", "l"]]
        assert heard == ["a"]  # applied, so every follower hears it
        assert srv.replication_barrier.inflight == 0

    serve(srv, scenario)
