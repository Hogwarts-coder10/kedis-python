"""Consumer group commands at the handler layer: argument parsing, reply
shapes, error encoding, the internal XGAPPLY, and replication payloads."""

import time

import pytest

from kedis_python.commands import CommandHandler
from kedis_python.parser import BulkString, KESPEncoder
from kedis_python.store import KedisStore
from kedis_python.stream import StreamError


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


def seed(h, n=5):
    for i in range(n):
        run(h, f"XADD s {i + 1}-0 n {i}")
    assert run(h, "XGROUP CREATE s g 0") == "OK"


def wire(reply) -> bytes:
    return KESPEncoder.encode(reply)


class TestXgroup:
    def test_create_forms(self, h):
        run(h, "XADD s 1-0 k v")
        assert run(h, "XGROUP CREATE s g1 $") == "OK"
        assert run(h, "xgroup create s g2 0") == "OK"  # case-insensitive
        assert run(h, "XGROUP CREATE fresh g $ MKSTREAM") == "OK"
        assert run(h, "XGROUP CREATE fresh2 g $ mkstream") == "OK"
        assert run(h, "XLEN fresh") == 0

    def test_error_replies_are_real_errors_on_the_wire(self, h):
        run(h, "XADD s 1-0 k v")
        run(h, "XGROUP CREATE s g 0")
        assert wire(run(h, "XGROUP CREATE s g 0")).startswith(b"EBUSYGROUP ")
        assert wire(run(h, "XREADGROUP GROUP x c STREAMS s >")).startswith(b"ENOGROUP ")
        assert wire(run(h, "XGROUP CREATE nope g 0")).startswith(b"EThe XGROUP subcommand")
        assert wire(run(h, "XGROUP CREATE s g2 zzz")).startswith(b"EInvalid stream ID")

    def test_entriesread_is_rejected(self, h):
        run(h, "XADD s 1-0 k v")
        assert "ENTRIESREAD is not supported" in run(h, "XGROUP CREATE s g 0 ENTRIESREAD 5")
        assert "ENTRIESREAD is not supported" in run(h, "XGROUP CREATE s g 0 MKSTREAM ENTRIESREAD 5")
        run(h, "XGROUP CREATE s g 0")
        assert "ENTRIESREAD is not supported" in run(h, "XGROUP SETID s g 0 ENTRIESREAD 5")
        assert run(h, "XINFO GROUPS s")[0][1] == "g"  # and nothing was changed

    @pytest.mark.parametrize(
        "line",
        [
            "XGROUP",
            "XGROUP CREATE s g",
            "XGROUP SETID s g",
            "XGROUP DESTROY s",
            "XGROUP DESTROY s g extra",
            "XGROUP CREATECONSUMER s g",
            "XGROUP DELCONSUMER s g c extra",
        ],
    )
    def test_arity(self, h, line):
        assert run(h, line).startswith("-ERR wrong number of arguments")

    def test_syntax_and_unknown_subcommand(self, h):
        run(h, "XADD s 1-0 k v")
        assert run(h, "XGROUP CREATE s g 0 BOGUS") == "-ERR syntax error"
        run(h, "XGROUP CREATE s g 0")
        assert run(h, "XGROUP SETID s g 0 junk") == "-ERR syntax error"
        assert "unknown subcommand" in run(h, "XGROUP HELP")

    def test_setid_destroy_and_consumer_commands(self, h):
        seed(h)
        run(h, "XREADGROUP GROUP g c STREAMS s >")
        assert run(h, "XGROUP SETID s g 2-0") == "OK"
        assert run(h, "XGROUP CREATECONSUMER s g extra") == 1
        assert run(h, "XGROUP CREATECONSUMER s g extra") == 0
        assert run(h, "XGROUP DELCONSUMER s g c") == 5
        assert run(h, "XGROUP DESTROY s g") == 1
        assert run(h, "XGROUP DESTROY s g") == 0
        assert "NOGROUP" in run(h, "XGROUP SETID s g 0")

    def test_wrongtype(self, h):
        run(h, "SET str v")
        for line in ("XGROUP CREATE str g 0", "XACK str g 1-0", "XPENDING str g", "XINFO GROUPS str"):
            assert "WRONGTYPE" in run(h, line), line
        assert "WRONGTYPE" in run(h, "XREADGROUP GROUP g c STREAMS str >")


class TestXreadgroup:
    def test_reply_shape_matches_xread(self, h):
        run(h, "XADD s 1-0 temp 21 hum 40")
        run(h, "XGROUP CREATE s g 0")
        reply = run(h, "XREADGROUP GROUP g c STREAMS s >")
        assert reply == [["s", [["1-0", ["temp", "21", "hum", "40"]]]]]
        assert isinstance(reply[0][0], BulkString) and isinstance(reply[0][1][0][0], BulkString)

    def test_nil_when_nothing_is_new(self, h):
        seed(h)
        run(h, "XREADGROUP GROUP g c STREAMS s >")
        assert run(h, "XREADGROUP GROUP g c STREAMS s >") is None

    def test_user_data_that_looks_like_a_status_stays_data(self, h):
        run(h, "XADD s 1-0 level ERROR_x ack OK")
        h.execute(["XADD", "s", "2-0", "msg", "ERROR disk full"])
        run(h, "XGROUP CREATE s g 0")
        data = wire(run(h, "XREADGROUP GROUP g c STREAMS s >"))
        assert b"S15\nERROR disk full\n" in data and b"S2\nOK\n" in data

    def test_count_noack_and_history(self, h):
        seed(h)
        assert [e[0] for e in run(h, "XREADGROUP GROUP g c COUNT 2 NOACK STREAMS s >")[0][1]] == [
            "1-0",
            "2-0",
        ]
        assert run(h, "XPENDING s g")[0] == 0
        run(h, "XREADGROUP GROUP g c COUNT 0 STREAMS s >")  # COUNT 0: unlimited
        assert [e[0] for e in run(h, "XREADGROUP GROUP g c STREAMS s 0")[0][1]] == [
            "3-0",
            "4-0",
            "5-0",
        ]

    def test_history_of_a_deleted_entry_is_id_and_nil(self, h):
        seed(h, 2)
        run(h, "XREADGROUP GROUP g c STREAMS s >")
        run(h, "XDEL s 1-0")
        reply = run(h, "XREADGROUP GROUP g c STREAMS s 0")
        assert reply == [["s", [["1-0", None], ["2-0", ["n", "1"]]]]]
        assert wire(reply).count(b"N\n") == 1

    def test_history_of_a_fresh_consumer_is_an_empty_list(self, h):
        seed(h)
        assert run(h, "XREADGROUP GROUP g new STREAMS s 0") == [["s", []]]

    def test_inline_block_never_waits(self, h):
        seed(h)
        run(h, "XREADGROUP GROUP g c STREAMS s >")
        started = time.monotonic()
        assert run(h, "XREADGROUP GROUP g c BLOCK 5000 STREAMS s >") is None
        assert time.monotonic() - started < 0.5

    @pytest.mark.parametrize(
        "line,msg",
        [
            ("XREADGROUP STREAMS s >", "syntax error"),
            ("XREADGROUP GROUP g STREAMS s >", "syntax error"),
            ("XREADGROUP GROUP g c", "syntax error"),
            ("XREADGROUP GROUP g c s >", "syntax error"),
            ("XREADGROUP GROUP g c BOGUS STREAMS s >", "syntax error"),
            ("XREADGROUP GROUP g c STREAMS", "Unbalanced"),
            ("XREADGROUP GROUP g c STREAMS a b >", "Unbalanced"),
            ("XREADGROUP GROUP g c COUNT STREAMS s >", "not an integer"),
            ("XREADGROUP GROUP g c BLOCK -1 STREAMS s >", "timeout is negative"),
            ("XREADGROUP GROUP g c BLOCK x STREAMS s >", "timeout is not an integer"),
        ],
    )
    def test_syntax_errors(self, h, line, msg):
        seed(h)
        assert msg in run(h, line)

    def test_dollar_and_missing_group(self, h):
        seed(h)
        assert "meaningless" in run(h, "XREADGROUP GROUP g c STREAMS s $")
        assert "NOGROUP" in run(h, "XREADGROUP GROUP other c STREAMS s >")

    def test_parse_is_case_insensitive_and_complete(self):
        parsed = CommandHandler.parse_xreadgroup(
            "XREADGROUP group g c count 3 block 10 noack streams a b > 0".split()
        )
        assert parsed == ("g", "c", 3, 10, True, [("a", ">"), ("b", "0")])
        assert CommandHandler.parse_xreadgroup("XREADGROUP GROUP g c STREAMS a >".split()) == (
            "g", "c", None, None, False, [("a", ">")],
        )  # fmt: skip

    def test_names_are_validated(self, h):
        seed(h)
        assert "Invalid group or consumer name" in h.execute(
            ["XREADGROUP", "GROUP", "g", "two words", "STREAMS", "s", ">"]
        )


class TestAckPendingInfo:
    def test_xack(self, h):
        seed(h)
        run(h, "XREADGROUP GROUP g c STREAMS s >")
        assert run(h, "XACK s g 1-0 2-0 9-9") == 2
        assert run(h, "XACK s g 1-0") == 0
        assert run(h, "XACK nope g 1-0") == 0
        assert "Invalid stream ID" in run(h, "XACK s g zzz")
        for line in ("XACK", "XACK s", "XACK s g"):
            assert run(h, line).startswith("-ERR wrong number of arguments")

    def test_pending_summary_shape(self, h):
        seed(h)
        run(h, "XREADGROUP GROUP g amy COUNT 2 STREAMS s >")
        run(h, "XREADGROUP GROUP g zed COUNT 1 STREAMS s >")
        reply = run(h, "XPENDING s g")
        assert reply == [3, "1-0", "3-0", [["amy", "2"], ["zed", "1"]]]
        assert wire(reply).startswith(b"A4\nI3\nS3\n1-0\nS3\n3-0\nA2\n")  # counts are bulk strings

    def test_pending_summary_when_empty(self, h):
        seed(h)
        assert run(h, "XPENDING s g") == [0, None, None, None]

    def test_pending_extended_forms(self, h):
        seed(h)
        run(h, "XREADGROUP GROUP g a COUNT 3 STREAMS s >")
        run(h, "XREADGROUP GROUP g b STREAMS s >")
        rows = run(h, "XPENDING s g - + 10")
        assert [(r[0], r[1], r[3]) for r in rows] == [
            ("1-0", "a", 1), ("2-0", "a", 1), ("3-0", "a", 1), ("4-0", "b", 1), ("5-0", "b", 1),
        ]  # fmt: skip
        assert all(isinstance(r[2], int) for r in rows)
        assert [r[0] for r in run(h, "XPENDING s g - + 10 b")] == ["4-0", "5-0"]
        assert [r[0] for r in run(h, "XPENDING s g (1-0 + 2")] == ["2-0", "3-0"]
        assert [r[0] for r in run(h, "XPENDING s g IDLE 0 - + 10 a")] == ["1-0", "2-0", "3-0"]
        assert run(h, "XPENDING s g IDLE 3600000 - + 10") == []
        assert [r[0] for r in run(h, "xpending s g idle 0 - + 1")] == ["1-0"]

    @pytest.mark.parametrize(
        "line,msg",
        [
            ("XPENDING s", "wrong number of arguments"),
            ("XPENDING s g -", "syntax error"),
            ("XPENDING s g - +", "syntax error"),
            ("XPENDING s g - + 10 c extra", "syntax error"),
            ("XPENDING s g IDLE", "syntax error"),
            ("XPENDING s g - + x", "not an integer"),
            ("XPENDING s g IDLE x - + 1", "not an integer"),
            ("XPENDING s g zzz + 1", "Invalid stream ID"),
            ("XPENDING s nogroup", "NOGROUP"),
            ("XPENDING nope g", "NOGROUP"),
        ],
    )
    def test_pending_errors(self, h, line, msg):
        seed(h)
        assert msg in run(h, line)

    def test_xinfo_groups(self, h):
        seed(h)
        run(h, "XREADGROUP GROUP g c COUNT 2 STREAMS s >")
        assert run(h, "XINFO GROUPS s") == [
            ["name", "g", "consumers", 1, "pending", 2, "last-delivered-id", "2-0",
             "entries-read", None, "lag", None]
        ]  # fmt: skip
        assert run(h, "xinfo groups nope").endswith("no such key")

    def test_xinfo_consumers(self, h):
        seed(h)
        run(h, "XREADGROUP GROUP g c COUNT 2 STREAMS s >")
        (row,) = run(h, "XINFO CONSUMERS s g")
        assert row[:4] == ["name", "c", "pending", 2]
        assert row[4] == "idle" and row[6] == "inactive"
        assert "NOGROUP" in run(h, "XINFO CONSUMERS s nope")

    def test_xinfo_usage_errors(self, h):
        seed(h)
        assert run(h, "XINFO").startswith("-ERR wrong number of arguments")
        assert run(h, "XINFO GROUPS").startswith("-ERR wrong number of arguments")
        assert run(h, "XINFO CONSUMERS s").startswith("-ERR wrong number of arguments")
        assert run(h, "XINFO GROUPS s extra").startswith("-ERR wrong number of arguments")
        assert "unknown subcommand" in run(h, "XINFO STREAM s")


class TestRegistration:
    def test_write_commands(self, h):
        assert {"XGROUP", "XREADGROUP", "XACK"} <= h.WRITE_COMMANDS
        assert not ({"XPENDING", "XINFO", "XGAPPLY"} & h.WRITE_COMMANDS)

    def test_dispatch(self, h):
        for c in ("XGROUP", "XREADGROUP", "XACK", "XPENDING", "XINFO", "XGAPPLY"):
            assert c in h._commands

    def test_internal_commands_are_not_offered_to_clients(self, h):
        """The CLI builds autocomplete and typo suggestions from this list."""
        assert "XGAPPLY" in h._commands  # dispatchable for replication...
        assert "XGAPPLY" not in h.public_commands  # ...but never advertised
        assert {"XGROUP", "XREADGROUP", "XACK", "XPENDING", "XINFO"} <= set(h.public_commands)
        assert set(h.public_commands) | h.INTERNAL_COMMANDS == set(h._commands)

    def test_type_is_still_stream(self, h):
        seed(h)
        assert run(h, "TYPE s") == "stream"


class TestExecuteRecords:
    def test_returns_the_records_a_command_produced(self, h):
        seed(h, 2)
        reply, records = h.execute_records("XREADGROUP GROUP g c STREAMS s >".split())
        assert reply is not None
        assert [r[0] for r in records] == ["XGCONS", "XGPEL", "XGPEL", "XGLAST"]

    def test_each_command_gets_only_its_own_records(self, h):
        seed(h)
        h.execute_records("XREADGROUP GROUP g c COUNT 1 STREAMS s >".split())
        _, records = h.execute_records("XACK s g 1-0".split())
        assert records == [["XACK", "s", "g", "1-0"]]
        _, records = h.execute_records("XLEN s".split())
        assert records == []

    def test_failed_commands_produce_none(self, h):
        seed(h)
        reply, records = h.execute_records("XREADGROUP GROUP nope c STREAMS s >".split())
        assert reply.startswith("-ERR NOGROUP") and records == []

    def test_plain_execute_does_not_leave_records_behind(self, h):
        seed(h)
        run(h, "XREADGROUP GROUP g c STREAMS s >")
        assert h.store.drain_group_records() == []

    def test_empty_command(self, h):
        assert h.execute_records([]) == ("-ERR empty command or syntax error", [])


class TestXgapply:
    def test_a_network_client_cannot_call_it(self, h):
        seed(h, 1)
        before = run(h, "XINFO GROUPS s")
        reply = h.execute("XGAPPLY XGLAST s g 1-0".split(), client_socket=object())
        assert reply == "-ERR unknown command 'XGAPPLY'"
        assert run(h, "XINFO GROUPS s") == before

    def test_the_replication_path_can(self, h):
        seed(h, 3)
        assert h.execute("XGAPPLY XGLAST s g 2-0".split(), None) == "OK"
        assert run(h, "XINFO GROUPS s")[0][7] == "2-0"

    def test_applying_does_not_forward_it_again(self, h):
        seed(h, 3)
        _, records = h.execute_records("XGAPPLY XGLAST s g 2-0".split(), None)
        assert records == []

    def test_errors(self, h):
        seed(h)
        assert h.execute("XGAPPLY".split(), None).startswith("-ERR wrong number")
        assert h.execute("XGAPPLY XGLAST s".split(), None).startswith("-ERR wrong number")
        assert "Invalid consumer group record" in h.execute("XGAPPLY XGLAST s nogroup 1-0".split(), None)
        assert "Invalid consumer group record" in h.execute("XGAPPLY BOGUS s g 1-0".split(), None)
        assert "Invalid consumer group record" in h.execute("XGAPPLY XGLAST ghost g 1-0".split(), None)
        run(h, "SET str v")
        assert "WRONGTYPE" in h.execute("XGAPPLY XGLAST str g 1-0".split(), None)


class TestReplicationPayloads:
    def test_group_commands_forward_records_wrapped_in_xgapply(self, h):
        seed(h, 2)
        tokens = "XREADGROUP GROUP g c STREAMS s >".split()
        reply, records = h.execute_records(tokens)
        payloads = h.replication_payloads(tokens, reply, records)
        assert payloads == [["XGAPPLY", *r] for r in records]
        assert all(p[0] == "XGAPPLY" for p in payloads) and "XREADGROUP" not in str(payloads)

    def test_a_group_command_that_changed_nothing_forwards_nothing(self, h):
        seed(h)
        run(h, "XREADGROUP GROUP g c STREAMS s >")
        tokens = "XREADGROUP GROUP g c STREAMS s >".split()
        reply, records = h.execute_records(tokens)
        assert h.replication_payloads(tokens, reply, records) == []

    def test_other_writes_forward_as_before(self, h):
        tokens = ["XADD", "s", "*", "k", "v"]
        reply, records = h.execute_records(tokens)
        assert h.replication_payloads(tokens, reply, records) == [
            ["XADD", "s", str(reply), "k", "v"]
        ]
        t2 = ["SET", "a", "b"]
        assert h.replication_payloads(t2, "OK", []) == [t2]

    def test_replica_converges_with_master(self, h, tmp_path, monkeypatch):
        master_cmds = [
            "XADD s * a 1",
            "XADD s * a 2",
            "XADD s * a 3",
            "XGROUP CREATE s g 0",
            "XREADGROUP GROUP g a COUNT 2 STREAMS s >",
            "XREADGROUP GROUP g b STREAMS s >",
            "XREADGROUP GROUP g a STREAMS s 0",
            "XACK s g " + "{first}",
            "XGROUP SETID s g 0",
            "XGROUP CREATECONSUMER s g idle",
            "XGROUP DELCONSUMER s g b",
        ]
        forwarded = []
        first = None
        for line in master_cmds:
            line = line.replace("{first}", str(first))
            tokens = line.split()
            reply, records = h.execute_records(tokens)
            if first is None:
                first = reply
            forwarded.extend(h.replication_payloads(tokens, reply, records))

        sub = tmp_path / "replica"
        sub.mkdir()
        monkeypatch.chdir(sub)
        replica = KedisStore(aof_filename="r.aof", appendfsync="always", lru_maxsize=100_000)
        try:
            rh = CommandHandler(replica)
            for tokens in forwarded:
                rh.execute(tokens, None)
            m = h.store._data["s"].to_state()
            r = replica._data["s"].to_state()
            assert r["groups"] == m["groups"] and r["entries"] == m["entries"]
        finally:
            replica.shutdown()


def test_error_reply_wraps_codes_and_keeps_err_messages():
    assert CommandHandler.error_reply(StreamError("ERR bad")) == "-ERR bad"
    assert CommandHandler.error_reply(StreamError("NOGROUP nope")) == "-ERR NOGROUP nope"
    assert CommandHandler.error_reply(TypeError("WRONGTYPE x")) == "-ERR WRONGTYPE x"
