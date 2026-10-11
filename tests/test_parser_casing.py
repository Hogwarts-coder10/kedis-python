"""The parser normalises the command name and nothing else: every other token
is data, so words like 'by' or 'limit' must reach the store exactly as typed.
Options are matched case-insensitively by the handlers themselves."""

import pytest

from kedis_python.commands import CommandHandler
from kedis_python.parser import CommandParser
from kedis_python.store import KedisStore

FORMER_FLAGS = ["by", "limit", "asc", "desc", "alpha", "withscores", "By", "WithScores"]


def inline(line: str) -> list:
    tokens, used = CommandParser.parse((line + "\n").encode())
    assert used == len(line) + 1
    return tokens


def kesp(*tokens: str) -> list:
    body = b"".join(f"S{len(t.encode())}\n{t}\n".encode() for t in tokens)
    parsed, used = CommandParser.parse(f"A{len(tokens)}\n".encode() + body)
    assert used > 0
    return parsed


class TestParserLeavesDataAlone:
    @pytest.mark.parametrize("word", FORMER_FLAGS)
    def test_inline_value_key_and_field_positions(self, word):
        assert inline(f"SET k {word}") == ["SET", "k", word]
        assert inline(f"SET {word} v") == ["SET", word, "v"]
        assert inline(f"XADD s * {word} {word}") == ["XADD", "s", "*", word, word]
        assert inline(f"XGROUP CREATE s {word} 0") == ["XGROUP", "CREATE", "s", word, "0"]

    @pytest.mark.parametrize("word", FORMER_FLAGS)
    def test_kesp_framing(self, word):
        assert kesp("SET", "k", word) == ["SET", "k", word]
        assert kesp("HSET", word, word, word) == ["HSET", word, word, word]

    def test_the_command_name_is_still_uppercased(self):
        assert inline("set k v")[0] == "SET"
        assert inline("xAdd s * a b")[0] == "XADD"
        assert kesp("get", "k")[0] == "GET"

    def test_arguments_keep_their_case(self):
        assert inline("SET Key Value") == ["SET", "Key", "Value"]
        assert kesp("set", "mIxEd", "CaSe") == ["SET", "mIxEd", "CaSe"]


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    yield CommandHandler(store)
    store.shutdown()


def run(h, line):
    return h.execute(inline(line))


class TestEndToEnd:
    @pytest.mark.parametrize("word", ["by", "limit", "asc", "desc", "alpha", "withscores"])
    def test_data_round_trips_unchanged(self, h, word):
        assert run(h, f"SET {word} {word}") == "OK"
        assert run(h, f"GET {word}") == word
        run(h, f"HSET h {word} {word}")
        assert run(h, f"HGET h {word}") == word
        run(h, f"XADD s 1-0 {word} {word}")
        assert run(h, "XRANGE s - +") == [["1-0", [word, word]]]

    def test_a_group_can_be_named_like_a_former_flag(self, h):
        run(h, "XADD s 1-0 k v")
        assert run(h, "XGROUP CREATE s by 0") == "OK"
        assert run(h, "XREADGROUP GROUP by limit STREAMS s >") == [["s", [["1-0", ["k", "v"]]]]]
        assert run(h, "XPENDING s by")[3] == [["limit", "1"]]
        assert run(h, "XACK s by 1-0") == 1

    def test_options_still_work_in_any_case(self, h):
        run(h, "ZADD z 1 a")
        run(h, "ZADD z 2 b")
        assert run(h, "zrange z 0 -1 withscores") == ["a", "1", "b", "2"]
        assert run(h, "ZRANGE z 0 -1 WithScores") == ["a", "1", "b", "2"]
        assert run(h, "zrange z 0 -1") == ["a", "b"]
        for i in range(2, 8):
            assert run(h, f"xadd t maxlen ~ 3 limit 10 {i}-0 k v") == f"{i}-0"
        assert run(h, "XLEN t") == 3
