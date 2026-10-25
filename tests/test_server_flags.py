"""Command-line flags: every limit the docs describe can be set at startup, bad
values are refused before anything starts, and the docs list exactly the flags
that exist."""

import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kedis_python import settings
from kedis_python.settings import (
    SETTINGS,
    build_parser,
    format_seconds,
    format_size,
    parse_and_apply,
    parse_count,
    parse_seconds,
    parse_size,
)

DOCS = Path(__file__).resolve().parent.parent / "docs" / "limits.md"


@pytest.fixture(autouse=True)
def restore_every_setting():
    """Flags overwrite module and class attributes: put them all back."""
    saved = [(s, s.current()) for s in SETTINGS]
    yield
    for setting, value in saved:
        setattr(setting.target(), setting.attr, value)


# --- the value parsers ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [("1", 1), ("10000", 10000), ("10_000", 10000), ("10,000", 10000), (" 7 ", 7), ("0", 0), ("-3", -3)],
)
def test_counts(text, expected):
    assert parse_count(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "1.5", "5k", "1e3", "--1", "0x10"])
def test_counts_reject(text):
    with pytest.raises(ValueError, match="whole number"):
        parse_count(text)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("65536", 65536),
        ("64KiB", 64 * 1024),
        ("64k", 64 * 1024),
        ("64 KB", 64 * 1024),
        ("1.5MiB", 1536 * 1024),
        ("2G", 2 * 1024**3),
        ("512mib", 512 * 1024**2),
        ("10b", 10),
        ("1,024", 1024),
    ],
)
def test_sizes(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "MiB", "12XB", "1.5", "0.3KiB", "abc", "1 2"])
def test_sizes_reject(text):
    with pytest.raises(ValueError):
        parse_size(text)


@pytest.mark.parametrize(
    "text, expected",
    [("30", 30.0), ("0.5", 0.5), ("30s", 30.0), ("500ms", 0.5), ("2m", 120.0), ("1h", 3600.0), ("1,5", 15.0)],
)
def test_durations(text, expected):
    assert parse_seconds(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "abc", "s", "5 days", "nan", "inf", "9" * 400])
def test_durations_reject(text):
    with pytest.raises(ValueError):
        parse_seconds(text)


def test_values_are_shown_the_way_they_are_read():
    assert format_size(64 * 1024 * 1024) == "64MiB"
    assert format_size(256 * 1024) == "256KiB"
    assert format_size(1024**3) == "1GiB"
    assert format_size(32) == "32"
    assert format_seconds(30.0) == "30s" and format_seconds(0.5) == "0.5s"
    for setting in SETTINGS:  # whatever --help prints for a default can be fed back in
        shown = setting.show(setting.current()).replace("/s", "").replace(",", "")
        if setting.kind in ("size", "rate"):
            assert parse_size(shown) == setting.current()


# --- the table ------------------------------------------------------------------------------------------


def test_every_flag_is_the_attribute_name_in_kebab_case():
    for setting in SETTINGS:
        assert setting.flag == setting.attr.lower().replace("_", "-"), setting


def test_flags_and_attributes_are_unique_and_the_attributes_exist():
    assert len({s.flag for s in SETTINGS}) == len(SETTINGS)
    assert len({(s.owner, s.attr) for s in SETTINGS}) == len(SETTINGS)
    for setting in SETTINGS:
        value = setting.current()
        assert isinstance(value, (int, float, str)), setting
        if setting.kind == "seconds":
            assert isinstance(value, float)  # the type the code already uses
        elif setting.kind != "host":
            assert isinstance(value, int) and not isinstance(value, bool), setting


def test_help_lists_every_flag_with_the_default_it_really_has():
    text = " ".join(build_parser().format_help().split())
    for setting in SETTINGS:
        assert f"--{setting.flag}" in text
        assert f"(default: {setting.show(setting.current())})" in text, setting.flag


# --- applying flags ---------------------------------------------------------------------------------------

VALID = {
    "count": ("7", 7),
    "size": ("3MiB", 3 * 1024**2),
    "rate": ("2KiB", 2048),
    "seconds": ("2.5", 2.5),
    "port": ("6400", 6400),
    "host": ("0.0.0.0", "0.0.0.0"),
}


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s.flag)
def test_each_flag_sets_its_attribute(setting):
    text, expected = VALID[setting.kind]
    applied = parse_and_apply([f"--{setting.flag}", text])
    assert setting.current() == expected
    assert type(setting.current()) is type(expected)
    assert applied == [(setting, expected)]


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s.flag)
def test_flag_equals_form_works_too(setting):
    text, expected = VALID[setting.kind]
    parse_and_apply([f"--{setting.flag}={text}"])
    assert setting.current() == expected


def test_no_flags_change_nothing():
    before = [(s, s.current()) for s in SETTINGS]
    assert parse_and_apply([]) == []
    assert [(s, s.current()) for s in SETTINGS] == before


INVALID = {
    "count": ["0", "-1", "abc", "1.5", ""],
    "size": ["0", "-5", "12XB", "MiB", ""],
    "rate": ["0", "-5", "12XB", ""],
    "seconds": ["0", "-1", "abc", "nan", "inf", ""],
    "port": ["0", "-1", "70000", "abc"],
    "host": ["", "   "],
}


@pytest.mark.parametrize(
    "setting, value",
    [(s, v) for s in SETTINGS for v in INVALID[s.kind]],
    ids=lambda x: x.flag if hasattr(x, "flag") else repr(x),
)
def test_a_bad_value_exits_with_status_2_naming_the_flag(setting, value, capsys):
    before = setting.current()
    with pytest.raises(SystemExit) as exc:
        parse_and_apply([f"--{setting.flag}", value])
    assert exc.value.code == 2
    assert f"--{setting.flag}" in capsys.readouterr().err
    assert setting.current() == before


def test_one_bad_flag_means_none_are_applied(capsys):
    from kedis_python import server

    before = server.MAX_CLIENTS
    with pytest.raises(SystemExit):
        parse_and_apply(["--max-clients", "5", "--stall-timeout", "-1"])
    assert server.MAX_CLIENTS == before


def test_unknown_flags_and_stray_arguments_are_refused(capsys):
    for argv in (["--no-such-limit", "1"], ["7001"], ["--port"]):
        with pytest.raises(SystemExit) as exc:
            parse_and_apply(argv)
        assert exc.value.code == 2


def test_help_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_and_apply(["--help"])
    assert exc.value.code == 0
    assert "--max-clients" in capsys.readouterr().out


def test_flags_reach_the_code_that_enforces_the_limits():
    """Every limit is read where it is enforced, at call time; setting the
    attribute from outside must change behaviour, not just the attribute."""
    from kedis_python.parser import CommandParser

    parse_and_apply(["--max-array-length", "3", "--max-inline-length", "10"])
    tokens, _, _ = CommandParser.parse_buffer(b"A4\nS1\na\nS1\nb\nS1\nc\nS1\nd\n")
    assert type(tokens).__name__ == "ProtocolError"
    tokens, _, _ = CommandParser.parse_buffer(b"x" * 11 + b"\n")
    assert type(tokens).__name__ == "ProtocolError"


def test_run_reads_the_argv_it_is_given_and_only_falls_back_to_sys_argv(monkeypatch):
    from kedis_python import server

    seen = []

    async def fake_main():
        seen.append((server.PORT, server.MAX_CLIENTS))

    monkeypatch.setattr(server, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["kedis-server", "--port", "9999"])

    server.run(["--port", "7001", "--max-clients", "3"])  # explicit: sys.argv is ignored
    assert seen[-1] == (7001, 3)

    server.PORT, server.MAX_CLIENTS = 6379, 10_000
    server.run([])  # embedding code keeps its own command line out of it
    assert seen[-1] == (6379, 10_000)

    server.run()  # the console script: the process's own arguments
    assert seen[-1][0] == 9999


# --- the docs ----------------------------------------------------------------------------------------------

# Documented constants that are not meant to be tuned, and why.
NOT_TUNABLE = {
    "MAX_HEADER_LINE": "a protocol constant: the length of A<n>/S<n> lines",
    "MAX_REPLY_BYTES": "the client's (network.py), not the server's",
    "MAX_LINE": "the client decoder's text-line cap",
    "MAX_DEPTH": "the nesting cap shared by encoder and client decoder",
}


def test_every_limit_the_docs_name_has_a_flag_or_a_reason_not_to():
    text = DOCS.read_text(encoding="utf-8")
    named = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", text))
    tunable = {s.attr for s in SETTINGS}
    owners = [s.target() for s in SETTINGS]
    numeric = {
        name
        for name in named
        if any(isinstance(getattr(o, name, None), (int, float)) for o in owners)
        or name in NOT_TUNABLE
    }
    missing = sorted(numeric - tunable - set(NOT_TUNABLE))
    assert not missing, f"documented limits with no flag: {missing}"
    assert numeric & tunable  # the check is looking at something


def test_the_docs_flag_table_matches_the_flags_that_exist():
    text = DOCS.read_text(encoding="utf-8")
    rows = re.findall(r"^\| `--([a-z0-9-]+)` \| `([A-Z0-9_]+)` \|", text, flags=re.M)
    assert rows, "docs/limits.md has no flag table"
    assert sorted(rows) == sorted((s.flag, s.attr) for s in SETTINGS)


# --- the real process --------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


LAUNCH = {
    # python -m runs server.py as __main__: a second copy of the module
    "module": [sys.executable, "-m", "kedis_python.server"],
    # what the kedis-server console script does
    "entry-point": [sys.executable, "-c", "from kedis_python.server import run; run()"],
}


def start(tmp_path, *flags, launch="module"):
    port = free_port()
    proc = subprocess.Popen(
        [*LAUNCH[launch], "--port", str(port), *flags],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return proc, port
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(proc.stdout.read().decode())
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("server did not start")


def stop(proc):
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


def kesp(*args: bytes) -> bytes:
    return b"A%d\n" % len(args) + b"".join(b"S%d\n%s\n" % (len(a), a) for a in args)


def hold_a_slot(port):
    """A connection the server has admitted (it answered), kept open."""
    deadline = time.time() + 5
    while True:
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.sendall(kesp(b"GET", b"nothing"))
        conn.settimeout(2)
        try:
            if conn.recv(100) == b"N\n":
                return conn
        except socket.timeout:
            pass
        conn.close()  # still counting the readiness probe's connection: try again
        assert time.time() < deadline
        time.sleep(0.1)


def ask(port, payload, wait=0.4):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(payload)
        time.sleep(wait)
        s.settimeout(3)
        try:
            return s.recv(65536)
        except socket.timeout:
            return b""


def test_the_server_listens_where_the_flags_say(tmp_path):
    proc, port = start(tmp_path, "--host", "127.0.0.1")
    try:
        assert ask(port, kesp(b"SET", b"k", b"v")).startswith(b"+OK")
        assert ask(port, kesp(b"GET", b"k")) == b"S1\nv\n"
    finally:
        stop(proc)


@pytest.mark.parametrize("launch", ["module", "entry-point"])
def test_the_flags_reach_the_server_however_it_is_started(tmp_path, launch):
    """--port decides where it listens and --max-array-length what it accepts, whether
    server.py runs as __main__ (python -m) or is imported (the kedis-server script)."""
    proc, port = start(tmp_path, "--max-array-length", "3", launch=launch)
    try:
        assert ask(port, kesp(b"SET", b"a", b"b")).startswith(b"+OK")  # on the port asked for
        assert ask(port, kesp(b"SET", b"a", b"b", b"c", b"d")).startswith(b"EProtocol error")
    finally:
        stop(proc)


def test_max_clients_is_enforced_with_the_flag(tmp_path):
    proc, port = start(tmp_path, "--max-clients", "1")
    try:
        first = hold_a_slot(port)
        assert ask(port, b"").startswith(b"Emax number of clients reached")
        first.close()
    finally:
        stop(proc)


def test_max_array_length_is_enforced_with_the_flag(tmp_path):
    proc, port = start(tmp_path, "--max-array-length", "3")
    try:
        assert ask(port, kesp(b"SET", b"a", b"b")).startswith(b"+OK")
        reply = ask(port, kesp(b"SET", b"a", b"b", b"c", b"d"))
        assert reply.startswith(b"EProtocol error")
    finally:
        stop(proc)


def test_stall_timeout_is_enforced_with_the_flag(tmp_path):
    proc, port = start(tmp_path, "--stall-timeout", "500ms")
    try:
        started = time.time()
        reply = ask(port, b"A2\nS3\nSET\nS5\nab", wait=2.0)  # a command that stops half-way
        assert b"timed out waiting for the rest of a command" in reply
        assert time.time() - started < 5
    finally:
        stop(proc)


def test_the_settings_line_is_printed_at_startup(tmp_path):
    proc, port = start(tmp_path, "--max-clients", "50", "--stall-timeout", "2m")
    try:
        time.sleep(0.3)
    finally:
        stop(proc)
    out = proc.stdout.read().decode()
    assert "--max-clients 50" in out and "--stall-timeout 120s" in out


def test_help_and_bad_flags_from_the_command_line(tmp_path):
    base = [sys.executable, "-m", "kedis_python.server"]
    done = subprocess.run(base + ["--help"], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and "--max-clients" in done.stdout
    bad = subprocess.run(base + ["--max-clients", "0"], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert bad.returncode == 2 and "--max-clients" in bad.stderr
    assert "KEDIS" not in bad.stdout  # it never got as far as the banner
