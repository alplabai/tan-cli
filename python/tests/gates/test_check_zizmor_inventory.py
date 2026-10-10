# SPDX-License-Identifier: Apache-2.0
"""`python/scripts/check_zizmor_inventory.py` -- Drift A and Drift B, both.

tan-cli#929: renaming an INVENTORY-tracked step used to leave
`.github/zizmor.yml` naming a step that no longer existed, with the gate
(then a bare finding COUNT compare, tan-cli#899/#919) still green -- two
renamed-past-each-other sites count the same, so a count alone cannot see an
identity change. These tests exercise the script's logic directly with
synthetic zizmor JSON, the same pattern `test_planner_resync.py` uses for
`planner_resync.py` -- the behaviour under test is the comparison logic, not
whatever the real tree's `.github/workflows/` happen to contain today (a
`test_zizmor_inventory` style test that shells the real `zizmor` binary would
also be reasonable, but would make CI depend on a tool this repo does not
otherwise require locally; `ci.yml`'s `workflow-security` job already runs
the real tool against the real tree every PR, which is the property that
actually matters -- see `.github/zizmor.yml`'s own "asking the real tool"
rationale, tan-cli#919 round 4).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "python" / "scripts" / "check_zizmor_inventory.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_zizmor_inventory", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


ZIZMOR_YML_TWO_ROWS = """\
# preamble
#   INVENTORY -- `artipacked` ONLY:
#
#     1. `planner-resync.yml`, the `Check out tan-cli dev` step (reason).
#        anchor: job=propose id=artipacked-inventory-1
#     2. `release.yml`, the `propose-dev-version-bump` job's `dev` checkout.
#        anchor: job=propose-dev-version-bump id=artipacked-inventory-2
#
# DRIFT THIS INVENTORY CATCHES, AND HOW.
# trailer prose
"""


def _finding(path: str, job: str, step_id: str | None, name: str | None = None) -> dict:
    """Build one `artipacked` finding shaped like zizmor's real JSON.

    Only the fields `check()`/`parse_artipacked_sites()` actually read are
    populated -- matching the real payload's shape (measured against zizmor
    1.29.0, see the script's own docstring), not the fields zizmor also
    emits but this script ignores (`determinations`, `fixes`, ...).
    """
    feature_lines = []
    if name is not None:
        feature_lines.append(f"name: {name}")
    if step_id is not None:
        feature_lines.append(f"        id: {step_id}")
    feature_lines.append("uses: actions/checkout@deadbeef # v7.0.1")
    return {
        "ident": "artipacked",
        "locations": [
            {
                "symbolic": {
                    "kind": "Primary",
                    "key": {"Local": {"verbatim_path": path}},
                    "route": {
                        "route": [
                            {"Key": "jobs"},
                            {"Key": job},
                            {"Key": "steps"},
                            {"Index": 0},
                        ]
                    },
                },
                "concrete": {"feature": "\n".join(feature_lines)},
            }
        ],
    }


FINDINGS_MATCHING = [
    _finding(
        ".github/workflows/planner-resync.yml",
        "propose",
        "artipacked-inventory-1",
        name="Check out tan-cli dev",
    ),
    _finding(
        ".github/workflows/release.yml",
        "propose-dev-version-bump",
        "artipacked-inventory-2",
    ),
]


def _write(tmp_path: pathlib.Path, findings: list[dict], zizmor_yml: str) -> tuple[pathlib.Path, pathlib.Path]:
    json_path = tmp_path / "no-ignores.json"
    json_path.write_text(json.dumps(findings), encoding="utf-8")
    yml_path = tmp_path / "zizmor.yml"
    yml_path.write_text(zizmor_yml, encoding="utf-8")
    return json_path, yml_path


def test_matching_inventory_passes(tmp_path, capsys):
    json_path, yml_path = _write(tmp_path, FINDINGS_MATCHING, ZIZMOR_YML_TWO_ROWS)
    assert mod.check(json_path, yml_path, "14") == 0
    out = capsys.readouterr().out
    assert "2 inline artipacked suppression(s)" in out


def test_renamed_step_name_does_not_break_the_gate(tmp_path):
    """tan-cli#929's own repro, inverted: a `name:` rename must NOT redden.

    The whole point of keying rows to `id:` instead of `name:` prose is that
    a step's human-facing display name can change without breaking the
    audit trail -- only its `id:` is load-bearing. This is the deliberate
    non-regression this fix must preserve, not merely a passing case.
    """
    renamed = [
        _finding(
            ".github/workflows/planner-resync.yml",
            "propose",
            "artipacked-inventory-1",
            name="Fetch the resync target",
        ),
        FINDINGS_MATCHING[1],
    ]
    json_path, yml_path = _write(tmp_path, renamed, ZIZMOR_YML_TWO_ROWS)
    assert mod.check(json_path, yml_path, "14") == 0


def test_renamed_step_id_is_drift_b_and_fails(tmp_path, capsys):
    """The actual tan-cli#929 failure mode once `id:` is the identity.

    Renaming the step's `id:` (the thing a row is now keyed to, the direct
    analogue of the original `name:` rename that used to slip through) must
    surface as a mismatch -- a real finding whose `id:` no longer matches
    any declared anchor, and a declared anchor with no matching finding.
    """
    renamed_id = [
        _finding(
            ".github/workflows/planner-resync.yml",
            "propose",
            "artipacked-inventory-1-renamed",
        ),
        FINDINGS_MATCHING[1],
    ]
    json_path, yml_path = _write(tmp_path, renamed_id, ZIZMOR_YML_TWO_ROWS)
    rc = mod.check(json_path, yml_path, "14")
    assert rc == 1
    err = capsys.readouterr().out
    assert "artipacked-inventory-1" in err
    assert "propose" in err


def test_inventory_row_removed_is_drift_a_and_fails(tmp_path, capsys):
    """Probe (b): an INVENTORY entry removed while the suppression stays."""
    one_row = """\
#   INVENTORY -- `artipacked` ONLY:
#
#     1. `planner-resync.yml`, the `Check out tan-cli dev` step (reason).
#        anchor: job=propose id=artipacked-inventory-1
#
# DRIFT THIS INVENTORY CATCHES, AND HOW.
"""
    json_path, yml_path = _write(tmp_path, FINDINGS_MATCHING, one_row)
    rc = mod.check(json_path, yml_path, "14")
    assert rc == 1
    out = capsys.readouterr().out
    assert "propose-dev-version-bump" in out


def test_empty_findings_with_nonempty_inventory_fails(tmp_path, capsys):
    """Probe (d): the tool exits 0 with valid-but-empty output.

    An empty findings list is exactly what a `zizmor` that silently stopped
    seeing `.github/workflows/` (or a wrong `--min-severity`/no-op flag)
    would produce -- structurally valid JSON, zero substance. It must not
    read as "nothing to report, all clear" when INVENTORY still declares
    suppressions.
    """
    json_path, yml_path = _write(tmp_path, [], ZIZMOR_YML_TWO_ROWS)
    rc = mod.check(json_path, yml_path, "0")
    assert rc == 1
    out = capsys.readouterr().out
    assert "artipacked-inventory-1" in out
    assert "artipacked-inventory-2" in out


def test_unparseable_json_fails_loud(tmp_path, capsys):
    """Probe (c): the tool absent from PATH (or crashed) leaves no valid JSON.

    `zizmor` missing from `PATH` makes the shell step's own `zizmor ...`
    invocation fail before this script ever runs -- `command not found`,
    non-zero rc, and (critically) no JSON file for the redirect to have
    populated. That is exactly the branch this test drives directly: a path
    that does not exist, standing in for "the redirect never wrote a file
    because the command never ran".
    """
    missing = tmp_path / "does-not-exist.json"
    yml_path = tmp_path / "zizmor.yml"
    yml_path.write_text(ZIZMOR_YML_TWO_ROWS, encoding="utf-8")
    rc = mod.check(missing, yml_path, "127")
    assert rc == 1
    out = capsys.readouterr().out
    assert "exited 127" in out
    assert "cannot run" in out


def test_extra_suppression_with_no_inventory_row_fails(tmp_path, capsys):
    """A THIRD real artipacked finding with no matching row -- Drift A."""
    extra = FINDINGS_MATCHING + [
        _finding(".github/workflows/other.yml", "some-job", "artipacked-inventory-3")
    ]
    json_path, yml_path = _write(tmp_path, extra, ZIZMOR_YML_TWO_ROWS)
    rc = mod.check(json_path, yml_path, "14")
    assert rc == 1
    out = capsys.readouterr().out
    assert "artipacked-inventory-3" in out


def test_unanchored_row_fails(tmp_path, capsys):
    """A numbered row with no `anchor:` trailer at all is refused, not

    silently treated as unpaired -- a maintainer who adds a row and forgets
    the anchor line gets a specific error, not a confusing count mismatch.
    """
    no_anchor = """\
#   INVENTORY -- `artipacked` ONLY:
#
#     1. `planner-resync.yml`, the `Check out tan-cli dev` step (reason).
#
# DRIFT THIS INVENTORY CATCHES, AND HOW.
"""
    json_path, yml_path = _write(tmp_path, [FINDINGS_MATCHING[0]], no_anchor)
    rc = mod.check(json_path, yml_path, "14")
    assert rc == 1
    out = capsys.readouterr().out
    assert "carry no" in out


def test_finding_with_no_id_on_the_step_fails(tmp_path, capsys):
    """A real artipacked finding whose step carries no `id:` at all."""
    no_id = [
        _finding(".github/workflows/planner-resync.yml", "propose", None),
        FINDINGS_MATCHING[1],
    ]
    json_path, yml_path = _write(tmp_path, no_id, ZIZMOR_YML_TWO_ROWS)
    rc = mod.check(json_path, yml_path, "14")
    assert rc == 1
    out = capsys.readouterr().out
    assert "no `id:`" in out


@pytest.mark.parametrize(
    "zizmor_yml",
    [
        "# no INVENTORY heading here\n",
        "#   INVENTORY -- `artipacked` ONLY:\n#     1. row with no end marker\n",
    ],
)
def test_malformed_inventory_section_fails(tmp_path, zizmor_yml, capsys):
    json_path, yml_path = _write(tmp_path, FINDINGS_MATCHING, zizmor_yml)
    rc = mod.check(json_path, yml_path, "14")
    assert rc == 1


def test_skipped_row_number_fails(tmp_path, capsys):
    skipped = """\
#   INVENTORY -- `artipacked` ONLY:
#
#     1. `planner-resync.yml`, the `Check out tan-cli dev` step (reason).
#        anchor: job=propose id=artipacked-inventory-1
#     3. `release.yml`, the `propose-dev-version-bump` job's `dev` checkout.
#        anchor: job=propose-dev-version-bump id=artipacked-inventory-2
#
# DRIFT THIS INVENTORY CATCHES, AND HOW.
"""
    json_path, yml_path = _write(tmp_path, FINDINGS_MATCHING, skipped)
    rc = mod.check(json_path, yml_path, "14")
    assert rc == 1
    out = capsys.readouterr().out
    assert "not a clean 1.." in out
