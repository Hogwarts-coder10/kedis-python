"""TYPE reports lowercase names (as Redis does) and `none` for a missing key;
KEYS uses the same names."""

import pytest

from kedis_python.commands import CommandHandler
from kedis_python.store import KedisStore


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    yield CommandHandler(store)
    store.shutdown()


def run(h, line):
    return h.execute(line.split())


CREATE = {
    "str": ("SET str v", "string"),
    "lst": ("RPUSH lst a", "list"),
    "st": ("SADD st a", "set"),
    "hsh": ("HSET hsh f v", "hash"),
    "zs": ("ZADD zs 1 a", "zset"),
    "strm": ("XADD strm 1-0 k v", "stream"),
    "bits": ("SETBIT bits 3 1", "string"),
    "hll": ("PFADD hll a b", "string"),
}


@pytest.mark.parametrize("key", CREATE)
def test_type_names(h, key):
    command, expected = CREATE[key]
    run(h, command)
    assert run(h, f"TYPE {key}") == expected


def test_a_missing_key_is_none(h):
    assert run(h, "TYPE nope") == "none"


def test_an_expired_key_is_none(h):
    run(h, "SET k v")
    h.store._expires["k"] = 0  # already past
    assert run(h, "TYPE k") == "none"


def test_a_key_that_changes_type_reports_the_new_one(h):
    run(h, "RPUSH k a")
    assert run(h, "TYPE k") == "list"
    run(h, "SET k v")
    assert run(h, "TYPE k") == "string"


def test_names_are_all_lowercase(h):
    for command, _ in CREATE.values():
        run(h, command)
    for key in (*CREATE, "missing"):
        reply = run(h, f"TYPE {key}")
        assert reply == reply.lower()


def test_keys_and_type_agree(h):
    for command, _ in CREATE.values():
        run(h, command)
    listed = {line.split(" | ")[0]: line.split(" | ")[1] for line in run(h, "KEYS")}
    assert set(listed) == set(CREATE)
    for key in CREATE:
        assert listed[key] == run(h, f"TYPE {key}")


def test_arity(h):
    assert run(h, "TYPE").startswith("-ERR wrong number")
    assert run(h, "TYPE a b").startswith("-ERR wrong number")
