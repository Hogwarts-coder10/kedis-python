import json
import os
import time
from typing import Optional


class KedisStore:
    def __init__(self, aof_filename="kedis.aof"):
        self.debug_mode = False
        # The primary hash map for storing key-value pairs
        self._data: dict[str, str] = {}
        # A secondary hash map to track expiration timestamps (Unix time)
        self._expires: dict[str, float] = {}

        self.aof_filename = aof_filename
        self.recover_from_aof()
        self.aof_file = open(self.aof_filename, "a")

    def _log_operation(self, *tokens):
        """
        Appends the raw command to AOF and forces it to the physical disk
        """

        command_string = " ".join(str(t) for t in tokens)
        self.aof_file.write(command_string + "\n")
        self.aof_file.flush()
        os.fsync(self.aof_file.fileno())

    def _evict_if_expired(self, key: str) -> bool:
        """
        Helper to passively check and evict an expired key. Returns True if evicted.
        """

        if key in self._expires:
            # If the current time has passed the expiration time
            if time.time() >= self._expires[key]:
                del self._data[key]
                del self._expires[key]
                return True
        return False

    def set(self, key: str, value: str) -> None:
        """
        Sets a key to hold a string value.
        """
        self._log_operation("SET", key, value)
        self._data[key] = value
        # If the key is overwritten, any previous TTL should be cleared
        if key in self._expires:
            del self._expires[key]

    def get(self, key: str) -> Optional[str]:
        """Gets the value of a key, returning None if it doesn't exist (or expired)."""
        if self.debug_mode:
            print(f"\n🕵️  DEBUG REPL: Engine asked for key -> '{key}'")

            print(
                f"🕵️  DEBUG MEMORY: Keys currently in memory -> {list(self._data.keys())}\n"
            )

        self._evict_if_expired(key)
        return self._data.get(key)

    def delete(self, key: str) -> int:
        """Removes the specified key. Returns 1 if deleted, 0 if not found."""
        self._evict_if_expired(key)
        if key in self._data:
            self._log_operation("DEL", key)
            del self._data[key]
            # No need to check if it's in _expires, pop handles it safely
            self._expires.pop(key, None)
            return 1
        return 0

    def exists(self, key: str) -> int:
        """Returns 1 if the key exists, 0 otherwise."""
        self._evict_if_expired(key)
        return 1 if key in self._data else 0

    def set_expire(self, key: str, seconds: int) -> int:
        """Sets a timeout on key. Returns 1 if set, 0 if key does not exist."""
        # Evict first just in case they try to set an expiry on an already-expired key
        if self._evict_if_expired(key) or key not in self._data:
            return 0

        absolute_expire_time = time.time() + seconds

        self._log_operation("EXPIREAT", key, absolute_expire_time)

        # time.time() gets the current epoch time in seconds
        self._expires[key] = absolute_expire_time
        return 1

    def keys(self) -> list[str]:
        """
        Returns a list of all active keys.
        """

        active_keys = []

        for k in list(self._data.keys()):
            self._evict_if_expired(k)

            if k in self._data:
                active_keys.append(k)

        return active_keys

    def ttl(self, key: str) -> int:
        """
        Returns the remaining  time to live of a key in seconds
        """

        self._evict_if_expired(key)
        if key not in self._data:  # key doesn't exists
            return -2

        if key not in self._expires:  # key is there but no expiry time
            return -1

        # Key has expiry time , hence calcuate seconds
        remaining_secs = int(self._expires[key] - time.time())

        return remaining_secs

    def flushall(self) -> None:
        """
        Removes all keys and expirations,
        completely resetting the database."
        """
        self._log_operation("FLUSHALL")
        self._data.clear()
        self._expires.clear()

    def save(self, filename: str = "dump.json") -> bool:
        """
        Snapshots the current memory state to a JSON file
        """

        try:
            # Bundling states to dictionaries
            state = {"data": self._data, "expires": self._expires}

            with open(filename, "w") as f:
                json.dump(state, f)

            return True

        except Exception as e:
            print(f"DEBUG: SAVE FAILED -{e}")
            return False

    def load(self, filename: str = "dump.json") -> None:
        """Loads the memory state from disk on startup."""
        if os.path.exists(filename):
            try:
                with open(filename, "r") as f:
                    state = json.load(f)
                    self._data = state.get("data", {})
                    self._expires = state.get("expires", {})
            except Exception as e:
                print(f"DEBUG: Load failed - {e}")

    def recover_from_aof(self):
        """Reads the AOF file on startup and cleanly rebuilds the database memory."""

        # 1. ALWAYS check if the file exists before trying to read it!
        if not os.path.exists(self.aof_filename):
            if self.debug_mode:
                print("⚠️  No AOF found. Starting with a clean slate.\n")
            return

        if self.debug_mode:
            print("\n" + "=" * 40)
            print("🛠️  INITIATING RESURRECTION SEQUENCE")
            print("=" * 40)
            full_path = os.path.abspath(self.aof_filename)
            print(f"🔍 Checking for AOF at: {full_path}")
            print(f"🔧 Found {self.aof_filename}! Reading telemetry...\n")

        # 2. Read the log line by line
        with open(self.aof_filename, "r") as f:
            for line in f:
                if self.debug_mode:
                    print(f"  -> Raw line found: '{line.strip()}'")

                tokens = line.strip().split()
                if not tokens:
                    continue

                cmd = tokens[0].upper()

                if cmd == "SET" and len(tokens) >= 3:
                    value = " ".join(tokens[2:])
                    self._data[tokens[1]] = value
                    self._expires.pop(tokens[1], None)
                    if self.debug_mode:
                        print(f"     ✅ INJECTED: {tokens[1]} = {value}")

                elif cmd == "DEL" and len(tokens) >= 2:
                    self._data.pop(tokens[1], None)
                    self._expires.pop(tokens[1], None)
                    if self.debug_mode:
                        print(f"     🗑️  DELETED: {tokens[1]}")

                # --- THE MISSING EXPIREAT PARSER ---
                elif cmd == "EXPIREAT" and len(tokens) >= 3:
                    absolute_time = float(tokens[2])
                    self._expires[tokens[1]] = absolute_time
                    if self.debug_mode:
                        print(
                            f"     ⏱️  EXPIRY SET: {tokens[1]} will die at {absolute_time}"
                        )

                elif cmd == "FLUSHALL":
                    self._data.clear()
                    self._expires.clear()
                    if self.debug_mode:
                        print("     🔥 FLUSHED ALL DATA")

        if self.debug_mode:
            print(
                f"\n✅ Recovery complete. Memory currently holds {len(self._data)} keys."
            )
            print("=" * 40 + "\n")
