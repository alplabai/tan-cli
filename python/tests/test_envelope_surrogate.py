# SPDX-License-Identifier: Apache-2.0
"""tan-cli#491 defect 1: a lone surrogate must not kill stdout AFTER
`_serialise()` has already reported success.

`emit()` writes the serialised envelope with a bare `print(text)`. Under
`ensure_ascii=False` a lone surrogate -- what `os.fsdecode`/`surrogateescape`
turns every un-decodable filesystem byte into -- goes straight through
`json.dumps` into that `str`, so the encode fails at the PRINT, one call after
`_serialise`'s own `except Exception  # no payload may ever crash stdout` guard
could still have done anything about it. The run then dies with ZERO bytes on
stdout and a Rich traceback on stderr, which is the one thing `--format json`
promises never to do: the alp-sdk-vscode extension's only channel is that
envelope, so it gets nothing to parse and no coded signal.

The oracle answers the same argv with an envelope, at exit 0, carrying U+FFFD
where the bad byte was (`Path::to_string_lossy`) -- measured, see
`envelope._REPLACEMENT_CHARACTER`. These pin the port to that.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tan.envelope import Envelope, Issue, Project

#: `python/` -- `python -m tan` resolves the package off `os.getcwd()`, so a
#: child run from a scratch cwd needs this pinned onto its `PYTHONPATH`
#: (mirrors `test_main.py`/`test_wants_help_positions.py`).
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

#: One un-decodable byte, as `surrogateescape` renders it.
LONE_SURROGATE = "\udcff"

#: The directory name the end-to-end case needs. `0xff` is not valid UTF-8 in
#: any position, which is the whole point: a filesystem that stores names as
#: opaque bytes (ext4) accepts it, one that validates them as UTF-8 (APFS) or
#: stores them as UTF-16 (NTFS) refuses it.
NON_UTF8_DIR_NAME = b"proj\xffx"


def _make_non_utf8_dir(parent: Path) -> Path:
    """Create `<parent>/proj\\xffx` and hand back its (surrogate-escaped) path.

    ONE function, used by both the capability probe and the test itself, so
    the probe cannot drift into probing a different operation than the one the
    test performs. Built from BYTES rather than `os.fsdecode(...)` at module
    scope because `os.fsdecode` itself raises on Windows (the filesystem codec
    there is UTF-8/`surrogatepass`, which cannot decode a bare `0xff`) -- that
    failure belongs in the probe's hands, not at import time.
    """
    raw = os.path.join(os.fsencode(parent), NON_UTF8_DIR_NAME)
    os.mkdir(raw)
    # A filesystem can also ACCEPT the mkdir and quietly rewrite the byte
    # (some network/FUSE mounts substitute or strip it). That leaves a
    # perfectly ordinary directory name, which would let the end-to-end case
    # below pass while exercising nothing -- a vacuous pass is worse than a
    # skip, so read the name back as bytes and insist it survived verbatim.
    if NON_UTF8_DIR_NAME not in os.listdir(os.fsencode(parent)):
        raise ValueError(
            f"the mkdir was accepted but {NON_UTF8_DIR_NAME!r} did not survive "
            f"the round trip; the filesystem rewrote the name to one of "
            f"{os.listdir(os.fsencode(parent))!r}"
        )
    return Path(os.fsdecode(raw))


@pytest.fixture
def non_utf8_project_dir(tmp_path):
    """A project directory whose name carries a raw non-UTF-8 byte, or a skip
    naming what refused it and where.

    PROBED, not inferred from `os.name`. The guard here used to be
    `skipif(os.name != "posix")`, whose own `reason` described the real
    requirement correctly -- *a filesystem that accepts an arbitrary non-UTF-8
    byte in a name* -- while testing something else entirely. **macOS is
    `os.name == "posix"`**, so the guard passed there and APFS then refused the
    `mkdir` with `OSError: [Errno 92] Illegal byte sequence`, reddening
    macos-latest while ubuntu (ext4, raw bytes) stayed green. The capability is
    a property of the FILESYSTEM, so the only honest test of it is to attempt
    the exact operation.

    The probe runs inside `tmp_path`, which is the filesystem the test will
    actually use -- `/tmp`, `$HOME` and the repo can be three different mounts
    on one machine, so probing any other location just re-guesses somewhere
    new. Same shape as `test_pinmux_command.py`'s symlink probe and
    `tan/env.py`'s `stderr_is_tty()`: try it, report what happened.
    """
    probe = tmp_path / "capability-probe"
    probe.mkdir()
    try:
        _make_non_utf8_dir(probe)
    except (OSError, ValueError) as err:
        # `ValueError` covers `UnicodeDecodeError`/`UnicodeEncodeError`, which
        # is how a filesystem codec that cannot represent the byte at all
        # (Windows) refuses, as opposed to an `OSError` from the kernel/FS
        # itself (macOS `EILSEQ`).
        pytest.skip(
            f"the filesystem backing {tmp_path} cannot hold a directory name "
            f"carrying the non-UTF-8 byte 0xff ({NON_UTF8_DIR_NAME!r}): "
            f"{type(err).__name__}: {err}"
        )
    finally:
        shutil.rmtree(probe)
    return _make_non_utf8_dir(tmp_path)


def test_to_json_replaces_a_lone_surrogate_and_stays_utf8_encodable():
    """The unit half. Pre-fix `to_json()` returned a `str` holding `\\udcff`
    verbatim -- valid Python, valid `json.dumps` output, and NOT encodable as
    UTF-8, so every writer of it (`emit()`'s `print`, `tan.cli`'s
    `_usage_error_envelope` print) raised `UnicodeEncodeError`."""
    env = Envelope(
        "inspect",
        Project(root=f"/w/proj{LONE_SURROGATE}x", board_yaml=None),
        {"scanned": [f"a{LONE_SURROGATE}b"], f"k{LONE_SURROGATE}": "v"},
        [Issue("inspect.board-yaml-missing", "warning", f"at proj{LONE_SURROGATE}x")],
        0,
    )
    text = env.to_json()

    # The actual defect: this raised.
    text.encode("utf-8")

    assert LONE_SURROGATE not in text
    # Every one of the four carriers -- value, list element, KEY, issue message.
    assert text.count("�") == 4, text
    parsed = json.loads(text)
    assert parsed["project"]["root"] == "/w/proj�x"
    assert parsed["data"]["k�"] == "v"


def test_the_serialize_failure_fallback_is_also_scrubbed():
    """The fallback arm exists so no payload can crash stdout, so it must not
    be the thing that crashes stdout: it copies `project` through verbatim and
    stringifies the original error, either of which can carry the surrogate
    that sent it down this path. `data` is a set -- `json.dumps` cannot encode
    it, which is what forces the fallback."""
    env = Envelope(
        "inspect",
        Project(root=f"/w/proj{LONE_SURROGATE}x", board_yaml=None),
        {"bad"},  # a set: `TypeError: Object of type set is not JSON serializable`
        [],
        0,
    )
    text, code = env._serialise()

    text.encode("utf-8")
    assert code == 5
    assert json.loads(text)["issues"][0]["code"] == "envelope.serialize-failed"
    assert json.loads(text)["project"]["root"] == "/w/proj�x"


def test_a_surrogate_in_the_cwd_still_answers_one_envelope(non_utf8_project_dir):
    """The end-to-end half, at the real process boundary -- `emit()`'s `print`
    is where this failed, and only a real spawn runs it.

    Measured against the frozen oracle from the identical directory:
    `target/debug/tan inspect --format json` exits 0 with
    `"root":".../proj\\xef\\xbf\\xbdx"`; the port exited 1 with 0 bytes on
    stdout and `UnicodeEncodeError: 'utf-8' codec can't encode character
    '\\udcff' in position 159: surrogates not allowed` at `envelope.py:307`.

    Runs wherever the filesystem can hold the name; see `non_utf8_project_dir`
    for why that is probed rather than assumed from `os.name`.
    """
    project = non_utf8_project_dir
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "tan", "inspect", "--format", "json"],
        cwd=project,
        capture_output=True,
        env=env,
        timeout=120,
    )

    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout, "zero bytes on stdout is the defect"
    envelope = json.loads(proc.stdout.decode("utf-8"))
    assert envelope["command"] == "inspect"
    assert envelope["project"]["root"].endswith("proj�x"), envelope["project"]["root"]
