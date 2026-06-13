import socketserver
import threading

from rich.console import Console
from rich.panel import Panel

from commands import CommandHandler
from parser import CommandParser
from store import KedisStore

console = Console()

# ---------------------------------------------------------
# The Global Engine Block
# ---------------------------------------------------------
# We create ONE instance of the storage engine.
# Every connected client will share this exact same dictionary in memory.

global_store = KedisStore()
global_handler = CommandHandler(global_store)

HOST = "127.0.0.1"
PORT = 6379


class KedisTCPHandler(socketserver.BaseRequestHandler):
    """
    This is the Pit Crew.
    A new instance of this class is spawned in a separate thread
    for EVERY client that connects.
    """

    def handle(self):
        client_id = f"{self.client_address[0]}:{self.client_address[1]}"
        console.print(
            f"[green]🔌 Client Connected:[/green] {client_id} (Active Threads: {threading.active_count() - 1})"
        )

        while True:
            try:
                # 1. Wait for the client to send a command
                data = self.request.recv(1024)
                if not data:
                    break  # Client disconnected gracefully

                raw_input = data.decode("utf-8").strip()

                # 2. Parse the command
                tokens = CommandParser.parse(raw_input)

                # 3. Handle parser errors
                if tokens and tokens[0] == "ERROR":
                    self.request.sendall(tokens[1].encode("utf-8"))
                    continue

                if not tokens:
                    self.request.sendall(b"")
                    continue

                # 4. Route Server-Side Commands
                cmd = tokens[0]
                if cmd == "DEBUG":
                    global_store.debug_mode = not getattr(
                        global_store, "debug_mode", False
                    )
                    self.request.sendall(b"+OK")
                    continue

                if cmd == "STATS":
                    response = global_handler.execute(["STATS"])
                    self.request.sendall(response.encode("utf-8"))
                    continue

                # 5. Execute against the Global Engine!
                response = global_handler.execute(tokens)

                # 6. Send the result back to the client
                self.request.sendall(response.encode("utf-8"))

            except ConnectionResetError:
                break  # Client force-quit or crashed
            except Exception as e:
                error_msg = f"-ERR internal server error: {str(e)}"
                self.request.sendall(error_msg.encode("utf-8"))

        console.print(f"[yellow]⚠️ Client Disconnected:[/yellow] {client_id}")


class ThreadedKedisServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """
    This MixIn is the magic! It tells the server to spawn a new Thread
    every time a client connects, instead of blocking the main loop.
    """

    daemon_threads = (
        True  # Allows the server to shut down even if clients are connected
    )
    allow_reuse_address = True


def start_server():
    console.print(
        Panel(
            f"[bold blue]Kedis Multi-Threaded Engine Online[/bold blue]\n"
            f"Listening on TCP {HOST}:{PORT}\n\n"
            f"Persistence: [yellow]Local AOF[/yellow]\n"
            f"Initial Keys: [cyan]{len(global_store._data)}[/cyan]",
            title="🚀 SERVER IGNITION",
            border_style="blue",
            expand=False,
        )
    )

    with ThreadedKedisServer((HOST, PORT), KedisTCPHandler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            console.print("\n[bold red]Shutting down Kedis Server...[/bold red]")
            server.shutdown()


if __name__ == "__main__":
    start_server()
