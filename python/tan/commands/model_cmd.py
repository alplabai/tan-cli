# SPDX-License-Identifier: Apache-2.0
"""`tan model build` -- compile + package `board.yaml`'s `models:` block into
`.alpmodel` packages.

Port of `scripts/alp_cli/model.py` (51 lines): the board.yaml discovery,
per-model source/compile-option path resolution, and the `built <path>`
summary all move here, in-process, exactly as they read there. What does NOT
move is `alp_model.build.build_model` itself -- the compiler-adapter engine
(CPU/Vela/DRP-AI/DeepX, `scripts/alp_model/`) that does the actual work. That
engine needs vendor NPU-compiler tooling only the SDK checkout's own Python
environment carries (DeepX's `dxcom` is license-gated), so this command
resolves the SDK checkout and its Python the same way `generate_cmd`'s
spawned-emitter escape hatch does, then runs ONE small driver script under it
(`_DRIVER`) that imports `alp_model.build` and calls it per model, reporting
back over stdout as one JSON document.

This is a REAL implementation, not a forward: it never spawns `python -m
alp_cli`, so `alp_cli` stops being load-bearing for `tan model` (the point of
this port -- see `crates/tan-cli/src/commands/sdk_cli.rs`'s module doc for
what it is replacing). Unlike that Rust forwarder, a resolvable SDK is
required unconditionally -- `alp_model` lives under `<sdk>/scripts`, and there
is no path that avoids importing it.

**Deliberate divergence 1 from the oracle**: `alp_cli/model.py` has no
try/except around `build_model()` at all, so a build failure (e.g. "no blob
compiled for model") tracebacks the whole click command. Every command in
this port instead resolves to a coded issue, never a traceback (the
established rule -- see `generate_cmd`'s module doc) -- so a per-model
failure here is caught in the driver and reported as a `model.build-failed`
issue, and the run continues to the next model rather than aborting the
whole batch.

**Deliberate divergence 2 from the oracle**: the oracle has no equivalent of
a spawned driver at all (it calls `build_model()` in-process), so it cannot
observe a driver that exits 0 having silently produced no result for a
declared model. This port can, and treats that as a failure: an empty/short
`_DRIVER` stdout is never coerced to `{}` (an empty document now falls
through to the same `JSONDecodeError` branch a malformed one already does),
and a driver that reports fewer `results` than models it was handed raises
`model.internal-failure` naming the missing model(s) rather than silently
reporting `built: []` -- indistinguishable otherwise from the legitimate
no-models no-op above.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer

from tan.commands.build_cmd import _planner_python
from tan.commands.build_output import resolve_metadata_sdk_root, resolve_project_context
from tan.commands.doctor_cmd import resolve_manifest_python_floor
from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS
from tan.core.global_flags import accept_global_flags
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

#: Seconds the compile driver may run. Generous -- a cold NPU-compiler
#: invocation (Vela, DRP-AI, DeepX) can be slow, and several models may be
#: queued in one run. Bounded regardless, so a wedged vendor tool cannot hang
#: a `--format json` consumer with no envelope and no error.
_BUILD_TIMEOUT_S = 1800

#: Driver run under the resolved SDK's Python, with `PYTHONPATH` pointed at
#: `<sdk>/scripts` so `alp_model` resolves. Reads one JSON payload on stdin
#: (`{"models": [{"name", "source", "sku", "outDir", "metadataRoot",
#: "compileOpts"}]}`), writes one JSON document to stdout
#: (`{"results": [{"name", "ok", "path"|"error"}]}`). No argv, no env beyond
#: what the caller already sets -- keeping the driver's own surface to a
#: single stdin/stdout contract is what lets it stay this short.
_DRIVER = """
import json, sys
from pathlib import Path

payload = json.loads(sys.stdin.read())
results = []
try:
    from alp_model.build import build_model
except Exception as err:
    print(json.dumps({"importError": f"{type(err).__name__}: {err}"}))
    sys.exit(0)

for m in payload["models"]:
    try:
        out = build_model(
            sku=payload["sku"],
            name=m["name"],
            source=Path(m["source"]),
            out_dir=Path(payload["outDir"]),
            metadata_root=Path(payload["metadataRoot"]),
            compile_opts=m.get("compileOpts"),
        )
        results.append({"name": m["name"], "ok": True, "path": str(out)})
    except Exception as err:
        results.append({
            "name": m["name"], "ok": False,
            "error": f"{type(err).__name__}: {err}",
        })
print(json.dumps({"results": results}))
"""


class ModelError(Exception):
    """A refusal whose issue code and exit code are already decided."""

    def __init__(self, code: str, message: str, exit_code: ExitCode) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _resolve_compile(block: dict | None, base: Path) -> dict | None:
    """Port of `model.py::_resolve_compile`: every string value in each
    per-backend compile block becomes an absolute path relative to the
    `board.yaml` dir -- every current opts value is a path."""
    if not block:
        return None
    return {
        backend: {
            k: (str((base / v).resolve()) if isinstance(v, str) else v)
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


def _run_driver(python: str, sdk_scripts: Path, payload: dict) -> dict:
    """Spawn `_DRIVER` under `python` with `<sdk>/scripts` prepended to
    `PYTHONPATH`, feed `payload` on stdin, and parse its one line of stdout.
    Raises `ModelError` for every way the spawn itself can fail; a per-model
    build failure is NOT one of those -- it comes back inside the parsed
    result and is turned into an issue by the caller."""
    pythonpath = os.pathsep.join(
        [str(sdk_scripts), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
    )
    env = {**os.environ, "PYTHONPATH": pythonpath}
    try:
        out = subprocess.run(
            [python, "-c", _DRIVER],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=_BUILD_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise ModelError(
            "model.build-timeout",
            f"model build timed out after {_BUILD_TIMEOUT_S}s.",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    except OSError as err:
        raise ModelError(
            "model.internal-failure",
            f"failed to launch `{python}`: {err}",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    if out.returncode != 0:
        stderr = (out.stderr or "").strip()
        raise ModelError(
            "model.internal-failure",
            f"model build driver exited with code {out.returncode}: "
            f"{stderr or '(no output)'}",
            ExitCode.RUNTIME_FAILURE,
        )
    # The last non-empty line, not the whole of stdout -- mirrors the same
    # defence `_python_too_old` already applies one screen up in this file,
    # against a future adapter `print()` or an inherited-stdout vendor tool
    # polluting the one JSON document the driver is meant to write. Empty
    # stdout (nothing printed at all -- a driver that silently produced
    # nothing) falls through to `json.loads("")`, which raises
    # `JSONDecodeError` below rather than being papered over as `{}`: a
    # driver that exits 0 having produced nothing is a failure, not a
    # legitimate no-op.
    lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
    try:
        return json.loads(lines[-1] if lines else "")
    except json.JSONDecodeError as err:
        raise ModelError(
            "model.internal-failure",
            f"model build driver produced unparsable output: {err}",
            ExitCode.INTERNAL_FAILURE,
        ) from err


def _python_too_old(python: str, floor: tuple[int, int]) -> str | None:
    """A message when `python` is below `floor`, else `None` -- also for
    "could not tell" (a missing/broken interpreter surfaces on its own at the
    real spawn). Mirrors `generate_cmd._python_too_old`; `floor` is the
    resolved SDK's OWN declared floor from
    `doctor_cmd.resolve_manifest_python_floor` -- not a second hardcoded 3.10
    that could drift from the manifest's, or from `generate_cmd`'s own copy."""
    try:
        out = subprocess.run(
            [python, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if out.returncode != 0:
        return None
    try:
        major, minor = (int(p) for p in out.stdout.strip().splitlines()[-1].split(".")[:2])
    except (IndexError, ValueError):
        return None
    if (major, minor) >= floor:
        return None
    return (
        f"Python {major}.{minor} found at `{python}`, but alp-sdk requires Python "
        f"{floor[0]}.{floor[1]}+. Put a newer `python` first on PATH."
    )


def _run_build(
    *,
    board: str,
    out: str,
    metadata_root: str | None,
    project: str | None,
    sdk_root: str | None,
) -> tuple[Project, SdkInfo | None, dict, list[Issue], ExitCode]:
    # `--board` plays the role `--board-yaml` does everywhere else, so the
    # SAME project-context resolution applies (I-31); the envelope's `project`
    # and `sdk` fields come from this ONE resolution, matching `size`/`image`
    # (`build_output.resolve_project_context`'s own doc: the envelope's `sdk`
    # block must be what THIS resolution produced, never a second lookup).
    context = resolve_project_context(project, board, sdk_root)
    workspace_root = Path(context.workspace_root)
    board_path = Path(context.board_yaml)
    reported_project = context.project()
    sdk_info = context.sdk

    # The metadata-reading resolution is DELIBERATELY separate and wider
    # (`build_output.resolve_metadata_sdk_root`'s own doc): a child/sibling
    # checkout the project-context tier chain does not consider can still
    # supply `alp_model`, in which case `sdk_info` stays absent while the
    # build still runs against it -- same divergence `tan size`'s budget
    # resolution already allows.
    resolved_sdk = resolve_metadata_sdk_root(sdk_root, context.workspace_root)
    if resolved_sdk is None:
        raise ModelError(
            "model.sdk-root-unresolved",
            # `tan sdk switch` refuses in this build (tan-cli#305) -- kept the
            # two mechanisms that actually work here (`--sdk-root`, placing
            # the project near a checkout) and swapped the third for
            # NO_SDK_NEXT_STEPS's honest "how to get one at all".
            "alp-sdk root is unresolved. Use --sdk-root, place the project near an "
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

    driver_models = []
    for m in models:
        if not isinstance(m, dict) or "name" not in m or "source" not in m:
            raise ModelError(
                "model.board-yaml-invalid",
                f"{board_path}: every `models:` entry needs `name` and `source`.",
                ExitCode.VALIDATION_FAILURE,
            )
        source = (base / m["source"]).resolve()
        driver_models.append({
            "name": m["name"],
            "source": str(source),
            "compileOpts": _resolve_compile(m.get("compile"), base),
        })

    python = _planner_python(str(workspace_root), str(resolved_sdk))
    floor, _floor_source = resolve_manifest_python_floor(str(resolved_sdk))
    too_old = _python_too_old(python, floor)
    if too_old is not None:
        raise ModelError("model.python-too-old", too_old, ExitCode.RUNTIME_FAILURE)

    payload = {
        "sku": sku,
        "outDir": str(out_dir),
        "metadataRoot": str(metadata_dir),
        "models": driver_models,
    }
    result = _run_driver(python, resolved_sdk / "scripts", payload)

    if "importError" in result:
        raise ModelError(
            "model.internal-failure",
            f"could not import alp_model from {resolved_sdk / 'scripts'}: "
            f"{result['importError']}",
            ExitCode.INTERNAL_FAILURE,
        )

    driver_results = result.get("results", [])
    if len(driver_results) != len(driver_models):
        # A driver that exits 0 but reports fewer results than models it was
        # asked to build is a failure, not a partial success -- otherwise a
        # wedged/short-circuited driver is indistinguishable from the
        # legitimate no-models no-op (both would report `ok: true`).
        reported = {r.get("name") for r in driver_results if isinstance(r, dict)}
        missing = [m["name"] for m in driver_models if m["name"] not in reported]
        raise ModelError(
            "model.internal-failure",
            f"model build driver reported {len(driver_results)} of "
            f"{len(driver_models)} model(s); missing: {', '.join(missing)}.",
            ExitCode.INTERNAL_FAILURE,
        )

    issues: list[Issue] = []
    built: list[str] = []
    for r in driver_results:
        if r.get("ok"):
            built.append(r["path"])
        else:
            issues.append(
                Issue(
                    "model.build-failed",
                    "error",
                    f"model '{r.get('name')}': {r.get('error', 'build failed')}",
                )
            )
    data["built"] = built
    exit_code = ExitCode.SUCCESS if not issues else ExitCode.WRITE_FAILURE
    return reported_project, sdk_info, data, issues, exit_code


def model(
    subcommand: str = typer.Argument(None, metavar="SUBCOMMAND", help="build."),
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
    """Compile + package board.yaml `models:` into `.alpmodel` packages."""
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
            for path in data.get("built", []):
                print(f"built {path}", file=sys.stderr)
            if not data.get("built") and not issues:
                print(
                    "model: no `models:` declared in board.yaml; nothing to build.",
                    file=sys.stderr,
                )
            for issue in issues:
                print(f"model: {issue.message}", file=sys.stderr)
        raise typer.Exit(int(exit_code))

    if subcommand != "build":
        finish(
            Project(root=None, board_yaml=None),
            None,
            {"schemaVersion": DATA_SCHEMA_VERSION, "sku": None, "built": []},
            [
                Issue(
                    "model.unknown-subcommand",
                    "error",
                    f"Unknown model subcommand: {'(none)' if subcommand is None else subcommand}. "
                    "Available: build.",
                )
            ],
            ExitCode.RUNTIME_FAILURE,
        )
        return

    try:
        project_, sdk, data, issues, exit_code = _run_build(
            board=board,
            out=out,
            metadata_root=metadata_root,
            project=project,
            sdk_root=sdk_root,
        )
    except ModelError as err:
        finish(
            Project(root=None, board_yaml=None),
            None,
            {"schemaVersion": DATA_SCHEMA_VERSION, "sku": None, "built": []},
            [Issue(err.code, "error", err.message)],
            err.exit_code,
        )
        return
    except Exception as err:  # noqa: BLE001 -- the envelope IS the error contract
        finish(
            Project(root=None, board_yaml=None),
            None,
            {"schemaVersion": DATA_SCHEMA_VERSION, "sku": None, "built": []},
            [
                Issue(
                    "model.internal-failure",
                    "error",
                    f"model build failed unexpectedly: {type(err).__name__}: {err}",
                )
            ],
            ExitCode.INTERNAL_FAILURE,
        )
        return

    finish(project_, sdk, data, issues, exit_code)


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
