"""A follower must load the leader's baseline snapshot exactly as sent.

The snapshot travels as a one-element KESP array holding JSON. The parser
uppercases the first token of a frame (it assumes a command name), which turned
the JSON into {"DATA": ...}: the follower found no "data" key and restored
nothing ("Restored 0 keys"), and JSON literals (true/null) became invalid."""

import asyncio
import io
import json
import time

import pytest
from rich.console import Console

from kedis_python.parser import CommandParser


def kesp(*args: bytes) -> bytes:
    return b"A%d\n" % len(args) + b"".join(b"S%d\n%s\n" % (len(a), a) for a in args)


# --- the parser option ----------------------------------------------------------------------------------


def test_the_first_token_is_uppercased_by_default():
    tokens, used = CommandParser.parse(kesp(b"set", b"Key", b"Value"))
    assert tokens == ["SET", "Key", "Value"] and used > 0


def test_normalise_false_leaves_the_first_token_as_sent():
    frame = kesp(b'{"data": {"user": "alice"}, "ok": true}')
    tokens, used = CommandParser.parse(frame, normalise=False)
    assert tokens == ['{"data": {"user": "alice"}, "ok": true}']
    assert used == len(frame)


def test_normalise_false_applies_to_parse_buffer_and_to_an_offset():
    frame = b"\n" + kesp(b"mixed", b"Case")
    tokens, used, need = CommandParser.parse_buffer(frame, 1, False)
    assert tokens == ["mixed", "Case"] and need == 0
    tokens, _, _ = CommandParser.parse_buffer(frame, 1)
    assert tokens == ["MIXED", "Case"]


def test_normalise_false_still_reports_incomplete_and_malformed_frames():
    assert CommandParser.parse(b"A1\nS10\nabc", normalise=False) == ([], 0)
    tokens, _ = CommandParser.parse(b"A1\nSx\n", normalise=False)
    assert type(tokens).__name__ == "ProtocolError"


# --- a follower loading a real snapshot ---------------------------------------------------------------


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    monkeypatch.setattr(server, "server_role", "master")
    monkeypatch.setattr(server, "server_host", None)
    monkeypatch.setattr(server, "master_port", None)
    yield server
    server.global_store.flushall()


def populate(srv):
    run = srv.global_handler.execute
    run(["SET", "user", "alice"], None)
    run(["SET", "MixedCase", "Hello World"], None)
    run(["SET", "flag", "true"], None)  # JSON-looking text as data
    run(["SET", "nothing", "null"], None)
    run(["HSET", "profile", "Name", "Bob", "city", "Pune"], None)
    run(["ZADD", "board", "10", "Ann", "20", "bob"], None)
    run(["RPUSH", "queue", "First", "second"], None)


def normalised(state):
    return json.loads(json.dumps(state))


def follow(srv, snapshot_json: bytes, live: bytes = b""):
    """Runs init_replication_stream against a stand-in leader; returns when it has
    loaded the snapshot and applied `live`. The stand-in checks it was sent SYNC."""

    async def scenario():
        async def leader(reader, writer):
            assert await reader.readline() == b"SYNC\n"
            writer.write(kesp(snapshot_json))
            await writer.drain()
            await asyncio.sleep(0.2)  # live writes arrive after the snapshot, as they do in practice
            writer.write(live)
            await writer.drain()
            await asyncio.sleep(30)

        server = await asyncio.start_server(leader, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        task = asyncio.ensure_future(srv.init_replication_stream("127.0.0.1", port))
        try:
            end = time.monotonic() + 10
            while time.monotonic() < end and not (
                srv.global_store._data and (not live or srv.global_store.get("later"))
            ):
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            server.close()
            await asyncio.sleep(0.05)

    asyncio.run(scenario())


def test_the_follower_restores_the_leaders_data_with_its_case_intact(srv):
    populate(srv)
    sent = normalised(srv.global_store.get_snapshot_state())
    snapshot_json = json.dumps(sent).encode()
    assert sent["data"]  # the leader really had something to send
    srv.global_store.flushall()

    follow(srv, snapshot_json)

    assert normalised(srv.global_store.get_snapshot_state()) == sent
    assert srv.global_store.get("user") == "alice"
    assert srv.global_store.get("MixedCase") == "Hello World"
    assert srv.global_store.get("flag") == "true"
    assert srv.global_store.get("nothing") == "null"
    assert srv.server_role == "replica"


def test_the_live_stream_after_the_snapshot_keeps_values_as_sent(srv):
    populate(srv)
    sent = normalised(srv.global_store.get_snapshot_state())
    snapshot_json = json.dumps(sent).encode()
    srv.global_store.flushall()

    follow(srv, snapshot_json, live=kesp(b"set", b"later", b"Bob"))

    assert srv.global_store.get("user") == "alice"  # baseline survived
    assert srv.global_store.get("later") == "Bob"  # command name folded, value not
