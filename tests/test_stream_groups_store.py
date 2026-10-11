"""Consumer groups at the store layer: XGROUP / XREADGROUP / XACK / XPENDING /
XINFO semantics, the resolved records every change produces, and persistence
(AOF replay, compaction, snapshot, replica apply)."""

import os

import pytest

from kedis_python.store import KedisStore
from kedis_python.stream import StreamError


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def open_store(aof="test.aof"):
    return KedisStore(aof_filename=aof, appendfsync="always", lru_maxsize=100_000)


@pytest.fixture
def store(workdir):
    s = open_store()
    yield s
    s.shutdown()


def fill(store, key="s", n=5):
    return [store.xadd(key, ["n", str(i)], f"{i + 1}-0") for i in range(n)]


def ids(entries):
    return [e[0] for e in entries]


def read(store, consumer, count=None, key="s", group="g", sid=">", noack=False):
    out = store.xreadgroup(group, consumer, [(key, sid)], count, noack)
    return out[0][1] if out else []


def clock(store, value):
    store._now_ms = lambda: value


def groups_of(s, key="s"):
    return s._data[key].to_state().get("groups")


@pytest.fixture
def grp(store):
    fill(store)
    store.xgroup_create("s", "g", "0")
    return store


# ----------------------------------------------------------------------
# XGROUP
# ----------------------------------------------------------------------
class TestAdmin:
    def test_create_and_basic_errors(self, store):
        fill(store)
        assert store.xgroup_create("s", "g", "0") == "OK"
        with pytest.raises(StreamError, match="BUSYGROUP"):
            store.xgroup_create("s", "g", "0")
        with pytest.raises(StreamError, match="requires the key to exist"):
            store.xgroup_create("nope", "g", "0")
        with pytest.raises(StreamError, match="Invalid stream ID"):
            store.xgroup_create("s", "g2", "zzz")
        assert list(store._data["s"].groups) == ["g"]  # failures changed nothing

    def test_mkstream_creates_an_empty_stream(self, store):
        assert store.xgroup_create("fresh", "g", "$", mkstream=True) == "OK"
        assert store.exists("fresh") == 1 and store.xlen("fresh") == 0
        assert store.type_of("fresh") == "stream"
        assert store.xadd("fresh", ["k", "v"], "1-0") == "1-0"

    def test_dollar_pins_to_the_current_last_id(self, store):
        fill(store, n=3)
        store.xgroup_create("s", "g", "$")
        assert store._data["s"].groups["g"].last_id == (3, 0)
        store.xadd("s", ["k", "v"], "4-0")
        assert ids(read(store, "c")) == ["4-0"]

    def test_zero_delivers_the_whole_backlog(self, grp):
        assert len(read(grp, "c")) == 5

    def test_a_bare_millisecond_id_is_accepted(self, store):
        fill(store)
        store.xgroup_create("s", "g", "3")
        assert ids(read(store, "c")) == ["4-0", "5-0"]

    def test_setid_moves_the_cursor(self, grp):
        read(grp, "c")
        assert grp.xgroup_setid("s", "g", "3-0") == "OK"
        assert ids(read(grp, "c")) == ["4-0", "5-0"]
        grp.xgroup_setid("s", "g", "$")
        assert read(grp, "c") == []

    def test_setid_errors(self, grp):
        with pytest.raises(StreamError, match="NOGROUP"):
            grp.xgroup_setid("s", "nope", "0")
        with pytest.raises(StreamError, match="requires the key to exist"):
            grp.xgroup_setid("nope", "g", "0")

    def test_destroy(self, grp):
        assert grp.xgroup_destroy("s", "g") == 1
        assert grp.xgroup_destroy("s", "g") == 0
        with pytest.raises(StreamError, match="requires the key to exist"):
            grp.xgroup_destroy("nope", "g")
        assert grp.xlen("s") == 5  # the stream itself is untouched

    def test_createconsumer(self, grp):
        assert grp.xgroup_createconsumer("s", "g", "c") == 1
        assert grp.xgroup_createconsumer("s", "g", "c") == 0
        with pytest.raises(StreamError, match="NOGROUP"):
            grp.xgroup_createconsumer("s", "nope", "c")

    def test_delconsumer_reports_and_drops_its_pending_entries(self, grp):
        read(grp, "a", count=3)
        read(grp, "b")
        assert grp.xgroup_delconsumer("s", "g", "a") == 3
        assert grp.xgroup_delconsumer("s", "g", "a") == 0
        assert grp.xpending_summary("s", "g")[0] == 2
        assert [c[0] for c in grp.xinfo_consumers("s", "g")] == ["b"]

    def test_delconsumer_of_an_idle_consumer_still_removes_it(self, grp):
        grp.xgroup_createconsumer("s", "g", "idle")
        assert grp.xgroup_delconsumer("s", "g", "idle") == 0
        assert grp.xinfo_consumers("s", "g") == []

    @pytest.mark.parametrize("name", ["", "two words", "héllo", "tab\t"])
    def test_names_must_be_aof_safe(self, grp, name):
        with pytest.raises(StreamError, match="Invalid group or consumer name"):
            grp.xgroup_create("s", name, "0")
        with pytest.raises(StreamError, match="Invalid group or consumer name"):
            grp.xgroup_createconsumer("s", "g", name)
        with pytest.raises(StreamError, match="Invalid group or consumer name"):
            grp.xreadgroup("g", name, [("s", ">")])

    def test_wrongtype(self, store):
        store.set("str", "v")
        for call in (
            lambda: store.xgroup_create("str", "g", "0", mkstream=True),
            lambda: store.xgroup_destroy("str", "g"),
            lambda: store.xreadgroup("g", "c", [("str", ">")]),
            lambda: store.xack("str", "g", "1-0"),
            lambda: store.xpending_summary("str", "g"),
            lambda: store.xinfo_groups("str"),
        ):
            with pytest.raises(TypeError, match="WRONGTYPE"):
                call()


# ----------------------------------------------------------------------
# XREADGROUP
# ----------------------------------------------------------------------
class TestXreadgroup:
    def test_consumers_share_the_stream_without_overlap(self, grp):
        a = ids(read(grp, "a", count=2))
        b = ids(read(grp, "b", count=2))
        c = ids(read(grp, "a"))
        assert (a, b, c) == (["1-0", "2-0"], ["3-0", "4-0"], ["5-0"])
        assert read(grp, "b") == []

    def test_delivery_is_recorded_in_the_pel(self, grp):
        clock(grp, 5000)
        read(grp, "a", count=2)
        g = grp._data["s"].groups["g"]
        assert g.last_id == (2, 0)
        assert {sid: (p.consumer, p.delivery_ms, p.count) for sid, p in g.pel.items()} == {
            (1, 0): ("a", 5000, 1),
            (2, 0): ("a", 5000, 1),
        }

    def test_new_entries_after_the_group_exists_are_delivered(self, grp):
        read(grp, "a")
        grp.xadd("s", ["late", "one"], "6-0")
        assert ids(read(grp, "a")) == ["6-0"]

    def test_noack_advances_the_cursor_but_leaves_nothing_pending(self, grp):
        assert len(read(grp, "a", count=3, noack=True)) == 3
        assert grp.xpending_summary("s", "g")[0] == 0
        assert ids(read(grp, "a")) == ["4-0", "5-0"]

    def test_history_returns_only_the_consumers_own_entries(self, grp):
        read(grp, "a", count=2)
        read(grp, "b", count=2)
        assert ids(read(grp, "a", sid="0")) == ["1-0", "2-0"]
        assert ids(read(grp, "b", sid="0")) == ["3-0", "4-0"]
        assert ids(read(grp, "a", sid="1-0")) == ["2-0"]  # strictly after
        assert ids(read(grp, "a", sid="0", count=1)) == ["1-0"]

    def test_history_counts_as_another_delivery_attempt(self, grp):
        clock(grp, 1000)
        read(grp, "a", count=1)
        clock(grp, 9000)
        read(grp, "a", sid="0")
        (row,) = grp.xpending_range("s", "g", "-", "+", 10)
        assert row[1:] == ("a", 0, 2)  # delivered again just now: idle 0, count 2

    def test_history_shows_a_deleted_entry_as_missing_fields(self, grp):
        read(grp, "a", count=2)
        grp.xdel("s", "1-0")
        assert read(grp, "a", sid="0") == [
            ("1-0", None),
            ("2-0", ["n", "1"]),
        ]
        assert grp.xpending_summary("s", "g")[0] == 2  # still pending until acked

    def test_history_is_present_even_when_empty(self, grp):
        assert grp.xreadgroup("g", "fresh", [("s", "0")]) == [("s", [])]

    def test_nothing_new_leaves_a_greater_than_stream_out(self, grp):
        read(grp, "a")
        assert grp.xreadgroup("g", "a", [("s", ">")]) == []

    def test_an_empty_read_still_registers_the_consumer(self, grp):
        read(grp, "a")
        assert grp.xreadgroup("g", "newcomer", [("s", ">")]) == []
        assert "newcomer" in [c[0] for c in grp.xinfo_consumers("s", "g")]

    def test_several_streams_in_one_call(self, store):
        fill(store, "a", 2)
        fill(store, "b", 3)
        store.xgroup_create("a", "g", "0")
        store.xgroup_create("b", "g", "0")
        out = store.xreadgroup("g", "c", [("a", ">"), ("b", ">")], count=2)
        assert [(k, ids(e)) for k, e in out] == [("a", ["1-0", "2-0"]), ("b", ["1-0", "2-0"])]

    def test_a_missing_group_on_any_stream_changes_nothing(self, store):
        fill(store, "a", 2)
        fill(store, "b", 2)
        store.xgroup_create("a", "g", "0")  # "b" has no group
        store.drain_group_records()
        with pytest.raises(StreamError, match="NOGROUP"):
            store.xreadgroup("g", "c", [("a", ">"), ("b", ">")])
        assert store.xpending_summary("a", "g")[0] == 0
        assert store._data["a"].groups["g"].last_id == (0, 0)
        assert store.drain_group_records() == []

    def test_a_repeated_key_sees_its_own_earlier_delivery(self, grp):
        out = grp.xreadgroup("g", "c", [("s", ">"), ("s", ">")], count=2)
        assert [ids(e) for _, e in out] == [["1-0", "2-0"], ["3-0", "4-0"]]

    def test_rewinding_the_group_redelivers_and_transfers_ownership(self, grp):
        read(grp, "a", count=2)
        grp.xgroup_setid("s", "g", "0")
        assert ids(read(grp, "b", count=2)) == ["1-0", "2-0"]
        rows = {r[0]: r[1:] for r in grp.xpending_range("s", "g", "-", "+", 10)}
        assert (rows["1-0"][0], rows["1-0"][2]) == ("b", 2)
        assert grp.xpending_summary("s", "g")[0] == 2  # moved, not duplicated

    def test_errors(self, grp):
        with pytest.raises(StreamError, match=r"\$ ID is meaningless"):
            grp.xreadgroup("g", "c", [("s", "$")])
        with pytest.raises(StreamError, match="NOGROUP No such key 'nope' or consumer group 'g'"):
            grp.xreadgroup("g", "c", [("nope", ">")])
        with pytest.raises(StreamError, match="NOGROUP"):
            grp.xreadgroup("other", "c", [("s", ">")])
        with pytest.raises(StreamError, match="Invalid stream ID"):
            grp.xreadgroup("g", "c", [("s", "zzz")])


# ----------------------------------------------------------------------
# XACK / XPENDING / XINFO
# ----------------------------------------------------------------------
class TestAckPendingInfo:
    def test_xack(self, grp):
        read(grp, "a")
        assert grp.xack("s", "g", "1-0", "3-0", "99-0") == 2
        assert grp.xack("s", "g", "1-0") == 0
        assert grp.xpending_summary("s", "g")[0] == 3

    def test_xack_counts_a_repeated_id_once(self, grp):
        read(grp, "a")
        assert grp.xack("s", "g", "1-0", "1-0", "1-0") == 1

    def test_xack_on_nothing_is_zero_and_bad_ids_raise(self, grp):
        assert grp.xack("nope", "g", "1-0") == 0
        assert grp.xack("s", "nogroup", "1-0") == 0
        read(grp, "a")
        with pytest.raises(StreamError, match="Invalid stream ID"):
            grp.xack("s", "g", "1-0", "zzz")
        assert grp.xpending_summary("s", "g")[0] == 5  # the bad call acked nothing

    def test_acking_an_entry_that_was_deleted_still_clears_it(self, grp):
        read(grp, "a", count=1)
        grp.xdel("s", "1-0")
        assert grp.xack("s", "g", "1-0") == 1

    def test_summary_empty_and_populated(self, grp):
        assert grp.xpending_summary("s", "g") == (0, None, None, [])
        read(grp, "zed", count=1)
        read(grp, "amy", count=2)
        assert grp.xpending_summary("s", "g") == (3, "1-0", "3-0", [("amy", 2), ("zed", 1)])

    def test_range_bounds_count_and_filters(self, grp):
        clock(grp, 1000)
        read(grp, "a", count=3)
        clock(grp, 4000)
        read(grp, "b")
        clock(grp, 10_000)
        rows = grp.xpending_range("s", "g", "-", "+", 10)
        assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
            ("1-0", "a", 9000, 1),
            ("2-0", "a", 9000, 1),
            ("3-0", "a", 9000, 1),
            ("4-0", "b", 6000, 1),
            ("5-0", "b", 6000, 1),
        ]
        assert ids(grp.xpending_range("s", "g", "(1-0", "4-0", 10)) == ["2-0", "3-0", "4-0"]
        assert ids(grp.xpending_range("s", "g", "-", "+", 2)) == ["1-0", "2-0"]
        assert grp.xpending_range("s", "g", "-", "+", 0) == []
        assert ids(grp.xpending_range("s", "g", "-", "+", 10, consumer="b")) == ["4-0", "5-0"]
        assert ids(grp.xpending_range("s", "g", "-", "+", 10, min_idle=7000)) == [
            "1-0", "2-0", "3-0",
        ]  # fmt: skip

    def test_pending_errors(self, grp):
        with pytest.raises(StreamError, match="NOGROUP No such key 'nope' or consumer group 'g'"):
            grp.xpending_summary("nope", "g")
        with pytest.raises(StreamError, match="NOGROUP"):
            grp.xpending_range("s", "other", "-", "+", 1)
        with pytest.raises(StreamError, match="Invalid stream ID"):
            grp.xpending_range("s", "g", "zzz", "+", 1)

    def test_xinfo_groups_sorted_by_name(self, grp):
        grp.xgroup_create("s", "alpha", "$")
        read(grp, "a", count=2)
        assert grp.xinfo_groups("s") == [("alpha", 0, 0, "5-0"), ("g", 1, 2, "2-0")]

    def test_xinfo_consumers(self, grp):
        clock(grp, 1000)
        read(grp, "busy", count=1)
        grp.xgroup_createconsumer("s", "g", "idle")
        clock(grp, 1600)
        assert grp.xinfo_consumers("s", "g") == [
            ("busy", 1, 600, 600),
            ("idle", 0, 600, -1),  # never handed an entry: no "inactive" time
        ]

    def test_xinfo_errors(self, grp):
        with pytest.raises(StreamError, match="no such key"):
            grp.xinfo_groups("nope")
        with pytest.raises(StreamError, match="no such key"):
            grp.xinfo_consumers("nope", "g")
        with pytest.raises(StreamError, match="NOGROUP"):
            grp.xinfo_consumers("s", "nope")


# ----------------------------------------------------------------------
# Resolved records: what replicas and the AOF are fed
# ----------------------------------------------------------------------
class TestRecords:
    def test_xreadgroup_emits_fully_resolved_records(self, grp):
        grp.drain_group_records()
        clock(grp, 777)
        read(grp, "c", count=2)
        assert grp.drain_group_records() == [
            ["XGCONS", "s", "g", "c", "777", "-1"],
            ["XGPEL", "s", "g", "c", "1-0", "777", "1"],
            ["XGPEL", "s", "g", "c", "2-0", "777", "1"],
            ["XGLAST", "s", "g", "2-0"],
        ]

    def test_noack_emits_no_pel_records(self, grp):
        grp.drain_group_records()
        clock(grp, 5)
        read(grp, "c", count=1, noack=True)
        assert grp.drain_group_records() == [
            ["XGCONS", "s", "g", "c", "5", "-1"],
            ["XGLAST", "s", "g", "1-0"],
        ]

    def test_admin_commands_emit_one_record_each(self, store):
        fill(store)
        store.drain_group_records()
        clock(store, 9)
        store.xgroup_create("s", "g", "$")
        store.xgroup_setid("s", "g", "2-0")
        store.xgroup_createconsumer("s", "g", "c")
        store.xgroup_delconsumer("s", "g", "c")
        store.xgroup_destroy("s", "g")
        assert store.drain_group_records() == [
            ["XGROUP", "s", "g", "5-0"],  # '$' is resolved to a real id
            ["XGROUP", "s", "g", "2-0"],
            ["XGCONS", "s", "g", "c", "9", "-1"],
            ["XGCDEL", "s", "g", "c"],
            ["XGDEL", "s", "g"],
        ]

    def test_xack_emits_only_ids_that_were_pending(self, grp):
        read(grp, "c", count=2)
        grp.drain_group_records()
        grp.xack("s", "g", "1-0", "1-0", "9-9", "2-0")
        assert grp.drain_group_records() == [["XACK", "s", "g", "1-0", "2-0"]]
        grp.xack("s", "g", "1-0")  # nothing pending: nothing to replicate
        assert grp.drain_group_records() == []

    def test_drain_hands_records_over_once(self, grp):
        read(grp, "c")
        assert grp.drain_group_records() != []
        assert grp.drain_group_records() == []

    def test_failed_and_read_only_calls_emit_nothing(self, grp):
        grp.drain_group_records()
        with pytest.raises(StreamError):
            grp.xgroup_create("s", "g", "0")
        with pytest.raises(StreamError):
            grp.xreadgroup("g", "c", [("nope", ">")])
        grp.xpending_summary("s", "g")
        grp.xinfo_groups("s")
        grp.xadd("s", ["k", "v"])
        assert grp.drain_group_records() == []

    def test_group_writes_bump_the_watch_version(self, grp):
        v = grp._versions["s"]
        read(grp, "c")
        assert grp._versions["s"] > v
        v = grp._versions["s"]
        grp.xack("s", "g", "1-0")
        assert grp._versions["s"] > v


# ----------------------------------------------------------------------
# Persistence and replicas
# ----------------------------------------------------------------------
def build(s):
    """A stream with two groups, several consumers and mixed pending state."""
    fill(s, n=6)
    s.xgroup_create("s", "g", "0")
    s.xgroup_create("s", "other", "3-0")
    read(s, "a", count=3)
    read(s, "b", count=1)
    read(s, "a", sid="0", count=1)  # redelivery: count 2
    s.xack("s", "g", "2-0")
    s.xgroup_createconsumer("s", "g", "idle")
    s.xgroup_delconsumer("s", "g", "b")
    s.xgroup_setid("s", "other", "4-0")
    return groups_of(s)


class TestPersistence:
    def test_aof_replay_rebuilds_groups_exactly(self, workdir):
        s = open_store()
        expected = build(s)
        s.shutdown()
        s2 = open_store()
        try:
            assert groups_of(s2) == expected
            assert expected  # the scenario really built something
        finally:
            s2.shutdown()

    def test_compaction_round_trip(self, workdir):
        s = open_store()
        expected = build(s)
        assert s.compact_aof()
        s.shutdown()
        s2 = open_store()
        try:
            assert groups_of(s2) == expected
        finally:
            s2.shutdown()

    def test_writes_after_compaction_replay(self, workdir):
        s = open_store()
        build(s)
        s.compact_aof()
        read(s, "c", count=2)
        s.xack("s", "g", "1-0")
        expected = groups_of(s)
        s.shutdown()
        s2 = open_store()
        try:
            assert groups_of(s2) == expected
        finally:
            s2.shutdown()

    def test_snapshot_round_trip(self, workdir):
        s = open_store()
        expected = build(s)
        assert s.save()
        s.shutdown()
        os.remove("test.aof")  # recover from the snapshot alone
        s2 = open_store()
        try:
            assert groups_of(s2) == expected
        finally:
            s2.shutdown()

    def test_an_emptied_stream_keeps_its_groups_through_compaction(self, workdir):
        s = open_store()
        s.xgroup_create("s", "g", "$", mkstream=True)
        s.xadd("s", ["k", "v"], "5-0")
        read(s, "c")
        s.xdel("s", "5-0")
        expected = groups_of(s)
        s.compact_aof()
        s.shutdown()
        s2 = open_store()
        try:
            assert groups_of(s2) == expected
            assert s2.xlen("s") == 0 and s2.xpending_summary("s", "g")[0] == 1
        finally:
            s2.shutdown()

    def test_an_empty_mkstream_stream_survives_a_restart(self, workdir):
        s = open_store()
        s.xgroup_create("s", "g", "$", mkstream=True)
        s.shutdown()
        s2 = open_store()
        try:
            assert s2.exists("s") == 1 and list(s2._data["s"].groups) == ["g"]
        finally:
            s2.shutdown()

    def test_aof_lines_are_plain_tokens(self, workdir):
        s = open_store()
        clock(s, 123)
        fill(s, n=1)
        s.xgroup_create("s", "g", "0")
        read(s, "c")
        s.shutdown()
        lines = open("test.aof").read().split("\n")
        assert "XGROUP s g 0-0" in lines
        assert "XGCONS s g c 123 -1" in lines
        assert "XGPEL s g c 1-0 123 1" in lines
        assert "XGLAST s g 1-0" in lines

    def test_replay_does_not_relog(self, workdir):
        s = open_store()
        build(s)
        s.shutdown()
        before = open("test.aof").read()
        open_store().shutdown()
        assert open("test.aof").read() == before

    def test_bad_records_are_skipped_good_ones_kept(self, workdir):
        with open("test.aof", "w") as f:
            f.write("SET str v\n")
            f.write("XADD s 1-0 a b\n")
            f.write("XGROUP str g 0-0\n")  # wrong type: skipped
            f.write("XGROUP s g zzz\n")  # bad id: skipped
            f.write("XGROUP s g\n")  # truncated: skipped
            f.write("XGPEL s nogroup c 1-0 5 1\n")  # unknown group: skipped
            f.write("XGPEL ghost g c 1-0 5 1\n")  # unknown key: skipped
            f.write("XGCONS s g c soon -1\n")  # skipped (and group doesn't exist yet)
            f.write("XGROUP s g 0-0\n")
            f.write("XGCONS s g c 10 -1\n")
            f.write("XGPEL s g c 1-0 20 1\n")
            f.write("XGPEL s g c 1-0 x 1\n")  # bad number: skipped
            f.write("XACK s g zzz\n")  # bad id: skipped
        s = open_store()
        try:
            assert s.get("str") == "v"
            assert s.exists("ghost") == 0
            (row,) = s.xpending_range("s", "g", "-", "+", 10)
            assert row[0] == "1-0" and row[1] == "c" and row[3] == 1
            assert [c[0] for c in s.xinfo_consumers("s", "g")] == ["c"]
        finally:
            s.shutdown()

    def test_a_replica_that_applies_the_records_converges(self, workdir, tmp_path, monkeypatch):
        master = open_store()
        master.drain_group_records()
        fill(master, n=5)
        master.drain_group_records()  # XADD is forwarded as XADD, not as a record
        shipped = []

        def ship():
            shipped.extend(master.drain_group_records())

        master.xgroup_create("s", "g", "0")
        ship()
        read(master, "a", count=3)
        ship()
        read(master, "b")
        ship()
        read(master, "a", sid="0")
        ship()
        master.xack("s", "g", "1-0", "4-0")
        ship()
        master.xgroup_delconsumer("s", "g", "b")
        ship()

        sub = tmp_path / "replica"
        sub.mkdir()
        monkeypatch.chdir(sub)
        replica = open_store("r.aof")
        try:
            for i in range(5):
                replica.xadd("s", ["n", str(i)], f"{i + 1}-0")
            for record in shipped:
                replica.apply_group_record(record)
            assert groups_of(replica) == groups_of(master)
        finally:
            replica.shutdown()
            master.shutdown()
