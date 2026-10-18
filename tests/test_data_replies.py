"""Stored data must reach the client as data, whatever it looks like."""

import asyncio
import io

import pytest
from rich.console import Console

from kedis_python.commands import CommandHandler
from kedis_python.parser import KESPEncoder
from kedis_python.store import KedisStore

TRICKY = ["OK", "+OK", "+QUEUED", "ERROR x", "-ERR y", "ERRORS", "N", "A1"]


@pytest.fixture
def handler(tmp_path, monkeypatch):
    # KedisStore replays and appends to ./kedis.aof, so run in a scratch directory.
    monkeypatch.chdir(tmp_path)
    store = KedisStore(aof_filename="test.aof", appendfsync="always")
    yield CommandHandler(store)
    store.shutdown()


def wire(handler, *tokens):
    return KESPEncoder.encode(handler.execute(list(tokens)))


def bulk(text: str) -> bytes:
    return b"S%d\n%s\n" % (len(text.encode()), text.encode())


@pytest.mark.parametrize("value", TRICKY)
def test_get_returns_the_value_as_a_bulk_string(handler, value):
    wire(handler, "SET", "k", value)
    assert wire(handler, "GET", "k") == bulk(value)


@pytest.mark.parametrize("value", TRICKY)
def test_hget_returns_the_value_as_a_bulk_string(handler, value):
    wire(handler, "HSET", "h", "f", value)
    assert wire(handler, "HGET", "h", "f") == bulk(value)


@pytest.mark.parametrize("value", TRICKY)
def test_lpop_and_rpop_return_bulk_strings(handler, value):
    wire(handler, "RPUSH", "l", value, value)
    assert wire(handler, "LPOP", "l") == bulk(value)
    assert wire(handler, "RPOP", "l") == bulk(value)


def test_missing_values_are_still_nil(handler):
    assert wire(handler, "GET", "nope") == b"N\n"
    assert wire(handler, "HGET", "h", "nope") == b"N\n"
    assert wire(handler, "LPOP", "nope") == b"N\n"


def test_collections_keep_every_element_as_data(handler):
    for v in TRICKY:
        wire(handler, "RPUSH", "l", v)
        wire(handler, "SADD", "s", v)
        wire(handler, "HSET", "h", v, v)
    lrange = wire(handler, "LRANGE", "l", "0", "-1")
    assert lrange == b"A%d\n" % len(TRICKY) + b"".join(bulk(v) for v in TRICKY)
    smembers = wire(handler, "SMEMBERS", "s")
    assert smembers.startswith(b"A%d\n" % len(TRICKY))
    assert not any(line.startswith((b"E", b"+")) for line in smembers.split(b"\n")[1::2])
    hgetall = wire(handler, "HGETALL", "h")
    assert hgetall.startswith(b"A%d\n" % (2 * len(TRICKY)))
    for v in TRICKY:
        assert bulk(v) in hgetall


def test_keys_named_like_errors_stay_data(handler):
    wire(handler, "SET", "ERROR x", "v")
    reply = wire(handler, "KEYS")
    assert reply.startswith(b"A1\nS")


def test_real_errors_are_still_errors(handler):
    assert wire(handler, "GET").startswith(b"E")  # wrong number of arguments
    wire(handler, "RPUSH", "l", "a")
    assert wire(handler, "GET", "l").startswith(b"E")  # wrong type
    assert wire(handler, "NOPE").startswith(b"E")


def test_set_and_friends_still_reply_ok(handler):
    assert wire(handler, "SET", "k", "v") == b"+OK\n"


# --- end to end over a socket ----------------------------------------------------------------


def test_value_that_looks_like_an_error_survives_the_wire(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()

    def kesp(*args):
        body = b"".join(b"S%d\n%s\n" % (len(a.encode()), a.encode()) for a in args)
        return b"A%d\n" % len(args) + body

    async def scenario():
        srv = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        try:
            w.write(kesp("SET", "k", "ERROR disk full") + kesp("GET", "k"))
            await w.drain()
            assert (await r.readline()).startswith(b"+OK")
            assert await r.readline() == b"S15\n"
            assert await r.readline() == b"ERROR disk full\n"
        finally:
            w.close()
            await asyncio.sleep(0.05)
            srv.close()

    try:
        asyncio.run(asyncio.wait_for(scenario(), 10))
    finally:
        server.global_store.flushall()
