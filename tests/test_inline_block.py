"""BLOCK on a stream read only waits where a writer can wake it: a client of
the async server. Executed inline (inside MULTI/EXEC, or in the standalone CLI
where no other client exists) it answers at once, as Redis does in MULTI."""

import io
import time

import pytest
from rich.console import Console

from kedis_python.commands import CommandHandler
from kedis_python.store import KedisStore
from test_stream_server import serve


class TestRequestedBlockMs:
    @pytest.mark.parametrize(
        "line,expected",
        [
            ("XREAD BLOCK 5000 STREAMS s $", 5000),
            ("xread block 250 streams s 0-0", 250),
            ("XREAD COUNT 2 BLOCK 0 STREAMS s $", 0),
            ("XREADGROUP GROUP g c BLOCK 3000 STREAMS s >", 3000),
            ("xreadgroup group g c block 0 noack streams s >", 0),
        ],
    )
    def test_reports_the_timeout(self, line, expected):
        assert CommandHandler.requested_block_ms(line.split()) == expected

    @pytest.mark.parametrize(
        "line",
        [
            "XREAD STREAMS s $",
            "XREADGROUP GROUP g c STREAMS s >",
            "XREAD BLOCK -1 STREAMS s $",  # malformed
            "XREAD BLOCK x STREAMS s $",
            "XREADGROUP GROUP g c BLOCK 5 STREAMS s",  # unbalanced
            "XRANGE s - +",
            "GET k",
            "BLOCK 5",
            "",
        ],
    )
    def test_none_when_there_is_nothing_to_wait_for(self, line):
        assert CommandHandler.requested_block_ms(line.split()) is None

    def test_empty_token_list(self):
        assert CommandHandler.requested_block_ms([]) is None


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    yield CommandHandler(store)
    store.shutdown()


class TestInlineNeverWaits:
    def test_handler_level(self, h):
        h.execute("XADD s 1-0 k v".split())
        h.execute("XGROUP CREATE s g 0".split())
        h.execute("XREADGROUP GROUP g c STREAMS s >".split())
        started = time.monotonic()
        assert h.execute("XREAD BLOCK 5000 STREAMS s $".split()) is None
        assert h.execute("XREADGROUP GROUP g c BLOCK 5000 STREAMS s >".split()) is None
        assert time.monotonic() - started < 0.5


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    server.stream_waiters.clear()
    yield server
    server.stream_waiters.clear()
    server.global_store.flushall()


async def status(c, *tokens):
    """Hand-written replies (MULTI, queued commands) end in \r\n: compare the text."""
    return str(await c.call(*tokens)).strip()


class TestInsideMulti:
    def test_blocking_reads_do_not_block_a_transaction(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await c.call("XADD", "s", "1-0", "k", "v")
            await c.call("XGROUP", "CREATE", "s", "g", "$")
            assert await status(c, "MULTI") == "OK"
            assert await status(c, "XREAD", "BLOCK", "5000", "STREAMS", "s", "$") == "OK"  # this server acks queued commands with +OK
            assert await status(c, "XREADGROUP", "GROUP", "g", "c", "BLOCK", "5000", "STREAMS", "s", ">") == "OK"  # this server acks queued commands with +OK
            assert await status(c, "XLEN", "s") == "OK"  # this server acks queued commands with +OK
            started = time.monotonic()
            assert await c.call("EXEC") == [None, None, 1]  # nil, nil, then XLEN
            assert time.monotonic() - started < 1.0

        assert serve(srv, scenario) == []

    def test_a_transaction_still_sees_data_that_is_already_there(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            await c.call("XADD", "s", "1-0", "k", "v")
            await c.call("MULTI")
            await c.call("XREAD", "BLOCK", "5000", "STREAMS", "s", "0-0")
            assert await c.call("EXEC") == [[["s", [["1-0", ["k", "v"]]]]]]

        assert serve(srv, scenario) == []
