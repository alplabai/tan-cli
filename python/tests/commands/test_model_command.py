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
