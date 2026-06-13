import socket


class NetworkManager:
    def __init__(self, host="127.0.0.1", port=6379):
        self.host = host
        self.port = port
        self.sock = None

    def connect(self):
        """
        Attempts to connect to TCP server.
        """

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        return self.sock

    def disconnect(self):
        """
        Safely severs the TCP connection.
        """

        if self.sock:
            self.sock.close()
            self.sock = None

    def ping_radar(self) -> bool:
        """
        Fires a rapid 50ms ping,
        to check if the server is alive.
        """

        radar = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        radar.settimeout(0.05)

        try:
            radar.connect((self.host, self.port))
            radar.close()
            return True

        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    def send_command(self, raw_input: str) -> str:
        """
        Transmits a command to the server
        and returns the decoded string.
        """

        if not self.sock:
            raise OSError("Not connected to server.")

        self.sock.sendall(raw_input.encode("utf-8"))
        data = self.sock.recv(1024)

        if not data:
            raise OSError("Server silently dropped connection")

        return data.decode("utf-8").strip()
