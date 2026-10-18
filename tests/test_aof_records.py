"""The AOF record format: framing, legacy lines, torn and corrupt files."""

import pytest

from kedis_python import aof
from kedis_python.aof import ScanResult, encode_record, scan


def read(buf: bytes):
    result = ScanResult()
    records = [tokens for tokens, _ in scan(buf, result)]
    return records, result


TRICKY = [
    "",
    "a b",
    "  padded  ",
    "x\ty",
    "line1\nline2",
    "a\r\nb",
    "A3\nS3\nSET",  # looks like a record
    "SET k v",
    "v\nFLUSHALL\n",  # newline injection attempt
    "h\u00e9llo \u2713",
    "\udc80",  # lone surrogate
    "x" * 5000,
]


def test_record_layout():
    assert encode_record(["SET", "k", "v"]) == b"A3\nS3\nSET\nS1\nk\nS1\nv\n"
    assert encode_record(["FLUSHALL"]) == b"A1\nS8\nFLUSHALL\n"


def test_non_string_tokens_are_written_as_text():
    assert encode_record(["SETBIT", "k", 7, 1]) == (
        b"A4\nS6\nSETBIT\nS1\nk\nS1\n7\nS1\n1\n"
    )
    records, _ = read(encode_record(["ZADD", "z", 1.5, "m"]))
    assert records == [["ZADD", "z", "1.5", "m"]]


@pytest.mark.parametrize("value", TRICKY)
def test_any_string_round_trips(value):
    records, result = read(encode_record(["SET", "key", value]))
    assert records == [["SET", "key", value]]
    assert result.status == "ok"


def test_a_value_with_newlines_cannot_plant_a_command():
    records, _ = read(encode_record(["SET", "k", "v\nFLUSHALL"]))
    assert len(records) == 1
    assert records[0][0] == "SET"


def test_records_come_back_in_order_with_end_offsets():
    blob = b"".join(encode_record(["SET", f"k{i}", "v"]) for i in range(3))
    result = ScanResult()
    got = list(scan(blob, result))
    assert [t for t, _ in got] == [["SET", f"k{i}", "v"] for i in range(3)]
    assert got[-1][1] == len(blob) == result.good_end


# --- legacy lines ---------------------------------------------------------------------------


def test_legacy_lines_are_split_on_whitespace():
    records, result = read(b"SET a 1\n\n   \nRPUSH l x y\n")
    assert records == [["SET", "a", "1"], ["RPUSH", "l", "x", "y"]]
    assert result.status == "ok"


def test_an_unterminated_last_legacy_line_is_still_read():
    assert read(b"SET a 1\nSET b 2")[0] == [["SET", "a", "1"], ["SET", "b", "2"]]


def test_a_file_may_mix_legacy_lines_and_records():
    blob = b"SET a 1\n" + encode_record(["SET", "b", "x y"]) + b"DEL a\n"
    records, result = read(blob)
    assert records == [["SET", "a", "1"], ["SET", "b", "x y"], ["DEL", "a"]]
    assert result.status == "ok"
    assert result.good_end == len(blob)


# --- torn tails (a crash while writing) -----------------------------------------------------


def test_every_cut_through_a_record_is_torn_and_loses_only_that_record():
    first = encode_record(["SET", "a", "x\ny"])
    second = encode_record(["SET", "b", "h\u00e9llo"])
    blob = first + second
    for cut in range(len(first), len(blob)):
        records, result = read(blob[:cut])
        assert records == [["SET", "a", "x\ny"]], cut
        assert result.good_end == len(first), cut
        assert result.status == ("ok" if cut == len(first) else "torn"), cut


def test_cutting_the_very_first_record_leaves_nothing():
    blob = encode_record(["SET", "a", "v"])
    for cut in range(1, len(blob)):
        records, result = read(blob[:cut])
        assert records == [] and result.good_end == 0 and result.status == "torn"


# --- corruption -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "garbage",
    [
        b"A1\nS3\nSETX",  # wrong terminator
        b"A1\nS3\nSETXX\n",
        b"A+1\nS1\nx\n",  # loose number
        b"A01\nS1\nx\n",
        b"A1\r\nS1\nx\n",
        b"A1\nX1\nx\n",  # wrong sigil
        b"A1\nS1_0\nx\n",
        b"A1\nS2\n\xff\xfe\n",  # not UTF-8
        b"A" + b"9" * 40 + b"\n",  # header too long
    ],
)
def test_malformed_records_stop_the_scan_at_the_last_good_boundary(garbage):
    good = encode_record(["SET", "a", "v"])
    records, result = read(good + garbage + encode_record(["SET", "later", "v"]))
    assert records == [["SET", "a", "v"]]
    assert result.status == "corrupt"
    assert result.good_end == len(good)
    assert result.reason


def test_a_legacy_command_word_starting_with_a_is_still_a_legacy_line():
    assert read(b"AUTH secret\n")[0] == [["AUTH", "secret"]]


def test_header_constants_match_the_wire_protocol():
    from kedis_python.parser import CommandParser

    assert aof.MAX_HEADER_LINE == CommandParser.MAX_HEADER_LINE
