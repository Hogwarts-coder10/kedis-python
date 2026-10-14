"""Keeps `HELP <command>` honest.

* every command a client can issue has a help entry, and nothing is documented
  that does not exist
* the topics, aliases and families are consistent and every page renders
* every example in the help runs against a real handler without an unknown-
  command or arity error
* the behaviours the notes call out as limits or known issues are pinned: if
  you fix one, update kedis_python/help_data.py in the same change.
"""

import shlex

import pytest
from rich.text import Text

from kedis_python import help_data
from kedis_python.commands import CommandHandler
from kedis_python.store import KedisStore

# Handled by the CLI/session layer (main.py, network.py) rather than by the
# engine's command table, but still real commands a person types.
CLI_COMMANDS = {
    "CLEAR", "DEBUG", "DISCARD", "DISCONNECT", "EXEC", "EXIT", "HELP", "INFO",
    "MODE", "MULTI", "RECONNECT", "REPLICAOF", "UNWATCH", "WATCH",
}  # fmt: skip

ENGINE_COMMANDS = set(CommandHandler(None).public_commands)


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = KedisStore(aof_filename="test.aof", appendfsync="always", lru_maxsize=100_000)
    yield CommandHandler(store)
    store.shutdown()


def run(h, line):
    return h.execute(shlex.split(line))


class TestCoverage:
    def test_every_engine_command_has_an_entry(self):
        missing = ENGINE_COMMANDS - set(help_data.COMMANDS)
        assert not missing, f"no HELP entry for: {sorted(missing)}"

    def test_every_entry_is_a_real_command(self):
        unknown = set(help_data.COMMANDS) - ENGINE_COMMANDS - CLI_COMMANDS
        assert not unknown, f"HELP documents commands that do not exist: {sorted(unknown)}"

    def test_cli_commands_are_not_engine_commands(self):
        # If one becomes an engine command, move it out of CLI_COMMANDS.
        assert not CLI_COMMANDS & ENGINE_COMMANDS

    def test_the_internal_command_stays_undocumented(self):
        assert "XGAPPLY" not in help_data.COMMANDS


class TestStructure:
    def test_every_command_is_in_exactly_one_family(self):
        seen = {}
        for key, (_title, names) in help_data.FAMILIES.items():
            for name in names:
                assert name in help_data.COMMANDS, f"{key} lists unknown command {name}"
                assert name not in seen, f"{name} is in both {seen[name]} and {key}"
                seen[name] = key
        assert set(seen) == set(help_data.COMMANDS)

    def test_aliases_point_at_real_targets_and_never_shadow_a_command(self):
        for alias, target in help_data.COMMAND_ALIASES.items():
            assert target in help_data.COMMANDS
            assert alias not in help_data.COMMANDS
        for alias, key in help_data.FAMILY_ALIASES.items():
            assert key in help_data.FAMILIES
            assert alias not in help_data.COMMANDS, f"{alias} is a command: a command wins"
            assert alias not in help_data.FAMILIES

    @pytest.mark.parametrize("name", sorted(help_data.COMMANDS))
    def test_entry_is_complete_and_well_formed(self, name):
        entry = help_data.COMMANDS[name]
        assert entry.name == name
        for form in entry.forms:
            assert form.split()[0] == name, f"{form!r} does not start with {name}"
        assert entry.summary and not entry.summary.endswith(".")  # render() adds the dot
        assert entry.details and entry.returns and entry.examples
        for example in entry.examples:
            assert example.split()[0].upper() in set(help_data.COMMANDS) | set(
                help_data.COMMAND_ALIASES
            )

    @pytest.mark.parametrize("topic", help_data.topics())
    def test_every_topic_renders_valid_markup(self, topic):
        rendered = help_data.render(topic)
        assert rendered
        Text.from_markup(rendered)  # raises on a broken tag
        assert help_data.title(topic)

    def test_unknown_topics_have_no_page(self):
        assert help_data.render("NOPE") is None

    def test_a_command_name_beats_a_topic_name(self):
        assert "SET key value" in help_data.render("set")
        assert help_data.title("sets") == "Set Commands"
        assert "SMEMBERS" in help_data.render("sets")

    def test_aliases_render_the_page_they_point_at(self):
        assert help_data.render("CLS") == help_data.render("CLEAR")
        assert help_data.render("QUIT") == help_data.render("EXIT")

    def test_the_overview_hint_names_every_topic(self):
        hint = help_data.topics_hint()
        for key in help_data.FAMILIES:
            assert key.lower() in hint


SEED = [
    "XADD orders * item book",
    "XGROUP CREATE orders shipping 0",
    "RPUSH tasks a b c",
    "SADD tags a b",
    "HSET user name ada",
    "ZADD board 1 a 2 b",
    "SETBIT flags 1 1",
    "PFADD visitors a b",
    "SET key value",
]


def engine_examples():
    for name, entry in sorted(help_data.COMMANDS.items()):
        if name in ENGINE_COMMANDS:
            for example in entry.examples:
                if example.split()[0].upper() in ENGINE_COMMANDS:
                    yield pytest.param(example, id=example)


class TestExamplesRun:
    @pytest.mark.parametrize("example", list(engine_examples()))
    def test_example_is_accepted_by_the_engine(self, h, example):
        if example.split()[0].upper() == "SUBSCRIBE":
            pytest.skip("SUBSCRIBE needs a client socket")
        for line in SEED:
            run(h, line)
        reply = str(run(h, example))
        lowered = reply.lower()
        assert "unknown command" not in lowered, reply
        assert "wrong number" not in lowered, reply
        assert "syntax" not in lowered, reply
        assert "nogroup" not in lowered, reply


class TestNotesArePinned:
    def test_del_and_exists_take_several_keys(self, h):
        run(h, "SET a 1")
        run(h, "SET b 2")
        assert run(h, "EXISTS a b c") == 2
        assert run(h, "DEL a b c") == 2
        for name in ("DEL", "EXISTS"):
            assert "[key ...]" in help_data.COMMANDS[name].syntax

    def test_xgroup_entriesread_is_not_supported(self, h):
        run(h, "XADD s * a 1")
        assert str(run(h, "XGROUP CREATE s g $ ENTRIESREAD 1")).startswith("-ERR")

    def test_xinfo_has_no_stream_subcommand_and_lag_is_nil(self, h):
        run(h, "XADD s * a 1")
        run(h, "XGROUP CREATE s g 0")
        assert str(run(h, "XINFO STREAM s")).startswith("-ERR")
        assert "nil" in help_data.COMMANDS["XINFO"].notes[0]

    def test_publish_no_longer_carries_a_known_issue_note(self):
        assert not help_data.COMMANDS["PUBLISH"].notes


class TestPlainOverview:
    """The plain `HELP` cheat sheet is a hand-written block in main.py; keep it
    from falling behind the commands."""

    def test_every_command_appears_in_the_overview(self):
        import re
        from pathlib import Path

        import kedis_python.main as cli

        source = Path(cli.__file__).read_text(encoding="utf-8")
        start = source.index("help_text = (")
        overview = source[start : source.index("UI.print_panel(help_text", start)]
        missing = [n for n in help_data.COMMANDS if not re.search(rf"\b{n}\b", overview)]
        assert not missing, f"missing from the plain HELP overview: {missing}"

    def test_bracketed_text_survives_rendering(self):
        """Rich reads [lowercase ...] as a style tag and silently drops it, so
        optional-argument brackets in the overview must be escaped as \\[...]."""
        import io
        from pathlib import Path

        from rich.console import Console

        import kedis_python.main as cli

        source = Path(cli.__file__).read_text(encoding="utf-8")
        start = source.index("help_text = (")
        block = source[start : source.index("UI.print_panel(help_text", start)]
        namespace = {}
        exec(block.replace("\n                ", "\n").strip(), {}, namespace)  # noqa: S102
        console = Console(file=io.StringIO(), width=200, force_terminal=False)
        console.print(namespace["help_text"])
        rendered = console.file.getvalue()
        for shown in ("DEL key [key ...]", "EXISTS key [key ...]", "BITCOUNT key [s e]", "key [group]"):
            assert shown in rendered, f"{shown!r} was swallowed by Rich markup"
