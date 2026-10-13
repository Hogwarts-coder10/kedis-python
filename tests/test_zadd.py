"""ZADD validates every pair before applying any, and rejects NaN scores."""

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


class TestAllOrNothing:
    def test_a_bad_score_late_in_the_command_adds_nothing(self, h):
        assert run(h, "ZADD z 1 a x b").startswith("-ERR")
        assert run(h, "EXISTS z") == 0
        assert run(h, "ZRANGE z 0 -1") == []

    def test_a_bad_score_changes_no_existing_scores(self, h):
        run(h, "ZADD z 1 a 2 b")
        assert run(h, "ZADD z 9 a 3 c oops d").startswith("-ERR")
        assert run(h, "ZRANGE z 0 -1 WITHSCORES") == ["a", "1", "b", "2"]

    def test_a_bad_first_score_adds_nothing(self, h):
        assert run(h, "ZADD z x a 1 b").startswith("-ERR")
        assert run(h, "EXISTS z") == 0

    def test_wrong_type_adds_nothing(self, h):
        run(h, "SET s v")
        assert run(h, "ZADD s 1 a").startswith("-ERR WRONGTYPE")
        assert run(h, "GET s") == "v"

    def test_nothing_is_logged_when_rejected(self, h):
        run(h, "ZADD z 1 a nan b")
        assert run(h, "EXISTS z") == 0
        h.store.shutdown()
        store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
        try:
            assert CommandHandler(store).execute(["EXISTS", "z"]) == 0
        finally:
            store.shutdown()


class TestNaN:
    @pytest.mark.parametrize("bad", ["nan", "NaN", "NAN", "-nan", "+nan"])
    def test_nan_is_rejected_in_any_spelling(self, h, bad):
        assert run(h, f"ZADD z {bad} a") == "-ERR value is not a valid float"
        assert run(h, "EXISTS z") == 0

    def test_nan_in_a_later_pair_adds_nothing(self, h):
        assert run(h, "ZADD z 1 a nan b").startswith("-ERR")
        assert run(h, "EXISTS z") == 0

    def test_nan_cannot_reach_the_store_directly(self, h):
        with pytest.raises(ValueError):
            h.store.zadd("z", float("nan"), "a")
        assert not h.store.exists("z")

    def test_nan_never_replaces_an_existing_score(self, h):
        run(h, "ZADD z 1 a")
        assert run(h, "ZADD z nan a").startswith("-ERR")
        assert run(h, "ZRANGE z 0 -1 WITHSCORES") == ["a", "1"]


class TestStillWorks:
    def test_normal_adds_and_updates(self, h):
        assert run(h, "ZADD z 1 a 2 b") == 2
        assert run(h, "ZADD z 5 a 3 c") == 1
        assert run(h, "ZRANGE z 0 -1") == ["b", "c", "a"]

    def test_repeated_member_last_score_wins(self, h):
        assert run(h, "ZADD z 1 a 2 a") == 1
        assert run(h, "ZRANGE z 0 -1 WITHSCORES") == ["a", "2"]

    def test_infinities_are_valid_scores(self, h):
        assert run(h, "ZADD z inf hi -inf lo 0 mid") == 3
        assert run(h, "ZRANGE z 0 -1") == ["lo", "mid", "hi"]

    def test_infinities_survive_a_restart(self, h):
        run(h, "ZADD z inf hi -inf lo 1 mid")
        h.store.shutdown()
        store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
        try:
            assert CommandHandler(store).execute(["ZRANGE", "z", "0", "-1"]) == ["lo", "mid", "hi"]
        finally:
            store.shutdown()

    def test_argument_count_errors_unchanged(self, h):
        assert run(h, "ZADD z 1").startswith("-ERR wrong number")
        assert run(h, "ZADD z 1 a 2").startswith("-ERR wrong number")
