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


@pytest.mark.skipif(
    os.name != "posix",
    reason="needs a filesystem that accepts an arbitrary non-UTF-8 byte in a name",
)
def test_a_surrogate_in_the_cwd_still_answers_one_envelope(tmp_path):
    """The end-to-end half, at the real process boundary -- `emit()`'s `print`
    is where this failed, and only a real spawn runs it.

    Measured against the frozen oracle from the identical directory:
    `target/debug/tan inspect --format json` exits 0 with
    `"root":".../proj\\xef\\xbf\\xbdx"`; the port exited 1 with 0 bytes on
    stdout and `UnicodeEncodeError: 'utf-8' codec can't encode character
    '\\udcff' in position 159: surrogates not allowed` at `envelope.py:307`.
    """
    project = tmp_path / os.fsdecode(b"proj\xffx")
    project.mkdir()
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
