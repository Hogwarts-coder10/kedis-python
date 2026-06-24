import socketserver
import threading

from rich.console import Console
from rich.panel import Panel

from commands import CommandHandler
from parser import CommandParser
from store import KedisStore

console = Console()

# ---------------------------------------------------------
# The Shared Global Database Core
# ---------------------------------------------------------
global_store = KedisStore()
global_handler = CommandHandler(global_store)

HOST = "127.0.0.1"
PORT = 6379


class KedisTCPHandler(socketserver.BaseRequestHandler):
    def setup(self):
        """
        Runs automatically once per client connection before handle() starts.
        Initializes isolated session memory and the O(1) Network Dispatch Table.
        """
        self.in_transaction = False
        self.tx_queue = []

        # --- THE NETWORK STATE DISPATCH TABLE ---
        self.tx_router = {
            "MULTI": self.handle_multi,
            "EXEC": self.handle_exec,
            "DISCARD": self.handle_discard,
        }

    # ---------------------------------------------------------
    # NETWORK STATE ROUTING OPERATIONS (The Register Buffer)
    # ---------------------------------------------------------
    def handle_multi(self):
        if self.in_transaction:
            self.request.sendall(b"-ERR MULTI calls can not be nested")
        else:
            self.in_transaction = True
            self.tx_queue = []
            self.request.sendall(b"+OK")

    def handle_discard(self):
        if not self.in_transaction:
            self.request.sendall(b"-ERR DISCARD without MULTI")
        else:
            self.in_transaction = False
            self.tx_queue = []  # Wipe the whiteboard buffer clear
            self.request.sendall(b"+OK")

    def handle_exec(self):
        if not self.in_transaction:
            self.request.sendall(b"-ERR EXEC without MULTI")
        else:
            if not self.tx_queue:
                self.request.sendall(b"*(0)")  # Empty array format
            else:
                # Dispatch the entire queued sequence sequentially
                responses = []
                for queued_tokens in self.tx_queue:
                    # Pass the physical socket (self.request) into the engine here also
                    res = global_handler.execute(queued_tokens, self.request)
                    responses.append(res)

                # Join individual results into a single multi-line response payload
                combined_response = "\n".join(responses)
                self.request.sendall(combined_response.encode("utf-8"))

            # Reset pipeline metrics post-ignition
            self.in_transaction = False
            self.tx_queue = []

    # ---------------------------------------------------------
    # MAIN NETWORK THREAD ENGINE LOOP
    # ---------------------------------------------------------
    def handle(self):
        client_id = f"{self.client_address[0]}:{self.client_address[1]}"
        console.print(
            f"[green]🔌 Client Connected:[/green] {client_id} (Active Threads: {threading.active_count() - 1})"
        )

        while True:
            try:
                data = self.request.recv(1024)
                if not data:
                    break

                raw_input = data.decode("utf-8").strip()
                tokens = CommandParser.parse(raw_input)

                if tokens and tokens[0] == "ERROR":
                    self.request.sendall(tokens[1].encode("utf-8"))
                    continue
                if not tokens:
                    self.request.sendall(b"")
                    continue

                cmd = tokens[0].upper()

                # 1. INTERCEPT PROTOCOL CONTROL STRATEGIES (O(1) Route check)
                if cmd in self.tx_router:
                    self.tx_router[cmd]()
                    continue

                # 2. EVALUATE TRANSIT ROUTING PATHS
                if self.in_transaction:
                    # Append commands directly into the isolated session buffer
                    self.tx_queue.append(tokens)
                    self.request.sendall(b"+QUEUED")
                else:
                    # Forward immediately to the storage engine dispatch table
                    # Pass the physical socket (self.request) into the engine
                    response = global_handler.execute(tokens, self.request)
                    self.request.sendall(response.encode("utf-8"))

            except ConnectionResetError:
                break
            except Exception as e:
                error_msg = f"-ERR internal server error: {str(e)}"
                self.request.sendall(error_msg.encode("utf-8"))

        console.print(f"[yellow]⚠️ Client Disconnected:[/yellow] {client_id}")


class ThreadedKedisServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_server():
    console.print(
        Panel(
            f"[bold blue]Kedis Engine Core Online[/bold blue]\n"
            f"Listening on TCP {HOST}:{PORT}\n\n"
            f"Network Routing: [green]O(1) Dispatch Table[/green]\n"
            f"Transaction Buffer: [green]Isolated Session Sandbox[/green]",
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
