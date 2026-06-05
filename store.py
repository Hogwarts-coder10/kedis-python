import json
import os
import time
from typing import Any, Optional

from skiplist import SkipList


class KedisStore:
    def __init__(self, aof_filename="kedis.aof"):
        self.debug_mode = False
        # The primary hash map for storing key-value pairs
        self._data: dict[str, Any] = {}
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

    def _evict_all_expired(self) -> None:
        """
        The Active Sweeper: Scans the entire engine for expired keys
        and safely unloads them from memory and the AOF log.
        """
        current_time = time.time()
        keys_to_delete = []

        # 1. Build the Hit-List (Avoids RuntimeError from changing dict size during loop)
        for key, exp_time in self._expires.items():
            if current_time > exp_time:
                keys_to_delete.append(key)

        # 2. Execute the Deletions
        for key in keys_to_delete:
            if key in self._data:
                del self._data[key]

            if key in self._expires:
                del self._expires[key]

            # 3. Tell the Persistence Layer so it doesn't resurrect on reboot
            self._log_operation("DEL", key)

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

    def keys(self) -> dict:
        """Returns metadata for all active keys (The Radar)."""
        self._evict_all_expired()
        result = {}

        for k, val in self._data.items():
            if k in self._expires and self._expires[k] < time.time():
                continue

            # --- THE RADAR SIGNATURES ---
            if isinstance(val, list):
                k_type = "list"
                k_len = str(len(val))
            elif isinstance(val, set):
                k_type = "set"
                k_len = str(len(val))
            elif isinstance(val, dict):
                k_type = "hash"
                k_len = str(len(val))
            elif isinstance(val, SkipList):
                k_type = "zset"
                k_len = str(len(val))
            else:
                k_type = "string"
                k_len = str(len(str(val)))

            # Calculate TTL
            ttl = -1
            if k in self._expires:
                ttl = int(self._expires[k] - time.time())

            result[k] = {"type": k_type, "ttl": ttl, "length": k_len}

        return result

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

                # --- NEW LIST PARSERS ---
                elif cmd == "LPUSH" and len(tokens) >= 3:
                    key = tokens[1]
                    if key not in self._data:
                        self._data[key] = []
                    for val in tokens[2:]:
                        self._data[key].insert(0, val)

                elif cmd == "RPUSH" and len(tokens) >= 3:
                    key = tokens[1]
                    if key not in self._data:
                        self._data[key] = []
                    self._data[key].extend(tokens[2:])

                elif cmd == "LPOP" and len(tokens) >= 2:
                    key = tokens[1]
                    if (
                        key in self._data
                        and isinstance(self._data[key], list)
                        and self._data[key]
                    ):
                        self._data[key].pop(0)
                        if len(self._data[key]) == 0:
                            del self._data[key]

                elif cmd == "RPOP" and len(tokens) >= 2:
                    key = tokens[1]
                    if (
                        key in self._data
                        and isinstance(self._data[key], list)
                        and self._data[key]
                    ):
                        self._data[key].pop(-1)
                        if len(self._data[key]) == 0:
                            del self._data[key]

                elif cmd == "SADD" and len(tokens) >= 3:
                    key = tokens[1]
                    if key not in self._data:
                        self._data[key] = set()
                    self._data[key].update(tokens[2:])

                elif cmd == "SREM" and len(tokens) >= 3:
                    key = tokens[1]
                    if key in self._data and isinstance(self._data[key], set):
                        self._data[key].difference_update(tokens[2:])
                        if len(self._data[key]) == 0:
                            del self._data[key]

                    # --- NEW HASH PARSER ---
                elif cmd == "HSET" and len(tokens) >= 4:
                    key = tokens[1]
                    field = tokens[2]
                    value = " ".join(tokens[3:])
                    if key not in self._data:
                        self._data[key] = {}
                    self._data[key][field] = value

                elif cmd == "ZADD" and len(tokens) >= 4:
                    key = tokens[1]
                    score = float(tokens[2])
                    member = " ".join(tokens[3:])
                    if key not in self._data:
                        self._data[key] = SkipList()
                    self._data[key].insert(score, member)

        if self.debug_mode:
            print(
                f"\n✅ Recovery complete. Memory currently holds {len(self._data)} keys."
            )
            print("=" * 40 + "\n")

    def compact_aof(self):
        """
        Compacts the AOF size down to only active and living keys
        """

        temp_file = f"temp_{self.aof_filename}"

        try:
            with open(temp_file, "w") as f:
                for key, value in self._data.items():
                    if isinstance(value, list):
                        # Write lists back as an RPUSH so they rebuild perfectly
                        f.write(f"RPUSH {key} {' '.join(value)}\n")

                    # --- THE SET COMPACTOR BARRIER ---
                    elif isinstance(value, set):
                        f.write(f"SADD {key} {' '.join(value)}\n")

                    elif isinstance(value, dict):
                        for h_field, h_val in value.items():
                            f.write(f"HSET {key} {h_field} {h_val}\n")

                    else:
                        f.write(f"SET {key} {value}\n")

                if hasattr(self, "_expires"):
                    for key, exp_time in self._expires.items():
                        if exp_time > time.time():
                            f.write(f"EXPIREAT {key} {exp_time}\n")

            # -----------------------------------
            # The OS Routing Fork
            # -----------------------------------
            if os.name == "nt":
                # Windows DriveTrain: Explicit Close, delete and rename required
                if hasattr(self, "aof_file") and not self.aof_file.closed:
                    self.aof_file.close()

                # Micro-pausing to let the OS-Level file lock fully clear
                time.sleep(0.1)

                if os.path.exists(self.aof_filename):
                    os.remove(self.aof_filename)

                os.rename(temp_file, self.aof_filename)

                self.aof_file = open(self.aof_filename, "a")

            else:
                # Linux / UNIX drivetrain: Atomic OS-Level Hotswap
                os.replace(temp_file, self.aof_filename)

                # Refresh the file pointer so it writes to the new file, not the ghost inode
                if hasattr(self, "aof_file") and not self.aof_file.closed:
                    self.aof_file.close()
                self.aof_file = open(self.aof_filename, "a")

            return True

        except Exception as e:
            # Clean the wrekage if crashed mid build
            if os.path.exists(temp_file):
                os.remove(temp_file)

            if hasattr(self, "file") and self.file.closed:
                self.file = open(self.aof_filename, "a")

            print(f"Compaction failed: {e}")
            return False

    # LIST OPERATIONS
    def lpush(self, key: str, *values: str) -> int:
        """
        Inserts all specified values at the head of the list
        stored at key. Returns the length of the list after push operations
        """

        self._evict_if_expired(key)

        # TYPE BARRIER: if key exists but isn't a list, throw an error (telemetry glitch)
        if key in self._data and not isinstance(self._data[key], list):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        if key not in self._data:
            self._data[key] = []

        for val in values:
            self._data[key].insert(0, val)

        # LPUSH inserts at the head (index 0).
        # Pushing multiple values happens one by one from left to right.

        self._log_operation("LPUSH", key, *values)

        return len(self._data[key])

    def lrange(self, key: str, start: int, stop: int) -> list:
        """
        Returns the specified Elements of the list stored at key
        """

        self._evict_if_expired(key)

        if key not in self._data:
            return []

        if not isinstance(self._data[key], list):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        if stop == -1:
            return self._data[key][start:]
        else:
            return self._data[key][start : stop + 1]

    def rpush(self, key: str, *values: str) -> int:
        """Inserts all specified values at the tail of the list."""
        self._evict_if_expired(key)

        if key in self._data and not isinstance(self._data[key], list):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        if key not in self._data:
            self._data[key] = []

        # RPUSH appends to the tail. Using extend adds them in the exact order provided.
        self._data[key].extend(values)
        self._log_operation("RPUSH", key, *values)

        return len(self._data[key])

    def lpop(self, key: str) -> Optional[str]:
        """Removes and returns the first element of the list."""
        self._evict_if_expired(key)

        if key not in self._data:
            return None

        if not isinstance(self._data[key], list):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        if not self._data[key]:  # Empty list fallback
            return None

        # Pop the head (index 0)
        popped_value = self._data[key].pop(0)
        self._log_operation("LPOP", key)

        # Redis standard: If the list is empty after popping, delete the key entirely
        if len(self._data[key]) == 0:
            del self._data[key]
            self._expires.pop(key, None)

        return popped_value

    def rpop(self, key: str) -> Optional[str]:
        """Removes and returns the last element of the list."""
        self._evict_if_expired(key)

        if key not in self._data:
            return None

        if not isinstance(self._data[key], list):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        if not self._data[key]:
            return None

        # Pop the tail (index -1)
        popped_value = self._data[key].pop(-1)
        self._log_operation("RPOP", key)

        if len(self._data[key]) == 0:
            del self._data[key]
            self._expires.pop(key, None)

        return popped_value

    # SET OPERATIONS
    def sadd(self, key: str, *members: str) -> int:
        """
        Adds one or more members to a set,
        returns the number of members added
        """

        self._evict_if_expired(key)

        if key in self._data and not isinstance(self._data[key], set):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        if key not in self._data:
            self._data[key] = set()

        initial_len = len(self._data[key])

        self._data[key].update(members)

        added_count = len(self._data[key]) - initial_len

        if added_count > 0:
            self._log_operation("SADD", key, *members)

        return added_count

    def smembers(self, key: str) -> list[str]:
        """
        Returns all members of the set value stored at key.
        """

        self._evict_if_expired(key)

        if key not in self._data:
            return []

        if not isinstance(self._data[key], set):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        # Convert the native Python set back into a list so the router can print it cleanly
        return list(self._data[key])

    def srem(self, key: str, *members: str) -> int:
        """
        Removes the specified members from the set. Returns the number of members removed.
        """

        self._evict_if_expired(key)

        if key not in self._data:
            return 0

        if not isinstance(self._data[key], set):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        initial_len = len(self._data[key])

        # .difference_update() cleanly removes items from a Python set
        self._data[key].difference_update(members)
        removed_count = initial_len - len(self._data[key])

        if removed_count > 0:
            self._log_operation("SREM", key, *members)

        # Clean up the garage: if the set is empty, delete the key entirely
        if len(self._data[key]) == 0:
            del self._data[key]
            self._expires.pop(key, None)

        return removed_count

    def hset(self, key: str, field: str, value: str) -> int:
        """Sets field in the hash stored at key to value."""
        self._evict_if_expired(key)

        if key in self._data and not isinstance(self._data[key], dict):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        if key not in self._data:
            self._data[key] = {}

        # Returns 1 if field is a new field, 0 if it was just updated
        is_new = 1 if field not in self._data[key] else 0
        self._data[key][field] = value

        self._log_operation("HSET", key, field, value)
        return is_new

    # HASH Operations
    def hget(self, key: str, field: str) -> Optional[str]:
        """Returns the value associated with field in the hash stored at key."""
        self._evict_if_expired(key)

        if key not in self._data or not isinstance(self._data[key], dict):
            return None

        return self._data[key].get(field)

    def hgetall(self, key: str) -> dict:
        """Returns all fields and values of the hash stored at key."""
        self._evict_if_expired(key)

        if key not in self._data:
            return {}

        if not isinstance(self._data[key], dict):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        return self._data[key]

    # ---------------------------------------------------------
    # SORTED SET OPERATIONS (The API)
    # ---------------------------------------------------------
    def zadd(self, key: str, score: float, member: str) -> int:
        self._evict_if_expired(key)

        if key in self._data and not isinstance(self._data[key], SkipList):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        if key not in self._data:
            self._data[key] = SkipList()

        is_new = self._data[key].insert(float(score), member)
        self._log_operation("ZADD", key, score, member)
        return is_new

    def zrange(self, key: str, start: int, stop: int, withscores: bool = False) -> list:
        self._evict_if_expired(key)

        if key not in self._data:
            return []

        if not isinstance(self._data[key], SkipList):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        return self._data[key].get_range(start, stop, withscores)

    def type_of(self, key: str) -> str:
        """
        Returns the internal data-structure of a given key
        """

        self._evict_if_expired(key)

        if key not in self._data:
            return "None"

        val = self._data[key]

        if isinstance(val, list):
            return "List"

        elif isinstance(val, set):
            return "Set"

        elif isinstance(val, dict):
            return "hash"

        elif isinstance(val, SkipList):
            return "zset"

        else:
            return "string"
