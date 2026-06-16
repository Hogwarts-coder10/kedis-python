import socket
import time


def send_cmd(cmd: str) -> str:
    """Connects to Kedis, fires a command, and returns the response."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", 6379))
        s.sendall(f"{cmd}\n".encode())
        response = s.recv(4096).decode().strip()
        s.close()
        return response
    except ConnectionRefusedError:
        return "ERROR: Is the Kedis server running?"


print("🏎️  Warming up the engine (FLUSHALL)...")
print(send_cmd("FLUSHALL"))

print("\n🔥 Firing 150 commands to intentionally redline the capacity (Limit: 128)...")
for i in range(1, 151):
    # We will mix data types to prove Global Eviction works on EVERYTHING
    if i % 3 == 0:
        send_cmd(f"LPUSH list_key_{i} valA valB")
    elif i % 2 == 0:
        send_cmd(f"HSET hash_key_{i} field1 val1")
    else:
        send_cmd(f"SET string_key_{i} some_data")

print("\n🎯 Forcing Cache Hits to test telemetry...")
# Reading keys that we know are still alive to bump their MRU
send_cmd("GET string_key_149")
send_cmd("LRANGE list_key_150 0 -1")

print("\n📊 Pulling Engine Telemetry (STATS)...")
# Assuming your server router maps the 'STATS' command to get_engine_stats()
stats_output = send_cmd("STATS")
print(stats_output)
