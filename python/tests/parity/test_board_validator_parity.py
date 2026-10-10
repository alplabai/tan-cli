# SPDX-License-Identifier: Apache-2.0
"""Engine parity: `tan validate`'s in-process validator vs alp-sdk's script (tan-cli#270).

`tan validate` used to spawn `<sdk>/scripts/validate_board_yaml.py`; it now runs a
port of that script in-process (`tan.core.board_validator_run`). The port is only
acceptable if it is indistinguishable, so this module runs BOTH engines over the
same `board.yaml` and compares what the script's process leaves behind -- exit
status, stdout and stderr, byte for byte (ANSI stripped from both sides: the SDK
script paints through `colorama`, which strips escapes when stderr is a pipe, and
the in-process engine renders without colour). An uncaught-exception traceback is
compared by its last line only -- its frames name the script's own files.

Two corpora, because they answer different questions:

* every `board.yaml` under the bound SDK's `examples/` -- the shipped, valid
  boards, including the camera, multi-core and Yocto shapes the cross-field
  checks care about;
* a hand-built set of INVALID boards (schema errors, unknown keys, bad types,
  bad enums, unknown SKU / preset / chip, YAML parse errors, duplicate keys,
  non-mapping documents, unreadable bytes, hw_rev refusals, a warning-only
  board) so the diagnostic text, the caret positions the YAML position loader
  computes, and the 1 / 3 / 4 / 5 exit-status split are all compared and not
  merely the clean path.

The comparison is also run one level up, through `analyze_validator_output`, so
the parsed findings -- what actually reaches the `tan validate` envelope -- are
equal, not just the raw text they were parsed from.

Requires an alp-sdk checkout: set `ALP_SDK_ROOT` (or `ALP_SDK_PARITY_ROOT`).
Skips, loudly, without one.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tan.commands.validate_cmd import analyze_validator_output
from tan.core.board_validator_run import run_board_validator
from tan.core.subprocess_env import spawn_env
from tests.conftest import sdk_root

SDK: Path | None = sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason=(
        "ALP_SDK_ROOT / ALP_SDK_PARITY_ROOT is not set -- no alp-sdk checkout "
        "to run the reference validate_board_yaml.py from, so engine parity "
        "cannot be measured. A SKIP about the missing root, not a pass."
    ),
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

#: Valid at the pinned SDK; every INVALID case below is a one-line mutation of it,
#: so a diagnostic that moves is caused by the mutation and nothing else.
_BASE = """\
libraries:
  - name: mbedtls
    cores: [m55_hp]

som:
  sku: E1M-AEN801

preset: e1m-evk

cores:
  m55_hp:
    app: ./src
    peripherals:
      - spi
      - gpio

chips:
  - cc3501e

diagnostics:
  log_level: info
"""


def _mutate(old: str, new: str) -> str:
    assert old in _BASE, old
    return _BASE.replace(old, new, 1)


#: name -> board.yaml text (str) or raw bytes.
CORPUS: dict[str, str | bytes] = {
    "valid-base": _BASE,
    "missing-required-cores": _BASE.split("cores:")[0] + "diagnostics:\n  log_level: info\n",
    "unknown-top-level-key": _BASE + "bogus_key: 1\n",
    "unknown-top-level-key-with-near-miss": _mutate("diagnostics:", "diagnostix:"),
    "unknown-nested-key": _mutate("    app: ./src\n", "    app: ./src\n    appp: ./src\n"),
    "bad-enum": _mutate("log_level: info", "log_level: loud"),
    "bad-type-cores-is-a-string": _BASE.split("cores:")[0] + "cores: nope\n",
    "bad-type-som-is-a-list": _mutate("som:\n  sku: E1M-AEN801", "som:\n  - E1M-AEN801"),
    "bad-type-name-is-an-int": _BASE + "name: 5\n",
    "bad-pattern-chip-token": _mutate("  - cc3501e", "  - Not-A-Chip!"),
    "unknown-sku": _mutate("E1M-AEN801", "E1M-AEN80"),
    "unknown-preset": _mutate("preset: e1m-evk", "preset: e1m-evq"),
    "unknown-chip": _mutate("  - cc3501e", "  - cc3501"),
    "preset-hosts-other-family": _mutate("E1M-AEN801", "E1M-V2N101"),
    "peripheral-not-on-silicon-is-only-a-warning": _mutate("      - gpio", "      - gpio\n      - emmc"),
    "hw-rev-fails-the-schema-pattern": _mutate("preset: e1m-evk", "preset: e1m-evk\nhw_rev: Z9"),
    # Exit 4: schema-valid, but not a key of the SoM's hw_revisions table.
    "hw-rev-unknown-to-the-table": _mutate("  sku: E1M-AEN801", "  sku: E1M-AEN801\n  hw_rev: zz"),
    # Exit 5: a key of the table whose `status: reserved` refuses a build.
    "hw-rev-reserved-is-not-buildable": _mutate("  sku: E1M-AEN801", "  sku: E1M-AEN801\n  hw_rev: r3"),
    "yaml-parse-error": _BASE + "cores: [unclosed\n",
    "yaml-tab-indent-error": _mutate("    app: ./src\n", "\tapp: ./src\n"),
    "duplicate-key": _BASE + "preset: e1m-evk\n",
    "top-level-list": "- a\n- b\n",
    "top-level-scalar": "just a string\n",
    "empty-file": "",
    "comments-only": "# nothing here\n",
    "crlf-line-endings": _BASE.replace("\n", "\r\n"),
    "non-ascii-comment-shifts-nothing": "# café — é\n" + _mutate("log_level: info", "log_level: loud"),
    "several-errors-in-one-file": _BASE.replace("log_level: info", "log_level: loud")
    + "bogus_key: 1\n",
    "non-utf8-bytes": b"som: {sku: \xff\xfe}\n",
}


def _normal(text: str) -> str:
    return _ANSI.sub("", text)


def _spawn(board: Path) -> tuple[int, str, str]:
    assert SDK is not None
    done = subprocess.run(
        [sys.executable, str(SDK / "scripts" / "validate_board_yaml.py"), "--input", str(board)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=300,
        env=spawn_env(),
        check=False,
    )
    return done.returncode, done.stdout, done.stderr


def _in_process(board: Path) -> tuple[int, str, str]:
    assert SDK is not None
    run = run_board_validator(str(board), SDK)
    return run.status, run.stdout, run.stderr


def _assert_same(board: Path) -> tuple[int, str, str]:
    ref = _spawn(board)
    got = _in_process(board)
    assert got[0] == ref[0], f"exit status: in-process {got[0]} != script {ref[0]}\n{ref[2]}\n---\n{got[2]}"
    assert _normal(got[1]) == _normal(ref[1]), f"stdout differs:\n{ref[1]!r}\n{got[1]!r}"
    if ref[2].startswith("Traceback"):
        # An uncaught exception: the frames name the SDK script's own files and
        # line numbers, which no port can share. The exception line must match.
        assert got[2].startswith("Traceback"), got[2]
        assert got[2].strip().splitlines()[-1] == ref[2].strip().splitlines()[-1]
    else:
        assert _normal(got[2]) == _normal(ref[2]), f"stderr differs:\n{ref[2]}\n--- in-process:\n{got[2]}"
    # One level up: the parsed outcome + findings, i.e. what the envelope carries.
    assert analyze_validator_output(got[0], got[2]) == analyze_validator_output(ref[0], ref[2])
    return ref


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_invalid_and_edge_boards_report_the_same_diagnostics(name, tmp_path):
    board = tmp_path / "board.yaml"
    body = CORPUS[name]
    if isinstance(body, bytes):
        board.write_bytes(body)
    else:
        board.write_bytes(body.encode("utf-8"))
    _assert_same(board)


def test_a_missing_file_reports_the_same_failure(tmp_path):
    _assert_same(tmp_path / "no-such-board.yaml")


def test_the_corpus_actually_exercises_every_exit_status_family():
    """A corpus that only ever produced `clean` would pass vacuously. Measured
    on the reference engine, the invalid set must hit the diagnostic path (exit
    1 with a rendered ALP code), the clean path, and at least one warning-only
    board -- otherwise the comparisons above compare nothing."""
    statuses: dict[str, int] = {}
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        for name, body in CORPUS.items():
            board = Path(td) / f"{name}.yaml"
            board.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
            statuses[name] = _spawn(board)[0]
    assert statuses["valid-base"] == 0, statuses
    assert statuses["unknown-top-level-key"] == 1, statuses
    assert statuses["unknown-sku"] == 1, statuses
    assert statuses["hw-rev-unknown-to-the-table"] == 4, statuses
    assert statuses["hw-rev-reserved-is-not-buildable"] == 5, statuses
    assert sum(1 for s in statuses.values() if s == 1) >= 15, statuses
    # The warning-only board is a PASS carrying a rendered warning.
    with tempfile.TemporaryDirectory() as td:
        board = Path(td) / "board.yaml"
        board.write_text(CORPUS["peripheral-not-on-silicon-is-only-a-warning"], encoding="utf-8")
        status, stdout, stderr = _spawn(board)
    assert status == 0 and "warning[ALP-B010]" in stderr and "warning(s))" in stdout, (status, stdout, stderr)


def _example_boards() -> list[Path]:
    return sorted((SDK / "examples").rglob("board.yaml")) if SDK is not None else []


def test_the_example_corpus_is_not_empty():
    assert len(_example_boards()) >= 20, (
        "the bound SDK ships fewer than 20 example board.yaml files -- the shipped-boards "
        "half of this parity gate would be vacuous"
    )


@pytest.mark.parametrize(
    "board",
    _example_boards(),
    ids=lambda p: str(p.parent.relative_to(SDK / "examples")) if SDK is not None else str(p),
)
def test_every_shipped_example_board_reports_the_same_verdict(board):
    _assert_same(board)
