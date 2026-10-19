"""Client side: waiting for a big reply, and refusing a runaway one."""

import socket
import threading
import time

import pytest

from kedis_python import network
from kedis_python.network import NetworkManager
from kedis_python.parser import (
    IncompleteReply,
    KESPDecoder,
    KESPEncoder,
    ReplyProtocolError,
)

WHOLE = [
    b"N\n",
    b"I-123\n",
    b"+OK\r\n",
    b"Eboom\n",
    b"S11\nline1\nline2\n",
    b"S300\n" + b"x" * 300 + b"\n",
    b"A3\nS1\na\nA2\nI1\nN\nS40\n" + b"y" * 40 + b"\n",
]


# --- the hint -------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", WHOLE)
def test_the_hint_is_exact_and_never_skips_a_decodable_prefix(raw):
    for cut in range(len(raw)):
        with pytest.raises(IncompleteReply) as info:
            KESPDecoder.decode(raw[:cut])
        need = info.value.need
        assert cut < need <= len(raw), (cut, need)
        with pytest.raises(IncompleteReply):  # nothing decodes before `need`
            KESPDecoder.decode(raw[: need - 1])
    KESPDecoder.decode(raw)


def test_the_hint_for_a_half_received_string_is_the_end_of_that_string():
    head = b"A2\nS3\nabc\nS1000\n"
    with pytest.raises(IncompleteReply) as info:
        KESPDecoder.decode(head + b"x" * 10)
    assert info.value.need == len(head) + 1000 + 1


def test_incomplete_reply_still_works_as_a_plain_exception():
    with pytest.raises(IncompleteReply):
        raise IncompleteReply
    assert IncompleteReply().need == 0


# --- read_reply --------------------------------------------------------------------------------


def _feed(sock, data, chunk=65536, pause=0.001):
    def run():
        for i in range(0, len(data), chunk):
            sock.sendall(data[i : i + chunk])
            time.sleep(pause)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_a_big_reply_is_not_decoded_after_every_chunk(monkeypatch):
    ours, theirs = socket.socketpair()
    nm = NetworkManager()
    nm.sock = ours
    value = "z" * (4 * 1024 * 1024)
    wire = KESPEncoder.encode([value, "tail"])

    calls = []
    real = KESPDecoder.decode

    def counting(buf, *args, **kwargs):
        calls.append(len(buf))
        return real(buf, *args, **kwargs)

    monkeypatch.setattr(KESPDecoder, "decode", staticmethod(counting))
    sender = _feed(theirs, wire)
    try:
        reply = nm.read_reply()
    finally:
        sender.join(10)
        theirs.close()
        nm.disconnect()
    assert reply == wire
    # ~64 chunks arrive; without the hint each one costs a decode of the whole buffer
    assert len(calls) <= 6, len(calls)


def test_a_reply_larger_than_the_buffer_cap_is_cut_off(monkeypatch):
    monkeypatch.setattr(network, "MAX_REPLY_BYTES", 1000)
    ours, theirs = socket.socketpair()
    nm = NetworkManager()
    nm.sock = ours
    theirs.sendall(b"S5000\n" + b"x" * 3000)  # promises 5000, keeps coming
    try:
        with pytest.raises(ReplyProtocolError, match="too large"):
            nm.read_reply()
        assert nm.sock is None and not nm._rbuf
    finally:
        theirs.close()
        nm.disconnect()


def test_normal_replies_are_unaffected_by_the_cap():
    ours, theirs = socket.socketpair()
    nm = NetworkManager()
    nm.sock = ours
    try:
        theirs.sendall(b"+OK\nI5\n")
        assert nm.read_reply() == b"+OK\n"
        assert nm.read_reply() == b"I5\n"
    finally:
        theirs.close()
        nm.disconnect()
