# Kedis Improvement Roadmap

## Overview

Kedis has successfully evolved from a basic key-value store into a Redis-inspired database engine featuring:

* TCP Client/Server Architecture
* AOF Persistence
* AOF Compaction
* TTL Support
* Automatic Failover
* Standalone Mode
* Reconnect Logic
* Rich TUI Dashboard
* INFO / MODE / HELP Commands

The next stage should focus on improving correctness, scalability, and database-engine concepts rather than adding random features.

---

# High Priority

## 1. Active Expiration Engine

### Current State

Expired keys are only removed when accessed.

Example:

```text
SET session abc
EXPIRE session 10
```

If the key is never touched again, it remains in memory indefinitely.

### Improvement

Implement a background expiration cycle that periodically scans a subset of keys and removes expired entries.

### Learning Outcome

* Redis expiration strategy
* Background maintenance tasks
* Memory management

---

## 2. Robust TCP Protocol

### Current State

The server assumes:

```text
1 send = 1 receive
```

which works for small commands but is not guaranteed by TCP.

### Improvement

Implement message framing.

Example:

```text
COMMAND_LENGTH
COMMAND_DATA
```

or newline-delimited commands.

### Learning Outcome

* TCP streams
* Message framing
* Real-world networking

---

## 3. AOF Integrity Verification

### Current State

A corrupted AOF file may break recovery.

### Improvement

Validate commands during replay.

Possible features:

* Skip malformed entries
* Recovery warnings
* Recovery statistics

### Learning Outcome

* Fault tolerance
* Recovery engineering

---

## 4. Unit Test Suite

### Current State

Testing is performed manually.

### Improvement

Introduce pytest-based tests.

Suggested areas:

* SET/GET
* TTL
* AOF Recovery
* Compaction
* TCP Mode
* Reconnect Logic

### Learning Outcome

* Automated testing
* Regression prevention

---

# Medium Priority

## 5. Lists

Commands:

```text
LPUSH
RPUSH
LPOP
RPOP
LRANGE
```

### Learning Outcome

* Dynamic data structures
* Redis internals

---

## 6. Hashes

Commands:

```text
HSET
HGET
HDEL
HKEYS
```

### Learning Outcome

* Structured storage
* Nested dictionaries

---

## 7. Sets

Commands:

```text
SADD
SREM
SMEMBERS
SINTER
SUNION
```

### Learning Outcome

* Set operations
* Fast membership checks

---

## 8. Persistence Statistics

Expand INFO output.

Example:

```text
AOF Entries
AOF Size
Recoveries
Compactions
Uptime
```

### Learning Outcome

* Observability
* Database telemetry

---

# Advanced Goals

## 9. Snapshot System (RDB-Inspired)

Add a secondary persistence mode.

Example:

```text
CHECKPOINT
```

creates a compact snapshot of memory state.

### Learning Outcome

* Snapshotting
* Database recovery models

---

## 10. Transactions

Commands:

```text
BEGIN
COMMIT
ROLLBACK
```

### Learning Outcome

* Atomicity
* Consistency

---

## 11. Replication

Primary:

```text
Server A
```

Replica:

```text
Server B
```

### Learning Outcome

* Distributed systems
* Event propagation

---

## 12. Pub/Sub

Commands:

```text
SUBSCRIBE
PUBLISH
```

### Learning Outcome

* Messaging systems
* Event-driven architecture

---

# Long-Term (KDB v2)

## Vector Storage

Commands:

```text
VSET
VGET
```

Store vector embeddings.

---

## Similarity Search

Commands:

```text
VSEARCH
```

Support:

* Cosine Similarity
* Euclidean Distance

---

## Approximate Nearest Neighbour Search

Potential algorithms:

* HNSW
* IVF
* Flat Index

### Learning Outcome

* Vector databases
* AI infrastructure
* RAG internals

---

# Engineering Philosophy

Because using Redis teaches me how to use Redis.

Building Kedis teaches me how Redis works.
