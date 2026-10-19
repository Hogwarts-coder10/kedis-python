"""Per-connection budgets: command size, half-sent commands, client count, and
slow subscribers."""

import asyncio
import io
import time

import pytest
from rich.console import Console

from kedis_python.commands import CommandHandler
from kedis_python.parser import CommandParser
from kedis_python.store import KedisStore


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
    """Runs scenario(host_port, srv) against a real listening server."""

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


async def bulk_reply(r) -> bytes:
    header = await r.readline()
    assert header.startswith(b"S"), header
    n = int(header[1:-1])
    data = await r.readexactly(n + 1)
    return data[:-1]


# --- aggregate payload budget ----------------------------------------------------------------------


def test_a_command_over_the_budget_is_refused_on_its_header(srv, monkeypatch):
    monkeypatch.setattr(CommandParser, "MAX_COMMAND_BYTES", 100)

    async def scenario(port):
        r, w = await connect(port)
        w.write(b"A2\nS3\nSET\nS101\n")  # no payload sent at all
        await w.drain()
        line = await r.readline()
        assert line.startswith(b"E") and b"too large" in line
        assert await r.read() == b""  # closed
        w.close()

    serve(srv, scenario)


def test_the_budget_is_summed_over_the_whole_command(srv, monkeypatch):
    monkeypatch.setattr(CommandParser, "MAX_COMMAND_BYTES", 100)

    async def scenario(port):
        r, w = await connect(port)
        w.write(b"A3\nS3\nSET\nS60\n" + b"k" * 60 + b"\nS60\n")
        await w.drain()
        assert b"too large" in await r.readline()
        w.close()

    serve(srv, scenario)


# --- big values arrive without being re-parsed on every chunk -----------------------------------------


def test_a_big_value_is_parsed_once_not_per_chunk(srv, monkeypatch):
    calls = []
    real = CommandParser.parse_buffer

    def counting(raw, start=0):
        calls.append(len(raw))
        return real(raw, start)

    monkeypatch.setattr(CommandParser, "parse_buffer", staticmethod(counting))
    size = 2 * 1024 * 1024
    value = bytes(range(256)) * (size // 256)

    async def scenario(port):
        r, w = await connect(port)
        w.write(b"A3\nS3\nSET\nS1\nk\nS%d\n" % size)
        for i in range(0, size, 65536):
            w.write(value[i : i + 65536])
            await w.drain()
        w.write(b"\n")
        await w.drain()
        assert (await r.readline()).startswith(b"+OK")
        w.write(kesp(b"GET", b"k"))
        await w.drain()
        assert await bulk_reply(r) == value
        w.close()

    serve(srv, scenario)
    # one pass for the header, one when the value is complete, a few for the GET
    assert len(calls) <= 8, len(calls)


def test_a_value_dripped_in_tiny_chunks_still_completes(srv):
    value = b"v" * 5000

    async def scenario(port):
        r, w = await connect(port)
        frame = kesp(b"SET", b"k", value) + kesp(b"GET", b"k")
        for i in range(0, len(frame), 97):
            w.write(frame[i : i + 97])
            await w.drain()
            await asyncio.sleep(0)
        assert (await r.readline()).startswith(b"+OK")
        assert await bulk_reply(r) == value
        w.close()

    serve(srv, scenario)


def test_pipelined_commands_blank_lines_and_a_split_frame_stay_in_order(srv):
    async def scenario(port):
        r, w = await connect(port)
        tail = kesp(b"GET", b"a")
        w.write(
            kesp(b"SET", b"a", b"1") + b"\n\nA0\n" + kesp(b"SET", b"b", b"2") + tail[:5]
        )
        await w.drain()
        assert (await r.readline()).startswith(b"+OK")
        assert (await r.readline()).startswith(b"+OK")
        await asyncio.sleep(0.05)
        w.write(tail[5:] + kesp(b"GET", b"b"))
        await w.drain()
        assert await bulk_reply(r) == b"1"
        assert await bulk_reply(r) == b"2"
        w.close()

    serve(srv, scenario)


# --- deadlines for half-sent commands -----------------------------------------------------------------


def test_a_half_sent_command_that_stalls_is_dropped(srv, monkeypatch):
    monkeypatch.setattr(srv, "STALL_TIMEOUT", 0.3)

    async def scenario(port):
        r, w = await connect(port)
        w.write(b"A2\nS3\nGET\nS5\nab")  # the rest never comes
        await w.drain()
        started = time.monotonic()
        line = await asyncio.wait_for(r.readline(), 3)
        assert line.startswith(b"E") and b"timed out" in line
        assert time.monotonic() - started < 2
        assert await r.read() == b""
        w.close()

    serve(srv, scenario)


def test_dripping_one_byte_at_a_time_cannot_hold_a_command_open(srv, monkeypatch):
    monkeypatch.setattr(srv, "STALL_TIMEOUT", 10.0)  # each drip beats the stall timer
    monkeypatch.setattr(srv, "FRAME_DEADLINE", 0.4)
    monkeypatch.setattr(srv, "FRAME_MIN_RATE", 10**9)

    async def scenario(port):
        r, w = await connect(port)
        started = time.monotonic()
        frame = kesp(b"SET", b"k", b"v" * 200)  # would take 10 s at this rate
        cut_off = False
        for byte in frame:
            try:
                w.write(bytes([byte]))
                await w.drain()
            except ConnectionError:
                cut_off = True
                break
            await asyncio.sleep(0.05)
            if r.at_eof():
                cut_off = True
                break
        elapsed = time.monotonic() - started
        assert cut_off and elapsed < 3, (cut_off, elapsed)
        # The server tells us why when the line gets through; if we were still
        # sending when it closed, TCP may reset the connection first.
        try:
            data = await asyncio.wait_for(r.read(), 3)
        except ConnectionError:
            data = b""
        assert data == b"" or b"timed out" in data
        w.close()

    serve(srv, scenario)


def test_an_idle_connection_with_nothing_half_sent_is_left_alone(srv, monkeypatch):
    monkeypatch.setattr(srv, "STALL_TIMEOUT", 0.2)
    monkeypatch.setattr(srv, "FRAME_DEADLINE", 0.2)

    async def scenario(port):
        r, w = await connect(port)
        await asyncio.sleep(0.6)  # far longer than either deadline
        w.write(kesp(b"SET", b"k", b"v"))
        await w.drain()
        assert (await r.readline()).startswith(b"+OK")
        w.close()

    serve(srv, scenario)


def test_a_slow_but_steady_upload_gets_time_in_proportion_to_its_size(srv, monkeypatch):
    monkeypatch.setattr(srv, "STALL_TIMEOUT", 5.0)
    monkeypatch.setattr(srv, "FRAME_DEADLINE", 0.3)
    monkeypatch.setattr(srv, "FRAME_MIN_RATE", 1000)  # 3000 bytes earns 3 s more

    async def scenario(port):
        r, w = await connect(port)
        frame = kesp(b"SET", b"k", b"v" * 3000)
        started = time.monotonic()
        for i in range(0, len(frame), 600):
            w.write(frame[i : i + 600])
            await w.drain()
            await asyncio.sleep(0.15)
        assert time.monotonic() - started > 0.5  # longer than FRAME_DEADLINE alone
        assert (await r.readline()).startswith(b"+OK")
        w.close()

    serve(srv, scenario)


# --- connection cap -------------------------------------------------------------------------------------


def test_connections_over_the_cap_are_refused_and_slots_are_reused(srv, monkeypatch):
    monkeypatch.setattr(srv, "MAX_CLIENTS", 2)

    async def scenario(port):
        a_r, a_w = await connect(port)
        b_r, b_w = await connect(port)
        for r, w in ((a_r, a_w), (b_r, b_w)):  # both are in service
            w.write(kesp(b"SET", b"k", b"v"))
            await w.drain()
            assert (await r.readline()).startswith(b"+OK")

        c_r, c_w = await connect(port)
        line = await asyncio.wait_for(c_r.readline(), 3)
        assert line.startswith(b"E") and b"max number of clients" in line
        assert await c_r.read() == b""
        c_w.close()

        a_w.close()  # frees a slot
        for _ in range(100):
            if srv._active_clients < 2:
                break
            await asyncio.sleep(0.02)
        d_r, d_w = await connect(port)
        d_w.write(kesp(b"GET", b"k"))
        await d_w.drain()
        assert await bulk_reply(d_r) == b"v"
        d_w.close()
        b_w.close()

    serve(srv, scenario)
    assert srv._active_clients == 0


# --- slow subscribers ------------------------------------------------------------------------------------


class FakeTransport:
    def __init__(self, buffered):
        self.buffered = buffered

    def get_write_buffer_size(self):
        return self.buffered


class FakeSubscriber:
    def __init__(self, buffered=0):
        self.transport = FakeTransport(buffered)
        self.written = []
        self.closed = False

    def is_closing(self):
        return self.closed

    def write(self, payload):
        self.written.append(payload)

    def close(self):
        self.closed = True


class FakeLoop:
    def is_running(self):
        return True

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


@pytest.fixture
def handler(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = KedisStore(aof_filename="test.aof", appendfsync="always")
    h = CommandHandler(store)
    h.event_loop = FakeLoop()
    yield h
    store.shutdown()


def test_a_subscriber_with_room_gets_the_message(handler):
    sub = FakeSubscriber(buffered=1024)
    handler._channels["c"] = [sub]
    assert handler.execute(["PUBLISH", "c", "hello"]) == 1
    assert len(sub.written) == 1 and not sub.closed


def test_a_subscriber_that_is_not_reading_is_cut_off(handler, monkeypatch):
    monkeypatch.setattr(CommandHandler, "MAX_SUBSCRIBER_BUFFER", 1000)
    slow, fine = FakeSubscriber(buffered=5000), FakeSubscriber(buffered=0)
    handler._channels["c"] = [slow, fine]
    assert handler.execute(["PUBLISH", "c", "hello"]) == 1  # only the healthy one
    assert slow.closed and slow.written == []
    assert len(fine.written) == 1
    assert handler._channels["c"] == [fine]  # the slow one is unsubscribed
