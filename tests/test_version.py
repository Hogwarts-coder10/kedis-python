"""The version is written in three places; they must agree."""

import importlib.metadata
import re
from pathlib import Path

import pytest

import kedis_python

PACKAGE = Path(kedis_python.__file__).resolve().parent


def declared_version() -> str:
    pyproject = PACKAGE.parent / "pyproject.toml"
    if pyproject.exists():  # a source checkout
        text = pyproject.read_text(encoding="utf-8")
        return re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)
    try:  # an installed package
        return importlib.metadata.version("kedis-python")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("neither a source checkout nor an installed package")


def test_the_cli_banner_default_matches_the_package_version():
    text = (PACKAGE / "ui.py").read_text(encoding="utf-8")
    shown = re.search(r'def print_banner\(version="([^"]+)"', text).group(1)
    assert shown == declared_version()


def test_the_info_command_version_matches_the_package_version():
    text = (PACKAGE / "main.py").read_text(encoding="utf-8")
    shown = re.search(r'cmd == "INFO":\s+version = "([^"]+)"', text).group(1)
    assert shown == declared_version()
