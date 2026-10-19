import select
import socket

from .parser import IncompleteReply, KESPDecoder, ReplyProtocolError
from .textcodec import to_bytes, to_text

RECV_CHUNK = 64 * 1024
MAX_REPLY_BYTES = 1024 * 1024 * 1024  # most bytes buffered while waiting for one reply


class NetworkManager:
    def __init__(self, host="127.0.0.1", port=6379):
        self.host = host
        self.port = port
        self.sock = None
        self._rbuf = bytearray()  # bytes received but not yet returned

    def connect(self):
        """
        Attempts to connect to TCP server.
        """

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._rbuf.clear()
        self.sock.connect((self.host, self.port))
        return self.sock

    def disconnect(self):
        """
        Safely severs the TCP connection.
        """

        if self.sock:
            self.sock.close()
            self.sock = None
        self._rbuf.clear()

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

    def _sock_has_data(self) -> bool:
        """True when more bytes can be read without blocking."""
        try:
            readable, _, _ = select.select([self.sock], [], [], 0)
        except (OSError, ValueError, TypeError):  # e.g. a socket stand-in
            return False
        return bool(readable)

    def read_reply(self) -> bytes:
        """
        Blocks until one COMPLETE KESP reply has arrived and returns its raw
        bytes. A reply bigger than one TCP segment (a long XRANGE, KEYS, ...)
        is stitched together; bytes belonging to a later reply stay buffered.
        """

        if not self.sock:
            raise OSError("Not connected to server.")

        need = 0  # buffer length at which the pending reply can progress
        while True:
            if self._rbuf and len(self._rbuf) >= need:
                try:
                    _, end = KESPDecoder.decode(self._rbuf)
                except IncompleteReply as incomplete:
                    need = incomplete.need
                except ReplyProtocolError:
                    # A garbled stream can't be resynchronised, and the next
                    # reply would be read from the middle of this one: drop
                    # the connection so the caller reconnects cleanly.
                    self.disconnect()
                    raise
                else:
                    reply = bytes(self._rbuf[:end])
                    del self._rbuf[:end]
                    return reply

            # Drain everything already queued before re-parsing, so a huge
            # reply is not re-decoded from the start after every chunk.
            while True:
                chunk = self.sock.recv(RECV_CHUNK)
                if not chunk:
                    raise OSError("Server silently dropped connection")
                self._rbuf += chunk
                if len(self._rbuf) > MAX_REPLY_BYTES:
                    self.disconnect()  # nothing sane is this big; stop buffering
                    raise ReplyProtocolError("reply too large")
                if not self._sock_has_data():
                    break

    def send_command(self, raw_input: str) -> str:
        """
        Transmits a command to the server
        and returns the decoded string.
        """

        if not self.sock:
            raise OSError("Not connected to server.")

        self.sock.sendall(to_bytes(raw_input))
        return to_text(self.read_reply()).strip()
