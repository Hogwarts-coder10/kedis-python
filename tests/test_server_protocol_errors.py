"""Wire-level behaviour for malformed or odd frames against the real async server."""

import asyncio
import io

import pytest
from rich.console import Console


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kedis_python import server

    server.console = Console(file=io.StringIO())
    server.global_store.flushall()
    yield server
    server.global_store.flushall()


def kesp(*args: str) -> bytes:
    body = b"".join(f"S{len(a.encode())}\n{a}\n".encode() for a in args)
    return f"A{len(args)}\n".encode() + body


def run(srv, scenario):
    async def runner():
        errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _l, ctx: errors.append(ctx.get("exception") or ctx)
        )
        server = await asyncio.start_server(srv.handle_connection, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            await asyncio.wait_for(scenario(reader, writer), 10)
        finally:
            writer.close()
            await asyncio.sleep(0.05)
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass
        return errors

    return asyncio.run(runner())


MALFORMED = [
    b"A1\nS3\nSETX",  # bulk payload not newline-terminated
    b"A+1\nS4\nPING\n",  # loose number
    b"A1\r\nS4\nPING\n",  # CRLF header
    b"A1\nX\n",  # wrong element sigil
    b"A1\nS" + b"0" * 60 + b"4\nPING\n",  # oversized terminated header
]


@pytest.mark.parametrize("raw", MALFORMED)
def test_protocol_error_gets_an_error_reply_then_the_connection_closes(srv, raw):
    async def scenario(r, w):
        w.write(raw)
        await w.drain()
        line = await r.readline()
        assert line.startswith(b"E"), line
        assert await r.read() == b""  # EOF: the server closed the connection

    assert run(srv, scenario) == []


def test_commands_before_the_bad_frame_are_still_answered(srv):
    async def scenario(r, w):
        w.write(kesp("SET", "k", "v") + b"A+1\n")
        await w.drain()
        first = await r.readline()
        assert first.startswith(b"+OK"), first
        second = await r.readline()
        assert second.startswith(b"E"), second
        assert await r.read() == b""

    assert run(srv, scenario) == []


def test_blank_lines_and_empty_arrays_do_not_wedge_the_connection(srv):
    async def scenario(r, w):
        w.write(b"\n\r\nA0\n" + kesp("SET", "k", "v"))
        await w.drain()
        assert (await r.readline()).startswith(b"+OK")
        # and again, with the blank frame arriving on its own first
        w.write(b"\n")
        await w.drain()
        await asyncio.sleep(0.05)
        w.write(kesp("GET", "k"))
        await w.drain()
        assert await r.readline() == b"S1\n"
        assert await r.readline() == b"v\n"

    assert run(srv, scenario) == []


def test_a_command_named_error_does_not_crash_the_session(srv):
    async def scenario(r, w):
        w.write(kesp("ERROR"))
        await w.drain()
        line = await r.readline()
        assert line.startswith((b"-ERR", b"E")), line
        # the connection is still usable
        w.write(kesp("SET", "k", "v"))
        await w.drain()
        assert (await r.readline()).startswith(b"+OK")

    assert run(srv, scenario) == []
