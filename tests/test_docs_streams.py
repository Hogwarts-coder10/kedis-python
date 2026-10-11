"""Keeps the stream documentation honest: every stream command a client can
issue is documented, nothing documented is made up, and the links between the
docs resolve (including their #anchors)."""

import re
from pathlib import Path

import pytest

from kedis_python.commands import CommandHandler

ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = ["README.md", "docs/streams.md", "docs/commands.md", "docs/persistence.md"]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def stream_commands() -> set:
    return {c for c in CommandHandler(None).public_commands if c.startswith("X")}


def command_headings(text: str) -> set:
    """Command names from headings written like: ### `XADD key ...`"""
    return {m.upper() for m in re.findall(r"^### `([A-Za-z]+)", text, flags=re.M)}


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation and emoji
    dropped, spaces become hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(text: str) -> set:
    out, fenced = set(), False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced:
            m = re.match(r"#{1,6} (.*)", line)
            if m:
                out.add(slug(m.group(1)))
    return out


class TestCommandCoverage:
    def test_every_public_stream_command_is_documented(self):
        missing = stream_commands() - command_headings(read("docs/commands.md"))
        assert not missing, f"undocumented in docs/commands.md: {sorted(missing)}"

    def test_every_documented_stream_command_exists(self):
        documented = {c for c in command_headings(read("docs/commands.md")) if c.startswith("X")}
        assert documented <= stream_commands(), sorted(documented - stream_commands())

    def test_the_internal_command_is_never_advertised(self):
        assert "XGAPPLY" in CommandHandler(None)._commands  # it does exist...
        for rel in ("docs/commands.md", "README.md"):
            assert "XGAPPLY" not in read(rel), rel  # ...but is not a user command

    def test_the_known_gaps_are_stated(self):
        text = read("docs/streams.md")
        for gap in ("XCLAIM", "XAUTOCLAIM", "XINFO STREAM", "ENTRIESREAD", "Exact trimming"):
            assert gap in text, gap


class TestRoadmap:
    def test_streams_and_groups_are_ticked(self):
        text = read("docs/roadmap.md")
        assert "- [x] Stream data type" in text
        assert "- [x] Consumer Groups" in text


class TestLinks:
    @pytest.mark.parametrize("rel", DOC_FILES)
    def test_local_markdown_links_and_anchors_resolve(self, rel):
        base = (ROOT / rel).parent
        for target, frag in re.findall(r"\]\(([^)#\s]*)(?:#([^)\s]*))?\)", read(rel)):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if target and not target.endswith(".md"):
                continue  # images and other assets are not checked here
            path = (base / target).resolve() if target else (ROOT / rel)
            assert path.exists(), f"{rel}: link to missing file {target!r}"
            if frag:
                assert frag in anchors(path.read_text(encoding="utf-8")), (
                    f"{rel}: no heading for #{frag} in {path.name}"
                )

    def test_the_readme_links_to_the_streams_page(self):
        assert "docs/streams.md" in read("README.md")
