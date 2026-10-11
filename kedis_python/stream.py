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
ERR_BAD_NAME = "ERR Invalid group or consumer name: use printable ASCII without spaces"
ERR_BAD_RECORD = "ERR Invalid consumer group record"


class StreamError(ValueError):
    """A client-visible error; str(e) is the full Redis-style message."""


def check_name(text: str) -> str:
    """Group and consumer names are written to the whitespace-tokenized AOF
    as bare tokens, so they must be non-empty printable ASCII, no spaces."""
    if not (text and text.isascii() and text.isprintable() and " " not in text):
        raise StreamError(ERR_BAD_NAME)
    return text


def _to_int(text: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        raise StreamError(ERR_BAD_RECORD) from None


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


class Pending:
    """One PEL entry: delivered to `consumer` at `delivery_ms`, `count` times."""

    __slots__ = ("consumer", "delivery_ms", "count")

    def __init__(self, consumer: str, delivery_ms: int, count: int):
        self.consumer = consumer
        self.delivery_ms = delivery_ms
        self.count = count


class Consumer:
    __slots__ = ("name", "seen_ms", "active_ms")

    def __init__(self, name: str, seen_ms: int, active_ms: int = -1):
        self.name = name
        self.seen_ms = seen_ms
        self.active_ms = active_ms  # -1 until the consumer is handed an entry


class Group:
    """A consumer group: a cursor (last_id), its consumers, and the PEL
    (pending entries list). The PEL is a dict for O(1) lookup plus a sorted
    ID list for range queries; both are changed only through pel_set and
    pel_remove so they cannot drift apart."""

    __slots__ = ("name", "last_id", "consumers", "pel", "_pel_ids")

    def __init__(self, name: str, last_id=MIN_ID):
        self.name = name
        self.last_id = last_id
        self.consumers = {}  # name -> Consumer
        self.pel = {}  # (ms, seq) -> Pending
        self._pel_ids = []  # sorted (ms, seq)

    def pel_set(self, sid, consumer: str, delivery_ms: int, count: int) -> None:
        if sid not in self.pel:
            bisect.insort(self._pel_ids, sid)
        self.pel[sid] = Pending(consumer, delivery_ms, count)

    def pel_remove(self, sid) -> bool:
        if sid not in self.pel:
            return False
        del self.pel[sid]
        del self._pel_ids[bisect.bisect_left(self._pel_ids, sid)]
        return True

    def pending_ids(self, lo=MIN_ID, hi=MAX_ID) -> list:
        """Sorted PEL IDs within [lo, hi]."""
        i = bisect.bisect_left(self._pel_ids, lo)
        j = bisect.bisect_right(self._pel_ids, hi)
        return self._pel_ids[i:j]

    def pending_after(self, consumer: str, sid, count=None) -> list:
        """IDs in `consumer`'s PEL strictly greater than `sid`, in order."""
        if count is not None and count <= 0:
            return []
        out = []
        for pid in self._pel_ids[bisect.bisect_right(self._pel_ids, sid) :]:
            if self.pel[pid].consumer == consumer:
                out.append(pid)
                if count is not None and len(out) >= count:
                    break
        return out

    def pending_counts(self) -> dict:
        counts = {}
        for p in self.pel.values():
            counts[p.consumer] = counts.get(p.consumer, 0) + 1
        return counts


# Number of tokens after the key, per resolved record kind (XACK is variadic).
_RECORD_ARITY = {
    "XGROUP": 2,  # group last_id            create the group, or move its cursor
    "XGDEL": 1,  # group                      destroy the group
    "XGCONS": 4,  # group consumer seen active  create a consumer (idempotent)
    "XGCDEL": 2,  # group consumer            delete a consumer and its PEL entries
    "XGPEL": 5,  # group consumer id delivery_ms count   set one PEL entry
    "XGLAST": 2,  # group last_id             advance the group cursor
}
GROUP_RECORD_KINDS = frozenset(_RECORD_ARITY) | {"XACK"}


class Stream:
    __slots__ = ("_ids", "_fields", "last_id", "entries_added", "groups")

    def __init__(self):
        self._ids = []  # sorted list of (ms, seq)
        self._fields = []  # parallel: flat [f1, v1, f2, v2, ...]
        self.last_id = MIN_ID  # highest ID ever used (never decreases)
        self.entries_added = 0  # lifetime XADD count
        self.groups = {}  # name -> Group

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

    def entry_fields(self, sid):
        """The field list of one entry, or None if it no longer exists."""
        i = bisect.bisect_left(self._ids, sid)
        if i < len(self._ids) and self._ids[i] == sid:
            return list(self._fields[i])
        return None

    # ------------------------------------------------------------------
    # Consumer groups: the single place group state changes
    # ------------------------------------------------------------------
    def apply_group_record(self, kind: str, args: list):
        """Applies one resolved record (`args` are the tokens after the key).
        Used by live commands, AOF replay and replicas. Raises StreamError on
        anything malformed, before changing anything."""
        n = len(args)
        if kind == "XACK":
            if n < 2:
                raise StreamError(ERR_BAD_RECORD)
        elif _RECORD_ARITY.get(kind) != n:
            raise StreamError(ERR_BAD_RECORD)

        name = args[0]
        if kind == "XGROUP":
            last_id = parse_id(args[1])
            group = self.groups.get(name)
            if group is None:
                self.groups[name] = Group(check_name(name), last_id)
            else:
                group.last_id = last_id
            return 1
        if kind == "XGDEL":
            return 1 if self.groups.pop(name, None) is not None else 0

        group = self.groups.get(name)
        if group is None:
            raise StreamError(ERR_BAD_RECORD)

        if kind == "XGLAST":
            group.last_id = parse_id(args[1])
            return 1
        if kind == "XACK":
            ids = [parse_id(t) for t in args[1:]]  # parse everything first
            return sum(1 for sid in ids if group.pel_remove(sid))

        cname = check_name(args[1])
        if kind == "XGCONS":
            seen, active = _to_int(args[2]), _to_int(args[3])
            consumer = group.consumers.get(cname)
            if consumer is None:
                group.consumers[cname] = Consumer(cname, seen, active)
                return 1
            consumer.seen_ms = max(consumer.seen_ms, seen)
            consumer.active_ms = max(consumer.active_ms, active)
            return 0
        if kind == "XGCDEL":
            group.consumers.pop(cname, None)
            gone = [sid for sid in group._pel_ids if group.pel[sid].consumer == cname]
            for sid in gone:
                del group.pel[sid]
            if gone:
                group._pel_ids = [sid for sid in group._pel_ids if sid in group.pel]
            return len(gone)

        # XGPEL
        sid, delivered, count = parse_id(args[2]), _to_int(args[3]), _to_int(args[4])
        consumer = group.consumers.get(cname)
        if consumer is None:  # tolerate a record stream that lost its XGCONS
            consumer = group.consumers[cname] = Consumer(cname, delivered, delivered)
        consumer.seen_ms = max(consumer.seen_ms, delivered)
        consumer.active_ms = max(consumer.active_ms, delivered)
        group.pel_set(sid, cname, delivered, count)
        return 1

    # ------------------------------------------------------------------
    # Persistence helpers (JSON-safe)
    # ------------------------------------------------------------------
    def to_state(self) -> dict:
        state = {
            "last_id": format_id(self.last_id),
            "entries_added": self.entries_added,
            "entries": [
                [format_id(i), list(f)] for i, f in zip(self._ids, self._fields)
            ],
        }
        if self.groups:  # streams without groups keep the original shape
            state["groups"] = [
                {
                    "name": g.name,
                    "last_id": format_id(g.last_id),
                    "consumers": [
                        [c.name, c.seen_ms, c.active_ms] for c in g.consumers.values()
                    ],
                    "pel": [
                        [format_id(sid), g.pel[sid].consumer]
                        + [g.pel[sid].delivery_ms, g.pel[sid].count]
                        for sid in g._pel_ids
                    ],
                }
                for g in self.groups.values()
            ]
        return state

    @classmethod
    def from_state(cls, state: dict) -> "Stream":
        s = cls()
        for id_text, fields in state["entries"]:
            s._ids.append(parse_id(id_text))
            s._fields.append(list(fields))
        s.last_id = parse_id(state["last_id"])
        s.entries_added = int(state["entries_added"])
        for gs in state.get("groups", []):
            g = Group(gs["name"], parse_id(gs["last_id"]))
            for name, seen_ms, active_ms in gs["consumers"]:
                g.consumers[name] = Consumer(name, seen_ms, active_ms)
            for id_text, consumer, delivery_ms, count in gs["pel"]:
                g.pel_set(parse_id(id_text), consumer, delivery_ms, count)
            s.groups[g.name] = g
        return s
