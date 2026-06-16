from store import KedisStore


class CommandHandler:
    def __init__(self, store: KedisStore):
        self.store = store

        # The O(1) Dispatch Table
        # Maps the string command directly to the handler function's memory address
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
        }

    def execute(self, tokens: list[str]) -> str:
        """
        Routes parsed tokens to the correct storage operations in O(1) time.
        """
        if not tokens:
            return "(error) empty command or syntax error"

        cmd = tokens[0]

        # The Magic O(1) Router
        # Instead of 9 if/elif checks, we instantly grab the exact function we need.
        if cmd in self._commands:
            handler_function = self._commands[cmd]
            return handler_function(tokens)
        else:
            return f"(error) ERR unknown command '{cmd}'"

    # ---------------------------------------------------------
    # COMMAND HANDLERS (The isolated engine components)
    # ---------------------------------------------------------

    def _handle_set(self, tokens: list[str]) -> str:
        if len(tokens) != 3:
            return "(error) ERR wrong number of arguments for 'SET' command"
        self.store.set(tokens[1], tokens[2])
        return "OK"

    def _handle_get(self, tokens: list[str]) -> str:
        if len(tokens) != 2:
            return "(error) ERR wrong number of arguments for 'GET' command"
        val = self.store.get(tokens[1])
        return val if val is not None else "(nil)"

    def _handle_del(self, tokens: list[str]) -> str:
        if len(tokens) != 2:
            return "(error) ERR wrong number of arguments for 'DEL' command"
        result = self.store.delete(tokens[1])
        return f"(integer) {result}"

    def _handle_exists(self, tokens: list[str]) -> str:
        if len(tokens) != 2:
            return "(error) ERR wrong number of arguments for 'EXISTS' command"
        result = self.store.exists(tokens[1])
        return f"(integer) {result}"

    def _handle_expire(self, tokens: list[str]) -> str:
        if len(tokens) != 3:
            return "(error) ERR wrong number of arguments for 'EXPIRE' command"
        try:
            seconds = int(tokens[2])
        except ValueError:
            return "(error) ERR value is not an integer or out of range"
        result = self.store.set_expire(tokens[1], seconds)
        return f"(integer) {result}"

    def _handle_keys(self, tokens: list[str]) -> str:
        if len(tokens) != 1:
            return "(error) ERR wrong number of arguments for 'KEYS' command"

        all_keys = self.store.keys()

        if not all_keys:
            return "(empty array)"

        # Formats output securely: 1) "user" | hash | -1 | 3
        response_lines = [
            f'{i}) "{k}" | {data["type"]} | {data["ttl"]} | {data["length"]}'
            for i, (k, data) in enumerate(all_keys.items(), 1)
        ]
        return "\n".join(response_lines)

    def _handle_ttl(self, tokens: list[str]) -> str:
        if len(tokens) != 2:
            return "(error) ERR wrong number of arguments for 'TTL' command"
        result = self.store.ttl(tokens[1])
        return f"(integer) {result}"

    def _handle_flushall(self, tokens: list[str]) -> str:
        if len(tokens) != 1:
            return "(error) ERR wrong number of arguments for 'FLUSHALL' command"
        self.store.flushall()
        return "OK"

    def _handle_save(self, tokens: list[str]) -> str:
        if len(tokens) != 1:
            return "(error) ERR wrong number of arguments for 'SAVE' command"
        success = self.store.save()
        return "OK" if success else "(error) ERR failed to save data"

    def _handle_compact(self, tokens: list[str]) -> str:
        if len(tokens) > 1:
            return "-ERR wrong number of arguments for 'compact' command"

        success = self.store.compact_aof()
        if success:
            return "+OK AOF log compacted successfully"
        else:
            return "-ERR Failed to compact AOF log"

    def _handle_lpush(self, tokens: list[str]) -> str:
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'lpush' command"
        try:
            length = self.store.lpush(tokens[1], *tokens[2:])
            return f"(integer) {length}"
        except TypeError as e:
            return f"-{str(e)}"

    def _handle_lrange(self, tokens: list[str]) -> str:
        if len(tokens) != 4:
            return "-ERR wrong number of arguments for 'lrange' command"
        try:
            result = self.store.lrange(tokens[1], int(tokens[2]), int(tokens[3]))
            if not result:
                return "(empty array)"
            return "\n".join(f'{i + 1}) "{val}"' for i, val in enumerate(result))
        except (TypeError, ValueError) as e:
            return f"-{str(e)}"

    def _handle_rpush(self, tokens: list[str]) -> str:
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'rpush' command"
        try:
            length = self.store.rpush(tokens[1], *tokens[2:])
            return f"(integer) {length}"
        except TypeError as e:
            return f"-{str(e)}"

    def _handle_lpop(self, tokens: list[str]) -> str:
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'lpop' command"
        try:
            result = self.store.lpop(tokens[1])
            return f'"{result}"' if result is not None else "(nil)"
        except TypeError as e:
            return f"-{str(e)}"

    def _handle_rpop(self, tokens: list[str]) -> str:
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'rpop' command"
        try:
            result = self.store.rpop(tokens[1])
            return f'"{result}"' if result is not None else "(nil)"
        except TypeError as e:
            return f"-{str(e)}"

    def _handle_sadd(self, tokens: list[str]) -> str:
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'sadd' command"
        try:
            added = self.store.sadd(tokens[1], *tokens[2:])
            return f"(integer) {added}"
        except TypeError as e:
            return f"-{str(e)}"

    def _handle_smembers(self, tokens: list[str]) -> str:
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'smembers' command"
        try:
            members = self.store.smembers(tokens[1])
            if not members:
                return "(empty array)"
            return "\n".join(f'{i + 1}) "{val}"' for i, val in enumerate(members))
        except TypeError as e:
            return f"-{str(e)}"

    def _handle_srem(self, tokens: list[str]) -> str:
        if len(tokens) < 3:
            return "-ERR wrong number of arguments for 'srem' command"
        try:
            removed = self.store.srem(tokens[1], *tokens[2:])
            return f"(integer) {removed}"
        except TypeError as e:
            return f"-{str(e)}"

    def _handle_hset(self, tokens: list[str]) -> str:
        if len(tokens) < 4:
            return "-ERR wrong number of arguments for 'hset' command"
        try:
            # We join the remaining tokens in case the value has spaces
            val = " ".join(tokens[3:])
            result = self.store.hset(tokens[1], tokens[2], val)
            return f"(integer) {result}"
        except TypeError as e:
            return f"-{str(e)}"

    def _handle_hget(self, tokens: list[str]) -> str:
        if len(tokens) != 3:
            return "-ERR wrong number of arguments for 'hget' command"
        try:
            val = self.store.hget(tokens[1], tokens[2])
            return f'"{val}"' if val is not None else "(nil)"
        except TypeError as e:
            return f"-{str(e)}"

    def _handle_hgetall(self, tokens: list[str]) -> str:
        if len(tokens) != 2:
            return "-ERR wrong number of arguments for 'hgetall' command"
        try:
            data = self.store.hgetall(tokens[1])
            if not data:
                return "(empty hash)"

            # Format: field \n value \n field \n value
            lines = []
            for i, (k, v) in enumerate(data.items()):
                lines.append(f'{i * 2 + 1}) "{k}"')
                lines.append(f'{i * 2 + 2}) "{v}"')
            return "\n".join(lines)
        except TypeError as e:
            return f"-{str(e)}"

    def _handle_zadd(self, tokens: list[str]) -> str:
        # Expected: ZADD key score member [score member ...]
        if len(tokens) < 4 or len(tokens) % 2 != 0:
            return "(error) ERR wrong number of arguments for 'zadd' command"

        key = tokens[1]
        added = 0
        try:
            # Loop through in pairs so we can add multiple cars at once!
            for i in range(2, len(tokens), 2):
                score = float(tokens[i])
                member = tokens[i + 1]
                added += self.store.zadd(key, score, member)
            return f"(integer) {added}"
        except ValueError:
            return "(error) ERR value is not a valid float"
        except TypeError as e:
            return f"(error) {str(e)}"

    def _handle_zrange(self, tokens: list[str]) -> str:
        # Expected: ZRANGE key start stop [WITHSCORES]
        if len(tokens) < 4 or len(tokens) > 5:
            return "(error) ERR wrong number of arguments for 'zrange' command"

        key = tokens[1]
        withscores = False

        if len(tokens) == 5:
            if tokens[4].upper() == "WITHSCORES":
                withscores = True
            else:
                return "(error) ERR syntax error"

        try:
            start = int(tokens[2])
            stop = int(tokens[3])
            result = self.store.zrange(key, start, stop, withscores)

            if not result:
                return "(empty array)"

            return "\n".join(f'{i + 1}) "{val}"' for i, val in enumerate(result))
        except ValueError:
            return "(error) ERR value is not an integer or out of range"
        except TypeError as e:
            return f"(error) {str(e)}"

    def _handle_type(self, tokens: list[str]) -> str:
        # Expected: TYPE key
        if len(tokens) != 2:
            return "(error) ERR wrong number of arguments for 'type' command"

        key = tokens[1]
        return self.store.type_of(key)

    def _handle_stats(self, tokens: list[str]) -> str:
        """Pulls both Global Data Stats and LRU Telemetry from the engine"""
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
