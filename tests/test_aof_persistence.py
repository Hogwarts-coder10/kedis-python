"""Values survive a restart exactly, and a damaged AOF is repaired, not trusted."""

import os

import pytest

from kedis_python import aof
from kedis_python.store import AOF_CHUNK, KedisStore


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd: KedisStore reads/writes kedis.snapshot in the cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def open_store():
    return KedisStore(aof_filename="test.aof", appendfsync="always")


def restart(store):
    store.shutdown()
    return open_store()


TRICKY = [
    "",
    " ",
    "two  spaces",
    "  leading",
    "trailing  ",
    "x\ty",
    "line1\nline2",
    "a\r\nb",
    "\nFLUSHALL\n",
    "SET victim planted",
    "A3\nS3\nSET",
    "h\u00e9llo w\u00f6rld \u2713",
    "\xa0nbsp",
]


def ids(values):
    return [repr(v)[:24] for v in values]


# --- every value survives a restart -----------------------------------------------------------


@pytest.mark.parametrize("value", TRICKY, ids=ids(TRICKY))
def test_string_values_survive_a_restart(workdir, value):
    s = open_store()
    s.set("k", value)
    s = restart(s)
    assert s.get("k") == value
    s.shutdown()


@pytest.mark.parametrize("text", TRICKY, ids=ids(TRICKY))
def test_collection_members_survive_a_restart(workdir, text):
    s = open_store()
    s.rpush("l", "first", text, "last")
    s.lpush("l", text)
    s.sadd("s", text, "other")
    s.hset("h", text or "empty-field", text)
    s.zadd("z", 1.5, text)
    s = restart(s)
    assert list(s.lrange("l", 0, -1)) == [text, "first", text, "last"]
    assert sorted(s.smembers("s")) == sorted([text, "other"])
    assert s.hget("h", text or "empty-field") == text
    assert s.zrange("z", 0, -1) == [text]
    s.shutdown()


def test_a_value_cannot_plant_commands_in_the_log(workdir):
    s = open_store()
    s.set("a", "1")
    s.set("evil", "v\nFLUSHALL\nSET planted yes\nDEL a")
    s.set("keep", "important")
    s = restart(s)
    assert s.get("a") == "1"
    assert s.get("evil") == "v\nFLUSHALL\nSET planted yes\nDEL a"
    assert s.get("keep") == "important"
    assert s.get("planted") is None
    s.shutdown()


def test_writes_are_one_framed_record_each(workdir):
    s = open_store()
    s.set("k", "a b\nc")
    s.shutdown()
    with open("test.aof", "rb") as f:
        assert f.read() == b"A3\nS3\nSET\nS1\nk\nS5\na b\nc\n"


# --- compaction -----------------------------------------------------------------------------------


def test_compaction_keeps_every_value_exactly(workdir):
    s = open_store()
    for i, v in enumerate(TRICKY):
        s.set(f"s{i}", v)
        s.rpush(f"l{i}", v, "x y", v)
        s.sadd(f"set{i}", v, "x y")
        s.hset(f"h{i}", "f g", v)
        s.zadd(f"z{i}", float(i), v)
    assert s.compact_aof()
    s = restart(s)
    for i, v in enumerate(TRICKY):
        assert s.get(f"s{i}") == v
        assert list(s.lrange(f"l{i}", 0, -1)) == [v, "x y", v]
        assert sorted(s.smembers(f"set{i}")) == sorted([v, "x y"])
        assert s.hget(f"h{i}", "f g") == v
        assert s.zrange(f"z{i}", 0, -1) == [v]
    s.shutdown()


def test_a_list_element_with_spaces_stays_one_element_after_compaction(workdir):
    s = open_store()
    s.rpush("l", "hello world", "x")
    s.compact_aof()
    s = restart(s)
    assert list(s.lrange("l", 0, -1)) == ["hello world", "x"]
    s.shutdown()


def test_big_collections_are_compacted_in_bounded_records(workdir):
    n = AOF_CHUNK * 2 + 17
    s = open_store()
    s.rpush("l", *[f"item {i}" for i in range(n)])
    s.sadd("s", *[f"m {i}" for i in range(n)])
    s.compact_aof()
    s.shutdown()
    with open("test.aof", "rb") as f:
        records = [t for t, _ in aof.scan(f.read(), aof.ScanResult())]
    assert max(len(r) for r in records) <= AOF_CHUNK + 2
    s = open_store()
    assert list(s.lrange("l", 0, -1)) == [f"item {i}" for i in range(n)]
    assert len(s.smembers("s")) == n
    s.shutdown()


def test_writes_after_compaction_still_replay(workdir):
    s = open_store()
    s.set("a", "x y")
    s.compact_aof()
    s.set("b", "p\nq")
    s = restart(s)
    assert (s.get("a"), s.get("b")) == ("x y", "p\nq")
    s.shutdown()


# --- legacy files ---------------------------------------------------------------------------------


def test_a_legacy_aof_still_loads(workdir):
    with open("test.aof", "w") as f:
        f.write("SET a 1\nRPUSH l x y\nHSET h f some value\nDEL a\nSET b 2\n")
    s = open_store()
    assert s.get("a") is None and s.get("b") == "2"
    assert list(s.lrange("l", 0, -1)) == ["x", "y"]
    assert s.hget("h", "f") == "some value"
    s.shutdown()


def test_new_writes_after_a_legacy_file_replay_together(workdir):
    with open("test.aof", "w") as f:
        f.write("SET old 1\nSET gone 2\n")
    s = open_store()
    s.set("new", "with  spaces\nand a newline")
    s.delete("gone")
    s = restart(s)
    assert s.get("old") == "1"
    assert s.get("gone") is None
    assert s.get("new") == "with  spaces\nand a newline"
    s.shutdown()


def test_legacy_setb64_records_still_load(workdir):
    import base64

    raw = "a b\n\u00e9".encode("utf-8")
    with open("test.aof", "w") as f:
        f.write(f"SETB64 bm {base64.b64encode(raw).decode()}\nSETB64 empty -\n")
    s = open_store()
    assert s.get("bm") == "a b\n\u00e9"
    assert s.get("empty") == ""
    s.shutdown()


# --- a crash while writing ------------------------------------------------------------------------


def test_a_torn_final_record_is_dropped_and_the_log_is_cut_back(workdir, capsys):
    s = open_store()
    s.set("one", "1 1")
    s.set("two", "2\n2")
    s.shutdown()
    good = os.path.getsize("test.aof")
    record = aof.encode_record(["SET", "three", "never finished"])
    with open("test.aof", "ab") as f:
        f.write(record[:-6])

    s = open_store()
    assert (s.get("one"), s.get("two"), s.get("three")) == ("1 1", "2\n2", None)
    assert os.path.getsize("test.aof") == good
    assert "incomplete final record" in capsys.readouterr().out
    s.set("four", "4")
    s = restart(s)
    assert s.get("four") == "4"  # not stranded behind the old fragment
    s.shutdown()


def test_every_possible_cut_recovers_a_prefix_of_the_writes(workdir):
    s = open_store()
    values = ["a", "b c", "d\ne", "", "\u2713"]
    for i, v in enumerate(values):
        s.set(f"k{i}", v)
    s.shutdown()
    with open("test.aof", "rb") as f:
        full = f.read()
    for cut in range(len(full) + 1):
        with open("test.aof", "wb") as f:
            f.write(full[:cut])
        r = open_store()
        got = [r.get(f"k{i}") for i in range(len(values))]
        r.shutdown()
        k = next((i for i, g in enumerate(got) if g is None), len(values))
        assert got[:k] == values[:k], cut
        assert all(g is None for g in got[k:]), cut


# --- corruption in the middle ------------------------------------------------------------------------


def test_a_malformed_record_stops_replay_keeps_a_backup_and_unblocks_new_writes(
    workdir, capsys
):
    first = aof.encode_record(["SET", "kept", "v"])
    junk = b"A1\nS3\nSETX"
    later = aof.encode_record(["SET", "after_damage", "v"])
    original = first + junk + later
    with open("test.aof", "wb") as f:
        f.write(original)

    s = open_store()
    assert s.get("kept") == "v"
    assert s.get("after_damage") is None
    assert os.path.getsize("test.aof") == len(first)
    with open("test.aof.corrupt", "rb") as f:
        assert f.read() == original
    out = capsys.readouterr().out
    assert "malformed record" in out and "test.aof.corrupt" in out

    s.set("fresh", "x y")
    s = restart(s)
    assert (s.get("kept"), s.get("fresh")) == ("v", "x y")
    s.shutdown()


def test_an_empty_aof_is_fine(workdir):
    open("test.aof", "wb").close()
    s = open_store()
    s.set("a", "1")
    s = restart(s)
    assert s.get("a") == "1"
    s.shutdown()


def test_replay_does_not_relog(workdir):
    s = open_store()
    s.set("a", "x y")
    s.rpush("l", "p q")
    s.shutdown()
    with open("test.aof", "rb") as f:
        before = f.read()
    open_store().shutdown()
    with open("test.aof", "rb") as f:
        assert f.read() == before
