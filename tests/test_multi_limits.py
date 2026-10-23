"""The MULTI queue is bounded: past MAX_MULTI_COMMANDS commands or MAX_MULTI_BYTES
bytes of arguments, the block is poisoned and EXEC aborts it."""

import asyncio
import io

import pytest
from rich.console import Console


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    yield server
    server.global_store.flushall()


def kesp(*args: bytes) -> bytes:
    return b"A%d\n" % len(args) + b"".join(b"S%d\n%s\n" % (len(a), a) for a in args)


def serve(srv, scenario):
    async def runner():
        server = await asyncio.start_server(srv.handle_connection, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            await asyncio.wait_for(scenario(port), 20)
        finally:
            server.close()
            await asyncio.sleep(0.05)

    asyncio.run(runner())


async def connect(port):
    return await asyncio.open_connection("127.0.0.1", port)


async def send(w, r, *args: bytes) -> bytes:
    """Sends one command and returns the first line of the reply."""
    w.write(kesp(*args))
    await w.drain()
    return await asyncio.wait_for(r.readline(), 5)


async def get(port, key: bytes):
    """Reads a key over a fresh connection: the bulk value, or None."""
    r, w = await connect(port)
    first = await send(w, r, b"GET", key)
    value = None
    if first.startswith(b"S"):
        value = (await r.readexactly(int(first[1:-1]) + 1))[:-1]
    w.close()
    return value


def small(srv, monkeypatch, commands=3, size=10_000):
    monkeypatch.setattr(srv, "MAX_MULTI_COMMANDS", commands)
    monkeypatch.setattr(srv, "MAX_MULTI_BYTES", size)


def test_the_defaults_are_the_documented_ones(srv):
    assert srv.MAX_MULTI_COMMANDS == 10_000
    assert srv.MAX_MULTI_BYTES == 64 * 1024 * 1024


# --- the command-count limit -----------------------------------------------------------------


def test_a_queue_exactly_at_the_limit_runs(srv, monkeypatch):
    small(srv, monkeypatch, commands=3)

    async def scenario(port):
        r, w = await connect(port)
        assert (await send(w, r, b"MULTI")).startswith(b"+OK")
        for k in (b"a", b"b", b"c"):
            assert (await send(w, r, b"SET", k, b"1")).startswith(b"+OK")
        assert await send(w, r, b"EXEC") == b"A3\n"
        w.close()
        assert await get(port, b"a") == b"1" and await get(port, b"c") == b"1"

    serve(srv, scenario)


def test_one_command_over_the_limit_poisons_the_block(srv, monkeypatch):
    small(srv, monkeypatch, commands=3)

    async def scenario(port):
        r, w = await connect(port)
        await send(w, r, b"MULTI")
        for k in (b"a", b"b", b"c"):
            await send(w, r, b"SET", k, b"1")
        over = await send(w, r, b"SET", b"d", b"1")
        assert over.startswith(b"-ERR MULTI queue limit")
        # Still poisoned: later commands are refused, not run and not queued.
        assert (await send(w, r, b"SET", b"e", b"1")).startswith(b"-ERR MULTI queue limit")
        assert (await send(w, r, b"EXEC")).startswith(b"-EXECABORT")
        # Nothing from the block ran, including the three that had been queued.
        for k in (b"a", b"b", b"c", b"d", b"e"):
            assert await get(port, k) is None
        # The connection is back to normal: no longer in MULTI.
        assert (await send(w, r, b"SET", b"z", b"9")).startswith(b"+OK")
        assert await get(port, b"z") == b"9"
        assert (await send(w, r, b"EXEC")).startswith(b"-ERR EXEC without MULTI")
        w.close()

    serve(srv, scenario)


def test_discard_clears_the_poison_and_a_new_block_works(srv, monkeypatch):
    small(srv, monkeypatch, commands=2)

    async def scenario(port):
        r, w = await connect(port)
        await send(w, r, b"MULTI")
        for k in (b"a", b"b", b"c"):
            await send(w, r, b"SET", k, b"1")  # the third poisons
        assert (await send(w, r, b"DISCARD")).startswith(b"+OK")
        assert (await send(w, r, b"MULTI")).startswith(b"+OK")
        await send(w, r, b"SET", b"x", b"1")
        assert await send(w, r, b"EXEC") == b"A1\n"
        w.close()
        assert await get(port, b"x") == b"1"

    serve(srv, scenario)


def test_a_new_multi_after_an_abort_starts_clean(srv, monkeypatch):
    small(srv, monkeypatch, commands=1)

    async def scenario(port):
        r, w = await connect(port)
        await send(w, r, b"MULTI")
        await send(w, r, b"SET", b"a", b"1")
        await send(w, r, b"SET", b"b", b"1")  # over
        assert (await send(w, r, b"EXEC")).startswith(b"-EXECABORT")
        await send(w, r, b"MULTI")
        await send(w, r, b"SET", b"c", b"1")
        assert await send(w, r, b"EXEC") == b"A1\n"
        w.close()
        assert await get(port, b"c") == b"1" and await get(port, b"a") is None

    serve(srv, scenario)


def test_the_limit_is_per_connection(srv, monkeypatch):
    small(srv, monkeypatch, commands=2)

    async def scenario(port):
        r1, w1 = await connect(port)
        r2, w2 = await connect(port)
        await send(w1, r1, b"MULTI")
        await send(w2, r2, b"MULTI")
        for k in (b"a", b"b", b"c"):
            await send(w1, r1, b"SET", k, b"1")  # client 1 overflows
        await send(w2, r2, b"SET", b"m", b"1")
        await send(w2, r2, b"SET", b"n", b"1")  # client 2 is exactly at the limit
        assert (await send(w1, r1, b"EXEC")).startswith(b"-EXECABORT")
        assert await send(w2, r2, b"EXEC") == b"A2\n"
        w1.close()
        w2.close()
        assert await get(port, b"m") == b"1"

    serve(srv, scenario)


def test_watch_is_cleared_when_an_overflowed_block_aborts(srv, monkeypatch):
    small(srv, monkeypatch, commands=1)

    async def scenario(port):
        r, w = await connect(port)
        await send(w, r, b"WATCH", b"k")
        await send(w, r, b"MULTI")
        await send(w, r, b"SET", b"a", b"1")
        await send(w, r, b"SET", b"b", b"1")
        assert (await send(w, r, b"EXEC")).startswith(b"-EXECABORT")
        # If the watch had survived, another client touching k would abort this
        # next transaction. It must not.
        r2, w2 = await connect(port)
        await send(w2, r2, b"SET", b"k", b"changed")
        w2.close()
        await send(w, r, b"MULTI")
        await send(w, r, b"SET", b"c", b"1")
        assert await send(w, r, b"EXEC") == b"A1\n"
        w.close()

    serve(srv, scenario)


# --- the byte limit --------------------------------------------------------------------------


def test_a_queue_exactly_at_the_byte_limit_runs(srv, monkeypatch):
    small(srv, monkeypatch, commands=100, size=10)  # SET a 1 = 3 + 1 + 1 = 5 bytes

    async def scenario(port):
        r, w = await connect(port)
        await send(w, r, b"MULTI")
        assert (await send(w, r, b"SET", b"a", b"1")).startswith(b"+OK")
        assert (await send(w, r, b"SET", b"b", b"1")).startswith(b"+OK")  # total 10
        assert await send(w, r, b"EXEC") == b"A2\n"
        w.close()

    serve(srv, scenario)


def test_one_byte_over_the_byte_limit_poisons_the_block(srv, monkeypatch):
    small(srv, monkeypatch, commands=100, size=10)

    async def scenario(port):
        r, w = await connect(port)
        await send(w, r, b"MULTI")
        await send(w, r, b"SET", b"a", b"1")  # 5
        over = await send(w, r, b"SET", b"b", b"11")  # 6 -> 11
        assert over.startswith(b"-ERR MULTI queue limit")
        assert (await send(w, r, b"EXEC")).startswith(b"-EXECABORT")
        assert await get(port, b"a") is None
        w.close()

    serve(srv, scenario)


def test_the_byte_limit_counts_utf8_bytes_not_characters(srv, monkeypatch):
    small(srv, monkeypatch, commands=100, size=9)
    # SET a é = 3 + 1 + 2 = 6 bytes (5 characters); GET a = 4. Total 10 bytes, 9 characters.
    value = "é".encode()

    async def scenario(port):
        r, w = await connect(port)
        await send(w, r, b"MULTI")
        assert (await send(w, r, b"SET", b"a", value)).startswith(b"+OK")  # 6
        assert (await send(w, r, b"GET", b"a")).startswith(b"-ERR MULTI queue limit")  # 6 + 4 = 10 > 9
        w.close()

    serve(srv, scenario)


# --- the queue itself is freed --------------------------------------------------------------


class FakeWriter:
    def get_extra_info(self, name, default=None):
        return None


def test_overflow_frees_the_queued_commands(srv, monkeypatch):
    small(srv, monkeypatch, commands=2)
    session = srv.AsyncKedisSession(None, FakeWriter())
    sent = []

    async def fake_send(data):
        sent.append(data)

    session.send = fake_send

    async def go():
        await session.queue_command(["SET", "a", "1"])
        await session.queue_command(["SET", "b", "1"])
        assert len(session.tx_queue) == 2
        await session.queue_command(["SET", "c", "1"])
        assert session.tx_queue == [] and session.tx_bytes == 0 and session.tx_overflowed
        for _ in range(1000):  # a client that keeps sending stores nothing more
            await session.queue_command(["SET", "x" * 100, "1"])
        assert session.tx_queue == [] and session.tx_bytes == 0

    asyncio.run(go())
    assert sent.count(b"+OK\n") == 2
    assert all(m.startswith(b"-ERR MULTI queue limit") for m in sent[2:])


def test_every_way_out_of_a_block_clears_the_overflow_state(srv, monkeypatch):
    small(srv, monkeypatch, commands=1)
    session = srv.AsyncKedisSession(None, FakeWriter())

    async def fake_send(data):
        pass

    session.send = fake_send

    async def poisoned():
        await session.handle_multi(["MULTI"])
        await session.queue_command(["SET", "a", "1"])
        await session.queue_command(["SET", "b", "1"])
        assert session.tx_overflowed

    async def go():
        for leave in (session.handle_discard, session.handle_exec):
            await poisoned()
            await leave(["X"])
            assert not session.tx_overflowed and not session.in_transaction
            assert session.tx_queue == [] and session.tx_bytes == 0
        await poisoned()  # and MULTI itself starts from a clean slate
        session.in_transaction = False
        await session.handle_multi(["MULTI"])
        assert not session.tx_overflowed and session.tx_queue == [] and session.tx_bytes == 0

    asyncio.run(go())
