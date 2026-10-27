"""A follower takes no writes through MULTI/EXEC either: a write is refused when it is
queued, the block is poisoned, and EXEC aborts instead of running the rest."""

import io

import pytest
from rich.console import Console

from test_stream_server import serve  # shared wire helper


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # server.py builds its global store in the cwd on import
    from kedis_python import server

    server.console = Console(file=io.StringIO(), width=200)
    server.global_store.flushall()
    server.stream_waiters.clear()
    monkeypatch.setattr(server, "server_role", "replica")
    monkeypatch.setattr(server, "connected_replicas", [])
    yield server
    server.stream_waiters.clear()
    server.global_store.flushall()


def run(srv, steps):
    async def scenario(connect, errors):
        await steps(await connect(), srv)

    assert serve(srv, scenario) == []


def is_err(reply, code):
    return isinstance(reply, str) and reply.startswith(code)


@pytest.mark.parametrize(
    "cmd",
    [("SET", "k", "v"), ("DEL", "k"), ("LPUSH", "k", "v"), ("RPUSH", "k", "v"),
     ("SADD", "k", "v"), ("HSET", "k", "f", "v"), ("ZADD", "k", "1", "m"),
     ("XADD", "k", "*", "f", "v"), ("FLUSHALL",)],
)
def test_a_write_is_refused_at_queue_time_and_exec_aborts(srv, cmd):
    async def steps(c, s):
        assert (await c.call("MULTI")).strip() == "OK"
        assert is_err(await c.call(*cmd), "EREADONLY")
        assert is_err(await c.call("EXEC"), "EXECABORT")

    run(srv, steps)
    assert srv.global_store.get("k") is None


def test_lowercase_writes_are_refused_too(srv):
    async def steps(c, s):
        await c.call("MULTI")
        assert is_err(await c.call("set", "k", "v"), "EREADONLY")
        assert is_err(await c.call("EXEC"), "EXECABORT")

    run(srv, steps)
    assert srv.global_store.get("k") is None


def test_reads_around_the_refused_write_do_not_run_either(srv):
    srv.global_store.set("r", "1")

    async def steps(c, s):
        await c.call("MULTI")
        assert (await c.call("GET", "r")).strip() == "OK"  # queued
        assert is_err(await c.call("SET", "k", "v"), "EREADONLY")
        assert (await c.call("GET", "r")).strip() == "OK"  # still queued, block is poisoned
        assert is_err(await c.call("EXEC"), "EXECABORT")

    run(srv, steps)
    assert srv.global_store.get("k") is None


def test_a_read_only_block_still_runs_on_a_follower(srv):
    srv.global_store.set("r", "1")

    async def steps(c, s):
        await c.call("MULTI")
        await c.call("GET", "r")
        await c.call("EXISTS", "r")
        assert await c.call("EXEC") == ["1", 1]

    run(srv, steps)


def test_the_poison_ends_with_the_block(srv):
    async def steps(c, s):
        await c.call("MULTI")
        await c.call("SET", "k", "v")
        assert is_err(await c.call("EXEC"), "EXECABORT")
        await c.call("MULTI")  # a fresh block is clean
        await c.call("GET", "k")
        assert await c.call("EXEC") == [None]

    run(srv, steps)


def test_discard_clears_the_poison(srv):
    async def steps(c, s):
        await c.call("MULTI")
        await c.call("SET", "k", "v")
        assert (await c.call("DISCARD")).strip() == "OK"
        await c.call("MULTI")
        await c.call("GET", "k")
        assert await c.call("EXEC") == [None]

    run(srv, steps)


def test_a_direct_write_is_still_refused(srv):
    async def steps(c, s):
        assert is_err(await c.call("SET", "k", "v"), "EREADONLY")

    run(srv, steps)
    assert srv.global_store.get("k") is None


def test_a_block_queued_on_a_master_that_then_became_a_follower_aborts(srv, monkeypatch):
    monkeypatch.setattr(srv, "server_role", "master")

    async def steps(c, s):
        await c.call("MULTI")
        assert (await c.call("SET", "k", "v")).strip() == "OK"  # fine while master
        monkeypatch.setattr(s, "server_role", "replica")  # REPLICAOF lands mid-block
        assert is_err(await c.call("EXEC"), "EXECABORT")

    run(srv, steps)
    assert srv.global_store.get("k") is None


def test_a_master_is_unaffected(srv, monkeypatch):
    monkeypatch.setattr(srv, "server_role", "master")

    async def steps(c, s):
        await c.call("MULTI")
        assert (await c.call("SET", "k", "v")).strip() == "OK"
        assert await c.call("EXEC") == ["OK"]

    run(srv, steps)
    assert srv.global_store.get("k") == "v"


def test_each_client_has_its_own_poison(srv):
    async def scenario(connect, errors):
        a, b = await connect(), await connect()
        await a.call("MULTI")
        await b.call("MULTI")
        assert is_err(await a.call("SET", "k", "v"), "EREADONLY")
        await b.call("GET", "k")
        assert is_err(await a.call("EXEC"), "EXECABORT")
        assert await b.call("EXEC") == [None]

    assert serve(srv, scenario) == []
