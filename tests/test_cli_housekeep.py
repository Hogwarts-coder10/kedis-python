"""HOUSEKEEP is drawn as a table in the CLI, from the real engine report."""

import io

import pytest
from rich.console import Console

from kedis_python import main as cli_main
from kedis_python import ui
from kedis_python.commands import CommandHandler
from kedis_python.main import KedisClient
from kedis_python.store import KedisStore
from kedis_python.ui import UI


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    console = Console(file=out, width=110, force_terminal=False)
    monkeypatch.setattr(ui, "console", console)
    monkeypatch.setattr(cli_main, "console", console)
    store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    handler = CommandHandler(store)
    client = KedisClient()
    client.local_mode, client.handler = True, handler
    yield client, handler, out
    store.shutdown()


def typed(client, line):
    client._execute_and_render(line.split()[0].upper(), line)


class TestThroughTheCli:
    def test_housekeep_is_drawn_as_a_table(self, env):
        client, handler, out = env
        handler.execute(["SET", "a", "1"])
        typed(client, "HOUSEKEEP")
        text = out.getvalue()
        assert "Housekeeping Pass" in text
        for label in (
            "Expired keys swept", "Orphan expiry entries", "Orphan LRU entries",
            "Untracked keys adopted", "Memory reclaimed", "Duration", "Keys now",
        ):  # fmt: skip
            assert label in text
        assert "expire_orphans:" not in text  # not the raw name:value dump
        assert "│" in text  # a real table

    def test_a_tidy_store_says_so(self, env):
        client, handler, out = env
        handler.execute(["SET", "a", "1"])
        typed(client, "HOUSEKEEP")
        assert "Nothing needed cleaning" in out.getvalue()

    def test_expired_keys_are_counted_and_highlighted(self, env):
        client, handler, out = env
        for k in "abc":
            handler.execute(["SET", k, "1"])
            handler.store._expires[k] = 0  # already past, never touched again
        typed(client, "HOUSEKEEP")
        text = out.getvalue()
        assert "Cleaned up" in text and "Nothing needed" not in text
        row = next(line for line in text.splitlines() if "Expired keys swept" in line)
        assert " 3 " in row
        assert handler.execute(["EXISTS", "a", "b", "c"]) == 0

    def test_wrong_arity_prints_the_error_not_a_table(self, env):
        client, _, out = env
        typed(client, "HOUSEKEEP now")
        text = out.getvalue()
        assert "wrong number of arguments" in text
        assert "Housekeeping Pass" not in text


class TestRenderHousekeep:
    REPORT = (
        "expired:3\nexpire_orphans:0\nlru_orphans:1\nlru_adopted:0\n"
        "reclaimed_bytes:20480\nduration_ms:0.412\nkeys:9"
    )

    def render(self, monkeypatch, response):
        out = io.StringIO()
        monkeypatch.setattr(ui, "console", Console(file=out, width=110, force_terminal=False))
        UI.render_housekeep(response)
        return out.getvalue()

    def test_bytes_and_milliseconds_are_readable(self, monkeypatch):
        text = self.render(monkeypatch, self.REPORT)
        assert "20.0 KB" in text and "0.412 ms" in text

    @pytest.mark.parametrize(
        "n, shown", [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (5 * 1024**2, "5.0 MB")]
    )
    def test_human_bytes(self, n, shown):
        assert UI._human_bytes(n) == shown

    def test_an_unknown_counter_still_shows(self, monkeypatch):
        text = self.render(monkeypatch, self.REPORT + "\nnew_counter:7")
        assert "new_counter" in text and "7" in text

    def test_an_error_reply_is_plain_text(self, monkeypatch):
        text = self.render(monkeypatch, "-ERR wrong number of arguments for 'housekeep' command")
        assert "wrong number" in text and "Housekeeping Pass" not in text

    def test_values_are_not_read_as_markup(self, monkeypatch):
        text = self.render(monkeypatch, "keys:[red]x[/red]")
        assert "[red]x[/red]" in text


class TestStatsHasNoStrayQuotes:
    """STATS was handed the quoted reply text, so its first and last lines
    showed a stray double quote."""

    def test_stats_panel_has_no_quote_characters(self, env):
        client, handler, out = env
        handler.execute(["SET", "a", "1"])
        typed(client, "STATS")
        text = out.getvalue()
        assert "DEEP MEMORY MAP" in text and "Total Keys" in text
        assert '"' not in text
