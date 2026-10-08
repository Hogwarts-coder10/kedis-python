"""Streams at the store layer: XADD/XLEN/XRANGE/XREVRANGE/XREAD/XDEL/XTRIM,
type safety, and persistence (AOF replay, compaction, snapshot)."""

import os

import pytest

from kedis_python.store import KedisStore
from kedis_python.stream import StreamError

WRONGTYPE = "WRONGTYPE"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd: KedisStore reads/writes kedis.snapshot in the cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def open_store():
    return KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)


@pytest.fixture
def store(workdir):
    s = open_store()
    yield s
    s.shutdown()


def fill(store, key="s", n=5):
    return [store.xadd(key, ["n", str(i)], f"{100 + i}-0") for i in range(n)]


def ids(entries):
    return [e[0] for e in entries]


# ----------------------------------------------------------------------
# XADD
# ----------------------------------------------------------------------
class TestXadd:
    def test_auto_ids_are_increasing(self, store):
        a = store.xadd("s", ["k", "1"])
        b = store.xadd("s", ["k", "2"])
        assert a != b and store.xlen("s") == 2
        assert tuple(map(int, b.split("-"))) > tuple(map(int, a.split("-")))

    def test_explicit_id_returned(self, store):
        assert store.xadd("s", ["k", "v"], "5-5") == "5-5"

    def test_rejects_non_increasing_id(self, store):
        store.xadd("s", ["k", "v"], "5-5")
        with pytest.raises(StreamError, match="equal or smaller"):
            store.xadd("s", ["k", "v"], "5-5")
        assert store.xlen("s") == 1

    def test_failed_xadd_does_not_create_the_key(self, store):
        with pytest.raises(StreamError):
            store.xadd("s", ["k", "v"], "0-0")
        assert store.exists("s") == 0
        with pytest.raises(StreamError):
            store.xadd("s", ["odd"])
        assert store.exists("s") == 0

    def test_nomkstream(self, store):
        assert store.xadd("s", ["k", "v"], nomkstream=True) is None
        assert store.exists("s") == 0
        store.xadd("s", ["k", "v"])
        assert store.xadd("s", ["k", "v2"], nomkstream=True) is not None

    def test_maxlen_trims_after_adding(self, store):
        for i in range(5):
            store.xadd("s", ["n", str(i)], f"{i + 1}-0", maxlen=3)
        assert ids(store.xrange("s")) == ["3-0", "4-0", "5-0"]

    def test_minid_trims_after_adding(self, store):
        for i in range(5):
            store.xadd("s", ["n", str(i)], f"{i + 1}-0", minid="3-0")
        assert ids(store.xrange("s")) == ["3-0", "4-0", "5-0"]

    def test_bad_maxlen_changes_nothing(self, store):
        store.xadd("s", ["k", "v"], "1-0")
        with pytest.raises(StreamError):
            store.xadd("s", ["k", "v"], "2-0", maxlen=-1)
        assert store.xlen("s") == 1

    def test_xadd_bumps_watch_version(self, store):
        store.xadd("s", ["k", "v"])
        v = store._versions["s"]
        store.xadd("s", ["k", "v"])
        assert store._versions["s"] > v


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------
class TestReads:
    def test_xlen_missing_is_zero(self, store):
        assert store.xlen("nope") == 0

    def test_xrange_whole_and_bounded(self, store):
        fill(store)
        assert len(store.xrange("s")) == 5
        assert ids(store.xrange("s", "101-0", "103-0")) == ["101-0", "102-0", "103-0"]
        assert ids(store.xrange("s", "(101-0", "(103-0")) == ["102-0"]
        assert ids(store.xrange("s", "-", "+", count=2)) == ["100-0", "101-0"]

    def test_xrange_missing_key_is_empty(self, store):
        assert store.xrange("nope") == []

    def test_xrevrange(self, store):
        fill(store)
        assert ids(store.xrevrange("s", "+", "-", count=2)) == ["104-0", "103-0"]
        assert ids(store.xrevrange("s", "103-0", "101-0")) == ["103-0", "102-0", "101-0"]

    def test_entries_carry_their_fields(self, store):
        store.xadd("s", ["temp", "21", "hum", "40"], "1-0")
        assert store.xrange("s") == [("1-0", ["temp", "21", "hum", "40"])]

    def test_bad_range_bound_raises(self, store):
        with pytest.raises(StreamError, match="Invalid stream ID"):
            store.xrange("s", "nonsense", "+")


class TestXread:
    def test_returns_entries_after_id(self, store):
        fill(store)
        out = store.xread([("s", "102-0")])
        assert out[0][0] == "s" and ids(out[0][1]) == ["103-0", "104-0"]

    def test_count_limits_per_stream(self, store):
        fill(store, "a")
        fill(store, "b")
        out = store.xread([("a", "0-0"), ("b", "0-0")], count=2)
        assert [len(e) for _, e in out] == [2, 2]

    def test_streams_without_news_are_omitted(self, store):
        fill(store, "a", 3)
        store.xadd("b", ["k", "v"], "1-0")
        out = store.xread([("a", "999-0"), ("b", "0-0"), ("ghost", "0-0")])
        assert [k for k, _ in out] == ["b"]

    def test_dollar_means_only_new_entries(self, store):
        fill(store)
        assert store.xread([("s", "$")]) == []

    def test_resolve_pins_dollar_to_the_current_last_id(self, store):
        fill(store)
        pinned = store.xresolve_ids([("s", "$"), ("missing", "$"), ("s", "7-7")])
        assert pinned == [("s", "104-0"), ("missing", "0-0"), ("s", "7-7")]
        store.xadd("s", ["late", "entry"], "105-0")  # arrives "while blocked"
        out = store.xread(pinned[:1])
        assert ids(out[0][1]) == ["105-0"]

    def test_bad_id_raises_before_any_read(self, store):
        fill(store, "a")
        with pytest.raises(StreamError):
            store.xread([("a", "0-0"), ("a", "zzz")])


# ----------------------------------------------------------------------
# XDEL / XTRIM
# ----------------------------------------------------------------------
class TestDeleteAndTrim:
    def test_xdel_counts_existing_only(self, store):
        fill(store)
        assert store.xdel("s", "101-0", "103-0", "999-0") == 2
        assert ids(store.xrange("s")) == ["100-0", "102-0", "104-0"]

    def test_xdel_missing_key(self, store):
        assert store.xdel("nope", "1-0") == 0
        assert store.exists("nope") == 0

    def test_emptied_stream_still_exists_and_keeps_its_top_id(self, store):
        fill(store, n=2)
        store.xdel("s", "100-0", "101-0")
        assert store.xlen("s") == 0 and store.exists("s") == 1
        with pytest.raises(StreamError):
            store.xadd("s", ["k", "v"], "101-0")

    def test_xtrim_maxlen_and_minid(self, store):
        fill(store, n=6)
        assert store.xtrim("s", maxlen=4) == 2
        assert store.xtrim("s", minid="103-0") == 1
        assert ids(store.xrange("s")) == ["103-0", "104-0", "105-0"]

    def test_xtrim_needs_exactly_one_strategy(self, store):
        fill(store)
        with pytest.raises(StreamError):
            store.xtrim("s")
        with pytest.raises(StreamError):
            store.xtrim("s", maxlen=1, minid="1-0")


# ----------------------------------------------------------------------
# Type safety
# ----------------------------------------------------------------------
class TestTypes:
    def test_stream_commands_on_other_types_are_wrongtype(self, store):
        store.set("str", "v")
        store.lpush("lst", "a")
        for key in ("str", "lst"):
            for call in (
                lambda: store.xadd(key, ["k", "v"]),
                lambda: store.xlen(key),
                lambda: store.xrange(key),
                lambda: store.xread([(key, "0-0")]),
                lambda: store.xdel(key, "1-0"),
            ):
                with pytest.raises(TypeError, match=WRONGTYPE):
                    call()

    def test_other_commands_on_a_stream_are_wrongtype(self, store):
        store.xadd("s", ["k", "v"])
        with pytest.raises(TypeError):
            store.get("s")
        with pytest.raises(TypeError):
            store.lpush("s", "a")
        with pytest.raises(TypeError):
            store.setbit("s", 1, 1)
        with pytest.raises(TypeError):
            store.pfadd("s", "x")

    def test_set_overwrites_a_stream(self, store):
        store.xadd("s", ["k", "v"])
        store.set("s", "plain")
        assert store.get("s") == "plain"

    def test_type_keys_and_stats(self, store):
        fill(store, n=3)
        assert store.type_of("s") == "stream"
        assert store.keys()["s"] == {"type": "stream", "ttl": -1, "length": "3"}
        assert store.get_engine_stats()["stream_entries"] == 3

    def test_ttl_works_on_streams(self, store):
        store.xadd("s", ["k", "v"])
        store.set_expire("s", 1000)
        assert 0 < store.ttl("s") <= 1000


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------
def dump(s, key="s"):
    st = s._data[key]
    return st.range(), st.last_id, st.entries_added


class TestPersistence:
    def _build(self, s):
        s.xadd("s", ["a", "1"])  # auto IDs: replay must reproduce them exactly
        s.xadd("s", ["a", "2"])
        s.xadd("s", ["a", "3"], maxlen=2)
        s.xadd("s", ["a", "4"])
        s.xdel("s", s.xrange("s")[0][0])
        return dump(s)

    def test_aof_replay_reproduces_auto_ids_exactly(self, workdir):
        s = open_store()
        expected = self._build(s)
        s.shutdown()
        s2 = open_store()
        try:
            assert dump(s2) == expected
        finally:
            s2.shutdown()

    def test_awkward_field_text_round_trips(self, workdir):
        fields = ["na me", "val\nue", "héllo", "", "tab\there", "emoji", "✓ ok", "last"]
        s = open_store()
        s.xadd("s", fields, "1-0")
        s.shutdown()
        s2 = open_store()
        try:
            assert s2.xrange("s") == [("1-0", fields)]
        finally:
            s2.shutdown()

    def test_plain_entries_stay_readable_in_the_aof(self, workdir):
        s = open_store()
        s.xadd("s", ["temp", "21"], "1-0")
        s.shutdown()
        assert "XADD s 1-0 temp 21" in open("test.aof").read().split("\n")

    def test_compaction_round_trip(self, workdir):
        s = open_store()
        expected = self._build(s)
        assert s.compact_aof()
        s.shutdown()
        s2 = open_store()
        try:
            assert dump(s2) == expected
        finally:
            s2.shutdown()

    def test_compaction_keeps_top_id_of_an_emptied_stream(self, workdir):
        s = open_store()
        s.xadd("s", ["k", "v"], "9-9")
        s.xdel("s", "9-9")
        s.compact_aof()
        s.shutdown()
        s2 = open_store()
        try:
            assert s2.exists("s") == 1 and s2.xlen("s") == 0
            with pytest.raises(StreamError):
                s2.xadd("s", ["k", "v"], "9-9")
            assert s2.xadd("s", ["k", "v"], "9-10") == "9-10"
        finally:
            s2.shutdown()

    def test_writes_after_compaction_replay(self, workdir):
        s = open_store()
        self._build(s)
        s.compact_aof()
        s.xadd("s", ["a", "5"])
        s.xtrim("s", maxlen=1)
        expected = dump(s)
        s.shutdown()
        s2 = open_store()
        try:
            assert dump(s2) == expected
        finally:
            s2.shutdown()

    def test_snapshot_round_trip_with_awkward_text(self, workdir):
        s = open_store()
        s.xadd("s", ["na me", "val\nue ✓"], "1-0")
        s.xadd("s", ["k", "v"], "2-0")
        s.xdel("s", "2-0")
        expected = dump(s)
        assert s.save()
        s.shutdown()
        os.remove("test.aof")  # recover from the snapshot alone
        s2 = open_store()
        try:
            assert dump(s2) == expected
        finally:
            s2.shutdown()

    def test_bad_records_are_skipped(self, workdir):
        with open("test.aof", "w") as f:
            f.write("SET str v\n")
            f.write("XADD str 1-0 a b\n")  # wrong type: skipped
            f.write("XADD s 0-0 a b\n")  # zero id: skipped
            f.write("XADD s 5-0 a\n")  # odd fields: skipped
            f.write("XADDB64 s 5-0 !!!notbase64!!!\n")  # skipped
            f.write("XADD s 5-0 a b\n")
            f.write("XADD s 4-0 a b\n")  # not increasing: skipped
            f.write("XDEL s zzz\n")  # bad id: skipped
            f.write("XTRIM s BOGUS 1\n")  # bad strategy: skipped
        s = open_store()
        try:
            assert s.get("str") == "v"
            assert s.xrange("s") == [("5-0", ["a", "b"])]
        finally:
            s.shutdown()

    def test_replay_does_not_relog(self, workdir):
        s = open_store()
        s.xadd("s", ["a", "b"], "1-0")
        s.shutdown()
        before = open("test.aof").read()
        open_store().shutdown()
        assert open("test.aof").read() == before
