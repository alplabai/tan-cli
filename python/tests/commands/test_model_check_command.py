# SPDX-License-Identifier: Apache-2.0
"""`tan model check` -- the CLI surface over `tan.model.check`/
`tan.model.analyze` (ADR-0028 amendment, tan-cli#782 Tasks 4/6): the static
NPU-eligibility screen for a board.yaml's declared `models:`.

Two families of test here: hermetic wiring through a REAL synthetic
`metadata/` tree (`--metadata-root`, same trick `test_compile_opts_paths_
are_resolved_absolute_relative_to_board_dir` uses for `build`), and CLI-shape
tests that monkeypatch `model_cmd.check_model_backends` directly to isolate
the envelope/text-rendering contract from the engine underneath it (the
engine itself is `tests/model/test_check.py`'s job, including the real-SKU
V2N/V2M cases ADR-0028 calls out).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
from typer.testing import CliRunner

from tan.commands import model_cmd
from tan.commands.model_cmd import model
from tan.model.analyze import BackendReport, OpVerdict

app = typer.Typer(add_completion=False)
app.command("model")(model)

runner = CliRunner()

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "models" / "tiny_int8.tflite"


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


def write_metadata(meta: Path, sku: str, silicon: str, npus: list[dict],
                    *, ethos_u_variant: str | None = None) -> None:
    """A throwaway `e1m_modules/` + `socs/` tree resolve_check_backends and
    resolve_ethos_u_variant can read for real -- the same synthetic-metadata
    trick `tests/model/test_check.py`/`test_analyze.py` use, wired through
    `--metadata-root` here instead of a direct function call."""
    body = f"silicon: {silicon}\n"
    if ethos_u_variant:
        body += f"inference:\n  ethos_u_variant: {ethos_u_variant}\n"
    write(meta / "e1m_modules" / f"{sku}.yaml", body)
    vendor, family, part = silicon.split(":")
    write(meta / "socs" / vendor / family / f"{part}.json",
          json.dumps({"ref": silicon, "npus": npus}))


def write_table(meta: Path, backend: str, filename: str, *, variant: str, supported: list[str]) -> None:
    write(meta / "npu_ops" / backend / filename, json.dumps({
        "applies_to": {"variant": variant, "products": [], "toolchain": "x", "toolchain_version": "1"},
        "op_namespace": "tflite", "authority": "tool-generated", "stance": "screening",
        "provenance": {}, "supported_ops": supported,
    }))


def _fake_report(**overrides) -> BackendReport:
    base = dict(backend="ethos_u", variant="u55", table="/x/u55@vela-1.0.0.json",
                npu_coverage="partial", compute_on_npu_pct_max=96.0, uncosted_cpu_op_count=0,
                ops=[OpVerdict(op="CONV_2D", status="npu-eligible", reason="constraint-unchecked", macs=100),
                     OpVerdict(op="NORMALIZE", status="cpu-certain", reason="op-not-in-table", macs=0),
                     OpVerdict(op="TOPK", status="cpu-certain", reason="op-not-in-table", macs=0)],
                basis="static-screen", confidence="screening",
                notes=["static screen (screening): operator-name membership against "
                       "u55@vela-1.0.0.json only. Eligible ops still carry unchecked "
                       "quantization/shape/dtype constraints this check cannot verify -- "
                       "the model will run either way, an unsupported op falls back to "
                       "the CPU silently rather than failing. Only a real compile proves "
                       "NPU execution."])
    base.update(overrides)
    return BackendReport(**base)


# --------------------------------------------------------------------------
# argument-shape / resolution refusals -- check_model_backends never reached
# --------------------------------------------------------------------------


def test_unknown_subcommand_now_lists_check_too(tmp_path):
    result = runner.invoke(app, ["bogus", "--format", "json"], catch_exceptions=False)
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["message"].endswith("Available: build, doctor, check.")


def test_no_sdk_resolvable_refuses(tmp_path):
    board_yaml(tmp_path)
    result = runner.invoke(
        app,
        ["check", "--project", str(tmp_path), "--sdk-root", str(tmp_path / "nope"), "--format", "json"],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.sdk-root-unresolved"
    assert "No models were checked." in doc["issues"][0]["message"]
    assert doc["data"] == {"schemaVersion": "1", "sku": None, "exact": False, "models": []}


def test_missing_board_yaml_refuses(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    result = runner.invoke(
        app, ["check", "--project", str(tmp_path), "--sdk-root", str(sdk), "--format", "json"],
    )
    assert result.exit_code == 2
    assert envelope(result)["issues"][0]["code"] == "model.board-yaml-missing"


def test_missing_sku_refuses(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "board.yaml", "models: []\n")
    result = runner.invoke(
        app, ["check", "--project", str(tmp_path), "--sdk-root", str(sdk), "--format", "json"],
    )
    assert result.exit_code == 2
    assert envelope(result)["issues"][0]["code"] == "model.board-yaml-invalid"


def test_no_models_declared_is_a_success_no_op(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    board_yaml(tmp_path)
    result = runner.invoke(
        app, ["check", "--project", str(tmp_path), "--sdk-root", str(sdk), "--format", "json"],
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["data"] == {"schemaVersion": "1", "sku": "E1M-TEST", "exact": False, "models": []}


def test_no_models_declared_text_mode_says_so(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    board_yaml(tmp_path)
    result = runner.invoke(app, ["check", "--project", str(tmp_path), "--sdk-root", str(sdk)])
    assert result.exit_code == 0
    assert "model check: no `models:` declared in board.yaml; nothing to check." in result.stderr


def test_an_unresolvable_sku_is_a_coded_refusal_not_a_crash(tmp_path):
    """`resolve_check_backends` raises `FileNotFoundError` for a SKU with no
    SoM preset at all -- a board-level fact, refused once for the whole run
    rather than surfacing as an internal-failure traceback."""
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "source.tflite", "x")
    board_yaml(tmp_path, sku="E1M-DOES-NOT-EXIST",
               models="models:\n  - name: m\n    source: source.tflite\n")
    result = runner.invoke(
        app, ["check", "--project", str(tmp_path), "--sdk-root", str(sdk), "--format", "json"],
    )
    assert result.exit_code == 2
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.check-sku-unresolved"
    assert "E1M-DOES-NOT-EXIST" in doc["issues"][0]["message"]


# --------------------------------------------------------------------------
# a real synthetic metadata tree -- hermetic end-to-end wiring
# --------------------------------------------------------------------------


def test_end_to_end_against_a_synthetic_ethos_u_som(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    write_metadata(sdk / "metadata", "E1M-FAKE", "fake:soc:u55",
                    [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}],
                    ethos_u_variant="u55")
    write_table(sdk / "metadata", "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                supported=["FULLY_CONNECTED"])
    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    shutil.copy(_FIXTURE, proj / "model.tflite")
    board_yaml(proj, sku="E1M-FAKE", models="models:\n  - name: m\n    source: model.tflite\n")

    result = runner.invoke(
        app, ["check", "--project", str(proj), "--sdk-root", str(sdk), "--format", "json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    m = doc["data"]["models"][0]
    assert m["name"] == "m"
    backend = m["backends"][0]
    assert backend["backend"] == "ethos_u"
    assert backend["npuCoverage"] == "full-eligible"
    assert backend["basis"] == "static-screen"


def test_a_model_source_that_cannot_be_read_is_a_per_model_issue_not_a_crash(tmp_path):
    sdk = make_sdk(tmp_path / "sdk")
    write_metadata(sdk / "metadata", "E1M-FAKE", "fake:soc:u55",
                    [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}],
                    ethos_u_variant="u55")
    proj = tmp_path / "proj"
    board_yaml(proj, sku="E1M-FAKE",
               models="models:\n  - name: m\n    source: missing.tflite\n")

    result = runner.invoke(
        app, ["check", "--project", str(proj), "--sdk-root", str(sdk), "--format", "json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1                          # RUNTIME_FAILURE
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "model.check-failed"
    assert "m" in doc["issues"][0]["message"]
    assert doc["data"]["models"] == []


# --------------------------------------------------------------------------
# envelope shape + ok/exit-0 for negative-sounding verdicts
# --------------------------------------------------------------------------


def _run_with_fake_backends(tmp_path, monkeypatch, reports: list[BackendReport]):
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "source.tflite", "x")
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: source.tflite\n")
    monkeypatch.setattr(model_cmd, "resolve_check_backends", lambda sku, **kw: ["ethos_u"])
    monkeypatch.setattr(model_cmd, "check_model_backends", lambda **kw: reports)
    return runner.invoke(
        app,
        ["check", "--project", str(tmp_path), "--sdk-root", str(sdk), "--format", "json"],
        catch_exceptions=False,
    )


def test_the_full_envelope_shape_carries_every_backendreport_field(tmp_path, monkeypatch):
    result = _run_with_fake_backends(tmp_path, monkeypatch, [_fake_report()])
    assert result.exit_code == 0
    doc = envelope(result)
    assert set(doc) == {"command", "ok", "exitCode", "project", "sdk", "data", "issues"}
    backend = doc["data"]["models"][0]["backends"][0]
    assert set(backend) == {
        "backend", "variant", "table", "npuCoverage", "computeOnNpuPctMax",
        "uncostedCpuOpCount", "basis", "confidence", "notes", "ops",
    }
    assert backend["npuCoverage"] == "partial"
    assert backend["computeOnNpuPctMax"] == 96.0
    assert backend["uncostedCpuOpCount"] == 0
    assert backend["basis"] == "static-screen"
    assert backend["confidence"] == "screening"
    op = backend["ops"][0]
    assert set(op) == {"op", "status", "reason", "macs"}


def test_ok_is_true_exit_0_for_undetermined(tmp_path, monkeypatch):
    result = _run_with_fake_backends(
        tmp_path, monkeypatch,
        [_fake_report(npu_coverage="undetermined", compute_on_npu_pct_max=None,
                      table=None, ops=[], notes=["no NPU-ops support table for this "
                                                  "backend/variant -- absence of data, "
                                                  "not evidence of no support."])],
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["data"]["models"][0]["backends"][0]["npuCoverage"] == "undetermined"


def test_ok_is_true_exit_0_for_a_heavily_cpu_model(tmp_path, monkeypatch):
    ops = [OpVerdict(op="RESHAPE", status="cpu-certain", reason="op-not-in-table", macs=10)]
    result = _run_with_fake_backends(
        tmp_path, monkeypatch,
        [_fake_report(npu_coverage="cpu-only", compute_on_npu_pct_max=0.0, ops=ops)],
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["data"]["models"][0]["backends"][0]["npuCoverage"] == "cpu-only"


# --------------------------------------------------------------------------
# text mode: the silent-CPU-fallback sentence + the upgrade hint
# --------------------------------------------------------------------------


def test_text_mode_carries_the_silent_cpu_fallback_sentence_and_the_upgrade_hint(tmp_path, monkeypatch):
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "source.tflite", "x")
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: source.tflite\n")
    monkeypatch.setattr(model_cmd, "resolve_check_backends", lambda sku, **kw: ["ethos_u"])
    monkeypatch.setattr(model_cmd, "check_model_backends", lambda **kw: [_fake_report()])
    result = runner.invoke(
        app, ["check", "--project", str(tmp_path), "--sdk-root", str(sdk)], catch_exceptions=False,
    )
    assert result.exit_code == 0
    text = result.stderr
    assert "Ethos-U55" in text
    assert "partial" in text
    assert "96% of compute" in text
    assert "NORMALIZE" in text and "TOPK" in text
    assert "falls back to the CPU silently rather than failing" in text
    assert "Exact:  pip install alp-tan[model-compile]  &&  tan model check --exact" in text


# --------------------------------------------------------------------------
# --exact: degrades cleanly (and says so) when vela is absent
# --------------------------------------------------------------------------


def test_exact_flag_degrades_cleanly_when_vela_is_absent(tmp_path, monkeypatch):
    sdk = make_sdk(tmp_path / "sdk")
    write_metadata(sdk / "metadata", "E1M-FAKE", "fake:soc:u55",
                    [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}],
                    ethos_u_variant="u55")
    write_table(sdk / "metadata", "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                supported=["FULLY_CONNECTED"])
    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    shutil.copy(_FIXTURE, proj / "model.tflite")
    board_yaml(proj, sku="E1M-FAKE", models="models:\n  - name: m\n    source: model.tflite\n")
    monkeypatch.setattr(shutil, "which", lambda name: None)

    result = runner.invoke(
        app, ["check", "--exact", "--project", str(proj), "--sdk-root", str(sdk), "--format", "json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["data"]["exact"] is True
    backend = doc["data"]["models"][0]["backends"][0]
    assert backend["basis"] == "static-screen"          # degraded, not crashed
    assert any(n.startswith("--exact") and "vela is not on PATH" in n for n in backend["notes"])


def test_exact_flag_off_by_default(tmp_path, monkeypatch):
    result = _run_with_fake_backends(tmp_path, monkeypatch, [_fake_report()])
    assert envelope(result)["data"]["exact"] is False


# --------------------------------------------------------------------------
# the retired "fits" vocabulary must never appear in a static-screen result,
# rendered text included -- not just the engine's own reports
# (extends test_analyze.py's test_the_word_fits_never_appears_in_a_static_
# screen_report, which only checked BackendReport CONTENT, never the CLI's
# rendered json/text output).
# --------------------------------------------------------------------------


def test_the_retired_word_never_leaks_into_the_clis_rendered_static_screen_output(tmp_path, monkeypatch):
    # NOT named with the retired word itself -- pytest's own `tmp_path`
    # fixture derives its directory name from the test node id, and this
    # command's envelope carries several tmp_path-derived paths
    # (`project.root`, `sdk.root`, `data.models[].source`); a test name
    # containing the word would poison its own fixture path and make this
    # assertion spuriously fail (test_analyze.py's own
    # `test_the_word_fits_never_appears_in_a_static_screen_report` documents
    # the identical trap for the engine-level version of this guard).
    reports = [
        _fake_report(),
        _fake_report(backend="drpai", variant=None, table=None, npu_coverage="undetermined",
                     compute_on_npu_pct_max=None, uncosted_cpu_op_count=0,
                     ops=[OpVerdict(op="Conv", status="unknown", reason="format-not-accepted")],
                     notes=["drpai does not ingest 'tflite' source models; no score "
                            "computed. This is not a verdict on the model, only on "
                            "the format/backend pairing."]),
        _fake_report(npu_coverage="full-eligible", compute_on_npu_pct_max=100.0,
                     ops=[OpVerdict(op="CONV_2D", status="npu-eligible",
                                    reason="constraint-unchecked", macs=10)]),
        _fake_report(npu_coverage="cpu-only", compute_on_npu_pct_max=0.0,
                     ops=[OpVerdict(op="RESHAPE", status="cpu-certain",
                                    reason="op-not-in-table", macs=10)]),
    ]
    sdk = make_sdk(tmp_path / "sdk")
    write(tmp_path / "source.tflite", "x")
    board_yaml(tmp_path, models="models:\n  - name: m\n    source: source.tflite\n")
    monkeypatch.setattr(model_cmd, "resolve_check_backends",
                        lambda sku, **kw: ["ethos_u", "drpai", "ethos_u", "ethos_u"])
    monkeypatch.setattr(model_cmd, "check_model_backends", lambda **kw: reports)

    json_result = runner.invoke(
        app, ["check", "--project", str(tmp_path), "--sdk-root", str(sdk), "--format", "json"],
        catch_exceptions=False,
    )
    assert "fits" not in json_result.stdout.lower()

    text_result = runner.invoke(
        app, ["check", "--project", str(tmp_path), "--sdk-root", str(sdk)], catch_exceptions=False,
    )
    assert "fits" not in text_result.stderr.lower()
