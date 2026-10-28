# Changelog

## 0.3.3 — 2026-10-28

Followers now hear writes in the order the master applied them, and a cancelled write can no longer let a joining follower see it twice. Both are rare races, reproduced by tests and never seen under load. `rich` is now bounded to `<16`.

### Replication
- A write whose connection is cancelled while its worker thread is still applying it (this happens at shutdown) now keeps the `SyncBarrier` until the thread has finished and the write is buffered for followers. Before, the barrier was released at once, so a `SYNC` could take its snapshot with the write half done and the follower could receive it twice. A client that merely hangs up mid-write was already handled correctly.
- Followers now hear writes in the order the master applied them. Before, a worker thread that was slow to report back after applying its write (it had released the engine lock, but not yet told the event loop) could let a later write overtake it on the way to the followers, so a follower could end with a different value or list order than the master. Plain commands, `EXEC` blocks and `XREADGROUP` passes are all covered. This was never seen under load (no reordering in about 46,000 concurrent writes), but a test reproduces it.

### Packaging
- `rich` is now limited to `>=14,<16` and `pyfiglet` to `>=1,<2` in `pyproject.toml`, matching `requirements.txt` (which said `rich<15` while installs got `rich` 15). The test suite passes on `rich` 14.3.4 and 15.0.0. A test keeps the two files in step.

## 0.3.2 — 2026-10-28

Upgrade if you run followers and consumer groups: 0.3.1 can send a joining follower a group change twice or out of order, which can move its group cursor backwards.

### Replication
- `XREADGROUP` is now covered by the exactly-once guarantee for a joining follower. Before, a group change could reach a follower twice (in its snapshot and again down the stream) or arrive after a later one, and because those records set absolute values, a late `XGLAST` could move the follower's group cursor backwards.
- A blocked `XREADGROUP` sends its group changes to followers as each pass makes them. Before, they were held back until the read ended, so a consumer registered by a read blocked with `BLOCK 0` was invisible to followers for as long as it stayed blocked.
- Removed the known gap about `XREADGROUP` from `docs/limits.md`.

## 0.3.1 — 2026-10-28

Upgrade if you run followers: 0.3.0 can lose a write, or deliver one twice, to a follower that joins while the master is taking writes.

### Replication
- A follower that joins now gets every write exactly once. Before, a write made while its baseline was being sent could be lost, and a write applied just before the baseline could arrive twice (in the baseline and again down the stream), which corrupts lists, counters and the like.
- The baseline is taken under the engine lock, so it can no longer be half of a `MULTI`/`EXEC` block or fail with a dictionary-changed error.
- A follower whose baseline cannot be sent is dropped at once, not at the next write.
- Known gap: `XREADGROUP` is not covered by this (see `docs/limits.md`).
- `XREAD`/`XREADGROUP` now put what they changed into the followers' buffers before the client gets its reply, as plain writes and `EXEC` already did. Before, a follower that attached just after a reply could receive that read's changes, depending on the Python version's event-loop scheduling.

### Packaging and CI
- A GitHub Actions workflow runs the test suite on Python 3.9 to 3.13 and builds, checks and smoke-tests the wheel.

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
