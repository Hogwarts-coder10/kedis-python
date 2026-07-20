import socket

# Crafting two KESP commands in one single string payload
# Command 1: SET pipe test
# Command 2: GET pipe
payload = b"A3\nS3\nSET\nS4\npipe\nS4\ntest\nA2\nS3\nGET\nS4\npipe\n"

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect(("127.0.0.1", 6379))

# Fire them both simultaneously in one TCP packet
s.sendall(payload)

# Read the two responses back
print("SERVER RESPONSE:")
print(s.recv(4096).decode())
s.close()
