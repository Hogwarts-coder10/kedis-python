import difflib
import os
import sys

# --- BUG FIX: Issue #1 (Linux Kitty Terminal Compatibility) ---

# Intercept and neutralize Kitty's advanced keyboard protocol before
# GNU readline or Rich initializes and breaks the terminal cursor state.

if (
    os.environ.get("TERM") == "xterm-kitty"
    or "kitty" in os.environ.get("TERM", "").lower()
):
    os.environ["KITTY_DISABLE_KEYBOARD_PROTOCOL"] = "1"

    os.environ["TERM"] = "xterm-256color"


from commands import CommandHandler
from network import NetworkManager
from parser import CommandParser
from store import KedisStore
from ui import UI, console

# --- The Command History Hook ---

try:
    import atexit
    import readline

    histfile = os.path.join(os.path.expanduser("~"), ".kedis_history")

    try:
        readline.read_history_file(histfile)

        readline.set_history_length(1000)

    except FileNotFoundError:
        pass

    atexit.register(readline.write_history_file, histfile)

except ImportError:
    pass


# --- The Dictionary of Valid Commands ---

VALID_COMMANDS = [
    "SET",
    "GET",
    "DEL",
    "EXPIRE",
    "TTL",
    "FLUSHALL",
    "LPUSH",
    "RPUSH",
    "LPOP",
    "RPOP",
    "LRANGE",
    "SADD",
    "SMEMBERS",
    "SREM",
    "HSET",
    "HGET",
    "HGETALL",
    "ZADD",
    "ZRANGE",
    "KEYS",
    "TYPE",
    "STATS",
    "COMPACT",
    "CONFIG",  # <-- Whitelisted for the client loop
    "MODE",
    "INFO",
    "HELP",
    "DEBUG",
    "RECONNECT",
    "CLEAR",
    "CLS",
    "EXIT",
    "QUIT",
    "MULTI",
    "EXEC",
    "DISCARD",
    "REPLICAOF",
]


class KedisClient:
    def __init__(self):
        self.network = NetworkManager()
        self.local_mode = False
        self.suppress_reconnect = False
        self.debug_mode = False
        self.store = None
        self.handler = None

    def boot(self):
        """The Ignition Sequence"""
        UI.print_banner()

        try:
            self.network.connect()
            console.print("[green]✓ Network Link Established[/green]")
            console.print(
                f"[bold blue]Ready (TCP Network Mode) - Connected to {self.network.host}:{self.network.port}[/bold blue]\n"
            )
        except ConnectionRefusedError:
            UI.print_panel(
                "[bold yellow]⚠️ Network database unavailable.[/bold yellow]\n\n"
                "[white]Switching to standalone mode will create or use\n"
                "a local database instance.\n\n"
                "Data may differ from the server.[/white]",
                "Connection Failed",
                "yellow",
            )
            choice = (
                console.input("[bold yellow]Continue? [Y/n]: [/bold yellow]")
                .strip()
                .lower()
            )

            if choice == "n":
                console.print(
                    "\n[bold red]Aborting. Shutting down Kedis CLI...[/bold red]"
                )
                sys.exit(0)

            self._init_local_engine()

        console.print("[dim]Type 'exit', 'quit', or press Ctrl+C to shut down.[/dim]\n")
        self.run_loop()

    def _init_local_engine(self):
        self.local_mode = True

        # --- THE DRIVETRAIN SELECTOR MENU ---
        engine_text = (
            "[bold cyan]1) appendfsync always[/bold cyan] [dim](Max Safety)[/dim]\n"
            "[white]Forces a physical OS disk write for every single command.\n"
            "Zero data loss on a power cut, but throttles Transactions Per Second (TPS).[/white]\n\n"
            "[bold green]2) appendfsync everysec[/bold green] [dim](Max Speed - Recommended)[/dim]\n"
            "[white]Batches memory writes to the SSD in a background thread once per second.\n"
            "Massive TPS boost, but risks losing 1 second of telemetry if the system crashes.[/white]"
        )
        UI.print_panel(engine_text, "⚙️ SELECT I/O DRIVETRAIN", "blue")

        choice = console.input(
            "[bold yellow]Select mode (1 or 2) [default: 2]: [/bold yellow]"
        ).strip()
        sync_mode = "always" if choice == "1" else "everysec"

        self.store = KedisStore(appendfsync=sync_mode, lru_maxsize=128)
        self.handler = CommandHandler(self.store)

        console.print("\n[green]✓ Local Storage Engine Online[/green]")
        console.print(f"[green]✓ I/O Drivetrain Locked: {sync_mode.upper()}[/green]")
        console.print("[green]✓ Command Router Online[/green]")
        console.print("[green]✓ Persistence Layer Online[/green]")
        console.print(
            f"[cyan]Loaded {len(self.store._data)} keys from local persistence.[/cyan]"
        )
        console.print("[bold purple]Ready (Standalone Local Mode).[/bold purple]\n")

    def run_loop(self):
        """The Core Terminal Loop"""
        while True:
            try:
                self._check_radar()

                # Determine prompt debug status
                current_debug = (
                    getattr(self.store, "debug_mode", False)
                    if self.local_mode
                    else self.debug_mode
                )

                # --- BUG FIX: Linux Kitty Terminal & POSIX Readline Invisible Input ---
                if os.name == "posix" and "readline" in sys.modules:
                    if current_debug:
                        prompt = "\001\x1b[1;31m\002echo-debug\001\x1b[0m\002 ❯ "
                    else:
                        prompt = "\001\x1b[1;36m\002echo\001\x1b[0m\002 ❯ "

                    raw_input = input(prompt).strip()

                else:
                    # Windows or fallback (rich handles this fine)
                    prompt = (
                        "[bold red]echo-debug[/bold red] ❯ "
                        if current_debug
                        else "[bold cyan]echo[/bold cyan] ❯ "
                    )
                    raw_input = console.input(prompt).strip()

                if not raw_input:
                    continue

                cmd = raw_input.split()[0].upper()

                # If it's a UI/Client command, handle it and skip the database trip
                if self._handle_client_commands(cmd, raw_input, current_debug):
                    continue

                # Otherwise, execute against the DB and render the result
                self._execute_and_render(cmd, raw_input)

            except KeyboardInterrupt:
                console.print("\n[bold red]Shutting down Kedis...[/bold red]")
                break
            except TypeError as e:
                # Catch-all for engine type errors leaking out
                UI.render_wrongtype(str(e), "key")
            except Exception as e:
                console.print(
                    f"[bold red](error) ERR internal client error: {str(e)}[/bold red]"
                )

        self.network.disconnect()

    def _check_radar(self):
        """Scans for TCP server recovery in the background."""
        if self.local_mode and not self.suppress_reconnect:
            if self.network.ping_radar():
                UI.print_panel(
                    f"[bold yellow]⚠️ Kedis Server detected at {self.network.host}:{self.network.port}[/bold yellow]\n\n"
                    "[white]You are currently using a standalone database.\n\n"
                    "Switching to TCP mode will connect to the server database.\n\n"
                    "Local and server data may differ.[/white]",
                    "Network Available",
                    "yellow",
                )
                choice = (
                    console.input("[bold yellow]Switch? [Y/n]: [/bold yellow]")
                    .strip()
                    .lower()
                )

                if choice == "n":
                    self.suppress_reconnect = True
                    console.print(
                        "[dim]Staying in Standalone Mode. Reconnect radar disabled.[/dim]\n"
                    )
                else:
                    self.network.connect()
                    self.local_mode = False
                    console.print("\n[green]✓ Network Link Re-Established[/green]")
                    console.print(
                        f"[bold blue]Ready (TCP Network Mode) - Connected to {self.network.host}:{self.network.port}[/bold blue]\n"
                    )

    def _handle_client_commands(self, cmd, raw_input, current_debug) -> bool:
        """Processes commands that don't need to hit the database engine."""
        if cmd in ["CLS", "CLEAR"]:
            os.system("cls" if os.name == "nt" else "clear")
            return True

        if cmd in ["EXIT", "QUIT"]:
            console.print("[bold red]Shutting down Kedis...[/bold red]")
            sys.exit(0)

        if cmd == "DEBUG":
            if self.local_mode:
                self.store.debug_mode = not getattr(self.store, "debug_mode", False)
                self.debug_mode = self.store.debug_mode
            else:
                self.debug_mode = not self.debug_mode
                self.network.send_command(raw_input)

            status = (
                "[bold red]ON 🔴[/bold red]"
                if self.debug_mode
                else "[bold green]OFF ⚪[/bold green]"
            )
            console.print(f"\n[dim]🔧 Diagnostic telemetry is now {status}[/dim]\n")
            return True

        if cmd == "RECONNECT":
            if not self.local_mode:
                UI.print_panel(
                    "[white]You are already connected to the Kedis TCP Engine.[/white]",
                    "⚠ NETWORK STATUS",
                    "yellow",
                )
            else:
                console.print(
                    "[dim]Attempting manual TCP network re-engagement...[/dim]"
                )

                try:
                    self.network.connect()
                    self.local_mode = False
                    self.suppress_reconnect = False
                    UI.print_panel(
                        "[bold green]✓ Reconnected successfully[/bold green]\n"
                        f"[blue]Mode: TCP Network ({self.network.host}:{self.network.port})[/blue]",
                        "🔌 CONNECTION RESTORED",
                        "green",
                    )
                except OSError:
                    UI.print_panel(
                        "[bold red]✖ Recovery failed. Server is still offline.[/bold red]",
                        "✖ CONNECTION FAILED",
                        "red",
                    )
            return True

        if cmd == "MODE":
            debug_status = (
                "[bold green]ON[/bold green]" if current_debug else "[dim]OFF[/dim]"
            )

            if self.local_mode:
                radar_status = (
                    "[dim]Disabled[/dim]"
                    if self.suppress_reconnect
                    else "[green]Scanning[/green]"
                )
                key_count = len(self.store._data) if self.store else 0

                mode_text = (
                    f"Mode:            [purple]Standalone[/purple]\n"
                    f"Persistence:     [yellow]Local Disk[/yellow]\n"
                    f"Reconnect Radar: {radar_status}\n"
                    f"Debug:           {debug_status}\n"
                    f"Keys Loaded:     [cyan]{key_count}[/cyan]"
                )
                UI.print_panel(mode_text, "Kedis Status", "purple")
            else:
                mode_text = (
                    f"Mode:       [blue]TCP[/blue]\n"
                    f"Host:       [cyan]{self.network.host}[/cyan]\n"
                    f"Port:       [cyan]{self.network.port}[/cyan]\n"
                    f"Connection: [green]Active[/green]\n"
                    f"Debug:      {debug_status}"
                )
                UI.print_panel(mode_text, "Kedis Status", "blue")
            return True

        if cmd == "INFO":
            version = "0.3.0"
            codename = "Echo"

            if self.local_mode:
                key_count = len(getattr(self.store, "_data", {})) if self.store else 0
                exp_count = (
                    len(getattr(self.store, "_expires", {})) if self.store else 0
                )
                aof_file = getattr(self.store, "aof_filename", "kedis.aof")
                aof_size = (
                    f"{os.path.getsize(aof_file) / 1024:.1f} KB"
                    if os.path.exists(aof_file)
                    else "0.0 KB"
                )

                str_c = list_c = set_c = hash_c = zset_c = 0
                if self.store:
                    for val in self.store._data.values():
                        val_type = type(val).__name__

                        if val_type == "list":
                            list_c += 1
                        elif val_type == "set":
                            set_c += 1
                        elif val_type == "dict":
                            hash_c += 1
                        elif val_type == "SkipList":
                            zset_c += 1
                        else:
                            str_c += 1

                type_breakdown = f"[dim]Str: {str_c} | Lst: {list_c} | Set: {set_c} | Hsh: {hash_c} | ZSet: {zset_c}[/dim]"

                # Dynamically pull the active I/O drivetrain (Safely scoped to Local Mode)
                sync_policy = (
                    self.store.appendfsync.upper()
                    if getattr(self, "store", None)
                    else "UNKNOWN"
                )
                persistence = f"AOF ({sync_policy})"
                current_mode = "Standalone"

            else:
                key_count = exp_count = aof_size = "N/A (Server)"
                type_breakdown = "[dim]N/A (Server)[/dim]"
                persistence = "TCP Stream"
                current_mode = "TCP"

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

            UI.print_panel(info_text, "Kedis Information", "blue")

            return True

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
                "[bold cyan]Sorted Set Commands (Leaderboard)[/bold cyan]\n"
                "  [green]ZADD[/green] key score val     : Add scored members\n"
                "  [green]ZRANGE[/green] key start stop  : Get the sorted leaderboard\n\n"
                "[bold purple]Client Commands (Dashboard)[/bold purple]\n"
                "  [yellow]KEYS[/yellow]                 : Radar of all active keys\n"
                "  [yellow]COMPACT[/yellow]              : Compress the AOF log file\n"
                "  [yellow]MODE[/yellow]                 : View current drivetrain (TCP/Local)\n"
                "  [yellow]INFO[/yellow]                 : View engine telemetry and version\n"
                "  [yellow]HELP[/yellow]                 : Show this command reference\n"
                "  [yellow]DEBUG[/yellow]                : Toggle diagnostic logs\n"
                "  [yellow]RECONNECT[/yellow]            : Manually reconnect to TCP server\n"
                "  [yellow]STATS[/yellow]                : Deep Memory Map\n"
                "  [yellow]CONFIG[/yellow]               : Hot-swap engine dials mid-flight\n"
                "  [yellow]CLEAR / CLS[/yellow]          : Clear the terminal screen\n"
                "  [yellow]EXIT / QUIT[/yellow]          : Shut down the client\n"
            )

            UI.print_panel(help_text, "Kedis Command Reference", "yellow")
            return True

        return False

    def _execute_and_render(self, cmd, raw_input):
        """The KESP Transmission: Routes execution, decodes KESP, and renders UI."""
        try:
            # 1. TRANSLATE HUMAN TYPING TO KESP BYTES
            kesp_payload = self._encode_kesp_for_engine(raw_input)
            if not kesp_payload:
                console.print(
                    "[bold red](error) Invalid syntax. Check your quotes.[/bold red]"
                )
                return

            if self.local_mode:
                if cmd == "STATS":
                    raw_response = self.handler.execute(["STATS"])
                else:
                    # Feed the KESP bytes to the intake parser
                    tokens = CommandParser.parse(kesp_payload)
                    if tokens and tokens[0] == "ERROR":
                        console.print(f"[red]{tokens[1]}[/red]")
                        return
                    if not tokens:
                        return
                    raw_response = self.handler.execute(tokens)

                # Route the local Python response through the KESP Exhaust Encoder
                from parser import KESPEncoder

                kesp_bytes = KESPEncoder.encode(raw_response)

            else:
                # TCP MODE: Send raw KESP bytes across the wire
                try:
                    self.network.socket.sendall(kesp_payload)
                    kesp_bytes = self.network.socket.recv(4096)
                except AttributeError:
                    # Fallback if NetworkManager doesn't expose the raw socket
                    kesp_bytes = self.network.send_command(kesp_payload.decode("utf-8"))

            # 2. TRANSLATE KESP BYTES BACK TO UI TEXT
            response_text = self._decode_kesp_for_ui(kesp_bytes)

        except OSError:
            self._handle_tcp_crash()
            return
        except Exception as e:
            console.print(
                f"[bold red](error) ERR internal client error: {str(e)}[/bold red]"
            )
            return

        # --- THE GRAND UX INTERCEPTOR (UNIFIED) ---
        if cmd == "COMPACT":
            if "OK" in response_text:
                UI.print_panel(
                    f"[bold green]Compaction Successful[/bold green]\n{response_text}",
                    "🧹 AOF Cleaner",
                    "green",
                )
            else:
                UI.print_panel(
                    f"[bold red]Compaction Failed[/bold red]\n{response_text}",
                    "❌ Error",
                    "red",
                )

        elif cmd == "STATS":
            UI.render_stats(response_text)

        elif cmd == "CONFIG":
            tokens = raw_input.split()
            sub_cmd = tokens[1].upper() if len(tokens) > 1 else ""
            UI.render_config(response_text, sub_cmd)

        elif (
            "unknown command" in response_text.lower()
            or "err command not found" in response_text.lower()
        ):
            matches = difflib.get_close_matches(cmd, VALID_COMMANDS, n=1, cutoff=0.5)
            if matches:
                UI.render_typo(cmd, matches)
            else:
                console.print(f"[bold red]{response_text}[/bold red]")

        elif "WRONGTYPE" in response_text:
            UI.render_wrongtype(response_text, raw_input)

        elif cmd == "TYPE":
            if "error" in response_text.lower():
                console.print(response_text)
            else:
                UI.render_type_sensor(response_text, raw_input)

        elif cmd in ["HGETALL", "LRANGE", "SMEMBERS", "KEYS", "ZRANGE"]:
            if (
                "error" in response_text.lower()
                or "wrongtype" in response_text.lower()
                or "(empty" in response_text
            ):
                console.print(response_text)
            else:
                UI.render_table(cmd, response_text, raw_input)

        else:
            console.print(response_text)

    def _encode_kesp_for_engine(self, user_input: str) -> bytes:
        """Packs standard UI typing into strict KESP Array bytes."""
        import shlex

        try:
            tokens = shlex.split(user_input)
        except ValueError:
            return b""

        if not tokens:
            return b""
        header = f"A{len(tokens)}\n".encode("utf-8")
        body = b"".join(
            f"S{len(t.encode('utf-8'))}\n{t}\n".encode("utf-8") for t in tokens
        )
        return header + body

    def _decode_kesp_for_ui(self, raw_bytes: bytes) -> str:
        """
        Translates raw KESP bytes into the human-readable text strings
        that the UI rendering functions (like render_table) expect.
        """
        if not raw_bytes:
            return "(connection closed)"

        try:
            # 🛡️ THE FIX: Safely handle both raw bytes and pre-decoded text strings
            text = (
                raw_bytes.decode("utf-8")
                if isinstance(raw_bytes, bytes)
                else str(raw_bytes)
            )
            if not text:
                return ""

            sigil = text[0]

            if sigil == "+":
                return text[1:].strip()
            elif sigil == "E":
                return f"(error) {text[1:].strip()}"
            elif sigil == "I":
                return f"(integer) {text[1:].strip()}"
            elif sigil == "N":
                return "(nil)"
            elif sigil == "S":
                parts = text.split("\n", 1)
                if len(parts) > 1:
                    data = parts[1].rsplit("\n", 1)[0]
                    return f'"{data}"'
            elif sigil == "A":
                # 🚀 Format KESP Arrays specifically so UI.render_table() can parse them
                lines = text.strip().split("\n")
                if len(lines) <= 1:
                    return "(empty array)"

                output = []
                idx = 1
                item_num = 1

                while idx < len(lines):
                    if lines[idx].startswith("S"):
                        output.append(f"{item_num}) {lines[idx + 1]}")
                        idx += 2
                    elif lines[idx].startswith("I"):
                        output.append(f"{item_num}) {lines[idx][1:]}")
                        idx += 1
                    elif lines[idx].startswith("N"):
                        output.append(f"{item_num}) (nil)")
                        idx += 1
                    else:
                        idx += 1
                        continue
                    item_num += 1

                return "\n".join(output) if output else "(empty array)"

            return text.strip()

        except Exception as e:
            return f"(decoder error) {e}"

    def _handle_tcp_crash(self):
        """Gracefully recovers if the TCP server explodes mid-query."""
        crash_text = (
            "[bold yellow]⚠️ FATAL: TCP Link Severed Mid-Session![/bold yellow]\n\n"
            "[white]Do you want to engage emergency hot-swap to the Local Engine?[/white]\n"
            "[dim](Data saved here will not sync to the server)[/dim]"
        )

        UI.print_panel(crash_text, "🚨 Connection Lost", "red")

        choice = (
            console.input(
                "\n[bold yellow]Continue in Standalone Mode? [Y/n]: [/bold yellow]"
            )
            .strip()
            .lower()
        )

        if choice == "n":
            console.print("\n[bold red]Aborting. Shutting down Kedis CLI...[/bold red]")
            sys.exit(0)

        self._init_local_engine()

        UI.print_panel(
            "[green]✓ Local Engine Hot-Swapped Successfully[/green]\n"
            f"[cyan]Loaded {len(self.store._data)} keys from local persistence.[/cyan]",
            "🔧 Emergency Override",
            "green",
        )


if __name__ == "__main__":
    client = KedisClient()
    client.boot()
