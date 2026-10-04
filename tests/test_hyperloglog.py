"""Tests for HyperLogLog: the data structure, the store layer, and the
command layer (PFADD / PFCOUNT / PFMERGE)."""

import os

import pytest

from kedis_python.commands import CommandHandler
from kedis_python.hyperloglog import M, HyperLogLog, _hash64
from kedis_python.store import KedisStore

WRONGTYPE = "WRONGTYPE Operation against a key holding the wrong kind of value"


def fill(hll, n, prefix="user", start=0):
    for i in range(start, start + n):
        hll.add(f"{prefix}{i}")
    return hll


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd: KedisStore reads/writes kedis.snapshot in the cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def store(workdir):
    s = KedisStore(aof_filename="test.aof", appendfsync="always")
    yield s
    s.shutdown()


@pytest.fixture
def handler(store):
    return CommandHandler(store)


def reopen(workdir):
    return KedisStore(aof_filename="test.aof", appendfsync="always")


# ----------------------------------------------------------------------
# HyperLogLog data structure
# ----------------------------------------------------------------------
class TestHyperLogLog:
    def test_empty_counts_zero(self):
        assert HyperLogLog().count() == 0

    @pytest.mark.parametrize("n", [1, 10, 100])
    def test_small_cardinalities_are_exact(self, n):
        # Linear counting makes the low range essentially exact.
        assert fill(HyperLogLog(), n).count() == n

    @pytest.mark.parametrize("n", [1_000, 10_000, 100_000])
    def test_accuracy_within_three_percent(self, n):
        # Standard error is ~0.81% at p=14; 3% is ~3.7 sigma.
        est = fill(HyperLogLog(), n).count()
        assert abs(est - n) / n < 0.03

    def test_add_returns_true_only_when_register_changes(self):
        h = HyperLogLog()
        assert h.add("x") is True
        assert h.add("x") is False

    def test_duplicates_do_not_change_count(self):
        h = fill(HyperLogLog(), 500)
        before = h.count()
        fill(h, 500)  # same 500 elements again
        assert h.count() == before

    def test_str_and_bytes_hash_identically(self):
        assert _hash64("hello") == _hash64(b"hello")

    def test_hash_is_stable_across_processes(self):
        # Guards against ever switching to Python's randomized hash().
        # A snapshot written by one process must mean the same thing in another.
        assert _hash64("hello") == 12085107955937391741
        assert _hash64("user1") == 14914577609760747527

    def test_count_cache_invalidates_after_add(self):
        h = fill(HyperLogLog(), 100)
        first = h.count()
        fill(h, 100, start=100)
        assert h.count() > first

    def test_merge_is_union(self):
        a = fill(HyperLogLog(), 1000)
        b = fill(HyperLogLog(), 1000, start=500)
        a.merge(b)
        assert abs(a.count() - 1500) / 1500 < 0.03

    def test_merge_reports_change_and_is_idempotent(self):
        a = fill(HyperLogLog(), 100)
        b = fill(HyperLogLog(), 100, start=100)
        assert a.merge(b) is True
        assert a.merge(b) is False

    def test_merge_is_commutative(self):
        a = fill(HyperLogLog(), 300)
        b = fill(HyperLogLog(), 300, start=150)
        ab, ba = a.copy(), b.copy()
        ab.merge(b)
        ba.merge(a)
        assert ab.registers == ba.registers

    def test_merge_does_not_modify_other(self):
        a = fill(HyperLogLog(), 100)
        b = fill(HyperLogLog(), 100, start=100)
        snapshot = bytes(b.registers)
        a.merge(b)
        assert bytes(b.registers) == snapshot

    def test_copy_is_independent(self):
        a = fill(HyperLogLog(), 100)
        c = a.copy()
        fill(c, 100, start=100)
        assert a.count() == 100
        assert c.count() > 100

    def test_bytes_round_trip(self):
        a = fill(HyperLogLog(), 5000)
        b = HyperLogLog.from_bytes(a.to_bytes())
        assert b.registers == a.registers
        assert b.count() == a.count()

    def test_from_bytes_rejects_wrong_length(self):
        with pytest.raises(ValueError):
            HyperLogLog.from_bytes(b"\x00" * (M - 1))


# ----------------------------------------------------------------------
# Store layer
# ----------------------------------------------------------------------
class TestStorePFADD:
    def test_new_key_returns_one(self, store):
        assert store.pfadd("k", "a", "b") == 1

    def test_repeat_returns_zero(self, store):
        store.pfadd("k", "a", "b")
        assert store.pfadd("k", "a", "b") == 0

    def test_new_element_returns_one(self, store):
        store.pfadd("k", *[f"u{i}" for i in range(100)])
        assert store.pfadd("k", "brand-new-element") == 1

    def test_no_elements_creates_empty_key(self, store):
        assert store.pfadd("k") == 1
        assert store.exists("k") == 1
        assert store.pfcount("k") == 0
        assert store.pfadd("k") == 0  # already exists

    def test_wrongtype_on_string_key(self, store):
        store.set("s", "hello")
        with pytest.raises(TypeError, match="WRONGTYPE"):
            store.pfadd("s", "x")

    def test_version_bumps_only_on_real_change(self, store):
        store.pfadd("k", "a")
        v = store._versions.get("k")
        store.pfadd("k", "a")  # no-op
        assert store._versions.get("k") == v
        store.pfadd("k", *[f"n{i}" for i in range(50)])
        assert store._versions.get("k") > v


class TestStorePFCOUNT:
    def test_missing_key_is_zero(self, store):
        assert store.pfcount("nope") == 0

    def test_single_key(self, store):
        store.pfadd("k", *[f"u{i}" for i in range(100)])
        assert store.pfcount("k") == 100

    def test_multi_key_is_union(self, store):
        store.pfadd("a", *[f"u{i}" for i in range(1000)])
        store.pfadd("b", *[f"u{i}" for i in range(500, 1500)])
        assert abs(store.pfcount("a", "b") - 1500) / 1500 < 0.03

    def test_missing_keys_ignored_in_union(self, store):
        store.pfadd("a", "x", "y", "z")
        assert store.pfcount("a", "nope") == 3
        assert store.pfcount("nope", "nada") == 0

    def test_multi_key_does_not_mutate_or_create(self, store):
        store.pfadd("a", *[f"u{i}" for i in range(100)])
        store.pfadd("b", *[f"u{i}" for i in range(100, 200)])
        a_before = bytes(store._data["a"].registers)
        store.pfcount("a", "b")
        assert bytes(store._data["a"].registers) == a_before
        assert store.pfcount("a") == 100
        assert store.exists("a b") == 0

    def test_wrongtype_among_valid_keys(self, store):
        store.pfadd("a", "x")
        store.set("s", "hello")
        with pytest.raises(TypeError, match="WRONGTYPE"):
            store.pfcount("a", "s")


class TestStorePFMERGE:
    def test_merge_into_new_dest(self, store):
        store.pfadd("a", *[f"u{i}" for i in range(1000)])
        store.pfadd("b", *[f"u{i}" for i in range(500, 1500)])
        store.pfmerge("c", "a", "b")
        assert abs(store.pfcount("c") - 1500) / 1500 < 0.03

    def test_merge_keeps_existing_dest_data(self, store):
        store.pfadd("dest", *[f"d{i}" for i in range(100)])
        store.pfadd("src", *[f"s{i}" for i in range(100)])
        store.pfmerge("dest", "src")
        assert abs(store.pfcount("dest") - 200) / 200 < 0.03

    def test_sources_are_untouched(self, store):
        store.pfadd("a", *[f"u{i}" for i in range(100)])
        store.pfadd("b", *[f"u{i}" for i in range(100, 200)])
        a_before = bytes(store._data["a"].registers)
        store.pfmerge("c", "a", "b")
        assert bytes(store._data["a"].registers) == a_before

    def test_no_sources_creates_empty_dest(self, store):
        store.pfmerge("solo")
        assert store.pfcount("solo") == 0
        assert store.exists("solo") == 1

    def test_missing_sources_are_skipped(self, store):
        store.pfadd("a", "x", "y")
        store.pfmerge("c", "a", "ghost")
        assert store.pfcount("c") == 2

    def test_wrongtype_source_is_atomic(self, store):
        store.pfadd("a", "x")
        store.set("s", "hello")
        with pytest.raises(TypeError, match="WRONGTYPE"):
            store.pfmerge("dest", "a", "s")
        assert store.exists("dest") == 0  # validated before mutating

    def test_wrongtype_dest(self, store):
        store.set("s", "hello")
        store.pfadd("a", "x")
        with pytest.raises(TypeError, match="WRONGTYPE"):
            store.pfmerge("s", "a")


class TestCrossTypeBehaviour:
    def test_get_on_hll_is_wrongtype(self, store):
        store.pfadd("k", "x")
        with pytest.raises(TypeError, match="WRONGTYPE"):
            store.get("k")

    def test_setbit_on_hll_is_wrongtype(self, store):
        store.pfadd("k", "x")
        with pytest.raises(TypeError):
            store.setbit("k", 1, 1)

    def test_set_overwrites_hll(self, store):
        store.pfadd("k", "x")
        store.set("k", "plain")
        assert store.get("k") == "plain"

    def test_type_keys_and_stats(self, store):
        store.pfadd("h", "a", "b")
        store.set("s", "v")
        assert store.type_of("h") == "string"
        info = store.keys()["h"]
        assert info["type"] == "string"
        assert info["length"] == str(M)
        stats = store.get_engine_stats()
        assert stats["hll_keys"] == 1
        assert stats["string_chars"] == 1  # only "v"; HLL not counted as chars


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------
class TestPersistence:
    def _seed(self, store):
        store.pfadd("a", *[f"u{i}" for i in range(1000)])
        store.pfadd("b", *[f"u{i}" for i in range(500, 1500)])
        store.pfmerge("c", "a", "b")
        store.pfadd("empty")
        return {k: store.pfcount(k) for k in ("a", "b", "c", "empty")}

    def _counts(self, s):
        return {k: s.pfcount(k) for k in ("a", "b", "c", "empty")}

    def test_aof_replay(self, workdir):
        s = reopen(workdir)
        expected = self._seed(s)
        s.shutdown()
        s2 = reopen(workdir)
        try:
            assert self._counts(s2) == expected
        finally:
            s2.shutdown()

    def test_snapshot_round_trip(self, workdir):
        s = reopen(workdir)
        expected = self._seed(s)
        assert s.save()
        s.shutdown()
        os.remove(workdir / "test.aof")  # force recovery from snapshot alone
        s2 = reopen(workdir)
        try:
            assert self._counts(s2) == expected
        finally:
            s2.shutdown()

    def test_snapshot_preserves_registers_exactly(self, workdir):
        s = reopen(workdir)
        self._seed(s)
        before = bytes(s._data["a"].registers)
        s.save()
        s.shutdown()
        os.remove(workdir / "test.aof")
        s2 = reopen(workdir)
        try:
            assert bytes(s2._data["a"].registers) == before
        finally:
            s2.shutdown()

    def test_compaction_uses_pfload_and_recovers(self, workdir):
        s = reopen(workdir)
        expected = self._seed(s)
        assert s.compact_aof()
        s.shutdown()
        with open(workdir / "test.aof") as f:
            assert "PFLOAD a " in f.read()
        s2 = reopen(workdir)  # no snapshot exists: PFLOAD path only
        try:
            assert self._counts(s2) == expected
        finally:
            s2.shutdown()

    def test_writes_after_compaction_still_replay(self, workdir):
        s = reopen(workdir)
        s.pfadd("a", *[f"u{i}" for i in range(100)])
        s.compact_aof()
        s.pfadd("a", *[f"v{i}" for i in range(100)])
        expected = s.pfcount("a")
        s.shutdown()
        s2 = reopen(workdir)
        try:
            assert s2.pfcount("a") == expected
        finally:
            s2.shutdown()


# ----------------------------------------------------------------------
# Command layer
# ----------------------------------------------------------------------
class TestCommands:
    def test_registered_in_dispatch_table(self, handler):
        for cmd in ("PFADD", "PFCOUNT", "PFMERGE"):
            assert cmd in handler._commands

    def test_write_commands_set(self, handler):
        assert {"PFADD", "PFMERGE"} <= handler.WRITE_COMMANDS
        assert "PFCOUNT" not in handler.WRITE_COMMANDS

    def test_basic_flow(self, handler):
        x = handler.execute
        assert x(["PFADD", "visitors", "alice", "bob", "carol"]) == 1
        assert x(["PFCOUNT", "visitors"]) == 3
        assert x(["PFADD", "other", "bob", "dave"]) == 1
        assert x(["PFCOUNT", "visitors", "other"]) == 4
        assert x(["PFMERGE", "all", "visitors", "other"]) == "OK"
        assert x(["PFCOUNT", "all"]) == 4

    def test_case_insensitive_command_name(self, handler):
        assert handler.execute(["pfadd", "k", "a"]) == 1
        assert handler.execute(["pfcount", "k"]) == 1

    def test_pfadd_repeat_returns_zero(self, handler):
        handler.execute(["PFADD", "k", "a"])
        assert handler.execute(["PFADD", "k", "a"]) == 0

    @pytest.mark.parametrize("cmd", ["PFADD", "PFCOUNT", "PFMERGE"])
    def test_arity_errors(self, handler, cmd):
        reply = handler.execute([cmd])
        assert reply.startswith("-ERR wrong number of arguments")
        assert cmd.lower() in reply

    def test_pfmerge_with_no_sources_is_ok(self, handler):
        assert handler.execute(["PFMERGE", "solo"]) == "OK"
        assert handler.execute(["PFCOUNT", "solo"]) == 0

    def test_wrongtype_replies(self, handler):
        x = handler.execute
        x(["SET", "s", "v"])
        x(["PFADD", "h", "a"])
        expected = f"-ERR {WRONGTYPE}"
        assert x(["PFADD", "s", "z"]) == expected
        assert x(["PFCOUNT", "s"]) == expected
        assert x(["PFMERGE", "s", "h"]) == expected
        assert x(["PFMERGE", "h", "s"]) == expected
        assert x(["GET", "h"]) == expected
        assert x(["SETBIT", "h", "1", "1"]) == expected

    def test_type_reports_string(self, handler):
        handler.execute(["PFADD", "k", "a"])
        assert handler.execute(["TYPE", "k"]) == "string"

    def test_del_and_exists_work_on_hll(self, handler):
        handler.execute(["PFADD", "k", "a"])
        assert handler.execute(["EXISTS", "k"]) == 1
        assert handler.execute(["DEL", "k"]) == 1
        assert handler.execute(["PFCOUNT", "k"]) == 0
