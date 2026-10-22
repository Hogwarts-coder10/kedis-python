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

## Framing Rules and Protocol Errors

The command decoder is strict about where one frame ends and the next begins:

- Header lines (`A<count>`, `S<length>`) end in exactly `\n`. A `\r` before it is an error. The number is a canonical ASCII decimal: no sign, spaces, underscores or leading zeros (`A+1`, `S04`, `A1_0` are all rejected).
- A bulk payload is exactly `<length>` bytes followed by exactly one `\n`. Anything else in that position is an error, so a wrong length can no longer shift the frame boundary silently.
- Header lines are capped at 32 bytes, inline lines at 64 KiB, whether or not the terminator has arrived yet. The count is of the line **without** its terminating `\n`: an inline line of exactly 65,536 bytes plus `\n` is accepted, one more byte is an error. A line that has no `\n` yet is judged by the bytes received so far, so it fails as soon as it passes the cap instead of waiting for a terminator that may never come.
- Blank lines and empty arrays (`A0`) are ignored between commands.

A frame that breaks these rules cannot be resynchronised, so the server sends one error reply (`E<message>`) and then closes the connection. A command that happens to be named `ERROR` is an ordinary command and is not treated as a framing failure.

### Reply decoding (client side)

`KESPDecoder` applies the same discipline to server replies. Header numbers (`I<n>`, `S<n>`, `A<n>`) are canonical ASCII decimals on lines of at most 32 bytes; `N` stands alone on its line; a trailing `\r` is only accepted on `+` and `-` lines, which the server writes as CRLF; and a reply starting with any other byte is an error instead of being guessed at as plain text. When the CLI hits such an error it drops the connection, because the next reply would be read from the middle of this one.

`KESPDecoder.decode(buf, lenient=True)` restores the old tolerant behaviour (CRLF anywhere, junk after `N`, plain-text lines from foreign servers) and is slated for removal in 0.3. Loose numbers are rejected in both modes.

## Reply Encoding Rules

`KESPEncoder` guarantees that everything it returns is one complete reply the strict decoder accepts.

- **Statuses and errors are replies, not data.** The magic strings (`OK`, `+OK`, `+QUEUED`, `-ERR ...`, `ERROR ...`) are only honoured for the reply itself, and an error must match the whole marker (`-ERR` or `ERROR`, then a space or the end). `ERRORS: 5` is data. The marker is removed once and the message is otherwise untouched.
- **Strings inside arrays and dicts are always bulk strings.** A list item or hash field named `ERROR x` is data, never an error element. Handlers that return a stored value as the whole reply (`GET`, `HGET`, `LPOP`, `RPOP`) wrap it in `BulkString`, so a value such as `OK` or `-ERR y` reaches the client as data.
- **Booleans are integers.** `True` is `I1`, `False` is `I0`.
- **Collection order.** Lists and tuples keep their order. A dict is flattened to `k1, v1, k2, v2, ...` in insertion order. A set has no order: its members go out in whatever order Python iterates it, which can differ between runs of the server, so a command that needs a stable order must sort before replying (`SMEMBERS` is documented as returning members in no particular order).
- **Error text is one line, and short enough to read back.** `\r` and `\n` in an error message become spaces so a message can't start a bogus reply. Error text often echoes client input (an unknown command's name, say), so it is cut to fit the decoder's 64 KiB line limit (the `E` counts, the `\n` does not): the cut is made at a character boundary and the text ends in `...`.
- **Anything unencodable becomes one error reply.** Integers of more than 31 digits, arrays above 1,048,576 elements, strings above 512 MiB, nesting deeper than 32 levels, text that no byte string can produce (a lone surrogate outside `U+DC80`-`U+DCFF`) and unknown types produce a single `EInternal server error: ...` line instead of an exception or a half-written frame.

## Binary Safety

Keys, values, field names, members and PUBLISH messages can be any bytes, in both directions. Inside the engine values are Python strings, and a byte that is not valid UTF-8 is carried as a lone surrogate `U+DC80`-`U+DCFF` (Python's `surrogateescape`), so every byte string round-trips exactly:

- The command parser and the reply decoder turn payload bytes into text this way instead of rejecting or replacing them. A value such as `\xff\xfe\x00\x80` no longer closes the connection.
- The encoder turns those characters back into the original bytes, so `GET` returns exactly what `SET` stored.
- The AOF and snapshots keep them intact (the AOF writes strings with `surrogatepass`, snapshots use JSON `\u` escapes), as does the replication snapshot.
- Bit operations work on those bytes (see `BITCOUNT` in `commands.md`).
- The CLI shows raw bytes as `\xNN` rather than failing to print them. A literal backslash in a value is shown as is, so `\xff` on screen can mean either.

Lengths and ordering stay per character, as before: `len()` of a value counts characters, and sorted-set members compare by code point.
