# Kedis Panel System Architecture

## Philosophy

As Kedis evolves from a simple key-value engine into a multi-datatype database system, observability becomes essential.

Panels are not cosmetic features.

They are visibility tools.

The goal of the Kedis panel system is:

```text id="p1"
Make internal database state understandable at a glance.
```

---

# Why Panels Matter

Kedis now contains:

* TCP Networking
* Standalone Mode
* AOF Persistence
* TTL Expiration
* Recovery Systems
* Lists
* Sets
* Hashes
* Reconnect Logic

As complexity grows, plain text output becomes difficult to read.

Panels improve:

* Readability
* Diagnostics
* User Experience
* Debugging
* Professional Feel

---

# Panel Design Philosophy

## 1. Every Panel Must Have Purpose

A panel should answer one of:

```text id="p2"
What happened?
What is the current state?
What should the user know?
```

Avoid unnecessary visual noise.

---

## 2. Consistent Color Language

### Green

Used for:

* Success
* Healthy state
* Completed operations
* Active connections

Example:

```text id="p3"
✓ Reconnected successfully
```

---

### Yellow

Used for:

* Warnings
* Failovers
* Recovery notices
* Temporary problems

Example:

```text id="p4"
⚠ Switching to standalone mode
```

---

### Red

Used for:

* Fatal errors
* Corruption
* Crashes
* Invalid operations

Example:

```text id="p5"
✖ Recovery failed
```

---

### Blue

Used for:

* Information
* Statistics
* Metadata
* Diagnostics

Example:

```text id="p6"
INFO dashboard
```

---

### Purple / Cyan

Used for:

* Database mode
* Engine state
* Internal diagnostics

Example:

```text id="p7"
MODE dashboard
```

---

# Core Panel Categories

# 1. Status Panels

Panels describing current engine state.

## Examples

### MODE

```text id="p8"
╭──────── KEDIS MODE ────────╮
│ Mode       : Standalone    │
│ Persistence: AOF           │
│ Radar      : Active        │
│ Debug      : OFF           │
╰────────────────────────────╯
```

---

### INFO

```text id="p9"
╭──────── DATABASE INFO ─────╮
│ Keys         : 12          │
│ Lists        : 3           │
│ Hashes       : 2           │
│ Sets         : 4           │
│ AOF Size     : 12.4 KB     │
╰────────────────────────────╯
```

---

# 2. Datatype Panels

Panels visualizing internal data structures.

---

## LIST PANEL

```text id="p10"
╭──────── LIST tasks ────────╮
│ 0 │ study                  │
│ 1 │ code                   │
│ 2 │ sleep                  │
╰────────────────────────────╯
```

---

## HASH PANEL

```text id="p11"
╭──────── HASH user ─────────╮
│ name │ K                   │
│ role │ engineer            │
│ age  │ 20                  │
╰────────────────────────────╯
```

---

## SET PANEL

```text id="p12"
╭──────── SET skills ────────╮
│ Python                     │
│ Linux                      │
│ C                          │
╰────────────────────────────╯
```

---

# 3. Keyspace Panels

Panels showing database overview.

## KEYS TABLE

```text id="p13"
╭────────────────────────────────────────────╮
│ Key      │ Type   │ TTL │ Length │ Status │
├────────────────────────────────────────────┤
│ user     │ hash   │ -1  │ 3      │ Alive  │
│ tasks    │ list   │ -1  │ 5      │ Alive  │
│ skills   │ set    │ 42  │ 4      │ Alive  │
│ message  │ string │ -1  │ 12 B   │ Alive  │
╰────────────────────────────────────────────╯
```

---

# 4. Warning Panels

Panels used for graceful failure handling.

## Connection Lost

```text id="p14"
╭────── Connection Lost ──────╮
│ Network database unavailable│
│                              │
│ Switching to standalone mode│
│ may create divergent state. │
╰─────────────────────────────╯
```

---

## Reconnect Available

```text id="p15"
╭──── Network Available ──────╮
│ A Kedis server is now active│
│                              │
│ Reconnect? [Y/n]            │
╰─────────────────────────────╯
```

---

# 5. Action Panels

Panels confirming successful operations.

## COMPACT Success

```text id="p16"
╭────── COMPACTION COMPLETE ──────╮
│ Old AOF Size : 8.2 MB           │
│ New AOF Size : 0.4 MB           │
│ Reduction     : 95%             │
╰─────────────────────────────────╯
```

---

## Recovery Success

```text id="p17"
╭──────── RECOVERY COMPLETE ───────╮
│ Keys Loaded      : 42            │
│ Expired Removed  : 5             │
│ Recovery Time    : 0.12 sec      │
╰──────────────────────────────────╯
```

---

# Future Panel Ideas

## Persistence Dashboard

```text id="p18"
AOF Entries
Last Compaction
Recovery Count
Persistence Status
```

---

## Live Monitor Mode

```text id="p19"
Commands/sec
Connected Clients
Reconnect Events
Memory Usage
```

---

## Debug Panel

```text id="p20"
Active Mode
Socket State
AOF Handle
Recovery Status
```

---

# UX Rules

## 1. Titles Everywhere

Bad:

```text id="p21"
╭──────────────────╮
│ Keys: 12         │
╰──────────────────╯
```

Good:

```text id="p22"
╭──── DATABASE INFO ────╮
│ Keys: 12              │
╰───────────────────────╯
```

---

## 2. Errors Must Teach

Bad:

```text id="p23"
Wrong type
```

Good:

```text id="p24"
WRONGTYPE Operation against key 'tasks'
Expected: list
Found: string
```

---

## 3. Panels Must Reduce Cognitive Load

A user should understand the system quickly without reading excessive text.

---

# Final Philosophy

Tony builds the engine.

Vettel builds the cockpit.

Verstappen crash-tests the machine.

Kedis succeeds when all three survive the race.
