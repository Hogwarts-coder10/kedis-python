import asyncio
import json
import signal
import sys
import time

from rich.console import Console

from .commands import CommandHandler
from .parser import CommandParser, KESPEncoder
from .store import KedisStore
from .stream import StreamError
from .ui import UI

console = Console()

# ---------------------------------------------------------
# The Shared Global Database Core
# ---------------------------------------------------------

global_store = KedisStore()
global_handler = CommandHandler(global_store)

HOST = "127.0.0.1"
PORT = 6379

# Replication Telemetry
server_role = "master"
server_host = None
master_port = None
connected_replicas = []  # 📡 Holds the writer sockets for all active Followers


async def loop_latency_monitor(store):
    """
    Diagnostic sensor: Pulses every 100ms. If it takes longer to wake up,
    the event loop is being blocked by a synchronous operation.
    """

    store.current_lag_ms = 0.0

    while True:
        start_time = time.perf_counter()

        # We expect the loop to hand control back in exactly 100ms
        await asyncio.sleep(0.1)

        end_time = time.perf_counter()

        # Calculating actual sleep time
        actual_sleep_time = (end_time - start_time) * 1000
        lag = actual_sleep_time - 100.0

        # Floor to 0 (sometimes sleep wakes up a fraction early), round to 2 decimals
        store.current_lag_ms = max(0.0, round(lag, 2))


HOUSEKEEPING_INTERVAL = 30.0  # seconds between maintenance passes

# ----------------------------------------------------------------------
# BLOCKING READS (XREAD ... BLOCK)
# A blocked session parks on a future. Writers call notify_stream_waiters()
# after a write completes; every parked reader wakes, re-reads, and goes back
# to sleep if its streams still have nothing new. Futures belong to the running
# loop, so there is no module-level loop-bound object to get wrong.
# ----------------------------------------------------------------------
stream_waiters: set = set()
_BLOCK_POLL = 0.5  # how often a blocked session checks that its client is alive


def notify_stream_waiters():
    for fut in list(stream_waiters):
        if not fut.done():
            fut.set_result(None)
    stream_waiters.clear()


async def blocking_xread(session, tokens: list):
    """Runs XREAD; honours BLOCK. Returns the reply object (None = nil)."""
    try:
        count, block_ms, streams = global_handler.parse_xread(tokens)
    except StreamError:
        block_ms = None
    if block_ms is None:  # no BLOCK (or a syntax error the handler will report)
        return await asyncio.to_thread(global_handler.execute, tokens, session.writer)

    try:
        # '$' must mean "last ID right now", fixed once, before we wait.
        resolved = global_handler.resolve_stream_ids(streams)
    except StreamError as e:
        return f"-{e}"
    except TypeError as e:
        return f"-ERR {e}"

    loop = asyncio.get_running_loop()
    deadline = None if block_ms == 0 else loop.time() + block_ms / 1000

    while True:
        # Register BEFORE reading: a write landing between the read and the
        # wait would otherwise be missed until the next unrelated write.
        wake = loop.create_future()
        stream_waiters.add(wake)
        try:
            try:
                result = await asyncio.to_thread(
                    global_handler.xread_nonblocking, resolved, count
                )
            except TypeError as e:
                return f"-ERR {e}"
            if result is not None:
                return result
            while True:
                if session.reader.at_eof() or session.writer.is_closing():
                    raise ConnectionResetError("client left while blocked")
                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    return None  # timed out: nil, like Redis
                step = _BLOCK_POLL if remaining is None else min(_BLOCK_POLL, remaining)
                try:
                    await asyncio.wait_for(asyncio.shield(wake), step)
                    break  # woken by a writer: read again
                except asyncio.TimeoutError:
                    continue
        finally:
            stream_waiters.discard(wake)
            if not wake.done():
                wake.cancel()


async def housekeeping_loop(handler, interval: float = HOUSEKEEPING_INTERVAL):
    """Periodic store maintenance: expiry sweep, LRU/expiry reconciliation and
    container compaction. Runs in a worker thread, under the engine lock, like
    every other command, so it never overlaps a client write."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(handler.run_housekeeping)
        except Exception as e:  # maintenance must never take the server down
            console.print(f"[bold red]❌ Housekeeping pass failed: {repr(e)}[/bold red]")


async def init_replication_stream(host: str, port: int):
    """
    Opens a permanent TCP socket to the Leader engine,
    downloads the baseline,
    and processes live replication streams.
    """
    global server_role, server_host, master_port

    try:
        console.print(
            f"[cyan]🔗 [REPLICATION] Initiating handshake with Leader at {host}:{port}...[/cyan]"
        )

        # 1. Open the direct TCP line to the Leader
        reader, writer = await asyncio.open_connection(host, port)

        # Lock in the telemetry state
        server_role = "replica"
        server_host = host
        master_port = port

        console.print(
            f"[bold green]✅ [REPLICATION] Slipstream locked! Successfully connected to {host}:{port}[/bold green]"
        )

        # 2. Demand the baseline state from the Leader
        console.print(
            "[cyan]📥 [REPLICATION] Requesting baseline snapshot via SYNC...[/cyan]"
        )
        writer.write(b"SYNC\n")
        await writer.drain()

        # 3. Read the incoming KESP payload containing the JSON data
        # Using a dense surge buffer to read the KESP array frame safely
        intake_buffer = bytearray()
        snapshot_loaded = False

        while not snapshot_loaded:
            chunk = await reader.read(65536)
            if not chunk:
                raise ConnectionError(
                    "Leader severed connection before snapshot arrived."
                )

            intake_buffer.extend(chunk)

            try:
                # 🚀 FIX: Unpack the tuple properly
                tokens, consumed = CommandParser.parse(bytes(intake_buffer))
                if tokens:
                    # The first token parsed will be the raw JSON snapshot string
                    raw_json = tokens[0]

                    # 🚀 FIX: Restore the data using your custom loader to rebuild SkipLists
                    parsed_data = json.loads(raw_json)
                    global_store.restore_snapshot_state(parsed_data)

                    console.print(
                        f"[bold green]💾 [REPLICATION] Cold Boot Successful! Restored {len(global_store._data)} keys from Leader.[/bold green]"
                    )
                    snapshot_loaded = True

                    # 🚀 FIX: Slice the buffer
                    del intake_buffer[:consumed]
            except Exception as e:
                # Payload is still fragmented across packets, loop back to read more
                console.print(
                    f"[bold yellow]⚠️ [REPLICATION] Parser skipping chunk: {e}[/bold yellow]"
                )
                continue

        # 4. 🚀 PHASE 3 LIVE STREAM: Stay locked in the slipstream forever catching live writes
        console.print(
            "[bold blue]⚡ [REPLICATION] Entering Live Stream Mode. Awaiting commands...[/bold blue]"
        )

        while True:
            chunk = await reader.read(65536)
            if not chunk:
                console.print(
                    "[bold red]⚠️ [REPLICATION] Leader connection lost![/bold red]"
                )
                break

            intake_buffer.extend(chunk)

            while True:
                try:
                    tokens, consumed = CommandParser.parse(bytes(intake_buffer))
                    if not tokens:
                        break

                    # Execute the mirrored write locally using your handler
                    await asyncio.to_thread(global_handler.execute, tokens, None)
                    notify_stream_waiters()
                    console.print(
                        f"[magenta]🔄 [REPLICATION Live] Executed: {' '.join(tokens)}[/magenta]"
                    )

                    del intake_buffer[:consumed]
                except Exception:
                    # Partial packet handling
                    break

    except Exception as e:
        console.print(f"[bold red]❌ [REPLICATION] Sync Engine crashed: {e}[/bold red]")
        server_role = "master"  # Fall back to master role if cluster drivetrain breaks


class AsyncKedisSession:
    """
    Manages the state and routing for a single async client connection.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.addr = writer.get_extra_info("peername")
        self.in_transaction = False
        self.tx_queue = []

        # Connection-specific watch state {key: expected_version}
        self.watched_keys = {}

        # Network Dispatch Table
        self.tx_router = {
            "MULTI": self.handle_multi,
            "EXEC": self.handle_exec,
            "DISCARD": self.handle_discard,
            "WATCH": self.handle_watch,
            "UNWATCH": self.handle_unwatch,
        }

    async def send(self, data: bytes):
        """
        Asynchronously flushes the bytes to the network sockets
        """
        self.writer.write(data)
        await self.writer.drain()

    # -------------------------------------
    # ASYNC TRANSACTION ROUTUING (Which will be used in dispatch table)
    # -------------------------------------

    async def handle_watch(self, tokens: list):
        if self.in_transaction:
            await self.send(b"-ERR WATCH inside MULTI is not allowed\r\n")
            return
        if len(tokens) < 2:
            await self.send(b"-ERR wrong number of arguments for 'watch'\r\n")
            return

        # Lock in the current version of the requested keys
        for key in tokens[1:]:
            current_ver = getattr(global_store, "_versions", {}).get(key, 0)
            self.watched_keys[key] = current_ver
        await self.send(b"+OK\r\n")

    async def handle_unwatch(self, tokens: list):
        self.watched_keys.clear()
        await self.send(b"+OK\r\n")

    async def handle_multi(self, tokens: list):
        if self.in_transaction:
            await self.send(b"-ERR MULTI calls are not nested\r\n")
        else:
            self.in_transaction = True
            self.tx_queue = []
            await self.send(b"+OK\r\n")

    async def handle_discard(self, tokens: list):
        if not getattr(self, "in_transaction", False):
            await self.send(b"-ERR DISCARD without MULTI\r\n")
            return

        self.in_transaction = False
        self.tx_queue = []
        self.watched_keys.clear()  # Discarding also clears watched keys
        await self.send(b"+OK\r\n")

    async def handle_exec(self, tokens: list):
        if not getattr(self, "in_transaction", False):
            await self.send(b"-ERR EXEC without MULTI\r\n")
            return

        #  OPTIMISTIC LOCK CHECK
        # Verify no watched keys have been modified by another client
        transaction_aborted = False
        for key, expected_version in self.watched_keys.items():
            current_version = getattr(global_store, "_versions", {}).get(key, 0)
            if current_version != expected_version:
                transaction_aborted = True
                break

        # Drop out of transaction mode and clear locks
        self.in_transaction = False
        self.watched_keys.clear()

        # If race condition detected, abort safely
        if transaction_aborted:
            self.tx_queue.clear()
            # kedis protocol returns a Null Array for aborted transactions
            await self.send(b"N\n")
            return

        # Handle empty queues
        if not self.tx_queue:
            await self.send(b"A0\n")
            return

        # Execute the payload
        results = []
        for cmd_args in self.tx_queue:
            result = await asyncio.to_thread(
                global_handler.execute, cmd_args, self.writer
            )
            results.append(result)

        self.tx_queue.clear()
        notify_stream_waiters()

        # Format and send the array of results back to the client
        response = f"A{len(results)}\n".encode("utf-8")
        for res in results:
            response += KESPEncoder.encode(res)

        await self.send(response)

    # -------------------------------------
    # ASYNC EVENT LOOP (MAIN)
    # -------------------------------------

    async def run(self):
        """
        The main non-blocking event loop with a dynamic I/O Surge Tank.
        """
        client_id = f"{self.addr[0]} : {self.addr[1]}"
        console.print(f"[green] 🔌 Client Connected:[/green] {client_id}")

        # The Surge Tank : Buffers the fragmmented TCP packets
        intake_buffer = bytearray()

        while True:
            try:
                # Widenning the intake pipe to 64KB per read
                chunk = await self.reader.read(65536)
                if not chunk:
                    break

                # pool the new bytes into the intake buffer
                intake_buffer.extend(chunk)

                # Inner loop to process pipelined commands within the buffer
                while True:
                    if not intake_buffer:
                        break

                    try:
                        # 🚀 FIX: Unpack the tuple
                        tokens, consumed = CommandParser.parse(bytes(intake_buffer))
                    except Exception:
                        break  # Wait for next TCP packet

                    if tokens and tokens[0] == "ERROR":
                        clean_err = tokens[1].replace("-ERR", " ")
                        await self.send(f"E{clean_err}\n".encode("utf-8"))
                        # 🚀 FIX: Slicing
                        del intake_buffer[:consumed]
                        continue

                    if not tokens:
                        break  # Parser returned nothing, wait for more data

                    # Execute the fully assembled command
                    cmd = tokens[0].upper()

                    # 🛡️ REPLICATION INTERCEPTOR
                    if cmd == "REPLICAOF" and len(tokens) >= 3:
                        r_host = tokens[1]
                        r_port = tokens[2]

                        if r_host.upper() == "NO" and r_port.upper() == "ONE":
                            global server_role
                            server_role = "master"
                            await self.send(b"+OK Engine promoted to Leader\r\n")
                        else:
                            asyncio.create_task(
                                init_replication_stream(r_host, int(r_port))
                            )
                            await self.send(b"+OK Replica handshake initiated\r\n")

                        # 🚀 FIX: Slicing
                        del intake_buffer[:consumed]
                        continue

                    if cmd == "SYNC":
                        if server_role == "master":
                            console.print(
                                "[cyan]📦 [REPLICATION] Follower requested baseline. Dumping RAM...[/cyan]"
                            )

                            # 🚀 FIX: Use the envelope serialization
                            safe_state = global_store.get_snapshot_state()
                            snapshot_json = json.dumps(safe_state)

                            json_bytes = snapshot_json.encode("utf-8")

                            header = b"A1\n"
                            body = (
                                f"S{len(json_bytes)}\n".encode("utf-8")
                                + json_bytes
                                + b"\n"
                            )
                            kesp_payload = header + body

                            await self.send(kesp_payload)
                            console.print(
                                "[bold green]✅ [REPLICATION] Baseline snapshot transmitted![/bold green]"
                            )

                            connected_replicas.append(self.writer)
                            console.print(
                                f"[bold magenta]📡 [REPLICATION] Follower locked into Live Stream. Total replicas: {len(connected_replicas)}[/bold magenta]"
                            )
                        else:
                            await self.send(
                                b"-ERR I'm a follower, I cannot sync you!!\n"
                            )

                        # 🚀 FIX: Slicing
                        del intake_buffer[:consumed]
                        continue

                    if cmd in self.tx_router:
                        await self.tx_router[cmd](tokens)

                    elif self.in_transaction:
                        self.tx_queue.append(tokens)
                        await self.send(b"+OK\n")

                    else:
                        # 🛡️ PHASE 4: THE READ-ONLY FIREWALL
                        # 🚀 FIX: Dynamic commands
                        write_commands = global_handler.WRITE_COMMANDS

                        if server_role == "replica" and cmd in write_commands:
                            console.print(
                                f"[bold yellow]⚠️ [SECURITY] Blocked client attempt to run {cmd} on Follower.[/bold yellow]"
                            )
                            await self.send(
                                b"-EREADONLY You can't write against a read-only replica.\n"
                            )
                            del intake_buffer[:consumed]
                            continue

                        if cmd == "XREAD":
                            response = await blocking_xread(self, tokens)
                        else:
                            response = await asyncio.to_thread(
                                global_handler.execute, tokens, self.writer
                            )
                        kesp_bytes = KESPEncoder.encode(response)
                        await self.send(kesp_bytes)

                        if cmd in write_commands:
                            notify_stream_waiters()

                        # 📡 PHASE 3: LIVE COMMAND FORWARDING
                        if server_role == "master" and cmd in write_commands:
                            console.print(
                                f"[cyan]📡 [BROADCAST] Firing {cmd} down the slipstream to {len(connected_replicas)} followers...[/cyan]"
                            )

                            # XADD is forwarded with the ID this master assigned
                            forwarded = global_handler.replication_tokens(
                                tokens, response
                            )
                            header = f"A{len(forwarded)}\n".encode("utf-8")
                            body = b"".join(
                                f"S{len(t.encode('utf-8'))}\n{t}\n".encode("utf-8")
                                for t in forwarded
                            )
                            broadcast_payload = header + body

                            dead_replicas = []
                            for rep_writer in connected_replicas:
                                try:
                                    rep_writer.write(broadcast_payload)
                                    await rep_writer.drain()
                                except Exception:
                                    dead_replicas.append(rep_writer)

                            for dead in dead_replicas:
                                connected_replicas.remove(dead)
                                console.print(
                                    "[yellow]⚠️ [REPLICATION] Follower disconnected. Removed from Live Stream.[/yellow]"
                                )

                    # 🚀 FIX: Slicing for standard commands
                    del intake_buffer[:consumed]

            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                break  # client vanished; fall through to cleanup
            except Exception as e:
                console.print(
                    f"[bold red]❌ [REPLICATION Live] Stream Crash: {repr(e)}[/bold red]"
                )
                intake_buffer.clear()
                break

        console.print(f"[yellow]⚠️ Client Disconnected:[/yellow] {client_id}")
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            # The peer reset the connection (e.g. killed client, closed with
            # unread data). wait_closed() re-raises that stored error, but
            # there is nothing left to clean up, so it is not an error here.
            pass


async def handle_connection(reader, writer):
    """
    Spawns a new isolated session object for every incoming TCP connection.
    """
    session = AsyncKedisSession(reader, writer)
    await session.run()


async def main():
    UI.print_banner(subtitle="DATABASE SERVER")
    UI.print_server_ready(HOST, PORT, server_role)

    server = await asyncio.start_server(handle_connection, HOST, PORT)
    asyncio.create_task(loop_latency_monitor(global_store))
    housekeeping_task = asyncio.create_task(housekeeping_loop(global_handler))  # noqa: F841

    # --- THE OS SIGNAL TRAP ---
    def shutdown_sequence(sig_name):
        console.print(
            f"\n[bold red]🛑 {sig_name} intercepted. Initiating Clean Engine Shutdown...[/bold red]"
        )
        global_store.shutdown()
        server.close()
        console.print(
            "[bold green]✅ Engine powered down safely. No data lost.[/bold green]"
        )
        sys.exit(0)

    loop = asyncio.get_event_loop()
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT, lambda: shutdown_sequence("SIGINT"))
        loop.add_signal_handler(signal.SIGTERM, lambda: shutdown_sequence("SIGTERM"))

    async with server:
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            pass
        except KeyboardInterrupt:
            shutdown_sequence("SIGINT")


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
