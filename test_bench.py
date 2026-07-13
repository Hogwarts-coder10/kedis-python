from commands import CommandHandler
from store import KedisStore


def run_diagnostics():
    print("🔧 Mounting engine to the test bench...")

    # 1. Initialize the parts
    store = KedisStore()
    router = CommandHandler(store)

    # 2. Fire test telemetry directly into the execute method
    tests = [
        ["SET", "driver", "karthik"],
        ["GET", "driver"],
        ["EXISTS", "driver"],
        ["DEL", "driver"],
        ["GET", "driver"],
        ["TURBO", "boost"],  # Intentionally fake command
    ]

    print("\n📊 Running O(1) Dispatch Diagnostics:\n" + "-" * 35)

    for tokens in tests:
        # Join the tokens just for a pretty print label
        cmd_label = " ".join(tokens)

        # Fire the command!
        response = router.execute(tokens)

        print(f"Command: {cmd_label:<20} | Response: {response}")


if __name__ == "__main__":
    run_diagnostics()
