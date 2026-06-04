import os
import socket
import sys

from pyfiglet import Figlet
from rich.console import Console
from rich.panel import Panel
from rich.table import Table  # <-- The new UI component

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
            "\n[green]Version: 0.2.0[/green]",
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

            # ----------------------------------------------------
            # Upgraded Telemetry Panels
            # ----------------------------------------------------
            if cmd == "MODE":
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

                    mode_text = (
                        f"Mode:            [purple]Standalone[/purple]\n"
                        f"Persistence:     [yellow]Local Disk[/yellow]\n"
                        f"Reconnect Radar: {radar_status}\n"
                        f"Debug:           {debug_status}\n"
                        f"Keys Loaded:     [cyan]{key_count}[/cyan]"
                    )
                    console.print(
                        Panel(
                            mode_text,
                            title="[bold purple]Kedis Status[/bold purple]",
                            border_style="purple",
                            expand=False,
                        )
                    )
                else:
                    mode_text = (
                        f"Mode:       [blue]TCP[/blue]\n"
                        f"Host:       [cyan]{HOST}[/cyan]\n"
                        f"Port:       [cyan]{PORT}[/cyan]\n"
                        f"Connection: [green]Active[/green]\n"
                        f"Debug:      {debug_status}"
                    )
                    console.print(
                        Panel(
                            mode_text,
                            title="[bold blue]Kedis Status[/bold blue]",
                            border_style="blue",
                            expand=False,
                        )
                    )
                continue

            if cmd == "INFO":
                version = "0.2.0"
                codename = "Echo"

                if local_mode:
                    key_count = len(getattr(store, "_data", {})) if store else 0
                    exp_count = len(getattr(store, "_expires", {})) if store else 0
                    aof_file = getattr(store, "aof_filename", "kedis.aof")
                    aof_size = (
                        f"{os.path.getsize(aof_file) / 1024:.1f} KB"
                        if os.path.exists(aof_file)
                        else "0.0 KB"
                    )

                    # --- NEW: TYPE RADAR ---
                    str_c = list_c = set_c = hash_c = 0
                    if store:
                        for val in store._data.values():
                            if isinstance(val, list):
                                list_c += 1
                            elif isinstance(val, set):
                                set_c += 1
                            elif isinstance(val, dict):
                                hash_c += 1
                            else:
                                str_c += 1
                    type_breakdown = f"[dim]Str: {str_c} | Lst: {list_c} | Set: {set_c} | Hsh: {hash_c}[/dim]"

                    persistence = "AOF"
                    current_mode = "Standalone"
                else:
                    key_count = "N/A (Server)"
                    exp_count = "N/A (Server)"
                    aof_size = "N/A (Server)"
                    type_breakdown = "[dim]N/A (Server)[/dim]"

                    persistence = "TCP Stream"
                    current_mode = "TCP"

                current_debug = (
                    getattr(store, "debug_mode", False) if local_mode else debug_mode
                )
                debug_status = (
                    "[bold green]ON[/bold green]" if current_debug else "[dim]OFF[/dim]"
                )

                info_text = (
                    f"Version        : [green]{version}[/green]\n"
                    f"Codename       : [yellow]{codename}[/yellow]\n\n"
                    f"Keys           : [cyan]{key_count}[/cyan]\n"
                    f"Breakdown      : {type_breakdown}\n"
                    f"Expiring Keys  : [cyan]{exp_count}[/cyan]\n\n"
                    f"Persistence    : [yellow]{persistence}[/yellow]\n"
                    f"AOF Size       : [magenta]{aof_size}[/magenta]\n\n"
                    f"Mode           : [purple]{current_mode}[/purple]\n"
                    f"Debug          : {debug_status}"
                )
                console.print(
                    Panel(
                        info_text,
                        title="[bold blue]Kedis Information[/bold blue]",
                        border_style="blue",
                        expand=False,
                    )
                )
                continue

            if cmd == "HELP":
                help_text = (
                    "[bold cyan]Core String Commands[/bold cyan]\n"
                    "  [green]SET[/green] key val          : Store a string value\n"
                    "  [green]GET[/green] key              : Retrieve a value\n"
                    "  [green]DEL[/green] key              : Remove a key\n"
                    "  [green]EXPIRE[/green] key sec       : Set a time-to-live (TTL)\n"
                    "  [green]TTL[/green] key              : Check remaining lifespan\n"
                    "  [green]FLUSHALL[/green]            : Wipe the entire database\n\n"
                    "[bold cyan]List Commands (Ordered)[/bold cyan]\n"
                    "  [green]LPUSH / RPUSH[/green] k v... : Push to head or tail\n"
                    "  [green]LPOP / RPOP[/green] key     : Pop from head or tail\n"
                    "  [green]LRANGE[/green] k start stop  : Get a list slice\n\n"
                    "[bold cyan]Set Commands (Unique)[/bold cyan]\n"
                    "  [green]SADD[/green] key val...      : Add unique members\n"
                    "  [green]SMEMBERS[/green] key         : View all members\n"
                    "  [green]SREM[/green] key val...      : Remove members\n\n"
                    "[bold cyan]Hash Commands (Telemetry)[/bold cyan]\n"
                    "  [green]HSET[/green] key field val   : Set a hash field\n"
                    "  [green]HGET[/green] key field       : Get a hash field\n"
                    "  [green]HGETALL[/green] key          : Get the full dossier\n\n"
                    "[bold purple]Client Commands (Dashboard)[/bold purple]\n"
                    "  [yellow]KEYS[/yellow]                 : Radar of all active keys\n"
                    "  [yellow]COMPACT[/yellow]              : Compress the AOF log file\n"
                    "  [yellow]MODE[/yellow]                 : View current drivetrain (TCP/Local)\n"
                    "  [yellow]INFO[/yellow]                 : View engine telemetry and version\n"
                    "  [yellow]HELP[/yellow]                 : Show this command reference\n"
                    "  [yellow]DEBUG[/yellow]                : Toggle diagnostic logs\n"
                    "  [yellow]RECONNECT[/yellow]            : Manually reconnect to TCP server\n"
                    "  [yellow]CLEAR / CLS[/yellow]          : Clear the terminal screen\n"
                    "  [yellow]EXIT / QUIT[/yellow]          : Shut down the client\n"
                )
                console.print(
                    Panel(
                        help_text,
                        title="[bold yellow]Kedis Command Reference[/bold yellow]",
                        border_style="yellow",
                        expand=False,
                    )
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

                # --- THE GRAND UX INTERCEPTOR (LOCAL) ---
                if cmd == "COMPACT":
                    if "+OK" in response:
                        console.print(
                            Panel(
                                f"[bold green]Compaction Successful[/bold green]\n{response}",
                                title="🧹 AOF Cleaner",
                                border_style="green",
                                expand=False,
                            )
                        )
                    else:
                        console.print(
                            Panel(
                                f"[bold red]Compaction Failed[/bold red]\n{response}",
                                title="❌ Error",
                                border_style="red",
                                expand=False,
                            )
                        )

                elif cmd in ["HGETALL", "LRANGE", "SMEMBERS", "KEYS"]:
                    if (
                        "error" in response.lower()
                        or "WRONGTYPE" in response
                        or "(empty" in response
                    ):
                        console.print(response)
                    else:
                        target_name = (
                            raw_input.split()[1] if len(raw_input.split()) > 1 else "*"
                        )
                        lines = response.split("\n")

                        if cmd == "HGETALL":
                            table = Table(
                                title=f"🏎️ Telemetry Dossier: [cyan]{target_name}[/cyan]",
                                border_style="cyan",
                                title_justify="left",
                            )
                            table.add_column("Field", style="yellow", justify="right")
                            table.add_column("Value", style="green", min_width=25)

                            for i in range(0, len(lines), 2):
                                try:
                                    field = lines[i].split('"')[1]
                                    value = lines[i + 1].split('"')[1]
                                    table.add_row(field, value)
                                except IndexError:
                                    continue
                            console.print(table)
                        else:
                            title_map = {
                                "LRANGE": "List Grid",
                                "SMEMBERS": "VIP Paddock",
                                "KEYS": "Active Key Radar",
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
                                table.add_column(
                                    "Length", style="cyan", justify="right"
                                )
                            else:
                                table.add_column("Value", style="green", min_width=25)

                            for line in lines:
                                try:
                                    idx = line.split(") ", 1)[0]
                                    remainder = line.split(") ", 1)[1]

                                    if cmd == "KEYS":
                                        parts = remainder.split(" | ")
                                        key_name = parts[0].strip('"')
                                        key_type = parts[1]
                                        key_ttl = parts[2]
                                        key_len = parts[3]
                                        table.add_row(
                                            idx, key_name, key_type, key_ttl, key_len
                                        )
                                    else:
                                        val = remainder.strip('"')
                                        table.add_row(idx, val)
                                except IndexError:
                                    continue
                            console.print(table)
                else:
                    console.print(response)

            else:
                # TCP MODE
                try:
                    s.sendall(raw_input.encode("utf-8"))
                    data = s.recv(1024)

                    if not data:
                        raise OSError("Server silently dropped connection")

                    response = data.decode("utf-8").strip()

                    # --- THE GRAND UX INTERCEPTOR (TCP) ---
                    if cmd == "COMPACT":
                        if "+OK" in response:
                            console.print(
                                Panel(
                                    f"[bold green]Compaction Successful[/bold green]\n{response}",
                                    title="🧹 AOF Cleaner",
                                    border_style="green",
                                    expand=False,
                                )
                            )
                        else:
                            console.print(
                                Panel(
                                    f"[bold red]Compaction Failed[/bold red]\n{response}",
                                    title="❌ Error",
                                    border_style="red",
                                    expand=False,
                                )
                            )

                    elif cmd in ["HGETALL", "LRANGE", "SMEMBERS", "KEYS"]:
                        if (
                            "error" in response.lower()
                            or "WRONGTYPE" in response
                            or "(empty" in response
                        ):
                            console.print(response)
                        else:
                            target_name = (
                                raw_input.split()[1]
                                if len(raw_input.split()) > 1
                                else "*"
                            )
                            lines = response.split("\n")

                            if cmd == "HGETALL":
                                table = Table(
                                    title=f"🏎️ Telemetry Dossier: [cyan]{target_name}[/cyan]",
                                    border_style="cyan",
                                    title_justify="left",
                                )
                                table.add_column(
                                    "Field", style="yellow", justify="right"
                                )
                                table.add_column("Value", style="green", min_width=25)

                                for i in range(0, len(lines), 2):
                                    try:
                                        field = lines[i].split('"')[1]
                                        value = lines[i + 1].split('"')[1]
                                        table.add_row(field, value)
                                    except IndexError:
                                        continue
                                console.print(table)
                            else:
                                title_map = {
                                    "LRANGE": "List Grid",
                                    "SMEMBERS": "VIP Paddock",
                                    "KEYS": "Active Key Radar",
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
                                    table.add_column(
                                        "TTL", style="yellow", justify="right"
                                    )
                                    table.add_column(
                                        "Length", style="cyan", justify="right"
                                    )
                                else:
                                    table.add_column(
                                        "Value", style="green", min_width=25
                                    )

                                for line in lines:
                                    try:
                                        idx = line.split(") ", 1)[0]
                                        remainder = line.split(") ", 1)[1]

                                        if cmd == "KEYS":
                                            parts = remainder.split(" | ")
                                            key_name = parts[0].strip('"')
                                            key_type = parts[1]
                                            key_ttl = parts[2]
                                            key_len = parts[3]
                                            table.add_row(
                                                idx,
                                                key_name,
                                                key_type,
                                                key_ttl,
                                                key_len,
                                            )
                                        else:
                                            val = remainder.strip('"')
                                            table.add_row(idx, val)
                                    except IndexError:
                                        continue
                                console.print(table)
                    else:
                        console.print(response)

                except OSError:
                    # THE MID-SESSION HOT-SWAP CONSENT CHECK
                    crash_text = (
                        "[bold yellow]⚠️ FATAL: TCP Link Severed Mid-Session![/bold yellow]\n\n"
                        "[white]Do you want to engage emergency hot-swap to the Local Engine?[/white]\n"
                        "[dim](Data saved here will not sync to the server)[/dim]"
                    )
                    console.print(
                        Panel(
                            crash_text,
                            title="🚨 Connection Lost",
                            border_style="red",
                            expand=False,
                        )
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

                    # Added a sleek success panel for the hot-swap completion
                    success_text = (
                        "[green]✓ Local Engine Hot-Swapped Successfully[/green]\n"
                        f"[cyan]Loaded {len(store._data)} keys from local persistence.[/cyan]"
                    )
                    console.print(
                        Panel(
                            success_text,
                            title="🔧 Emergency Override",
                            border_style="green",
                            expand=False,
                        )
                    )
                    print()  # Blank line for spacing
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
