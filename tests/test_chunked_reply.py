"""Replies are encoded in slices that hand the event loop back in between, and
sent in one go: no stall for every client, and still one complete frame or one
error line."""

import asyncio
import io
import time
from collections import deque

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from rich.console import Console

from kedis_python.parser import (
    CommandParser,
    KESPDecoder,
    KESPEncoder,
    ReplyEncodeError,
)


def kesp(*args: bytes) -> bytes:
    return b"A%d\n" % len(args) + b"".join(b"S%d\n%s\n" % (len(a), a) for a in args)


def chunked(data, chunk):
    """What a caller of encode_chunks does: all pieces, or the one failure line."""
    try:
        return b"".join(KESPEncoder.encode_chunks(data, chunk))
    except ReplyEncodeError as exc:
        return KESPEncoder.failure_reply(exc)


# --- encode_chunks: the same bytes as encode() ----------------------------------------------------------

scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.text(max_size=12),
    st.sampled_from(["OK", "+OK", "-ERR boom", "ERROR x", "\ud800", "é", ""]),
)
values = st.recursive(
    scalars,
    lambda inner: st.one_of(
        st.lists(inner, max_size=8),
        st.tuples(inner, inner),
        st.dictionaries(st.text(max_size=6), inner, max_size=6),
    ),
    max_leaves=25,
)
replies = st.one_of(
    values,
    st.lists(values, max_size=40),
    st.frozensets(st.text(max_size=5), max_size=40).map(set),
    st.dictionaries(st.text(max_size=6), values, max_size=30),
)


@given(data=replies, chunk=st.integers(min_value=1, max_value=7))
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_pieces_always_add_up_to_what_encode_returns(data, chunk):
    assert chunked(data, chunk) == KESPEncoder.encode(data)


@pytest.mark.parametrize("n", [0, 1, 511, 512, 513, 1024, 1025, 5000])
def test_every_size_around_the_chunk_boundary(n):
    for data in (
        ["item%d" % i for i in range(n)],
        tuple("item%d" % i for i in range(n)),
        {"k%d" % i: "v%d" % i for i in range(n)},
        {"m%d" % i for i in range(n)},
    ):
        assert chunked(data, 512) == KESPEncoder.encode(data)


def test_a_small_reply_is_a_single_piece_and_a_big_one_is_a_header_and_slices():
    assert len(list(KESPEncoder.encode_chunks(["a"] * 512, 512))) == 1
    assert len(list(KESPEncoder.encode_chunks("OK", 512))) == 1
    assert len(list(KESPEncoder.encode_chunks(None, 512))) == 1
    assert len(list(KESPEncoder.encode_chunks({"k": "v"}, 512))) == 1
    pieces = list(KESPEncoder.encode_chunks(["a"] * 1000, 100))
    assert len(pieces) == 11 and pieces[0] == b"A1000\n"
    pieces = list(KESPEncoder.encode_chunks({str(i): "v" for i in range(1000)}, 100))
    assert len(pieces) == 11 and pieces[0] == b"A2000\n"


def test_status_and_error_looking_strings_inside_a_big_reply_are_data():
    tricky = ["OK", "+OK", "-ERR x", "ERROR y", "+QUEUED"]
    as_dict = {"k%d" % i: tricky[i % 5] for i in range(300)}
    as_list = [tricky[i % 5] for i in range(300)]
    assert chunked(as_dict, 100) == KESPEncoder.encode(as_dict)
    assert chunked(as_list, 100) == KESPEncoder.encode(as_list)
    assert b"S2\nOK\n" in KESPEncoder.encode(as_list)  # a bulk string, not "+OK"


def test_nested_replies_are_sliced_at_the_top_level_only():
    reply = [["id%d" % i, ["field", "value"]] for i in range(1000)]
    assert len(list(KESPEncoder.encode_chunks(reply, 100))) == 11
    assert chunked(reply, 100) == KESPEncoder.encode(reply)


# --- failures: the same single error line, and why the caller must hold the pieces ------------------------


def test_a_late_failure_raises_after_earlier_pieces_were_already_made():
    data = ["fine"] * 3000 + ["\ud800 not valid utf-8"]
    made = []
    with pytest.raises(ReplyEncodeError, match="UTF-8"):
        for piece in KESPEncoder.encode_chunks(data, 100):
            made.append(piece)
    assert len(made) > 10  # these must never reach the wire on their own
    assert chunked(data, 100) == KESPEncoder.encode(data)
    assert KESPEncoder.encode(data).startswith(b"EInternal server error: string is not valid UTF-8")


@pytest.mark.parametrize(
    "limit, value, data",
    [
        (("KESPDecoder", "MAX_REPLY_ELEMENTS"), 1500, ["x"] * 2000),
        (("KESPDecoder", "MAX_REPLY_ELEMENTS"), 1500, {str(i): "v" for i in range(2000)}),
        (("KESPDecoder", "MAX_REPLY_PAYLOAD"), 5000, ["x" * 10] * 2000),
        (("CommandParser", "MAX_ARRAY_LENGTH"), 1500, ["x"] * 2000),
        (("CommandParser", "MAX_ARRAY_LENGTH"), 1500, {str(i): "v" for i in range(1000)}),
        (("KESPDecoder", "MAX_DEPTH"), 3, [[[[["deep"]]]]] + ["x"] * 2000),
        # The up-front checks pass; the running budget runs out part-way. Keys
        # count too: without them this dict (1,801 elements) would fit.
        (("KESPDecoder", "MAX_REPLY_ELEMENTS"), 2000, {str(i): ["a", "b"] for i in range(600)}),
        (("KESPDecoder", "MAX_REPLY_ELEMENTS"), 2000, [["a"]] * 1500),
    ],
)
def test_every_budget_failure_gives_the_same_error_line_as_encode(monkeypatch, limit, value, data):
    owner = {"KESPDecoder": KESPDecoder, "CommandParser": CommandParser}[limit[0]]
    monkeypatch.setattr(owner, limit[1], value)
    expected = KESPEncoder.encode(data)
    assert expected.startswith(b"EInternal server error")
    assert chunked(data, 100) == expected


@pytest.mark.parametrize(
    "limit, value, data",
    [
        (("KESPDecoder", "MAX_REPLY_ELEMENTS"), 1500, ["x"] * 2000),
        (("KESPDecoder", "MAX_REPLY_ELEMENTS"), 1500, {str(i): "v" for i in range(2000)}),
        (("CommandParser", "MAX_ARRAY_LENGTH"), 1500, ["x"] * 2000),
        (("CommandParser", "MAX_ARRAY_LENGTH"), 1500, {str(i): "v" for i in range(1000)}),
    ],
)
def test_a_reply_that_is_over_budget_by_size_is_refused_before_any_slice_is_encoded(
    monkeypatch, limit, value, data
):
    owner = {"KESPDecoder": KESPDecoder, "CommandParser": CommandParser}[limit[0]]
    monkeypatch.setattr(owner, limit[1], value)
    encoded = []
    real = KESPEncoder._encode
    monkeypatch.setattr(
        KESPEncoder, "_encode", staticmethod(lambda *a: encoded.append(1) or real(*a))
    )
    with pytest.raises(ReplyEncodeError):
        next(KESPEncoder.encode_chunks(data, 100))
    assert encoded == []  # no work was spent on a reply that cannot be sent


# --- encode_reply: hands the loop back between slices -----------------------------------------------------


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    yield server
    server.global_store.flushall()


async def loop_turns_during(coro):
    """How many times the rest of the loop got to run while coro was running."""
    turns = 0
    stop = False

    async def counter():
        nonlocal turns
        while not stop:
            turns += 1
            await asyncio.sleep(0)

    task = asyncio.ensure_future(counter())
    await asyncio.sleep(0)
    before = turns
    result = await coro
    gained = turns - before
    stop = True
    await task
    return result, gained


def test_a_big_reply_lets_the_loop_run_between_slices(srv, monkeypatch):
    monkeypatch.setattr(srv, "ENCODE_CHUNK", 100)
    data = ["item%d" % i for i in range(5000)]

    async def scenario():
        parts, turns = await loop_turns_during(srv.encode_reply(data))
        assert b"".join(parts) == KESPEncoder.encode(data)
        assert turns >= 40  # 50 slices: the loop ran in between, not only at the end

    asyncio.run(scenario())


def test_a_reply_of_one_slice_or_less_never_yields(srv, monkeypatch):
    monkeypatch.setattr(srv, "ENCODE_CHUNK", 100)

    async def scenario():
        for data in (["a"] * 100, "OK", None, 7, {"k": "v"}):
            parts, turns = await loop_turns_during(srv.encode_reply(data))
            assert b"".join(parts) == KESPEncoder.encode(data)
            assert turns == 0

    asyncio.run(scenario())


def test_a_reply_that_cannot_be_encoded_is_one_error_part_not_half_a_frame(srv, monkeypatch):
    monkeypatch.setattr(srv, "ENCODE_CHUNK", 100)
    data = ["fine"] * 3000 + ["\ud800"]

    async def scenario():
        parts = await srv.encode_reply(data)
        assert parts == [KESPEncoder.encode(data)]
        assert parts[0].startswith(b"E") and parts[0].count(b"\n") == 1

    asyncio.run(scenario())


# --- send_parts: written together ---------------------------------------------------------------------


def test_send_parts_hands_every_part_over_in_one_synchronous_call(srv):
    events = []

    class Spy:
        transport = None

        def get_extra_info(self, name):
            return ("127.0.0.1", 1)

        def write(self, data):
            events.append(("write", data))

        def writelines(self, parts):
            events.append(("writelines", list(parts)))

        async def drain(self):
            events.append(("drain",))

    async def scenario():
        session = srv.AsyncKedisSession(None, Spy())
        await session.send_parts([b"A3\n", b"I1\n", b"I2\n"])

    asyncio.run(scenario())
    assert events == [("writelines", [b"A3\n", b"I1\n", b"I2\n"]), ("drain",)]


# --- against a real server -------------------------------------------------------------------------------


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


async def read_exactly(reader, n):
    return await asyncio.wait_for(reader.readexactly(n), 10)


def test_a_big_lrange_arrives_byte_for_byte_and_the_connection_stays_usable(srv, monkeypatch):
    monkeypatch.setattr(srv, "ENCODE_CHUNK", 100)
    items = ["item%d" % i for i in range(7000)]
    srv.global_store.rpush("big", *items)
    expected = KESPEncoder.encode(items)

    async def scenario(port):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(kesp(b"LRANGE", b"big", b"0", b"-1") + kesp(b"GET", b"nothing"))
        await w.drain()
        assert await read_exactly(r, len(expected)) == expected
        assert await read_exactly(r, 2) == b"N\n"
        w.close()

    serve(srv, scenario)


def test_a_big_reply_that_fails_late_is_one_error_line_and_the_next_reply_is_intact(srv, monkeypatch):
    monkeypatch.setattr(srv, "ENCODE_CHUNK", 100)
    srv.global_store._data["bad"] = deque(["fine"] * 3000 + ["\ud800"])

    async def scenario(port):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(kesp(b"LRANGE", b"bad", b"0", b"-1") + kesp(b"GET", b"nothing"))
        await w.drain()
        line = await asyncio.wait_for(r.readline(), 10)
        assert line.startswith(b"EInternal server error: string is not valid UTF-8")
        assert await read_exactly(r, 2) == b"N\n"  # framing was not disturbed
        w.close()

    serve(srv, scenario)


def test_exec_returns_each_big_reply_in_order(srv, monkeypatch):
    monkeypatch.setattr(srv, "ENCODE_CHUNK", 100)
    items = ["item%d" % i for i in range(1500)]
    srv.global_store.rpush("big", *items)
    expected = b"A3\n" + KESPEncoder.encode(items) + b"I0\n" + KESPEncoder.encode(items[:2])

    async def scenario(port):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(kesp(b"MULTI"))
        for cmd in ((b"LRANGE", b"big", b"0", b"-1"), (b"EXISTS", b"nope"), (b"LRANGE", b"big", b"0", b"1")):
            w.write(kesp(*cmd))
        w.write(kesp(b"EXEC"))
        await w.drain()
        # MULTI and the three queued commands each answer with a status line
        for _ in range(4):
            assert (await asyncio.wait_for(r.readline(), 10)).startswith(b"+")
        assert await read_exactly(r, len(expected)) == expected
        w.close()

    serve(srv, scenario)


def test_the_loop_is_not_held_while_a_client_fetches_a_big_reply(srv, monkeypatch):
    """The longest the loop is held, with a big reply on its way out, is a small
    fraction of what encoding that reply in one go takes."""
    items = ["item%d" % i for i in range(300_000)]
    srv.global_store.rpush("big", *items)
    started = time.perf_counter()
    expected = KESPEncoder.encode(items)
    one_go = time.perf_counter() - started
    if one_go < 0.05:
        pytest.skip("encoding is too fast here to tell a stall from noise")

    async def scenario(port):
        longest = 0.0
        stop = False

        async def ticker():
            nonlocal longest
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.002)
                now = time.perf_counter()
                longest = max(longest, now - last - 0.002)
                last = now

        tick = asyncio.ensure_future(ticker())
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.05)
        longest = 0.0
        w.write(kesp(b"LRANGE", b"big", b"0", b"-1"))
        await w.drain()
        assert await asyncio.wait_for(r.readexactly(len(expected)), 20) == expected
        stop = True
        await tick
        w.close()
        assert longest < one_go / 3, (longest, one_go)

    serve(srv, scenario)


def test_both_reply_paths_go_through_encode_reply(srv, monkeypatch):
    """A plain command's reply, and each result inside EXEC."""
    seen = []
    real = srv.encode_reply

    async def spy(data):
        seen.append(data)
        return await real(data)

    monkeypatch.setattr(srv, "encode_reply", spy)
    srv.global_store.rpush("l", "a", "b")

    async def scenario(port):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(kesp(b"LRANGE", b"l", b"0", b"-1"))
        await w.drain()
        assert await read_exactly(r, len(KESPEncoder.encode(["a", "b"]))) == KESPEncoder.encode(["a", "b"])
        assert seen == [["a", "b"]]

        seen.clear()
        for cmd in ((b"MULTI",), (b"LRANGE", b"l", b"0", b"0"), (b"GET", b"nothing"), (b"EXEC",)):
            w.write(kesp(*cmd))
        await w.drain()
        for _ in range(3):  # MULTI and the two queued commands
            assert (await asyncio.wait_for(r.readline(), 10)).startswith(b"+")
        expected = b"A2\n" + KESPEncoder.encode(["a"]) + b"N\n"
        assert await read_exactly(r, len(expected)) == expected
        assert seen == [["a"], None]  # one per result, not one for the whole array
        w.close()

    serve(srv, scenario)
