import itertools
import json
import os
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Optional

from skiplist import SkipList


class KedisStore:
    def __init__(
        self,
        aof_filename="kedis.aof",
        lru_maxsize: int = 128,
        appendfsync: str = "everysec",
    ):
        self.debug_mode = False
        # The primary hash map for storing key-value pairs of ANY type
        self._data: dict[str, Any] = {}
        # A secondary hash map to track expiration timestamps (Unix time)
        self._expires: dict[str, float] = {}

        # ---------------------------------------------------------
        # TRUE LRU SURVIVAL TRACKER
        # Uses OrderedDict purely to track the timeline of access.
        # Values are None to save RAM. Protects ALL data types globally.
        # ---------------------------------------------------------
        self._lru_tracker: OrderedDict[str, None] = OrderedDict()
        self._lru_maxsize = lru_maxsize
        self._lru_hits = 0
        self._lru_misses = 0

        # 🚀 Issue 9 FIX: Cold Boot SnapShot recovery
        # Pulls the heavy data into RAM before the network even turns on
        self._load_snapshot()

        self._is_recovering = True

        self.aof_filename = aof_filename

        # --- THE I/O DRIVETRAIN ---
        self.appendfsync = appendfsync
        self._shutdown_flag = False

        self.recover_from_aof()

        self._is_recovering = False
        self.aof_file = open(self.aof_filename, "a")

        # SLOWLOG TELEMETRY ENGINE
        self.slowlog_slower_than = (
            10000  # Default threshold: 10,000 microseconds (10ms)
        )
        self.slowlog_max_len = 128  # Maximum number of logged items to retain in RAM
        self.slowblog_id_counter = 0  # Maximum number of logged items to retain in RAM
        self._slowlog_queue: deque = deque(maxlen=self.slowlog_max_len)

        # Start the background sync engine if using high-performance mode
        if self.appendfsync == "everysec":
            self._sync_thread = threading.Thread(
                target=self._background_fsync, daemon=True
            )
            self._sync_thread.start()

    # ------------------------------------------------------------------
    # SNAPSHOT SERIALIZATION ENVELOPES (Used by SAVE and REPLICATION)
    # ------------------------------------------------------------------
    def get_snapshot_state(self) -> dict:
        """
        Packages the complex memory map into a JSON-safe dictionary envelope.
        """
        serializable_data = {}
        for k, v in self._data.items():
            v_type = type(v).__name__
            if v_type == "SkipList":
                serializable_data[k] = {"__type__": "skiplist", "data": v.member_map}
            elif v_type == "set":
                serializable_data[k] = {"__type__": "set", "data": list(v)}
            elif v_type == "deque":
                serializable_data[k] = {"__type__": "deque", "data": list(v)}
            else:
                serializable_data[k] = v

        return {"data": serializable_data, "expires": getattr(self, "_expires", {})}

    def restore_snapshot_state(self, state: dict) -> None:
        """
        Restores memory and rebuilds complex objects from a JSON-safe envelope.
        """
        raw_data = state.get("data", {})
        self._expires = state.get("expires", {})
        self._data = {}

        for k, v in raw_data.items():
            if isinstance(v, dict) and "__type__" in v:
                if v["__type__"] == "skiplist":
                    sl = SkipList()
                    for member, score in v["data"].items():
                        sl.insert(score, member)
                    self._data[k] = sl
                elif v["__type__"] == "set":
                    self._data[k] = set(v["data"])
                elif v["__type__"] == "deque":
                    self._data[k] = deque(v["data"])
            else:
                self._data[k] = v

        if hasattr(self, "_lru_clear"):
            self._lru_clear()
            for k in self._data.keys():
                self._touch_write(k)

    def set_appendfsync(self, mode: str) -> str:
        """
        Dynamically shifts the I/O drivetrain while the engine is running.
        """

        if mode not in ("always", "everysec"):
            return "-ERR unsupported sync mode. Use 'always' or 'everysec'"

        self.appendfsync = mode

        if mode == "always":
            # Shifting to MAX SAFETY: Immediately flush any pending RAM buffers to disk
            if getattr(self, "aof_file", None) and not self.aof_file.closed:
                try:
                    self.aof_file.flush()
                    os.fsync(self.aof_file.fileno())
                except OSError:
                    pass
            return "+OK appendfsync set to always"

        if mode == "everysec":
            # Shifting to MAX SPEED: Make sure the background thread is actually alive
            if not hasattr(self, "_sync_thread") or not self._sync_thread.is_alive():
                self._shutdown_flag = False
                self._sync_thread = threading.Thread(
                    target=self._background_fsync, daemon=True
                )
                self._sync_thread.start()
            return "+OK appendfsync set to everysec"

    def shutdown(self):
        """
        Executes a clean shutdown. Stops the background I/O thread,
        and forces a final synchronous write to the physical disk

        (Basically Gracefully shutting down)....
        """

        self._shutdown_flag = (
            True  # signals the background thread to safely exit it's loop
        )

        if hasattr(self, "_sync_thread") and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=2.0)

        # Force one final, blocking flush of the RAM buffer to the SSD
        if hasattr(self, "aof_file") and not self.aof_file.closed:
            try:
                self.aof_file.flush()
                os.fsync(self.aof_file.fileno())
                self.aof_file.close()
                if self.debug_mode:
                    print(
                        "💾 [SHUTDOWN] Final AOF buffer flushed to disk successfully."
                    )

            except OSError as e:
                if self.debug_mode:
                    print(f"⚠️ [SHUTDOWN] Failed to flush final buffer: {e}")

    def _background_fsync(self):
        """
        The background I/O thread that flushes the OS buffer to disk once per second.
        """

        while not self._shutdown_flag:
            time.sleep(1.0)
            # Only hit the metal if the drivetrain is currently set to 'everysec'
            if (
                self.appendfsync == "everysec"
                and getattr(self, "aof_file", None)
                and not self.aof_file.closed
            ):
                try:
                    self.aof_file.flush()
                    os.fsync(self.aof_file.fileno())
                except OSError:
                    pass

    def _log_operation(self, *tokens):
        """
        Appends the raw command to AOF based on the active appendfsync policy.
        """

        if getattr(self, "_is_recovering", False):
            return

        if not tokens or not tokens[0]:
            return

        command_string = " ".join(str(t) for t in tokens)
        if not command_string:
            return

        self.aof_file.write(command_string + "\n")

        # Only block the main Python thread if the user demands absolute safety
        if self.appendfsync == "always":
            self.aof_file.flush()
            try:
                os.fsync(self.aof_file.fileno())
            except OSError:
                pass

    def _evict_if_expired(self, key: str) -> bool:
        """Helper to passively check and evict an expired key. Returns True if evicted."""
        if key in self._expires:
            if time.time() >= self._expires[key]:
                del self._data[key]
                del self._expires[key]
                self._lru_invalidate(key)  # keep cache coherent
                return True
        return False

    def _evict_all_expired(self) -> None:
        """The Active Sweeper: Scans the entire engine for expired keys."""
        current_time = time.time()
        keys_to_delete = []

        for key, exp_time in self._expires.items():
            if current_time > exp_time:
                keys_to_delete.append(key)

        for key in keys_to_delete:
            if key in self._data:
                del self._data[key]
            if key in self._expires:
                del self._expires[key]
            self._lru_invalidate(key)
            self._log_operation("DEL", key)

    # ------------------------------------------------------------------
    # LRU ENGINE INTERNALS (Upgraded to True Global Eviction)
    # ------------------------------------------------------------------

    def _touch_read(self, key: str) -> None:
        """Tracks a read access. Bumps to MRU if found, updates telemetry."""
        if key in self._lru_tracker:
            self._lru_tracker.move_to_end(key)
            self._lru_hits += 1
        else:
            self._lru_misses += 1

    def _touch_write(self, key: str) -> None:
        """Tracks a write. If the engine hits the redline, executes the oldest key."""
        if key in self._lru_tracker:
            self._lru_tracker.move_to_end(key)
        else:
            self._lru_tracker[key] = None

        # RUTHLESS OOM PREVENTION
        while len(self._lru_tracker) > self._lru_maxsize:
            oldest_key, _ = self._lru_tracker.popitem(last=False)  # pop from head

            # Kill the actual data
            if oldest_key in self._data:
                del self._data[oldest_key]
                self._expires.pop(oldest_key, None)
                self._log_operation("DEL", oldest_key)
                if self.debug_mode:
                    print(f"⚠️ [OOM PREVENTION] Evicted '{oldest_key}' to free up RAM.")

    def _lru_invalidate(self, key: str) -> None:
        """Removes a single key from the tracker (call on explicitly delete/expire)."""
        self._lru_tracker.pop(key, None)

    def _lru_clear(self) -> None:
        """Wipes the entire cache tracker (call on FLUSHALL)."""
        self._lru_tracker.clear()

    def lru_stats(self) -> dict:
        """Returns  brilliant cache telemetry."""
        total = self._lru_hits + self._lru_misses
        hit_rate = (self._lru_hits / total * 100) if total else 0.0
        return {
            "hits": self._lru_hits,
            "misses": self._lru_misses,
            "hit_rate_pct": round(hit_rate, 2),
            "tracked_keys": len(self._lru_tracker),
            "max_size": self._lru_maxsize,
        }

    def _log_slow_command(self, tokens: list, duration_us: int):
        """
        Checks if a command exceeded the execution threshold and logs it.
        """

        if self.slowlog_slower_than < 0:
            return  # Slowlog is disabled if threshold is negative

        if duration_us >= self.slowlog_slower_than:
            self.slowblog_id_counter += 1
            entry = [
                self.slowblog_id_counter,
                int(time.time()),
                duration_us,
                [str(t) for t in tokens],
            ]
            self._slowlog_queue.append(entry)

    def slowlog_get(self, count: Optional[int] = None) -> list:
        """
        Retuns the most recent slow log queries
        """

        logs = list(self._slowlog_queue)
        logs.reverse()

        if count is not None and count >= 0:
            return logs[:count]

        return logs

    def slowlog_len(self) -> int:
        """
        Returns the total number of items currently in slowlog
        """

        return len(self._slowlog_queue)

    def slowlog_reset(self) -> None:
        """
        Clears all slowlog entries
        """

        self._slowlog_queue.clear()

    # ------------------------------------------------------------------
    # COMMANDS (Wired into the LRU Tracker globally)
    # ------------------------------------------------------------------

    def set(self, key: str, value: str) -> None:
        self._log_operation("SET", key, value)
        self._data[key] = value
        if key in self._expires:
            del self._expires[key]
        self._touch_write(key)

    def get(self, key: str) -> Optional[str]:
        if self.debug_mode:
            print(f"\n🕵️  DEBUG REPL: Engine asked for key -> '{key}'")

        if self._evict_if_expired(key):
            self._touch_read(key)  # will register as a miss
            return None

        self._touch_read(key)
        return self._data.get(key)

    def delete(self, key: str) -> int:
        self._evict_if_expired(key)
        if key in self._data:
            self._log_operation("DEL", key)
            del self._data[key]
            self._expires.pop(key, None)
            self._lru_invalidate(key)
            return 1
        return 0

    def exists(self, key: str) -> int:
        self._evict_if_expired(key)
        self._touch_read(key)
        return 1 if key in self._data else 0

    def set_expire(self, key: str, seconds: int) -> int:
        if self._evict_if_expired(key) or key not in self._data:
            return 0
        absolute_expire_time = time.time() + seconds
        self._log_operation("EXPIREAT", key, absolute_expire_time)
        self._expires[key] = absolute_expire_time
        return 1

    def keys(self) -> dict:
        self._evict_all_expired()
        result = {}
        for k, val in self._data.items():
            if k in self._expires and self._expires[k] < time.time():
                continue

            if isinstance(val, deque):
                k_type, k_len = "list", str(len(val))
            elif isinstance(val, set):
                k_type, k_len = "set", str(len(val))
            elif isinstance(val, dict):
                k_type, k_len = "hash", str(len(val))
            elif isinstance(val, SkipList):
                k_type, k_len = "zset", str(len(val))
            else:
                k_type, k_len = "string", str(len(str(val)))

            ttl = int(self._expires[k] - time.time()) if k in self._expires else -1
            result[k] = {"type": k_type, "ttl": ttl, "length": k_len}
        return result

    def ttl(self, key: str) -> int:
        self._evict_if_expired(key)
        if key not in self._data:
            return -2
        if key not in self._expires:
            return -1
        return int(self._expires[key] - time.time())

    def flushall(self) -> None:
        self._log_operation("FLUSHALL")
        self._data.clear()
        self._expires.clear()
        self._lru_clear()

    def save(self):
        """
        Safely flushes memory to a snapshot file using an atomic swap.
        """
        main_file = "kedis.snapshot"
        temp_file = "kedis.snapshot.tmp"

        try:
            # 🚀 FIX: Use the extracted snapshot state method
            safe_state = self.get_snapshot_state()

            # 1. Write to a temporary file first
            with open(temp_file, "w") as f:
                json.dump(safe_state, f)

                f.flush()
                os.fsync(f.fileno())

            # 🚀 Issue 10 FIX: Added atomic swap
            # 3. Atomically swap the temp file over the main file
            os.replace(temp_file, main_file)
            return True

        except Exception as e:
            # Cleanup the debris if the write exploded mid-flight
            if os.path.exists(temp_file):
                os.remove(temp_file)
            print(f"SAVE ERROR: {e}")
            return False

    def load(self, filename: str = "kedis.snapshot") -> None:
        """
        Rebuilds the memory map and restores complex data structures from disk.
        """
        if os.path.exists(filename):
            try:
                with open(filename, "r") as f:
                    state = json.load(f)
                    # 🚀 FIX: Use the extracted restoration method
                    self.restore_snapshot_state(state)
            except Exception as e:
                print(f"DEBUG: Load failed - {e}")

    def recover_from_aof(self):
        if not os.path.exists(self.aof_filename):
            return

        with open(self.aof_filename, "r") as f:
            for line in f:
                tokens = line.strip().split()
                if not tokens:
                    continue
                cmd = tokens[0].upper()

                if cmd == "SET" and len(tokens) >= 3:
                    self._data[tokens[1]] = " ".join(tokens[2:])
                    self._expires.pop(tokens[1], None)
                    self._touch_write(tokens[1])
                elif cmd == "DEL" and len(tokens) >= 2:
                    self._data.pop(tokens[1], None)
                    self._expires.pop(tokens[1], None)
                    self._lru_invalidate(tokens[1])
                elif cmd == "EXPIREAT" and len(tokens) >= 3:
                    self._expires[tokens[1]] = float(tokens[2])
                elif cmd == "FLUSHALL":
                    self.flushall()
                elif cmd == "LPUSH" and len(tokens) >= 3:
                    key = tokens[1]
                    if key not in self._data:
                        self._data[key] = deque()
                    for val in tokens[2:]:
                        self._data[key].appendleft(val)
                    self._touch_write(key)
                elif cmd == "RPUSH" and len(tokens) >= 3:
                    key = tokens[1]
                    if key not in self._data:
                        self._data[key] = deque()
                    self._data[key].extend(tokens[2:])
                    self._touch_write(key)
                elif cmd == "LPOP" and len(tokens) >= 2:
                    key = tokens[1]
                    if (
                        key in self._data
                        and isinstance(self._data[key], deque)
                        and self._data[key]
                    ):
                        self._data[key].popleft()
                        self._touch_write(key)
                        if not self._data[key]:
                            del self._data[key]
                            self._lru_invalidate(key)
                elif cmd == "RPOP" and len(tokens) >= 2:
                    key = tokens[1]
                    if (
                        key in self._data
                        and isinstance(self._data[key], deque)
                        and self._data[key]
                    ):
                        self._data[key].pop()
                        self._touch_write(key)
                        if not self._data[key]:
                            del self._data[key]
                            self._lru_invalidate(key)
                elif cmd == "SADD" and len(tokens) >= 3:
                    key = tokens[1]
                    if key not in self._data:
                        self._data[key] = set()
                    self._data[key].update(tokens[2:])
                    self._touch_write(key)
                elif cmd == "SREM" and len(tokens) >= 3:
                    key = tokens[1]
                    if key in self._data and isinstance(self._data[key], set):
                        self._data[key].difference_update(tokens[2:])
                        self._touch_write(key)
                        if not self._data[key]:
                            del self._data[key]
                            self._lru_invalidate(key)
                elif cmd == "HSET" and len(tokens) >= 4:
                    key = tokens[1]
                    if key not in self._data:
                        self._data[key] = {}
                    self._data[key][tokens[2]] = " ".join(tokens[3:])
                    self._touch_write(key)
                elif cmd == "ZADD" and len(tokens) >= 4:
                    key = tokens[1]
                    if key not in self._data:
                        self._data[key] = SkipList()
                    self._data[key].insert(float(tokens[2]), " ".join(tokens[3:]))
                    self._touch_write(key)

    def compact_aof(self):
        """Compacts the AOF size down to only active and living keys"""
        temp_file = f"temp_{self.aof_filename}"
        try:
            with open(temp_file, "w") as f:
                for key, value in self._data.items():
                    if isinstance(value, deque):
                        f.write(f"RPUSH {key} {' '.join(value)}\n")
                    elif isinstance(value, set):
                        f.write(f"SADD {key} {' '.join(value)}\n")
                    elif isinstance(value, dict):
                        for h_field, h_val in value.items():
                            f.write(f"HSET {key} {h_field} {h_val}\n")
                    elif isinstance(value, SkipList):
                        flat_list = value.get_range(0, -1, withscores=True)
                        for i in range(0, len(flat_list), 2):
                            mem = flat_list[i]
                            scr = flat_list[i + 1]
                            f.write(f"ZADD {key} {scr} {mem}\n")
                    else:
                        f.write(f"SET {key} {value}\n")

                for key, exp_time in self._expires.items():
                    if exp_time > time.time():
                        f.write(f"EXPIREAT {key} {exp_time}\n")

            if os.name == "nt":
                if hasattr(self, "aof_file") and not self.aof_file.closed:
                    self.aof_file.close()
                time.sleep(0.1)
                if os.path.exists(self.aof_filename):
                    os.remove(self.aof_filename)
                os.rename(temp_file, self.aof_filename)
                self.aof_file = open(self.aof_filename, "a")
            else:
                os.replace(temp_file, self.aof_filename)
                if hasattr(self, "aof_file") and not self.aof_file.closed:
                    self.aof_file.close()
                self.aof_file = open(self.aof_filename, "a")
            return True
        except Exception as e:
            if os.path.exists(temp_file):
                os.remove(temp_file)
            print(f"Compaction failed: {e}")
            return False

    def _load_snapshot(self):
        """
        Restores database state from atomic snapshot file on boot
        """
        main_file = "kedis.snapshot"

        if os.path.exists(main_file):
            try:
                # 🚀 FIX: Delegate to the new load method to safely unpack envelopes on startup
                self.load(main_file)
                print(
                    f"✓ Snapshot recovery successful: {len(self._data)} keys restored to RAM."
                )

            except Exception as e:
                print(f"❌ CRITICAL: Failed to load snapshot: {e}")
                self._data = {}  # Failsafe to an empty engine if the file is completely destroyed

    # LIST OPERATIONS
    def lpush(self, key: str, *values: str) -> int:
        self._evict_if_expired(key)
        if key in self._data and not isinstance(self._data[key], deque):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )
        if key not in self._data:
            self._data[key] = deque()

        for val in values:
            self._data[key].appendleft(val)

        self._log_operation("LPUSH", key, *values)
        self._touch_write(key)
        return len(self._data[key])

    def lrange(self, key: str, start: int, stop: int) -> list:
        self._evict_if_expired(key)
        self._touch_read(key)
        if key not in self._data:
            return []
        if not isinstance(self._data[key], deque):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        target_list = self._data[key]
        actual_stop = None if stop == -1 else stop + 1
        return list(itertools.islice(target_list, start, actual_stop))

    def rpush(self, key: str, *values: str) -> int:
        self._evict_if_expired(key)
        if key in self._data and not isinstance(self._data[key], deque):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )
        if key not in self._data:
            self._data[key] = deque()

        for val in values:
            self._data[key].append(val)

        self._log_operation("RPUSH", key, *values)
        self._touch_write(key)
        return len(self._data[key])

    def lpop(self, key: str) -> Optional[str]:
        self._evict_if_expired(key)
        if key not in self._data:
            return None
        if not isinstance(self._data[key], deque):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )
        if not self._data[key]:
            return None

        popped_value = self._data[key].popleft()
        self._log_operation("LPOP", key)
        self._touch_write(key)

        if len(self._data[key]) == 0:
            del self._data[key]
            self._expires.pop(key, None)
            self._lru_invalidate(key)
        return popped_value

    def rpop(self, key: str) -> Optional[str]:
        self._evict_if_expired(key)
        if key not in self._data:
            return None
        if not isinstance(self._data[key], deque):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )
        if not self._data[key]:
            return None

        popped_value = self._data[key].pop()
        self._log_operation("RPOP", key)
        self._touch_write(key)

        if len(self._data[key]) == 0:
            del self._data[key]
            self._expires.pop(key, None)
            self._lru_invalidate(key)
        return popped_value

    # SET OPERATIONS
    def sadd(self, key: str, *members: str) -> int:
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
        self._touch_write(key)
        return added_count

    def smembers(self, key: str) -> list[str]:
        self._evict_if_expired(key)
        self._touch_read(key)
        if key not in self._data:
            return []
        if not isinstance(self._data[key], set):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )
        return list(self._data[key])

    def srem(self, key: str, *members: str) -> int:
        self._evict_if_expired(key)
        if key not in self._data:
            return 0
        if not isinstance(self._data[key], set):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        initial_len = len(self._data[key])
        self._data[key].difference_update(members)
        removed_count = initial_len - len(self._data[key])

        if removed_count > 0:
            self._log_operation("SREM", key, *members)
            self._touch_write(key)

        if len(self._data[key]) == 0:
            del self._data[key]
            self._expires.pop(key, None)
            self._lru_invalidate(key)
        return removed_count

    # HASH OPERATIONS
    def hset(self, key: str, field: str, value: str) -> int:
        self._evict_if_expired(key)
        if key in self._data and not isinstance(self._data[key], dict):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )
        if key not in self._data:
            self._data[key] = {}

        is_new = 1 if field not in self._data[key] else 0
        self._data[key][field] = value

        self._log_operation("HSET", key, field, value)
        self._touch_write(key)
        return is_new

    def hget(self, key: str, field: str) -> Optional[str]:
        self._evict_if_expired(key)
        self._touch_read(key)
        if key not in self._data or not isinstance(self._data[key], dict):
            return None
        return self._data[key].get(field)

    def hgetall(self, key: str) -> dict:
        self._evict_if_expired(key)
        self._touch_read(key)
        if key not in self._data:
            return {}
        if not isinstance(self._data[key], dict):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )
        return self._data[key]

    # ZSET OPERATIONS
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
        self._touch_write(key)
        return is_new

    def zrange(self, key: str, start: int, stop: int, withscores: bool = False) -> list:
        self._evict_if_expired(key)
        self._touch_read(key)
        if key not in self._data:
            return []
        if not isinstance(self._data[key], SkipList):
            raise TypeError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )
        return self._data[key].get_range(start, stop, withscores)

    def type_of(self, key: str) -> str:
        self._evict_if_expired(key)
        self._touch_read(key)
        if key not in self._data:
            return "None"
        val = self._data[key]
        if isinstance(val, deque):
            return "List"
        elif isinstance(val, set):
            return "Set"
        elif isinstance(val, dict):
            return "hash"
        elif isinstance(val, SkipList):
            return "zset"
        else:
            return "string"

    def get_engine_stats(self) -> dict[str, Any]:
        self._evict_all_expired()
        stats = {
            "total_keys": len(self._data),
            "string_chars": 0,
            "list_items": 0,
            "set_members": 0,
            "hash_fields": 0,
            "zset_nodes": 0,
        }
        for val in self._data.values():
            if isinstance(val, deque):
                stats["list_items"] += len(val)
            elif isinstance(val, set):
                stats["set_members"] += len(val)
            elif isinstance(val, dict):
                stats["hash_fields"] += len(val)
            elif type(val).__name__ == "SkipList":
                stats["zset_nodes"] += len(val)
            else:
                stats["string_chars"] += len(str(val))

        stats["lru_cache"] = self.lru_stats()
        return stats
