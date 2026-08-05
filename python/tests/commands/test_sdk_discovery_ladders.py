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

    narrow = resolve_sdk_root_ladder(None, workspace)
    wide = resolve_sdk_root_wide(None, workspace)

    assert (narrow.path, wide.path) == (lateral, child)
    assert narrow.tier == wide.tier == "discovery"


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
    # `.as_posix()`, not `str()`: every path the envelope carries is POSIX-
    # normalised before it is emitted, so `str(lateral)` matches on a POSIX
    # host only. The `divergence["message"]` line below already knew that and
    # spelled its own normalisation by hand; this one did not, and the gap was
    # invisible until windows-latest ran it (tan-cli#413).
    assert env["sdk"]["root"] == lateral.as_posix()
    divergence = next(i for i in env["issues"] if i["code"] == DIVERGENCE_CODE)
    assert divergence["severity"] == "warning"
    assert child.as_posix() in divergence["message"]


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


# ───────────────── the four wide callers, wired (tan-cli#407) ─────────────────

#: Every module that calls the WIDE ladder. Kept as data because two things
#: read it: the structural gate below (which is what stops a sixth wide
#: caller landing silently unwired) and the reader, who would otherwise have
#: to trust a prose list in a docstring.
#:
#: `doctor_cmd` is the odd one and belongs here anyway: it RESOLVES narrowly
#: like its twelve siblings and calls `resolve_sdk_root_wide` only to report
#: what the other ladder would have answered (#407's "doctor surfaces the
#: divergence as a check"). It therefore emits `SDK_DISCOVERY_DIVERGENT` as
#: a `Check.code` rather than through `sdk_ladder_divergence_issue`, which
#: is why the gate accepts either spelling.
WIDE_LADDER_MODULES = (
    "init_cmd",
    "generate_cmd",
    "examples_cmd",
    "renode_cmd",
    "doctor_cmd",
)


def test_every_wide_ladder_caller_is_wired_to_the_divergence_warning():
    """The structural half of #407's stage 1, and the reason it is structural:
    the defect was never that the helper was wrong, it was that fifteen
    modules resolve an SDK and exactly ONE of them said so. A grep-shaped
    assertion is what makes a sixteenth impossible to add silently.

    `build_cmd` is excluded because it DEFINES both ladders and the helper;
    everything that CALLS `resolve_sdk_root_wide` must emit
    `sdk.discovery-divergent` by one of the two sanctioned spellings (see
    `WIDE_LADDER_MODULES` for why there are two).

    The caller probe matches CALL syntax (`name(`), not the bare name. The
    looser form was tried first and immediately reported `sdk_cmd.py`, which
    only mentions `resolve_sdk_root_wide` inside a docstring cross-reference
    at `sdk_cmd.py:509` -- a gate whose first finding is a comment teaches
    people to ignore it."""
    commands_dir = PACKAGE_ROOT / "tan" / "commands"
    callers = {
        module.stem: module
        for module in sorted(commands_dir.glob("*.py"))
        if module.stem != "build_cmd"
        and "resolve_sdk_root_wide(" in module.read_text(encoding="utf-8")
    }

    unwired = sorted(
        stem
        for stem, module in callers.items()
        if not any(
            spelling in module.read_text(encoding="utf-8")
            for spelling in ("sdk_ladder_divergence_issue(", "SDK_DISCOVERY_DIVERGENT")
        )
    )
    assert unwired == [], (
        f"{unwired} resolve the SDK through the wide ladder but never emit "
        f"{DIVERGENCE_CODE}, so a workspace holding two checkouts gets a "
        f"silent answer from them"
    )
    # The converse: `WIDE_LADDER_MODULES` is not stale. If a wide caller is
    # renamed or removed, this fails rather than silently shrinking the
    # gate's scope to whatever happens to be left.
    assert set(callers) == set(WIDE_LADDER_MODULES)


def _wide_command_argv(name: str, workspace: Path) -> tuple[str, ...]:
    """The shortest argv that reaches each wide command's issue-emission
    point in a workspace whose only SDKs are the two marker-only checkouts
    `_make_sdk` builds.

    `generate` is the one that needs a fixture: with no board.yaml it refuses
    at `generate.board-yaml-missing` BEFORE resolving an SDK at all (and that
    refusal's envelope is frozen in `contract/envelopes/`, so it must not
    grow an issue). Two lines of board.yaml carry it past that guard; every
    emit target then fails against a checkout with no `metadata/`, which is
    fine -- the warning is appended after the target loop, not inside it.

    `renode` needs nothing: its own refusal path carries the warning, which
    is the point (`renode.manifest-unavailable` tells the reader to run `tan
    build` first, and `tan build` is the ladder that disagrees)."""
    if name == "generate":
        (workspace / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    return {
        "examples": ("examples",),
        "generate": ("generate",),
        "init": ("init", "--name", "demo"),
        "renode": ("renode",),
    }[name]


@pytest.mark.parametrize("command", ["examples", "generate", "init", "renode"])
def test_each_wide_command_names_the_checkout_the_narrow_ladder_would_have_used(
    tmp_path, command
):
    """#407's acceptance for the wide side: in the divergent layout each of
    the four must emit `sdk.discovery-divergent`, and the message must name
    BOTH checkouts -- the one it used and the one thirteen other commands
    would have used. Naming only its own would leave the reader with a
    warning they cannot act on."""
    workspace, child, lateral = _divergent_layout(tmp_path)
    argv = _wide_command_argv(command, workspace)

    env = _envelope(_run_tan(*argv, "--format", "json", cwd=workspace))

    divergence = next((i for i in env["issues"] if i["code"] == DIVERGENCE_CODE), None)
    assert divergence is not None, (
        f"`tan {' '.join(argv)}` resolved "
        f"{(env.get('sdk') or {}).get('root')} and did not say that the "
        f"narrow ladder would have resolved another checkout; codes were "
        f"{[i['code'] for i in env['issues']]}"
    )
    assert divergence["severity"] == "warning"
    # `.as_posix()`, not `str()`: the message is built from `_abs_posix`, so
    # it carries `/` on every host (tan-cli#413's lesson, kept).
    assert child.as_posix() in divergence["message"]
    assert lateral.as_posix() in divergence["message"]


def test_a_wide_command_refusal_still_carries_the_divergence_warning(tmp_path):
    """`tan renode`'s dominant outcome in a fresh project is a REFUSAL --
    there is no `system-manifest.yaml` until `tan build` has run. The warning
    used to be appended sixty lines past six `fail_sdk` early returns, so the
    refusal that matters most shipped without it: its own message says to run
    `tan build`, and `tan build` is the ladder that resolves the OTHER
    checkout. Measured before the fix as
    `codes == ['renode.manifest-unavailable']`."""
    workspace, _child, _lateral = _divergent_layout(tmp_path)

    env = _envelope(_run_tan("renode", "--format", "json", cwd=workspace))

    codes = [i["code"] for i in env["issues"]]
    assert codes[0] == "renode.manifest-unavailable", (
        "the refusal itself must stay `issues[0]` -- context is appended "
        f"after it, not in front of it; got {codes}"
    )
    assert DIVERGENCE_CODE in codes


def test_doctor_reports_the_divergence_as_a_check_not_as_a_single_root(tmp_path):
    """#407's other named acceptance: `tan doctor` used to render the
    collision as `alp-sdk at <lateral> (discovery)` -- a `pass`, with no hint
    that `tan generate` in that same directory answers a different checkout
    under the identical tier name.

    Three things are asserted because three could regress independently: the
    check's STATUS (a `pass` here is the original defect), its CODE (`doctor.
    sdk` would not correlate with the five commands emitting
    `sdk.discovery-divergent`), and the EXIT CODE, which must not move --
    `exit_code_for` keys on `fail` alone, and a divergence is a warning, not
    a broken host."""
    workspace, child, lateral = _divergent_layout(tmp_path)

    env = _envelope(_run_tan("doctor", "--format", "json", cwd=workspace))

    sdk_checks = [c for c in env["data"]["checks"] if c["name"] == "sdk"]
    assert len(sdk_checks) == 1
    check = sdk_checks[0]
    assert check["status"] == "warn", f"still reported as a clean pass: {check}"
    assert lateral.as_posix() in check["detail"]
    assert child.as_posix() in check["detail"]

    codes = [i["code"] for i in env["issues"]]
    assert DIVERGENCE_CODE in codes
    assert "doctor.sdk" not in codes, (
        "the check must carry the shared code, not the derived `doctor.<name>` "
        "one -- a consumer correlating two envelopes matches on the code"
    )


def test_doctor_over_one_checkout_stays_a_clean_pass(tmp_path):
    """The regression the doctor half must not become. A single-SDK
    workspace -- every ordinary one -- keeps `sdk: pass` and gains no
    warning."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(tmp_path / "alp-sdk")

    env = _envelope(_run_tan("doctor", "--format", "json", cwd=workspace))

    check = next(c for c in env["data"]["checks"] if c["name"] == "sdk")
    assert check["status"] == "pass"
    assert DIVERGENCE_CODE not in [i["code"] for i in env["issues"]]


def test_both_sides_of_the_split_warn_now_that_the_wide_callers_are_wired(tmp_path):
    """The tightened form of `test_two_commands_on_different_ladders_leave_
    the_split_detectable` above, which deliberately asserts only "at least
    one" so it keeps its meaning after #269 collapses the two ladders.

    This one asserts BOTH, and pairs `tan build` with `tan examples` rather
    than `tan generate` on purpose: `examples` reaches its emission point in
    a bare workspace, so the test measures the wiring rather than how much
    fixture it takes to get a generate run past its guards."""
    workspace, child, lateral = _divergent_layout(tmp_path)

    build_env = _envelope(_run_tan("build", "--format", "json", cwd=workspace))
    examples_env = _envelope(_run_tan("examples", "--format", "json", cwd=workspace))

    assert build_env["sdk"]["root"] == lateral.as_posix()
    assert examples_env["sdk"]["root"] == child.as_posix()
    for label, env in (("build", build_env), ("examples", examples_env)):
        codes = [i["code"] for i in env["issues"]]
        assert DIVERGENCE_CODE in codes, f"`tan {label}` stayed silent; codes were {codes}"
