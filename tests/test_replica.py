import socket


def send_handshake():
    # Connect to the Follower on 6380
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", 6381))

    # Send the raw KESP array for REPLICAOF 127.0.0.1 6379
    # *3\r\n$9\r\nREPLICAOF\r\n$9\r\n127.0.0.1\r\n$4\r\n6379\r\n
    payload = b"\r\n\r\nREPLICAOF\r\n\r\n127.0.0.1\r\n\r\n6379\r\n"

    print("🚀 Firing replication command to Follower...")
    client.sendall(payload)

    response = client.recv(1024)
    print(f"📥 Response from Follower: {response.decode()}")

    client.close()


if __name__ == "__main__":
    send_handshake()
