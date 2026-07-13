import asyncio
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
PORT = 6381

# Replication Telemetry
server_role = "master"
server_host = None
master_port = None


async def establish_replica_slipstream(host: str, port: int):
    """Opens a permanent TCP socket to the Leader engine."""
    global server_role, server_host, master_port

    try:
        console.print(
            f"[cyan]🔗 [REPLICATION] Initiating handshake with Leader at {host}:{port}...[/cyan]"
        )

        # Open the direct TCP line to the Leader
        reader, writer = await asyncio.open_connection(host, port)

        # Lock in the new state
        server_role = "replica"
        server_host = host
        master_port = port

        console.print(
            f"[bold green]✅ [REPLICATION] Slipstream locked! Successfully connected to {host}:{port}[/bold green]"
        )

        # ⚠️ TOMORROW: This is where we will write the loop that
        # waits for the snapshot and live commands from the Leader.

    except Exception as e:
        console.print(
            f"[bold red]❌ [REPLICATION] Handshake failed. Engine could not reach {host}:{port} - {e}[/bold red]"
        )
        server_role = "master"  # Fall back to master if the connection fails


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

    async def handle_exec(self):
        if not self.in_transaction:
            await self.send(b"EEXEC without MULTI\n")
        else:
            self.in_transaction = True
            self.tx_queue = []
            await self.send(b"+OK")

    async def handle_discard(self):
        if not self.in_transaction:
            await self.send(b"EDISCARD without MULTI\n")
        else:
            self.in_transaction = True
            self.tx_queue = []
            await self.send(b"+OK")

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

                # attempt to parse the buffer

                try:
                    tokens = CommandParser.parse(bytes(intake_buffer))
                except Exception:
                    # If the parser crashes, the KESP payload is likely sliced in half.
                    # We simply yield control and wait for the next TCP packet to arrive.
                    continue

                if tokens and tokens[0] == "ERROR":
                    clean_err = tokens[1].replace("-ERR", " ")
                    await self.send(f"E{clean_err}\n".encode("utf-8"))
                    intake_buffer.clear()
                    continue

                if not tokens:
                    # Parser has returned nothing; still waiting for a complete KESP frame
                    continue

                # Exceute the fully assembled command
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
                        # Ignite the handshake sequence in the background without blocking the loop
                        asyncio.create_task(
                            establish_replica_slipstream(r_host, int(r_port))
                        )
                        await self.send(b"+OK Replica handshake initiated\r\n")

                    intake_buffer.clear()
                    continue  # Skip sending this command to the store

                if cmd in self.tx_router:
                    await self.tx_router[cmd]()

                elif self.in_transaction:
                    self.tx_queue.append(tokens)
                    await self.send(b"+OK")

                else:
                    response = global_handler.execute(tokens, self.writer)
                    kesp_bytes = KESPEncoder.encode(response)
                    await self.send(kesp_bytes)

                # Flush the intake buffer after a sucessful ignition
                intake_buffer.clear()

            except ConnectionResetError:
                break
            except Exception as e:
                await self.send(f"Einternal server error: {str(e)}\n".encode("utf-8"))
                intake_buffer.clear()

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

    # Booting the async server socket
    server = await asyncio.start_server(handle_connection, HOST, PORT)

    # --- THE OS SIGNAL TRAP (Issue #12 Fix - Async Compatible) ---
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

    # Wiring up the cross-platform signal handlers
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
            # Fallback for Windows if signal handlers fail
            shutdown_sequence("SIGINT")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
