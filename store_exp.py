from collections import OrderedDict


class ExperimentalLRUStore:
    def __init__(self, max_keys=3):
        # The Redline is artificially set to 3 for quick testing
        self.data = OrderedDict()
        self.max_keys = max_keys

    def _evict_if_needed(self):
        """Checks the redline and drops the oldest, unread key."""
        while len(self.data) >= self.max_keys:
            evicted_key, _ = self.data.popitem(last=False)
            print(
                f"⚠️  [EVICTION] Redline hit! Dropped least recently used key: '{evicted_key}'"
            )

    def set(self, key, value):
        if key not in self.data:
            self._evict_if_needed()

        self.data[key] = value
        # Instantly teleport to the front of the line (Most Recently Used)
        self.data.move_to_end(key)
        print(f"💾 [SET] '{key}' = {value} (Moved to front of queue)")
        return True

    def get(self, key):
        if key not in self.data:
            print(f"❌ [GET] '{key}' not found (nil)")
            return None

        # Reading a key proves it is active! Teleport it back to the front.
        self.data.move_to_end(key)
        print(f"📖 [GET] '{key}' = {self.data[key]} (Promoted to front of queue)")
        return self.data[key]

    def dump_cache(self):
        """Helper function to visualize the internal memory state."""
        print(
            f"📊 [STATE] Current Cache (Oldest -> Newest): {list(self.data.keys())}\n"
        )


# ==========================================
# THE TELEMETRY TEST SCRIPT
# ==========================================
if __name__ == "__main__":
    print("🏎️  IGNITING LRU TEST BENCH...\n")
    store = ExperimentalLRUStore(max_keys=3)

    # 1. Fill the track to absolute capacity
    store.set("lap1", "1m 32s")
    store.set("lap2", "1m 31s")
    store.set("lap3", "1m 30s")
    store.dump_cache()

    # 2. Read the oldest key (lap1) to save it from the chopping block
    print("-> Simulating a read on the oldest key to save it from eviction...")
    store.get("lap1")
    store.dump_cache()

    # 3. Push a new key to force the engine over the redline
    print("-> Pushing a new key to force an eviction...")
    store.set("lap4", "1m 29s")
    store.dump_cache()

    # 4. Attempt to read the key that should have been killed (lap2)
    print("-> Attempting to read the evicted key...")
    store.get("lap2")
