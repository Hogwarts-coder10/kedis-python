"""On-disk record format for the append-only file (AOF).

Every logged command is one KESP-framed record, the same shape the wire
protocol uses for commands:

    A<count>\\n  then, for each token:  S<length>\\n<payload>\\n

Lengths count bytes of the UTF-8 payload, so a token can hold anything: spaces,
newlines, tabs, an empty string, text that looks like another command. The old
format joined tokens with spaces on one line, so a value could not survive a
restart and a value containing a newline could plant commands in the log.

Strings are encoded with "surrogatepass", so every Python str round-trips
exactly, lone surrogates included.

Files written by older versions hold one whitespace-separated command per line.
scan() still reads those, line by line, and a file may mix both: old lines
first, framed records after.
"""

import re

_COUNT = re.compile(rb"(?:0|[1-9][0-9]*)")
MAX_HEADER_LINE = 32  # "A<n>" / "S<n>" lines are never longer than this


def encode_record(tokens) -> bytes:
    """One command as a framed record."""
    parts = [b"A%d\n" % len(tokens)]
    for token in tokens:
        raw = str(token).encode("utf-8", "surrogatepass")
        parts.append(b"S%d\n" % len(raw))
        parts.append(raw)
        parts.append(b"\n")
    return b"".join(parts)


class ScanResult:
    """How a scan ended.

    status:   "ok"      every byte was consumed
              "torn"    the file ends in the middle of a record (a crash while
                        writing); everything before good_end is intact
              "corrupt" a record is malformed; see reason
    good_end: offset just past the last complete record or line
    """

    __slots__ = ("good_end", "reason", "status")

    def __init__(self):
        self.status = "ok"
        self.good_end = 0
        self.reason = ""


def _count(buf, start: int, end: int):
    field = bytes(buf[start:end])
    return int(field) if _COUNT.fullmatch(field) else None


def scan(buf, result: ScanResult):
    """Yields (tokens, end_offset) for each command in buf, a bytes-like object
    that supports find() and slicing (bytes or mmap). Stops at the first
    incomplete or malformed record and says why in result."""
    total = len(buf)
    pos = 0

    def stop(status: str, reason: str):
        result.status = status
        result.reason = reason

    while pos < total:
        if buf[pos : pos + 1] == b"A":
            following = buf[pos + 1 : pos + 2]
            if following == b"":
                return stop("torn", "file ends inside a record header")
            # A legacy command word starts "A" + a letter (AUTH, ...). Anything
            # else after the "A" is a record header and must validate as one.
            framed = not following.isalpha()
        else:
            framed = False

        if not framed:
            # Legacy line: whitespace-separated tokens, as older versions wrote.
            nl = buf.find(b"\n", pos)
            line_end = total if nl == -1 else nl
            next_pos = total if nl == -1 else nl + 1
            tokens = bytes(buf[pos:line_end]).decode("utf-8", "replace").split()
            result.good_end = next_pos
            if tokens:
                yield tokens, next_pos
            pos = next_pos
            continue

        nl = buf.find(b"\n", pos)
        if nl == -1:
            if total - pos > MAX_HEADER_LINE:
                return stop("corrupt", "array header too long")
            return stop("torn", "file ends inside a record header")
        if nl - pos > MAX_HEADER_LINE:
            return stop("corrupt", "array header too long")
        count = _count(buf, pos + 1, nl)
        if count is None:
            return stop("corrupt", "invalid array header")

        cursor = nl + 1
        tokens = []
        for _ in range(count):
            if cursor >= total:
                return stop("torn", "file ends between tokens")
            if buf[cursor : cursor + 1] != b"S":
                return stop("corrupt", "expected a string header")
            nl = buf.find(b"\n", cursor)
            if nl == -1:
                if total - cursor > MAX_HEADER_LINE:
                    return stop("corrupt", "string header too long")
                return stop("torn", "file ends inside a string header")
            if nl - cursor > MAX_HEADER_LINE:
                return stop("corrupt", "string header too long")
            length = _count(buf, cursor + 1, nl)
            if length is None:
                return stop("corrupt", "invalid string length")
            start = nl + 1
            end = start + length
            if end + 1 > total:
                return stop("torn", "file ends inside a string")
            if buf[end : end + 1] != b"\n":
                return stop("corrupt", "string is not newline-terminated")
            try:
                tokens.append(bytes(buf[start:end]).decode("utf-8", "surrogatepass"))
            except UnicodeDecodeError:
                return stop("corrupt", "string is not valid UTF-8")
            cursor = end + 1

        result.good_end = cursor
        if tokens:
            yield tokens, cursor
        pos = cursor
