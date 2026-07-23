# 📡 KESP: Kedis Engine Serialization Protocol

To achieve microsecond latency, Kedis does not use HTTP, JSON, or XML. Instead, it relies on a custom, lightweight binary wire protocol called **KESP** (Kedis Engine Serialization Protocol). 

KESP is designed to be human-readable, incredibly fast to parse, and entirely resilient to TCP packet fragmentation.

## The Sigil System

Every KESP payload uses a specific starting character (a "sigil") to tell the parser exactly what type of data is arriving, followed by the data itself, and terminated by a newline (`\n`).

Here is the strict mapping of KESP sigils to their data types:

| Sigil | Data Type      | Description                                      | Example Definition    |
| :---: | :------------- | :----------------------------------------------- | :-------------------- |
| `+`   | Simple String  | Used for basic success messages.                 | `+OK\n`               |
| `S`   | Bulk String    | Used for arbitrary binary/text payloads.         | `S\nVerstappen\n`     |
| `I`   | Integer        | Used for numeric responses (e.g., counters).     | `I100\n`              |
| `A`   | Array          | Used to send multiple elements (like commands).  | `A3\n`                |
| `E`   | Error          | Used for syntax or runtime engine errors.        | `EProtocol Error\n`   |
| `N`   | Null / Nil     | Used for missing keys or aborted transactions.   | `N\n`                 |

---

## Example 1: A Standard Command

When a client wants to send a command like `SET balance 100`, it wraps the tokens in a KESP Array (`A`) of Bulk Strings (`S`).

**Client Request (Raw Bytes):**
```text
A3
S
SET
S
balance
S
100
```
**Server Response (Raw Bytes):** The server successfully mutates the data and replies with a Simple String (`+`).

Plaintext

```
+OK

```

## Example 2: Optimistic Lock Abort (Null)

If a client attempts to `EXEC` a transaction, but the `WATCH` lock detects that another connection has modified the data, KESP safely returns a Null response (`N`) to indicate the abort.

**Client Request:**

Plaintext

```
A1
S
EXEC

```

**Server Response:**

Plaintext

```
N

```

## Example 3: Nested Telemetry Payloads

For complex diagnostics like `LATENCY DOCTOR`, the engine wraps multiple data types inside an Array payload.

**Server Response (Raw Bytes):**

Plaintext

```
A2
+Event Loop Lag: 0.12ms
+Active Keys: 45

```

## Parsing Mechanics

Because TCP streams do not guarantee that a full payload will arrive in a single packet, the engine's `CommandParser` reads from a `bytearray` Surge Tank. It scans the bytes for the matching sigils and terminating `\n` characters. If an Array sigil (`A3\n`) arrives, the parser knows it must wait until exactly three distinct elements have fully materialized in the tank before attempting to route the command.
