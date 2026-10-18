"""KESPDecoder (client-side reply decoder) strictness and framing."""

import socket

import pytest

from kedis_python.network import NetworkManager
from kedis_python.parser import (
    BulkString,
    CommandParser,
    ErrorReply,
    IncompleteReply,
    KESPDecoder,
    KESPEncoder,
    LegacyError,
    ReplyProtocolError,
    Status,
)

decode = KESPDecoder.decode


def bad(raw: bytes, **kw):
    with pytest.raises(ReplyProtocolError):
        decode(raw, **kw)


# --- well-formed replies -------------------------------------------------------


@pytest.mark.parametrize(
    "raw, value",
    [
        (b"N\n", None),
        (b"I0\n", 0),
        (b"I42\n", 42),
        (b"I-5\n", -5),
        (b"+OK\n", "OK"),
        (b"+OK\r\n", "OK"),  # hand-written replies end in CRLF
        (b"Eboom\n", "boom"),
        (b"-ERR bad\n", "-ERR bad"),
        (b"-ERR bad\r\n", "-ERR bad"),
        (b"S0\n\n", ""),
        (b"S5\nhello\n", "hello"),
        (b"S3\na\nb\n", "a\nb"),
        (b"A0\n", []),
        (b"A2\nI1\nS1\nx\n", [1, "x"]),
        (b"A2\nA1\nN\nA0\n", [[None], []]),
    ],
)
def test_valid_replies(raw, value):
    got, end = decode(raw)
    assert got == value
    assert end == len(raw)


def test_value_types():
    assert isinstance(decode(b"+OK\n")[0], Status)
    assert isinstance(decode(b"Eboom\n")[0], ErrorReply)
    assert isinstance(decode(b"-ERR x\n")[0], LegacyError)
    assert isinstance(decode(b"S1\nx\n")[0], BulkString)


def test_decode_stops_at_the_end_of_one_reply():
    assert decode(b"+OK\nI5\n") == ("OK", 4)
    assert decode(b"+OK\nI5\n", 4) == (5, 7)


# --- numbers ---------------------------------------------------------------------

LOOSE = [b"+5", b"05", b"1_0", b" 5", b"5 ", b"\xd9\xa5", b"-0", b"", b"-", b"0x5", b"5\r"]


@pytest.mark.parametrize("num", LOOSE)
def test_loose_integers_are_rejected(num):
    bad(b"I" + num + b"\n")


@pytest.mark.parametrize("num", LOOSE + [b"-1"])
def test_loose_bulk_lengths_are_rejected(num):
    bad(b"S" + num + b"\nhello\n")


@pytest.mark.parametrize("num", LOOSE + [b"-1"])
def test_loose_array_counts_are_rejected(num):
    bad(b"A" + num + b"\nN\n")


def test_oversized_declared_lengths_are_rejected():
    bad(b"S%d\n" % (CommandParser.MAX_BULK_LENGTH + 1))
    bad(b"A%d\n" % (CommandParser.MAX_ARRAY_LENGTH + 1))


# --- line shape --------------------------------------------------------------------


@pytest.mark.parametrize("raw", [b"Nx\n", b"N \n", b"N\r\n"])
def test_nil_must_be_exactly_n(raw):
    bad(raw)


@pytest.mark.parametrize("raw", [b"I5\r\n", b"S1\r\nx\n", b"A0\r\n"])
def test_cr_is_only_tolerated_on_status_and_legacy_error_lines(raw):
    bad(raw)


@pytest.mark.parametrize("raw", [b"X", b"hello\n", b"\n", b"\r\n", b"OK\r\n", b"i5\n"])
def test_unknown_sigils_are_rejected_without_waiting_for_a_newline(raw):
    bad(raw)


def test_bulk_payload_must_be_followed_by_newline():
    bad(b"S3\nabcX")
    bad(b"S3\nabcdef\n")  # payload longer than declared


def test_error_inside_an_array_propagates():
    bad(b"A2\nI1\nX\n")


# --- limits apply to terminated lines too -------------------------------------------


def test_terminated_numeric_headers_over_limit_are_rejected():
    pad = b"0" * 60
    bad(b"I" + pad + b"1\n")
    bad(b"S" + pad + b"1\nx\n")
    bad(b"A" + pad + b"1\nN\n")


def test_unterminated_numeric_header_over_limit_fails_before_a_newline():
    bad(b"I" + b"9" * 40)
    bad(b"S" + b"9" * 40)


def test_status_line_limit_applies_whether_or_not_it_is_terminated():
    long_line = b"+" + b"x" * (KESPDecoder.MAX_LINE + 1)
    bad(long_line)
    bad(long_line + b"\n")
    ok = b"+" + b"x" * (KESPDecoder.MAX_LINE - 1) + b"\n"
    assert decode(ok)[1] == len(ok)


def test_nesting_depth_is_limited():
    n = KESPDecoder.MAX_DEPTH + 2
    bad(b"A1\n" * n + b"N\n")
    assert decode(b"A1\n" * 5 + b"N\n")[1] == 17


# --- lenient mode (legacy servers; slated for removal in 0.3) ---------------------------


def test_lenient_restores_the_plain_text_fallback():
    assert decode(b"hello there\r\n", lenient=True) == (Status("hello there"), 13)
    assert decode(b"\n", lenient=True) == (Status(""), 1)


def test_lenient_restores_crlf_and_trailing_junk_tolerance():
    assert decode(b"I5\r\n", lenient=True) == (5, 4)
    assert decode(b"N junk\n", lenient=True) == (None, 7)


def test_lenient_still_rejects_loose_numbers():
    bad(b"I+5\n", lenient=True)
    bad(b"S1_0\n", lenient=True)


def test_lenient_applies_inside_arrays():
    assert decode(b"A2\nplain\nI1\n", lenient=True)[0] == ["plain", 1]


# --- incomplete input / fragmentation ------------------------------------------------------

WHOLE = [
    b"N\n",
    b"I-123\n",
    b"+OK\r\n",
    b"Eboom\n",
    b"S11\nline1\nline2\n",
    "S10\nh\u00e9llo \u2713\n".encode(),  # multi-byte UTF-8, cut mid-character
    b"A3\nS1\na\nA2\nI1\nN\nS0\n\n",
]


def test_empty_buffer_is_incomplete():
    with pytest.raises(IncompleteReply):
        decode(b"")


@pytest.mark.parametrize("raw", WHOLE)
def test_every_proper_prefix_is_incomplete_and_the_whole_decodes(raw):
    _, end = decode(raw)
    assert end == len(raw)
    for cut in range(len(raw)):
        with pytest.raises(IncompleteReply):
            decode(raw[:cut])


# --- encoder -> decoder ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        (0, 0),
        (-7, -7),
        ("OK", "OK"),
        ("+QUEUED", "QUEUED"),
        (BulkString("x\ny"), "x\ny"),
        ("plain text", "plain text"),
        ([], []),
        (["a", ["b", None], 3], ["a", ["b", None], 3]),
    ],
)
def test_encoder_output_decodes_back(value, expected):
    raw = KESPEncoder.encode(value)
    got, end = decode(raw)
    assert got == expected
    assert end == len(raw)


# --- NetworkManager closes the connection on a reply protocol error ---------------------------------


def _manager_with_peer():
    ours, theirs = socket.socketpair()
    nm = NetworkManager()
    nm.sock = ours
    return nm, theirs


def test_read_reply_returns_one_reply_and_keeps_the_rest_buffered():
    nm, peer = _manager_with_peer()
    try:
        peer.sendall(b"+OK\nI5\n")
        assert nm.read_reply() == b"+OK\n"
        assert nm.read_reply() == b"I5\n"
    finally:
        peer.close()
        nm.disconnect()


def test_read_reply_closes_the_socket_on_a_protocol_error():
    nm, peer = _manager_with_peer()
    try:
        peer.sendall(b"garbage\n")
        with pytest.raises(ReplyProtocolError):
            nm.read_reply()
        assert nm.sock is None
        assert not nm._rbuf
    finally:
        peer.close()
        nm.disconnect()
