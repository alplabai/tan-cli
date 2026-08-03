# SPDX-License-Identifier: Apache-2.0
"""`tan kconfig` -- CLI surface tests.

`kconfig` is not registered in `tan.cli.app` by this change (the shared
`cli.py` registration point is owned by the orchestrator wiring commands in
parallel), so these tests mount the command on a throwaway `typer.Typer()`
rather than importing `tan.cli.app` -- matching `test_faultdecode_command.py`'s
own note for the same situation.

Named twin: `crates/tan-cli/src/commands/kconfig.rs`'s `#[cfg(test)] mod
tests`. These tests cover the same setup-class-check ladder (no SDK root, no
board.yaml, an ambiguous/absent Zephyr core, no bootstrapped workspace) --
none of them need a real Zephyr checkout, so all of them run unconditionally.
The one thing they cannot reach hermetically is a real symbol menu (needs a
bootstrapped `ZEPHYR_BASE`); that is `tan.planner_root.emit`'s own contract,
already exercised by `tests/core/test_kconfig_symbols.py`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands.kconfig_cmd import _CoreResolutionError, _resolve_core, kconfig

app = typer.Typer()
app.command("kconfig")(kconfig)

runner = CliRunner()


def _sdk_root(tmp_path: Path) -> Path:
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    return sdk


def _project(tmp_path: Path, board_yaml_text: str | None) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    if board_yaml_text is not None:
        (proj / "board.yaml").write_text(board_yaml_text, encoding="utf-8")
    return proj


def test_help_lists_the_core_flag() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--core" in result.output


def test_no_sdk_root_reports_kconfig_no_sdk_root(tmp_path: Path) -> None:
    proj = _project(tmp_path, "cores:\n  m55_he:\n    os: zephyr\n")
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 2
    # `--format json` writes exactly one document, to STDOUT -- not the
    # combined `result.output` (stdout+stderr), which would still parse even
    # if the envelope leaked onto stderr, unpinning the stream contract the
    # vscode extension actually relies on.
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["issues"][0]["code"] == "kconfig.no-sdk-root"
    assert envelope["data"]["schemaVersion"] == 1
    assert envelope["data"]["symbols"] == []
    # No SDK resolved -- nothing to report; matches the oracle, which omits
    # `sdk` on this one failure only.
    assert "sdk" not in envelope
    # Finding 6: the em dash (U+2014), byte-for-byte, not a respelled `--`
    # (captured from `target/debug/tan.exe`).
    # tan-cli#305: `tan sdk switch`/`tan bootstrap` both used to be named
    # here and neither is a working remedy for an unresolved SDK in this
    # build -- see NO_SDK_NEXT_STEPS's own docstring in `sdk_cmd.py`.
    assert (
        envelope["issues"][0]["message"]
        == "no alp-sdk checkout found — pass `--sdk-root <PATH>`, or get an alp-sdk "
        "checkout (`git clone https://github.com/alplabai/alp-sdk`), then point tan "
        "at it with `--sdk-root <path>`."
    )


def test_missing_board_yaml_reports_kconfig_board_yaml_missing(tmp_path: Path) -> None:
    sdk = _sdk_root(tmp_path)
    proj = _project(tmp_path, None)
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "kconfig.board-yaml-missing"
    assert envelope["sdk"]["sourceTier"] == "sdkRootFlag"


def test_ambiguous_cores_reports_kconfig_core_ambiguous_and_names_them(
    tmp_path: Path,
) -> None:
    sdk = _sdk_root(tmp_path)
    proj = _project(
        tmp_path, "cores:\n  m55_he:\n    os: zephyr\n  m55_hp:\n    os: zephyr\n"
    )
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 2
    issue = json.loads(result.stdout)["issues"][0]
    assert issue["code"] == "kconfig.core-ambiguous"
    assert "m55_he" in issue["message"]
    assert "m55_hp" in issue["message"]


def test_no_workspace_reports_kconfig_no_workspace_once_core_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk = _sdk_root(tmp_path)
    proj = _project(tmp_path, "cores:\n  m55_he:\n    os: zephyr\n")
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "kconfig.no-workspace"
    # The core WAS resolved (single declared Zephyr core) before the
    # workspace check ran, and the failing envelope still reports it.
    assert envelope["data"]["core"] == "m55_he"
    assert envelope["sdk"]["sourceTier"] == "sdkRootFlag"
    # Finding 6: the em dash (U+2014), byte-for-byte.
    assert (
        envelope["issues"][0]["message"]
        == "no bootstrapped Zephyr workspace found for `--emit kconfig` "
        "(needs ZEPHYR_BASE) — run `tan bootstrap` first."
    )


def test_explicit_core_skips_the_board_yaml_read_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No board.yaml on disk at all -- an explicit `--core` must not need one,
    matching Rust's `resolve_core_uses_the_explicit_flag_without_touching_board_yaml`."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk = _sdk_root(tmp_path)
    proj = _project(tmp_path, None)
    result = runner.invoke(
        app,
        [
            "--project", str(proj), "--sdk-root", str(sdk),
            "--core", "m55_he", "--format", "json",
        ],
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    # Reaches the WORKSPACE check, not board-yaml-missing.
    assert envelope["issues"][0]["code"] == "kconfig.no-workspace"


def test_text_mode_failure_goes_to_stderr_not_stdout(tmp_path: Path) -> None:
    """Planted break: `print(..., file=sys.stderr)` -> `print(...)` at
    `_fail`'s text-mode branch left this test at `13 passed` -- `result.output`
    is stdout+stderr COMBINED, so asserting against it cannot tell the two
    streams apart. Assert each stream independently instead."""
    proj = _project(tmp_path, "cores:\n  m55_he:\n    os: zephyr\n")
    result = runner.invoke(app, ["--project", str(proj)])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("kconfig: no alp-sdk checkout found")


# ---------------------------------------------------------------------------
# Everything past the workspace check: the emit call, the version-skew
# guard, the JSON/shape parse, the success envelope and both text-render
# paths. Previously untested -- four separate planted breaks (the guard
# deleted, `--core` swapped for a literal, the text render mangled, the
# success envelope's exit code swapped) each left `13 passed`. Hermetic:
# `tan.planner_root.emit` and `_resolve_zephyr_base` are both monkeypatched,
# so none of this needs a real SDK checkout or a bootstrapped Zephyr
# workspace.
# ---------------------------------------------------------------------------

#: The canonical `--emit kconfig` contract anchor (alp-sdk#893/#894),
#: vendored byte-for-byte at `tests/fixtures/kconfig-contract/
#: emit-kconfig.golden.json` -- the SAME file `crates/tan-cli/src/commands/
#: kconfig.rs`'s own tests read (`CANONICAL_EMIT_KCONFIG_FIXTURE`), one
#: vendored copy on disk shared by both implementations' tests.
_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "kconfig-contract"
    / "emit-kconfig.golden.json"
)


def _golden_emit_body() -> dict:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _stub_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the workspace setup-class check hermetically -- nothing past
    it needs a real bootstrapped Zephyr checkout."""
    monkeypatch.setattr(
        "tan.commands.kconfig_cmd._resolve_zephyr_base",
        lambda root, sdk_root: "/fake/zephyr-base",
    )


def test_core_flag_reaches_the_planner_emit_call_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planted break (b): `core=resolved_core` -> `core="WRONG"` in the
    `_plan_emit` call left `13 passed` -- `--core` becomes accepted but
    silently ignored, and the customer gets another core's menu. Assert the
    actual kwarg the emit call received, not just the exit code."""
    sdk = _sdk_root(tmp_path)
    proj = _project(
        tmp_path, "cores:\n  m55_he:\n    os: zephyr\n  m55_hp:\n    os: zephyr\n"
    )
    _stub_workspace(monkeypatch)
    received: dict = {}

    def fake_emit(mode, *, root, board_yaml, core=None, **kwargs):
        received["mode"] = mode
        received["core"] = core
        return json.dumps(_golden_emit_body())

    monkeypatch.setattr("tan.planner_root.emit", fake_emit)
    result = runner.invoke(
        app,
        [
            "--project", str(proj), "--sdk-root", str(sdk),
            "--core", "m55_hp", "--format", "json",
        ],
    )
    assert result.exit_code == 0
    assert received["mode"] == "kconfig"
    assert received["core"] == "m55_hp"


def test_default_core_resolution_reaches_the_planner_emit_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same guard as above, for the `--core`-omitted path: the resolved
    default core must reach the emit call too."""
    sdk = _sdk_root(tmp_path)
    proj = _project(tmp_path, "cores:\n  m55_he:\n    os: zephyr\n")
    _stub_workspace(monkeypatch)
    received: dict = {}

    def fake_emit(mode, *, root, board_yaml, core=None, **kwargs):
        received["core"] = core
        return json.dumps(_golden_emit_body())

    monkeypatch.setattr("tan.planner_root.emit", fake_emit)
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 0
    assert received["core"] == "m55_he"


def test_success_envelope_reports_the_emit_body_as_data_and_sdk_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planted break (d): the success envelope emitted with
    `ExitCode.RUNTIME_FAILURE` left `13 passed` too -- assert exit 0 AND that
    `data` is exactly the emit body (Finding 2's rebuild-from-modelled-fields
    path), AND that the `sdk` block (Finding 1) is present."""
    sdk = _sdk_root(tmp_path)
    proj = _project(tmp_path, "cores:\n  m55_he:\n    os: zephyr\n")
    _stub_workspace(monkeypatch)
    body = _golden_emit_body()
    monkeypatch.setattr("tan.planner_root.emit", lambda *a, **kw: json.dumps(body))

    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True
    assert envelope["data"] == body
    assert envelope["sdk"]["sourceTier"] == "sdkRootFlag"


def test_bumped_schema_version_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planted break (a): `if not isinstance(...) or data.get("schemaVersion")
    != KCONFIG_SCHEMA_VERSION:` -> `if False:` (guard deleted) left `13
    passed`."""
    sdk = _sdk_root(tmp_path)
    proj = _project(tmp_path, "cores:\n  m55_he:\n    os: zephyr\n")
    _stub_workspace(monkeypatch)
    body = _golden_emit_body()
    body["schemaVersion"] = 2
    monkeypatch.setattr("tan.planner_root.emit", lambda *a, **kw: json.dumps(body))

    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "kconfig.parse-failed"
    assert "schemaVersion" in envelope["issues"][0]["message"]


def test_non_json_emit_stdout_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = _sdk_root(tmp_path)
    proj = _project(tmp_path, "cores:\n  m55_he:\n    os: zephyr\n")
    _stub_workspace(monkeypatch)
    monkeypatch.setattr("tan.planner_root.emit", lambda *a, **kw: "not json at all")

    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "kconfig.parse-failed"


def test_shape_validation_rejects_a_missing_required_symbol_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2's coverage: a structurally valid JSON document that is
    missing a required `KconfigSymbol` key (an SDK rename) must not be a
    silent exit 0."""
    sdk = _sdk_root(tmp_path)
    proj = _project(tmp_path, "cores:\n  m55_he:\n    os: zephyr\n")
    _stub_workspace(monkeypatch)
    body = _golden_emit_body()
    del body["symbols"][0]["depends"]
    monkeypatch.setattr("tan.planner_root.emit", lambda *a, **kw: json.dumps(body))

    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "kconfig.parse-failed"
    assert "depends" in envelope["issues"][0]["message"]


def test_text_mode_reports_the_summary_line_verbatim_and_verbose_symbol_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planted break (c): `_text_lines` summary mangled and the `--verbose`
    symbol line deleted left `13 passed` -- `--verbose` had no
    observable-effect test."""
    sdk = _sdk_root(tmp_path)
    proj = _project(tmp_path, "cores:\n  m55_he:\n    os: zephyr\n")
    _stub_workspace(monkeypatch)
    body = _golden_emit_body()
    monkeypatch.setattr("tan.planner_root.emit", lambda *a, **kw: json.dumps(body))

    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--verbose"]
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert lines[0] == (
        f"kconfig: {len(body['symbols'])} symbol(s) for core '{body['core']}' "
        f"({body['board']})"
    )
    assert len(lines) == 1 + len(body["symbols"])
    first = body["symbols"][0]
    assert lines[1] == f"  {first['name']} [{first['type']}] — {first['prompt']}"


def test_zephyr_base_resolves_via_the_project_tree_walked_upward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5, tier 1: an ordinary `tan bootstrap` creates the west
    workspace as an ancestor of the project dir and does not export
    `$ZEPHYR_BASE` into the caller's shell -- `tan kconfig` must still reach
    the emit call."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk = _sdk_root(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / ".west").mkdir(parents=True)
    (workspace / "zephyr").mkdir()
    proj = workspace / "app"
    proj.mkdir()
    (proj / "board.yaml").write_text("cores:\n  m55_he:\n    os: zephyr\n", encoding="utf-8")

    monkeypatch.setattr(
        "tan.planner_root.emit", lambda *a, **kw: json.dumps(_golden_emit_body())
    )
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 0


def test_zephyr_base_resolves_via_the_sdk_derived_zephyrproject_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5, tier 3: the legacy `<sdk-parent>/zephyrproject` layout,
    with no project-tree workspace and no exported `$ZEPHYR_BASE`."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk = _sdk_root(tmp_path)
    workspace = tmp_path / "zephyrproject"
    (workspace / ".west").mkdir(parents=True)
    (workspace / "zephyr").mkdir()
    proj = _project(tmp_path, "cores:\n  m55_he:\n    os: zephyr\n")

    monkeypatch.setattr(
        "tan.planner_root.emit", lambda *a, **kw: json.dumps(_golden_emit_body())
    )
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 0


def test_the_resolved_base_not_the_ambient_one_reaches_the_emit_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5's second half: `kconfig_symbols` reads
    `os.environ["ZEPHYR_BASE"]` directly (in-process, no child env to set it
    on), so the RESOLVED base -- from tier 1 here, which outranks an already-
    exported `$ZEPHYR_BASE` -- must be what's actually in the environment
    during the emit call, not a stale/wrong ambient value. Also confirms the
    injection is undone afterwards so it has no lasting effect on the
    process env."""
    monkeypatch.setenv("ZEPHYR_BASE", str(tmp_path / "some-other-unrelated-zephyr"))
    sdk = _sdk_root(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / ".west").mkdir(parents=True)
    (workspace / "zephyr").mkdir()
    proj = workspace / "app"
    proj.mkdir()
    (proj / "board.yaml").write_text("cores:\n  m55_he:\n    os: zephyr\n", encoding="utf-8")

    seen: dict = {}

    def fake_emit(mode, *, root, board_yaml, core=None, **kwargs):
        seen["ZEPHYR_BASE"] = os.environ.get("ZEPHYR_BASE")
        return json.dumps(_golden_emit_body())

    monkeypatch.setattr("tan.planner_root.emit", fake_emit)
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 0
    assert seen["ZEPHYR_BASE"] == str(workspace / "zephyr")
    # Restored, not left mutated for the next command/test in this process.
    assert os.environ.get("ZEPHYR_BASE") == str(tmp_path / "some-other-unrelated-zephyr")


# ---------------------------------------------------------------------------
# `_resolve_core` unit tests -- named twins of kconfig.rs's own.
# ---------------------------------------------------------------------------


def test_resolve_core_uses_the_explicit_flag_without_touching_board_yaml(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    assert _resolve_core("m55_he", str(missing)) == "m55_he"


def test_resolve_core_picks_the_one_declared_zephyr_core(tmp_path: Path) -> None:
    board_yaml = tmp_path / "board.yaml"
    board_yaml.write_text(
        "cores:\n  m55_he:\n    os: zephyr\n  a55_cluster:\n    os: yocto\n",
        encoding="utf-8",
    )
    assert _resolve_core(None, str(board_yaml)) == "m55_he"


def test_resolve_core_is_ambiguous_with_two_zephyr_cores(tmp_path: Path) -> None:
    board_yaml = tmp_path / "board.yaml"
    board_yaml.write_text(
        "cores:\n  m55_he:\n    os: zephyr\n  m55_hp:\n    os: zephyr\n",
        encoding="utf-8",
    )
    with pytest.raises(_CoreResolutionError) as excinfo:
        _resolve_core(None, str(board_yaml))
    assert excinfo.value.code == "kconfig.core-ambiguous"


def test_resolve_core_reports_missing_board_yaml(tmp_path: Path) -> None:
    with pytest.raises(_CoreResolutionError) as excinfo:
        _resolve_core(None, str(tmp_path / "board.yaml"))
    assert excinfo.value.code == "kconfig.board-yaml-missing"


def test_resolve_core_reports_a_non_utf8_board_yaml_as_a_coded_refusal(
    tmp_path: Path,
) -> None:
    """tan-cli#396: `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so
    the read guard could not fire for a board.yaml saved in cp1252/latin-1 or
    carrying one stray byte -- it escaped as a raw `UnicodeDecodeError`.

    `kconfig.board-yaml-missing` is the oracle's own code for this: Rust's
    `read_to_string` fails with an `io::Error` on non-UTF-8 bytes, and
    `kconfig.rs:156-161` maps every read failure to that one code.
    """
    board_yaml = tmp_path / "board.yaml"
    board_yaml.write_bytes(b"cores:\n  cm33:\n    os: zephyr\n# \xff\xfe\xfd\n")
    with pytest.raises(_CoreResolutionError) as excinfo:
        _resolve_core(None, str(board_yaml))
    assert excinfo.value.code == "kconfig.board-yaml-missing"


def test_a_non_utf8_board_yaml_is_an_envelope_not_zero_bytes(tmp_path: Path) -> None:
    """The shape that made tan-cli#396 worth an issue: rc 1 with ZERO bytes on
    stdout. A JSON consumer gets nothing to parse, so the extension's matches
    fail open and the `prj.conf` symbol menu silently renders empty or stale.

    Asserted on `result.stdout` alone -- the envelope has to be on the stdout
    stream, not merely somewhere in the combined output.
    """
    sdk = _sdk_root(tmp_path)
    proj = _project(tmp_path, None)
    (proj / "board.yaml").write_bytes(b"cores:\n  cm33:\n    os: zephyr\n# \xff\xfe\xfd\n")
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["issues"][0]["code"] == "kconfig.board-yaml-missing"
    # The offending byte is named, so the remedy ("re-save board.yaml as UTF-8")
    # is derivable from the envelope alone -- the user cannot see the decode
    # error any other way now that it no longer reaches the terminal.
    assert "0xff" in envelope["issues"][0]["message"]


def test_resolve_core_reports_invalid_yaml(tmp_path: Path) -> None:
    board_yaml = tmp_path / "board.yaml"
    board_yaml.write_text("cores: [this is not a mapping\n", encoding="utf-8")
    with pytest.raises(_CoreResolutionError) as excinfo:
        _resolve_core(None, str(board_yaml))
    assert excinfo.value.code == "kconfig.board-yaml-invalid"


def test_resolve_core_no_cores_declared_names_the_gap(tmp_path: Path) -> None:
    board_yaml = tmp_path / "board.yaml"
    board_yaml.write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    with pytest.raises(_CoreResolutionError) as excinfo:
        _resolve_core(None, str(board_yaml))
    assert excinfo.value.code == "kconfig.core-ambiguous"
    assert "no cores" in excinfo.value.message


def test_resolve_core_reports_a_list_shaped_board_yaml_as_invalid_not_ambiguous(
    tmp_path: Path,
) -> None:
    """Finding 7: a structurally broken (list-shaped) board.yaml is
    syntactically valid YAML, so the naive `isinstance(doc, dict)` guard used
    to swallow it and fall through to `kconfig.core-ambiguous` -- same exit
    code, wrong issue code, wrong remedy (verified against
    `target/debug/tan.exe`: `kconfig.board-yaml-invalid` /
    "...board.yaml is not valid YAML: invalid type: sequence, expected struct
    BoardModel")."""
    board_yaml = tmp_path / "board.yaml"
    board_yaml.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(_CoreResolutionError) as excinfo:
        _resolve_core(None, str(board_yaml))
    assert excinfo.value.code == "kconfig.board-yaml-invalid"
    assert "not valid YAML" in excinfo.value.message


def test_resolve_core_reports_a_list_shaped_cores_block_as_invalid(
    tmp_path: Path,
) -> None:
    """Finding 7's second case: `cores:` present but not itself a mapping."""
    board_yaml = tmp_path / "board.yaml"
    board_yaml.write_text("cores:\n  - 1\n  - 2\n", encoding="utf-8")
    with pytest.raises(_CoreResolutionError) as excinfo:
        _resolve_core(None, str(board_yaml))
    assert excinfo.value.code == "kconfig.board-yaml-invalid"
    assert "not valid YAML" in excinfo.value.message
