import re

from .textcodec import to_bytes, to_text


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
    MAX_COMMAND_BYTES = 512 * 1024 * 1024  # cap on all payload bytes in one command

    # Header numbers are canonical ASCII decimals: no sign, no spaces, no
    # underscores, no leading zeros, no non-ASCII digits (all of which int()
    # would quietly accept).
    _COUNT = re.compile(rb"(?:0|[1-9][0-9]*)")

    @staticmethod
    def parse(raw_data, start: int = 0):
        """
        Parses incoming network data. Returns (tokens, bytes_consumed).

        - ([], 0): the next frame is incomplete, wait for more data.
        - (tokens, n): one complete command; n bytes belong to it (and to any
          blank frames in front of it).
        - ([], n) with n > 0: only blank frames (empty line / empty array),
          which carry no command. The caller must drop n bytes.
        - (ProtocolError, n): the stream is not valid KESP. It cannot be
          resynchronised, so the caller should reply and close the connection.

        raw_data may be bytes or a bytearray. Parsing starts at offset start and
        bytes_consumed counts from there, so a caller can work through a buffer
        holding many pipelined commands without copying or trimming it each time.
        """
        tokens, consumed, _ = CommandParser.parse_buffer(raw_data, start)
        return tokens, consumed

    @staticmethod
    def parse_buffer(raw_data, start: int = 0):
        """parse(), plus a hint for incomplete frames: returns (tokens, consumed,
        need). When the next frame is incomplete, need is the length the bytes
        from start + consumed must reach before parsing can make any progress
        (a half-received string payload is the usual case), so the caller can
        keep reading without re-parsing on every chunk. need is 0 otherwise."""
        total = len(raw_data)
        pos = start
        while pos < total:
            hint = [0]
            tokens, end = CommandParser._parse_frame(raw_data, pos, hint)
            if tokens is None:  # frame incomplete
                return [], pos - start, max(hint[0] - pos, 1)
            if isinstance(tokens, ProtocolError):
                return tokens, end - start, 0
            if tokens:
                # Only the command name is normalised. Every other token is data
                # (keys, values, field names) until a handler says otherwise, so
                # handlers match their own options case-insensitively.
                tokens[0] = tokens[0].upper()
                return tokens, end - start, 0
            pos = end  # blank frame: skip it and keep scanning
        return [], pos - start, 0

    @staticmethod
    def _count(raw_data: bytes, start: int, end: int):
        """The canonical decimal in raw_data[start:end], or None."""
        field = raw_data[start:end]
        if not CommandParser._COUNT.fullmatch(field):
            return None
        return int(field)

    @staticmethod
    def _parse_frame(raw_data, pos: int, hint: list):
        """Parses one frame starting at pos. Returns (tokens, end), where
        tokens is a list (possibly empty), a ProtocolError, or None when the
        frame is incomplete (then hint[0] is set to the buffer length at which it
        could progress). For a ProtocolError, end is len(raw_data)."""
        total = len(raw_data)

        def fail(message):
            return ProtocolError(message), total

        # Inline fallback — newline-terminated, exactly like Redis's own
        # inline command mode. This is what protects it against
        # fragmentation: a command split across two TCP packets is NOT
        # treated as complete until its terminator actually arrives.
        if raw_data[pos : pos + 1] != b"A":
            nl_index = raw_data.find(b"\n", pos)

            if nl_index == -1:
                # No terminator yet — bound how long we'll wait, so a
                # client that never sends '\n' can't grow the intake
                # buffer without limit.
                if total - pos > CommandParser.MAX_INLINE_LENGTH:
                    return fail("-ERR Protocol error: too big inline request")
                hint[0] = total + 1
                return None, pos  # incomplete, wait for the newline

            if nl_index - pos > CommandParser.MAX_INLINE_LENGTH:
                return fail("-ERR Protocol error: too big inline request")

            return to_text(raw_data[pos:nl_index]).strip().split(), nl_index + 1

        # KESP Decoder: Strict, Binary-safe byte counting
        try:
            nl_index = raw_data.find(b"\n", pos)

            if nl_index == -1:
                if total - pos > CommandParser.MAX_HEADER_LINE:
                    return fail("-ERR Protocol error: invalid array header")
                hint[0] = total + 1
                return None, pos  # Incomplete array header, wait for more bytes

            if nl_index - pos > CommandParser.MAX_HEADER_LINE:
                return fail("-ERR Protocol error: invalid array header")

            expected_args = CommandParser._count(raw_data, pos + 1, nl_index)
            if expected_args is None or expected_args > CommandParser.MAX_ARRAY_LENGTH:
                return fail("-ERR Protocol error: invalid multibulk length")

            pointer = nl_index + 1
            tokens = []
            declared = 0  # payload bytes this command has asked for so far

            for _ in range(expected_args):
                if pointer >= total:
                    hint[0] = total + 1
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
                    hint[0] = total + 1
                    return None, pos  # incomplete string header

                if nl_idx - pointer > CommandParser.MAX_HEADER_LINE:
                    return fail("-ERR Protocol error: invalid bulk length")

                str_len = CommandParser._count(raw_data, pointer + 1, nl_idx)
                if str_len is None or str_len > CommandParser.MAX_BULK_LENGTH:
                    return fail("-ERR Protocol error: invalid bulk length")

                declared += str_len
                if declared > CommandParser.MAX_COMMAND_BYTES:
                    return fail("-ERR Protocol error: command too large")

                pointer = nl_idx + 1
                payload_end = pointer + str_len

                # Check if the full string + trailing newline has arrived yet
                if payload_end + 1 > total:
                    hint[0] = payload_end + 1
                    return None, pos  # incomplete payload, wait for next payload

                if raw_data[payload_end : payload_end + 1] != b"\n":
                    return fail("-ERR Protocol error: bulk string not newline-terminated")

                tokens.append(to_text(raw_data[pointer:payload_end]))
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
    """The buffer ends in the middle of a reply: read more bytes, retry.

    need is the buffer length (counted from the start of the buffer decode() was
    given) at which retrying could make progress, so a caller waiting for a large
    string need not decode again after every chunk. 0 means unknown."""

    def __init__(self, need: int = 0):
        super().__init__(need)
        self.need = need


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
    # Whole-reply budgets. MAX_DEPTH and the per-array/per-string caps bound one
    # node; these bound the *sum* over a reply, so many small nodes cannot add up
    # to an unbounded amount of memory or work. Looked up at call time.
    MAX_REPLY_ELEMENTS = 4_194_304  # every value in the reply, containers included
    MAX_REPLY_PAYLOAD = 1024 * 1024 * 1024  # string and text-line bytes, summed

    _COUNT = re.compile(rb"(?:0|[1-9][0-9]*)")
    _INTEGER = re.compile(rb"(?:0|-?[1-9][0-9]*)")

    _TEXT_SIGILS = (b"+", b"-", b"E")
    _NUMERIC_SIGILS = (b"I", b"S", b"A")

    @staticmethod
    def decode(
        buf, pos: int = 0, _depth: int = 0, lenient: bool = False, _budget=None
    ):
        if _budget is None:
            # [elements left, payload bytes left], shared by the whole reply.
            _budget = [KESPDecoder.MAX_REPLY_ELEMENTS, KESPDecoder.MAX_REPLY_PAYLOAD]
        if _depth > KESPDecoder.MAX_DEPTH:
            raise ReplyProtocolError("reply nested too deeply")
        if pos >= len(buf):
            raise IncompleteReply(pos + 1)

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
            raise IncompleteReply(len(buf) + 1)
        if nl - pos > limit:
            raise ReplyProtocolError("reply line too long")

        line = bytes(buf[pos:nl])
        if line.endswith(b"\r") and (lenient or sigil in (b"+", b"-")):
            line = line[:-1]  # the hand-written replies end in \r\n
        after = nl + 1
        rest = line[1:]

        # Charge this value to the whole-reply budget. A reply that is still
        # incomplete never gets here, so a retry is not charged twice.
        if _budget[0] <= 0:
            raise ReplyProtocolError("reply has too many elements")
        _budget[0] -= 1
        if sigil not in (b"N", b"I", b"S", b"A"):  # line-carried text
            if len(line) > _budget[1]:
                raise ReplyProtocolError("reply payload too large")
            _budget[1] -= len(line)

        if sigil == b"N":
            if rest and not lenient:
                raise ReplyProtocolError("invalid nil reply")
            return None, after
        if sigil == b"I":
            return KESPDecoder._integer(rest), after
        if sigil == b"+":
            return Status(to_text(rest)), after
        if sigil == b"E":
            return ErrorReply(to_text(rest)), after
        if sigil == b"-":
            return LegacyError(to_text(line)), after

        if sigil == b"S":
            length = KESPDecoder._count(rest)
            if length > CommandParser.MAX_BULK_LENGTH:
                raise ReplyProtocolError("invalid bulk length")
            if length > _budget[1]:
                # Refuse on the header, before waiting for the bytes to arrive.
                raise ReplyProtocolError("reply payload too large")
            _budget[1] -= length
            end = after + length
            if len(buf) < end + 1:
                raise IncompleteReply(end + 1)
            if buf[end : end + 1] != b"\n":
                raise ReplyProtocolError("bulk string not newline-terminated")
            return BulkString(to_text(buf[after:end])), end + 1

        if sigil == b"A":
            count = KESPDecoder._count(rest)
            if count > CommandParser.MAX_ARRAY_LENGTH:
                raise ReplyProtocolError("invalid array length")
            if count > _budget[0]:
                # Every element costs at least one; refuse on the header.
                raise ReplyProtocolError("reply has too many elements")
            items = []
            for _ in range(count):
                item, after = KESPDecoder.decode(
                    buf, after, _depth + 1, lenient, _budget
                )
                items.append(item)
            return items, after

        # lenient only: a plain text line from an older/foreign server.
        return Status(to_text(line)), after

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
        # The same whole-reply budgets the decoder enforces, so a reply that
        # leaves the server is one the client will accept.
        budget = [KESPDecoder.MAX_REPLY_ELEMENTS, KESPDecoder.MAX_REPLY_PAYLOAD]
        try:
            return KESPEncoder._encode(data, 0, True, budget)
        except ReplyEncodeError as exc:
            return KESPEncoder._error_line(f"Internal server error: {exc}")

    @staticmethod
    def encode_command(tokens) -> bytes:
        """A command as the KESP array a server reads. Binary-safe: every token
        is its exact bytes, whatever it holds."""
        parts = [b"A%d\n" % len(tokens)]
        for token in tokens:
            raw = to_bytes(str(token))
            parts += [b"S%d\n" % len(raw), raw, b"\n"]
        return b"".join(parts)

    @staticmethod
    def _error_line(text: str) -> bytes:
        # One physical line: a newline in the text would start a bogus reply.
        text = text.replace("\r", " ").replace("\n", " ")
        try:
            raw = to_bytes(text)
        except UnicodeEncodeError:
            raw = text.encode("utf-8", "replace")
        return b"E" + raw + b"\n"

    @staticmethod
    def _bulk(text: str, budget=None) -> bytes:
        try:
            raw = to_bytes(text)
        except UnicodeEncodeError:
            raise ReplyEncodeError("string is not valid UTF-8") from None
        if len(raw) > CommandParser.MAX_BULK_LENGTH:
            raise ReplyEncodeError("string too large")
        if budget is not None:
            if len(raw) > budget[1]:
                raise ReplyEncodeError("reply payload too large")
            budget[1] -= len(raw)
        return b"S%d\n%s\n" % (len(raw), raw)

    @staticmethod
    def _encode(data, depth: int, top: bool, budget) -> bytes:
        if depth > KESPDecoder.MAX_DEPTH:
            raise ReplyEncodeError("reply nested too deeply")
        # One element per value, containers included (as the decoder counts).
        if budget[0] <= 0:
            raise ReplyEncodeError("reply has too many elements")
        budget[0] -= 1

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
                return KESPEncoder._bulk(data, budget)

            if data in KESPEncoder._STATUSES:
                val = data if data.startswith("+") else f"+{data}"
                return f"{val}\n".encode("utf-8")

            for prefix in KESPEncoder._ERROR_PREFIXES:
                if data == prefix or data.startswith(prefix + " "):
                    # Only the leading marker goes; the message is kept as is.
                    return KESPEncoder._error_line(data[len(prefix) + 1 :])

            # Otherwise, it's a standard Binary-Safe Bulk String
            return KESPEncoder._bulk(data, budget)

        # 5. Arrays (Lists/Sets)
        if isinstance(data, (list, set, tuple)):
            if len(data) > CommandParser.MAX_ARRAY_LENGTH:
                raise ReplyEncodeError("array too large")
            if len(data) > budget[0]:
                raise ReplyEncodeError("reply has too many elements")
            header = f"A{len(data)}\n".encode("utf-8")
            # Recursively encode every element inside the array
            elements = b"".join(
                KESPEncoder._encode(item, depth + 1, False, budget) for item in data
            )
            return header + elements

        # 6. Dictionaries (Flattens into an alternating Key-Value Array)
        if isinstance(data, dict):
            # A dictionary of 3 items becomes a KESP array of 6 items [k1, v1, k2, v2...]
            if len(data) * 2 > CommandParser.MAX_ARRAY_LENGTH:
                raise ReplyEncodeError("array too large")
            if len(data) * 2 > budget[0]:
                raise ReplyEncodeError("reply has too many elements")
            header = f"A{len(data) * 2}\n".encode("utf-8")
            parts = []
            for k, v in data.items():
                if budget[0] <= 0:
                    raise ReplyEncodeError("reply has too many elements")
                budget[0] -= 1  # the key
                parts.append(KESPEncoder._bulk(str(k), budget))
                parts.append(KESPEncoder._encode(v, depth + 1, False, budget))
            elements = b"".join(parts)
            return header + elements

        # Fallback
        raise ReplyEncodeError("Unknown return type")
