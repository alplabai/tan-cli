# SPDX-License-Identifier: Apache-2.0
"""`tan.core.board_context` -- where a project's `board.yaml` is, and the one
fact `tan scaffold` reports out of it (tan-cli#1031).

Two things are under test and they fail differently, so they are separated
below:

* [`resolve_board_path`] is `tan validate`'s resolver, moved. Its
  `--board-yaml`-wins arm is the half tan-cli#1031 names explicitly: the flag's
  help text says "overrides project resolution", and `tan scaffold` honoured
  neither the flag nor the default because it called no resolver at all.

* [`read_board_context`] renders the generated `// Board context:` line, from
  a file the customer hand-edits and nothing upstream validates. **Every way
  that file can be broken must fall back to `unavailable`, never raise** --
  a module scaffold's job is to write a module, and it must still write one
  beside a broken board.yaml. That is the bulk of this file: absent, empty,
  a directory, non-UTF-8, malformed YAML, a non-mapping document, a missing
  `som` block, a `som` that is not a mapping, a `sku` that is missing/empty/
  not a string, and PyYAML not installed.
"""
from __future__ import annotations

import ntpath
import posixpath
import sys
from pathlib import PureWindowsPath

import pytest

from tan.core.board_context import UNSET, read_board_context, resolve_board_path

#: A minimal v2 `board.yaml` -- the shape `tan init --som E1M-AEN801
#: --template zephyr-app` writes, measured, and the exact project tan-cli#1031
#: reports against. It declares no `os:` anywhere, which is normal: alp-sdk's
#: `board.schema.json` says of `cores.<id>.os` that the runtime "is DERIVED
#: from the core's silicon class and is not selectable... Omit `os:` to take
#: that runtime".
V2_NO_OS = """
som:
  sku: E1M-AEN801
preset: e1m-evk
cores:
  m55_hp:
    app: ./src
"""


def write(tmp_path, text, name="board.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# resolve_board_path -- --board-yaml wins, as its help text promises
# ---------------------------------------------------------------------------


def test_defaults_to_board_yaml_under_the_project_root():
    assert resolve_board_path("/w/proj", None) == ("/w/proj", "/w/proj/board.yaml")


def test_no_project_keeps_the_dot_relative_shape_the_fixtures_pin():
    """`"."` / `"./board.yaml"`, not `"board.yaml"`: `Path(".") / "board.yaml"`
    normalises the `./` away, `Path::new(".").join(...)` does not, and the
    conformance fixtures pin the Rust shape."""
    assert resolve_board_path(None, None) == (".", "./board.yaml")


def test_an_absolute_board_yaml_flag_wins_verbatim():
    """tan-cli#1031's second reported invocation. The flag names a file
    OUTSIDE the project root and must still be the one read."""
    root, board = resolve_board_path("/w/proj", "/elsewhere/other.yaml")
    assert (root, board) == ("/w/proj", "/elsewhere/other.yaml")


def test_a_relative_board_yaml_flag_joins_onto_the_root():
    assert resolve_board_path("/w/proj", "alt.yaml") == ("/w/proj", "/w/proj/alt.yaml")


def test_a_root_that_already_ends_in_a_separator_does_not_double_it():
    """A drive root (`C:/`) would otherwise emit `C://board.yaml`."""
    assert resolve_board_path("C:/", None) == ("C:/", "C:/board.yaml")
    assert resolve_board_path("C:\\", None) == ("C:\\", "C:\\board.yaml")


# ---------------------------------------------------------------------------
# ... and the separator follows the ROOT (tan-cli#1031, found on windows-latest)
# ---------------------------------------------------------------------------
#
# WHY THESE RUN ANYWHERE. `resolve_board_path` picks its separator from the
# root STRING, never from `os.sep`, so a Windows-spelled root exercises the
# Windows branch identically on Linux -- these are real executions of the
# shipped code path, not a simulation. What CANNOT be reached from Linux is
# `os.path.isabs`, which is host-aware by design (whether `C:\x\y` is
# absolute genuinely IS a host question); the flag-override tests above stay
# on host-absolute inputs for that reason.
#
# THE INVARIANT: `boardYaml == root + <sep> + <leaf>`, in the root's own
# spelling. Every other reporter of this pair already satisfies it --
# `test_build_command.py:1182`, `test_presets_command.py:702` and
# `test_inspect_command.py:163` each assert `f"{root}/board.yaml"`, reading
# `/` only because their root is posix-normalised upstream. This resolver
# reports the root the caller TYPED, so its separator has to follow.


def test_a_windows_root_joins_with_a_backslash_not_a_mixed_path():
    """The CI failure, reduced. A hardcoded `/` answered the MIXED
    `C:\\w\\proj/board.yaml` for a root reported as `C:\\w\\proj` --
    two spellings of one directory in one envelope. `ntpath.join` is the
    oracle here, not a hand-typed literal."""
    root, board = resolve_board_path("C:\\w\\proj", None)
    assert (root, board) == ("C:\\w\\proj", "C:\\w\\proj\\board.yaml")
    assert board == ntpath.join(root, "board.yaml")
    assert "/" not in board, "a native root must not produce a mixed path"


def test_a_relative_windows_root_joins_with_a_backslash_too():
    """Not only absolute roots: `--project sub\\p` is reported verbatim, so
    its board path has to be spelled the same way."""
    root, board = resolve_board_path("sub\\p", None)
    assert board == ntpath.join(root, "board.yaml") == "sub\\p\\board.yaml"


def test_a_windows_root_and_a_relative_board_yaml_flag_agree_too():
    root, board = resolve_board_path("C:\\w\\proj", "alt.yaml")
    assert board == ntpath.join(root, "alt.yaml")


def test_a_posix_root_still_joins_with_a_forward_slash():
    """`posixpath.join` is the oracle on this side -- the fix must be a
    complete no-op for every posix-spelled root, which is every root the
    committed conformance goldens and the whole Linux/macOS surface use."""
    root, board = resolve_board_path("/w/proj", None)
    assert board == posixpath.join(root, "board.yaml") == "/w/proj/board.yaml"


def test_a_separatorless_root_keeps_the_forward_slash_on_every_platform():
    """`"."` and a bare name carry no separator to follow, so they keep `/`
    and stay byte-identical across hosts. Deliberately NOT `ntpath.join`,
    which would answer `.\\board.yaml`: the conformance goldens pin
    `"./board.yaml"`, and a platform-identical handshake is worth more here
    than matching a join function on a root that has nothing to match."""
    assert resolve_board_path(".", None)[1] == "./board.yaml"
    assert resolve_board_path("proj", None)[1] == "proj/board.yaml"
    assert ntpath.join(".", "board.yaml") == ".\\board.yaml"  # what we do NOT do


def test_a_caller_mixed_root_is_left_alone_and_not_made_worse():
    """A root the caller spelled with both separators is their own mix; `/`
    wins so the result is never MORE mixed than the input was."""
    assert resolve_board_path("C:/w\\proj", None)[1] == "C:/w\\proj/board.yaml"


def test_the_end_to_end_assertion_holds_under_windows_path_semantics():
    """`tests/commands/test_scaffold_command.py` asserts the envelope equals
    `str(proj / "board.yaml")`, where `proj` is a `tmp_path`. This is the
    strongest proof available from a Linux box that the assertion is right on
    Windows too: under `PureWindowsPath`, `str(proj / leaf)` IS
    `ntpath.join(root, leaf)`, which is exactly what this resolver answers for
    a natively spelled root. Before the fix the resolver answered
    `C:\\w\\proj/board.yaml` and that equality failed -- which is the CI
    failure, reproduced here without a Windows runner."""
    root = "C:\\Users\\runner\\AppData\\Local\\Temp\\pytest-0\\proj"
    expected = str(PureWindowsPath(root) / "board.yaml")
    assert expected == ntpath.join(root, "board.yaml")
    assert resolve_board_path(root, None)[1] == expected


@pytest.mark.parametrize(
    "root",
    ["/w/proj", "C:\\w\\proj", "sub\\p", "proj", ".", "C:/", "C:\\", "C:/w/proj"],
)
def test_the_board_path_always_starts_with_the_reported_root(root):
    """The invariant itself, over every root shape above: whatever `project.
    root` says, `project.boardYaml` is that string plus one separator plus the
    leaf -- so the two can never disagree about where the project is."""
    reported_root, board = resolve_board_path(root, None)
    assert reported_root == root
    assert board.startswith(root)
    tail = board[len(root):]
    assert tail in ("board.yaml", "/board.yaml", "\\board.yaml"), tail


# ---------------------------------------------------------------------------
# read_board_context -- the resolved line
# ---------------------------------------------------------------------------


def test_reads_the_sku_and_reports_an_undeclared_os_as_unset(tmp_path):
    """The headline project. `som.sku` is real; the OS half is `<unset>`
    because this board.yaml does not declare one -- the value lives in the SoM
    preset inside an alp-sdk checkout, and `tan scaffold` resolves no checkout
    (see the module docstring of `tan.core.board_context`). `<unset>` is the
    retired alp-sdk-vscode generator's own spelling for the same case."""
    assert read_board_context(write(tmp_path, V2_NO_OS)) == f"E1M-AEN801 / {UNSET}"


def test_a_v1_top_level_os_is_read(tmp_path):
    """The exact shape the deleted TypeScript port read (`boardModel.os`) and
    the exact line tan-cli#1031 quotes as the thing to restore."""
    ctx = read_board_context(write(tmp_path, "som:\n  sku: E1M-AEN801\nos: zephyr\n"))
    assert ctx == "E1M-AEN801 / zephyr"


def test_a_v2_core_declared_os_is_read(tmp_path):
    ctx = read_board_context(
        write(tmp_path, "som:\n  sku: E1M-V2N101\ncores:\n  a55_cluster:\n    os: yocto\n")
    )
    assert ctx == "E1M-V2N101 / yocto"


def test_a_parked_core_is_not_a_runtime(tmp_path):
    """`os: "off"` disables a core. 51 of the 53 alp-sdk v0.16.0 example
    board.yaml files that declare `os:` at all declare ONLY this, so reading
    the first declared value blindly would report `off` as the board context
    for most real projects. This mixed shape is not hypothetical: it is
    `examples/power-timing/power-managed-sensor/board.yaml`, one of exactly
    two example boards in that release that name a real runtime."""
    ctx = read_board_context(
        write(
            tmp_path,
            'som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    os: "off"\n'
            "  m55_he:\n    os: zephyr\n",
        )
    )
    assert ctx == "E1M-AEN801 / zephyr"


def test_cores_that_disagree_collapse_to_unset(tmp_path):
    """A heterogeneous project has no single OS, and a module scaffold is
    never told which core it is for -- so there is nothing to choose with, and
    naming one of the two would be a guess."""
    ctx = read_board_context(
        write(
            tmp_path,
            "som:\n  sku: E1M-V2N101\ncores:\n  a55_cluster:\n    os: yocto\n"
            "  m33_sm:\n    os: zephyr\n",
        )
    )
    assert ctx == f"E1M-V2N101 / {UNSET}"


# ---------------------------------------------------------------------------
# ... and every way the input can be broken (must not raise)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,text",
    [
        ("empty", ""),
        ("whitespace only", "   \n\n"),
        ("a comment only", "# nothing here\n"),
        ("a list, not a mapping", "- som\n- cores\n"),
        ("a bare scalar", "just-a-string\n"),
        ("malformed YAML", "som:\n  sku: [unclosed\n"),
        ("a duplicate-anchor parse error", "som: *nope\n"),
        ("no som block", "cores:\n  m55_hp:\n    app: ./src\n"),
        ("som is not a mapping", "som: E1M-AEN801\n"),
        ("som has no sku", "som:\n  hw_rev: A\n"),
        ("an empty sku", 'som:\n  sku: ""\n'),
        ("a non-string sku", "som:\n  sku: 801\n"),
    ],
)
def test_a_broken_board_yaml_is_unavailable_not_a_traceback(tmp_path, label, text):
    """`None` -- which the caller renders as the `unavailable` placeholder --
    for every one of these, and NOT an exception. `som.sku` is the gate rather
    than an `<unset>`-able half: it is the one value alp-sdk's
    `board.schema.json` requires at the top level, so a document without a
    usable one is not a board, and `<unset> / <unset>` would dress a broken
    file up as a resolved one."""
    assert read_board_context(write(tmp_path, text)) is None, label


def test_no_path_at_all_is_unavailable():
    assert read_board_context(None) is None
    assert read_board_context("") is None


def test_a_missing_file_is_unavailable(tmp_path):
    assert read_board_context(str(tmp_path / "board.yaml")) is None


def test_a_directory_where_the_file_should_be_is_unavailable(tmp_path):
    """`IsADirectoryError` on POSIX, `PermissionError` on Windows -- both are
    `OSError`, and neither may reach the caller."""
    (tmp_path / "board.yaml").mkdir()
    assert read_board_context(str(tmp_path / "board.yaml")) is None


def test_an_undecodable_byte_is_unavailable(tmp_path):
    """`UnicodeDecodeError` is a `ValueError`, NOT an `OSError`, so an
    `except OSError` alone could never catch it -- one undecodable byte in a
    customer's own board.yaml escaped `tan model` as a traceback for exactly
    that reason (tan-cli#396)."""
    path = tmp_path / "board.yaml"
    path.write_bytes(b"som:\n  sku: E1M-\xff\xfeAEN801\n")
    assert read_board_context(str(path)) is None


def test_no_pyyaml_is_unavailable_not_an_import_error(tmp_path, monkeypatch):
    """PyYAML is a declared BASE dependency (`pyproject.toml`), so this is not
    an "optional dep" path -- it is the stale-venv/broken-freeze case
    `tan.core.system_manifest._import_yaml` guards for too, degraded here to
    "nothing resolved" because this is a comment line rather than a manifest
    read. `None` in `sys.modules` is what makes `import yaml` raise
    `ImportError` without unloading a real PyYAML the rest of the session
    needs."""
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert read_board_context(write(tmp_path, V2_NO_OS)) is None
