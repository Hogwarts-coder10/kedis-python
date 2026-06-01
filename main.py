import os
import socket
import sys

from pyfiglet import Figlet
from rich.console import Console
from rich.panel import Panel

from commands import CommandHandler
from parser import CommandParser
from store import KedisStore

console = Console()

HOST = "127.0.0.1"
PORT = 6379


def main():
    # ----------------------------------------------------
    # Startup Banner
    # ----------------------------------------------------
    fig = Figlet(font="big")
    logo = fig.renderText("KEDIS")

    console.print(
        Panel.fit(
            f"[bold cyan]{logo}[/bold cyan]"
            "\n[yellow]Codename: Echo[/yellow]"
            "\n[green]Version: 0.1.0[/green]",
            title="Database Client",
            border_style="cyan",
        )
    )

    # ----------------------------------------------------
    # The Auto-Detect Ignition (Network First, Local Fallback)
    # ----------------------------------------------------
    local_mode = False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    store = None
    handler = None

    try:
        s.connect((HOST, PORT))
        console.print("[green]✓ Network Link Established[/green]")
        console.print(
            f"[bold blue]Ready (TCP Network Mode) - Connected to {HOST}:{PORT}[/bold blue]\n"
        )

    except ConnectionRefusedError:
        # THE BOOT-UP CONSENT CHECK
        console.print(
            Panel(
                "[bold yellow]⚠️ Network database unavailable.[/bold yellow]\n\n"
                "[white]Switching to standalone mode will create or use\n"
                "a local database instance.\n\n"
                "Data may differ from the server.[/white]",
                border_style="yellow",
            )
        )

        choice = (
            console.input("[bold yellow]Continue? [Y/n]: [/bold yellow]")
            .strip()
            .lower()
        )
        if choice == "n":
            console.print("\n[bold red]Aborting. Shutting down Kedis CLI...[/bold red]")
            sys.exit(0)

        local_mode = True

        store = KedisStore()
        handler = CommandHandler(store)

        console.print("\n[green]✓ Local Storage Engine Online[/green]")
        console.print("[green]✓ Command Router Online[/green]")
        console.print("[green]✓ Persistence Layer Online[/green]")
        console.print(
            f"[cyan]Loaded {len(store._data)} keys from local persistence.[/cyan]"
        )
        console.print("[bold purple]Ready (Standalone Local Mode).[/bold purple]\n")

    console.print("[dim]Type 'exit', 'quit', or press Ctrl+C to shut down.[/dim]\n")

    debug_mode = False
    suppress_reconnect = False

    # ----------------------------------------------------
    # Main Event Loop
    # ----------------------------------------------------
    while True:
        try:
            # ----------------------------------------------------
            # THE AUTO-RECONNECT RADAR
            # ----------------------------------------------------
            if local_mode and not suppress_reconnect:
                radar = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                radar.settimeout(0.05)  # 50ms silent ping

                try:
                    radar.connect((HOST, PORT))
                    radar.close()  # Port is alive! Close the radar ping.

                    console.print(
                        Panel(
                            f"[bold yellow]⚠️ Kedis Server detected at {HOST}:{PORT}[/bold yellow]\n\n"
                            "[white]You are currently using a standalone database.\n\n"
                            "Switching to TCP mode will connect to the server database.\n\n"
                            "Local and server data may differ.[/white]",
                            border_style="yellow",
                        )
                    )

                    choice = (
                        console.input("[bold yellow]Switch? [Y/n]: [/bold yellow]")
                        .strip()
                        .lower()
                    )

                    if choice == "n":
                        suppress_reconnect = True
                        console.print(
                            "[dim]Staying in Standalone Mode. Reconnect radar disabled.[/dim]\n"
                        )
                    else:
                        # Re-engage the main TCP socket
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.connect((HOST, PORT))
                        local_mode = False

                        console.print("\n[green]✓ Network Link Re-Established[/green]")
                        console.print(
                            f"[bold blue]Ready (TCP Network Mode) - Connected to {HOST}:{PORT}[/bold blue]\n"
                        )

                except (socket.timeout, ConnectionRefusedError, OSError):
                    pass  # Server is still down, move along silently

            # ----------------------------------------------------
            # Dynamic Prompt
            # ----------------------------------------------------
            current_debug = (
                getattr(store, "debug_mode", False) if local_mode else debug_mode
            )
            prompt = (
                "[bold red]echo-debug[/bold red] ❯ "
                if current_debug
                else "[bold cyan]echo[/bold cyan] ❯ "
            )

            raw_input = console.input(prompt).strip()

            if not raw_input:
                continue

            cmd = raw_input.split()[0].upper()

            # Client-side Commands
            if cmd in ["CLS", "CLEAR"]:
                os.system("cls" if os.name == "nt" else "clear")
                continue

            if cmd == "DEBUG":
                if local_mode:
                    store.debug_mode = not getattr(store, "debug_mode", False)
                    debug_mode = store.debug_mode
                else:
                    debug_mode = not debug_mode
                    s.sendall(raw_input.encode("utf-8"))
                    s.recv(1024)

                status = (
                    "[bold red]ON 🔴[/bold red]"
                    if debug_mode
                    else "[bold green]OFF ⚪[/bold green]"
                )
                console.print(f"🔧 Diagnostic telemetry is now {status}")
                continue

            if cmd == "MODE":
                # Check current debug state
                current_debug = (
                    getattr(store, "debug_mode", False) if local_mode else debug_mode
                )
                debug_status = (
                    "[bold green]ON[/bold green]" if current_debug else "[dim]OFF[/dim]"
                )

                if local_mode:
                    radar_status = (
                        "[dim]Disabled[/dim]"
                        if suppress_reconnect
                        else "[green]Scanning[/green]"
                    )
                    key_count = len(store._data) if store else 0

                    console.print(
                        "\n[bold purple]Kedis Status[/bold purple]\n"
                        "[dim]────────────[/dim]\n"
                        f"Mode:            [purple]Standalone[/purple]\n"
                        f"Persistence:     [yellow]Local Disk[/yellow]\n"
                        f"Reconnect Radar: {radar_status}\n"
                        f"Debug:           {debug_status}\n"
                        f"Keys Loaded:     [cyan]{key_count}[/cyan]\n"
                    )
                else:
                    console.print(
                        "\n[bold blue]Kedis Status[/bold blue]\n"
                        "[dim]────────────[/dim]\n"
                        f"Mode:       [blue]TCP[/blue]\n"
                        f"Host:       [cyan]{HOST}[/cyan]\n"
                        f"Port:       [cyan]{PORT}[/cyan]\n"
                        f"Connection: [green]Active[/green]\n"
                        f"Debug:      {debug_status}\n"
                    )
                continue

            if cmd == "RECONNECT":
                if not local_mode:
                    console.print(
                        "[yellow]You are already connected to the Kedis TCP Engine.[/yellow]"
                    )
                else:
                    console.print(
                        "[white]Attempting manual TCP network re-engagement...[/white]"
                    )
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.connect((HOST, PORT))

                        local_mode = False
                        suppress_reconnect = False

                        console.print("[green]✓ Network Link Re-Established[/green]")
                        console.print(
                            f"[bold blue]Ready (TCP Network Mode) - Connected to {HOST}:{PORT}[/bold blue]\n"
                        )

                    except ConnectionRefusedError:
                        console.print(
                            "[bold red]❌ Connection failed. Kedis Server is still offline.[/bold red]"
                        )
                continue

            if cmd in ["EXIT", "QUIT"]:
                console.print("[bold red]Shutting down Kedis...[/bold red]")
                break

            # ----------------------------------------------------
            # The Routing Fork
            # ----------------------------------------------------
            if local_mode:
                # STANDALONE MODE
                tokens = CommandParser.parse(raw_input)

                if tokens and tokens[0] == "ERROR":
                    console.print(f"[red]{tokens[1]}[/red]")
                    continue

                if not tokens:
                    continue

                response = handler.execute(tokens)
                console.print(response)

            else:
                # TCP MODE
                try:
                    s.sendall(raw_input.encode("utf-8"))
                    data = s.recv(1024)

                    if not data:
                        raise OSError("Server silently dropped connection")

                    console.print(data.decode("utf-8").strip())

                except OSError:
                    # THE MID-SESSION HOT-SWAP CONSENT CHECK
                    console.print(
                        "\n[bold yellow]⚠️ FATAL: TCP Link Severed Mid-Session![/bold yellow]"
                    )
                    console.print(
                        "[white]Do you want to engage emergency hot-swap to the Local Engine?[/white]\n[dim](Data saved here will not sync to the server)[/dim]"
                    )

                    choice = (
                        console.input(
                            "\n[bold yellow]Continue in Standalone Mode? [Y/n]: [/bold yellow]"
                        )
                        .strip()
                        .lower()
                    )
                    if choice == "n":
                        console.print(
                            "\n[bold red]Aborting. Shutting down Kedis CLI...[/bold red]"
                        )
                        break

                    local_mode = True
                    suppress_reconnect = False

                    store = KedisStore()
                    handler = CommandHandler(store)
                    store.debug_mode = debug_mode

                    console.print(
                        "\n[green]✓ Local Engine Hot-Swapped Successfully[/green]"
                    )
                    console.print(
                        f"[cyan]Loaded {len(store._data)} keys from local persistence.[/cyan]\n"
                    )
                    continue

        except KeyboardInterrupt:
            console.print("\n[bold red]Shutting down Kedis...[/bold red]")
            break

        except Exception as e:
            console.print(
                f"[bold red](error) ERR internal server error: {str(e)}[/bold red]"
            )

    if not local_mode:
        s.close()


if __name__ == "__main__":
    main()
