"""LRANGE index semantics: negative indexes count from the tail, out-of-range
ends are clamped, empty or inverted ranges give [] (as in Redis)."""

import pytest

from kedis_python.commands import CommandHandler
from kedis_python.store import KedisStore

ITEMS = ["a", "b", "c", "d", "e"]


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    handler = CommandHandler(store)
    handler.execute(["RPUSH", "l", *ITEMS])
    yield handler
    store.shutdown()


def lrange(h, start, stop, key="l"):
    return h.execute(["LRANGE", key, str(start), str(stop)])


def reference(items, start, stop):
    """Redis' definition, written independently of the implementation."""
    n = len(items)
    if start < 0:
        start = max(n + start, 0)
    if stop < 0:
        stop += n
    if start > stop or start >= n:
        return []
    return items[start : min(stop, n - 1) + 1]


class TestNegativeIndexes:
    def test_whole_list(self, h):
        assert lrange(h, 0, -1) == ITEMS

    def test_negative_start(self, h):
        assert lrange(h, -2, -1) == ["d", "e"]
        assert lrange(h, -3, 3) == ["c", "d"]

    def test_negative_stop(self, h):
        assert lrange(h, 0, -2) == ["a", "b", "c", "d"]
        assert lrange(h, 1, -3) == ["b", "c"]

    def test_the_last_element(self, h):
        assert lrange(h, -1, -1) == ["e"]


class TestClampingAndEmptyRanges:
    def test_start_before_the_head_is_clamped(self, h):
        assert lrange(h, -100, 1) == ["a", "b"]
        assert lrange(h, -100, -1) == ITEMS

    def test_stop_past_the_end_is_clamped(self, h):
        assert lrange(h, 3, 100) == ["d", "e"]

    def test_inverted_or_out_of_range_gives_empty(self, h):
        assert lrange(h, 3, 1) == []
        assert lrange(h, -1, -3) == []
        assert lrange(h, 5, 10) == []
        assert lrange(h, 0, -100) == []
        assert lrange(h, -100, -50) == []

    def test_missing_key_is_empty(self, h):
        assert lrange(h, 0, -1, key="nope") == []

    def test_stop_zero_is_the_head_only(self, h):
        assert lrange(h, 0, 0) == ["a"]


class TestErrors:
    def test_non_integer_index_is_an_error(self, h):
        assert h.execute(["LRANGE", "l", "x", "1"]).startswith("-ERR")

    def test_wrong_type_is_an_error(self, h):
        h.execute(["SET", "s", "v"])
        assert lrange(h, 0, -1, key="s").startswith("-ERR WRONGTYPE")


@pytest.mark.parametrize("start", range(-8, 8))
@pytest.mark.parametrize("stop", range(-8, 8))
def test_matches_the_reference_for_every_index_pair(h, start, stop):
    assert lrange(h, start, stop) == reference(ITEMS, start, stop)
