"""
Tests for CommandParser's framing hardening.

Covers:
- The inline-mode fragmentation fix (a partial command must NOT be
  treated as complete just because it's currently all we have).
- The new bounds against unbounded buffer growth from unterminated
  or absurdly-oversized frames.
- That normal KESP parsing and pipelining still work exactly as before.

Run: pytest test_parser_hardening.py -v
"""

import pytest
from kedis_python.parser import CommandParser


def kesp_encode(*args: str) -> bytes:
    """Small helper to build a valid KESP array frame for test input."""
    header = f"A{len(args)}\n".encode()
    body = b"".join(f"S{len(a.encode())}\n{a}\n".encode() for a in args)
    return header + body


# ---------------------------------------------------------------------
# Sanity: normal parsing still works
# ---------------------------------------------------------------------

def test_kesp_basic_command():
    frame = kesp_encode("SET", "foo", "bar")
    tokens, consumed = CommandParser.parse(frame)
    assert tokens == ["SET", "foo", "bar"]
    assert consumed == len(frame)


def test_inline_basic_command():
    frame = b"SET foo bar\n"
    tokens, consumed = CommandParser.parse(frame)
    assert tokens == ["SET", "foo", "bar"]
    assert consumed == len(frame)


def test_kesp_pipelined_commands_only_consumes_first():
    first = kesp_encode("SET", "a", "1")
    second = kesp_encode("SET", "b", "2")
    buffer = first + second

    tokens, consumed = CommandParser.parse(buffer)
    assert tokens == ["SET", "a", "1"]
    assert consumed == len(first)

    # Simulate the server slicing the buffer and parsing again
    remaining = buffer[consumed:]
    tokens2, consumed2 = CommandParser.parse(remaining)
    assert tokens2 == ["SET", "b", "2"]
    assert consumed2 == len(second)


def test_incomplete_kesp_frame_waits_for_more_data():
    frame = kesp_encode("SET", "foo", "bar")
    tokens, consumed = CommandParser.parse(frame[:-3])  # chop off the tail
    assert tokens == []
    assert consumed == 0


# ---------------------------------------------------------------------
# The fragmentation bug fix: inline mode must wait for a real terminator
# ---------------------------------------------------------------------

def test_inline_fragmented_across_two_reads_is_not_executed_early():
    # First "packet" — no newline yet, command is NOT complete.
    partial = b"SET foo ba"
    tokens, consumed = CommandParser.parse(partial)

    assert tokens == [], "a partial inline command must not be treated as complete"
    assert consumed == 0

    # Second "packet" arrives, completing the buffer.
    full = partial + b"r\n"
    tokens, consumed = CommandParser.parse(full)
    assert tokens == ["SET", "foo", "bar"]
    assert consumed == len(full)


def test_inline_pipelined_commands_only_consumes_first_line():
    buffer = b"SET a 1\nSET b 2\n"
    tokens, consumed = CommandParser.parse(buffer)
    assert tokens == ["SET", "a", "1"]
    assert consumed == buffer.index(b"\n") + 1

    remaining = buffer[consumed:]
    tokens2, consumed2 = CommandParser.parse(remaining)
    assert tokens2 == ["SET", "b", "2"]
    assert consumed2 == len(remaining)


# ---------------------------------------------------------------------
# Hardening: bounded buffering, no infinite waits on hostile input
# ---------------------------------------------------------------------

def test_unterminated_inline_command_within_limit_waits():
    junk = b"S" * (CommandParser.MAX_INLINE_LENGTH - 1)
    tokens, consumed = CommandParser.parse(junk)
    assert tokens == []
    assert consumed == 0


def test_unterminated_inline_command_over_limit_errors_out():
    junk = b"S" * (CommandParser.MAX_INLINE_LENGTH + 1)
    tokens, consumed = CommandParser.parse(junk)
    assert tokens[0] == "ERROR"
    assert consumed == len(junk)  # buffer is discarded, not held forever


def test_unterminated_array_header_over_limit_errors_out():
    junk = b"A" + b"9" * (CommandParser.MAX_HEADER_LINE + 1)
    tokens, consumed = CommandParser.parse(junk)
    assert tokens[0] == "ERROR"
    assert consumed == len(junk)


def test_absurd_declared_array_length_is_rejected():
    frame = f"A{CommandParser.MAX_ARRAY_LENGTH + 1}\n".encode()
    tokens, consumed = CommandParser.parse(frame)
    assert tokens[0] == "ERROR"
    assert "multibulk" in tokens[1].lower()
    assert consumed == len(frame)


def test_absurd_declared_bulk_length_is_rejected():
    header = b"A1\n"
    bad_string_header = f"S{CommandParser.MAX_BULK_LENGTH + 1}\n".encode()
    frame = header + bad_string_header

    tokens, consumed = CommandParser.parse(frame)
    assert tokens[0] == "ERROR"
    assert "bulk length" in tokens[1].lower()
    assert consumed == len(frame)


def test_negative_declared_lengths_are_rejected():
    # Shouldn't normally happen over the wire, but the parser should
    # not silently misbehave (e.g. negative slicing) if it does.
    frame = b"A-1\n"
    tokens, consumed = CommandParser.parse(frame)
    assert tokens[0] == "ERROR"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
