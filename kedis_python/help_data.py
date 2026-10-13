"""Detailed per-command help: `HELP <command>` and `HELP <topic>`.

Pure data plus rendering, so it can be tested without a terminal. Adding help
for a new command means adding one CommandHelp entry and listing its name in a
family; nothing else changes. Keep every entry in step with the code and with
docs/commands.md: tests/test_help_data.py checks that every command has an
entry, that syntax lines agree with the docs headings, and that the examples
really run.
"""

from dataclasses import dataclass

from rich.markup import escape


@dataclass(frozen=True)
class CommandHelp:
    name: str
    syntax: str  # the main form
    summary: str  # one line, shown in the topic listing
    details: str
    returns: str
    examples: tuple
    notes: tuple = ()
    variants: tuple = ()  # further forms, for commands with subcommands

    @property
    def forms(self) -> tuple:
        return (self.syntax, *self.variants)


def _c(name, syntax, summary, details, returns, examples, notes=(), variants=()):
    return CommandHelp(
        name, syntax, summary, details, returns, tuple(examples), tuple(notes), tuple(variants)
    )


_ENTRIES = [
    # ---------------------------------------------------------------- strings
    _c(
        "SET", "SET key value",
        "Store a string value under a key",
        "Overwrites whatever the key held, whatever its type. Takes exactly one value and no "
        "expiry options: use EXPIRE for a lifetime.",
        "+OK",
        ["SET session_123 active"],
    ),
    _c(
        "GET", "GET key",
        "Read a string value",
        "Works on strings only: a key holding another type returns a WRONGTYPE error.",
        "the value, or (nil) if the key doesn't exist",
        ["GET session_123"],
    ),
    # --------------------------------------------------------------- keyspace
    _c(
        "DEL", "DEL key",
        "Delete a key",
        "Removes the key whatever type it holds. One key per call.",
        "(integer) 1 if the key was deleted, 0 if it didn't exist",
        ["DEL session_123"],
    ),
    _c(
        "EXISTS", "EXISTS key",
        "Check whether a key exists",
        "A key that has expired counts as missing. One key per call.",
        "(integer) 1 if it exists, 0 if not",
        ["EXISTS session_123"],
    ),
    _c(
        "TYPE", "TYPE key",
        "Get the type of a key's value",
        "Bitmaps and HyperLogLogs are strings underneath, so they report string, as in Redis.",
        "the type name (string, hash, zset, stream, ...), or (nil) if the key doesn't exist",
        ["TYPE tasks"],
    ),
    _c(
        "KEYS", "KEYS",
        "List every key with its type, TTL and size",
        "There is no pattern argument: it always lists the whole keyspace. Each line reads "
        "'key | type | ttl | length', where a ttl of -1 means no expiry.",
        "an array of strings (empty if there are no keys)",
        ["KEYS"],
    ),
    _c(
        "EXPIRE", "EXPIRE key seconds",
        "Give a key a time to live",
        "The key is evicted automatically once the time is up. Works on every key type. A time "
        "of zero or less deletes the key immediately.",
        "(integer) 1 if the timeout was set, 0 if the key doesn't exist",
        ["EXPIRE temp_cache 300"],
    ),
    _c(
        "TTL", "TTL key",
        "Get the remaining time to live",
        "Counts down in whole seconds.",
        "(integer) seconds left, -1 if the key has no expiry, -2 if it doesn't exist",
        ["TTL temp_cache"],
    ),
    _c(
        "FLUSHALL", "FLUSHALL",
        "Delete every key",
        "Removes all keys of every type. This cannot be undone.",
        "+OK",
        ["FLUSHALL"],
    ),
    # ------------------------------------------------------------------ lists
    _c(
        "LPUSH", "LPUSH key value [value ...]",
        "Push values onto the head (left)",
        "Creates the list if it doesn't exist. Several values are pushed one at a time, so the "
        "last value ends up first.",
        "(integer) the length of the list after the push",
        ["LPUSH tasks urgent"],
    ),
    _c(
        "RPUSH", "RPUSH key value [value ...]",
        "Push values onto the tail (right)",
        "Creates the list if it doesn't exist. Values keep the order you give them.",
        "(integer) the length of the list after the push",
        ["RPUSH tasks later"],
    ),
    _c(
        "LPOP", "LPOP key",
        "Remove and return the first element",
        "Takes the element at the head. There is no COUNT argument. A list that loses its last "
        "element disappears.",
        "the element, or (nil) if the key doesn't exist",
        ["LPOP tasks"],
    ),
    _c(
        "RPOP", "RPOP key",
        "Remove and return the last element",
        "Takes the element at the tail. There is no COUNT argument. A list that loses its last "
        "element disappears.",
        "the element, or (nil) if the key doesn't exist",
        ["RPOP tasks"],
    ),
    _c(
        "LRANGE", "LRANGE key start stop",
        "Get a slice of a list by index",
        "Index 0 is the head and both ends are inclusive. Negative indexes count from the tail "
        "(-1 is the last element), so 0 -1 is the whole list. A start before the head and a stop "
        "past the end are clamped; an empty or inverted range gives an empty array.",
        "an array of elements (empty if the key doesn't exist)",
        ["LRANGE tasks 0 -1", "LRANGE tasks 0 2", "LRANGE tasks -2 -1"],
    ),
    # ------------------------------------------------------------------- sets
    _c(
        "SADD", "SADD key member [member ...]",
        "Add members to a set",
        "Creates the set if needed. Members already present are ignored.",
        "(integer) how many members were newly added",
        ["SADD skills python systems"],
    ),
    _c(
        "SREM", "SREM key member [member ...]",
        "Remove members from a set",
        "Missing members are ignored. A set that loses its last member disappears.",
        "(integer) how many members were removed",
        ["SREM skills python"],
    ),
    _c(
        "SMEMBERS", "SMEMBERS key",
        "List every member of a set",
        "Members come back in no particular order.",
        "an array of members (empty if the key doesn't exist)",
        ["SMEMBERS skills"],
    ),
    # ----------------------------------------------------------------- hashes
    _c(
        "HSET", "HSET key field value",
        "Set one field of a hash",
        "Everything after the field name is the value, joined with single spaces, so "
        "'HSET user bio likes databases' stores 'likes databases'. One field per call.",
        "(integer) 1 if the field is new, 0 if it was overwritten",
        ["HSET user name karthik"],
    ),
    _c(
        "HGET", "HGET key field",
        "Read one field of a hash",
        "Looks up a single field.",
        "the value, or (nil) if the key or field doesn't exist",
        ["HGET user name"],
    ),
    _c(
        "HGETALL", "HGETALL key",
        "Read every field and value of a hash",
        "Fields come back in the order they were first added.",
        "a flat array: field, value, field, value, ... (empty if the key doesn't exist)",
        ["HGETALL user"],
    ),
    # ------------------------------------------------------------ sorted sets
    _c(
        "ZADD", "ZADD key score member [score member ...]",
        "Add members to a sorted set, or update scores",
        "Members with equal scores are ordered by name. Every score is checked "
        "before anything is applied, so one bad score (nan is rejected; inf and -inf are "
        "allowed) means nothing is added or changed. If a member repeats, the last score wins.",
        "(integer) how many members were newly added (score updates aren't counted)",
        ["ZADD leaderboard 100 karthik 250 ada"],
    ),
    _c(
        "ZRANGE", "ZRANGE key start stop [WITHSCORES]",
        "Get members of a sorted set by rank",
        "Lowest score first. Rank 0 is the lowest, negative ranks count from the end (-1 is the "
        "highest) and a stop past the end is clamped. WITHSCORES (any case) adds each member's "
        "score.",
        "an array of members, or alternating member and score with WITHSCORES (empty if the key "
        "doesn't exist)",
        ["ZRANGE leaderboard 0 -1", "ZRANGE leaderboard 0 -1 WITHSCORES"],
    ),
    # ---------------------------------------------------------------- bitmaps
    _c(
        "SETBIT", "SETBIT key offset bit",
        "Set one bit of a string",
        "Bit 0 is the most significant bit of the first byte, as in Redis. The string grows with "
        "zero bytes when the offset is past its end. Bitmaps are ordinary strings, so GET, SET, "
        "DEL and EXPIRE work on them too.",
        "(integer) the bit's previous value",
        ["SETBIT flags 7 1"],
    ),
    _c(
        "GETBIT", "GETBIT key offset",
        "Read one bit of a string",
        "Reads past the end of the string, or from a missing key, give 0.",
        "(integer) the bit",
        ["GETBIT flags 7"],
    ),
    _c(
        "BITCOUNT", "BITCOUNT key [start end]",
        "Count the bits set to 1",
        "With a range, start and end are byte positions (inclusive), not bit offsets, and "
        "negative values count from the end. Give both or neither.",
        "(integer) the number of set bits, 0 if the key doesn't exist",
        ["BITCOUNT flags", "BITCOUNT flags 0 0"],
    ),
    _c(
        "BITOP", "BITOP AND|OR|XOR|NOT destkey key [key ...]",
        "Combine strings bit by bit",
        "Stores the result in destkey. Shorter sources are padded with zero bytes, and NOT takes "
        "exactly one source key. An empty result removes destkey.",
        "(integer) the length of the result in bytes",
        ["BITOP OR either a b"],
    ),
    # ------------------------------------------------------------ hyperloglog
    _c(
        "PFADD", "PFADD key [element ...]",
        "Add elements to a HyperLogLog counter",
        "Creates the counter if needed (with no elements it just creates an empty one). It "
        "counts distinct elements in about 16 KiB however many you add, with roughly 0.81% "
        "error.",
        "(integer) 1 if the estimate changed or the key was created, 0 otherwise",
        ["PFADD visitors alice bob carol"],
    ),
    _c(
        "PFCOUNT", "PFCOUNT key [key ...]",
        "Estimate the number of distinct elements",
        "With several keys it returns the size of their union without changing any of them. "
        "Missing keys count as empty.",
        "(integer) the estimated count",
        ["PFCOUNT visitors", "PFCOUNT visitors other"],
    ),
    _c(
        "PFMERGE", "PFMERGE destkey [sourcekey ...]",
        "Merge HyperLogLog counters into one",
        "The sources are merged into destkey, which is created if needed and keeps what it "
        "already had.",
        "+OK",
        ["PFMERGE all visitors other"],
    ),
    # ---------------------------------------------------------------- streams
    _c(
        "XADD",
        "XADD key [NOMKSTREAM] [MAXLEN|MINID [=|~] threshold [LIMIT count]] id|* field value [field value ...]",
        "Append an entry to a stream",
        "'*' lets the engine pick the ID, and 'ms-*' picks only the sequence. IDs only ever "
        "increase. Trimming runs right after the append and is always exact: '~' and LIMIT are "
        "accepted but ignored.",
        "the new entry's ID, or (nil) if NOMKSTREAM was given and the key doesn't exist",
        ["XADD orders * item book qty 2"],
    ),
    _c(
        "XLEN", "XLEN key",
        "Count the entries in a stream",
        "Counts what is currently stored, not everything ever added.",
        "(integer) the number of entries, 0 if the key doesn't exist",
        ["XLEN orders"],
    ),
    _c(
        "XRANGE", "XRANGE key start end [COUNT n]",
        "Read entries between two IDs, oldest first",
        "'-' and '+' are the smallest and largest possible IDs. A leading '(' makes a bound "
        "exclusive.",
        "an array of [id, [field, value, ...]]",
        ["XRANGE orders - + COUNT 10"],
    ),
    _c(
        "XREVRANGE", "XREVRANGE key end start [COUNT n]",
        "Read entries between two IDs, newest first",
        "Like XRANGE, but the bounds are swapped: the end comes first.",
        "an array of [id, [field, value, ...]]",
        ["XREVRANGE orders + - COUNT 1"],
    ),
    _c(
        "XREAD", "XREAD [COUNT n] [BLOCK ms] STREAMS key [key ...] id [id ...]",
        "Read entries newer than a given ID",
        "Returns entries with an ID greater than the one given, per stream. '$' means 'only "
        "entries added from now'. With BLOCK it waits up to ms milliseconds (0 = forever) for "
        "something new.",
        "an array of [key, entries] for streams that have something, or (nil)",
        ["XREAD COUNT 10 BLOCK 5000 STREAMS orders $"],
        notes=["BLOCK only waits for clients of a running server. Inside MULTI, and in "
               "standalone mode, it answers at once."],
    ),
    _c(
        "XDEL", "XDEL key id [id ...]",
        "Delete entries from a stream",
        "The stream's top ID is kept, so IDs are never reused.",
        "(integer) how many of the IDs existed",
        ["XDEL orders 1-0 2-0"],
    ),
    _c(
        "XTRIM", "XTRIM key MAXLEN|MINID [=|~] threshold [LIMIT count]",
        "Trim the oldest entries of a stream",
        "MAXLEN keeps at most threshold entries; MINID drops everything below an ID. Trimming "
        "is always exact: '~' and LIMIT are accepted but ignored.",
        "(integer) how many entries were removed",
        ["XTRIM orders MAXLEN 1000"],
    ),
    # --------------------------------------------------------- consumer groups
    _c(
        "XGROUP", "XGROUP CREATE key group id|$ [MKSTREAM]",
        "Create and manage consumer groups",
        "CREATE starts the group's cursor at id: 0 delivers the whole backlog, $ only new "
        "entries; MKSTREAM creates an empty stream if the key doesn't exist. SETID moves the "
        "cursor of an existing group. DESTROY removes a group. CREATECONSUMER adds a consumer. "
        "DELCONSUMER removes one and drops its pending entries.",
        "CREATE and SETID: +OK (CREATE gives BUSYGROUP if the group exists). DESTROY: (integer) "
        "1 or 0. CREATECONSUMER: (integer) 1 if created, else 0. DELCONSUMER: (integer) how "
        "many pending entries it had",
        ["XGROUP CREATE orders billing $", "XGROUP SETID orders shipping 0",
         "XGROUP CREATECONSUMER orders shipping w1", "XGROUP DELCONSUMER orders shipping w1",
         "XGROUP DESTROY orders shipping"],
        variants=["XGROUP SETID key group id|$", "XGROUP DESTROY key group",
                  "XGROUP CREATECONSUMER key group consumer",
                  "XGROUP DELCONSUMER key group consumer"],
        notes=["ENTRIESREAD is not supported."],
    ),
    _c(
        "XREADGROUP",
        "XREADGROUP GROUP group consumer [COUNT n] [BLOCK ms] [NOACK] STREAMS key [key ...] id [id ...]",
        "Read from a stream as a member of a group",
        "The ID '>' delivers entries nobody in the group has seen: each goes to one consumer and "
        "becomes pending until XACK. Any other ID returns that consumer's own pending entries "
        "after it. NOACK delivers without making entries pending. A consumer is created the "
        "first time it is named.",
        "an array of [key, entries], or (nil) when nothing is new. A pending entry that has "
        "since been deleted shows as [id, (nil)]",
        ["XREADGROUP GROUP shipping w1 COUNT 10 BLOCK 5000 STREAMS orders >"],
        notes=["BLOCK works with '>' only. History reads never wait."],
    ),
    _c(
        "XACK", "XACK key group id [id ...]",
        "Acknowledge entries as processed",
        "Removes the entries from the group's pending list.",
        "(integer) how many of the IDs were actually pending",
        ["XACK orders shipping 1-0"],
    ),
    _c(
        "XPENDING", "XPENDING key group [[IDLE min-idle-time] start end count [consumer]]",
        "Inspect a group's pending entries",
        "Without a range it summarises the pending list. With a range it lists the entries, "
        "optionally only those held by one consumer or idle for at least min-idle-time ms.",
        "summary: [total, smallest-id, largest-id, [[consumer, count], ...]]. Range: an array "
        "of [id, consumer, idle-ms, delivery-count]",
        ["XPENDING orders shipping", "XPENDING orders shipping - + 10"],
    ),
    _c(
        "XINFO", "XINFO GROUPS key",
        "Describe a stream's groups and consumers",
        "GROUPS lists each group's name, consumers, pending count and last-delivered ID. "
        "CONSUMERS lists each consumer's name, pending count and idle time in milliseconds.",
        "one entry per group or consumer, as name/value pairs",
        ["XINFO GROUPS orders", "XINFO CONSUMERS orders shipping"],
        variants=["XINFO CONSUMERS key group"],
        notes=["entries-read and lag are always (nil). XINFO STREAM is not implemented."],
    ),
    # ----------------------------------------------------------- transactions
    _c(
        "MULTI", "MULTI",
        "Start a transaction",
        "Commands sent afterwards are queued instead of run, and each is acknowledged with +OK. "
        "EXEC runs them as one block; DISCARD throws them away. MULTI can't be nested.",
        "+OK",
        ["MULTI", "SET balance 90", "SET last_paid today", "EXEC"],
    ),
    _c(
        "EXEC", "EXEC",
        "Run every queued command as one atomic block",
        "If a key you WATCHed was changed by someone else since you watched it, nothing runs. "
        "An error if there is no MULTI.",
        "an array with each command's result, or (nil) if a watched key changed",
        ["EXEC"],
    ),
    _c(
        "DISCARD", "DISCARD",
        "Throw away the queued commands",
        "Leaves transaction mode and also clears any WATCHed keys. An error if there is no "
        "MULTI.",
        "+OK",
        ["DISCARD"],
    ),
    _c(
        "WATCH", "WATCH key [key ...]",
        "Watch keys for changes before a transaction",
        "If another client modifies a watched key before you EXEC, the transaction aborts "
        "instead of running (optimistic locking). Not allowed inside MULTI. EXEC, DISCARD and "
        "UNWATCH clear the watches.",
        "+OK",
        ["WATCH balance", "MULTI", "SET balance 90", "EXEC"],
    ),
    _c(
        "UNWATCH", "UNWATCH",
        "Forget every watched key",
        "Lets a transaction run without version checks.",
        "+OK",
        ["UNWATCH"],
    ),
    # ----------------------------------------------------------------- pub/sub
    _c(
        "SUBSCRIBE", "SUBSCRIBE channel",
        "Listen for messages on a channel",
        "Takes one channel per call and needs a network connection: in standalone mode it "
        "returns an error. Messages are never stored. They are pushed to whoever is subscribed "
        "at that moment and never touch the AOF.",
        "a confirmation array: subscribe, channel, 1",
        ["SUBSCRIBE news"],
    ),
    _c(
        "PUBLISH", "PUBLISH channel message",
        "Send a message to a channel",
        "Everything after the channel is the message, joined with single spaces. It is "
        "delivered to every current subscriber at once and nothing is stored.",
        "(integer) 0 when nobody is subscribed to the channel",
        ["PUBLISH news hello world"],
        # Known bug, pinned by tests/test_help_data.py: remove this note when
        # PUBLISH is fixed to return the number of receivers.
        notes=["Known issue: when the channel has subscribers it currently replies (nil) "
               "instead of the number of receivers."],
    ),
    # ------------------------------------------------------------ persistence
    _c(
        "SAVE", "SAVE",
        "Write a snapshot to disk",
        "Saves the whole dataset to the snapshot file now. The AOF keeps logging as usual.",
        "+OK, or an error if the save failed",
        ["SAVE"],
    ),
    _c(
        "COMPACT", "COMPACT",
        "Rewrite the AOF log to its smallest form",
        "Replaces the append-only log with the fewest commands that rebuild the current data, so "
        "it stops growing without bound.",
        "+OK AOF log compacted successfully, or an error",
        ["COMPACT"],
    ),
    _c(
        "CONFIG", "CONFIG GET appendfsync",
        "Read or change the AOF sync policy",
        "appendfsync is the only setting. 'always' syncs the log on every write (safest); "
        "'everysec' syncs about once a second (faster). Switching to 'always' flushes pending "
        "data at once.",
        "GET: an array of [appendfsync, current value]. SET: +OK, or an error for any other "
        "value or setting",
        ["CONFIG GET appendfsync", "CONFIG SET appendfsync everysec"],
        variants=["CONFIG SET appendfsync always|everysec"],
    ),
    # ------------------------------------------------------------ replication
    _c(
        "REPLICAOF", "REPLICAOF host port",
        "Follow another server, or become a leader",
        "REPLICAOF host port starts a handshake: this server loads a baseline snapshot from the "
        "master, then receives every write live. REPLICAOF NO ONE promotes a replica back to a "
        "leader. A replica refuses writes.",
        "+OK Replica handshake initiated, or +OK Engine promoted to Leader",
        ["REPLICAOF 127.0.0.1 6379", "REPLICAOF NO ONE"],
        variants=["REPLICAOF NO ONE"],
    ),
    # ------------------------------------------------------------ diagnostics
    _c(
        "INFO", "INFO",
        "Show the engine's version and a summary",
        "In standalone mode it shows the version, the number of keys with a breakdown by type, "
        "keys with an expiry, the sync policy and the AOF size. Connected to a server, those "
        "fields read 'N/A (Server)': use STATS and KEYS instead.",
        "an information panel (answered by the CLI)",
        ["INFO"],
    ),
    _c(
        "STATS", "STATS",
        "Show engine counters",
        "Total keys and the size of each type (string characters, list items, set members, hash "
        "fields, sorted-set nodes, HyperLogLog keys), LRU cache hits, misses and hit rate, and "
        "housekeeping totals: runs, expired keys swept and bytes reclaimed.",
        "a multi-line text report",
        ["STATS"],
    ),
    _c(
        "SLOWLOG", "SLOWLOG GET [count]",
        "Inspect commands that ran slowly",
        "The engine records commands that exceeded its execution-time threshold. GET lists the "
        "recorded entries (only the first count if you give one), LEN says how many there are "
        "and RESET clears the log.",
        "GET: an array of lines like 'ID: 0 | Time: ... | 1234µs | Cmd: ...'. LEN: (integer). "
        "RESET: +OK",
        ["SLOWLOG GET 10", "SLOWLOG LEN", "SLOWLOG RESET"],
        variants=["SLOWLOG LEN", "SLOWLOG RESET"],
    ),
    _c(
        "LATENCY", "LATENCY LAG",
        "Check how responsive the event loop is",
        "LAG returns the current event-loop lag in milliseconds. DOCTOR prints a health report: "
        "the lag, a status (EXCELLENT under 5 ms, WARNING under 20 ms, otherwise CRITICAL), the "
        "I/O sync mode and the key count. Lag is only measured while a server is running; in "
        "standalone mode it reads 0.0ms.",
        "a string",
        ["LATENCY LAG", "LATENCY DOCTOR"],
        variants=["LATENCY DOCTOR"],
    ),
    _c(
        "HOUSEKEEP", "HOUSEKEEP",
        "Run a housekeeping pass now",
        "Sweeps expired keys, repairs stale LRU bookkeeping and compacts containers that are "
        "mostly wasted space. The same pass also runs periodically on its own.",
        "a text report with one name:value line per counter",
        ["HOUSEKEEP"],
    ),
    _c(
        "MODE", "MODE",
        "Show how the CLI is connected",
        "Standalone mode shows the persistence, the reconnect radar, debug status and keys "
        "loaded. TCP mode shows the host, port and connection state.",
        "a status panel (answered by the CLI)",
        ["MODE"],
    ),
    _c(
        "DEBUG", "DEBUG",
        "Toggle diagnostic telemetry",
        "Switches the engine's diagnostic output on or off each time you run it.",
        "a message saying whether telemetry is now on or off (answered by the CLI)",
        ["DEBUG"],
    ),
    # ----------------------------------------------------------- cli & session
    _c(
        "HELP", "HELP [command|topic]",
        "Show help",
        "Without an argument it prints the command overview. HELP <command> shows that "
        "command's usage, replies, examples and the other commands in its group, and HELP "
        "<topic> (for example HELP LISTS) lists a whole group.",
        "a help panel (answered by the CLI)",
        ["HELP", "HELP LRANGE", "HELP LISTS"],
    ),
    _c(
        "DISCONNECT", "DISCONNECT",
        "Leave the server and switch to standalone mode",
        "Closes the TCP connection and carries on with a local engine backed by local disk. The "
        "reconnect radar stays off until you run RECONNECT.",
        "a status panel (answered by the CLI)",
        ["DISCONNECT"],
    ),
    _c(
        "RECONNECT", "RECONNECT",
        "Reconnect to the server",
        "Tries to open the TCP connection again and switches back to server mode. If the "
        "server is still offline you stay in standalone mode.",
        "a status panel (answered by the CLI)",
        ["RECONNECT"],
    ),
    _c(
        "CLEAR", "CLEAR",
        "Clear the screen",
        "CLS does the same.",
        "nothing",
        ["CLEAR"],
    ),
    _c(
        "EXIT", "EXIT",
        "Quit the CLI",
        "QUIT does the same.",
        "nothing",
        ["EXIT"],
    ),
]

COMMANDS = {entry.name: entry for entry in _ENTRIES}
assert len(COMMANDS) == len(_ENTRIES), "duplicate help entry"

# Names that are just another way to type a command.
COMMAND_ALIASES = {"CLS": "CLEAR", "QUIT": "EXIT"}

# family key -> (title, command names in display order)
FAMILIES = {
    "STRINGS": ("String Commands", ("SET", "GET")),
    "KEYSPACE": ("Keys & Expiration", ("DEL", "EXISTS", "TYPE", "KEYS", "EXPIRE", "TTL", "FLUSHALL")),
    "LISTS": ("List Commands", ("LPUSH", "RPUSH", "LPOP", "RPOP", "LRANGE")),
    "SETS": ("Set Commands", ("SADD", "SREM", "SMEMBERS")),
    "HASHES": ("Hash Commands", ("HSET", "HGET", "HGETALL")),
    "ZSETS": ("Sorted Set Commands", ("ZADD", "ZRANGE")),
    "BITMAPS": ("Bitmap Commands", ("SETBIT", "GETBIT", "BITCOUNT", "BITOP")),
    "HYPERLOGLOG": ("HyperLogLog Commands", ("PFADD", "PFCOUNT", "PFMERGE")),
    "STREAMS": ("Stream Commands", ("XADD", "XLEN", "XRANGE", "XREVRANGE", "XREAD", "XDEL", "XTRIM")),
    "GROUPS": ("Consumer Group Commands", ("XGROUP", "XREADGROUP", "XACK", "XPENDING", "XINFO")),
    "TRANSACTIONS": ("Transaction Commands", ("MULTI", "EXEC", "DISCARD", "WATCH", "UNWATCH")),
    "PUBSUB": ("Pub/Sub Commands", ("SUBSCRIBE", "PUBLISH")),
    "PERSISTENCE": ("Persistence & Config", ("SAVE", "COMPACT", "CONFIG")),
    "REPLICATION": ("Replication", ("REPLICAOF",)),
    "DIAGNOSTICS": ("Diagnostics", ("INFO", "STATS", "SLOWLOG", "LATENCY", "HOUSEKEEP", "MODE", "DEBUG")),
    "CLIENT": ("CLI & Session Commands", ("HELP", "DISCONNECT", "RECONNECT", "CLEAR", "EXIT")),
}
# Other ways to name a topic. None may equal a command name (SET is a command,
# so the sets topic is only SETS): a command always wins.
FAMILY_ALIASES = {
    "STRING": "STRINGS", "EXPIRATION": "KEYSPACE", "LIST": "LISTS", "HASH": "HASHES",
    "ZSET": "ZSETS", "SORTEDSETS": "ZSETS", "BITMAP": "BITMAPS", "HLL": "HYPERLOGLOG",
    "STREAM": "STREAMS", "GROUP": "GROUPS", "TRANSACTION": "TRANSACTIONS",
    "REPLICA": "REPLICATION", "DIAGNOSTIC": "DIAGNOSTICS", "SESSION": "CLIENT",
}  # fmt: skip


def _family_of(name: str):
    for key, (_title, names) in FAMILIES.items():
        if name in names:
            return key
    return None


def _canonical(topic: str) -> str:
    topic = topic.upper()
    return COMMAND_ALIASES.get(topic, topic)


def _family_key(topic: str):
    topic = topic.upper()
    if topic in COMMANDS or topic in COMMAND_ALIASES:
        return None  # a command name always means the command
    topic = FAMILY_ALIASES.get(topic, topic)
    return topic if topic in FAMILIES else None


def topics() -> list:
    """Every name HELP can explain in detail."""
    return sorted(set(COMMANDS) | set(COMMAND_ALIASES) | set(FAMILIES) | set(FAMILY_ALIASES))


def topics_hint() -> str:
    """One line for the HELP overview pointing at the detailed pages."""
    names = " ".join(name.lower() for name in FAMILIES)
    return f"[dim]Details: HELP <command>  |  topics: {escape(names)}[/dim]"


def title(topic: str) -> str:
    key = _family_key(topic)
    return FAMILIES[key][0] if key else f"HELP {_canonical(topic)}"


def _family_listing(key: str, current: str = "", heading: bool = True) -> str:
    title_text, names = FAMILIES[key]
    width = max(len(n) for n in names)
    lines = [f"[bold cyan]{escape(title_text)}[/bold cyan]"] if heading else []
    for n in names:
        mark = "[bold yellow]▸[/bold yellow]" if n == current else " "
        lines.append(
            f" {mark} [green]{n}[/green]{' ' * (width - len(n))}  {escape(COMMANDS[n].summary)}"
        )
    return "\n".join(lines)


def render(topic: str):
    """Rich markup for `HELP <topic>`, or None if there is no detailed help."""
    key = _family_key(topic)
    if key:
        return (
            _family_listing(key, heading=False)
            + "\n\n[dim]Type HELP <command> for usage, replies and examples.[/dim]"
        )

    entry = COMMANDS.get(_canonical(topic))
    if entry is None:
        return None

    parts = [f"[bold green]{escape(form)}[/bold green]" for form in entry.forms]
    parts += [
        "",
        escape(entry.summary) + ".",
        escape(entry.details),
        "",
        f"[bold]Returns[/bold]  {escape(entry.returns)}",
        "",
        "[bold]Examples[/bold]",
    ]
    parts += [f"  [cyan]> {escape(example)}[/cyan]" for example in entry.examples]
    for note in entry.notes:
        parts += ["", f"[bold yellow]Note[/bold yellow]  {escape(note)}"]

    family = _family_of(entry.name)
    if family:
        parts += ["", _family_listing(family, current=entry.name)]
    parts += ["", "[dim]Type HELP for every command.[/dim]"]
    return "\n".join(parts)
