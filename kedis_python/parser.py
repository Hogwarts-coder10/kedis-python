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
    """

    MAX_DEPTH = 32  # nesting guard, so a hostile reply can't blow the stack
    MAX_LINE = 64 * 1024  # a header/status line longer than this is garbage

    @staticmethod
    def decode(buf, pos: int = 0, _depth: int = 0):
        if _depth > KESPDecoder.MAX_DEPTH:
            raise ReplyProtocolError("reply nested too deeply")
        if pos >= len(buf):
            raise IncompleteReply

        nl = buf.find(b"\n", pos)
        if nl == -1:
            if len(buf) - pos > KESPDecoder.MAX_LINE:
                raise ReplyProtocolError("reply line too long")
            raise IncompleteReply

        line = bytes(buf[pos:nl])
        if line.endswith(b"\r"):  # the hand-written replies end in \r\n
            line = line[:-1]
        after = nl + 1
        sigil, rest = line[:1], line[1:]

        if sigil == b"N":
            return None, after
        if sigil == b"I":
            return KESPDecoder._number(rest), after
        if sigil == b"+":
            return Status(rest.decode("utf-8", "replace")), after
        if sigil == b"E":
            return ErrorReply(rest.decode("utf-8", "replace")), after
        if sigil == b"-":
            return LegacyError(line.decode("utf-8", "replace")), after

        if sigil == b"S":
            length = KESPDecoder._number(rest)
            if length < 0 or length > CommandParser.MAX_BULK_LENGTH:
                raise ReplyProtocolError("invalid bulk length")
            end = after + length
            if len(buf) < end + 1:
                raise IncompleteReply
            if buf[end : end + 1] != b"\n":
                raise ReplyProtocolError("bulk string not newline-terminated")
            text = bytes(buf[after:end]).decode("utf-8", "replace")
            return BulkString(text), end + 1

        if sigil == b"A":
            count = KESPDecoder._number(rest)
            if count < 0 or count > CommandParser.MAX_ARRAY_LENGTH:
                raise ReplyProtocolError("invalid array length")
            items = []
            for _ in range(count):
                item, after = KESPDecoder.decode(buf, after, _depth + 1)
                items.append(item)
            return items, after

        # Anything else is a plain text line from an older/foreign server.
        return Status(line.decode("utf-8", "replace")), after

    @staticmethod
    def _number(raw: bytes) -> int:
        try:
            return int(raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            raise ReplyProtocolError("invalid number in reply header") from None


class KESPEncoder:
    @staticmethod
    def encode(data) -> bytes:
        """
        Translates raw Python objects from the router into strict KESP network bytes.
        """
        # 1. Null / Missing Data
        if data is None:
            return b"N\n"

        # 2. Integers
        elif isinstance(data, int):
            return f"I{data}\n".encode("utf-8")

        # 3. Strings & Status Messages
        elif isinstance(data, str):
            if isinstance(data, BulkString):
                encoded_str = data.encode("utf-8")
                return f"S{len(encoded_str)}\n".encode("utf-8") + encoded_str + b"\n"

            # Check for simple protocol statuses
            if data in ["OK", "+OK", "+QUEUED"]:
                val = data if data.startswith("+") else f"+{data}"
                return f"{val}\n".encode("utf-8")

            # Check for errors
            elif data.startswith("-ERR") or data.startswith("ERROR"):
                clean_err = data.replace("-ERR ", "").replace("ERROR ", "")
                return f"E{clean_err}\n".encode("utf-8")

            # Otherwise, it's a standard Binary-Safe Bulk String
            else:
                encoded_str = data.encode("utf-8")
                return f"S{len(encoded_str)}\n".encode("utf-8") + encoded_str + b"\n"

        # 4. Arrays (Lists/Sets)
        elif isinstance(data, (list, set, tuple)):
            header = f"A{len(data)}\n".encode("utf-8")
            # Recursively encode every element inside the array
            elements = b"".join(KESPEncoder.encode(item) for item in data)
            return header + elements

        # 🚀 5. Dictionaries (Flattens into an alternating Key-Value Array)
        elif isinstance(data, dict):
            # A dictionary of 3 items becomes a KESP array of 6 items [k1, v1, k2, v2...]
            header = f"A{len(data) * 2}\n".encode("utf-8")
            elements = b"".join(
                KESPEncoder.encode(str(k)) + KESPEncoder.encode(v)
                for k, v in data.items()
            )
            return header + elements

        # Fallback
        else:
            return b"EInternal server error: Unknown return type\n"
