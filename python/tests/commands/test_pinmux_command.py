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


def test_capitalized_bool_pad_literal_is_a_known_divergence_from_the_oracle(
    tmp_path: Path,
) -> None:
    """Both sides accept the row (exit 0, one pad). The oracle preserved the
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

    assert result.exit_code == 0
    assert p_out["issues"] == []
    p_pad = p_out["data"]["pads"][0]
    assert p_pad == {
        "e1mPad": "A3",
        "e1mFunction": "PWM6",
        "owner": "true",
        "siliconPeripheral": "true",
        "siliconPad": "true",
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


def test_text_mode_renders_the_issue_on_a_hard_failure(tmp_path: Path) -> None:
    """tan-cli#458: the reproduce, verbatim. `--sdk-root <sdk> --family v2n`
    against an all-`TBD` table used to print only the unconditional summary
    line (`pinmux: family=v2n pads=0`) at exit 2, with the envelope's own
    `pinmux.table-empty` issue never reaching a terminal -- a user saw a
    plausible `pads=0` and no error at all. FAILS against the pre-fix code:
    the old text branch wrote the summary line and returned, so
    `issue.message` never appeared in `result.output`."""
    all_tbd = (
        "schemaVersion: pinmux-capability-v1\nfamily: v2n\npads:\n"
        '  - { e1m_pad: "TBD", e1m_function: "TBD", owner: "renesas", '
        'silicon_peripheral: "X", silicon_pad: "PA2" }\n'
    )
    sdk = _sdk_root(tmp_path, {"v2n": all_tbd})
    proj = _project(tmp_path)
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--family", "v2n"]
    )
    assert result.exit_code == 2
    assert "pinmux: family=v2n pads=0" in result.output
    assert (
        "pinmux: Pinmux capability table for family 'v2n' parsed with zero pads "
        "(metadata/pinmux/v2n.yaml)." in result.output
    )


def test_text_mode_renders_a_warning_issue_too(tmp_path: Path) -> None:
    """The no-target case is `warning`-severity at exit 0, not the `error`
    case above -- pinned separately so a fix that renders only `error`-
    severity issues (matching just the reported repro) still shows as a gap."""
    proj = _project(tmp_path)
    result = runner.invoke(app, ["--project", str(proj)])
    assert result.exit_code == 0
    assert "pinmux: Provide --sku <sku> or --family <family>." in result.output


def _marker_sdk(root: Path) -> Path:
    """The one file `discover_workspace_sdk` keys on -- no `metadata/`
    needed, matching `tests/commands/test_sdk_discovery_ladders.py`'s own
    `_make_sdk`: this reproduces the tan-cli#407 divergence layout, not a
    pinmux read."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    return root


def test_text_mode_renders_the_sdk_divergence_warning(tmp_path: Path) -> None:
    """Review finding, re-verified: `Envelope.__init__` appends
    `sdk.discovery-divergent` (tan-cli#407) to a NEW issues list
    (`_with_sdk_divergence`), not the one `_resolve` returned -- so a text
    branch iterating the pre-envelope `issues` local never sees it.
    Reproduces the exact layout `_with_sdk_divergence` gates on: a child
    `<project>/alp-sdk` AND a lateral `<project's parent>/alp-sdk`, no
    `--sdk-root`, so BOTH SDK ladders answer `sourceTier: discovery` for
    different checkouts. FAILS against the pre-fix code: the old text branch
    iterated `issues` (never `envelope.issues`), so the divergence warning
    reached the JSON envelope but never a terminal."""
    proj = _project(tmp_path)
    _marker_sdk(proj / "alp-sdk")  # the wide ladder's answer
    lateral = _marker_sdk(tmp_path / "alp-sdk")  # the narrow ladder's answer

    result = runner.invoke(app, ["--project", str(proj)])
    assert result.exit_code == 0
    assert "pinmux: Provide --sku <sku> or --family <family>." in result.output
    assert "pinmux: two alp-sdk checkouts resolve from this directory" in result.output
    assert lateral.as_posix() in result.output

    json_result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    envelope = json.loads(json_result.stdout)
    codes = {issue["code"] for issue in envelope["issues"]}
    assert "sdk.discovery-divergent" in codes


# ---------------------------------------------------------------------------
# tan-cli#359 -- `--family` may not escape `<sdkRoot>/metadata/pinmux`
# ---------------------------------------------------------------------------

#: Every `--family` shape that must be refused BEFORE any read. The Windows
#: rows are exercised on POSIX too, deliberately: `pathlib` on POSIX treats a
#: backslash as an ordinary filename character and `C:` as an ordinary
#: filename, so a guard written against `os.sep` alone passes every one of
#: these on Linux while leaving Windows wide open. The check under test reads
#: the RAW string on every host, so these rows are meaningful everywhere --
#: which is the whole point of running them here rather than behind a
#: `sys.platform` skip.
_REFUSED_FAMILIES = [
    pytest.param("/etc/passwd", id="posix-absolute"),
    pytest.param("../../../etc/passwd", id="posix-traversal"),
    pytest.param("..", id="dotdot-component"),
    pytest.param(".", id="dot-component"),
    pytest.param("C:", id="windows-drive-relative"),
    pytest.param("C:aen", id="windows-drive-relative-named"),
    pytest.param("C:\\Windows\\aen", id="windows-rooted-drive"),
    pytest.param("\\\\server\\share\\aen", id="windows-unc"),
    pytest.param("\\aen", id="windows-rooted-no-drive"),
    pytest.param("sub\\aen", id="backslash-separator"),
    pytest.param("..\\..\\aen", id="backslash-traversal"),
    pytest.param("sub/aen", id="slash-separator"),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("family", _REFUSED_FAMILIES)
def test_family_outside_the_pinmux_dir_is_one_coded_refusal(tmp_path: Path, family: str) -> None:
    """ONE coded issue, exit 2, no pads -- and, since a real table sits in the
    SDK under the ordinary `aen` stem, the empty `pads` also witnesses that
    nothing else was read in its place."""
    sdk = _sdk_root(tmp_path, {"aen": _SAMPLE_TABLE})
    proj = _project(tmp_path)
    result = runner.invoke(
        app,
        [
            "--project", str(proj),
            "--family", family,
            "--sdk-root", str(sdk),
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["data"]["pads"] == []
    assert "displayName" not in envelope["data"]
    assert [issue["code"] for issue in envelope["issues"]] == ["pinmux.family-invalid"]


def test_absolute_family_no_longer_reads_a_second_sdk_checkout(tmp_path: Path) -> None:
    """The issue's own reproduction, verbatim (tan-cli#359): with two alp-sdk
    checkouts, `--sdk-root <A> --family <B>/metadata/pinmux/aen` used to exit 0
    reporting `sdkRoot: <A>`, `pads: 96`, `issues: []` -- the envelope claiming
    A while the table came from B. `Path(A) / "<B>/metadata/pinmux/aen.yaml"`
    IS the B path (an absolute join discards the accumulated prefix), which is
    exactly what made the sdkRoot field a lie rather than merely unhelpful.

    B's table is a VALID one here, so a regression cannot hide behind a parse
    error: if the read happened, `pads` is 2 and `issues` is empty."""
    sdk_a = _sdk_root(tmp_path / "a", {})
    sdk_b = _sdk_root(tmp_path / "b", {"aen": _SAMPLE_TABLE})
    proj = _project(tmp_path)
    escaping = str(sdk_b / "metadata" / "pinmux" / "aen")
    result = runner.invoke(
        app,
        [
            "--project", str(proj),
            "--sdk-root", str(sdk_a),
            "--family", escaping,
            "--format", "json",
        ],
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["data"]["sdkRoot"] == str(sdk_a).replace("\\", "/")
    assert envelope["data"]["pads"] == []
    assert [issue["code"] for issue in envelope["issues"]] == ["pinmux.family-invalid"]


def test_symlinked_table_escaping_the_pinmux_dir_is_refused(tmp_path: Path) -> None:
    """The half the stem check structurally CANNOT see: `aen` is a perfectly
    plain stem, and the escape lives in the filesystem instead. This is why
    the containment re-check is a second, independent guard rather than a
    belt-and-braces duplicate of the charset."""
    sdk = _sdk_root(tmp_path, {})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "aen.yaml").write_text(_SAMPLE_TABLE, encoding="utf-8")
    link = sdk / "metadata" / "pinmux" / "aen.yaml"
    try:
        os.symlink(outside / "aen.yaml", link)
    except (OSError, NotImplementedError) as err:
        # Windows needs Developer Mode or SeCreateSymbolicLinkPrivilege; the
        # guard is host-independent, so skipping the FIXTURE here costs no
        # coverage of the guard itself on such a host.
        pytest.skip(f"host cannot create a symlink: {err}")
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
    assert envelope["data"]["pads"] == []
    assert [issue["code"] for issue in envelope["issues"]] == ["pinmux.family-invalid"]


# NON-VACUITY for the three refusals above -- a guard that refused EVERYTHING
# would satisfy every one of them. Deliberately not a fresh unit test of the
# private predicate: `test_family_overrides_sku_without_evaluating_it`
# (`--family aen`, `issues == []`) and
# `test_real_table_resolves_family_display_name_and_pads` (`--sku E1M-AEN801`
# -> the `aen` stem, 2 pads) already assert the accepting side end to end,
# through the same call path, and would both go red the moment the charset
# stopped admitting an ordinary stem.


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
