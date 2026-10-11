"""Consumer group state inside a Stream (no store, no commands): the resolved
records, how they apply, and how group state survives to_state/from_state."""

import json

import pytest

from kedis_python.stream import (
    GROUP_RECORD_KINDS,
    Stream,
    StreamError,
    check_name,
    parse_id,
)


def make(n=5):
    s = Stream()
    for i in range(n):
        s.add(["k", f"v{i}"], f"{i + 1}-0")
    return s


def apply(s, kind, *args):
    return s.apply_group_record(kind, [str(a) for a in args])


def group_state(s):
    return s.to_state().get("groups")


class TestNames:
    @pytest.mark.parametrize("name", ["g", "billing-1", "a.b_c", "x" * 200])
    def test_valid(self, name):
        assert check_name(name) == name

    @pytest.mark.parametrize("name", ["", "has space", "tab\t", "new\nline", "héllo", "✓"])
    def test_invalid(self, name):
        with pytest.raises(StreamError, match="Invalid group or consumer name"):
            check_name(name)


class TestGroupRecords:
    def test_xgroup_creates_then_moves_the_cursor(self):
        s = make()
        assert apply(s, "XGROUP", "g", "0-0") == 1
        assert s.groups["g"].last_id == (0, 0)
        apply(s, "XGROUP", "g", "3-0")  # same record, group exists: cursor moves
        assert s.groups["g"].last_id == (3, 0)
        assert len(s.groups) == 1

    def test_xglast_moves_the_cursor(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        apply(s, "XGLAST", "g", "4-0")
        assert s.groups["g"].last_id == (4, 0)

    def test_xgdel(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        assert apply(s, "XGDEL", "g") == 1
        assert apply(s, "XGDEL", "g") == 0
        assert s.groups == {}

    def test_xgcons_is_idempotent_and_never_moves_times_backwards(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        assert apply(s, "XGCONS", "g", "c", 100, -1) == 1
        assert apply(s, "XGCONS", "g", "c", 50, 80) == 0  # exists: no new consumer
        c = s.groups["g"].consumers["c"]
        assert (c.seen_ms, c.active_ms) == (100, 80)

    def test_xgpel_sets_an_entry_and_stamps_the_consumer(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        apply(s, "XGCONS", "g", "c", 10, -1)
        apply(s, "XGPEL", "g", "c", "2-0", 500, 3)
        g = s.groups["g"]
        p = g.pel[(2, 0)]
        assert (p.consumer, p.delivery_ms, p.count) == ("c", 500, 3)
        assert (g.consumers["c"].seen_ms, g.consumers["c"].active_ms) == (500, 500)

    def test_xgpel_tolerates_a_missing_consumer_record(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        apply(s, "XGPEL", "g", "ghost", "1-0", 700, 1)
        assert s.groups["g"].consumers["ghost"].active_ms == 700

    def test_pel_ids_stay_sorted_whatever_the_insert_order(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        for sid in ("4-0", "1-0", "3-0", "2-0"):
            apply(s, "XGPEL", "g", "c", sid, 1, 1)
        g = s.groups["g"]
        assert g._pel_ids == [(1, 0), (2, 0), (3, 0), (4, 0)]
        assert set(g.pel) == set(g._pel_ids)

    def test_redelivering_an_entry_moves_ownership_without_duplicating(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        apply(s, "XGPEL", "g", "a", "1-0", 1, 1)
        apply(s, "XGPEL", "g", "b", "1-0", 9, 2)
        g = s.groups["g"]
        assert g._pel_ids == [(1, 0)]
        assert (g.pel[(1, 0)].consumer, g.pel[(1, 0)].count) == ("b", 2)

    def test_xack_removes_only_what_is_pending(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        for sid in ("1-0", "2-0", "3-0"):
            apply(s, "XGPEL", "g", "c", sid, 1, 1)
        assert apply(s, "XACK", "g", "1-0", "3-0", "9-9") == 2
        assert apply(s, "XACK", "g", "3-0") == 0
        assert s.groups["g"]._pel_ids == [(2, 0)]

    def test_xack_with_a_bad_id_changes_nothing(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        apply(s, "XGPEL", "g", "c", "1-0", 1, 1)
        with pytest.raises(StreamError, match="Invalid stream ID"):
            apply(s, "XACK", "g", "1-0", "zzz")
        assert (1, 0) in s.groups["g"].pel

    def test_xgcdel_drops_the_consumer_and_only_its_entries(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        for sid, who in (("1-0", "a"), ("2-0", "b"), ("3-0", "a"), ("4-0", "b")):
            apply(s, "XGPEL", "g", who, sid, 1, 1)
        assert apply(s, "XGCDEL", "g", "a") == 2
        g = s.groups["g"]
        assert "a" not in g.consumers and "b" in g.consumers
        assert g._pel_ids == [(2, 0), (4, 0)] and set(g.pel) == set(g._pel_ids)
        assert apply(s, "XGCDEL", "g", "a") == 0

    def test_deleting_a_stream_entry_leaves_it_pending(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        apply(s, "XGPEL", "g", "c", "2-0", 1, 1)
        s.delete((2, 0))
        assert (2, 0) in s.groups["g"].pel
        assert s.entry_fields((2, 0)) is None


class TestBadRecords:
    @pytest.mark.parametrize(
        "kind,args",
        [
            ("XGROUP", ["g"]),
            ("XGROUP", ["g", "0-0", "extra"]),
            ("XGDEL", []),
            ("XGCONS", ["g", "c", "1"]),
            ("XGPEL", ["g", "c", "1-0", "1"]),
            ("XGLAST", ["g"]),
            ("XACK", ["g"]),
            ("NOPE", ["g", "0-0"]),
        ],
    )
    def test_wrong_shape(self, kind, args):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        before = group_state(s)
        with pytest.raises(StreamError, match="Invalid consumer group record"):
            s.apply_group_record(kind, args)
        assert group_state(s) == before

    @pytest.mark.parametrize(
        "kind,args",
        [
            ("XGLAST", ["missing", "1-0"]),
            ("XGCONS", ["missing", "c", "1", "1"]),
            ("XGPEL", ["missing", "c", "1-0", "1", "1"]),
            ("XACK", ["missing", "1-0"]),
        ],
    )
    def test_unknown_group_is_rejected(self, kind, args):
        with pytest.raises(StreamError, match="Invalid consumer group record"):
            make().apply_group_record(kind, args)

    def test_bad_numbers_and_ids(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        for kind, args in [
            ("XGCONS", ["g", "c", "soon", "-1"]),
            ("XGPEL", ["g", "c", "1-0", "x", "1"]),
            ("XGPEL", ["g", "c", "1-0", "1", "x"]),
            ("XGPEL", ["g", "c", "zzz", "1", "1"]),
            ("XGLAST", ["g", "zzz"]),
            ("XGROUP", ["g2", "zzz"]),
        ]:
            with pytest.raises(StreamError):
                s.apply_group_record(kind, args)
        assert list(s.groups) == ["g"]
        assert s.groups["g"].pel == {} and s.groups["g"].consumers == {}

    def test_bad_names_are_rejected_on_creation(self):
        s = make()
        with pytest.raises(StreamError, match="Invalid group or consumer name"):
            apply(s, "XGROUP", "bad\u00e9", "0-0")
        apply(s, "XGROUP", "g", "0-0")
        with pytest.raises(StreamError, match="Invalid group or consumer name"):
            apply(s, "XGCONS", "g", "bad\u00e9", 1, -1)
        assert s.groups["g"].consumers == {}

    def test_every_kind_is_advertised(self):
        assert GROUP_RECORD_KINDS == {
            "XGROUP", "XGDEL", "XGCONS", "XGCDEL", "XGPEL", "XGLAST", "XACK",
        }  # fmt: skip


class TestPendingQueries:
    def _grp(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        for sid, who in (("1-0", "a"), ("2-0", "b"), ("3-0", "a"), ("5-0", "a")):
            apply(s, "XGPEL", "g", who, sid, 1, 1)
        return s.groups["g"]

    def test_pending_after_is_strict_ordered_and_owner_filtered(self):
        g = self._grp()
        assert g.pending_after("a", (0, 0)) == [(1, 0), (3, 0), (5, 0)]
        assert g.pending_after("a", (1, 0)) == [(3, 0), (5, 0)]
        assert g.pending_after("b", (2, 0)) == []
        assert g.pending_after("nobody", (0, 0)) == []

    def test_pending_after_count(self):
        g = self._grp()
        assert g.pending_after("a", (0, 0), count=2) == [(1, 0), (3, 0)]
        assert g.pending_after("a", (0, 0), count=0) == []

    def test_pending_ids_range_is_inclusive(self):
        g = self._grp()
        assert g.pending_ids((2, 0), (5, 0)) == [(2, 0), (3, 0), (5, 0)]
        assert g.pending_ids((4, 0), (4, 9)) == []

    def test_pending_counts(self):
        assert self._grp().pending_counts() == {"a": 3, "b": 1}


class TestEntryFields:
    def test_returns_a_copy_or_none(self):
        s = make(2)
        fields = s.entry_fields((1, 0))
        assert fields == ["k", "v0"]
        fields[1] = "mutated"
        assert s.entry_fields((1, 0)) == ["k", "v0"]
        assert s.entry_fields((9, 9)) is None


class TestStateRoundTrip:
    def _busy(self):
        s = make()
        apply(s, "XGROUP", "g", "0-0")
        apply(s, "XGROUP", "other", "2-0")
        apply(s, "XGCONS", "g", "idle-one", 111, -1)
        for sid, who, ms, n in (("3-0", "a", 300, 2), ("1-0", "a", 100, 1), ("2-0", "b", 200, 1)):
            apply(s, "XGPEL", "g", who, sid, ms, n)
        apply(s, "XGLAST", "g", "3-0")
        return s

    def test_round_trip_is_exact(self):
        s = self._busy()
        t = Stream.from_state(s.to_state())
        assert group_state(t) == group_state(s)
        assert t.groups["g"]._pel_ids == [(1, 0), (2, 0), (3, 0)]
        assert t.groups["g"].last_id == (3, 0)
        assert t.groups["g"].consumers["idle-one"].active_ms == -1

    def test_state_is_json_safe(self):
        s = self._busy()
        t = Stream.from_state(json.loads(json.dumps(s.to_state())))
        assert group_state(t) == group_state(s)

    def test_a_stream_without_groups_keeps_the_original_shape(self):
        assert "groups" not in make().to_state()

    def test_state_without_a_groups_key_still_loads(self):
        state = make(3).to_state()
        assert Stream.from_state(state).groups == {}

    def test_restored_pel_still_accepts_changes(self):
        t = Stream.from_state(self._busy().to_state())
        assert apply(t, "XACK", "g", "2-0") == 1
        assert apply(t, "XGCDEL", "g", "a") == 2
        assert t.groups["g"].pel == {}
        assert parse_id("3-0") == t.groups["g"].last_id
