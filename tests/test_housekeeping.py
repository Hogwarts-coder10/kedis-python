"""Periodic housekeeping: active expiry, LRU/expiry reconciliation, container
compaction, the BITOP ghost-slot fix, the HOUSEKEEP command and STATS."""

import sys
import time

import pytest

from kedis_python.commands import CommandHandler
from kedis_python.store import KedisStore


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd: KedisStore reads/writes kedis.snapshot in the cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def store(workdir):
    s = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    yield s
    s.shutdown()


@pytest.fixture
def handler(store):
    return CommandHandler(store)


def expire_now(store, key):
    """Force a key to be already expired without waiting on the clock."""
    store._expires[key] = time.time() - 1


# ----------------------------------------------------------------------
# Active expiry
# ----------------------------------------------------------------------
class TestActiveExpiry:
    def test_untouched_expired_keys_are_swept(self, store):
        for k in ("a", "b", "c"):
            store.set(k, "v")
        store.set("keep", "v")
        for k in ("a", "b", "c"):
            expire_now(store, k)
        report = store.housekeeping()
        assert report["expired"] == 3
        assert set(store._data) == {"keep"}
        assert set(store._lru_tracker) == {"keep"}  # slots freed too

    def test_sweep_logs_del_so_replay_agrees(self, workdir):
        s = KedisStore(aof_filename="test.aof", appendfsync="always")
        s.set("gone", "v")
        s.set("stay", "v")
        expire_now(s, "gone")
        s.housekeeping()
        s.shutdown()
        s2 = KedisStore(aof_filename="test.aof", appendfsync="always")
        try:
            assert s2.get("gone") is None
            assert s2.get("stay") == "v"
        finally:
            s2.shutdown()

    def test_live_keys_with_future_ttl_are_kept(self, store):
        store.set("k", "v")
        store.set_expire("k", 1000)
        assert store.housekeeping()["expired"] == 0
        assert store.get("k") == "v"


# ----------------------------------------------------------------------
# Reconciliation
# ----------------------------------------------------------------------
class TestReconcile:
    def test_ghost_tracker_entries_are_dropped(self, store):
        store.set("real", "v")
        store._lru_tracker["ghost1"] = None
        store._lru_tracker["ghost2"] = None
        report = store.housekeeping()
        assert report["lru_orphans"] == 2
        assert set(store._lru_tracker) == {"real"}

    def test_stale_expiry_entries_are_dropped(self, store):
        store.set("real", "v")
        store._expires["ghost"] = time.time() + 100
        report = store.housekeeping()
        assert report["expire_orphans"] == 1
        assert "ghost" not in store._expires

    def test_stale_expiry_no_longer_crashes_passive_eviction(self, store):
        store._expires["ghost"] = time.time() - 1  # expired, but no data
        store.housekeeping()
        assert store.get("ghost") is None  # would KeyError without cleanup

    def test_untracked_data_is_adopted_at_the_cold_end(self, store):
        store.set("a", "1")
        store.set("b", "2")
        store.set("lost", "3")
        del store._lru_tracker["lost"]
        report = store.housekeeping()
        assert report["lru_adopted"] == 1
        assert next(iter(store._lru_tracker)) == "lost"  # coldest = first out
        assert store._data["lost"] == "3"  # adoption never deletes data

    def test_live_data_is_never_touched(self, store):
        store.set("s", "v")
        store.lpush("l", "a")
        store.sadd("m", "x")
        store.hset("h", "f", "v")
        store.pfadd("p", "e")
        before = {k: repr(v) for k, v in store._data.items() if k != "p"}
        store._lru_tracker["ghost"] = None
        store.housekeeping()
        after = {k: repr(v) for k, v in store._data.items() if k != "p"}
        assert before == after
        assert store.pfcount("p") == 1

    def test_second_pass_finds_nothing(self, store):
        store.set("k", "v")
        store._lru_tracker["ghost"] = None
        store.housekeeping()
        again = store.housekeeping()
        assert (again["expired"], again["lru_orphans"], again["lru_adopted"]) == (0, 0, 0)
        assert again["expire_orphans"] == 0 and again["reclaimed_bytes"] == 0


class TestBitopGhostFix:
    def test_empty_result_leaves_no_ghost_slot(self, store):
        store.setbit("a", 3, 1)
        assert store.bitop("AND", "dest", "missing1", "missing2") == 0
        assert "dest" not in store._lru_tracker
        assert set(store._lru_tracker) == set(store._data)

    def test_empty_result_deleting_existing_dest_leaves_no_ghost(self, store):
        store.set("dest", "x")
        assert store.bitop("OR", "dest", "missing") == 0
        assert "dest" not in store._data
        assert "dest" not in store._lru_tracker

    def test_empty_result_still_bumps_the_watch_version(self, store):
        store.set("dest", "x")
        before = store._versions.get("dest", 0)
        store.bitop("OR", "dest", "missing")
        assert store._versions["dest"] > before

    def test_non_empty_result_is_tracked_as_before(self, store):
        store.setbit("a", 0, 1)
        store.bitop("OR", "dest", "a")
        assert "dest" in store._lru_tracker and "dest" in store._data

    def test_ghosts_no_longer_cost_live_keys_their_slot(self, workdir):
        s = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=5)
        try:
            for i in range(3):
                s.set(f"k{i}", "v")
            for i in range(10):  # each empty BITOP used to leak one slot
                s.bitop("AND", f"d{i}", "nope")
            assert all(s.get(f"k{i}") == "v" for i in range(3))
        finally:
            s.shutdown()


# ----------------------------------------------------------------------
# Compaction
# ----------------------------------------------------------------------
class TestCompaction:
    def _bloat(self, store, n=20_000, keep=10):
        # Touch the containers directly: going through set()/delete() would
        # fsync the AOF 40k times, and this test is about dict sizes, not I/O.
        for i in range(n):
            store._data[f"key{i}"] = "v"
            store._lru_tracker[f"key{i}"] = None
        for i in range(keep, n):
            del store._data[f"key{i}"]
            del store._lru_tracker[f"key{i}"]

    def test_sparse_containers_are_rebuilt(self, store):
        self._bloat(store)
        data_before = sys.getsizeof(store._data)
        tracker_before = sys.getsizeof(store._lru_tracker)
        report = store.housekeeping()
        assert report["reclaimed_bytes"] > 100_000
        assert sys.getsizeof(store._data) < data_before
        assert sys.getsizeof(store._lru_tracker) < tracker_before

    def test_contents_and_lru_order_survive_compaction(self, store):
        self._bloat(store, n=5_000, keep=10)
        store.get("key3")  # bump to MRU
        order_before = list(store._lru_tracker)
        data_before = dict(store._data)
        store.housekeeping()
        assert list(store._lru_tracker) == order_before
        assert dict(store._data) == data_before

    def test_store_keeps_working_after_compaction(self, store):
        self._bloat(store, n=5_000)
        store.housekeeping()
        store.set("fresh", "v")
        assert store.get("fresh") == "v"
        assert store.get("key0") == "v"
        store.delete("key0")
        assert store.get("key0") is None

    def test_small_stores_are_left_alone(self, store):
        for i in range(20):
            store.set(f"k{i}", "v")
        assert store.housekeeping()["reclaimed_bytes"] == 0

    def test_dense_containers_are_left_alone(self, store):
        for i in range(5_000):
            store.set(f"k{i}", "v")  # nothing deleted: already compact
        assert store.housekeeping()["reclaimed_bytes"] == 0


# ----------------------------------------------------------------------
# Telemetry, command, and locking
# ----------------------------------------------------------------------
class TestTelemetry:
    def test_totals_accumulate_across_runs(self, store):
        store.set("a", "v")
        expire_now(store, "a")
        store.housekeeping()
        store._lru_tracker["ghost"] = None
        store.housekeeping()
        t = store.housekeeping_stats()
        assert t["runs"] == 2 and t["expired"] == 1 and t["lru_orphans"] == 1
        assert t["last_run"] > 0 and t["last_duration_ms"] >= 0

    def test_engine_stats_carries_housekeeping(self, store):
        store.housekeeping()
        assert store.get_engine_stats()["housekeeping"]["runs"] == 1


class TestCommand:
    def test_housekeep_reports_every_field(self, handler):
        reply = handler.execute(["HOUSEKEEP"])
        fields = dict(line.split(":", 1) for line in reply.split("\n"))
        assert set(fields) == {
            "expired",
            "expire_orphans",
            "lru_orphans",
            "lru_adopted",
            "reclaimed_bytes",
            "duration_ms",
            "keys",
        }

    def test_housekeep_sweeps_through_the_command(self, handler, store):
        handler.execute(["SET", "a", "v"])
        expire_now(store, "a")
        reply = handler.execute(["HOUSEKEEP"])
        assert "expired:1" in reply.split("\n")

    def test_housekeep_arity(self, handler):
        assert handler.execute(["HOUSEKEEP", "now"]).startswith(
            "-ERR wrong number of arguments"
        )

    def test_housekeep_is_a_local_maintenance_command(self, handler):
        assert "HOUSEKEEP" in handler._commands
        assert "HOUSEKEEP" not in handler.WRITE_COMMANDS  # never replicated

    def test_stats_shows_housekeeping_lines(self, handler):
        handler.execute(["HOUSEKEEP"])
        stats = handler.execute(["STATS"])
        assert "Housekeeping Runs:1" in stats
        assert "Bytes Reclaimed:" in stats

    def test_run_housekeeping_holds_the_engine_lock(self, handler, store, monkeypatch):
        seen = []
        real = store.housekeeping

        def spy():
            seen.append(handler._engine_lock.locked())
            return real()

        monkeypatch.setattr(store, "housekeeping", spy)
        handler.run_housekeeping()
        assert seen == [True]
        assert not handler._engine_lock.locked()  # released afterwards
