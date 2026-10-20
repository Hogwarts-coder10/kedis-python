"""Whole-reply budgets: the decoder and the encoder agree on how many values
and how many payload bytes one reply may hold.

The defaults (4,194,304 values, 1 GiB) are far too big to reach in a test, so
these tests shrink them with monkeypatch and probe both sides of each boundary.
"""

import pytest

from kedis_python.parser import (
    BulkString,
    IncompleteReply,
    KESPDecoder,
    KESPEncoder,
    ReplyProtocolError,
)


@pytest.fixture
def small(monkeypatch):
    """Shrink both budgets: 8 values, 20 payload bytes."""
    monkeypatch.setattr(KESPDecoder, "MAX_REPLY_ELEMENTS", 8)
    monkeypatch.setattr(KESPDecoder, "MAX_REPLY_PAYLOAD", 20)


def flat(n: int) -> bytes:
    """An array of n nils: n + 1 values."""
    return b"A%d\n" % n + b"N\n" * n


def nested(*sizes: int) -> bytes:
    """An array holding one nil-array per size: 1 + len(sizes) + sum(sizes)."""
    out = b"A%d\n" % len(sizes)
    for size in sizes:
        out += flat(size)
    return out


# --- decoder: element budget ----------------------------------------------------------------


def test_decoder_accepts_exactly_the_element_budget(small):
    reply = flat(7)  # 1 + 7 = 8 values
    value, end = KESPDecoder.decode(reply)
    assert value == [None] * 7 and end == len(reply)


def test_decoder_rejects_one_element_over_budget(small):
    with pytest.raises(ReplyProtocolError, match="too many elements"):
        KESPDecoder.decode(flat(8))  # 9 values


def test_decoder_budget_is_cumulative_across_nested_arrays(small):
    # Each inner array is small; only the sum breaks the budget.
    assert KESPDecoder.decode(nested(2, 2))[0] == [[None, None], [None, None]]  # 7 values
    with pytest.raises(ReplyProtocolError, match="too many elements"):
        KESPDecoder.decode(nested(3, 3))  # 1 + 2 + 6 = 9


def test_decoder_refuses_a_declared_count_over_budget_on_the_header_alone(small):
    # No elements follow. Refused anyway, not reported as incomplete.
    with pytest.raises(ReplyProtocolError, match="too many elements"):
        KESPDecoder.decode(b"A100\n")


def test_decoder_counts_scalars_too(small):
    reply = b"A7\n" + b"I1\n" * 7  # 8 values
    assert KESPDecoder.decode(reply)[0] == [1] * 7
    with pytest.raises(ReplyProtocolError, match="too many elements"):
        KESPDecoder.decode(b"A8\n" + b"I1\n" * 8)


# --- decoder: payload budget ----------------------------------------------------------------


def test_decoder_accepts_exactly_the_payload_budget(small):
    reply = b"S20\n" + b"x" * 20 + b"\n"
    assert KESPDecoder.decode(reply)[0] == "x" * 20


def test_decoder_rejects_one_payload_byte_over_budget(small):
    with pytest.raises(ReplyProtocolError, match="payload too large"):
        KESPDecoder.decode(b"S21\n" + b"x" * 21 + b"\n")


def test_decoder_payload_budget_is_cumulative(small):
    ok = b"A2\n" + b"S10\n" + b"a" * 10 + b"\n" + b"S10\n" + b"b" * 10 + b"\n"
    assert KESPDecoder.decode(ok)[0] == ["a" * 10, "b" * 10]
    bad = b"A2\n" + b"S10\n" + b"a" * 10 + b"\n" + b"S11\n" + b"b" * 11 + b"\n"
    with pytest.raises(ReplyProtocolError, match="payload too large"):
        KESPDecoder.decode(bad)


def test_decoder_refuses_an_oversized_string_before_its_bytes_arrive(small):
    # Only the header is here. Waiting for 1000 bytes would be pointless.
    with pytest.raises(ReplyProtocolError, match="payload too large"):
        KESPDecoder.decode(b"S1000\n")


def test_decoder_counts_status_and_error_lines_as_payload(small):
    assert KESPDecoder.decode(b"+" + b"k" * 19 + b"\n")[0] == "k" * 19
    with pytest.raises(ReplyProtocolError, match="payload too large"):
        KESPDecoder.decode(b"E" + b"k" * 40 + b"\n")


def test_incomplete_replies_are_not_charged_twice(small):
    # A reply arriving in pieces is re-decoded from the start each time; every
    # attempt gets a fresh budget, so a legal reply never fails on a retry.
    reply = b"A2\n" + b"S10\n" + b"a" * 10 + b"\n" + b"S10\n" + b"b" * 10 + b"\n"
    for cut in range(1, len(reply)):
        with pytest.raises(IncompleteReply):
            KESPDecoder.decode(reply[:cut])
    assert KESPDecoder.decode(reply)[0] == ["a" * 10, "b" * 10]


def test_each_decode_call_starts_with_a_full_budget(small):
    for _ in range(5):
        assert KESPDecoder.decode(flat(7))[0] == [None] * 7


def test_decoder_budget_applies_from_a_nonzero_offset(small):
    buf = b"I5\n" + flat(7)
    value, end = KESPDecoder.decode(buf, 3)
    assert value == [None] * 7 and end == len(buf)


def test_default_budgets_are_the_documented_ones():
    assert KESPDecoder.MAX_REPLY_ELEMENTS == 4_194_304
    assert KESPDecoder.MAX_REPLY_PAYLOAD == 1024**3


# --- encoder: same budgets ------------------------------------------------------------------


def decoded_or_error(data):
    """Encode under the shrunken budgets; decode with roomy ones, so the error
    line the encoder falls back to is not itself refused by the tiny payload cap."""
    wire = KESPEncoder.encode(data)
    return wire, KESPDecoder.decode(wire, _budget=[10**9, 10**9])[0]


def test_encoder_emits_a_reply_exactly_at_the_element_budget(small):
    wire, value = decoded_or_error([None] * 7)
    assert value == [None] * 7 and wire == flat(7)


def test_encoder_turns_an_over_budget_reply_into_one_error_line(small):
    wire, value = decoded_or_error([None] * 8)
    assert wire.startswith(b"E") and wire.count(b"\n") == 1
    assert "too many elements" in value


def test_encoder_budget_is_cumulative_across_nested_lists(small):
    _, ok = decoded_or_error([[None, None], [None, None]])
    assert ok == [[None, None], [None, None]]
    wire, _ = decoded_or_error([[None] * 3, [None] * 3])
    assert wire.startswith(b"E")


def test_encoder_counts_dict_keys_and_values(small):
    # 1 array + 2 per entry: 3 entries = 7 values, 4 entries = 9.
    ok = {f"k{i}": i for i in range(3)}
    wire, value = decoded_or_error(ok)
    assert value == ["k0", 0, "k1", 1, "k2", 2]
    wire, _ = decoded_or_error({f"k{i}": i for i in range(4)})
    assert wire.startswith(b"E")


def test_encoder_payload_budget_boundary(small):
    assert decoded_or_error(BulkString("x" * 20))[1] == "x" * 20
    wire, value = decoded_or_error(BulkString("x" * 21))
    assert wire.startswith(b"E") and "payload too large" in value


def test_encoder_payload_budget_is_cumulative(small):
    assert decoded_or_error(["a" * 10, "b" * 10])[1] == ["a" * 10, "b" * 10]
    wire, _ = decoded_or_error(["a" * 10, "b" * 11])
    assert wire.startswith(b"E")


def test_encoder_and_decoder_agree_on_the_edge(small):
    # Whatever the encoder emits, the decoder accepts; whatever it would
    # refuse, it replaces with an error the decoder also accepts.
    for n in range(0, 12):
        wire = KESPEncoder.encode([None] * n)
        KESPDecoder.decode(wire, _budget=[10**9, 10**9])  # never raises
        assert wire.startswith(b"A") == (n + 1 <= 8)


# --- boundary shapes the cheap up-front checks do not cover ---------------------------------
# Refusing a declared count on its header only sees direct children. These
# replies pass every header check and overflow only once their scalars are
# counted one by one.


def test_decoder_refuses_the_value_that_crosses_the_budget(small):
    # 1 + (1 + 4) + 2 = 8 values: exactly at the budget.
    at = b"A3\n" + flat(4) + b"I1\n" + b"I2\n"
    assert KESPDecoder.decode(at)[0] == [[None] * 4, 1, 2]
    # One more scalar: 9 values, and no header declares more than was left.
    over = b"A4\n" + flat(4) + b"I1\n" + b"I2\n" + b"I3\n"
    with pytest.raises(ReplyProtocolError, match="too many elements"):
        KESPDecoder.decode(over)


def test_encoder_refuses_the_value_that_crosses_the_budget(small):
    assert decoded_or_error([[None] * 4, 1, 2])[1] == [[None] * 4, 1, 2]
    wire, value = decoded_or_error([[None] * 4, 1, 2, 3])
    assert wire.startswith(b"E") and "too many elements" in value


def test_decoder_header_check_is_exact(small):
    # A7 needs 7 more values after itself: 8 in all, so it may still arrive.
    with pytest.raises(IncompleteReply):
        KESPDecoder.decode(b"A7\n")
    # A8 needs 9 in all: refused on the header, not left waiting.
    with pytest.raises(ReplyProtocolError, match="too many elements"):
        KESPDecoder.decode(b"A8\n")


def test_encoder_counts_a_dict_nested_in_a_list(small):
    # [dict of 3, None] = 1 + (1 + 6) + 1 = 9 values; header checks alone pass it.
    wire, value = decoded_or_error([{"a": 0, "b": 0, "c": 0}, None])
    assert wire.startswith(b"E") and "too many elements" in value
    # Without the trailing None it is 8 values and goes through.
    assert decoded_or_error([{"a": 0, "b": 0, "c": 0}])[1] == [["a", 0, "b", 0, "c", 0]]
