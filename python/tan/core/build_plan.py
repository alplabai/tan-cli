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

#: The oracle types `schemaVersion` as a Rust `u32`, so the accepted domain is
#: exactly `[0, 2**32 - 1]` -- established by RUNNING it: `-1` and
#: `4294967296` are refused as `invalid value: integer ..., expected u32`,
#: while `4294967295` gets through the type check and lands on the
#: version-skew message instead.
_U32_MAX = 0xFFFFFFFF

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
    warnings: list[dict[str, Any]]
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


def _warnings(raw: Any) -> list[dict[str, Any]]:
    """Validate `warnings` -- the LIST and now also every ENTRY.

    `warnings` is a CONTRACT SURFACE, not merely an internal field: `tan
    build` copies it verbatim into the envelope's `data.warnings`, and a
    consumer doing `data.warnings ?? []` then `.map(w => w.code)` breaks on a
    string. An earlier version checked only that the value was a list and
    left the ENTRIES untyped "on purpose", reasoning that new warning codes
    may appear without a schemaVersion bump. Running the oracle refutes the
    second half: `{"warnings": ["oops"]}` is refused as `invalid type: string
    "oops", expected struct PlanWarning`. The forward compatibility is in the
    CODES being open, not the entry SHAPE -- measured, `PlanWarning` is a
    3-field struct: `code` and `message` are required strings, `coreId` is an
    `Option<String>` (null and absent both fine), and an unknown key such as
    `zzz` is accepted AND came back untouched in `data.warnings`. `code` is
    checked before `message`, the order serde reports them in.

    Deliberately NOT replicated: the oracle also accepts a 3-element ARRAY per
    entry (serde's tuple form for the same struct -- hence its "expected
    struct PlanWarning with 3 elements" wording). No producer emits that, the
    schema says objects, and honouring it would mean carrying a second entry
    shape through every consumer of `data.warnings`."""
    if not isinstance(raw, list):
        raise PlanParseError("build.plan-invalid", "`warnings` must be a list")
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise PlanParseError(
                "build.plan-invalid",
                f"`warnings[{i}]` must be an object with string `code` and `message` fields",
            )
        for key in ("code", "message"):
            if key not in entry:
                raise PlanParseError(
                    "build.plan-invalid", f"`warnings[{i}]` is missing required key `{key}`"
                )
            if not isinstance(entry[key], str):
                raise PlanParseError(
                    "build.plan-invalid", f"`warnings[{i}].{key}` must be a string"
                )
        if entry.get("coreId") is not None and not isinstance(entry["coreId"], str):
            raise PlanParseError(
                "build.plan-invalid", f"`warnings[{i}].coreId` must be a string or null"
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
    """`os.environ`/`subprocess` reject an empty name, one containing `=`
    (`ValueError: illegal environment variable name`) or one containing an
    embedded NUL (`ValueError: embedded null byte`) -- catch that shape here,
    at parse time, instead of at spawn time deep inside `execute.py`."""
    return name != "" and "=" not in name and "\0" not in name


def _reject_nul(value: str, context: str) -> None:
    """Refuse an embedded NUL in a string that is headed for `subprocess`.

    Every C-level exec/environ interface takes NUL-terminated strings, so
    CPython refuses the whole call with a bare `ValueError: embedded null
    byte`. Measured before this fix, on `tan build --plan-from <f>
    --execute`, a NUL in `env` (name OR value), `envAppendPath`, `command.args`
    or `command.cwd` each produced `exit 5 build.internal-failure:
    ValueError: embedded null byte` -- tan reporting ITSELF as broken for
    what is a malformed plan. That is the same failure mode, and the same
    remedy, as the `=`/empty env names `_is_legal_env_name` already refuses
    two lines up.

    ORACLE NOTE -- a MEASURED DIVERGENCE, stated plainly. v0.4.1 has no
    `--execute` flag, so the oracle has no observable behaviour at all for a
    NUL-bearing plan on the spawn path; the only path it can reach is
    `--plan` display, where it echoes the NUL back at exit 0. This parse runs
    on that path too, so `tan build --plan --plan-from <nul-plan>` now exits
    1 here where the oracle exits 0. That trade is taken deliberately, for
    the same reason and in the same shape as the `=`/empty env names above,
    which already diverge from the oracle in exactly this way (measured: the
    oracle accepts `{"F=OO": "bar"}` at exit 0; this module has refused it
    since before this change). The alternative is keeping a
    `build.internal-failure` exit 5 on the only path where the value is ever
    USED.

    The divergence is deliberately NARROW. Where the oracle DOES have a
    measured answer, it is honoured rather than pre-empted:

      * `configArtefacts[].path` -- oracle exits 3 with
        `build.materialise-failed`, "file name contained an unexpected NUL
        byte". Refusing it here would replace that specific error with a
        vaguer one, so it is left to the materialiser.
      * `command.tool` -- already answered downstream as `build.missing-tool`
        at exit 1, because a NUL-bearing path can never exist on disk.
      * `buildDir` -- no crash; the slice fails on its own terms.
    """
    if "\0" in value:
        raise PlanParseError(
            "build.plan-invalid",
            f"`{context}` contains an embedded NUL byte -- it cannot be passed to a "
            f"process or its environment. This is a malformed plan; re-emit it.",
        )


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
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise PlanParseError("build.plan-invalid", f"`{context}.args` must be a list of strings")
    for i, arg in enumerate(args):
        _reject_nul(arg, f"{context}.args[{i}]")
    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise PlanParseError("build.plan-invalid", f"`{context}.cwd` must be a string or null")
    if cwd is not None:
        _reject_nul(cwd, f"{context}.cwd")
    return SliceCommand(tool=tool, args=list(args), cwd=cwd)


def _env(raw: Any, context: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and _is_legal_env_name(k) and isinstance(v, str) for k, v in raw.items()
    ):
        raise PlanParseError(
            "build.plan-invalid",
            f"`{context}` must be an object mapping a legal env-var name to a string value",
        )
    for key, value in raw.items():
        _reject_nul(value, f"{context}.{key}")
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
    for key, values in raw.items():
        for i, value in enumerate(values):
            _reject_nul(value, f"{context}.{key}[{i}]")
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


def _schema_version(raw: dict[str, Any]) -> int:
    """Read and TYPE `schemaVersion`, then apply the version-skew guard.

    The type check is not pedantry, it is the whole defect (tan-cli#491,
    defect 9). This was a bare `version != SUPPORTED_SCHEMA_VERSION`, and in
    Python `True == 1` and `1.0 == 1` are both true -- so a plan declaring
    `"schemaVersion": true` or `"schemaVersion": 1.0` compared EQUAL to 1 and
    was accepted as a v1 plan. Measured, the oracle refuses both outright:
    `invalid type: boolean `true`, expected u32` and `invalid type: floating
    point `1.0`, expected u32`.

    `isinstance(True, int)` is True (bool subclasses int), so the bool test
    must come first and cannot be folded into the int test.

    The two codes are a deliberate split the oracle does not make -- it
    answers every case below with `build.plan-invalid`. A value that is not a
    u32 AT ALL is a malformed plan and keeps that code; a well-typed u32 that
    simply is not 1 is a version SKEW, which this port reports as
    `build.plan-unsupported-schema` so a consumer can tell "your SDK and your
    tan disagree" from "this file is garbage". That split predates this fix
    and is preserved exactly; what changes is which values reach it."""
    if "schemaVersion" not in raw:
        raise PlanParseError(
            "build.plan-invalid",
            "plan is missing required key(s): schemaVersion",
        )
    version = raw["schemaVersion"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise PlanParseError(
            "build.plan-invalid",
            f"`schemaVersion` must be an integer -- got `{version!r}`. A build plan "
            f"declares its schema as a whole number (the SDK emits "
            f"{SUPPORTED_SCHEMA_VERSION}); re-emit the plan.",
        )
    if not 0 <= version <= _U32_MAX:
        raise PlanParseError(
            "build.plan-invalid",
            f"`schemaVersion` ({version}) is outside the representable range "
            f"0..{_U32_MAX}; re-emit the plan.",
        )
    if version != SUPPORTED_SCHEMA_VERSION:
        raise PlanParseError(
            "build.plan-unsupported-schema",
            f"unsupported build-plan schemaVersion `{version}` (this tan supports "
            f"{SUPPORTED_SCHEMA_VERSION}) -- refusing rather than falling back to "
            f"hand-ported behaviour. Upgrade tan, or re-emit the plan.",
        )
    return version


def parse_build_plan(text: str) -> BuildPlan:
    try:
        raw = json.loads(text)
    except ValueError as err:
        raise PlanParseError("build.plan-invalid", f"plan is not valid JSON: {err}") from err

    if not isinstance(raw, dict):
        raise PlanParseError("build.plan-invalid", "plan is not a JSON object")

    version = _schema_version(raw)

    missing = [k for k in _REQUIRED_TOP if k not in raw]
    if missing:
        raise PlanParseError(
            "build.plan-invalid",
            f"plan is missing required key(s): {', '.join(missing)}",
        )

    _require_strings(raw, _REQUIRED_STR_TOP, _OPTIONAL_STR_TOP, "")

    if not isinstance(raw["slices"], list):
        raise PlanParseError("build.plan-invalid", "`slices` must be a list")
    # Validated HERE, ahead of the slices, purely to keep the pre-existing
    # report order: a plan with both a bad warning and a bad slice named the
    # warning first before this change, and still does.
    warnings = _warnings(raw["warnings"])

    return BuildPlan(
        schema_version=version, generated_by=raw["generatedBy"], board_yaml=raw["boardYaml"],
        sku=raw["sku"], build_root=raw["buildRoot"],
        slices=[_slice(s, i) for i, s in enumerate(raw["slices"])],
        shared_artefacts=_artefacts(raw["sharedArtefacts"], "sharedArtefacts"),
        warnings=warnings,
        sdk_version=raw.get("sdkVersion"), sdk_commit=raw.get("sdkCommit"),
        plan_path_mode=raw.get("planPathMode"), execution_policy=_policy(raw.get("executionPolicy")),
    )
