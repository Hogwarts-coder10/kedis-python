"""Runs the fuzzer in short, seeded bursts, and tests the fuzzer itself.

Long sessions belong on the command line (see tests/fuzzing.py). Here:

* short fixed-seed runs of every target, in default and shrunken-limit mode;
* the reference readers against hand-picked frames from docs/protocol.md;
* harness self-tests: the shrinker shrinks, runs are deterministic, and a parser
  with a deliberately injected fault is caught, whether it raises, loops,
  balloons in memory, or merely disagrees with the reference;

The hypothesis property tests are in test_fuzz_properties.py, so that this file
still runs when hypothesis is not installed.
"""

import signal

import pytest

import fuzzing
from fuzzing import (
    TARGETS,
    fuzz,
    limits,
    minimize,
    ref_decode_reply,
    ref_parse_command,
    small_limits,
)
from kedis_python.parser import CommandParser, KESPDecoder, KESPEncoder

ITERATIONS = 1500

needs_timer = pytest.mark.skipif(
    not hasattr(signal, "setitimer") or not hasattr(signal, "SIGVTALRM"),
    reason="needs a POSIX CPU-time timer",
)


def assert_clean(report):
    assert not report.failures, "\n".join(str(f) for f in report.failures)


# --- the real code survives the fuzzer -------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("target", TARGETS)
def test_fuzz_default_limits(target, seed):
    assert_clean(fuzz(target, seed=seed, iterations=ITERATIONS))


@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("target", ["parser", "decoder"])
def test_fuzz_small_limits(target, seed):
    # Caps shrunk to a few bytes, so every boundary is reachable.
    assert_clean(fuzz(target, seed=seed, iterations=ITERATIONS, small=True))


def test_small_limits_restores_everything():
    before = (
        CommandParser.MAX_INLINE_LENGTH,
        CommandParser.MAX_ARRAY_LENGTH,
        KESPDecoder.MAX_DEPTH,
        KESPDecoder.MAX_LINE,
        fuzzing.Ref.inline,
    )
    with small_limits():
        assert CommandParser.MAX_ARRAY_LENGTH == fuzzing.SMALL["array"]
        assert fuzzing.Ref.array == fuzzing.SMALL["array"]
        assert KESPDecoder.MAX_DEPTH == fuzzing.SMALL["depth"]
    assert before == (
        CommandParser.MAX_INLINE_LENGTH,
        CommandParser.MAX_ARRAY_LENGTH,
        KESPDecoder.MAX_DEPTH,
        KESPDecoder.MAX_LINE,
        fuzzing.Ref.inline,
    )


# --- the reference readers agree with the documented protocol --------------------------------


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"PING\n", ("command", ("PING",), 5)),
        (b"  get   k \n", ("command", ("GET", "k"), 11)),
        (b"A2\nS3\nset\nS0\n\n", ("command", ("SET", ""), 14)),
        (b"\n\nPING\n", ("command", ("PING",), 7)),
        (b"\n", ("none", 1, False)),
        (b"A0\n", ("none", 3, False)),
        (b"", ("none", 0, False)),
        (b"A2\nS3\nset\n", ("none", 0, True)),  # waiting for the second element
        (b"A1\nS3\nabcX", ("error", 10)),  # payload not followed by \n
        (b"A1\nX", ("error", 4)),  # element must start with S
        (b"A01\n", ("error", 4)),  # leading zero
        (b"A1\r\n", ("error", 4)),  # \r in a header
        (b"A\n", ("error", 2)),  # empty count
        (b"A+1\n", ("error", 4)),
    ],
)
def test_reference_command_reader(data, expected):
    assert ref_parse_command(data) == expected


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"N\n", ("value", None, 2)),
        (b"I-5\n", ("value", -5, 4)),
        (b"+OK\r\n", ("value", ("status", "OK"), 5)),
        (b"-ERR x\r\n", ("value", ("legacy", "-ERR x"), 8)),
        (b"Eboom\r\n", ("value", ("error", "boom\r"), 7)),  # \r only stripped on + and -
        (b"S2\nhi\n", ("value", ("bulk", "hi"), 6)),
        (b"A2\nI1\nN\n", ("value", [1, None], 8)),
        (b"A0\n", ("value", [], 3)),
        (b"A2\nI1\n", ("incomplete",)),
        (b"S5\nab", ("incomplete",)),
        (b"", ("incomplete",)),
        (b"?\n", ("error",)),
        (b"N x\n", ("error",)),
        (b"I-0\n", ("error",)),
        (b"I007\n", ("error",)),
        (b"S2\nhiX", ("error",)),
        (b"A1\n" * 40 + b"N\n", ("error",)),  # deeper than MAX_DEPTH
    ],
)
def test_reference_reply_reader(data, expected):
    assert ref_decode_reply(data) == expected


# --- the shrinker ----------------------------------------------------------------------------


def test_minimize_reduces_to_the_trigger():
    data = b"A3\nS1\nx\nS5\nhello\nS9\nXYZhello!!\n"
    assert minimize(data, lambda d: b"hello" in d) == b"hello"


def test_minimize_keeps_input_that_cannot_shrink():
    assert minimize(b"ab", lambda d: d == b"ab") == b"ab"


def test_minimize_respects_the_step_budget():
    calls = []

    def still(d):
        calls.append(1)
        return True

    minimize(b"x" * 500, still, max_steps=50)
    assert len(calls) <= 50 + 1


# --- the fuzzer finds injected faults --------------------------------------------------------


@pytest.fixture
def real_parse(monkeypatch):
    return CommandParser.parse_buffer


def test_fuzz_is_deterministic_for_a_seed(monkeypatch, real_parse):
    def buggy(buf, *a, **k):
        if b"\x00" in bytes(buf):
            raise RuntimeError("boom")
        return real_parse(buf, *a, **k)

    monkeypatch.setattr(CommandParser, "parse_buffer", staticmethod(buggy))
    one = fuzz("parser", seed=5, iterations=800, stop_after=2)
    two = fuzz("parser", seed=5, iterations=800, stop_after=2)
    assert [(f.kind, f.data, f.minimized) for f in one.failures] == [
        (f.kind, f.data, f.minimized) for f in two.failures
    ]
    assert one.inputs == two.inputs


def test_a_parser_that_raises_is_caught_and_shrunk(monkeypatch, real_parse):
    def buggy(buf, *a, **k):
        if b"\x00" in bytes(buf):
            raise RuntimeError("boom")
        return real_parse(buf, *a, **k)

    monkeypatch.setattr(CommandParser, "parse_buffer", staticmethod(buggy))
    report = fuzz("parser", seed=1, iterations=3000, stop_after=1)
    assert report.failures and report.failures[0].kind == "exception"
    assert report.failures[0].minimized == b"\x00"


def test_a_parser_that_disagrees_with_the_reference_is_caught(monkeypatch, real_parse):
    def buggy(buf, *a, **k):
        tokens, consumed, need = real_parse(buf, *a, **k)
        if tokens and not isinstance(tokens, Exception) and len(tokens) >= 3:
            consumed -= 1  # misreports how much it used
        return tokens, consumed, need

    monkeypatch.setattr(CommandParser, "parse_buffer", staticmethod(buggy))
    report = fuzz("parser", seed=1, iterations=3000, stop_after=1)
    assert report.failures and report.failures[0].kind == "differential"
    assert len(report.failures[0].minimized) <= 20


def test_a_hint_that_is_too_large_is_caught(monkeypatch, real_parse):
    def buggy(buf, *a, **k):
        tokens, consumed, need = real_parse(buf, *a, **k)
        if need and not tokens:
            need += 1  # asks for a byte more than it needs
        return tokens, consumed, need

    monkeypatch.setattr(CommandParser, "parse_buffer", staticmethod(buggy))
    report = fuzz("parser", seed=1, iterations=3000, stop_after=1)
    assert report.failures and report.failures[0].kind in ("hint", "differential")


def test_a_decoder_that_raises_the_wrong_exception_is_caught(monkeypatch):
    real = KESPDecoder.decode

    def buggy(buf, *a, **k):
        if bytes(buf).startswith(b"I1"):
            raise ValueError("not a protocol error")
        return real(buf, *a, **k)

    monkeypatch.setattr(KESPDecoder, "decode", staticmethod(buggy))
    report = fuzz("decoder", seed=1, iterations=3000, stop_after=1)
    assert report.failures and report.failures[0].kind == "exception"


@needs_timer
def test_a_runaway_parser_is_stopped_and_reported(monkeypatch, real_parse):
    def buggy(buf, *a, **k):
        if b"\xff" in bytes(buf):
            while True:
                pass
        return real_parse(buf, *a, **k)

    monkeypatch.setattr(CommandParser, "parse_buffer", staticmethod(buggy))
    report = fuzz("parser", seed=1, iterations=3000, stop_after=1, time_limit=0.2, mem_limit_mib=None)
    assert report.failures and report.failures[0].kind == "timeout"


def test_a_memory_hog_is_reported(monkeypatch, real_parse):
    def buggy(buf, *a, **k):
        if b"\x80" in bytes(buf):
            hog = bytearray(8 * 1024 * 1024)
            hog[0] = 1
        return real_parse(buf, *a, **k)

    monkeypatch.setattr(CommandParser, "parse_buffer", staticmethod(buggy))
    report = fuzz("parser", seed=1, iterations=3000, stop_after=1, mem_limit_mib=2)
    assert report.failures and report.failures[0].kind == "memory"


def test_a_broken_encoder_is_caught_by_the_roundtrip_target(monkeypatch):
    real = KESPEncoder.encode_command

    def buggy(tokens):
        return real(tokens)[:-1]  # drops the last byte

    monkeypatch.setattr(KESPEncoder, "encode_command", staticmethod(buggy))
    report = fuzz("roundtrip", seed=1, iterations=500, stop_after=1)
    assert report.failures and report.failures[0].kind == "roundtrip"


@needs_timer
def test_limits_stop_a_busy_loop():
    with pytest.raises(fuzzing._Timeout):
        with limits(0.1, None):
            while True:
                pass


def test_limits_report_memory():
    with pytest.raises(MemoryError):
        with limits(5, 1024 * 1024):
            hog = bytearray(4 * 1024 * 1024)
            hog[0] = 1


def test_limits_let_a_healthy_body_through():
    with limits(5, 8 * 1024 * 1024):
        sum(range(1000))


def test_cli_exit_status(tmp_path, capsys):
    assert fuzzing.main(["--target", "decoder", "--iterations", "200", "--limits", "default"]) == 0
    assert "decoder" in capsys.readouterr().out
