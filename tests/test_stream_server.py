"""End-to-end stream tests over real TCP sockets against the real async server:
nested replies on the wire and XREAD ... BLOCK (wake-ups, timeouts, races,
unrelated writes, disconnects)."""

import asyncio
import io
import time

import pytest
from rich.console import Console


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # server.py builds its global store in the cwd on import
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    server.stream_waiters.clear()
    yield server
    server.stream_waiters.clear()
    server.global_store.flushall()


class Client:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer

    @classmethod
    async def connect(cls, port):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        return cls(r, w)

    async def send(self, *tokens):
        body = b"".join(f"S{len(t.encode())}\n{t}\n".encode() for t in tokens)
        self.w.write(f"A{len(tokens)}\n".encode() + body)
        await self.w.drain()

    async def read(self):
        head = await self.r.readline()
        sigil, rest = chr(head[0]), head[1:-1].decode()
        if sigil == "A":
            return [await self.read() for _ in range(int(rest))]
        if sigil == "S":
            raw = await self.r.readexactly(int(rest) + 1)
            return raw[:-1].decode()
        if sigil == "I":
            return int(rest)
        if sigil == "N":
            return None
        if sigil == "E":
            return ("ERR", rest)
        return rest

    async def call(self, *tokens):
        await self.send(*tokens)
        return await self.read()

    def close(self):
        self.w.close()


def serve(srv, scenario):
    """Runs `scenario(port, clients, errors)` against a live server."""

    async def runner():
        loop = asyncio.get_running_loop()
        errors = []
        loop.set_exception_handler(lambda _l, ctx: errors.append(ctx.get("exception") or ctx))
        server = await asyncio.start_server(srv.handle_connection, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        clients = []

        async def connect():
            c = await Client.connect(port)
            clients.append(c)
            return c

        try:
            await asyncio.wait_for(scenario(connect, errors), 20)
        finally:
            for c in clients:
                c.close()
            await asyncio.sleep(0.05)
            server.close()
            try:  # a leaked session would otherwise hang the test run here
                await asyncio.wait_for(server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass
        return errors

    return asyncio.run(runner())


class TestWire:
    def test_nested_replies_round_trip(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            assert await c.call("XADD", "s", "1-0", "temp", "21", "hum", "40") == "1-0"
            assert await c.call("XADD", "s", "2-0", "temp", "22") == "2-0"
            assert await c.call("XLEN", "s") == 2
            assert await c.call("XRANGE", "s", "-", "+") == [
                ["1-0", ["temp", "21", "hum", "40"]],
                ["2-0", ["temp", "22"]],
            ]
            assert await c.call("XREAD", "STREAMS", "s", "1-0") == [
                ["s", [["2-0", ["temp", "22"]]]]
            ]
            assert await c.call("XREAD", "STREAMS", "s", "2-0") is None

        assert serve(srv, scenario) == []

    def test_user_data_that_looks_like_a_status_arrives_as_data(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await c.call("XADD", "s", "1-0", "level", "ERROR disk full", "ack", "OK")
            assert await c.call("XRANGE", "s", "-", "+") == [
                ["1-0", ["level", "ERROR disk full", "ack", "OK"]]
            ]

        assert serve(srv, scenario) == []


class TestBlocking:
    def test_returns_immediately_when_data_is_available(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await c.call("XADD", "s", "1-0", "k", "v")
            started = time.monotonic()
            reply = await c.call("XREAD", "BLOCK", "5000", "STREAMS", "s", "0-0")
            assert reply == [["s", [["1-0", ["k", "v"]]]]]
            assert time.monotonic() - started < 1.0

        assert serve(srv, scenario) == []

    def test_wakes_when_another_client_writes(self, srv):
        async def scenario(connect, errors):
            reader, writer = await connect(), await connect()
            await writer.call("XADD", "s", "1-0", "old", "entry")
            await reader.send("XREAD", "BLOCK", "5000", "STREAMS", "s", "$")
            await asyncio.sleep(0.2)  # reader is now parked
            started = time.monotonic()
            await writer.call("XADD", "s", "2-0", "new", "entry")
            reply = await reader.read()
            assert reply == [["s", [["2-0", ["new", "entry"]]]]]  # only the new one
            assert time.monotonic() - started < 1.0  # woken, not timed out

        assert serve(srv, scenario) == []

    def test_times_out_with_nil(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await c.call("XADD", "s", "1-0", "k", "v")
            started = time.monotonic()
            assert await c.call("XREAD", "BLOCK", "300", "STREAMS", "s", "$") is None
            elapsed = time.monotonic() - started
            assert 0.25 <= elapsed < 1.5
            assert await c.call("XLEN", "s") == 1  # connection still healthy

        assert serve(srv, scenario) == []

    def test_block_zero_waits_until_data_arrives(self, srv):
        async def scenario(connect, errors):
            reader, writer = await connect(), await connect()
            await reader.send("XREAD", "BLOCK", "0", "STREAMS", "s", "$")
            await asyncio.sleep(0.8)  # longer than the poll interval: still waiting
            await writer.call("XADD", "s", "1-0", "k", "v")
            assert await asyncio.wait_for(reader.read(), 3) == [["s", [["1-0", ["k", "v"]]]]]

        assert serve(srv, scenario) == []

    def test_a_write_right_after_blocking_is_never_missed(self, srv):
        async def scenario(connect, errors):
            reader, writer = await connect(), await connect()
            for i in range(1, 16):
                await reader.send("XREAD", "BLOCK", "3000", "STREAMS", "s", "$")
                await writer.send("XADD", "s", f"{i}-0", "n", str(i))  # no sleep: race
                got = await asyncio.wait_for(reader.read(), 2)
                assert got == [["s", [[f"{i}-0", ["n", str(i)]]]]], f"iteration {i}"
                await writer.read()

        assert serve(srv, scenario) == []

    def test_unrelated_stream_writes_do_not_end_the_block_early(self, srv):
        async def scenario(connect, errors):
            reader, writer = await connect(), await connect()
            started = time.monotonic()
            await reader.send("XREAD", "BLOCK", "800", "STREAMS", "wanted", "$")
            await asyncio.sleep(0.1)
            for i in range(1, 4):
                await writer.call("XADD", "other", f"{i}-0", "k", "v")
            assert await reader.read() is None  # still nil: nothing for "wanted"
            assert time.monotonic() - started >= 0.7

        assert serve(srv, scenario) == []

    def test_every_blocked_reader_wakes(self, srv):
        async def scenario(connect, errors):
            readers = [await connect() for _ in range(3)]
            writer = await connect()
            for r in readers:
                await r.send("XREAD", "BLOCK", "5000", "STREAMS", "s", "$")
            await asyncio.sleep(0.2)
            await writer.call("XADD", "s", "1-0", "k", "v")
            for r in readers:
                assert await asyncio.wait_for(r.read(), 2) == [["s", [["1-0", ["k", "v"]]]]]

        assert serve(srv, scenario) == []

    def test_reader_on_two_streams_wakes_for_either(self, srv):
        async def scenario(connect, errors):
            reader, writer = await connect(), await connect()
            await reader.send("XREAD", "BLOCK", "5000", "STREAMS", "a", "b", "$", "$")
            await asyncio.sleep(0.2)
            await writer.call("XADD", "b", "1-0", "k", "v")
            assert await asyncio.wait_for(reader.read(), 2) == [["b", [["1-0", ["k", "v"]]]]]

        assert serve(srv, scenario) == []

    def test_dollar_is_pinned_before_waiting(self, srv):
        async def scenario(connect, errors):
            reader, writer = await connect(), await connect()
            await writer.call("XADD", "s", "5-0", "k", "before")
            await reader.send("XREAD", "BLOCK", "3000", "STREAMS", "s", "$")
            await asyncio.sleep(0.2)
            await writer.call("XADD", "s", "6-0", "k", "after")
            await writer.call("XADD", "s", "7-0", "k", "later")
            got = await asyncio.wait_for(reader.read(), 2)
            assert got[0][1][0][0] == "6-0"  # not 5-0, and it starts at the first new entry

        assert serve(srv, scenario) == []

    def test_wrongtype_errors_immediately(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await c.call("SET", "str", "v")
            started = time.monotonic()
            reply = await c.call("XREAD", "BLOCK", "5000", "STREAMS", "str", "$")
            assert reply[0] == "ERR" and "WRONGTYPE" in reply[1]
            assert time.monotonic() - started < 1.0

        assert serve(srv, scenario) == []

    def test_syntax_error_replies_without_blocking(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            reply = await asyncio.wait_for(c.call("XREAD", "BLOCK", "5000", "STREAMS", "s"), 2)
            assert reply[0] == "ERR" and "Unbalanced" in reply[1]

        assert serve(srv, scenario) == []

    def test_non_blocking_xread_is_unchanged(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            started = time.monotonic()
            assert await c.call("XREAD", "STREAMS", "s", "$") is None
            assert time.monotonic() - started < 0.5

        assert serve(srv, scenario) == []


class TestDisconnects:
    def test_a_client_that_leaves_while_blocked_is_cleaned_up(self, srv):
        async def scenario(connect, errors):
            ghost, other = await connect(), await connect()
            await ghost.send("XREAD", "BLOCK", "0", "STREAMS", "s", "$")
            await asyncio.sleep(0.2)
            assert len(srv.stream_waiters) == 1
            ghost.close()
            for _ in range(40):  # poll interval is 0.5s; allow a few
                if not srv.stream_waiters:
                    break
                await asyncio.sleep(0.1)
            assert not srv.stream_waiters
            assert await other.call("XADD", "s", "1-0", "k", "v") == "1-0"  # server still fine

        assert serve(srv, scenario) == []  # and no "unhandled exception" noise

    def test_a_client_that_resets_while_blocked_is_cleaned_up_quietly(self, srv):
        import socket
        import struct

        async def scenario(connect, errors):
            ghost = await connect()
            await ghost.send("XREAD", "BLOCK", "0", "STREAMS", "s", "$")
            await asyncio.sleep(0.2)
            sock = ghost.w.get_extra_info("socket")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            ghost.close()
            for _ in range(40):
                if not srv.stream_waiters:
                    break
                await asyncio.sleep(0.1)
            assert not srv.stream_waiters

        assert serve(srv, scenario) == []


class TestReplicationForwarding:
    def test_followers_receive_the_assigned_id_not_a_star(self, srv):
        """The master must forward XADD with its own ID so replicas converge."""

        async def scenario(connect, errors):
            class FakeFollower:
                def __init__(self):
                    self.buf = bytearray()

                def write(self, data):
                    self.buf.extend(data)

                async def drain(self):
                    pass

            follower = FakeFollower()
            srv.connected_replicas.append(follower)
            try:
                c = await connect()
                sid = await c.call("XADD", "s", "*", "k", "v")
                await asyncio.sleep(0.1)
            finally:
                srv.connected_replicas.remove(follower)
            assert f"S{len(sid)}\n{sid}\n".encode() in bytes(follower.buf)
            assert b"S1\n*\n" not in bytes(follower.buf)

        assert serve(srv, scenario) == []
