"""
Tests for bitmap operations (SETBIT/GETBIT/BITCOUNT/BITOP) on KedisStore.

Uses a temp directory per test so the AOF file and the (hardcoded)
kedis.snapshot lookup in KedisStore don't touch or get confused by
whatever's sitting in the real project directory.

Run: pytest test_bitmaps.py -v
"""

import pytest
from kedis_python.store import KedisStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # appendfsync="always" avoids spinning up the background fsync
    # daemon thread that "everysec" mode starts — cleaner for tests.
    s = KedisStore(aof_filename="test_kedis.aof", appendfsync="always")
    yield s
    try:
        s.aof_file.close()
    except Exception:
        pass


def byte_at(s: str, i: int) -> int:
    return ord(s[i])


# ---------------------------------------------------------------------
# SETBIT / GETBIT
# ---------------------------------------------------------------------

def test_setbit_returns_previous_value(store):
    # Key doesn't exist yet — previous bit is always 0.
    assert store.setbit("mykey", 7, 1) == 0
    # Setting the same bit again — previous value is now 1.
    assert store.setbit("mykey", 7, 1) == 1


def test_setbit_sets_the_correct_bit_msb_first(store):
    store.setbit("mykey", 0, 1)  # MSB of byte 0
    assert byte_at(store.get("mykey"), 0) == 0b10000000

    store.setbit("mykey", 7, 1)  # LSB of byte 0
    assert byte_at(store.get("mykey"), 0) == 0b10000001


def test_setbit_extends_string_with_zero_bytes(store):
    store.setbit("mykey", 23, 1)  # byte index 2 (bits 16-23)
    val = store.get("mykey")
    assert len(val) == 3
    assert byte_at(val, 0) == 0
    assert byte_at(val, 1) == 0
    assert byte_at(val, 2) == 0b00000001


def test_getbit_reflects_setbit(store):
    store.setbit("mykey", 100, 1)
    assert store.getbit("mykey", 100) == 1
    assert store.getbit("mykey", 99) == 0
    assert store.getbit("mykey", 101) == 0


def test_getbit_out_of_range_and_missing_key_return_zero(store):
    assert store.getbit("missing", 0) == 0
    store.setbit("mykey", 0, 1)
    assert store.getbit("mykey", 999) == 0


def test_setbit_rejects_invalid_bit_value(store):
    with pytest.raises(ValueError):
        store.setbit("mykey", 0, 2)


def test_setbit_rejects_negative_offset(store):
    with pytest.raises(ValueError):
        store.setbit("mykey", -1, 1)


def test_setbit_wrongtype_on_non_string_key(store):
    store.lpush("mylist", "a")
    with pytest.raises(TypeError):
        store.setbit("mylist", 0, 1)


def test_bitmap_key_reports_type_string(store):
    store.setbit("mykey", 0, 1)
    assert store.type_of("mykey") == "string"


# ---------------------------------------------------------------------
# BITCOUNT
# ---------------------------------------------------------------------

def test_bitcount_missing_key_is_zero(store):
    assert store.bitcount("missing") == 0


def test_bitcount_whole_string(store):
    store.set("mykey", "foobar")  # known Redis doc example: BITCOUNT = 26
    assert store.bitcount("mykey") == 26


def test_bitcount_with_byte_range(store):
    store.set("mykey", "foobar")
    assert store.bitcount("mykey", 1, 1) == 6  # just 'o' -> 4 bits set... (doc value)
    assert store.bitcount("mykey", 0, 0) == 4  # just 'f'


def test_bitcount_with_negative_indices(store):
    store.set("mykey", "foobar")
    assert store.bitcount("mykey", -2, -1) == store.bitcount("mykey", 4, 5)


# ---------------------------------------------------------------------
# BITOP
# ---------------------------------------------------------------------

def test_bitop_and(store):
    store.set("a", chr(0b11001100))
    store.set("b", chr(0b10101010))
    length = store.bitop("AND", "dest", "a", "b")
    assert length == 1
    assert byte_at(store.get("dest"), 0) == 0b10001000


def test_bitop_or(store):
    store.set("a", chr(0b11001100))
    store.set("b", chr(0b10101010))
    store.bitop("OR", "dest", "a", "b")
    assert byte_at(store.get("dest"), 0) == 0b11101110


def test_bitop_xor(store):
    store.set("a", chr(0b11001100))
    store.set("b", chr(0b10101010))
    store.bitop("XOR", "dest", "a", "b")
    assert byte_at(store.get("dest"), 0) == 0b01100110


def test_bitop_not(store):
    store.set("a", chr(0b11001100))
    store.bitop("NOT", "dest", "a")
    assert byte_at(store.get("dest"), 0) == 0b00110011


def test_bitop_not_rejects_multiple_sources(store):
    store.set("a", "x")
    store.set("b", "y")
    with pytest.raises(ValueError):
        store.bitop("NOT", "dest", "a", "b")


def test_bitop_pads_shorter_sources_with_zero_bytes(store):
    store.set("short", chr(0b11111111))  # 1 byte
    store.set("long", chr(0b11111111) + chr(0b11111111))  # 2 bytes

    length = store.bitop("AND", "dest", "short", "long")
    assert length == 2
    result = store.get("dest")
    assert byte_at(result, 0) == 0b11111111  # both had this byte set
    assert byte_at(result, 1) == 0b00000000  # "short" padded with \x00 here


def test_bitop_wrongtype_on_source_key(store):
    store.lpush("mylist", "a")
    store.set("mystring", "x")
    with pytest.raises(TypeError):
        store.bitop("AND", "dest", "mylist", "mystring")


def test_bitop_missing_source_treated_as_zero_length(store):
    store.set("a", chr(0b11111111))
    length = store.bitop("AND", "dest", "a", "does_not_exist")
    assert length == 1
    assert byte_at(store.get("dest"), 0) == 0b00000000


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
