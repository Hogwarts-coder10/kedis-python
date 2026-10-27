import json
import math
import threading
import time
from typing import Any

from .parser import BulkString, KESPEncoder
from .store import KedisStore
from .stream import StreamError


class CommandHandler:
    # Bytes a pub/sub subscriber may leave unread before the server cuts it off.
    MAX_SUBSCRIBER_BUFFER = 32 * 1024 * 1024

    def __init__(self, store: KedisStore):
        self.store = store

        # Global Engine lock
        self._engine_lock = threading.Lock()

        # Pub / Sub SwitchBoard
        self._channels = {}
        # The server's event loop, set per connection; pub/sub pushes are queued
        # onto it because commands run in worker threads.
        self.event_loop = None

        # The O(1) Dispatch Table
        self._commands = {
            "SET": self._handle_set,
            "GET": self._handle_get,
            "DEL": self._handle_del,
            "EXISTS": self._handle_exists,
            "EXPIRE": self._handle_expire,
            "KEYS": self._handle_keys,
            "TTL": self._handle_ttl,
            "FLUSHALL": self._handle_flushall,
            "SAVE": self._handle_save,
            "COMPACT": self._handle_compact,
            "LPUSH": self._handle_lpush,
            "LRANGE": self._handle_lrange,
            "RPUSH": self._handle_rpush,
            "LPOP": self._handle_lpop,
            "RPOP": self._handle_rpop,
            "SADD": self._handle_sadd,
            "SMEMBERS": self._handle_smembers,
            "SREM": self._handle_srem,
            "HSET": self._handle_hset,
            "HGET": self._handle_hget,
            "HGETALL": self._handle_hgetall,
            "ZADD": self._handle_zadd,
            "ZRANGE": self._handle_zrange,
            "TYPE": self._handle_type,
            "STATS": self._handle_stats,
            "CONFIG": self._handle_config,
            "SUBSCRIBE": self._handle_subscribe,
            "PUBLISH": self._handle_publish,
            "SLOWLOG": self.cmd_slowlog,
            "LATENCY": self.cmd_latency,
            "SETBIT": self._handle_setbit,
            "GETBIT": self._handle_getbit,
            "BITCOUNT": self._handle_bitcount,
            "BITOP": self._handle_bitop,
            "PFADD": self._handle_pfadd,
            "PFCOUNT": self._handle_pfcount,
            "PFMERGE": self._handle_pfmerge,
            "HOUSEKEEP": self._handle_housekeep,
            "XADD": self._handle_xadd,
            "XLEN": self._handle_xlen,
            "XRANGE": self._handle_xrange,
            "XREVRANGE": self._handle_xrevrange,
            "XREAD": self._handle_xread,
            "XDEL": self._handle_xdel,
            "XTRIM": self._handle_xtrim,
            "XGROUP": self._handle_xgroup,
            "XREADGROUP": self._handle_xreadgroup,
            "XACK": self._handle_xack,
            "XPENDING": self._handle_xpending,
            "XINFO": self._handle_xinfo,
            "XGAPPLY": self._handle_xgapply,  # internal: replication only
        }

    @property
    def WRITE_COMMANDS(self) -> set[str]:
        """
        The definitive list of all engine commands that mutate database state.
        Used by the replication slipstream to broadcast changes and enforce read-only firewalls.
        """
        return {
            "SET",
            "DEL",
            "EXPIREAT",
            "FLUSHALL",
            "LPUSH",
            "RPUSH",
            "LPOP",
            "RPOP",
            "SADD",
            "SREM",
            "HSET",
            "ZADD",
            "SETBIT",
            "BITOP",
            "PFADD",
            "PFMERGE",
            "XADD",
            "XDEL",
            "XTRIM",
            "XGROUP",
            "XREADGROUP",
            "XACK",
        }

    # Commands whose effect is carried to replicas as resolved records.
    GROUP_COMMANDS = frozenset({"XGROUP", "XREADGROUP", "XACK"})

    # Dispatchable, but never meant for a person to type (replication only).
    INTERNAL_COMMANDS = frozenset({"XGAPPLY"})

    @property
    def public_commands(self) -> list[str]:
        """Every command a client may issue: what the CLI offers for
        autocomplete and typo suggestions."""
        return [c for c in self._commands if c not in self.INTERNAL_COMMANDS]

    def execute(self, tokens: list[str], client_socket=None):
        """
        Routes parsed tokens to the correct storage operations in O(1) time.
        Returns raw Python types (int, list, str, None) for the KESP Encoder.
        """
        return self.execute_records(tokens, client_socket)[0]

    def execute_records(self, tokens: list[str], client_socket=None):
        """Like execute(), but returns (result, group_records): the resolved
        consumer-group records this command produced, which a master forwards
        to replicas (see replication_payloads)."""
        if not tokens:
            return "-ERR empty command or syntax error", []

        # 🚀 TRACK A: Start the high-precision telemetry stopwatch (in nanoseconds)
        start_time = time.perf_counter_ns()
        try:
            with self._engine_lock:
                return self._run_locked(tokens, client_socket)
        finally:
            self._record_duration(tokens, start_time)

    def snapshot_bytes(self) -> bytes:
        """The baseline a new follower is sent, as one KESP frame. Built with the
        engine lock held, so it is never half of a transaction and no worker thread
        changes the data while it is read."""
        with self._engine_lock:
            body = json.dumps(self.store.get_snapshot_state()).encode("utf-8")
        return b"A1\n" + f"S{len(body)}\n".encode("utf-8") + body + b"\n"

    def execute_batch(self, batch: list, client_socket=None):
        """Runs several commands as one unit: the engine lock is taken once, so
        no other client's command can run between them, and the AOF records
        them back to back. Returns (results, forwards): one result per command,
        and the commands a master must send to replicas, in order (see
        replication_payloads). Commands are not skipped or reordered; one that
        fails gives an error result and the rest still run, like the one-at-a-
        time path."""
        results: list = []
        forwards: list = []
        write_commands = self.WRITE_COMMANDS
        starts: list = []
        with self._engine_lock:
            for tokens in batch:
                if not tokens:
                    results.append("-ERR empty command or syntax error")
                    starts.append(None)
                    continue
                starts.append(time.perf_counter_ns())
                result, records = self._run_locked(tokens, client_socket)
                results.append(result)
                if tokens[0].upper() in write_commands:
                    forwards.extend(self.replication_payloads(tokens, result, records))
        for tokens, started in zip(batch, starts):
            if started is not None:
                self._record_duration(tokens, started)
        return results, forwards

    def _run_locked(self, tokens: list[str], client_socket=None):
        """Runs one command. The caller holds the engine lock."""
        cmd = tokens[0].upper()
        result = None
        records = []

        try:
            try:
                if cmd == "XGAPPLY" and client_socket is not None:
                    # Internal replication command: a network client
                    # must not be able to write raw group records.
                    result = f"-ERR unknown command '{cmd}'"
                elif cmd in self._commands:
                    handler_function = self._commands[cmd]
                    if cmd in ["SUBSCRIBE", "PUBLISH"]:
                        result = handler_function(tokens, client_socket)
                    else:
                        # Handlers take the whole token list, command name included.
                        result = handler_function(tokens)
                else:
                    result = f"-ERR unknown command '{cmd}'"
            finally:
                records = self.store.drain_group_records()
            return result, records
        except TypeError:
            return f"-ERR wrong number of arguments for '{cmd}' command", records
        except Exception as e:
            return f"-ERR {str(e)}", records

    def _record_duration(self, tokens: list[str], start_time: int) -> None:
        # 🚀 TRACK A: Stop the stopwatch, convert nanoseconds to microseconds
        duration_us = (time.perf_counter_ns() - start_time) // 1000

        # Feed the metrics to the store's slowlog ring buffer if available
        if (
            hasattr(self, "store")
            and self.store
            and hasattr(self.store, "_log_slow_command")
        ):
            self.store._log_slow_command(tokens, duration_us)

    # ---------------------------------------------------------
    # COMMAND HANDLERS (The isolated engine components)
    # ---------------------------------------------------------

    def _handle_config(self, tokens: list[str]):
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'CONFIG' command"

        sub_cmd = tokens[1].upper()
        param = tokens[2].lower()

        if param != "appendfsync":
            return f"-ERR unsupported CONFIG parameter '{param}'"

        if sub_cmd == "SET":
            if len(tokens) != 4:
                return "-ERR wrong number of arguments for 'CONFIG SET'"
            new_mode = tokens[3].lower()
            return self.store.set_appendfsync(new_mode)

        elif sub_cmd == "GET":
            if len(tokens) != 3:
                return "-ERR wrong number of arguments for 'CONFIG GET'"
            return [param, self.store.appendfsync]

        else:
            return f"-ERR unknown CONFIG subcommand '{sub_cmd}'"

    def _handle_set(self, tokens: list[str]):
        if len(tokens) != 3:
            return "-ERR wrong number of arguments for 'SET' command"
        self.store.set(tokens[1], tokens[2])
        return "OK"

    @staticmethod
    def _data(value):
        """A stored value as a reply: always data, never read as a status or
        an error, whatever it looks like ("OK", "ERROR x", "-ERR y")."""
        return BulkString(value) if isinstance(value, str) else value

    def _handle_get(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'GET' command"
        try:
            return self._data(self.store.get(tokens[1]))
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_del(self, tokens: list[str]):
        if len(tokens) < 2:
            return "-ERR wrong number of arguments for 'DEL' command"
        return sum(self.store.delete(key) for key in tokens[1:])

    def _handle_exists(self, tokens: list[str]):
        if len(tokens) < 2:
            return "-ERR wrong number of arguments for 'EXISTS' command"
        # Like Redis, a key named twice counts twice.
        return sum(self.store.exists(key) for key in tokens[1:])

    def _handle_expire(self, tokens: list[str]):
        if len(tokens) != 3:
            return "-ERR wrong number of arguments for 'EXPIRE' command"
        try:
            seconds = int(tokens[2])
        except ValueError:
            return "-ERR value is not an integer or out of range"
        return self.store.set_expire(tokens[1], seconds)

    def _handle_keys(self, tokens: list[str]):
        if len(tokens) != 1:
            return "-ERR wrong number of arguments for 'KEYS' command"
        all_keys = self.store.keys()
        if not all_keys:
            return []

        return [
            f"{k} | {data['type']} | {data['ttl']} | {data['length']}"
            for k, data in all_keys.items()
        ]

    def _handle_ttl(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'TTL' command"
        return self.store.ttl(tokens[1])

    def _handle_flushall(self, tokens: list[str]):
        if len(tokens) != 1:
            return "-ERR wrong number of arguments for 'FLUSHALL' command"
        self.store.flushall()
        return "OK"

    def _handle_save(self, tokens: list[str]):
        if len(tokens) != 1:
            return "-ERR wrong number of arguments for 'SAVE' command"
        success = self.store.save()
        return "OK" if success else "-ERR failed to save data"

    def _handle_compact(self, tokens: list[str]):
        if len(tokens) > 1:
            return "-ERR wrong number of arguments for 'compact' command"
        success = self.store.compact_aof()
        return (
            "+OK AOF log compacted successfully"
            if success
            else "-ERR Failed to compact AOF log"
        )

    def _handle_lpush(self, tokens: list[str]):
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'lpush' command"
        try:
            return self.store.lpush(tokens[1], *tokens[2:])
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_lrange(self, tokens: list[str]):
        if len(tokens) != 4:
            return "-ERR wrong number of arguments for 'lrange' command"
        try:
            result = self.store.lrange(tokens[1], int(tokens[2]), int(tokens[3]))
            return result if result else []
        except (TypeError, ValueError) as e:
            return f"-ERR {str(e)}"

    def _handle_rpush(self, tokens: list[str]):
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'rpush' command"
        try:
            return self.store.rpush(tokens[1], *tokens[2:])
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_lpop(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'lpop' command"
        try:
            return self._data(self.store.lpop(tokens[1]))
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_rpop(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'rpop' command"
        try:
            return self._data(self.store.rpop(tokens[1]))
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_sadd(self, tokens: list[str]):
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'sadd' command"
        try:
            return self.store.sadd(tokens[1], *tokens[2:])
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_smembers(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'smembers' command"
        try:
            members = self.store.smembers(tokens[1])
            return members if members else []
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_srem(self, tokens: list[str]):
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'srem' command"
        try:
            return self.store.srem(tokens[1], *tokens[2:])
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_hset(self, tokens: list[str]):
        if len(tokens) < 4:
            return "-ERR wrong number of arguments for 'hset' command"
        try:
            val = " ".join(tokens[3:])
            return self.store.hset(tokens[1], tokens[2], val)
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_hget(self, tokens: list[str]):
        if len(tokens) != 3:
            return "-ERR wrong number of arguments for 'hget' command"
        try:
            return self._data(self.store.hget(tokens[1], tokens[2]))
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_hgetall(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'hgetall' command"
        try:
            data = self.store.hgetall(tokens[1])
            if not data:
                return []

            # Flattens dict into [key1, val1, key2, val2]
            flat_list = []
            for k, v in data.items():
                flat_list.extend([k, v])
            return flat_list
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_zadd(self, tokens: list[str]):
        if len(tokens) < 4 or len(tokens) % 2 != 0:
            return "-ERR wrong number of arguments for 'zadd' command"
        key = tokens[1]
        added = 0
        try:
            # Validate every pair before applying any: a bad score anywhere
            # means nothing is added.
            pairs = []
            for i in range(2, len(tokens), 2):
                score = float(tokens[i])
                if math.isnan(score):
                    raise ValueError
                pairs.append((score, tokens[i + 1]))
            for score, member in pairs:
                added += self.store.zadd(key, score, member)
            return added
        except ValueError:
            return "-ERR value is not a valid float"
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_zrange(self, tokens: list[str]):
        if len(tokens) < 4 or len(tokens) > 5:
            return "-ERR wrong number of arguments for 'zrange' command"

        key = tokens[1]
        withscores = False

        if len(tokens) == 5:
            if tokens[4].upper() == "WITHSCORES":
                withscores = True
            else:
                return "-ERR syntax error"

        try:
            start = int(tokens[2])
            stop = int(tokens[3])
            result = self.store.zrange(key, start, stop, withscores)
            return result if result else []
        except ValueError:
            return "-ERR value is not an integer or out of range"
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_setbit(self, tokens: list[str]):
        if len(tokens) != 4:
            return "-ERR wrong number of arguments for 'setbit' command"
        try:
            offset = int(tokens[2])
            bit = int(tokens[3])
            return self.store.setbit(tokens[1], offset, bit)
        except ValueError as e:
            return f"-ERR {str(e)}"
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_getbit(self, tokens: list[str]):
        if len(tokens) != 3:
            return "-ERR wrong number of arguments for 'getbit' command"
        try:
            offset = int(tokens[2])
            return self.store.getbit(tokens[1], offset)
        except ValueError as e:
            return f"-ERR {str(e)}"
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_bitcount(self, tokens: list[str]):
        if len(tokens) not in (2, 4):
            return "-ERR wrong number of arguments for 'bitcount' command"
        try:
            if len(tokens) == 4:
                start = int(tokens[2])
                end = int(tokens[3])
                return self.store.bitcount(tokens[1], start, end)
            return self.store.bitcount(tokens[1])
        except ValueError:
            return "-ERR value is not an integer or out of range"
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_bitop(self, tokens: list[str]):
        if len(tokens) < 4:
            return "-ERR wrong number of arguments for 'bitop' command"
        try:
            operation = tokens[1]
            destkey = tokens[2]
            srckeys = tokens[3:]
            return self.store.bitop(operation, destkey, *srckeys)
        except ValueError as e:
            return f"-ERR {str(e)}"
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_pfadd(self, tokens: list[str]):
        if len(tokens) < 2:
            return "-ERR wrong number of arguments for 'pfadd' command"
        try:
            return self.store.pfadd(tokens[1], *tokens[2:])
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_pfcount(self, tokens: list[str]):
        if len(tokens) < 2:
            return "-ERR wrong number of arguments for 'pfcount' command"
        try:
            return self.store.pfcount(*tokens[1:])
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_pfmerge(self, tokens: list[str]):
        if len(tokens) < 2:
            return "-ERR wrong number of arguments for 'pfmerge' command"
        try:
            self.store.pfmerge(tokens[1], *tokens[2:])
            return "OK"
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_type(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'type' command"
        return self.store.type_of(tokens[1])

    def _handle_stats(self, tokens: list[str]):
        stats = self.store.get_engine_stats()
        lru = stats.get("lru_cache", {})
        hk = stats.get("housekeeping", {})

        return (
            f"Total Keys:{stats.get('total_keys', 0)}\n"
            f"String Chars:{stats.get('string_chars', 0)}\n"
            f"List Items:{stats.get('list_items', 0)}\n"
            f"Set Members:{stats.get('set_members', 0)}\n"
            f"Hash Fields:{stats.get('hash_fields', 0)}\n"
            f"ZSet Nodes:{stats.get('zset_nodes', 0)}\n"
            f"HLL Keys:{stats.get('hll_keys', 0)}\n"
            "---\n"
            f"LRU Hits:{lru.get('hits', 0)}\n"
            f"LRU Misses:{lru.get('misses', 0)}\n"
            f"Hit Rate:{lru.get('hit_rate_pct', 0.0)}%\n"
            f"LRU Tracked:{lru.get('tracked_keys', 0)} / {lru.get('max_size', 1024)}\n"
            "---\n"
            f"Housekeeping Runs:{hk.get('runs', 0)}\n"
            f"Expired Swept:{hk.get('expired', 0)}\n"
            f"LRU Ghosts Fixed:{hk.get('lru_orphans', 0)}\n"
            f"Bytes Reclaimed:{hk.get('reclaimed_bytes', 0)}"
        )

    # ------------------------------------------------------------------
    # STREAMS
    # Replies are nested arrays exactly like Redis: XRANGE -> [[id, [f, v..]]],
    # XREAD -> [[key, [[id, [f, v..]]]]]. Everything that is user data is
    # wrapped in BulkString so values like "OK" or "ERROR ..." stay data.
    # ------------------------------------------------------------------

    @staticmethod
    def _to_int(text: str, message: str = "ERR value is not an integer or out of range"):
        try:
            return int(text)
        except ValueError:
            raise StreamError(message)

    @staticmethod
    def _format_entries(entries) -> list:
        return [
            [BulkString(sid), [BulkString(x) for x in fields]] for sid, fields in entries
        ]

    def _parse_trim(self, tokens: list, i: int, command: str):
        """Parses `MAXLEN|MINID [=|~] threshold [LIMIT n]` starting at tokens[i].
        Returns (maxlen, minid, next_index). '~' is accepted but trimming is
        always exact, and LIMIT is accepted and ignored."""
        strategy = tokens[i].upper()
        i += 1
        if i < len(tokens) and tokens[i] in ("=", "~"):
            i += 1
        if i >= len(tokens):
            raise StreamError("ERR syntax error")
        maxlen = minid = None
        if strategy == "MAXLEN":
            maxlen = self._to_int(tokens[i])
        else:
            minid = tokens[i]
        i += 1
        if i < len(tokens) and tokens[i].upper() == "LIMIT":
            if i + 1 >= len(tokens):
                raise StreamError("ERR syntax error")
            self._to_int(tokens[i + 1])
            i += 2
        return maxlen, minid, i

    def _parse_xadd(self, tokens: list) -> dict:
        """XADD key [NOMKSTREAM] [MAXLEN|MINID [=|~] n [LIMIT c]] id|* field value ..."""
        i = 2
        opts = {"nomkstream": False, "maxlen": None, "minid": None}
        while i < len(tokens):
            word = tokens[i].upper()
            if word == "NOMKSTREAM":
                opts["nomkstream"] = True
                i += 1
            elif word in ("MAXLEN", "MINID"):
                if opts["maxlen"] is not None or opts["minid"] is not None:
                    raise StreamError("ERR syntax error")
                opts["maxlen"], opts["minid"], i = self._parse_trim(tokens, i, "xadd")
            else:
                break
        if i >= len(tokens):
            raise StreamError("ERR wrong number of arguments for 'xadd' command")
        fields = tokens[i + 1 :]
        if not fields or len(fields) % 2:
            raise StreamError("ERR wrong number of arguments for 'xadd' command")
        return {**opts, "id_index": i, "id": tokens[i], "fields": fields}

    def _handle_xadd(self, tokens: list[str]):
        if len(tokens) < 5:
            return "-ERR wrong number of arguments for 'xadd' command"
        try:
            p = self._parse_xadd(tokens)
            sid = self.store.xadd(
                tokens[1],
                p["fields"],
                p["id"],
                maxlen=p["maxlen"],
                minid=p["minid"],
                nomkstream=p["nomkstream"],
            )
        except StreamError as e:
            return f"-{e}"
        except TypeError as e:
            return f"-ERR {e}"
        return None if sid is None else BulkString(sid)

    def replication_tokens(self, tokens: list, response) -> list:
        """What a master should forward to replicas for this command.
        XADD must carry the ID the master actually assigned: a replica that
        saw '*' would pick its own clock-based ID and the two would diverge."""
        if (
            tokens
            and tokens[0].upper() == "XADD"
            and isinstance(response, str)
            and not response.startswith("-")
        ):
            try:
                id_index = self._parse_xadd(tokens)["id_index"]
            except StreamError:
                return tokens
            forwarded = list(tokens)
            forwarded[id_index] = str(response)
            return forwarded
        return tokens

    def _handle_xlen(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'xlen' command"
        try:
            return self.store.xlen(tokens[1])
        except TypeError as e:
            return f"-ERR {e}"

    def _range_command(self, tokens: list, reverse: bool):
        name = "xrevrange" if reverse else "xrange"
        if len(tokens) not in (4, 6):
            return f"-ERR wrong number of arguments for '{name}' command"
        count = None
        try:
            if len(tokens) == 6:
                if tokens[4].upper() != "COUNT":
                    return "-ERR syntax error"
                count = self._to_int(tokens[5])
            if reverse:
                entries = self.store.xrevrange(tokens[1], tokens[2], tokens[3], count)
            else:
                entries = self.store.xrange(tokens[1], tokens[2], tokens[3], count)
        except StreamError as e:
            return f"-{e}"
        except TypeError as e:
            return f"-ERR {e}"
        return self._format_entries(entries)

    def _handle_xrange(self, tokens: list[str]):
        return self._range_command(tokens, reverse=False)

    def _handle_xrevrange(self, tokens: list[str]):
        return self._range_command(tokens, reverse=True)

    @staticmethod
    def parse_xread(tokens: list):
        """XREAD [COUNT n] [BLOCK ms] STREAMS key... id...
        Returns (count, block_ms, [(key, id), ...]); count None = unlimited,
        block_ms None = do not block (0 = block forever)."""
        count = block_ms = None
        i = 1
        while i < len(tokens):
            word = tokens[i].upper()
            if word == "COUNT":
                if i + 1 >= len(tokens):
                    raise StreamError("ERR syntax error")
                n = CommandHandler._to_int(tokens[i + 1])
                count = None if n <= 0 else n  # Redis: COUNT 0 means no limit
                i += 2
            elif word == "BLOCK":
                if i + 1 >= len(tokens):
                    raise StreamError("ERR syntax error")
                block_ms = CommandHandler._to_int(
                    tokens[i + 1], "ERR timeout is not an integer or out of range"
                )
                if block_ms < 0:
                    raise StreamError("ERR timeout is negative")
                i += 2
            elif word == "STREAMS":
                i += 1
                break
            else:
                raise StreamError("ERR syntax error")
        else:
            raise StreamError("ERR syntax error")
        rest = tokens[i:]
        if not rest or len(rest) % 2:
            raise StreamError(
                "ERR Unbalanced 'xread' list of streams: for each stream key "
                "an ID or '$' must be specified."
            )
        half = len(rest) // 2
        return count, block_ms, list(zip(rest[:half], rest[half:]))

    def _xread_reply(self, streams: list, count):
        out = self.store.xread(streams, count)
        if not out:
            return None
        return [[BulkString(key), self._format_entries(entries)] for key, entries in out]

    def _handle_xread(self, tokens: list[str]):
        # Executed inline (also inside MULTI) this never blocks: BLOCK is
        # honoured by the async server, which waits for writers to wake it.
        try:
            count, _block, streams = self.parse_xread(tokens)
            return self._xread_reply(streams, count)
        except StreamError as e:
            return f"-{e}"
        except TypeError as e:
            return f"-ERR {e}"

    @staticmethod
    def requested_block_ms(tokens: list):
        """The BLOCK timeout (ms) an XREAD/XREADGROUP asked for, or None if it
        has no BLOCK, is malformed, or is some other command."""
        try:
            cmd = tokens[0].upper() if tokens else ""
            if cmd == "XREAD":
                return CommandHandler.parse_xread(tokens)[1]
            if cmd == "XREADGROUP":
                return CommandHandler.parse_xreadgroup(tokens)[3]
        except StreamError:
            pass
        return None

    def resolve_stream_ids(self, streams: list) -> list:
        with self._engine_lock:
            return self.store.xresolve_ids(streams)

    def xread_nonblocking(self, streams: list, count):
        with self._engine_lock:
            return self._xread_reply(streams, count)

    def _handle_xdel(self, tokens: list[str]):
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'xdel' command"
        try:
            return self.store.xdel(tokens[1], *tokens[2:])
        except StreamError as e:
            return f"-{e}"
        except TypeError as e:
            return f"-ERR {e}"

    def _handle_xtrim(self, tokens: list[str]):
        if len(tokens) < 4:
            return "-ERR wrong number of arguments for 'xtrim' command"
        try:
            maxlen, minid, end = self._parse_trim(tokens, 2, "xtrim")
            if end != len(tokens):
                return "-ERR syntax error"
            return self.store.xtrim(tokens[1], maxlen=maxlen, minid=minid)
        except StreamError as e:
            return f"-{e}"
        except TypeError as e:
            return f"-ERR {e}"

    # ------------------------------------------------------------------
    # CONSUMER GROUPS
    # ------------------------------------------------------------------
    @staticmethod
    def error_reply(e) -> str:
        """An error string the KESP encoder turns into an error reply. Codes
        such as NOGROUP/BUSYGROUP are kept as the start of the message."""
        msg = str(e)
        return f"-{msg}" if msg.startswith("ERR ") else f"-ERR {msg}"

    def _guarded(self, call):
        try:
            return call()
        except (StreamError, TypeError) as e:
            return self.error_reply(e)

    @staticmethod
    def _format_group_entries(entries) -> list:
        # fields is None for a pending entry whose stream entry was deleted
        return [
            [BulkString(sid), None if fields is None else [BulkString(x) for x in fields]]
            for sid, fields in entries
        ]

    def _handle_xgroup(self, tokens: list[str]):
        """XGROUP CREATE key group id|$ [MKSTREAM] | SETID key group id|$ |
        DESTROY key group | CREATECONSUMER key group c | DELCONSUMER key group c"""
        if len(tokens) < 2:
            return "-ERR wrong number of arguments for 'xgroup' command"
        sub = tokens[1].upper()
        store, n = self.store, len(tokens)

        def create():
            mkstream = False
            for word in (w.upper() for w in tokens[5:]):
                if word == "MKSTREAM":
                    mkstream = True
                elif word == "ENTRIESREAD":
                    raise StreamError("ERR ENTRIESREAD is not supported")
                else:
                    raise StreamError("ERR syntax error")
            return store.xgroup_create(tokens[2], tokens[3], tokens[4], mkstream)

        def setid():
            if n > 5:
                if tokens[5].upper() == "ENTRIESREAD":
                    raise StreamError("ERR ENTRIESREAD is not supported")
                raise StreamError("ERR syntax error")
            return store.xgroup_setid(tokens[2], tokens[3], tokens[4])

        arity = {"CREATE": 5, "SETID": 5, "DESTROY": 4, "CREATECONSUMER": 5, "DELCONSUMER": 5}
        if sub not in arity:
            return f"-ERR unknown subcommand '{tokens[1]}' for 'xgroup' command"
        if n < arity[sub]:
            return "-ERR wrong number of arguments for 'xgroup' command"
        if sub == "CREATE":
            return self._guarded(create)
        if sub == "SETID":
            return self._guarded(setid)
        if n != arity[sub]:
            return "-ERR wrong number of arguments for 'xgroup' command"
        if sub == "DESTROY":
            return self._guarded(lambda: store.xgroup_destroy(tokens[2], tokens[3]))
        if sub == "CREATECONSUMER":
            return self._guarded(
                lambda: store.xgroup_createconsumer(tokens[2], tokens[3], tokens[4])
            )
        return self._guarded(
            lambda: store.xgroup_delconsumer(tokens[2], tokens[3], tokens[4])
        )

    @staticmethod
    def parse_xreadgroup(tokens: list):
        """XREADGROUP GROUP group consumer [COUNT n] [BLOCK ms] [NOACK]
        STREAMS key... id...  Returns (group, consumer, count, block_ms, noack,
        [(key, id), ...]); count None = unlimited, block_ms None = no BLOCK."""
        group = consumer = count = block_ms = None
        noack = False
        i = 1
        while i < len(tokens):
            word = tokens[i].upper()
            if word == "GROUP":
                if i + 2 >= len(tokens):
                    raise StreamError("ERR syntax error")
                group, consumer = tokens[i + 1], tokens[i + 2]
                i += 3
            elif word == "COUNT":
                if i + 1 >= len(tokens):
                    raise StreamError("ERR syntax error")
                n = CommandHandler._to_int(tokens[i + 1])
                count = None if n <= 0 else n  # Redis: COUNT 0 means no limit
                i += 2
            elif word == "BLOCK":
                if i + 1 >= len(tokens):
                    raise StreamError("ERR syntax error")
                block_ms = CommandHandler._to_int(
                    tokens[i + 1], "ERR timeout is not an integer or out of range"
                )
                if block_ms < 0:
                    raise StreamError("ERR timeout is negative")
                i += 2
            elif word == "NOACK":
                noack = True
                i += 1
            elif word == "STREAMS":
                i += 1
                break
            else:
                raise StreamError("ERR syntax error")
        else:
            raise StreamError("ERR syntax error")
        if group is None:
            raise StreamError("ERR syntax error")
        rest = tokens[i:]
        if not rest or len(rest) % 2:
            raise StreamError(
                "ERR Unbalanced 'xreadgroup' list of streams: for each stream "
                "key an ID or '>' must be specified."
            )
        half = len(rest) // 2
        return group, consumer, count, block_ms, noack, list(zip(rest[:half], rest[half:]))

    def _xreadgroup_reply(self, group, consumer, streams, count, noack):
        out = self.store.xreadgroup(group, consumer, streams, count, noack)
        if not out:
            return None
        return [[BulkString(key), self._format_group_entries(e)] for key, e in out]

    def _handle_xreadgroup(self, tokens: list[str]):
        # Executed inline (also inside MULTI) this never blocks: BLOCK is
        # honoured by the async server, like XREAD.
        def run():
            group, consumer, count, _block, noack, streams = self.parse_xreadgroup(tokens)
            return self._xreadgroup_reply(group, consumer, streams, count, noack)

        return self._guarded(run)

    def xreadgroup_attempt(self, group, consumer, streams, count, noack):
        """One non-blocking XREADGROUP pass for the async server's wait loop.
        Returns (reply, records): reply is None when there is nothing yet,
        and the records must be forwarded to replicas."""
        with self._engine_lock:
            try:
                reply = self._guarded(
                    lambda: self._xreadgroup_reply(group, consumer, streams, count, noack)
                )
            finally:
                records = self.store.drain_group_records()
        return reply, records

    def _handle_xack(self, tokens: list[str]):
        if len(tokens) < 4:
            return "-ERR wrong number of arguments for 'xack' command"
        return self._guarded(lambda: self.store.xack(tokens[1], tokens[2], *tokens[3:]))

    def _handle_xpending(self, tokens: list[str]):
        """XPENDING key group [[IDLE min-idle] start end count [consumer]]"""
        n = len(tokens)
        if n < 3:
            return "-ERR wrong number of arguments for 'xpending' command"
        key, group = tokens[1], tokens[2]

        def run():
            if n == 3:
                total, lo, hi, consumers = self.store.xpending_summary(key, group)
                return [
                    total,
                    None if lo is None else BulkString(lo),
                    None if hi is None else BulkString(hi),
                    [[BulkString(c), BulkString(str(k))] for c, k in consumers] or None,
                ]
            i, min_idle = 3, None
            if tokens[i].upper() == "IDLE":
                if i + 1 >= n:
                    raise StreamError("ERR syntax error")
                min_idle = self._to_int(tokens[i + 1])
                i += 2
            if n - i not in (3, 4):
                raise StreamError("ERR syntax error")
            count = self._to_int(tokens[i + 2])
            consumer = tokens[i + 3] if n - i == 4 else None
            rows = self.store.xpending_range(
                key, group, tokens[i], tokens[i + 1], count, consumer, min_idle
            )
            return [[BulkString(sid), BulkString(c), idle, k] for sid, c, idle, k in rows]

        return self._guarded(run)

    def _handle_xinfo(self, tokens: list[str]):
        """XINFO GROUPS key | XINFO CONSUMERS key group"""
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'xinfo' command"
        sub = tokens[1].upper()

        def groups():
            return [
                [
                    "name", BulkString(name),
                    "consumers", consumers,
                    "pending", pending,
                    "last-delivered-id", BulkString(last_id),
                    "entries-read", None,  # not tracked
                    "lag", None,  # not tracked
                ]
                for name, consumers, pending, last_id in self.store.xinfo_groups(tokens[2])
            ]

        def consumers():
            return [
                [
                    "name", BulkString(name),
                    "pending", pending,
                    "idle", idle,
                    "inactive", inactive,
                ]
                for name, pending, idle, inactive in self.store.xinfo_consumers(
                    tokens[2], tokens[3]
                )
            ]

        if sub == "GROUPS" and len(tokens) == 3:
            return self._guarded(groups)
        if sub == "CONSUMERS" and len(tokens) == 4:
            return self._guarded(consumers)
        if sub in ("GROUPS", "CONSUMERS"):
            return "-ERR wrong number of arguments for 'xinfo' command"
        return f"-ERR unknown subcommand '{tokens[1]}' for 'xinfo' command (supported: GROUPS, CONSUMERS)"

    def _handle_xgapply(self, tokens: list[str]):
        """Internal: XGAPPLY KIND key args... applies one resolved group
        record sent by a master. execute() refuses it from network clients."""
        if len(tokens) < 4:
            return "-ERR wrong number of arguments for 'xgapply' command"

        def apply():
            self.store.apply_group_record(tokens[1:])
            return "OK"

        return self._guarded(apply)

    def replication_payloads(self, tokens: list, response, records: list) -> list:
        """The commands a master forwards to replicas for one executed write.
        Group commands forward the resolved records they produced (wrapped in
        XGAPPLY), never the raw command: a replica re-running XREADGROUP
        would choose its own entries and timestamps."""
        if tokens and tokens[0].upper() in self.GROUP_COMMANDS:
            return [["XGAPPLY", *record] for record in records]
        return [self.replication_tokens(tokens, response)]

    def run_housekeeping(self) -> dict:
        """Entry point for the background task: one pass under the engine lock."""
        with self._engine_lock:
            return self.store.housekeeping()

    def _handle_housekeep(self, tokens: list[str]):
        if len(tokens) != 1:
            return "-ERR wrong number of arguments for 'housekeep' command"
        report = self.store.housekeeping()  # execute() already holds the lock
        return "\n".join(f"{k}:{v}" for k, v in report.items())

    def _handle_subscribe(self, tokens: list[str], client_socket):
        if client_socket is None:
            return "-ERR network socket not found"
        if len(tokens) < 2:
            return "-ERR wrong number of arguments for 'subscribe' command"

        channel = tokens[1]

        if channel not in self._channels:
            self._channels[channel] = []

        if client_socket not in self._channels[channel]:
            self._channels[channel].append(client_socket)

        return ["subscribe", channel, 1]

    def _handle_publish(self, tokens: list[str], client_socket):
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'publish' command"

        channel = tokens[1]
        message = " ".join(tokens[2:])

        if channel not in self._channels:
            return 0

        subscribers = self._channels[channel]
        receivers = 0

        # Construct raw KESP bytes for the broadcast push
        kesp_payload = KESPEncoder.encode_command(["message", channel, message])

        dead_sockets = []
        for sock in subscribers:
            try:
                self._push(sock, kesp_payload)
                receivers += 1
            except Exception:
                dead_sockets.append(sock)

        for dead in dead_sockets:
            subscribers.remove(dead)
        if not subscribers:
            del self._channels[channel]

        return receivers

    def _push(self, sub, payload: bytes) -> None:
        """Delivers one pub/sub message to a subscriber, raising if it is gone.

        The server hands subscribers over as asyncio StreamWriters, and it runs
        commands in worker threads, where a StreamWriter must not be touched
        directly: the write is queued onto the event loop instead. A plain
        socket (any object with sendall) is written to directly."""
        if hasattr(sub, "sendall"):
            sub.sendall(payload)
            return
        if sub.is_closing():
            raise ConnectionError("subscriber is gone")
        transport = getattr(sub, "transport", None)
        if (
            transport is not None
            and transport.get_write_buffer_size() > self.MAX_SUBSCRIBER_BUFFER
        ):
            # A subscriber that isn't reading would otherwise make the server
            # hold every message for it, without limit. Cut it off instead.
            loop = self.event_loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(sub.close)
            else:
                sub.close()
            raise ConnectionError("subscriber output buffer limit exceeded")
        loop = self.event_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(sub.write, payload)
        else:
            sub.write(payload)

    def cmd_slowlog(self, tokens: list[str]) -> Any:
        if len(tokens) < 2:
            return "-ERR wrong number of arguments for 'slowlog' command"

        subcmd = tokens[1].upper()

        if subcmd == "GET":
            count = None
            if len(tokens) > 2:
                try:
                    count = int(tokens[2])
                except ValueError:
                    return "-ERR value is not an integer or out of range"

            # Fetch raw logs
            raw_logs = self.store.slowlog_get(count)
            if not raw_logs:
                return []

            # Formatting raw logs for client UI
            formatted_logs = []

            for entry in raw_logs:
                log_id = entry[0]
                timestamp = entry[1]
                duration_us = entry[2]
                cmd_string = " ".join(entry[3])

                formatted_logs.append(
                    f"ID: {log_id} | Time: {timestamp} | {duration_us}µs | Cmd: {cmd_string}"
                )

            return formatted_logs

        elif subcmd == "LEN":
            return self.store.slowlog_len()

        elif subcmd == "RESET":
            self.store.slowlog_reset()
            return "+OK"

        else:
            return f"-ERR Unknown subcommand '{subcmd}'. Try SLOWLOG <GET|LEN|RESET>"

    def cmd_latency(self, tokens: list[str]) -> Any:
        if len(tokens) < 2:
            return "-ERR wrong number of arguments for 'latency' command"

        subcmd = tokens[1].upper()
        # Safely fetch the lag, defaulting to 0.0 if in Local Mode without a heartbeat
        lag_ms = getattr(self.store, "current_lag_ms", 0.0)

        if subcmd == "LAG":
            return f"{lag_ms}ms"

        elif subcmd == "DOCTOR":
            # The Engine Health Diagnostic Report
            drivetrain = self.store.appendfsync.upper()
            keys = len(getattr(self.store, "_data", {}))

            # Determine health status based on lag severity
            if lag_ms < 5.0:
                health = "[green]EXCELLENT[/green]"
            elif lag_ms < 20.0:
                health = "[yellow]WARNING (Mild Loop Delay)[/yellow]"
            else:
                health = "[red]CRITICAL (Heavy Blocking)[/red]"

            report = (
                f"--- KEDIS ENGINE HEALTH ---\n"
                f"Event Loop Lag : {lag_ms}ms\n"
                f"Status         : {health}\n"
                f"I/O Drivetrain : {drivetrain}\n"
                f"Active Keys    : {keys}\n"
            )
            return report

        else:
            return f"-ERR Unknown subcommand '{subcmd}'. Try LATENCY <LAG|DOCTOR>"
