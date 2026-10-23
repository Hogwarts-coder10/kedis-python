# 📖 The Spellbook: Command Reference

Kedis operates via a strict, atomic command set sent over the KESP wire protocol. Below is the complete API reference for interacting with the engine.

---

## 🛠️ Core Data Operations

### `SET key value`
Stores a string value under the given key. Overwrites existing values.
* **Returns:** `+OK`
* **Example:** `SET session_123 "active"`

### `GET key`
Retrieves the string value associated with a key.
* **Returns:** The value as a Bulk String, or `(nil)` if the key does not exist.
* **Example:** `GET session_123`

### `DEL key [key ...]`
Removes one or more specified keys from the storage engine, whatever type they hold. Ignores keys that do not exist; a key named twice is only deleted (and counted) once.
* **Returns:** `(integer)` The number of keys successfully removed.
* **Example:** `DEL session_123 session_456`

### `EXISTS key [key ...]`
Checks whether one or more keys exist. An expired key counts as missing, and a key named twice is counted twice (`EXISTS a a` returns `2` when `a` exists).
* **Returns:** `(integer)` How many of the given keys exist.
* **Example:** `EXISTS session_123 session_456`

### `TYPE key`
Reports the type of the value stored at a key. The names are lowercase: `string`, `list`, `set`, `hash`, `zset` or `stream`. Bitmaps and HyperLogLogs are strings underneath, so they report `string`, as in Redis. `KEYS` uses the same names.
* **Returns:** The type name as a string, or `none` if the key does not exist.
* **Example:** `TYPE tasks`

---

## ⏳ Lifespan & Expiration

### `EXPIRE key seconds`
Assigns a Time-To-Live (TTL) to a key. Once the seconds elapse, the key is automatically evicted from memory.
* **Returns:** `(integer) 1` if the timeout was set, `0` if the key does not exist.
* **Example:** `EXPIRE temp_cache 300`

### `TTL key`
Checks the remaining lifespan of a volatile key.
* **Returns:** `(integer)` Remaining seconds. Returns `-1` if the key exists but has no expiration, or `-2` if the key does not exist.
* **Example:** `TTL temp_cache`

---

### `HELP [command|topic]`
Shows the command reference. Without an argument, it prints an overview of all available commands and topics. Use `HELP <command>` to see detailed syntax, return values, and examples for a specific command.
* **Returns:** A formatted help panel in the CLI.
* **Example:** `HELP` or `HELP ZADD`

---


## 🛡️ Transactions & Concurrency (Optimistic Locking)

Kedis ensures thread-safe data mutations through isolated execution queues and version tracking.

### `WATCH key [key ...]`
Locks the specified keys to your current session. If another client modifies any of these keys before you call `EXEC`, your transaction will be safely aborted to prevent a race condition.
* **Returns:** `+OK`

### `UNWATCH`
Flushes all active locks on your current session, allowing transactions to proceed without version verification.
* **Returns:** `+OK`

### `MULTI`
Opens a transaction block. Subsequent commands are not executed immediately; they are queued in the session's Surge Tank. A block holds at most 10,000 commands and 64 MiB of arguments; past that, `EXEC` aborts (see below and [limits](limits.md)).
* **Returns:** `+OK`

### `EXEC`
Executes all queued commands in a single, atomic block. If a `WATCH` lock was tripped by another client, the transaction aborts.
* **Returns:** An Array of the command results, or `(nil)` if the transaction was aborted. If the queue limit was exceeded, `-EXECABORT Transaction discarded: the MULTI queue limit was exceeded` and nothing runs.

### `DISCARD`
Flushes the transaction queue and exits the `MULTI` state without executing any commands.
* **Returns:** `+OK`

---

## 🧱 Lists, Sets, Hashes, Sorted Sets, Bitmaps & HyperLogLog

A key holds **one type at a time**. Running a command from the wrong family against it returns `WRONGTYPE Operation against a key holding the wrong kind of value`, and plain `SET` overwrites whatever was there. Every command below is written to the AOF, so the data survives a restart, and every key type honours `EXPIRE` and `TTL`.

---

## 📜 Lists

Ordered sequences with fast pushes and pops at both ends. A list that loses its last element disappears.

### `LPUSH key value [value ...]`
Pushes values onto the **head** of the list, creating it if needed. Several values are pushed one at a time, so the last one ends up first.
* **Returns:** `(integer)` The length of the list after the push.
* **Example:** `LPUSH tasks urgent`

### `RPUSH key value [value ...]`
Pushes values onto the **tail** of the list, creating it if needed.
* **Returns:** `(integer)` The length of the list after the push.
* **Example:** `RPUSH tasks later`

### `LPOP key`
Removes and returns the first element. There is no `COUNT` argument.
* **Returns:** The element as a Bulk String, or `(nil)` if the key doesn't exist.
* **Example:** `LPOP tasks`

### `RPOP key`
Removes and returns the last element.
* **Returns:** The element as a Bulk String, or `(nil)` if the key doesn't exist.
* **Example:** `RPOP tasks`

### `LRANGE key start stop`
Returns the elements from index `start` to index `stop`, both inclusive. Index `0` is the head. Negative indexes count from the tail (`-1` is the last element, `-2` the one before it), so `LRANGE tasks 0 -1` is the whole list. An out-of-range `start` is clamped to the head, a `stop` past the end is clamped to the tail, and an empty or inverted range returns an empty Array.
* **Returns:** An Array of elements, or an empty Array if the key doesn't exist.
* **Example:** `LRANGE tasks 0 -1`, or `LRANGE tasks -2 -1` for the last two elements

```
> RPUSH tasks a b c
(integer) 3
> LPUSH tasks x y
(integer) 5
> LRANGE tasks 0 -1
1) y
2) x
3) a
4) b
5) c
> LPOP tasks
"y"
```

> **Not implemented yet:** `LLEN`, `LINDEX`, `LSET`, `LTRIM`, `LINSERT`, `LREM`, `BLPOP`

---

## 🎯 Sets

Unordered collections of unique strings.

### `SADD key member [member ...]`
Adds members, creating the set if needed. Members already present are ignored.
* **Returns:** `(integer)` How many members were **newly** added.
* **Example:** `SADD skills python systems`

### `SREM key member [member ...]`
Removes members. Missing members are ignored, and a set that loses its last member disappears.
* **Returns:** `(integer)` How many members were actually removed.
* **Example:** `SREM skills python`

### `SMEMBERS key`
Returns every member, in no particular order.
* **Returns:** An Array of members, or an empty Array if the key doesn't exist.
* **Example:** `SMEMBERS skills`

```
> SADD skills python python systems
(integer) 2
> SADD skills python
(integer) 0
> SMEMBERS skills
1) python
2) systems
> SREM skills python nope
(integer) 1
```

> **Not implemented yet:** `SISMEMBER`, `SCARD`, `SINTER`, `SUNION`, `SDIFF`, `SPOP`

---

## 🗂️ Hashes

A map of fields to string values under one key, kept in insertion order.

### `HSET key field value`
Sets one field. **Everything after the field name is the value**, joined with single spaces, so `HSET user bio likes databases` stores `likes databases`. Only one field per call.
* **Returns:** `(integer)` `1` if the field is new, `0` if an existing field was overwritten.
* **Example:** `HSET user name karthik`

### `HGET key field`
* **Returns:** The value as a Bulk String, or `(nil)` if the key or field doesn't exist.
* **Example:** `HGET user name`

### `HGETALL key`
Returns every field and value.
* **Returns:** A flat Array alternating field and value (`field1, value1, field2, value2, ...`), or an empty Array if the key doesn't exist.
* **Example:** `HGETALL user`

```
> HSET user name karthik
(integer) 1
> HSET user name Karthik V
(integer) 0
> HSET user city amaravati
(integer) 1
> HGET user name
"Karthik V"
> HGETALL user
1) name
2) Karthik V
3) city
4) amaravati
```

> **Not implemented yet:** `HDEL`, `HLEN`, `HEXISTS`, `HMGET`, `HKEYS`, `HVALS`, `HINCRBY`

---

## 🏆 Sorted Sets

Unique members ordered by a floating-point score, backed by a skip list with rank-aware spans, so ranked range queries stay fast. Members with equal scores are ordered by name.

### `ZADD key score member [score member ...]`
Adds members, or updates the score of members that already exist. Every score is checked before anything is applied, so if any score isn't a valid number (`nan` is rejected; `inf` and `-inf` are allowed) the error is returned and nothing is added or changed. If a member appears twice, the last score wins.
* **Returns:** `(integer)` How many members were **newly** added. Score updates are not counted.
* **Example:** `ZADD leaderboard 100 karthik 250 ada`

### `ZRANGE key start stop [WITHSCORES]`
Returns members by rank, lowest score first. Rank `0` is the lowest, negative ranks count from the end (`-1` is the highest), and a `stop` past the end is clamped. With `WITHSCORES` (any case) each member is followed by its score.
* **Returns:** An Array of members, or a flat Array alternating member and score with `WITHSCORES`. Empty if the key doesn't exist or the range is empty.
* **Example:** `ZRANGE leaderboard 0 -1 WITHSCORES`

```
> ZADD board 100 karthik 250 ada
(integer) 2
> ZADD board 300 karthik
(integer) 0
> ZRANGE board 0 -1
1) ada
2) karthik
> ZRANGE board 0 -1 WITHSCORES
1) ada
2) 250
3) karthik
4) 300
```

> **Not implemented yet:** `ZREM`, `ZSCORE`, `ZRANK`, `ZCARD`, `ZREVRANGE`, `ZRANGEBYSCORE`, `ZINCRBY`

---

## 🔦 Bitmaps

Bitmaps are not a separate type. Exactly as in Redis, they are bit operations on an ordinary **string**, so `TYPE` reports `string` and `GET`, `SET`, `DEL` and `EXPIRE` work on the same key. Bit `0` is the **most significant** bit of the first byte, matching Redis, and the string grows with zero bytes as needed.

### `SETBIT key offset bit`
Sets the bit at `offset` (0 or more) to `bit` (`0` or `1`), extending the string if the offset is past its end.
* **Returns:** `(integer)` The bit's **previous** value.
* **Example:** `SETBIT flags 7 1`

### `GETBIT key offset`
* **Returns:** `(integer)` The bit at `offset`. A missing key, or an offset past the end, reads as `0`.
* **Example:** `GETBIT flags 7`

### `BITCOUNT key [start end]`
Counts the bits set to 1. With a range, `start` and `end` are **byte** positions (inclusive, not bit offsets), and negative values count from the end. Give both or neither.
* **Returns:** `(integer)` The number of set bits, `0` if the key doesn't exist.
* **Example:** `BITCOUNT flags 0 0`

### `BITOP AND|OR|XOR|NOT destkey key [key ...]`
Combines strings bit by bit and stores the result in `destkey`. Shorter sources are padded with zero bytes. `NOT` takes exactly one source key. If the result is empty, `destkey` is removed.
* **Returns:** `(integer)` The length of the result in bytes.
* **Example:** `BITOP OR either a b`

```
> SETBIT a 0 1
(integer) 0
> SETBIT a 1 1
(integer) 0
> SETBIT b 1 1
(integer) 0
> SETBIT b 2 1
(integer) 0
> BITOP OR either a b
(integer) 1
> BITCOUNT either
(integer) 3
```

> **Note:** bit operations work on the value's bytes. For text that is its UTF-8 encoding, so `BITCOUNT` on `é` (bytes `c3 a9`) is 8, as in Redis, and `GET` after `SETBIT` returns the real bytes. This changed in 0.2.0: a bitmap saved by an earlier version that holds a byte of `0x80` or above was stored one code point per byte and reads differently now, so rebuild such bitmaps (bitmaps whose bytes are all below `0x80` are unaffected).

> **Not implemented yet:** `BITPOS`, `BITFIELD`

---

## 🔭 HyperLogLog

A probabilistic counter of **distinct** elements that uses a fixed ~16 KiB per key however many items you add. It uses 16,384 registers, the same as Redis, for a standard error of about 0.81%, so counts are estimates (small sets are usually exact). Hashing is stable across restarts, so counts survive persistence. Like Redis, `TYPE` reports `string`.

### `PFADD key [element ...]`
Adds elements to the counter, creating it if needed. With no elements it just creates an empty counter.
* **Returns:** `(integer)` `1` if the counter's estimate changed or the key was created, `0` otherwise.
* **Example:** `PFADD visitors alice bob carol`

### `PFCOUNT key [key ...]`
Estimates the number of distinct elements. With several keys it returns the size of their **union** without modifying any of them. Missing keys count as empty.
* **Returns:** `(integer)` The estimated cardinality.
* **Example:** `PFCOUNT visitors other`

### `PFMERGE destkey [sourcekey ...]`
Merges the source counters into `destkey` (created if needed, existing contents kept).
* **Returns:** `+OK`
* **Example:** `PFMERGE all visitors other`

```
> PFADD visitors alice bob carol
(integer) 1
> PFADD visitors alice
(integer) 0
> PFADD other carol erin
(integer) 1
> PFCOUNT visitors other
(integer) 4
> PFMERGE all visitors other
OK
> PFCOUNT all
(integer) 4
```

---

## 🌊 Streams & Consumer Groups

Append-only event logs and the worker groups that share them. How the pieces behave (IDs, blocking, pending lists, persistence, limits) is explained in [Streams & Consumer Groups](streams.md).

### `XADD key [NOMKSTREAM] [MAXLEN|MINID [=|~] threshold [LIMIT count]] id|* field value [field value ...]`
Appends an entry. `*` lets the engine pick the ID; `ms-*` picks only the sequence. Trimming runs right after the append and is always exact (`~` and `LIMIT` are accepted but ignored).
* **Returns:** The new entry's ID as a Bulk String, or `(nil)` if `NOMKSTREAM` was given and the key doesn't exist.
* **Example:** `XADD orders * item book qty 2`

### `XLEN key`
* **Returns:** `(integer)` The number of entries, `0` if the key doesn't exist.
* **Example:** `XLEN orders`

### `XRANGE key start end [COUNT n]`
Entries with IDs between `start` and `end`, oldest first. `-` and `+` are the smallest and largest IDs; a leading `(` makes a bound exclusive.
* **Returns:** An Array of `[id, [field, value, ...]]`.
* **Example:** `XRANGE orders - + COUNT 10`

### `XREVRANGE key end start [COUNT n]`
Like `XRANGE`, newest first (note the swapped bounds).
* **Returns:** An Array of `[id, [field, value, ...]]`.
* **Example:** `XREVRANGE orders + - COUNT 1`

### `XREAD [COUNT n] [BLOCK ms] STREAMS key [key ...] id [id ...]`
Entries with an ID **greater than** the one given, per stream. `$` means "only entries added from now". With `BLOCK`, waits up to `ms` milliseconds (`0` = forever) for something new. Inside `MULTI` or the standalone CLI it never waits.
* **Returns:** An Array of `[key, entries]` for streams that have something, or `(nil)`.
* **Example:** `XREAD COUNT 10 BLOCK 5000 STREAMS orders $`

### `XDEL key id [id ...]`
Removes entries. The stream's top ID is kept, so IDs are never reused.
* **Returns:** `(integer)` How many of the IDs existed.
* **Example:** `XDEL orders 1-0 2-0`

### `XTRIM key MAXLEN|MINID [=|~] threshold [LIMIT count]`
Drops the oldest entries, down to `threshold` entries (`MAXLEN`) or to IDs at or above it (`MINID`). Always exact.
* **Returns:** `(integer)` How many entries were removed.
* **Example:** `XTRIM orders MAXLEN 1000`

### `XGROUP CREATE key group id|$ [MKSTREAM]`
Creates a consumer group whose cursor starts at `id` (`0` = the whole backlog, `$` = only new entries). `MKSTREAM` creates an empty stream if the key doesn't exist. `ENTRIESREAD` is not supported.
* **Returns:** `+OK`, or `BUSYGROUP` if the group already exists.
* **Example:** `XGROUP CREATE orders shipping $ MKSTREAM`

### `XGROUP SETID key group id|$`
Moves a group's cursor, forwards or backwards.
* **Returns:** `+OK`
* **Example:** `XGROUP SETID orders shipping 0`

### `XGROUP DESTROY key group`
Removes a group, its consumers and its pending list. The stream is untouched.
* **Returns:** `(integer)` `1` if the group existed, `0` if not.
* **Example:** `XGROUP DESTROY orders shipping`

### `XGROUP CREATECONSUMER key group consumer`
* **Returns:** `(integer)` `1` if the consumer was created, `0` if it already existed.
* **Example:** `XGROUP CREATECONSUMER orders shipping w1`

### `XGROUP DELCONSUMER key group consumer`
Removes a consumer **and drops its pending entries**.
* **Returns:** `(integer)` How many pending entries the consumer had.
* **Example:** `XGROUP DELCONSUMER orders shipping w1`

### `XREADGROUP GROUP group consumer [COUNT n] [BLOCK ms] [NOACK] STREAMS key [key ...] id [id ...]`
Reads on behalf of a consumer. The ID `>` delivers entries nobody in the group has seen yet (each goes to one consumer and becomes pending); any other ID returns that consumer's own pending entries after it. `NOACK` delivers without making entries pending. `BLOCK` works with `>` only.
* **Returns:** An Array of `[key, entries]`, or `(nil)` when nothing new. A pending entry that has since been deleted is returned as `[id, (nil)]`.
* **Example:** `XREADGROUP GROUP shipping w1 COUNT 10 BLOCK 5000 STREAMS orders >`

### `XACK key group id [id ...]`
Marks entries as processed, removing them from the pending list.
* **Returns:** `(integer)` How many of the IDs were actually pending.
* **Example:** `XACK orders shipping 1-0`

### `XPENDING key group [[IDLE min-idle-time] start end count [consumer]]`
Without a range: a summary of the pending list. With one: the entries themselves.
* **Returns:** Summary: `[total, smallest-id, largest-id, [[consumer, count], ...]]` (`[0, (nil), (nil), (nil)]` when empty). Range: an Array of `[id, consumer, idle-ms, delivery-count]`.
* **Example:** `XPENDING orders shipping - + 10`

### `XINFO GROUPS key`
* **Returns:** One entry per group, as `name`, `consumers`, `pending`, `last-delivered-id`, `entries-read` and `lag` with their values. `entries-read` and `lag` are always `(nil)`.
* **Example:** `XINFO GROUPS orders`

### `XINFO CONSUMERS key group`
* **Returns:** One entry per consumer, as `name`, `pending`, `idle` and `inactive` (milliseconds; `inactive` is `-1` until the consumer has been handed an entry).
* **Example:** `XINFO CONSUMERS orders shipping`

---

## 🏎️ Telemetry & Diagnostics

Monitor the internal health and network latency of the engine in real-time.

### `INFO`
Returns a formatted string detailing engine version, memory consumption, key counts, and active connections.
* **Returns:** Bulk String

### `LATENCY DOCTOR`
Triggers the internal event loop heartbeat sensor. Reports on `asyncio` loop lag and I/O blocking metrics.
* **Returns:** Bulk String

### `SLOWLOG LEN`
Returns the total number of queries that exceeded the engine's microsecond execution threshold.
* **Returns:** `(integer)`


