"""Keeps the list/set/hash/sorted-set/bitmap/HyperLogLog docs honest.

* every command of those families is documented in docs/commands.md
* every "Not implemented yet" claim is true (so adding a command prompts a
  docs update instead of leaving a stale claim behind)
* the behaviours the docs call out as quirks are pinned: if you fix one, update
  docs/commands.md in the same change.
"""

import re
from pathlib import Path

import pytest

from kedis_python.commands import CommandHandler
from kedis_python.store import KedisStore
from kedis_python.textcodec import to_bytes

DOCS = Path(__file__).resolve().parent.parent / "docs" / "commands.md"

FAMILIES = {
    "lists": ["LPUSH", "RPUSH", "LPOP", "RPOP", "LRANGE"],
    "sets": ["SADD", "SREM", "SMEMBERS"],
    "hashes": ["HSET", "HGET", "HGETALL"],
    "sorted sets": ["ZADD", "ZRANGE"],
    "bitmaps": ["SETBIT", "GETBIT", "BITCOUNT", "BITOP"],
    "hyperloglog": ["PFADD", "PFCOUNT", "PFMERGE"],
}
ALL_COMMANDS = [c for cmds in FAMILIES.values() for c in cmds]


def doc_text() -> str:
    return DOCS.read_text(encoding="utf-8")


def headings(text: str) -> set:
    return {m.upper() for m in re.findall(r"^### `([A-Za-z]+)", text, flags=re.M)}


def not_implemented_claims(text: str) -> list:
    names = []
    for line in text.splitlines():
        if line.startswith("> **Not implemented yet:**"):
            names += re.findall(r"`([A-Z]+)`", line)
    return names


class TestCoverage:
    @pytest.mark.parametrize("command", ALL_COMMANDS)
    def test_the_command_exists_and_is_documented(self, command):
        assert command in CommandHandler(None).public_commands
        assert command in headings(doc_text()), f"{command} has no ### heading in docs/commands.md"

    def test_every_family_has_a_section(self):
        text = doc_text()
        for title in ("Lists", "Sets", "Hashes", "Sorted Sets", "Bitmaps", "HyperLogLog"):
            assert re.search(rf"^## .*{title}", text, flags=re.M), title

    def test_not_implemented_claims_are_still_true(self):
        claims = not_implemented_claims(doc_text())
        assert len(claims) >= 20  # the lists are really there
        implemented = [c for c in claims if c in CommandHandler(None).public_commands]
        assert not implemented, (
            f"{implemented} now exist: document them and remove them from the "
            "'Not implemented yet' lines in docs/commands.md"
        )


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    yield CommandHandler(store)
    store.shutdown()


def run(h, line):
    return h.execute(line.split())


class TestDocumentedBehaviour:
    def test_return_values_the_docs_describe(self, h):
        assert run(h, "RPUSH l a b c") == 3
        assert run(h, "LPUSH l x y") == 5  # the last value pushed ends up first
        assert run(h, "LRANGE l 0 -1") == ["y", "x", "a", "b", "c"]
        assert run(h, "SADD s a a b") == 2 and run(h, "SADD s a") == 0
        assert run(h, "SREM s a nope") == 1
        assert run(h, "HSET u f v") == 1 and run(h, "HSET u f w") == 0
        assert run(h, "ZADD z 1 a 2 b") == 2 and run(h, "ZADD z 5 a") == 0  # updates aren't counted
        assert run(h, "PFADD p a b") == 1 and run(h, "PFADD p a") == 0
        assert run(h, "SETBIT k 7 1") == 0 and run(h, "SETBIT k 7 1") == 1  # previous bit

    def test_hset_value_is_everything_after_the_field(self, h):
        run(h, "HSET user bio likes databases")
        assert run(h, "HGET user bio") == "likes databases"

    def test_emptied_collections_disappear(self, h):
        run(h, "RPUSH l a")
        run(h, "LPOP l")
        run(h, "SADD s a")
        run(h, "SREM s a")
        assert run(h, "EXISTS l") == 0 and run(h, "EXISTS s") == 0

    def test_zrange_orders_by_score_then_member_and_clamps(self, h):
        run(h, "ZADD t 1 b 1 a 1 c 0 z")
        assert run(h, "ZRANGE t 0 -1") == ["z", "a", "b", "c"]
        assert run(h, "ZRANGE t -2 99") == ["b", "c"]
        assert run(h, "ZRANGE t 0 1 withscores") == ["z", "0", "a", "1"]

    def test_bitcount_range_is_in_bytes_and_bit_zero_is_the_high_bit(self, h):
        run(h, "SETBIT f 7 1")
        run(h, "SETBIT f 9 1")  # bits 7 and 9 live in bytes 0 and 1
        assert run(h, "BITCOUNT f") == 2
        assert run(h, "BITCOUNT f 0 0") == 1
        assert run(h, "BITCOUNT f -1 -1") == 1
        run(h, "SETBIT g 0 1")
        assert to_bytes(h.store.get("g")) == b"\x80"

    def test_bitop_pads_short_sources_and_reports_bytes(self, h):
        run(h, "SETBIT a 0 1")
        run(h, "SETBIT b 9 1")
        assert run(h, "BITOP OR both a b") == 2
        assert run(h, "BITCOUNT both") == 2

    def test_bitmaps_and_hyperloglogs_report_string(self, h):
        run(h, "SETBIT k 1 1")
        run(h, "PFADD p a")
        assert run(h, "TYPE k") == "string" and run(h, "TYPE p") == "string"

    def test_pfcount_of_several_keys_is_the_union_and_changes_nothing(self, h):
        run(h, "PFADD a x y")
        run(h, "PFADD b y z")
        assert run(h, "PFCOUNT a b") == 3
        assert run(h, "PFCOUNT a") == 2 and run(h, "PFCOUNT b") == 2
        assert run(h, "PFCOUNT nope") == 0

    def test_wrong_family_is_wrongtype_and_expire_works_on_all(self, h):
        run(h, "SET str v")
        for line in ("LPUSH str a", "SADD str a", "HSET str f v", "ZADD str 1 a", "PFADD str a"):
            assert "WRONGTYPE" in run(h, line), line
        for line in ("RPUSH k1 a", "SADD k2 a", "HSET k3 f v", "ZADD k4 1 a", "SETBIT k5 1 1", "PFADD k6 a"):
            run(h, line)
        for n in range(1, 7):
            assert run(h, f"EXPIRE k{n} 100") == 1


class TestDocumentedLimits:
    """Current behaviour the docs list as limits. If you fix one, update the docs."""

    def test_hset_takes_a_single_field(self, h):
        run(h, "HSET u a 1 b 2")
        assert run(h, "HGETALL u") == ["a", "1 b 2"]
