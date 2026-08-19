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

Divergences from the retired v0.4.1 oracle, all deliberate.
`tests/parity/test_image_size_oracle.py` pinned them until tan-cli#269 deleted
the oracle axis with `crates/`. They did not go unpinned: the nesting bullet is
held tan-side by `tests/commands/test_size_command.py`'s
`test_i18_nested_west_output_is_measured_not_reported_not_built` and
`test_the_un_nested_path_still_wins_when_both_exist`, and the candidate ORDER
by `tests/core/test_system_manifest.py`'s
`test_i18_nesting_is_probed_after_the_plain_path_never_before`. The size-tool
timeout below is the one that really is unpinned:
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
from tan.commands.sdk_cmd import sdk_resolution_issues
from tan.core.flash_plan import slice_should_flash
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
from tan.core.tool_lookup import resolve_tool
from tan.env import use_color
from tan.envelope import Envelope, Issue, Project, SdkDisclosure, SdkInfo, emit
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


def _find_on_path(command: str) -> str | None:
    """Where `command` resolves on PATH -- the ABSOLUTE path, or `None`.

    Port of `util::command_on_path`, hand-rolled rather than `shutil.which`,
    which on Windows ALWAYS inserts `os.curdir` ahead of the search list (even
    when an explicit `path=` is given). `where.exe` has the same behaviour, and
    the oracle walks PATH by hand for exactly this reason: a project checked out
    with its own `size.exe` at its root would otherwise be reported as
    "available" and then SPAWNED. A project directory must never supply the
    executable.

    **tan-cli#567: it now returns the PATH, not a bool.** The walk above was
    correct and its result was then thrown away -- `_sizes_from_size_tool` was
    handed the bare identity and `subprocess.run` gave it straight back to
    `CreateProcess`, whose documented search order puts the parent process's
    current directory AHEAD of `%PATH%`. The hardened check found the real
    `size.exe`; the spawn then ran the project's. Returning the resolved path
    is what makes the check and the spawn agree, and it is the same
    one-resolver rule tan-cli#510 established for the build spawn -- which is
    why the walk itself is no longer written out here but delegated to
    `tan.core.tool_lookup.resolve_tool`, the ONE copy (tan-cli#532).

    `os.environ`, deliberately: unlike a build slice, `tan size` pins no `PATH`
    of its own, so the environment this resolves against IS the one
    `_sizes_from_size_tool`'s child inherits.
    """
    return resolve_tool(command, os.environ).resolved


def _sizes_from_size_tool(size_bin: str, elf: str) -> tuple[int, int] | None:
    """Run the size tool on `elf` and parse its Berkeley output. `None` when the
    tool fails to spawn, times out, exits non-zero, or its output does not parse
    -- every one of which falls through to the next measurement rung rather than
    failing the command.

    `size_bin` is the ABSOLUTE path [`_find_on_path`] resolved, never a bare
    identity (tan-cli#567). A bare name here would be handed back to the
    platform's own resolver, which on Windows searches the parent process's
    current directory before `%PATH%` -- so a bare `size_bin` is refused
    outright rather than spawned. Refused, not "resolved here": a second
    resolution at the spawn is exactly the drift tan-cli#510 was filed over,
    and this rung failing closed simply falls through to the ELF-section-header
    measurement, which needs no subprocess at all."""
    if not Path(size_bin).is_absolute():
        return None
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


def _read_som_memory_map(path: str) -> list[dict]:
    """The SoM preset's `memory_map:` entries, or `[]` when it declares none
    (tan-cli#747).

    Read here rather than widened into `read_sdk_som_and_soc`'s return tuple:
    that walk is shared with `tan debug-config`, and growing a tuple every
    caller destructures to give ONE of them a field it alone needs is how the
    two readers drift apart -- the thing that walk exists to prevent. The
    preset is small and already on this path, so re-reading it is cheaper than
    a shared shape nobody else wants.

    Deliberately NOT `schema_version`-guarded a second time: the caller has
    already been through `_read_som_preset`/`read_sdk_som_and_soc` for this
    same file, so a preset that fails that guard never reaches here. A
    non-list `memory_map`, or entries that are not mappings, degrade to `[]`
    /are skipped downstream -- an unreadable budget must fall back to
    `mram_mb`, never raise out of `tan size`.
    """
    text = _read_text(path)
    if text is None:
        return []
    try:
        root = load_yaml_document(text)
    except Exception:  # noqa: BLE001 -- SystemManifestError or any PyYAML failure
        return []
    if not isinstance(root, dict):
        return []
    entries = root.get("memory_map")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _clean_str(value: Any) -> str | None:
    """`str_clean`: a string, with the `TBD` sentinel dropped to absent.

    Delegates to `tan.core.pending.is_pending_placeholder` (#276) for the
    trimmed, non-case-folded, non-substring comparison shared with `tan
    flash` -- not a second hand-rolled `== "TBD"`."""
    if not isinstance(value, str) or is_pending_placeholder(value):
        return None
    return value


def _as_f64(value: Any) -> float | None:
    """serde_json's `as_f64`: a JSON number, never a bool and never a string.

    The `float()` is guarded (tan-cli#499 defect 8, REVIEW round). Python's
    `json` reads an arbitrarily long integer literal exactly, so a 400-digit
    `mram_mb` / `soc_flash_mb` / `tcm_kb` in a SoC JSON arrives here as an
    `int` that `float()` cannot represent -- `OverflowError: int too large to
    convert to float`, which escaped to `size`'s catch-all and collapsed the
    WHOLE run: measured `exit 5 size.internal-failure`, `data.slices` empty
    (every other slice's measurement discarded). The first pass at defect 8
    fixed `core/size.py`'s three casts and left this one, so the same input
    class still reached the same exit-5 point through a different door.

    `None` -- "no such number" -- is the answer, which is what a value serde_json
    could not have produced deserves; `resolve_budget` then leaves that region
    unresolved instead of guessing. Measured after: the same SoC JSON is
    `exit 0`, other slices intact. Still not byte-identical to the oracle, which
    refuses the whole document and answers `budget_note: "unreadable SoM preset
    for <SKU>"` -- that residual lives in `_read_soc`'s degrade-to-empty (and in
    `json.loads` accepting literals serde_json rejects), not here.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value)
    except OverflowError:
        return None


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
    # tan-cli#747: this core's OWN slot0 window, when the SoM declares one, is
    # the only FLASH a core can link into -- `mram_mb` is the whole part,
    # including the other core's slot0.
    memory_map = _read_som_memory_map(preset_path)
    return resolve_budget(
        core_id, mram_mb, soc_flash_mb, banks, soc_cores, memory_map
    )


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

    # tan-cli#499 defect 2: `size` never consulted the manifest `status`, so a
    # slice recorded `failed`/`skipped` was still measured from whatever
    # `zephyr.elf` was left on disk -- RUN 1's artefact -- and emitted as an
    # ordinary measured row (`source: "size-tool"`, exit 0, `issues: []`).
    # `overlay_run_results_raw` deliberately PRESERVES the previous
    # `output_artefact` when a run carries none, and an empty one falls
    # through to the preserved `build_dir` probe, so the compensation
    # `build_cmd` applies on the WRITE side does not help. Worse under
    # `--fail-over-budget`, which then computes its verdict from stale bytes.
    # This is the same stale-artefact class `flash_plan.slice_should_flash`
    # and `image_bundle.slice_should_bundle` both guard -- whose own docstring
    # claims the predicate is "shared on purpose so `flash` and `image` can
    # never disagree about which artefacts are real"; `size` consulted
    # neither.
    #
    # Only a DECLARED, non-ok status refuses. An ABSENT `status` still
    # measures, unchanged: a manifest that omits the key says nothing about
    # whether the slice built, and refusing there would break the oracle
    # parity every no-status case has today (the whole `test_field_type_
    # leniency_parity` family, and `test_size_measured_slice_and_the_sdk_
    # envelope_key`, all write slices with no `status`).
    #
    # And only when a measurement was actually SUPPRESSED. When the probe
    # found nothing anyway the row is already `not-built` for the honest
    # reason, and saying so in the oracle's own words keeps byte parity on
    # every frozen case that carries a non-ok status with no artefact behind
    # it -- so this divergence materialises exactly where the defect does.
    declared_status = slice_.get("status")
    stale = (
        sizes is not None
        and isinstance(declared_status, str)
        and not slice_should_flash(declared_status)
    )
    if stale:
        return SliceSize(
            core_id=core_id,
            os=os_name,
            status="not-built",
            flash_total=budget.flash_total,
            ram_total=budget.ram_total,
            note=budget.note,
            notes=[
                f"slice did not build (manifest status: {declared_status}); "
                f"refusing to report a stale measurement from {probed_elf}"
            ],
        )

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
    """Delegates to `tan.env.use_color` (tan-cli's UX-polish sweep moved the
    logic there so `doctor_cmd` can reuse it without importing this command
    module) -- kept as a same-named wrapper so this file's own call site and
    `test_size_command.py` stay untouched."""
    return use_color(no_color, ci)


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


def _sdk_warning_lines(issues: list[Issue]) -> list[str]:
    """The text-mode rendering of the `sdk_resolution_issues` pair
    (tan-cli#497 defect 5). `{severity}: {message}` -- the shape `tan build`
    and `tan run` already print a resolution warning with, so the same
    workspace reads the same way whichever command a developer runs."""
    return [f"{issue.severity}: {issue.message}" for issue in issues]


def _error_outcome(
    project: Project,
    context: ProjectContext,
    code: str,
    message: str,
) -> _Outcome:
    """A hard manifest error: exit 1, an empty `alp-size/1` report as `data` so
    the envelope shape stays stable, and the message prefixed for text mode.

    tan-cli#464 review: this used to take a bare `sdk: SdkInfo | None` and
    report only the `code`/`message` issue -- so a manifest gate that fires
    before `_run`'s own `sdk_resolution_issues` call further down reported
    `sdk.sourceTier: "globalDefault"` with neither
    `sdk.project-pin-unresolved` nor `sdk.global-default-foreign-project`,
    even when one applied. Takes the whole `context` instead of just its `sdk`
    field so it can compute the same pair `_run` computes on the happy path,
    from the one shared `sdk_resolution_issues` -- no future early return
    through here can drop them again.

    tan-cli#497 defect 5 (the `size` sibling the issue names alongside
    `image`): that fix reached `issues` and stopped there -- `text` was built
    without the pair, so the DEFAULT mode dropped both warnings while
    `--format json` reported them. Composed from the SAME list now.
    """
    issues = sdk_resolution_issues(
        context.broken_project_pin, context.sdk_source_tier, context.foreign_global_default_for
    )
    text = _sdk_warning_lines(issues)
    issues.append(Issue(code, "error", message))
    return _Outcome(
        ExitCode.RUNTIME_FAILURE,
        build_size_report([]),
        project,
        issues,
        [*text, f"size: {message}"],
        context.sdk,
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
    disclosure: SdkDisclosure,
) -> _Outcome:
    """`disclosure` is the caller's, by reference -- the resolution facts are
    computed HERE and `size`'s `size.internal-failure` catch-all needs a name
    to read them from once this function has already raised. See
    `SdkDisclosure`."""
    context: ProjectContext = resolve_project_context(
        project_arg, board_yaml_arg, sdk_root_arg
    )
    project = context.project()
    # Recorded the instant the ladder answers, ahead of every other step this
    # function performs -- all of which can raise something unenumerated.
    disclosure.record(
        context.sdk,
        sdk_resolution_issues(
            context.broken_project_pin,
            context.sdk_source_tier,
            context.foreign_global_default_for,
        ),
    )

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
            context,
            "size.manifest-unavailable",
            f"no system-manifest.yaml at {err.path}; run `tan build` first "
            f"({err.detail}).",
        )
    except ManifestInvalid as err:
        return _error_outcome(
            project, context, "size.manifest-invalid", f"{err.path}: {err.detail}"
        )

    sku = board if board is not None else (manifest.sku.strip() or None)
    # tan-cli#567: the RESOLVED absolute path of the first size tool that is
    # really on PATH, not its bare name -- that path is what
    # `_sizes_from_size_tool` spawns, so the current directory cannot supply a
    # different `size.exe` than the one this check just approved. `None` still
    # means "no size tool on PATH", and the fallback rungs below are unchanged.
    size_bin = next(filter(None, (_find_on_path(t) for t in SIZE_TOOLS)), None)
    rows = [
        _measure_slice(s, build_root, sku, metadata_root, size_bin)
        for s in manifest.slices
    ]

    text: list[str] = []
    # The same pair `_error_outcome` above computes for a manifest-gate
    # refusal, from the same shared `sdk_resolution_issues` -- read back off
    # the disclosure rather than computed a second time, so this path, that one
    # and the catch-all can never disagree about whether either warning applies.
    issues: list[Issue] = list(disclosure.issues)
    exit_code = ExitCode.SUCCESS

    if not json_mode:
        # tan-cli#497 defect 5, happy-path half: the pair above went into
        # `issues` and nowhere else, so a measured report solved out of a
        # FALLBACK checkout printed its table with no mention that the
        # project pin had been ignored. Ahead of the table so it is not lost
        # below a long slice list.
        text.extend(_sdk_warning_lines(issues))
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

    # tan-cli#497 defect 5, the site the first pass missed -- see the twin
    # comment in `image_cmd.image`. `_error_outcome` and the happy path both
    # report the SDK-resolution pair; this handler, which runs strictly after
    # `resolve_project_context` has already answered, reported only the crash.
    # Recorded rather than recomputed here: the resolver is itself one of the
    # things that can raise, and this handler must not.
    disclosure = SdkDisclosure()
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
            disclosure=disclosure,
        )
    except Exception as err:  # noqa: BLE001
        # The port's most-repeated defect class: an uncaught exception escapes as
        # a raw traceback, stdout stays EMPTY, and the extension renders nothing
        # at all with no error on either side. Anything reaching here is a tan
        # bug and is reported as one -- with an envelope. Nothing in this handler
        # may itself throw: it only formats `err`, `_sdk_warning_lines` only
        # formats, and `Project(None, None)` and `build_size_report([])` are
        # both total.
        outcome = _Outcome(
            ExitCode.INTERNAL_FAILURE,
            build_size_report([]),
            Project(root=None, board_yaml=None),
            [
                *disclosure.issues,
                Issue(
                    "size.internal-failure",
                    "error",
                    f"size failed unexpectedly: {type(err).__name__}: {err}",
                ),
            ],
            [*_sdk_warning_lines(disclosure.issues), "size: internal failure"],
            disclosure.sdk,
        )

    # Built ONCE, for both formats: `Envelope.__init__` appends the tan-cli#407
    # `sdk.discovery-divergent` warning at the shared seam (`_with_sdk_
    # divergence`), and `outcome.text` was assembled strictly before any
    # `Envelope` existed -- so a seam-appended issue reached `--format json`
    # and was silent on the default text channel (tan-cli#799). Diffed
    # against `outcome.issues` (by value: `Issue` is a frozen dataclass) so
    # only what the seam ADDED is rendered, never a duplicate of a warning
    # `outcome.text` already carries (the pair `_run`/`_error_outcome` render
    # via `_sdk_warning_lines` above).
    envelope = Envelope(
        "size",
        outcome.project,
        outcome.data,
        outcome.issues,
        outcome.exit_code,
        sdk=outcome.sdk,
    )
    if json_mode:
        emit(envelope)
    else:
        seam_extra = [issue for issue in envelope.issues if issue not in outcome.issues]
        # stdout is the envelope channel and carries nothing else, in either
        # mode; stderr carries no contract of its own.
        stream = typer.get_text_stream("stderr")
        for issue in seam_extra:
            stream.write(f"{issue.severity}: {issue.message}\n")
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
