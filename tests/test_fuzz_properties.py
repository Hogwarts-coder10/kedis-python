"""Property tests (hypothesis) over the same invariants the fuzzer checks.

Skipped as a whole when hypothesis is not installed (it is in requirements-dev.txt).
"""

import pytest

import fuzzing
from fuzzing import check_decoder, check_parser
from kedis_python.parser import KESPEncoder
from kedis_python.textcodec import to_text

pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

settings.register_profile("kedis", max_examples=200, deadline=None)
settings.load_profile("kedis")

raw_text = st.binary(max_size=40).map(to_text)  # includes non-UTF-8 bytes, as surrogates


@given(st.lists(raw_text, min_size=1, max_size=8))
def test_property_a_framed_command_parses_back(tokens):
    framed = KESPEncoder.encode_command(tokens)
    want = list(tokens)
    want[0] = want[0].upper()
    assert fuzzing.real_parse_command(framed) == ("command", tuple(want), len(framed))


values = st.recursive(
    st.none() | st.integers(-(10**12), 10**12) | raw_text.map(fuzzing.BulkString),
    lambda inner: st.lists(inner, max_size=5),
    max_leaves=25,
)


@given(values)
def test_property_an_encoded_value_decodes_back(value):
    wire = KESPEncoder.encode(value)
    assert fuzzing.real_decode_reply(wire) == ("value", fuzzing._expected_tag(value), len(wire))


@given(st.binary(max_size=300))
def test_property_the_parser_survives_any_bytes(data):
    assert check_parser(data) is None


@given(st.binary(max_size=300))
def test_property_the_decoder_survives_any_bytes(data):
    assert check_decoder(data) is None


@given(st.lists(st.lists(raw_text, min_size=1, max_size=4), min_size=1, max_size=5), st.randoms())
def test_property_any_chunking_gives_the_same_commands(commands, rnd):
    stream = b"".join(KESPEncoder.encode_command(c) for c in commands)
    sizes, left = [], len(stream)
    while left:
        sizes.append(min(left, rnd.randint(1, 9)))
        left -= sizes[-1]
    whole = fuzzing.drive_parser(stream, None)
    assert fuzzing.drive_parser(stream, sizes) == whole
    assert len(whole) == len(commands)
