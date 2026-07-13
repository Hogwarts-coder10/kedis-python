# Kedis UX Roadmap

## Philosophy

Kedis is no longer just a storage engine.

It is evolving into a developer-facing database system.

A good database is not only durable and fast — it must also be understandable, observable, and pleasant to use.

The goal of the Kedis UX layer is:

```text id="ux1"
Make internal engine state visible and intuitive.
```

---

# Current UX Features

## Existing

* Rich Panels
* HELP command
* INFO dashboard
* MODE dashboard
* Reconnect Warnings
* TCP/Standalone visibility
* Color-coded status system

---

# Immediate UX Goals

## 1. Keyspace Table

### Problem

As advanced datatypes grow, users forget:

* Which keys exist
* Which datatype each key uses
* Which keys have TTL

### Solution

Create a structured Rich table.

Example:

```text id="ux2"
╭────────────────────────────────────────────╮
│ Key      │ Type   │ TTL │ Length │ Status │
├────────────────────────────────────────────┤
│ user     │ hash   │ -1  │ 3      │ Alive  │
│ tasks    │ list   │ -1  │ 5      │ Alive  │
│ skills   │ set    │ 42  │ 4      │ Alive  │
│ message  │ string │ -1  │ 12 B   │ Alive  │
╰────────────────────────────────────────────╯
```

### Benefits

* Better observability
* Faster debugging
* Easier datatype inspection
* More professional CLI feel

---

## 2. TYPE Command

### Goal

Allow users to inspect the datatype of any key.

### Example

```text id="ux3"
TYPE user
```

Output:

```text id="ux4"
hash
```

### Benefits

* Easier debugging
* Better developer experience
* Cleaner command workflows

---

## 3. Better WRONGTYPE Errors

### Current Problem

Errors may feel generic.

### Goal

Return detailed, human-readable type errors.

### Example

```text id="ux5"
LPUSH user hello
```

Output:

```text id="ux6"
WRONGTYPE Operation against key 'user'
Expected: list
Found: string
```

### Benefits

* Better clarity
* Easier debugging
* More Redis-like experience

---

# Datatype Visualization Panels

## 4. List Visualization

### Example

```text id="ux7"
╭──────── LIST tasks ────────╮
│ 0 │ study                  │
│ 1 │ code                   │
│ 2 │ sleep                  │
╰────────────────────────────╯
```

### Future Enhancements

* List length
* Pagination
* Head/Tail indicators

---

## 5. Hash Visualization

### Example

```text id="ux8"
╭──────── HASH user ─────────╮
│ name │ K                   │
│ role │ engineer            │
│ age  │ 20                  │
╰────────────────────────────╯
```

### Future Enhancements

* Sorted display
* Pretty formatting
* Nested structures (future)

---

## 6. Set Visualization

### Example

```text id="ux9"
╭──────── SET skills ────────╮
│ Python                     │
│ Linux                      │
│ C                          │
╰────────────────────────────╯
```

### Future Enhancements

* Set size
* Sorted output
* Difference/intersection preview

---

# CLI Quality Improvements

## 7. Linux Command History

### Goal

Enable:

* Arrow key history
* Persistent history
* Session recall

### Suggested Features

```text id="ux10"
↑ Previous command
↓ Next command
```

Persistent file:

```text id="ux11"
.kedis_history
```

### Future Enhancements

* History search
* Command replay

---

## 8. Command Suggestions

### Goal

Suggest corrections for invalid commands.

### Example

```text id="ux12"
HGTE user
```

Output:

```text id="ux13"
Unknown command: HGTE
Did you mean: HGET?
```

### Benefits

* Better UX
* Reduced frustration
* Faster onboarding

---

## 9. Command Auto-Completion (Future)

### Goal

Support tab completion.

### Example

```text id="ux14"
HGE<TAB>
```

↓

```text id="ux15"
HGET
```

### Future Scope

* Key completion
* Command completion
* Context-aware suggestions

---

# Observability Improvements

## 10. Enhanced INFO Dashboard

### Future Metrics

```text id="ux16"
Keys Loaded
Lists
Sets
Hashes
Expired Keys
AOF Entries
AOF Size
Compactions
Recoveries
Uptime
```

### Benefits

* Better diagnostics
* Better engine visibility
* More professional tooling

---

## 11. Persistence Status Panel

### Goal

Expose persistence state.

### Example

```text id="ux17"
╭────── Persistence ──────╮
│ Mode      : AOF         │
│ Size      : 12.4 KB     │
│ Entries   : 483         │
│ Last Save : 12 sec ago  │
╰─────────────────────────╯
```

---

# Long-Term UX Vision

## 12. Interactive Dashboard Mode

### Goal

Create a real-time monitoring mode.

### Example

```text id="ux18"
kedis monitor
```

Display:

* Commands/sec
* Connected clients
* Expired keys
* Reconnect events
* AOF growth

---

# Design Principles

## 1. Engine First

The engine layer must stabilize before heavy UX expansion.

Correctness > Cosmetics

---

## 2. Consistency

Use consistent:

* Colors
* Borders
* Error formats
* Status messages

---

## 3. Observability

Users should always understand:

* Current mode
* Current datatype
* Current persistence state
* Current errors

---

## 4. Graceful Failure

Errors should never feel hostile.

Failures should:

* Explain what happened
* Explain why
* Suggest next action

---

# Final Philosophy

Tony builds the engine.

Vettel makes it intuitive.

Verstappen ensures it survives reality.

Kedis succeeds only when all three agree.
