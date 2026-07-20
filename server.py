import asyncio
import json
import signal
import sys

from rich.console import Console
from rich.panel import Panel

from commands import CommandHandler
from parser import CommandParser, KESPEncoder
from store import KedisStore

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

        # Network Dispatch Table
        self.tx_router = {
            "MULTI": self.handle_multi,
            "EXEC": self.handle_exec,
            "DISCARD": self.handle_discard,
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

    async def handle_multi(self):
        if self.in_transaction:
            await self.send(b"EMULTI calls are not nested\n")
        else:
            self.in_transaction = True
            self.tx_queue = []
            await self.send(b"+OK")

    async def handle_discard(self):
        """
        Aborts the transaction, clears the queue, and resets the flag.
        """
        if not getattr(self, "in_transaction", False):
            await self.send(b"-ERR DISCARD without MULTI\r\n")
            return

        self.in_transaction = False
        self.tx_queue = []
        await self.send(b"+OK\r\n")

    async def handle_exec(self):
        """
        Executes all queued commands sequentially,
        and returns an array of results.
        """
        if not getattr(self, "in_transaction", False):
            await self.send(b"-ERR EXEC without MULTI\r\n")
            return

        # drop out of transaction mode
        self.in_transaction = False

        # Handle empty queues
        if not self.tx_queue:
            await self.send(b"*0\r\n")
            return

        # execute the payload
        results = []
        for cmd_args in self.tx_queue:
            result = await asyncio.to_thread(
                global_handler.execute, cmd_args, self.writer
            )
            results.append(result)

        self.tx_queue.clear()

        # Format and send the array of results back to the client
        response = f"*{len(results)}\r\n".encode("utf-8")
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
                        await self.tx_router[cmd]()

                    elif self.in_transaction:
                        self.tx_queue.append(tokens)
                        await self.send(b"+OK")

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

                        response = await asyncio.to_thread(
                            global_handler.execute, tokens, self.writer
                        )
                        kesp_bytes = KESPEncoder.encode(response)
                        await self.send(kesp_bytes)

                        # 📡 PHASE 3: LIVE COMMAND FORWARDING
                        if server_role == "master" and cmd in write_commands:
                            console.print(
                                f"[cyan]📡 [BROADCAST] Firing {cmd} down the slipstream to {len(connected_replicas)} followers...[/cyan]"
                            )

                            header = f"A{len(tokens)}\n".encode("utf-8")
                            body = b"".join(
                                f"S{len(t.encode('utf-8'))}\n{t}\n".encode("utf-8")
                                for t in tokens
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

            except ConnectionResetError:
                break
            except Exception as e:
                console.print(
                    f"[bold red]❌ [REPLICATION Live] Stream Crash: {repr(e)}[/bold red]"
                )
                intake_buffer.clear()
                break

        console.print(f"[yellow]⚠️ Client Disconnected:[/yellow] {client_id}")
        self.writer.close()
        await self.writer.wait_closed()


async def handle_connection(reader, writer):
    """
    Spawns a new isolated session object for every incoming TCP connection.
    """
    session = AsyncKedisSession(reader, writer)
    await session.run()


async def main():
    console.print(
        Panel(
            f"[bold blue]Kedis Engine Core Online[/bold blue]\n"
            f"Listening on TCP {HOST}:{PORT}\n\n"
            f"Network Architecture: [green]asyncio Event Loop[/green]\n"
            f"Concurrency: [green]Non-blocking I/O[/green]",
            title="🚀 ASYNC IGNITION",
            border_style="blue",
            expand=False,
        )
    )

    server = await asyncio.start_server(handle_connection, HOST, PORT)

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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
