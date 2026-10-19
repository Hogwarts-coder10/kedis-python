"""Any byte string round-trips: the wire, the AOF, snapshots, replication and display."""

import asyncio
import io
import json
import os
import random

import pytest
from rich.console import Console

from kedis_python.main import KedisClient
from kedis_python.parser import (
    BulkString,
    CommandParser,
    ErrorReply,
    IncompleteReply,
    KESPDecoder,
    KESPEncoder,
    ProtocolError,
    Status,
)
from kedis_python.store import KedisStore
from kedis_python.textcodec import printable, to_bytes, to_text

ALL_BYTES = bytes(range(256))
SAMPLES = [
    ALL_BYTES,
    ALL_BYTES[::-1],
    b"\xff\xfe\x00\x80",
    b"\xc3",  # half of a two-byte character
    b"\xed\xb2\x80",  # the UTF-8 *look* of U+DC80, which is not valid UTF-8
    "h\u00e9llo \u2713".encode(),
    b"\x00",
    b"a\r\nb\nA1\nS3\nSET",
    b"",
]


def ids(values):
    return [repr(v)[:20] for v in values]


# --- the codec -------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", SAMPLES, ids=ids(SAMPLES))
def test_bytes_round_trip_through_text(raw):
    assert to_bytes(to_text(raw)) == raw


def test_every_single_byte_round_trips():
    for b in range(256):
        assert to_bytes(to_text(bytes([b]))) == bytes([b])


def test_random_byte_strings_round_trip():
    rng = random.Random(7)
    for _ in range(300):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40)))
        text = to_text(raw)
        assert to_bytes(text) == raw
        assert to_text(to_bytes(text)) == text


def test_valid_utf8_is_ordinary_text():
    assert to_text("h\u00e9llo \u2713".encode()) == "h\u00e9llo \u2713"
    assert to_text(b"\xff") == "\udcff"


def test_text_no_byte_string_can_produce_is_rejected():
    with pytest.raises(UnicodeEncodeError):
        to_bytes("\ud800")


@pytest.mark.parametrize(
    "text, shown",
    [
        ("plain", "plain"),
        ("h\u00e9llo \u2713", "h\u00e9llo \u2713"),
        (to_text(b"a\xffb"), "a\\xffb"),
        (to_text(b"\x80\x81"), "\\x80\\x81"),
        ("\ud800", "\\ud800"),
    ],
)
def test_printable_escapes_raw_bytes_and_nothing_else(text, shown):
    assert printable(text) == shown
    printable(text).encode("utf-8")  # always safe to print


# --- command parser ----------------------------------------------------------------------------


@pytest.mark.parametrize("raw", SAMPLES, ids=ids(SAMPLES))
def test_a_command_carries_any_bytes(raw):
    frame = b"A3\nS3\nSET\nS1\nk\nS%d\n%s\n" % (len(raw), raw)
    tokens, consumed = CommandParser.parse(frame)
    assert not isinstance(tokens, ProtocolError)
    assert consumed == len(frame)
    assert to_bytes(tokens[2]) == raw


@pytest.mark.parametrize("raw", SAMPLES, ids=ids(SAMPLES))
def test_every_prefix_of_a_binary_command_waits(raw):
    frame = b"A2\nS3\nGET\nS%d\n%s\n" % (len(raw), raw)
    for cut in range(len(frame)):
        assert CommandParser.parse(frame[:cut]) == ([], 0), cut
    assert CommandParser.parse(frame)[1] == len(frame)


def test_the_client_encoder_and_the_server_parser_agree_on_any_bytes():
    tokens = ["SET", to_text(ALL_BYTES), to_text(b"\xff\xfe"), "h\u00e9llo"]
    frame = KESPEncoder.encode_command(tokens)
    got, consumed = CommandParser.parse(frame)
    assert consumed == len(frame)
    assert got == ["SET"] + tokens[1:]


def test_an_inline_command_with_invalid_utf8_is_no_longer_an_error():
    tokens, consumed = CommandParser.parse(b"SET k \xff\xfe\n")
    assert tokens == ["SET", "k", "\udcff\udcfe"]
    assert consumed == len(b"SET k \xff\xfe\n")


# --- reply decoder / encoder ---------------------------------------------------------------------


@pytest.mark.parametrize("raw", SAMPLES, ids=ids(SAMPLES))
def test_a_bulk_reply_carries_any_bytes(raw):
    wire = KESPEncoder.encode(BulkString(to_text(raw)))
    assert wire == b"S%d\n%s\n" % (len(raw), raw)
    value, end = KESPDecoder.decode(wire)
    assert isinstance(value, BulkString)
    assert to_bytes(value) == raw
    assert end == len(wire)


def test_arrays_of_binary_values_round_trip():
    items = [to_text(s) for s in SAMPLES]
    wire = KESPEncoder.encode(items)
    value, end = KESPDecoder.decode(wire)
    assert value == items and end == len(wire)


def test_status_and_error_lines_keep_their_bytes():
    assert to_bytes(KESPDecoder.decode(b"+hi \xff\n")[0]) == b"hi \xff"
    assert to_bytes(KESPDecoder.decode(b"Ebad \xfe\n")[0]) == b"bad \xfe"
    assert isinstance(KESPDecoder.decode(b"+x\n")[0], Status)
    assert isinstance(KESPDecoder.decode(b"Ex\n")[0], ErrorReply)


def test_every_prefix_of_a_binary_reply_is_incomplete():
    wire = KESPEncoder.encode([to_text(ALL_BYTES), "x"])
    for cut in range(len(wire)):
        with pytest.raises(IncompleteReply):
            KESPDecoder.decode(wire[:cut])


# --- the server, end to end --------------------------------------------------------------------------


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    yield server
    server.global_store.flushall()


def run(srv, scenario):
    async def runner():
        server = await asyncio.start_server(srv.handle_connection, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            await asyncio.wait_for(scenario(reader, writer), 10)
        finally:
            writer.close()
            await asyncio.sleep(0.05)
            server.close()

    asyncio.run(runner())


def frame(*parts: bytes) -> bytes:
    return b"A%d\n" % len(parts) + b"".join(b"S%d\n%s\n" % (len(p), p) for p in parts)


async def bulk_reply(r) -> bytes:
    header = await r.readline()
    assert header.startswith(b"S"), header
    n = int(header[1:-1])
    data = await r.readexactly(n + 1)
    assert data.endswith(b"\n")
    return data[:-1]


def test_binary_values_and_keys_round_trip_over_the_wire(srv):
    async def scenario(r, w):
        for i, raw in enumerate(SAMPLES):
            w.write(frame(b"SET", b"key" + raw, raw))
            await w.drain()
            assert (await r.readline()).startswith(b"+OK")
            w.write(frame(b"GET", b"key" + raw))
            await w.drain()
            assert await bulk_reply(r) == raw, i

    run(srv, scenario)


def test_the_connection_survives_a_binary_value(srv):
    async def scenario(r, w):
        w.write(frame(b"SET", b"k", b"\xff\xfe\x00\x80"))
        await w.drain()
        assert (await r.readline()).startswith(b"+OK")
        w.write(frame(b"SET", b"after", b"still here"))
        await w.drain()
        assert (await r.readline()).startswith(b"+OK")

    run(srv, scenario)


def test_bitmaps_hold_real_bytes_over_the_wire(srv):
    async def scenario(r, w):
        for bit in range(8):
            w.write(frame(b"SETBIT", b"bm", str(bit).encode(), b"1"))
            await w.drain()
            assert await r.readline() == b"I0\n"  # the bit's previous value
        w.write(frame(b"GET", b"bm"))
        await w.drain()
        assert await bulk_reply(r) == b"\xff"  # one byte, not c3 bf

    run(srv, scenario)


def test_bitcount_counts_the_bits_of_the_utf8_bytes(srv):
    async def scenario(r, w):
        w.write(frame(b"SET", b"e", "\u00e9".encode()))
        await w.drain()
        assert (await r.readline()).startswith(b"+OK")
        w.write(frame(b"BITCOUNT", b"e"))
        await w.drain()
        assert (await r.readline()).strip() == b"I8"  # c3 a9 -> 4 + 4 bits

    run(srv, scenario)


# --- persistence ----------------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def open_store():
    return KedisStore(aof_filename="test.aof", appendfsync="always")


def populate(s):
    for i, raw in enumerate(SAMPLES):
        t = to_text(raw)
        s.set(f"s{i}", t)
        s.rpush(f"l{i}", t, "x")
        s.sadd(f"set{i}", t)
        s.hset(f"h{i}", t or "empty", t)
        s.zadd(f"z{i}", 1.0, t)
        s.set(to_text(b"key\xff" + bytes([i])), t)


def verify(s):
    for i, raw in enumerate(SAMPLES):
        t = to_text(raw)
        assert to_bytes(s.get(f"s{i}")) == raw
        assert [to_bytes(v) for v in s.lrange(f"l{i}", 0, -1)] == [raw, b"x"]
        assert [to_bytes(v) for v in s.smembers(f"set{i}")] == [raw]
        assert to_bytes(s.hget(f"h{i}", t or "empty")) == raw
        assert [to_bytes(v) for v in s.zrange(f"z{i}", 0, -1)] == [raw]
        assert to_bytes(s.get(to_text(b"key\xff" + bytes([i])))) == raw


def test_binary_data_survives_an_aof_restart(workdir):
    s = open_store()
    populate(s)
    s.shutdown()
    s = open_store()
    verify(s)
    s.shutdown()


def test_binary_data_survives_compaction(workdir):
    s = open_store()
    populate(s)
    assert s.compact_aof()
    s.shutdown()
    s = open_store()
    verify(s)
    s.shutdown()


def test_binary_data_survives_a_snapshot_with_no_aof(workdir):
    s = open_store()
    populate(s)
    s.save()
    s.shutdown()
    os.remove("test.aof")
    s = open_store()
    verify(s)
    s.shutdown()


def test_binary_data_survives_the_replication_snapshot(workdir):
    s = open_store()
    populate(s)
    wire = json.dumps(s.get_snapshot_state()).encode("utf-8")  # as the leader sends it
    s.shutdown()
    os.remove("test.aof")
    follower = KedisStore(aof_filename="follower.aof", appendfsync="always")
    follower.restore_snapshot_state(json.loads(wire.decode("utf-8")))
    verify(follower)
    follower.shutdown()


# --- the CLI ------------------------------------------------------------------------------------------


def test_the_cli_shows_raw_bytes_escaped_and_keeps_reply_types():
    client = KedisClient.__new__(KedisClient)
    wire = KESPEncoder.encode([to_text(b"a\xffb"), "plain", None, 3])
    shown = client._decode_kesp_value(wire)
    assert shown == ["a\\xffb", "plain", None, 3]
    assert all(isinstance(v, BulkString) for v in shown[:2])
    assert "\n".join(v for v in shown if isinstance(v, str)).encode("utf-8")

    err = client._decode_kesp_value(b"Ebad \xfe\n")
    assert isinstance(err, ErrorReply) and err == "bad \\xfe"
