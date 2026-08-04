# SPDX-License-Identifier: Apache-2.0
"""`tan new-som` -- scaffold the metadata for porting a brand-new SoM.

Port of `alp-sdk`'s `scripts/alp_cli/new_som.py`: the vendor-N+1 porting kit.
Generates the two metadata skeletons a new SoM port needs (the SoM preset
YAML and, when absent, the SoC spec JSON), with every schema-required
hardware-fact field present as an explicit TBD placeholder -- values are
NEVER invented -- and prints the numbered porting checklist.

Today's shipped `tan new-som` (`crates/tan-cli/src/commands/sdk_cli.rs`) is a
thin, stdio-inheriting forward to `python -m alp_cli new-som`. This file
becomes the real, in-process implementation; nothing here re-derives a
hardware fact the SDK's `metadata/**` already owns (ADR-0017, invariant
I-26) -- every SKU, part number, address or pin name in the generated
skeleton is either a caller-supplied argument or an explicit `TBD`.

Differences from the alp_cli original, and why:

* **`--format json` emits the standard envelope; TEXT mode is verbatim.**
  The original has no `--format`/`--json` flag at all. The Rust forwarder's
  clap `GlobalArgs` DOES parse `--format` for this verb (`global = true` --
  confirmed live: `tan.exe new-som --format json --sdk-root <bad>` reaches
  the SDK-root-unresolved failure, not a parse error); its VALUE is
  validated against the same `text`/`json` pair clap allows (tan-cli#378 --
  accepting the flag never licensed accepting any string after it, and this
  command WRITES files, so a value the oracle refuses at parse time must not
  reach them).

  Text mode is byte-for-byte the original's: stdout carries the same
  human-readable lines (skeleton validation notes, `Created <path>`, the
  checklist), stderr the same error lines, matching `click.echo(..., err=...)`
  verbatim.

  `--format json` is a `{command,ok,exitCode,project,data,issues}` envelope
  (tan-cli#399). FAILURE matches the oracle exactly -- `command: "new-som"`,
  `data: {"subcommand":"new-som"}`, one `new-som.failed` issue (see
  `_ISSUE_CODE`, measured not read). SUCCESS is the one deliberate
  divergence this file adds: the oracle forwards stdio and never synthesises
  a `--json` for this verb (`sdk_cli.rs::build_argv`), so its successful
  `--format json` stdout is plain text -- which `contract/README.md`'s
  envelope-hard-depending consumer cannot read at all (`json.load(stdout)`
  throws; alp-sdk-vscode's `isEnvelope` guard then fails OPEN and reports
  `Command completed.` with the planned files unreachable). The envelope
  wins, and the file lists live under `data`.

* **`--sdk-root`/`--project` are new, and required.** The original ran
  FROM WITHIN an alp-sdk checkout (`REPO_ROOT =
  Path(__file__).resolve().parents[2]`) and never needed to ask where the
  SDK was. `tan` is a separate, standalone binary, so this file resolves
  the target checkout the same way every other metadata-facing command
  does (`sdk_cmd.resolve_sdk_tiered`: `--sdk-root` > project pin > global
  default > discovery) before doing anything else -- this is not a new
  user-facing surface either: `tan new-som` already accepts `--sdk-root`
  today, via the Rust forwarder's own `GlobalArgs`.

* **Interactive prompts use `click.prompt`/`click.Choice`, not
  `questionary`.** The original's arrow-key select menus need a dependency
  `tan` does not carry -- the package's runtime dependency set is a
  deliberately short, enumerated list (`typer`, `rich`, `pyyaml`,
  `jsonschema`; see `pyproject.toml`), and adding a fifth for one
  command's menu widget is the opposite of that discipline. The SAME
  questions are asked, in the same order, with the same defaults; only the
  answer widget differs (type-and-choose instead of arrow-key-and-enter).

* **The SKU-prefix -> family-directory mapping is asked of the SDK, not
  reproduced.** `_family_hw_revisions` cross-checks `--default-hw-rev`
  against the new SKU's family's `hw-revisions.yaml`, which needs the
  family directory (`aen`, `v2n`, ...) for a SKU prefix. That map
  (`alp_project_loader._sku_family`) is hand-maintained and SDK-internal
  with, by its own comment, "no second on-disk source" -- so it cannot be
  read out of `metadata/**` the way every other fact in this file is, and
  copying its dict into `tan` would be exactly the RFC #843 drift
  (hand-porting SDK logic instead of consuming the SDK) this port exists
  to avoid. `_resolve_sku_family` below instead asks the TARGET SDK's own
  `alp_project_loader.py` for the answer via a short-lived subprocess,
  matching the original's best-effort semantics (`None` on any failure --
  unresolvable script, unrecognised prefix, or a brand-new family with no
  mapping yet -- falls through to the "add this to the checklist"
  branch, same as the original's `ImportError`-guarded import).

* **`--sku`'s help text (and the matching interactive prompt) say
  "E1M-<UPPERCASE> shaped" instead of the oracle's "e.g. E1M-XYZ101"**
  (`scripts/alp_cli/new_som.py:404`). Both describe the same `_SKU_RE`
  pattern; the oracle's wording embeds a literal example SKU, and I-26's
  hardware-fact gate (`tests/gates/test_no_new_hardware_facts.py`) would
  then have to allowlist a string that names no real part just to keep the
  wording verbatim, so this file describes the pattern shape instead.

`DEFAULT_BOARD` (`"E1M-EVK"`) is a UI default only, not a value this file
trusts: every accepted `--default-board` (including the unedited default)
is cross-checked against `metadata/boards/*.yaml`'s real `name:` values
before anything is rendered, so a stale literal here fails LOUD --
"default board 'E1M-EVK' does not match any `name:` in metadata/boards/"
-- rather than silently shipping a wrong one; see the allowlist entry in
`tests/gates/test_no_new_hardware_facts.py`.
"""
from __future__ import annotations

import functools
import json
import re
import sys
from pathlib import Path

import click
import jsonschema
import typer
import yaml

from tan.commands.build_cmd import _planner_python
from tan.commands.doctor_cmd import probe
from tan.commands.sdk_cmd import (
    NO_SDK_NEXT_STEPS,
    SDK_MARKER,
    project_pin_issue,
    resolve_sdk_tiered,
)
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode

#: Every `--format json` refusal this command emits (tan-cli#399), and NOT a
#: code this port invented: it is the oracle's own, measured rather than read
#: -- `target/debug/tan.exe new-som --format json` with no SDK resolvable
#: answers
#:
#:   {"command":"new-som","ok":false,"exitCode":2,
#:    "project":{"root":null,"boardYaml":null},
#:    "data":{"subcommand":"new-som"},
#:    "issues":[{"code":"new-som.failed","severity":"error","message":"..."}]}
#:
#: One code for every failure, exactly as there: the oracle's `new-som` is a
#: forwarder whose every refusal (its own preflight, and any bad exit from the
#: child) reports this one spelling, with the specifics in `message`. Minting
#: finer codes here would read better and diverge permanently from the
#: envelope a consumer already gets from the shipped binary -- and the
#: consumer that renders this shows the message, not the code.
_ISSUE_CODE = "new-som.failed"

#: The oracle's failure `data` for this verb, verbatim from the measurement
#: above. Carried so a failure envelope from this port is byte-comparable with
#: the shipped binary's, rather than diverging on a field nobody chose.
_FAILURE_DATA = {"subcommand": "new-som"}

_SKU_RE = re.compile(r"^E1M-[A-Z0-9-]+$")
_SOC_REF_RE = re.compile(r"^[a-z0-9-]+:[a-z0-9-]+:[a-z0-9-]+$")
_FAMILY_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_CORE_ID_RE = re.compile(r"^[a-z][a-z0-9_]+$")

#: Canonical backend keys already known to the device dispatcher, plus the
#: schema-legal lowercase `tbd` placeholder -- verbatim from the original.
INFERENCE_BACKENDS = ("ethos_u", "drpai", "deepx_dxm1", "tbd")
ETHOS_U_VARIANTS = ("u55", "u65", "u85")

DEFAULT_CORES = ("tbd_core0",)
#: UI default only -- see the module docstring and the gate allowlist entry.
DEFAULT_BOARD = "E1M-EVK"
DEFAULT_HW_REV = "r1"

#: `scripts/alp_project.py` is THE marker for an alp-sdk checkout (I-31).
#: `tan sdk switch` refuses in this build (tan-cli#305) -- kept the two
#: mechanisms that actually work here (`--sdk-root`, placing the project near
#: a checkout, both live tiers of `resolve_sdk_tiered`) and swapped the third
#: for `NO_SDK_NEXT_STEPS`'s honest "how to get one at all".
_SDK_ROOT_UNRESOLVED = (
    "alp-sdk root is unresolved. Use --sdk-root, place the project near an "
    f"alp-sdk checkout, or {NO_SDK_NEXT_STEPS}."
)


def _split_cores_csv(_ctx: click.Context, _param: click.Parameter, value: str | None):
    if value is None:
        return None
    return tuple(s.strip() for s in value.split(",") if s.strip())


def _check_output_root(_ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    """`click.Path(file_okay=False)` equivalent for `--output-root` -- the
    oracle's own type (`scripts/alp_cli/new_som.py:432`). An existing
    REGULAR FILE is rejected outright; a directory that does not exist yet
    is fine (it is created later). Reproduced as an explicit check rather
    than relied on falling out of a later `mkdir`/`write_text`, matching
    the sibling `--elf`/`--file` checks in `faultdecode_cmd.py`
    (`_check_elf_path`, `_check_file_path`)."""
    if value is None:
        return None
    if Path(value).is_file():
        raise typer.BadParameter(f"'{value}' is a file.", param_hint="'--output-root'")
    return value


def _fail(
    message: str,
    exit_code: ExitCode = ExitCode.RUNTIME_FAILURE,
    *,
    json_mode: bool = False,
    quiet: bool = False,
) -> None:
    """Report an error and exit -- 1 by default, the flat exit code the
    alp_cli original's `_fail` always used (`raise SystemExit(1)`) for every
    validation failure it can raise (bad SKU, unknown board, ...); prefix
    reworded from `alp new-som:` to `new-som:` (RFC #837: the binary is
    `tan`). `exit_code` overrides this for the one failure this port adds
    that the original never had to: the `--sdk-root`/`--project` resolution
    preflight below (the original always ran FROM WITHIN a checkout). That
    check mirrors the Rust forwarder's own preflight
    (`crates/tan-cli/src/commands/sdk_cli.rs::run`), which exits
    `ExitCode::ValidationFailure` (2) for it specifically -- confirmed live:
    `tan.exe new-som --sdk-root <bad>` exits 2, not 1 (every OTHER new-som
    failure, including a bad-exit from the forwarded child, is RuntimeFailure
    (1) there too, matching this default).

    Under `--format json` the same refusal is ONE envelope on stdout in the
    ORACLE's own shape (tan-cli#399, see `_ISSUE_CODE`). Before that,
    `new-som` emitted no envelope at all, so `cli.py`'s generic usage-error
    fallback caught the non-zero exit and reported `command: "cli"` with
    `cli.parse-error` -- a code `contract/issue-codes.json` records as
    `consumer: "none"`, on a refusal that really did happen, from a command
    the consumer could not identify. Text mode is untouched: the stderr line
    is byte-identical to the one the oracle's forwarded child printed.

    `quiet` is for the two INTERNAL-ERROR paths, which have already written a
    multi-line block (a header plus one bullet per schema error) to stderr:
    text mode must not gain a summarising `new-som:` line it never had -- the
    oracle-parity tests byte-compare that output -- while JSON mode still
    needs the one envelope those paths otherwise never emitted."""
    if json_mode:
        emit(
            Envelope(
                "new-som",
                Project(root=None, board_yaml=None),
                dict(_FAILURE_DATA),
                [Issue(_ISSUE_CODE, "error", message)],
                exit_code,
            )
        )
    elif not quiet:
        typer.echo(f"new-som: {message}", err=True)
    raise typer.Exit(int(exit_code))


def _yaml_dquote(value: str) -> str:
    r"""Escape ``value`` for embedding in a YAML double-quoted scalar.

    YAML double-quoted style uses C-like escapes, so `\` and `"` are the
    only characters that need escaping here (control characters are
    rejected up front by the command's validation).
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _som_schema_path(sdk_root: Path) -> Path:
    return sdk_root / "metadata" / "schemas" / "som-preset-v1.schema.json"


def _soc_schema_path(sdk_root: Path) -> Path:
    return sdk_root / "metadata" / "schemas" / "soc-spec-v1.schema.json"


def _current_sku_pattern(schema_path: Path) -> str:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return schema["properties"]["sku"]["pattern"]


def _known_board_names(sdk_root: Path) -> set[str] | None:
    """Board ``name:`` values from ``<sdk_root>/metadata/boards/``, or
    ``None`` when the directory is missing -- the closed set of shared
    carrier boards lives in the SDK checkout, not under ``--output-root``.
    """
    boards_dir = sdk_root / "metadata" / "boards"
    if not boards_dir.is_dir():
        return None
    names: set[str] = set()
    for path in sorted(boards_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("name"), str):
            names.add(doc["name"])
    return names or None


def _resolve_sku_family(sku: str, sdk_root: Path) -> str | None:
    """The SKU's family DIRECTORY name (``aen``, ``v2n``, ...), resolved by
    asking the TARGET SDK's own ``alp_project_loader._sku_family`` -- never
    reproduced in `tan` (I-26; see the module docstring). Best-effort: any
    failure (script missing, unrecognised prefix, broken interpreter) is
    ``None``, matching the original's `ImportError`-guarded import.
    """
    script = sdk_root / "scripts" / "alp_project_loader.py"
    if not script.is_file():
        return None
    out = probe(
        [
            _planner_python(str(sdk_root), str(sdk_root)),
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from alp_project_loader import _sku_family; "
            "print(_sku_family(sys.argv[2]))",
            str(sdk_root / "scripts"),
            sku,
        ]
    )
    if out is None:
        return None
    family_dir = out.strip()
    return family_dir or None


def _family_hw_revisions(
    sku: str, output_root: Path, sdk_root: Path
) -> tuple[Path, set[str]] | None:
    """(path, rev keys) of the SKU family's hw-revisions.yaml, or None.

    None means "not resolvable at scaffold time" -- a brand-new family with
    no SKU-prefix mapping / no hw-revisions file yet (creating one is a
    porting-checklist step).
    """
    family_dir = _resolve_sku_family(sku, sdk_root)
    if family_dir is None:
        return None
    for root in (output_root, sdk_root):
        path = root / "metadata" / "e1m_modules" / family_dir / "hw-revisions.yaml"
        if path.is_file():
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                return None
            revs = doc.get("hw_revisions")
            if isinstance(revs, dict):
                return path, {str(k) for k in revs}
            return None
    return None


def _interactive(
    sku: str | None,
    soc_ref: str | None,
    family: str | None,
    vendor: str | None,
    display_name: str | None,
    inference_backend: str | None,
    ethos_u_variant: str | None,
    cores: tuple[str, ...] | None,
    default_board: str | None,
    default_hw_rev: str | None,
):
    """Ask the same questions, in the same order, the original's
    `questionary`-based `_interactive` did -- see the module docstring for
    why the answer widget is `click.prompt`/`click.Choice` here instead.
    """
    if sku is None:
        sku = click.prompt("New SoM SKU (E1M-<UPPERCASE> shaped)").strip()
    if soc_ref is None:
        soc_ref = click.prompt(
            "Silicon triple-colon ref (vendor:family:part, e.g. nxp:imx9:imx95)"
        ).strip()
    if family is None:
        family = click.prompt("Human-readable family slug (e.g. nxp-imx9)").strip()
    if vendor is None:
        vendor = click.prompt(
            "Vendor display name for the SoC JSON",
            default=soc_ref.split(":")[0] if _SOC_REF_RE.match(soc_ref) else "",
        ).strip()
    if display_name is None:
        display_name = click.prompt(
            "Display name",
            default=f"{sku} ({vendor} -- scaffold, silicon facts TBD)",
        ).strip()
    if inference_backend is None:
        inference_backend = click.prompt(
            "Inference backend (silicon-determined; pick `tbd` when unknown)",
            type=click.Choice(INFERENCE_BACKENDS),
            default="tbd",
        )
    if inference_backend == "ethos_u" and ethos_u_variant is None:
        ethos_u_variant = click.prompt(
            "Primary Ethos-U variant",
            type=click.Choice(ETHOS_U_VARIANTS),
        )
    if cores is None:
        answer = click.prompt(
            "Canonical core ids, comma-separated (leave the tbd placeholder "
            "when unknown)",
            default=",".join(DEFAULT_CORES),
        )
        cores = tuple(s.strip() for s in answer.split(",") if s.strip())
    if default_board is None:
        default_board = click.prompt(
            "Default board (stock carrier)", default=DEFAULT_BOARD
        ).strip()
    if default_hw_rev is None:
        default_hw_rev = click.prompt(
            "Default hw rev", default=DEFAULT_HW_REV
        ).strip()
    return (
        sku,
        soc_ref,
        family,
        vendor,
        display_name,
        inference_backend,
        ethos_u_variant,
        cores,
        default_board,
        default_hw_rev,
    )


def _render_preset(
    sku: str,
    soc_ref: str,
    family: str,
    display_name: str,
    inference_backend: str,
    ethos_u_variant: str | None,
    cores: tuple[str, ...],
    default_board: str,
    default_hw_rev: str,
) -> str:
    vendor_slug, family_slug, part_slug = soc_ref.split(":")
    soc_rel = f"metadata/socs/{vendor_slug}/{family_slug}/{part_slug}.json"

    lines: list[str] = []
    a = lines.append
    a(f"# Stock preset skeleton for {sku}, generated by `tan new-som`.")
    a("#")
    a("# Every value below marked TBD awaits the authoritative hardware")
    a("# config (datasheet / schematic / BOM).  Fill facts in from the")
    a("# primary source named next to each field -- NEVER guess (see")
    a("# docs/porting-new-som.md).")
    a("")
    a("schema_version: 1")
    a("")
    a("# SKU is assigned by Alp Lab product planning.  A brand-new family")
    a("# also needs one alternation added to the `sku:` pattern in")
    a("# metadata/schemas/som-preset-v1.schema.json (porting guide step 3).")
    a(f"sku: {sku}")
    a("")
    a("# Human-readable family slug (NOT the directory name; the SKU-prefix")
    a("# -> family-directory map lives in scripts/alp_project.py).")
    a(f"family: {family}")
    a("")
    a(f"# Triple-colon silicon ref; must resolve to {soc_rel}.")
    a(f"silicon: {soc_ref}")
    a("")
    a("# Vendor order code from the datasheet's Ordering Information table;")
    a(f"# must match a variants[].order_code in {soc_rel}.")
    a("# While TBD, the loader falls back to variants[].alp_module_skus")
    a("# reverse lookup.")
    a("silicon_variant: TBD")
    a("")
    a(f'display_name: "{_yaml_dquote(display_name)}"')
    a("")
    a("# Populated on-module chips -- the module BOM / schematic is")
    a("# authoritative.  Chip values are slugs matching a")
    a("# metadata/chips/<chip>.yaml manifest.  The legal role keys are the")
    a("# CLOSED set in som-preset-v1.schema.json `on_module:` (pmic_main,")
    a("# pmic_secondary, clock_generator, rtc_external, temperature_sensor,")
    a("# eeprom, secure_element, wifi_ble, supervisor_mcu, ethernet_phy,")
    a("# nor_flash, emmc, npu, pcie_mux, ospi_memories, hyperram,")
    a("# i2c_devices).  Delete the TBD rows for roles this module does NOT")
    a("# populate; add rows for the ones it does.")
    a("on_module:")
    a(f"  silicon:              {soc_ref}")
    a("  pmic_main:            TBD                    # main PMIC part (BOM)")
    a("  eeprom:               TBD                    # identity EEPROM part (BOM)")
    a("  # i2c_devices:                               # per-bus I2C device tables;")
    a("  #   <bus_name>:                              # bus names are a per-family")
    a("  #     bus_master: TBD                        # fact (schematic).  Chip")
    a("  #     devices:                               # slugs + 7-bit addresses come")
    a('  #       - { chip: <slug>, role: <role>, address_7bit: "TBD" }')
    a("")
    a("# Off-SoC module memory in the canonical cross-family shape.")
    a("# Authoritative source: the module BOM (DRAM + flash parts).")
    a("# On-die SRAM/MRAM is derived from the SoC variant, never declared here.")
    a("memory:")
    a("  dram_mbit:            TBD                    # external volatile memory, Mbit")
    a("  flash_mbit:           TBD                    # external non-volatile memory, Mbit")
    a("")
    a("# Inference-accelerator selection -- silicon-determined (SoC")
    a("# datasheet), never customer-facing.  `tbd` is the schema-legal")
    a("# placeholder backend; scripts/check_inference_backend_parity.py")
    a("# cross-checks the value against the device dispatcher and accepts")
    a("# `tbd` ONLY while `status.preliminary:` below is true, so this")
    a("# scaffold commits green as-is.  Replace `tbd` with the real")
    a("# canonical key (ethos_u, drpai, deepx_dxm1, ...) before clearing")
    a("# the preliminary flag.")
    a("inference:")
    a(f"  preferred_backend:    {inference_backend}")
    if inference_backend == "ethos_u":
        # Primary variant only.  Which Ethos-U instances the part carries is
        # silicon-determined -- the SDK derives it from the SoC JSON npus[] /
        # capabilities.ethos_uNN_count -- so the preset does NOT enumerate them
        # (the deprecated `npu_population` field is intentionally not scaffolded).
        a(f"  ethos_u_variant:      {ethos_u_variant}")
    a("")
    a("# capabilities: -- OPTIONAL, omitted in the skeleton.  Declare ONLY")
    a("# keys the SoM ADDS on top of the silicon capabilities (e.g. an")
    a("# on-module secure element or bridge accelerator); silicon caps come")
    a(f"# from {soc_rel} `capabilities:`.")
    a("")
    a("# Per-core default runtime + app mapping.  Keys MUST match cores[].id")
    a(f"# in {soc_rel} (cross-checked by pr-metadata-validate).  Rename any")
    a("# tbd placeholder id in lockstep with the SoC JSON, then fill each")
    a("# entry: `machine:` + `toolchain:` for Yocto A-class cores, `board:`")
    a("# + `toolchain:` for Zephyr M-class cores (see E1M-AEN801.yaml /")
    a("# E1M-V2N102.yaml for the two shapes).")
    a("topology:")
    for core in cores:
        a(f"  {core}: {{}}")
    a("")
    a("# Memory layout (SRAM banks + on-die flash) is derived from the SoC")
    a("# variant resolved via `silicon_variant:` -- see the SoC JSON")
    a("# `variants[].sram_banks_kb`.  Declare a memory_map: block here ONLY")
    a("# for non-stock partitioning.")
    a("")
    a("# Vendor mailbox / IPC controller -- the vendor reference manual /")
    a("# hand-written HW config is authoritative.  The channel reservations")
    a("# below are the SDK-standard convention shared by every released")
    a("# preset; keep them unless the controller has fewer channels.")
    a("mailbox:")
    a("  controller: TBD")
    a("  channels:")
    a("    - { id: 0, reserved_for: alp_default_rpmsg }")
    a("    - { id: 1, reserved_for: app }")
    a("    - { id: 2, reserved_for: app }")
    a("    - { id: 3, reserved_for: power_mgmt }")
    a("")
    a("# pad_routes: -- OPTIONAL, omitted in the skeleton.  Once the")
    a("# schematic is available, either (a) list every E1M pad that routes")
    a("# through an on-module mediator chip (see E1M-V2N102.yaml), (b)")
    a("# declare an explicit empty list to assert \"no mediator, everything")
    a("# is SoC-direct\" (see E1M-NX9101.yaml), or (c) list pads with")
    a("# `dispatch: TBD` while routing is pending.  Omitted, the loader")
    a("# treats every pad as SoC-direct -- resolve this before clearing")
    a("# status.partial_hw_config.")
    a("")
    a("# helper_firmware: -- OPTIONAL, omitted in the skeleton.  One entry")
    a("# per independently-flashed on-module helper MCU image (see")
    a("# E1M-V2N102.yaml `gd32_bridge`).  Omit when the module has none.")
    a("")
    a("# Must resolve against metadata/e1m_modules/<family-dir>/hw-revisions.yaml.")
    a(f"default_hw_rev:         {default_hw_rev}")
    a("")
    a("# Stock carrier board this SoM ships on (see metadata/boards/).")
    a(f"default_board:          {default_board}")
    a("")
    a("# Keep both flags true until every TBD above is resolved from the")
    a("# authoritative HW config.  `preliminary: true` is also the marker")
    a("# that lets the parity gate accept the `tbd` inference backend.")
    a("status:")
    a("  preliminary:          true")
    a("  partial_hw_config:    true")
    a("")
    return "\n".join(lines)


def _soc_skeleton(sku: str, soc_ref: str, vendor: str, cores: tuple[str, ...]) -> dict:
    _, family_slug, part_slug = soc_ref.split(":")
    core_rows = []
    for core in cores:
        # `count: 1` is the schema minimum, NOT a datasheet fact -- the
        # notes[] entry below flags it (JSON has no comments; this file
        # uses the schema-sanctioned `_pending_reason` + `notes` fields).
        core_rows.append({"id": core, "type": "TBD", "count": 1})
    return {
        "soc_spec_version": 1,
        "ref": soc_ref,
        "vendor": vendor,
        "family": family_slug,
        "part": part_slug,
        "status": "preliminary",
        "pending_reference_manual_ingestion": True,
        "_pending_reason": (
            "Scaffolded by `tan new-som`: every silicon fact in this file "
            "is a placeholder pending datasheet / reference-manual "
            "ingestion.  peripherals: {} means unknown, not zero."
        ),
        "cores": core_rows,
        "npus": [],
        "peripherals": {},
        "variants": [
            {
                "order_code": "TBD",
                "notes": (
                    "Placeholder row: replace order_code with the vendor's "
                    "Ordering Information part number, then mirror it in the "
                    "SoM preset's silicon_variant."
                ),
                "alp_module_skus": [sku],
            }
        ],
        "notes": [
            "JSON has no comments -- scaffold TODOs live in this notes[] "
            "array plus _pending_reason (both schema-sanctioned fields).",
            "cores[]: the tbd id / type TBD / count 1 rows are "
            "schema-minimum placeholders, NOT datasheet facts; rename the "
            "ids in lockstep with the SoM preset's topology: keys.",
            "vendor/family/part hold the ref slugs until the datasheet "
            "display names are filled in.",
        ],
    }


def _schema_errors(doc, schema_path: Path) -> list[str]:
    """Validate ``doc`` against ``schema_path``; return error strings."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]


def new_som(
    sku: str = typer.Option(None, "--sku", help="New SoM SKU (E1M-<UPPERCASE> shaped)."),
    soc_ref: str = typer.Option(
        None, "--soc-ref", help="Silicon triple-colon ref, e.g. nxp:imx9:imx95."
    ),
    family: str = typer.Option(
        None, "--family", help="Human-readable family slug, e.g. nxp-imx9."
    ),
    vendor: str = typer.Option(
        None,
        "--vendor",
        help="Vendor display name for the SoC JSON (default: the soc-ref vendor segment).",
    ),
    display_name: str = typer.Option(
        None, "--display-name", help="Preset display_name (default: derived from the SKU)."
    ),
    inference_backend: str = typer.Option(
        None,
        "--inference-backend",
        help="Silicon-determined inference backend "
        f"({'/'.join(INFERENCE_BACKENDS)}; default: tbd).",
    ),
    ethos_u_variant: str = typer.Option(
        None,
        "--ethos-u-variant",
        help="Primary Ethos-U variant "
        f"({'/'.join(ETHOS_U_VARIANTS)}); required with --inference-backend ethos_u.",
    ),
    cores: str = typer.Option(
        None,
        "--cores",
        callback=_split_cores_csv,
        help="Comma-separated canonical core ids (default: tbd_core0).",
    ),
    default_board: str = typer.Option(
        None,
        "--default-board",
        help="Stock carrier board (a `name:` from metadata/boards/; "
        "default: the SDK's stock development-board carrier).",
    ),
    default_hw_rev: str = typer.Option(
        None, "--default-hw-rev", help="Default hardware revision (default: r1)."
    ),
    output_root: str = typer.Option(
        None,
        "--output-root",
        metavar="PATH",
        callback=_check_output_root,
        help="Root to generate metadata/ under (default: the SDK checkout).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate and print the planned files; write nothing."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing preset for this SKU."
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    board_yaml: str = typer.Option(None, "--board-yaml", hidden=True),
    target: str = typer.Option(None, "--target", hidden=True),
    all_targets: bool = typer.Option(False, "--all", hidden=True),
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
    verbose: bool = typer.Option(False, "--verbose", hidden=True),
    quiet: bool = typer.Option(False, "--quiet", hidden=True),
    no_color: bool = typer.Option(False, "--no-color", hidden=True),
    non_interactive: bool = typer.Option(False, "--non-interactive", hidden=True),
    ci: bool = typer.Option(False, "--ci", hidden=True),
) -> None:
    """Scaffold the metadata skeletons for porting a new SoM."""
    # The eight `del`'d options above are clap's `GlobalArgs` members
    # (`global = true`) that the oracle accepts on EVERY verb, `new-som`
    # included, and never reads for this one -- declared purely so the argv
    # SURFACE matches (`--format` is the ninth, and is now read; see below):
    # `tan new-som --ci ...` exits the same as the same invocation without
    # `--ci` on the oracle; without this, it was a Click "No such option"
    # usage error (exit 2) instead. This is a DIFFERENT claim from the
    # docstring's "No --format json" bullet above -- confirmed live
    # (`tan.exe new-som --format json --sdk-root <bad>` still reaches the
    # SDK-root-unresolved failure rather than a parse error): `--format` IS a
    # legal flag on the oracle's `new-som`, it just never gets forwarded as a
    # synthesised `--json` (unlike `faultdecode`'s) -- see `clean_cmd.clean`'s
    # identical fix for the same port-wide gap.
    del board_yaml, target, all_targets
    del verbose, quiet, no_color, non_interactive, ci
    # `--format`'s VALUE is validated first of all: ACCEPTING a flag is not the
    # same as accepting any string after it, and the oracle refuses a bad value
    # at PARSE time in both positions (measured: `target/debug/tan.exe --format
    # bogus new-som --sku FOO` and `... new-som --format bogus --sku FOO` are
    # both `invalid value 'bogus' for '--format <FORMAT>' [possible values:
    # text, json]`, exit 2, nothing run) -- while this command ran on to WRITING
    # METADATA FILES for the same argv. First statement in the body, before the
    # SDK-root preflight, so the refusal lands before any work exactly as it
    # does there.
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    # tan-cli#399: and the MODE is now read, not just the value validated.
    # `contract/README.md` states that alp-sdk-vscode drives `tan <cmd>
    # --format json` and hard-depends on the `{command,ok,exitCode,project,
    # data,issues}` envelope; `new-som` accepted the flag and printed the same
    # human text either way, so `json.load(stdout)` threw on the SUCCESS path
    # while the FAILURE path produced a parseable (but wrongly-labelled
    # `command: "cli"`) envelope from `cli.py`'s fallback. The extension's own
    # `isEnvelope` guard fails open on the first and mis-dispatches on the
    # second: rc 0, "Command completed.", and the two planned files
    # unreachable.
    #
    # A DELIBERATE divergence from the oracle on the SUCCESS path only, and the
    # only one this adds: the oracle's `new-som` is a stdio-INHERITING forward
    # to `python -m alp_cli new-som` that never gets a synthesised `--json`
    # (`crates/tan-cli/src/commands/sdk_cli.rs` `build_argv`/`run`: `json: None`
    # on success), so `--format json` is plain text on stdout there. The
    # envelope contract wins: a documented, envelope-hard-depending consumer
    # cannot read plain text, and #399 rules an undeclared exemption out. The
    # FAILURE path stays byte-comparable with the oracle -- see `_ISSUE_CODE`.
    json_mode = output_format == "json"
    fail = functools.partial(_fail, json_mode=json_mode)
    #: Every line the text mode writes to STDOUT, in order. Under `--format
    #: json` stdout belongs to the envelope alone, so these are collected and
    #: reported as structured `data` instead of printed -- one funnel, so a
    #: line added later cannot leak onto a JSON stdout by being written the
    #: old way. stderr is untouched in both modes.
    notes: list[str] = []

    def say(line: str) -> None:
        notes.append(line)
        if not json_mode:
            typer.echo(line)
    # Mirrors the original's `type=click.Choice(...)` flag-level validation --
    # a Click-usage error (exit 2) BEFORE anything else runs, same as the
    # original raised it during argument parsing itself. Written as an
    # explicit check rather than `click_type=click.Choice(...)` on the
    # `typer.Option` above: that form crashes with an uncaught `StopIteration`
    # under this repo's pinned typer/click pair instead of the clean
    # `BadParameter` every other command in this package raises by hand (see
    # `sdk_cmd.sdk`'s `--format` check for the established pattern).
    if inference_backend is not None and inference_backend not in INFERENCE_BACKENDS:
        raise typer.BadParameter(
            f"{inference_backend!r} is not one of "
            f"{', '.join(repr(c) for c in INFERENCE_BACKENDS)}.",
            param_hint="'--inference-backend'",
        )
    if ethos_u_variant is not None and ethos_u_variant not in ETHOS_U_VARIANTS:
        raise typer.BadParameter(
            f"{ethos_u_variant!r} is not one of "
            f"{', '.join(repr(c) for c in ETHOS_U_VARIANTS)}.",
            param_hint="'--ethos-u-variant'",
        )

    workspace_root = Path.cwd() / project if project else Path.cwd()
    active = resolve_sdk_tiered(sdk_root, workspace_root)
    if active.path is None or not Path(active.path).joinpath(*SDK_MARKER).exists():
        fail(_SDK_ROOT_UNRESOLVED, ExitCode.VALIDATION_FAILURE)
        return
    resolved_sdk = Path(active.path)
    # tan-cli#263 review: this command WRITES metadata skeletons into
    # `resolved_sdk` -- a silently-missed `.alp/sdk-path` pin means porting a
    # new SoM into the wrong checkout. The stderr line stays in BOTH modes
    # (stderr carries no envelope promises), and since tan-cli#399 the same
    # warning also rides the success envelope's `issues[]`, where a consumer
    # that only reads stdout can see it.
    pin_issue = project_pin_issue(active.broken_project_pin, active.tier)
    if pin_issue is not None:
        typer.echo(f"new-som: warning: {pin_issue.message}", err=True)

    # -- 1. Gather inputs.  Interactive prompts need a real terminal; in a
    # pipe / CI, fail fast naming exactly what is missing instead of an
    # opaque prompt-library abort.
    if sku is None or soc_ref is None or family is None:
        if not sys.stdin.isatty():
            missing = [
                flag
                for flag, value in (("--sku", sku), ("--soc-ref", soc_ref), ("--family", family))
                if value is None
            ]
            fail(
                "stdin is not a terminal, so interactive prompts are "
                "unavailable; pass the missing required flag(s): " + ", ".join(missing)
            )
            return
        (
            sku,
            soc_ref,
            family,
            vendor,
            display_name,
            inference_backend,
            ethos_u_variant,
            cores,
            default_board,
            default_hw_rev,
        ) = _interactive(
            sku,
            soc_ref,
            family,
            vendor,
            display_name,
            inference_backend,
            ethos_u_variant,
            cores,
            default_board,
            default_hw_rev,
        )

    # Flag-driven (CI) defaults for everything not provided.
    if inference_backend is None:
        inference_backend = "tbd"
    if cores is None:
        cores = DEFAULT_CORES
    if default_board is None:
        default_board = DEFAULT_BOARD
    if default_hw_rev is None:
        default_hw_rev = DEFAULT_HW_REV
    output_root_path = Path(output_root) if output_root else resolved_sdk

    # -- 2. Validate EVERYTHING before touching the filesystem, so a
    # rejected invocation never leaves half-written skeletons behind.
    if not _SKU_RE.match(sku):
        fail(f"SKU '{sku}' must look like E1M-<UPPERCASE>")
        return
    if not _SOC_REF_RE.match(soc_ref):
        fail(f"soc-ref '{soc_ref}' must be <vendor>:<family>:<part> (lowercase slugs)")
        return
    if not _FAMILY_RE.match(family):
        fail(f"family '{family}' must be a lowercase slug")
        return
    if not cores:
        fail(
            "--cores was given but contains no core ids "
            "(omit the flag for the tbd_core0 placeholder)"
        )
        return
    bad_cores = [c for c in cores if not _CORE_ID_RE.match(c)]
    if bad_cores:
        fail(f"core id(s) {bad_cores} must match ^[a-z][a-z0-9_]+$")
        return
    if inference_backend == "ethos_u" and ethos_u_variant is None:
        fail("--inference-backend ethos_u requires --ethos-u-variant (u55/u65/u85)")
        return
    if vendor is None:
        vendor = soc_ref.split(":")[0]
    if display_name is None:
        display_name = f"{sku} ({vendor} -- scaffold, silicon facts TBD)"
    if any(ord(ch) < 0x20 for ch in display_name):
        fail("display name must not contain newlines or other control characters")
        return

    # Resolve the cross-references the checklist used to defer: the stock
    # carrier must exist in metadata/boards/, and the hw rev must resolve in
    # the family hw-revisions file when that file exists (a brand-new family
    # has none yet -- that stays a checklist step).
    board_names = _known_board_names(resolved_sdk)
    if board_names is not None and default_board not in board_names:
        fail(
            f"default board '{default_board}' does not match any `name:` "
            f"in metadata/boards/ (known: {', '.join(sorted(board_names))})"
        )
        return
    hw_revs = _family_hw_revisions(sku, output_root_path, resolved_sdk)
    if hw_revs is not None:
        hwrev_path, revs = hw_revs
        if default_hw_rev not in revs:
            fail(
                f"default hw rev '{default_hw_rev}' does not resolve in "
                f"{hwrev_path} (known: {', '.join(sorted(revs))})"
            )
            return

    preset_path = output_root_path / "metadata" / "e1m_modules" / f"{sku}.yaml"
    vendor_slug, family_slug, part_slug = soc_ref.split(":")
    soc_path = (
        output_root_path / "metadata" / "socs" / vendor_slug / family_slug / f"{part_slug}.json"
    )

    if preset_path.exists() and not force:
        fail(f"{preset_path} already exists (pass --force to overwrite)")
        return

    # -- 3. Render + self-check both skeletons in memory (still nothing on
    # disk): the skeletons must be schema-valid on arrival.  The one
    # sanctioned exception is a SKU outside the schema's current pattern --
    # extending that pattern IS a porting step (see below).
    preset_text = _render_preset(
        sku,
        soc_ref,
        family,
        display_name,
        inference_backend,
        ethos_u_variant,
        cores,
        default_board,
        default_hw_rev,
    )
    soc_doc = None if soc_path.exists() else _soc_skeleton(sku, soc_ref, vendor, cores)

    som_schema_path = _som_schema_path(resolved_sdk)
    soc_schema_path = _soc_schema_path(resolved_sdk)
    sku_needs_pattern = re.match(_current_sku_pattern(som_schema_path), sku) is None
    preset_doc = yaml.safe_load(preset_text)
    if preset_doc is not None:
        errors = _schema_errors(preset_doc, som_schema_path)
        if sku_needs_pattern:
            errors = [e for e in errors if not e.startswith("sku:")]
        if errors:
            typer.echo(
                "new-som: INTERNAL ERROR -- generated preset does not "
                "validate against som-preset-v1:",
                err=True,
            )
            for e in errors:
                typer.echo(f"  - {e}", err=True)
            fail(
                "INTERNAL ERROR -- generated preset does not validate against "
                "som-preset-v1: " + "; ".join(errors),
                quiet=True,
            )
            return
        say(
            "Preset skeleton validates against som-preset-v1"
            + (" (except the sku pattern -- see step below)" if sku_needs_pattern else "")
        )
    if soc_doc is not None:
        errors = _schema_errors(soc_doc, soc_schema_path)
        if errors:
            typer.echo(
                "new-som: INTERNAL ERROR -- generated SoC spec does not "
                "validate against soc-spec-v1:",
                err=True,
            )
            for e in errors:
                typer.echo(f"  - {e}", err=True)
            fail(
                "INTERNAL ERROR -- generated SoC spec does not validate against "
                "soc-spec-v1: " + "; ".join(errors),
                quiet=True,
            )
            return
        say("SoC spec skeleton validates against soc-spec-v1")

    # -- 4. Write (or, under --dry-run, only report) the skeletons.
    #: What this run PLANS to create (`--dry-run`) or DID create, plus what it
    #: deliberately left alone -- the structured half of `data` under
    #: `--format json` (tan-cli#399). The extension's scaffold panel binds to
    #: exactly this; before it existed the panel rendered empty for a run that
    #: planned two files, because `data` was unreachable.
    planned: list[str] = []
    created: list[str] = []
    untouched: list[str] = []
    if dry_run:
        planned.append(str(preset_path))
        say(f"Would create {preset_path}")
        if soc_doc is None:
            untouched.append(str(soc_path))
            say(f"SoC spec already present: {soc_path} (not touched)")
        else:
            planned.append(str(soc_path))
            say(f"Would create {soc_path}")
    else:
        written: list[Path] = []
        try:
            preset_path.parent.mkdir(parents=True, exist_ok=True)
            preset_path.write_text(preset_text, encoding="utf-8", newline="\n")
            written.append(preset_path)
            if soc_doc is not None:
                soc_path.parent.mkdir(parents=True, exist_ok=True)
                soc_path.write_text(
                    json.dumps(soc_doc, indent=2) + "\n", encoding="utf-8", newline="\n"
                )
                written.append(soc_path)
        except OSError as exc:
            # Never leave a half-written scaffold behind: remove both the
            # partially-written target and anything already created.
            targets = [preset_path] + ([soc_path] if soc_doc is not None else [])
            for path in {*written, *targets}:
                path.unlink(missing_ok=True)
            fail(f"could not write the skeletons ({exc}); removed any partial output")
            return
        created.append(str(preset_path))
        say(f"Created {preset_path}")
        if soc_doc is None:
            untouched.append(str(soc_path))
            say(f"SoC spec already present: {soc_path} (not touched)")
        else:
            created.append(str(soc_path))
            say(f"Created {soc_path}")

    # -- 5. Numbered next-steps checklist.
    soc_created = soc_doc is not None
    steps = [
        f"Fill every TBD in {preset_path.name}"
        + (f" and {soc_path.name}" if soc_created else "")
        + " from the authoritative datasheet / schematic / BOM"
        " -- never invent values.",
    ]
    if sku_needs_pattern:
        steps.append(
            f"Extend the `sku:` pattern in "
            f"metadata/schemas/som-preset-v1.schema.json to accept {sku} "
            f"(docs/porting-new-som.md, schema-pattern step)."
        )
    steps.append(
        f'Register the silicon ref: add "{soc_ref}" to '
        f"metadata/registries/silicon-kconfig.json and the matching "
        f"ALP_SOC_* stanza in zephyr/Kconfig."
    )
    if hw_revs is None:
        # Only when the family hw-revisions file could not be resolved at
        # scaffold time (brand-new family); otherwise the rev was already
        # validated above.
        steps.append(
            f"Ensure default_hw_rev '{default_hw_rev}' resolves in "
            f"metadata/e1m_modules/<family-dir>/hw-revisions.yaml "
            f"(add the family file for a brand-new family)."
        )
    steps += [
        "Validate all metadata: python scripts/validate_metadata.py",
        "Regenerate derived headers: python scripts/gen_soc_caps.py "
        "&& python scripts/gen_board_header.py",
        "Run the conformance suite: tests/zephyr/conformance via twister (native_sim).",
        "Full walkthrough: docs/porting-new-som.md",
    ]
    say("")
    say("Next steps:")
    for i, step in enumerate(steps, start=1):
        say(f"  {i}. {step}")
    if dry_run:
        say("")
        say("Dry run -- validated OK, nothing was written.")

    if json_mode:
        # The one envelope, on the success path (tan-cli#399). `notes` carries
        # the same lines text mode printed, in the same order, so nothing this
        # command reports is reachable in only one of the two modes; the file
        # lists and `nextSteps` are what a panel actually binds to.
        emit(
            Envelope(
                "new-som",
                Project(root=None, board_yaml=None),
                {
                    "dryRun": dry_run,
                    "planned": planned,
                    "created": created,
                    "untouched": untouched,
                    "nextSteps": steps,
                    "notes": notes,
                },
                [] if pin_issue is None else [pin_issue],
                ExitCode.SUCCESS,
            )
        )
