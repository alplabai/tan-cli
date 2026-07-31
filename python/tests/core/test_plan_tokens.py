# SPDX-License-Identifier: Apache-2.0
import dataclasses
import json

import pytest

from tan.core.build_plan import parse_build_plan
from tan.core.plan_tokens import (
    DemotedSlice,
    LeftoverToken,
    TokenValues,
    UnknownPlanPathMode,
    UnresolvedToolchainRoot,
    sdk_commit_mismatches,
    substitute_plan_tokens,
    project_root_diverges_from_exec_base,
)

LEGACY = """{
  "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml", "sku": "S",
  "buildRoot": "build", "slices": [], "sharedArtefacts": [], "warnings": []
}"""


def _tokened(board_yaml: str, slices: str, shared: str = "[]") -> str:
    return f"""{{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened",
      "boardYaml": "{board_yaml}", "sku": "S", "buildRoot": "build",
      "slices": [{slices}], "sharedArtefacts": {shared}, "warnings": []
    }}"""


def _slice(
    env: str = "{}",
    core_id: str = "c1",
    app_dir: str | None = "app",
    build_dir: str = "build/c1",
    env_append_path: str = "{}",
    cmd_args: str = '["build"]',
    cmd_cwd: str = "build/c1",
    config_artefacts: str = "[]",
) -> str:
    app_dir_json = "null" if app_dir is None else f'"{app_dir}"'
    return f"""{{
      "coreId": "{core_id}", "backend": "zephyr", "buildDir": "{build_dir}", "appDir": {app_dir_json},
      "configArtefacts": {config_artefacts}, "toolchain": null, "artifacts": {{}}, "debug": {{}},
      "command": {{"tool": "west", "args": {cmd_args}, "cwd": "{cmd_cwd}"}},
      "env": {env}, "envAppendPath": {env_append_path}
    }}"""


def values(**kw):
    base = dict(sdk_root="/sdk", project_root="/w", python="python3", toolchain_root=None)
    base.update(kw)
    return TokenValues(**base)


# ---------------------------------------------------------------------------
# Legacy / mode guards
# ---------------------------------------------------------------------------


def test_legacy_plan_without_plan_path_mode_is_untouched():
    plan = parse_build_plan(LEGACY)
    out, demoted = substitute_plan_tokens(plan, values())
    assert out == plan
    # A fresh top-level object -- matches Rust's plan.clone() -- so a
    # downstream mutation of `out` can never alias back into `plan`.
    assert out is not plan
    assert demoted == []


def test_unknown_plan_path_mode_is_a_hard_error():
    plan = parse_build_plan(_tokened("${PROJECT_ROOT}/board.yaml", "").replace(
        '"planPathMode": "tokened"', '"planPathMode": "tokened-v2"'
    ))
    with pytest.raises(UnknownPlanPathMode) as e:
        substitute_plan_tokens(plan, values())
    assert e.value.mode == "tokened-v2"


# ---------------------------------------------------------------------------
# Per-field substitution -- one test per row of the field table.
# ---------------------------------------------------------------------------


def test_board_yaml_substitutes_project_root():
    plan = parse_build_plan(_tokened("${PROJECT_ROOT}/board.yaml", ""))
    out, demoted = substitute_plan_tokens(plan, values())
    assert out.board_yaml == "/w/board.yaml"
    assert demoted == []


def test_slice_build_dir_substitutes():
    plan = parse_build_plan(
        _tokened("/w/board.yaml", _slice(build_dir="${SDK_ROOT}/build/c1"))
    )
    out, demoted = substitute_plan_tokens(plan, values())
    assert out.slices[0].build_dir == "/sdk/build/c1"
    assert demoted == []


def test_slice_app_dir_substitutes():
    plan = parse_build_plan(_tokened("/w/board.yaml", _slice(app_dir="${PROJECT_ROOT}/app")))
    out, demoted = substitute_plan_tokens(plan, values())
    assert out.slices[0].app_dir == "/w/app"
    assert demoted == []


def test_slice_app_dir_null_survives_untouched():
    """appDir is nullable per the SDK schema -- a Yocto slice built from the
    stock-image token emits `appDir: null`. Must pass through as None, never
    raise (regression: `.replace()` on None)."""
    plan = parse_build_plan(_tokened("/w/board.yaml", _slice(app_dir=None)))
    out, demoted = substitute_plan_tokens(plan, values())
    assert out.slices[0].app_dir is None
    assert demoted == []


def test_slice_env_value_substitutes():
    plan = parse_build_plan(
        _tokened("/w/board.yaml", _slice(env='{"ALP_SDK_ROOT": "${SDK_ROOT}"}'))
    )
    out, demoted = substitute_plan_tokens(plan, values())
    assert out.slices[0].env["ALP_SDK_ROOT"] == "/sdk"
    assert demoted == []


def test_slice_env_append_path_substitutes_each_list_element():
    """Miss this one and a literal ${SDK_ROOT}/scripts lands on PYTHONPATH --
    a silently wrong build, not a crash."""
    plan = parse_build_plan(
        _tokened(
            "/w/board.yaml",
            _slice(
                env_append_path=(
                    '{"PYTHONPATH": ["${SDK_ROOT}/scripts", "${PROJECT_ROOT}/lib"]}'
                )
            ),
        )
    )
    out, demoted = substitute_plan_tokens(plan, values())
    assert out.slices[0].env_append_path["PYTHONPATH"] == ["/sdk/scripts", "/w/lib"]
    assert demoted == []


def test_command_cwd_substitutes():
    plan = parse_build_plan(
        _tokened("/w/board.yaml", _slice(cmd_cwd="${PROJECT_ROOT}/build/c1"))
    )
    out, demoted = substitute_plan_tokens(plan, values())
    assert out.slices[0].command.cwd == "/w/build/c1"
    assert demoted == []


def test_command_args_substitute_each_element():
    plan = parse_build_plan(
        _tokened(
            "/w/board.yaml",
            _slice(cmd_args='["build", "-b", "board", "${PROJECT_ROOT}/app", "--", "-DX=${PYTHON}"]'),
        )
    )
    out, demoted = substitute_plan_tokens(plan, values(python="/w/.venv/bin/python"))
    assert out.slices[0].command.args == [
        "build", "-b", "board", "/w/app", "--", "-DX=/w/.venv/bin/python",
    ]
    assert demoted == []


def test_config_artefact_path_and_contents_substitute():
    plan = parse_build_plan(
        _tokened(
            "/w/board.yaml",
            _slice(
                config_artefacts=(
                    '[{"path": "${PROJECT_ROOT}/build/alp.conf", '
                    '"contents": "ROOT=${SDK_ROOT}\\n"}]'
                )
            ),
        )
    )
    out, demoted = substitute_plan_tokens(plan, values())
    art = out.slices[0].config_artefacts[0]
    assert art["path"] == "/w/build/alp.conf"
    assert art["contents"] == "ROOT=/sdk\n"
    assert demoted == []


def test_shared_artefact_path_and_contents_substitute():
    plan = parse_build_plan(
        _tokened(
            "/w/board.yaml",
            "",
            shared='[{"path": "${PROJECT_ROOT}/build/ipc.h", "contents": "/* ${SDK_ROOT} */\\n"}]',
        )
    )
    out, demoted = substitute_plan_tokens(plan, values())
    art = out.shared_artefacts[0]
    assert art["path"] == "/w/build/ipc.h"
    assert art["contents"] == "/* /sdk */\n"
    assert demoted == []


# ---------------------------------------------------------------------------
# Leftover-token guard
# ---------------------------------------------------------------------------


def test_leftover_unknown_token_is_refused():
    plan = parse_build_plan(_tokened("${UNKNOWN}/board.yaml", ""))
    with pytest.raises(LeftoverToken) as e:
        substitute_plan_tokens(plan, values())
    assert "${UNKNOWN}" in str(e.value)
    assert e.value.field == "boardYaml"


def test_unterminated_token_is_a_leftover_too():
    plan = parse_build_plan(_tokened("${SDK_ROOT/board.yaml", ""))
    with pytest.raises(LeftoverToken) as e:
        substitute_plan_tokens(plan, values())
    assert e.value.token == "${SDK_ROOT/board.yaml"


# ---------------------------------------------------------------------------
# ${TOOLCHAIN_ROOT} fatal-vs-demoted asymmetry
# ---------------------------------------------------------------------------


def test_plan_level_unresolved_toolchain_root_is_fatal():
    plan = parse_build_plan(_tokened("${TOOLCHAIN_ROOT}/board.yaml", ""))
    with pytest.raises(UnresolvedToolchainRoot) as e:
        substitute_plan_tokens(plan, values(toolchain_root=None))
    assert e.value.field == "boardYaml"


def test_shared_artefact_unresolved_toolchain_root_is_fatal():
    """sharedArtefacts are cross-slice -- no owning slice to demote to -- so
    this stays the plan-fatal error, same as boardYaml."""
    plan = parse_build_plan(
        _tokened(
            "/w/board.yaml", "",
            shared='[{"path": "build/ipc.h", "contents": "${TOOLCHAIN_ROOT}\\n"}]',
        )
    )
    with pytest.raises(UnresolvedToolchainRoot) as e:
        substitute_plan_tokens(plan, values(toolchain_root=None))
    assert e.value.field == "sharedArtefacts[0].contents"


def test_slice_confined_unresolved_toolchain_root_is_demoted_not_fatal():
    """A slice-confined unresolved ${TOOLCHAIN_ROOT} has an owning slice AND a
    dispatch seam (executionPolicy.missingTool) to route to, so it must NOT fail
    the whole plan. The literal token survives in the never-dispatched slice."""
    plan = parse_build_plan(
        _tokened(
            "${PROJECT_ROOT}/board.yaml",
            _slice(env='{"ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}"}', core_id="m33_sm"),
        )
    )
    out, demoted = substitute_plan_tokens(plan, values(toolchain_root=None))
    assert len(demoted) == 1
    assert demoted[0].slice_index == 0
    assert demoted[0].core_id == "m33_sm"
    assert demoted[0].field == "slices[0].env.ZEPHYR_SDK_INSTALL_DIR"
    assert out.slices[0].env["ZEPHYR_SDK_INSTALL_DIR"] == "${TOOLCHAIN_ROOT}"


def test_resolved_toolchain_root_substitutes_in_args_and_env():
    plan = parse_build_plan(
        _tokened(
            "${PROJECT_ROOT}/board.yaml",
            _slice(
                env='{"ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}"}',
                cmd_args='["build", "-DCMAKE_MAKE_PROGRAM=${TOOLCHAIN_ROOT}/ninja"]',
            ),
        )
    )
    out, demoted = substitute_plan_tokens(plan, values(toolchain_root="/opt/zephyr-sdk-1.0.1"))
    assert demoted == []
    assert out.slices[0].env["ZEPHYR_SDK_INSTALL_DIR"] == "/opt/zephyr-sdk-1.0.1"
    assert "-DCMAKE_MAKE_PROGRAM=/opt/zephyr-sdk-1.0.1/ninja" in out.slices[0].command.args


def test_blank_toolchain_root_is_treated_as_unresolved():
    """Some("") is the shape a caller that degrades a failed lookup to an
    empty string would hand in -- it must not substitute."""
    plan = parse_build_plan(_tokened("${TOOLCHAIN_ROOT}/board.yaml", ""))
    with pytest.raises(UnresolvedToolchainRoot):
        substitute_plan_tokens(plan, values(toolchain_root=""))


def test_plan_that_never_names_toolchain_root_builds_without_one():
    """Lazy resolution: a plan naming no ${TOOLCHAIN_ROOT} must not fail even
    though this host has none resolved."""
    plan = parse_build_plan(
        _tokened("${PROJECT_ROOT}/board.yaml", _slice(env='{"ALP_SDK_ROOT": "${SDK_ROOT}"}'))
    )
    out, demoted = substitute_plan_tokens(plan, values(toolchain_root=None))
    assert out.board_yaml == "/w/board.yaml"
    assert demoted == []


def test_demoted_slice_config_artefacts_are_stripped():
    """A demoted slice's configArtefacts must not survive into the output
    plan -- nothing will ever consume them this run."""
    plan = parse_build_plan(
        _tokened(
            "${PROJECT_ROOT}/board.yaml",
            _slice(
                env='{"ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}"}',
                config_artefacts='[{"path": "build/c1/alp.conf", "contents": "CONFIG_GPIO=y\\n"}]',
            ),
        )
    )
    out, demoted = substitute_plan_tokens(plan, values(toolchain_root=None))
    assert len(demoted) == 1
    assert out.slices[0].config_artefacts == []


def test_second_untouched_slice_is_unaffected_by_first_slices_demotion():
    plan = parse_build_plan(
        _tokened(
            "${PROJECT_ROOT}/board.yaml",
            _slice(env='{"ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}"}', core_id="m33_sm")
            + ", "
            + _slice(env='{"ALP_SDK_ROOT": "${SDK_ROOT}"}', core_id="m55_hp", build_dir="build/c2", cmd_cwd="build/c2"),
        )
    )
    out, demoted = substitute_plan_tokens(plan, values(toolchain_root=None))
    assert len(demoted) == 1
    assert demoted[0].core_id == "m33_sm"
    assert out.slices[1].env["ALP_SDK_ROOT"] == "/sdk"


def test_unknown_token_elsewhere_in_an_otherwise_demotable_slice_is_still_a_hard_error():
    """Demotion must never swallow a real bug elsewhere in the same slice."""
    plan = parse_build_plan(
        _tokened(
            "${PROJECT_ROOT}/board.yaml",
            _slice(
                env='{"ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}"}',
                cmd_args='["build", "${UNKNOWN}"]',
            ),
        )
    )
    with pytest.raises(LeftoverToken) as e:
        substitute_plan_tokens(plan, values(toolchain_root=None))
    assert e.value.field == "slices[0].command.args[1]"
    assert e.value.token == "${UNKNOWN}"


def test_toolchain_root_followed_by_unknown_token_in_one_field_reports_the_unknown_token():
    """A single field carrying BOTH tokens: the unresolved-but-known one must
    not stop the scan before the genuinely unknown one is found."""
    plan = parse_build_plan(
        _tokened(
            "${PROJECT_ROOT}/board.yaml",
            _slice(env='{"ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}/x/${UNKNOWN}"}'),
        )
    )
    with pytest.raises(LeftoverToken) as e:
        substitute_plan_tokens(plan, values(toolchain_root=None))
    assert e.value.field == "slices[0].env.ZEPHYR_SDK_INSTALL_DIR"
    assert e.value.token == "${UNKNOWN}"


def test_demoted_slice_config_artefact_contents_with_unknown_token_is_still_a_hard_error():
    """The LeftoverToken scan of configArtefacts contents happens BEFORE the
    caller ever clears the list on demotion -- an unknown token in there must
    not be silently discarded along with the rest of the artefact."""
    plan = parse_build_plan(
        _tokened(
            "${PROJECT_ROOT}/board.yaml",
            _slice(
                env='{"ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}"}',
                config_artefacts='[{"path": "build/c1/alp.conf", "contents": "${UNKNOWN}\\n"}]',
            ),
        )
    )
    with pytest.raises(LeftoverToken) as e:
        substitute_plan_tokens(plan, values(toolchain_root=None))
    assert e.value.field == "slices[0].configArtefacts[0].contents"
    assert e.value.token == "${UNKNOWN}"


# ---------------------------------------------------------------------------
# sdk_commit_mismatches / project_root_diverges_from_exec_base
# ---------------------------------------------------------------------------


def test_sdk_commit_mismatch_detection_treats_absent_as_no_signal():
    assert sdk_commit_mismatches("deadbee", "0000000") is True
    assert sdk_commit_mismatches("deadbee", "deadbee") is False
    # An SDK checkout with no .git (a release tarball) is a supported setup --
    # "could not resolve HEAD" is NO SIGNAL, never a mismatch.
    assert sdk_commit_mismatches("deadbee", "") is False
    assert sdk_commit_mismatches("", "deadbee") is False
    assert sdk_commit_mismatches("", "") is False


def test_sdk_commit_mismatch_short_vs_full_sha_agrees_by_common_prefix():
    assert sdk_commit_mismatches("deadbee", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef") is False
    assert sdk_commit_mismatches("DEADBEE", "deadbeefdead") is False
    assert sdk_commit_mismatches("deadbee", "1234567abcdef") is True


def test_project_root_diverges_from_exec_base_default_config_agrees():
    assert project_root_diverges_from_exec_base("/work/proj", "/work/proj") is False
    assert project_root_diverges_from_exec_base("/work/proj/", "/work/proj") is False


def test_project_root_diverges_from_exec_base_nested_board_yaml():
    assert (
        project_root_diverges_from_exec_base("/work/proj/examples/foo", "/work/proj") is True
    )


# ---------------------------------------------------------------------------
# Mandatory structural sweep -- catches a field nobody remembered to list.
# ---------------------------------------------------------------------------


def test_structural_sweep_no_token_survives_anywhere_in_the_plan():
    """Every string field that must be substituted carries a token; after
    substitution, re-serialise the WHOLE resulting plan and assert the
    literal substring `${` appears nowhere. A failure here means a missed
    field -- fix the field, never relax this assertion."""
    plan_json = _tokened(
        "${PROJECT_ROOT}/board.yaml",
        _slice(
            core_id="m33_sm",
            build_dir="${SDK_ROOT}/build/c1",
            app_dir="${PROJECT_ROOT}/app",
            env='{"ALP_SDK_ROOT": "${SDK_ROOT}", "PY": "${PYTHON}"}',
            env_append_path='{"PYTHONPATH": ["${SDK_ROOT}/scripts", "${PROJECT_ROOT}/lib"]}',
            cmd_cwd="${PROJECT_ROOT}/build/c1",
            cmd_args='["build", "${SDK_ROOT}/west", "-DX=${PYTHON}"]',
            config_artefacts=(
                '[{"path": "${PROJECT_ROOT}/build/alp.conf", "contents": "R=${SDK_ROOT}\\n"}]'
            ),
        ),
        shared='[{"path": "${PROJECT_ROOT}/build/ipc.h", "contents": "/* ${SDK_ROOT} */\\n"}]',
    )
    plan = parse_build_plan(plan_json)
    out, demoted = substitute_plan_tokens(
        plan, values(sdk_root="/sdk", project_root="/w", python="/w/.venv/bin/python")
    )
    assert demoted == []

    serialised = json.dumps(dataclasses.asdict(out))
    assert "${" not in serialised, serialised
