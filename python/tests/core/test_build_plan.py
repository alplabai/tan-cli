# SPDX-License-Identifier: Apache-2.0
import pytest

from tan.core.build_plan import PlanParseError, parse_build_plan
from tan.core.plan_exec import PolicyAction

MINIMAL = """{
  "schemaVersion": 1, "generatedBy": "alp_orchestrate", "boardYaml": "/w/board.yaml",
  "sku": "E1M-AEN801", "buildRoot": "build", "slices": [], "sharedArtefacts": [], "warnings": []
}"""


def test_parses_a_minimal_plan():
    plan = parse_build_plan(MINIMAL)
    assert plan.schema_version == 1
    assert plan.sku == "E1M-AEN801"
    assert plan.slices == []
    # Optional-but-always-emitted fields default cleanly (tolerant consumer).
    assert plan.execution_policy is None
    assert plan.plan_path_mode is None


def test_rejects_an_unsupported_schema_version():
    """The version-skew guard: fail LOUDLY rather than silently falling back to
    hand-ported behaviour -- that fallback is exactly the RFC #843 drift."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(MINIMAL.replace('"schemaVersion": 1', '"schemaVersion": 2'))
    assert e.value.code == "build.plan-unsupported-schema"
    assert "2" in e.value.message


def test_rejects_a_plan_missing_a_required_key():
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(MINIMAL.replace('"sku": "E1M-AEN801", ', ""))
    assert e.value.code == "build.plan-invalid"
    assert "sku" in e.value.message


def test_parses_slice_env_append_and_policy():
    plan = parse_build_plan("""{
      "schemaVersion": 1, "generatedBy": "alp_orchestrate", "boardYaml": "/w/board.yaml",
      "sku": "S", "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "executionPolicy": {"missingTool": "skip", "unknownBackend": "fail"},
      "slices": [{
        "coreId": "m55_hp", "backend": "zephyr", "buildDir": "build/m55_hp",
        "appDir": "app", "configArtefacts": [], "toolchain": null, "artifacts": [],
        "debug": {}, "command": {"tool": "west", "args": ["build"], "cwd": "build/m55_hp"},
        "env": {"ALP_SDK_ROOT": "/sdk"}, "envAppendPath": {"PYTHONPATH": ["/sdk/scripts"]}
      }]
    }""")
    assert plan.execution_policy.missing_tool is PolicyAction.SKIP
    assert plan.execution_policy.unknown_backend is PolicyAction.FAIL
    assert plan.execution_policy.null_command is None
    s = plan.slices[0]
    assert s.core_id == "m55_hp"
    assert s.command.tool == "west"
    assert s.env_append_path == {"PYTHONPATH": ["/sdk/scripts"]}


def test_null_command_slice_parses_as_none():
    """command: null is a legitimate skip-with-warning slice, not a parse error."""
    plan = parse_build_plan("""{
      "schemaVersion": 1, "generatedBy": "alp_orchestrate", "boardYaml": "/w/board.yaml",
      "sku": "S", "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "slices": [{
        "coreId": "a55", "backend": "yocto", "buildDir": "build/a55", "appDir": "app",
        "configArtefacts": [], "toolchain": null, "artifacts": [], "debug": {},
        "command": null, "env": {}, "envAppendPath": {}
      }]
    }""")
    assert plan.slices[0].command is None
