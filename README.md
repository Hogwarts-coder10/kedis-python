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

Implemented using a custom Skip List inspired by Redis sorted set internals.

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

---

## 🌐 Networking

TCP server implementation supporting:

* multiple client connections
* command execution over sockets
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
TCP Server
   │
   ▼
Command Parser
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
   ▼
Persistence Layer (AOF)
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


## 🏎️ Performance Telemetry & Concurrency Curve

Kedis is rigorously stress-tested to understand its exact physical hardware limits and concurrency behavior. The following telemetry was generated using the custom Kedis Wind Tunnel benchmark script, executing **100,000 Operations** (50% `SET`, 50% `GET`) across varying thread counts with `appendfsync everysec` enabled.

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
To guarantee 100% thread safety and prevent memory corruption (`WinError 10054`), the Kedis core routing engine is protected by a Global Mutex Lock. 

* **Parallel I/O Scaling (1-10 Threads):** Throughput actually *increases* under multi-threading. While Thread A holds the lock to execute a memory write, the other threads efficiently read packets off the TCP socket in parallel, perfectly masking the network overhead. 
* **Lock Contention (15+ Threads):** As concurrent connections scale beyond the optimal window, the overhead of the Python Global Interpreter Lock (GIL) thrashing—constantly pausing and waking dozens of threads fighting for the single Mutex lock—creates significant context-switching overhead, stabilizing the throughput floor around ~10,000 RPS. 

**Verdict:** The Kedis engine is fully thread-safe, surviving 50-thread max-pressure stress tests without dropping a packet, and hits its absolute optimal operational window at **10 concurrent network connections** (21.4k RPS).

---

# ⚠ Known Limitations

Kedis is an educational systems project and is **not intended for production use**.

Current limitations include:

* custom text protocol (RESP not implemented)
* thread-based concurrency model
* simplified memory accounting
* limited fault tolerance compared to production databases

These limitations are intentional learning opportunities and areas of active development.

---

# 🛣️ Roadmap

Planned improvements:

* Buffered AOF persistence
* RESP protocol support
* Async networking
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
* TCP networking
* transaction processing
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
