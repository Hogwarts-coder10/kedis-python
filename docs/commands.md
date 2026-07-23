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


