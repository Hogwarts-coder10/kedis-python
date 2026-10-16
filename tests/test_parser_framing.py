"""Framing rules for CommandParser (the server-side command decoder).

Covers what test_parser_harden.py does not: terminators, limits on lines that
*are* terminated, strict ASCII header numbers, protocol errors signalled
out-of-band, blank frames, and fragmentation at every byte boundary.
"""

import pytest

from kedis_python.parser import CommandParser, ProtocolError


def kesp(*args: str) -> bytes:
    body = b"".join(f"S{len(a.encode())}\n{a}\n".encode() for a in args)
    return f"A{len(args)}\n".encode() + body


def is_error(result) -> bool:
    return isinstance(result[0], ProtocolError)


# --- terminators -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"A1\nS3\nSETX",  # wrong terminator byte
        b"A1\nS3\nSET\r",  # CR is not a terminator
        b"A1\nS3\nSETA1\nS4\nPING\n",  # missing terminator, next frame glued on
        b"A1\nS3\nSETSET\n",  # payload longer than its declared length
    ],
)
def test_bulk_payload_must_be_followed_by_newline(raw):
    result = CommandParser.parse(raw)
    assert is_error(result)
    assert result[1] == len(raw)


def test_short_bulk_payload_still_waits_for_terminator():
    assert CommandParser.parse(b"A1\nS3\nSET") == ([], 0)


# --- limits apply to terminated lines too ------------------------------------


def test_terminated_inline_line_over_limit_is_rejected():
    raw = b"X" * (CommandParser.MAX_INLINE_LENGTH + 1) + b"\n"
    assert is_error(CommandParser.parse(raw))


def test_terminated_inline_line_at_limit_is_accepted():
    raw = b"X" * CommandParser.MAX_INLINE_LENGTH + b"\n"
    tokens, consumed = CommandParser.parse(raw)
    assert not isinstance(tokens, ProtocolError)
    assert consumed == len(raw)


def test_terminated_array_header_over_limit_is_rejected():
    raw = b"A" + b"0" * 60 + b"1\n" + kesp("PING")[3:]
    assert is_error(CommandParser.parse(raw))


def test_terminated_bulk_header_over_limit_is_rejected():
    raw = b"A1\nS" + b"0" * 60 + b"4\nPING\n"
    assert is_error(CommandParser.parse(raw))


# --- strict header numbers ---------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [b"A+1", b"A 1", b"A1 ", b"A01", b"A1_0", b"A-0", b"A", b"A1\r", b"A\xd9\xa1", b"A0x1"],
)
def test_loose_array_counts_are_rejected(header):
    assert is_error(CommandParser.parse(header + b"\nS4\nPING\n"))


@pytest.mark.parametrize(
    "header",
    [b"S+4", b"S 4", b"S4 ", b"S04", b"S4_", b"S-0", b"S", b"S4\r", b"S\xd9\xa4", b"S0x4"],
)
def test_loose_bulk_lengths_are_rejected(header):
    assert is_error(CommandParser.parse(b"A1\n" + header + b"\nPING\n"))


def test_wrong_element_sigil_is_rejected_without_waiting_for_a_newline():
    assert is_error(CommandParser.parse(b"A1\nX"))


def test_crlf_array_header_is_rejected():
    assert is_error(CommandParser.parse(b"A1\r\nS4\nPING\n"))


# --- protocol errors are out-of-band -------------------------------------------


def test_protocol_error_keeps_the_legacy_token_shape():
    err, _ = CommandParser.parse(b"A+1\n")
    assert isinstance(err, list)
    assert err[0] == "ERROR"
    assert err[1] == err.message


@pytest.mark.parametrize(
    "raw, expected",
    [
        (kesp("ERROR"), ["ERROR"]),
        (kesp("ERROR", "foo"), ["ERROR", "foo"]),
        (kesp("error", "x"), ["ERROR", "x"]),
        (b"error x\n", ["ERROR", "x"]),
    ],
)
def test_a_command_named_error_is_an_ordinary_command(raw, expected):
    tokens, consumed = CommandParser.parse(raw)
    assert not isinstance(tokens, ProtocolError)
    assert tokens == expected
    assert consumed == len(raw)


# --- blank frames --------------------------------------------------------------


@pytest.mark.parametrize("raw", [b"\n", b"\r\n", b"   \n", b"A0\n", b"\n\n\n", b"\nA0\n\n"])
def test_blank_frames_are_consumed_not_held(raw):
    assert CommandParser.parse(raw) == ([], len(raw))


@pytest.mark.parametrize(
    "prefix", [b"\n", b"A0\n", b"\n\nA0\n", b"  \r\n"]
)
def test_blank_frames_before_a_command_are_skipped(prefix):
    frame = kesp("SET", "k", "v")
    assert CommandParser.parse(prefix + frame) == (["SET", "k", "v"], len(prefix + frame))


def test_blank_frames_before_a_partial_command_are_reported_consumed():
    frame = kesp("SET", "k", "v")
    assert CommandParser.parse(b"\n\n" + frame[:-2]) == ([], 2)


def test_long_run_of_blank_lines_is_one_pass():
    raw = b"\n" * 200_000 + b"PING\n"
    assert CommandParser.parse(raw) == (["PING"], len(raw))


# --- fragmentation at every byte boundary ----------------------------------------

FRAMES = [
    kesp("SET", "k", "v"),
    kesp("SET", "k", "line1\nline2"),  # newline inside a payload
    kesp("SET", "k", "A1\nS3\nSET"),  # payload that looks like a frame
    kesp("SET", "k", "h\u00e9llo \u2713"),  # multi-byte UTF-8, split mid-character
    kesp("SET", "", ""),  # empty strings
    b"SET k v\n",
]


@pytest.mark.parametrize("frame", FRAMES)
def test_every_proper_prefix_waits_and_the_whole_frame_parses(frame):
    whole, consumed = CommandParser.parse(frame)
    assert consumed == len(frame)
    assert whole and whole[0] == "SET"
    for cut in range(len(frame)):
        assert CommandParser.parse(frame[:cut]) == ([], 0), f"cut at {cut}"


@pytest.mark.parametrize("frame", FRAMES)
def test_pipelined_frames_are_consumed_one_at_a_time(frame):
    buf = frame + frame
    first, used = CommandParser.parse(buf)
    second, used2 = CommandParser.parse(buf[used:])
    assert first == second
    assert used == used2 == len(frame)
