# 🌐 Networking: The Surge Tank & Connection Routing

Kedis is built for speed. To eliminate the parsing overhead of standard web servers, it completely bypasses HTTP and REST. Instead, it operates a custom TCP socket architecture, providing connected clients with a persistent, low-latency slipstream directly into the engine's core.

## The `asyncio` Event Loop

The engine relies on a strict, single-threaded **non-blocking I/O** model powered by Python's `asyncio`. 

Instead of spawning a heavy, memory-intensive OS thread for every single client that connects, Kedis uses an event loop. The loop acts as a high-speed traffic controller: it accepts a network connection, reads whatever bytes are currently available on the socket, and instantly moves to the next client. This allows a single Python process to efficiently juggle thousands of concurrent connections without breaking a sweat.

## The Surge Tank: Handling TCP Fragmentation

Because TCP is a streaming protocol, it does not respect command boundaries. A massive payload (like a 10MB `SET` command) might arrive chopped up into three separate, fragmented packets. Assuming that a single `recv()` call equals one complete command is a fatal flaw that crashes naive databases.

**The Solution:** Kedis utilizes a **Surge Tank**.
1. Every connected client is assigned an isolated `bytearray` buffer (the Surge Tank).
2. As raw bytes arrive on the socket, they are blindly dumped into this tank.
3. The KESP Parser continuously inspects the tank. If a command is incomplete, the engine ignores it and yields control back to the event loop.
4. Once the parser detects a fully formed, valid KESP payload, it strictly slices those exact bytes out of the tank (`del tank[:consumed]`) and routes the command. 

This guarantees zero data loss, zero memory leaks, and perfect payload reconstruction regardless of network lag or packet size.

## Client Sessions & State Isolation

When a connection is established, the server spins up an `AsyncKedisSession`. This class tracks the specific state of that individual user, entirely isolated from the rest of the engine.

The session object holds:
* The client's unique IP and port.
* **Transaction State:** A boolean flag tracking if the client is currently inside a `MULTI` block, alongside their isolated `tx_queue` of deferred commands.
* **Optimistic Locks:** A `watched_keys` dictionary mapping specific keys to their expected version numbers to monitor for race conditions.

## Error Handling & Graceful Teardown

* **Malformed Packets:** If a client sends gibberish or invalid KESP syntax, the parser flushes the Surge Tank to prevent buffer poisoning and fires an `E` (Error) sigil back down the wire.
* **Disconnections:** If a client forcefully drops their connection (e.g., pulling the ethernet cable) while inside a transaction, the engine detects the closed socket. The session gracefully self-destructs, clearing all pending locks and queues, ensuring the global engine state remains pristine and unblocked.
