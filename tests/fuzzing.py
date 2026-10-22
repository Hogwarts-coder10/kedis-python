"""Mutation fuzzer and reference readers for the KESP command parser and reply
decoder.

Run it as a script for a long session:

    python tests/fuzzing.py --target all --seed 1 --iterations 200000
    python tests/fuzzing.py --target parser --seed 7 --max-seconds 600 --save-dir fuzz-out

Exit status is 1 if any input broke an invariant. Every failure is shrunk to a
minimal reproduction and printed as a Python bytes literal.

What is checked for each input
------------------------------
* exceptions    : the command parser never raises; the reply decoder raises only
                  IncompleteReply or ReplyProtocolError.
* limits        : each input is handled within a CPU-time and a peak-memory
                  limit (a CPU-time timer stops a runaway on POSIX).
* contract      : consumed is in [0, len]; a protocol error consumes the whole
                  buffer; an incomplete frame comes with a hint that is > 0.
* differential  : an independent reader, written from docs/protocol.md as a
                  byte-at-a-time state machine, must reach the same outcome.
* prefixes      : no strict prefix of a complete frame is complete or an error.
* hints         : adding fewer bytes than the hint asks for never makes
                  progress.
* chunking      : feeding the bytes in random pieces (as the server reads them)
                  gives the same commands as parsing the whole buffer.
* round trips   : encoder output decodes, and encode_command output parses,
                  back to the original values.

The reference readers are deliberately a different shape from the code under
test (a state machine and an explicit stack instead of recursive descent), so
a shared misreading of the spec is less likely to hide in both.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import random
import re
import signal
import sys
import time
import tracemalloc
from dataclasses import dataclass, field

from kedis_python.parser import (
    BulkString,
    CommandParser,
    ErrorReply,
    IncompleteReply,
    KESPDecoder,
    KESPEncoder,
    LegacyError,
    ProtocolError,
    ReplyProtocolError,
    Status,
)
from kedis_python.textcodec import to_bytes, to_text

TARGETS = ("parser", "decoder", "roundtrip")

class Ref:
    """Limits restated from docs/protocol.md, independent of the constants in
    parser.py. Mutable, so small_limits() can shrink them in step with the real ones."""

    header = 32
    inline = 64 * 1024
    array = 1024 * 1024
    bulk = 512 * 1024 * 1024
    command = 512 * 1024 * 1024
    depth = 32
    text_line = 64 * 1024


_CANON = re.compile(rb"(?:0|[1-9][0-9]*)")
_CANON_SIGNED = re.compile(rb"(?:0|-?[1-9][0-9]*)")


# --- reference reader: commands --------------------------------------------------------------


def ref_parse_command(data: bytes):
    """Outcome of parsing data from offset 0, as one of

        ("error", len(data))
        ("command", tokens, consumed)
        ("none", consumed, incomplete)   # blank frames only, or a partial frame

    Written as a state machine that consumes one byte at a time, so the rules
    for "wait" versus "fail" are the ones a streaming reader would apply.
    """
    START, INLINE, AHDR, ELEM, SHDR, PAYLOAD, PAYEND = range(7)
    state = START
    fs = 0  # index where the current frame (or header line) began
    consumed_blank = 0
    tokens: list[str] = []
    remaining = 0
    declared = 0
    payload = bytearray()
    want = 0
    i = 0
    n = len(data)

    def error():
        return ("error", n)

    while i < n:
        b = data[i : i + 1]
        if state == START:
            fs = i
            if b == b"A":
                state = AHDR
            else:
                state = INLINE
                continue  # reprocess this byte as part of the inline line
        elif state == INLINE:
            if b == b"\n":
                if i - fs > Ref.inline:
                    return error()
                toks = to_text(data[fs:i]).strip().split()
                if toks:
                    toks[0] = toks[0].upper()
                    return ("command", tuple(toks), i + 1)
                consumed_blank = i + 1
                state = START
            elif i + 1 - fs > Ref.inline:
                return error()
        elif state == AHDR:
            if b == b"\n":
                if i - fs > Ref.header:
                    return error()
                field_ = data[fs + 1 : i]
                if not _CANON.fullmatch(field_):
                    return error()
                count = int(field_)
                if count > Ref.array:
                    return error()
                if count == 0:
                    consumed_blank = i + 1
                    state = START
                else:
                    remaining, declared, tokens = count, 0, []
                    state = ELEM
            elif i + 1 - fs > Ref.header:
                return error()
        elif state == ELEM:
            if b != b"S":
                return error()
            fs = i
            state = SHDR
        elif state == SHDR:
            if b == b"\n":
                if i - fs > Ref.header:
                    return error()
                field_ = data[fs + 1 : i]
                if not _CANON.fullmatch(field_):
                    return error()
                want = int(field_)
                if want > Ref.bulk:
                    return error()
                declared += want
                if declared > Ref.command:
                    return error()
                payload = bytearray()
                state = PAYEND if want == 0 else PAYLOAD
            elif i + 1 - fs > Ref.header:
                return error()
        elif state == PAYLOAD:
            take = min(want - len(payload), n - i)
            payload += data[i : i + take]
            i += take
            if len(payload) == want:
                state = PAYEND
            continue
        elif state == PAYEND:
            if b != b"\n":
                return error()
            tokens.append(to_text(bytes(payload)))
            remaining -= 1
            if remaining == 0:
                tokens[0] = tokens[0].upper()
                return ("command", tuple(tokens), i + 1)
            state = ELEM
        i += 1

    return ("none", consumed_blank, state != START)


def real_parse_command(data: bytes):
    tokens, consumed, need = CommandParser.parse_buffer(data)
    if isinstance(tokens, ProtocolError):
        return ("error", consumed)
    if tokens:
        return ("command", tuple(tokens), consumed)
    return ("none", consumed, need > 0)


# --- reference reader: replies ---------------------------------------------------------------


def _tag(value):
    """A comparable, type-exact copy of a decoded value."""
    if value is None or type(value) is int:
        return value
    if type(value) is list:
        return [_tag(v) for v in value]
    for cls, name in (
        (BulkString, "bulk"),
        (Status, "status"),
        (LegacyError, "legacy"),
        (ErrorReply, "error"),
    ):
        if type(value) is cls:
            return (name, str(value))
    return ("other", type(value).__name__, repr(value))


def ref_decode_reply(data: bytes):
    """("error",) | ("incomplete",) | ("value", tagged_value, consumed), strict mode,
    iteratively with an explicit stack."""
    n = len(data)
    pos = 0
    stack: list[list] = []  # [remaining, items]
    while True:
        if len(stack) > Ref.depth:
            return ("error",)
        if pos >= n:
            return ("incomplete",)
        sig = data[pos : pos + 1]
        if sig not in (b"N", b"+", b"-", b"E", b"I", b"S", b"A"):
            return ("error",)
        limit = Ref.text_line if sig in (b"+", b"-", b"E") else Ref.header
        nl = data.find(b"\n", pos)
        if nl == -1:
            return ("error",) if n - pos > limit else ("incomplete",)
        if nl - pos > limit:
            return ("error",)
        line = data[pos:nl]
        if sig in (b"+", b"-") and line.endswith(b"\r"):
            line = line[:-1]
        rest = line[1:]
        after = nl + 1
        value = None
        done = True
        if sig == b"N":
            if rest:
                return ("error",)
        elif sig == b"I":
            if not _CANON_SIGNED.fullmatch(rest):
                return ("error",)
            value = int(rest)
        elif sig == b"+":
            value = ("status", to_text(rest))
        elif sig == b"E":
            value = ("error", to_text(rest))
        elif sig == b"-":
            value = ("legacy", to_text(line))
        elif sig == b"S":
            if not _CANON.fullmatch(rest):
                return ("error",)
            length = int(rest)
            if length > Ref.bulk:
                return ("error",)
            end = after + length
            if n < end + 1:
                return ("incomplete",)
            if data[end : end + 1] != b"\n":
                return ("error",)
            value = ("bulk", to_text(data[after:end]))
            after = end + 1
        else:  # A
            if not _CANON.fullmatch(rest):
                return ("error",)
            count = int(rest)
            if count > Ref.array:
                return ("error",)
            if count == 0:
                value = []
            else:
                stack.append([count, []])
                done = False
        pos = after
        if not done:
            continue
        while True:
            if not stack:
                return ("value", value, pos)
            top = stack[-1]
            top[1].append(value)
            top[0] -= 1
            if top[0] == 0:
                value = top[1]
                stack.pop()
                continue
            break


def real_decode_reply(data: bytes):
    try:
        value, end = KESPDecoder.decode(data)
    except IncompleteReply:
        return ("incomplete",)
    except ReplyProtocolError:
        return ("error",)
    return ("value", _tag(value), end)


# Real limits are far beyond anything a few-KiB mutated input reaches, so a bug
# at a boundary (an off-by-one in a cap) would never fire. small_limits() shrinks
# the caps in the code under test and in the reference together, so the same
# fuzzing reaches every boundary.
SMALL = {
    "header": 8,
    "inline": 40,
    "array": 6,
    "bulk": 24,
    "command": 40,
    "depth": 4,
    "text_line": 40,
}


@contextlib.contextmanager
def small_limits():
    patches = [
        (Ref, "header", SMALL["header"]),
        (Ref, "inline", SMALL["inline"]),
        (Ref, "array", SMALL["array"]),
        (Ref, "bulk", SMALL["bulk"]),
        (Ref, "command", SMALL["command"]),
        (Ref, "depth", SMALL["depth"]),
        (Ref, "text_line", SMALL["text_line"]),
        (CommandParser, "MAX_HEADER_LINE", SMALL["header"]),
        (CommandParser, "MAX_INLINE_LENGTH", SMALL["inline"]),
        (CommandParser, "MAX_ARRAY_LENGTH", SMALL["array"]),
        (CommandParser, "MAX_BULK_LENGTH", SMALL["bulk"]),
        (CommandParser, "MAX_COMMAND_BYTES", SMALL["command"]),
        (KESPDecoder, "MAX_HEADER_LINE", SMALL["header"]),
        (KESPDecoder, "MAX_LINE", SMALL["text_line"]),
        (KESPDecoder, "MAX_DEPTH", SMALL["depth"]),
    ]
    saved = [(obj, name, getattr(obj, name)) for obj, name, _ in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)
    try:
        yield
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


# --- failures --------------------------------------------------------------------------------


@dataclass
class Failure:
    target: str
    kind: str  # exception | timeout | memory | contract | differential | prefix | hint | chunking | roundtrip
    detail: str
    data: bytes
    minimized: bytes | None = None

    def signature(self):
        return (self.target, self.kind)

    def __str__(self):
        shown = self.minimized if self.minimized is not None else self.data
        return f"[{self.target}/{self.kind}] {self.detail}\n    input: {shown!r}"


class _Timeout(BaseException):
    pass


@contextlib.contextmanager
def limits(seconds: float, mem_bytes: int | None):
    """Runs the body under a CPU-time and a peak-memory limit.

    CPU time, not wall-clock, so a busy machine does not fail healthy inputs.
    Memory is the tracemalloc peak while the body ran. The time limit is
    enforced with a CPU-time timer (SIGVTALRM) where that is available (POSIX,
    main thread), so an infinite loop is stopped; elsewhere it is only checked
    afterwards.
    """
    use_alarm = (
        hasattr(signal, "setitimer")
        and hasattr(signal, "SIGVTALRM")
        and _in_main_thread()
    )

    def on_alarm(signum, frame):
        raise _Timeout()

    old = None
    if use_alarm:
        old = signal.signal(signal.SIGVTALRM, on_alarm)
        signal.setitimer(signal.ITIMER_VIRTUAL, seconds)
    tracking = mem_bytes is not None
    if tracking:
        if not tracemalloc.is_tracing():
            tracemalloc.start()
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
    start = time.process_time()
    try:
        yield
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_VIRTUAL, 0)
            signal.signal(signal.SIGVTALRM, old)
    elapsed = time.process_time() - start
    if elapsed > seconds:
        raise _Timeout()
    if tracking:
        peak = tracemalloc.get_traced_memory()[1] - base
        if peak > mem_bytes:
            raise MemoryError(f"peak {peak} bytes over limit {mem_bytes}")


def _in_main_thread() -> bool:
    import threading

    return threading.current_thread() is threading.main_thread()


# --- checks ----------------------------------------------------------------------------------


def check_parser(data: bytes):
    """Returns (kind, detail) for the first broken invariant, or None."""
    try:
        tokens, consumed, need = CommandParser.parse_buffer(data)
    except Exception as exc:  # noqa: BLE001 - any exception is the finding
        return "exception", f"parse_buffer raised {type(exc).__name__}: {exc}"

    if not 0 <= consumed <= len(data):
        return "contract", f"consumed {consumed} outside 0..{len(data)}"
    if isinstance(tokens, ProtocolError):
        if consumed != len(data):
            return "contract", "a protocol error must consume the whole buffer"
        if need != 0:
            return "contract", "a protocol error must not carry a hint"
    else:
        if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
            return "contract", "tokens must be a list of str"
        if tokens and need != 0:
            return "contract", "a complete command must not carry a hint"
        if need < 0:
            return "contract", "negative hint"

    expected = ref_parse_command(data)
    got = real_parse_command(data)
    if expected != got:
        return "differential", f"reference {expected!r} vs parser {got!r}"

    if got[0] == "command":
        n = got[2]
        # No strict prefix of a complete command may be complete or an error.
        for cut in _prefix_cuts(n):
            prefix = data[:cut]
            if real_parse_command(prefix)[0] in ("error", "command"):
                return "prefix", f"prefix of length {cut} (of {n}) gave {real_parse_command(prefix)[0]}"
    if got[0] == "none" and need > 0:
        # The hint must be safe: with fewer new bytes than it asks for, still no progress.
        rng = random.Random(len(data) * 7919 + sum(data[:16]))
        base = len(data) - consumed
        if need - base > 1:
            extra = rng.randrange(0, min(need - base, 256))  # fewer than needed (a short run is enough)
            filler = bytes(rng.choice(FILLER) for _ in range(extra))
            again = real_parse_command(data + filler)
            if again[0] in ("command", "error") or (again[0] == "none" and again[1] != consumed):
                return "hint", f"hint {need} (have {base}) but +{extra} bytes made progress: {again!r}"
    return None


def _prefix_cuts(n: int):
    """Cut points to try: every one for short frames, a spread for long ones."""
    if n <= 200:
        return range(0, n)
    step = max(1, n // 100)
    return sorted(set(list(range(0, 40)) + list(range(0, n, step)) + list(range(n - 40, n))))


def drive_parser(data: bytes, chunks: list[int] | None):
    """What a server would do with this stream: feed it in the given chunk sizes,
    parse after each, and collect what came out. chunks=None feeds it all at once."""
    buf = bytearray()
    out = []
    sizes = chunks if chunks is not None else [len(data)]
    pos = 0
    dead = False
    for size in sizes:
        buf += data[pos : pos + size]
        pos += size
        while buf and not dead:
            tokens, consumed, _ = CommandParser.parse_buffer(buf)
            if isinstance(tokens, ProtocolError):
                out.append(("error",))
                dead = True
                break
            if consumed == 0 and not tokens:
                break
            del buf[:consumed]
            if tokens:
                out.append(tuple(tokens))
    return out


def check_chunking(data: bytes, rng: random.Random):
    whole = drive_parser(data, None)
    for _ in range(3):
        sizes = []
        left = len(data)
        while left > 0:
            s = rng.choice((1, 1, 2, 3, 5, 8, 21, max(1, left)))
            sizes.append(min(s, left))
            left -= sizes[-1]
        if drive_parser(data, sizes) != whole:
            return "chunking", f"chunks {sizes[:12]}... gave a different result: {drive_parser(data, sizes)!r} vs {whole!r}"
    return None


def check_decoder(data: bytes):
    try:
        try:
            KESPDecoder.decode(data)
        except (IncompleteReply, ReplyProtocolError):
            pass
    except Exception as exc:  # noqa: BLE001
        return "exception", f"decode raised {type(exc).__name__}: {exc}"

    expected = ref_decode_reply(data)
    got = real_decode_reply(data)
    if expected != got:
        return "differential", f"reference {expected!r} vs decoder {got!r}"

    if got[0] == "value":
        n = got[2]
        for cut in _prefix_cuts(n):
            prefix = data[:cut]
            r = real_decode_reply(prefix)
            if r[0] != "incomplete":
                return "prefix", f"prefix of length {cut} (of {n}) gave {r[0]}"
    elif got[0] == "incomplete":
        try:
            KESPDecoder.decode(data)
        except IncompleteReply as inc:
            if inc.need <= len(data):
                return "hint", f"need {inc.need} is not beyond the {len(data)} bytes held"
            gap = inc.need - len(data)
            if gap > 1:
                rng = random.Random(len(data) * 104729 + sum(data[:16]))
                extra = rng.randrange(0, min(gap, 256))
                filler = bytes(rng.choice(FILLER) for _ in range(extra))
                again = real_decode_reply(data + filler)
                if again[0] != "incomplete":
                    return "hint", f"need {inc.need} (have {len(data)}) but +{extra} bytes gave {again[0]}"
    return None


# --- corpus and mutation ---------------------------------------------------------------------

# Bytes used to pad a frame in the hint checks: structural ones included, so a hint
# that is too large (one byte would have completed the frame) is noticed.
FILLER = b"abcxyz019 \x00\n\nASNIE+-"

INTERESTING = [
    b"\n", b"\r", b"\r\n", b"\x00", b"\xff", b"\x80", b"A", b"S", b"I", b"N", b"E",
    b"+", b"-", b"0", b"1", b"9", b"-1", b"00", b" ", b"\t", b"A0\n", b"A1\n",
    b"S0\n\n", b"A99999999999\n", b"S99999999999\n", b"\xed\xa0\x80", b"\xc3",
    b"\xf0\x9f\x98\x80", b"A+1\n", b"S1_0\n", b"N\n", b"I-0\n",
]


def random_text(rng: random.Random) -> bytes:
    kind = rng.randrange(6)
    if kind == 0:
        return b""
    if kind == 1:
        return bytes(rng.randrange(256) for _ in range(rng.randrange(0, 12)))
    if kind == 2:
        return "".join(rng.choice("abcXYZ 09é€😀\n\r") for _ in range(rng.randrange(0, 10))).encode()
    if kind == 3:
        return rng.choice([b"SET", b"get", b"PING", b"XADD", b"ERROR", b"x" * rng.randrange(1, 40)])
    if kind == 4:
        return str(rng.randrange(-5, 10**rng.randrange(1, 12))).encode()
    return rng.choice(INTERESTING)


def _near(limit: int, rng: random.Random, cap: int = 2000):
    """limit-1, limit or limit+1 when that is small enough to build, else None."""
    if limit + 1 > cap:
        return None
    return limit + rng.choice((-1, 0, 1))


def boundary_command(rng: random.Random) -> bytes | None:
    """A frame sitting on, or one step off, one of the documented caps. Only built
    when the cap is small (see small_limits), since inputs are a few KiB."""
    kind = rng.randrange(6)
    if kind == 0 and (n := _near(Ref.inline, rng)) is not None:
        return b"x" * n + rng.choice((b"\n", b""))
    if kind == 1 and (n := _near(Ref.array, rng)) is not None:
        have = rng.randrange(0, 3)
        return b"A%d\n" % n + b"".join(b"S1\nx\n" for _ in range(have))
    if kind == 2 and (n := _near(Ref.bulk, rng)) is not None:
        return b"A1\nS%d\n" % n + b"y" * n + rng.choice((b"\n", b"", b"z"))
    if kind == 3 and Ref.command <= 2000:
        half = Ref.command // 2
        a, b = half, Ref.command - half + rng.choice((-1, 0, 1))
        if max(a, b) <= Ref.bulk:
            return b"A2\nS%d\n%s\nS%d\n%s\n" % (a, b"p" * a, b, b"q" * b)
    if kind == 4 and Ref.header <= 64:
        digits = max(0, Ref.header - 1 + rng.choice((-1, 0, 1, 2)))
        return rng.choice((b"A", b"A1\nS")) + b"1" * digits + rng.choice((b"", b"\n"))
    if kind == 5 and Ref.array <= 64:
        return b"A%d\n" % rng.choice((Ref.array, Ref.array + 1)) + b"S0\n\n" * rng.randrange(0, Ref.array + 2)
    return None


def boundary_reply(rng: random.Random) -> bytes | None:
    kind = rng.randrange(6)
    if kind == 0 and (n := _near(Ref.text_line, rng)) is not None:
        return rng.choice((b"+", b"-", b"E")) + b"t" * max(0, n - 1) + rng.choice((b"\n", b"", b"\r\n"))
    if kind == 1:
        d = Ref.depth + rng.choice((-1, 0, 1, 2))
        if 0 <= d <= 64:
            return b"A1\n" * d + rng.choice((b"N\n", b"I1\n", b""))
    if kind == 2 and (n := _near(Ref.bulk, rng)) is not None:
        return b"S%d\n" % n + b"y" * n + rng.choice((b"\n", b"", b"z"))
    if kind == 3 and (n := _near(Ref.array, rng)) is not None:
        return b"A%d\n" % n + b"N\n" * rng.randrange(0, 3)
    if kind == 4 and Ref.header <= 64:
        digits = max(0, Ref.header - 1 + rng.choice((-1, 0, 1, 2)))
        return rng.choice((b"A", b"S", b"I")) + b"1" * digits + rng.choice((b"", b"\n"))
    if kind == 5 and Ref.header <= 64:
        return rng.choice((b"N", b"I0", b"A0")) + b" " * rng.randrange(0, Ref.header + 2) + b"\n"
    return None


def make_command(rng: random.Random) -> bytes:
    if rng.random() < 0.3 and (b := boundary_command(rng)) is not None:
        return b
    shape = rng.randrange(8)
    if shape == 0:  # inline
        line = b" ".join(random_text(rng).replace(b"\n", b"") for _ in range(rng.randrange(0, 4)))
        return line + b"\n"
    if shape == 1:  # blank frames
        return rng.choice([b"\n", b"A0\n", b"\n\nA0\n", b"  \n"])
    tokens = [random_text(rng) for _ in range(rng.randrange(0, 5))]
    return b"A%d\n" % len(tokens) + b"".join(b"S%d\n%s\n" % (len(t), t) for t in tokens)


def random_value(rng: random.Random, depth: int = 0):
    kind = rng.randrange(8 if depth < 4 else 6)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([0, 1, -1, 255, rng.randrange(-10**9, 10**9), 10**20])
    if kind == 2:
        return BulkString(to_text(random_text(rng)))
    if kind == 3:
        return True
    if kind == 4:
        return BulkString("")
    if kind == 5:
        return BulkString(to_text(bytes(rng.randrange(256) for _ in range(rng.randrange(0, 8)))))
    return [random_value(rng, depth + 1) for _ in range(rng.randrange(0, 5))]


def make_reply(rng: random.Random) -> bytes:
    if rng.random() < 0.3 and (b := boundary_reply(rng)) is not None:
        return b
    shape = rng.randrange(8)
    if shape == 0:
        return rng.choice([b"+OK\n", b"+OK\r\n", b"-ERR boom\r\n", b"Ebad thing\n", b"N\n", b"I-42\n", b"I0\n"])
    if shape == 1:
        text = to_bytes(to_text(random_text(rng))).replace(b"\n", b" ").replace(b"\r", b" ")
        return rng.choice([b"E", b"+", b"-"]) + text + rng.choice([b"\n", b"\r\n"])
    return KESPEncoder.encode(random_value(rng))


def mutate(rng: random.Random, data: bytes, pool: list[bytes]) -> bytes:
    buf = bytearray(data)
    for _ in range(rng.randrange(1, 5)):
        op = rng.randrange(11)
        if op == 0 and buf:  # flip a bit
            buf[rng.randrange(len(buf))] ^= 1 << rng.randrange(8)
        elif op == 1 and buf:  # replace a byte
            buf[rng.randrange(len(buf))] = rng.randrange(256)
        elif op == 2:  # insert something interesting
            at = rng.randrange(len(buf) + 1)
            buf[at:at] = rng.choice(INTERESTING)
        elif op == 3 and buf:  # delete a run
            a = rng.randrange(len(buf))
            del buf[a : a + rng.randrange(1, 6)]
        elif op == 4 and buf:  # duplicate a run
            a = rng.randrange(len(buf))
            b = min(len(buf), a + rng.randrange(1, 12))
            buf[b:b] = buf[a:b]
        elif op == 5 and buf:  # truncate
            del buf[rng.randrange(len(buf)) :]
        elif op == 6 and pool:  # splice another input
            other = rng.choice(pool)
            buf[rng.randrange(len(buf) + 1) :] = other[rng.randrange(len(other) + 1) :]
        elif op == 7:  # rewrite a number
            runs = [m for m in re.finditer(rb"[0-9]+", bytes(buf))]
            if runs:
                m = rng.choice(runs)
                caps = [Ref.array, Ref.array + 1, Ref.bulk, Ref.bulk + 1, Ref.header, Ref.depth]
                new = rng.choice([b"%d" % rng.choice(caps), b"0", b"00", b"-1", b"+1", b"4294967296", b"99999999999999999999",
                                  b"%d" % (int(m.group()) + rng.choice((-1, 1))) if int(m.group()) else b"1"])
                buf[m.start() : m.end()] = new
        elif op == 8 and buf:  # swap two bytes
            a, b = rng.randrange(len(buf)), rng.randrange(len(buf))
            buf[a], buf[b] = buf[b], buf[a]
        elif op == 9:  # repeat the whole thing (pipelining)
            buf += bytes(buf)
        elif op == 10:  # append a trailing fragment
            buf += rng.choice(INTERESTING)
    return bytes(buf[:4096])


# --- shrinking -------------------------------------------------------------------------------


def minimize(data: bytes, still_fails, max_steps: int = 3000) -> bytes:
    """Delta debugging: drop ever smaller chunks while the failure persists, then
    simplify single bytes towards b'a'."""
    best = data
    steps = 0
    chunk = max(1, len(best) // 2)
    while chunk >= 1 and steps < max_steps:
        i = 0
        shrunk = False
        while i < len(best) and steps < max_steps:
            candidate = best[:i] + best[i + chunk :]
            steps += 1
            if candidate != best and still_fails(candidate):
                best = candidate
                shrunk = True
            else:
                i += chunk
        if not shrunk:
            chunk //= 2
    for i in range(len(best)):
        if steps >= max_steps:
            break
        if best[i : i + 1] in (b"a", b"A", b"S", b"\n", b"0", b"1"):
            continue
        for repl in (b"a", b"0"):
            candidate = best[:i] + repl + best[i + 1 :]
            steps += 1
            if still_fails(candidate):
                best = candidate
                break
    return best


# --- the fuzzing loop ------------------------------------------------------------------------


@dataclass
class Report:
    target: str
    seed: int
    inputs: int = 0
    seconds: float = 0.0
    failures: list[Failure] = field(default_factory=list)

    def _relabel(self, suffix: str) -> "Report":
        self.target += suffix
        for f in self.failures:
            f.target += suffix
        return self

    def summary(self) -> str:
        status = "FAIL" if self.failures else "ok"
        return f"{self.target:<9} seed={self.seed:<6} inputs={self.inputs:<9} {self.seconds:6.1f}s  {status}"


def _check_one(target: str, data: bytes, rng: random.Random):
    if target == "parser":
        return check_parser(data) or check_chunking(data, rng)
    if target == "decoder":
        return check_decoder(data)
    raise ValueError(target)


def check_roundtrip(rng: random.Random):
    """Returns (data, kind, detail) or None."""
    value = random_value(rng)
    wire = KESPEncoder.encode(value)
    got = real_decode_reply(wire)
    expected = _expected_tag(value)
    if got != ("value", expected, len(wire)):
        # An encoder fallback (error line) is also a legal outcome, but it must decode.
        if not (wire.startswith(b"E") and got[0] == "value" and got[2] == len(wire)):
            return wire, "roundtrip", f"{value!r} encoded to {wire!r} decoded as {got!r}"
    tokens = [to_text(random_text(rng)) for _ in range(rng.randrange(0, 6))]
    framed = KESPEncoder.encode_command(tokens)
    parsed = real_parse_command(framed)
    if tokens:
        want = list(tokens)
        want[0] = want[0].upper()
        if parsed != ("command", tuple(want), len(framed)):
            return framed, "roundtrip", f"{tokens!r} framed as {framed!r} parsed as {parsed!r}"
    elif parsed != ("none", len(framed), False):
        return framed, "roundtrip", f"empty command framed as {framed!r} parsed as {parsed!r}"
    return None


def _expected_tag(value):
    if value is None:
        return None
    if value is True:
        return 1
    if value is False:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, BulkString):
        return ("bulk", str(value))
    return [_expected_tag(v) for v in value]


def fuzz(
    target: str,
    seed: int = 0,
    iterations: int = 10_000,
    max_seconds: float | None = None,
    time_limit: float = 2.0,
    mem_limit_mib: int | None = 64,
    stop_after: int = 5,
    on_failure=None,
    small: bool = False,
) -> Report:
    """Fuzz one target. Deterministic for a given (target, seed, iterations)
    unless max_seconds cuts it short."""
    if small:
        if target == "roundtrip":
            raise ValueError("roundtrip is not run under small limits")
        with small_limits():
            return fuzz(
                target, seed, iterations, max_seconds, time_limit, mem_limit_mib,
                stop_after, on_failure, small=False,
            )._relabel("[small]")
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}")
    rng = random.Random(seed)
    report = Report(target, seed)
    mem = None if mem_limit_mib is None else mem_limit_mib * 1024 * 1024
    make = make_reply if target == "decoder" else make_command
    pool: list[bytes] = [make(rng) for _ in range(32)]
    started = time.perf_counter()
    seen: set = set()

    def record(failure: Failure, still_fails):
        if failure.signature() in seen and len(report.failures) >= 1:
            pass
        failure.minimized = minimize(failure.data, still_fails)
        report.failures.append(failure)
        seen.add(failure.signature())
        if on_failure:
            on_failure(failure)

    for _ in range(iterations):
        if max_seconds is not None and time.perf_counter() - started > max_seconds:
            break
        if target == "roundtrip":
            data = b""
            try:
                with limits(time_limit, mem):
                    found = check_roundtrip(rng)
            except _Timeout:
                found = None
                record(Failure(target, "timeout", f"over {time_limit}s", b""), lambda d: False)
            except MemoryError as exc:
                found = None
                record(Failure(target, "memory", str(exc), b""), lambda d: False)
            except Exception as exc:  # noqa: BLE001
                found = None
                record(Failure(target, "exception", f"{type(exc).__name__}: {exc}", b""), lambda d: False)
            report.inputs += 1
            if found:
                data, kind, detail = found
                record(Failure(target, kind, detail, data), lambda d: False)
        else:
            data = mutate(rng, rng.choice(pool), pool) if rng.random() < 0.9 else make(rng)
            if rng.random() < 0.2:
                pool[rng.randrange(len(pool))] = data
            report.inputs += 1
            check_rng = random.Random(rng.randrange(2**32))
            failure = None
            try:
                with limits(time_limit, mem):
                    found = _check_one(target, data, random.Random(check_rng.random()))
                if found:
                    failure = Failure(target, found[0], found[1], data)
            except _Timeout:
                failure = Failure(target, "timeout", f"over {time_limit}s", data)
            except MemoryError as exc:
                failure = Failure(target, "memory", str(exc), data)
            except Exception as exc:  # noqa: BLE001 - raised by a later check: still a finding
                failure = Failure(target, "exception", f"{type(exc).__name__}: {exc}", data)
            if failure:
                record(failure, _make_predicate(target, failure, time_limit, mem))
        if len(report.failures) >= stop_after:
            break
    report.seconds = time.perf_counter() - started
    return report


def _make_predicate(target: str, failure: Failure, time_limit: float, mem):
    """True when a candidate input still fails the same way."""

    def still_fails(candidate: bytes) -> bool:
        try:
            with limits(time_limit, mem):
                found = _check_one(target, candidate, random.Random(1))
        except _Timeout:
            return failure.kind == "timeout"
        except MemoryError:
            return failure.kind == "memory"
        except Exception:  # noqa: BLE001
            return failure.kind == "exception"
        return bool(found) and found[0] == failure.kind

    return still_fails


# --- command line ----------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fuzz the KESP command parser and reply decoder.")
    ap.add_argument("--target", default="all", choices=("all",) + TARGETS)
    ap.add_argument("--seed", type=int, action="append", help="repeat for several seeds (default 0)")
    ap.add_argument(
        "--limits",
        default="both",
        choices=("default", "small", "both"),
        help="real limits, shrunken limits (reaches every cap), or both (default)",
    )
    ap.add_argument("--iterations", type=int, default=20_000, help="inputs per target and seed")
    ap.add_argument("--max-seconds", type=float, help="stop each run after this long")
    ap.add_argument("--time-limit", type=float, default=2.0, help="CPU seconds allowed per input")
    ap.add_argument("--mem-limit", type=int, default=64, help="MiB of peak memory per input (0 = off)")
    ap.add_argument("--stop-after", type=int, default=5, help="stop a run after this many failures")
    ap.add_argument("--save-dir", help="write each minimized reproduction here as a .bin file")
    args = ap.parse_args(argv)

    targets = TARGETS if args.target == "all" else (args.target,)
    seeds = args.seed or [0]
    failed = False
    total = 0
    modes = {"default": (False,), "small": (True,), "both": (False, True)}[args.limits]
    runs = [
        (seed, target, small)
        for seed in seeds
        for target in targets
        for small in modes
        if not (small and target == "roundtrip")
    ]
    for seed, target, small in runs:
        report = fuzz(
            target,
            seed=seed,
            iterations=args.iterations,
            max_seconds=args.max_seconds,
            time_limit=args.time_limit,
            mem_limit_mib=args.mem_limit or None,
            small=small,
            stop_after=args.stop_after,
        )
        total += report.inputs
        print(report.summary(), flush=True)
        for n, failure in enumerate(report.failures):
            failed = True
            print("  " + str(failure).replace("\n", "\n  "))
            if args.save_dir:
                os.makedirs(args.save_dir, exist_ok=True)
                label = failure.target.replace("[", "-").replace("]", "")
                name = f"{label}-{failure.kind}-seed{seed}-{n}.bin"
                with open(os.path.join(args.save_dir, name), "wb") as fh:
                    fh.write(failure.minimized if failure.minimized is not None else failure.data)
    print(f"total inputs: {total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
