# ⚡ Benchmarks & Telemetry

Kedis is engineered to squeeze the maximum possible throughput out of a single Python process. To prove it, the engine includes built-in, microsecond-accurate diagnostic tools that allow you to monitor performance in real-time.

## The Event Loop Heartbeat

Because Kedis runs on a single-threaded `asyncio` event loop, blocking operations are the enemy. The engine runs a continuous internal heartbeat sensor—the **Latency Monitor**—which pulses every 100 milliseconds. 

If a command (or a disk write) takes too long and blocks the thread, the heartbeat will be delayed. The engine records this delta, allowing administrators to see exactly how much lag is building up in the system.

## Telemetry Commands

You can track engine performance live using the following KESP commands:

### `LATENCY DOCTOR`
Generates a real-time health report of the engine's internal queues.
* **Event Loop Lag:** Displays the current microsecond delay of the heartbeat sensor. (A healthy engine should remain under `1.00ms`).
* **Active Connections:** The number of clients currently routed through the TCP server.
* **Memory State:** Key counts and current database version mutations.

### `SLOWLOG LEN`
Kedis automatically tracks any command that exceeds the strict execution time threshold (default: `10,000 microseconds`). This command returns the number of queries that breached that limit, helping you identify performance bottlenecks or heavy payloads.

### `INFO`
Provides a snapshot of the engine's current state, including the chosen I/O Drivetrain policy (`everysec` vs `always`) and total memory consumption.

## TPS (Transactions Per Second) Testing

To truly stress-test the engine, you can run a network blast test. 

Because Kedis pipelines connections and bypasses HTTP overhead via the Surge Tank, it can handle massive concurrent command floods. When testing TPS, remember that the bottleneck will typically be the **I/O Drivetrain**. 

* **With `appendfsync always`:** TPS is strictly limited by your physical SSD write speed, as the engine waits for the OS to finalize the write on every single command.
* **With `appendfsync everysec`:** TPS is uncapped. The engine will process network commands as fast as the Python dictionary can mutate them, safely batching the disk writes to the background thread once per second.

*(Note: To run a custom benchmark, spin up a Python script that uses `asyncio.gather()` to fire 10,000 parallel `SET` commands over the socket, and measure the round-trip time).*
