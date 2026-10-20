"""An error line the server writes must be one its own client decoder accepts.

Error text often echoes client input (an unknown command's name), so it can be
arbitrarily long. The encoder cuts it to the decoder's MAX_LINE, at a character
boundary, and ends it with "...".
"""

import asyncio
import io

import pytest
from rich.console import Console

from kedis_python.parser import KESPDecoder, KESPEncoder
from kedis_python.textcodec import to_bytes, to_text

LIMIT = KESPDecoder.MAX_LINE - 1  # most bytes of error text after the 'E'


def err(text: str) -> bytes:
    return KESPEncoder.encode("-ERR " + text)


def roundtrip(wire: bytes) -> str:
    value, end = KESPDecoder.decode(wire)  # strict: raises on a line over MAX_LINE
    assert end == len(wire)
    return value


# --- the cut ----------------------------------------------------------------------------------


def test_short_text_is_untouched():
    assert roundtrip(err("unknown command 'foo'")) == "unknown command 'foo'"


def test_text_exactly_at_the_limit_is_untouched():
    text = "x" * LIMIT
    assert roundtrip(err(text)) == text


def test_one_byte_over_the_limit_is_cut():
    got = roundtrip(err("x" * (LIMIT + 1)))
    assert got == "x" * (LIMIT - 3) + "..."
    assert len(to_bytes(got)) == LIMIT


@pytest.mark.parametrize("size", [LIMIT + 1, LIMIT * 2, 10 * LIMIT])
def test_any_oversize_text_comes_back_as_a_decodable_line_ending_in_dots(size):
    got = roundtrip(err("a" * size))
    assert got.endswith("...") and len(to_bytes(got)) <= LIMIT


def test_the_old_behaviour_is_gone_for_the_unknown_command_case():
    # This is the reply that used to fail with "reply line too long".
    got = roundtrip(err("unknown command '" + "x" * 100_000 + "'"))
    assert got.startswith("unknown command 'xxx") and got.endswith("...")


@pytest.mark.parametrize("char", ["é", "€", "😀"])  # 2-, 3- and 4-byte characters
@pytest.mark.parametrize("pad", range(0, 5))  # slide the cut through every offset
def test_the_cut_never_splits_a_character(char, pad):
    text = "a" * pad + char * 40_000
    got = roundtrip(err(text))
    body = got[:-3]
    assert got.endswith("...")
    # Strictly valid UTF-8, no replacement characters, and a true prefix.
    to_bytes(got).decode("utf-8")
    assert text.startswith(body)
    assert len(to_bytes(got)) <= LIMIT
    # No more than one character's worth was given up to stay on a boundary.
    assert len(to_bytes(got)) >= LIMIT - 4


def test_raw_bytes_carried_as_surrogates_survive_the_cut():
    text = to_text(b"\xff\xfe" * 40_000)  # not UTF-8: lone surrogates U+DCxx
    got = roundtrip(err(text))
    assert got.endswith("...") and len(to_bytes(got)) <= LIMIT
    assert text.startswith(got[:-3])


def test_a_cut_line_is_still_one_physical_line():
    wire = err("a\nb\r\n" + "z" * 100_000)
    assert wire.count(b"\n") == 1 and wire.endswith(b"\n")
    assert roundtrip(wire).startswith("a b  zzz")


def test_every_error_path_goes_through_the_cap():
    big = "y" * 100_000
    for data in ("-ERR " + big, "ERROR " + big):
        assert roundtrip(KESPEncoder.encode(data)).endswith("...")
    # The encoder's own fallback echoes a message too.
    assert roundtrip(KESPEncoder._error_line("Internal server error: " + big)).endswith(
        "..."
    )


# --- against a real server --------------------------------------------------------------------


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    yield server
    server.global_store.flushall()


def test_server_reply_to_a_huge_unknown_command_is_accepted_by_the_client(srv):
    name = b"x" * 100_000
    frame = b"A1\nS%d\n%s\n" % (len(name), name)

    async def scenario():
        server = await asyncio.start_server(srv.handle_connection, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(frame)
            await w.drain()
            line = await asyncio.wait_for(r.readline(), 10)
            w.close()
            return line
        finally:
            server.close()
            await asyncio.sleep(0.05)

    line = asyncio.run(scenario())
    value = roundtrip(line)
    assert "unknown command" in value and value.endswith("...")
