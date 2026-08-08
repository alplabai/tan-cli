# SPDX-License-Identifier: Apache-2.0
"""The build-plan CONSUMER model (alp-sdk metadata/schemas/build-plan-v1.schema.json).

Strict producer / tolerant consumer: the required keys are enforced, the
optional-but-always-emitted ones default cleanly, and an unsupported
schemaVersion is REFUSED rather than silently hand-ported around."""
import json
from dataclasses import dataclass
from pathlib import Path
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

# The scalar fields Rust models as `String` / `Option<String>`. Typed here for
# the same reason serde types them there: unvalidated, a non-string escapes
# parse entirely and only detonates much later and far away -- `boardYaml` as
# an int reaches `str.replace` in the token pass, `sdkCommit` reaches
# `.strip()`, an unhashable `backend` reaches `in KNOWN_BACKENDS` -- as a bare
# AttributeError/TypeError in the executor's catch-all, i.e. reported as a tan
# bug (exit 5) when the truth is a malformed plan (`build.plan-invalid`,
# exit 1).
_REQUIRED_STR_TOP = ("generatedBy", "boardYaml", "sku", "buildRoot")
_OPTIONAL_STR_TOP = ("sdkVersion", "sdkCommit", "planPathMode")
_REQUIRED_STR_SLICE = ("coreId", "backend", "buildDir")
_OPTIONAL_STR_SLICE = ("appDir",)


def _json_type_name(value: Any) -> str:
    """serde_json's own spelling of an unexpected value's type, for the
    `invalid type: <this>, expected u32` message a malformed `schemaVersion`
    needs to match (tan-cli#491) -- measured against the real v0.4.1 oracle
    (`target/debug/tan build --plan-from <plan> --format json`) for every
    branch below: `true`/`false` -> "boolean `true`"/"boolean `false`", `1.0`
    -> "floating point `1.0`", `"1"` -> 'string "1"', `null` -> "null", `[1]`
    -> "sequence".

    Deliberately NOT shared with `system_manifest._serde_type_name`: that one
    serialises the same Python shapes for a DIFFERENT serde backend
    (`serde_yaml`, for `system-manifest.yaml`), which spells a JSON-null
    shape differently -- `system_manifest`'s own helper answers "unit value"
    there, where THIS format's real oracle answers plain "null" for the
    equivalent JSON input (measured, not assumed: the two backends are
    different Rust `Deserialize` implementations of the same abstract
    concept, not guaranteed to agree on wording)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"boolean `{str(value).lower()}`"
    if isinstance(value, str):
        return f'string "{value}"'
    if isinstance(value, int):
        return f"integer `{value}`"
    if isinstance(value, float):
        return f"floating point `{value}`"
    if isinstance(value, (list, tuple)):
        return "sequence"
    return "map"


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


def _require_strings(
    raw: dict[str, Any], required: tuple[str, ...], optional: tuple[str, ...], context: str
) -> None:
    """Type the scalar fields at parse time, where a mistyped one is still a
    plan defect rather than a crash somewhere downstream. `context` is `""` at
    the plan root and `slices[N]` inside a slice, so the message names the
    field the way the plan spells it."""
    prefix = f"{context}." if context else ""
    for key in required:
        if not isinstance(raw[key], str):
            raise PlanParseError("build.plan-invalid", f"`{prefix}{key}` must be a string")
    for key in optional:
        if raw.get(key) is not None and not isinstance(raw[key], str):
            raise PlanParseError("build.plan-invalid", f"`{prefix}{key}` must be a string or null")


def _action(raw: Any, context: str) -> PolicyAction | None:
    """One `executionPolicy` entry, refused as a coded parse error rather than
    a bare `ValueError` escaping the enum constructor.

    This is a FORWARD-COMPATIBILITY trap, not a typo guard: the day the SDK
    adds a fourth `executionPolicy` action, every plan carrying it hit
    `PolicyAction(raw)` -> `ValueError` -> the caller's catch-all, so every
    `tan build` against that SDK reported `build.internal-failure` at exit 5
    -- a tan bug -- for a plan this binary merely does not understand yet.
    Rust's serde refuses it as `build.plan-invalid` at exit 1; so does this."""
    if raw is None:
        return None
    try:
        return PolicyAction(raw)
    except ValueError as err:
        known = ", ".join(f"`{a.value}`" for a in PolicyAction)
        raise PlanParseError(
            "build.plan-invalid",
            f"`{context}` must be one of {known} -- got `{raw}`. Upgrade tan if the SDK "
            f"has added a newer execution policy.",
        ) from err


def _policy(raw: Any) -> ExecutionPolicy | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PlanParseError("build.plan-invalid", "`executionPolicy` must be an object or null")
    return ExecutionPolicy(
        unknown_backend=_action(raw.get("unknownBackend"), "executionPolicy.unknownBackend"),
        missing_tool=_action(raw.get("missingTool"), "executionPolicy.missingTool"),
        null_command=_action(raw.get("nullCommand"), "executionPolicy.nullCommand"),
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


def _warning(raw: Any) -> dict[str, Any]:
    """Validate one `warnings[]` entry at parse time (tan-cli#491): the whole
    list used to pass through UNVALIDATED past a bare `isinstance(list)`
    check on the list itself, so a non-object entry (a bare string, `null`)
    parsed clean and reached the envelope's `data.warnings` verbatim --
    where the real oracle refuses the identical plan outright
    (`build.plan-invalid`, "invalid type: string \\"oops\\", expected struct
    PlanWarning"). `data.warnings` is copied straight from this list into
    every `tan build --plan-from ... --format json` envelope (`build_cmd.
    py`), so an unvalidated entry reached a consumer's `.map(w =>
    w.code/.coreId/.message)` as `undefined` (a bare string) or a throw.

    Every shape below is MEASURED against the real v0.4.1 oracle, not read
    off the published JSON schema, which over-declares: it marks `coreId`
    `required` alongside `code`/`message`, but the shipped Rust struct
    accepts it ABSENT or explicitly `null` -- both parse `ok:true` on the
    real binary -- so `coreId` is `Option<String>` there, and this validator
    matches the ORACLE. An extra key is likewise NOT rejected (measured:
    an entry with `code`/`coreId`/`message` plus an unrecognised `extra`
    parses clean and echoes `extra` verbatim) despite the schema's
    `additionalProperties:false` -- `PlanWarning` is not `#[serde(deny_
    unknown_fields)]`. Checked here: `code`/`message` present and
    string-typed, `coreId` string-or-absent-or-null, nothing more."""
    if not isinstance(raw, dict):
        raise PlanParseError(
            "build.plan-invalid",
            f"plan is not valid JSON: invalid type: {_json_type_name(raw)}, "
            f"expected struct PlanWarning",
        )
    for key in ("code", "message"):
        if key not in raw:
            raise PlanParseError(
                "build.plan-invalid", f"plan is not valid JSON: missing field `{key}`"
            )
        if not isinstance(raw[key], str):
            raise PlanParseError(
                "build.plan-invalid",
                f"plan is not valid JSON: invalid type: {_json_type_name(raw[key])}, "
                f"expected a string",
            )
    core_id = raw.get("coreId")
    if core_id is not None and not isinstance(core_id, str):
        raise PlanParseError(
            "build.plan-invalid",
            f"plan is not valid JSON: invalid type: {_json_type_name(core_id)}, "
            f"expected a string",
        )
    return raw


def _foreign_os_absolute(tool: str) -> str | None:
    """Return a short name for the OTHER OS's path convention if `tool`
    looks absolute under it, else `None`.

    `Path(tool).is_absolute()` (below) answers relative to THIS host's own
    `pathlib` flavour: `/usr/bin/west` is absolute under `PurePosixPath` but
    NOT under `PureWindowsPath` (no drive), and `C:\\tools\\west.exe` is
    absolute under `PureWindowsPath` but NOT under `PurePosixPath` (no
    leading `/`, and `pathlib` never special-cases a drive-letter prefix on
    POSIX). Used only to name the reason precisely in the refusal message
    below -- a bare "relative path" reading is misleading for a path that IS
    absolute, just under the wrong OS's rules."""
    if tool.startswith("/"):
        return "POSIX"
    if len(tool) >= 3 and tool[1] == ":" and tool[2] in "\\/" and tool[0].isalpha():
        return "Windows"
    if tool.startswith("\\\\"):
        return "Windows"
    return None


def _is_legal_env_name(name: str) -> bool:
    """`os.environ`/`subprocess` reject an empty name or one containing `=`
    with `ValueError: illegal environment variable name` -- catch that shape
    here, at parse time, instead of at spawn time deep inside `execute.py`.

    Also refuses an embedded NUL byte (tan-cli#491): `subprocess.Popen`
    raises `ValueError: embedded null byte` for a NUL anywhere in an env
    name, value, `envAppendPath` entry, `command.args` element, or
    `command.cwd` -- the SAME misclassification shape this function's own
    docstring already describes for `=`/empty, just a second illegal
    character `os.environ` rejects. Reported here at parse time
    (`build.plan-invalid`) rather than at spawn time several frames inside
    `execute.py`, where the executor's generic catch-all turned it into
    `build.internal-failure` (exit 5, "a tan bug") for what is actually a
    malformed plan. See `_no_nul` below for the sibling check applied to
    every OTHER field this same `ValueError` is reachable from -- a NUL is
    not scoped to env names alone."""
    return name != "" and "=" not in name and "\x00" not in name


def _no_nul(value: str) -> bool:
    """Whether `value` is safe to hand to `os.environ`/`subprocess.Popen`,
    which raise `ValueError: embedded null byte` for a NUL anywhere in an
    env value, an `envAppendPath` entry, a `command.args` element, or
    `command.cwd` (tan-cli#491) -- four vectors beyond the env-NAME case
    `_is_legal_env_name` already covered before this fix, all reachable only
    through a hand-authored or corrupted `--plan-from` file (the SDK planner
    never emits a NUL). `command.tool` is deliberately NOT checked here:
    `_tool_is_available`'s `shutil.which` already degrades a NUL-carrying
    tool name gracefully to a `skipped` slice ("tool `...` not found") rather
    than raising, so adding a parse-time guard there would only turn one
    already-graceful outcome into another, not fix a misclassification."""
    return "\x00" not in value


def _validate_tool_shape(tool: str, context: str) -> None:
    """Refuse a `command.tool` that is neither a bare identity nor an
    already-resolved absolute path -- split out of `_command` so that
    function stays under the repo's long-function budget.

    tan-cli#510 review, MAJOR 4: `command.tool` is an IDENTITY per ADR-0020
    (a bare name to look up, e.g. `west`) or an ALREADY-RESOLVED absolute
    path a producer computed -- never a relative path. A relative path
    carrying a separator (`bin/sh`) is neither: `_resolve_tool`
    (`build/execute.py`) answers `Path(tool).is_absolute()` False for it, so
    it falls into the bare-identity PATH search, which checks it against
    THIS process's cwd -- but the spawn below hands it to
    `subprocess.Popen(cwd=spawn_cwd)`, resolved against the CHILD's cwd
    instead. Two different directories deciding what "the tool" even means
    is the exact defect this issue exists to close; refused here, at parse
    time, rather than reaching that check/spawn split at all. A bare `west`
    (no separator) is untouched -- it is exactly the identity shape this
    refusal is not about.

    tan-cli#530, decision (a): "absolute" is deliberately THIS HOST's own
    notion (`Path(tool).is_absolute()`) -- an already-absolute
    `command.tool` is inherently host-specific even though the plan around
    it is portable (see `_foreign_os_absolute`'s docstring for the full
    reasoning). No emitting flow produces this shape today (the SDK planner
    only emits bare `west`/`bitbake`/`cmake`); this guards a hand-authored
    or foreign-host `--plan-from` file."""
    if Path(tool).is_absolute() or ("/" not in tool and "\\" not in tool):
        return
    foreign = _foreign_os_absolute(tool)
    if foreign is not None:
        raise PlanParseError(
            "build.plan-invalid",
            f"`{context}.tool` (`{tool}`) is a {foreign}-absolute path, not "
            f"executable on this host -- a build plan is a portable artefact, "
            f"but an absolute `command.tool` is inherently host-specific: "
            f"`{tool}` is only absolute under {foreign} path rules, and this "
            f"host cannot resolve or spawn it. `command.tool` must be either "
            f"a bare identity to look up on PATH (e.g. `west`) or a path this "
            f"host itself recognises as absolute.",
        )
    raise PlanParseError(
        "build.plan-invalid",
        f"`{context}.tool` (`{tool}`) is a relative path, not an identity -- "
        f"`command.tool` must be either a bare identity to look up on PATH "
        f"(e.g. `west`) or an already-resolved absolute path; a relative path "
        f"carrying a separator is neither.",
    )


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
    _validate_tool_shape(tool, context)
    args = raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) and _no_nul(a) for a in args):
        raise PlanParseError(
            "build.plan-invalid",
            f"`{context}.args` must be a list of strings with no embedded NUL byte",
        )
    cwd = raw.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not _no_nul(cwd)):
        raise PlanParseError(
            "build.plan-invalid",
            f"`{context}.cwd` must be a string or null with no embedded NUL byte",
        )
    return SliceCommand(tool=tool, args=list(args), cwd=cwd)


def _env(raw: Any, context: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and _is_legal_env_name(k) and isinstance(v, str) and _no_nul(v)
        for k, v in raw.items()
    ):
        raise PlanParseError(
            "build.plan-invalid",
            f"`{context}` must be an object mapping a legal env-var name to a string value "
            f"with no embedded NUL byte",
        )
    return dict(raw)


def _env_append_path(raw: Any, context: str) -> dict[str, list[str]]:
    if not isinstance(raw, dict) or not all(
        isinstance(k, str)
        and _is_legal_env_name(k)
        and isinstance(v, list)
        and all(isinstance(x, str) and _no_nul(x) for x in v)
        for k, v in raw.items()
    ):
        raise PlanParseError(
            "build.plan-invalid",
            f"`{context}` must be an object mapping a legal env-var name to a list of strings "
            f"with no embedded NUL byte",
        )
    return {k: list(v) for k, v in raw.items()}


def _slice(raw: Any, i: int) -> Slice:
    if not isinstance(raw, dict):
        raise PlanParseError("build.plan-invalid", f"`slices[{i}]` must be an object")
    missing = [k for k in _REQUIRED_SLICE if k not in raw]
    if missing:
        raise PlanParseError(
            "build.plan-invalid",
            f"slice is missing required key(s): {', '.join(missing)}",
        )
    _require_strings(raw, _REQUIRED_STR_SLICE, _OPTIONAL_STR_SLICE, f"slices[{i}]")
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
    # `bool` excluded explicitly, BEFORE the `int` check: `True == 1` in
    # Python, so a bare `version != SUPPORTED_SCHEMA_VERSION` let
    # `"schemaVersion": true` silently pass as version 1 and get DISPATCHED
    # -- `BuildPlan.schema_version: int` ending up holding an actual `bool` --
    # where the oracle refuses it outright (serde's `as_u64()` rejects a JSON
    # boolean). `1.0 == 1` is the same trap for a float. Every non-int (and
    # every bool) is refused HERE, as `build.plan-invalid` with serde's own
    # "invalid type" wording, matching the oracle's real refusal of a
    # malformed `schemaVersion` -- rather than falling through to the
    # `!= SUPPORTED_SCHEMA_VERSION` branch below, which used to catch this
    # shape too but under the WRONG code (`build.plan-unsupported-schema`)
    # and a self-contradictory message ("unsupported ... (this tan supports
    # 1)" for a plan whose version WAS 1, just not typed as one) -- see
    # `tan/core/bootstrap.py::parse_bootstrap_manifest` and
    # `tan/core/system_manifest.py` for the same `isinstance(..., bool)`
    # guard already applied to their own sibling `schemaVersion`s.
    if isinstance(version, bool) or not isinstance(version, int):
        raise PlanParseError(
            "build.plan-invalid",
            f"plan is not valid JSON: invalid type: {_json_type_name(version)}, expected u32",
        )
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

    _require_strings(raw, _REQUIRED_STR_TOP, _OPTIONAL_STR_TOP, "")

    if not isinstance(raw["slices"], list):
        raise PlanParseError("build.plan-invalid", "`slices` must be a list")
    # `warnings` is a CONTRACT SURFACE, not merely an internal field: `tan
    # build` copies it verbatim into the envelope's `data.warnings`, and a
    # consumer doing `data.warnings ?? []` then `.map()` breaks on a string.
    # Rust types it `Vec<PlanWarning>` and refuses the plan; the list check is
    # the part of that this port needs -- the ENTRIES stay untyped on purpose,
    # since the schema says new warning codes may appear without a
    # schemaVersion bump, so a consumer must not treat them as a closed set.
    if not isinstance(raw["warnings"], list):
        raise PlanParseError("build.plan-invalid", "`warnings` must be a list")

    return BuildPlan(
        schema_version=version, generated_by=raw["generatedBy"], board_yaml=raw["boardYaml"],
        sku=raw["sku"], build_root=raw["buildRoot"],
        slices=[_slice(s, i) for i, s in enumerate(raw["slices"])],
        shared_artefacts=_artefacts(raw["sharedArtefacts"], "sharedArtefacts"),
        warnings=[_warning(w) for w in raw["warnings"]],
        sdk_version=raw.get("sdkVersion"), sdk_commit=raw.get("sdkCommit"),
        plan_path_mode=raw.get("planPathMode"), execution_policy=_policy(raw.get("executionPolicy")),
    )
