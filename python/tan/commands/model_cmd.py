# SPDX-License-Identifier: Apache-2.0
"""`tan model build`/`tan model doctor`/`tan model check`/`tan model list` --
compile + package `board.yaml`'s `models:` block into `.alpmodel` packages,
report NPU-compiler toolchain availability, statically screen a declared
model's NPU eligibility against the SoM's own support tables, and list what
is declared next to what is already built.

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

`doctor` also reports OPTIONAL prerequisites, in `data.optional[]` and in the
same five-key row shape -- today one: the vendor vela config `.ini` a licensed
customer names in `ALP_VELA_CONFIG` (`_vela_vendor_config_status`). Kept out of
`backends[]` deliberately: `available: false` there means tan cannot compile
for that backend, while an absent vendor `.ini` means the backend works and an
enhancement is not installed. Without it vela uses Arm's own built-in system
config, which is what the arena/SRAM figures tan reports already describe, so
neither the JSON nor the text line may read as a fault.

**`check` is the static NPU-eligibility screen ADR-0028's amendment adds
(tan-cli#782).** `tan.model.check`/`tan.model.analyze` do the actual work
(resolving @sku's real NPU backends, walking a model's ops, scoring them);
this module only resolves the board.yaml the same way `build` does, calls
that engine per declared model, and shapes the envelope. `check`'s exit code
is ALWAYS `SUCCESS` for a run that completed -- `partial`/`cpu-only`/
`undetermined` are the feature this command exists to report, never a
failure. See `tan.model.check`'s own module doc for `--exact`.

**`list` is the smallest of the four (tan-cli#674): what `board.yaml`
declares, next to what `--out` already holds for each one.** It mirrors
`doctor`'s SDK-root TOLERANCE, not `build`'s `_require_metadata_sdk_root`
refusal -- naming what is declared and what is already built on disk needs no
`metadata/**` at all, so `_run_list` itself never resolves, refuses, or warns
over a *metadata* SDK root the way `_run_build`/`_run_check`/`_run_doctor` do
(no `--sdk-root`/`--metadata-root` handling inside it, no `model.sdk-root-
unresolved`/`model.doctor-sdk-unresolved`).

**That is narrower than "never resolves or warns about an SDK root" (tan-cli#674
review MAJOR 2)** -- `list` still goes through the SAME shared project-context
preamble every subcommand here does (`resolve_project_context`, below), and
that preamble's own resolution is what populates the envelope's `sdk` block
and what `sdk_resolution_issues` reads to warn about a broken `.alp/sdk-path`
pin or a foreign global default. Measured: `tan model list --format json` from
a workspace with a resolvable alp-sdk checkout emits a real `"sdk":{"root":
...,"sourceTier":...}` block, and `test_a_broken_project_pin_is_reported_on_a_
model_list` (`tests/commands/test_model_list_command.py`) already pins a
`sdk.project-pin-unresolved` warning on this exact subcommand. `list` opts out
of only the SECOND, metadata-reading resolution `build`/`check`/`doctor`
additionally perform -- not the first.

It is read-only and spawns nothing: a declared model's `.alpmodel`
(`tan.model.build.build_model`'s own `{name}.alpmodel` naming) is only ever
`stat()`-ed and its manifest read back, never compiled. See
`tan.core.model_list`'s own module doc for the per-model `artifact` shape,
including `stale` (has `source` changed since the package on disk was built)
and the `model.artifact-stale-unknown` warning a readback failure there emits.
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
from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS
from tan.core.global_flags import accept_global_flags
from tan.core.model_check import backend_report_as_dict, render_check_text
from tan.core.model_doctor import backend_row, optional_row, registry_backends
from tan.core.model_list import declared_sku, list_entry, render_list_text
from tan.core.sdk_discovery import sdk_resolution_issues
from tan.core.shapes import rejected_sdk_root_message
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.model.adapters.drpai import _compiler_version as _drpai_compiler_version
from tan.model.adapters.drpai import _tvm_home as _drpai_tvm_home
from tan.model.adapters.ethos_u import _VELA_CONFIG_ENV, _vela_version, _vendor_config_path
from tan.model.build import _ADAPTERS, build_model
from tan.model.check import check_model_backends, resolve_check_backends
from tan.model.package import read_manifest_file
from tan.output_format import FORMAT_HELP, OutputFormat

#: `data.schemaVersion` for `build`'s payload.
DATA_SCHEMA_VERSION = "1"

#: `data.schemaVersion` for `doctor`'s payload -- versioned independently of
#: `build`'s (a different `data` shape entirely: `backends[]`, not `built[]`).
DOCTOR_DATA_SCHEMA_VERSION = "1"

#: `data.schemaVersion` for `check`'s payload -- versioned independently of
#: the other two for the same reason (`models[].backends[]`, a third shape).
CHECK_DATA_SCHEMA_VERSION = "1"

#: `data.schemaVersion` for `list`'s payload -- versioned independently of
#: the other three (`models[].artifact`, a fourth shape).
LIST_DATA_SCHEMA_VERSION = "1"

#: `SUBCOMMANDS` names every subcommand this command accepts, in the order the
#: unknown-subcommand refusal lists them.
SUBCOMMANDS = ("build", "doctor", "check", "list")


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


def _require_sku(board_doc: dict, board_path: Path) -> str:
    """`board.yaml`'s `som.sku` -- shared by `build` and `check`, both of
    which need a real SKU before they can resolve anything metadata-shaped
    (a compile target, or a NPU support table)."""
    som = board_doc.get("som")
    sku = som.get("sku") if isinstance(som, dict) else None
    if not isinstance(sku, str) or not sku:
        raise ModelError(
            "model.board-yaml-invalid",
            f"{board_path}: som.sku is missing.",
            ExitCode.VALIDATION_FAILURE,
        )
    return sku


def _require_models_list(board_doc: dict, board_path: Path) -> list:
    """`board.yaml`'s `models:` -- `[]` when absent (a board that declares no
    models is valid, not an error), a `ModelError` when present but not a
    list."""
    models = board_doc.get("models") or []
    if not isinstance(models, list):
        raise ModelError(
            "model.board-yaml-invalid",
            f"{board_path}: `models:` must be a list.",
            ExitCode.VALIDATION_FAILURE,
        )
    return models


def _require_model_entry(m: Any, board_path: Path) -> None:
    """Every `models:` entry needs `name` and `source` -- `build`'s own
    per-model `compile:` block is optional and stays that command's own
    concern; this is the shape floor both `build` and `check` share."""
    if not isinstance(m, dict) or "name" not in m or "source" not in m:
        raise ModelError(
            "model.board-yaml-invalid",
            f"{board_path}: every `models:` entry needs `name` and `source`.",
            ExitCode.VALIDATION_FAILURE,
        )


def _require_metadata_sdk_root(sdk_root: str | None, workspace_root: str, no_models_msg: str) -> Path:
    """`resolve_metadata_sdk_root`, or a coded `model.sdk-root-unresolved`
    refusal -- shared by `build` and `check`, whose only difference is the
    trailing "no models were {built,checked}" clause (tan-cli#497 defect 7:
    a REJECTED `--sdk-root` names the value it rejected)."""
    resolved_sdk = resolve_metadata_sdk_root(sdk_root, workspace_root)
    if resolved_sdk is None:
        raise ModelError(
            "model.sdk-root-unresolved",
            rejected_sdk_root_message(sdk_root, no_models_msg)
            if sdk_root
            # `tan sdk switch` refuses in this build (tan-cli#305) -- kept the
            # two mechanisms that actually work here (`--sdk-root`, placing
            # the project near a checkout) and swapped the third for
            # NO_SDK_NEXT_STEPS's honest "how to get one at all".
            else "alp-sdk root is unresolved. Use --sdk-root, place the project near an "
            f"alp-sdk checkout, or {NO_SDK_NEXT_STEPS}.",
            ExitCode.VALIDATION_FAILURE,
        )
    return resolved_sdk


def _resolve_metadata_dir(metadata_root: str | None, resolved_sdk: Path, workspace_root: Path) -> Path:
    """`<sdk-root>/metadata` unless `--metadata-root` overrides it -- shared
    by `build` and `check`, both of which read `metadata/**` for the same
    resolved SKU."""
    metadata_dir = Path(metadata_root) if metadata_root else resolved_sdk / "metadata"
    if metadata_root and not metadata_dir.is_absolute():
        metadata_dir = workspace_root / metadata_dir
    return metadata_dir


def _shipped_caveat_issues(name: str, out_path: Path) -> list[Issue]:
    """One `model.target-caveat` WARNING per caveated target in the package
    just written -- read back OUT OF THE FILE, not out of the in-memory build.

    `tan model check --exact` already surfaced the compiler's own caveats, but
    `check` ships nothing; `build` is the path that puts bytes on a board, and
    it dropped them. A vela blob compiled against vela's BUILT-IN default
    profile carries an `arena`/`sram_kib` pair describing THAT memory model,
    and those are the very figures alp-sdk's on-device selector gates on
    (`src/backends/inference/alp_model_select.c`), so the operator who runs
    `tan model build` has to be told -- once per target, verbatim, at the
    moment the package is produced.

    Reported from `package.read_manifest_file` so the line describes the
    ARTIFACT: a caveat that never reached the file cannot be narrated here.
    A readback that fails is itself a warning (`model.caveat-readback-
    failed`), never an error -- the package IS written, and downgrading a
    successful build to a failure over a diagnostic read would be the wrong
    trade. It is not swallowed either: silence would be indistinguishable
    from "no caveats"."""
    try:
        mft = read_manifest_file(out_path)
    except Exception as err:  # noqa: BLE001 -- a diagnostic readback, not the build
        return [Issue("model.caveat-readback-failed", "warning",
                      f"model '{name}': wrote {out_path} but could not read its manifest "
                      f"back to report compiler caveats: {type(err).__name__}: {err}")]
    out: list[Issue] = []
    for target in mft.targets:
        label = f"{target.backend} {target.accel_config}".strip()
        for caveat in target.caveats:
            out.append(Issue(
                "model.target-caveat", "warning",
                f"model '{name}': the {label} target in {out_path.name} ships with a "
                f"compiler caveat -- {caveat}"))
    return out


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
    resolved_sdk = _require_metadata_sdk_root(sdk_root, context.workspace_root, "No models were built.")

    board_doc = _load_board(board_path)
    sku = _require_sku(board_doc, board_path)
    models = _require_models_list(board_doc, board_path)

    data: dict[str, Any] = {"schemaVersion": DATA_SCHEMA_VERSION, "sku": sku, "built": []}
    if not models:
        return reported_project, sdk_info, data, [], ExitCode.SUCCESS

    base = board_path.parent
    out_dir = Path(out)
    if not out_dir.is_absolute():
        out_dir = workspace_root / out_dir
    metadata_dir = _resolve_metadata_dir(metadata_root, resolved_sdk, workspace_root)

    # Validate + prepare every model BEFORE building any of them -- an invalid
    # `models:` entry refuses the whole run with no partial build, matching
    # the pre-ADR-0028 shape where this same loop only assembled the driver
    # payload and never itself built anything.
    prepared: list[tuple[str, Path, dict | None]] = []
    for m in models:
        _require_model_entry(m, board_path)
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
            issues.extend(_shipped_caveat_issues(name, out_path))
    data["built"] = built
    # ERRORS decide the exit code, not `issues` being non-empty. `_shipped_
    # caveat_issues` puts WARNINGS in this list, and a warning about a package
    # that was written must not report it as a write failure -- the same
    # separation `finish()`'s renderer already makes between `warnings` and
    # `errors`, and the same one the caller's `sdk_issues` rely on.
    exit_code = (ExitCode.SUCCESS if not any(i.severity == "error" for i in issues)
                 else ExitCode.WRITE_FAILURE)
    return reported_project, sdk_info, data, issues, exit_code


def _declared_hw_rev(board_doc: dict, board_path: Path) -> str | None:
    """`board.yaml`'s `som.hw_rev`, or `None` when it declares none.

    OPTIONAL by `board.schema.json`, which also states the fallback: "the SDK
    falls back to the SKU preset's `default_hw_rev` when omitted". That
    fallback is applied downstream (`tan.model.perf_apply._resolve_hw_rev`),
    where the metadata root is in hand; this only reports what the customer
    wrote. Unlike `som.sku` a missing value is not a refusal -- `check` worked
    without it before a bench measurement existed to pin to a revision, and
    must keep working.

    A PRESENT-BUT-UNUSABLE value fails CLOSED instead, rather than silently
    falling through to the same `None` a genuinely absent key returns
    (tan-cli#791 review MINOR 5). `board.yaml` is YAML: an unquoted
    `hw_rev: 2` parses to the int `2`, not the string `"r2"` no perf point is
    ever published under. Measured: `{"som": {"hw_rev": 2}} -> None` here
    used to silently defer to the SKU preset's own `default_hw_rev` --
    serving a customer who wrote a real hw_rev a DIFFERENT module revision's
    bench measurement, exactly the "describes a different machine" failure
    `hw_rev` exists to refuse in the first place (alp-sdk `f724d3e4`).
    `_run_check` does not otherwise schema-validate `board.yaml`, so this is
    the one place this particular shape of bad input is ever caught -- raised
    as a `ModelError` (a board-level fact, refusing the WHOLE run, same
    scoping `_require_sku`/`_require_check_backends` already use) rather than
    returned, since a caller that got `None` back has no way to tell "the
    customer said nothing" apart from "the customer said something tan could
    not use"."""
    som = board_doc.get("som")
    if not isinstance(som, dict) or "hw_rev" not in som:
        return None
    hw_rev = som["hw_rev"]
    if isinstance(hw_rev, str) and hw_rev:
        return hw_rev
    raise ModelError(
        "model.board-yaml-invalid",
        f"{board_path}: som.hw_rev must be a non-empty string (e.g. \"r2\"); got {hw_rev!r}.",
        ExitCode.VALIDATION_FAILURE,
    )


def _check_one_model(name: str, source: Path, backends: list[str], sku: str,
                      metadata_dir: Path, exact: bool,
                      hw_rev: str | None = None) -> dict | Issue:
    """One declared model's `check` result: the serialised `{name, source,
    backends}` block on success, or a coded `model.check-failed` Issue on any
    failure (an unreadable/unparseable source, most commonly) -- never a
    traceback, matching `build`'s own per-model `model.build-failed` shape so
    one bad model in a multi-model board.yaml does not abort the batch."""
    try:
        reports = check_model_backends(backends=backends, sku=sku, source=source,
                                        metadata_root=metadata_dir, exact=exact,
                                        hw_rev=hw_rev)
    except Exception as err:  # noqa: BLE001 -- a per-model failure is a coded issue, not a traceback
        return Issue("model.check-failed", "error", f"model '{name}': {type(err).__name__}: {err}")
    return {"name": name, "source": str(source),
            "backends": [backend_report_as_dict(r) for r in reports]}


def _require_check_backends(sku: str, metadata_dir: Path, board_path: Path) -> list[str]:
    """`resolve_check_backends`, or a coded `model.check-sku-unresolved`
    refusal -- a board-level fact about `som.sku`, reported once for the
    whole run rather than once per declared model."""
    try:
        return resolve_check_backends(sku, metadata_root=metadata_dir)
    except (FileNotFoundError, ValueError) as err:
        raise ModelError(
            "model.check-sku-unresolved",
            f"{board_path}: could not resolve NPU backends for som.sku {sku!r}: {err}",
            ExitCode.VALIDATION_FAILURE,
        ) from err


def _run_check(
    *,
    context: ProjectContext,
    metadata_root: str | None,
    sdk_root: str | None,
    exact: bool,
) -> tuple[Project, SdkInfo | None, dict, list[Issue], ExitCode]:
    """Static NPU-eligibility screen for every model `board.yaml` declares --
    same project-context/SDK-root resolution shape as `_run_build` (see its
    own docstring for why that resolution happens in the caller). `ok` stays
    `True`/exit 0 for a run that completed, whatever the verdicts read --
    reporting `undetermined`/`cpu-only` IS the feature; only a run that could
    not complete (an unresolved SKU, a per-model read failure) is non-zero."""
    workspace_root = Path(context.workspace_root)
    board_path = Path(context.board_yaml)
    reported_project = context.project()
    sdk_info = context.sdk

    resolved_sdk = _require_metadata_sdk_root(sdk_root, context.workspace_root, "No models were checked.")

    board_doc = _load_board(board_path)
    sku = _require_sku(board_doc, board_path)
    models = _require_models_list(board_doc, board_path)

    data: dict[str, Any] = {
        "schemaVersion": CHECK_DATA_SCHEMA_VERSION, "sku": sku, "exact": exact, "models": [],
    }
    if not models:
        return reported_project, sdk_info, data, [], ExitCode.SUCCESS

    base = board_path.parent
    metadata_dir = _resolve_metadata_dir(metadata_root, resolved_sdk, workspace_root)
    backends = _require_check_backends(sku, metadata_dir, board_path)
    # Board-level, like `sku`/`backends` above: resolved ONCE, ahead of the
    # per-model loop, so an unusable `som.hw_rev` (`ModelError`, see
    # `_declared_hw_rev`) refuses the whole run instead of repeating the same
    # parse -- and the same failure -- once per declared model.
    declared_hw_rev = _declared_hw_rev(board_doc, board_path)

    issues: list[Issue] = []
    model_reports: list[dict] = []
    for m in models:
        _require_model_entry(m, board_path)
        source = (base / m["source"]).resolve()
        result = _check_one_model(m["name"], source, backends, sku, metadata_dir, exact,
                                   declared_hw_rev)
        (issues if isinstance(result, Issue) else model_reports).append(result)
    data["models"] = model_reports
    exit_code = ExitCode.SUCCESS if not issues else ExitCode.RUNTIME_FAILURE
    return reported_project, sdk_info, data, issues, exit_code


def _run_list(
    *,
    context: ProjectContext,
    out: str,
) -> tuple[Project, SdkInfo | None, dict, list[Issue], ExitCode]:
    """Every declared model next to what `--out` already holds for it (module
    doc): no `--sdk-root`/`metadata_root` handling at all, unlike `_run_build`/
    `_run_check` above -- `list` reads only `board.yaml` and `out`'s own
    directory, so there is nothing here to resolve a checkout for. Never
    refuses over a missing/invalid `som.sku` either (`declared_sku` degrades
    to `None`): the SoM is not needed to say what is declared and what is
    already on disk."""
    workspace_root = Path(context.workspace_root)
    board_path = Path(context.board_yaml)
    reported_project = context.project()
    sdk_info = context.sdk

    board_doc = _load_board(board_path)
    sku = declared_sku(board_doc)
    models = _require_models_list(board_doc, board_path)

    data: dict[str, Any] = {"schemaVersion": LIST_DATA_SCHEMA_VERSION, "sku": sku, "models": []}
    if not models:
        return reported_project, sdk_info, data, [], ExitCode.SUCCESS

    base = board_path.parent
    out_dir = Path(out)
    if not out_dir.is_absolute():
        out_dir = workspace_root / out_dir

    entries = []
    issues: list[Issue] = []
    for m in models:
        _require_model_entry(m, board_path)
        source = (base / m["source"]).resolve()
        entry, issue = list_entry(m["name"], source, out_dir)
        entries.append(entry)
        if issue is not None:
            issues.append(issue)
    data["models"] = entries
    return reported_project, sdk_info, data, issues, ExitCode.SUCCESS


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


def _vela_vendor_config_status() -> tuple[bool, str | None]:
    """The OPTIONAL vendor vela `.ini` row's verdict -- is `ALP_VELA_CONFIG`
    pointing at a file `VelaAdapter.compile()` could pass to `vela --config`?

    Read-only and non-spawning like every other probe here: `os.environ.get` +
    `Path.is_file`, through the adapter's own `_vendor_config_path()` so doctor
    and the compile answer the same question from one implementation. It reads
    no metadata: WHICH vendor file a given part wants is a per-SoC fact
    (`npu_toolchain.vela.vendor_config_filename`), and doctor reports
    host-toolchain facts that hold whatever project it was run from.

    Returns `(available, reason)`. The reason MUST make it plain that absence
    is not a fault: without this file vela uses Arm's own built-in system
    config, which is exactly what tan's reported arena/SRAM figures describe --
    the vendor file buys a vendor-tuned bandwidth/latency model, not
    correctness. A customer without a license is complete, not broken.
    """
    raw = os.environ.get(_VELA_CONFIG_ENV)
    if not raw:
        return False, (
            f"OPTIONAL, not a fault: {_VELA_CONFIG_ENV} is not set, so vela uses Arm's "
            "built-in system config -- the arena/SRAM figures tan reports describe that "
            "model and are correct. A licensed customer may set it to their vendor vela "
            "config .ini (e.g. Alif's ensemble_vela.ini) for the vendor-tuned profile"
        )
    if _vendor_config_path() is None:
        return False, (
            f"OPTIONAL, not a fault: {_VELA_CONFIG_ENV}={raw} does not name a readable "
            "file, so it is ignored and vela uses Arm's built-in system config -- point "
            "it at the vendor vela config .ini or unset it"
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

    # OPTIONAL prerequisites, reported separately from `backends[]` so an
    # absent one cannot read as a broken backend (`model_doctor.optional_row`).
    vendor_config_available, vendor_config_reason = _vela_vendor_config_status()
    optional_rows = [
        optional_row("ethos_u", tool=_VELA_CONFIG_ENV,
                     available=vendor_config_available, reason=vendor_config_reason),
    ]

    data: dict[str, Any] = {
        "schemaVersion": DOCTOR_DATA_SCHEMA_VERSION,
        "backends": [row.as_dict() for row in rows],
        # Additive: `schemaVersion` stays "1". A consumer reading `backends[]`
        # sees the same list it always did, and one that does not know this key
        # ignores it -- which is the correct outcome for an enhancement nobody
        # is required to have.
        "optional": [row.as_dict() for row in optional_rows],
    }
    return reported_project, sdk_info, data, issues, ExitCode.SUCCESS


def _empty_data(subcommand: str | None) -> dict[str, Any]:
    """The `data` shape for a refusal that never reached `_run_build`/
    `_run_doctor`/`_run_check`/`_run_list` -- each subcommand's OWN empty
    payload shape, not a generic stand-in, so a consumer parsing
    `data.backends`/`data.built`/`data.models` off a refusal envelope gets the
    same shape it would from a run that resolved nothing."""
    if subcommand == "doctor":
        return {"schemaVersion": DOCTOR_DATA_SCHEMA_VERSION, "backends": [], "optional": []}
    if subcommand == "check":
        return {"schemaVersion": CHECK_DATA_SCHEMA_VERSION, "sku": None, "exact": False, "models": []}
    if subcommand == "list":
        return {"schemaVersion": LIST_DATA_SCHEMA_VERSION, "sku": None, "models": []}
    return {"schemaVersion": DATA_SCHEMA_VERSION, "sku": None, "built": []}


def model(
    subcommand: str = typer.Argument(
        None, metavar="SUBCOMMAND", help="build | doctor | check | list."
    ),
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
    exact: bool = typer.Option(
        False,
        "--exact",
        help="With `check`: attempt a real compile (Ethos-U only, via `vela`) "
        "instead of the static screen. Ignored by `build`/`doctor`/`list`.",
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
) -> None:
    """Compile + package board.yaml `models:` into `.alpmodel` packages
    (`build`), report NPU-compiler toolchain availability (`doctor`),
    statically screen a declared model's NPU eligibility (`check`), or list
    what is declared next to what is already built (`list`)."""
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
                # OPTIONAL prerequisites, after the backend rows and never
                # spelled `unavailable`: the word this branch leads with is
                # what stops a licensed-only enhancement reading as a fault
                # in a scrollback (`tan.core.model_doctor.optional_row`).
                for row in data.get("optional", []):
                    tool = row["tool"] or "-"
                    if row["available"]:
                        print(
                            f"{row['backend']}: optional (tool={tool}) present",
                            file=sys.stderr,
                        )
                    else:
                        suffix = f" -- {row['reason']}" if row["reason"] else ""
                        print(
                            f"{row['backend']}: optional (tool={tool}) not in use{suffix}",
                            file=sys.stderr,
                        )
            elif subcommand == "list":
                # `list`: checked by SUBCOMMAND, not by `"models" in data` --
                # `check`'s payload carries that same key, so shape alone
                # cannot tell the two apart (tan-cli#674). Rendered from the
                # SAME serialised `data` dict `--format json` emits
                # (`tan.core.model_list.render_list_text`), the identical
                # one-source-two-renderers split `doctor`/`check` above use.
                if not data.get("models") and not errors:
                    print(
                        "model list: no `models:` declared in board.yaml; nothing to list.",
                        file=sys.stderr,
                    )
                for line in render_list_text(data):
                    print(line, file=sys.stderr)
            elif "models" in data:
                # `check`: rendered from the SAME serialised `data` dict
                # `--format json` emits (`tan.core.model_check.render_check_text`),
                # the identical one-source-two-renderers split `doctor`'s own
                # branch above already uses.
                if not data.get("models") and not errors:
                    print(
                        "model check: no `models:` declared in board.yaml; nothing to check.",
                        file=sys.stderr,
                    )
                for line in render_check_text(data):
                    print(line, file=sys.stderr)
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
        elif subcommand == "check":
            project_, sdk, data, issues, exit_code = _run_check(
                context=context,
                metadata_root=metadata_root,
                sdk_root=sdk_root,
                exact=exact,
            )
        elif subcommand == "list":
            project_, sdk, data, issues, exit_code = _run_list(
                context=context,
                out=out,
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
