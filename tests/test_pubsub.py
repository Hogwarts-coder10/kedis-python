"""Pub/Sub: PUBLISH delivers to every live subscriber and replies with how
many it reached, over the real async server (the subscriber is an asyncio
StreamWriter there, not a raw socket) and for plain-socket subscribers."""

import asyncio
import io

import pytest
from rich.console import Console
from test_stream_server import serve  # shared wire helper

from kedis_python.commands import CommandHandler
from kedis_python.store import KedisStore


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # server.py builds its global store in the cwd on import
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    server.global_handler._channels.clear()
    yield server
    server.global_handler._channels.clear()
    server.global_store.flushall()


async def subscribe(connect, channel):
    c = await connect()
    assert await c.call("SUBSCRIBE", channel) == ["subscribe", channel, 1]
    return c


class TestOverTheWire:
    def test_a_subscriber_receives_the_message_and_publish_counts_it(self, srv):
        async def scenario(connect, errors):
            sub, pub = await subscribe(connect, "news"), await connect()
            assert await pub.call("PUBLISH", "news", "hello") == 1
            assert await asyncio.wait_for(sub.read(), 3) == ["message", "news", "hello"]

        assert serve(srv, scenario) == []

    def test_every_subscriber_gets_it_and_the_count_matches(self, srv):
        async def scenario(connect, errors):
            subs = [await subscribe(connect, "news") for _ in range(3)]
            pub = await connect()
            assert await pub.call("PUBLISH", "news", "hi") == 3
            for s in subs:
                assert await asyncio.wait_for(s.read(), 3) == ["message", "news", "hi"]

        assert serve(srv, scenario) == []

    def test_only_the_channels_subscribers_receive_it(self, srv):
        async def scenario(connect, errors):
            a, b = await subscribe(connect, "a"), await subscribe(connect, "b")
            pub = await connect()
            assert await pub.call("PUBLISH", "a", "x") == 1
            assert await asyncio.wait_for(a.read(), 3) == ["message", "a", "x"]
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(b.read(), 0.3)

        assert serve(srv, scenario) == []

    def test_messages_arrive_in_order(self, srv):
        async def scenario(connect, errors):
            sub, pub = await subscribe(connect, "n"), await connect()
            for i in range(20):
                assert await pub.call("PUBLISH", "n", str(i)) == 1
            got = [(await asyncio.wait_for(sub.read(), 3))[2] for _ in range(20)]
            assert got == [str(i) for i in range(20)]

        assert serve(srv, scenario) == []

    def test_multi_word_and_unicode_messages(self, srv):
        async def scenario(connect, errors):
            sub, pub = await subscribe(connect, "n"), await connect()
            assert await pub.call("PUBLISH", "n", "hello", "wörld", "🏎️") == 1
            assert await asyncio.wait_for(sub.read(), 3) == ["message", "n", "hello wörld 🏎️"]

        assert serve(srv, scenario) == []

    def test_a_subscriber_that_left_is_not_counted_and_is_cleaned_up(self, srv):
        async def scenario(connect, errors):
            sub, stay, pub = (
                await subscribe(connect, "n"),
                await subscribe(connect, "n"),
                await connect(),
            )
            sub.close()
            await asyncio.sleep(0.3)
            assert await pub.call("PUBLISH", "n", "x") == 1
            assert await asyncio.wait_for(stay.read(), 3) == ["message", "n", "x"]
            assert len(srv.global_handler._channels["n"]) == 1

        assert serve(srv, scenario) == []

    def test_a_channel_with_no_live_subscribers_is_dropped(self, srv):
        async def scenario(connect, errors):
            sub, pub = await subscribe(connect, "n"), await connect()
            sub.close()
            await asyncio.sleep(0.3)
            assert await pub.call("PUBLISH", "n", "x") == 0
            assert "n" not in srv.global_handler._channels
            assert await pub.call("PUBLISH", "n", "y") == 0

        assert serve(srv, scenario) == []

    def test_no_subscribers_replies_zero(self, srv):
        async def scenario(connect, errors):
            pub = await connect()
            assert await pub.call("PUBLISH", "nobody", "x") == 0

        assert serve(srv, scenario) == []

    def test_a_subscriber_can_still_run_commands(self, srv):
        async def scenario(connect, errors):
            sub, pub = await subscribe(connect, "n"), await connect()
            await sub.call("SET", "k", "v")
            assert await sub.call("GET", "k") == "v"
            assert await pub.call("PUBLISH", "n", "still here") == 1
            assert await asyncio.wait_for(sub.read(), 3) == ["message", "n", "still here"]

        assert serve(srv, scenario) == []

    def test_publishing_to_your_own_channel(self, srv):
        async def scenario(connect, errors):
            c = await subscribe(connect, "n")
            await c.send("PUBLISH", "n", "me")
            # The push is queued onto the loop before the reply is written, so
            # it can arrive first: accept either order, require both.
            got = [await asyncio.wait_for(c.read(), 3) for _ in range(2)]
            assert sorted(got, key=str) == sorted([1, ["message", "n", "me"]], key=str)

        assert serve(srv, scenario) == []

    def test_arity(self, srv):
        async def scenario(connect, errors):
            c = await connect()
            assert (await c.call("PUBLISH", "n"))[0] == "ERR"

        assert serve(srv, scenario) == []


class FakeWriter:
    def __init__(self, closing=False):
        self.closing, self.sent = closing, []

    def is_closing(self):
        return self.closing

    def write(self, data):
        self.sent.append(data)


class FakeSocket:
    def __init__(self, broken=False):
        self.broken, self.sent = broken, []

    def sendall(self, data):
        if self.broken:
            raise BrokenPipeError
        self.sent.append(data)


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    yield CommandHandler(store)
    store.shutdown()


class TestHandlerDirectly:
    PAYLOAD = b"A3\nS7\nmessage\nS1\nc\nS2\nhi\n"

    def test_the_count_and_the_wire_bytes(self, h):
        w, s = FakeWriter(), FakeSocket()
        h.execute(["SUBSCRIBE", "c"], w)
        h.execute(["SUBSCRIBE", "c"], s)
        assert h.execute(["PUBLISH", "c", "hi"]) == 2
        assert w.sent == [self.PAYLOAD] and s.sent == [self.PAYLOAD]

    def test_closing_writers_and_broken_sockets_are_dropped_not_counted(self, h):
        live, closing, broken = FakeWriter(), FakeWriter(closing=True), FakeSocket(broken=True)
        for sub in (live, closing, broken):
            h.execute(["SUBSCRIBE", "c"], sub)
        assert h.execute(["PUBLISH", "c", "hi"]) == 1
        assert h._channels["c"] == [live]
        assert closing.sent == []

    def test_a_writer_is_written_via_the_loop_when_one_is_running(self, h):
        calls = []

        class Loop:
            def is_running(self):
                return True

            def call_soon_threadsafe(self, fn, *args):
                calls.append((fn, args))

        w = FakeWriter()
        h.event_loop = Loop()
        h.execute(["SUBSCRIBE", "c"], w)
        assert h.execute(["PUBLISH", "c", "hi"]) == 1
        assert w.sent == [] and calls == [(w.write, (self.PAYLOAD,))]

    def test_the_same_client_is_subscribed_once(self, h):
        w = FakeWriter()
        h.execute(["SUBSCRIBE", "c"], w)
        h.execute(["SUBSCRIBE", "c"], w)
        assert h.execute(["PUBLISH", "c", "hi"]) == 1

    def test_publish_is_never_logged_to_the_aof(self, h):
        h.execute(["SUBSCRIBE", "c"], FakeWriter())
        h.execute(["PUBLISH", "c", "hi"])
        h.store.shutdown()
        import os

        assert not os.path.exists("test.aof") or "PUBLISH" not in open("test.aof").read()
