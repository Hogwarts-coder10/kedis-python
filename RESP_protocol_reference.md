# RESP, reverse-engineered from a live wire capture

Everything below was captured by opening a raw TCP socket to a real
`redis-server 7.0.15` instance and recording the exact bytes sent and
received — no client library, no abstraction. Method: a small Python
script (`socket.create_connection`, manual `*N\r\n$len\r\n...` encoding)
talking to `redis-server --port 7777`. Commands are quoted as
Python `bytes` reprs, so `\r\n` is literally CRLF.

## 1. The request format never changes

Every command a client sends — regardless of RESP2/RESP3 — is a flat
**array of bulk strings**. There is no separate "request protocol";
clients just send a RESP Array where every element is a Bulk String.

```
SET foo bar  ==>  *3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n
```

`*3` = 3 elements follow. Each element is `$<byte-length>\r\n<bytes>\r\n`.
Note the length is in **bytes**, not characters — this matters once you
support binary-safe keys/values with embedded `\r\n` or NUL.

### Legacy inline commands

Redis still accepts plain text with no `*`/`$` framing at all, terminated
by `\r\n` — this is the old "telnet protocol" kept for backward compat:

```
SENT: b'PING\r\n'          RECV: b'+PONG\r\n'
SENT: b'SET inlinekey inlineval\r\n'   RECV: b'+OK\r\n'
```

Real clients never use this; it exists so you can `nc localhost 6379` and
type commands by hand. You can skip supporting it in Kedis-C unless you
want telnet-debuggability, but real Redis checks the first byte: if it's
not `*`, it falls back to inline parsing.

## 2. RESP2 reply types — captured examples

| Sigil | Type | Captured bytes | Notes |
|---|---|---|---|
| `+` | Simple String | `+PONG\r\n`, `+OK\r\n` | No embedded `\r\n` allowed — it's a status line, not binary-safe |
| `-` | Error | `-ERR unknown command 'NOTACOMMAND', with args beginning with: \r\n` | Format is `-<ERRCODE> <message>\r\n` by convention, not enforced by the protocol |
| `:` | Integer | `:1\r\n`, `:3\r\n` | Signed 64-bit, no decimal point ever |
| `$` | Bulk String | `$3\r\nbar\r\n` | `$<len>\r\n<len bytes>\r\n`, binary safe |
| `$-1` | Null Bulk String | `$-1\r\n` | RESP2's "nil" — no body, no trailing extra `\r\n` |
| `*` | Array | `*3\r\n$1\r\na\r\n$1\r\nb\r\n$1\r\nc\r\n` | Elements can be any RESP type, including nested arrays |
| `*0` | Empty Array | `*0\r\n` | e.g. `LRANGE` on an empty/missing list |
| `*-1` | Null Array | `*-1\r\n` | e.g. `BLPOP` that times out — distinct from `*0` (empty) |

`WRONGTYPE` errors come back as ordinary `-` errors with that word as a
prefix by convention:
```
-WRONGTYPE Operation against a key holding the wrong kind of value\r\n
```

### Nested arrays in practice

`MULTI`/`EXEC` is the clearest real-world example of nesting — queued
commands reply `+QUEUED\r\n` individually, then `EXEC` returns one array
whose elements are the *actual* per-command replies, which can themselves
be arrays:

```
EXEC -> *2\r\n+PONG\r\n*3\r\n$1\r\na\r\n$1\r\nb\r\n$1\r\nc\r\n
```
That's: array of 2 → [Simple String "PONG", Array of 3 bulk strings].

## 3. RESP3 — negotiated via `HELLO 3`

A client opts in by sending `HELLO 3`. The reply itself demonstrates the
new Map type (`%`):

```
SENT: *2\r\n$5\r\nHELLO\r\n$1\r\n3\r\n
RECV: %7\r\n$6\r\nserver\r\n$5\r\nredis\r\n$7\r\nversion\r\n$6\r\n7.0.15\r\n
      $5\r\nproto\r\n:3\r\n$2\r\nid\r\n:4\r\n$4\r\nmode\r\n$10\r\nstandalone\r\n
      $4\r\nrole\r\n$6\r\nmaster\r\n$7\r\nmodules\r\n*0\r\n
```
`%7` = 7 key/value pairs follow (14 elements total) — same counting
convention as Array but pairs instead of flat elements.

Once on RESP3, reply *shapes* change for some commands without you
asking again — e.g. `HGETALL` switches from a flat Array to a real Map:

```
RESP3 HGETALL -> %2\r\n$2\r\nf1\r\n$2\r\nv1\r\n$2\r\nf2\r\n$2\r\nv2\r\n
RESP3 SMEMBERS -> ~2\r\n$1\r\ny\r\n$1\r\nx\r\n      (Set type, sigil ~)
```

### Every RESP3 type, straight from `DEBUG PROTOCOL`

Redis ships a command literally for this: `DEBUG PROTOCOL <type>`
(needs `--enable-debug-command yes` or a local socket). Captured on a
RESP3 connection:

| Sigil | Type | Captured bytes |
|---|---|---|
| `$` | String | `$11\r\nHello World\r\n` |
| `:` | Integer | `:12345\r\n` |
| `,` | Double | `,3.141\r\n` |
| `(` | Big Number | `(1234567999999999999999999999999999999\r\n` |
| `_` | Null | `_\r\n` |
| `*` | Array | `*3\r\n:0\r\n:1\r\n:2\r\n` |
| `~` | Set | `~3\r\n:0\r\n:1\r\n:2\r\n` |
| `%` | Map | `%3\r\n:0\r\n#f\r\n:1\r\n#t\r\n:2\r\n#f\r\n` |
| `#` | Boolean | `#t\r\n` / `#f\r\n` |
| `=` | Verbatim String | `=29\r\ntxt:This is a verbatim\nstring\r\n` |
| `\|` | Attribute | `\|1\r\n$14\r\nkey-popularity\r\n*2\r\n$7\r\nkey:123\r\n:90\r\n` then the *real* reply follows immediately after |
| `>` | Push | `>2\r\n$16\r\nserver-cpu-usage\r\n:42\r\n` then the *real* reply follows immediately after |

Two things that only become obvious by watching the bytes:

- **Attribute (`\|`) and Push (`>`) aren't standalone replies** — they're
  *prefixed* onto a real reply. The capture for `attrib` was
  `|1\r\n...*2\r\n$7\r\nkey:123\r\n:90\r\n$39\r\nSome real reply following the attribute\r\n`
  — note there's a Bulk String tacked on the end that's the actual
  answer. A correct parser treats `|` and `>` as "consume this metadata
  block, then keep parsing — the next thing is the real reply."
- **Verbatim String (`=`)** has a 3-byte format tag (`txt`, `mkd`, ...)
  before a colon-less `:`... actually a literal `:`-free separator —
  looking at the bytes it's `=<len>\r\n<3-char-type>:<data>\r\n`. The
  length includes the `txt:` prefix.

### Null: RESP2 vs RESP3 (same key, two encodings)

```
RESP2 GET on missing key:    $-1\r\n
RESP3 GET on missing key:    _\r\n          <- new unified Null type

RESP2 BLPOP timeout:         *-1\r\n        <- "null array"
RESP3 BLPOP timeout:         _\r\n          <- same unified Null
```
RESP3 collapses "null string" and "null array" into one `_\r\n` sigil.
If you implement both protocol versions in Kedis-C, this is the detail
that'll bite you if you reuse one null-encoding function for both.

## 4. Pipelining (confirmed empirically)

Three commands written to the socket in a **single** `send()` call, with
zero reads in between, still produce three replies concatenated in order:

```
WRITE: SET p 1 / INCR p / GET p   (one packet, one syscall)
READ:  +OK\r\n:2\r\n$1\r\n2\r\n    (one packet back, three replies)
```
This confirms RESP is a strict request/response stream with no need for
ids or framing beyond length-prefixing — the parser just needs to consume
exactly the bytes for the type-of-the-moment and loop. This is also why
your earlier Kedis benchmarking bottleneck (per-write fsync,
single-threaded GIL) is orthogonal to the wire protocol itself — RESP
parsing is cheap; pipelining is "free" parallelism a client can exploit
without any server-side protocol support beyond reading the socket in a
loop.

## 5. Parser design notes for Kedis-C / CDSA

Translating the above into a C parser:

- **Request side is simple**: you only ever need to parse Arrays of Bulk
  Strings (plus optionally inline mode). You do NOT need to parse `:`,
  `+`, `~`, `%`, etc. as *requests* — those are reply-only types. This
  cuts your request parser down to: read `*N\r\n`, then N times read
  `$len\r\n<len bytes>\r\n`.
- **Buffering**: a command can arrive split across multiple `read()`
  calls, and multiple commands can arrive in one `read()` (see
  pipelining above). The parser needs to be resumable: track how many
  bytes of the current token you still need, and keep a growable input
  buffer per-connection rather than assuming one `read()` == one command.
- **Bulk length is a byte count, not a delimiter search** — once you've
  parsed `$<len>`, do a deterministic read of exactly `len` bytes, then
  expect literally `\r\n`. Don't scan for `\r\n` inside the body; binary
  values can contain it.
- **Negative length is the null sentinel** (`$-1`, `*-1`) — check for the
  `-` before doing free()/alloc() math, or you'll allocate a few
  exabytes when -1 gets cast to `size_t`. (Said as someone who's read
  your ASan logs.)
- **Reply side decides your protocol version**: if you only ever target
  RESP2 clients for Kedis-C's first cut, you can skip the entire RESP3
  type zoo (`%`, `~`, `,`, `(`, `#`, `=`, `|`, `>`) and just emit `+`,
  `-`, `:`, `$`, `*`. RESP3 is purely an enhancement clients opt into via
  `HELLO 3` — a server that never advertises RESP3 support in its
  `HELLO` reply never needs to emit those sigils at all.
- **One write buffer, not N small writes**: build the full reply for a
  pipelined batch in one buffer and do one `write()`/`send()` — this is
  the server-side mirror of the client pipelining test above, and avoids
  syscall overhead per reply.

## 5b. Parser design notes for KESP (Python)

Same protocol, different headaches. The C section above doesn't transfer
directly — here's the Python-shaped version of the same decisions:

- **No manual buffer math** — but you still need a stream abstraction
  that can ask for "give me N more bytes" without caring how many
  `recv()` calls that takes. `asyncio.StreamReader` already does this:
  `reader.readexactly(n)` and `reader.readuntil(b"\r\n")` handle the
  partial-read/pipelining problem for you. Don't hand-roll a socket loop
  unless you have a reason to.
- **The negative-length-as-null gotcha doesn't exist in Python** — there's
  no `size_t` to underflow. `$-1` just means "the value is `None`, there's
  no body to read." Check for it before calling `readexactly`, not
  because it'll crash, but because `readexactly(-1)` throws a confusing
  exception instead.
- **Request parsing, concretely**:
  ```python
  async def read_command(reader: asyncio.StreamReader) -> list[bytes]:
      line = await reader.readuntil(b"\r\n")
      assert line[0:1] == b"*"
      n = int(line[1:-2])
      args = []
      for _ in range(n):
          line = await reader.readuntil(b"\r\n")
          assert line[0:1] == b"$"
          length = int(line[1:-2])
          data = await reader.readexactly(length + 2)  # +2 for trailing \r\n
          args.append(data[:-2])
      return args
  ```
  That's the whole request-side parser — KESP only needs to understand
  `*` and `$` on the way in, same as the C version did.
- **Reply encoding** — build replies as `bytes`, not string formatting on
  the hot path. `f"${len(val)}\r\n".encode() + val + b"\r\n"` for bulk
  strings; for arrays just join. Watch out for binary-safe values: the
  declared length must be the actual byte count, not `len()` of a
  decoded `str` — multi-byte UTF-8 will silently break this.
- **Pipelining is the same story** — write the whole batched reply with
  one `writer.write()` then a single `await writer.drain()`, don't drain
  per-command. This is the part that actually helps the fsync/GIL
  bottleneck from your v0.3.0 benchmarking, since it's a server-side cost
  independent of how slow `SET` itself is.
- **RESP3 is still skippable for v1** — same reasoning as the C section:
  it's opt-in via `HELLO 3`, so KESP can ship RESP2-only and add `,` `(`
  `_` `#` `=` `%` `~` `|` `>` later without breaking existing clients.

## 6. Quick sigil reference

```
RESP2:  +  -  :  $  *
RESP3 adds:  ,  (  _  #  =  %  ~  |  >
```
