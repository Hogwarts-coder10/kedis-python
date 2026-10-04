"""AOF / snapshot persistence for bitmaps and for strings that cannot be
written as bare AOF tokens (whitespace, control chars, non-ASCII, empty)."""

import os

import pytest

from kedis_python.store import KedisStore

TRICKY_BYTES = [0, 9, 10, 13, 32, 127, 128, 160, 200, 255]  # incl. whitespace


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd: KedisStore reads/writes kedis.snapshot in the cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def open_store(aof="test.aof"):
    return KedisStore(aof_filename=aof, appendfsync="always")


def set_byte(store, key, byte_index, value):
    """Set every 1-bit of `value` at byte `byte_index` via SETBIT."""
    for bit in range(8):
        if value & (1 << (7 - bit)):
            store.setbit(key, byte_index * 8 + bit, 1)


def build_tricky_bitmap(store, key="bm"):
    for i, b in enumerate(TRICKY_BYTES):
        if b == 0:
            store.setbit(key, i * 8, 0)  # extends with a zero byte
        else:
            set_byte(store, key, i, b)
    return store.get(key)


def aof_lines(path="test.aof"):
    with open(path) as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


# ----------------------------------------------------------------------
# AOF replay of bitmap commands
# ----------------------------------------------------------------------
class TestReplay:
    def test_setbit_replays(self, workdir):
        s = open_store()
        s.setbit("b", 7, 1)
        s.setbit("b", 8, 1)
        s.setbit("b", 20, 1)
        s.setbit("b", 8, 0)
        live = s.get("b")
        s.shutdown()

        s2 = open_store()
        try:
            assert s2.get("b") == live
            assert s2.getbit("b", 7) == 1
            assert s2.getbit("b", 8) == 0
            assert s2.getbit("b", 20) == 1
            assert s2.bitcount("b") == 2
        finally:
            s2.shutdown()

    @pytest.mark.parametrize("op", ["AND", "OR", "XOR"])
    def test_bitop_binary_ops_replay(self, workdir, op):
        s = open_store()
        for off in (0, 3, 9, 17):
            s.setbit("a", off, 1)
        for off in (3, 4, 9, 30):
            s.setbit("b", off, 1)
        s.bitop(op, "dest", "a", "b")
        live = s.get("dest")
        s.shutdown()

        s2 = open_store()
        try:
            assert s2.get("dest") == live
        finally:
            s2.shutdown()

    def test_bitop_not_replays(self, workdir):
        s = open_store()
        s.setbit("a", 1, 1)
        s.setbit("a", 12, 1)
        s.bitop("NOT", "n", "a")
        live = s.get("n")
        s.shutdown()

        s2 = open_store()
        try:
            assert s2.get("n") == live
        finally:
            s2.shutdown()

    def test_tricky_bytes_survive_replay(self, workdir):
        s = open_store()
        live = build_tricky_bitmap(s)
        s.shutdown()
        s2 = open_store()
        try:
            assert s2.get("bm") == live
            assert [ord(c) for c in s2.get("bm")] == TRICKY_BYTES
        finally:
            s2.shutdown()

    def test_replay_does_not_relog_commands(self, workdir):
        s = open_store()
        s.setbit("b", 3, 1)
        s.shutdown()
        before = aof_lines()
        s2 = open_store()
        s2.shutdown()
        assert aof_lines() == before

    def test_bad_records_are_skipped_not_fatal(self, workdir):
        with open("test.aof", "w") as f:
            f.write("LPUSH l a b\n")
            f.write("SETBIT l 1 1\n")  # list key: WRONGTYPE, skipped
            f.write("SETBIT b -5 1\n")  # negative offset, skipped
            f.write("SETBIT b x 1\n")  # not an integer, skipped
            f.write("SETBIT b 2 7\n")  # bit not 0/1, skipped
            f.write("BITOP NAND d b\n")  # unknown op, skipped
            f.write("SETB64 broken !!!notbase64!!!\n")  # skipped
            f.write("SETBIT good 0 1\n")
        s = open_store()
        try:
            assert list(s._data["l"]) == ["b", "a"]  # LPUSH reverses
            assert s.getbit("good", 0) == 1
            assert "b" not in s._data
            assert "broken" not in s._data
        finally:
            s.shutdown()


# ----------------------------------------------------------------------
# Compaction
# ----------------------------------------------------------------------
class TestCompaction:
    def test_tricky_bitmap_round_trips_through_compaction(self, workdir):
        s = open_store()
        live = build_tricky_bitmap(s)
        assert s.compact_aof()
        s.shutdown()

        s2 = open_store()  # no snapshot: compacted AOF is the only source
        try:
            assert s2.get("bm") == live
        finally:
            s2.shutdown()

    def test_compacted_aof_has_no_raw_whitespace_in_bitmap_record(self, workdir):
        s = open_store()
        build_tricky_bitmap(s)
        s.compact_aof()
        s.shutdown()
        lines = aof_lines()
        assert len(lines) == 1  # one record, no stray newline splitting it
        assert lines[0].startswith("SETB64 bm ")
        assert len(lines[0].split()) == 3

    def test_plain_ascii_strings_stay_readable(self, workdir):
        s = open_store()
        s.set("plain", "hello")
        s.compact_aof()
        s.shutdown()
        assert "SET plain hello" in aof_lines()

    @pytest.mark.parametrize(
        "value",
        [
            "",
            " ",
            "two  spaces",
            "  leading",
            "trailing  ",
            "line1\nline2",
            "tab\there",
            "carriage\rreturn",
            "h\u00e9llo w\u00f6rld \u2713",
            "\xa0nbsp",
        ],
    )
    def test_awkward_strings_round_trip(self, workdir, value):
        s = open_store()
        s.set("k", value)
        s.compact_aof()
        s.shutdown()
        s2 = open_store()
        try:
            assert s2.get("k") == value
        finally:
            s2.shutdown()

    def test_ttl_survives_compaction_for_bitmap(self, workdir):
        s = open_store()
        s.setbit("bm", 9, 1)
        s.set_expire("bm", 1000)
        live = s.get("bm")
        s.compact_aof()
        s.shutdown()
        s2 = open_store()
        try:
            assert s2.get("bm") == live
            assert 0 < s2.ttl("bm") <= 1000
        finally:
            s2.shutdown()

    def test_writes_after_compaction_replay(self, workdir):
        s = open_store()
        s.setbit("bm", 9, 1)
        s.compact_aof()
        s.setbit("bm", 2, 1)
        s.bitop("NOT", "inv", "bm")
        live = (s.get("bm"), s.get("inv"))
        s.shutdown()
        s2 = open_store()
        try:
            assert (s2.get("bm"), s2.get("inv")) == live
        finally:
            s2.shutdown()

    def test_double_compaction_is_stable(self, workdir):
        s = open_store()
        live = build_tricky_bitmap(s)
        s.compact_aof()
        first = aof_lines()
        s.compact_aof()
        assert aof_lines() == first
        s.shutdown()
        s2 = open_store()
        try:
            assert s2.get("bm") == live
        finally:
            s2.shutdown()

    def test_absolute_aof_path_compacts(self, workdir):
        path = str(workdir / "abs.aof")
        s = KedisStore(aof_filename=path, appendfsync="always")
        s.setbit("bm", 9, 1)
        assert s.compact_aof() is True
        assert not os.path.exists(workdir / "temp_abs.aof")
        s.shutdown()
        s2 = KedisStore(aof_filename=path, appendfsync="always")
        try:
            assert s2.getbit("bm", 9) == 1
        finally:
            s2.shutdown()


# ----------------------------------------------------------------------
# Snapshot (JSON) path for the same binary-ish strings
# ----------------------------------------------------------------------
class TestSnapshot:
    def test_tricky_bitmap_survives_snapshot(self, workdir):
        s = open_store()
        live = build_tricky_bitmap(s)
        assert s.save()
        s.shutdown()
        os.remove("test.aof")  # force recovery from snapshot alone
        s2 = open_store()
        try:
            assert s2.get("bm") == live
        finally:
            s2.shutdown()
