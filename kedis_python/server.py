import asyncio
import contextlib
import json
import signal
import sys
import threading
import time

from rich.console import Console
from rich.markup import escape

from .commands import CommandHandler
from .parser import CommandParser, KESPEncoder, ProtocolError, ReplyEncodeError
from .store import KedisStore
from .stream import StreamError
from .textcodec import to_bytes
from .ui import UI

console = Console()

# ---------------------------------------------------------
# The Shared Global Database Core
# ---------------------------------------------------------

global_store = KedisStore()
global_handler = CommandHandler(global_store)

HOST = "127.0.0.1"
PORT = 6379

# --- Per-connection budgets (see docs/limits.md) ---------------------------------
MAX_CLIENTS = 10_000  # connections served at once; more are refused
# A command that is only partly received must keep arriving: it is dropped if no
# byte comes for STALL_TIMEOUT seconds, or if it is still unfinished after
# FRAME_DEADLINE seconds plus the time FRAME_MIN_RATE bytes/second would need for
# its size. A connection with nothing half-sent can idle for as long as it likes.
STALL_TIMEOUT = 30.0
FRAME_DEADLINE = 60.0
FRAME_MIN_RATE = 256 * 1024
YIELD_EVERY = 256  # commands run back to back before other clients get a turn
ENCODE_CHUNK = 512  # reply elements encoded before other clients get a turn
# Replies must be read too: a client whose unread replies leave the socket full
# is dropped once a send has waited WRITE_DEADLINE seconds plus the time
# WRITE_MIN_RATE bytes/second would need to move what is already queued for it.
# Without this a client that never reads holds its connection (and a MAX_CLIENTS
# slot) for ever. See docs/limits.md.
WRITE_DEADLINE = 30.0
WRITE_MIN_RATE = 256 * 1024
# One client's open MULTI block may queue at most this many commands, and at most
# this many bytes of arguments (UTF-8), so a client that never sends EXEC cannot
# grow the queue without bound. See docs/limits.md.
MAX_MULTI_COMMANDS = 10_000
MAX_MULTI_BYTES = 64 * 1024 * 1024
_active_clients = 0

# Replication Telemetry
server_role = "master"
server_host = None
master_port = None
connected_replicas = []  # 📡 Holds the writer sockets for all active Followers


class SyncBarrier:
    """Keeps a new follower's snapshot and the live stream from overlapping or
    leaving a gap.

    A write is applied in a worker thread and forwarded to followers afterwards,
    on the event loop. A follower that joined between the two would get the write
    twice (it is in the snapshot, and it is forwarded), and one whose snapshot was
    taken just before a write but registered just after would never get it. So a
    write holds the barrier from before it is applied until it is in every
    follower's buffer, and a SYNC takes the snapshot and registers the follower
    with no write in between. Writes do not wait for each other, only for a SYNC
    that is in progress (a snapshot, not the send of it).
    """

    def __init__(self):
        self.inflight = 0  # writes between "about to apply" and "buffered for followers"
        self.syncing = False  # a SYNC is taking its snapshot
        self._waiters = []

    def _wake(self):
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            try:
                if not waiter.done():
                    waiter.set_result(None)
            except RuntimeError:  # its event loop is gone
                pass

    async def _until(self, ready):
        while not ready():
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            await waiter

    def write(self):
        return _WriteScope(self)  # a plain class: this is on the path of every write

    @contextlib.asynccontextmanager
    async def snapshot(self):
        await self._until(lambda: not self.syncing)  # one SYNC at a time
        self.syncing = True
        try:
            await self._until(lambda: self.inflight == 0)
            yield
        finally:
            self.syncing = False
            self._wake()


class _WriteScope:
    __slots__ = ("_barrier",)

    def __init__(self, barrier):
        self._barrier = barrier

    async def __aenter__(self):
        barrier = self._barrier
        if barrier.syncing:
            await barrier._until(lambda: not barrier.syncing)
        barrier.inflight += 1

    async def __aexit__(self, *exc):
        barrier = self._barrier
        barrier.inflight -= 1
        if barrier._waiters:
            barrier._wake()


replication_barrier = SyncBarrier()


# Taken, in a worker thread, around "apply a write, then hand it to the event loop to
# buffer for followers". Writes are applied one at a time under the engine lock, but
# a thread can be slow to report back once it has applied its write, and a later
# write could then overtake it on the way to the followers. Scheduling the buffering
# from inside this lock puts it on the loop in the order the writes were applied
# (the loop runs what it is handed first in, first out). Always taken before the
# engine lock, never after.
_write_order = threading.Lock()


async def apply_in_order(run, frames_of, label: str):
    """Runs `run()` (a write) in a worker thread and returns (its result, queued),
    where `queued` is what drain_broadcast needs for the frames `frames_of(result)`
    produced, already in the followers' buffers: the buffering is scheduled onto the
    loop from inside the write-order lock, so it runs before this coroutine resumes
    and in the order the writes were applied. `queued` is None when there was
    nothing to forward (or this node is not a master)."""
    loop = asyncio.get_running_loop()
    queued: list = []

    def work():
        with _write_order:
            out = run()
            if server_role == "master":
                frames = frames_of(out)
                if frames:
                    loop.call_soon_threadsafe(
                        lambda: queued.append(enqueue_broadcast(frames, label))
                    )
            return out

    out = await asyncio.to_thread(work)
    return out, (queued[0] if queued else None)


async def loop_latency_monitor(store):
    """
    Diagnostic sensor: Pulses every 100ms. If it takes longer to wake up,
    the event loop is being blocked by a synchronous operation.
    """

    store.current_lag_ms = 0.0

    while True:
        start_time = time.perf_counter()

        # We expect the loop to hand control back in exactly 100ms
        await asyncio.sleep(0.1)

        end_time = time.perf_counter()

        # Calculating actual sleep time
        actual_sleep_time = (end_time - start_time) * 1000
        lag = actual_sleep_time - 100.0

        # Floor to 0 (sometimes sleep wakes up a fraction early), round to 2 decimals
        store.current_lag_ms = max(0.0, round(lag, 2))


HOUSEKEEPING_INTERVAL = 30.0  # seconds between maintenance passes

# ----------------------------------------------------------------------
# BLOCKING READS (XREAD ... BLOCK)
# A blocked session parks on a future. Writers call notify_stream_waiters()
# after a write completes; every parked reader wakes, re-reads, and goes back
# to sleep if its streams still have nothing new. Futures belong to the running
# loop, so there is no module-level loop-bound object to get wrong.
# ----------------------------------------------------------------------
stream_waiters: set = set()
_BLOCK_POLL = 0.5  # how often a blocked session checks that its client is alive


def notify_stream_waiters():
    for fut in list(stream_waiters):
        if not fut.done():
            fut.set_result(None)
    stream_waiters.clear()


def _encode_command(tokens: list) -> bytes:
    """A command as the KESP array a replica reads from the slipstream."""
    return KESPEncoder.encode_command(tokens)


async def encode_reply(data) -> list:
    """The KESP bytes of a reply, as a list of parts, without holding the loop.

    Encoding a big reply is pure Python and takes about a second per million
    elements; done in one go on the event loop it stalls every client. Here the
    loop gets a turn after each ENCODE_CHUNK elements (a reply that size or
    smaller never yields). Nothing is written until the whole reply is encoded,
    so the caller sends every part in one go: a reply that cannot be encoded
    still becomes a single error line rather than half a frame, and a pub/sub
    push cannot land in the middle of one.
    """
    parts = []
    try:
        for piece in KESPEncoder.encode_chunks(data, ENCODE_CHUNK):
            if parts:
                await asyncio.sleep(0)
            parts.append(piece)
    except ReplyEncodeError as exc:
        return [KESPEncoder.failure_reply(exc)]
    return parts


async def drain_within_deadline(writer) -> None:
    """Waits for `writer` to drain, but not for ever (see WRITE_DEADLINE).

    A peer that is not reading cannot be sent anything more, not even an error
    line: the socket is aborted and ConnectionAbortedError raised.
    """
    transport = getattr(writer, "transport", None)
    queued = transport.get_write_buffer_size() if transport is not None else 0
    allowed = WRITE_DEADLINE + queued / WRITE_MIN_RATE
    try:
        await asyncio.wait_for(writer.drain(), allowed)
    except asyncio.TimeoutError:
        if transport is not None:
            transport.abort()
        raise ConnectionAbortedError("peer did not read in time")


# Internal replication command: "TXBATCH <n>" announces that the next n commands
# are one transaction, which a follower applies as a unit. It is not a client
# command, and the engine does not know it: only the follower's stream loop does.
TX_BATCH = "TXBATCH"


def transaction_frames(forwards: list) -> list:
    """What a master sends for the commands one EXEC produced. A single command
    is atomic on its own and goes out bare; two or more go behind a TXBATCH
    header, in the same write (see broadcast_to_replicas), so no other command can
    land between them on the wire."""
    if len(forwards) < 2:
        return forwards
    return [[TX_BATCH, str(len(forwards))], *forwards]


def enqueue_broadcast(forwards: list, label: str):
    """Puts already-resolved commands into every follower's buffer, with no await,
    so the order they are buffered in is the order the caller reached this line.
    Returns what drain_broadcast needs, or None when there is nothing to send."""
    if not forwards:
        return None
    console.print(
        f"[cyan]📡 [BROADCAST] Firing {label} down the slipstream to {len(connected_replicas)} followers...[/cyan]"
    )
    payload = b"".join(_encode_command(forwarded) for forwarded in forwards)
    dead_replicas = []
    waiting = []
    for rep_writer in connected_replicas:
        try:
            rep_writer.write(payload)
        except Exception:
            dead_replicas.append(rep_writer)
        else:
            waiting.append(rep_writer)
    return waiting, dead_replicas


async def drain_broadcast(queued) -> None:
    """Waits for the followers enqueue_broadcast wrote to; drops dead ones.

    The followers are waited for together, so a stalled one costs one write
    deadline, not one each, and is dropped when it runs out (it re-syncs if it
    comes back).
    """
    if queued is None:
        return
    waiting, dead_replicas = queued
    outcomes = await asyncio.gather(
        *(drain_within_deadline(rep_writer) for rep_writer in waiting),
        return_exceptions=True,
    )
    for rep_writer, outcome in zip(waiting, outcomes):
        if isinstance(outcome, Exception):
            dead_replicas.append(rep_writer)

    for dead in dead_replicas:
        if dead in connected_replicas:  # a concurrent broadcast may have removed it
            connected_replicas.remove(dead)
            console.print(
                "[yellow]⚠️ [REPLICATION] Follower disconnected. Removed from Live Stream.[/yellow]"
            )


async def broadcast_to_replicas(forwards: list, label: str) -> None:
    """Writes already-resolved commands to every follower; drops dead ones."""
    await drain_broadcast(enqueue_broadcast(forwards, label))


async def run_attempt(attempt, forward: bool):
    """One pass of a read, in a worker thread. Returns (reply, records).

    With `forward`, a pass that changed a group is applied and put in the
    followers' buffers under the SyncBarrier, like any other write, so a SYNC
    cannot fall between the two halves; and it reaches followers now, not when a
    blocked read finally ends. The barrier is held for this one pass, never for
    the wait between passes."""
    if not forward:
        return await asyncio.to_thread(attempt)
    async with replication_barrier.write():
        (reply, records), queued = await apply_in_order(
            attempt,
            lambda out: (
                global_handler.replication_payloads(["XREADGROUP"], None, out[1])
                if out[1]
                else []
            ),
            "XREADGROUP",
        )
    await drain_broadcast(queued)
    return reply, records


async def wait_for_stream_data(session, block_ms: int, attempt, forward: bool = False):
    """The wait loop shared by XREAD and XREADGROUP.

    `attempt` is a synchronous callable, run in a worker thread, that returns
    (reply, records). A reply of None means "nothing yet": park until a writer
    wakes us (or the deadline passes) and try again. Anything else, including
    an error string, is final. Returns (reply, []); reply is None on timeout.

    With `forward` (XREADGROUP), the group changes of every pass are sent on as
    they happen (see run_attempt): the first pass registers its consumer even
    when nothing is delivered, and that change has to reach replicas whatever
    happens next. Nothing is left to forward when the read ends."""
    loop = asyncio.get_running_loop()
    deadline = None if block_ms == 0 else loop.time() + block_ms / 1000

    while True:
        # Register BEFORE reading: a write landing between the read and the
        # wait would otherwise be missed until the next unrelated write.
        wake = loop.create_future()
        stream_waiters.add(wake)
        try:
            try:
                reply, _ = await run_attempt(attempt, forward)
            except (StreamError, TypeError) as e:
                return global_handler.error_reply(e), []
            if reply is not None:
                return reply, []
            while True:
                if session.reader.at_eof() or session.writer.is_closing():
                    # Nobody will get a reply; any group change a pass made has
                    # already gone to the followers.
                    raise ConnectionResetError("client left while blocked")
                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    return None, []  # timed out: nil, like Redis
                step = _BLOCK_POLL if remaining is None else min(_BLOCK_POLL, remaining)
                try:
                    await asyncio.wait_for(asyncio.shield(wake), step)
                    break  # woken by a writer: read again
                except asyncio.TimeoutError:
                    continue
        finally:
            stream_waiters.discard(wake)
            if not wake.done():
                wake.cancel()


async def blocking_xread(session, tokens: list):
    """Runs XREAD; honours BLOCK. Returns the reply object (None = nil)."""
    try:
        count, block_ms, streams = global_handler.parse_xread(tokens)
    except StreamError:
        block_ms = None
    if block_ms is None:  # no BLOCK (or a syntax error the handler will report)
        return await asyncio.to_thread(global_handler.execute, tokens, session.writer)

    try:
        # '$' must mean "last ID right now", fixed once, before we wait.
        resolved = global_handler.resolve_stream_ids(streams)
    except (StreamError, TypeError) as e:
        return global_handler.error_reply(e)

    reply, _ = await wait_for_stream_data(
        session,
        block_ms,
        lambda: (global_handler.xread_nonblocking(resolved, count), []),
    )
    return reply


async def blocking_xreadgroup(session, tokens: list):
    """Runs XREADGROUP; honours BLOCK. Returns (reply, []): the group changes
    the read made have already been sent to the followers, pass by pass (see
    run_attempt). A read of a consumer's own history (an explicit ID) always
    answers at once."""
    try:
        group, consumer, count, block_ms, noack, streams = (
            global_handler.parse_xreadgroup(tokens)
        )
    except StreamError:
        block_ms = None
    if block_ms is None:  # no BLOCK (or a syntax error the handler will report)
        reply, _ = await run_attempt(
            lambda: global_handler.execute_records(tokens, session.writer), True
        )
        return reply, []
    return await wait_for_stream_data(
        session,
        block_ms,
        lambda: global_handler.xreadgroup_attempt(
            group, consumer, streams, count, noack
        ),
        forward=True,
    )


async def housekeeping_loop(handler, interval: float = HOUSEKEEPING_INTERVAL):
    """Periodic store maintenance: expiry sweep, LRU/expiry reconciliation and
    container compaction. Runs in a worker thread, under the engine lock, like
    every other command, so it never overlaps a client write."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(handler.run_housekeeping)
        except Exception as e:  # maintenance must never take the server down
            console.print(f"[bold red]❌ Housekeeping pass failed: {repr(e)}[/bold red]")


class ReplicationError(Exception):
    """The leader's stream cannot be followed any further (bad data, or it stalled)."""


class LeaderStream:
    """Frames the bytes a leader sends: the baseline snapshot, then live commands.

    It applies the same budgets as a client connection: the parser's size limits,
    and the stall/frame deadlines of STALL_TIMEOUT and FRAME_DEADLINE for a command
    that is only partly received. A leader that is simply quiet between commands
    can idle for as long as it likes.
    """

    def __init__(self, reader):
        self.reader = reader
        self.buf = bytearray()
        self.pos = 0  # start of the bytes not yet handed out
        self.need = 0  # unread length at which the pending frame can progress
        self.frame_started = asyncio.get_running_loop().time()

    async def next_command(self, normalise: bool = True, first_wait=None):
        """The next complete command, or None if the leader closed between commands.

        first_wait bounds the wait for a frame's first byte (None: wait for ever).
        Raises ReplicationError for malformed data, a stall, or a cut-off frame.
        """
        loop = asyncio.get_running_loop()
        while True:
            unread = len(self.buf) - self.pos
            if unread and unread >= self.need:
                tokens, consumed, hint = CommandParser.parse_buffer(
                    self.buf, self.pos, normalise
                )
                if isinstance(tokens, ProtocolError):
                    # Cannot be resynchronised: nothing after this can be trusted.
                    raise ReplicationError(f"leader sent malformed data ({tokens.message})")
                self.pos += consumed
                self.need = hint
                if tokens:
                    self.frame_started = loop.time()  # what is left is a new frame
                    return tokens

            # Need more bytes. Drop what was handed out, once, before reading.
            if self.pos:
                del self.buf[: self.pos]
                self.pos = 0

            timeout = first_wait
            if self.buf:  # a half-received frame must keep moving
                allowed = FRAME_DEADLINE + max(self.need, len(self.buf)) / FRAME_MIN_RATE
                timeout = min(STALL_TIMEOUT, self.frame_started + allowed - loop.time())
                if timeout <= 0:
                    raise ReplicationError("timed out waiting for the rest of a command")
            try:
                chunk = await asyncio.wait_for(self.reader.read(65536), timeout)
            except asyncio.TimeoutError:
                raise ReplicationError("timed out waiting for the leader")
            if not chunk:
                if self.buf:
                    raise ReplicationError("leader closed the connection in the middle of a command")
                return None
            if not self.buf:
                self.frame_started = loop.time()
            self.buf.extend(chunk)


async def read_transaction(stream, header: list) -> list:
    """The commands of one TXBATCH frame, read in full before any is applied, so
    a leader that fails half-way leaves the store as it was.

    Raises ReplicationError for a bad header, a nested transaction, a leader that
    stalls or closes in the middle, or more payload than one command may carry.
    """
    try:
        count = int(header[1]) if len(header) == 2 else -1
    except ValueError:
        count = -1
    if not 2 <= count <= CommandParser.MAX_ARRAY_LENGTH:
        raise ReplicationError("leader sent a malformed transaction header")

    batch: list = []
    size = 0
    for _ in range(count):
        tokens = await stream.next_command(first_wait=STALL_TIMEOUT)
        if tokens is None:
            raise ReplicationError("leader closed the connection in the middle of a transaction")
        if tokens[0] == TX_BATCH:
            raise ReplicationError("leader sent a transaction inside a transaction")
        size += sum(len(to_bytes(t)) for t in tokens)
        if size > CommandParser.MAX_COMMAND_BYTES:
            raise ReplicationError("leader sent a transaction larger than a command may be")
        batch.append(tokens)
    return batch


def describe_command(tokens, width: int = 120) -> str:
    """A short, markup-safe one-line summary of a command for the log: values can
    be megabytes, and can contain text that looks like console markup."""
    parts = []
    used = 0
    for token in tokens:
        if used >= width:
            parts.append("...")
            break
        piece = token if len(token) <= 40 else f"{token[:40]}...(+{len(token) - 40} chars)"
        parts.append(piece)
        used += len(piece) + 1
    return escape(" ".join(parts))


# The stream we are following, if any. It lives here rather than in a throwaway
# local because the event loop only keeps weak references to tasks: a task nobody
# holds can be garbage-collected in mid-run, which silently ends replication.
replication_task = None


def start_replication(host: str, port: int):
    """Follows host:port, replacing any stream already running."""
    global replication_task
    stop_replication()
    replication_task = asyncio.create_task(init_replication_stream(host, port))
    return replication_task


def stop_replication() -> None:
    """Stops following the current leader, if there is one (its socket is closed)."""
    global replication_task
    if replication_task is not None and not replication_task.done():
        replication_task.cancel()
    replication_task = None


async def init_replication_stream(host: str, port: int):
    """
    Opens a permanent TCP socket to the Leader engine,
    downloads the baseline,
    and processes live replication streams.
    """
    global server_role, server_host, master_port

    writer = None
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

        # 3. The baseline: the first frame is the snapshot, as JSON, sent as one
        # string. It is data from its first token, not a command (normalise=False),
        # and the leader gets FRAME_DEADLINE to start sending it.
        stream = LeaderStream(reader)
        tokens = await stream.next_command(normalise=False, first_wait=FRAME_DEADLINE)
        if tokens is None:
            raise ConnectionError("Leader severed connection before snapshot arrived.")
        if len(tokens) != 1:
            raise ReplicationError("the leader's first frame is not a snapshot")
        try:
            parsed_data = await asyncio.to_thread(json.loads, tokens[0])
        except ValueError as e:
            raise ReplicationError(f"the snapshot is not valid JSON ({e})")
        if not isinstance(parsed_data, dict):
            raise ReplicationError("the snapshot is not a JSON object")
        del tokens

        # Restore using the custom loader, which rebuilds the SkipLists.
        global_store.restore_snapshot_state(parsed_data)
        console.print(
            f"[bold green]💾 [REPLICATION] Cold Boot Successful! Restored {len(global_store._data)} keys from Leader.[/bold green]"
        )
        del parsed_data

        # 4. 🚀 PHASE 3 LIVE STREAM: Stay locked in the slipstream forever catching live writes
        console.print(
            "[bold blue]⚡ [REPLICATION] Entering Live Stream Mode. Awaiting commands...[/bold blue]"
        )

        while True:
            tokens = await stream.next_command()  # bytes that came with the snapshot first
            if tokens is None:
                console.print(
                    "[bold red]⚠️ [REPLICATION] Leader connection lost![/bold red]"
                )
                break

            if tokens[0] == TX_BATCH:
                batch = await read_transaction(stream, tokens)
                try:
                    await asyncio.to_thread(global_handler.execute_batch, batch, None)
                except Exception as e:
                    console.print(
                        f"[bold red]❌ [REPLICATION Live] Could not apply a transaction of {len(batch)} commands: {escape(repr(e))}[/bold red]"
                    )
                    continue
                notify_stream_waiters()
                console.print(
                    f"[magenta]🔄 [REPLICATION Live] Executed a transaction of {len(batch)} commands[/magenta]"
                )
                continue

            try:
                # Execute the mirrored write locally using your handler
                await asyncio.to_thread(global_handler.execute, tokens, None)
            except Exception as e:
                # Retrying the same command can never succeed, and everything
                # behind it is waiting: log it and carry on.
                console.print(
                    f"[bold red]❌ [REPLICATION Live] Could not apply {describe_command(tokens)}: {escape(repr(e))}[/bold red]"
                )
                continue
            notify_stream_waiters()
            console.print(
                f"[magenta]🔄 [REPLICATION Live] Executed: {describe_command(tokens)}[/magenta]"
            )

    except ReplicationError as e:
        # The leader sent bad data or stalled. We cannot trust or resync the
        # stream, so stop following. We stay a (read-only) replica: a follower
        # never promotes itself because its leader misbehaved.
        console.print(
            f"[bold red]❌ [REPLICATION] Dropping the leader: {escape(str(e))}[/bold red]"
        )
    except Exception as e:
        console.print(f"[bold red]❌ [REPLICATION] Sync Engine crashed: {e}[/bold red]")
        server_role = "master"  # Fall back to master role if cluster drivetrain breaks
    finally:
        if writer is not None:
            writer.close()  # also when cancelled: don't leave the leader's socket open


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
        self.tx_bytes = 0  # argument bytes held in tx_queue
        self.tx_overflowed = False  # the queue limit was hit: EXEC will abort
        self.tx_readonly = False  # a write was refused at queue time: EXEC will abort

        # Connection-specific watch state {key: expected_version}
        self.watched_keys = {}

        # Network Dispatch Table
        self.tx_router = {
            "MULTI": self.handle_multi,
            "EXEC": self.handle_exec,
            "DISCARD": self.handle_discard,
            "WATCH": self.handle_watch,
            "UNWATCH": self.handle_unwatch,
        }

    async def send(self, data: bytes):
        """
        Asynchronously flushes the bytes to the network sockets
        """
        self.writer.write(data)
        await drain_within_deadline(self.writer)

    async def send_parts(self, parts: list):
        """send() for a reply held as parts: all written at once, with no await
        in between, so nothing else is written to this connection mid-reply."""
        self.writer.writelines(parts)
        await drain_within_deadline(self.writer)

    # -------------------------------------
    # ASYNC TRANSACTION ROUTUING (Which will be used in dispatch table)
    # -------------------------------------

    async def handle_watch(self, tokens: list):
        if self.in_transaction:
            await self.send(b"-ERR WATCH inside MULTI is not allowed\r\n")
            return
        if len(tokens) < 2:
            await self.send(b"-ERR wrong number of arguments for 'watch'\r\n")
            return

        # Lock in the current version of the requested keys
        for key in tokens[1:]:
            current_ver = getattr(global_store, "_versions", {}).get(key, 0)
            self.watched_keys[key] = current_ver
        await self.send(b"+OK\r\n")

    async def handle_unwatch(self, tokens: list):
        self.watched_keys.clear()
        await self.send(b"+OK\r\n")

    def _reset_queue(self):
        self.tx_queue = []
        self.tx_bytes = 0
        self.tx_overflowed = False
        self.tx_readonly = False

    async def queue_command(self, tokens: list):
        """Adds a command to the open MULTI block, within the queue limits.

        Past a limit the queue is dropped and the block is poisoned: the client
        is told, still-queued commands are refused, and EXEC aborts. The block is
        not simply ended, or the rest of what the client meant to be atomic would
        run one command at a time.
        """
        # A follower takes no writes, in a block or out of it. The write is refused
        # now and the block poisoned, so EXEC cannot run the rest of it half-done.
        if server_role == "replica" and tokens[0].upper() in global_handler.WRITE_COMMANDS:
            self.tx_readonly = True
            await self.send(b"-EREADONLY You can't write against a read-only replica.\n")
            return
        if not self.tx_overflowed:
            size = sum(len(to_bytes(t)) for t in tokens)
            if (
                len(self.tx_queue) >= MAX_MULTI_COMMANDS
                or self.tx_bytes + size > MAX_MULTI_BYTES
            ):
                self.tx_queue = []  # free the memory now
                self.tx_bytes = 0
                self.tx_overflowed = True
            else:
                self.tx_queue.append(tokens)
                self.tx_bytes += size
                await self.send(b"+OK\n")
                return
        await self.send(
            b"-ERR MULTI queue limit reached; EXEC will abort the transaction\r\n"
        )

    async def handle_multi(self, tokens: list):
        if self.in_transaction:
            await self.send(b"-ERR MULTI calls are not nested\r\n")
        else:
            self.in_transaction = True
            self._reset_queue()
            await self.send(b"+OK\r\n")

    async def handle_discard(self, tokens: list):
        if not getattr(self, "in_transaction", False):
            await self.send(b"-ERR DISCARD without MULTI\r\n")
            return

        self.in_transaction = False
        self._reset_queue()
        self.watched_keys.clear()  # Discarding also clears watched keys
        await self.send(b"+OK\r\n")

    async def handle_exec(self, tokens: list):
        if not getattr(self, "in_transaction", False):
            await self.send(b"-ERR EXEC without MULTI\r\n")
            return

        #  OPTIMISTIC LOCK CHECK
        # Verify no watched keys have been modified by another client
        transaction_aborted = False
        for key, expected_version in self.watched_keys.items():
            current_version = getattr(global_store, "_versions", {}).get(key, 0)
            if current_version != expected_version:
                transaction_aborted = True
                break

        # Drop out of transaction mode and clear locks
        self.in_transaction = False
        self.watched_keys.clear()

        # The queue overflowed: some commands were never kept, so running the rest
        # would not be the transaction the client asked for.
        if self.tx_overflowed:
            self._reset_queue()
            await self.send(
                b"-EXECABORT Transaction discarded: the MULTI queue limit was exceeded\r\n"
            )
            return

        # A write was refused at queue time, or this node became a follower after
        # it was queued: either way the block must not run on a read-only replica.
        if self.tx_readonly or (
            server_role == "replica"
            and any(t[0].upper() in global_handler.WRITE_COMMANDS for t in self.tx_queue)
        ):
            self._reset_queue()
            await self.send(
                b"-EXECABORT Transaction discarded: writes are refused on a read-only replica\r\n"
            )
            return

        # If race condition detected, abort safely
        if transaction_aborted:
            self._reset_queue()
            # kedis protocol returns a Null Array for aborted transactions
            await self.send(b"N\n")
            return

        # Handle empty queues
        if not self.tx_queue:
            await self.send(b"A0\n")
            return

        # Execute the payload as one unit: the engine lock is held across the whole
        # block, so nothing else runs in between and followers see the same order.
        # The block is held in the followers' buffers before any SYNC can snapshot.
        async with replication_barrier.write():
            (results, forwards), queued = await apply_in_order(
                lambda: global_handler.execute_batch(self.tx_queue, self.writer),
                lambda out: transaction_frames(out[1]),
                "EXEC",
            )

        self._reset_queue()
        notify_stream_waiters()

        # Format and send the array of results back to the client
        parts = [f"A{len(results)}\n".encode("utf-8")]
        for res in results:
            parts.extend(await encode_reply(res))

        await self.send_parts(parts)

        # 📡 What the block changed goes to followers as one transaction.
        await drain_broadcast(queued)

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
        protocol_failed = False
        loop = asyncio.get_running_loop()
        need = 0  # buffer length at which the pending command can progress
        frame_started = loop.time()  # when the pending (half-received) command began
        executed = 0
        timed_out = b"EProtocol error: timed out waiting for the rest of a command\n"

        while True:
            try:
                # A half-received command must keep moving (see STALL_TIMEOUT).
                timeout = None
                if intake_buffer:
                    allowed = FRAME_DEADLINE + max(need, len(intake_buffer)) / FRAME_MIN_RATE
                    timeout = min(STALL_TIMEOUT, frame_started + allowed - loop.time())
                    if timeout <= 0:
                        await self.send(timed_out)
                        break

                # Widenning the intake pipe to 64KB per read
                try:
                    chunk = await asyncio.wait_for(self.reader.read(65536), timeout)
                except asyncio.TimeoutError:
                    await self.send(timed_out)
                    break
                if not chunk:
                    break

                # pool the new bytes into the intake buffer
                if not intake_buffer:
                    frame_started = loop.time()
                intake_buffer.extend(chunk)

                # A big value is coming and has not all arrived: don't re-parse
                # the buffer after every chunk, just keep reading.
                if len(intake_buffer) < need:
                    continue

                # Inner loop to process pipelined commands within the buffer.
                # pos walks the buffer; consumed bytes are dropped once at the end.
                pos = 0
                need = 0
                while pos < len(intake_buffer):
                    try:
                        tokens, consumed, hint = CommandParser.parse_buffer(
                            intake_buffer, pos
                        )
                    except Exception:
                        break  # Wait for next TCP packet

                    if isinstance(tokens, ProtocolError):
                        # The stream cannot be resynchronised once a frame is
                        # malformed: tell the client why, then drop it.
                        clean_err = tokens.message.replace("-ERR", "").strip()
                        await self.send(f"E{clean_err}\n".encode("utf-8"))
                        intake_buffer.clear()
                        pos = 0
                        protocol_failed = True
                        break

                    if not tokens:
                        # Nothing runnable: only blank frames (dropped below), then
                        # a command that has not all arrived.
                        pos += consumed
                        need = hint
                        break

                    executed += 1
                    if executed % YIELD_EVERY == 0:
                        await asyncio.sleep(0)  # let other clients run

                    # Execute the fully assembled command
                    cmd = tokens[0].upper()

                    # 🛡️ REPLICATION INTERCEPTOR
                    if cmd == "REPLICAOF" and len(tokens) >= 3:
                        r_host = tokens[1]
                        r_port = tokens[2]

                        if r_host.upper() == "NO" and r_port.upper() == "ONE":
                            global server_role
                            stop_replication()
                            server_role = "master"
                            await self.send(b"+OK Engine promoted to Leader\r\n")
                        else:
                            start_replication(r_host, int(r_port))
                            await self.send(b"+OK Replica handshake initiated\r\n")

                        # 🚀 FIX: Slicing
                        pos += consumed
                        continue

                    if cmd == "SYNC":
                        if server_role == "master":
                            console.print(
                                "[cyan]📦 [REPLICATION] Follower requested baseline. Dumping RAM...[/cyan]"
                            )

                            # The snapshot is taken under the engine lock and the follower is
                            # registered in the same step, with no write in between: every
                            # write is then in the snapshot or in the stream, once. The slow
                            # part, sending the snapshot, happens after, outside the barrier.
                            async with replication_barrier.snapshot():
                                kesp_payload = await asyncio.to_thread(
                                    global_handler.snapshot_bytes
                                )
                                self.writer.write(kesp_payload)
                                connected_replicas.append(self.writer)
                            try:
                                await drain_within_deadline(self.writer)
                            except BaseException:
                                if self.writer in connected_replicas:
                                    connected_replicas.remove(self.writer)
                                raise
                            console.print(
                                "[bold green]✅ [REPLICATION] Baseline snapshot transmitted![/bold green]"
                            )
                            console.print(
                                f"[bold magenta]📡 [REPLICATION] Follower locked into Live Stream. Total replicas: {len(connected_replicas)}[/bold magenta]"
                            )
                        else:
                            await self.send(
                                b"-ERR I'm a follower, I cannot sync you!!\n"
                            )

                        # 🚀 FIX: Slicing
                        pos += consumed
                        continue

                    if cmd in self.tx_router:
                        await self.tx_router[cmd](tokens)

                    elif self.in_transaction:
                        await self.queue_command(tokens)

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
                            pos += consumed
                            continue

                        queued = None
                        forwards = None
                        if cmd == "XREAD":
                            response, records = await blocking_xread(self, tokens), []
                        elif cmd == "XREADGROUP":
                            response, records = await blocking_xreadgroup(self, tokens)
                        elif cmd in write_commands:
                            # Applied and buffered for followers under the barrier, so a
                            # SYNC cannot snapshot between the two (see SyncBarrier).
                            async with replication_barrier.write():
                                (response, records), queued = await apply_in_order(
                                    lambda: global_handler.execute_records(
                                        tokens, self.writer
                                    ),
                                    lambda out: global_handler.replication_payloads(
                                        tokens, out[0], out[1]
                                    ),
                                    cmd,
                                )
                                forwards = []  # handled: see apply_in_order
                        else:
                            response, records = await asyncio.to_thread(
                                global_handler.execute_records, tokens, self.writer
                            )
                        # 📡 PHASE 3: LIVE COMMAND FORWARDING
                        # XADD goes out with the ID this master assigned; group
                        # commands go out as the resolved records they produced
                        # (nothing at all if they changed nothing). Buffered for
                        # followers before the client hears back, so a reply means
                        # "already on its way to every follower there is".
                        if forwards is None:  # XREAD / XREADGROUP: not under the barrier
                            forwards = (
                                global_handler.replication_payloads(tokens, response, records)
                                if server_role == "master" and cmd in write_commands
                                else []
                            )
                            queued = enqueue_broadcast(forwards, cmd)

                        await self.send_parts(await encode_reply(response))

                        if cmd in write_commands:
                            notify_stream_waiters()

                        # 📡 PHASE 3: wait for the followers the change was buffered for
                        await drain_broadcast(queued)

                    # 🚀 FIX: Slicing for standard commands
                    pos += consumed

                del intake_buffer[:pos]
                if pos:
                    frame_started = loop.time()  # what is left is a new command
                if protocol_failed:
                    break  # fall through to the close below

            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                break  # client vanished; fall through to cleanup
            except Exception as e:
                console.print(
                    f"[bold red]❌ [REPLICATION Live] Stream Crash: {repr(e)}[/bold red]"
                )
                intake_buffer.clear()
                break

        console.print(f"[yellow]⚠️ Client Disconnected:[/yellow] {client_id}")
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            # The peer reset the connection (e.g. killed client, closed with
            # unread data). wait_closed() re-raises that stored error, but
            # there is nothing left to clean up, so it is not an error here.
            pass


async def handle_connection(reader, writer):
    """
    Spawns a new isolated session object for every incoming TCP connection.
    """
    global _active_clients
    if _active_clients >= MAX_CLIENTS:
        # Refuse politely and free the socket at once; don't build a session.
        try:
            writer.write(b"Emax number of clients reached\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()
        return

    global_handler.event_loop = asyncio.get_running_loop()  # pub/sub pushes are queued onto it
    _active_clients += 1
    try:
        session = AsyncKedisSession(reader, writer)
        await session.run()
    finally:
        _active_clients -= 1


async def main():
    UI.print_banner(subtitle="DATABASE SERVER")
    UI.print_server_ready(HOST, PORT, server_role)

    server = await asyncio.start_server(handle_connection, HOST, PORT)
    asyncio.create_task(loop_latency_monitor(global_store))
    housekeeping_task = asyncio.create_task(housekeeping_loop(global_handler))  # noqa: F841

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


def run(argv=None):
    """Starts the server. Command-line flags (sys.argv[1:], or `argv`) override the
    limits first; see settings.py. Embedding code that sets the limits itself
    can pass argv=[] to keep its own command line out of it."""
    from .settings import parse_and_apply

    applied = parse_and_apply(argv)
    if applied:
        console.print(
            "[cyan]⚙️  Settings:[/cyan] "
            + "  ".join(f"--{setting.flag} {setting.show(value)}" for setting, value in applied)
        )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
