# 🏛️ Kedis Engine Architecture

Kedis is designed as a high-throughput, non-blocking, in-memory data store. To achieve microsecond latency in Python, the architecture strictly separates network I/O from storage execution, utilizing a custom binary protocol to eliminate parsing overhead.

## The Data Pipeline

The core request lifecycle follows a strict, one-way pipeline from the network socket down to the physical disk:

```text
       Client
         │ (TCP Socket)
         ▼
     TCP Server
         │ (Raw Bytes via Surge Tank)
         ▼
   Command Parser
         │ (Python Tokens / KESP Decoder)
         ▼
 Command Dispatcher
         │ (Execution Router / Locks)
         ▼
   Storage Engine
         │ (In-Memory Dictionary)
         ▼
     Persistence
       (AOF Log via Background Thread)
```

## Component Breakdown

### 1. Client

The entry point. Clients connect to Kedis over raw TCP sockets. Because the engine doesn't rely on HTTP, the connection remains persistently open, allowing clients to stream thousands of commands per second without the overhead of re-establishing handshakes.

### 2. TCP Server (`server.py`)

Built on Python's `asyncio.start_server`. This is the traffic controller of the engine. It runs on a single-threaded event loop, adhering to a strict non-blocking philosophy. It accepts connections, reads available bytes, and immediately yields control back to the loop to handle the next client.

### 3. Command Parser (`parser.py`)

Because TCP is a streaming protocol, payloads are often fragmented across multiple packets.

-   **The Surge Tank:** Raw bytes are dumped into a connection-specific `bytearray` buffer.
    
-   **KESP Decoding:** The parser inspects this tank, waiting until a complete Kedis Engine Serialization Protocol (KESP) payload is detected. Once complete, it safely slices the command out of the buffer and translates it into executable Python tokens.
    

### 4. Command Dispatcher (`main.py` / `server.py`)

The routing manifold. The dispatcher takes the parsed tokens and maps them to the correct internal functions.

-   **Concurrency Control:** Before executing any write, the dispatcher checks the session's state for Optimistic Locks (`WATCH` and `UNWATCH`), safely aborting the transaction if another client has mutated the requested data.
    

### 5. Storage Engine (`store.py`)

The core memory map. This is where the actual data structures (Strings, Lists, Hashes) live. It is built on highly optimized Python dictionaries and handles:

-   Immediate data retrieval and mutation.
    
-   Background TTL (Time-To-Live) expirations.
    
-   Memory limits and LRU (Least Recently Used) cache eviction.
    

### 6. Persistence

The I/O Drivetrain. To prevent disk latency from stalling the `asyncio` event loop, Kedis offloads physical OS file writes to background worker threads. Using an Append-Only File (AOF) architecture, every mutating command is logged to disk sequentially, allowing the engine to rebuild the exact memory state upon the next boot.

## Example: The Anatomy of a `SET` Command

When a client sends `SET driver Verstappen`:

1.  **TCP Server:** Receives the raw byte string `*3\r\n$3\r\nSET\r\n$6\r\ndriver\r\n$10\r\nVerstappen\r\n` (KESP format) into the Surge Tank.
    
2.  **Command Parser:** Reads the tank, recognizes a complete 3-token array, and parses it into `["SET", "driver", "Verstappen"]`.
    
3.  **Command Dispatcher:** Routes the tokens to the `set()` method.
    
4.  **Storage Engine:** Updates the internal dictionary: `self._data["driver"] = "Verstappen"` and bumps the mutation version for any watching clients.
    
5.  **Persistence:** The `_log_operation` method pushes the raw command into a background thread queue, which appends it to the `.aof` file on the SSD.
    
6.  **TCP Server:** Encodes a `+OK\r\n` KESP response and streams it back up the socket to the client.

