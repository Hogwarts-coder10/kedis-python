# Limits & Budgets

What one client can make the server spend, and where each cap lives. Every value is a module or class attribute, so you can change it before starting the server, for example:

```python
from kedis_python import server
from kedis_python.parser import CommandParser

server.MAX_CLIENTS = 500
CommandParser.MAX_COMMAND_BYTES = 64 * 1024 * 1024
server.run()
```

There are no command-line flags for these yet.

## What a command may be (`CommandParser`)

| Limit | Default | When it is exceeded |
|---|---|---|
| `MAX_HEADER_LINE` | 32 bytes | the `A<n>` / `S<n>` lines |
| `MAX_INLINE_LENGTH` | 64 KiB | one inline (plain text) command line |
| `MAX_ARRAY_LENGTH` | 1,048,576 | elements in one command |
| `MAX_BULK_LENGTH` | 512 MiB | one string |
| `MAX_COMMAND_BYTES` | 512 MiB | **all** string payload in one command, added up |

The aggregate cap is checked as each string header arrives, before its payload is sent, so a command that declares too much is refused immediately. Breaking any of these gets one error reply and the connection is closed (see [protocol.md](protocol.md)).

A command at the cap needs several times its size in memory for a moment (the receive buffer, the decoded text, and the AOF record), so lower `MAX_COMMAND_BYTES` if the host is small.

## What a connection may cost (`server.py`)

| Setting | Default | Effect |
|---|---|---|
| `MAX_CLIENTS` | 10,000 | The next connection gets `Emax number of clients reached` and is closed. A slot frees when a client disconnects. |
| `STALL_TIMEOUT` | 30 s | A half-received command that gets no new byte for this long is dropped. |
| `FRAME_DEADLINE` + `FRAME_MIN_RATE` | 60 s + size / 256 KiB per s | A half-received command still unfinished after this long is dropped, even if bytes keep trickling in. A 64 MiB upload is allowed about 5 minutes. |
| `YIELD_EVERY` | 256 | Commands run back to back before other clients get a turn. |

Both timeouts send `EProtocol error: timed out waiting for the rest of a command` and close the connection. They only apply while a command is half-sent: a connection with nothing pending can idle for as long as it likes, so `SUBSCRIBE` and blocking reads are not affected.

The server also parses large values once rather than after every 64 KiB chunk: when a string is on its way, it keeps reading until the bytes have arrived. A 32 MiB `SET` took about 2.6 s of server time when every chunk re-parsed the buffer, and about 0.2 s now.

## Pub/sub (`CommandHandler.MAX_SUBSCRIBER_BUFFER`)

A subscriber that stops reading may leave up to 32 MiB queued. Past that it is disconnected and unsubscribed, instead of the server holding every message for it.

## The client (`network.py`, `KESPDecoder`)

| Limit | Default |
|---|---|
| `MAX_REPLY_BYTES` buffered for one reply | 1 GiB (the connection is dropped past it) |
| numeric header lines | 32 bytes |
| status / error lines | 64 KiB |
| nesting depth | 32 |

While waiting for a large reply, `read_reply` does not decode again until the bytes the decoder asked for have arrived.

## Not covered yet

- **`MULTI` queues** are not capped: a client can queue commands without limit.
- **Output to ordinary clients** is held back one reply at a time (the server waits for the socket to drain), but there is no write deadline, so a client that never reads holds its connection and counts toward `MAX_CLIENTS`.
- **Replication:** the follower loops and the replica stream do not have these budgets.
- **Command cost:** `KEYS` on a huge store, or a very long list operation, blocks for as long as it takes. Nothing limits it.
