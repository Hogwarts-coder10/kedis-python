# 🚀 Kedis-Python

**A Redis-inspired in-memory datastore built in pure Python to explore storage engines, networking, persistence, caching, and systems architecture.**

> Built to understand systems, not just use them.

---

# 📖 Overview

Kedis-Python is a Redis-inspired in-memory datastore implemented from scratch in Python.

The project was created to explore how modern in-memory databases work internally by implementing core components such as:

* storage engines
* command routing
* networking
* persistence
* memory management
* transactions
* data structures
* replication

Rather than focusing on Redis compatibility, Kedis focuses on understanding the architectural and engineering principles behind high-performance backend systems.

---

# ✨ Features

## 🔑 Core Key-Value Operations

* SET
* GET
* DEL
* EXISTS

---

## 📦 Multiple Data Structures

### Strings

```bash
SET user karthik
GET user
```

### Lists

```bash
LPUSH tasks coding
RPUSH tasks testing
LPOP tasks
```

### Sets

```bash
SADD skills python
SADD skills systems
SMEMBERS skills
```

### Hashes

```bash
HSET user name karthik
HGET user name
```

### Sorted Sets (Skip Lists)

```bash
ZADD leaderboard 100 karthik
ZRANGE leaderboard
```

Implemented using a custom Skip List inspired by Redis sorted set internals, with rank-aware span tracking for fast positional queries.

---

## ⏳ Expiration Support

```bash
EXPIRE session 60
TTL session
```

Supports automatic key expiration.

---

## 💾 Persistence

Append-Only File (AOF) persistence:

* durable write logging
* automatic recovery on startup
* AOF compaction support

---

## 🧠 Memory Management

LRU-based eviction support:

* tracks key usage
* evicts least recently used entries when limits are reached

---

## 🔄 Transactions

Supports transactional execution:

```bash
MULTI
SET a 1
SET b 2
EXEC
```

Optimistic locking via `WATCH` / `UNWATCH`:

```bash
WATCH balance
MULTI
DECR balance
EXEC
```

If a watched key is modified by another client before `EXEC`, the transaction is aborted instead of committing against stale state — preventing race conditions on concurrent read-modify-write operations.

---

## 🔌 Custom Wire Protocol (KESP)

Kedis implements its own binary-safe wire protocol, KESP, rather than adopting RESP.

* Requests and responses are framed with explicit byte-length prefixes, so the parser never has to guess where one message ends and the next begins — the bug class that affects naive `recv()`-based servers
* Implemented in `parser.py` as a dedicated encoder/decoder, decoupled from the networking layer

---

## 🔁 Replication

Kedis supports master-replica replication:

* Replicas connect to a master and perform a `SYNC` handshake to receive the initial dataset
* The master streams subsequent writes to connected replicas in real time
* Replicas are read-only — writes issued directly to a replica are rejected

---

## 🌐 Networking

Asyncio-based server implementation supporting:

* many concurrent client connections on a single event loop
* command execution over sockets via the KESP protocol
* standalone local mode fallback

---

## 📊 Observability

Built-in INFO command exposing:

* key counts
* data type statistics
* persistence information
* expiration information
* runtime metadata

---

## 📴 Offline / Standalone Mode

When the server becomes unavailable:

* users can switch to standalone mode
* local operations continue
* users are warned about possible state divergence
* reconnection remains user-controlled

This feature was added to explore failure handling and graceful degradation.

---

# 🏗️ Architecture

```text
Client
   │
   ▼
Asyncio Server
   │
   ▼
KESP Parser
   │
   ▼
Command Router
   │
   ▼
Storage Engine
   ├── Strings
   ├── Lists
   ├── Sets
   ├── Hashes
   └── Sorted Sets (Skip Lists)
   │
   ├──────────────┐
   ▼              ▼
Persistence     Replication
Layer (AOF)     (Master → Replica)
```

The architecture is intentionally modular to make experimentation and future rewrites easier.

---

# ⚡ Benchmark
Single-thread localhost benchmark
100,000 total operations (50,000 SET + 50,000 GET)
```
| AOF Mode | Throughput |
|-----------|------------|
| appendfsync always | ~1,081 req/sec |
| appendfsync everysec | ~13,898 req/sec |
```
### Observation

Persistence strategy has a significant impact on throughput.

`appendfsync always` prioritizes durability by forcing a disk sync after every write.

`appendfsync everysec` batches synchronization operations, significantly improving throughput while accepting up to one second of potential data loss during unexpected crashes.

---

### Benchmark Configuration

- Host: localhost
- Threads: 1
- Operations: 100,000
- Workload:
  - SET
  - GET
- Persistence:
  - appendfsync always
  - appendfsync everysec
 
---

# 🧪 Testing

The project includes automated tests covering:

* command execution
* data structures
* persistence recovery
* expiration behavior

Additional coverage is actively being expanded as the project evolves.

---

# 🎯 Design Goals

Kedis was created to explore:

* storage engine design
* command-driven architectures
* networking fundamentals
* persistence strategies
* memory management
* data structure implementation
* systems engineering tradeoffs

---

# 🤔 Why Skip Lists?

Sorted Sets are implemented using Skip Lists.

Reasons:

* expected O(log N) insertion
* expected O(log N) lookup
* natural ordered traversal
* simpler implementation than self-balancing trees

This mirrors the design approach used by Redis for sorted sets.

---

# 🏎️ Concurrency Ceiling: GIL Contention Analysis

This benchmark characterizes Kedis's original threading limitations under the GIL, and was the direct motivation for moving the networking layer to asyncio.

<img width="600" height="390" alt="image" src="https://github.com/user-attachments/assets/a9caa7f5-2390-4fc0-a9d4-96c1f36f6800" />

### The Dyno Sheet (Hardware Limits)

| Active Threads | Throughput (Requests/Sec) |
| :--- | :--- |
| **1 Thread** | 13,898 RPS |
| **2 Threads** | 15,631 RPS |
| **5 Threads** | 18,390 RPS |
| **10 Threads** | **21,492 RPS** |
| **20 Threads** | 13,984 RPS |
| **50 Threads** | 10,977 RPS |

### Systems Analysis: The Python GIL & Global Mutex Lock
The original Kedis core routing engine used a Global Mutex Lock to serialize access to shared state. No data corruption was observed across stress tests up to 50 threads.

* **Parallel I/O Scaling (1-10 Threads):** Throughput actually *increased* under multi-threading. While Thread A held the lock to execute a memory write, the other threads efficiently read packets off the TCP socket in parallel, perfectly masking the network overhead. 
* **Lock Contention (15+ Threads):** As concurrent connections scaled beyond the optimal window, the overhead of the Python Global Interpreter Lock (GIL) thrashing—constantly pausing and waking dozens of threads fighting for the single Mutex lock—created significant context-switching overhead, stabilizing the throughput floor around ~10,000 RPS. 

**Takeaway:** Throughput peaked at ~10 concurrent connections (21.4k RPS) and degraded past that point as GIL contention dominated — dropping to ~11k RPS at 50 threads. This was a hard ceiling imposed by CPython's threading model, not a tunable parameter — and the direct motivation for the move to asyncio networking, now shipped.

---

# ⚠ Known Limitations

Kedis is an educational systems project and is **not intended for production use**.

Current limitations include:

* simplified memory accounting
* limited fault tolerance compared to production databases
* no memory-based eviction ceiling (LRU tracks usage, but isn't yet tied to a hard memory limit)
* AOF persistence is not buffered — writes can trigger a sync depending on config

These limitations are intentional learning opportunities and areas of active development.

---

# 🛣️ Roadmap

Planned improvements:

* Buffered AOF persistence
* Improved observability
* Memory-based eviction
* Enhanced benchmark tooling
* Additional persistence optimizations

---

# 📚 Learning Outcomes

This project explores concepts commonly found in modern backend systems:

* Redis-style architecture
* command dispatch systems
* persistence mechanisms
* cache eviction policies
* asyncio networking
* custom wire protocol design
* replication
* transaction processing and optimistic locking
* skip lists
* systems performance analysis

---

# 🚀 Future Direction

Kedis serves as an architecture and systems-design exploration platform before moving toward lower-level implementations and more advanced storage engine designs.

The long-term goal is to understand how production systems are engineered, optimized, and maintained.

---

# 🤝 Contributing

Suggestions, issues, and discussions are welcome.

This project is primarily a learning and exploration platform, but contributions are appreciated.

---

# 👨‍💻 Author

**V SS Karthik**

AI/ML Student • Systems Enthusiast • Builder of developer tools and infrastructure projects

> "Built to understand systems, not just use them."
