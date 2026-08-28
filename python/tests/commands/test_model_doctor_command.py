# SPDX-License-Identifier: Apache-2.0
"""`tan model doctor` -- per-backend NPU-compiler toolchain availability.

Exercises the REAL adapter registry (`tan.model.build._ADAPTERS`), not a
monkeypatched stand-in: the whole point of this command is that
`is_available()` never spawns, and the only way to prove that is to run the
real probes with `subprocess.run`/`Popen` booby-trapped (see
`test_doctor_never_spawns_a_subprocess` below).

`tan model check` -- a fit verdict against a board's declared models and
`metadata/npu_ops/` -- is a separate, not-yet-approved command; nothing here
reads a board.yaml or `metadata/`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import typer
from typer.testing import CliRunner

from tan.commands import model_cmd
from tan.commands.model_cmd import model
from tan.core import model_doctor

app = typer.Typer(add_completion=False)
app.command("model")(model)

runner = CliRunner()


def envelope(result):
    assert result.stdout.count("\n") == 1, result.stdout
    return json.loads(result.stdout)


def _force_all_unavailable(monkeypatch) -> None:
    """No `vela`/`dxcom` on PATH, no DRP-AI/DeepX/vendor-vela env var set --
    the deterministic "fresh host" state every host-independent assertion below
    needs, regardless of what happens to be installed on the machine actually
    running the suite."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.delenv("ALP_DRPAI_TVM_HOME", raising=False)
    monkeypatch.delenv("ALP_DEEPX_SDK_HOME", raising=False)
    monkeypatch.delenv("ALP_VELA_CONFIG", raising=False)


def _rows(doc) -> dict[str, dict]:
    return {row["backend"]: row for row in doc["data"]["backends"]}


def _optional(doc) -> list[dict]:
    return doc["data"]["optional"]


# --------------------------------------------------------------------------
# shape + the fixed four backends, in registry order
# --------------------------------------------------------------------------


def test_json_shape_carries_one_row_per_backend(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["command"] == "model"
    assert doc["ok"] is True
    backends = doc["data"]["backends"]
    assert [row["backend"] for row in backends] == ["cpu", "ethos_u", "drpai", "deepx_dxm1"]
    for row in backends:
        assert set(row) == {"backend", "tool", "available", "version", "reason"}


def test_cpu_is_always_available_with_no_tool_and_no_reason(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["cpu"]
    assert row == {
        "backend": "cpu",
        "tool": None,
        "available": True,
        "version": None,
        "reason": None,
    }


# --------------------------------------------------------------------------
# per-backend availability + exact, actionable reason strings
# --------------------------------------------------------------------------


def test_ethos_u_unavailable_reason_is_exact(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["ethos_u"]
    assert row["tool"] == "vela"
    assert row["available"] is False
    assert row["version"] is None
    assert row["reason"] == "vela not on PATH; pip install alp-tan[model-compile]"


def test_ethos_u_available_when_vela_is_on_path(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["ethos_u"]
    assert row["available"] is True
    assert row["reason"] is None


def test_deepx_dxm1_unavailable_reason_is_exact(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["deepx_dxm1"]
    assert row["tool"] == "dxcom"
    assert row["available"] is False
    assert row["version"] is None
    assert row["reason"] == "dxcom not on PATH; license-gated, Linux-only"


def test_deepx_dxm1_available_when_dxcom_is_on_path(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/dxcom" if name == "dxcom" else None
    )
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["deepx_dxm1"]
    assert row["available"] is True
    assert row["reason"] is None


def test_deepx_dxm1_alp_deepx_sdk_home_alone_stays_unavailable_with_caveat(tmp_path, monkeypatch):
    """`DeepxAdapter.is_available()`'s SECOND arm -- an SDK home directory,
    not just a PATH hit -- flips THAT method true, but `DeepxAdapter.compile()`
    never reads `ALP_DEEPX_SDK_HOME` at all (it always shells the bare `dxcom`
    off PATH): a doctor row gated on `is_available()` alone reported this
    green with no `dxcom` anywhere, and the very next `model build` raised
    `FileNotFoundError: [Errno 2] No such file or directory: 'dxcom'`
    (measured). The row must stay unavailable, with a reason that explains
    the var is set but not what `compile()` reads -- never silently green."""
    _force_all_unavailable(monkeypatch)
    sdk_home = tmp_path / "deepx-sdk"
    sdk_home.mkdir()
    monkeypatch.setenv("ALP_DEEPX_SDK_HOME", str(sdk_home))
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["deepx_dxm1"]
    assert row["available"] is False
    assert row["reason"] is not None
    assert "ALP_DEEPX_SDK_HOME" in row["reason"]
    assert "dxcom" in row["reason"]
    assert str(sdk_home) in row["reason"]


def test_deepx_dxm1_dxcom_on_path_wins_even_with_alp_deepx_sdk_home_set(tmp_path, monkeypatch):
    """The PATH fact is what actually matters -- `dxcom` on PATH flips the row
    available (with no caveat) regardless of whether `ALP_DEEPX_SDK_HOME` is
    also set."""
    _force_all_unavailable(monkeypatch)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/dxcom" if name == "dxcom" else None
    )
    sdk_home = tmp_path / "deepx-sdk"
    sdk_home.mkdir()
    monkeypatch.setenv("ALP_DEEPX_SDK_HOME", str(sdk_home))
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["deepx_dxm1"]
    assert row["available"] is True
    assert row["reason"] is None


def test_drpai_unavailable_names_the_real_env_var(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["drpai"]
    assert row["tool"] == "compile_onnx_model_quant.py"
    assert row["available"] is False
    assert row["version"] is None
    assert "ALP_DRPAI_TVM_HOME" in row["reason"]


def test_drpai_available_when_the_env_var_names_a_built_install(tmp_path, monkeypatch):
    """`ALP_DRPAI_TVM_HOME` naming a real directory is not enough on its own
    -- `DrpaiAdapter.compile()` spawns the vendor tutorial script
    `tutorials/compile_onnx_model_quant.py` under it, so the row only goes
    green once that script is actually there (a BUILT install, not just an
    existing directory). Also proves the no-spawn invariant on this specific
    branch -- MINOR 3: a mutation that inserted a live `subprocess.run` call
    into `adapters/drpai.py::_compiler_version` previously left this file
    green (rc=0), because no test on the "available" branch booby-trapped
    subprocess; this one does."""
    _force_all_unavailable(monkeypatch)
    # `_drpai_status` also gates on `shutil.which("python3")` (the interpreter
    # `DrpaiAdapter.compile()` shells the tutorial script with) -- resolve
    # only that one name so this "everything drpai needs is present" case
    # stays green, without loosening `_force_all_unavailable`'s blanket None
    # for every other tool this file's other tests rely on.
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None
    )
    tvm_home = tmp_path / "drpai-tvm"
    script_dir = tvm_home / "tutorials"
    script_dir.mkdir(parents=True)
    (script_dir / "compile_onnx_model_quant.py").write_text("# stub\n")
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(tvm_home))

    def _boom(*a, **k):
        raise AssertionError("model doctor must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    doc = envelope(result)
    # Assert the envelope actually resolved BEFORE indexing into
    # `data.backends` -- a caught spawn trips `model_cmd.py`'s broad
    # `except Exception`, which swallows the `AssertionError` raised by
    # `_boom` above and re-emits it as an `ok=false` envelope with an EMPTY
    # `data.backends` list. Indexing straight into `_rows(doc)["drpai"]`
    # would then fail with a `KeyError: 'drpai'` that names none of that --
    # this assertion makes the actual trap (a subprocess spawn) the thing
    # that prints, not an unrelated-looking `KeyError`.
    assert result.exit_code == 0, doc
    row = _rows(doc)["drpai"]
    assert row["available"] is True
    assert row["reason"] is None


def test_drpai_env_var_directory_without_tutorial_script_stays_unavailable_with_caveat(
    tmp_path, monkeypatch
):
    """A customer pointing `ALP_DRPAI_TVM_HOME` at an unpacked-but-unbuilt
    tree (the directory exists, but `tutorials/compile_onnx_model_quant.py`
    -- the script `compile()` actually spawns -- does not) must NOT get a
    silently green row; the reason explains the var is set but the tutorial
    script wasn't found under it."""
    _force_all_unavailable(monkeypatch)
    tvm_home = tmp_path / "drpai-tvm"
    tvm_home.mkdir()
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(tvm_home))
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["drpai"]
    assert row["available"] is False
    assert row["reason"] is not None
    assert "ALP_DRPAI_TVM_HOME" in row["reason"]
    assert "compile_onnx_model_quant.py" in row["reason"]
    assert str(tvm_home) in row["reason"]


def test_drpai_built_tree_without_python3_on_path_stays_unavailable_with_caveat(
    tmp_path, monkeypatch
):
    """`DrpaiAdapter.compile()` shells the tutorial script with `python3`
    (`cmd = ["python3", str(script), ...]`, `tan/model/adapters/drpai.py`) --
    a host with a fully BUILT toolchain tree (the env var set, the tutorial
    script present) but no `python3` on PATH must NOT read green either; it
    is the same false-green class as the missing-tutorial-script case above,
    one dependency further out."""
    _force_all_unavailable(monkeypatch)
    tvm_home = tmp_path / "drpai-tvm"
    script_dir = tvm_home / "tutorials"
    script_dir.mkdir(parents=True)
    (script_dir / "compile_onnx_model_quant.py").write_text("# stub\n")
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(tvm_home))
    # `_force_all_unavailable` already stubs `shutil.which` to return None
    # for every name, "python3" included -- no further patching needed to
    # simulate its absence.
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["drpai"]
    assert row["available"] is False
    assert row["reason"] is not None
    assert "python3" in row["reason"]
    assert str(tvm_home) in row["reason"]


def test_drpai_env_var_pointing_at_a_non_directory_stays_unavailable(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    not_a_dir = tmp_path / "not-a-dir.txt"
    not_a_dir.write_text("x")
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(not_a_dir))
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["drpai"]
    assert row["available"] is False


# --------------------------------------------------------------------------
# version: best-effort, and the vela degraded-sentinel rule
# --------------------------------------------------------------------------


def test_ethos_u_degraded_vela_version_string_surfaces_as_none(tmp_path, monkeypatch):
    """`_vela_version()` returns the bare literal `"vela"` when the
    `ethos-u-vela` distribution's metadata is absent -- a DEGRADED answer,
    never surfaced as if it were a real version."""
    _force_all_unavailable(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    monkeypatch.setattr(model_cmd, "_vela_version", lambda: "vela")
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["ethos_u"]
    assert row["available"] is True
    assert row["version"] is None


def test_ethos_u_real_vela_version_is_reported(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    monkeypatch.setattr(model_cmd, "_vela_version", lambda: "vela 3.9.0")
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["ethos_u"]
    assert row["version"] == "vela 3.9.0"


def test_drpai_degraded_version_string_surfaces_as_none(tmp_path, monkeypatch):
    """`_compiler_version()` (`tan.model.adapters.drpai`) falls back to the
    bare literal `"drp-ai_tvm"` when none of `setup/version` / `version` /
    `VERSION` is found under the toolchain checkout -- the identical
    degraded-answer shape `ethos_u`'s `_vela_version()` returns, and this
    backend must be guarded the same way: never surfaced as if it were a
    real version."""
    _force_all_unavailable(monkeypatch)
    # `_drpai_status` also gates on `shutil.which("python3")`; resolve only
    # that name so the "otherwise fully built" tree this test needs reads
    # available (see the same override in
    # test_drpai_available_when_the_env_var_names_a_built_install).
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None
    )
    tvm_home = tmp_path / "drpai-tvm"
    script_dir = tvm_home / "tutorials"
    script_dir.mkdir(parents=True)
    (script_dir / "compile_onnx_model_quant.py").write_text("# stub\n")
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(tvm_home))
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["drpai"]
    assert row["available"] is True
    assert row["version"] is None


def test_drpai_real_version_is_reported(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None
    )
    tvm_home = tmp_path / "drpai-tvm"
    script_dir = tvm_home / "tutorials"
    script_dir.mkdir(parents=True)
    (script_dir / "compile_onnx_model_quant.py").write_text("# stub\n")
    (tvm_home / "VERSION").write_text("1.2.3\n")
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(tvm_home))
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["drpai"]
    assert row["version"] == "drp-ai_tvm 1.2.3"


def test_deepx_dxm1_version_is_never_probed_even_when_available(tmp_path, monkeypatch):
    """DeepxAdapter's only version probe (`_dxcom_version`) spawns
    `dxcom -v` -- doctor must never invoke a compiler, so this backend's
    version stays `None` even when it is available, with no subprocess call
    attempted to find out."""
    _force_all_unavailable(monkeypatch)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/dxcom" if name == "dxcom" else None
    )

    def _boom(*a, **k):
        raise AssertionError("model doctor must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _rows(envelope(result))["deepx_dxm1"]
    assert row["available"] is True
    assert row["version"] is None


# --------------------------------------------------------------------------
# never spawns -- the real adapter registry, booby-trapped
# --------------------------------------------------------------------------


def test_doctor_never_spawns_a_subprocess(tmp_path, monkeypatch):
    """The REAL `_ADAPTERS` registry, no adapter mocked out: every
    `is_available()` today is a `shutil.which`/env-var check, and this proves
    it by making a spawn a test failure rather than trusting the reading."""

    def _boom(*a, **k):
        raise AssertionError("model doctor must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    assert result.exit_code == 0
    assert envelope(result)["ok"] is True


# --------------------------------------------------------------------------
# a missing/broken SDK root is a reason, never a crash
# --------------------------------------------------------------------------


def test_unresolvable_sdk_root_yields_a_reported_reason_not_a_traceback(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    typo = tmp_path / "alp-sdk-typo"
    typo.mkdir()
    result = runner.invoke(
        app,
        ["doctor", "--project", str(tmp_path), "--sdk-root", str(typo), "--format", "json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["issues"][0]["code"] == "model.doctor-sdk-unresolved"
    assert doc["issues"][0]["severity"] == "warning"
    assert str(typo) in doc["issues"][0]["message"]
    # the backend rows are unaffected by the unresolved SDK root
    assert len(doc["data"]["backends"]) == 4


def test_no_sdk_at_all_still_reports_full_backend_rows(tmp_path, monkeypatch):
    """No `--sdk-root`, no project pin, no global default, no sibling
    checkout near `tmp_path` -- doctor still answers in full; an alp-sdk
    checkout is not a precondition for a toolchain-availability report."""
    _force_all_unavailable(monkeypatch)
    result = runner.invoke(
        app,
        ["doctor", "--project", str(tmp_path), "--format", "json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert "sdk" not in doc
    assert [row["backend"] for row in doc["data"]["backends"]] == [
        "cpu", "ethos_u", "drpai", "deepx_dxm1",
    ]
    codes = [i["code"] for i in doc["issues"]]
    assert "model.doctor-sdk-unresolved" in codes


def test_the_internal_failure_catch_all_still_covers_doctor(tmp_path, monkeypatch):
    """The same outer `except Exception` `model build` already relies on --
    pinned here for `doctor` too, so a genuinely unexpected failure is a coded
    issue, never a bare traceback out of the CLI."""

    def boom(*args, **kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(model_cmd, "_run_doctor", boom)
    result = runner.invoke(app, ["doctor", "--project", str(tmp_path), "--format", "json"])
    assert result.exit_code == 5
    doc = envelope(result)
    assert doc["issues"][-1]["code"] == "model.internal-failure"
    assert "model doctor failed unexpectedly" in doc["issues"][-1]["message"]
    assert doc["data"] == {"schemaVersion": model_cmd.DOCTOR_DATA_SCHEMA_VERSION,
                           "backends": [], "optional": []}


# --------------------------------------------------------------------------
# the OPTIONAL vendor vela `.ini` (`ALP_VELA_CONFIG`) -- reported, never as a
# fault (alp-sdk #1470 Task 5)
# --------------------------------------------------------------------------


def test_the_vendor_vela_config_is_an_optional_row_not_a_backend_row(tmp_path, monkeypatch):
    """It rides in `data.optional[]`, in the SAME five-key shape the backend
    rows use, and NOT in `data.backends[]`.

    Kept apart because `available: false` means two different things in the two
    lists: in `backends[]` it means tan cannot compile for that backend at all,
    here it means the backend works and a licensed-only enhancement is not
    installed. A consumer counting `backends[]` (or keying it by backend name)
    must not suddenly see a second `ethos_u`."""
    _force_all_unavailable(monkeypatch)
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    doc = envelope(result)
    assert [row["backend"] for row in doc["data"]["backends"]] == [
        "cpu", "ethos_u", "drpai", "deepx_dxm1",
    ]
    optional = _optional(doc)
    assert len(optional) == 1
    assert set(optional[0]) == {"backend", "tool", "available", "version", "reason"}
    assert optional[0]["backend"] == "ethos_u"
    assert optional[0]["tool"] == "ALP_VELA_CONFIG"
    assert optional[0]["version"] is None


def test_an_absent_vendor_vela_config_reads_as_optional_never_as_a_fault(tmp_path, monkeypatch):
    """The unlicensed customer's case, which is the COMMON one: they are not
    broken. Without the `.ini` vela uses Arm's own built-in system config,
    which is exactly what the arena/SRAM figures tan reports describe -- so the
    reason has to say the absence is not a fault AND say what setting it would
    buy, or `doctor` turns a licensing boundary into a bug report."""
    _force_all_unavailable(monkeypatch)
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _optional(envelope(result))[0]
    assert row["available"] is False
    assert row["reason"].startswith("OPTIONAL, not a fault: ALP_VELA_CONFIG is not set")
    assert "Arm's built-in system config" in row["reason"]
    assert "are correct" in row["reason"]
    # ... and it must NOT fall through to the ethos_u BACKEND reason, which is
    # about `vela` not being on PATH and would be actionable nonsense here.
    assert "pip install" not in row["reason"]
    # An optional prerequisite never changes the run's verdict.
    assert envelope(result)["ok"] is True
    assert result.exit_code == 0


def test_a_present_vendor_vela_config_reports_available_with_no_reason(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    ini = tmp_path / "ensemble_vela.ini"
    ini.write_text("[System_Config.Ethos_U85_SRAM_Only]\n", encoding="utf-8")
    monkeypatch.setenv("ALP_VELA_CONFIG", str(ini))
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _optional(envelope(result))[0]
    assert row["available"] is True
    assert row["reason"] is None


def test_a_vendor_vela_config_pointing_at_nothing_says_which_path(tmp_path, monkeypatch):
    """Set-but-wrong is its own actionable state, not the generic "not set"
    one: the value is ignored (vela is never handed a path it cannot open), and
    the reason echoes the path so a customer can see the typo."""
    _force_all_unavailable(monkeypatch)
    missing = tmp_path / "not-here.ini"
    monkeypatch.setenv("ALP_VELA_CONFIG", str(missing))
    result = runner.invoke(app, ["doctor", "--format", "json"], catch_exceptions=False)
    row = _optional(envelope(result))[0]
    assert row["available"] is False
    assert row["reason"].startswith("OPTIONAL, not a fault:")
    assert str(missing) in row["reason"]
    assert "does not name a readable file" in row["reason"]


def test_text_mode_never_calls_the_optional_row_unavailable(tmp_path, monkeypatch):
    """The word a scrollback leads with decides how this reads. `unavailable`
    is what an actually-broken backend gets one line above; this row must not
    borrow it."""
    _force_all_unavailable(monkeypatch)
    result = runner.invoke(
        app, ["doctor", "--project", str(tmp_path)], catch_exceptions=False
    )
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    optional_lines = [ln for ln in lines if "optional" in ln]
    assert optional_lines == [
        "ethos_u: optional (tool=ALP_VELA_CONFIG) not in use -- OPTIONAL, not a fault: "
        "ALP_VELA_CONFIG is not set, so vela uses Arm's built-in system config -- the "
        "arena/SRAM figures tan reports describe that model and are correct. A licensed "
        "customer may set it to their vendor vela config .ini (e.g. Alif's "
        "ensemble_vela.ini) for the vendor-tuned profile"
    ]


# --------------------------------------------------------------------------
# every backend unavailable is still a healthy exit
# --------------------------------------------------------------------------


def test_every_backend_unavailable_is_still_ok_true_exit_0(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    result = runner.invoke(
        app,
        ["doctor", "--project", str(tmp_path), "--format", "json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    non_cpu = [row for row in doc["data"]["backends"] if row["backend"] != "cpu"]
    assert non_cpu and all(row["available"] is False for row in non_cpu)
    assert all(row["reason"] for row in non_cpu)


# --------------------------------------------------------------------------
# text mode
# --------------------------------------------------------------------------


def test_text_mode_reports_one_readable_line_per_backend(tmp_path, monkeypatch):
    _force_all_unavailable(monkeypatch)
    result = runner.invoke(
        app, ["doctor", "--project", str(tmp_path)], catch_exceptions=False
    )
    assert result.exit_code == 0
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert any(ln.startswith("cpu: available") for ln in lines)
    assert any(
        ln.startswith("ethos_u: unavailable") and "pip install alp-tan[model-compile]" in ln
        for ln in lines
    )
    assert any(
        ln.startswith("deepx_dxm1: unavailable") and "license-gated" in ln for ln in lines
    )
    assert any(ln.startswith("drpai: unavailable") and "ALP_DRPAI_TVM_HOME" in ln for ln in lines)


def test_text_mode_drops_the_reason_clause_entirely_when_reason_is_none(tmp_path, monkeypatch):
    """NIT: a backend with no `_UNAVAILABLE_REASONS` entry and no doctor-side
    caveat carries `reason: None` -- the text line must drop the `-- ...`
    clause rather than rendering the Python literal `None` into it."""
    _force_all_unavailable(monkeypatch)
    monkeypatch.setattr(
        model_cmd,
        "backend_row",
        lambda backend, **kw: model_doctor.BackendRow(
            backend=backend,
            tool=model_doctor.BACKEND_TOOLS.get(backend),
            available=False,
            version=None,
            reason=None,
        ),
    )
    result = runner.invoke(
        app, ["doctor", "--project", str(tmp_path)], catch_exceptions=False
    )
    assert result.exit_code == 0
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    # BACKEND rows only: `ethos_u` also owns an OPTIONAL row (the vendor vela
    # `.ini`, `data.optional[]`), whose line is prefixed with the same backend
    # name and is not what this test is about. `backend_row` is what was
    # monkeypatched above; `optional_row` is a different builder and keeps its
    # own reason.
    ethos_u_lines = [ln for ln in lines
                     if ln.startswith("ethos_u:") and "optional" not in ln]
    assert ethos_u_lines == ["ethos_u: unavailable (tool=vela)"]
    assert "None" not in ethos_u_lines[0]


# --------------------------------------------------------------------------
# `doctor` joins `build`/`check` in the unknown-subcommand help text
# --------------------------------------------------------------------------


def test_unknown_subcommand_lists_build_doctor_and_check(tmp_path):
    result = runner.invoke(app, ["bogus", "--format", "json"], catch_exceptions=False)
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["message"].endswith("Available: build, doctor, check.")
