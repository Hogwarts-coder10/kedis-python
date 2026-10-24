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
| `WRITE_DEADLINE` + `WRITE_MIN_RATE` | 30 s + queued bytes / 256 KiB per s | A client, or a replication follower, that does not read what it is sent is dropped once a send has waited this long for the socket to drain. A client's slot is freed. |
| `YIELD_EVERY` | 256 | Commands run back to back before other clients get a turn. |
| `MAX_MULTI_COMMANDS` | 10,000 | Most commands one client's open `MULTI` block may queue. |
| `MAX_MULTI_BYTES` | 64 MiB | Most bytes of arguments (UTF-8) the block may hold, summed over its commands. |

Past either `MULTI` limit the queue is dropped and the block is poisoned, not ended: the command that crossed the limit, and every command after it, gets `-ERR MULTI queue limit reached; EXEC will abort the transaction` and is not queued, and `EXEC` replies `-EXECABORT Transaction discarded: the MULTI queue limit was exceeded` without running anything. `DISCARD`, or the `EXEC`, leaves the block and the next `MULTI` starts clean. Ending the block at the limit instead would run the rest of what the client meant to be atomic one command at a time. The limits are per connection.

Both timeouts send `EProtocol error: timed out waiting for the rest of a command` and close the connection. They only apply while a command is half-sent: a connection with nothing pending can idle for as long as it likes, so `SUBSCRIBE` and blocking reads are not affected.

The write deadline covers every reply a session sends to its own client (pub/sub pushes have their own cap, below, and the one-line `max number of clients` refusal is written before a session exists). When a reply does not fit in the socket buffers, the server waits for the client to read; if that wait passes `WRITE_DEADLINE` plus the time `WRITE_MIN_RATE` would need for the bytes still queued for that client, the socket is aborted (nothing more is sent, not even an error line), and the session ends. Commands the client had already pipelined behind that reply are not run. A client that reads promptly, or pauses for less than the deadline, is not affected, and a connection with nothing to send is never timed. Other clients keep being served while one is stuck.

The master's broadcast to followers uses the same deadline. A write command's payload is queued for every follower first, then all of them are waited for together, so a follower that stops reading costs the command that wrote to it one deadline (not one per follower), and then loses its place in the live stream. That connection is closed; a follower that comes back has to `SYNC` again. Until the deadline passes, the connection that ran the write cannot run its next command, and a stuck follower holds each concurrent write up in the same way.

A follower reads its leader's stream under the same rules (`LeaderStream` in `server.py`): the parser's size limits, `STALL_TIMEOUT`, and `FRAME_DEADLINE` + `FRAME_MIN_RATE` for a command that is only partly received. The leader also gets `FRAME_DEADLINE` to begin sending the baseline snapshot. A leader that is merely quiet between commands is left alone. A follower stops following, and closes the connection, when the leader:

- sends bytes that are not valid KESP (the stream cannot be resynchronised), or stalls or trickles a command past the limits above, or closes in the middle of one;
- sends a baseline that is not a single JSON object (the store is left as it was).

It stays a read-only replica when that happens: a follower never promotes itself because its leader misbehaved. A command that fails to apply is logged and skipped rather than retried, since retrying it could never succeed and everything behind it would wait.

The server also parses large values once rather than after every 64 KiB chunk: when a string is on its way, it keeps reading until the bytes have arrived. A 32 MiB `SET` took about 2.6 s of server time when every chunk re-parsed the buffer, and about 0.2 s now.

## Pub/sub (`CommandHandler.MAX_SUBSCRIBER_BUFFER`)

A subscriber that stops reading may leave up to 32 MiB queued. Past that it is disconnected and unsubscribed, instead of the server holding every message for it.

## The client (`network.py`, `KESPDecoder`)

| Limit | Default |
|---|---|
| `MAX_REPLY_BYTES` buffered for one reply | 1 GiB (the connection is dropped past it) |
| numeric header lines | 32 bytes |
| status / error lines | 64 KiB, counting the sigil but not the `\n` |
| nesting depth | 32 |
| values in one reply (`MAX_REPLY_ELEMENTS`) | 4,194,304, containers included |
| string and text-line bytes in one reply (`MAX_REPLY_PAYLOAD`) | 1 GiB, added up |

The last two are budgets for the whole reply, so many small values cannot add up to an unbounded amount of work or memory. An array that declares more elements than remain in the budget, or a string longer than the payload that remains, is refused on its header, before the bytes arrive. `KESPEncoder` applies the same two budgets, so a reply that would go over leaves the server as one `E` line instead.

While waiting for a large reply, `read_reply` does not decode again until the bytes the decoder asked for have arrived.

## Not covered yet

- **Command cost:** `KEYS` on a huge store, or a very long list operation, blocks for as long as it takes. Nothing limits it.
