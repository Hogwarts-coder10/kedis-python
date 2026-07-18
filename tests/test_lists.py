import os

from store import KedisStore


def run_diagnostics():
    print("🔧 Booting Kedis Engine for Drivetrain Diagnostics...\n")

    # Booting with a temporary AOF file so we don't pollute your main database
    store = KedisStore(aof_filename="test_kedis.aof")
    store.flushall()

    try:
        # 1. Test Push Mechanics
        print("⏩ Testing LPUSH & RPUSH...")
        store.rpush("race_cars", "ferrari")  # [ferrari]
        store.rpush("race_cars", "mclaren")  # [ferrari, mclaren]
        store.lpush("race_cars", "redbull")  # [redbull, ferrari, mclaren]
        print("✓ Pushes successful.")

        # 2. Test Range Mechanics (Itertools slicing)
        print("\n⏩ Testing LRANGE (itertools.islice)...")
        full_list = store.lrange("race_cars", 0, -1)
        print(f"  Current List: {full_list}")
        assert full_list == ["redbull", "ferrari", "mclaren"], (
            "LRANGE failed to slice correctly!"
        )

        partial_list = store.lrange("race_cars", 0, 1)
        print(f"  Partial Slice (0 to 1): {partial_list}")
        assert partial_list == ["redbull", "ferrari"], "LRANGE partial slice failed!"
        print("✓ Range slicing successful.")

        # 3. Test Type Checking
        print("\n⏩ Testing TYPE_OF...")
        key_type = store.type_of("race_cars")
        print(f"  Type detected: {key_type}")
        assert key_type == "List", "TYPE_OF failed to recognize deque as a List!"
        print("✓ Type checking successful.")

        # 4. Test Pop Mechanics
        print("\n⏩ Testing LPOP & RPOP...")
        left_pop = store.lpop("race_cars")
        print(f"  LPOP: {left_pop}")
        assert left_pop == "redbull", "LPOP popped the wrong value!"

        right_pop = store.rpop("race_cars")
        print(f"  RPOP: {right_pop}")
        assert right_pop == "mclaren", "RPOP popped the wrong value!"

        remaining = store.lrange("race_cars", 0, -1)
        print(f"  Remaining List: {remaining}")
        assert remaining == ["ferrari"], "List state corrupted after pops!"
        print("✓ Pop mechanics successful.")

        print(
            "\n🏁 ALL DIAGNOSTICS PASSED! The C-extension deque is firing on all cylinders."
        )

    except AssertionError as e:
        print(f"\n❌ CRASH: {e}")
    except Exception as e:
        print(f"\n❌ ENGINE FAULT: {e}")
    finally:
        # Clean up the test tracks
        store.shutdown()
        if os.path.exists("test_kedis.aof"):
            os.remove("test_kedis.aof")


if __name__ == "__main__":
    run_diagnostics()
