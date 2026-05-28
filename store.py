import time
import os
import json
from typing import Optional

class KedisStore:
    def __init__(self):
        # The primary hash map for storing key-value pairs
        self._data: dict[str, str] = {}
        # A secondary hash map to track expiration timestamps (Unix time)
        self._expires: dict[str, float] = {}

    def _evict_if_expired(self, key: str) -> bool:
        """Helper to passively check and evict an expired key. Returns True if evicted."""
        if key in self._expires:
            # If the current time has passed the expiration time
            if time.time() >= self._expires[key]:
                del self._data[key]
                del self._expires[key]
                return True
        return False

    def set(self, key: str, value: str) -> None:
        """Sets a key to hold a string value."""
        self._data[key] = value
        # If the key is overwritten, any previous TTL should be cleared
        if key in self._expires:
            del self._expires[key]


    def get(self, key: str) -> Optional[str]:
        """Gets the value of a key, returning None if it doesn't exist (or expired)."""
        self._evict_if_expired(key)
        return self._data.get(key)

    def delete(self, key: str) -> int:
        """Removes the specified key. Returns 1 if deleted, 0 if not found."""
        self._evict_if_expired(key)
        if key in self._data:
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

        # time.time() gets the current epoch time in seconds
        self._expires[key] = time.time() + seconds
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

    def ttl(self,key : str) -> int:
        """
        Returns the remaining  time to live of a key in seconds
        """

        self._evict_if_expired(key)
        if key not in self._data: # key doesn't exists
            return -2

        if key not in self._expires: # key is there but no expiry time
            return -1

        # Key has expiry time , hence calcuate seconds
        remaining_secs = int(self._expires[key] - time.time())

        return remaining_secs

    def flushall(self) -> None:
        """
        Removes all keys and expirations,
        completely resetting the database."
        """

        self._data.clear()
        self._expires.clear()

    def save(self,filename: str = "dump.json") -> bool:
        """
        Snapshots the current memory state to a JSON file
        """

        try:
            # Bundling states to dictionaries
            state = {
                "data" : self._data,
                "expires" : self._expires
            }

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
