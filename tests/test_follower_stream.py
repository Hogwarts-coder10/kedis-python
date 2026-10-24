"""The follower's side of replication: framing the leader's stream under the same
budgets as a client connection, and not wedging on what the leader sends."""

import asyncio
import io
import json
import time

import pytest
from rich.console import Console

from kedis_python.parser import CommandParser


def kesp(*args: bytes) -> bytes:
    return b"A%d\n" % len(args) + b"".join(b"S%d\n%s\n" % (len(a), a) for a in args)


def snapshot(data=None) -> bytes:
    return kesp(json.dumps({"data": data or {}, "expires": {}}).encode())


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO(), width=200)
    server.global_store.flushall()
    monkeypatch.setattr(server, "server_role", "master")
    monkeypatch.setattr(server, "server_host", None)
    monkeypatch.setattr(server, "master_port", None)
    monkeypatch.setattr(server, "replication_task", None)
    yield server
    server.stop_replication()
    server.global_store.flushall()


def run(scenario, limit=30):
    asyncio.run(asyncio.wait_for(scenario(), limit))


async def wait_until(predicate, limit=10.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def reader_with(*chunks: bytes, eof=True):
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


# --- LeaderStream: framing --------------------------------------------------------------------------


def test_commands_come_out_in_order_with_the_name_uppercased(srv):
    async def scenario():
        stream = srv.LeaderStream(reader_with(kesp(b"set", b"a", b"One") + kesp(b"del", b"b")))
        assert await stream.next_command() == ["SET", "a", "One"]
        assert await stream.next_command() == ["DEL", "b"]
        assert await stream.next_command() is None

    run(scenario)


def test_normalise_false_leaves_the_first_token_alone(srv):
    async def scenario():
        stream = srv.LeaderStream(reader_with(kesp(b'{"data": true}')))
        assert await stream.next_command(normalise=False) == ['{"data": true}']

    run(scenario)


def test_many_commands_in_one_chunk_are_all_returned_without_another_read(srv):
    async def scenario():
        chunk = b"".join(kesp(b"SET", b"k%d" % i, b"v") for i in range(500))
        stream = srv.LeaderStream(reader_with(chunk, eof=False))  # nothing more will ever arrive
        for i in range(500):
            assert await asyncio.wait_for(stream.next_command(), 2) == ["SET", "k%d" % i, "v"]

    run(scenario)


def test_blank_frames_between_commands_are_skipped(srv):
    async def scenario():
        stream = srv.LeaderStream(reader_with(b"\n\nA0\n" + kesp(b"PING")))
        assert await stream.next_command() == ["PING"]

    run(scenario)


def test_a_command_split_across_reads_is_assembled(srv):
    async def scenario():
        frame = kesp(b"SET", b"key", b"value")
        reader = asyncio.StreamReader()
        stream = srv.LeaderStream(reader)
        task = asyncio.ensure_future(stream.next_command())
        for i in range(0, len(frame), 3):
            reader.feed_data(frame[i : i + 3])
            await asyncio.sleep(0)
        assert await asyncio.wait_for(task, 2) == ["SET", "key", "value"]

    run(scenario)


def test_a_big_value_is_not_reparsed_after_every_chunk(srv, monkeypatch):
    calls = []
    real = CommandParser.parse_buffer

    def counting(raw, start=0, normalise=True):
        calls.append(len(raw))
        return real(raw, start, normalise)

    monkeypatch.setattr(CommandParser, "parse_buffer", staticmethod(counting))

    async def scenario():
        frame = kesp(b"SET", b"big", b"x" * (4 * 1024 * 1024))
        reader = asyncio.StreamReader(limit=1 << 30)
        stream = srv.LeaderStream(reader)
        task = asyncio.ensure_future(stream.next_command())
        for i in range(0, len(frame), 65536):
            reader.feed_data(frame[i : i + 65536])
            await asyncio.sleep(0)
        tokens = await asyncio.wait_for(task, 5)
        assert len(tokens[2]) == 4 * 1024 * 1024
        assert len(calls) <= 4  # 64 chunks arrived; the parser was not run for each

    run(scenario)


def test_the_buffer_does_not_grow_as_commands_go_by(srv):
    async def scenario():
        reader = asyncio.StreamReader()
        stream = srv.LeaderStream(reader)
        for i in range(200):
            reader.feed_data(kesp(b"SET", b"k", b"v" * 1000))
            assert await stream.next_command() == ["SET", "k", "v" * 1000]
        reader.feed_data(kesp(b"PING"))
        await stream.next_command()
        assert len(stream.buf) <= 2048

    run(scenario)


# --- LeaderStream: what goes wrong ------------------------------------------------------------------


def test_the_leader_closing_between_commands_is_a_clean_end(srv):
    async def scenario():
        stream = srv.LeaderStream(reader_with(kesp(b"PING")))
        assert await stream.next_command() == ["PING"]
        assert await stream.next_command() is None

    run(scenario)


def test_the_leader_closing_in_the_middle_of_a_command_is_an_error(srv):
    async def scenario():
        stream = srv.LeaderStream(reader_with(kesp(b"SET", b"k", b"value")[:-4]))
        with pytest.raises(srv.ReplicationError, match="middle of a command"):
            await stream.next_command()

    run(scenario)


@pytest.mark.parametrize(
    "garbage", [b"A1\nSx\n", b"A1\nS3\nabcd\n", b"A99999999999\n", b"A1\n" + b"S" * 40 + b"\n"]
)
def test_malformed_data_is_an_error_not_something_to_skip(srv, garbage):
    async def scenario():
        stream = srv.LeaderStream(reader_with(kesp(b"PING") + garbage, eof=False))
        assert await stream.next_command() == ["PING"]
        with pytest.raises(srv.ReplicationError, match="malformed"):
            await stream.next_command()

    run(scenario)


def test_a_half_received_command_that_stops_arriving_times_out(srv, monkeypatch):
    monkeypatch.setattr(srv, "STALL_TIMEOUT", 0.3)

    async def scenario():
        stream = srv.LeaderStream(reader_with(kesp(b"SET", b"k", b"value")[:10], eof=False))
        started = time.monotonic()
        with pytest.raises(srv.ReplicationError, match="timed out"):
            await stream.next_command()
        assert 0.2 < time.monotonic() - started < 3

    run(scenario)


def test_a_quiet_leader_between_commands_is_left_alone(srv, monkeypatch):
    monkeypatch.setattr(srv, "STALL_TIMEOUT", 0.2)
    monkeypatch.setattr(srv, "FRAME_DEADLINE", 0.2)

    async def scenario():
        reader = asyncio.StreamReader()
        stream = srv.LeaderStream(reader)
        task = asyncio.ensure_future(stream.next_command())
        await asyncio.sleep(0.8)  # idle for much longer than either limit
        assert not task.done()
        reader.feed_data(kesp(b"PING"))
        assert await asyncio.wait_for(task, 2) == ["PING"]

    run(scenario)


def test_a_command_dripped_in_for_ever_hits_the_frame_deadline(srv, monkeypatch):
    monkeypatch.setattr(srv, "STALL_TIMEOUT", 5.0)
    monkeypatch.setattr(srv, "FRAME_DEADLINE", 0.6)
    monkeypatch.setattr(srv, "FRAME_MIN_RATE", 1e15)

    async def scenario():
        reader = asyncio.StreamReader()
        stream = srv.LeaderStream(reader)
        task = asyncio.ensure_future(stream.next_command())

        async def drip():
            reader.feed_data(b"A1\nS1000\n")
            while True:  # a byte every 0.1 s: never stalls, never finishes
                await asyncio.sleep(0.1)
                reader.feed_data(b"x")

        dripping = asyncio.ensure_future(drip())
        started = time.monotonic()
        with pytest.raises(srv.ReplicationError, match="timed out"):
            await asyncio.wait_for(task, 5)
        assert time.monotonic() - started < 2.5
        dripping.cancel()

    run(scenario)


def test_the_allowance_for_a_frame_grows_with_its_size(srv, monkeypatch):
    monkeypatch.setattr(srv, "STALL_TIMEOUT", 5.0)
    monkeypatch.setattr(srv, "FRAME_DEADLINE", 0.3)
    monkeypatch.setattr(srv, "FRAME_MIN_RATE", 1000)  # a 2,000-byte value earns about 2 s more

    async def scenario():
        reader = asyncio.StreamReader()
        stream = srv.LeaderStream(reader)
        task = asyncio.ensure_future(stream.next_command(normalise=False))
        reader.feed_data(b"A1\nS2000\n")
        await asyncio.sleep(0.2)
        reader.feed_data(b"y" * 1000)
        await asyncio.sleep(0.5)  # past FRAME_DEADLINE alone, inside the size allowance
        assert not task.done()
        reader.feed_data(b"y" * 1000 + b"\n")
        assert await asyncio.wait_for(task, 2) == ["y" * 2000]

    run(scenario)


def test_first_wait_bounds_the_wait_for_a_first_byte(srv):
    async def scenario():
        stream = srv.LeaderStream(asyncio.StreamReader())
        with pytest.raises(srv.ReplicationError, match="timed out"):
            await stream.next_command(first_wait=0.2)

    run(scenario)


# --- describe_command ---------------------------------------------------------------------------------


def test_describe_command_is_short_and_safe_to_print(srv):
    line = srv.describe_command(["SET", "key", "x" * (8 * 1024 * 1024)])
    assert len(line) < 200 and "SET key" in line and "chars" in line
    many = srv.describe_command(["RPUSH", "q"] + ["item%d" % i for i in range(5000)])
    assert len(many) < 300
    assert srv.describe_command(["SET", "k", "[/bold]"]) == "SET k \\[/bold]"


# --- init_replication_stream against a stand-in leader -------------------------------------------------


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


def log(srv) -> str:
    return srv.console.file.getvalue()


def test_a_live_write_in_the_same_chunk_as_the_snapshot_is_applied(srv):
    async def script(writer):
        writer.write(snapshot({"base": "1"}) + kesp(b"SET", b"fast", b"yes"))
        await writer.drain()

    async def scenario():
        leader, task = await follow(srv, script)
        assert await wait_until(lambda: srv.global_store.get("fast") == "yes")
        assert srv.global_store.get("base") == "1"
        await leader.stop()

    run(scenario)


def test_values_that_look_like_console_markup_replicate(srv):
    async def script(writer):
        writer.write(snapshot() + kesp(b"SET", b"k1", b"[/bold]") + kesp(b"SET", b"k2", b"[red]x"))
        await asyncio.sleep(0.2)
        writer.write(kesp(b"SET", b"after", b"ok"))
        await writer.drain()

    async def scenario():
        leader, task = await follow(srv, script)
        assert await wait_until(lambda: srv.global_store.get("after") == "ok")
        assert srv.global_store.get("k1") == "[/bold]"
        assert srv.global_store.get("k2") == "[red]x"
        await leader.stop()

    run(scenario)


def test_a_big_value_replicates_and_the_log_line_stays_small(srv):
    big = b"x" * (4 * 1024 * 1024)

    async def script(writer):
        writer.write(snapshot())
        await asyncio.sleep(0.1)
        writer.write(kesp(b"SET", b"big", big))
        await writer.drain()

    async def scenario():
        leader, task = await follow(srv, script)
        assert await wait_until(lambda: srv.global_store.get("big") == big.decode())
        assert max(len(line) for line in log(srv).splitlines()) < 500
        await leader.stop()

    run(scenario)


def test_a_command_that_raises_is_skipped_and_the_ones_after_it_still_apply(srv, monkeypatch):
    real = srv.global_handler.execute

    def execute(tokens, client_socket=None):
        if tokens[0] == "BOOM":
            raise RuntimeError("cannot apply this")
        return real(tokens, client_socket)

    monkeypatch.setattr(srv.global_handler, "execute", execute)

    async def script(writer):
        writer.write(snapshot() + kesp(b"BOOM", b"x") + kesp(b"SET", b"after", b"1"))
        await writer.drain()

    async def scenario():
        leader, task = await follow(srv, script)
        assert await wait_until(lambda: srv.global_store.get("after") == "1")
        assert "Could not apply BOOM x" in log(srv)
        assert not task.done()
        await leader.stop()

    run(scenario)


def test_malformed_data_from_the_leader_drops_it_and_keeps_the_replica_role(srv):
    async def script(writer):
        writer.write(snapshot({"base": "1"}) + kesp(b"SET", b"before", b"1"))
        await asyncio.sleep(0.2)
        writer.write(b"A1\nSx\n" + kesp(b"SET", b"after", b"1"))
        await writer.drain()

    async def scenario():
        leader, task = await follow(srv, script)
        await asyncio.wait_for(task, 10)  # the stream ended by itself
        await asyncio.wait_for(leader.hung_up.wait(), 10)
        assert srv.global_store.get("before") == "1"
        assert srv.global_store.get("after") is None
        assert srv.server_role == "replica"
        assert "malformed" in log(srv)
        await leader.stop()

    run(scenario)


def test_a_leader_that_stalls_in_the_middle_of_a_command_is_dropped(srv, monkeypatch):
    monkeypatch.setattr(srv, "STALL_TIMEOUT", 0.4)

    async def script(writer):
        writer.write(snapshot({"base": "1"}) + kesp(b"SET", b"k", b"value")[:12])
        await writer.drain()

    async def scenario():
        leader, task = await follow(srv, script)
        await asyncio.wait_for(task, 10)
        await asyncio.wait_for(leader.hung_up.wait(), 10)
        assert srv.global_store.get("base") == "1" and srv.global_store.get("k") is None
        assert srv.server_role == "replica"
        await leader.stop()

    run(scenario)


def test_a_leader_that_never_sends_the_snapshot_is_dropped(srv, monkeypatch):
    monkeypatch.setattr(srv, "FRAME_DEADLINE", 0.4)

    async def script(writer):
        pass  # accepts SYNC and says nothing

    async def scenario():
        leader, task = await follow(srv, script)
        started = time.monotonic()
        await asyncio.wait_for(task, 10)
        assert 0.3 < time.monotonic() - started < 5
        await asyncio.wait_for(leader.hung_up.wait(), 10)
        assert srv.server_role == "replica"
        await leader.stop()

    run(scenario)


@pytest.mark.parametrize(
    "frame, complaint",
    [
        (kesp(b"{not json"), "not valid JSON"),
        (kesp(b"[1, 2, 3]"), "not a JSON object"),
        (kesp(b"{}", b"extra"), "not a snapshot"),
    ],
)
def test_a_bad_snapshot_is_refused_and_the_store_is_left_alone(srv, frame, complaint):
    srv.global_store.set("pre", "existing")

    async def script(writer):
        writer.write(frame + kesp(b"SET", b"after", b"1"))
        await writer.drain()

    async def scenario():
        leader, task = await follow(srv, script)
        await asyncio.wait_for(task, 10)
        await asyncio.wait_for(leader.hung_up.wait(), 10)
        assert srv.global_store.get("pre") == "existing"
        assert srv.global_store.get("after") is None
        assert complaint in log(srv)
        await leader.stop()

    run(scenario)


def test_a_quiet_leader_is_not_dropped_for_being_quiet(srv, monkeypatch):
    monkeypatch.setattr(srv, "STALL_TIMEOUT", 0.2)
    monkeypatch.setattr(srv, "FRAME_DEADLINE", 0.5)

    async def script(writer):
        writer.write(snapshot({"base": "1"}))
        await asyncio.sleep(1.0)  # idle longer than every limit, between commands
        writer.write(kesp(b"SET", b"late", b"1"))
        await writer.drain()

    async def scenario():
        leader, task = await follow(srv, script)
        assert await wait_until(lambda: srv.global_store.get("late") == "1")
        assert not task.done()
        await leader.stop()

    run(scenario)
