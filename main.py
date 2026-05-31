import os
import sys

from rich.console import Console
from rich.panel import Panel

from commands import CommandHandler
from parser import CommandParser
from store import KedisStore

console = Console()


def main():
    console.print(
        Panel.fit(
            "[bold cyan]KEDIS-PYTHON[/bold cyan]\n"
            "[yellow]Codename: Echo[/yellow]\n"
            "[green]Version: 0.1.0[/green]",
            title="Database Engine",
            border_style="cyan",
        )
    )

    console.print("[green]✓ Storage Engine Online[/green]")
    console.print("[green]✓ Command Router Online[/green]")
    console.print("[green]✓ Persistence Layer Online[/green]")
    console.print("[bold blue]Ready.[/bold blue]\n")

    console.print("[dim]Type 'exit', 'quit', or press Ctrl+C to shut down.[/dim]\n")

    # Initialize the core engine and routing layer
    store = KedisStore()
    handler = CommandHandler(store)

    console.print(f"[cyan]Loaded {len(store._data)} keys from persistence.[/cyan]\n")

    # Main Event Loop
    while True:
        try:
            # ----------------------------------------------------
            # Dynamic Prompt
            # ----------------------------------------------------
            if store.debug_mode:
                prompt = "[bold red]echo-debug[/bold red] ❯ "
            else:
                prompt = "[bold cyan]echo[/bold cyan] ❯ "

            raw_input = console.input(prompt).strip()

            if not raw_input:
                continue

            # ----------------------------------------------------
            # Parse
            # ----------------------------------------------------
            tokens = CommandParser.parse(raw_input)

            if tokens and tokens[0] == "ERROR":
                console.print(f"[red]{tokens[1]}[/red]")
                continue

            if not tokens:
                continue

            cmd = tokens[0].upper()

            # ----------------------------------------------------
            # Client-side Commands
            # ----------------------------------------------------
            if cmd in ["CLS", "CLEAR"]:
                os.system("cls" if os.name == "nt" else "clear")
                continue

            if cmd == "DEBUG":
                store.debug_mode = not store.debug_mode

                status = (
                    "[bold red]ON 🔴[/bold red]"
                    if store.debug_mode
                    else "[bold green]OFF ⚪[/bold green]"
                )

                console.print(f"🔧 Diagnostic telemetry is now {status}")

                continue

            if cmd in ["EXIT", "QUIT"]:
                console.print("[bold red]Shutting down Kedis...[/bold red]")
                break

            # ----------------------------------------------------
            # Route to Engine
            # ----------------------------------------------------
            response = handler.execute(tokens)
            console.print(response)

        except KeyboardInterrupt:
            console.print("\n[bold red]Shutting down Kedis...[/bold red]")
            break

        except Exception as e:
            console.print(
                f"[bold red](error) ERR internal server error: {str(e)}[/bold red]"
            )


if __name__ == "__main__":
    main()
