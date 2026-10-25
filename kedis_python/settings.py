"""Command-line flags for the server: where it listens and the limits it enforces.

One table (SETTINGS) drives everything: the flags, their parsing and validation,
the defaults shown by --help, and the tests that keep docs/limits.md honest. A
setting is just an attribute somewhere in the package (``server.MAX_CLIENTS``,
``CommandParser.MAX_COMMAND_BYTES``, ...); a flag overwrites that attribute
before the server starts. Defaults are never repeated here: --help reads them
from the attributes themselves.
"""

import argparse
import importlib
import math
import re
import sys
from dataclasses import dataclass

_NUMBER = re.compile(r"^\s*(-?[0-9][0-9_,]*(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*$")

# Binary, as everywhere else in the server's docs: 1 KB is 1024 bytes.
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
}
_TIME_UNITS = {"": 1.0, "s": 1.0, "ms": 0.001, "m": 60.0, "h": 3600.0}


def _split(text: str, units: dict, what: str):
    match = _NUMBER.match(text)
    if not match:
        raise ValueError(f"not {what}: {text!r}")
    number, unit = match.group(1).replace(",", "").replace("_", ""), match.group(2).lower()
    if unit not in units:
        raise ValueError(f"unknown unit {match.group(2)!r} in {text!r}")
    return float(number), units[unit]


def parse_count(text: str) -> int:
    """A whole number: 10000, 10_000 or 10,000."""
    match = _NUMBER.match(text)
    if not match or match.group(2) or "." in match.group(1):
        raise ValueError(f"not a whole number: {text!r}")
    return int(match.group(1).replace(",", "").replace("_", ""))


def parse_size(text: str) -> int:
    """A number of bytes: 65536, 64KiB, 64k, 1.5MiB, 2G (suffixes are binary)."""
    number, factor = _split(text, _SIZE_UNITS, "a size")
    value = number * factor
    if value != int(value):
        raise ValueError(f"not a whole number of bytes: {text!r}")
    return int(value)


def parse_seconds(text: str) -> float:
    """A duration: 30, 0.5, 30s, 500ms, 2m, 1h."""
    number, factor = _split(text, _TIME_UNITS, "a duration")
    value = number * factor
    if not math.isfinite(value):
        raise ValueError(f"not a finite duration: {text!r}")
    return value


def format_size(n) -> str:
    for suffix, factor in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= factor and n % factor == 0:
            return f"{n // factor}{suffix}"
    return f"{n:,}" if isinstance(n, int) and n >= 10_000 else str(n)


def format_seconds(n) -> str:
    return f"{n:g}s"


def _module(name: str):
    """The module that is actually running. `python -m kedis_python.server` runs
    server.py as __main__, a second copy of the module under another name; the
    flags must reach that copy, not a fresh import of the first."""
    main = sys.modules.get("__main__")
    if getattr(getattr(main, "__spec__", None), "name", None) == name:
        return main
    return importlib.import_module(name)


@dataclass(frozen=True)
class Setting:
    flag: str  # without the leading dashes
    owner: str  # "module" or "module:Class" holding the attribute
    attr: str
    kind: str  # count | size | seconds | rate | port | host
    help: str
    minimum: float = 1  # inclusive; for seconds the value must be above it
    maximum: float = None

    @property
    def dest(self) -> str:
        return self.attr.lower()

    def target(self):
        module, _, cls = self.owner.partition(":")
        obj = _module(module)
        return getattr(obj, cls) if cls else obj

    def current(self):
        return getattr(self.target(), self.attr)

    def show(self, value) -> str:
        if self.kind == "size":
            return format_size(value)
        if self.kind == "rate":
            return f"{format_size(value)}/s"
        if self.kind == "seconds":
            return format_seconds(value)
        if self.kind == "count":
            return f"{value:,}"
        return str(value)

    def convert(self, text: str):
        """text -> the value to store; raises ValueError saying what is wrong."""
        if self.kind == "host":
            if not text.strip():
                raise ValueError("must not be empty")
            return text.strip()
        if self.kind in ("count", "port"):
            value = parse_count(text)
        elif self.kind in ("size", "rate"):
            value = parse_size(text)
        else:
            value = parse_seconds(text)
        if self.kind == "seconds":
            if value <= 0:
                raise ValueError(f"must be above 0 (got {text!r})")
        else:
            low = 1 if self.kind == "port" else self.minimum
            if value < low:
                raise ValueError(f"must be at least {low:g} (got {text!r})")
        high = 65535 if self.kind == "port" else self.maximum
        if high is not None and value > high:
            raise ValueError(f"must be at most {high:g} (got {text!r})")
        return value


_SERVER = "kedis_python.server"
_PARSER = "kedis_python.parser:CommandParser"
_REPLY = "kedis_python.parser:KESPDecoder"

SETTINGS = [
    Setting("host", _SERVER, "HOST", "host", "address to listen on"),
    Setting("port", _SERVER, "PORT", "port", "TCP port to listen on"),
    # what a connection may cost
    Setting("max-clients", _SERVER, "MAX_CLIENTS", "count", "connections served at once; more are refused"),
    Setting("stall-timeout", _SERVER, "STALL_TIMEOUT", "seconds", "drop a half-sent command that gets no new byte for this long"),
    Setting("frame-deadline", _SERVER, "FRAME_DEADLINE", "seconds", "base time a command may take to arrive, however it trickles in"),
    Setting("frame-min-rate", _SERVER, "FRAME_MIN_RATE", "rate", "bytes/second added to that time for each byte of the command"),
    Setting("write-deadline", _SERVER, "WRITE_DEADLINE", "seconds", "base time a client or follower gets to read what it was sent"),
    Setting("write-min-rate", _SERVER, "WRITE_MIN_RATE", "rate", "bytes/second added to that time for each queued byte"),
    Setting("yield-every", _SERVER, "YIELD_EVERY", "count", "commands run back to back before other clients get a turn"),
    Setting("encode-chunk", _SERVER, "ENCODE_CHUNK", "count", "reply elements encoded before other clients get a turn"),
    Setting("max-multi-commands", _SERVER, "MAX_MULTI_COMMANDS", "count", "most commands one MULTI block may queue"),
    Setting("max-multi-bytes", _SERVER, "MAX_MULTI_BYTES", "size", "most argument bytes one MULTI block may hold"),
    # what a command may be
    Setting("max-inline-length", _PARSER, "MAX_INLINE_LENGTH", "size", "longest inline (plain text) command line"),
    Setting("max-array-length", _PARSER, "MAX_ARRAY_LENGTH", "count", "most elements in one command"),
    Setting("max-bulk-length", _PARSER, "MAX_BULK_LENGTH", "size", "longest single string in a command"),
    Setting("max-command-bytes", _PARSER, "MAX_COMMAND_BYTES", "size", "all string bytes in one command, added up"),
    # what the server will send
    Setting("max-reply-elements", _REPLY, "MAX_REPLY_ELEMENTS", "count", "values in one reply, containers included"),
    Setting("max-reply-payload", _REPLY, "MAX_REPLY_PAYLOAD", "size", "string bytes in one reply, added up"),
    Setting("max-subscriber-buffer", "kedis_python.commands:CommandHandler", "MAX_SUBSCRIBER_BUFFER", "size", "bytes a pub/sub subscriber may leave unread"),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kedis-server",
        description=(
            "Kedis server. Every flag overrides one limit; sizes take B, KiB, MiB, "
            "GiB (or K, M, G: all binary), durations take s, ms, m, h. "
            "See docs/limits.md."
        ),
    )
    for setting in SETTINGS:
        parser.add_argument(
            f"--{setting.flag}",
            dest=setting.dest,
            metavar=_METAVAR[setting.kind],
            type=_converter(setting),
            default=None,
            help=f"{setting.help} (default: {setting.show(setting.current())})",
        )
    return parser


_METAVAR = {
    "count": "N",
    "port": "PORT",
    "host": "ADDRESS",
    "size": "SIZE",
    "rate": "SIZE",
    "seconds": "TIME",
}


def _converter(setting: Setting):
    def convert(text: str):
        try:
            return setting.convert(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc))

    convert.__name__ = setting.flag  # shown by argparse in its error message
    return convert


def apply(args: argparse.Namespace) -> list:
    """Sets every attribute whose flag was given; returns [(Setting, value), ...]."""
    applied = []
    for setting in SETTINGS:
        value = getattr(args, setting.dest, None)
        if value is not None:
            setattr(setting.target(), setting.attr, value)
            applied.append((setting, value))
    return applied


def parse_and_apply(argv=None) -> list:
    """Reads the flags (sys.argv[1:] by default) and applies them. Nothing is
    applied unless every flag is valid; a bad flag exits with status 2."""
    return apply(build_parser().parse_args(argv))
