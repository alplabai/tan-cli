# SPDX-License-Identifier: Apache-2.0
"""The build-plan CONSUMER model (alp-sdk metadata/schemas/build-plan-v1.schema.json).

Strict producer / tolerant consumer: the required keys are enforced, the
optional-but-always-emitted ones default cleanly, and an unsupported
schemaVersion is REFUSED rather than silently hand-ported around."""
import json
from dataclasses import dataclass, field
from typing import Any

from tan.core.plan_exec import ExecutionPolicy, PolicyAction

SUPPORTED_SCHEMA_VERSION = 1

_REQUIRED_TOP = (
    "schemaVersion", "generatedBy", "boardYaml", "sku",
    "buildRoot", "slices", "sharedArtefacts", "warnings",
)
_REQUIRED_SLICE = (
    "coreId", "backend", "buildDir", "appDir", "configArtefacts",
    "toolchain", "artifacts", "debug", "command", "env", "envAppendPath",
)


class PlanParseError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SliceCommand:
    tool: str
    args: list[str]
    cwd: str | None


@dataclass(frozen=True)
class Slice:
    core_id: str
    backend: str
    build_dir: str
    app_dir: str
    config_artefacts: list[dict[str, Any]]
    toolchain: Any
    artifacts: list[Any]
    debug: dict[str, Any]
    command: SliceCommand | None
    env: dict[str, str]
    env_append_path: dict[str, list[str]]


@dataclass(frozen=True)
class BuildPlan:
    schema_version: int
    generated_by: str
    board_yaml: str
    sku: str
    build_root: str
    slices: list[Slice]
    shared_artefacts: list[dict[str, Any]]
    warnings: list[Any]
    sdk_version: str | None = None
    sdk_commit: str | None = None
    plan_path_mode: str | None = None
    execution_policy: ExecutionPolicy | None = None


def _action(raw: Any) -> PolicyAction | None:
    return PolicyAction(raw) if raw is not None else None


def _policy(raw: dict[str, Any] | None) -> ExecutionPolicy | None:
    if raw is None:
        return None
    return ExecutionPolicy(
        unknown_backend=_action(raw.get("unknownBackend")),
        missing_tool=_action(raw.get("missingTool")),
        null_command=_action(raw.get("nullCommand")),
    )


def _slice(raw: dict[str, Any]) -> Slice:
    missing = [k for k in _REQUIRED_SLICE if k not in raw]
    if missing:
        raise PlanParseError(
            "build.plan-invalid",
            f"slice is missing required key(s): {', '.join(missing)}",
        )
    cmd = raw["command"]
    return Slice(
        core_id=raw["coreId"], backend=raw["backend"], build_dir=raw["buildDir"],
        app_dir=raw["appDir"], config_artefacts=raw["configArtefacts"],
        toolchain=raw["toolchain"], artifacts=raw["artifacts"], debug=raw["debug"],
        command=None if cmd is None else SliceCommand(
            tool=cmd["tool"], args=list(cmd.get("args", [])), cwd=cmd.get("cwd")
        ),
        env=dict(raw["env"]), env_append_path={k: list(v) for k, v in raw["envAppendPath"].items()},
    )


def parse_build_plan(text: str) -> BuildPlan:
    try:
        raw = json.loads(text)
    except ValueError as err:
        raise PlanParseError("build.plan-invalid", f"plan is not valid JSON: {err}") from err

    version = raw.get("schemaVersion")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise PlanParseError(
            "build.plan-unsupported-schema",
            f"unsupported build-plan schemaVersion `{version}` (this tan supports "
            f"{SUPPORTED_SCHEMA_VERSION}) -- refusing rather than falling back to "
            f"hand-ported behaviour. Upgrade tan, or re-emit the plan.",
        )

    missing = [k for k in _REQUIRED_TOP if k not in raw]
    if missing:
        raise PlanParseError(
            "build.plan-invalid",
            f"plan is missing required key(s): {', '.join(missing)}",
        )

    return BuildPlan(
        schema_version=version, generated_by=raw["generatedBy"], board_yaml=raw["boardYaml"],
        sku=raw["sku"], build_root=raw["buildRoot"],
        slices=[_slice(s) for s in raw["slices"]],
        shared_artefacts=raw["sharedArtefacts"], warnings=raw["warnings"],
        sdk_version=raw.get("sdkVersion"), sdk_commit=raw.get("sdkCommit"),
        plan_path_mode=raw.get("planPathMode"), execution_policy=_policy(raw.get("executionPolicy")),
    )
