"""KESPEncoder: edge cases, data-vs-status handling, and the invariant that
everything the encoder emits is accepted by the strict KESPDecoder."""

import random

import pytest

from kedis_python.parser import (
    BulkString,
    CommandParser,
    ErrorReply,
    KESPDecoder,
    KESPEncoder,
    Status,
)

encode = KESPEncoder.encode
decode = KESPDecoder.decode


def is_single_error_line(raw: bytes) -> bool:
    return raw.startswith(b"E") and raw.endswith(b"\n") and raw.count(b"\n") == 1


# --- booleans and integers ---------------------------------------------------------


def test_booleans_encode_as_integers():
    assert encode(True) == b"I1\n"
    assert encode(False) == b"I0\n"
    assert encode([True, False]) == b"A2\nI1\nI0\n"


def test_integers_round_trip_up_to_the_decoders_header_limit():
    biggest = 10**31 - 1  # "I" + 31 digits == MAX_HEADER_LINE
    for n in (0, 1, -1, 2**63, -(2**63), biggest, -(10**30 - 1)):
        raw = encode(n)
        assert decode(raw) == (n, len(raw))


@pytest.mark.parametrize(
    "n",
    [
        pytest.param(10**31, id="32-digits"),
        pytest.param(-(10**30), id="negative-31-digits"),
        pytest.param(10**5000, id="beyond-int-str-limit"),
    ],
)
def test_integers_the_decoder_would_reject_become_an_error_reply(n):
    assert is_single_error_line(encode(n))
    assert is_single_error_line(encode([1, n, 2]))  # the whole reply, not a hole in it


# --- errors and statuses -------------------------------------------------------------


@pytest.mark.parametrize(
    "text, wire",
    [
        ("-ERR boom", b"Eboom\n"),
        ("ERROR boom", b"Eboom\n"),
        ("-ERR", b"E\n"),
        ("ERROR", b"E\n"),
        ("-ERR a ERROR b -ERR c", b"Ea ERROR b -ERR c\n"),  # prefix removed once
        ("ERROR x ERROR y", b"Ex ERROR y\n"),
    ],
)
def test_error_prefix_is_removed_once(text, wire):
    assert encode(text) == wire


def test_newlines_cannot_split_an_error_reply():
    raw = encode("-ERR line1\nline2\r\nline3")
    assert is_single_error_line(raw)
    value, end = decode(raw)
    assert isinstance(value, ErrorReply)
    assert end == len(raw)
    assert "\n" not in value and "\r" not in value


@pytest.mark.parametrize("text", ["ERRORS: 5", "-ERRNO 2", "ERROR:x", "-ERRX", "error x", "-err x"])
def test_lookalikes_are_data_not_errors(text):
    assert decode(encode(text)) == (text, len(encode(text)))
    assert encode(text).startswith(b"S")


@pytest.mark.parametrize("text", ["OK", "+OK", "+QUEUED"])
def test_known_statuses_still_encode_as_statuses(text):
    value, _ = decode(encode(text))
    assert isinstance(value, Status)


def test_other_plus_strings_are_data():
    assert encode("+OK extra").startswith(b"S")


def test_top_level_error_and_status_detection_is_unchanged_for_replies():
    assert encode("-ERR unknown command 'X'") == b"Eunknown command 'X'\n"
    assert encode("OK") == b"+OK\n"


# --- nested strings are always data ----------------------------------------------------


def test_strings_inside_arrays_are_always_bulk():
    raw = encode(["OK", "ERROR x", "-ERR y", "+QUEUED"])
    assert raw == b"A4\nS2\nOK\nS7\nERROR x\nS6\n-ERR y\nS7\n+QUEUED\n"


def test_dict_keys_and_values_are_always_bulk():
    raw = encode({"OK": "ERROR x", "ERROR": "-ERR y"})
    assert raw == b"A4\nS2\nOK\nS7\nERROR x\nS5\nERROR\nS6\n-ERR y\n"


def test_non_string_dict_keys_become_bulk_strings():
    assert encode({1: 2}) == b"A2\nS1\n1\nI2\n"


# --- whatever can't be encoded cleanly becomes one error reply -------------------------------


def _nest(levels):
    v = None
    for _ in range(levels):
        v = [v]
    return v


def test_nesting_is_limited_to_what_the_decoder_accepts():
    ok = encode(_nest(KESPDecoder.MAX_DEPTH))
    assert decode(ok)[1] == len(ok)
    assert is_single_error_line(encode(_nest(KESPDecoder.MAX_DEPTH + 1)))


def test_cyclic_structures_do_not_recurse_forever():
    loop = []
    loop.append(loop)
    assert is_single_error_line(encode(loop))


def test_oversized_arrays_are_rejected_before_any_element_is_encoded():
    assert is_single_error_line(encode([None] * (CommandParser.MAX_ARRAY_LENGTH + 1)))
    assert is_single_error_line(
        encode(dict.fromkeys(range(CommandParser.MAX_ARRAY_LENGTH // 2 + 1)))
    )


def test_oversized_strings_are_rejected(monkeypatch):
    monkeypatch.setattr(CommandParser, "MAX_BULK_LENGTH", 10)
    assert encode(BulkString("x" * 10)) == b"S10\n" + b"x" * 10 + b"\n"
    assert is_single_error_line(encode(BulkString("x" * 11)))


@pytest.mark.parametrize("value", ["\udc80", BulkString("\ud800"), ["fine", "\ud800"]])
def test_strings_that_are_not_valid_utf8_do_not_raise(value):
    assert is_single_error_line(encode(value))


def test_unknown_types_become_an_error_reply():
    assert encode(1.5) == b"EInternal server error: Unknown return type\n"
    assert encode(["ok", 1.5]) == b"EInternal server error: Unknown return type\n"


# --- everything the encoder emits, the strict decoder accepts ---------------------------------

ALPHABET = ["", "a", "OK", "ERROR x", "-ERR y", "+QUEUED", "A1", "S3", "\n", "\r\n", "line1\nline2",
            "h\u00e9llo", "\u2713", " ", "N", "I5", "ERRORS", "x" * 40]


def _random_value(rng, depth=0):
    kind = rng.choice(["none", "int", "str", "str", "list", "dict"] if depth < 4 else ["none", "int", "str"])
    if kind == "none":
        return None
    if kind == "int":
        return rng.choice([0, 1, -1, 7, 10**12, -(10**12), 2**63 - 1, rng.randint(-10**9, 10**9)])
    if kind == "str":
        return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 3)))
    if kind == "list":
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {rng.choice(ALPHABET) + str(i): _random_value(rng, depth + 1) for i in range(rng.randint(0, 3))}


def _expected(value):
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in (str(k), _expected(v))]
    if isinstance(value, list):
        return [_expected(v) for v in value]
    return value


def test_encoder_output_always_decodes_to_the_same_data():
    rng = random.Random(20260618)
    for case in range(400):
        value = [_random_value(rng)]  # a container, so nothing is a top-level reply
        raw = encode(value)
        got, end = decode(raw)
        assert end == len(raw), (case, value)
        assert got == _expected(value), (case, value)
        for cut in range(len(raw)):
            with pytest.raises(Exception) as info:
                decode(raw[:cut])
            assert type(info.value).__name__ == "IncompleteReply", (case, cut)
