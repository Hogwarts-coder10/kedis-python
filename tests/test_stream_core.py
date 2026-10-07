"""Unit tests for the Stream data structure (no store, no commands)."""

import pytest

from kedis_python.stream import (
    MAX_ID,
    MIN_ID,
    U64_MAX,
    Stream,
    StreamError,
    format_id,
    parse_bound,
    parse_id,
)


def make(n=5, ms=1000):
    s = Stream()
    for i in range(n):
        s.add(["k", f"v{i}"], f"{ms + i}-0")
    return s


def ids(entries):
    return [e[0] for e in entries]


class TestIdParsing:
    def test_full_and_bare(self):
        assert parse_id("5-3") == (5, 3)
        assert parse_id("5") == (5, 0)
        assert parse_id("5", default_seq=U64_MAX) == (5, U64_MAX)

    @pytest.mark.parametrize(
        "bad", ["", "-", "a", "1-b", "-1", "1--2", "1-2-3", "18446744073709551616", "1.5"]
    )
    def test_rejects_garbage(self, bad):
        with pytest.raises(StreamError, match="Invalid stream ID"):
            parse_id(bad)

    def test_bounds(self):
        assert parse_bound("-", True) == MIN_ID
        assert parse_bound("+", False) == MAX_ID
        assert parse_bound("7", True) == (7, 0)
        assert parse_bound("7", False) == (7, U64_MAX)
        assert parse_bound("(7-0", True) == (7, 1)
        assert parse_bound("(7-0", False) == (6, U64_MAX)

    def test_exclusive_edges_error(self):
        with pytest.raises(StreamError):
            parse_bound(f"({U64_MAX}-{U64_MAX}", True)
        with pytest.raises(StreamError):
            parse_bound("(0-0", False)


class TestAdd:
    def test_auto_id_uses_clock_and_increments_seq(self):
        s = Stream()
        assert s.add(["a", "1"], "*", now_ms=100) == "100-0"
        assert s.add(["a", "2"], "*", now_ms=100) == "100-1"
        assert s.add(["a", "3"], "*", now_ms=101) == "101-0"

    def test_backwards_clock_never_reorders(self):
        s = Stream()
        s.add(["a", "1"], "*", now_ms=500)
        assert s.add(["a", "2"], "*", now_ms=100) == "500-1"

    def test_explicit_id(self):
        s = Stream()
        assert s.add(["a", "1"], "5-5") == "5-5"
        with pytest.raises(StreamError, match="equal or smaller"):
            s.add(["a", "2"], "5-5")
        with pytest.raises(StreamError, match="equal or smaller"):
            s.add(["a", "2"], "5-4")
        assert s.add(["a", "2"], "5-6") == "5-6"

    def test_zero_id_rejected(self):
        with pytest.raises(StreamError, match="greater than 0-0"):
            Stream().add(["a", "1"], "0-0")

    def test_partial_auto_sequence(self):
        s = Stream()
        assert s.add(["a", "1"], "0-*") == "0-1"  # 0-0 is never valid
        assert s.add(["a", "2"], "7-*") == "7-0"
        assert s.add(["a", "3"], "7-*") == "7-1"
        with pytest.raises(StreamError, match="equal or smaller"):
            s.add(["a", "4"], "6-*")

    @pytest.mark.parametrize("fields", [[], ["only-field"], ["a", "b", "c"]])
    def test_bad_field_lists(self, fields):
        with pytest.raises(StreamError, match="wrong number of arguments"):
            Stream().add(fields)

    def test_last_id_and_counters(self):
        s = make(3)
        assert s.last_id == (1002, 0)
        assert s.entries_added == 3 and len(s) == 3
        assert s.first_id == (1000, 0)

    def test_failed_add_changes_nothing(self):
        s = make(3)
        with pytest.raises(StreamError):
            s.add(["a", "1"], "1-1")
        assert len(s) == 3 and s.entries_added == 3 and s.last_id == (1002, 0)

    def test_sequence_overflow_is_an_error(self):
        s = Stream()
        s.add(["a", "1"], f"5-{U64_MAX}")
        with pytest.raises(StreamError):
            s.add(["a", "2"], "5-*")
        assert s.add(["a", "3"], "*", now_ms=1) == "6-0"


class TestReads:
    def test_range_inclusive(self):
        s = make(5)
        assert ids(s.range((1001, 0), (1003, 0))) == ["1001-0", "1002-0", "1003-0"]

    def test_range_whole_and_empty(self):
        s = make(5)
        assert len(s.range()) == 5
        assert s.range((2000, 0), (3000, 0)) == []
        assert Stream().range() == []

    def test_range_count(self):
        s = make(5)
        assert ids(s.range(count=2)) == ["1000-0", "1001-0"]
        assert s.range(count=0) == []

    def test_revrange(self):
        s = make(5)
        assert ids(s.revrange()) == [f"{1004 - i}-0" for i in range(5)]
        assert ids(s.revrange(count=2)) == ["1004-0", "1003-0"]
        assert ids(s.revrange((1003, 0), (1001, 0))) == ["1003-0", "1002-0", "1001-0"]

    def test_entries_carry_fields(self):
        s = Stream()
        s.add(["temp", "21", "hum", "40"], "1-0")
        assert s.range() == [("1-0", ["temp", "21", "hum", "40"])]

    def test_returned_fields_are_copies(self):
        s = Stream()
        s.add(["a", "1"], "1-0")
        s.range()[0][1][1] = "mutated"
        assert s.range()[0][1] == ["a", "1"]

    def test_read_after_is_exclusive(self):
        s = make(5)
        assert ids(s.read_after((1002, 0))) == ["1003-0", "1004-0"]
        assert s.read_after((1004, 0)) == []
        assert ids(s.read_after(MIN_ID, count=2)) == ["1000-0", "1001-0"]
        assert s.read_after(MAX_ID) == []

    def test_read_after_between_entries(self):
        s = make(3)
        assert ids(s.read_after((1000, 5))) == ["1001-0", "1002-0"]


class TestDeleteAndTrim:
    def test_delete_existing_and_missing(self):
        s = make(5)
        assert s.delete((1001, 0), (1003, 0), (9, 9)) == 2
        assert ids(s.range()) == ["1000-0", "1002-0", "1004-0"]

    def test_delete_does_not_lower_last_id(self):
        s = make(3)
        s.delete((1002, 0))
        assert s.last_id == (1002, 0)
        with pytest.raises(StreamError):
            s.add(["a", "1"], "1002-0")
        assert s.add(["a", "1"], "1002-1") == "1002-1"

    def test_trim_maxlen(self):
        s = make(5)
        assert s.trim(maxlen=2) == 3
        assert ids(s.range()) == ["1003-0", "1004-0"]
        assert s.trim(maxlen=10) == 0

    def test_trim_maxlen_zero_empties_but_keeps_last_id(self):
        s = make(3)
        assert s.trim(maxlen=0) == 3
        assert len(s) == 0 and s.last_id == (1002, 0)

    def test_trim_minid(self):
        s = make(5)
        assert s.trim(minid=(1002, 0)) == 2
        assert s.first_id == (1002, 0)

    def test_trim_combined_uses_stricter_limit(self):
        s = make(10)
        assert s.trim(maxlen=8, minid=(1005, 0)) == 5

    def test_negative_maxlen(self):
        with pytest.raises(StreamError):
            make(3).trim(maxlen=-1)


class TestPersistenceState:
    def test_round_trip(self):
        s = make(4)
        s.delete((1001, 0))
        t = Stream.from_state(s.to_state())
        assert t.range() == s.range()
        assert t.last_id == s.last_id and t.entries_added == s.entries_added

    def test_round_trip_preserves_last_id_after_trim(self):
        s = make(3)
        s.trim(maxlen=0)
        t = Stream.from_state(s.to_state())
        assert len(t) == 0 and t.last_id == (1002, 0)

    def test_state_is_json_safe(self):
        import json

        s = make(3)
        assert Stream.from_state(json.loads(json.dumps(s.to_state()))).range() == s.range()

    def test_format_id(self):
        assert format_id((12, 3)) == "12-3"
