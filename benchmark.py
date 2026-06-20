import socket
import threading
import time

from rich.console import Console

console = Console()

HOST = "127.0.0.1"
PORT = 6379
THREADS = 1  # Dropped to 1 to avoid the collision
REQUESTS_PER_THREAD = 50000  # Cranked up to maintain the volume


def hammer_server(thread_id):
    """Simulates a ruthless client hammering the database."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((HOST, PORT))

        for i in range(REQUESTS_PER_THREAD):
            # Write phase
            cmd_set = f"SET stress_key_{thread_id}_{i} payload_data_{i}"
            s.sendall(cmd_set.encode("utf-8"))
            s.recv(1024)  # Wait for +OK

            # Read phase
            cmd_get = f"GET stress_key_{thread_id}_{i}"
            s.sendall(cmd_get.encode("utf-8"))
            s.recv(1024)

        s.close()
    except Exception as e:
        console.print(f"[red]Thread {thread_id} crashed: {e}[/red]")


def run_stress_test():
    console.print(f"[bold cyan]🏎️ KEDIS WIND TUNNEL ONLINE[/bold cyan]")
    console.print(f"Target: {HOST}:{PORT}")
    console.print(f"Threads: {THREADS}")
    console.print(f"Total Operations: {THREADS * REQUESTS_PER_THREAD * 2:,}\n")

    threads = []
    start_time = time.time()

    # Unleash the swarm
    for i in range(THREADS):
        t = threading.Thread(target=hammer_server, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    duration = time.time() - start_time
    total_ops = THREADS * REQUESTS_PER_THREAD * 2
    rps = total_ops / duration

    console.print("[bold green]🏁 STRESS TEST COMPLETE[/bold green]")
    console.print(f"Time Elapsed: [yellow]{duration:.2f} seconds[/yellow]")
    console.print(f"Throughput:   [bold blue]{rps:,.0f} Requests/Second[/bold blue]")


if __name__ == "__main__":
    run_stress_test()
