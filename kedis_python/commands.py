import threading
import time
from typing import Any

from .parser import BulkString
from .store import KedisStore
from .stream import StreamError


class CommandHandler:
    def __init__(self, store: KedisStore):
        self.store = store

        # Global Engine lock
        self._engine_lock = threading.Lock()

        # Pub / Sub SwitchBoard
        self._channels = {}

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
        }

    def execute(self, tokens: list[str], client_socket=None):
        """
        Routes parsed tokens to the correct storage operations in O(1) time.
        Returns raw Python types (int, list, str, None) for the KESP Encoder.
        """
        if not tokens:
            return "-ERR empty command or syntax error"

        cmd = tokens[0].upper()
        args = tokens[
            1:
        ]  # (Though your current code passes tokens, let's keep your routing exact)

        # 🚀 TRACK A: Start the high-precision telemetry stopwatch (in nanoseconds)
        start_time = time.perf_counter_ns()
        result = None

        try:
            with self._engine_lock:
                if cmd in self._commands:
                    if cmd in ["SUBSCRIBE", "PUBLISH"]:
                        handler_function = self._commands[cmd]
                        result = handler_function(tokens, client_socket)
                    else:
                        handler_function = self._commands[cmd]
                        # Note: Depending on how your handlers are registered,
                        # they might expect 'tokens' or 'args'. Keeping your existing call:
                        result = handler_function(tokens)
                    return result
                else:
                    result = f"-ERR unknown command '{cmd}'"
                    return result

        except TypeError as e:
            result = f"-ERR wrong number of arguments for '{cmd}' command"
            return result
        except Exception as e:
            result = f"-ERR {str(e)}"
            return result

        finally:
            # 🚀 TRACK A: Stop the stopwatch, convert nanoseconds to microseconds
            end_time = time.perf_counter_ns()
            duration_us = (end_time - start_time) // 1000

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

    def _handle_get(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'GET' command"
        try:
            return self.store.get(tokens[1])
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_del(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'DEL' command"
        return self.store.delete(tokens[1])

    def _handle_exists(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'EXISTS' command"
        return self.store.exists(tokens[1])

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
            return self.store.lpop(tokens[1])
        except TypeError as e:
            return f"-ERR {str(e)}"

    def _handle_rpop(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'rpop' command"
        try:
            return self.store.rpop(tokens[1])
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
            return self.store.hget(tokens[1], tokens[2])
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
            for i in range(2, len(tokens), 2):
                score = float(tokens[i])
                member = tokens[i + 1]
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
        kesp_payload = (
            f"A3\n"
            f"S7\nmessage\n"
            f"S{len(channel.encode('utf-8'))}\n{channel}\n"
            f"S{len(message.encode('utf-8'))}\n{message}\n"
        ).encode("utf-8")

        dead_sockets = []
        for sock in subscribers:
            try:
                sock.sendall(kesp_payload)
                receivers += 1
            except Exception:
                dead_sockets.append(sock)

        for dead in dead_sockets:
            subscribers.remove(dead)

        return

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
