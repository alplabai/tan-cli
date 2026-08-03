# SPDX-License-Identifier: Apache-2.0
"""`tan pinmux` -- CLI surface tests.

`pinmux` is not registered in `tan.cli.app` by this change (the shared
`cli.py` registration point is owned by the orchestrator wiring commands in
parallel), so these tests mount the command on a throwaway `typer.Typer()`
rather than importing `tan.cli.app`, matching
`test_faultdecode_command.py`/`test_kconfig_command.py`'s own note for the
same situation.

Every wire-shape assertion below (issue codes, `data`/`sdk` envelope shape,
the `--family` overriding `--sku` without evaluating it, the "no
`project-pin-unresolved` warning" behaviour) was measured directly against
`target/debug/tan.exe` (tan 0.4.1-dev), including a full byte-for-byte diff of
the real `metadata/pinmux/aen.yaml` (96 pads) table against a live alp-sdk
checkout where one was reachable. The `metadata/pinmux/v2n.yaml` table in the
real checkout is (at the time of writing) entirely `e1m_pad: "TBD"` rows, so
`pinmux.table-empty` is exercised here with a small SYNTHETIC table rather
than depending on that fact staying true.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands.pinmux_cmd import (
    PinmuxParseError,
    parse_pinmux_table_checked,
    pinmux_family_for_sku,
)
from tan.commands.pinmux_cmd import pinmux as pinmux_command

app = typer.Typer()
app.command("pinmux")(pinmux_command)

runner = CliRunner()

#: `target/{release,debug}/tan(.exe)` next to this checkout -- the same
#: discovery `tests/parity/oracle.py`'s `rust_binary()` uses, kept
#: independent here rather than imported so this file's only non-stdlib
#: dependency stays `tan.commands.pinmux_cmd` (matching every other test file
#: under `tests/commands/`). `TAN_RUST_BINARY` overrides, same env var.
_EXE = ".exe" if sys.platform == "win32" else ""
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _oracle_binary() -> str | None:
    override = os.environ.get("TAN_RUST_BINARY")
    if override:
        return override
    for profile in ("release", "debug"):
        candidate = _REPO_ROOT / "target" / profile / f"tan{_EXE}"
        if candidate.exists():
            return str(candidate)
    return None


_ORACLE = _oracle_binary()
_ORACLE_REQUIRED = pytest.mark.skipif(
    _ORACLE is None,
    reason="needs a built Rust tan (cargo build --bin tan) to measure the divergence",
)


def _run_oracle(argv: list[str], cwd: Path) -> tuple[int, dict]:
    proc = subprocess.run(
        [_ORACLE, *argv], capture_output=True, text=True, encoding="utf-8", cwd=cwd
    )
    return proc.returncode, json.loads(proc.stdout)

_SAMPLE_TABLE = """\
schemaVersion: pinmux-capability-v1
family: aen
display_name: "E1M-AEN (Alif Ensemble)"
pads:
  - { e1m_pad: "A3", e1m_function: "PWM6", owner: "alif", silicon_peripheral: "UT3_T1_C", silicon_pad: "P10_7" }
  - { e1m_pad: "A15", e1m_function: "ANA_S0", owner: "alif", silicon_peripheral: "", silicon_pad: "P0_0" }
"""


def _sdk_root(tmp_path: Path, pinmux_yaml: dict[str, str]) -> Path:
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    pinmux_dir = sdk / "metadata" / "pinmux"
    pinmux_dir.mkdir(parents=True)
    for family, text in pinmux_yaml.items():
        (pinmux_dir / f"{family}.yaml").write_text(text, encoding="utf-8")
    return sdk


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_help_lists_sku_and_family() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--sku" in result.output
    assert "--family" in result.output


def test_no_target_is_a_warning_at_exit_zero(tmp_path: Path) -> None:
    # `--sdk-root` given so ONLY the no-target branch fires -- an unresolved
    # SDK independently pushes its own `pinmux.sdk-root-unresolved` issue
    # (measured against the oracle: neither branch suppresses the other), and
    # this test pins the no-target case in isolation.
    sdk = _sdk_root(tmp_path, {})
    proj = _project(tmp_path)
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True
    assert envelope["data"]["family"] is None
    assert envelope["data"]["pads"] == []
    assert envelope["issues"] == [
        {
            "code": "pinmux.no-target",
            "severity": "warning",
            "message": "Provide --sku <sku> or --family <family>.",
        }
    ]


def test_no_target_and_unresolved_sdk_both_report(tmp_path: Path) -> None:
    """Measured against the oracle: the two independent guards do not
    suppress each other -- both `pinmux.no-target` and
    `pinmux.sdk-root-unresolved` appear when neither a target nor an SDK
    resolves."""
    proj = _project(tmp_path)
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert "sdk" not in envelope  # absent, not null -- nothing resolved
    codes = {issue["code"] for issue in envelope["issues"]}
    assert codes == {"pinmux.no-target", "pinmux.sdk-root-unresolved"}


def test_unresolved_sdk_root_is_a_warning_family_still_resolves(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    result = runner.invoke(
        app, ["--project", str(proj), "--sku", "E1M-AEN801", "--format", "json"]
    )
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert "sdk" not in envelope
    assert envelope["data"]["sdkRoot"] is None
    assert envelope["data"]["sku"] == "E1M-AEN801"
    assert envelope["data"]["family"] == "aen"  # resolved from the SKU regardless
    assert envelope["issues"][0]["code"] == "pinmux.sdk-root-unresolved"


def test_unknown_sku_is_a_warning(tmp_path: Path) -> None:
    sdk = _sdk_root(tmp_path, {})
    proj = _project(tmp_path)
    result = runner.invoke(
        app,
        [
            "--project", str(proj),
            "--sku", "E1M-BOGUS",
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["data"]["family"] is None
    assert envelope["issues"][0]["code"] == "pinmux.unknown-sku"


def test_family_overrides_sku_without_evaluating_it(tmp_path: Path) -> None:
    """Measured against the oracle: `--sku E1M-BOGUS --family aen` reports
    family "aen" with NO `pinmux.unknown-sku` issue -- `--family` short-
    circuits before the SKU is ever looked up."""
    sdk = _sdk_root(tmp_path, {"aen": _SAMPLE_TABLE})
    proj = _project(tmp_path)
    result = runner.invoke(
        app,
        [
            "--project", str(proj),
            "--sku", "E1M-BOGUS",
            "--family", "aen",
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["data"]["sku"] == "E1M-BOGUS"  # still echoed
    assert envelope["data"]["family"] == "aen"
    assert envelope["issues"] == []


def test_table_not_found_is_a_warning(tmp_path: Path) -> None:
    sdk = _sdk_root(tmp_path, {})
    proj = _project(tmp_path)
    result = runner.invoke(
        app,
        [
            "--project", str(proj),
            "--family", "bogus",
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "pinmux.table-not-found"


def test_real_table_resolves_family_display_name_and_pads(tmp_path: Path) -> None:
    sdk = _sdk_root(tmp_path, {"aen": _SAMPLE_TABLE})
    proj = _project(tmp_path)
    result = runner.invoke(
        app,
        [
            "--project", str(proj),
            "--sku", "E1M-AEN801",
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["sdk"] == {"root": str(sdk).replace("\\", "/"), "sourceTier": "sdkRootFlag"}
    assert envelope["data"]["displayName"] == "E1M-AEN (Alif Ensemble)"
    assert envelope["data"]["pads"] == [
        {
            "e1mPad": "A3",
            "e1mFunction": "PWM6",
            "owner": "alif",
            "siliconPeripheral": "UT3_T1_C",
            "siliconPad": "P10_7",
        },
        {
            "e1mPad": "A15",
            "e1mFunction": "ANA_S0",
            "owner": "alif",
            "siliconPeripheral": "",
            "siliconPad": "P0_0",
        },
    ]
    assert envelope["issues"] == []


def test_non_string_scalar_pad_fields_coerce_instead_of_refusing(tmp_path: Path) -> None:
    """BLOCKER regression: `owner`/`silicon_peripheral`/`silicon_pad` used to
    hard-refuse (exit 2) any non-`str` PyYAML scalar. Every `PinmuxPad` field
    is a `String` on the oracle, which coerces ANY scalar to its own text
    instead of rejecting it -- byte-matches the oracle: `owner: 7` -> `"7"`,
    `silicon_peripheral: 3.5` -> `"3.5"`, `silicon_pad: true` -> `"true"`, all
    at exit 0."""
    table = (
        "schemaVersion: pinmux-capability-v1\nfamily: v2n\npads:\n"
        "  - { e1m_pad: A1, e1m_function: GPIO, owner: 7, silicon_peripheral: 3.5, "
        "silicon_pad: true }\n"
    )
    sdk = _sdk_root(tmp_path, {"v2n": table})
    proj = _project(tmp_path)
    result = runner.invoke(
        app,
        ["--project", str(proj), "--family", "v2n", "--sdk-root", str(sdk), "--format", "json"],
    )
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["issues"] == []
    assert envelope["data"]["pads"] == [
        {
            "e1mPad": "A1",
            "e1mFunction": "GPIO",
            "owner": "7",
            "siliconPeripheral": "3.5",
            "siliconPad": "true",
        }
    ]


def test_compound_pad_fields_still_refuse(tmp_path: Path) -> None:
    """The one type mismatch a `String` field can never absorb, unaffected by
    the leniency above: `owner: [a, b]` and `e1m_pad: {a: b}` both still exit
    2 on the oracle, and still do here."""
    sequence_owner = (
        "schemaVersion: pinmux-capability-v1\nfamily: v2n\npads:\n"
        "  - { e1m_pad: A1, e1m_function: GPIO, owner: [a, b], silicon_peripheral: X, "
        "silicon_pad: Y }\n"
    )
    sdk = _sdk_root(tmp_path, {"v2n": sequence_owner})
    proj = _project(tmp_path)
    result = runner.invoke(
        app,
        ["--project", str(proj), "--family", "v2n", "--sdk-root", str(sdk), "--format", "json"],
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "pinmux.schema-version-unsupported"

    mapping_pad = (
        "schemaVersion: pinmux-capability-v1\nfamily: v2n\npads:\n"
        "  - { e1m_pad: {a: b}, e1m_function: GPIO, owner: x, silicon_peripheral: X, "
        "silicon_pad: Y }\n"
    )
    (sdk / "metadata" / "pinmux" / "v2n.yaml").write_text(mapping_pad, encoding="utf-8")
    result = runner.invoke(
        app,
        ["--project", str(proj), "--family", "v2n", "--sdk-root", str(sdk), "--format", "json"],
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "pinmux.schema-version-unsupported"


@_ORACLE_REQUIRED
def test_capitalized_bool_pad_literal_is_a_known_divergence_from_the_oracle(
    tmp_path: Path,
) -> None:
    """Both sides accept the row (exit 0, one pad). The oracle preserves the
    RAW YAML source spelling of a coerced scalar (`owner: True` -> `"True"`,
    `silicon_peripheral: on` -> `"on"`, `silicon_pad: yes` -> `"yes"`) --
    there is no equivalent recovery available to this port: PyYAML's stock
    (unmodified, per the module docstring) `SafeLoader` has already collapsed
    `True`/`On`/`Yes` to a single Python `bool True` by the time `_pad_field`
    ever sees it, with no way back to which of those spellings the document
    used. `_pad_field` prints the YAML-CANONICAL spelling instead
    (`"true"`, lowercase) -- correct for the common case (a lowercase
    `true`/`false` in a real generated table), divergent only for a
    capitalized or `on`/`off`/`yes`/`no`-style pad value, which no real
    `metadata/pinmux/*.yaml` table in this repo has ever contained.
    """
    table = (
        "schemaVersion: pinmux-capability-v1\nfamily: aen\npads:\n"
        '  - { e1m_pad: "A3", e1m_function: "PWM6", owner: True, '
        "silicon_peripheral: on, silicon_pad: yes }\n"
    )
    sdk = _sdk_root(tmp_path, {"aen": table})
    proj = _project(tmp_path)
    argv = [
        "--project", str(proj),
        "--family", "aen",
        "--sdk-root", str(sdk),
        "--format", "json",
    ]
    result = runner.invoke(app, argv)
    p_out = json.loads(result.stdout)
    r_code, r_out = _run_oracle(["pinmux", *argv], tmp_path)

    assert result.exit_code == r_code == 0
    assert p_out["issues"] == r_out["issues"] == []
    r_pad, p_pad = r_out["data"]["pads"][0], p_out["data"]["pads"][0]
    assert r_pad == {
        "e1mPad": "A3",
        "e1mFunction": "PWM6",
        "owner": "True",
        "siliconPeripheral": "on",
        "siliconPad": "yes",
    }
    assert p_pad == {
        "e1mPad": "A3",
        "e1mFunction": "PWM6",
        "owner": "true",
        "siliconPeripheral": "true",
        "siliconPad": "true",
    }
    # Every OTHER field on the envelope is a real match, not coincidentally
    # unchecked.
    assert {**r_out, "data": {**r_out["data"], "pads": []}} == {
        **p_out,
        "data": {**p_out["data"], "pads": []},
    }


def test_table_empty_after_tbd_filtering_is_a_validation_failure(tmp_path: Path) -> None:
    all_tbd = (
        "schemaVersion: pinmux-capability-v1\nfamily: v2n\npads:\n"
        '  - { e1m_pad: "TBD", e1m_function: "TBD", owner: "renesas", '
        'silicon_peripheral: "X", silicon_pad: "PA2" }\n'
    )
    sdk = _sdk_root(tmp_path, {"v2n": all_tbd})
    proj = _project(tmp_path)
    result = runner.invoke(
        app,
        [
            "--project", str(proj),
            "--family", "v2n",
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["data"]["pads"] == []
    assert envelope["issues"][0]["code"] == "pinmux.table-empty"


def test_schema_version_skew_is_a_validation_failure(tmp_path: Path) -> None:
    v2_doc = "schemaVersion: pinmux-capability-v2\nfamily: aen\npads: []\n"
    sdk = _sdk_root(tmp_path, {"aen": v2_doc})
    proj = _project(tmp_path)
    result = runner.invoke(
        app,
        [
            "--project", str(proj),
            "--family", "aen",
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "pinmux.schema-version-unsupported"


def test_text_mode_reports_family_and_pad_count(tmp_path: Path) -> None:
    sdk = _sdk_root(tmp_path, {"aen": _SAMPLE_TABLE})
    proj = _project(tmp_path)
    result = runner.invoke(
        app,
        ["--project", str(proj), "--sku", "E1M-AEN801", "--sdk-root", str(sdk)],
    )
    assert result.exit_code == 0
    assert "pinmux: family=aen pads=2" in result.output


def test_text_mode_no_target_shows_a_dash(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    result = runner.invoke(app, ["--project", str(proj)])
    assert "pinmux: family=- pads=0" in result.output


# ---------------------------------------------------------------------------
# Pure-function unit tests (mirrors `crates/tan-core/src/pinmux.rs`'s own
# `#[cfg(test)]` module)
# ---------------------------------------------------------------------------


def test_sku_to_family_prefix_map():
    assert pinmux_family_for_sku("E1M-AEN701") == "aen"
    assert pinmux_family_for_sku("E1M-V2N44") == "v2n"
    # E1M-V2M reuses the base V2N pinout in full; no separate table.
    assert pinmux_family_for_sku("E1M-V2M01") == "v2n"
    assert pinmux_family_for_sku("E1M-NX93") == "imx93"
    assert pinmux_family_for_sku("E1M-UNKNOWN") is None


def test_parse_drops_tbd_sentinel_pads():
    table = parse_pinmux_table_checked(
        "schemaVersion: pinmux-capability-v1\nfamily: v2n\npads:\n"
        '  - { e1m_pad: "TBD", e1m_function: "TBD", owner: "renesas", '
        'silicon_peripheral: "BL_PWM", silicon_pad: "PA5" }\n'
        '  - { e1m_pad: "A3", e1m_function: "PWM6", owner: "alif", '
        'silicon_peripheral: "", silicon_pad: "P0_0" }\n'
    )
    assert len(table.pads) == 1
    assert table.pads[0].e1m_pad == "A3"


def test_parse_drops_pads_missing_required_keys_and_defaults_owner():
    table = parse_pinmux_table_checked(
        "schemaVersion: pinmux-capability-v1\nfamily: aen\npads:\n"
        '  - { e1m_pad: "A3" }\n'
        '  - { e1m_pad: "A4", e1m_function: "PWM4" }\n'
    )
    assert len(table.pads) == 1
    assert table.pads[0].e1m_function == "PWM4"
    assert table.pads[0].owner == ""


def test_parse_rejects_non_v1_schema_version():
    try:
        parse_pinmux_table_checked("schemaVersion: pinmux-capability-v2\nfamily: aen\npads: []\n")
    except PinmuxParseError:
        pass
    else:
        raise AssertionError("expected a PinmuxParseError")

    try:
        parse_pinmux_table_checked(
            'family: aen\npads:\n  - { e1m_pad: "A3", e1m_function: "PWM6", owner: "alif", '
            'silicon_peripheral: "", silicon_pad: "P0_0" }\n'
        )
    except PinmuxParseError:
        pass
    else:
        raise AssertionError("expected a PinmuxParseError for a missing schemaVersion")


def test_parse_fails_soft_on_malformed_yaml_as_a_document_error():
    try:
        parse_pinmux_table_checked(": : not yaml : :")
    except PinmuxParseError:
        pass
    else:
        raise AssertionError("expected a PinmuxParseError")
