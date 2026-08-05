# SPDX-License-Identifier: Apache-2.0
"""`tan size` -- measure each Zephyr slice's `zephyr.elf` FLASH/RAM and compare
it to the SoM memory budget resolved from SoC metadata.

Port of `crates/tan-cli/src/commands/size.rs`: the IO/subprocess half. Every
measurement / budget / classification / render *decision* is pure in
`tan.core.size`, so this module is only file reads, one subprocess, and the
envelope.

**No `--os`, no `--backend`.** A slice's runtime arrives already resolved in the
manifest (I-01); this command reads `os` and never chooses it.

**tan learns no hardware fact.** The budget is whatever `<sdk>/metadata/**` says
-- SKU, variant order code, SRAM bank names and `tcm_kb` are all read out of
those files at runtime and none is spelled here (I-26). The SDK is READ, never
executed: no `alp_project.py`, no `alp_orchestrate`, no `west` (I-32 and its
anti-pattern #22 -- shelling the SDK gives a command a checkout dependency it
does not have, and no gate catches the first one). A missing SDK is not fatal:
the budget resolves `unknown` and measurement still runs.

Divergences from the oracle, all deliberate and all pinned by
`tests/parity/test_image_size_oracle.py`:
  - a `zephyr.elf` (or `rom.json`/`ram.json`) that landed in west's nested
    `<build_dir>/build/` is FOUND rather than reported `not-built`. That is I-18:
    `west build` is emitted with no `-d`, and the shipped binary reconciles the
    nesting at BUILD time (`resolve_zephyr_artefact` writes the resolved paths
    into the manifest it rewrites). This port's `tan build` does not yet write
    that manifest, so the reconciliation happens on the read side -- see
    `tan.core.system_manifest`. The un-nested path is always tried first, so
    every input the oracle measures is measured identically.
  - the size tool gets a timeout. The oracle has none; a wedged
    `arm-zephyr-eabi-size` would hang `tan size` forever.
  - a PyYAML that is declared but missing anyway (a frozen artifact built from a
    stale venv) is a coded `size.manifest-invalid`, not a traceback.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.commands.build_output import (
    ManifestInvalid,
    ManifestUnavailable,
    ProjectContext,
    load_manifest,
    read_sdk_som_and_soc,
    resolve_app_base,
    resolve_build_root,
    resolve_metadata_sdk_root,
    resolve_project_context,
)
from tan.commands.sdk_cmd import global_default_foreign_project_issue, project_pin_issue
from tan.core.global_flags import accept_global_flags
from tan.core.pending import is_pending_placeholder
from tan.core.size import (
    MemoryBudget,
    SliceSize,
    budget_note_only,
    build_size_report,
    classify,
    footprint_total,
    over_budget_rows,
    parse_berkeley_size,
    render_table_lines,
    resolve_budget,
    resolve_variant,
    sizes_from_elf_sections,
    sram_banks,
    unknown_budget_rows,
)
from tan.core.system_manifest import (
    load_yaml_document,
    slice_elf_candidates,
    slice_footprint_dirs,
)
from tan.env import no_color_requested
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

#: Size tools probed on PATH, most-specific first. The Zephyr SDK ships
#: `arm-zephyr-eabi-size`; an LLVM toolchain ships `llvm-size`; host binutils
#: ships `size`. All speak the same Berkeley columns.
SIZE_TOOLS = ("arm-zephyr-eabi-size", "llvm-size", "size")

#: Seconds before the size tool is abandoned and the next measurement rung tried.
#: The oracle spawns it unbounded; a hung tool there hangs the whole command, and
#: every subprocess in this port is bounded.
_SIZE_TOOL_TIMEOUT = 30

#: Streaming read size for the ELF, so a large image is not slurped whole just
#: to read its section headers. Section headers sit at the END of an ELF, so the
#: file does have to be read -- but bounded, and refused above this.
_MAX_ELF_BYTES = 512 * 1024 * 1024


def _find_on_path(command: str) -> bool:
    """Whether `command` resolves on PATH -- port of `util::command_on_path`.

    Hand-rolled rather than `shutil.which`, which on Windows ALWAYS inserts
    `os.curdir` ahead of the search list (even when an explicit `path=` is
    given). `where.exe` has the same behaviour, and the oracle walks PATH by hand
    for exactly this reason: a project checked out with its own `size.exe` at its
    root would otherwise be reported as "available" and then SPAWNED. A project
    directory must never supply the executable.
    """
    raw = os.environ.get("PATH")
    if not raw:
        return False
    dirs = [d for d in raw.split(os.pathsep) if d]
    if os.name != "nt":
        for directory in dirs:
            candidate = Path(directory) / command
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return True
        return False
    pathext = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    exts = [e for e in pathext.split(";") if e]
    has_ext = Path(command).suffix != ""
    for directory in dirs:
        base = Path(directory)
        if has_ext:
            if (base / command).is_file():
                return True
        elif any((base / f"{command}{ext}").is_file() for ext in exts):
            return True
    return False


def _sizes_from_size_tool(size_bin: str, elf: str) -> tuple[int, int] | None:
    """Run the size tool on `elf` and parse its Berkeley output. `None` when the
    tool fails to spawn, times out, exits non-zero, or its output does not parse
    -- every one of which falls through to the next measurement rung rather than
    failing the command."""
    try:
        out = subprocess.run(
            [size_bin, elf],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SIZE_TOOL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if out.returncode != 0:
        return None
    return parse_berkeley_size(out.stdout or "")


def _read_bytes(path: str) -> bytes | None:
    try:
        if os.stat(path).st_size > _MAX_ELF_BYTES:
            return None
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _is_file(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except (OSError, ValueError):
        # A path the OS refuses to stat at all (a too-long name, an embedded NUL
        # from a hand-edited manifest) is "not a file", never an exception that
        # escapes the envelope.
        return False


def _extract_sizes(
    elf_candidates: list[str], footprint_dirs: list[str], size_bin: str | None
) -> tuple[tuple[int, int] | None, str | None, str]:
    """`(sizes, source_label, probed_elf)` via the best available source, in the
    oracle's order: the size tool (only if the elf exists), then the elf's own
    section headers, then `rom.json` + `ram.json`.

    `probed_elf` is the path named in a `not-built` note -- the FIRST candidate,
    which is the one the oracle would have named.
    """
    for elf in elf_candidates:
        if not _is_file(elf):
            continue
        if size_bin is not None:
            sizes = _sizes_from_size_tool(size_bin, elf)
            if sizes is not None:
                return sizes, "size-tool", elf_candidates[0]
        # Middle rung: no size tool, or it failed to parse -- read the section
        # headers directly so a present elf is still measured. The `pyelftools`
        # label is kept for JSON parity with the retired command.
        raw = _read_bytes(elf)
        if raw is not None:
            sizes = sizes_from_elf_sections(raw)
            if sizes is not None:
                return sizes, "pyelftools", elf_candidates[0]
    for directory in footprint_dirs:
        rom_text = _read_text(os.path.join(directory, "rom.json"))
        ram_text = _read_text(os.path.join(directory, "ram.json"))
        rom = None if rom_text is None else footprint_total(rom_text)
        ram = None if ram_text is None else footprint_total(ram_text)
        if rom is not None and ram is not None:
            return (rom, ram), "rom/ram.json", elf_candidates[0]
    return None, None, elf_candidates[0]


def _read_som_preset(path: str) -> tuple[str, str | None] | None:
    """`(silicon, silicon_variant)` from a SoM preset, or `None` when it cannot
    be used.

    Only the two fields the budget needs, plus the SAME `schema_version == 1`
    guard `parse_som_preset` applies -- a preset the oracle refuses must be
    `unreadable SoM preset for <sku>` here too, not silently half-read. `TBD` is
    dropped to absent exactly as `str_clean` does, which is what variant
    resolution wants.
    """
    text = _read_text(path)
    if text is None:
        return None
    try:
        root = load_yaml_document(text)
    except Exception:  # noqa: BLE001 -- SystemManifestError or any PyYAML failure
        return None
    if not isinstance(root, dict):
        return None
    version = root.get("schema_version")
    if isinstance(version, bool) or version != 1:
        return None
    silicon = _clean_str(root.get("silicon")) or ""
    return silicon, _clean_str(root.get("silicon_variant"))


def _clean_str(value: Any) -> str | None:
    """`str_clean`: a string, with the `TBD` sentinel dropped to absent.

    Delegates to `tan.core.pending.is_pending_placeholder` (#276) for the
    trimmed, non-case-folded, non-substring comparison shared with `tan
    flash` -- not a second hand-rolled `== "TBD"`."""
    if not isinstance(value, str) or is_pending_placeholder(value):
        return None
    return value


def _as_f64(value: Any) -> float | None:
    """serde_json's `as_f64`: a JSON number, never a bool and never a string."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _read_soc(path: str) -> tuple[list[dict], float | None, list[tuple[str, float | None]]]:
    """`(variants, soc_flash_mb, soc_cores)` from a SoC JSON, defaulting to
    empty on any failure.

    `variants` is all-or-nothing on purpose: the oracle deserializes the whole
    list with `unwrap_or_default()`, so ONE malformed entry empties it. Reading
    the good ones here would resolve a budget the shipped binary does not.
    """
    text = _read_text(path)
    if text is None:
        return [], None, []
    try:
        soc = json.loads(text)
    except (ValueError, RecursionError):
        return [], None, []
    if not isinstance(soc, dict):
        return [], None, []

    variants: list[dict] = []
    raw_variants = soc.get("variants")
    if raw_variants is not None:
        variants = _coerce_variants(raw_variants)

    soc_flash_mb = _as_f64(soc.get("soc_flash_mb"))

    soc_cores: list[tuple[str, float | None]] = []
    raw_cores = soc.get("cores")
    if isinstance(raw_cores, list):
        for core in raw_cores:
            if not isinstance(core, dict):
                continue
            core_id = core.get("id")
            if not isinstance(core_id, str):
                continue  # `filter_map` skips an entry with no string `id`
            soc_cores.append((core_id, _as_f64(core.get("tcm_kb"))))
    return variants, soc_flash_mb, soc_cores


def _coerce_variants(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return []
        order_code = entry.get("order_code")
        if order_code is not None and not isinstance(order_code, str):
            return []
        skus = entry.get("alp_module_skus", [])
        if not isinstance(skus, list) or any(not isinstance(s, str) for s in skus):
            return []
        mram = entry.get("mram_mb")
        if mram is not None and (isinstance(mram, bool) or not isinstance(mram, (int, float))):
            return []
        banks = entry.get("sram_banks_kb", {})
        if not isinstance(banks, dict):
            return []
        out.append(entry)
    return out


def _resolve_slice_budget(
    sku: str | None, core_id: str, metadata_root: str | None
) -> MemoryBudget:
    """One core's `(flash_total, ram_total)` from the SoM preset + SoC JSON under
    `<sdk>/metadata`. The pure FLASH/RAM/note logic is `core.size.resolve_budget`;
    this only reads the files."""
    if sku is None:
        return budget_note_only("no SKU in manifest")
    if metadata_root is None:
        return budget_note_only(f"no SoM preset for {sku}")

    preset_path = os.path.join(metadata_root, "e1m_modules", f"{sku}.yaml")
    if not _is_file(preset_path):
        return budget_note_only(f"no SoM preset for {sku}")
    walked = read_sdk_som_and_soc(metadata_root, sku)
    if walked is None:
        return budget_note_only(f"unreadable SoM preset for {sku}")
    _silicon, silicon_variant, variants, soc_flash_mb, soc_cores = walked

    variant = resolve_variant(silicon_variant, sku, variants)
    mram_mb = None if variant is None else _as_f64(variant.get("mram_mb"))
    banks = [] if variant is None else sram_banks(variant)
    return resolve_budget(core_id, mram_mb, soc_flash_mb, banks, soc_cores)


def _measure_slice(
    slice_: dict,
    build_root: str,
    sku: str | None,
    metadata_root: str | None,
    size_bin: str | None,
) -> SliceSize:
    """Measure + budget one manifest slice into a `SliceSize` row."""
    core_id = str(slice_.get("core_id"))
    os_name = str(slice_.get("os"))

    if os_name != "zephyr":
        return SliceSize(
            core_id=core_id,
            os=os_name,
            status="n/a",
            note="no Zephyr image (Yocto/baremetal)",
        )

    sizes, source, probed_elf = _extract_sizes(
        slice_elf_candidates(slice_, build_root),
        slice_footprint_dirs(slice_, build_root),
        size_bin,
    )
    budget = _resolve_slice_budget(sku, core_id, metadata_root)

    if sizes is None:
        return SliceSize(
            core_id=core_id,
            os=os_name,
            status="not-built",
            flash_total=budget.flash_total,
            ram_total=budget.ram_total,
            note=budget.note,
            notes=[f"no footprint source at {probed_elf}"],
        )

    flash_used, ram_used = sizes
    if budget.flash_total is None and budget.ram_total is None:
        status = "no-budget"
    else:
        status = classify(
            flash_used, budget.flash_total, ram_used, budget.ram_total
        )
    return SliceSize(
        core_id=core_id,
        os=os_name,
        status=status,
        flash_used=flash_used,
        flash_total=budget.flash_total,
        ram_used=ram_used,
        ram_total=budget.ram_total,
        source=source,
        note=budget.note,
    )


def _use_color(no_color: bool, ci: bool) -> bool:
    """`Theme::from_args`: human text goes to stderr, so the TTY probe is against
    stderr."""
    if no_color or ci or no_color_requested():
        return False
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _join_core_ids(rows: list[SliceSize]) -> str:
    return ", ".join(r.core_id for r in rows)


@dataclass
class _Outcome:
    """What one run produced. Built and returned, never emitted in place, so the
    exception guard in [`size`] can wrap the whole computation without also
    catching `typer.Exit` (a RuntimeError subclass, not a SystemExit)."""

    exit_code: ExitCode
    data: dict[str, Any]
    project: Project
    issues: list[Issue]
    text: list[str]
    #: Absent, never `null`. Carried from the project resolution that produced
    #: `project`, never a second lookup -- so it is present on the manifest-error
    #: paths too (`Envelope::new` reads what `resolve_cli_project_context`
    #: recorded, and that ran before the manifest was opened).
    sdk: SdkInfo | None = None


def _error_outcome(
    project: Project, sdk: SdkInfo | None, code: str, message: str
) -> _Outcome:
    """A hard manifest error: exit 1, an empty `alp-size/1` report as `data` so
    the envelope shape stays stable, and the message prefixed for text mode."""
    return _Outcome(
        ExitCode.RUNTIME_FAILURE,
        build_size_report([]),
        project,
        [Issue(code, "error", message)],
        [f"size: {message}"],
        sdk,
    )


def _run(
    *,
    app_path: str,
    build_root_arg: str | None,
    board: str | None,
    fail_over_budget: bool,
    project_arg: str | None,
    board_yaml_arg: str | None,
    sdk_root_arg: str | None,
    json_mode: bool,
    no_color: bool,
    ci: bool,
) -> _Outcome:
    context: ProjectContext = resolve_project_context(
        project_arg, board_yaml_arg, sdk_root_arg
    )
    project = context.project()

    app_base = resolve_app_base(app_path, context.workspace_root)
    build_root = resolve_build_root(build_root_arg, app_base)
    metadata_sdk = resolve_metadata_sdk_root(sdk_root_arg, context.workspace_root)
    metadata_root = (
        None if metadata_sdk is None else os.path.join(str(metadata_sdk), "metadata")
    )

    try:
        _text, manifest = load_manifest(build_root)
    except ManifestUnavailable as err:
        return _error_outcome(
            project,
            context.sdk,
            "size.manifest-unavailable",
            f"no system-manifest.yaml at {err.path}; run `tan build` first "
            f"({err.detail}).",
        )
    except ManifestInvalid as err:
        return _error_outcome(
            project, context.sdk, "size.manifest-invalid", f"{err.path}: {err.detail}"
        )

    sku = board if board is not None else (manifest.sku.strip() or None)
    size_bin = next((t for t in SIZE_TOOLS if _find_on_path(t)), None)
    rows = [
        _measure_slice(s, build_root, sku, metadata_root, size_bin)
        for s in manifest.slices
    ]

    text: list[str] = []
    issues: list[Issue] = []
    exit_code = ExitCode.SUCCESS

    pin_issue = project_pin_issue(context.broken_project_pin, context.sdk_source_tier)
    if pin_issue is not None:
        issues.append(pin_issue)
    foreign_issue = global_default_foreign_project_issue(context.foreign_global_default_for)
    if foreign_issue is not None:
        issues.append(foreign_issue)

    if not json_mode:
        text.extend(render_table_lines(rows, _use_color(no_color, ci)))
        if size_bin is None:
            text.append(
                "size: no size tool on PATH (arm-zephyr-eabi-size / llvm-size / "
                "size); measured from ELF section headers (or rom.json+ram.json)."
            )

    if fail_over_budget:
        unknown = unknown_budget_rows(rows)
        if unknown:
            message = (
                f"size: budget unknown for [{_join_core_ids(unknown)}] "
                "— skipped by --fail-over-budget (no guess)."
            )
            if not json_mode:
                text.append(message)
            issues.append(Issue("size.budget-unknown", "info", message))
        over = over_budget_rows(rows)
        if over:
            message = f"size: over budget: [{_join_core_ids(over)}]."
            if not json_mode:
                text.append(message)
            issues.append(Issue("size.over-budget", "error", message))
            exit_code = ExitCode.RUNTIME_FAILURE

    return _Outcome(
        exit_code, build_size_report(rows), project, issues, text, context.sdk
    )


def size(
    ctx: typer.Context,
    app_path: str = typer.Argument(
        ".",
        metavar="APP_PATH",
        help="Application source directory (default: '.'). build_root defaults "
        "to <APP_PATH>/build. Overrides the global --project when not '.'.",
    ),
    build_root: str = typer.Option(
        None,
        "--build-root",
        metavar="PATH",
        help="Override the build root holding system-manifest.yaml "
        "(default: <APP_PATH>/build).",
    ),
    board: str = typer.Option(
        None,
        "--board",
        metavar="SKU",
        help="Override the SoM SKU used to resolve the memory budget (default: "
        "hw_info.sku from the manifest). Distinct from --board-yaml.",
    ),
    fail_over_budget: bool = typer.Option(
        False,
        "--fail-over-budget",
        help="Exit non-zero if any slice exceeds its resolved budget (slices "
        "with an unknown budget are skipped + reported, never guessed).",
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
    no_color: bool = typer.Option(False, "--no-color", help="Disable ANSI colour."),
    ci: bool = typer.Option(False, "--ci", help="Non-interactive CI mode."),
) -> None:
    """Report per-slice firmware footprint vs the SoM memory budget."""
    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    json_mode = resolved_format == "json"

    try:
        outcome = _run(
            app_path=app_path,
            build_root_arg=build_root,
            board=board,
            fail_over_budget=fail_over_budget,
            project_arg=project,
            board_yaml_arg=board_yaml,
            sdk_root_arg=sdk_root,
            json_mode=json_mode,
            no_color=no_color,
            ci=ci,
        )
    except Exception as err:  # noqa: BLE001
        # The port's most-repeated defect class: an uncaught exception escapes as
        # a raw traceback, stdout stays EMPTY, and the extension renders nothing
        # at all with no error on either side. Anything reaching here is a tan
        # bug and is reported as one -- with an envelope. Nothing in this handler
        # may itself throw: it only formats `err`, and `Project(None, None)` and
        # `build_size_report([])` are both total.
        outcome = _Outcome(
            ExitCode.INTERNAL_FAILURE,
            build_size_report([]),
            Project(root=None, board_yaml=None),
            [
                Issue(
                    "size.internal-failure",
                    "error",
                    f"size failed unexpectedly: {type(err).__name__}: {err}",
                )
            ],
            ["size: internal failure"],
        )

    if json_mode:
        emit(
            Envelope(
                "size",
                outcome.project,
                outcome.data,
                outcome.issues,
                outcome.exit_code,
                sdk=outcome.sdk,
            )
        )
    else:
        # stdout is the envelope channel and carries nothing else, in either
        # mode; stderr carries no contract of its own.
        stream = typer.get_text_stream("stderr")
        for line in outcome.text:
            stream.write(f"{line}\n")
    raise typer.Exit(int(outcome.exit_code))


# tan-cli#261: adds the five oracle `GlobalArgs` flags this command was still
# missing (`--all`/`--non-interactive`/`--quiet`/`--target`/`--verbose`) on
# top of `--no-color`/`--ci`, already declared and read above (`_use_color`);
# see `tan.core.global_flags`. `ctx: typer.Context` (this command's own
# `_HONOURS_ROOT_FORMAT` seam) is untouched -- appended parameters are all
# keyword-only Options, never repositioned relative to it.
size = accept_global_flags(size)
