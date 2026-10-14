"""The CLI draws stream replies as tables: XRANGE, XREVRANGE, XREAD and
XREADGROUP (whose reply has the same nested shape as XREAD)."""

import io

import pytest
from rich.console import Console

from kedis_python import main as cli_main
from kedis_python import ui
from kedis_python.commands import CommandHandler
from kedis_python.main import KedisClient
from kedis_python.parser import KESPEncoder
from kedis_python.store import KedisStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    monkeypatch.setattr(ui, "console", Console(file=out, width=140, force_terminal=False))
    store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    handler = CommandHandler(store)
    client = KedisClient()

    def run(line):
        """Types `line` into the CLI path: engine -> wire bytes -> decoded value -> view."""
        tokens = line.split()
        value = client._decode_kesp_value(KESPEncoder.encode(handler.execute(tokens)))
        drawn = tokens[0].upper() in client.STREAM_VIEWS and client._render_stream_reply(
            tokens[0].upper(), line, value
        )
        return drawn, out.getvalue()

    yield run, handler, out
    store.shutdown()


def seed(handler):
    handler.execute("XADD orders 1-0 item book qty 2".split())
    handler.execute("XADD orders 2-0 item pen qty 5".split())
    handler.execute("XGROUP CREATE orders shipping 0".split())


class TestXreadgroup:
    def test_is_registered_as_a_stream_view(self):
        assert KedisClient.STREAM_VIEWS["XREADGROUP"] == "Group Read"

    def test_new_entries_are_drawn_as_a_table(self, env):
        run, handler, _ = env
        seed(handler)
        drawn, text = run("XREADGROUP GROUP shipping w1 STREAMS orders >")
        assert drawn
        assert "Group Read (shipping/w1): orders (2 entries)" in text
        for cell in ("1-0", "2-0", "book", "pen", "item", "qty"):
            assert cell in text

    def test_count_limits_what_is_drawn(self, env):
        run, handler, _ = env
        seed(handler)
        drawn, text = run("XREADGROUP GROUP shipping w1 COUNT 1 STREAMS orders >")
        assert drawn and "(1 entry)" in text and "book" in text and "pen" not in text

    def test_a_history_read_is_drawn_too(self, env):
        run, handler, out = env
        seed(handler)
        run("XREADGROUP GROUP shipping w1 STREAMS orders >")
        out.truncate(0), out.seek(0)
        drawn, text = run("XREADGROUP GROUP shipping w1 STREAMS orders 0")
        assert drawn and "book" in text and "pen" in text

    def test_several_streams_each_get_a_table(self, env):
        run, handler, _ = env
        seed(handler)
        handler.execute("XADD other 1-0 k v".split())
        handler.execute("XGROUP CREATE other shipping 0".split())
        drawn, text = run("XREADGROUP GROUP shipping w1 STREAMS orders other > >")
        assert drawn
        assert "orders (2 entries)" in text and "other (1 entry)" in text

    def test_the_group_keyword_is_case_insensitive_and_noack_is_fine(self, env):
        run, handler, _ = env
        seed(handler)
        drawn, text = run("xreadgroup group shipping w1 NOACK streams orders >")
        assert drawn and "(shipping/w1)" in text

    def test_markup_in_names_and_values_is_not_interpreted(self, env):
        run, handler, _ = env
        handler.execute(["XADD", "s", "1-0", "f", "[red]boom[/red]"])
        handler.execute("XGROUP CREATE s [bold]g 0".split())
        drawn, text = run("XREADGROUP GROUP [bold]g [x]c STREAMS s >")
        assert drawn
        assert "[red]boom[/red]" in text
        assert "([bold]g/[x]c)" in text

    def test_nothing_new_is_not_drawn_so_the_text_path_shows_it(self, env):
        run, handler, _ = env
        seed(handler)
        run("XREADGROUP GROUP shipping w1 STREAMS orders >")
        drawn, _ = run("XREADGROUP GROUP shipping w1 STREAMS orders >")
        assert not drawn

    def test_an_error_reply_is_not_drawn(self, env):
        run, handler, _ = env
        drawn, _ = run("XREADGROUP GROUP nope w1 STREAMS missing >")
        assert not drawn


class TestExistingViewsStillWork:
    def test_xread_xrange_xrevrange(self, env):
        run, handler, out = env
        seed(handler)
        assert run("XRANGE orders - +")[0]
        assert "Stream Range: orders (2 entries)" in out.getvalue()
        assert run("XREVRANGE orders + -")[0]
        assert "newest first" in out.getvalue()
        assert run("XREAD STREAMS orders 0")[0]
        assert "Stream Read: orders (2 entries)" in out.getvalue()
