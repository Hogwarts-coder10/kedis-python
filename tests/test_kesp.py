import socket

# This is exactly how your future kesp-cli will talk to the engine
# Notice the intentional weird spacing and symbols in the value!
payload = b"A3\nS3\nSET\nS6\nmy_key\nS19\n> Binary Safe Test!\n"

client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client.connect(("127.0.0.1", 6379))

print(f"Sending KESP Payload: {payload}")
client.sendall(payload)

response = client.recv(1024)
print(f"Server Response: {response.decode('utf-8')}")
client.close()
