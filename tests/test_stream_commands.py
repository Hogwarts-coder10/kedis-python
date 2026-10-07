"""Stream commands at the handler layer: argument parsing, reply shapes,
KESP encoding of user data, replication token rewriting."""

import time

import pytest

from kedis_python.commands import CommandHandler
from kedis_python.parser import BulkString, KESPEncoder
from kedis_python.store import KedisStore


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def store(workdir):
    s = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    yield s
    s.shutdown()


@pytest.fixture
def h(store):
    return CommandHandler(store)


def run(h, line):
    return h.execute(line.split())


def seed(h, key="s", n=5):
    for i in range(n):
        run(h, f"XADD {key} {100 + i}-0 n {i}")


def entry_ids(reply):
    return [e[0] for e in reply]


class TestXadd:
    def test_returns_the_id_as_bulk_string(self, h):
        reply = run(h, "XADD s 5-5 temp 21")
        assert reply == "5-5" and isinstance(reply, BulkString)

    def test_auto_id(self, h):
        a, b = run(h, "XADD s * k 1"), run(h, "XADD s * k 2")
        assert a != b and run(h, "XLEN s") == 2

    def test_nomkstream_returns_nil_and_creates_nothing(self, h):
        assert run(h, "XADD s NOMKSTREAM * k v") is None
        assert run(h, "XLEN s") == 0

    @pytest.mark.parametrize(
        "opts",
        ["MAXLEN 3", "MAXLEN = 3", "MAXLEN ~ 3", "MAXLEN ~ 3 LIMIT 10", "NOMKSTREAM MAXLEN 3"],
    )
    def test_maxlen_forms(self, h, opts):
        run(h, "XADD s 1-0 k v")  # so NOMKSTREAM is satisfied
        for i in range(2, 8):
            assert run(h, f"XADD s {opts} {i}-0 k v") == f"{i}-0"
        assert run(h, "XLEN s") == 3

    def test_minid(self, h):
        for i in range(1, 6):
            run(h, f"XADD s MINID 3 {i}-0 k v")
        assert entry_ids(run(h, "XRANGE s - +")) == ["3-0", "4-0", "5-0"]

    def test_maxlen_and_minid_together_is_a_syntax_error(self, h):
        assert run(h, "XADD s MAXLEN 3 MINID 2 * k v") == "-ERR syntax error"

    @pytest.mark.parametrize(
        "line", ["XADD", "XADD s", "XADD s *", "XADD s * f", "XADD s * f v extra"]
    )
    def test_arity(self, h, line):
        assert run(h, line).startswith("-ERR wrong number of arguments")

    def test_error_messages_match_redis(self, h):
        assert "Invalid stream ID" in run(h, "XADD s bogus k v")
        assert "greater than 0-0" in run(h, "XADD s 0-0 k v")
        run(h, "XADD s 5-5 k v")
        assert "equal or smaller" in run(h, "XADD s 5-5 k v")

    def test_bad_maxlen_value(self, h):
        assert run(h, "XADD s MAXLEN abc * k v").startswith("-ERR value is not an integer")

    def test_wrongtype(self, h):
        run(h, "SET str v")
        assert "WRONGTYPE" in run(h, "XADD str * k v")
        run(h, "XADD s 1-0 k v")
        assert "WRONGTYPE" in run(h, "GET s")


class TestRanges:
    def test_nested_reply_shape(self, h):
        run(h, "XADD s 1-0 temp 21 hum 40")
        assert run(h, "XRANGE s - +") == [["1-0", ["temp", "21", "hum", "40"]]]

    def test_bounds_and_count(self, h):
        seed(h)
        assert entry_ids(run(h, "XRANGE s 101-0 103-0")) == ["101-0", "102-0", "103-0"]
        assert entry_ids(run(h, "XRANGE s (101-0 (103-0")) == ["102-0"]
        assert entry_ids(run(h, "XRANGE s - + COUNT 2")) == ["100-0", "101-0"]
        assert run(h, "XRANGE s - + COUNT 0") == []

    def test_revrange(self, h):
        seed(h)
        assert entry_ids(run(h, "XREVRANGE s + - COUNT 2")) == ["104-0", "103-0"]

    def test_missing_key_is_empty(self, h):
        assert run(h, "XRANGE nope - +") == []
        assert run(h, "XLEN nope") == 0

    @pytest.mark.parametrize("line", ["XRANGE s", "XRANGE s -", "XREVRANGE s + - COUNT"])
    def test_arity(self, h, line):
        assert run(h, line).startswith("-ERR wrong number of arguments")

    def test_bad_arguments(self, h):
        assert run(h, "XRANGE s - + LIMIT 2") == "-ERR syntax error"
        assert "Invalid stream ID" in run(h, "XRANGE s zzz +")
        assert run(h, "XRANGE s - + COUNT x").startswith("-ERR value is not an integer")


class TestXread:
    def test_nil_when_nothing_new(self, h):
        seed(h)
        assert run(h, "XREAD STREAMS s $") is None
        assert run(h, "XREAD STREAMS s 104-0") is None
        assert run(h, "XREAD STREAMS nope 0-0") is None

    def test_returns_entries_after_id(self, h):
        seed(h)
        reply = run(h, "XREAD COUNT 2 STREAMS s 100-0")
        assert reply[0][0] == "s" and entry_ids(reply[0][1]) == ["101-0", "102-0"]

    def test_multiple_streams(self, h):
        seed(h, "a", 3)
        seed(h, "b", 3)
        reply = run(h, "XREAD STREAMS a b 0-0 101-0")
        assert [(k, entry_ids(e)) for k, e in reply] == [
            ("a", ["100-0", "101-0", "102-0"]),
            ("b", ["102-0"]),
        ]

    def test_count_zero_means_unlimited(self, h):
        seed(h)
        assert len(run(h, "XREAD COUNT 0 STREAMS s 0-0")[0][1]) == 5

    def test_inline_block_never_waits(self, h):
        seed(h)
        started = time.monotonic()
        assert run(h, "XREAD BLOCK 5000 STREAMS s $") is None
        assert time.monotonic() - started < 0.5

    @pytest.mark.parametrize(
        "line,msg",
        [
            ("XREAD s 0-0", "syntax error"),
            ("XREAD STREAMS", "Unbalanced"),
            ("XREAD STREAMS a b 0-0", "Unbalanced"),
            ("XREAD COUNT STREAMS s 0", "not an integer"),
            ("XREAD BLOCK -1 STREAMS s 0", "timeout is negative"),
            ("XREAD BLOCK x STREAMS s 0", "timeout is not an integer"),
            ("XREAD BOGUS 1 STREAMS s 0", "syntax error"),
        ],
    )
    def test_syntax_errors(self, h, line, msg):
        assert msg in run(h, line)

    def test_parse_xread_is_case_insensitive(self):
        parsed = CommandHandler.parse_xread("XREAD count 3 block 10 streams k 0".split())
        assert parsed == (3, 10, [("k", "0")])


class TestDeleteAndTrim:
    def test_xdel(self, h):
        seed(h)
        assert run(h, "XDEL s 101-0 103-0 9-9") == 2
        assert run(h, "XLEN s") == 3
        assert "Invalid stream ID" in run(h, "XDEL s zzz")
        assert run(h, "XDEL s").startswith("-ERR wrong number of arguments")

    def test_xtrim(self, h):
        seed(h, n=6)
        assert run(h, "XTRIM s MAXLEN 4") == 2
        assert run(h, "XTRIM s MAXLEN ~ 3") == 1
        assert run(h, "XTRIM s MINID 104") == 1
        assert entry_ids(run(h, "XRANGE s - +")) == ["104-0", "105-0"]

    def test_xtrim_syntax(self, h):
        seed(h)
        assert run(h, "XTRIM s MAXLEN").startswith("-ERR wrong number of arguments")
        assert run(h, "XTRIM s MAXLEN 1 junk") == "-ERR syntax error"


class TestRegistration:
    def test_write_commands(self, h):
        assert {"XADD", "XDEL", "XTRIM"} <= h.WRITE_COMMANDS
        assert not ({"XREAD", "XRANGE", "XREVRANGE", "XLEN"} & h.WRITE_COMMANDS)

    def test_dispatch(self, h):
        for c in ("XADD", "XLEN", "XRANGE", "XREVRANGE", "XREAD", "XDEL", "XTRIM"):
            assert c in h._commands

    def test_type_command(self, h):
        run(h, "XADD s 1-0 k v")
        assert run(h, "TYPE s") == "stream"


def decode_kesp(data: bytes):
    """Tiny recursive KESP decoder for tests: arrays -> lists, others -> (sigil, text)."""
    pos = 0

    def line():
        nonlocal pos
        end = data.index(b"\n", pos)
        out = data[pos:end]
        pos = end + 1
        return out

    def value():
        nonlocal pos
        head = line()
        sigil, rest = chr(head[0]), head[1:].decode()
        if sigil == "A":
            return [value() for _ in range(int(rest))]
        if sigil == "S":
            n = int(rest)
            raw = data[pos : pos + n]
            pos += n + 1
            return ("S", raw.decode())
        return (sigil, rest)

    return value()


class TestEncodingOfUserData:
    def test_values_that_look_like_statuses_stay_data(self, h):
        texts = ["OK", "+OK", "+QUEUED", "ERROR disk full", "-ERR nope"]
        for text in texts:
            h.execute(["XADD", "s", "*", "status", text])
        wire = KESPEncoder.encode(h.execute(["XRANGE", "s", "-", "+"]))
        entries = decode_kesp(wire)
        # each entry: [("S", id), [("S", field), ("S", value)]]
        assert [e[1][1] for e in entries] == [("S", t) for t in texts]

    def test_plain_str_still_encodes_as_status(self):
        # BulkString is opt-in: existing behaviour for other commands is intact
        assert KESPEncoder.encode("OK") == b"+OK\n"
        assert KESPEncoder.encode("ERROR boom").startswith(b"E")
        assert KESPEncoder.encode(BulkString("OK")) == b"S2\nOK\n"

    def test_multibyte_text(self, h):
        h.execute(["XADD", "s", "1-0", "name", "h\u00e9llo \u2713"])
        entries = decode_kesp(KESPEncoder.encode(h.execute(["XRANGE", "s", "-", "+"])))
        assert entries[0][1][1] == ("S", "h\u00e9llo \u2713")


class TestReplicationTokens:
    def test_star_is_replaced_by_the_assigned_id(self, h):
        tokens = ["XADD", "s", "*", "k", "v"]
        reply = h.execute(tokens)
        assert h.replication_tokens(tokens, reply) == ["XADD", "s", str(reply), "k", "v"]

    def test_options_are_preserved_and_id_found_after_them(self, h):
        tokens = "XADD s NOMKSTREAM MAXLEN ~ 5 * a b".split()
        h.execute("XADD s 1-0 x y".split())
        reply = h.execute(tokens)
        out = h.replication_tokens(tokens, reply)
        assert out == ["XADD", "s", "NOMKSTREAM", "MAXLEN", "~", "5", str(reply), "a", "b"]

    def test_failed_or_other_commands_pass_through(self, h):
        t = ["XADD", "s", "0-0", "k", "v"]
        assert h.replication_tokens(t, h.execute(t)) == t
        t2 = ["SET", "a", "b"]
        assert h.replication_tokens(t2, "OK") == t2

    def test_replica_converges_with_master(self, h, tmp_path, monkeypatch):
        master_cmds = [
            "XADD s * a 1",
            "XADD s * a 2",
            "XADD s MAXLEN 2 * a 3",
            "XADD s * a 4",
        ]
        replies = [h.execute(c.split()) for c in master_cmds]
        sub = tmp_path / "replica"
        sub.mkdir()
        monkeypatch.chdir(sub)
        replica = KedisStore(aof_filename="r.aof", appendfsync="always", lru_maxsize=100_000)
        try:
            rh = CommandHandler(replica)
            for c, reply in zip(master_cmds, replies):
                rh.execute(h.replication_tokens(c.split(), reply))
            assert rh.execute(["XRANGE", "s", "-", "+"]) == h.execute(["XRANGE", "s", "-", "+"])
        finally:
            replica.shutdown()
