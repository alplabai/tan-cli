# SPDX-License-Identifier: Apache-2.0
"""`tan.planner.kconfig_symbols`'s rendered `--emit kconfig` dumper.

`_DUMPER` used to be a module-level `Path` anchored on the bound alp-sdk
checkout (`REPO / "scripts" / "kconfig" / "alp_kconfig_dump.py"`) -- the last
coupling `--emit kconfig` had to a Python file living outside `tan`, and one
that would have reintroduced the classic PyInstaller-onefile hazard (a path
under `sys._MEIPASS`, deleted at process exit, baked into a generated CMake
build tree that outlives the process) had it simply been swapped for a
bundled-data path instead. It is now `_DUMPER_SOURCE`, a self-contained
script rendered fresh onto disk -- inside the run's OWN scratch tree -- by
every `_load_board_symbols` call.

These tests pin that render's invariants without a bootstrapped Zephyr
workspace; `tests/scripts/test_emit_kconfig_workspace.py` (alp-sdk, mirrored
by `tan#35`) covers the real `west build` round trip when `ZEPHYR_BASE` is
set. Importing `tan.planner.*` needs a bound alp-sdk root with real
metadata (the package's eager import chain reads several
`metadata/registries/*` files at import time) -- same requirement, and the
same env vars, as `tests/parity/test_planner_emit_parity.py`.
"""

from __future__ import annotations

import ast
import os
import sys
import types
from pathlib import Path

import pytest


def _sdk_root() -> Path | None:
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


SDK = _sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner can bind "
           "a root and import (same requirement as the parity suite)",
)


@pytest.fixture(scope="module")
def ks():
    from tan.planner_root import bind_sdk_root
    assert SDK is not None
    bind_sdk_root(SDK)
    from tan.planner import kconfig_symbols
    return kconfig_symbols


# ---------------------------------------------------------------------
# The coupling removed: no more path into the bound SDK checkout.
# ---------------------------------------------------------------------


def test_the_dumper_no_longer_names_a_path_inside_the_sdk_checkout(ks):
    assert not hasattr(ks, "_DUMPER"), (
        "_DUMPER (a Path into the bound alp-sdk checkout) should be gone -- "
        "the dumper is rendered from _DUMPER_SOURCE now")
    assert isinstance(ks._DUMPER_SOURCE, str) and ks._DUMPER_SOURCE.strip()


def test_the_rendered_dumper_is_syntactically_valid_python(ks):
    ast.parse(ks._DUMPER_SOURCE)  # raises SyntaxError on failure


def test_the_rendered_dumper_imports_nothing_from_tan_or_alp_orchestrate(ks):
    tree = ast.parse(ks._DUMPER_SOURCE, filename="<rendered dumper>")
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    assert names, "expected at least argparse/json/os/sys/Path + kconfiglib"
    forbidden = [
        n for n in names
        if n == "tan" or n.startswith("tan.")
        or n == "alp_orchestrate" or n.startswith("alp_orchestrate.")
    ]
    assert not forbidden, forbidden


# ---------------------------------------------------------------------
# Rendering onto disk: fresh every time, never stale-reused.
# ---------------------------------------------------------------------


def test_write_dumper_renders_the_source_verbatim(tmp_path, ks):
    target = tmp_path / "nested" / "alp_kconfig_dump.py"
    ks._write_dumper(target)
    assert target.read_text(encoding="utf-8") == ks._DUMPER_SOURCE


def test_rendering_again_overwrites_rather_than_reusing_stale_content(tmp_path, ks):
    target = tmp_path / "alp_kconfig_dump.py"
    target.write_text("stale content from a previous project\n", encoding="utf-8")
    ks._write_dumper(target)
    assert target.read_text(encoding="utf-8") == ks._DUMPER_SOURCE


# ---------------------------------------------------------------------
# Behavioral equivalence: the inlined projection logic vs the planner's own.
# ---------------------------------------------------------------------


class _FakeNode:
    def __init__(self, prompt=None, help=None):
        self.prompt = prompt
        self.help = help


class _FakeSym:
    def __init__(self, name, type_, nodes, direct_dep="", orig_defaults=()):
        self.name = name
        self.type = type_
        self.nodes = nodes
        self.direct_dep = direct_dep
        self.orig_defaults = list(orig_defaults)


_FAKE_BOOL, _FAKE_INT = 7, 42
_FAKE_TYPE_TO_STR = {_FAKE_BOOL: "bool", _FAKE_INT: "int"}
# Deliberately absent from _FAKE_TYPE_TO_STR -- exercises the
# `TYPE_TO_STR.get(sym.type, "unknown")` fallback.
_FAKE_UNKNOWN_TYPE = 99


def _exec_rendered_dumper(ks) -> types.ModuleType:
    """Exec `_DUMPER_SOURCE` in an isolated namespace against a FAKE
    `kconfiglib` (installed into `sys.modules` so the rendered script's own
    module-level `import kconfiglib` resolves it) and a dummy `ZEPHYR_BASE`
    (the `sys.path.insert` line only needs the env var to be present, never
    reads the path -- the fake module already satisfies the import). Returns
    the executed namespace so callers can reach `_project_symbols`."""
    ns: dict = {"__name__": "rendered_dumper_under_test"}
    exec(compile(ks._DUMPER_SOURCE, "<rendered dumper>", "exec"), ns)
    return ns


@pytest.fixture
def fake_kconfiglib(monkeypatch):
    fake = types.ModuleType("kconfiglib")
    fake.TYPE_TO_STR = dict(_FAKE_TYPE_TO_STR)
    fake.expr_str = lambda e: e if isinstance(e, str) else str(e)
    monkeypatch.setitem(sys.modules, "kconfiglib", fake)
    monkeypatch.setenv("ZEPHYR_BASE", "Z:/does-not-need-to-exist-for-this-test")
    return fake


def test_the_rendered_projection_matches_a_literal_expected_result(ks, fake_kconfiglib):
    """The rendered dumper's inlined `_project_symbols` is the ONLY copy of
    this projection logic left (see the module docstring) -- so it is
    verified against a literal expected result (an independent oracle)
    rather than a second, hand-synced copy that could drift in lockstep and
    still pass. Covers, per symbol:

      * SERIAL -- the "first prompt-bearing node, not nodes[0]" regression
        case (alp-sdk #893: a defconfig override node with no prompt
        precedes the real declaration), plus a non-empty `direct_dep` (the
        `depends` field the prj.conf LSP greys symbols out with).
      * STILL_HIDDEN -- promptless on every node, must stay excluded.
      * ALP_ARENA_KIB -- a non-string default expr (kconfiglib's real
        `orig_defaults` entries are AST nodes, not strings) with TWO
        `orig_defaults` entries, so picking `[0]` vs `[-1]` yields a
        different value.
      * MYSTERY_TYPE -- a `type` absent from `_FAKE_TYPE_TO_STR`, exercising
        the `TYPE_TO_STR.get(sym.type, "unknown")` fallback.
    """
    syms = [
        _FakeSym(
            "SERIAL", _FAKE_BOOL,
            nodes=[
                _FakeNode(prompt=None, help=None),  # defconfig override, no prompt
                _FakeNode(prompt=("Serial drivers", None), help="Enable serial."),
            ],
            direct_dep="PLATFORM_HAS_SERIAL",
        ),
        _FakeSym(
            "STILL_HIDDEN", _FAKE_BOOL,
            nodes=[_FakeNode(prompt=None, help=None), _FakeNode(prompt=None, help=None)],
        ),
        _FakeSym(
            "ALP_ARENA_KIB", _FAKE_INT,
            nodes=[_FakeNode(prompt=("Arena size (KiB)", None), help=None)],
            orig_defaults=[(4, None), (5, None)],
        ),
        _FakeSym(
            "MYSTERY_TYPE", _FAKE_UNKNOWN_TYPE,
            nodes=[_FakeNode(prompt=("Mystery", None), help=None)],
        ),
    ]

    want = [
        {
            "name":    "ALP_ARENA_KIB",
            "type":    "int",
            "prompt":  "Arena size (KiB)",
            "depends": "",
            "default": "4",
            "help":    "",
        },
        {
            "name":    "MYSTERY_TYPE",
            "type":    "unknown",
            "prompt":  "Mystery",
            "depends": "",
            "default": None,
            "help":    "",
        },
        {
            "name":    "SERIAL",
            "type":    "bool",
            "prompt":  "Serial drivers",
            "depends": "PLATFORM_HAS_SERIAL",
            "default": None,
            "help":    "Enable serial.",
        },
    ]

    rendered_ns = _exec_rendered_dumper(ks)
    got = rendered_ns["_project_symbols"](syms)

    assert got == want


# ---------------------------------------------------------------------
# Requirement 4: a CMake target that reports success but writes nothing (or
# something unparseable) is a coded refusal, not a bare JSON error.
# ---------------------------------------------------------------------


def _patch_west_build(monkeypatch, ks, *, output_contents: str | None):
    """Fake `subprocess.run` for both `_load_board_symbols` calls: the
    `--cmake-only` configure succeeds with no side effect, and `-t
    <target>` "succeeds" (rc 0) while writing `output_contents` to the
    build dir's `alp_kconfig.json` -- reproducing a CMake target that
    reports success having produced nothing (or something malformed).
    `output_contents=None` reproduces the already-covered missing-file case
    (nothing written at all)."""

    def fake_run(cmd, **kwargs):
        if "-t" in cmd and "--cmake-only" not in cmd and output_contents is not None:
            build_dir = Path(cmd[cmd.index("-d") + 1])
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "alp_kconfig.json").write_text(
                output_contents, encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ks.subprocess, "run", fake_run)


def test_an_empty_output_is_a_coded_refusal_not_a_bare_json_error(monkeypatch, ks):
    _patch_west_build(monkeypatch, ks, output_contents="")
    with pytest.raises(ks.OrchestratorError, match="empty"):
        ks._load_board_symbols(Path("Z:/fake-zephyr-base"), "some_board_target")


def test_a_corrupt_output_is_a_coded_refusal_not_a_bare_json_error(monkeypatch, ks):
    _patch_west_build(monkeypatch, ks, output_contents="{ not json")
    with pytest.raises(ks.OrchestratorError, match="not valid JSON"):
        ks._load_board_symbols(Path("Z:/fake-zephyr-base"), "some_board_target")


def test_a_well_formed_output_still_loads_normally(monkeypatch, ks):
    _patch_west_build(monkeypatch, ks, output_contents='[{"name": "LOG"}]')
    assert ks._load_board_symbols(
        Path("Z:/fake-zephyr-base"), "some_board_target") == [{"name": "LOG"}]


@pytest.mark.parametrize("output_contents", ["null", "42", '{"a": 1}'])
def test_a_non_list_output_is_a_coded_refusal_not_a_silent_pass(
    monkeypatch, ks, output_contents
):
    """Well-formed JSON that isn't a symbol list (`null`/a bare number/an
    object) must not ship as a well-formed-looking envelope -- the same
    "never emit a partial/empty menu" promise the empty/corrupt-output
    guards above keep."""
    _patch_west_build(monkeypatch, ks, output_contents=output_contents)
    with pytest.raises(ks.OrchestratorError, match="not a list of symbols"):
        ks._load_board_symbols(Path("Z:/fake-zephyr-base"), "some_board_target")
