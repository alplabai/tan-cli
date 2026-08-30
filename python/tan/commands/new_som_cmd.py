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

* **`--format json` emits the envelope; the default text mode is the
  original's, verbatim.** The original has no `--format`/`--json` flag, and
  the Rust forwarder parses `--format` for this verb (clap `GlobalArgs`,
  `global = true` -- confirmed live: `tan.exe new-som --format json
  --sdk-root <bad>` reaches the SDK-root-unresolved failure, not a parse
  error) without ever synthesising a `--json` for the child the way
  `faultdecode`'s does (`sdk_cli.rs::build_argv`), so a successful run on
  the oracle is plain text either way. This file used to match that split
  exactly -- `--format` accepted and thrown away (`del ... output_format`),
  zero `emit(` calls in the module. tan-cli#399 ends it: `contract/README.md`
  states that the vscode extension drives `tan <cmd> --format json` and
  hard-depends on `{command, ok, exitCode, project, data, issues}`, and a
  verb that answers that flag with 1238 bytes of prose fails the consumer's
  `isEnvelope` guard, which then reports the whole scaffold as the literal
  string `Command completed.` with the planned file list unreachable. So
  `--format json` now emits one envelope carrying the planned/written paths
  and the porting checklist as `data`, and every refusal carries a
  `new-som.failed` code instead of `cli.main`'s generic `cli.parse-error`.
  WITHOUT `--format json` nothing changed: stdout carries the same
  human-readable lines the original wrote there (skeleton validation notes,
  `Created <path>`, the checklist), stderr the same error lines, matching
  `click.echo(..., err=...)` verbatim -- which is what the oracle-parity
  tests byte-compare.

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

import json
import re
import sys
from pathlib import Path

import click
import typer

from tan.commands.doctor_cmd import probe
from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS
from tan.core.metadata_schema import schema_errors
from tan.core.sdk_discovery import (
    _planner_python,
    global_default_foreign_project_issue,
    project_pin_issue,
    resolve_sdk_tiered,
)
from tan.core.shapes import SDK_MARKER, rejected_sdk_root_message
from tan.env import stderr_is_tty, stdin_is_tty
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

_SKU_RE = re.compile(r"^E1M-[A-Z0-9-]+$")
_SOC_REF_RE = re.compile(r"^[a-z0-9-]+:[a-z0-9-]+:[a-z0-9-]+$")
_FAMILY_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_CORE_ID_RE = re.compile(r"^[a-z][a-z0-9_]+$")
#: `som-preset-v1.schema.json`'s own `default_hw_rev` pattern, verbatim
#: (`metadata/schemas/som-preset-v1.schema.json` `properties.default_hw_rev
#: .pattern`). tan-cli#496 defect 5: this used to be checked ONLY by the
#: post-render schema self-check, which -- for a brand-new family, where the
#: `_family_hw_revisions` cross-check below is skipped -- reported a bad
#: value as `new-som.internal-failure` ("a tan bug, never bad user input"),
#: not the `new-som.failed` every other bad flag gets. Validated up front now
#: (`.fullmatch`, not `.match`: a trailing `$` still matches just before a
#: FINAL newline in non-MULTILINE mode, which is exactly the shape defect 3's
#: multi-line injection needs to slip past a `.match` check unnoticed).
_HW_REV_RE = re.compile(r"^[a-z0-9_-]+$")

#: Canonical backend keys already known to the device dispatcher, plus the
#: schema-legal lowercase `tbd` placeholder -- verbatim from the original.
INFERENCE_BACKENDS = ("ethos_u", "drpai", "deepx_dxm1", "tbd")
ETHOS_U_VARIANTS = ("u55", "u65", "u85")

#: `data.schemaVersion` for `--format json` (tan-cli#399). 1 because this is
#: the first shape `new-som` has ever put on the wire -- there is no earlier
#: one to be compatible with.
DATA_SCHEMA_VERSION = 1

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


def _envelope(data, issues: list[Issue], exit_code: ExitCode) -> None:
    """THE `new-som` envelope. `project` is hardcoded null/null on purpose:
    this command scaffolds metadata INTO an alp-sdk checkout and never reads a
    `board.yaml` -- `--project` is only one input tier to the SDK-root ladder,
    so reporting a project root here would claim a resolution that never
    happened (`envelope.Project.resolved`'s own "reporting is the seam" note).
    The checkout that was actually scaffolded is `data.sdkRoot`."""
    emit(Envelope("new-som", Project(root=None, board_yaml=None), data, issues, exit_code))


def _fail(message: str, exit_code: ExitCode = ExitCode.RUNTIME_FAILURE, *,
          json_mode: bool = False, extra_issues: list[Issue] | None = None) -> None:
    """Refuse: one error to stderr (or one envelope under `--format json`) and
    exit -- 1 by default, the flat exit code the alp_cli original's `_fail`
    always used (`raise SystemExit(1)`) for every validation failure it can
    raise (bad SKU, unknown board, ...); prefix reworded from `alp new-som:`
    to `new-som:` (RFC #837: the binary is `tan`). `exit_code` overrides this
    for the one failure this port adds that the original never had to: the
    `--sdk-root`/`--project` resolution preflight below (the original always
    ran FROM WITHIN a checkout). That check mirrors the Rust forwarder's own
    preflight (`crates/tan-cli/src/commands/sdk_cli.rs::run`), which exits
    `ExitCode::ValidationFailure` (2) for it specifically -- confirmed live:
    `tan.exe new-som --sdk-root <bad>` exits 2, not 1 (every OTHER new-som
    failure, including a bad-exit from the forwarded child, is RuntimeFailure
    (1) there too, matching this default).

    ONE code for every refusal, `new-som.failed`, matching the spelling the
    v0.4.1 oracle's own forwarder emits for the identical class of refusal --
    the alternative was a family of codes no consumer asked for, each of which
    would then be a frozen wire string (tan-cli#399). Before this, a
    `--format json` refusal reached `cli.main`'s generic fallback and went out
    as `command: "cli"` / `cli.parse-error`, so the two paths of one command
    disagreed about whose envelope a consumer was reading.

    `data` is `{"subcommand": "new-som"}`, not `null`, matching the oracle
    byte-for-byte -- measured live: `target/debug/tan.exe new-som --format
    json --sdk-root <bad>` answers `data:{"subcommand":"new-som"}`, exit 2.
    That is the SAME generic forwarder-preflight shape `sdk_cli.rs::run`
    hands every `alp-*` forward it refuses before spawning a child (the
    class faultdecode/model/monitor share), not something specific to any
    one refusal reason here.

    `extra_issues` (tan-cli#926) PREPENDS ahead of `new-som.failed`, the
    `clean_cmd._run` shape; `None` at every other call site in this file."""
    if json_mode:
        _envelope(
            {"subcommand": "new-som"},
            [*(extra_issues or []), Issue("new-som.failed", "error", message)], exit_code,
        )
    else:
        typer.echo(f"new-som: {message}", err=True)
    raise typer.Exit(int(exit_code))


def _internal_error(subject: str, schema: str, errors: list[str], json_mode: bool) -> None:
    """A generated skeleton that does not validate against its own schema is a
    `tan` BUG, never bad user input -- reported as one, and never left to
    `cli.main`'s parse-error fallback under `--format json` (tan-cli#399).
    Shared by the preset and SoC-spec self-checks, which differ only in the
    two names."""
    message = (
        f"INTERNAL ERROR -- generated {subject} does not validate against {schema}:"
    )
    if json_mode:
        _envelope(
            None,
            [Issue("new-som.internal-failure", "error", " ".join([message, *errors]))],
            ExitCode.RUNTIME_FAILURE,
        )
    else:
        typer.echo(f"new-som: {message}", err=True)
        for err in errors:
            typer.echo(f"  - {err}", err=True)
    raise typer.Exit(int(ExitCode.RUNTIME_FAILURE))


def _has_control_char(value: str) -> bool:
    r"""True if ``value`` carries a C0 control character (0x00-0x1F) or DEL
    (0x7F) -- the full ASCII control set, not just C0.

    tan-cli#496 round-2 major 2: DEL alone used to pass the narrower
    `ord(ch) < 0x20` check every call site here used to write inline, reach
    a rendered YAML double-quoted scalar unescaped (`_yaml_dquote` below
    only escapes `\` and `"`), and crash the unguarded
    `yaml.safe_load(preset_text)` self-check with a raw
    `yaml.reader.ReaderError` -- exit 1, zero bytes on stdout under
    `--format json` (measured: `--display-name $'X\x7fY'`). One shared
    check closes it at every call site instead of widening each inline
    comparison by hand.
    """
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _yaml_dquote(value: str) -> str:
    r"""Escape ``value`` for embedding in a YAML double-quoted scalar.

    YAML double-quoted style uses C-like escapes, so `\` and `"` are the
    only characters that need escaping here (control characters are
    rejected up front by the command's validation, `_has_control_char`).
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _yaml_scalar(value: str) -> str:
    """A single-line YAML scalar for ``value``, chosen by PyYAML's OWN
    emitter -- plain, single- or double-quoted, whichever round-trips
    exactly -- rather than a hand-rolled quoting rule.

    tan-cli#496 defect 3: `_yaml_dquote` above is a SECOND, independently
    maintained escaping implementation, and it only ever covered
    `display_name`; `default_hw_rev`/`default_board` were spliced RAW, so
    `--default-hw-rev $'r1\\ncapabilities: {secure_element: true}'` did not
    need to defeat any quoting at all -- the literal newline in the raw
    argument became a second YAML mapping-key line the instant it was
    f-string-joined into the preset, planting a fabricated hardware
    capability the schema self-check never sees (it only re-parses the
    ALREADY-INJECTED document). Routing both fields through the real
    emitter instead removes the second implementation, not just patches it.

    Raises ``ValueError`` -- "must not contain newlines or other control
    characters" -- for a ``value`` this cannot represent on one line; the
    caller pre-refuses those before render time, so this is the backstop,
    not the primary gate (`fail()`-worded messages belong at the call site,
    which knows the flag name).

    tan-cli#496 round-2 blocker 1: this used to call `yaml.safe_dump(value)`
    at PyYAML's DEFAULT 80-column `best_width` and keep only
    `.splitlines()[0]` -- fine for a value that fits in 80 columns, but
    PyYAML folds anything longer at a space, so any `--default-board`/
    `--default-hw-rev` past that width was SILENTLY TRUNCATED into the
    generated preset and reported as a clean success (a SoM vendor
    hardware fact, corrupted with no error). Blocker 2: at greater length
    the fold can land INSIDE an emitted quoted scalar, so the truncated
    text is not even valid YAML -- `new_som`'s unguarded
    `yaml.safe_load(preset_text)` self-check then raised a raw
    `yaml.scanner.ScannerError`, exit 1 with zero bytes on stdout under
    `--format json` (measured: `--default-board "$(python3 -c "print('word
    '*30)")"`). `width=float("inf")` tells the emitter never to fold at
    all, so whatever style it picks always lands on ONE content line --
    `dumped[0]` then keeps the full value, and any trailing lines are only
    PyYAML's own `...` document-end marker, never truncated content.
    """
    if _has_control_char(value):
        raise ValueError("must not contain newlines or other control characters")
    # tan-cli#810: deferred, because `cli.py` static-imports every command
    # module -- a module-scope `import yaml` here was paid by `tan --version`.
    # `sys.modules` caches it, so this file's other three call sites cost a
    # dict lookup. `tests/gates/test_cli_import_is_lean.py` holds the rule.
    import yaml  # noqa: PLC0415

    dumped = yaml.safe_dump(value, width=float("inf")).splitlines()
    return dumped[0] if dumped else "''"


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
    import yaml  # noqa: PLC0415 -- deferred, see `_yaml_scalar` (tan-cli#810)

    names: set[str] = set()
    for path in sorted(boards_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, UnicodeDecodeError):
            # tan-cli#415: `UnicodeDecodeError` is a `ValueError`, not a
            # `yaml.YAMLError`, so a non-UTF-8 board file escaped this
            # best-effort scan as an unhandled traceback instead of being
            # skipped the same way an unparseable one already is.
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
    import yaml  # noqa: PLC0415 -- deferred, see `_yaml_scalar` (tan-cli#810)

    for root in (output_root, sdk_root):
        path = root / "metadata" / "e1m_modules" / family_dir / "hw-revisions.yaml"
        if path.is_file():
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (yaml.YAMLError, UnicodeDecodeError):
                # tan-cli#415: same widening as `_known_board_names` above --
                # `UnicodeDecodeError` is a `ValueError`, not a `yaml.YAMLError`,
                # and previously escaped uncaught rather than resolving to
                # "not resolvable".
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

    Every prompt below is `err=True`: `click.prompt`'s OWN default is
    `err=False` (the question goes to stdout), which is backwards for a
    command whose consent gate (`tan.core.consent.can_prompt`,
    `_interactive`'s only caller checks stdin+STDERR) already promises the
    question rides stderr and the answer rides stdin -- stdout stays free
    for whatever the invocation is piping or redirecting (tan-cli#496
    defect 6). Without this, `tan new-som > log.txt` with a real terminal
    on stdin/stderr writes the question into `log.txt` instead of the
    terminal and blocks on a question the human never saw.
    """
    if sku is None:
        sku = click.prompt("New SoM SKU (E1M-<UPPERCASE> shaped)", err=True).strip()
    if soc_ref is None:
        soc_ref = click.prompt(
            "Silicon triple-colon ref (vendor:family:part, e.g. nxp:imx9:imx95)", err=True
        ).strip()
    if family is None:
        family = click.prompt(
            "Human-readable family slug (e.g. nxp-imx9)", err=True
        ).strip()
    if vendor is None:
        vendor = click.prompt(
            "Vendor display name for the SoC JSON",
            default=soc_ref.split(":")[0] if _SOC_REF_RE.match(soc_ref) else "",
            err=True,
        ).strip()
    if display_name is None:
        display_name = click.prompt(
            "Display name",
            default=f"{sku} ({vendor} -- scaffold, silicon facts TBD)",
            err=True,
        ).strip()
    if inference_backend is None:
        inference_backend = click.prompt(
            "Inference backend (silicon-determined; pick `tbd` when unknown)",
            type=click.Choice(INFERENCE_BACKENDS),
            default="tbd",
            err=True,
        )
    if inference_backend == "ethos_u" and ethos_u_variant is None:
        ethos_u_variant = click.prompt(
            "Primary Ethos-U variant",
            type=click.Choice(ETHOS_U_VARIANTS),
            err=True,
        )
    if cores is None:
        answer = click.prompt(
            "Canonical core ids, comma-separated (leave the tbd placeholder "
            "when unknown)",
            default=",".join(DEFAULT_CORES),
            err=True,
        )
        cores = tuple(s.strip() for s in answer.split(",") if s.strip())
    if default_board is None:
        default_board = click.prompt(
            "Default board (stock carrier)", default=DEFAULT_BOARD, err=True
        ).strip()
    if default_hw_rev is None:
        default_hw_rev = click.prompt(
            "Default hw rev", default=DEFAULT_HW_REV, err=True
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
    a(f"default_hw_rev:         {_yaml_scalar(default_hw_rev)}")
    a("")
    a("# Stock carrier board this SoM ships on (see metadata/boards/).")
    a(f"default_board:          {_yaml_scalar(default_board)}")
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


# tan-cli#964: the validator itself moved to `tan.core.metadata_schema` --
# every READ consumer (`tan build`, `tan generate`, `tan presets`, `tan
# size`, `tan bootstrap`) needed the SAME `jsonschema.Draft202012Validator`
# call this file already had for its WRITE-path self-check, and a second
# hand-rolled copy is exactly the per-consumer-guard drift #964 is about.
# `_schema_errors` is kept as the local name (both call sites below are
# unchanged) and still means exactly what it always has here: `source=None`,
# so a validation failure is reported against the in-memory generated
# skeleton (`pointer: message`), not a file on disk -- this scaffold has not
# been written yet when either call below runs.
_schema_errors = schema_errors


def _rollback_write_failure(
    preset_path: Path,
    preset_existed_before: bool,
    preset_backup: bytes | None,
    preset_write_attempted: bool,
    soc_path: Path,
    soc_doc,
) -> list[str]:
    """Undo whatever THIS invocation actually touched after a write failure,
    and report exactly what could not be undone -- never more. Returns a
    description string per candidate that could not be undone; an empty
    list means the rollback is clean.

    Three tan-cli#496 fixes live here:

    * **Defect 2.** `preset_path` is RESTORED to its original bytes when it
      pre-existed (a `--force` re-scaffold whose SoC-spec write then
      failed), never deleted -- a destructive op must not remove a good,
      pre-existing artifact before its replacement is proven good. A
      brand-new preset (did not exist before this run) is still removed;
      there is nothing of the customer's to lose there.
    * **The finding against #496's own fix.** `soc_path` is removed only
      when it PHYSICALLY EXISTS -- `Path.exists()` swallows `OSError`
      (including the `NotADirectoryError` a regular file below
      `--output-root` raises), unlike `unlink(missing_ok=True)`, which only
      swallows `FileNotFoundError` and lets that one through. The issue's
      own primary repro (a regular file at `<output-root>/metadata/socs`)
      fails at `mkdir()` before `soc_path` is ever created, so `exists()`
      is False and nothing is attempted -- the old code attempted the
      unlink anyway, got a second `NotADirectoryError`, and told the
      caller "this may be a half-written scaffold" for a path that was
      never written at all.
    * **tan-cli#496 round-2 blocker 3.** The preset branch below is gated
      on `preset_write_attempted`, a flag the caller sets the instant the
      preset write is ABOUT to start -- not on membership in a `written`
      list the caller only appended to AFTER `write_text` returned
      successfully. `Path.write_text` opens the file (truncating any
      pre-existing content) and issues one `write()` call; an `OSError`
      from THAT call (`ENOSPC`/`EDQUOT`/`EFBIG`/`EIO`) leaves a physically
      created, partially-written file on disk but raises before the
      caller's `written.append(preset_path)` ever ran. Gating on `written`
      alone treated that file as never touched by this run and left the
      partial write unrolled-back; `preset_write_attempted` covers that
      case too, on top of the "wrote fine, the SoC-spec write after it
      failed" case a `written` check alone already caught.
    """
    cleanup_failures: list[str] = []
    if preset_write_attempted:
        if preset_existed_before:
            if preset_backup is None:
                cleanup_failures.append(
                    f"{preset_path} (no backup was captured; its content "
                    "cannot be safely restored -- left as written by this run)"
                )
            else:
                try:
                    preset_path.write_bytes(preset_backup)
                except OSError as exc:
                    cleanup_failures.append(
                        f"{preset_path} (could not restore its original content: {exc})"
                    )
        else:
            try:
                if preset_path.exists():
                    preset_path.unlink()
            except OSError as exc:
                cleanup_failures.append(f"{preset_path} ({exc})")
    if soc_doc is not None:
        try:
            if soc_path.exists():
                soc_path.unlink()
        except OSError as exc:
            cleanup_failures.append(f"{soc_path} ({exc})")
    return cleanup_failures


def new_som(
    sku: str = typer.Option(
        None,
        "--sku",
        # tan-cli#720: `--som` accepted as an alias, matching `tan init`'s
        # spelling of the same value. The refusal message below keeps naming
        # `--sku`, the canonical form for this command.
        "--som",
        help="New SoM SKU (E1M-<UPPERCASE> shaped) (alias: --som).",
    ),
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
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
    verbose: bool = typer.Option(False, "--verbose", hidden=True),
    quiet: bool = typer.Option(False, "--quiet", hidden=True),
    no_color: bool = typer.Option(False, "--no-color", hidden=True),
    non_interactive: bool = typer.Option(False, "--non-interactive", hidden=True),
    ci: bool = typer.Option(False, "--ci", hidden=True),
) -> None:
    """Scaffold the metadata skeletons for porting a new SoM."""
    # The nine options above are clap's `GlobalArgs` members (`global = true`)
    # that the oracle accepts on EVERY verb, `new-som` included, and never
    # reads for this one -- declared here purely so the argv SURFACE matches:
    # `tan new-som --ci ...` exits the same as the same invocation without
    # `--ci` on the oracle; without this, it was a Click "No such option"
    # usage error (exit 2) instead. `--format` is no longer one of them: it
    # was accepted-and-deleted here until tan-cli#399, which is what made a
    # `--format json` scaffold print prose at the one consumer that parses
    # this flag -- see the docstring bullet above.
    del board_yaml, target, all_targets
    del verbose, quiet, no_color, non_interactive, ci
    resolved_format = output_format or OutputFormat.TEXT
    json_mode = resolved_format == "json"

    report_lines: list[str] = []

    def say(line: str = "") -> None:
        """One line of the human report. Suppressed under `--format json`,
        where stdout carries the ONE envelope and nothing else -- the same
        lines survive there as `data.report`."""
        report_lines.append(line)
        if not json_mode:
            typer.echo(line)

    def fail(message: str, exit_code: ExitCode = ExitCode.RUNTIME_FAILURE, *,
             extra_issues: list[Issue] | None = None) -> None:
        """`_fail` with this run's output mode bound, so no refusal site has to
        remember to pass it -- a forgotten one would silently print prose to
        stdout under `--format json` and leave the envelope to `cli.main`."""
        _fail(message, exit_code, json_mode=json_mode, extra_issues=extra_issues)

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
    # tan-cli#926: computed before the `.path is None` refusal below (tan-cli#900 fix).
    pin_issue = project_pin_issue(active.broken_project_pin, active.tier)
    foreign_issue = global_default_foreign_project_issue(active.foreign_global_default_for)
    resolution_issues = [i for i in (pin_issue, foreign_issue) if i is not None]
    if not json_mode:
        for issue in resolution_issues:
            typer.echo(f"new-som: warning: {issue.message}", err=True)
    if active.path is None or not Path(active.path).joinpath(*SDK_MARKER).exists():
        # tan-cli#497 defect 7: a REJECTED `--sdk-root` names the value.
        # `resolve_sdk_tiered` is TERMINAL on the flag (I-31) and returns it
        # verbatim even when invalid, so the marker check right above is the
        # ONLY thing that knows the path was rejected -- and this refusal
        # carries no `sdk` block, so the typed value left no trace at all while
        # the message told the caller to "Use --sdk-root".
        fail(
            rejected_sdk_root_message(sdk_root, "No SoM scaffold was written.")
            if sdk_root
            else _SDK_ROOT_UNRESOLVED,
            ExitCode.VALIDATION_FAILURE,
            extra_issues=resolution_issues,
        )
        return
    resolved_sdk = Path(active.path)
    # tan-cli#263 review: this command WRITES metadata skeletons into
    # `resolved_sdk` -- a silently-missed `.alp/sdk-path` pin means porting a
    # new SoM into the wrong checkout. Under `--format json` it rides the
    # envelope's `issues` as the already-frozen `sdk.project-pin-unresolved`
    # (tan-cli#399); in text mode it stays the stderr line it always was.
    # tan-cli#464: same reasoning -- this command writes metadata skeletons
    # into `resolved_sdk`, and a `globalDefault` answer a DIFFERENT project's
    # bootstrap relocation actually decided is exactly as dangerous here as a
    # silently-missed project pin.
    issues: list[Issue] = list(resolution_issues)

    # -- 1. Gather inputs.  Interactive prompts need a real terminal; in a
    # pipe / CI, fail fast naming exactly what is missing instead of an
    # opaque prompt-library abort.
    if sku is None or soc_ref is None or family is None:
        missing = [
            flag
            for flag, value in (("--sku", sku), ("--soc-ref", soc_ref), ("--family", family))
            if value is None
        ]
        # `sys.stdin`/`sys.stderr` are bare references, not guaranteed
        # streams. Two distinct failures, and a guard has to cover both:
        # either can be `None` under a console-less launcher, and either can
        # be an object that EXISTS but has no `.isatty()` -- `cli.py`'s own
        # `_TeeStderr` under `--format json` is exactly that shape. A
        # hand-rolled `X is None or not X.isatty()` catches the first and
        # raises `AttributeError` on the second.
        #
        # tan-cli#496 round-2 major 1: BOTH streams are checked, not only
        # `stdin`. `_interactive`'s prompts are `err=True` (defect 6 above)
        # -- the question rides stderr, the answer rides stdin -- so a
        # redirected/piped stderr with a real stdin terminal has no way to
        # show the human the question, and the old stdin-only gate admitted
        # exactly that shape, blocking on a prompt nobody could see.
        #
        # tan-cli#488 round 5: both operands route through
        # `tan.env.stdin_is_tty`/`stderr_is_tty` -- the one shared probe
        # (tan-cli#288) that catches `AttributeError` (replaced stream) and
        # `ValueError` (closed stream) alike -- rather than repeating the
        # hand-rolled shape a sixth time. `tan.core.consent.can_prompt`
        # carries the same pair for the same reason.
        if not (stdin_is_tty() and stderr_is_tty()):
            fail(
                "stdin or stderr is not a terminal, so interactive prompts "
                "are unavailable; pass the missing required flag(s): "
                + ", ".join(missing)
            )
            return
        if json_mode:
            # A prompt writes to stdout, which under `--format json` carries
            # the one envelope -- prompting there would corrupt the very
            # document the caller asked for, on a terminal that happens to be
            # attached (tan-cli#399).
            fail(
                "--format json makes stdout the envelope channel, so "
                "interactive prompts are unavailable; pass the missing "
                "required flag(s): " + ", ".join(missing)
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
    # tan-cli#496 defect 4: a repeated `--cores` id used to reach both
    # skeletons -- a duplicate `<id>: {}` mapping key in the preset's
    # `topology:` block (invalid YAML; PyYAML keeps the last, a strict
    # loader like alp-sdk's `validate_metadata.py` rejects it outright) and
    # two identical `cores[]` rows in the SoC JSON -- reported as a clean
    # success by both schema self-checks, since neither schema forbids a
    # repeated id within its own document. `tan init`'s sibling parser
    # (`tan.core.scaffold.parse_cores`) already refuses this; new-som didn't.
    dup_cores = sorted({c for c in cores if cores.count(c) > 1})
    if dup_cores:
        fail(f"--cores has duplicate id(s) {dup_cores}; every core id must be unique")
        return
    if inference_backend == "ethos_u" and ethos_u_variant is None:
        fail("--inference-backend ethos_u requires --ethos-u-variant (u55/u65/u85)")
        return
    if vendor is None:
        vendor = soc_ref.split(":")[0]
    if display_name is None:
        display_name = f"{sku} ({vendor} -- scaffold, silicon facts TBD)"
    if _has_control_char(display_name):
        fail("display name must not contain newlines or other control characters")
        return
    # tan-cli#496 defect 3/5: `default_hw_rev` used to reach the RENDERED
    # preset unchecked by this command itself -- only the post-render schema
    # self-check saw it, and only after the value had already been spliced
    # into YAML text. A single-line-but-out-of-pattern value (`R1`) then
    # surfaced as `new-som.internal-failure` for a brand-new family (defect
    # 5: the `_family_hw_revisions` cross-check below is skipped there, the
    # one place that would otherwise have caught it as `new-som.failed`
    # first) -- a caller mistake misreported as a tan bug. A MULTI-LINE
    # value defeated the self-check entirely (defect 3): the injected lines
    # became sibling YAML keys, so `default_hw_rev` itself still read back
    # pattern-clean. `.fullmatch`, not `.match`+`$`: see `_HW_REV_RE`'s
    # comment for why `$` alone is not equivalent here.
    if not _HW_REV_RE.fullmatch(default_hw_rev):
        fail(f"default hw rev '{default_hw_rev}' must match {_HW_REV_RE.pattern!r}")
        return
    # `default_board` has no schema pattern (a bare `type: string`, since a
    # real carrier name may contain spaces/hyphens the SKU-shaped fields
    # don't), so the injection above is closed by refusing control
    # characters -- the one thing a single-line preset value can never
    # legitimately need -- rather than by a charset it does not have.
    if _has_control_char(default_board):
        fail("default board must not contain newlines or other control characters")
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
    try:
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
    except ValueError as exc:
        # Backstop for `_yaml_scalar`, not the primary gate: the checks above
        # already refuse every value that would land here. Never reached in
        # a correct build; kept so a future field spliced in without its own
        # upfront check fails loud (`new-som.failed`) instead of an unhandled
        # traceback with zero bytes on stdout under `--format json`.
        fail(f"could not render the preset skeleton: {exc}")
        return
    soc_doc = None if soc_path.exists() else _soc_skeleton(sku, soc_ref, vendor, cores)

    som_schema_path = _som_schema_path(resolved_sdk)
    soc_schema_path = _soc_schema_path(resolved_sdk)
    try:
        sku_needs_pattern = re.match(_current_sku_pattern(som_schema_path), sku) is None
    except (OSError, UnicodeDecodeError) as exc:
        # tan-cli#415: `som-preset-v1.schema.json` is SDK-supplied rather than
        # user-supplied, but an unreadable or non-UTF-8 copy must still reach a
        # coded envelope -- never a bare traceback with zero bytes on stdout.
        # `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so it has to
        # be named explicitly beside it.
        fail(f"could not read {som_schema_path} ({exc})")
        return
    import yaml  # noqa: PLC0415 -- deferred, see `_yaml_scalar` (tan-cli#810)

    try:
        preset_doc = yaml.safe_load(preset_text)
    except yaml.YAMLError as exc:
        # tan-cli#496 round-2 major 2: this was unguarded -- a value that
        # slipped past every upfront check above (the DEL byte
        # `_has_control_char` now rejects; a hypothetical future field
        # spliced into `_render_preset` without its own check) reached the
        # rendered document and could still make it unparseable, raising a
        # raw `yaml.reader.ReaderError`/`yaml.scanner.ScannerError` -- exit
        # 1, zero bytes on stdout under `--format json`. Backstop, not the
        # primary gate, same shape as the `_render_preset` `ValueError`
        # catch just above.
        fail(f"could not parse the rendered preset skeleton: {exc}")
        return
    if preset_doc is not None:
        try:
            errors = _schema_errors(preset_doc, som_schema_path)
        except (OSError, UnicodeDecodeError) as exc:
            fail(f"could not read {som_schema_path} ({exc})")
            return
        if sku_needs_pattern:
            errors = [e for e in errors if not e.startswith("sku:")]
        if errors:
            _internal_error("preset", "som-preset-v1", errors, json_mode)
        say(
            "Preset skeleton validates against som-preset-v1"
            + (" (except the sku pattern -- see step below)" if sku_needs_pattern else "")
        )
    if soc_doc is not None:
        try:
            errors = _schema_errors(soc_doc, soc_schema_path)
        except (OSError, UnicodeDecodeError) as exc:
            fail(f"could not read {soc_schema_path} ({exc})")
            return
        if errors:
            _internal_error("SoC spec", "soc-spec-v1", errors, json_mode)
        say("SoC spec skeleton validates against soc-spec-v1")

    # -- 4. Write (or, under --dry-run, only report) the skeletons.
    #
    # `planned` is every path this run creates or would create, and `existing`
    # every one it deliberately left alone -- the two lists the extension's
    # scaffold panel renders (tan-cli#399), built from the same values the
    # human lines are built from so the two can never disagree.
    planned: list[Path] = [preset_path] + ([soc_path] if soc_doc is not None else [])
    existing: list[Path] = [] if soc_doc is not None else [soc_path]
    written: list[Path] = []
    # tan-cli#496 defect 2: captured BEFORE any write touches `preset_path`,
    # so a `--force` re-scaffold whose SoC-spec write later fails can
    # restore the customer's original preset instead of deleting it (see
    # `_rollback_write_failure`). `None` when the preset is brand new --
    # there was nothing to back up, and nothing must be restored either.
    preset_existed_before = preset_path.exists()
    preset_backup: bytes | None = None
    # tan-cli#496 round-2 blocker 3: set the instant the write is ABOUT to
    # start, not after it succeeds -- `written.append(preset_path)` below
    # only runs once `write_text` has already returned, so gating the
    # rollback on `preset_path in written` alone missed a write that
    # started, physically created/truncated the file, and THEN raised
    # (ENOSPC/EDQUOT/EFBIG/EIO) partway through. See
    # `_rollback_write_failure`'s docstring.
    preset_write_attempted = False
    if dry_run:
        say(f"Would create {preset_path}")
        if soc_doc is None:
            say(f"SoC spec already present: {soc_path} (not touched)")
        else:
            say(f"Would create {soc_path}")
    else:
        try:
            preset_path.parent.mkdir(parents=True, exist_ok=True)
            if preset_existed_before:
                preset_backup = preset_path.read_bytes()
            preset_write_attempted = True
            preset_path.write_text(preset_text, encoding="utf-8", newline="\n")
            written.append(preset_path)
            if soc_doc is not None:
                soc_path.parent.mkdir(parents=True, exist_ok=True)
                soc_path.write_text(
                    json.dumps(soc_doc, indent=2) + "\n", encoding="utf-8", newline="\n"
                )
                written.append(soc_path)
        except OSError as exc:
            # Never leave a half-written scaffold behind, and never destroy
            # a pre-existing one either -- `_rollback_write_failure` is both
            # halves of tan-cli#496 (defect 2's restore-not-delete and the
            # finding against this file's OWN earlier fix: only a path that
            # PHYSICALLY EXISTS is ever reported as a cleanup failure, so a
            # target this run never reached disk for is never called
            # "may be half-written") plus round-2 blocker 3's
            # attempted-not-just-succeeded tracking.
            cleanup_failures = _rollback_write_failure(
                preset_path,
                preset_existed_before,
                preset_backup,
                preset_write_attempted,
                soc_path,
                soc_doc,
            )
            if cleanup_failures:
                fail(
                    f"could not write the skeletons ({exc}); cleanup also "
                    "failed, so this may be a half-written scaffold -- "
                    "could not remove: " + "; ".join(cleanup_failures)
                )
            else:
                # "Rolled back", not "removed": a --force re-scaffold whose
                # preset pre-existed was RESTORED, not deleted (defect 2).
                fail(f"could not write the skeletons ({exc}); rolled back any partial output")
            return
        say(f"Created {preset_path}")
        if soc_doc is None:
            say(f"SoC spec already present: {soc_path} (not touched)")
        else:
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
        # The success envelope, and the whole point of tan-cli#399: a consumer
        # that parsed stdout on the failure path got 1238 bytes of prose here.
        # Paths are the native spelling `data.sdkPath` uses elsewhere -- only
        # the envelope's own `sdk.root` is normalised to forward slashes.
        _envelope(
            {
                "schemaVersion": DATA_SCHEMA_VERSION,
                "sku": sku,
                "socRef": soc_ref,
                "sdkRoot": str(resolved_sdk),
                "outputRoot": str(output_root_path),
                "dryRun": dry_run,
                "planned": [str(p) for p in planned],
                "written": [str(p) for p in written],
                "existing": [str(p) for p in existing],
                "nextSteps": steps,
                "report": report_lines,
            },
            issues,
            ExitCode.SUCCESS,
        )
