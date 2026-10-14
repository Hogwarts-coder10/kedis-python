"""DEL and EXISTS take one or more keys, with Redis' counting rules."""

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


def restart(h):
    h.store.shutdown()
    store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    return CommandHandler(store)


class TestDel:
    def test_deletes_several_keys_and_counts_them(self, h):
        run(h, "SET a 1")
        run(h, "SET b 2")
        run(h, "SET c 3")
        assert run(h, "DEL a b c") == 3
        assert run(h, "EXISTS a b c") == 0

    def test_missing_keys_are_ignored(self, h):
        run(h, "SET a 1")
        assert run(h, "DEL a nope other") == 1
        assert run(h, "DEL nope") == 0

    def test_a_key_named_twice_counts_once(self, h):
        run(h, "SET a 1")
        assert run(h, "DEL a a") == 1

    def test_works_across_types(self, h):
        for line in ("SET s v", "RPUSH l a", "SADD st a", "HSET hs f v", "ZADD z 1 a",
                     "XADD x 1-0 k v", "PFADD p a"):  # fmt: skip
            run(h, line)
        assert run(h, "DEL s l st hs z x p") == 7
        assert run(h, "KEYS") == []

    def test_single_key_still_works(self, h):
        run(h, "SET a 1")
        assert run(h, "DEL a") == 1
        assert run(h, "DEL a") == 0

    def test_an_expired_key_is_not_counted(self, h):
        run(h, "SET a 1")
        run(h, "SET b 2")
        h.store._expires["a"] = 0
        assert run(h, "DEL a b") == 1

    def test_every_deletion_survives_a_restart(self, h):
        for k in "abc":
            run(h, f"SET {k} 1")
        run(h, "DEL a c")
        h2 = restart(h)
        assert h2.execute(["EXISTS", "a", "b", "c"]) == 1
        assert h2.execute(["GET", "b"]) == "1"
        h2.store.shutdown()

    def test_bumps_the_version_of_each_deleted_key_for_watch(self, h):
        run(h, "SET a 1")
        run(h, "SET b 2")
        before = dict(h.store._versions)
        run(h, "DEL a b")
        assert h.store._versions["a"] == before["a"] + 1
        assert h.store._versions["b"] == before["b"] + 1

    def test_arity(self, h):
        assert run(h, "DEL").startswith("-ERR wrong number")


class TestExists:
    def test_counts_how_many_exist(self, h):
        run(h, "SET a 1")
        run(h, "SET b 2")
        assert run(h, "EXISTS a b c") == 2
        assert run(h, "EXISTS c d") == 0

    def test_a_key_named_twice_counts_twice(self, h):
        run(h, "SET a 1")
        assert run(h, "EXISTS a a") == 2
        assert run(h, "EXISTS a a nope") == 2

    def test_an_expired_key_counts_as_missing(self, h):
        run(h, "SET a 1")
        run(h, "SET b 2")
        h.store._expires["a"] = 0
        assert run(h, "EXISTS a b") == 1

    def test_single_key_still_works(self, h):
        run(h, "SET a 1")
        assert run(h, "EXISTS a") == 1
        assert run(h, "EXISTS z") == 0

    def test_arity(self, h):
        assert run(h, "EXISTS").startswith("-ERR wrong number")
