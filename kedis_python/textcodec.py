"""Text <-> bytes at the edges of the engine.

Values are Python strings inside the engine, but clients may send any bytes.
Bytes that are not valid UTF-8 are carried as lone surrogates U+DC80..U+DCFF
("surrogateescape"), so every byte string round-trips exactly:

    to_text(raw) -> str      never fails
    to_bytes(text) -> bytes  exact inverse of to_text; raises UnicodeEncodeError
                             only for lone surrogates outside U+DC80..U+DCFF,
                             which no byte string can produce

The AOF writes strings with "surrogatepass" (see aof.py) and snapshots are
JSON with \\u escapes; both keep these characters intact.
"""


def to_text(raw) -> str:
    return bytes(raw).decode("utf-8", "surrogateescape")


def to_bytes(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def printable(text: str) -> str:
    """text with raw (non-UTF-8) bytes shown as \\xNN, safe to print anywhere."""
    if text.isascii():
        return text
    try:
        return to_bytes(text).decode("utf-8", "backslashreplace")
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")
