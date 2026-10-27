# Changelog

## Unreleased

### Replication
- A follower that joins now gets every write exactly once. Before, a write made while its baseline was being sent could be lost, and a write applied just before the baseline could arrive twice (in the baseline and again down the stream), which corrupts lists, counters and the like.
- The baseline is taken under the engine lock, so it can no longer be half of a `MULTI`/`EXEC` block or fail with a dictionary-changed error.
- A follower whose baseline cannot be sent is dropped at once, not at the next write.
- Known gap: `XREADGROUP` is not covered by this (see `docs/limits.md`).

## 0.3.0 — 2026-10-27

### Replication
- `MULTI`/`EXEC` is now replicated. The master runs the block under one hold of the engine lock and sends it to followers as one `TXBATCH` transaction, which a follower applies as a unit. A bad or cut-off batch drops the leader.
- `EXEC` is isolated: another client's command can no longer run in the middle of a block.
- A read-only follower refuses writes inside `MULTI` too. The write gets `-EREADONLY` when it is queued, the block is poisoned, and `EXEC` aborts with `-EXECABORT` (also when the node became a follower after the write was queued).
- A follower loads the leader's snapshot without upper-casing it, keeps its follower task alive, stops it on `REPLICAOF NO ONE`, and reads the leader's stream under the same client budgets as any client.
- The master drops followers that stop reading.

### Server
- Every limit can be set with a command-line flag.
- One `MULTI` block may queue at most `max-multi-commands` commands and `max-multi-bytes` bytes; past either, the block is poisoned and `EXEC` aborts.
- Clients that do not read their replies are dropped.
- Big replies are encoded in slices so other clients keep moving.

### KESP
- Whole-reply budgets for the reply decoder and encoder.
- Oversized server error lines are cut so our own client accepts them.

### Testing and docs
- A mutation fuzzer, reference readers and property tests; a fuzzing guide, the line-limit and collection-order rules, and the new budgets are documented.

### Packaging
- Removed `kedis_python/replica.py`, an old unused copy of the server that was shipped in the wheel.
