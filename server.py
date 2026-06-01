import socket
import threading

from commands import CommandHandler
from parser import CommandParser
from store import KedisStore

HOST = "127.0.0.1"
PORT = 6379


# 1. Added 'store' back to the parameters for your DEBUG toggle!
def handle_client(conn, addr, handler, store):
    """
    Handles an individual client connection.
    """
    print(f"🔌 Client connected from {addr}")

    with conn:
        while True:
            try:
                data = conn.recv(1024)
                if not data:
                    break  # Client dropped the connection

                raw_command = data.decode("utf-8").strip()
                if not raw_command:
                    continue

                tokens = CommandParser.parse(raw_command)

                if tokens and tokens[0] == "ERROR":
                    conn.sendall(tokens[1].encode("utf-8") + b"\n")
                    continue

                if not tokens:
                    continue

                cmd = tokens[0].upper()

                # The Remote Debug Intercept
                if cmd == "DEBUG":
                    store.debug_mode = not getattr(store, "debug_mode", False)
                    conn.sendall(b"OK\n")
                    continue

                response = handler.execute(tokens)
                conn.sendall(str(response).encode("utf-8") + b"\n")

            except Exception as e:
                error_msg = f"(error) ERR {str(e)}\n"
                conn.sendall(error_msg.encode("utf-8"))
                break

    print(f"🔌 Connection closed: {addr}")


def boot_server():
    print("🏁 Booting Kedis TCP Server...")
    print("Press Ctrl+C to gracefully shut down the engine.\n")

    store = KedisStore()
    handler = CommandHandler(store)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((HOST, PORT))

            # --- THE FIX ---
            s.listen()
            s.settimeout(1.0)  # Wake up every 1 second to check for Ctrl+C
            # ---------------

            print(f"🚀 Kedis Engine running and listening on {HOST}:{PORT}")

            while True:
                try:
                    conn, addr = s.accept()

                    # Spin up the daemon thread for the client
                    thread = threading.Thread(
                        target=handle_client,
                        args=(conn, addr, handler, store),
                        daemon=True,
                    )
                    thread.start()

                except socket.timeout:
                    # Nobody connected in the last 1 second.
                    # Silently loop back to the top and check for Ctrl+C.
                    continue

    except KeyboardInterrupt:
        print("\n🛑 Main kill switch hit. Shutting down the Kedis Engine...")


if __name__ == "__main__":
    boot_server()
