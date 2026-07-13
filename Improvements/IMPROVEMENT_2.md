# Kedis Improvement Roadmap

## Overview

Kedis is a Redis-inspired in-memory datastore implemented in pure Python featuring:

* TCP networking
* Command routing
* Strings, Lists, Sets, Hashes, and Sorted Sets
* Skip List implementation
* TTL support
* AOF persistence
* Transactions
* LRU eviction
* Interactive CLI

The primary objective is not feature parity with Redis, but to explore storage engine internals, networking, persistence, and systems design concepts.

---

# Current Strengths

## Data Structures

Implemented support for:

* Strings
* Lists
* Sets
* Hashes
* Sorted Sets (Skip List)

### Notable Achievement

Custom Skip List implementation inspired by Redis ZSET internals.

---

## Persistence

Implemented:

* Append Only File (AOF)
* Recovery from AOF
* AOF compaction

---

## Networking

Implemented:

* TCP server
* Concurrent client support
* Command dispatch architecture
* Transaction buffering

---

## Packaging

Planned PyPI distribution.

---

# High Impact Improvements

## 1. Persistence Optimization

### Current Problem

Every write operation performs:

* write()
* flush()
* fsync()

This significantly limits throughput.

### Proposed Solution

Introduce buffered AOF persistence.

Architecture:

Client Request
↓
Memory Update
↓
AOF Buffer
↓
Background Persistence Thread

### Benefits

* Higher throughput
* Lower latency
* Similar durability guarantees to Redis appendfsync everysec

### Priority

CRITICAL

---

## 2. Replace Python Lists With Deques

### Current Problem

LPUSH:

O(N)

LPOP:

O(N)

Because Python lists shift memory during head operations.

### Proposed Solution

Replace internal list implementation with:

collections.deque

### Benefits

* O(1) LPUSH
* O(1) LPOP
* Better scalability

### Priority

HIGH

---

## 3. Protocol Redesign

### Current Problem

TCP currently assumes:

recv(1024)

contains exactly one command.

This is unsafe because TCP is a byte stream.

### Proposed Solution

Implement RESP-inspired protocol framing.

### Benefits

* Reliable command boundaries
* Binary-safe values
* Better client compatibility

### Priority

HIGH

---

## 4. Concurrency Safety

### Current Problem

Multiple threads operate on shared datastore state.

No synchronization currently exists.

### Proposed Solutions

Option A:

* Introduce locks

Option B (Preferred):

* Single-threaded event loop
* Async networking

### Benefits

* Eliminate race conditions
* Predictable behavior

### Priority

HIGH

---

# Medium-Term Improvements

## 5. Memory-Based Eviction

### Current State

Eviction based on key count.

### Problem

128 tiny keys and 128 huge keys are treated identically.

### Proposed Solution

Track approximate memory usage.

Possible metrics:

* Key size
* Value size
* Total memory consumption

### Goal

Redis-style memory-aware eviction.

---

## 6. Improved Skip List

### Current State

ZRANGE traverses entire structure before slicing.

### Goal

Implement rank-aware traversal.

Target complexity:

O(log N + M)

instead of:

O(N)

### Benefits

Better sorted set scalability.

---

## 7. Active Expiration Engine

### Current State

Expiration often requires scanning.

### Goal

Introduce expiration sampling.

Inspired by Redis active expiration cycles.

### Benefits

Better performance with large datasets.

---

## 8. Observability

Add:

* Command counters
* Latency statistics
* Slow query tracking
* Memory statistics
* Expired key counters

Potential commands:

INFO
SLOWLOG
LATENCY

---

# Engineering Improvements

## 9. Testing Infrastructure

Current tests are demonstration-oriented.

### Goals

Introduce:

* pytest
* integration tests
* persistence tests
* recovery tests
* concurrency tests
* fuzz tests

Target:

80%+ coverage

---

## 10. Documentation

Improve:

* Architecture diagrams
* Data structure documentation
* Persistence flow diagrams
* Complexity analysis

Add:

docs/architecture.md

docs/persistence.md

docs/networking.md

---

## 11. Code Quality

Replace decorative comments with engineering-focused documentation.

Example:

Bad:

"RUTHLESS OOM PREVENTION"

Good:

"Evict least recently used keys when cache capacity is exceeded."

Goal:

Improve maintainability and onboarding.

---

# Advanced Roadmap

## Async Networking

Replace ThreadingMixIn with:

asyncio

Potential Benefits:

* Reduced context switching
* Improved scalability
* Cleaner concurrency model

---

## Snapshot Persistence (RDB)

Implement periodic snapshots.

Benefits:

* Faster startup
* Smaller disk footprint

---

## Replication

Investigate:

Primary
↓
Replica

architecture.

Educational distributed systems milestone.

---

## Pub/Sub

Implement lightweight messaging.

---

## Metrics Dashboard

Expose telemetry:

* RPS
* Memory
* Latency
* Expiration rate
* Active clients

---

# Benchmark Goals

## Current

~5.3K Requests/Second

Mixed SET/GET workload

Pure Python

AOF enabled

---

## Target 1

10K+ Requests/Second

Buffered persistence

---

## Target 2

20K+ Requests/Second

Async persistence

---

## Target 3

50K+ Requests/Second

Protocol improvements
Memory optimization
Event-loop networking

---

# Long-Term Vision

Kedis is not intended to become a Redis replacement.

The goal is to serve as:

* A learning platform for database internals
* A systems engineering portfolio project
* A practical exploration of storage engine design
* A demonstration of networking, persistence, and data structure implementation in pure Python

Success is measured by engineering quality, architectural understanding, and educational value rather than Redis feature parity.

---

# Resume Narrative

Built Kedis, a Redis-inspired in-memory datastore in pure Python featuring TCP networking, append-only persistence, transactions, TTL support, custom skip-list-based sorted sets, and multiple native data structures. Achieved ~5.3K requests/sec under concurrent workloads and actively optimized persistence and networking architecture to improve throughput and reliability.
