# SPDX-License-Identifier: Apache-2.0
"""Negative-matrix test for the retuned seam-1 comparator (alp-sdk#874
follow-up).

Guards `_drop_artefact_contents` (config-artefact CONTENT no longer
compared) and the sysbuild-scoped `-DEXTRA_CONF_FILE` strip (only
non-sysbuild slices get the arg stripped) against a future edit quietly
reintroducing either the pre-retune content diff or the "strip on every
slice regardless of sysbuild" hole: a real plan-SHAPE regression (command,
env, slice-count, probe, artefact added/removed/moved, a sysbuild slice
wrongly gaining `-DEXTRA_CONF_FILE`) must still fail the comparator; a
content-only artefact mutation, and the one hand-reviewed `debug.probe`
delta, must not.

Vendored from alp-sdk's `tests/parity/test_seam1_field_diff.py` -- KEEP IN
LOCKSTEP with the original.

Run: python -m pytest tests/parity/test_seam1_field_diff.py -q
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seam1_field_diff as s  # noqa: E402

_ORACLE_DIR = Path(__file__).resolve().parent / "oracle"


def _load(name: str) -> dict:
    return json.loads((_ORACLE_DIR / f"{name}.build-plan.json").read_text())


def _fails(oracle: dict, mutated: dict) -> bool:
    _, failing = s.diff_plans(s.normalize_plan(oracle), s.normalize_plan(mutated))
    return bool(failing)


def test_mutated_command_fails():
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    mutated["slices"][0]["command"]["tool"] = "not-cmake"
    assert _fails(oracle, mutated)


def test_mutated_env_fails():
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    mutated["slices"][0]["env"]["ALP_SDK_ROOT"] = "/something/else/entirely"
    assert _fails(oracle, mutated)


def test_slice_count_change_fails():
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    mutated["slices"].pop()
    assert _fails(oracle, mutated)


def test_disallowed_probe_transition_fails():
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    for sl in mutated["slices"]:
        if sl.get("debug", {}).get("probe") == "openocd":
            sl["debug"]["probe"] = "jlink"
            break
    assert _fails(oracle, mutated)


def test_allowed_probe_transition_passes():
    """The one hand-reviewed delta (openocd -> null, #848) must still pass."""
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    for sl in mutated["slices"]:
        if sl.get("debug", {}).get("probe") == "openocd":
            sl["debug"]["probe"] = None
    assert not _fails(oracle, mutated)


def test_artefact_added_fails():
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    mutated["slices"][0]["configArtefacts"].append(
        {"path": "build/extra/alp.conf", "contents": "# new\n"})
    assert _fails(oracle, mutated)


def test_artefact_removed_fails():
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    mutated["slices"][0]["configArtefacts"] = []
    assert _fails(oracle, mutated)


def test_artefact_path_moved_fails():
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    mutated["slices"][0]["configArtefacts"][0]["path"] += ".moved"
    assert _fails(oracle, mutated)


def test_content_only_mutation_passes():
    """The intended retune: config-artefact CONTENT alone is no longer
    diffed -- covered by the alp-sdk-side emit-snapshot goldens instead
    (see tests/parity/README.md)."""
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    for sl in mutated["slices"]:
        for art in sl.get("configArtefacts", []):
            art["contents"] = "# totally different content\n"
    assert not _fails(oracle, mutated)


def test_sdk_version_only_mutation_passes():
    """alp-sdk#883 (mirrored here): `sdkVersion` bumps on every version-bump
    PR with zero shape change (e.g. the oracle's 0.11.1 vs. a live 0.13.0
    emit) and must not, on its own, fail the comparator."""
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    mutated["sdkVersion"] = "0.13.0"
    assert not _fails(oracle, mutated)


def test_sysbuild_slice_wrongly_gaining_extra_conf_file_fails():
    """The `-DEXTRA_CONF_FILE` strip is scoped to non-sysbuild slices only.
    Both `connectivity_iot-fleet-ota` slices are sysbuild (`--sysbuild` in
    command.args); if one wrongly gained `-DEXTRA_CONF_FILE` (the Option-A
    per-core-config-drop bug ADR-0020 Amendment item 4 warns about -- it
    would land on the sysbuild image, not the app, silently dropping the
    per-core alp.conf), that must still fail rather than get silently
    stripped away like a non-sysbuild slice's legitimate
    `-DEXTRA_CONF_FILE`."""
    oracle = _load("connectivity_iot-fleet-ota")
    mutated = copy.deepcopy(oracle)
    sl = mutated["slices"][0]
    assert "--sysbuild" in sl["command"]["args"]
    sl["command"]["args"] = list(sl["command"]["args"]) + [
        "-DEXTRA_CONF_FILE=/should/not/be/on/a/sysbuild/slice/alp.conf"]
    assert _fails(oracle, mutated)


def test_non_sysbuild_slice_extra_conf_file_still_stripped():
    """Sanity check for the scoping itself: a NON-sysbuild slice's real
    `-DEXTRA_CONF_FILE` arg (the #863/#871 intended delta) is still
    stripped and does not, on its own, fail the comparator."""
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    sl = mutated["slices"][0]
    assert "--sysbuild" not in (sl["command"].get("args") or [])
    sl["command"]["args"] = list(sl["command"]["args"]) + [
        "-DEXTRA_CONF_FILE=/some/path/alp.conf"]
    assert not _fails(oracle, mutated)


def test_project_relpath_derives_boardyaml_directory():
    assert s._project_relpath(
        {"boardYaml": "examples/audio/i2s-tone/board.yaml"}
    ) == "examples/audio/i2s-tone"


def test_tokened_plan_reconciles_with_absolute_oracle_shape():
    """alp-sdk#865: a live plan emitted with `planPathMode: tokened` carries
    literal `${SDK_ROOT}`/`${PROJECT_ROOT}` in place of the pre-#865 frozen
    oracle's absolute checkout-root paths. `normalize_plan` must map both to
    the SAME normalized shape rather than diff a tokened plan as foreign."""
    absolute = {
        "boardYaml": "examples/audio/i2s-tone/board.yaml",
        "sdkCommit": "97ad481b",
        "slices": [{
            "appDir": "/abs/sdk/examples/audio/i2s-tone/zephyr",
            "env": {"ALP_SDK_ROOT": "/abs/sdk"},
            "command": {"tool": "cmake", "args": [
                "-DBOARD=e1m",
                "-DAPP_DIR=/abs/sdk/examples/audio/i2s-tone/zephyr"]},
        }],
    }
    tokened = {
        "planPathMode": "tokened",
        "boardYaml": "examples/audio/i2s-tone/board.yaml",
        "sdkCommit": "97ad481b",
        "slices": [{
            "appDir": "${PROJECT_ROOT}/zephyr",
            "env": {"ALP_SDK_ROOT": "${SDK_ROOT}"},
            "command": {"tool": "cmake", "args": [
                "-DBOARD=e1m", "-DAPP_DIR=${PROJECT_ROOT}/zephyr"]},
        }],
    }
    assert s.normalize_plan(absolute) == s.normalize_plan(tokened)
    assert not _fails(absolute, tokened)


# ---------------------------------------------------------------------
# #1344 / alplabai/tan-cli#550 -- the two keys the 97ad481b oracle
# predates entirely, allowed through ONLY at their inert default.
# ---------------------------------------------------------------------


def test_additive_keys_at_their_inert_default_pass():
    """`postCommands: []` and `artifacts.outputDir: null` on a zephyr /
    yocto slice are the whole live-vs-oracle delta #1344 introduces --
    the oracle carries neither key. Without this allowance the
    comparator exits 1 on all five oracle boards."""
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    for sl in mutated["slices"]:
        sl["postCommands"] = []
        sl["artifacts"]["outputDir"] = None
    assert not _fails(oracle, mutated)


def test_additive_key_with_a_real_value_still_fails():
    """The allowance is keyed on the exact inert value, not on "the
    oracle lacked this key": a slice that really grows a build step (a
    baremetal slice) is an unreviewed shape delta against an oracle that
    emitted none, and must still FAIL."""
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    for sl in mutated["slices"]:
        sl["postCommands"] = []
        sl["artifacts"]["outputDir"] = None
    mutated["slices"][0]["postCommands"] = [
        {"tool": "cmake", "args": ["--build", "."], "cwd": "build/x"}]
    assert _fails(oracle, mutated)

    mutated = copy.deepcopy(oracle)
    for sl in mutated["slices"]:
        sl["postCommands"] = []
        sl["artifacts"]["outputDir"] = None
    mutated["slices"][0]["artifacts"]["outputDir"] = "build/x/output"
    assert _fails(oracle, mutated)


def test_an_unrelated_new_key_is_not_swept_in_by_the_allowance():
    """Negative control against over-broadness: an allowance shaped as
    "oracle `<missing>` plus a falsy live value is fine" would blind the
    gate to every future additive key. Only the two NAMED keys pass."""
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    mutated["slices"][0]["someBrandNewKey"] = []
    assert _fails(oracle, mutated)
    mutated = copy.deepcopy(oracle)
    mutated["slices"][0]["artifacts"]["someBrandNewArtifact"] = None
    assert _fails(oracle, mutated)


def test_a_changed_existing_value_is_never_an_allowed_additive():
    """The allowance requires the oracle to genuinely LACK the key
    (`<missing>`). A key the oracle carries with a real value, blanked to
    the same inert default, is a capability loss -- still a failure."""
    oracle = _load("multicore_rpmsg-aen")
    mutated = copy.deepcopy(oracle)
    for sl in mutated["slices"]:
        sl["postCommands"] = []
        sl["artifacts"]["outputDir"] = None
    # slices[1] is the zephyr slice -- the one whose `compileCommands`
    # the oracle carries as a real path (slices[0] is yocto, null there).
    assert oracle["slices"][1]["artifacts"]["compileCommands"]
    mutated["slices"][1]["artifacts"]["compileCommands"] = None
    assert _fails(oracle, mutated)
