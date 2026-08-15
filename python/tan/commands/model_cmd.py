# SPDX-License-Identifier: Apache-2.0
"""`tan model build`/`tan model doctor` -- compile + package `board.yaml`'s
`models:` block into `.alpmodel` packages, and report NPU-compiler toolchain
availability.

Port of `scripts/alp_cli/model.py` (51 lines): the board.yaml discovery,
per-model source/compile-option path resolution, and the `built <path>`
summary all move here, in-process, exactly as they read there. Also
in-process, since ADR-0028: `tan.model.build.build_model` itself -- the
compiler-adapter engine (CPU/Vela/DRP-AI/DeepX) that does the actual work,
relocated verbatim from alp-sdk's `scripts/alp_model/` into `tan.model`. This
command calls it directly, per model, no subprocess involved.

Earlier revisions of this file spawned a `python -c` driver under the SDK
checkout's own interpreter, on the premise that the vendor NPU-compiler
tooling was only reachable from that checkout's Python environment. That
premise was never accurate: every adapter resolves its own tool with
`shutil.which` and spawns an external binary -- `adapters/ethos_u.py`
(`shutil.which("vela")`, `cmd = ["vela", ...]`), `adapters/deepx.py`
(`shutil.which("dxcom")`, `cmd = ["dxcom", "-m", ...]`), `adapters/drpai.py`
likewise -- so what building a model actually needs is `vela`/`dxcom` on
PATH, a host fact, not a checkout fact. The one genuine Python-environment
dependency, `adapters/ethos_u.py`'s `_vela_version()`, already degrades
gracefully (`except PackageNotFoundError: return "vela"`), costing a less
precise `compiler_version` string and nothing else. **This module still
resolves the SDK root** (via `resolve_metadata_sdk_root`) -- not for its
Python, but because `tan.model.build.build_model` reads alp-sdk's
`metadata/**` at call time (ADR-0017: metadata stays in alp-sdk).

This is a REAL implementation, not a forward: it never spawns `python -m
alp_cli`, so `alp_cli` stops being load-bearing for `tan model` (the point of
this port -- see `crates/tan-cli/src/commands/sdk_cli.rs`'s module doc for
what it is replacing).

**Deliberate divergence 1 from the oracle survives ADR-0028**: `alp_cli/
model.py` has no try/except around `build_model()` at all, so a build failure
(e.g. "no blob compiled for model") tracebacks the whole click command. Every
command in this port instead resolves to a coded issue, never a traceback
(the established rule -- see `generate_cmd`'s module doc) -- so a per-model
failure here is caught around the direct `build_model()` call and reported as
a `model.build-failed` issue, and the run continues to the next model rather
than aborting the whole batch.

**Deliberate divergence 2 from the oracle is RETIRED with the driver it
existed for.** It used to guard against a spawned driver that exited 0 having
silently produced no result for a declared model -- a failure mode only a
subprocess boundary could produce. With `build_model()` called directly,
in-process, per model, there is no boundary across which a result can go
missing: either the call returns a path (recorded as built) or raises
(caught and reported as `model.build-failed`). There is no third outcome left
to guard against, so the guard is gone, not silently dropped.

**`doctor` is the diagnostic half ADR-0028 makes tan responsible for.** With
the compiler-adapter engine relocated here, tan is the customer's only
surface for "why did my compile produce nothing" -- `model doctor` answers
that ahead of a build by reporting, per registered backend, whether its
toolchain is installed and, if not, an ACTIONABLE reason (`tan.core.
model_doctor`). It is READ-ONLY: every adapter's `is_available()` is a
`shutil.which`/env-var check today, never a spawn, and `_run_doctor` below
never calls one that isn't. `_run_doctor` does not always take that
`is_available()` verdict verbatim, though: `deepx_dxm1` and `drpai` each get
a NARROWER doctor-side probe (`_deepx_dxm1_status`/`_drpai_status` below)
that checks what `compile()` actually reads/spawns, because each adapter's
`is_available()` ORs in a signal `compile()` never acts on -- reporting a row
green there meant the very next `model build` failed anyway. Still read-only,
still never a spawn. An unavailable toolchain is the expected, common
case, not a failure -- `_run_doctor` always resolves to `ExitCode.SUCCESS`;
reporting absence is the feature, not a reason to fail the run. A board.yaml
and a resolvable alp-sdk checkout are NOT required: this command intends to
run on the exact host where something else already broke, so an unresolved
`--sdk-root` is folded into a `model.doctor-sdk-unresolved` WARNING rather
than the `model.sdk-root-unresolved` ERROR `build` uses for the same
situation -- the backend rows below it are unaffected by a missing SDK either
way, since none of them reads `metadata/**`.

`tan model check` -- a fit verdict against a board's declared models and
`metadata/npu_ops/` -- is a DIFFERENT, not-yet-approved command and is not
implemented here.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import typer

from tan.commands.build_output import (
    ProjectContext,
    resolve_metadata_sdk_root,
    resolve_project_context,
)
from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS, sdk_resolution_issues
from tan.core.global_flags import accept_global_flags
from tan.core.model_doctor import backend_row, registry_backends
from tan.core.shapes import rejected_sdk_root_message
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.model.adapters.drpai import _compiler_version as _drpai_compiler_version
from tan.model.adapters.drpai import _tvm_home as _drpai_tvm_home
from tan.model.adapters.ethos_u import _vela_version
from tan.model.build import _ADAPTERS, build_model
from tan.output_format import FORMAT_HELP, OutputFormat

#: `data.schemaVersion` for `build`'s payload.
DATA_SCHEMA_VERSION = "1"

#: `data.schemaVersion` for `doctor`'s payload -- versioned independently of
#: `build`'s (a different `data` shape entirely: `backends[]`, not `built[]`).
DOCTOR_DATA_SCHEMA_VERSION = "1"

#: `SUBCOMMANDS` names every subcommand this command accepts, in the order the
#: unknown-subcommand refusal lists them. `tan model check` (a fit verdict
#: against `metadata/npu_ops/`) is a separate, not-yet-approved command and is
#: deliberately absent.
SUBCOMMANDS = ("build", "doctor")


class ModelError(Exception):
    """A refusal whose issue code and exit code are already decided."""

    def __init__(self, code: str, message: str, exit_code: ExitCode) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


#: Compile-opt keys that name a filesystem path (resolved relative to
#: board.yaml). Not every value in a models[].compile.<backend> block is a
#: path -- e.g. drpai's input_shape ("1,3,224,224"), input_name ("images") and
#: product ("V2N") are opaque strings that must reach the adapter unchanged
#: (alp-sdk#1271: resolving them as paths corrupted a genuine shape string into
#: a filesystem path, which then made the adapter's own shape check misfire).
_PATH_OPT_KEYS = {"config", "calibration", "images", "spec"}


def _resolve_compile(block: dict | None, base: Path) -> dict | None:
    """Port of `model.py::_resolve_compile`: resolve known path-valued keys
    in each per-backend compile block to an absolute path relative to the
    `board.yaml` dir; every other value (shape strings, node names, product
    ids, ...) passes through unchanged (alp-sdk#1271)."""
    if not block:
        return None
    return {
        backend: {
            k: (str((base / v).resolve()) if k in _PATH_OPT_KEYS and isinstance(v, str) else v)
            for k, v in (opts or {}).items()
        }
        for backend, opts in block.items()
    }


def _load_board(path: Path) -> dict[str, Any]:
    """`board.yaml` as a dict, or a `ModelError` for every way that can fail --
    missing file, bad encoding, not YAML, not a mapping. `yaml.safe_load`,
    matching the oracle's own parse exactly (unlike `system_manifest`'s
    core-schema loader, there is no serde_yaml parity requirement here)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ModelError(
            "model.board-yaml-missing",
            f"board.yaml not found at {path}: {err}",
            ExitCode.VALIDATION_FAILURE,
        ) from err
    except UnicodeDecodeError as err:
        # tan-cli#396: `UnicodeDecodeError` is a `ValueError`, NOT an
        # `OSError`, so `except OSError` alone could never catch it -- one
        # undecodable byte in the customer's own board.yaml escaped this
        # whole command as a traceback: measured `exitCode: 5` /
        # `model.internal-failure` / "model build failed unexpectedly:
        # UnicodeDecodeError: ...", which tells a script "tan broke" rather
        # than "your board.yaml has a bad byte". The file opened and read
        # fine, so `board-yaml-missing` would send the customer looking for
        # the wrong problem -- this is the same unusable-input class the
        # YAML-syntax and not-a-mapping arms below already land on.
        #
        # `model` has no Rust oracle to defer to here (unlike `kconfig`,
        # which folds this same byte-for-byte case into `board-yaml-missing`
        # to mirror `read_to_string`'s single `io::Error` arm) -- a merge
        # briefly folded this arm into the OSError one above on that other
        # command's precedent (tan-cli#415), which made this whole `except`
        # unreachable dead code and silently reverted the tan-cli#396 fix.
        # Restored as its own arm: this file's own established
        # classification, not an oracle's.
        raise ModelError(
            "model.board-yaml-invalid",
            f"{path}: not valid UTF-8: {err}",
            ExitCode.VALIDATION_FAILURE,
        ) from err
    try:
        import yaml  # noqa: PLC0415 (declared dependency, guarded anyway)
    except ImportError as err:
        raise ModelError(
            "model.internal-failure",
            f"no YAML parser available ({err}); install PyYAML (`pip install pyyaml`).",
            ExitCode.INTERNAL_FAILURE,
        ) from err
    try:
        doc = yaml.safe_load(text)
    except Exception as err:  # noqa: BLE001 -- any PyYAML failure is bad input
        raise ModelError(
            "model.board-yaml-invalid", f"{path}: {err}", ExitCode.VALIDATION_FAILURE
        ) from err
    if not isinstance(doc, dict):
        raise ModelError(
            "model.board-yaml-invalid",
            f"{path}: expected a YAML mapping at the top level.",
            ExitCode.VALIDATION_FAILURE,
        )
    return doc


def _run_build(
    *,
    context: ProjectContext,
    out: str,
    metadata_root: str | None,
    sdk_root: str | None,
) -> tuple[Project, SdkInfo | None, dict, list[Issue], ExitCode]:
    """`--board` plays the role `--board-yaml` does everywhere else, so the
    SAME project-context resolution applies (I-31); the envelope's `project`
    and `sdk` fields come from that ONE resolution, matching `size`/`image`
    (`build_output.resolve_project_context`'s own doc: the envelope's `sdk`
    block must be what THAT resolution produced, never a second lookup).

    tan-cli#497 defect 3: the `resolve_project_context` call used to live at
    the top of this function, which meant its `broken_project_pin` /
    `foreign_global_default_for` were unreachable from `model`'s own
    `except ModelError` handler -- so the pin warning was dropped on the
    refusal paths as well as the happy one. It is now resolved by the caller
    and handed in, so the ONE resolution feeds every exit."""
    workspace_root = Path(context.workspace_root)
    board_path = Path(context.board_yaml)
    reported_project = context.project()
    sdk_info = context.sdk

    # The metadata-reading resolution is DELIBERATELY separate and wider
    # (`build_output.resolve_metadata_sdk_root`'s own doc): a child/sibling
    # checkout the project-context tier chain does not consider can still
    # supply `metadata/`, in which case `sdk_info` stays absent while the
    # build still runs against it -- same divergence `tan size`'s budget
    # resolution already allows.
    resolved_sdk = resolve_metadata_sdk_root(sdk_root, context.workspace_root)
    if resolved_sdk is None:
        raise ModelError(
            "model.sdk-root-unresolved",
            # tan-cli#497 defect 7: a REJECTED `--sdk-root` names the value.
            # The no-flag message below opens with "Use --sdk-root" -- exactly
            # the flag the caller just typed -- and the rejected path appeared
            # nowhere else in the envelope, since `resolve_metadata_sdk_root`
            # returning `None` is also what leaves the `sdk` block absent.
            rejected_sdk_root_message(sdk_root, "No models were built.")
            if sdk_root
            # `tan sdk switch` refuses in this build (tan-cli#305) -- kept the
            # two mechanisms that actually work here (`--sdk-root`, placing
            # the project near a checkout) and swapped the third for
            # NO_SDK_NEXT_STEPS's honest "how to get one at all".
            else "alp-sdk root is unresolved. Use --sdk-root, place the project near an "
            f"alp-sdk checkout, or {NO_SDK_NEXT_STEPS}.",
            ExitCode.VALIDATION_FAILURE,
        )

    board_doc = _load_board(board_path)
    som = board_doc.get("som")
    sku = som.get("sku") if isinstance(som, dict) else None
    if not isinstance(sku, str) or not sku:
        raise ModelError(
            "model.board-yaml-invalid",
            f"{board_path}: som.sku is missing.",
            ExitCode.VALIDATION_FAILURE,
        )
    models = board_doc.get("models") or []
    if not isinstance(models, list):
        raise ModelError(
            "model.board-yaml-invalid",
            f"{board_path}: `models:` must be a list.",
            ExitCode.VALIDATION_FAILURE,
        )

    data: dict[str, Any] = {"schemaVersion": DATA_SCHEMA_VERSION, "sku": sku, "built": []}
    if not models:
        return reported_project, sdk_info, data, [], ExitCode.SUCCESS

    base = board_path.parent
    out_dir = Path(out)
    if not out_dir.is_absolute():
        out_dir = workspace_root / out_dir
    metadata_dir = (
        Path(metadata_root) if metadata_root else resolved_sdk / "metadata"
    )
    if metadata_root and not metadata_dir.is_absolute():
        metadata_dir = workspace_root / metadata_dir

    # Validate + prepare every model BEFORE building any of them -- an invalid
    # `models:` entry refuses the whole run with no partial build, matching
    # the pre-ADR-0028 shape where this same loop only assembled the driver
    # payload and never itself built anything.
    prepared: list[tuple[str, Path, dict | None]] = []
    for m in models:
        if not isinstance(m, dict) or "name" not in m or "source" not in m:
            raise ModelError(
                "model.board-yaml-invalid",
                f"{board_path}: every `models:` entry needs `name` and `source`.",
                ExitCode.VALIDATION_FAILURE,
            )
        source = (base / m["source"]).resolve()
        prepared.append((m["name"], source, _resolve_compile(m.get("compile"), base)))

    # Deliberate divergence 1 from the oracle (see module doc): a per-model
    # `build_model()` failure is caught here and reported as a coded
    # `model.build-failed` issue, and the loop continues to the next model
    # rather than aborting the whole batch.
    issues: list[Issue] = []
    built: list[str] = []
    for name, source, compile_opts in prepared:
        try:
            out_path = build_model(
                sku=sku,
                name=name,
                source=source,
                out_dir=out_dir,
                metadata_root=metadata_dir,
                compile_opts=compile_opts,
            )
        except Exception as err:  # noqa: BLE001 -- a per-model failure is a coded issue, not a traceback
            issues.append(
                Issue(
                    "model.build-failed",
                    "error",
                    f"model '{name}': {type(err).__name__}: {err}",
                )
            )
        else:
            built.append(str(out_path))
    data["built"] = built
    exit_code = ExitCode.SUCCESS if not issues else ExitCode.WRITE_FAILURE
    return reported_project, sdk_info, data, issues, exit_code


def _backend_version(backend: str, *, available: bool) -> str | None:
    """Best-effort compiler-version string for an AVAILABLE backend --
    READ-ONLY, never a spawn (module doc). `None` whenever no non-spawning
    probe exists, or the one that does answers a DEGRADED sentinel rather
    than a real version:

    * `ethos_u` -- `_vela_version()` reads `importlib.metadata`, no
      subprocess. It returns the bare literal `"vela"` when the
      `ethos-u-vela` distribution's metadata is absent
      (`except PackageNotFoundError`) -- that is a degraded answer, not a
      version, so it is reported here as `None` rather than surfaced as if
      it were one.
    * `drpai` -- `_compiler_version(tvm_home)` reads a version file inside
      the toolchain checkout (`tan/model/adapters/drpai.py`), also no
      subprocess. It returns the bare literal `"drp-ai_tvm"` when none of
      `setup/version` / `version` / `VERSION` is found under `tvm_home` --
      that is the identical degraded-sentinel shape `ethos_u` guards above,
      so it gets the same guard: reported here as `None` rather than
      surfaced as if it were a real version.
    * `deepx_dxm1` -- `_dxcom_version()` is the only version probe DeepxAdapter
      has, and it SPAWNS (`subprocess.run(["dxcom", "-v"], ...)`,
      `tan/model/adapters/deepx.py`). `doctor` must never invoke a compiler,
      so this backend's version is always unknown here, never guessed at.
    * `cpu` -- no external tool at all (`BACKEND_TOOLS["cpu"] is None`), so
      no version to report.
    """
    if not available:
        return None
    if backend == "ethos_u":
        version = _vela_version()
        return None if version == "vela" else version
    if backend == "drpai":
        tvm_home = _drpai_tvm_home()
        if tvm_home is None:
            return None
        version = _drpai_compiler_version(tvm_home)
        return None if version == "drp-ai_tvm" else version
    return None


def _deepx_dxm1_status() -> tuple[bool, str | None]:
    """`deepx_dxm1`'s doctor verdict -- gated on what `DeepxAdapter.compile()`
    actually needs (the bare `dxcom` off PATH: `cmd = ["dxcom", "-m", ...]`,
    `tan/model/adapters/deepx.py`), NOT `DeepxAdapter.is_available()`.

    That adapter method ORs in a second arm -- `ALP_DEEPX_SDK_HOME` naming a
    directory -- that `compile()` never reads at all, so a row gated on it
    reported `available: true` on a host with the env var pointed at an
    empty directory and no `dxcom` anywhere, and the very next `model build`
    raised `FileNotFoundError: [Errno 2] No such file or directory: 'dxcom'`.
    Deliberately NOT a change to `DeepxAdapter.is_available()` itself --
    `tan.model.build.build_model` uses that method for a different decision
    (whether to attempt this backend at all) that this env-var arm is
    legitimate for; this is doctor's own, narrower "would compile() actually
    run" question, answered without spawning anything.

    Read-only: `shutil.which` + `os.environ.get` + `Path.is_dir`, the exact
    same primitives `is_available()` itself uses, never a subprocess.

    Returns `(available, reason)`. `reason` is `None` when either `dxcom` is
    on PATH (row is available, no reason needed) or neither PATH nor the env
    var carries any signal at all (the caller falls back to
    `_UNAVAILABLE_REASONS["deepx_dxm1"]`'s generic reason). When the env var
    IS set to a real directory but `dxcom` still isn't on PATH, `reason`
    carries an explicit caveat -- the var is set, but it is not what
    `compile()` reads -- rather than silently reporting green.
    """
    if shutil.which("dxcom"):
        return True, None
    sdk_home = os.environ.get("ALP_DEEPX_SDK_HOME")
    if sdk_home and Path(sdk_home).is_dir():
        return False, (
            f"ALP_DEEPX_SDK_HOME={sdk_home} is set, but dxcom is not on PATH; "
            "DeepxAdapter.compile() always shells the bare `dxcom` off PATH "
            "and never reads ALP_DEEPX_SDK_HOME, so this environment would "
            "still fail `model build`"
        )
    return False, None


def _drpai_status() -> tuple[bool, str | None]:
    """`drpai`'s doctor verdict -- gated on what `DrpaiAdapter.compile()`
    actually needs under `$ALP_DRPAI_TVM_HOME`: the vendor tutorial script
    `tutorials/compile_onnx_model_quant.py` it spawns AND the `python3`
    interpreter it shells that script with (`tan/model/adapters/drpai.py`'s
    own `cmd = ["python3", str(script), ...]`), NOT just the bare directory
    `DrpaiAdapter.is_available()`/`_tvm_home()` check for.

    A customer who points `ALP_DRPAI_TVM_HOME` at an unpacked-but-unbuilt
    checkout (the tutorials/ tree not yet present, or laid out differently)
    got `available: true` from the bare directory check, then a vendor-script
    failure on the next real build -- and a host with a BUILT tree but no
    `python3` on PATH hit the exact same false-green class one dependency
    further out, still reading green right up until that same `model build`
    failed. Deliberately NOT a change to `DrpaiAdapter.is_available()`/
    `_tvm_home()` themselves -- same reasoning as `_deepx_dxm1_status` above:
    this is doctor's own, narrower question. `python3` is deliberately NOT
    `BACKEND_TOOLS["drpai"]` (see that dict's own comment: naming an
    interpreter present on essentially every host tells a customer nothing
    actionable) -- it is checked here as a `compile()` prerequisite, not
    reported as *the* tool this row names.

    Read-only: `_tvm_home()` (env var + `Path.is_dir`) + `Path.is_file` on the
    tutorial script path + `shutil.which("python3")`, never a subprocess.

    Returns `(available, reason)`, same shape as `_deepx_dxm1_status`: `None`
    reason falls back to `_UNAVAILABLE_REASONS["drpai"]`'s generic reason
    (env var not set at all); an explicit caveat when the var names a real
    directory but the tutorial script isn't under it, or when both are
    present but `python3` is not on PATH.
    """
    tvm_home = _drpai_tvm_home()
    if tvm_home is None:
        return False, None
    script = tvm_home / "tutorials" / "compile_onnx_model_quant.py"
    if not script.is_file():
        return False, (
            f"ALP_DRPAI_TVM_HOME={tvm_home} is set, but tutorials/"
            "compile_onnx_model_quant.py was not found under it -- point it at "
            "a BUILT rzv_drp-ai_tvm install, not an unpacked/incomplete tree"
        )
    if shutil.which("python3") is None:
        return False, (
            f"ALP_DRPAI_TVM_HOME={tvm_home} and tutorials/"
            "compile_onnx_model_quant.py are both present, but python3 is "
            "not on PATH -- DrpaiAdapter.compile() shells `python3 "
            "<script> ...` (tan/model/adapters/drpai.py), so this "
            "environment would still fail `model build`"
        )
    return True, None


def _probe_backend(backend: str) -> tuple[bool, str | None]:
    """`(available, reason override)` for one backend row -- `deepx_dxm1` and
    `drpai` each get a NARROWER probe than the adapter's own `is_available()`
    (see `_deepx_dxm1_status`/`_drpai_status`'s own docs): `is_available()`
    reports green on a signal `compile()` doesn't actually act on, so gating
    a row on it can report available when the next `model build` would
    immediately fail. Every other backend keeps the plain `is_available()`
    OR across its registered adapters, with no reason override."""
    if backend == "deepx_dxm1":
        return _deepx_dxm1_status()
    if backend == "drpai":
        return _drpai_status()
    available = any(a.is_available() for a in _ADAPTERS if a.backend == backend)
    return available, None


def _run_doctor(
    *,
    context: ProjectContext,
    sdk_root: str | None,
) -> tuple[Project, SdkInfo | None, dict, list[Issue], ExitCode]:
    """One row per registered compiler-backend, per the module doc: READ-ONLY,
    spawns nothing, and never fails the run -- an unavailable toolchain is
    reported, not refused. A board.yaml is not read at all: unlike `build`,
    `doctor` has no per-model work to resolve, only host-toolchain facts that
    hold regardless of which (if any) project this was run from.

    The SDK root is still resolved, mirroring `_run_build` (module doc), but
    ONLY to decide whether `model.doctor-sdk-unresolved` belongs in `issues`
    -- a WARNING, never the VALIDATION_FAILURE `_run_build` raises for the
    same non-resolution, since nothing below actually needs it: no backend
    row reads `metadata/**`.
    """
    reported_project = context.project()
    sdk_info = context.sdk

    issues: list[Issue] = []
    resolved_sdk = resolve_metadata_sdk_root(sdk_root, context.workspace_root)
    if resolved_sdk is None:
        issues.append(
            Issue(
                "model.doctor-sdk-unresolved",
                "warning",
                (
                    rejected_sdk_root_message(
                        sdk_root, "Backend availability below is unaffected."
                    )
                    if sdk_root
                    else "alp-sdk root is unresolved. Use --sdk-root, place the project "
                    f"near an alp-sdk checkout, or {NO_SDK_NEXT_STEPS}. Backend "
                    "availability below is unaffected."
                ),
            )
        )

    rows = []
    for backend in registry_backends(_ADAPTERS):
        available, reason = _probe_backend(backend)
        version = _backend_version(backend, available=available)
        rows.append(backend_row(backend, available=available, version=version, reason=reason))

    data: dict[str, Any] = {
        "schemaVersion": DOCTOR_DATA_SCHEMA_VERSION,
        "backends": [row.as_dict() for row in rows],
    }
    return reported_project, sdk_info, data, issues, ExitCode.SUCCESS


def _empty_data(subcommand: str | None) -> dict[str, Any]:
    """The `data` shape for a refusal that never reached `_run_build`/
    `_run_doctor` -- each subcommand's OWN empty payload shape, not a
    generic stand-in, so a consumer parsing `data.backends`/`data.built`
    off a refusal envelope gets the same shape it would from a run that
    resolved nothing."""
    if subcommand == "doctor":
        return {"schemaVersion": DOCTOR_DATA_SCHEMA_VERSION, "backends": []}
    return {"schemaVersion": DATA_SCHEMA_VERSION, "sku": None, "built": []}


def model(
    subcommand: str = typer.Argument(None, metavar="SUBCOMMAND", help="build | doctor."),
    board: str = typer.Option(
        # tan-cli#398: `--board-yaml` is a REAL second spelling of this one
        # option, not a second option -- it is what `build`, `run`, `kconfig`,
        # `validate`, `generate` and `inspect` all call the board file, and
        # what `docs/CLI.md`'s "Common flags" tells a caller every command
        # supports. Declared here so `accept_global_flags` sees it as already
        # covered and stops injecting an inert twin behind this one: a caller
        # who used the surface-wide spelling was served `./board.yaml`'s
        # `som.sku` instead of the file they named, and `som.sku` is what
        # picks the silicon `build_model` compiles for.
        "board.yaml", "--board", "--board-yaml", metavar="PATH", help="Path to board.yaml."
    ),
    out: str = typer.Option(
        "build/models", "--out", metavar="PATH", help="Output directory."
    ),
    metadata_root: str = typer.Option(
        None,
        "--metadata-root",
        metavar="PATH",
        help="Path to the metadata/ root (default: <sdk-root>/metadata).",
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
) -> None:
    """Compile + package board.yaml `models:` into `.alpmodel` packages
    (`build`), or report NPU-compiler toolchain availability (`doctor`)."""
    json_mode = output_format == "json"

    def finish(
        project_: Project,
        sdk: SdkInfo | None,
        data: dict,
        issues: list[Issue],
        exit_code: ExitCode,
    ) -> None:
        if json_mode:
            emit(Envelope("model", project_, data, issues, exit_code, sdk=sdk))
        else:
            # tan-cli#497 defect 3: warnings LEAD. `issues` now carries the
            # SDK-resolution pair, which is a fact about the whole run --
            # which checkout answered -- not a per-model outcome, so it must
            # be readable before the result lines rather than buried under
            # them. `{severity}: {message}`, the shape `build`/`run`/`kconfig`
            # print a resolution warning with, so the same workspace reads
            # the same way whichever command a developer runs.
            warnings = [i for i in issues if i.severity != "error"]
            errors = [i for i in issues if i.severity == "error"]
            for issue in warnings:
                print(f"{issue.severity}: {issue.message}", file=sys.stderr)
            if "backends" in data:
                # `doctor`: one line per backend, in registry order --
                # unavailable rows carry their reason inline, so a scrollback
                # answers "why not" without a second command.
                for row in data["backends"]:
                    tool = row["tool"] or "-"
                    if row["available"]:
                        version = f", version={row['version']}" if row["version"] else ""
                        print(
                            f"{row['backend']}: available (tool={tool}{version})",
                            file=sys.stderr,
                        )
                    else:
                        # A backend with no `_UNAVAILABLE_REASONS` entry (and
                        # no doctor-side caveat) carries `reason: None` --
                        # drop the `-- ...` clause entirely rather than
                        # rendering the Python literal `None` into the line.
                        suffix = f" -- {row['reason']}" if row["reason"] else ""
                        print(
                            f"{row['backend']}: unavailable (tool={tool}){suffix}",
                            file=sys.stderr,
                        )
            else:
                for path in data.get("built", []):
                    print(f"built {path}", file=sys.stderr)
                # `not issues` before -- the same thing until `issues` started
                # carrying those warnings. A board with no `models:` and a
                # broken project pin must still be told nothing was declared;
                # a warning is not a reason to withhold the only line this
                # run had to say.
                if not data.get("built") and not errors:
                    print(
                        "model: no `models:` declared in board.yaml; nothing to build.",
                        file=sys.stderr,
                    )
            for issue in errors:
                print(f"model: {issue.message}", file=sys.stderr)
        raise typer.Exit(int(exit_code))

    if subcommand not in SUBCOMMANDS:
        finish(
            Project(root=None, board_yaml=None),
            None,
            _empty_data(subcommand),
            [
                Issue(
                    "model.unknown-subcommand",
                    "error",
                    f"Unknown model subcommand: {'(none)' if subcommand is None else subcommand}. "
                    f"Available: {', '.join(SUBCOMMANDS)}.",
                )
            ],
            ExitCode.RUNTIME_FAILURE,
        )
        return

    # tan-cli#497 defect 3: `model build` was the only `resolve_project_context`
    # caller that read `.workspace_root`/`.board_yaml`/`.project()`/`.sdk` off
    # the returned context and NONE of the three resolution facts -- so it
    # alone dropped BOTH `sdk.project-pin-unresolved` (tan-cli#263) and
    # `sdk.global-default-foreign-project` (tan-cli#464), in JSON and text
    # alike, while `size` and `image` reported them from the same directory
    # through the same resolver. That matters most here: the `.alpmodel`
    # packages are compiled against `<resolved checkout>/metadata`, i.e.
    # against that checkout's target/backend table for `som.sku`.
    #
    # Resolved HERE rather than inside `_run_build` so the warnings survive a
    # `ModelError` too -- `project_pin_issue`'s contract is "shared by EVERY
    # caller of `resolve_sdk_tiered`", not "every caller that got as far as a
    # successful build".
    sdk_issues: list[Issue] = []
    try:
        context = resolve_project_context(project, board, sdk_root)
        sdk_issues = sdk_resolution_issues(
            context.broken_project_pin,
            context.sdk_source_tier,
            context.foreign_global_default_for,
        )
        if subcommand == "doctor":
            project_, sdk, data, issues, exit_code = _run_doctor(
                context=context,
                sdk_root=sdk_root,
            )
        else:
            project_, sdk, data, issues, exit_code = _run_build(
                context=context,
                out=out,
                metadata_root=metadata_root,
                sdk_root=sdk_root,
            )
    except ModelError as err:
        finish(
            Project(root=None, board_yaml=None),
            None,
            _empty_data(subcommand),
            [*sdk_issues, Issue(err.code, "error", err.message)],
            err.exit_code,
        )
        return
    except Exception as err:  # noqa: BLE001 -- the envelope IS the error contract
        finish(
            Project(root=None, board_yaml=None),
            None,
            _empty_data(subcommand),
            [
                *sdk_issues,
                Issue(
                    "model.internal-failure",
                    "error",
                    f"model {subcommand} failed unexpectedly: {type(err).__name__}: {err}",
                ),
            ],
            ExitCode.INTERNAL_FAILURE,
        )
        return

    finish(project_, sdk, data, [*sdk_issues, *issues], exit_code)


# tan-cli#261: adds the seven oracle `GlobalArgs` flags this command is
# missing entirely (`--all`/`--ci`/`--no-color`/`--non-interactive`/
# `--quiet`/`--target`/`--verbose`); see `tan.core.global_flags`.
#
# `--board-yaml` is NOT among them any more (tan-cli#398). It used to be, and
# the note here claimed that was harmless because "`model`'s own `--board`
# already plays `--board-yaml`'s role for real" -- but the two spellings were
# never wired together, so the injected one was accepted, dropped, and the
# default `./board.yaml` packaged instead of the file the caller named, at
# `exitCode: 0` with `issues: []`. It is now a second decl on `--board`
# itself (above), so both spellings are one option and read one file.
#
# The six arity-0 flags above stay accepted-and-dropped; the injected
# `--target` is now REFUSED when supplied rather than ignored, per the same
# issue -- `model` has no emit target to honour it with.
model = accept_global_flags(model)
