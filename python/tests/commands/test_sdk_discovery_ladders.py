# SPDX-License-Identifier: Apache-2.0
"""tan-cli#407: the two SDK discovery ladders must be TELLABLE APART from the
envelope alone.

`resolve_sdk_root_ladder` (narrow -- `build`, `doctor`, `clean`, `run`,
`flash`, `size`, `image`, `kconfig`, `validate`, `presets`, `inspect`,
`trace`, `sdk current`) and `resolve_sdk_root_wide` (wide -- `init`,
`generate`, `examples`) legitimately DISAGREE about which checkout
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

from tan.core.sdk_discovery import (
    resolve_sdk_root_ladder,
    resolve_sdk_root_wide,
    sdk_ladder_divergence_issue,
)

#: `python/` -- the directory holding the `tan` package, so the subprocess
#: e2e below runs THIS tree's source rather than whatever `tan` is on PATH.
#: Repo-relative, never an absolute local path.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

DIVERGENCE_CODE = "sdk.discovery-divergent"

#: A stable substring of `sdk_ladder_divergence_issue`'s own message
#: (`tan.core.sdk_discovery`'s `sdk_ladder_divergence_issue`), pinned the same way
#: `E2E_DIVERGENCE_PHRASE` below pins doctor's -- so a reword that keeps both
#: checkout paths in the text but drops the sentence naming what they mean
#: (or drops the "warning:" severity the text renderer prepends) still fails
#: this file instead of silently passing.
DIVERGENCE_TEXT_PHRASE = 'both report sourceTier "discovery"'


def _make_sdk(root: Path) -> Path:
    """The one marker every discovery tier keys on (`SDK_MARKER`). No
    `metadata/` deliberately: these tests measure RESOLUTION, and a planner
    that then refuses for a missing schema is the expected, coded outcome."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    return root


def _write_pin(workspace: Path, target: Path) -> None:
    """`.alp/sdk-path` in the `{"sdkPath": ...}` shape `sdk_discovery._pointer_target`
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


#: The exact sentence `scripts/e2e-full.sh`'s two #407 assertions grep for --
#: one positive, one negative control. Pinned here because NOTHING else pins
#: it: the unit suite keys on `DIVERGENCE_CODE`, and no workflow runs
#: `e2e-full.sh` at all (`grep -rn e2e-full.sh .github/workflows/` finds
#: nothing), so a reword of `doctor_cmd.py`'s message would leave every CI job
#: green while the harness's positive assertion fabricates a product defect and
#: its negative control silently reverts to un-fireable. That second half is
#: exactly the tan-cli#500 defect the harness was just fixed for, one string
#: over. Reword the message and this fails, naming the file to update.
E2E_DIVERGENCE_PHRASE = "resolve a DIFFERENT checkout"


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
    (`generate_cmd`, `examples_cmd`, `init_cmd`) still have to
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


# ───────────────── the three wide callers, wired (tan-cli#407) ────────────────

#: Every module that calls the WIDE ladder. Kept as data because two things
#: read it: the structural gate below (which is what stops a fifth wide
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
    "doctor_cmd",
)


def test_every_wide_ladder_caller_is_wired_to_the_divergence_warning():
    """The structural half of #407's stage 1, and the reason it is structural:
    the defect was never that the helper was wrong, it was that fifteen
    modules resolve an SDK and exactly ONE of them said so. A grep-shaped
    assertion is what makes a sixteenth impossible to add silently.

    `build_cmd` is excluded by name (tan-cli#408: the ladders and the helper
    now live in `tan.core.sdk_discovery`, not here, so this scan only ever
    walks `tan/commands/*.py` for CALLERS -- `build_cmd` merely imports
    `resolve_sdk_root_ladder` for its own narrow resolution and never calls
    the wide one, so the exclusion is belt-and-suspenders rather than
    load-bearing, and is kept so a future re-import of the wide ladder into
    `build_cmd` does not silently exempt it); everything that CALLS
    `resolve_sdk_root_wide` must emit `sdk.discovery-divergent` by one of the
    two sanctioned spellings (see `WIDE_LADDER_MODULES` for why there are
    two).

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
    fine -- the warning is appended after the target loop, not inside it."""
    if name == "generate":
        (workspace / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    return {
        "examples": ("examples",),
        "generate": ("generate",),
        "init": ("init", "--name", "demo"),
    }[name]


@pytest.mark.parametrize("command", ["examples", "generate", "init"])
def test_each_wide_command_names_the_checkout_the_narrow_ladder_would_have_used(
    tmp_path, command
):
    """#407's acceptance for the wide side: in the divergent layout each of
    the three must emit `sdk.discovery-divergent`, and the message must name
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
    """A wide command that refuses on its OWN first guard must still name the
    checkout it resolved. The warning is worth least on the success path and
    most on the refusal: a refusal is what a caller pointed at the wrong
    checkout actually sees.

    `init` is the one that can regress. Its refusals leave through
    `_emit_error`, a SECOND envelope-construction site that builds its own
    single-issue list and never reaches the `divergence_issue` computed forty
    lines below it; the warning survives only because `_emit_error` still
    passes `sdk=`, from which `Envelope._with_sdk_divergence` appends it. Drop
    that one keyword and every refusal ships silent while every success path
    stays green.

    `--template bogus` is the shortest refusal that still resolves an SDK
    first: `init.invalid-template` is raised inside `_plan_from_template`,
    after `_resolve_sdk_root`. A refusal raised BEFORE resolution reports no
    `sdk` block at all and is correctly warning-free."""
    workspace, _child, _lateral = _divergent_layout(tmp_path)

    env = _envelope(
        _run_tan(
            "init", "--name", "demo", "--template", "bogus", "--format", "json", cwd=workspace
        )
    )

    codes = [i["code"] for i in env["issues"]]
    assert codes[0] == "init.invalid-template", (
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

    # A FOURTH thing that can regress independently, and the only one nothing
    # else covers (tan-cli#500). `scripts/e2e-full.sh` greps this sentence in
    # both its #407 assertions -- the positive one and the negative control --
    # and no workflow runs that harness at all, so a reword here would leave
    # every CI job green while the positive assertion fabricates a product
    # defect and the control silently reverts to un-fireable. The control being
    # un-fireable is the defect #500 was opened for, one string over.
    assert E2E_DIVERGENCE_PHRASE in check["detail"], (
        f"scripts/e2e-full.sh greps {E2E_DIVERGENCE_PHRASE!r} in both its #407 "
        f"assertions and nothing else pins it -- the rest of this suite keys on "
        f"{DIVERGENCE_CODE!r}. Reword doctor's message and that harness starts "
        f"lying in both directions, silently. Update scripts/e2e-full.sh in the "
        f"same change.\n\ndetail was: {check['detail']!r}"
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


# ───────── the five narrow TEXT-channel callers, fixed (tan-cli#799) ─────────

#: The shortest argv that reaches each command's post-envelope point in the
#: bare `_divergent_layout` fixture (no board.yaml, no `metadata/`, no
#: `system-manifest.yaml`) WITHOUT the run taking the internal-failure guard
#: -- that guard reports `sdk=None` on `clean`/`run`, and a `None` sdk can
#: never carry the divergence warning (`Envelope._with_sdk_divergence`'s own
#: gate). Each of these five resolves the SDK, then refuses on something else
#: entirely (a missing system-manifest.yaml, a missing board.yaml) -- exactly
#: the shape #799 measured: the refusal reaches text, the warning didn't.
NARROW_TEXT_COMMANDS: dict[str, tuple[str, ...]] = {
    "size": ("size",),
    "image": ("image",),
    "clean": ("clean", "--dry-run"),
    "run": ("run",),
    "validate": ("validate",),
}


@pytest.mark.parametrize("command", sorted(NARROW_TEXT_COMMANDS))
def test_narrow_text_channel_carries_the_divergence_warning(tmp_path, command):
    """tan-cli#799: `size`/`image`/`clean`/`run`/`validate` each constructed
    their `Envelope` only inside `if json_mode:` and rendered text from a
    local `issues`/`text` list built strictly BEFORE that -- so
    `sdk.discovery-divergent`, appended at the `Envelope.__init__` seam,
    reached `--format json` and was silent on the default text channel.
    `pinmux_cmd.py` was already correct (builds the envelope once,
    unconditionally, and renders text from `envelope.issues`); this pins the
    same fix on the other five, measured end to end rather than at the
    seam/helper level the rest of this module already covers.
    """
    workspace, child, lateral = _divergent_layout(tmp_path)
    argv = NARROW_TEXT_COMMANDS[command]

    json_env = _envelope(_run_tan(*argv, "--format", "json", cwd=workspace))
    divergence = next((i for i in json_env["issues"] if i["code"] == DIVERGENCE_CODE), None)
    assert divergence is not None, (
        f"the premise: `tan {' '.join(argv)} --format json` in the divergent "
        f"layout must itself carry {DIVERGENCE_CODE}, or this test proves "
        f"nothing about the text channel; issues were "
        f"{[i['code'] for i in json_env['issues']]}"
    )

    text_proc = _run_tan(*argv, cwd=workspace)
    assert "Traceback" not in text_proc.stderr, (
        f"an exception escaped the contract:\n{text_proc.stderr}"
    )
    # Both checkout paths AND the stable phrase/severity prefix that make
    # them mean something -- a reword that dropped the "warning:" severity
    # or the sentence explaining what the two paths are (while still, by
    # coincidence, printing both paths somewhere in stderr) must fail this,
    # not silently pass it. Same reasoning as `E2E_DIVERGENCE_PHRASE` above.
    assert (
        child.as_posix() in text_proc.stderr
        and lateral.as_posix() in text_proc.stderr
        and "warning:" in text_proc.stderr
        and DIVERGENCE_TEXT_PHRASE in text_proc.stderr
    ), (
        f"`tan {' '.join(argv)}` (default/text) carried {DIVERGENCE_CODE} in "
        f"its `--format json` envelope but not on the default text channel; "
        f"stderr was:\n{text_proc.stderr!r}"
    )


def test_a_narrow_text_run_over_one_checkout_gets_no_divergence_warning(tmp_path):
    """The regression the fix must not become: every ordinary single-SDK
    workspace keeps a plain refusal on the text channel, with no collision
    warning invented for it -- the same control
    `test_a_workspace_with_one_checkout_gets_no_divergence_warning_from_tan_build`
    already pins for `--format json`."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _make_sdk(tmp_path / "alp-sdk")

    proc = _run_tan("size", cwd=workspace)

    assert "Traceback" not in proc.stderr
    assert "discovery-divergent" not in proc.stderr
    assert "two alp-sdk checkouts resolve" not in proc.stderr
