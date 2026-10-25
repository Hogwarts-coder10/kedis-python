"""A reply must be a snapshot: no list, set, dict or deque in it may be one the
store (or the handler) still owns.

Replies are encoded after the engine lock is released, and in slices with the
event loop running in between, while other clients' commands run in worker
threads. A reply that held a live container could change size under the
encoder ("Set changed size during iteration") or show a half-applied write."""

import os
from collections import deque

import pytest

from kedis_python.commands import CommandHandler
from kedis_python.store import KedisStore

CONTAINERS = (list, set, dict, deque)


@pytest.fixture(scope="module")
def _engine(tmp_path_factory):
    """One engine for the module (shutting a store down takes a second)."""
    previous = os.getcwd()
    os.chdir(tmp_path_factory.mktemp("engine"))
    store = KedisStore()
    yield CommandHandler(store)
    store.shutdown()
    os.chdir(previous)


@pytest.fixture
def handler(_engine):
    """The engine, emptied and filled with one key of every type."""
    h = _engine
    run = lambda *t: h.execute(list(t), None)  # noqa: E731
    run("FLUSHALL")
    run("SET", "s", "hello")
    run("EXPIRE", "s", "100")
    run("RPUSH", "l", "a", "b", "c")
    run("SADD", "set", "x", "y")
    run("HSET", "h", "f1", "v1", "f2", "v2")
    run("ZADD", "z", "1", "a", "2", "b")
    run("SETBIT", "bits", "7", "1")
    run("PFADD", "hll", "a", "b")
    run("XADD", "st", "1-1", "f", "v")
    run("XADD", "st", "2-1", "g", "w")
    run("XGROUP", "CREATE", "st", "grp", "0")
    run("XREADGROUP", "GROUP", "grp", "c1", "COUNT", "1", "STREAMS", "st", ">")
    run("GET", "s")  # leaves a latency sample and a slowlog-eligible command behind
    return h


def owned_containers(h) -> set:
    """ids of every mutable container reachable from the store and the handler."""
    owned, seen = set(), set()

    def walk(obj):
        if id(obj) in seen or isinstance(obj, (str, bytes, int, float, bool, type(None))):
            return
        seen.add(id(obj))
        if isinstance(obj, CONTAINERS):
            owned.add(id(obj))
            if isinstance(obj, dict):
                for key, value in obj.items():
                    walk(key)
                    walk(value)
            else:
                for item in obj:
                    walk(item)
        elif isinstance(obj, tuple):
            for item in obj:
                walk(item)
        elif callable(obj):
            return
        elif hasattr(obj, "__dict__"):
            for value in vars(obj).values():
                walk(value)

    for name, value in vars(h).items():
        if name not in ("store", "_commands"):
            walk(value)
    walk(h.store)
    return owned


def aliased_paths(reply, owned) -> list:
    hits = []

    def walk(obj, path):
        if isinstance(obj, (list, set, dict, deque, tuple)):
            if isinstance(obj, CONTAINERS) and id(obj) in owned:
                hits.append(path)
            items = obj.items() if isinstance(obj, dict) else enumerate(obj)
            for key, value in items:
                walk(value, path + [key])

    walk(reply, [])
    return hits


# Commands whose reply can be a container, with arguments that make them return one.
AUDITED = {
    "KEYS": [[]],
    "LRANGE": [["l", "0", "-1"]],
    "SMEMBERS": [["set"]],
    "HGETALL": [["h"]],
    "ZRANGE": [["z", "0", "-1"], ["z", "0", "-1", "WITHSCORES"]],
    "XRANGE": [["st", "-", "+"]],
    "XREVRANGE": [["st", "+", "-"]],
    "XREAD": [["STREAMS", "st", "0"]],
    "XREADGROUP": [["GROUP", "grp", "c2", "COUNT", "5", "STREAMS", "st", ">"]],
    "XINFO": [["GROUPS", "st"], ["CONSUMERS", "st", "grp"]],
    "XPENDING": [["st", "grp"], ["st", "grp", "-", "+", "10"]],
    "STATS": [[]],
    "CONFIG": [["GET", "appendfsync"]],
    "SLOWLOG": [["GET"]],
    "LATENCY": [["LAG"], ["DOCTOR"]],
}

# Commands that answer with a status, a number, or one string by construction.
SCALARS = {
    "SET": [["k", "v"]],
    "GET": [["s"]],
    "DEL": [["s"]],
    "EXISTS": [["s"]],
    "EXPIRE": [["s", "10"]],
    "TTL": [["s"]],
    "FLUSHALL": [[]],
    "SAVE": [[]],
    "COMPACT": [[]],
    "LPUSH": [["l", "z"]],
    "RPUSH": [["l", "z"]],
    "LPOP": [["l"]],
    "RPOP": [["l"]],
    "SADD": [["set", "q"]],
    "SREM": [["set", "x"]],
    "HSET": [["h", "a", "b"]],
    "HGET": [["h", "f1"]],
    "ZADD": [["z", "3", "c"]],
    "TYPE": [["l"]],
    "PUBLISH": [["ch", "m"]],
    "SETBIT": [["bits", "1", "1"]],
    "GETBIT": [["bits", "7"]],
    "BITCOUNT": [["bits"]],
    "BITOP": [["AND", "dest", "bits", "bits"]],
    "PFADD": [["hll", "z"]],
    "PFCOUNT": [["hll"]],
    "PFMERGE": [["dest", "hll", "hll"]],
    "HOUSEKEEP": [[]],
    "XADD": [["st", "3-1", "a", "b"]],
    "XLEN": [["st"]],
    "XDEL": [["st", "1-1"]],
    "XTRIM": [["st", "MAXLEN", "1"]],
    "XGROUP": [["CREATE", "st", "g2", "0"]],
    "XACK": [["st", "grp", "1-1"]],
}

# Needs a live connection to answer; its replies are built from that connection.
NEEDS_CONNECTION = {"SUBSCRIBE"}


def test_every_public_command_is_classified_so_a_new_one_cannot_skip_the_audit(handler):
    classified = set(AUDITED) | set(SCALARS) | NEEDS_CONNECTION
    assert set(handler.public_commands) == classified, (
        "classify new commands in AUDITED (can return a container) or SCALARS"
    )
    assert not (set(AUDITED) & set(SCALARS))


def test_the_detector_sees_an_alias_when_there_is_one(handler):
    owned = owned_containers(handler)
    assert owned
    assert aliased_paths(handler.store.hgetall("h"), owned) == [[]]  # the live dict itself
    assert aliased_paths(handler.store._data["l"], owned) == [[]]  # the live deque
    assert aliased_paths({"wrapped": handler.store._data["set"]}, owned) == [["wrapped"]]
    assert aliased_paths(list(handler.store._data["l"]), owned) == []  # a copy is fine


@pytest.mark.parametrize(
    "command, args",
    [(c, a) for c, variants in AUDITED.items() for a in variants],
    ids=lambda v: v if isinstance(v, str) else " ".join(v),
)
def test_a_reply_that_can_hold_a_container_holds_only_copies(handler, command, args):
    owned = owned_containers(handler)
    reply = handler.execute([command] + args, None)
    assert not (isinstance(reply, str) and reply.startswith("-ERR")), reply
    assert aliased_paths(reply, owned) == []


@pytest.mark.parametrize(
    "command, args",
    [(c, a) for c, variants in SCALARS.items() for a in variants],
    ids=lambda v: v if isinstance(v, str) else " ".join(v),
)
def test_a_scalar_command_replies_with_a_scalar(handler, command, args):
    reply = handler.execute([command] + args, None)
    assert isinstance(reply, (str, int, float, type(None))), (command, reply)
