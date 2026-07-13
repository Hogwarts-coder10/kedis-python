# 1. THE MAIN TRACK (Global Database)
global_store = {"points": 100}

# 2. THE PRIVATE WHITEBOARD (Client Session State)
in_transaction = False
tx_queue = []


def run_engine(cmd_string):
    global in_transaction, tx_queue

    # --- A. PROTOCOL CONTROL (The Guardrails) ---
    if cmd_string == "MULTI":
        in_transaction = True
        return "+OK (Switched to Queue Mode)"

    elif cmd_string == "DISCARD":
        in_transaction = False
        tx_queue = []  # Wipe the whiteboard
        return "+OK (Queue Trashed)"

    elif cmd_string == "EXEC":
        print("\n🏁 GREEN FLAG: Firing the Pipeline!")
        for queued_cmd in tx_queue:
            print(f" -> Executing: {queued_cmd}")
            # (In the real app, global_handler.execute() goes here)

        in_transaction = False
        tx_queue = []
        return "Transaction Complete\n"

    # --- B. DATA ROUTING (The Engine) ---
    if in_transaction:
        tx_queue.append(cmd_string)
        return f"+QUEUED '{cmd_string}'"
    else:
        print(f"⚡ IMMEDIATE EXECUTE: {cmd_string}")
        return "+OK"


# --- THE TEST DRIVE ---
print(run_engine("SET driver Max"))  # Runs immediately
print(run_engine("MULTI"))  # Flips the flag
print(run_engine("SET points 50"))  # Goes to queue
print(run_engine("INCR points"))  # Goes to queue
print(run_engine("EXEC"))  # Fires the queue!
