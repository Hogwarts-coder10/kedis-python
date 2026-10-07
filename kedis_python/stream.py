"""Redis-style stream: an append-only log of (id, field/value list) entries.

IDs are (milliseconds, sequence) pairs, strictly increasing for the life of
the stream (the top ID survives deletes and trims, exactly like Redis).
Entries live in two parallel sorted lists, so lookups use bisect (O(log n)).
Deleting from the middle or trimming the front is O(n); that is fine at this
engine's scale and keeps the structure simple to persist and to test.
"""

import bisect
import time

U64_MAX = (1 << 64) - 1
MIN_ID = (0, 0)
MAX_ID = (U64_MAX, U64_MAX)

ERR_BAD_ID = "ERR Invalid stream ID specified as stream command argument"
ERR_ZERO_ID = "ERR The ID specified in XADD must be greater than 0-0"
ERR_SMALL_ID = (
    "ERR The ID specified in XADD is equal or smaller than the target stream top item"
)
ERR_BAD_INTERVAL = "ERR invalid start ID for the interval"


class StreamError(ValueError):
    """A client-visible error; str(e) is the full Redis-style message."""


def format_id(sid) -> str:
    return f"{sid[0]}-{sid[1]}"


def _to_u64(text: str) -> int:
    if not text.isascii() or not text.isdigit():
        raise StreamError(ERR_BAD_ID)
    value = int(text)
    if value > U64_MAX:
        raise StreamError(ERR_BAD_ID)
    return value


def parse_id(text: str, default_seq: int = 0):
    """'ms-seq' or bare 'ms' (seq = default_seq). No '*', '-', '+' here."""
    ms_text, sep, seq_text = text.partition("-")
    ms = _to_u64(ms_text)
    seq = _to_u64(seq_text) if sep else default_seq
    return (ms, seq)


def parse_bound(text: str, is_start: bool):
    """Range bound for XRANGE/XREVRANGE: '-', '+', 'ms', 'ms-seq', '(id'.

    A bare 'ms' means ms-0 for a start bound and ms-<max> for an end bound.
    A leading '(' makes the bound exclusive: handled by stepping the ID one
    position inward, as Redis does.
    """
    if text == "-":
        return MIN_ID
    if text == "+":
        return MAX_ID
    exclusive = text.startswith("(")
    if exclusive:
        text = text[1:]
    sid = parse_id(text, default_seq=0 if is_start else U64_MAX)
    if not exclusive:
        return sid
    if is_start:
        if sid == MAX_ID:
            raise StreamError(ERR_BAD_INTERVAL)
        return (sid[0], sid[1] + 1) if sid[1] < U64_MAX else (sid[0] + 1, 0)
    if sid == MIN_ID:
        raise StreamError(ERR_BAD_INTERVAL)
    return (sid[0], sid[1] - 1) if sid[1] > 0 else (sid[0] - 1, U64_MAX)


class Stream:
    __slots__ = ("_ids", "_fields", "last_id", "entries_added")

    def __init__(self):
        self._ids = []  # sorted list of (ms, seq)
        self._fields = []  # parallel: flat [f1, v1, f2, v2, ...]
        self.last_id = MIN_ID  # highest ID ever used (never decreases)
        self.entries_added = 0  # lifetime XADD count

    def __len__(self):
        return len(self._ids)

    @property
    def first_id(self):
        return self._ids[0] if self._ids else None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def _next_id(self, spec: str, now_ms: int):
        last_ms, last_seq = self.last_id
        if spec == "*":
            ms = max(now_ms, last_ms)  # a backwards clock must not reorder IDs
            if ms == last_ms:
                if last_seq < U64_MAX:
                    return (ms, last_seq + 1)
                if ms == U64_MAX:  # nothing left above MAX_ID
                    raise StreamError(ERR_SMALL_ID)
                return (ms + 1, 0)  # sequence exhausted: roll to the next ms
            return (ms, 0)

        if spec.endswith("-*"):  # explicit ms, auto sequence
            ms = _to_u64(spec[:-2])
            if ms < last_ms:
                raise StreamError(ERR_SMALL_ID)
            if ms == last_ms:
                if last_seq == U64_MAX:
                    raise StreamError(ERR_SMALL_ID)
                return (ms, last_seq + 1)
            return (ms, 1 if ms == 0 else 0)  # 0-0 is never valid

        sid = parse_id(spec)  # fully explicit
        if sid == MIN_ID:
            raise StreamError(ERR_ZERO_ID)
        if sid <= self.last_id:
            raise StreamError(ERR_SMALL_ID)
        return sid

    def add(self, fields, id_spec: str = "*", now_ms=None) -> str:
        """Append one entry and return its ID string.

        `fields` is a flat [field, value, ...] list (even length, non-empty).
        `id_spec` is '*', 'ms-*' or an explicit 'ms-seq'.
        """
        if not fields or len(fields) % 2:
            raise StreamError("ERR wrong number of arguments for 'xadd' command")
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        sid = self._next_id(id_spec, now_ms)
        self._ids.append(sid)
        self._fields.append(list(fields))
        self.last_id = sid
        self.entries_added += 1
        return format_id(sid)

    def delete(self, *ids) -> int:
        """Remove entries by ID tuple. Returns how many existed."""
        removed = 0
        for sid in ids:
            i = bisect.bisect_left(self._ids, sid)
            if i < len(self._ids) and self._ids[i] == sid:
                del self._ids[i]
                del self._fields[i]
                removed += 1
        return removed

    def trim(self, maxlen=None, minid=None) -> int:
        """Drop oldest entries so len <= maxlen and/or all IDs >= minid.
        Exact trimming only. Returns the number of entries removed."""
        cut = 0
        if maxlen is not None:
            if maxlen < 0:
                raise StreamError("ERR The MAXLEN argument must be >= 0.")
            cut = max(cut, len(self._ids) - maxlen)
        if minid is not None:
            cut = max(cut, bisect.bisect_left(self._ids, minid))
        if cut <= 0:
            return 0
        del self._ids[:cut]
        del self._fields[:cut]
        return cut

    # ------------------------------------------------------------------
    # Reads. Each returns a list of (id_string, [field, value, ...]).
    # ------------------------------------------------------------------
    def _slice(self, lo, hi, reverse, count):
        if count is not None and count <= 0:
            return []
        i = bisect.bisect_left(self._ids, lo)
        j = bisect.bisect_right(self._ids, hi)
        idx = range(j - 1, i - 1, -1) if reverse else range(i, j)
        out = []
        for k in idx:
            out.append((format_id(self._ids[k]), list(self._fields[k])))
            if count is not None and len(out) >= count:
                break
        return out

    def range(self, start=MIN_ID, end=MAX_ID, count=None):
        return self._slice(start, end, False, count)

    def revrange(self, end=MAX_ID, start=MIN_ID, count=None):
        return self._slice(start, end, True, count)

    def read_after(self, sid, count=None):
        """Entries with ID strictly greater than `sid` (XREAD semantics)."""
        if sid >= MAX_ID:
            return []
        nxt = (sid[0], sid[1] + 1) if sid[1] < U64_MAX else (sid[0] + 1, 0)
        return self._slice(nxt, MAX_ID, False, count)

    # ------------------------------------------------------------------
    # Persistence helpers (JSON-safe)
    # ------------------------------------------------------------------
    def to_state(self) -> dict:
        return {
            "last_id": format_id(self.last_id),
            "entries_added": self.entries_added,
            "entries": [
                [format_id(i), list(f)] for i, f in zip(self._ids, self._fields)
            ],
        }

    @classmethod
    def from_state(cls, state: dict) -> "Stream":
        s = cls()
        for id_text, fields in state["entries"]:
            s._ids.append(parse_id(id_text))
            s._fields.append(list(fields))
        s.last_id = parse_id(state["last_id"])
        s.entries_added = int(state["entries_added"])
        return s
