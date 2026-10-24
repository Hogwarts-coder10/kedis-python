"""The master's broadcast to followers has a write deadline too: a follower that
stops reading is dropped, and does not hold up writes for everyone else."""

import asyncio
import io
import socket
import time

import pytest
from rich.console import Console

BIG = 8 * 1024 * 1024  # larger than the kernel buffers on both ends of a loopback socket
_KEEP = []  # follower connections must stay referenced, or garbage collection closes them


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    monkeypatch.setattr(server, "_active_clients", 0)
    monkeypatch.setattr(server, "connected_replicas", [])
    monkeypatch.setattr(server, "server_role", "master")
    _KEEP.clear()
    yield server
    _KEEP.clear()
    server.global_store.flushall()


def kesp(*args: bytes) -> bytes:
    return b"A%d\n" % len(args) + b"".join(b"S%d\n%s\n" % (len(a), a) for a in args)


def serve(srv, scenario):
    async def runner():
        server = await asyncio.start_server(srv.handle_connection, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            await asyncio.wait_for(scenario(port), 30)
        finally:
            server.close()
            await asyncio.sleep(0.05)

    asyncio.run(runner())


async def connect(port, rcvbuf=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if rcvbuf:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    sock.setblocking(False)
    await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port))
    return await asyncio.open_connection(sock=sock)


async def wait_until(predicate, limit=10.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def join_as_follower(srv, port, rcvbuf=None):
    """SYNCs like a real follower and reads the baseline; returns (reader, writer)."""
    before = len(srv.connected_replicas)
    r, w = await connect(port, rcvbuf)
    w.write(b"SYNC\n")
    await w.drain()
    assert (await r.readline()) == b"A1\n"
    n = int((await r.readline())[1:-1])
    await r.readexactly(n + 1)
    assert await wait_until(lambda: len(srv.connected_replicas) == before + 1)
    _KEEP.append((r, w))
    return r, w


async def set_key(port, key: bytes, value: bytes):
    """SET, then a probe on the same connection. The server replies +OK before it
    broadcasts, but runs nothing else on the connection until the broadcast is
    done, so the probe's answer shows how long the follower held it up.
    Returns (reply to SET, seconds until the probe was answered)."""
    r, w = await connect(port)
    started = time.monotonic()
    w.write(kesp(b"SET", key, value) + kesp(b"GET", b"no-such-key"))
    await w.drain()
    reply = await r.readline()
    probe = await r.readline()
    elapsed = time.monotonic() - started
    w.close()
    assert probe != b""  # the session survived the broadcast (a crash closes the connection)
    return reply, elapsed


def fast_deadline(srv, monkeypatch, deadline=0.5):
    monkeypatch.setattr(srv, "WRITE_DEADLINE", deadline)
    monkeypatch.setattr(srv, "WRITE_MIN_RATE", 1e15)  # queued size adds no time


def collect(reader):
    """Reads a follower's stream in the background, like a follower that is keeping up."""
    got = bytearray()

    async def pump():
        try:
            while chunk := await reader.read(65536):
                got.extend(chunk)
        except (ConnectionError, OSError):
            pass

    return asyncio.ensure_future(pump()), got


# --- a stalled follower is dropped --------------------------------------------------------------------


def test_a_follower_that_stops_reading_is_dropped_and_the_write_completes(srv, monkeypatch):
    fast_deadline(srv, monkeypatch)

    async def scenario(port):
        await join_as_follower(srv, port, rcvbuf=4096)  # then it never reads again
        reply, took = await set_key(port, b"k", b"x" * BIG)
        assert reply.startswith(b"+OK")
        assert 0.4 < took < 5  # held for about the deadline, not for ever
        assert srv.connected_replicas == []
        assert srv.global_store.get("k") == "x" * BIG

        # With nobody left to wait for, later writes are not held up at all.
        reply, took = await set_key(port, b"k2", b"x" * BIG)
        assert reply.startswith(b"+OK") and took < 0.4

    serve(srv, scenario)


def test_a_healthy_follower_keeps_its_place_and_its_stream(srv, monkeypatch):
    fast_deadline(srv, monkeypatch)

    async def scenario(port):
        await join_as_follower(srv, port, rcvbuf=4096)  # stalls
        good_r, good_w = await join_as_follower(srv, port)
        task, got = collect(good_r)
        assert len(srv.connected_replicas) == 2

        assert (await set_key(port, b"first", b"x" * BIG))[0].startswith(b"+OK")
        assert len(srv.connected_replicas) == 1  # only the stalled one went

        assert (await set_key(port, b"second", b"v"))[0].startswith(b"+OK")
        assert await wait_until(lambda: b"second" in bytes(got))
        assert b"first" in bytes(got)  # the stream it got before the drop is intact
        good_w.close()
        task.cancel()

    serve(srv, scenario)


def test_several_stalled_followers_cost_one_deadline_not_one_each(srv, monkeypatch):
    fast_deadline(srv, monkeypatch, deadline=1.0)

    async def scenario(port):
        for _ in range(3):
            await join_as_follower(srv, port, rcvbuf=4096)
        reply, took = await set_key(port, b"k", b"x" * BIG)
        assert reply.startswith(b"+OK")
        assert srv.connected_replicas == []
        assert 0.9 < took < 2.0  # waited for together; one after another would be 3 s

    serve(srv, scenario)


def test_concurrent_writers_with_a_stalled_follower_all_succeed(srv, monkeypatch):
    fast_deadline(srv, monkeypatch)

    async def scenario(port):
        await join_as_follower(srv, port, rcvbuf=4096)
        replies = await asyncio.gather(
            *(set_key(port, b"k%d" % i, b"x" * BIG) for i in range(3))
        )
        assert all(reply.startswith(b"+OK") for reply, _ in replies)
        assert all(took < 5 for _, took in replies)
        assert srv.connected_replicas == []
        assert "Stream Crash" not in srv.console.file.getvalue()

    serve(srv, scenario)


# --- followers that do read are not affected ---------------------------------------------------------


def test_a_follower_that_reads_after_a_short_pause_is_kept(srv, monkeypatch):
    fast_deadline(srv, monkeypatch, deadline=2.0)

    async def scenario(port):
        r, w = await join_as_follower(srv, port, rcvbuf=4096)

        async def late_reader():
            await asyncio.sleep(0.5)  # not reading, but not for long
            return await r.readexactly(len(kesp(b"SET", b"k", b"x" * BIG)))

        reading = asyncio.ensure_future(late_reader())
        assert (await set_key(port, b"k", b"x" * BIG))[0].startswith(b"+OK")
        assert len(srv.connected_replicas) == 1
        assert await reading == kesp(b"SET", b"k", b"x" * BIG)
        w.close()

    serve(srv, scenario)


def test_a_slow_follower_gets_time_in_proportion_to_what_is_queued_for_it(srv, monkeypatch):
    monkeypatch.setattr(srv, "WRITE_DEADLINE", 0.3)
    monkeypatch.setattr(srv, "WRITE_MIN_RATE", 4 * 1024 * 1024)  # 8 MiB queued ~ 2 s more

    async def scenario(port):
        r, w = await join_as_follower(srv, port, rcvbuf=4096)

        async def late_reader():
            await asyncio.sleep(0.8)  # longer than WRITE_DEADLINE alone
            return await r.readexactly(len(kesp(b"SET", b"k", b"x" * BIG)))

        reading = asyncio.ensure_future(late_reader())
        assert (await set_key(port, b"k", b"x" * BIG))[0].startswith(b"+OK")
        assert len(srv.connected_replicas) == 1
        assert len(await reading) > BIG
        w.close()

    serve(srv, scenario)


# --- a follower stand-in without a transport still works (as in the stream tests) -----------------


def test_a_writer_without_a_transport_is_still_broadcast_to(srv):
    class Fake:
        def __init__(self):
            self.buf = bytearray()

        def write(self, data):
            self.buf.extend(data)

        async def drain(self):
            pass

    fake = Fake()
    srv.connected_replicas.append(fake)
    asyncio.run(srv.broadcast_to_replicas([["SET", "a", "1"]], "SET"))
    assert bytes(fake.buf) == kesp(b"SET", b"a", b"1")
    assert srv.connected_replicas == [fake]


def test_a_follower_whose_write_raises_is_removed(srv):
    class Broken:
        def write(self, data):
            raise ConnectionResetError

    srv.connected_replicas.append(Broken())
    asyncio.run(srv.broadcast_to_replicas([["SET", "a", "1"]], "SET"))
    assert srv.connected_replicas == []


def test_two_broadcasts_that_both_find_a_follower_dead_do_not_trip_over_each_other(srv):
    class Dying:
        def write(self, data):
            pass

        async def drain(self):
            await asyncio.sleep(0.05)
            raise ConnectionResetError

    async def both():
        await asyncio.gather(
            srv.broadcast_to_replicas([["SET", "a", "1"]], "SET"),
            srv.broadcast_to_replicas([["SET", "b", "2"]], "SET"),
        )

    srv.connected_replicas.append(Dying())
    asyncio.run(both())  # the second removal must find it already gone, not raise
    assert srv.connected_replicas == []
