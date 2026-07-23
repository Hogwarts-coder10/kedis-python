# 💾 Persistence: The I/O Drivetrain

In-memory databases are inherently volatile; if the power goes out, the RAM clears, and the data is lost. Kedis solves this using a heavily optimized **Append-Only File (AOF)** architecture that guarantees crash resilience without choking the `asyncio` network loop.

## Why AOF? (Write-Ahead Logging)

Instead of pausing the engine to take massive, blocking snapshots of the entire memory state, Kedis treats the disk as a continuous stream of events. 

Every mutating command (e.g., `SET`, `DEL`, `HSET`) is logged sequentially to an `.aof` file on the disk *before* it is finalized in memory. When the engine reboots, it reads this log from top to bottom, feeding the historical commands back through the internal execution router to perfectly rebuild the database state in RAM.

## The Blocking Problem & Thread Delegation

Writing to a physical SSD is significantly slower than writing to RAM. In a single-threaded Python `asyncio` environment, waiting for a disk write to finish will freeze the event loop, causing thousands of connected network clients to time out. 

**The Solution:** Kedis strictly decouples disk I/O from network I/O.
Using `asyncio.to_thread()`, the engine pushes the actual file writing operation into a background OS worker thread. The main event loop instantly returns a `+OK` to the client while the disk safely finalizes the write in the background.

## Adjustable Fsync Policies

Kedis allows administrators to tune the I/O Drivetrain based on their specific need for speed vs. safety. 

* **`appendfsync always` (Maximum Safety):** 
  Every single write command is immediately flushed to the physical disk before the next command is processed. Slower, but mathematically guarantees zero data loss in a crash.
  
* **`appendfsync everysec` (Maximum Throughput):** 
  The engine pools the logs in memory and hands them off to the background thread to be bulk-flushed to the SSD exactly once per second. This is the default setting and massively boosts Transactions Per Second (TPS) while only risking a maximum of 1 second of data loss.

## Log Compaction (`COMPACT`)

Because AOF logs every single mutation, a counter that changes from 1 to 100 will generate 100 lines in the log file, even though the final state is just `100`. 

To prevent the `.aof` file from infinitely bloating, Kedis features a `COMPACT` command. When triggered:
1. The engine pauses the background logger.
2. A background thread scans the current, precise state of the RAM.
3. It writes a completely fresh, minimized `.aof` file containing only the commands needed to recreate the current state.
4. The old, bloated log is hot-swapped for the new one, and normal logging resumes.
