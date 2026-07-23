# 🗺️ Engine Roadmap & Future Horizons

Kedis has reached its first major milestone: an asynchronous, thread-safe, network-attached data structure store with a custom protocol (KESP), optimistic concurrency control, and persistent storage.

This roadmap describes the long-term architectural direction of **Kedis-Python**. It serves as a reference implementation focused on clarity, maintainability, and education. Performance-critical innovations will primarily target **Kedis-C**, while both projects will continue to share the KESP protocol.

> **Note:** This roadmap is aspirational rather than a strict commitment. Features and priorities may evolve as the project matures.

---

# 📍 Phase 1: Storage Engine Evolution

The current engine uses Python dictionaries for O(1) key lookups. The next milestone is improving ordered data access and storage scalability.

- [ ] **B+ Tree Storage Engine**
  - Replace the flat key-value map with a custom B+ Tree index.

- [ ] **Efficient Range Queries**
  - Support ordered sequential scans for operations such as range traversal.

- [ ] **Write-Ahead Log Hardening**
  - Introduce page-aware WAL records with stronger crash recovery semantics.

---

# 📍 Phase 2: Web-Native Ecosystem

Make Kedis easier to integrate with modern web applications.

- [ ] **WebSocket Gateway**
  - Translate KESP requests into WebSocket/JSON for browser-based clients.

- [ ] **Live Monitoring Dashboard**
  - Build a Vite.js frontend showing:
    - throughput
    - latency
    - active connections
    - memory usage
    - command statistics

---

# 📍 Phase 3: Advanced Data Structures

Expand the engine beyond simple key-value storage.

- [ ] Bitmaps
- [ ] HyperLogLog
- [ ] Stream data type
- [ ] Consumer Groups
- [ ] Background memory defragmentation

---

# 📍 Phase 4: Replication & Reliability

Improve durability and distributed capabilities.

- [ ] Replica synchronization
- [ ] Snapshot scheduling
- [ ] Incremental backup support
- [ ] Replication protocol research

---

# 📍 KESP Protocol Evolution

Continue expanding and stabilizing the Kedis Engine Serialization Protocol (KESP).

- [ ] Protocol versioning
- [ ] Binary frame validation
- [ ] Additional command support
- [ ] Better error reporting
- [ ] Formal protocol specification

---

# 🚀 Future Research Ideas

These ideas are exploratory and may or may not become part of Kedis.

- Cluster mode
- Distributed sharding
- Raft-based consensus
- Disk-backed storage engine
- MVCC
- LSM Tree storage engine
- Compression techniques
- Pluggable storage backends

---

# ⚡ About Kedis-C

Performance-focused work—including the native implementation of the **KESP parser**, networking stack, and low-level memory optimizations—will be developed in **Kedis-C**.

Kedis-Python will continue serving as the reference implementation, while Kedis-C will prioritize maximum throughput and systems-level performance. Both engines will remain compatible through the shared KESP protocol.
