# 🌊 Streams & Consumer Groups: The Event Log

A **stream** is an append-only log of entries. Each entry has a unique, ever-increasing **ID** and a list of field/value pairs. Streams are how Kedis handles event feeds, job queues and anything where "what happened, in order" matters more than "what is the current value".

Consumer groups sit on top: several workers share one stream, each entry goes to exactly one of them, and an entry stays on a **pending list** until its worker acknowledges it.

Command syntax lives in the [Command Spellbook](commands.md#-streams--consumer-groups). This page explains how the pieces behave.

---

## 🪪 Entry IDs

An ID is `<milliseconds>-<sequence>`, for example `1718000000000-0`.

* IDs are strictly increasing for the life of the stream. `XADD ... *` picks `now-0`, or `now-<next sequence>` if that millisecond is taken. A clock that moves backwards never reorders IDs.
* You can give an explicit ID (`5-5`) or a partial one (`7-*`, auto sequence). It must be greater than the stream's top ID, and `0-0` is never valid.
* **The top ID survives deletes and trims.** Emptying a stream with `XDEL` or `XTRIM` does not let an old ID be reused.
* A bare `ms` (no sequence) in a range or read means `ms-0` for a start bound and `ms-<max>` for an end bound.

---

## 📥 Writing and Trimming

`XADD` appends one entry and returns its ID. Options go between the key and the ID:

* `NOMKSTREAM` returns `(nil)` instead of creating the key when it doesn't exist.
* `MAXLEN threshold` or `MINID threshold` trims right after the append (not both together). `=` and `~` are accepted before the threshold, and `LIMIT count` after `~`.

> **Trimming is always exact.** `~` and `LIMIT` are accepted so that Redis-style commands keep working, but they are ignored: the stream is trimmed to exactly the threshold.

`XDEL` removes specific entries and `XTRIM` trims on demand. Both leave the stream's top ID alone.

---

## 📤 Reading

* `XRANGE key start end [COUNT n]` and `XREVRANGE key end start [COUNT n]` return entries between two IDs, inclusive. `-` and `+` are the smallest and largest possible IDs, and a leading `(` makes a bound exclusive.
* `XREAD [COUNT n] [BLOCK ms] STREAMS key ... id ...` returns entries with an ID **greater than** the one you give, from one or more streams. Streams with nothing new are left out, and if none have anything the reply is `(nil)`.
* `$` as an ID means "only entries added after now".

### Blocking reads

`BLOCK ms` makes a read wait for new entries instead of returning `(nil)` straight away. `BLOCK 0` waits forever.

* `$` is pinned to the stream's top ID **once, before waiting**. An entry added in the gap between your read and your wait is never missed.
* A write to any stream wakes every blocked reader; each re-checks and goes back to sleep if its own streams have nothing new.
* On timeout the reply is `(nil)`. A client that disconnects while blocked is cleaned up.
* An error (wrong type, unknown group) ends the wait at once instead of after the timeout.

Waiting needs *another client* to do the writing, so it only happens for clients of the async server. Two places run a command inline and never wait:

* **Inside `MULTI`/`EXEC`.** Blocking reads return immediately with their current result, exactly as Redis does in a transaction.
* **Standalone (offline) CLI mode.** There is no other client to wake a blocked read, so `BLOCK` returns at once, and the CLI prints a note saying so.

---

## 👥 Consumer Groups

A group is a **cursor** over a stream (its *last-delivered ID*), a set of **consumers**, and a **pending entries list (PEL)** of everything delivered but not yet acknowledged.

```
> XGROUP CREATE orders shipping 0
OK
> XREADGROUP GROUP shipping w1 COUNT 1 STREAMS orders >
1) 1) "orders"
   2) 1) 1) "1-0"
         2) 1) "item"
            2) "book"
            3) "qty"
            4) "2"
> XPENDING orders shipping
1) (integer) 1
2) "1-0"
3) "1-0"
4) 1) 1) "w1"
      2) "1"
> XACK orders shipping 1-0
(integer) 1
```

### Creating a group

`XGROUP CREATE key group id` starts the cursor at `id`: `0` delivers the whole backlog, `$` only entries added from now on. `MKSTREAM` creates an empty stream if the key doesn't exist (otherwise a missing key is an error). `XGROUP SETID` moves the cursor of an existing group, in either direction.

### Delivering entries

`XREADGROUP GROUP group consumer ... STREAMS key ... id ...` has two modes, chosen per stream by the ID:

| ID | Meaning |
| :-- | :-- |
| `>` | **New entries**: ones no consumer in the group has been given. Each is delivered to exactly one consumer, added to the PEL (delivery count 1) and the cursor moves past it. |
| any other ID | **This consumer's own history**: its pending entries with an ID greater than the one given. Each counts as another delivery attempt. |

* A consumer is created the first time it is named.
* `NOACK` skips the PEL: entries are delivered and the cursor advances, but nothing becomes pending.
* `$` is rejected for groups: use `>` for new entries or a real ID for history.
* `BLOCK` works with `>` exactly like `XREAD BLOCK`. Reading history never blocks: it answers at once, even with an empty list.
* If a group is destroyed while a consumer is blocked on it, the wait ends with `NOGROUP`.
* If an entry is `XDEL`ed or trimmed while still pending, it stays pending, and a history read shows it as its ID with `(nil)` in place of the fields. `XACK` clears it.
* Giving the same stream twice in one call is fine: the second sees what the first delivered.

### Acknowledging and inspecting

`XACK` removes entries from the PEL and returns how many were actually pending. Unknown IDs, groups and keys count as zero.

`XPENDING key group` summarises the PEL: total, smallest ID, largest ID and a count per consumer. With a range (`start end count [consumer]`, optionally `IDLE min-idle-time` first) it lists entries as `[id, consumer, idle-ms, delivery-count]`.

`XINFO GROUPS key` and `XINFO CONSUMERS key group` describe groups and consumers (below). `XGROUP DELCONSUMER` removes a consumer and **drops its pending entries**, returning how many it had; `XGROUP DESTROY` removes the whole group.

```
> XINFO GROUPS orders
1) 1) "name"
   2) "shipping"
   3) "consumers"
   4) (integer) 1
   5) "pending"
   6) (integer) 0
   7) "last-delivered-id"
   8) "1-0"
   9) "entries-read"
   10) (nil)
   11) "lag"
   12) (nil)
```

---

## ⚠️ Errors

| Reply | When |
| :-- | :-- |
| `NOGROUP No such key 'k' or consumer group 'g' ...` | `XREADGROUP` or `XPENDING` on a missing key or group |
| `NOGROUP No such consumer group 'g' for key name 'k'` | `XGROUP` subcommands and `XINFO CONSUMERS` on a missing group |
| `BUSYGROUP Consumer Group name already exists` | `XGROUP CREATE` of an existing group |
| `The XGROUP subcommand requires the key to exist...` | `XGROUP` on a missing key without `MKSTREAM` |
| `Invalid stream ID specified as stream command argument` | a malformed ID anywhere |
| `Invalid group or consumer name ...` | a name that is not printable ASCII without spaces |
| `WRONGTYPE ...` | any stream command on a key holding something else |

---

## 💾 Persistence & Replication

Streams are logged and replicated as **fully resolved records**: every ID, timestamp and delivery count is decided once, on the master, and written down. Replaying the AOF, restoring a snapshot or feeding a replica therefore rebuilds exactly the same state, including every `*` ID and every pending entry. The record formats are listed in [Persistence](persistence.md#-streams-in-the-log).

* Replicas receive `XADD` with the ID the master assigned, never a `*`.
* Group changes are sent to replicas as the records they produced, wrapped in an internal `XGAPPLY` command. Replicas never re-run `XREADGROUP`, which would pick its own entries and timestamps. A read that changed nothing sends nothing.
* `XGAPPLY` exists only for replication: network clients get `unknown command`, and the CLI doesn't offer it.
* `XGROUP`, `XREADGROUP` and `XACK` are writes: a read-only replica refuses them.
* Snapshots and `COMPACT` keep groups, consumers and the PEL.

---

## 🚧 Known Limits

* **No `XCLAIM` or `XAUTOCLAIM`.** A dead consumer's pending entries can be listed and acknowledged, but not taken over by another consumer.
* **`XINFO STREAM` is not implemented**, only `XINFO GROUPS` and `XINFO CONSUMERS`.
* **`ENTRIESREAD` is rejected**, and `entries-read` and `lag` in `XINFO GROUPS` are always `(nil)`.
* **Names:** group and consumer names must be printable ASCII with no spaces, because they are written to the whitespace-separated AOF. Stream *field and value text* has no such limit.
* **Exact trimming only** (see above).
* **Consumer idle time** (`idle`, `inactive` in `XINFO CONSUMERS`) only advances when a read creates the consumer or delivers it entries. Empty reads and `NOACK` reads don't count, which keeps replicas identical to the master.
* **Inside `MULTI`, queued commands are acknowledged with `+OK`**, not `+QUEUED` as in Redis.
