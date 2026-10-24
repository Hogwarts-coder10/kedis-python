"""REPLICAOF starts a background task that follows the leader. Its lifetime must
be managed: it was started and forgotten, so it could be garbage-collected
mid-run (replication silently stopping), REPLICAOF NO ONE left it running, and a
second REPLICAOF started a second stream next to the first."""

import asyncio
import gc
import io
import json
import time

import pytest
from rich.console import Console


def kesp(*args: bytes) -> bytes:
    return b"A%d\n" % len(args) + b"".join(b"S%d\n%s\n" % (len(a), a) for a in args)


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    monkeypatch.setattr(server, "server_role", "master")
    monkeypatch.setattr(server, "server_host", None)
    monkeypatch.setattr(server, "master_port", None)
    monkeypatch.setattr(server, "replication_task", None)
    monkeypatch.setattr(server, "_active_clients", 0)
    yield server
    server.stop_replication()
    server.global_store.flushall()


class FakeLeader:
    """Answers SYNC with a one-key snapshot, then sends whatever live() is given."""

    def __init__(self, base: str):
        self.base = base
        self.synced = asyncio.Event()
        self.closed = asyncio.Event()  # the follower hung up
        self._writer = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        assert await reader.readline() == b"SYNC\n"
        snapshot = json.dumps({"data": {self.base: "1"}, "expires": {}}).encode()
        writer.write(kesp(snapshot))
        await writer.drain()
        self._writer = writer
        self.synced.set()
        while await reader.read(65536):
            pass
        self.closed.set()

    async def live(self, *args: bytes):
        self._writer.write(kesp(*args))
        await self._writer.drain()

    async def stop(self):
        self.server.close()
        if self._writer is not None:
            self._writer.close()


def run(scenario):
    asyncio.run(asyncio.wait_for(scenario(), 30))


async def wait_until(predicate, limit=10.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def replicaof(srv, *words: str) -> bytes:
    """Sends REPLICAOF through a real session, like a client would."""
    server = await asyncio.start_server(srv.handle_connection, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(kesp(b"REPLICAOF", *(x.encode() for x in words)))
    await w.drain()
    reply = await r.readline()
    w.close()
    server.close()
    return reply


# --- the task is kept alive -------------------------------------------------------------------------


def test_the_task_is_kept_and_survives_garbage_collection(srv):
    async def scenario():
        leader = FakeLeader("base")
        await leader.start()
        srv.start_replication("127.0.0.1", leader.port)
        assert isinstance(srv.replication_task, asyncio.Task)
        await asyncio.wait_for(leader.synced.wait(), 10)
        assert await wait_until(lambda: srv.global_store.get("base") == "1")

        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0.05)
        assert not srv.replication_task.done()

        await leader.live(b"set", b"after-gc", b"yes")
        assert await wait_until(lambda: srv.global_store.get("after-gc") == "yes")
        await leader.stop()

    run(scenario)


def test_replicaof_through_a_session_keeps_the_task_too(srv):
    async def scenario():
        leader = FakeLeader("base")
        await leader.start()
        assert (await replicaof(srv, "127.0.0.1", str(leader.port))).startswith(
            b"+OK Replica handshake initiated"
        )
        assert isinstance(srv.replication_task, asyncio.Task)
        await asyncio.wait_for(leader.synced.wait(), 10)
        assert await wait_until(lambda: srv.global_store.get("base") == "1")
        gc.collect()
        await leader.live(b"SET", b"k", b"v")
        assert await wait_until(lambda: srv.global_store.get("k") == "v")
        await leader.stop()

    run(scenario)


# --- REPLICAOF NO ONE stops following ---------------------------------------------------------------


def test_replicaof_no_one_stops_the_stream_and_closes_its_socket(srv):
    async def scenario():
        leader = FakeLeader("base")
        await leader.start()
        await replicaof(srv, "127.0.0.1", str(leader.port))
        await asyncio.wait_for(leader.synced.wait(), 10)
        assert await wait_until(lambda: srv.global_store.get("base") == "1")
        assert srv.server_role == "replica"

        assert (await replicaof(srv, "NO", "ONE")).startswith(b"+OK Engine promoted")
        assert srv.server_role == "master"
        assert srv.replication_task is None
        await asyncio.wait_for(leader.closed.wait(), 10)  # we hung up on the leader

        await leader.live(b"SET", b"late", b"x")  # it no longer reaches us
        await asyncio.sleep(0.3)
        assert srv.global_store.get("late") is None
        assert srv.server_role == "master"
        await leader.stop()

    run(scenario)


def test_replicaof_no_one_with_nothing_to_stop_is_fine(srv):
    async def scenario():
        assert (await replicaof(srv, "NO", "ONE")).startswith(b"+OK Engine promoted")
        assert srv.replication_task is None

    run(scenario)


# --- a second REPLICAOF replaces the first stream ---------------------------------------------------


def test_a_second_replicaof_replaces_the_first_stream(srv):
    async def scenario():
        first, second = FakeLeader("from-first"), FakeLeader("from-second")
        await first.start()
        await second.start()
        await replicaof(srv, "127.0.0.1", str(first.port))
        await asyncio.wait_for(first.synced.wait(), 10)
        old_task = srv.replication_task

        await replicaof(srv, "127.0.0.1", str(second.port))
        await asyncio.wait_for(second.synced.wait(), 10)
        await asyncio.wait_for(first.closed.wait(), 10)  # the old connection was dropped
        assert old_task.cancelled() or old_task.done()
        assert srv.replication_task is not old_task

        assert await wait_until(lambda: srv.global_store.get("from-second") == "1")
        await first.live(b"SET", b"old", b"1")
        await second.live(b"SET", b"new", b"1")
        assert await wait_until(lambda: srv.global_store.get("new") == "1")
        assert srv.global_store.get("old") is None
        await first.stop()
        await second.stop()

    run(scenario)


# --- the stream closes its socket however it ends ---------------------------------------------------


def test_the_leader_socket_is_closed_when_the_stream_ends_on_its_own(srv):
    async def scenario():
        leader = FakeLeader("base")
        await leader.start()
        task = srv.start_replication("127.0.0.1", leader.port)
        await asyncio.wait_for(leader.synced.wait(), 10)
        leader._writer.close()  # the leader goes away
        await asyncio.wait_for(task, 10)
        assert srv.server_role == "replica"  # it ended cleanly (leader lost), no crash
        await asyncio.wait_for(leader.closed.wait(), 10)
        await leader.stop()

    run(scenario)
