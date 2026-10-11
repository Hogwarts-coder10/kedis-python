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
Removes one or more specified keys from the storage engine. Ignores keys that do not exist.
* **Returns:** `(integer)` The number of keys successfully removed.
* **Example:** `DEL session_123 session_456`

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

## 🛡️ Transactions & Concurrency (Optimistic Locking)

Kedis ensures thread-safe data mutations through isolated execution queues and version tracking.

### `WATCH key [key ...]`
Locks the specified keys to your current session. If another client modifies any of these keys before you call `EXEC`, your transaction will be safely aborted to prevent a race condition.
* **Returns:** `+OK`

### `UNWATCH`
Flushes all active locks on your current session, allowing transactions to proceed without version verification.
* **Returns:** `+OK`

### `MULTI`
Opens a transaction block. Subsequent commands are not executed immediately; they are queued in the session's Surge Tank.
* **Returns:** `+OK`

### `EXEC`
Executes all queued commands in a single, atomic block. If a `WATCH` lock was tripped by another client, the transaction aborts.
* **Returns:** An Array of the command results, or `(nil)` if the transaction was aborted.

### `DISCARD`
Flushes the transaction queue and exits the `MULTI` state without executing any commands.
* **Returns:** `+OK`

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


