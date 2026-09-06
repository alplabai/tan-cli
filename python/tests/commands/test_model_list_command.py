# SPDX-License-Identifier: Apache-2.0
"""`tan model list` (tan-cli#674) -- what `board.yaml` declares next to what
`--out` already holds for it. Read-only: no SDK checkout is ever required or
even probed, unlike `build`/`check` (mirrors `doctor`'s SDK-root TOLERANCE,
but goes further -- `list` never even resolves or warns about one, since
nothing it reports comes out of `metadata/**`).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from tan.commands.model_cmd import model
from tan.model.manifest import Manifest, Target
from tan.model.package import write_package

app = typer.Typer(add_completion=False)
app.command("model")(model)

runner = CliRunner()


def envelope(result):
    assert result.stdout.count("\n") == 1, result.stdout
    return json.loads(result.stdout)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def make_sdk(root: Path) -> Path:
    write(root / "scripts" / "alp_project.py", "")
    return root


def board_yaml(root: Path, sku: str = "E1M-TEST", models: str = "") -> Path:
    path = root / "board.yaml"
    write(path, f"som:\n  sku: {sku}\n{models}")
    return path


def package_bytes(src_sha: bytes) -> bytes:
    """A minimal well-formed `.alpmodel`, parameterised on `src_sha` alone --
    what `_artifact_status`'s staleness check reads back. Everything else
    about the package is irrelevant to `list`, which never reads a target."""
    mft = Manifest(
        name="m",
        src_sha=src_sha,
        targets=[Target("cpu", "*", "tflite", "", 1024,
                         {"sram_kib": 0, "op_features": []}, 0,
                         compiler_version="passthrough")],
    )
    return write_package(mft, [b"BLOB"])


def write_built_package(out_dir: Path, name: str, built_from: bytes) -> None:
    """Write `<out_dir>/<name>.alpmodel` as if `tan model build` had just
    compiled it from source bytes `built_from` -- `src_sha` is the REAL
    sha256 of those bytes, exactly as `build_model` computes it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(built_from).digest()
    (out_dir / f"{name}.alpmodel").write_bytes(package_bytes(sha))


# --------------------------------------------------------------------------
# the golden subcommand inventory (tan-cli#674)
# --------------------------------------------------------------------------


def test_unknown_subcommand_now_lists_list_too(tmp_path):
    result = runner.invoke(app, ["bogus", "--format", "json"], catch_exceptions=False)
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["message"].endswith("Available: build, doctor, check, list.")


# --------------------------------------------------------------------------
# no SDK metadata is ever consulted
# --------------------------------------------------------------------------


def test_list_works_with_no_sdk_resolvable_at_all(tmp_path):
    """Unlike `build`/`check` (`model.sdk-root-unresolved`, exit 2) and even
    `doctor` (a `model.doctor-sdk-unresolved` WARNING), `list` raises no issue
    at all over an unresolvable SDK -- it never asks for one."""
    write(tmp_path / "source.tflite", "x")
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: source.tflite\n")
    result = runner.invoke(
        app, ["list", "--project", str(tmp_path), "--format", "json"], catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["issues"] == []
    assert doc.get("sdk") is None


def test_missing_board_yaml_refuses(tmp_path):
    result = runner.invoke(
        app, ["list", "--project", str(tmp_path), "--format", "json"],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.board-yaml-missing"


def test_no_models_declared_is_a_success_no_op(tmp_path):
    board_yaml(tmp_path)
    result = runner.invoke(
        app, ["list", "--project", str(tmp_path), "--format", "json"], catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["data"] == {"schemaVersion": "1", "sku": "E1M-TEST", "models": []}


def test_no_models_declared_text_mode_says_so(tmp_path):
    board_yaml(tmp_path)
    result = runner.invoke(app, ["list", "--project", str(tmp_path)], catch_exceptions=False)
    assert result.exit_code == 0
    assert "model list: no `models:` declared in board.yaml; nothing to list." in result.stderr


def test_missing_sku_does_not_refuse(tmp_path):
    """The one place `list` diverges from `build`/`check`'s `_require_sku`:
    naming what is declared and what is built needs no real SoM, so a
    missing/invalid `som.sku` degrades to `sku: null` instead of refusing."""
    write(tmp_path / "board.yaml", "models: []\n")
    result = runner.invoke(
        app, ["list", "--project", str(tmp_path), "--format", "json"], catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["data"]["sku"] is None


# --------------------------------------------------------------------------
# declared vs built
# --------------------------------------------------------------------------


def test_a_declared_model_with_no_package_reports_not_built(tmp_path):
    write(tmp_path / "source.tflite", "x")
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: source.tflite\n")
    result = runner.invoke(
        app, ["list", "--project", str(tmp_path), "--format", "json"], catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    entry = doc["data"]["models"][0]
    assert entry["name"] == "m"
    assert entry["source"] == str((tmp_path / "source.tflite").resolve())
    assert entry["artifact"] == {"exists": False}


def test_a_declared_model_with_a_fresh_package_reports_size_and_not_stale(tmp_path):
    source_bytes = b"the current model bytes"
    write(tmp_path / "source.tflite", "")
    (tmp_path / "source.tflite").write_bytes(source_bytes)
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: source.tflite\n")
    write_built_package(tmp_path / "build" / "models", "m", built_from=source_bytes)

    result = runner.invoke(
        app, ["list", "--project", str(tmp_path), "--format", "json"], catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    artifact = doc["data"]["models"][0]["artifact"]
    assert artifact["exists"] is True
    assert artifact["stale"] is False
    expected_bytes = (tmp_path / "build" / "models" / "m.alpmodel").stat().st_size
    assert artifact["bytes"] == expected_bytes


def test_a_declared_model_whose_source_changed_since_build_is_reported_stale(tmp_path):
    (tmp_path / "source.tflite").write_bytes(b"edited after the last build")
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: source.tflite\n")
    write_built_package(tmp_path / "build" / "models", "m", built_from=b"the ORIGINAL bytes")

    result = runner.invoke(
        app, ["list", "--project", str(tmp_path), "--format", "json"], catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    artifact = doc["data"]["models"][0]["artifact"]
    assert artifact["exists"] is True
    assert artifact["stale"] is True


def test_a_custom_out_directory_is_honoured(tmp_path):
    write(tmp_path / "source.tflite", "x")
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: source.tflite\n")
    write_built_package(tmp_path / "artifacts", "m", built_from=b"x")

    result = runner.invoke(
        app,
        ["list", "--project", str(tmp_path), "--out", "artifacts", "--format", "json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert envelope(result)["data"]["models"][0]["artifact"]["exists"] is True


def test_an_unreadable_package_degrades_to_bare_exists_true(tmp_path):
    """A corrupt/truncated container must not crash `list` or hide that a
    file is there at all -- `bytes`/`stale` are best-effort, `exists` is not."""
    write(tmp_path / "source.tflite", "x")
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: source.tflite\n")
    out_dir = tmp_path / "build" / "models"
    out_dir.mkdir(parents=True)
    (out_dir / "m.alpmodel").write_bytes(b"not a real container")

    result = runner.invoke(
        app, ["list", "--project", str(tmp_path), "--format", "json"], catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    artifact = doc["data"]["models"][0]["artifact"]
    assert artifact["exists"] is True
    assert "stale" not in artifact


def test_a_source_that_no_longer_resolves_reports_a_warning_not_silence(tmp_path):
    """tan-cli#674 review MAJOR 1: a `board.yaml` declaring a `source` that no
    longer resolves, with a VALID package already on disk, must not answer a
    clean `exists: True` row with no sign the staleness check never ran --
    that used to leave `ok: true` / `issues: []` / no `stale` key, which reads
    identically to a package the tool successfully confirmed is fresh."""
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: gone.tflite\n")
    write_built_package(tmp_path / "build" / "models", "m", built_from=b"whatever")

    result = runner.invoke(
        app, ["list", "--project", str(tmp_path), "--format", "json"], catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    artifact = doc["data"]["models"][0]["artifact"]
    assert artifact["exists"] is True
    assert "stale" not in artifact
    codes = [i["code"] for i in doc["issues"]]
    assert "model.artifact-stale-unknown" in codes
    severities = {i["code"]: i["severity"] for i in doc["issues"]}
    assert severities["model.artifact-stale-unknown"] == "warning"


# --------------------------------------------------------------------------
# text mode
# --------------------------------------------------------------------------


def test_text_mode_renders_one_line_per_declared_model(tmp_path):
    write(tmp_path / "built.tflite", "x")
    write(tmp_path / "missing.tflite", "x")
    board_yaml(
        tmp_path,
        models=(
            "models:\n"
            "  - name: built\n    source: built.tflite\n"
            "  - name: missing\n    source: missing.tflite\n"
        ),
    )
    write_built_package(tmp_path / "build" / "models", "built", built_from=b"x")

    result = runner.invoke(app, ["list", "--project", str(tmp_path)], catch_exceptions=False)
    assert result.exit_code == 0
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert any(ln.startswith("built: built") and "bytes" in ln for ln in lines)
    assert any(ln == f"missing: not built ({tmp_path / 'missing.tflite'})" for ln in lines)


def test_text_mode_flags_a_stale_package(tmp_path):
    (tmp_path / "source.tflite").write_bytes(b"new bytes")
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: source.tflite\n")
    write_built_package(tmp_path / "build" / "models", "m", built_from=b"old bytes")

    result = runner.invoke(app, ["list", "--project", str(tmp_path)], catch_exceptions=False)
    assert result.exit_code == 0
    assert any("STALE" in ln for ln in result.stderr.splitlines())


# --------------------------------------------------------------------------
# the finish() dispatch collision `list`/`check` both carry `data.models`
# --------------------------------------------------------------------------


def test_list_text_never_routes_through_checks_renderer(tmp_path):
    """tan-cli#674 trap 3: `finish()`'s text dispatch used to key on `"models"
    in data` alone, and `check`'s payload carries that same key -- a `list`
    payload would have rendered through `render_check_text` (which prints an
    `npuCoverage` line, never a byte count) instead of its own lines."""
    write(tmp_path / "source.tflite", "x")
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: source.tflite\n")
    result = runner.invoke(app, ["list", "--project", str(tmp_path)], catch_exceptions=False)
    assert result.exit_code == 0
    assert "not built" in result.stderr
    assert "npuCoverage" not in result.stderr
    assert "undetermined" not in result.stderr


# --------------------------------------------------------------------------
# the shared project-context preamble still applies
# --------------------------------------------------------------------------


def test_a_broken_project_pin_is_reported_on_a_model_list(tmp_path, monkeypatch):
    make_sdk(tmp_path / "alp-sdk")
    project = tmp_path / "proj"
    board_yaml(project)
    write(
        project / ".alp" / "sdk-path",
        json.dumps({"sdkPath": str(tmp_path / "gone-checkout")}),
    )
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["list", "--format", "json"], catch_exceptions=False)
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert [i["code"] for i in doc["issues"]] == ["sdk.project-pin-unresolved"]
