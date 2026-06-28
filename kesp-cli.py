import shlex
import socket

import pyfiglet
from rich.console import Console
from rich.panel import Panel
from rich.theme import Theme

# ---------------------------------------------------------
# CLI Aesthetic & Branding
# ---------------------------------------------------------
custom_theme = Theme(
    {
        "info": "dim cyan",
        "danger": "bold red",
        "success": "bold green",
        "prompt": "bold cyan",
        "string": "green",
        "integer": "yellow",
    }
)

console = Console(theme=custom_theme)


class KESPClient:
    def __init__(self, host="127.0.0.1", port=6379):
        self.host = host
        self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def connect(self):
        try:
            self.socket.connect((self.host, self.port))
            console.print(
                f"[success]🔌 Connected to Engine [KESP Protocol] at {self.host}:{self.port}[/success]\n"
            )
            return True
        except ConnectionRefusedError:
            console.print(
                "[danger]❌ Could not connect. Is the Kedis server running?[/danger]"
            )
            return False

    def encode_command(self, user_input: str) -> bytes:
        """Translates human typing into a strict KESP Array of Strings."""
        try:
            tokens = shlex.split(user_input)
        except ValueError:
            return b""  # Catches missing closing quotes

        if not tokens:
            return b""

        header = f"A{len(tokens)}\n".encode("utf-8")
        body = b"".join(
            f"S{len(t.encode('utf-8'))}\n{t}\n".encode("utf-8") for t in tokens
        )

        return header + body

    def decode_response(self, raw_bytes: bytes) -> str:
        """Translates KESP server bytes into Rich-formatted console markup."""
        if not raw_bytes:
            return "[info](connection closed)[/info]"

        try:
            text = raw_bytes.decode("utf-8")
            sigil = text[0]

            if sigil == "+":
                return f"[success]{text[1:].strip()}[/success]"
            elif sigil == "E":
                return f"[danger](error) {text[1:].strip()}[/danger]"
            elif sigil == "I":
                return f"[integer](integer) {text[1:].strip()}[/integer]"
            elif sigil == "N":
                return "[info](nil)[/info]"
            elif sigil == "S":
                parts = text.split("\n", 1)
                if len(parts) > 1:
                    data = parts[1].rsplit("\n", 1)[0]
                    return f'[string]"{data}"[/string]'
            elif sigil == "A":
                return f"[info][KESP Array][/info]\n{text.strip()}"

            return text.strip()

        except Exception as e:
            return f"[danger](decoder error) {e}[/danger]"

    def repl(self):
        """The main Read-Eval-Print Loop."""
        # Print the glorious ASCII banner
        ascii_banner = pyfiglet.figlet_format("KEDIS", font="slant")
        panel = Panel(
            f"[bold cyan]{ascii_banner}[/bold cyan][dim]Kedis Serialization Protocol (KESP) Client[/dim]",
            border_style="cyan",
            expand=False,
        )
        console.print(panel)

        if not self.connect():
            return

        while True:
            try:
                # Using rich.console.input for the custom prompt color
                cmd = console.input("[prompt]kedis>[/prompt] ")
                if cmd.lower() in ["exit", "quit"]:
                    break
                if not cmd.strip():
                    continue

                kesp_payload = self.encode_command(cmd)
                if not kesp_payload:
                    console.print(
                        "[danger](error) Invalid syntax. Check your quotes.[/danger]"
                    )
                    continue

                self.socket.sendall(kesp_payload)
                response = self.socket.recv(4096)

                # Print the rich markup returned by the decoder
                console.print(self.decode_response(response))

            except KeyboardInterrupt:
                break
            except Exception as e:
                console.print(f"\n[danger]❌ Network Error: {e}[/danger]")
                break

        self.socket.close()
        console.print("\n[info]Disconnected.[/info]")


if __name__ == "__main__":
    cli = KESPClient()
    cli.repl()
