"""MULTI/EXEC and replication: a block runs as one unit on the master, goes to
followers as one transaction (TXBATCH), and a follower applies it as one unit."""

import asyncio
import io
import json
import threading
import time

import pytest
from rich.console import Console

from kedis_python.parser import CommandParser
from test_stream_server import serve  # shared wire helper


def kesp(*args: bytes) -> bytes:
    return b"A%d\n" % len(args) + b"".join(b"S%d\n%s\n" % (len(a), a) for a in args)


def snapshot(data=None) -> bytes:
    return kesp(json.dumps({"data": data or {}, "expires": {}}).encode())


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # server.py builds its global store in the cwd on import
    from kedis_python import server

    server.console = Console(file=io.StringIO(), width=200)
    server.global_store.flushall()
    server.stream_waiters.clear()
    monkeypatch.setattr(server, "server_role", "master")
    monkeypatch.setattr(server, "server_host", None)
    monkeypatch.setattr(server, "master_port", None)
    monkeypatch.setattr(server, "replication_task", None)
    monkeypatch.setattr(server, "connected_replicas", [])
    yield server
    server.stop_replication()
    server.stream_waiters.clear()
    server.global_store.flushall()


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


def on_master(srv, steps):
    """Runs `steps(c)` against a live server with one fake follower attached;
    returns the commands the follower was sent."""
    follower = FakeFollower()

    async def scenario(connect, errors):
        srv.connected_replicas.append(follower)
        c = await connect()
        await steps(c)

    assert serve(srv, scenario) == []
    return follower.commands(), bytes(follower.buf)


async def transaction(c, *commands):
    assert (await c.call("MULTI")).strip() == "OK"
    for command in commands:
        await c.call(*command)  # queued
    return await c.call("EXEC")


# --- the master forwards one EXEC as one transaction -------------------------------------------------


def test_a_block_of_writes_goes_out_behind_one_header_in_order(srv):
    async def steps(c):
        assert await transaction(c, ("SET", "a", "1"), ("SET", "b", "2"), ("DEL", "a")) == [
            "OK",
            "OK",
            1,
        ]

    sent, _ = on_master(srv, steps)
    assert sent == [["TXBATCH", "3"], ["SET", "a", "1"], ["SET", "b", "2"], ["DEL", "a"]]


def test_a_block_with_one_write_goes_out_bare(srv):
    async def steps(c):
        await transaction(c, ("GET", "x"), ("SET", "a", "1"), ("GET", "a"))

    sent, _ = on_master(srv, steps)
    assert sent == [["SET", "a", "1"]]


def test_reads_inside_a_block_are_not_forwarded(srv):
    async def steps(c):
        await transaction(c, ("GET", "x"), ("EXISTS", "x"))

    sent, _ = on_master(srv, steps)
    assert sent == []


def test_an_empty_block_sends_nothing(srv):
    async def steps(c):
        assert await transaction(c) == []

    sent, _ = on_master(srv, steps)
    assert sent == []


def test_a_discarded_block_sends_nothing(srv):
    async def steps(c):
        await c.call("MULTI")
        await c.call("SET", "a", "1")
        assert (await c.call("DISCARD")).strip() == "OK"

    sent, _ = on_master(srv, steps)
    assert sent == []
    assert srv.global_store.get("a") is None


def test_a_block_aborted_by_a_tripped_watch_sends_nothing(srv):
    follower = FakeFollower()

    async def scenario(connect, errors):
        srv.connected_replicas.append(follower)
        c, other = await connect(), await connect()
        await c.call("WATCH", "k")
        await c.call("MULTI")
        await c.call("SET", "a", "1")
        await other.call("SET", "k", "changed")  # trips the watch
        assert await c.call("EXEC") is None

    assert serve(srv, scenario) == []
    # Only the other client's own SET was forwarded; nothing from the aborted block.
    assert follower.commands() == [["SET", "k", "changed"]]
    assert srv.global_store.get("a") is None


def test_a_block_that_overflowed_sends_nothing(srv, monkeypatch):
    monkeypatch.setattr(srv, "MAX_MULTI_COMMANDS", 2)

    async def steps(c):
        await c.call("MULTI")
        for i in range(3):
            await c.call("SET", f"k{i}", "v")
        reply = await c.call("EXEC")
        assert "EXECABORT" in reply

    sent, _ = on_master(srv, steps)
    assert sent == []


def test_a_stream_id_chosen_by_the_master_is_what_the_follower_is_told(srv):
    master_id = {}

    async def steps(c):
        results = await transaction(
            c, ("XADD", "s", "*", "k", "v"), ("XADD", "s", "*", "k", "w")
        )
        master_id["ids"] = results

    sent, _ = on_master(srv, steps)
    ids = master_id["ids"]
    assert sent == [
        ["TXBATCH", "2"],
        ["XADD", "s", ids[0], "k", "v"],
        ["XADD", "s", ids[1], "k", "w"],
    ]


def test_group_commands_in_a_block_go_out_as_resolved_records(srv):
    async def steps(c):
        await c.call("XADD", "s", "1-0", "n", "0")
        await c.call("XADD", "s", "2-0", "n", "1")
        await c.call("XGROUP", "CREATE", "s", "g", "0")
        await transaction(
            c,
            ("XREADGROUP", "GROUP", "g", "a", "COUNT", "2", "STREAMS", "s", ">"),
            ("XACK", "s", "g", "1-0"),
        )

    sent, _ = on_master(srv, steps)
    start = next(i for i, t in enumerate(sent) if t[0] == "TXBATCH")
    batch = sent[start + 1 : start + 1 + int(sent[start][1])]
    assert batch and all(t[0] == "XGAPPLY" for t in batch)  # never the raw XREADGROUP/XACK


def test_a_client_cannot_send_the_internal_header(srv):
    async def steps(c):
        reply = await c.call("TXBATCH", "2")
        assert reply[0] == "ERR" and "unknown command" in reply[1]

    sent, _ = on_master(srv, steps)
    assert sent == []


def test_a_replica_does_not_forward_anything(srv, monkeypatch):
    # A follower never re-broadcasts (it has no followers of its own to feed).
    monkeypatch.setattr(srv, "server_role", "replica")

    async def steps(c):
        await c.call("MULTI")
        await c.call("GET", "x")
        await c.call("EXEC")

    sent, _ = on_master(srv, steps)
    assert sent == []


# --- EXEC is isolated: nothing runs between the commands of a block --------------------------------------


def test_another_clients_command_cannot_run_in_the_middle_of_a_block(srv):
    handler = srv.global_handler
    started = threading.Event()

    def slow(tokens):
        started.set()
        time.sleep(0.3)
        return "OK"

    handler._commands["SLOWTEST"] = slow
    try:

        def other():
            started.wait(5)
            handler.execute(["SET", "x", "other"])

        t = threading.Thread(target=other)
        t.start()
        results, _ = handler.execute_batch(
            [["SET", "x", "mine"], ["SLOWTEST"], ["GET", "x"]], None
        )
        t.join(5)
    finally:
        del handler._commands["SLOWTEST"]

    assert results[2] == "mine"  # the other client's SET did not land inside the block
    assert srv.global_store.get("x") == "other"  # and it ran once the block was over


def test_a_failing_command_in_a_block_does_not_stop_the_rest(srv):
    handler = srv.global_handler
    results, forwards = handler.execute_batch(
        [["SET", "a", "1"], ["NOSUCHCOMMAND"], ["SET", "b", "2"]], None
    )
    assert results[0] == "OK" and results[2] == "OK"
    assert results[1].startswith("-ERR unknown command")
    assert forwards == [["SET", "a", "1"], ["SET", "b", "2"]]
    assert srv.global_store.get("b") == "2"


# --- a follower applies a transaction as one unit -------------------------------------------------------


class Leader:
    """Reads SYNC, then runs script(writer); records whether the follower hung up."""

    def __init__(self, script):
        self.script = script
        self.hung_up = asyncio.Event()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        assert await reader.readline() == b"SYNC\n"
        self.writer = writer
        asyncio.ensure_future(self.script(writer))
        while await reader.read(65536):
            pass
        self.hung_up.set()

    async def stop(self):
        self.server.close()
        try:
            self.writer.close()
        except AttributeError:
            pass


async def follow(srv, script):
    leader = Leader(script)
    await leader.start()
    task = srv.start_replication("127.0.0.1", leader.port)
    return leader, task


async def wait_until(predicate, limit=10.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def run(scenario, limit=30):
    asyncio.run(asyncio.wait_for(scenario(), limit))


def log(srv) -> str:
    return srv.console.file.getvalue()


def test_a_follower_applies_a_transaction(srv):
    async def script(writer):
        writer.write(
            snapshot()
            + kesp(b"TXBATCH", b"3")
            + kesp(b"SET", b"a", b"1")
            + kesp(b"SET", b"b", b"2")
            + kesp(b"DEL", b"a")
            + kesp(b"SET", b"after", b"ok")
        )
        await writer.drain()

    async def scenario():
        leader, task = await follow(srv, script)
        assert await wait_until(lambda: srv.global_store.get("after") == "ok")
        assert srv.global_store.get("a") is None
        assert srv.global_store.get("b") == "2"
        assert not task.done()
        await leader.stop()

    run(scenario)


def test_a_reader_never_sees_a_half_applied_transaction(srv):
    size = 3000
    commands = b"".join(kesp(b"SET", b"k%d" % i, b"v") for i in range(size))

    async def script(writer):
        writer.write(snapshot())
        await asyncio.sleep(0.2)
        writer.write(kesp(b"TXBATCH", str(size).encode()) + commands)
        await writer.drain()

    torn = []
    stop = threading.Event()

    def reader():
        pair = [["GET", "k0"], ["GET", f"k{size - 1}"]]
        while not stop.is_set():
            (first, last), _ = srv.global_handler.execute_batch(pair, None)
            if first is not None and last is None:
                torn.append((first, last))
                return
            time.sleep(0.0005)

    async def scenario():
        thread = threading.Thread(target=reader)
        thread.start()
        leader, task = await follow(srv, script)
        assert await wait_until(lambda: srv.global_store.get(f"k{size - 1}") == "v")
        stop.set()
        thread.join(5)
        await leader.stop()

    run(scenario)
    assert torn == []


@pytest.mark.parametrize(
    "tail",
    [
        kesp(b"TXBATCH", b"3") + kesp(b"SET", b"a", b"1"),  # cut short, then quiet
        kesp(b"TXBATCH", b"2") + kesp(b"SET", b"a", b"1") + b"A1\nSx\n",  # garbage inside
        kesp(b"TXBATCH", b"2") + kesp(b"SET", b"a", b"1") + kesp(b"TXBATCH", b"2"),  # nested
        kesp(b"TXBATCH", b"x"),
        kesp(b"TXBATCH"),
        kesp(b"TXBATCH", b"1") + kesp(b"SET", b"a", b"1"),  # a one-command batch is never sent
        kesp(b"TXBATCH", b"99999999999"),
    ],
)
def test_a_bad_transaction_drops_the_leader_and_applies_nothing(srv, tail, monkeypatch):
    monkeypatch.setattr(srv, "STALL_TIMEOUT", 0.4)

    async def script(writer):
        writer.write(snapshot({"base": "1"}) + kesp(b"SET", b"before", b"1"))
        await asyncio.sleep(0.2)
        writer.write(tail)
        await writer.drain()

    async def scenario():
        leader, task = await follow(srv, script)
        assert await wait_until(lambda: srv.global_store.get("before") == "1")
        await asyncio.wait_for(leader.hung_up.wait(), 10)  # the follower dropped the leader
        assert srv.global_store.get("a") is None  # nothing from the bad transaction
        assert srv.server_role == "replica"  # and it did not promote itself
        await leader.stop()

    run(scenario)


def test_a_leader_that_closes_in_the_middle_of_a_transaction_applies_nothing(srv):
    async def script(writer):
        writer.write(snapshot() + kesp(b"TXBATCH", b"3") + kesp(b"SET", b"a", b"1"))
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()

    async def scenario():
        leader, task = await follow(srv, script)
        await asyncio.wait_for(leader.hung_up.wait(), 10)
        await asyncio.sleep(0.2)
        assert srv.global_store.get("a") is None
        assert "in the middle of a transaction" in log(srv)
        await leader.stop()

    run(scenario)


# --- the whole round trip: what a master sends rebuilds the same state --------------------------------------


def test_what_the_master_sends_rebuilds_the_same_state_on_a_follower(srv):
    async def steps(c):
        await c.call("SET", "plain", "1")
        await transaction(
            c,
            ("SET", "a", "1"),
            ("RPUSH", "l", "x", "y"),
            ("XADD", "s", "*", "k", "v"),
            ("XADD", "s", "*", "k", "w"),
            ("XGROUP", "CREATE", "s", "g", "0"),
            ("XREADGROUP", "GROUP", "g", "c", "COUNT", "1", "STREAMS", "s", ">"),
            ("DEL", "plain"),
        )

    _, wire = on_master(srv, steps)

    def state():
        h = srv.global_handler
        return {
            "plain": h.execute(["GET", "plain"]),
            "a": h.execute(["GET", "a"]),
            "l": h.execute(["LRANGE", "l", "0", "-1"]),
            "s": h.execute(["XRANGE", "s", "-", "+"]),
            "pending": h.execute(["XPENDING", "s", "g"]),
        }

    on_master_state = state()
    assert on_master_state["plain"] is None and on_master_state["a"] == "1"
    srv.global_store.flushall()  # the follower starts from nothing, and gets the stream
    assert state()["a"] is None

    async def script(writer):
        writer.write(snapshot({"plain": "1"}) + wire)
        await writer.drain()

    async def scenario():
        leader, task = await follow(srv, script)
        assert await wait_until(lambda: srv.global_store.get("a") == "1")
        assert await wait_until(lambda: state() == on_master_state)
        assert state() == on_master_state
        await leader.stop()

    run(scenario)
