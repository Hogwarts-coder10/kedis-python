"""Write deadline: a client that does not read its replies is dropped and its
slot freed, while clients that do read are not affected."""

import asyncio
import io
import socket
import time

import pytest
from rich.console import Console

BIG = 8 * 1024 * 1024  # larger than the kernel buffers on both ends of a loopback socket


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    monkeypatch.setattr(server, "_active_clients", 0)
    yield server
    server.global_store.flushall()


def kesp(*args: bytes) -> bytes:
    return b"A%d\n" % len(args) + b"".join(b"S%d\n%s\n" % (len(a), a) for a in args)


def serve(srv, scenario):
    """Runs scenario(port) against a real listening server."""

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
    """A connection; a small rcvbuf makes it easy to stop reading for real."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if rcvbuf:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    sock.setblocking(False)
    await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port))
    return await asyncio.open_connection(sock=sock)


async def bulk_reply(r) -> bytes:
    header = await r.readline()
    assert header.startswith(b"S"), header[:40]
    n = int(header[1:-1])
    data = await r.readexactly(n + 1)
    return data[:-1]


async def store_big_value(port, key=b"big", size=BIG):
    r, w = await connect(port)
    w.write(kesp(b"SET", key, b"x" * size))
    await w.drain()
    assert (await r.readline()).startswith(b"+OK")
    w.close()


async def wait_until(predicate, limit=10.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def fast_deadline(srv, monkeypatch, deadline=0.5):
    # Rate so high that the queued size adds no time: the deadline is the deadline.
    monkeypatch.setattr(srv, "WRITE_DEADLINE", deadline)
    monkeypatch.setattr(srv, "WRITE_MIN_RATE", 1e15)


# --- a client that never reads is dropped -------------------------------------------------------------


def test_a_client_that_never_reads_is_dropped_and_its_slot_freed(srv, monkeypatch):
    fast_deadline(srv, monkeypatch)
    monkeypatch.setattr(srv, "MAX_CLIENTS", 1)

    async def scenario(port):
        await store_big_value(port)
        assert await wait_until(lambda: srv._active_clients == 0)

        r, w = await connect(port, rcvbuf=4096)
        w.write(kesp(b"GET", b"big"))  # the reply cannot fit: and nobody reads it
        await w.drain()

        # While it is stuck it holds the only slot...
        await asyncio.sleep(0.1)
        r2, w2 = await connect(port)
        assert (await r2.readline()).startswith(b"Emax number of clients")
        w2.close()

        # ...and once the deadline passes the server lets go of it.
        assert await wait_until(lambda: srv._active_clients == 0)
        r3, w3 = await connect(port)
        w3.write(kesp(b"SET", b"k", b"v"))
        await w3.drain()
        assert (await r3.readline()).startswith(b"+OK")
        w3.close()
        w.close()

    serve(srv, scenario)


def test_commands_pipelined_behind_the_stuck_reply_are_not_run(srv, monkeypatch):
    fast_deadline(srv, monkeypatch)

    async def scenario(port):
        await store_big_value(port)
        r, w = await connect(port, rcvbuf=4096)
        # The GET's reply cannot be delivered; the SET behind it must die with the connection.
        w.write(kesp(b"GET", b"big") + kesp(b"SET", b"after", b"1"))
        await w.drain()
        assert await wait_until(lambda: srv._active_clients == 0)
        await asyncio.sleep(0.1)
        assert srv.global_store.get("after") is None
        w.close()

    serve(srv, scenario)


def test_the_dropped_client_sees_its_connection_reset_not_a_complete_reply(srv, monkeypatch):
    fast_deadline(srv, monkeypatch)

    async def scenario(port):
        await store_big_value(port)
        r, w = await connect(port, rcvbuf=4096)
        w.write(kesp(b"GET", b"big"))
        await w.drain()
        assert await wait_until(lambda: srv._active_clients == 0)

        # Reading now returns whatever was already in flight, then the end: never all 8 MiB.
        got = 0
        try:
            while chunk := await r.read(65536):
                got += len(chunk)
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        assert got < BIG
        w.close()

    serve(srv, scenario)


def test_a_stalled_client_does_not_hold_up_one_that_reads(srv, monkeypatch):
    fast_deadline(srv, monkeypatch, deadline=2.0)

    async def scenario(port):
        await store_big_value(port)
        stalled_r, stalled_w = await connect(port, rcvbuf=4096)
        stalled_w.write(kesp(b"GET", b"big"))
        await stalled_w.drain()
        await asyncio.sleep(0.1)  # it is stuck now

        r, w = await connect(port)
        started = time.monotonic()
        w.write(kesp(b"GET", b"big"))
        await w.drain()
        assert len(await bulk_reply(r)) == BIG
        w.write(kesp(b"SET", b"k", b"v"))
        await w.drain()
        assert (await r.readline()).startswith(b"+OK")
        assert time.monotonic() - started < 1.5  # served while the other one was stuck
        w.close()
        stalled_w.close()

    serve(srv, scenario)


# --- clients that do read are not affected -----------------------------------------------------------


def test_a_client_that_reads_after_a_pause_shorter_than_the_deadline_is_fine(srv, monkeypatch):
    fast_deadline(srv, monkeypatch, deadline=2.0)

    async def scenario(port):
        await store_big_value(port)
        r, w = await connect(port, rcvbuf=4096)
        w.write(kesp(b"GET", b"big"))
        await w.drain()
        await asyncio.sleep(0.5)  # not reading, but not for long
        assert len(await bulk_reply(r)) == BIG
        w.write(kesp(b"SET", b"k", b"v"))
        await w.drain()
        assert (await r.readline()).startswith(b"+OK")
        w.close()

    serve(srv, scenario)


def test_a_slow_but_steady_reader_of_a_big_reply_is_given_time_in_proportion_to_its_size(
    srv, monkeypatch
):
    # 0.3 s base, but the queued bytes at 4 MiB/s add about a second or two more.
    monkeypatch.setattr(srv, "WRITE_DEADLINE", 0.3)
    monkeypatch.setattr(srv, "WRITE_MIN_RATE", 4 * 1024 * 1024)

    async def scenario(port):
        await store_big_value(port)
        r, w = await connect(port, rcvbuf=4096)
        w.write(kesp(b"GET", b"big"))
        await w.drain()
        await asyncio.sleep(0.8)  # longer than WRITE_DEADLINE alone
        assert len(await bulk_reply(r)) == BIG
        w.close()

    serve(srv, scenario)


def test_the_deadline_is_not_applied_to_a_connection_with_nothing_to_send(srv, monkeypatch):
    fast_deadline(srv, monkeypatch, deadline=0.2)

    async def scenario(port):
        r, w = await connect(port)
        await asyncio.sleep(0.6)  # idle for longer than the write deadline
        w.write(kesp(b"SET", b"k", b"v"))
        await w.drain()
        assert (await r.readline()).startswith(b"+OK")
        assert srv._active_clients == 1
        w.close()

    serve(srv, scenario)


def test_the_defaults_match_the_documented_numbers(srv):
    assert srv.WRITE_DEADLINE == 30.0
    assert srv.WRITE_MIN_RATE == 256 * 1024
