import threading

from store import KedisStore


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
        }

    def execute(self, tokens: list[str], client_socket=None):
        """
        Routes parsed tokens to the correct storage operations in O(1) time.
        Returns raw Python types (int, list, str, None) for the KESP Encoder.
        """
        if not tokens:
            return "-ERR empty command or syntax error"

        cmd = tokens[0].upper()

        with self._engine_lock:
            if cmd in self._commands:
                if cmd in ["SUBSCRIBE", "PUBLISH"]:
                    handler_function = self._commands[cmd]
                    return handler_function(tokens, client_socket)
                else:
                    handler_function = self._commands[cmd]
                    return handler_function(tokens)
            else:
                return f"-ERR unknown command '{cmd}'"

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
        return self.store.get(tokens[1])

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

    def _handle_type(self, tokens: list[str]):
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'type' command"
        return self.store.type_of(tokens[1])

    def _handle_stats(self, tokens: list[str]):
        stats = self.store.get_engine_stats()
        lru = stats.get("lru_cache", {})

        return (
            f"Total Keys:{stats.get('total_keys', 0)}\n"
            f"String Chars:{stats.get('string_chars', 0)}\n"
            f"List Items:{stats.get('list_items', 0)}\n"
            f"Set Members:{stats.get('set_members', 0)}\n"
            f"Hash Fields:{stats.get('hash_fields', 0)}\n"
            f"ZSet Nodes:{stats.get('zset_nodes', 0)}\n"
            "---\n"
            f"LRU Hits:{lru.get('hits', 0)}\n"
            f"LRU Misses:{lru.get('misses', 0)}\n"
            f"Hit Rate:{lru.get('hit_rate_pct', 0.0)}%\n"
            f"LRU Tracked:{lru.get('tracked_keys', 0)} / {lru.get('max_size', 128)}"
        )

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

        return receivers
