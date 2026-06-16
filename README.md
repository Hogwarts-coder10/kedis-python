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

Current benchmark results:

```text
~5300 requests/sec
```

Environment:

* Python 3.x
* TCP networking enabled
* Concurrent client workload
* Mixed GET / SET operations
* AOF persistence enabled

Benchmarking is ongoing as persistence and networking layers continue to evolve.

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
