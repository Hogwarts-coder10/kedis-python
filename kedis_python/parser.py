import re


class ProtocolError(list):
    """What CommandParser.parse() returns, in place of a token list, when the
    bytes are not valid KESP.

    It is a list shaped ["ERROR", message] so older callers that look at
    tokens[0] / tokens[1] keep working, but callers should test for the type:
    a client can send a *command* named ERROR, and that must stay an ordinary
    command rather than being mistaken for a framing failure.
    """

    def __init__(self, message: str):
        super().__init__(["ERROR", message])
        self.message = message


class CommandParser:
    # --- Protocol hardening limits, mirroring the guards real Redis puts
    # on its own wire protocol. Without these, a client can make the
    # server buffer network input without limit — either by never
    # sending a terminator, or by declaring an absurd array/string
    # length and then slow-drip feeding bytes forever.
    MAX_INLINE_LENGTH = 64 * 1024  # cap on an inline command line
    MAX_HEADER_LINE = 32  # more than enough for "A123456\n" / "S123456\n"
    MAX_ARRAY_LENGTH = 1024 * 1024  # cap on a declared element count
    MAX_BULK_LENGTH = 512 * 1024 * 1024  # cap on a single declared string length

    # Header numbers are canonical ASCII decimals: no sign, no spaces, no
    # underscores, no leading zeros, no non-ASCII digits (all of which int()
    # would quietly accept).
    _COUNT = re.compile(rb"(?:0|[1-9][0-9]*)")

    @staticmethod
    def parse(raw_data: bytes):
        """
        Parses incoming network data. Returns (tokens, bytes_consumed).

        - ([], 0): the next frame is incomplete, wait for more data.
        - (tokens, n): one complete command; n bytes belong to it (and to any
          blank frames in front of it).
        - ([], n) with n > 0: only blank frames (empty line / empty array),
          which carry no command. The caller must drop n bytes.
        - (ProtocolError, n): the stream is not valid KESP. It cannot be
          resynchronised, so the caller should reply and close the connection.
        """
        pos = 0
        while pos < len(raw_data):
            tokens, end = CommandParser._parse_frame(raw_data, pos)
            if tokens is None:  # frame incomplete
                return [], pos  # pos > 0 only if blank frames were skipped
            if isinstance(tokens, ProtocolError):
                return tokens, end
            if tokens:
                # Only the command name is normalised. Every other token is data
                # (keys, values, field names) until a handler says otherwise, so
                # handlers match their own options case-insensitively.
                tokens[0] = tokens[0].upper()
                return tokens, end
            pos = end  # blank frame: skip it and keep scanning
        return [], pos

    @staticmethod
    def _count(raw_data: bytes, start: int, end: int):
        """The canonical decimal in raw_data[start:end], or None."""
        field = raw_data[start:end]
        if not CommandParser._COUNT.fullmatch(field):
            return None
        return int(field)

    @staticmethod
    def _parse_frame(raw_data: bytes, pos: int):
        """Parses one frame starting at pos. Returns (tokens, end), where
        tokens is a list (possibly empty), a ProtocolError, or None when the
        frame is incomplete. For a ProtocolError, end is len(raw_data)."""
        total = len(raw_data)

        def fail(message):
            return ProtocolError(message), total

        try:
            first_byte = raw_data[pos : pos + 1].decode("utf-8")
        except UnicodeDecodeError:
            return fail("-ERR Malformed payload")

        # Inline fallback — newline-terminated, exactly like Redis's own
        # inline command mode. This is what protects it against
        # fragmentation: a command split across two TCP packets is NOT
        # treated as complete until its terminator actually arrives.
        if first_byte != "A":
            nl_index = raw_data.find(b"\n", pos)

            if nl_index == -1:
                # No terminator yet — bound how long we'll wait, so a
                # client that never sends '\n' can't grow the intake
                # buffer without limit.
                if total - pos > CommandParser.MAX_INLINE_LENGTH:
                    return fail("-ERR Protocol error: too big inline request")
                return None, pos  # incomplete, wait for the newline

            if nl_index - pos > CommandParser.MAX_INLINE_LENGTH:
                return fail("-ERR Protocol error: too big inline request")

            try:
                tokens = raw_data[pos:nl_index].decode("utf-8").strip().split()
            except UnicodeDecodeError:
                return ProtocolError("-ERR Invalid text encoding"), nl_index + 1
            return tokens, nl_index + 1

        # KESP Decoder: Strict, Binary-safe byte counting
        try:
            nl_index = raw_data.find(b"\n", pos)

            if nl_index == -1:
                if total - pos > CommandParser.MAX_HEADER_LINE:
                    return fail("-ERR Protocol error: invalid array header")
                return None, pos  # Incomplete array header, wait for more bytes

            if nl_index - pos > CommandParser.MAX_HEADER_LINE:
                return fail("-ERR Protocol error: invalid array header")

            expected_args = CommandParser._count(raw_data, pos + 1, nl_index)
            if expected_args is None or expected_args > CommandParser.MAX_ARRAY_LENGTH:
                return fail("-ERR Protocol error: invalid multibulk length")

            pointer = nl_index + 1
            tokens = []

            for _ in range(expected_args):
                if pointer >= total:
                    return None, pos  # next element header not here yet

                # Reject a wrong sigil as soon as it arrives, not after a newline.
                if raw_data[pointer : pointer + 1] != b"S":
                    return fail("-ERR Protocol Desync: Expected 'S'")

                nl_idx = raw_data.find(b"\n", pointer)
                if nl_idx == -1:
                    # Same reasoning as the inline cap: a client that
                    # never terminates a bulk-string header shouldn't
                    # be able to buffer forever either.
                    if total - pointer > CommandParser.MAX_HEADER_LINE:
                        return fail("-ERR Protocol error: invalid bulk length")
                    return None, pos  # incomplete string header

                if nl_idx - pointer > CommandParser.MAX_HEADER_LINE:
                    return fail("-ERR Protocol error: invalid bulk length")

                str_len = CommandParser._count(raw_data, pointer + 1, nl_idx)
                if str_len is None or str_len > CommandParser.MAX_BULK_LENGTH:
                    return fail("-ERR Protocol error: invalid bulk length")

                pointer = nl_idx + 1
                payload_end = pointer + str_len

                # Check if the full string + trailing newline has arrived yet
                if payload_end + 1 > total:
                    return None, pos  # incomplete payload, wait for next payload

                if raw_data[payload_end : payload_end + 1] != b"\n":
                    return fail("-ERR Protocol error: bulk string not newline-terminated")

                tokens.append(raw_data[pointer:payload_end].decode("utf-8"))
                pointer = payload_end + 1

            return tokens, pointer

        except (ValueError, IndexError):
            return fail("-ERR Malformed KESP payload")


class BulkString(str):
    """A string that is ALWAYS encoded as a binary-safe bulk string.

    Plain str values that look like a status ("OK", "+QUEUED") or an error
    ("ERROR ...", "-ERR ...") are encoded as protocol statuses/errors. That is
    right for command replies but wrong for user data: a stream entry whose
    value is "ERROR disk full" must reach the client as data. Wrap data in
    BulkString to opt out of that detection.
    """

    __slots__ = ()


class Status(str):
    """A decoded simple status reply ('+OK', '+QUEUED', ...)."""

    __slots__ = ()


class ErrorReply(str):
    """A decoded KESP error reply ('E<message>')."""

    __slots__ = ()


class LegacyError(ErrorReply):
    """A raw '-ERR ...' line, which the server still writes by hand for
    MULTI/WATCH/read-only replies. The text keeps its leading '-'."""

    __slots__ = ()


class IncompleteReply(Exception):
    """The buffer ends in the middle of a reply: read more bytes, retry."""


class ReplyProtocolError(ValueError):
    """The peer sent bytes that are not a valid KESP reply."""


class KESPDecoder:
    """Client-side decoder for server replies (the mirror of KESPEncoder).

    decode(buf) returns (value, bytes_consumed) and raises IncompleteReply
    when the buffer holds only part of a reply, so callers can keep reading
    until a whole reply has arrived. Values:

        None               N         (nil)
        int                I<n>
        Status             +text
        ErrorReply         Etext
        LegacyError        -text     (leading '-' kept)
        BulkString         S<len>    (binary-safe: counted in bytes)
        list               A<n>      (elements may themselves be lists)

    The decoder is strict by default. Header numbers are canonical ASCII
    decimals, 'N' stands alone on its line, a trailing '\\r' is only tolerated
    on '+' and '-' lines (the server writes those by hand as CRLF), and a
    reply that starts with any other byte is a ReplyProtocolError rather than
    a guess. Pass lenient=True to get the old tolerant behaviour (CRLF on any
    line, junk after 'N', plain-text lines from foreign servers returned as
    Status). Loose numbers stay rejected either way. The flag is slated for
    removal in 0.3.
    """

    MAX_DEPTH = 32  # nesting guard, so a hostile reply can't blow the stack
    MAX_LINE = 64 * 1024  # a status/error line longer than this is garbage
    MAX_HEADER_LINE = 32  # I<n> / S<n> / A<n> headers are never longer

    _COUNT = re.compile(rb"(?:0|[1-9][0-9]*)")
    _INTEGER = re.compile(rb"(?:0|-?[1-9][0-9]*)")

    _TEXT_SIGILS = (b"+", b"-", b"E")
    _NUMERIC_SIGILS = (b"I", b"S", b"A")

    @staticmethod
    def decode(buf, pos: int = 0, _depth: int = 0, lenient: bool = False):
        if _depth > KESPDecoder.MAX_DEPTH:
            raise ReplyProtocolError("reply nested too deeply")
        if pos >= len(buf):
            raise IncompleteReply

        sigil = bytes(buf[pos : pos + 1])
        known = sigil == b"N" or sigil in (
            KESPDecoder._TEXT_SIGILS + KESPDecoder._NUMERIC_SIGILS
        )
        if not known and not lenient:
            # Fail on the first byte; don't wait for a newline that may never come.
            raise ReplyProtocolError("unexpected reply type byte")

        # Lines that carry free text may be long; everything else is a short header.
        if sigil in KESPDecoder._NUMERIC_SIGILS or sigil == b"N":
            limit = KESPDecoder.MAX_HEADER_LINE
        else:
            limit = KESPDecoder.MAX_LINE

        nl = buf.find(b"\n", pos)
        if nl == -1:
            if len(buf) - pos > limit:
                raise ReplyProtocolError("reply line too long")
            raise IncompleteReply
        if nl - pos > limit:
            raise ReplyProtocolError("reply line too long")

        line = bytes(buf[pos:nl])
        if line.endswith(b"\r") and (lenient or sigil in (b"+", b"-")):
            line = line[:-1]  # the hand-written replies end in \r\n
        after = nl + 1
        rest = line[1:]

        if sigil == b"N":
            if rest and not lenient:
                raise ReplyProtocolError("invalid nil reply")
            return None, after
        if sigil == b"I":
            return KESPDecoder._integer(rest), after
        if sigil == b"+":
            return Status(rest.decode("utf-8", "replace")), after
        if sigil == b"E":
            return ErrorReply(rest.decode("utf-8", "replace")), after
        if sigil == b"-":
            return LegacyError(line.decode("utf-8", "replace")), after

        if sigil == b"S":
            length = KESPDecoder._count(rest)
            if length > CommandParser.MAX_BULK_LENGTH:
                raise ReplyProtocolError("invalid bulk length")
            end = after + length
            if len(buf) < end + 1:
                raise IncompleteReply
            if buf[end : end + 1] != b"\n":
                raise ReplyProtocolError("bulk string not newline-terminated")
            text = bytes(buf[after:end]).decode("utf-8", "replace")
            return BulkString(text), end + 1

        if sigil == b"A":
            count = KESPDecoder._count(rest)
            if count > CommandParser.MAX_ARRAY_LENGTH:
                raise ReplyProtocolError("invalid array length")
            items = []
            for _ in range(count):
                item, after = KESPDecoder.decode(buf, after, _depth + 1, lenient)
                items.append(item)
            return items, after

        # lenient only: a plain text line from an older/foreign server.
        return Status(line.decode("utf-8", "replace")), after

    @staticmethod
    def _count(raw: bytes) -> int:
        """A non-negative canonical ASCII decimal (lengths and counts)."""
        if not KESPDecoder._COUNT.fullmatch(raw):
            raise ReplyProtocolError("invalid number in reply header")
        return int(raw)

    @staticmethod
    def _integer(raw: bytes) -> int:
        """A canonical ASCII decimal, optionally negative (I replies)."""
        if not KESPDecoder._INTEGER.fullmatch(raw):
            raise ReplyProtocolError("invalid number in reply header")
        return int(raw)


class ReplyEncodeError(ValueError):
    """A value cannot be put on the wire as a reply the client would accept."""


class KESPEncoder:
    """Turns the values handlers return into KESP bytes.

    The invariant: whatever encode() returns is a single complete reply that
    the strict KESPDecoder accepts. A value that can't be encoded that way
    (too deep, too large, not valid UTF-8, an unknown type) never raises into
    the server loop and never leaves half a frame on the wire: the whole
    reply becomes one 'E' error line instead.

    Handlers express statuses and errors as magic strings ("OK", "-ERR ..."),
    and that is only honoured for the reply itself. Strings *inside* an array
    or dict are data, so a list item or a field named "ERROR x" can no longer
    turn into an error element. Wrap a top-level data reply in BulkString.
    """

    _STATUSES = ("OK", "+OK", "+QUEUED")
    _ERROR_PREFIXES = ("-ERR", "ERROR")

    @staticmethod
    def encode(data) -> bytes:
        """
        Translates raw Python objects from the router into strict KESP network bytes.
        """
        try:
            return KESPEncoder._encode(data, 0, True)
        except ReplyEncodeError as exc:
            return KESPEncoder._error_line(f"Internal server error: {exc}")

    @staticmethod
    def _error_line(text: str) -> bytes:
        # One physical line: a newline in the text would start a bogus reply.
        text = text.replace("\r", " ").replace("\n", " ")
        return f"E{text}\n".encode("utf-8", "replace")

    @staticmethod
    def _bulk(text: str) -> bytes:
        try:
            raw = text.encode("utf-8")
        except UnicodeEncodeError:
            raise ReplyEncodeError("string is not valid UTF-8") from None
        if len(raw) > CommandParser.MAX_BULK_LENGTH:
            raise ReplyEncodeError("string too large")
        return b"S%d\n%s\n" % (len(raw), raw)

    @staticmethod
    def _encode(data, depth: int, top: bool) -> bytes:
        if depth > KESPDecoder.MAX_DEPTH:
            raise ReplyEncodeError("reply nested too deeply")

        # 1. Null / Missing Data
        if data is None:
            return b"N\n"

        # 2. Booleans are integers in Python; test them first, or they would
        #    be written as 'ITrue'.
        if isinstance(data, bool):
            return b"I1\n" if data else b"I0\n"

        # 3. Integers. Reject what the decoder's header limit would refuse
        #    (and never ask Python to print a gigantic int).
        if isinstance(data, int):
            if data.bit_length() > 128:
                raise ReplyEncodeError("integer out of range")
            line = f"I{int(data)}\n".encode("ascii")
            if len(line) - 1 > KESPDecoder.MAX_HEADER_LINE:
                raise ReplyEncodeError("integer out of range")
            return line

        # 4. Strings & Status Messages
        if isinstance(data, str):
            if isinstance(data, BulkString) or not top:
                return KESPEncoder._bulk(data)

            if data in KESPEncoder._STATUSES:
                val = data if data.startswith("+") else f"+{data}"
                return f"{val}\n".encode("utf-8")

            for prefix in KESPEncoder._ERROR_PREFIXES:
                if data == prefix or data.startswith(prefix + " "):
                    # Only the leading marker goes; the message is kept as is.
                    return KESPEncoder._error_line(data[len(prefix) + 1 :])

            # Otherwise, it's a standard Binary-Safe Bulk String
            return KESPEncoder._bulk(data)

        # 5. Arrays (Lists/Sets)
        if isinstance(data, (list, set, tuple)):
            if len(data) > CommandParser.MAX_ARRAY_LENGTH:
                raise ReplyEncodeError("array too large")
            header = f"A{len(data)}\n".encode("utf-8")
            # Recursively encode every element inside the array
            elements = b"".join(
                KESPEncoder._encode(item, depth + 1, False) for item in data
            )
            return header + elements

        # 6. Dictionaries (Flattens into an alternating Key-Value Array)
        if isinstance(data, dict):
            # A dictionary of 3 items becomes a KESP array of 6 items [k1, v1, k2, v2...]
            if len(data) * 2 > CommandParser.MAX_ARRAY_LENGTH:
                raise ReplyEncodeError("array too large")
            header = f"A{len(data) * 2}\n".encode("utf-8")
            elements = b"".join(
                KESPEncoder._bulk(str(k)) + KESPEncoder._encode(v, depth + 1, False)
                for k, v in data.items()
            )
            return header + elements

        # Fallback
        raise ReplyEncodeError("Unknown return type")
