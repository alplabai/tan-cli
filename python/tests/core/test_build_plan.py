# SPDX-License-Identifier: Apache-2.0
import json
import os

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
        "appDir": "app", "configArtefacts": [], "toolchain": null, "artifacts": {},
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
        "configArtefacts": [], "toolchain": null, "artifacts": {}, "debug": {},
        "command": null, "env": {}, "envAppendPath": {}
      }]
    }""")
    assert plan.slices[0].command is None


def test_missing_schemaVersion_reports_build_plan_invalid():
    """Missing schemaVersion key (not wrong version) is a missing-required-key error,
    not a version-skew error."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(MINIMAL.replace('"schemaVersion": 1, ', ""))
    assert e.value.code == "build.plan-invalid"
    assert "schemaVersion" in e.value.message


def test_rejects_non_object_json_array():
    """Non-object top-level JSON (e.g., array) raises PlanParseError, not AttributeError."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan("[1, 2, 3]")
    assert e.value.code == "build.plan-invalid"
    assert "not a JSON object" in e.value.message


def test_rejects_non_object_json_null():
    """Non-object top-level JSON (e.g., null) raises PlanParseError, not AttributeError."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan("null")
    assert e.value.code == "build.plan-invalid"
    assert "not a JSON object" in e.value.message


def test_rejects_a_malformed_shared_artefact():
    """A malformed artefact entry (missing `contents`, non-string `path`, or
    not an object at all) must raise a coded PlanParseError, never a bare
    KeyError/AttributeError escaping later out of token substitution, which
    indexes `path`/`contents` unguarded."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(MINIMAL.replace('"sharedArtefacts": []', '"sharedArtefacts": [{"path": "x"}]'))
    assert e.value.code == "build.plan-invalid"
    assert "sharedArtefacts[0]" in e.value.message


def test_rejects_a_malformed_config_artefact():
    plan_json = """{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml",
      "sku": "S", "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "slices": [{
        "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1", "appDir": "app",
        "configArtefacts": [{"path": "x", "contents": null}], "toolchain": null, "artifacts": {},
        "debug": {}, "command": null, "env": {}, "envAppendPath": {}
      }]
    }"""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(plan_json)
    assert e.value.code == "build.plan-invalid"
    assert "slices[0].configArtefacts[0]" in e.value.message


def test_app_dir_null_parses_as_none():
    """appDir is nullable per the schema (a Yocto stock-image slice has
    none)."""
    plan = parse_build_plan("""{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml",
      "sku": "S", "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "slices": [{
        "coreId": "a55", "backend": "yocto", "buildDir": "build/a55", "appDir": null,
        "configArtefacts": [], "toolchain": null, "artifacts": {}, "debug": {},
        "command": null, "env": {}, "envAppendPath": {}
      }]
    }""")
    assert plan.slices[0].app_dir is None


def _plan_with_command(command_json: str) -> str:
    return f"""{{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml",
      "sku": "S", "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "slices": [{{
        "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1", "appDir": "app",
        "configArtefacts": [], "toolchain": null, "artifacts": {{}}, "debug": {{}},
        "command": {command_json}, "env": {{}}, "envAppendPath": {{}}
      }}]
    }}"""


@pytest.mark.parametrize(
    "command_json,fragment",
    [
        ('{"args": [], "cwd": null}', "command.tool"),  # I1: missing "tool" key -> was KeyError
        ('{"tool": null, "args": [], "cwd": null}', "command.tool"),  # I1: tool is null -> was TypeError
        ('{"tool": 5, "args": [], "cwd": null}', "command.tool"),  # I1: tool is an int -> was TypeError
        ('{"tool": "west", "args": [5], "cwd": null}', "command.args"),  # I1: args element is an int
        ('{"tool": "west", "args": [], "cwd": 5}', "command.cwd"),  # I1: cwd is an int -> was TypeError
        ('5', "command"),  # command itself is not an object or null
        # tan-cli#510 review, MAJOR 4: a relative path carrying a separator
        # is neither a bare identity to look up on PATH nor an
        # already-resolved absolute path -- `_resolve_tool` ("bin/sh", cwd
        # /usr) answered `resolved='bin/sh'` (checked against TAN's own
        # cwd), which the spawn then re-resolved against the CHILD's cwd --
        # two different directories deciding what "the tool" means, the
        # exact defect #510 exists to close. Refused at parse time instead.
        ('{"tool": "bin/sh", "args": [], "cwd": null}', "command.tool"),
    ],
)
def test_rejects_a_malformed_command(command_json, fragment):
    """I1: Rust's serde rejects a malformed `ToolStep` at parse time; an
    unguarded `cmd["tool"]`/non-string field let each of these escape
    `execute.py` as a bare KeyError/TypeError instead of a coded
    PlanParseError."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(_plan_with_command(command_json))
    assert e.value.code == "build.plan-invalid"
    assert fragment in e.value.message


# tan-cli#530: "absolute" for the parse-time check is deliberately THIS
# HOST's own notion (`pathlib`'s `ntpath` on Windows / `posixpath`
# elsewhere) -- decision (a) below. Build the accepted absolute path in the
# HOST's own convention, not a hardcoded POSIX string, so this test asserts
# the real invariant on every platform instead of only ever exercising the
# POSIX branch (the trap that made `/usr/bin/west` a Windows CI failure:
# `PureWindowsPath("/usr/bin/west").is_absolute()` is False -- no drive --
# even though `ntpath.isabs` said True on the 3.12 floor this was written
# against; from 3.13 on it says False too, tan-cli#1126. The trap is
# `is_absolute()`'s answer, which has not moved on any measured
# interpreter -- the `ntpath` aside was only ever the contrast).
_HOST_ABSOLUTE_TOOL = "C:\\tools\\west.exe" if os.name == "nt" else "/usr/bin/west"
_FOREIGN_ABSOLUTE_TOOL = "/usr/bin/west" if os.name == "nt" else "C:\\tools\\west.exe"


@pytest.mark.parametrize(
    "tool",
    [
        "west",  # a bare identity -- untouched
        _HOST_ABSOLUTE_TOOL,  # already-resolved absolute, in THIS host's own notion of absolute
    ],
)
def test_accepts_an_identity_or_an_absolute_tool(tool):
    """tan-cli#510 review, MAJOR 4's refusal is scoped to a RELATIVE path
    carrying a separator only -- a bare identity (no separator at all) and
    an already-absolute path (in the HOST's own sense of absolute) must
    both still parse."""
    plan = parse_build_plan(_plan_with_command(json.dumps({"tool": tool, "args": [], "cwd": None})))
    assert plan.slices[0].command.tool == tool


def test_refuses_a_foreign_os_absolute_tool():
    """tan-cli#530, decision (a): a build plan is a portable artefact, but an
    already-absolute `command.tool` is inherently host-specific -- nothing
    can re-root `/usr/bin/west` onto Windows, or `C:\\tools\\west.exe` onto
    POSIX, because the two conventions don't overlap. Refusing it HERE, at
    parse time, with a message naming the reason ("this host cannot resolve
    or spawn it") beats reaching `_resolve_tool`/`Popen` and failing with a
    bare `FileNotFoundError` that doesn't say why.

    No real emitting flow produces this shape today -- `tan build`'s own
    planner (`tan/planner/orchestrator.py::_slice_command`) only ever emits
    the bare identities `west`/`bitbake`/`cmake` for `tool`, never an
    absolute path -- so this refusal has no known real-plan casualty; it
    guards a hand-authored or foreign-host `--plan-from` file."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(
            _plan_with_command(json.dumps({"tool": _FOREIGN_ABSOLUTE_TOOL, "args": [], "cwd": None}))
        )
    assert e.value.code == "build.plan-invalid"
    assert "command.tool" in e.value.message
    assert "host" in e.value.message


def _plan_with_env(env_json: str) -> str:
    return f"""{{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml",
      "sku": "S", "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "slices": [{{
        "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1", "appDir": "app",
        "configArtefacts": [], "toolchain": null, "artifacts": {{}}, "debug": {{}},
        "command": null, "env": {env_json}, "envAppendPath": {{}}
      }}]
    }}"""


@pytest.mark.parametrize(
    "env_json,fragment",
    [
        ('{"X": 5}', "slices[0].env"),  # I1: env value is an int -> was TypeError at spawn
        ('{"BAD=NAME": "x"}', "slices[0].env"),  # I1: env key contains "=" -> was ValueError at spawn
        ('{"": "x"}', "slices[0].env"),  # empty env name -> same "illegal environment variable name"
    ],
)
def test_rejects_a_malformed_env(env_json, fragment):
    """I1: an env value that isn't a string, or a key that isn't a legal
    env-var name, used to reach `subprocess.Popen` unguarded and raise
    TypeError/ValueError there instead of failing at parse time."""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(_plan_with_env(env_json))
    assert e.value.code == "build.plan-invalid"
    assert fragment in e.value.message


def test_rejects_a_malformed_env_append_path():
    """Same validation class as `env`, applied to `envAppendPath` -- its
    values feed the same eventual `subprocess` env dict via
    `assemble_slice_env`/`apply_env_append`."""
    plan_json = """{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml",
      "sku": "S", "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "slices": [{
        "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1", "appDir": "app",
        "configArtefacts": [], "toolchain": null, "artifacts": {}, "debug": {},
        "command": null, "env": {}, "envAppendPath": {"PYTHONPATH": [5]}
      }]
    }"""
    with pytest.raises(PlanParseError) as e:
        parse_build_plan(plan_json)
    assert e.value.code == "build.plan-invalid"
    assert "slices[0].envAppendPath" in e.value.message


def test_valid_command_and_env_still_parse():
    """Guard against the validators above being too strict -- a well-formed
    command/env/envAppendPath (the shape every other test in this file
    already relies on) must still parse cleanly."""
    plan = parse_build_plan(_plan_with_command(
        '{"tool": "west", "args": ["build", "-b", "e1m_aen801"], "cwd": "build/c1"}'
    ))
    cmd = plan.slices[0].command
    assert cmd.tool == "west"
    assert cmd.args == ["build", "-b", "e1m_aen801"]
    assert cmd.cwd == "build/c1"
