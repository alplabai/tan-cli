# SPDX-License-Identifier: Apache-2.0
"""`tan validate`'s default engine is in-process (tan-cli#270).

Hermetic: nothing here needs an alp-sdk checkout except the one test marked
SDK-gated, and the byte-level agreement with the SDK's own script is
`tests/parity/test_board_validator_parity.py`'s job. What these pin is the
WIRING -- that the default run spawns nothing, that the opt-in env var still
reaches the spawn path, that the in-process verdict goes through the same
status-to-outcome map and envelope as a spawned one -- plus the ported
validator's own passes against a miniature `metadata/` tree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands import validate_cmd
from tan.core import board_validator_run
from tan.core.board_diagnostic import Diagnostic, render
from tan.core.board_validator import PlannerFacts, validate_board_yaml  # noqa: F401
from tan.core.board_validator_compat import _soc_has_kind, split_silicon_ref
from tan.core.board_yaml_pos import load_with_positions, node_position
from tan.exit_codes import ExitCode
from tests.conftest import sdk_root

runner = CliRunner()
SDK: Path | None = sdk_root()

_BOARD = "som:\n  sku: E1M-AEN701\npreset: e1m-evk\ncores:\n  m55_hp:\n    app: ./src\n"


def _stand_in_sdk(root: Path) -> Path:
    """The loader marker only; no validator script at all -- an engine that tried
    to spawn it would fail loudly."""
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "alp_project.py").write_text("", encoding="utf-8")
    return root


def _project(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text(_BOARD, encoding="utf-8")
    monkeypatch.chdir(project)
    return project


def _no_spawn(monkeypatch) -> list[object]:
    spawned: list[object] = []

    def refuse(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("the default engine must not spawn a process")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    return spawned


# ───────────────────────────── the wiring ─────────────────────────────


def test_the_default_engine_spawns_nothing_and_goes_through_the_status_map(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(validate_cmd.VALIDATE_ENGINE_ENV, raising=False)
    _project(tmp_path, monkeypatch)
    sdk = _stand_in_sdk(tmp_path / "alp-sdk")
    spawned = _no_spawn(monkeypatch)
    calls: list[tuple[str, Path]] = []

    def fake_run(board_path, sdk_root_):
        calls.append((board_path, sdk_root_))
        return board_validator_run.ValidatorRun(
            1,
            "",
            "error[ALP-B005]: SoM SKU 'E1M-X' does not resolve to a known module\n"
            "  --> ./board.yaml:2:8\n   |\n 2 |   sku: E1M-X\n   |        ^^^^^\n"
            "   = hint: did you mean 'E1M-AEN701'?\n   = see: docs/diagnostics/ALP-B005.md\n",
        )

    monkeypatch.setattr(board_validator_run, "run_board_validator", fake_run)
    result = runner.invoke(app, ["validate", "--sdk-root", str(sdk), "--format", "json"])

    assert spawned == []
    assert len(calls) == 1
    assert Path(calls[0][1]).resolve() == sdk.resolve()
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["outcome"] == "schema-violation"
    assert [i["code"] for i in envelope["issues"]] == ["validate.schema-violation"]
    assert envelope["issues"][0]["message"].startswith("ALP-B005: SoM SKU 'E1M-X'")
    # Nothing was spawned, so there is no command line to report; the validator's
    # status still is (the envelope shape did not change).
    assert envelope["data"]["commandLine"] == ""
    assert envelope["data"]["validatorExitStatus"] == 1


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(0, "clean"), (3, "hardware-revision"), (4, "hardware-revision-unknown"),
     (5, "hardware-revision-not-buildable")],
)
def test_every_script_exit_status_keeps_its_outcome(tmp_path, monkeypatch, status, outcome):
    monkeypatch.delenv(validate_cmd.VALIDATE_ENGINE_ENV, raising=False)
    _project(tmp_path, monkeypatch)
    sdk = _stand_in_sdk(tmp_path / "alp-sdk")
    _no_spawn(monkeypatch)
    monkeypatch.setattr(
        board_validator_run,
        "run_board_validator",
        lambda *_a: board_validator_run.ValidatorRun(
            status, "", "" if status == 0 else "FAIL sdk-compat: hw_rev 'zz' is not known\n"
        ),
    )
    result = runner.invoke(app, ["validate", "--sdk-root", str(sdk), "--format", "json"])
    envelope = json.loads(result.output)
    assert envelope["data"]["outcome"] == outcome
    assert envelope["data"]["validatorExitStatus"] == status
    assert result.exit_code == int(ExitCode.SUCCESS if status == 0 else ExitCode.VALIDATION_FAILURE)


def test_a_crash_inside_the_engine_is_failed_not_a_board_verdict(tmp_path, monkeypatch):
    monkeypatch.delenv(validate_cmd.VALIDATE_ENGINE_ENV, raising=False)
    _project(tmp_path, monkeypatch)
    sdk = _stand_in_sdk(tmp_path / "alp-sdk")
    _no_spawn(monkeypatch)

    def boom(_board, _sdk):
        raise KeyError("metadata")

    # `run_board_validator` itself never raises (it returns the traceback shape);
    # a hard fault past it is the command's own backstop.
    monkeypatch.setattr(board_validator_run, "run_board_validator", boom)
    result = runner.invoke(app, ["validate", "--sdk-root", str(sdk), "--format", "json"])
    envelope = json.loads(result.output)
    assert result.exit_code == int(ExitCode.INTERNAL_FAILURE)
    assert envelope["issues"][0]["code"] == "validate.internal-failure"


def test_run_board_validator_turns_an_exception_into_the_crash_shape(tmp_path):
    """An unreadable (non-UTF-8) board is the script's uncaught-exception exit 1;
    the shape `analyze_validator_output` reads as a crash, not as a verdict."""
    board = tmp_path / "board.yaml"
    board.write_bytes(b"som: {sku: \xff}\n")
    run = board_validator_run.run_board_validator(str(board), tmp_path)
    assert run.status == 1
    assert run.stderr.startswith("Traceback (most recent call last):")
    assert run.stderr.strip().splitlines()[-1].startswith("UnicodeDecodeError")
    result = validate_cmd.analyze_validator_output(run.status, run.stderr)
    assert result.outcome == "failed"


def test_a_missing_board_is_the_scripts_own_failure_line(tmp_path):
    run = board_validator_run.run_board_validator(str(tmp_path / "nope.yaml"), tmp_path)
    assert (run.status, run.stdout) == (1, "")
    assert run.stderr == f"FAIL {tmp_path / 'nope.yaml'}: file not found\n"


def test_the_opt_in_env_var_still_reaches_the_spawn_path(tmp_path, monkeypatch):
    monkeypatch.setenv(validate_cmd.VALIDATE_ENGINE_ENV, "subprocess")
    _project(tmp_path, monkeypatch)
    sdk = _stand_in_sdk(tmp_path / "alp-sdk")
    (sdk / "scripts" / "validate_board_yaml.py").write_text(
        "import sys\nsys.stdout.write('ok\\n')\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        board_validator_run,
        "run_board_validator",
        lambda *_a: pytest.fail("the opt-in subprocess engine must not run the in-process one"),
    )
    import sys as _sys

    monkeypatch.setattr(
        validate_cmd, "_planner_python_resolution", lambda *_a, **_k: (_sys.executable, True)
    )
    result = runner.invoke(app, ["validate", "--sdk-root", str(sdk), "--format", "json"])
    envelope = json.loads(result.output)
    assert envelope["data"]["outcome"] == "clean"
    assert envelope["data"]["commandLine"].endswith("--input ./board.yaml")


def test_offline_is_untouched_by_the_engine_choice(tmp_path, monkeypatch):
    monkeypatch.delenv(validate_cmd.VALIDATE_ENGINE_ENV, raising=False)
    _project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        board_validator_run,
        "run_board_validator",
        lambda *_a: pytest.fail("--offline never reaches an SDK validator"),
    )
    result = runner.invoke(app, ["validate", "--offline", "--format", "json"])
    assert result.exit_code == int(ExitCode.SUCCESS), result.output


@pytest.mark.skipif(SDK is None, reason="set ALP_SDK_ROOT to a real alp-sdk checkout")
def test_a_real_sdk_backed_board_passes_in_process(tmp_path, monkeypatch):
    """The in-process twin of `test_validate_command.py`'s real-SDK test: same
    `tan init` board, same checkout, default engine, no process spawned."""
    monkeypatch.delenv(validate_cmd.VALIDATE_ENGINE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    init = runner.invoke(app, ["init", "--name", "my-app", "--format", "json"])
    assert init.exit_code == 0, init.output
    monkeypatch.chdir(tmp_path / "my-app")
    spawned = _no_spawn(monkeypatch)
    result = runner.invoke(app, ["validate", "--sdk-root", str(SDK), "--format", "json"])
    assert spawned == []
    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["outcome"] == "clean"
    assert envelope["issues"] == []
    assert envelope["data"]["commandLine"] == ""
    assert envelope["data"]["validatorExitStatus"] == 0
    assert envelope["sdk"]["sourceTier"] == "sdkRootFlag"


# ──────────────── the ported passes, against a miniature metadata tree ────────────────

_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["som", "cores"],
    "additionalProperties": False,
    "properties": {
        "som": {
            "type": "object",
            "required": ["sku"],
            "properties": {"sku": {"type": "string"}},
        },
        "preset": {"type": "string"},
        "cores": {"type": "object"},
        "chips": {"type": "array", "items": {"type": "string"}},
        "mode": {"enum": ["fast", "slow"]},
    },
}


@pytest.fixture
def meta(tmp_path) -> Path:
    root = tmp_path / "metadata"
    (root / "schemas").mkdir(parents=True)
    (root / "schemas" / "board.schema.json").write_text(json.dumps(_SCHEMA), encoding="utf-8")
    (root / "e1m_modules").mkdir()
    (root / "e1m_modules" / "E1M-TEST1.yaml").write_text(
        "family: testfam\nsilicon: acme:fam:chip1\n", encoding="utf-8"
    )
    (root / "boards").mkdir()
    (root / "boards" / "p1.yaml").write_text("hosts_som_families: [testfam]\n", encoding="utf-8")
    (root / "boards" / "p2.yaml").write_text("hosts_som_families: [otherfam]\n", encoding="utf-8")
    (root / "chips").mkdir()
    (root / "chips" / "realchip.yaml").write_text("driver_status: complete\n", encoding="utf-8")
    (root / "chips" / "plannedchip.yaml").write_text("driver_status: planned\n", encoding="utf-8")
    (root / "camera_modules").mkdir()
    (root / "socs" / "acme" / "fam").mkdir(parents=True)
    (root / "socs" / "acme" / "fam" / "chip1.json").write_text(
        json.dumps({"peripherals": {"i2c": 2, "can_fd": 1, "timer_0": 4}}), encoding="utf-8"
    )
    return root


def _diagnose(meta: Path, tmp_path: Path, text: str, *, block_slugs=frozenset()):
    board = tmp_path / "board.yaml"
    board.write_text(text, encoding="utf-8")
    collector = validate_board_yaml(
        board,
        metadata_root=meta,
        facts=PlannerFacts(repo_root=meta.parent, block_slugs=block_slugs),
    )
    return board, [(d.code, d.severity, d.line, d.col, d.span) for d in collector], list(collector)


def test_a_valid_board_yields_no_diagnostics(meta, tmp_path):
    _b, found, _all = _diagnose(
        meta, tmp_path, "som: {sku: E1M-TEST1}\npreset: p1\ncores:\n  c0: {peripherals: [i2c]}\n"
    )
    assert found == []


def test_schema_errors_get_their_alp_codes_and_positions(meta, tmp_path):
    text = (
        "som: {sku: E1M-TEST1}\n"
        "cores: {}\n"
        "mode: medium\n"
        "bogus: 1\n"
    )
    _b, found, _all = _diagnose(meta, tmp_path, text)
    codes = [c for c, *_ in found]
    assert "ALP-B003" in codes  # enum
    assert "ALP-B002" in codes  # unknown key
    unknown = next(f for f in found if f[0] == "ALP-B002")
    assert unknown[2:4] == (4, 1)  # the offending key's own line/column
    enum = next(f for f in found if f[0] == "ALP-B003")
    assert enum[2] == 3


def test_missing_required_and_wrong_type(meta, tmp_path):
    _b, found, _all = _diagnose(meta, tmp_path, "som: {sku: E1M-TEST1}\n")
    assert [c for c, *_ in found] == ["ALP-B001"]
    _b, found, _all = _diagnose(meta, tmp_path, "som: 7\ncores: {}\n")
    assert [c for c, *_ in found] == ["ALP-B004"]


def test_yaml_parse_error_is_b000(meta, tmp_path):
    _b, found, _all = _diagnose(meta, tmp_path, "som: [unclosed\n")
    assert [(c, s) for c, s, *_ in found] == [("ALP-B000", "error")]


def test_duplicate_keys_are_a_parse_error(meta, tmp_path):
    _b, found, all_ = _diagnose(meta, tmp_path, "som: {sku: a}\nsom: {sku: b}\ncores: {}\n")
    assert [c for c, *_ in found] == ["ALP-B000"]
    assert "duplicate key 'som'" in all_[0].message


def test_xref_unknown_sku_preset_and_family_mismatch(meta, tmp_path):
    _b, found, all_ = _diagnose(meta, tmp_path, "som: {sku: E1M-TESTO}\npreset: p9\ncores: {}\n")
    assert [c for c, *_ in found] == ["ALP-B005", "ALP-B006"]
    assert all_[0].hint == "did you mean 'E1M-TEST1'?"
    _b, found, _all = _diagnose(meta, tmp_path, "som: {sku: E1M-TEST1}\npreset: p2\ncores: {}\n")
    assert [c for c, *_ in found] == ["ALP-B007"]


def test_chips_must_name_a_manifest_or_a_block_helper(meta, tmp_path):
    text = "som: {sku: E1M-TEST1}\ncores: {}\nchips: [realchip, realshp, plannedchip, button_led]\n"
    _b, found, all_ = _diagnose(meta, tmp_path, text, block_slugs=frozenset({"button_led"}))
    assert [c for c, *_ in found] == ["ALP-B008", "ALP-B008"]
    assert all_[0].hint == "did you mean 'realchip'?"
    assert "driver_status: planned" in all_[1].message
    # Without the helper set, the block helper is just another unknown chip --
    # the SDK file's behaviour when its lazy planner import failed.
    _b, found, _all = _diagnose(meta, tmp_path, text)
    assert [c for c, *_ in found].count("ALP-B008") == 3


def test_a_peripheral_the_soc_lacks_is_a_warning_not_an_error(meta, tmp_path):
    text = "som: {sku: E1M-TEST1}\ncores:\n  c0:\n    peripherals: [i2c, can, counter, spi]\n"
    _b, found, all_ = _diagnose(meta, tmp_path, text)
    assert [(c, s) for c, s, *_ in found] == [("ALP-B010", "warning")]
    assert "'spi'" in all_[0].message
    assert not any(d.severity == "error" for d in all_)


def test_soc_kind_matching_rules():
    caps = {"i2c": 2, "i2c_lp": 0, "can_fd": 1, "timer_0": 4, "pwm": 0}
    assert _soc_has_kind(caps, "i2c")
    assert _soc_has_kind(caps, "can")  # variant suffix
    assert _soc_has_kind(caps, "counter")  # alias onto timer_*
    assert _soc_has_kind(caps, "sensor")  # unconditional
    assert not _soc_has_kind(caps, "spi")
    assert split_silicon_ref("a:b:c") == ("a", "b", "c")
    assert split_silicon_ref("a:b") is None and split_silicon_ref(None) is None


def test_positions_and_the_renderer(tmp_path):
    text = "a: 1\nb:\n  c: [1, 2]\n  d: x\n"
    data = load_with_positions(text, source="t.yaml")
    assert node_position(data, "b", target="key") == (2, 1)
    assert node_position(data["b"], "d", target="value") == (4, 6)
    assert node_position(data, "missing") == (1, 1)
    with pytest.raises(ValueError, match="not a mapping"):
        load_with_positions("- x\n", source="t.yaml")
    block = render(
        Diagnostic("error", Path("t.yaml"), 4, 6, 1, "ALP-B003", "bad", hint="try x"),
        source_text=text,
        color=False,
    )
    assert block == (
        "error[ALP-B003]: bad\n"
        "  --> t.yaml:4:6\n"
        "   |\n"
        " 4 |   d: x\n"
        "   |      ^\n"
        "   = hint: try x\n"
        "   = see: docs/diagnostics/ALP-B003.md\n"
    )


@pytest.mark.skipif(SDK is None, reason="set ALP_SDK_ROOT to a real alp-sdk checkout")
def test_the_run_module_always_hands_the_validator_real_planner_callables(monkeypatch):
    """`PlannerFacts()` with no callables silently skips the camera-owner checks
    and the block-slug allowance (the SDK file's failed-import degradation).
    `board_validator_run` must never take that path."""
    seen: list[PlannerFacts] = []
    real = board_validator_run.validate_board_yaml

    def spy(path, *, metadata_root, facts):
        seen.append(facts)
        return real(path, metadata_root=metadata_root, facts=facts)

    monkeypatch.setattr(board_validator_run, "validate_board_yaml", spy)
    boards = sorted((SDK / "examples").rglob("board.yaml"))
    assert boards
    board_validator_run.run_board_validator(str(boards[0]), SDK)
    assert len(seen) == 1
    facts = seen[0]
    assert callable(facts.plan_cameras) and callable(facts.resolve_cores)
    assert facts.block_slugs and "button_led" in facts.block_slugs
    assert facts.repo_root == SDK


def test_an_engine_crash_is_reported_as_tans_gap_not_a_board_defect(tmp_path, monkeypatch):
    monkeypatch.delenv(validate_cmd.VALIDATE_ENGINE_ENV, raising=False)
    _project(tmp_path, monkeypatch)
    sdk = _stand_in_sdk(tmp_path / "alp-sdk")
    (sdk / "metadata").mkdir()
    (sdk / "metadata" / "sdk_version.yaml").write_text("version: 9.9.9\n", encoding="utf-8")
    _no_spawn(monkeypatch)
    monkeypatch.setattr(
        board_validator_run,
        "run_board_validator",
        lambda *_a: board_validator_run.ValidatorRun(
            1, "", "Traceback (most recent call last):\nKeyError: 'new_field'\n"
        ),
    )
    result = runner.invoke(app, ["validate", "--sdk-root", str(sdk), "--format", "json"])
    envelope = json.loads(result.output)
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE)
    assert envelope["data"]["outcome"] == "failed"
    (issue,) = envelope["issues"]
    assert issue["code"] == "validate.failed"
    assert "could not read this SDK" in issue["message"]
    assert "v9.9.9" in issue["message"] and "a5a137c7" in issue["message"]
    assert "TAN_VALIDATE_ENGINE=subprocess" in issue["message"]
    assert "KeyError: 'new_field'" in issue["message"]
