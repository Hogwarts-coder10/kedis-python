"""The CLI's command-history hook must never stop the CLI from starting."""

import errno

import pytest

from kedis_python.main import _setup_history


class FakeReadline:
    def __init__(self, read_error=None, write_error=None):
        self.read_error, self.write_error = read_error, write_error
        self.calls = []

    def set_history_length(self, n):
        self.calls.append(("length", n))

    def read_history_file(self, path):
        self.calls.append(("read", path))
        if self.read_error:
            raise self.read_error

    def write_history_file(self, path):
        self.calls.append(("write", path))
        if self.write_error:
            raise self.write_error


def setup(readline, path="hist"):
    registered = []
    _setup_history(readline, registered.append, path)
    assert len(registered) == 1
    return registered[0]


def test_history_is_loaded_limited_and_saved():
    rl = FakeReadline()
    save = setup(rl, "h")
    assert rl.calls == [("length", 1000), ("read", "h")]
    save()
    assert rl.calls[-1] == ("write", "h")


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(errno.ENOENT, "No such file"),
        OSError(errno.EINVAL, "Invalid argument"),  # libedit on an empty file
        PermissionError(errno.EACCES, "Permission denied"),
    ],
    ids=["missing", "einval", "unreadable"],
)
def test_a_history_file_that_cannot_be_read_is_ignored(error):
    rl = FakeReadline(read_error=error)
    save = setup(rl)
    save()  # and saving still works afterwards
    assert ("write", "hist") in rl.calls


def test_a_history_file_that_cannot_be_written_does_not_raise_at_exit():
    save = setup(
        FakeReadline(write_error=OSError(errno.EROFS, "Read-only file system"))
    )
    save()


def test_the_real_readline_survives_an_empty_history_file(tmp_path):
    """libedit rejects an empty history file; GNU readline accepts it."""
    readline = pytest.importorskip("readline")
    empty = tmp_path / ".kedis_history"
    empty.write_bytes(b"")
    registered = []
    _setup_history(readline, registered.append, str(empty))
    registered[0]()  # saving works, too
