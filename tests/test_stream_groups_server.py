"""Consumer groups over real TCP sockets against the real async server:
nested replies, XREADGROUP ... BLOCK (wake-ups, timeouts, races, a group that
disappears, disconnects), the internal XGAPPLY, and replica forwarding."""

import asyncio
import io
import time

import pytest
from rich.console import Console

from kedis_python.commands import CommandHandler
from kedis_python.parser import CommandParser
from kedis_python.store import KedisStore
from test_stream_server import serve  # shared wire helper


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


async def setup_stream(c, n=3):
    for i in range(n):
        await c.call("XADD", "s", f"{i + 1}-0", "n", str(i))
    assert await c.call("XGROUP", "CREATE", "s", "g", "0") == "OK"


class TestWire:
    def test_group_commands_round_trip(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await setup_stream(c)
            assert await c.call("XREADGROUP", "GROUP", "g", "a", "COUNT", "2", "STREAMS", "s", ">") == [
                ["s", [["1-0", ["n", "0"]], ["2-0", ["n", "1"]]]]
            ]
            assert await c.call("XPENDING", "s", "g") == [2, "1-0", "2-0", [["a", "2"]]]
            rows = await c.call("XPENDING", "s", "g", "-", "+", "10")
            assert [(r[0], r[1], r[3]) for r in rows] == [("1-0", "a", 1), ("2-0", "a", 1)]
            assert await c.call("XACK", "s", "g", "1-0", "9-9") == 1
            assert await c.call("XREADGROUP", "GROUP", "g", "a", "STREAMS", "s", "0") == [
                ["s", [["2-0", ["n", "1"]]]]
            ]
            info = await c.call("XINFO", "GROUPS", "s")
            assert info[0][:8] == ["name", "g", "consumers", 1, "pending", 1, "last-delivered-id", "2-0"]
            assert await c.call("XGROUP", "DELCONSUMER", "s", "g", "a") == 1
            assert await c.call("XGROUP", "DESTROY", "s", "g") == 1

        assert serve(srv, scenario) == []

    def test_errors_arrive_as_error_replies(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await setup_stream(c, 1)
            busy = await c.call("XGROUP", "CREATE", "s", "g", "0")
            assert busy[0] == "ERR" and busy[1].startswith("BUSYGROUP")
            nogroup = await c.call("XREADGROUP", "GROUP", "x", "c", "STREAMS", "s", ">")
            assert nogroup[0] == "ERR" and nogroup[1].startswith("NOGROUP")
            assert await c.call("XLEN", "s") == 1  # the connection is still healthy

        assert serve(srv, scenario) == []

    def test_user_data_that_looks_like_a_status_arrives_as_data(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await c.call("XADD", "s", "1-0", "level", "ERROR disk full", "ack", "OK")
            await c.call("XGROUP", "CREATE", "s", "g", "0")
            assert await c.call("XREADGROUP", "GROUP", "g", "c", "STREAMS", "s", ">") == [
                ["s", [["1-0", ["level", "ERROR disk full", "ack", "OK"]]]]
            ]

        assert serve(srv, scenario) == []

    def test_a_client_cannot_call_xgapply(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await setup_stream(c, 1)
            reply = await c.call("XGAPPLY", "XGLAST", "s", "g", "1-0")
            assert reply[0] == "ERR" and "unknown command" in reply[1]
            info = await c.call("XINFO", "GROUPS", "s")
            assert info[0][7] == "0-0"  # the cursor did not move

        assert serve(srv, scenario) == []

    def test_a_read_only_replica_refuses_group_writes(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await setup_stream(c, 1)
            srv.server_role = "replica"
            try:
                for tokens in (
                    ("XREADGROUP", "GROUP", "g", "c", "STREAMS", "s", ">"),
                    ("XGROUP", "DESTROY", "s", "g"),
                    ("XACK", "s", "g", "1-0"),
                ):
                    assert "EREADONLY" in await c.call(*tokens)
                assert await c.call("XPENDING", "s", "g") == [0, None, None, None]  # reads still work
            finally:
                srv.server_role = "master"

        assert serve(srv, scenario) == []


class TestBlocking:
    def test_wakes_when_a_writer_adds_an_entry(self, srv):
        async def scenario(connect, errors):
            reader, writer = await connect(), await connect()
            await writer.call("XGROUP", "CREATE", "s", "g", "$", "MKSTREAM")
            await reader.send("XREADGROUP", "GROUP", "g", "c", "BLOCK", "5000", "STREAMS", "s", ">")
            await asyncio.sleep(0.2)
            started = time.monotonic()
            await writer.call("XADD", "s", "1-0", "k", "v")
            assert await asyncio.wait_for(reader.read(), 2) == [["s", [["1-0", ["k", "v"]]]]]
            assert time.monotonic() - started < 1.0  # woken, not timed out
            assert await writer.call("XPENDING", "s", "g") == [1, "1-0", "1-0", [["c", "1"]]]

        assert serve(srv, scenario) == []

    def test_returns_immediately_when_data_is_waiting(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await setup_stream(c, 1)
            started = time.monotonic()
            reply = await c.call("XREADGROUP", "GROUP", "g", "c", "BLOCK", "5000", "STREAMS", "s", ">")
            assert reply == [["s", [["1-0", ["n", "0"]]]]]
            assert time.monotonic() - started < 1.0

        assert serve(srv, scenario) == []

    def test_times_out_with_nil(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await setup_stream(c, 1)
            await c.call("XREADGROUP", "GROUP", "g", "c", "STREAMS", "s", ">")
            started = time.monotonic()
            assert await c.call("XREADGROUP", "GROUP", "g", "c", "BLOCK", "300", "STREAMS", "s", ">") is None
            assert 0.25 <= time.monotonic() - started < 1.5
            assert await c.call("XLEN", "s") == 1

        assert serve(srv, scenario) == []

    def test_block_zero_waits_until_data_arrives(self, srv):
        async def scenario(connect, errors):
            reader, writer = await connect(), await connect()
            await writer.call("XGROUP", "CREATE", "s", "g", "$", "MKSTREAM")
            await reader.send("XREADGROUP", "GROUP", "g", "c", "BLOCK", "0", "STREAMS", "s", ">")
            await asyncio.sleep(0.8)  # longer than the poll interval: still waiting
            await writer.call("XADD", "s", "1-0", "k", "v")
            assert await asyncio.wait_for(reader.read(), 3) == [["s", [["1-0", ["k", "v"]]]]]

        assert serve(srv, scenario) == []

    def test_a_write_right_after_blocking_is_never_missed(self, srv):
        async def scenario(connect, errors):
            reader, writer = await connect(), await connect()
            await writer.call("XGROUP", "CREATE", "s", "g", "$", "MKSTREAM")
            for i in range(1, 16):
                await reader.send("XREADGROUP", "GROUP", "g", "c", "BLOCK", "3000", "STREAMS", "s", ">")
                await writer.send("XADD", "s", f"{i}-0", "n", str(i))  # no sleep: race
                got = await asyncio.wait_for(reader.read(), 2)
                assert got == [["s", [[f"{i}-0", ["n", str(i)]]]]], f"iteration {i}"
                await writer.read()

        assert serve(srv, scenario) == []

    def test_blocked_consumers_of_one_group_never_get_the_same_entry(self, srv):
        async def scenario(connect, errors):
            a, b, writer = await connect(), await connect(), await connect()
            await writer.call("XGROUP", "CREATE", "s", "g", "$", "MKSTREAM")
            await a.send("XREADGROUP", "GROUP", "g", "a", "BLOCK", "5000", "STREAMS", "s", ">")
            await b.send("XREADGROUP", "GROUP", "g", "b", "BLOCK", "5000", "STREAMS", "s", ">")
            await asyncio.sleep(0.2)
            reads = {asyncio.ensure_future(a.read()), asyncio.ensure_future(b.read())}
            await writer.call("XADD", "s", "1-0", "k", "one")
            # Both are woken; exactly one may take the entry. The second entry is added
            # only once someone has it, or the first consumer to run could take both
            # (which one runs first depends on the event loop, not on the server).
            done, rest = await asyncio.wait(reads, timeout=3, return_when=asyncio.FIRST_COMPLETED)
            assert len(done) == 1
            await writer.call("XADD", "s", "2-0", "k", "two")
            done2, _ = await asyncio.wait(rest, timeout=3)
            assert len(done2) == 1
            delivered = sorted(t.result()[0][1][0][0] for t in done | done2)
            assert delivered == ["1-0", "2-0"]
            assert await writer.call("XPENDING", "s", "g", "-", "+", "10") != []

        assert serve(srv, scenario) == []

    def test_unrelated_writes_do_not_end_the_block_early(self, srv):
        async def scenario(connect, errors):
            reader, writer = await connect(), await connect()
            await writer.call("XGROUP", "CREATE", "wanted", "g", "$", "MKSTREAM")
            started = time.monotonic()
            await reader.send("XREADGROUP", "GROUP", "g", "c", "BLOCK", "800", "STREAMS", "wanted", ">")
            await asyncio.sleep(0.1)
            for i in range(1, 4):
                await writer.call("XADD", "other", f"{i}-0", "k", "v")
            assert await reader.read() is None
            assert time.monotonic() - started >= 0.7

        assert serve(srv, scenario) == []

    def test_reading_history_never_blocks(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await setup_stream(c, 1)
            started = time.monotonic()
            reply = await c.call("XREADGROUP", "GROUP", "g", "c", "BLOCK", "5000", "STREAMS", "s", "0")
            assert reply == [["s", []]]
            assert time.monotonic() - started < 1.0

        assert serve(srv, scenario) == []

    def test_a_group_destroyed_while_blocked_ends_the_wait_with_nogroup(self, srv):
        async def scenario(connect, errors):
            reader, admin = await connect(), await connect()
            await admin.call("XGROUP", "CREATE", "s", "g", "$", "MKSTREAM")
            await reader.send("XREADGROUP", "GROUP", "g", "c", "BLOCK", "5000", "STREAMS", "s", ">")
            await asyncio.sleep(0.2)
            started = time.monotonic()
            assert await admin.call("XGROUP", "DESTROY", "s", "g") == 1
            reply = await asyncio.wait_for(reader.read(), 2)
            assert reply[0] == "ERR" and reply[1].startswith("NOGROUP")
            assert time.monotonic() - started < 1.0

        assert serve(srv, scenario) == []

    def test_errors_reply_without_blocking(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await setup_stream(c, 1)
            for tokens, text in (
                (("XREADGROUP", "GROUP", "g", "c", "BLOCK", "5000", "STREAMS", "s"), "Unbalanced"),
                (("XREADGROUP", "GROUP", "g", "c", "BLOCK", "5000", "STREAMS", "s", "$"), "meaningless"),
                (("XREADGROUP", "GROUP", "x", "c", "BLOCK", "5000", "STREAMS", "s", ">"), "NOGROUP"),
            ):
                started = time.monotonic()
                reply = await asyncio.wait_for(c.call(*tokens), 2)
                assert reply[0] == "ERR" and text in reply[1]
                assert time.monotonic() - started < 1.0

        assert serve(srv, scenario) == []

    def test_a_client_that_leaves_while_blocked_is_cleaned_up(self, srv):
        async def scenario(connect, errors):
            ghost, other = await connect(), await connect()
            await other.call("XGROUP", "CREATE", "s", "g", "$", "MKSTREAM")
            await ghost.send("XREADGROUP", "GROUP", "g", "c", "BLOCK", "0", "STREAMS", "s", ">")
            await asyncio.sleep(0.2)
            assert len(srv.stream_waiters) == 1
            ghost.close()
            for _ in range(40):
                if not srv.stream_waiters:
                    break
                await asyncio.sleep(0.1)
            assert not srv.stream_waiters
            assert await other.call("XADD", "s", "1-0", "k", "v") == "1-0"

        assert serve(srv, scenario) == []

    def test_blocking_xread_with_a_bad_id_replies_with_an_error(self, srv):
        """Regression: the shared wait helper must turn a StreamError into an
        error reply instead of letting it escape and drop the connection."""

        async def scenario(connect, errors):
            c = await connect()
            await c.call("XADD", "s", "1-0", "k", "v")
            reply = await asyncio.wait_for(c.call("XREAD", "BLOCK", "300", "STREAMS", "s", "zzz"), 2)
            assert reply[0] == "ERR" and "Invalid stream ID" in reply[1]
            assert await c.call("XLEN", "s") == 1  # still connected

        assert serve(srv, scenario) == []


class FakeFollower:
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


class TestReplicationForwarding:
    def test_followers_get_resolved_records_never_the_raw_command(self, srv):
        async def scenario(connect, errors):
            follower = FakeFollower()
            srv.connected_replicas.append(follower)
            try:
                c = await connect()
                await c.call("XADD", "s", "1-0", "k", "v")
                await c.call("XGROUP", "CREATE", "s", "g", "0")
                await c.call("XREADGROUP", "GROUP", "g", "c", "STREAMS", "s", ">")
                await c.call("XACK", "s", "g", "1-0")
                await asyncio.sleep(0.1)
            finally:
                srv.connected_replicas.remove(follower)
            sent = follower.commands()
            names = [t[0] for t in sent]
            assert names == ["XADD", "XGAPPLY", "XGAPPLY", "XGAPPLY", "XGAPPLY", "XGAPPLY"]
            assert [t[1] for t in sent[1:]] == ["XGROUP", "XGCONS", "XGPEL", "XGLAST", "XACK"]
            assert not any(t[0] in ("XREADGROUP", "XGROUP", "XACK") for t in sent)

        assert serve(srv, scenario) == []

    def test_a_read_that_changes_nothing_sends_nothing(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await setup_stream(c, 1)
            await c.call("XREADGROUP", "GROUP", "g", "c", "STREAMS", "s", ">")
            follower = FakeFollower()
            srv.connected_replicas.append(follower)
            try:
                assert await c.call("XREADGROUP", "GROUP", "g", "c", "STREAMS", "s", ">") is None
                assert (
                    await c.call("XREADGROUP", "GROUP", "g", "c", "BLOCK", "200", "STREAMS", "s", ">") is None
                )
                await asyncio.sleep(0.1)
            finally:
                srv.connected_replicas.remove(follower)
            assert follower.commands() == []

        assert serve(srv, scenario) == []

    def test_a_blocked_read_forwards_what_it_delivered(self, srv):
        async def scenario(connect, errors):
            reader, writer = await connect(), await connect()
            await writer.call("XGROUP", "CREATE", "s", "g", "$", "MKSTREAM")
            follower = FakeFollower()
            srv.connected_replicas.append(follower)
            try:
                await reader.send("XREADGROUP", "GROUP", "g", "c", "BLOCK", "5000", "STREAMS", "s", ">")
                await asyncio.sleep(0.2)
                await writer.call("XADD", "s", "1-0", "k", "v")
                await asyncio.wait_for(reader.read(), 2)
                await asyncio.sleep(0.2)
            finally:
                srv.connected_replicas.remove(follower)
            assert [t[1] for t in follower.commands() if t[0] == "XGAPPLY"] == ["XGCONS", "XGPEL", "XGLAST"]

        assert serve(srv, scenario) == []

    def test_a_new_consumer_that_times_out_is_still_forwarded(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await setup_stream(c, 1)
            await c.call("XREADGROUP", "GROUP", "g", "old", "STREAMS", "s", ">")
            follower = FakeFollower()
            srv.connected_replicas.append(follower)
            try:
                reply = await c.call(
                    "XREADGROUP", "GROUP", "g", "newbie", "BLOCK", "200", "STREAMS", "s", ">"
                )
                assert reply is None
                await asyncio.sleep(0.1)
            finally:
                srv.connected_replicas.remove(follower)
            sent = follower.commands()
            assert [t[:4] for t in sent] == [["XGAPPLY", "XGCONS", "s", "g"]]
            assert sent[0][4] == "newbie"

        assert serve(srv, scenario) == []

    def test_a_new_consumer_whose_client_leaves_is_still_forwarded(self, srv):
        async def scenario(connect, errors):
            ghost, other = await connect(), await connect()
            await other.call("XGROUP", "CREATE", "s", "g", "$", "MKSTREAM")
            follower = FakeFollower()
            srv.connected_replicas.append(follower)
            try:
                await ghost.send("XREADGROUP", "GROUP", "g", "ghost", "BLOCK", "0", "STREAMS", "s", ">")
                await asyncio.sleep(0.2)
                ghost.close()
                for _ in range(40):
                    if follower.buf:
                        break
                    await asyncio.sleep(0.1)
            finally:
                srv.connected_replicas.remove(follower)
            sent = follower.commands()
            assert [t[:3] for t in sent] == [["XGAPPLY", "XGCONS", "s"]] and sent[0][4] == "ghost"
            assert await other.call("XADD", "s", "1-0", "k", "v") == "1-0"  # server fine

        assert serve(srv, scenario) == []

    def test_a_replica_fed_the_slipstream_ends_up_identical(self, srv, tmp_path, monkeypatch):
        sent = []

        async def scenario(connect, errors):
            follower = FakeFollower()
            srv.connected_replicas.append(follower)
            try:
                c = await connect()
                for i in range(4):
                    await c.call("XADD", "s", "*", "n", str(i))
                await c.call("XGROUP", "CREATE", "s", "g", "0")
                await c.call("XREADGROUP", "GROUP", "g", "a", "COUNT", "2", "STREAMS", "s", ">")
                await c.call("XREADGROUP", "GROUP", "g", "b", "STREAMS", "s", ">")
                await c.call("XREADGROUP", "GROUP", "g", "a", "STREAMS", "s", "0")
                first = (await c.call("XPENDING", "s", "g", "-", "+", "1"))[0][0]
                await c.call("XACK", "s", "g", first)
                await c.call("XGROUP", "CREATECONSUMER", "s", "g", "idle")
                await asyncio.sleep(0.1)
            finally:
                srv.connected_replicas.remove(follower)
            sent.extend(follower.commands())

        assert serve(srv, scenario) == []

        sub = tmp_path / "replica"
        sub.mkdir()
        monkeypatch.chdir(sub)
        replica = KedisStore(aof_filename="r.aof", appendfsync="always", lru_maxsize=100_000)
        monkeypatch.chdir(tmp_path)
        try:
            rh = CommandHandler(replica)
            for tokens in sent:
                assert not str(rh.execute(tokens, None)).startswith("-"), tokens
            master = srv.global_store._data["s"].to_state()
            copy = replica._data["s"].to_state()
            assert copy["entries"] == master["entries"]
            assert copy["groups"] == master["groups"]
        finally:
            replica.shutdown()
