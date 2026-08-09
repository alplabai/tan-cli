# SPDX-License-Identifier: Apache-2.0
"""tan-cli#491 defects 7, 8, 9 -- the plan parser accepted what the v0.4.1
oracle refuses.

Every expectation below was established by RUNNING `target/debug/tan`
(`tan 0.4.1`) with the same plan under `--format json` and reading its
envelope, never by reading `crates/`. The measured oracle envelopes are
quoted on each test."""
import json

import pytest

from tan.core.build_plan import PlanParseError, parse_build_plan

NUL = chr(0)

_BASE = {
    "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml",
    "sku": "S", "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
    "slices": [],
}
_SLICE = {
    "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1", "appDir": "app",
    "configArtefacts": [], "toolchain": None, "artifacts": {}, "debug": {},
    "command": {"tool": "west", "args": ["build"], "cwd": "build/c1"},
    "env": {}, "envAppendPath": {},
}


def _plan(**top) -> str:
    return json.dumps({**_BASE, **top})


def _plan_with_slice(**slice_fields) -> str:
    return _plan(slices=[{**_SLICE, **slice_fields}])


def _plan_with_command(**command_fields) -> str:
    return _plan_with_slice(command={**_SLICE["command"], **command_fields})


# --------------------------------------------------------------------------
# Defect 7 -- `warnings` entries were accepted untyped.
# --------------------------------------------------------------------------
# Oracle, `warnings: ["oops"]`:
#   exit 1, {"code": "build.plan-invalid", "message": "build plan is not valid
#   JSON: invalid type: string \"oops\", expected struct PlanWarning at line 1
#   column 184"}
# The three-field `PlanWarning` shape was measured field by field:
#   {"code":"x"}                     -> "missing field `message`"
#   {"nope":1}                       -> "missing field `code`"
#   {"code":1,"message":"y"}         -> "invalid type: integer `1`, expected a string"
#   {"code":"x","message":null}      -> "invalid type: null, expected a string"
#   {"code":"x","message":"y","coreId":true} -> "invalid type: boolean `true`, expected a string"
#   {"code":"x","message":"y","coreId":null} -> ACCEPTED (Option<String>)
#   {"code":"x","message":"y","zzz":"q"}     -> ACCEPTED, `zzz` preserved in data.warnings
@pytest.mark.parametrize(
    "entry,fragment",
    [
        ("oops", "warnings[0]"),
        (123, "warnings[0]"),
        (None, "warnings[0]"),
        (True, "warnings[0]"),
        ({"nope": 1}, "code"),
        ({"code": "x"}, "message"),
        ({"code": 1, "message": "y"}, "code"),
        ({"code": None, "message": "y"}, "code"),
        ({"code": "x", "message": 1}, "message"),
        ({"code": "x", "message": None}, "message"),
        ({"code": "x", "message": "y", "coreId": True}, "coreId"),
        ({"code": "x", "message": "y", "coreId": ["a"]}, "coreId"),
    ],
)
def test_a_malformed_warning_entry_is_refused(entry, fragment):
    """`warnings` is a CONTRACT SURFACE -- `tan build` copies it verbatim into
    the envelope's `data.warnings`, so a consumer doing `.map(w => w.code)`
    breaks on a string. The oracle types it `Vec<PlanWarning>` and refuses the
    whole plan; before this fix the port let every one of these through."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(_plan(warnings=[entry]))
    assert e.value.code == "build.plan-invalid"
    assert fragment in e.value.message


@pytest.mark.parametrize(
    "entry",
    [
        {"code": "x", "message": "y"},
        {"code": "x", "message": "y", "coreId": "c1"},
        {"code": "x", "message": "y", "coreId": None},
        {"code": "x", "message": "y", "zzz": "q"},
    ],
)
def test_a_well_formed_warning_entry_still_parses(entry):
    """The other direction, and the reason the entries are NOT a closed set:
    the oracle tolerates and PRESERVES an unknown key (measured: `zzz` came
    back in `data.warnings`), because new warning codes may appear without a
    schemaVersion bump. Only `code`/`message`/`coreId` are typed."""
    assert parse_build_plan(_plan(warnings=[entry])).warnings == [entry]


# --------------------------------------------------------------------------
# Defect 8 -- an embedded NUL crashed the executor instead of being refused.
# --------------------------------------------------------------------------
# Measured against the port BEFORE this fix, via `tan build --plan-from <f>
# --execute` on a real project tree: every case below produced
#   exit 5, {"code": "build.internal-failure", "message": "ValueError:
#   embedded null byte"}
# -- a tan bug, for what is a malformed plan. The oracle has no `--execute`
# flag, so it has no observable behaviour on this path at all; the refusal
# below follows the shape `_is_legal_env_name` already established in this
# module for the sibling `=`/empty env names.
@pytest.mark.parametrize(
    "plan_json,fragment",
    [
        (_plan_with_slice(env={"FOO": "ba" + NUL + "r"}), "slices[0].env.FOO"),
        (_plan_with_slice(env={"F" + NUL + "OO": "bar"}), "slices[0].env"),
        (_plan_with_slice(envAppendPath={"PYTHONPATH": ["/a" + NUL + "b"]}),
         "slices[0].envAppendPath.PYTHONPATH[0]"),
        (_plan_with_slice(envAppendPath={"PY" + NUL + "PATH": ["/a"]}),
         "slices[0].envAppendPath"),
        (_plan_with_command(args=["bu" + NUL + "ild"]), "slices[0].command.args[0]"),
        (_plan_with_command(args=["ok", "bu" + NUL + "ild"]), "slices[0].command.args[1]"),
        (_plan_with_command(cwd="build/c" + NUL + "1"), "slices[0].command.cwd"),
        # REVIEW round: a fourth vector of the same class, missed by the first
        # pass. Re-measured on `--execute` against this branch BEFORE the
        # `buildRoot` refusal was added: `exit 5 build.internal-failure:
        # ValueError: embedded null byte`, from
        # `manifest.write_post_build_manifest` ->
        # `confine_to_build_root(Path(base), plan_build_root)` -> `Path.resolve`,
        # which runs after EVERY dispatch. No oracle answer to pre-empt: v0.4.1
        # has no `--execute`, and on `--materialise` it never reads the plan's
        # `buildRoot` (measured `exit 0 ok:true`).
        (_plan(buildRoot="bui" + NUL + "ld"), "buildRoot"),
    ],
)
def test_an_embedded_nul_on_the_spawn_path_is_a_coded_refusal(plan_json, fragment):
    """`subprocess`/`os.environ` raise a bare `ValueError: embedded null byte`
    for any of these, which reached the executor's catch-all and was reported
    as `build.internal-failure` at exit 5. It is a malformed plan, so it must
    be `build.plan-invalid` at exit 1."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(plan_json)
    assert e.value.code == "build.plan-invalid"
    assert fragment in e.value.message


@pytest.mark.parametrize(
    "plan_json",
    [
        # `command.tool`: measured on `--execute` -- the slice is `skipped`
        # with "tool `tr<NUL>ue` not found", the envelope carries a
        # `build.missing-tool` WARNING and a `build.nothing-built` ERROR, and
        # the run exits 1. A NUL-bearing path can never exist on disk, so the
        # miss is guaranteed. Not a crash, so not refused here.
        _plan_with_command(tool="we" + NUL + "st"),
        # `configArtefacts[].path` / `sharedArtefacts[].path`: the ORACLE has
        # a measured answer -- exit 3 `build.materialise-failed`, "failed to
        # write `gen/a.conf`: file name contained an unexpected NUL byte" --
        # so this parser must not pre-empt it with a vaguer refusal. THIS PORT
        # does not reach that answer: measured on `--materialise`, both give
        # `exit 5 build.internal-failure: ValueError: embedded null byte`.
        # That is a LIVE defect of this same class, left open only because the
        # fix belongs in `tan.commands.build.materialise`, out of this lane's
        # file -- listed as known-remaining on the PR. It is NOT, as this test
        # used to say, "left to the materialiser": the materialiser does not
        # handle it.
        _plan_with_slice(configArtefacts=[{"path": "b/m" + NUL + "55.conf", "contents": "x"}]),
        _plan(sharedArtefacts=[{"path": "gen/s" + NUL + ".h", "contents": "x"}]),
        # `buildDir`: measured on `--execute` with a slice that would succeed
        # (`backend: baremetal`, `command.tool: true`) -- exit 0, `ok:true`,
        # slice status `ok`. Nothing on the execute path opens it, so the NUL
        # is carried into `data` and dropped. Accepted because the value never
        # reaches an OS call -- NOT, as this test used to say, because "the
        # slice fails on its own terms"; it does not fail at all.
        _plan_with_slice(buildDir="build/c" + NUL + "1"),
    ],
)
def test_a_nul_off_the_spawn_path_is_deliberately_still_accepted(plan_json):
    """The scope line. Each of these carries a NUL this parser lets through,
    for a reason recorded per-case above -- either the oracle owns the answer
    (the two artefact paths) or the value never reaches an OS call
    (`command.tool`, `buildDir`). Two of the four are still WRONG downstream
    and are reported, not silently fixed."""
    assert parse_build_plan(plan_json) is not None


# --------------------------------------------------------------------------
# Defect 9 -- `schemaVersion` was compared with a bare `!=`, so `true` and
# `1.0` both compared equal to 1 and were ACCEPTED.
# --------------------------------------------------------------------------
# Oracle, measured one value at a time:
#   true       -> build.plan-invalid "invalid type: boolean `true`, expected u32"
#   false      -> build.plan-invalid "invalid type: boolean `false`, expected u32"
#   1.0        -> build.plan-invalid "invalid type: floating point `1.0`, expected u32"
#   2.5        -> build.plan-invalid "invalid type: floating point `2.5`, expected u32"
#   "1"        -> build.plan-invalid "invalid type: string \"1\", expected u32"
#   null       -> build.plan-invalid "invalid type: null, expected u32"
#   -1         -> build.plan-invalid "invalid value: integer `-1`, expected u32"
#   4294967296 -> build.plan-invalid "invalid value: integer `4294967296`, expected u32"
#   0 / 2 / 4294967295 -> the unsupported-schemaVersion message (an IN-RANGE u32
#                         that is simply not 1)
@pytest.mark.parametrize("version", [True, False, 1.0, 2.5, "1", None, [1], {"v": 1}])
def test_a_non_integer_schema_version_is_plan_invalid_not_a_version_skew(version):
    """`isinstance(True, int)` is True and `1.0 == 1` is True in Python, so a
    bare `!=` against 1 ACCEPTED both `true` and `1.0` as schemaVersion 1 --
    a plan the oracle refuses outright. A mistyped version is a malformed
    plan (`build.plan-invalid`), not a version skew."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(_plan(schemaVersion=version))
    assert e.value.code == "build.plan-invalid"
    assert "schemaVersion" in e.value.message


@pytest.mark.parametrize("version", [-1, -4294967296, 4294967296])
def test_an_out_of_u32_range_schema_version_is_plan_invalid(version):
    """The oracle types the field `u32`, so an in-type but out-of-range
    integer is refused as `invalid value: integer ..., expected u32` -- still
    `build.plan-invalid`, not a version skew."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(_plan(schemaVersion=version))
    assert e.value.code == "build.plan-invalid"
    assert "schemaVersion" in e.value.message


@pytest.mark.parametrize("version", [0, 2, 4294967295])
def test_an_in_range_but_unsupported_schema_version_is_still_a_version_skew(version):
    """The boundary the fix must NOT move: a well-typed u32 that simply is
    not 1 keeps `build.plan-unsupported-schema`, which is this port's
    deliberate refinement of the oracle's single `build.plan-invalid` code."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(_plan(schemaVersion=version))
    assert e.value.code == "build.plan-unsupported-schema"


def test_schema_version_one_still_parses():
    assert parse_build_plan(_plan()).schema_version == 1
