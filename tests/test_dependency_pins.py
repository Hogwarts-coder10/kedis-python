"""The runtime dependencies are declared twice (pyproject.toml, which `pip install`
reads, and requirements.txt); they must name the same ranges, and every one of
them must be bounded above, so an install cannot pull in a major release the
test suite has never run against."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pyproject_dependencies():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M).group(1)
    return sorted(re.findall(r'"([^"]+)"', block))


def requirements():
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return sorted(line.strip() for line in lines if line.strip() and not line.startswith("#"))


def test_pyproject_and_requirements_agree():
    assert pyproject_dependencies() == requirements()


def test_every_dependency_has_an_upper_bound():
    for dep in pyproject_dependencies():
        assert re.search(r"<\s*\d", dep), f"{dep} has no upper bound"
