# SPDX-License-Identifier: Apache-2.0
"""An upstream release-list outage must skip the SDK work, not relocate the red.

tan-cli#840. `getting-started.yml` runs `west sdk install` because it is the
remedy `tan doctor` PRINTS. When `GET /repos/zephyrproject-rtos/sdk-ng/releases`
answers `[]`, west reports the empty list as `Unavailable SDK version: 1.0.1`
-- indistinguishable from a wrong pin, and the pin is re-derived upstream by
`check_toolchain_lock.py` at every run. `scripts/sdk_release_list_probe.py`
measures that signature; this gate holds the WORKFLOW half of the contract.

WHY SKIPPING ONE STEP IS NOT ENOUGH
------------------------------------

Skipping only `west sdk install` leaves four steps behind it running without a
toolchain, and each fails with a message about something else entirely:

  * `tan build` and the ARM-ELF assertion have nothing to build with.
  * `dirty host 1/3` requires `tan doctor` to exit 0. `zephyrSdk` is a `"fail"`
    when the toolchain is absent (`doctor_cmd.py:1201-1204`) and
    `exit_code_for` returns 4 on ANY fail, so the step reds accusing
    tan-cli#299 -- a false-refusal bug that is not happening.
  * `dirty host 3/3` runs a bare `tan doctor` under `set -euo pipefail`, so it
    dies on that same exit 4 before reaching a single one of its tan-cli#301
    leakage assertions.

That is the same defect the issue is about, moved one step down: a red whose
message names the wrong cause.

WHY `dirty host 2/3` IS DELIBERATELY NOT GATED
-----------------------------------------------

It asserts `tan doctor` exits **4** with west absent everywhere. `exit_code_for`
is binary -- 4 if any check fails, 0 otherwise -- so a missing Zephyr SDK
cannot change that verdict, and the step's `westResolved` assertion is
untouched by it. Gating it would drop real coverage for no reason, on a day
the job is already running degraded. `test_the_west_absent_step_is_not_gated`
holds that on purpose, so a later well-meant edit that gates everything
uniformly has to argue with this file first.
"""

from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "getting-started.yml"

_SCRIPT_PATH = REPO_ROOT / "python" / "scripts" / "sdk_release_list_probe.py"
_spec = importlib.util.spec_from_file_location("sdk_release_list_probe", _SCRIPT_PATH)
assert _spec and _spec.loader
probe_mod = importlib.util.module_from_spec(_spec)
sys.modules["sdk_release_list_probe"] = probe_mod
_spec.loader.exec_module(probe_mod)

#: The probe step's `id:`. Both halves of the gate expression are derived from
#: it below rather than spelled twice.
STEP_ID = "sdk_list"

PROBE_STEP = "probe the upstream sdk-ng release list (tan-cli#840)"
INSTALL_STEP = "install the Zephyr SDK (west sdk install, the printed remedy)"

#: Every step that cannot do its job without the Zephyr SDK toolchain, with
#: why -- the reason is in the message so a failure here explains itself.
SDK_DEPENDENT_STEPS = {
    "tan build (real ARM build of the scaffolded project)": "there is no toolchain to build with",
    "the scaffolded project produced an ARM ELF": "the build it asserts about did not run",
    "dirty host 1/3: west in the venv, absent from bare PATH (tan-cli#299)": (
        "it requires `tan doctor` exit 0, and a missing SDK makes zephyrSdk a "
        "fail, which exit_code_for turns into 4"
    ),
    "dirty host 3/3: stale $ZEPHYR_BASE + stale ~/.alp/sdk-default must not leak "
    "into the report (tan-cli#301)": (
        "its bare `tan doctor` runs under `set -euo pipefail`, so the same exit "
        "4 kills the step before any tan-cli#301 assertion runs"
    ),
}

#: Deliberately absent from the set above. See the module docstring.
WEST_ABSENT_STEP = (
    "dirty host 2/3: west absent EVERYWHERE, doctor must exit 4 "
    "(tan-cli#299 false-pass)"
)

#: The exact `if:` the gated steps must carry. Built from the script's own
#: OUTPUT_KEY so a rename there cannot leave this file asserting a stale
#: contract that the workflow silently reads as "no outage" forever.
GATE_EXPRESSION = f"steps.{STEP_ID}.outputs.{probe_mod.OUTPUT_KEY} != 'true'"


@functools.cache
def _steps() -> list[dict]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["first-install"]["steps"]


def _step(name: str) -> dict | None:
    return next((s for s in _steps() if s.get("name") == name), None)


def _index(name: str) -> int:
    return next(i for i, s in enumerate(_steps()) if s.get("name") == name)


def test_every_step_this_gate_names_still_exists():
    """Anti-vacuity, first: every assertion below looks a step up BY NAME. If
    a rename made those lookups return nothing, the rest of this file would
    pass having examined nothing at all."""
    named = [PROBE_STEP, INSTALL_STEP, WEST_ABSENT_STEP, *SDK_DEPENDENT_STEPS]
    missing = [n for n in named if _step(n) is None]
    assert not missing, (
        "these steps are named by this gate but are not in "
        "getting-started.yml's first-install job -- they were renamed or "
        "removed, and this gate stopped checking them SILENTLY. Update the "
        "names here in the same change, or drop the gate along with the "
        f"steps:\n  " + "\n  ".join(missing)
    )


def test_the_probe_step_carries_the_id_the_gates_read():
    step = _step(PROBE_STEP)
    assert step.get("id") == STEP_ID, (
        f"the probe step's id is {step.get('id')!r}, but every gated step "
        f"below reads `steps.{STEP_ID}.outputs`. A mismatch is not an error "
        f"on a runner -- it silently evaluates to the empty string, which "
        f"reads as 'no outage' forever."
    )


def test_the_probe_step_runs_the_script_with_the_pinned_version():
    run = _step(PROBE_STEP)["run"]
    assert "sdk_release_list_probe.py" in run, (
        f"the probe step does not run the probe script:\n{run}"
    )
    assert "--version" in run, (
        "the script is invoked without --version, so its warning cannot name "
        f"the pin it is exonerating -- which is the whole point:\n{run}"
    )


def test_the_probe_step_carries_the_token_that_keeps_it_measuring():
    """Without `GH_TOKEN` the probe's `gh api` calls fall back to the
    UNAUTHENTICATED per-IP quota, which is shared across every hosted runner
    in the region -- the exact limit the install step below already carries a
    token for. A rate-limited call returns non-zero, which this probe
    correctly refuses to read as a measurement, so it degrades to `proceed`:
    safe, silent, and permanently unable to report the outage it exists for.
    Dropping the token would therefore turn the fix off with nothing going
    red."""
    step = _step(PROBE_STEP)
    env = step.get("env") or {}
    assert "GH_TOKEN" in env, (
        "the probe step carries no GH_TOKEN, so its gh api calls run on the "
        "unauthenticated per-IP quota and the probe silently degrades to "
        f"'proceed' whenever that quota is exhausted. Step env: {env!r}"
    )
    assert "secrets.GITHUB_TOKEN" in str(env["GH_TOKEN"]), (
        f"GH_TOKEN is {env['GH_TOKEN']!r}; expected the workflow's own "
        f"GITHUB_TOKEN, which `permissions: contents: read` already covers "
        f"for listing public releases -- no new secret is needed here."
    )


def test_the_script_the_workflow_names_exists():
    """The workflow names a path. A moved or renamed script turns the probe
    step into a red of its own."""
    assert _SCRIPT_PATH.is_file(), _SCRIPT_PATH
    run = _step(PROBE_STEP)["run"]
    rel = "python/scripts/sdk_release_list_probe.py"
    assert rel in run, (
        f"the probe step must name {rel} so this assertion tracks the real "
        f"file:\n{run}"
    )


def test_the_probe_runs_before_the_install_it_guards():
    assert _index(PROBE_STEP) < _index(INSTALL_STEP), (
        "the probe runs AFTER the install it is supposed to guard -- the "
        "outage would be reported only once west had already failed on it."
    )


def test_the_install_step_is_gated_on_the_probe():
    step = _step(INSTALL_STEP)
    assert step.get("if") == GATE_EXPRESSION, (
        f"expected `if: {GATE_EXPRESSION}` on the install step, found "
        f"{step.get('if')!r}"
    )


def test_every_sdk_dependent_step_is_gated_on_the_probe():
    """Skipping only the install relocates the red one step down, into a
    message about tan-cli#299 or tan-cli#301 -- neither of which is
    happening."""
    wrong = {
        name: _step(name).get("if")
        for name in SDK_DEPENDENT_STEPS
        if _step(name).get("if") != GATE_EXPRESSION
    }
    assert not wrong, "\n".join(
        f"{name!r}\n    has if: {found!r}\n    wants:  {GATE_EXPRESSION!r}\n"
        f"    because {SDK_DEPENDENT_STEPS[name]}"
        for name, found in wrong.items()
    )


def test_the_west_absent_step_is_not_gated():
    """Coverage that survives an outage must keep running through one. See
    the module docstring for why this step is unaffected by a missing SDK."""
    found = _step(WEST_ABSENT_STEP).get("if")
    assert found is None, (
        f"{WEST_ABSENT_STEP!r} carries `if: {found!r}`, but its assertion "
        f"(tan doctor exits 4, westResolved is fail) cannot be changed by a "
        f"missing Zephyr SDK: exit_code_for is binary -- 4 on ANY failing "
        f"check -- and west is already absent here. Gating it drops real "
        f"coverage on exactly the degraded day it is most worth having. If "
        f"this is a deliberate reversal, argue it in the module docstring "
        f"above rather than deleting this test."
    )
