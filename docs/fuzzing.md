# Fuzzing the Protocol

The command parser and the reply decoder read bytes from the network, so they get the most hostile input in the project. `tests/fuzzing.py` is a seeded mutation fuzzer for both, with independent reference readers to compare against. This page says how to run it, what it checks, what it found, and what it does not cover.

## Running it

```bash
pip install -r requirements-dev.txt

# a long session: every target, three seeds, real limits and shrunken limits
python tests/fuzzing.py --target all --seed 1 --seed 2 --seed 3 --iterations 100000

# one target, for ten minutes, keeping minimized reproductions
python tests/fuzzing.py --target parser --seed 7 --max-seconds 600 --save-dir fuzz-out
```

Targets are `parser` (command parser), `decoder` (reply decoder) and `roundtrip` (encoder output decodes back; framed commands parse back). The exit status is 1 if anything broke an invariant. Every failure is shrunk to a minimal input and printed as a bytes literal; `--save-dir` also writes it to a `.bin` file. A run is deterministic for a given target, seed and `--iterations` (`--max-seconds` cuts it short, so use it for exploring, not for reproducing).

`pytest` runs the same fuzzer in short seeded bursts (`tests/test_fuzz.py`) plus hypothesis property tests (`tests/test_fuzz_properties.py`, skipped if hypothesis is not installed).

## What is checked for each input

| Check | What it asserts |
|---|---|
| exceptions | The command parser never raises. The reply decoder raises only `IncompleteReply` or `ReplyProtocolError`. |
| limits | Each input is handled within a CPU-time limit (default 2 s) and a peak-memory limit (default 64 MiB). |
| contract | `consumed` is within the buffer. A protocol error consumes the whole buffer and carries no hint. An incomplete frame carries a positive hint. |
| differential | A reference reader, written from [protocol.md](protocol.md), reaches the same outcome: same tokens or value, same `consumed`, same choice between "wait" and "fail". |
| prefixes | No strict prefix of a complete frame is complete or an error. |
| hints | Appending fewer bytes than the hint asks for never makes progress. A hint that is too large would leave a server that skips re-parsing until the hinted length has arrived waiting for bytes the frame does not need. |
| chunking | Feeding the bytes in random pieces, parsing after each (as the server reads them), gives the same commands as parsing the whole buffer. |
| round trips | Whatever the encoder emits, the decoder reads back; whatever `encode_command` frames, the parser reads back. |

The reference readers are a different shape from the code they check: a byte-at-a-time state machine for commands, and an explicit stack instead of recursion for replies. That makes a shared misreading of the spec less likely to hide in both.

## Shrunken limits

The real caps (64 KiB inline lines, 1,048,576 array elements, 512 MiB strings) are far beyond anything a few-KiB mutated input reaches, so a bug at a boundary, such as `>=` where `>` was meant, would never fire. With `--limits small` (on by default, next to the real limits) the caps in the code and in the reference are shrunk together (header 8 bytes, inline 40, array 6, string 24, command 40, depth 4, text line 40), and the generators build frames on, and one step either side of, each cap.

## What it has found

Nothing, so far, in the code as it stands. 1,500,000 inputs (three seeds of 100,000 per target and mode, 15 runs) produced no failures. That is a result on a harness that is known to be able to fail, which is a weaker statement than "the parser is correct".

How well it can fail was measured by injecting bugs into the parser and decoder and running the fuzzer against each:

- 19 injected bugs: counts that accept leading zeros, an off-by-one on each cap, caps ignored, the payload terminator not checked, hints one too large, CR stripped on every line, junk accepted after `N`.
- 17 were caught, each as a `differential` or `hint` failure.
- 2 were not, and are equivalent to the original: they change the cap on a header line that does end in `\n`. A header that long would have to carry a number of at least 31 digits, which the array and bulk caps already reject with the same result (an error, whole buffer consumed). Only the cap on a header line with no terminator yet matters, and a bug there is caught.

The tests also inject faults into the harness's own targets (an exception, an infinite loop, a memory hog, a wrong `consumed`, a hint that is too large, the wrong exception type, a broken encoder) and check each is reported.

## What it does not cover

- **Whole-reply budgets.** The reference decoder does not model `MAX_REPLY_ELEMENTS` and `MAX_REPLY_PAYLOAD` (4,194,304 values and 1 GiB), which are far beyond the fuzzer's inputs. They are tested at boundaries with shrunken budgets in `tests/test_reply_budgets.py`.
- **Everything above the parser.** Command handlers, the store, the AOF, snapshots, replication and the server's timeouts are not fuzzed. A frame that parses can still make a handler misbehave.
- **The lenient decoder.** `decode(..., lenient=True)` is only exercised by its own unit tests, and is due for removal in 0.3.
- **Large inputs.** Mutated inputs are cut at 4 KiB. Behaviour with multi-megabyte frames is covered by the unit tests in `tests/test_parser_limits.py` and `tests/test_server_limits.py`, not by fuzzing.
- **Time and memory limits are approximate.** Time is CPU time, enforced with a timer only on POSIX (elsewhere it is checked after the input finishes, so an infinite loop would hang). Memory is the `tracemalloc` peak, which counts Python allocations only.
- **Coverage guidance.** The fuzzer mutates a pool of valid and near-valid frames; it does not measure code coverage to steer itself, so a long run is a spread of tries rather than a systematic search.

## When it finds something

1. Re-run with the printed seed to see it again, or paste the minimized bytes into a test.
2. Add a regression test next to the existing limit tests with that exact input, and fix the bug.
3. If the reference reader was the one that was wrong, fix it against [protocol.md](protocol.md), and change the doc if the doc was unclear. The reference and the parser must both follow the written rules.
