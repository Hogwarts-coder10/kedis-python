from store import KedisStore

class CommandHandler:
    def __init__(self, store: KedisStore):
        self.store = store

    def execute(self, tokens: list[str]) -> str:
        """
        Routes parsed tokens to the correct storage operations.
        Returns a formatted Redis-style string response.
        """
        if not tokens:
            return "(error) empty command or syntax error"

        # The parser already uppercased tokens[0] for us
        cmd = tokens[0]

        if cmd == 'SET':
            # Format: SET key value
            if len(tokens) != 3:
                return "(error) ERR wrong number of arguments for 'SET' command"
            self.store.set(tokens[1], tokens[2])
            return "OK"

        elif cmd == 'GET':
            # Format: GET key
            if len(tokens) != 2:
                return "(error) ERR wrong number of arguments for 'GET' command"
            val = self.store.get(tokens[1])
            return val if val is not None else "(nil)"

        elif cmd == 'DEL':
            # Format: DEL key
            if len(tokens) != 2:
                return "(error) ERR wrong number of arguments for 'DEL' command"
            result = self.store.delete(tokens[1])
            return f"(integer) {result}"

        elif cmd == 'EXISTS':
            # Format: EXISTS key
            if len(tokens) != 2:
                return "(error) ERR wrong number of arguments for 'EXISTS' command"
            result = self.store.exists(tokens[1])
            return f"(integer) {result}"

        elif cmd == 'EXPIRE':
            # Format: EXPIRE key seconds
            if len(tokens) != 3:
                return "(error) ERR wrong number of arguments for 'EXPIRE' command"
            try:
                seconds = int(tokens[2])
            except ValueError:
                return "(error) ERR value is not an integer or out of range"

            result = self.store.set_expire(tokens[1], seconds)
            return f"(integer) {result}"

        elif cmd == 'KEYS':
            # Format: KEYS
            if len(tokens) != 1:
                return "(error) ERR wrong number of arguments for 'KEYS' command"

            all_keys = self.store.keys()

            if not all_keys:
                return "(empty array)"

            # Redis formats array replies as a numbered list
            response_lines = []
            for i, key in enumerate(all_keys, 1):
                response_lines.append(f"{i}) \"{key}\"")

            return "\n".join(response_lines)

        elif cmd == 'TTL':
            if len(tokens) != 2:
                return "(error) ERR wrong number of arguments for 'TTL' command"

            result = self.store.ttl(tokens[1])

            return f"(integer) {result}"

        elif cmd == "FLUSHALL":
            # Format: FLUSHALL
            if len(tokens) != 1:
                return "(error) ERR wrong number of arguments for 'FLUSHALL' command"

            self.store.flushall()
            return "OK"

        elif cmd == "SAVE":
            if len(tokens) != 1:
                return "(error) ERR wrong number of arguments for 'SAVE' command"

            sucess = self.store.save()

            return "OK" if sucess else "(error) ERR failed to save data"

        else:
            return f"(error) ERR unknown command '{cmd}'"
