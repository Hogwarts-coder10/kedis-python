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

        # Refactored to a clean list comprehension
        response_lines = [f'{i}) "{key}"' for i, key in enumerate(all_keys, 1)]
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
