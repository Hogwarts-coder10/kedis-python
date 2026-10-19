"""CommandParser: working through a buffer by offset, the 'need' hint, and the
aggregate payload budget."""

import pytest

from kedis_python.parser import CommandParser, ProtocolError


def kesp(*args: str) -> bytes:
    body = b"".join(b"S%d\n%s\n" % (len(a.encode()), a.encode()) for a in args)
    return b"A%d\n" % len(args) + body


FRAMES = [
    kesp("SET", "k", "v"),
    kesp("SET", "k", "line1\nline2"),
    kesp("SET", "", ""),
    kesp("SET", "key", "x" * 300),
    b"SET k v\n",
]


# --- offsets ------------------------------------------------------------------------------


def test_a_pipeline_can_be_parsed_by_offset_without_trimming():
    pipeline = b"".join(FRAMES)
    pos, got = 0, []
    while pos < len(pipeline):
        tokens, consumed = CommandParser.parse(pipeline, pos)
        assert tokens and consumed > 0
        got.append(tokens)
        pos += consumed
    assert pos == len(pipeline)
    assert got == [CommandParser.parse(f)[0] for f in FRAMES]


@pytest.mark.parametrize("frame", FRAMES)
def test_a_bytearray_parses_like_bytes(frame):
    assert CommandParser.parse(bytearray(frame)) == CommandParser.parse(bytes(frame))
    padded = bytearray(b"\n\n" + frame)
    assert CommandParser.parse(padded, 2) == CommandParser.parse(frame)


def test_consumed_counts_from_start_including_blank_frames():
    tokens, consumed = CommandParser.parse(b"xx\n\nA0\n" + kesp("GET", "k"), 2)
    assert tokens == ["GET", "k"]
    assert consumed == len(b"\n\nA0\n") + len(kesp("GET", "k"))


def test_a_protocol_error_consumes_the_rest_of_the_buffer_from_start():
    raw = b"AAAA" + b"A+1\n"
    err, consumed = CommandParser.parse(raw, 4)
    assert isinstance(err, ProtocolError)
    assert consumed == len(raw) - 4


# --- the need hint -------------------------------------------------------------------------


def test_need_is_the_end_of_the_half_received_payload():
    head = b"A2\nS3\nGET\nS100\n"
    tokens, consumed, need = CommandParser.parse_buffer(head + b"abc")
    assert (tokens, consumed) == ([], 0)
    assert need == len(head) + 100 + 1


def test_need_is_relative_to_the_unparsed_frame_after_blank_lines():
    head = b"A1\nS50\n"
    tokens, consumed, need = CommandParser.parse_buffer(b"\n\n" + head + b"x", 0)
    assert (tokens, consumed) == ([], 2)
    assert need == len(head) + 50 + 1


def test_need_is_zero_when_a_command_or_error_came_back():
    assert CommandParser.parse_buffer(kesp("GET", "k"))[2] == 0
    assert CommandParser.parse_buffer(b"A+1\n")[2] == 0


def test_need_is_at_least_one_more_byte_for_headers():
    for raw in (b"A", b"A2", b"A1\nS", b"A1\nS3", b"SET k"):
        tokens, _, need = CommandParser.parse_buffer(raw)
        assert tokens == [] and need >= len(raw) + 1, raw


@pytest.mark.parametrize("frame", FRAMES)
def test_the_hint_never_skips_past_a_parse_that_would_succeed(frame):
    assert CommandParser.parse_buffer(b"") == (
        [],
        0,
        0,
    )  # nothing buffered, nothing needed
    for cut in range(1, len(frame)):
        tokens, consumed, need = CommandParser.parse_buffer(frame[:cut])
        assert tokens == [] and consumed == 0, cut
        assert cut < need <= len(frame), (cut, need)
        # up to need - 1 bytes, still nothing to parse
        assert CommandParser.parse(frame[: need - 1]) == ([], 0), (cut, need)


# --- aggregate payload budget ------------------------------------------------------------------


def test_one_bulk_over_the_budget_is_rejected_on_its_header(monkeypatch):
    monkeypatch.setattr(CommandParser, "MAX_COMMAND_BYTES", 100)
    err, consumed = CommandParser.parse(b"A1\nS101\n")
    assert isinstance(err, ProtocolError) and "too large" in err.message
    assert consumed == len(b"A1\nS101\n")


def test_many_bulks_that_add_up_over_the_budget_are_rejected_early(monkeypatch):
    monkeypatch.setattr(CommandParser, "MAX_COMMAND_BYTES", 100)
    first = b"A3\nS3\nSET\nS60\n" + b"x" * 60 + b"\n"
    err, _ = CommandParser.parse(
        first + b"S60\n"
    )  # 3 + 60 + 60 > 100, payload not sent yet
    assert isinstance(err, ProtocolError) and "too large" in err.message


def test_a_command_exactly_at_the_budget_is_accepted(monkeypatch):
    monkeypatch.setattr(CommandParser, "MAX_COMMAND_BYTES", 100)
    raw = b"A3\nS3\nSET\nS1\nk\nS96\n" + b"v" * 96 + b"\n"
    tokens, consumed = CommandParser.parse(raw)
    assert tokens[:2] == ["SET", "k"] and consumed == len(raw)


def test_the_budget_is_per_command_not_per_connection(monkeypatch):
    monkeypatch.setattr(CommandParser, "MAX_COMMAND_BYTES", 100)
    one = b"A2\nS3\nGET\nS60\n" + b"k" * 60 + b"\n"
    pos = 0
    for _ in range(5):
        tokens, consumed = CommandParser.parse(one * 5, pos)
        assert tokens[0] == "GET"
        pos += consumed


def test_the_default_budget_matches_the_largest_single_value():
    assert CommandParser.MAX_COMMAND_BYTES == CommandParser.MAX_BULK_LENGTH
