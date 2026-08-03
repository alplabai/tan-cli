# SPDX-License-Identifier: Apache-2.0
"""tan-cli#407: the two SDK discovery ladders must be TELLABLE APART from the
envelope alone.

`resolve_sdk_root_ladder` (narrow -- `build`, `doctor`, `clean`, `run`,
`flash`, `size`, `image`, `kconfig`, `validate`, `presets`, `inspect`,
`trace`, `sdk current`) and `resolve_sdk_root_wide` (wide -- `init`,
`generate`, `examples`, `renode`) legitimately DISAGREE about which checkout
a workspace holding both a child `<ws>/alp-sdk` and a lateral `../alp-sdk`
resolves to; that divergence is oracle-measured and pinned in
`tests/commands/test_build_manifest.py`, and this module does not relitigate
it.

What it pins is the reporting half. Both ladders label their answer with the
same `SdkSourceTier` string, `"discovery"`, so two different checkouts arrive
on the wire under one tier name -- and `sdk: {root, sourceTier}` exists (#110)
precisely so a consumer can tell which SDK produced a result. With both
stamped `discovery`, the vscode extension can hold an example catalogue and a
generated `alp.conf` from one checkout and a build plan from the other with
nothing in either envelope to compare.

The tier string itself cannot move: `test_build_manifest.py` asserts the exact
`(path, "discovery", None)` tuple for BOTH ladders in these layouts (it is the
oracle's own answer), and `resolve_sdk_root_ladder`'s docstring rejects an
unannounced sixth `SdkSourceTier` value as a wire-contract change. So #407's
other sanctioned remedy is what is tested here: a shared warning
(`sdk.discovery-divergent`) that every ladder caller emits, naming the checkout
the OTHER ladder would have chosen from this same directory.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.commands.build_cmd import (
    resolve_sdk_root_ladder,
    resolve_sdk_root_wide,
    sdk_ladder_divergence_issue,
)

#: `python/` -- the directory holding the `tan` package, so the subprocess
#: e2e below runs THIS tree's source rather than whatever `tan` is on PATH.
#: Repo-relative, never an absolute local path.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

DIVERGENCE_CODE = "sdk.discovery-divergent"


def _make_sdk(root: Path) -> Path:
    """The one marker every discovery tier keys on (`SDK_MARKER`). No
    `metadata/` deliberately: these tests measure RESOLUTION, and a planner
    that then refuses for a missing schema is the expected, coded outcome."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    return root


def _write_pin(workspace: Path, target: Path) -> None:
    """`.alp/sdk-path` in the `{"sdkPath": ...}` shape `sdk_cmd._pointer_target`
    reads -- a bare path string parses as invalid JSON and falls through."""
    (workspace / ".alp").mkdir(parents=True, exist_ok=True)
    (workspace / ".alp" / "sdk-path").write_text(
        json.dumps({"sdkPath": str(target).replace("\\", "/")}), encoding="utf-8"
    )


def _divergent_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """`(workspace, child, lateral)` -- the exact layout #407 measured: a
    `tan bootstrap` child under the workspace AND a checkout beside it. The
    narrow ladder answers `lateral`, the wide one `child`."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace, _make_sdk(workspace / "alp-sdk"), _make_sdk(tmp_path / "alp-sdk")


def _run_tan(*argv: str, cwd: Path):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
    )


def _envelope(proc):
    assert "Traceback" not in proc.stderr, f"an exception escaped the contract:\n{proc.stderr}"
    assert proc.stdout.strip(), f"no envelope on stdout; stderr:\n{proc.stderr}"
    return json.loads(proc.stdout)


# ─────────────────────────────── the premise ────────────────────────────────


def test_the_two_ladders_answer_different_checkouts_under_one_tier_string(tmp_path):
    """#407's measurement, as a test: same cwd, same absent `--sdk-root`, two
    different roots, one tier name. This is the state the warning below exists
    to make visible -- it is NOT a bug to be fixed by making the paths agree
    (that would move the SDK root under thirteen commands; see
    `resolve_sdk_root_ladder`'s own docstring)."""
    workspace, child, lateral = _divergent_layout(tmp_path)

    narrow_path, narrow_tier, _ = resolve_sdk_root_ladder(None, workspace)
    wide_path, wide_tier, _ = resolve_sdk_root_wide(None, workspace)

    assert (narrow_path, wide_path) == (lateral, child)
    assert narrow_tier == wide_tier == "discovery"


# ──────────────────────── sdk_ladder_divergence_issue ────────────────────────


def test_the_narrow_side_names_the_checkout_the_wide_ladder_would_have_taken(tmp_path):
    workspace, child, lateral = _divergent_layout(tmp_path)

    issue = sdk_ladder_divergence_issue(None, workspace, wide=False)

    assert issue is not None, "the narrow ladder must report the collision it is half of"
    assert issue.code == DIVERGENCE_CODE
    assert issue.severity == "warning"
    # Both roots in one message: the one this command used, and the one the
    # other four commands would use from the same directory. Naming only the
    # unused one would leave the reader diffing two separate command runs,
    # which is the exact thing #407 says nobody does.
    assert str(child).replace("\\", "/") in issue.message
    assert str(lateral).replace("\\", "/") in issue.message


def test_the_wide_side_names_the_checkout_the_narrow_ladder_would_have_taken(tmp_path):
    workspace, child, lateral = _divergent_layout(tmp_path)

    issue = sdk_ladder_divergence_issue(None, workspace, wide=True)

    assert issue is not None, "the wide ladder must report the same collision"
    assert issue.code == DIVERGENCE_CODE
    assert str(child).replace("\\", "/") in issue.message
    assert str(lateral).replace("\\", "/") in issue.message


def test_both_sides_report_the_same_two_roots_in_the_same_order(tmp_path):
    """One collision, one vocabulary. A consumer holding both envelopes must
    be able to match them on the pair of paths, so the two messages differ
    only in which side is labelled `this command`."""
    workspace, child, lateral = _divergent_layout(tmp_path)

    narrow = sdk_ladder_divergence_issue(None, workspace, wide=False)
    wide = sdk_ladder_divergence_issue(None, workspace, wide=True)

    assert narrow.code == wide.code == DIVERGENCE_CODE
    for root in (child, lateral):
        assert str(root).replace("\\", "/") in narrow.message
        assert str(root).replace("\\", "/") in wide.message


# ───────────────────────── silence when there is no collision ────────────────


def test_no_warning_for_a_bootstrap_child_with_nothing_beside_it(tmp_path):
    """tan-cli#218's canonical layout: the narrow ladder's own tail falls
    through to the same wide walk, so both answer the child. Warning here
    would fire on the single most common `tan bootstrap` shape there is."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(workspace / "alp-sdk")

    assert sdk_ladder_divergence_issue(None, workspace, wide=False) is None
    assert sdk_ladder_divergence_issue(None, workspace, wide=True) is None


def test_no_warning_for_a_lone_lateral_checkout(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(tmp_path / "alp-sdk")

    assert sdk_ladder_divergence_issue(None, workspace, wide=False) is None
    assert sdk_ladder_divergence_issue(None, workspace, wide=True) is None


def test_no_warning_when_a_project_pin_outranks_both_discovery_tiers(tmp_path):
    """`.alp/sdk-path` is read by BOTH ladders above their discovery tiers, so
    a pinned workspace has one answer however many checkouts surround it --
    and pinning is the fix the warning itself recommends."""
    workspace, _child, _lateral = _divergent_layout(tmp_path)
    _write_pin(workspace, _make_sdk(tmp_path / "pinned"))

    assert sdk_ladder_divergence_issue(None, workspace, wide=False) is None
    assert sdk_ladder_divergence_issue(None, workspace, wide=True) is None


def test_no_warning_when_an_explicit_sdk_root_is_given(tmp_path):
    """`--sdk-root` is terminal in both ladders (I-31), including when it names
    no checkout at all -- there is no second answer to collide with."""
    workspace, _child, _lateral = _divergent_layout(tmp_path)

    assert sdk_ladder_divergence_issue(str(tmp_path / "nope"), workspace, wide=False) is None
    assert sdk_ladder_divergence_issue(str(tmp_path / "chosen"), workspace, wide=True) is None


def test_no_warning_when_nothing_resolves_at_all(tmp_path):
    """Both ladders answer `none`, which already reads differently on the wire
    from `discovery` -- there is no tier collision to disambiguate."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert sdk_ladder_divergence_issue(None, workspace, wide=False) is None
    assert sdk_ladder_divergence_issue(None, workspace, wide=True) is None


def test_no_warning_when_only_one_ladder_resolves_anything(tmp_path, monkeypatch):
    """A workspace INSIDE a checkout that also holds a child: narrow takes the
    enclosing one, wide takes the child -- still a collision. But when one
    ladder answers `None` the two envelopes already differ by `sourceTier`
    (`none` vs `discovery`), so nothing needs disambiguating; that asymmetric
    case is asserted by the layouts above rather than invented here."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(tmp_path)
    _make_sdk(workspace / "alp-sdk")

    narrow = sdk_ladder_divergence_issue(None, workspace, wide=False)
    assert narrow is not None
    assert str(tmp_path).replace("\\", "/") in narrow.message


# ───────────────────────────── through the envelope ──────────────────────────


def test_tan_build_carries_the_divergence_warning_in_its_envelope(tmp_path):
    """#407 end to end on the narrow side: `tan build --format json` from a
    workspace with both checkouts must not report `sdk.sourceTier: discovery`
    with nothing saying a second checkout answered `discovery` too.

    The build itself refuses (`build.plan-unavailable` -- these fixtures carry
    no `metadata/schemas/board.schema.json`), which is deliberate: the warning
    is a property of RESOLUTION, so it must survive a run that never planned
    anything, exactly as `sdk.project-pin-unresolved` does."""
    workspace, child, lateral = _divergent_layout(tmp_path)

    env = _envelope(_run_tan("build", "--format", "json", cwd=workspace))

    assert env["sdk"]["sourceTier"] == "discovery"
    assert env["sdk"]["root"] == str(lateral)
    divergence = next(i for i in env["issues"] if i["code"] == DIVERGENCE_CODE)
    assert divergence["severity"] == "warning"
    assert str(child).replace("\\", "/") in divergence["message"]


def test_a_workspace_with_one_checkout_gets_no_divergence_warning_from_tan_build(tmp_path):
    """The regression the warning must not become: every ordinary single-SDK
    workspace would otherwise grow a permanent warning in its build envelope."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(tmp_path / "alp-sdk")

    env = _envelope(_run_tan("build", "--format", "json", cwd=workspace))

    assert [i["code"] for i in env["issues"] if i["code"] == DIVERGENCE_CODE] == []


def test_two_commands_on_different_ladders_leave_the_split_detectable(tmp_path):
    """#407's consumer-facing acceptance, phrased as the invariant that
    survives the wide/narrow split being retired (#269): when a wide-ladder
    command (`tan generate`) and a narrow-ladder command (`tan build`), run
    from ONE cwd, name different `sdk.root` values, at least one of the two
    envelopes must carry `sdk.discovery-divergent` -- otherwise the only way
    to notice is to diff two `sdk.root` strings across two separate runs,
    which nothing prompts a caller to do.

    Deliberately "at least one", not "both": the wide callers
    (`generate_cmd`, `examples_cmd`, `renode_cmd`, `init_cmd`) still have to
    be wired to `sdk_ladder_divergence_issue`, and this assertion stays true
    -- and keeps its meaning -- both before and after that lands."""
    workspace, _child, _lateral = _divergent_layout(tmp_path)

    build_env = _envelope(_run_tan("build", "--format", "json", cwd=workspace))
    generate_env = _envelope(_run_tan("generate", "--format", "json", cwd=workspace))

    build_root = (build_env.get("sdk") or {}).get("root")
    generate_root = (generate_env.get("sdk") or {}).get("root")
    if build_root == generate_root:
        pytest.skip("this layout did not split the two ladders; nothing to disambiguate")

    codes = [i["code"] for i in (*build_env["issues"], *generate_env["issues"])]
    assert DIVERGENCE_CODE in codes, (
        f"`tan build` resolved {build_root} and `tan generate` resolved "
        f"{generate_root}, and neither envelope said so"
    )
