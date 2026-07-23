# ⏳ Lifespan Mechanics: TTL & Eviction

In a high-throughput memory store, relying on infinite memory is a fatal flaw. Kedis implements a Time-To-Live (TTL) subsystem that allows developers to flag specific keys as volatile, ensuring they automatically evaporate after a set duration.

To maintain microsecond latency, the engine cannot afford to constantly scan the entire database looking for expired keys. Instead, Kedis uses a dual-pronged approach to memory eviction: **Passive** and **Active** expiration.

## The `_expires` Dictionary

Internally, Kedis does not bloat the main data structure with timestamp metadata. Instead, it maintains a completely separate, highly optimized Python dictionary called `_expires`. 

When a client runs `EXPIRE temp_session 300`, the engine calculates the exact Unix timestamp 300 seconds into the future and maps it: `_expires["temp_session"] = <future_timestamp>`.

## 1. Passive Expiration (Lazy Eviction)

This is the primary defense mechanism. Kedis evaluates expiration on-the-fly at the exact moment of access.

When a client sends a `GET temp_session` command:
1. The engine checks if `temp_session` exists in the `_expires` dictionary.
2. If it does, the engine compares the stored timestamp against the current OS clock.
3. If the timestamp is in the past, the engine actively intercepts the read. It deletes the key from both the main data store and the `_expires` dictionary, and returns `(nil)` to the client as if the key never existed.

**Advantage:** Zero background CPU overhead. The cost of eviction is baked directly into the `GET` command's O(1) time complexity.

## 2. Active Expiration (Background Sweeping)

Relying entirely on Passive Expiration creates a memory leak vulnerability: if an expired key is *never* accessed again, it will sit in RAM forever.

To combat this, Kedis runs an asynchronous background sweep on the event loop. 
* Periodically, the engine samples a small batch of keys from the `_expires` dictionary.
* It checks their timestamps.
* Any expired keys found in the sample are immediately purged.
* If a high percentage of the sampled keys were expired, the engine assumes the memory is currently volatile and aggressively triggers another sweep.

**Advantage:** Prevents stale data from silently filling up the RAM, without blocking the main event loop with a massive `O(N)` scan of the entire dataset.

## Transaction Safety

If a key is actively locked via `WATCH` by Client A, and the background sweeper evicts that key because its TTL ran out, the engine correctly registers this as a mutation. Client A's subsequent `EXEC` will safely abort, ensuring that expiring data does not cause phantom reads or race conditions.
