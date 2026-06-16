from pyfiglet import Figlet
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


class UI:
    @staticmethod
    def print_banner(version="0.3.0", codename="Echo"):
        fig = Figlet(font="big")
        logo = fig.renderText("KEDIS")
        console.print(
            Panel.fit(
                f"[bold cyan]{logo}[/bold cyan]\n[yellow]Codename: {codename}[/yellow]\n[green]Version: {version}[/green]",
                title="Database Client",
                border_style="cyan",
            )
        )

    @staticmethod
    def print_panel(text, title, color):
        console.print(
            Panel(
                text,
                title=f"[bold {color}]{title}[/bold {color}]",
                border_style=color,
                expand=False,
            )
        )

    @staticmethod
    def render_typo(bad_cmd, matches):
        UI.print_panel(
            f"[bold red]❌ Unknown command:[/bold red] '{bad_cmd}'\n"
            f"[bold yellow]💡 Did you mean:[/bold yellow] [green]{matches[0]}[/green]?",
            "Syntax Error",
            "red",
        )

    @staticmethod
    def render_stats(response):
        """Beautifully renders the Engine Stats with the new LRU subsystem divider."""
        lines = response.split("\n")
        if len(lines) > 1:
            formatted_lines = []
            for line in lines:
                if line == "---":
                    # Create a slick divider for the LRU sub-system
                    formatted_lines.append("[dim]" + "─" * 30 + "[/dim]")
                elif ":" in line:
                    key, val = line.split(":", 1)
                    formatted_lines.append(
                        f"[cyan]{key.ljust(15)}[/cyan]: [yellow]{val}[/yellow]"
                    )

            UI.print_panel("\n".join(formatted_lines), "🔧 DEEP MEMORY MAP", "blue")
        else:
            console.print(f"[bold red]{response}[/bold red]")

    @staticmethod
    def render_wrongtype(response, raw_input):
        if "Expected" in response and "Found" in response:
            parts = response.replace("WRONGTYPE Expected ", "").split(", Found ")
            if len(parts) == 2:
                UI.print_panel(
                    "[bold red]❌ Operation against a key holding the wrong kind of value[/bold red]\n\n"
                    f"Expected: [green]{parts[0].strip()}[/green]\n"
                    f"Found:    [yellow]{parts[1].strip()}[/yellow]",
                    "Type Mismatch",
                    "red",
                )
            else:
                console.print(f"[bold red]{response}[/bold red]")
        else:
            target_key = raw_input.split()[1] if len(raw_input.split()) > 1 else "key"
            UI.print_panel(
                f"[bold red]❌ {response.lstrip('-')}[/bold red]\n\n"
                "[white]You attempted to run a command on a key that is built for a different data type.[/white]\n"
                f"[dim]Tip: Use 'TYPE {target_key}' to check the current structure.[/dim]",
                "Type Mismatch",
                "red",
            )

    @staticmethod
    def render_type_sensor(response, raw_input):
        target_name = raw_input.split()[1] if len(raw_input.split()) > 1 else "unknown"
        UI.print_panel(
            f"Key  : [bold white]{target_name}[/bold white]\nType : [bold cyan]{response}[/bold cyan]",
            "🔍 DATATYPE SENSOR",
            "blue",
        )

    @staticmethod
    def render_table(cmd, response, raw_input):
        target_name = raw_input.split()[1] if len(raw_input.split()) > 1 else "*"
        lines = response.split("\n")

        if cmd == "HGETALL" or (cmd == "ZRANGE" and "WITHSCORES" in raw_input.upper()):
            panel_title = (
                "🏎️ Telemetry Dossier" if cmd == "HGETALL" else "⏱️ Live Leaderboard"
            )
            table = Table(
                title=f"{panel_title}: [cyan]{target_name}[/cyan]",
                border_style="cyan",
                title_justify="left",
            )
            table.add_column("Field", style="yellow", justify="right")
            table.add_column("Value", style="green", min_width=25)

            for i in range(0, len(lines), 2):
                try:
                    table.add_row(lines[i].split('"')[1], lines[i + 1].split('"')[1])
                except IndexError:
                    continue
            console.print(table)
        else:
            title_map = {
                "LRANGE": "List Grid",
                "SMEMBERS": "VIP Paddock",
                "KEYS": "Active Key Radar",
                "ZRANGE": "Leaderboard Grid",
            }
            table = Table(
                title=f"📋 {title_map[cmd]}: [cyan]{target_name}[/cyan]",
                border_style="cyan",
                title_justify="left",
            )
            table.add_column("#", style="dim", justify="right")

            if cmd == "KEYS":
                table.add_column("Key", style="green", min_width=15)
                table.add_column("Type", style="magenta")
                table.add_column("TTL", style="yellow", justify="right")
                table.add_column("Length", style="cyan", justify="right")
            else:
                table.add_column("Value", style="green", min_width=25)

            for line in lines:
                try:
                    idx, remainder = line.split(") ", 1)
                    if cmd == "KEYS":
                        parts = remainder.split(" | ")
                        table.add_row(
                            idx, parts[0].strip('"'), parts[1], parts[2], parts[3]
                        )
                    else:
                        table.add_row(idx, remainder.strip('"'))
                except IndexError:
                    continue
            console.print(table)
