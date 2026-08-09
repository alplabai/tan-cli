# SPDX-License-Identifier: Apache-2.0
"""`tan model build` -- board.yaml resolution, compile-option path resolution,
and the driver-spawn contract, exercised end to end against a fake alp-sdk
checkout carrying a stub `alp_model.build.build_model`.

Port of `scripts/alp_cli/model.py`'s own shape; no committed Rust `model.rs`
exists (the retired forwarder in `crates/tan-cli/src/commands/sdk_cli.rs` is
the oracle for the outer envelope contract, not the model-building logic,
which stays alp-sdk's own).

The driver-spawn tests are real subprocesses against the *system* `python` on
PATH (this port's `_planner_python()` never uses `sys.executable`, matching
the Rust `resolve_python_binary` it mirrors) -- they are the only way to prove
the `PYTHONPATH`-prepend + stdin/stdout JSON contract with the SDK's own
`alp_model` package actually round-trips, not just that the surrounding
envelope code compiles. The skip guard below probes that SAME interpreter
(`_planner_python()`), not `sys.executable` -- on a host where the running
interpreter works but PATH has no `python`/`python3` (the frozen-`tan` case
this repo plans for), the spawn tests must skip, not fail for an environment
reason.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands import model_cmd
from tan.commands.model_cmd import model

app = typer.Typer(add_completion=False)
app.command("model")(model)

runner = CliRunner()

# The bare-PATH-name fallback `_planner_python` returns when no west-capable
# workspace `.venv` resolves for the args a given test actually passes --
# `str(tmp_path)` / a from-scratch fake SDK, per test, never `os.getcwd()` /
# `None`. Gating on `_planner_python(os.getcwd(), None)` at import time
# probes DIFFERENT args than the command under test ever receives: the two
# can each resolve a different `.venv` (or none), so the gate could
# green-light a run whose real interpreter is absent, or skip a run whose
# real interpreter is fine. `shutil.which` makes the gate what it was written
# for -- whether the bare PATH fallback name is reachable at all -- without
# guessing at a `_find_workspace_venv` walk this module doesn't control.
_HAS_PYTHON = shutil.which("python" if os.name == "nt" else "python3") is not None


def envelope(result):
    assert result.stdout.count("\n") == 1, result.stdout
    return json.loads(result.stdout)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def make_sdk(root: Path) -> Path:
    write(root / "scripts" / "alp_project.py", "")
    return root


STUB_BUILD_OK = '''
from pathlib import Path

def build_model(*, sku, name, source, out_dir, metadata_root, compile_opts=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{name}.alpmodel"
    out.write_bytes(b"stub-package")
    return out
'''

STUB_BUILD_FAILS_ONE = '''
from pathlib import Path

def build_model(*, sku, name, source, out_dir, metadata_root, compile_opts=None):
    if name == "bad":
        raise ValueError(f"no blob compiled for model '{name}'")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{name}.alpmodel"
    out.write_bytes(b"stub-package")
    return out
'''


def stub_alp_model(sdk_root: Path, body: str) -> None:
    write(sdk_root / "scripts" / "alp_model" / "__init__.py", "")
    write(sdk_root / "scripts" / "alp_model" / "build.py", body)


def board_yaml(root: Path, models: str = "") -> Path:
    path = root / "board.yaml"
    write(path, f"som:\n  sku: E1M-TEST\n{models}")
    return path


# --------------------------------------------------------------------------
# argument-shape / resolution refusals -- no subprocess spawned
# --------------------------------------------------------------------------


def test_unknown_subcommand_is_a_coded_refusal(tmp_path):
    result = runner.invoke(app, ["bogus", "--format", "json"], catch_exceptions=False)
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.unknown-subcommand"


def test_no_sdk_resolvable_refuses(tmp_path):
    board_yaml(tmp_path)
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(tmp_path / "nope"),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.sdk-root-unresolved"


def test_missing_board_yaml_refuses(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.board-yaml-missing"


def test_non_utf8_board_yaml_is_a_coded_validation_refusal_not_internal_failure(tmp_path):
    """tan-cli#396: `_load_board`'s docstring promises "a `ModelError` for
    every way that can fail -- missing file, bad encoding, not YAML, not a
    mapping", but `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so
    the bad-encoding half fell through to `model`'s outer catch-all instead:
    measured `exitCode: 5` / `model.internal-failure` / "model build failed
    unexpectedly: UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in
    position 32: invalid start byte". A script cannot tell "your board.yaml
    has a bad byte" from "tan broke" at exit 5."""
    sdk = make_sdk(tmp_path / "sdk")
    (tmp_path / "board.yaml").write_bytes(b"som:\n  sku: E1M-TEST\n# \xff\xfe\xfd\n")
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.board-yaml-invalid"
    assert doc["issues"][0]["severity"] == "error"


def test_no_models_declared_is_a_success_no_op(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    board_yaml(tmp_path)
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["data"]["built"] == []
    assert doc["data"]["sku"] == "E1M-TEST"


def test_missing_sku_refuses(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "board.yaml", "models: []\n")
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.board-yaml-invalid"


def test_run_driver_treats_empty_stdout_as_an_internal_failure_not_an_empty_ok(
    tmp_path, monkeypatch
):
    """A driver that exits 0 having printed NOTHING must not be coerced to
    `{}` (which would read as a legitimate empty result and let the caller's
    `result.get("results", [])` silently become `[]`) -- it must fall into
    the same unparsable-output failure a malformed document already does."""

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        model_cmd.subprocess, "run", lambda *a, **k: _Completed()
    )
    try:
        model_cmd._run_driver("python", tmp_path, {"models": []})
        raised = False
    except model_cmd.ModelError as err:
        raised = True
        assert err.code == "model.internal-failure"
        assert "unparsable output" in err.message
    assert raised


def test_run_driver_parses_the_last_line_ignoring_stray_earlier_output(
    tmp_path, monkeypatch
):
    """Mirrors `_python_too_old`'s own defence one screen up in this file: a
    future adapter `print()`, or a vendor tool that inherits stdout, must not
    turn every `tan model build` into `model.internal-failure`."""

    class _Completed:
        returncode = 0
        stdout = 'a vendor tool printed this first\n{"results": []}\n'
        stderr = ""

    monkeypatch.setattr(
        model_cmd.subprocess, "run", lambda *a, **k: _Completed()
    )
    assert model_cmd._run_driver("python", tmp_path, {"models": []}) == {"results": []}


def test_a_driver_that_reports_fewer_results_than_requested_is_an_internal_failure(
    tmp_path, monkeypatch
):
    """A driver that exits 0 but reports no result for a declared model must
    not read the same as the legitimate no-models no-op above -- both would
    otherwise report `ok: true` with `built: []`. No real spawn needed: the
    driver call itself is stubbed."""
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "source.tflite", "x")
    board_yaml(
        tmp_path,
        "models:\n  - name: mymodel\n    source: source.tflite\n",
    )
    monkeypatch.setattr(model_cmd, "_python_too_old", lambda python, floor: None)
    monkeypatch.setattr(model_cmd, "_run_driver", lambda *a, **k: {"results": []})
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code != 0
    doc = envelope(result)
    assert doc["ok"] is False
    assert doc["issues"][0]["code"] == "model.internal-failure"
    assert "mymodel" in doc["issues"][0]["message"]


# --------------------------------------------------------------------------
# real driver spawn -- against a stub alp_model on PATH's python
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_PYTHON, reason="no python interpreter available to spawn")
def test_a_built_model_reports_its_output_path(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    stub_alp_model(sdk, STUB_BUILD_OK)
    write(tmp_path / "source.tflite", "fake-tflite-bytes")
    board_yaml(
        tmp_path,
        "models:\n  - name: mymodel\n    source: source.tflite\n",
    )
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--out", "build/models",
            "--format", "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    doc = envelope(result)
    assert doc["ok"] is True
    built = doc["data"]["built"]
    assert len(built) == 1
    assert built[0].endswith("mymodel.alpmodel")
    assert (tmp_path / "build" / "models" / "mymodel.alpmodel").is_file()


@pytest.mark.skipif(not _HAS_PYTHON, reason="no python interpreter available to spawn")
def test_a_failed_model_is_an_issue_not_a_traceback_and_the_batch_continues(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    stub_alp_model(sdk, STUB_BUILD_FAILS_ONE)
    write(tmp_path / "good.tflite", "x")
    write(tmp_path / "bad.tflite", "x")
    board_yaml(
        tmp_path,
        "models:\n"
        "  - name: good\n    source: good.tflite\n"
        "  - name: bad\n    source: bad.tflite\n",
    )
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 3  # WriteFailure -- at least one model failed
    doc = envelope(result)
    assert doc["data"]["built"] == [
        str(tmp_path / "build" / "models" / "good.alpmodel")
    ] or doc["data"]["built"][0].endswith("good.alpmodel")
    codes = [i["code"] for i in doc["issues"]]
    assert "model.build-failed" in codes
    assert "no blob compiled" in doc["issues"][0]["message"]


@pytest.mark.skipif(not _HAS_PYTHON, reason="no python interpreter available to spawn")
def test_compile_opts_paths_are_resolved_absolute_relative_to_board_dir(tmp_path):
    """Port of `model.py::_resolve_compile`: string opt values become absolute
    paths relative to board.yaml's own directory. Also covers the two other
    values `build_model` uses to choose which silicon to compile for -- `sku`
    and `metadata_root` -- untested before: get either wrong and the driver
    silently compiles blobs for the wrong part."""
    sdk = make_sdk(tmp_path / "sdk")
    write(
        tmp_path / "sdk" / "scripts" / "alp_model" / "__init__.py", ""
    )
    write(
        tmp_path / "sdk" / "scripts" / "alp_model" / "build.py",
        '''
from pathlib import Path
import json, os

def build_model(*, sku, name, source, out_dir, metadata_root, compile_opts=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    # Record what this run actually received, so the test can assert on it.
    (out_dir / "opts.json").write_text(json.dumps({
        "sku": sku,
        "metadataRoot": str(metadata_root),
        "compileOpts": compile_opts,
    }))
    out = out_dir / f"{name}.alpmodel"
    out.write_bytes(b"x")
    return out
''',
    )
    write(tmp_path / "source.tflite", "x")
    write(tmp_path / "vela.ini", "x")
    board_yaml(
        tmp_path,
        "models:\n"
        "  - name: mymodel\n"
        "    source: source.tflite\n"
        "    compile:\n"
        "      ethos_u:\n"
        "        config: vela.ini\n",
    )
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(tmp_path),
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    opts = json.loads((tmp_path / "build" / "models" / "opts.json").read_text())
    assert opts["sku"] == "E1M-TEST"
    assert opts["metadataRoot"] == str(sdk / "metadata")
    assert opts["compileOpts"]["ethos_u"]["config"] == str((tmp_path / "vela.ini").resolve())


# --------------------------------------------------------------------------
# tan-cli#398 -- `--board-yaml` is the spelling `build`/`run`/`kconfig`/
# `validate`/`generate`/`inspect` all use for the board file, and the vscode
# extension's own CLI contract (`docs/CLI.md`, "Common flags") lists it among
# the flags "All commands should support". `model` declared only `--board` and
# took `--board-yaml` as one of `accept_global_flags`' INJECTED options, which
# are accepted and then dropped -- harmless for an arity-0 `--verbose`, a
# wrong answer for a flag that carries a value. Measured before the fix: the
# same file named two ways gave two different SKUs, BOTH at `exitCode: 0` with
# `issues: []`, so `build_model` compiled `.alpmodel` artefacts for the wrong
# silicon and nothing refused or warned.
# --------------------------------------------------------------------------


def _two_boards(tmp_path):
    """`<project>/board.yaml` (the DEFAULT `model` falls back to) and
    `<other>/board.yaml` (the one the caller names), with different
    `som.sku` -- `som.sku` is what selects which silicon `build_model`
    compiles for, so the SKU in the envelope names the file actually read."""
    project = tmp_path / "project"
    other = tmp_path / "other"
    write(project / "board.yaml", "som:\n  sku: E1M-AEN801\n")
    write(other / "board.yaml", "som:\n  sku: E1M-V2N101\n")
    return project, other / "board.yaml"


def test_board_yaml_spelling_reads_the_file_it_names_trailing_form(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    project, other_board = _two_boards(tmp_path)
    result = runner.invoke(
        app,
        [
            "build",
            "--project", str(project),
            "--sdk-root", str(sdk),
            "--board-yaml", str(other_board),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    doc = envelope(result)
    assert doc["data"]["sku"] == "E1M-V2N101", (
        "`--board-yaml` was accepted and dropped, so the default "
        "`<project>/board.yaml` was packaged instead of the file named"
    )
    # `.as_posix()`, not `str()`: `project.boardYaml` is POSIX-normalised
    # before it is emitted, so `str()` matches on a POSIX host only and this
    # assertion was green on macOS/ubuntu and red on windows-latest alone
    # (tan-cli#414). Normalise the filesystem side; the envelope is the
    # contract.
    assert doc["project"]["boardYaml"] == other_board.as_posix()


def test_board_yaml_spelling_reads_the_file_it_names_leading_form(tmp_path):
    """The canonical leading-global form the extension's own `withSdkRoot`
    produces (`tan --board-yaml <path> model build`). It goes through
    `cli._reorder_global_flags`, which relocates the flag across the
    subcommand boundary -- and, pre-fix, straight into the same drop. Driven
    through the REAL `tan.cli.app` registration plus that real rewrite, not
    this module's throwaway one-command app, because the rewrite is the half
    under test."""
    from tan.cli import _reorder_global_flags
    from tan.cli import app as real_app

    sdk = make_sdk(tmp_path / "sdk")
    project, other_board = _two_boards(tmp_path)
    argv = _reorder_global_flags(
        [
            "--board-yaml", str(other_board),
            "model", "build",
            "--project", str(project),
            "--sdk-root", str(sdk),
            "--format", "json",
        ]
    )
    result = runner.invoke(real_app, argv)
    assert result.exit_code == 0, result.stdout
    doc = envelope(result)
    assert doc["data"]["sku"] == "E1M-V2N101"
    # `.as_posix()`, not `str()`: `project.boardYaml` is POSIX-normalised
    # before it is emitted, so `str()` matches on a POSIX host only and this
    # assertion was green on macOS/ubuntu and red on windows-latest alone
    # (tan-cli#414). Normalise the filesystem side; the envelope is the
    # contract.
    assert doc["project"]["boardYaml"] == other_board.as_posix()


def test_board_and_board_yaml_are_the_same_option(tmp_path):
    """Both spellings must land on ONE parameter -- two independent options
    would put the port right back where tan-cli#398 found it the first time a
    caller passed the other one."""
    sdk = make_sdk(tmp_path / "sdk")
    project, other_board = _two_boards(tmp_path)
    common = ["--project", str(project), "--sdk-root", str(sdk), "--format", "json"]
    via_board = runner.invoke(app, ["build", "--board", str(other_board), *common])
    via_board_yaml = runner.invoke(app, ["build", "--board-yaml", str(other_board), *common])
    assert via_board.exit_code == via_board_yaml.exit_code == 0
    assert envelope(via_board) == envelope(via_board_yaml)


# --------------------------------------------------------------------------
# tan-cli#497 defect 3 -- the SDK-resolution warnings are no longer dropped
# --------------------------------------------------------------------------


def _broken_pin_project(tmp_path: Path) -> Path:
    """A project whose `.alp/sdk-path` names a checkout that does not resolve,
    beside a sibling `alp-sdk` that discovery DOES find -- so resolution
    answers with a DIFFERENT checkout than the pin names. `conftest.py`'s
    autouse fixture has already repointed HOME, so `~/.alp/sdk-default`
    cannot interfere."""
    make_sdk(tmp_path / "alp-sdk")
    project = tmp_path / "proj"
    board_yaml(project)
    write(
        project / ".alp" / "sdk-path",
        json.dumps({"sdkPath": str(tmp_path / "gone-checkout")}),
    )
    return project


def test_a_broken_project_pin_is_reported_on_a_model_build(tmp_path, monkeypatch):
    """tan-cli#497 defect 3. `model build` was the only
    `resolve_project_context` caller that read `.workspace_root`/`.board_yaml`
    /`.project()`/`.sdk` off the returned context and NONE of the three
    resolution facts, so it alone dropped BOTH `sdk.project-pin-unresolved`
    and `sdk.global-default-foreign-project` -- while `tan size` and `tan
    image`, on the identical resolver, reported them from the same directory.

    It matters most here: the `.alpmodel` packages are compiled against
    `<resolved checkout>/metadata`, i.e. against THAT checkout's
    target/backend table for `som.sku`.

    Fails against dev: there `issues` is `[]` at `ok: true`."""
    project = _broken_pin_project(tmp_path)
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["build", "--format", "json"])
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert [i["code"] for i in doc["issues"]] == ["sdk.project-pin-unresolved"]
    assert doc["issues"][0]["severity"] == "warning"
    assert "gone-checkout" in doc["issues"][0]["message"]
    assert doc["sdk"]["sourceTier"] == "discovery"


def test_a_broken_project_pin_reaches_model_text_mode_too(tmp_path, monkeypatch):
    """The DEFAULT mode, which `finish()` renders from `data.built` plus
    `issues`. Warnings LEAD, ahead of the result lines: which checkout
    answered is a fact about the whole run, not a per-model outcome.

    Fails against dev: stderr carries only the no-models line."""
    project = _broken_pin_project(tmp_path)
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 0
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert lines[0].startswith("warning: .alp/sdk-path names")
    assert "gone-checkout" in lines[0]
    # ...and the run's own line is NOT swallowed by the new warning: the
    # "nothing to build" branch used to key on `not issues`.
    assert any("no `models:` declared in board.yaml" in ln for ln in lines)


def test_a_broken_project_pin_survives_a_model_error_refusal(tmp_path, monkeypatch):
    """The `except ModelError` path. `resolve_project_context` used to be the
    first statement INSIDE `_run_build`, so its resolution facts were
    unreachable from the handler and the pin warning was dropped on every
    refusal too. Resolved by the caller now, so the ONE resolution feeds
    every exit.

    Fails against dev: `issues` is `[model.board-yaml-invalid]` alone."""
    make_sdk(tmp_path / "alp-sdk")
    project = tmp_path / "proj"
    write(project / "board.yaml", "models: []\n")  # no som.sku -> ModelError
    write(
        project / ".alp" / "sdk-path",
        json.dumps({"sdkPath": str(tmp_path / "gone-checkout")}),
    )
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["build", "--format", "json"])
    assert result.exit_code == 2
    doc = envelope(result)
    assert [i["code"] for i in doc["issues"]] == [
        "sdk.project-pin-unresolved",
        "model.board-yaml-invalid",
    ]


def test_a_clean_workspace_model_build_still_reports_no_issues(tmp_path):
    """The negative control: with no pin and no foreign global default the
    envelope must stay `issues: []`, or a fix that appended unconditionally
    would look identical to the cases above."""
    sdk = make_sdk(tmp_path / "sdk")
    board_yaml(tmp_path)
    result = runner.invoke(
        app,
        ["build", "--project", str(tmp_path), "--sdk-root", str(sdk), "--format", "json"],
    )
    assert result.exit_code == 0
    assert envelope(result)["issues"] == []


def test_the_internal_failure_catch_all_reports_the_pin_warning_too(
    tmp_path, monkeypatch
):
    """The outer `except Exception` -- the site that was left open in
    `kconfig`/`image`/`size` and is already closed here, pinned so it stays
    that way. `model` resolves in the OUTER function (`sdk_issues` is assigned
    between the `try:` and `_run_build`), so its handler can read the pair
    directly; no `SdkDisclosure` carrier is needed.

    Passes on the pre-fix branch too: this guards a property that is already
    true against a regression, it does not report a defect."""
    project = _broken_pin_project(tmp_path)
    monkeypatch.chdir(project)

    def boom(*args, **kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr("tan.commands.model_cmd._run_build", boom)
    result = runner.invoke(app, ["build", "--format", "json"])
    assert result.exit_code == 5
    doc = envelope(result)
    assert [i["code"] for i in doc["issues"]] == [
        "sdk.project-pin-unresolved",
        "model.internal-failure",
    ]
    assert "gone-checkout" in doc["issues"][0]["message"]


def test_the_internal_failure_catch_all_reaches_model_text_mode_too(
    tmp_path, monkeypatch
):
    """The DEFAULT mode, same site -- `finish`'s text branch prints warnings
    first, so the pair leads the crash line."""
    project = _broken_pin_project(tmp_path)
    monkeypatch.chdir(project)

    def boom(*args, **kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr("tan.commands.model_cmd._run_build", boom)
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 5
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert lines[0].startswith("warning: .alp/sdk-path names")
    assert any("model build failed unexpectedly" in ln for ln in lines)
