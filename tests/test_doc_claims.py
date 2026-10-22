"""Pins the exact boundary claims made in docs/protocol.md and docs/limits.md."""

import pytest

from kedis_python.parser import (
    CommandParser,
    KESPDecoder,
    KESPEncoder,
    ProtocolError,
    ReplyProtocolError,
)

INLINE = CommandParser.MAX_INLINE_LENGTH
TEXT = KESPDecoder.MAX_LINE


def parse(data):
    return CommandParser.parse_buffer(data)


# --- line limits count the line without its terminator ------------------------------------------


def test_an_inline_line_of_exactly_the_cap_plus_terminator_is_accepted():
    tokens, consumed, _ = parse(b"x" * INLINE + b"\n")
    assert not isinstance(tokens, ProtocolError) and consumed == INLINE + 1


def test_one_byte_more_is_an_error():
    tokens, _, _ = parse(b"x" * (INLINE + 1) + b"\n")
    assert isinstance(tokens, ProtocolError)


def test_an_unterminated_line_is_judged_by_the_bytes_received_so_far():
    tokens, consumed, need = parse(b"x" * INLINE)
    assert tokens == [] and consumed == 0 and need > 0  # still waiting
    tokens, _, _ = parse(b"x" * (INLINE + 1))
    assert isinstance(tokens, ProtocolError)  # fails now, without waiting for a \n


def test_a_reply_text_line_counts_its_sigil_but_not_its_terminator():
    assert KESPDecoder.decode(b"E" + b"x" * (TEXT - 1) + b"\n")[1] == TEXT + 1
    with pytest.raises(ReplyProtocolError):
        KESPDecoder.decode(b"E" + b"x" * TEXT + b"\n")


# --- collection order ------------------------------------------------------------------------


def test_lists_and_tuples_keep_their_order():
    assert KESPEncoder.encode(["b", "a", "c"]) == b"A3\nS1\nb\nS1\na\nS1\nc\n"
    assert KESPEncoder.encode(("b", "a", "c")) == KESPEncoder.encode(["b", "a", "c"])


def test_a_dict_is_flattened_in_insertion_order():
    wire = KESPEncoder.encode({"z": 1, "a": 2, "m": 3})
    assert wire == b"A6\nS1\nz\nI1\nS1\na\nI2\nS1\nm\nI3\n"


def test_a_set_goes_out_as_an_array_of_its_members_in_iteration_order():
    members = {"x", "y", "z"}
    value, _ = KESPDecoder.decode(KESPEncoder.encode(members))
    assert list(value) == [str(m) for m in members]  # whatever order Python iterates


# --- the error-text cap ----------------------------------------------------------------------


def test_error_text_is_cut_at_a_character_boundary_and_ends_in_dots():
    value, _ = KESPDecoder.decode(KESPEncoder.encode("-ERR " + "é" * 100_000))
    assert value.endswith("...") and set(value[:-3]) == {"é"}
    assert len(value.encode()) <= TEXT - 1
