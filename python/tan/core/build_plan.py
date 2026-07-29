# SPDX-License-Identifier: Apache-2.0
"""The build-plan CONSUMER model (alp-sdk metadata/schemas/build-plan-v1.schema.json).

Strict producer / tolerant consumer: the required keys are enforced, the
optional-but-always-emitted ones default cleanly, and an unsupported
schemaVersion is REFUSED rather than silently hand-ported around."""
import json
from dataclasses import dataclass
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
    app_dir: str | None
    config_artefacts: list[dict[str, Any]]
    toolchain: Any
    artifacts: dict[str, Any]
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


def _artefacts(raw: Any, context: str) -> list[dict[str, Any]]:
    """Validate a `configArtefacts`/`sharedArtefacts` list at parse time
    (Rust's serde rejects a malformed `GeneratedFile` here too) so a
    malformed or null-valued entry fails as a coded `PlanParseError` instead
    of an uncaught `KeyError`/`AttributeError` reaching `plan_tokens`, which
    indexes `path`/`contents` unguarded."""
    if not isinstance(raw, list):
        raise PlanParseError("build.plan-invalid", f"`{context}` must be a list")
    for i, art in enumerate(raw):
        if (
            not isinstance(art, dict)
            or not isinstance(art.get("path"), str)
            or not isinstance(art.get("contents"), str)
        ):
            raise PlanParseError(
                "build.plan-invalid",
                f"`{context}[{i}]` must be an object with string `path` and `contents` fields",
            )
    return raw


def _is_legal_env_name(name: str) -> bool:
    """`os.environ`/`subprocess` reject an empty name or one containing `=`
    with `ValueError: illegal environment variable name` -- catch that shape
    here, at parse time, instead of at spawn time deep inside `execute.py`."""
    return name != "" and "=" not in name


def _command(raw: Any, context: str) -> SliceCommand | None:
    """Validate a slice's `command` (or `null`) at parse time. Rust's serde
    rejects a malformed `ToolStep` the same way -- an unguarded `cmd["tool"]`
    let a missing key escape as a bare `KeyError`, and a non-string
    `tool`/`args` element/`cwd` reached `subprocess.Popen` as an uncaught
    `TypeError`."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PlanParseError("build.plan-invalid", f"`{context}` must be an object or null")
    tool = raw.get("tool")
    if not isinstance(tool, str):
        raise PlanParseError("build.plan-invalid", f"`{context}.tool` must be a string")
    args = raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise PlanParseError("build.plan-invalid", f"`{context}.args` must be a list of strings")
    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise PlanParseError("build.plan-invalid", f"`{context}.cwd` must be a string or null")
    return SliceCommand(tool=tool, args=list(args), cwd=cwd)


def _env(raw: Any, context: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and _is_legal_env_name(k) and isinstance(v, str) for k, v in raw.items()
    ):
        raise PlanParseError(
            "build.plan-invalid",
            f"`{context}` must be an object mapping a legal env-var name to a string value",
        )
    return dict(raw)


def _env_append_path(raw: Any, context: str) -> dict[str, list[str]]:
    if not isinstance(raw, dict) or not all(
        isinstance(k, str)
        and _is_legal_env_name(k)
        and isinstance(v, list)
        and all(isinstance(x, str) for x in v)
        for k, v in raw.items()
    ):
        raise PlanParseError(
            "build.plan-invalid",
            f"`{context}` must be an object mapping a legal env-var name to a list of strings",
        )
    return {k: list(v) for k, v in raw.items()}


def _slice(raw: dict[str, Any], i: int) -> Slice:
    missing = [k for k in _REQUIRED_SLICE if k not in raw]
    if missing:
        raise PlanParseError(
            "build.plan-invalid",
            f"slice is missing required key(s): {', '.join(missing)}",
        )
    return Slice(
        core_id=raw["coreId"], backend=raw["backend"], build_dir=raw["buildDir"],
        app_dir=raw["appDir"],
        config_artefacts=_artefacts(raw["configArtefacts"], f"slices[{i}].configArtefacts"),
        toolchain=raw["toolchain"], artifacts=raw["artifacts"], debug=raw["debug"],
        command=_command(raw["command"], f"slices[{i}].command"),
        env=_env(raw["env"], f"slices[{i}].env"),
        env_append_path=_env_append_path(raw["envAppendPath"], f"slices[{i}].envAppendPath"),
    )


def parse_build_plan(text: str) -> BuildPlan:
    try:
        raw = json.loads(text)
    except ValueError as err:
        raise PlanParseError("build.plan-invalid", f"plan is not valid JSON: {err}") from err

    if not isinstance(raw, dict):
        raise PlanParseError("build.plan-invalid", "plan is not a JSON object")

    if "schemaVersion" not in raw:
        raise PlanParseError(
            "build.plan-invalid",
            "plan is missing required key(s): schemaVersion",
        )

    version = raw["schemaVersion"]
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
        slices=[_slice(s, i) for i, s in enumerate(raw["slices"])],
        shared_artefacts=_artefacts(raw["sharedArtefacts"], "sharedArtefacts"),
        warnings=raw["warnings"],
        sdk_version=raw.get("sdkVersion"), sdk_commit=raw.get("sdkCommit"),
        plan_path_mode=raw.get("planPathMode"), execution_policy=_policy(raw.get("executionPolicy")),
    )
