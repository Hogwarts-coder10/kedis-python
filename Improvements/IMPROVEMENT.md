# Kedis — Codebase Improvement Plan

**Reviewer:** Systems Engineer, Level II  
**Files reviewed:** `main.py`, `store.py`, `commands.py`, `network.py`, `parser.py`, `server.py`, `skiplist.py`, `ui.py`  
**Current version:** 0.3.0 (Echo)

---

## Executive Summary

Kedis is a well-structured, genuinely impressive solo project. The layering is clean (parser → commands → store), the AOF persistence is durable, and the dual TCP/local mode with radar auto-reconnect is a sophisticated UX touch. The SkipList for sorted sets is the right structural call.

The improvements below are grouped by priority. **P0 is correctness — fix these before anything else.** P1 is production-readiness. P2 is architecture. P3 is polish.

---

## P0 — Correctness Bugs (Fix Immediately)

### 1. `_evict_all_expired` doesn't invalidate the LRU cache

**File:** `store.py` → `_evict_all_expired()`

The active sweeper deletes expired keys from `_data` and `_expires` but never touches `_lru_cache`. A key evicted here will still be served from cache on the next `GET` — which is a **silent data correctness bug**.

```python
# Current (wrong)
for key in keys_to_delete:
    if key in self._data:
        del self._data[key]
    if key in self._expires:
        del self._expires[key]
    self._log_operation("DEL", key)

# Fix — add one line per key
for key in keys_to_delete:
    if key in self._data:
        del self._data[key]
    if key in self._expires:
        del self._expires[key]
    self._lru_invalidate(key)      # ← add this
    self._log_operation("DEL", key)
```

---

### 2. Wrong attribute name in `compact_aof` error handler

**File:** `store.py` → `compact_aof()`

```python
# Current (wrong — silently does nothing on crash)
if hasattr(self, "file") and self.file.closed:
    self.file = open(self.aof_filename, "a")

# Fix
if hasattr(self, "aof_file") and self.aof_file.closed:
    self.aof_file = open(self.aof_filename, "a")
```

If compaction crashes mid-write, the file handle is never re-opened and all subsequent writes are silently dropped.

---

### 3. `recv(1024)` in benchmark and `network.py` will split large responses

**File:** `network.py` → `send_command()`, `benchmark.py`

`recv(1024)` reads *at most* 1024 bytes per call. Any response larger than that (e.g. `HGETALL` on a large hash, `KEYS` with many entries) will be **silently truncated**. The client will parse a partial response as if it were complete.

```python
# Fix — read until a sentinel or use length-prefixed framing
def send_command(self, raw_input: str) -> str:
    self.sock.sendall(raw_input.encode("utf-8"))
    chunks = []
    while True:
        chunk = self.sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if chunk.endswith(b"\n"):   # or use a proper RESP terminator
            break
    return b"".join(chunks).decode("utf-8").strip()
```

The real fix is to implement a proper framing protocol (length prefix or RESP `\r\n` termination) on both the server send and client recv side.

---

### 4. No thread safety on shared state

**File:** `server.py`, `store.py`

`global_store` and `global_handler` are shared across all client threads with zero locking. `ThreadingMixIn` spawns a new thread per connection, so concurrent `SET`/`GET`/`DEL` calls all race on `_data`, `_expires`, and `_lru_cache` simultaneously.

CPython's GIL provides *some* accidental protection for simple dict operations, but `OrderedDict.move_to_end` + size check + `popitem` is a multi-step operation that is **not atomic** and will corrupt under concurrent access.

```python
# Fix — add a RLock to KedisStore.__init__
import threading
self._lock = threading.RLock()

# Wrap all public methods
def get(self, key):
    with self._lock:
        ...

def set(self, key, value):
    with self._lock:
        ...
```

Alternatively, use `asyncio` with a single-threaded event loop (see P2 section).

---

## P1 — Production Readiness

### 5. `fsync` on every write kills throughput

**File:** `store.py` → `_log_operation()`

Every single `SET`, `LPUSH`, `ZADD`, etc. calls `os.fsync()`, which blocks until the OS confirms the write hit physical disk. This is the **primary bottleneck** behind the 5,301 req/sec ceiling — not Python, not the LRU, not the network.

Redis solves this with `appendfsync everysec` — flush and fsync at most once per second.

```python
# In __init__
self._last_fsync = time.time()
self.FSYNC_INTERVAL = 1.0   # seconds — tune to taste

def _log_operation(self, *tokens):
    command_string = " ".join(str(t) for t in tokens)
    self.aof_file.write(command_string + "\n")
    
    now = time.time()
    if now - self._last_fsync >= self.FSYNC_INTERVAL:
        self.aof_file.flush()
        os.fsync(self.aof_file.fileno())
        self._last_fsync = now
```

**Expected impact: 3–6x throughput increase.** The tradeoff is at-most-1-second data loss on a hard crash — acceptable for most use cases.

---

### 6. AOF and snapshot (`save`/`load`) are uncoordinated

**File:** `store.py`

Both `save()`/`load()` (JSON snapshot) and `recover_from_aof()` exist independently with no coordination. On startup, only AOF is replayed. If someone runs `SAVE`, creates a snapshot, then the AOF grows further, a future restart replays the full AOF from scratch — not from the checkpoint.

Redis solves this by recording the snapshot offset in the AOF. At minimum, add a startup flag:

```python
def recover(self):
    """Prefer snapshot if newer than AOF, otherwise replay AOF."""
    snap_mtime = os.path.getmtime("dump.json") if os.path.exists("dump.json") else 0
    aof_mtime  = os.path.getmtime(self.aof_filename) if os.path.exists(self.aof_filename) else 0
    
    if snap_mtime > aof_mtime:
        self.load("dump.json")
    else:
        self.recover_from_aof()
```

---

### 7. `recv` buffer size in server is 1024 bytes

**File:** `server.py` → `handle()`

```python
data = self.request.recv(1024)
```

Same issue as #3. A client sending a large `SET` value or multi-argument `LPUSH` will have its command **silently truncated**, causing a parse error or phantom data. Increase to at least 64KB, or implement proper stream buffering:

```python
data = self.request.recv(65536)
```

For a real fix, buffer until a newline/CRLF delimiter is found.

---

### 8. `_data` has no size cap — unbounded memory growth

**File:** `store.py`

The primary store grows indefinitely. In a long-running server, this will consume all available RAM. Add a `maxkeys` limit with a configurable eviction policy (LRU, LFU, or `noeviction`):

```python
def __init__(self, ..., maxkeys: int = 0):  # 0 = unlimited
    self._maxkeys = maxkeys

def _maybe_evict_primary(self):
    """Evict LRU key from primary store if over capacity."""
    if self._maxkeys and len(self._data) >= self._maxkeys:
        # Evict the LRU key (reuse _lru_cache ordering as a hint)
        if self._lru_cache:
            lru_key = next(iter(self._lru_cache))
            self.delete(lru_key)
```

---

### 9. LRU hit rate stats reset on every restart

**File:** `store.py` → `lru_stats()`

`_lru_hits` and `_lru_misses` are in-memory counters lost on restart. You lose all historical cache performance data. At minimum, persist them to the snapshot or log them periodically:

```python
# In save()
state = {
    "data": self._data,
    "expires": self._expires,
    "lru_stats": {"hits": self._lru_hits, "misses": self._lru_misses}
}

# In load()
self._lru_hits   = state.get("lru_stats", {}).get("hits", 0)
self._lru_misses = state.get("lru_stats", {}).get("misses", 0)
```

---

### 10. `STATS` command doesn't expose LRU cache metrics

**File:** `commands.py` → `_handle_stats()`, `store.py` → `get_engine_stats()`

`get_engine_stats()` now returns `lru_cache` stats (added in the LRU implementation), but `_handle_stats()` in `commands.py` never serializes or sends them to the client:

```python
def _handle_stats(self, tokens):
    stats = self.store.get_engine_stats()
    lru = stats.get("lru_cache", {})
    return (
        f"Keys:{stats['total_keys']}\n"
        f"StringChars:{stats['string_chars']}\n"
        f"ListItems:{stats['list_items']}\n"
        f"SetMembers:{stats['set_members']}\n"
        f"HashFields:{stats['hash_fields']}\n"
        f"ZSetNodes:{stats['zset_nodes']}\n"
        f"CacheHits:{lru.get('hits', 0)}\n"
        f"CacheMisses:{lru.get('misses', 0)}\n"
        f"CacheHitRate:{lru.get('hit_rate_pct', 0.0)}%\n"
        f"CachedKeys:{lru.get('cached_keys', 0)}/{lru.get('max_size', 0)}"
    )
```

---

## P2 — Architecture

### 11. `main.py` directly accesses `store._data` and `store._expires` (private internals)

**File:** `main.py` → `_init_local_engine()`, `_handle_tcp_crash()`, `INFO`, `MODE`

```python
key_count = len(self.store._data)          # line 116, 265, 289
exp_count = len(self.store._expires)       # line 292
for val in self.store._data.values(): ...  # line 302
```

This breaks encapsulation — `main.py` now depends on internal implementation details of `KedisStore`. If `_data` is ever renamed or restructured, the UI breaks silently. Add public accessors to `KedisStore`:

```python
def key_count(self) -> int:
    return len(self._data)

def expire_count(self) -> int:
    return len(self._expires)

def type_breakdown(self) -> dict:
    counts = {"string": 0, "list": 0, "set": 0, "hash": 0, "zset": 0}
    for val in self._data.values():
        ...
    return counts
```

---

### 12. Version string is hardcoded in two places and is wrong in one

**File:** `main.py` → `INFO` handler (line 286), `ui.py` → `print_banner()`

`ui.py` prints version `0.3.0`. `main.py`'s `INFO` command hardcodes `0.2.0`. They're out of sync. Fix with a single source of truth:

```python
# config.py
VERSION  = "0.3.0"
CODENAME = "Echo"
HOST     = "127.0.0.1"
PORT     = 6379
```

Import from `config.py` everywhere.

---

### 13. `VALID_COMMANDS` in `main.py` is manually maintained and will drift

**File:** `main.py`

`VALID_COMMANDS` is a hardcoded list used for typo suggestions. Every time a new command is added to `CommandHandler._commands`, it has to be manually added here too — and it will be forgotten. Generate it from the dispatch table instead:

```python
# In KedisClient, after handler is created
VALID_COMMANDS = list(self.handler._commands.keys()) + [
    "MODE", "INFO", "HELP", "DEBUG", "RECONNECT",
    "CLEAR", "CLS", "EXIT", "QUIT"
]
```

---

### 14. Transaction (`MULTI`/`EXEC`) replay has no error isolation

**File:** `server.py` → `handle_exec()`

In Redis, if a command inside a `MULTI` block fails, the other commands still execute and the error is returned inline. In the current implementation, a `TypeError` raised by any queued command will bubble up as an unhandled exception and terminate the client's connection mid-transaction.

```python
# Fix — catch per-command errors inside exec
for queued_tokens in self.tx_queue:
    try:
        res = global_handler.execute(queued_tokens)
    except Exception as e:
        res = f"-ERR {str(e)}"
    responses.append(res)
```

---

### 15. `CommandParser` silently uppercases only `tokens[0]`

**File:** `parser.py`

Command names are uppercased, but option flags like `WITHSCORES` in `ZRANGE key 0 -1 withscores` are not. The server handles it with `.upper()` checks, but it's fragile — a user typing `withscores` in lowercase will get a syntax error. Fix at parse time:

```python
tokens[0] = tokens[0].upper()
# Also uppercase known option flags
OPTION_FLAGS = {"WITHSCORES", "ALPHA", "LIMIT", "BY", "ASC", "DESC"}
tokens = [
    t.upper() if t.upper() in OPTION_FLAGS else t
    for t in tokens
]
```

---

## P3 — Polish & Developer Experience

### 16. `INFO` command shows stale version (`0.2.0`) to clients

**File:** `main.py` line 286  
Covered by the config.py fix in point 12. The banner says `0.3.0`, `INFO` says `0.2.0`. Fix the single source of truth and this resolves itself.

---

### 17. `_handle_stats` in `ui.py` is brittle — splits on `:` naively

**File:** `ui.py` → `render_stats()`

```python
f"[cyan]{line.split(':')[0].ljust(15)}[/cyan]: [yellow]{line.split(':')[1]}[/yellow]"
```

If any stat value contains a colon (e.g. `CacheHitRate:84.0%`), this will crash with an `IndexError`. Use `split(':', 1)` to split only on the first colon:

```python
key, val = line.split(':', 1)
```

---

### 18. `ping_radar()` fires on every REPL loop iteration

**File:** `main.py` → `_check_radar()`, `network.py` → `ping_radar()`

Every keypress in the REPL triggers a TCP connection attempt (`connect` + `settimeout(0.05)`) to check if the server is back. On a cold network, this adds ~50ms latency to every command. Rate-limit the radar check:

```python
RADAR_INTERVAL = 5.0  # seconds

def _check_radar(self):
    now = time.time()
    if not hasattr(self, '_last_radar_check'):
        self._last_radar_check = 0
    if now - self._last_radar_check < RADAR_INTERVAL:
        return
    self._last_radar_check = now
    # ... existing logic
```

---

### 19. `SkipList.get_range` loads all elements into memory before slicing

**File:** `skiplist.py` → `get_range()`

```python
elements = []
while current:
    elements.append((current.member, current.score))
    current = current.forward[0]
slice_items = elements[start:] if stop == -1 else elements[start : stop + 1]
```

For a sorted set with 1M members, `ZRANGE key 0 9` still traverses and loads all 1M nodes before returning 10. Fix by stopping traversal early:

```python
def get_range(self, start: int, stop: int, withscores: bool = False) -> list:
    current = self.head.forward[0]
    result = []
    idx = 0
    end = stop if stop != -1 else float("inf")

    while current:
        if idx > end:
            break
        if idx >= start:
            result.append(current.member)
            if withscores:
                result.append(f"{current.score:g}")
        current = current.forward[0]
        idx += 1
    return result
```

---

### 20. No `EXISTS` in `VALID_COMMANDS`

**File:** `main.py`

`EXISTS` is implemented in `CommandHandler._commands` and in `store.py`, but it's missing from the `VALID_COMMANDS` list in `main.py`. A typo like `EXSTS` will never suggest `EXISTS` as a correction. This is exactly the kind of drift that point 13 (auto-generate from dispatch table) would prevent.

---

## Summary Table

| # | File | Severity | Category | One-line description |
|---|------|----------|----------|----------------------|
| 1 | `store.py` | 🔴 P0 | Correctness | Active sweeper never invalidates LRU cache |
| 2 | `store.py` | 🔴 P0 | Correctness | Wrong attribute name in `compact_aof` error handler |
| 3 | `network.py` | 🔴 P0 | Correctness | `recv(1024)` silently truncates large responses |
| 4 | `server.py` | 🔴 P0 | Correctness | No locking on shared store across threads |
| 5 | `store.py` | 🟠 P1 | Performance | `fsync` per write is the throughput ceiling |
| 6 | `store.py` | 🟠 P1 | Reliability | AOF and snapshot are uncoordinated on startup |
| 7 | `server.py` | 🟠 P1 | Reliability | Server `recv` buffer too small for large commands |
| 8 | `store.py` | 🟠 P1 | Reliability | No `maxkeys` cap — unbounded memory growth |
| 9 | `store.py` | 🟠 P1 | Observability | LRU hit/miss counters lost on restart |
| 10 | `commands.py` | 🟠 P1 | Observability | `STATS` doesn't expose LRU metrics |
| 11 | `main.py` | 🟡 P2 | Architecture | `main.py` accesses `_data` and `_expires` directly |
| 12 | `main.py`/`ui.py` | 🟡 P2 | Architecture | Version string hardcoded in two files, out of sync |
| 13 | `main.py` | 🟡 P2 | Architecture | `VALID_COMMANDS` will drift from dispatch table |
| 14 | `server.py` | 🟡 P2 | Architecture | MULTI/EXEC errors not isolated per command |
| 15 | `parser.py` | 🟡 P2 | Architecture | Option flags not uppercased at parse time |
| 16 | `main.py` | 🟢 P3 | Polish | `INFO` shows wrong version `0.2.0` |
| 17 | `ui.py` | 🟢 P3 | Polish | `render_stats` splits on `:` naively — will crash |
| 18 | `main.py` | 🟢 P3 | Polish | Radar pings on every REPL iteration |
| 19 | `skiplist.py` | 🟢 P3 | Polish | `get_range` loads all nodes before slicing |
| 20 | `main.py` | 🟢 P3 | Polish | `EXISTS` missing from `VALID_COMMANDS` |

---

## Recommended Fix Order

1. **Fix #1, #2, #4** — correctness and crash bugs, zero performance cost to fix  
2. **Fix #5** — batched fsync, single biggest performance lever (3–6x throughput)  
3. **Fix #3, #7** — recv buffer, prevents silent data corruption on any large payload  
4. **Fix #12, #13** — config.py + auto-generated VALID_COMMANDS, prevents future drift  
5. **Fix #11** — public accessors on KedisStore, cleans up the main.py coupling  
6. **Everything else** — P3 polish, in any order
