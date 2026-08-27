# SPDX-License-Identifier: Apache-2.0
"""`tan pinmux` -- the E1M pinmux capability table (E1M pad -> silicon
function) for a SoM family (tan-cli#257).

Mirrors `crates/tan-cli/src/commands/pinmux.rs` plus the `tan-core` helpers it
composes (`pinmux::{parse_pinmux_table_checked, pinmux_family_for_sku}`).
Resolves a `metadata/pinmux/<family>.yaml` family stem from an explicit
`--family` or a `--sku` prefix, reads that table out of the resolved SDK root,
and echoes it in the envelope -- the single source the extension/LSP consume
instead of parsing `metadata/pinmux/<family>.yaml` themselves.

**Fail-soft, deliberately, with two exceptions** -- this paragraph's, and the
refused `--family` tan-cli#359 added below. An unresolved SDK root, an
unknown SKU, no `--sku`/`--family` at all, or a family with no generated table
on disk are each a `warning`-severity issue at exit 0 -- `pinmux` answers "I
don't know" the same way for all of them, never a hard failure. A table that
DOES exist on disk but fails to parse (schema-version skew) or parses to ZERO
pads (`pinmux-capability-v1.schema.json` requires `minItems: 1`, so an empty
table is never a legitimate v1 document -- and, measured against the real
`metadata/pinmux/v2n.yaml` in this checkout, an all-`"TBD"` family genuinely
reaches this today) is NOT fail-soft: `error` severity,
[`tan.exit_codes.ExitCode.VALIDATION_FAILURE`].

**No SDK-resolution warning for a broken project pin.** Unlike `presets`/
`sdk current`, this command never emits `sdk.project-pin-unresolved` --
measured against the oracle with a `.alp/sdk-path` pointing at a nonexistent
checkout: `pinmux` silently falls through to `pinmux.sdk-root-unresolved`
exactly as it would with no pointer at all. `resolve_project_paths`/
`resolve_sdk` (from `presets_cmd`, reused here rather than re-derived) already
carry the pin-rejection detail; this module simply never reads it.

**Row-level fail-soft mirrors `parse_pinmux_table_checked`, not the
un-checked `parse_pinmux_table`**: a pad row missing `e1m_pad`/`e1m_function`
is DROPPED (never an error), and a row's `e1m_pad == "TBD"` sentinel is
dropped too (the source TSV carries no E1M edge pad for that silicon pad --
`metadata/pinmux/v2n.yaml`'s ENTIRE table is TBD-only at the time of writing,
which is exactly what makes `pinmux.table-empty` a live path, not a
hypothetical one).

**`--family` is validated, and the resolved table path re-checked -- a
DELIBERATE divergence from the oracle (tan-cli#359).** The oracle builds
`sdk_root.join("metadata").join("pinmux").join(format!("{fam}.yaml"))` with no
check on `fam` at all, and `Path::join`/`pathlib` both DISCARD the accumulated
prefix when the joined component is absolute -- so `--family <other-sdk>/
metadata/pinmux/aen` read a table out of a completely different checkout while
the envelope still reported `sdkRoot` as the one it never touched (`..`
components walked out the same way). [`_is_safe_family_stem`] and the
[`resolve_confined`] re-check below close that; see `_resolve`'s own comment for
why BOTH are needed rather than either alone.

**A pad field is a `String` in the oracle, not a strict `serde_yaml`
struct-typed deserialize -- refuted by running it.** Every `PinmuxPad` field
is `String`, and (measured against the oracle) `String` fields coerce ANY
scalar to its own YAML-spelled text rather than rejecting a wrong-looking
one: `owner: 7` reads back `"7"`, `silicon_pad: true` reads back `"true"`,
both at exit 0 with no issue -- an earlier version of this module treated any
non-`str` scalar there as a hard parse failure, which was simply wrong, not a
documented divergence. Only a genuine compound value (`owner: [a, b]`,
`e1m_pad: {a: b}`) is a real type mismatch no `String` field can absorb, and
that half stayed a document-level `PinmuxParseError` -- one malformed
sequence/mapping field still fails the whole table, not just that row.

**Text mode renders every issue, not just the summary line -- a DELIBERATE
divergence from the oracle (tan-cli#458).** `crates/tan-cli/src/commands/
pinmux.rs`'s own `text` build (its lines 151-159) is exactly
`vec![format!("pinmux: family={} pads={}", ...)]` and nothing else -- an
exit-2 `pinmux.table-empty`/`pinmux.family-invalid`/
`pinmux.schema-version-unsupported` reaches an oracle terminal as a bare
`pads=0`, with the reason visible only in `--format json`. This port instead
follows the summary line with one `f"pinmux: {issue.message}"` line per issue
on stderr, matching the text-mode convention `generate_cmd`/`monitor_cmd`/
`build_cmd`/`doctor_cmd`/`examples_cmd` already use for THEIR issues (not
every sibling command's: `size_cmd`/`image_cmd` build a separate text list
that drops issues from it entirely, which is their own gap, not a pattern to
match).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.commands.presets_cmd import resolve_project_paths, resolve_sdk
from tan.commands.sdk_cmd import ActiveSdk
from tan.core.fs_confine import PathEscapeError, resolve_confined
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format
from tan.core.shapes import rejected_sdk_root_message, yaml_kind

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

#: The only `schemaVersion` a `metadata/pinmux/<family>.yaml` may declare
#: (`pinmux-capability-v1.schema.json`).
SCHEMA_VERSION = "pinmux-capability-v1"

#: `sku` prefix -> pinmux family stem (`metadata/pinmux/<stem>.yaml`), checked
#: in order -- verbatim from `tan_core::pinmux::pinmux_family_for_sku`. E1M-V2M
#: reuses the base V2N pinout in full (`metadata/e1m_modules/v2n-m1/README.md`)
#: -- there is no separate `v2n-m1.yaml` table, so it maps to `"v2n"` too.
_FAMILY_PREFIX_TABLE = (
    ("E1M-AEN", "aen"),
    ("E1M-NX9", "imx93"),
    ("E1M-V2N", "v2n"),
    ("E1M-V2M", "v2n"),
)


def pinmux_family_for_sku(sku: str) -> str | None:
    """The pinmux family stem for `sku`'s prefix, or `None` for an
    unrecognized SKU."""
    for prefix, stem in _FAMILY_PREFIX_TABLE:
        if sku.startswith(prefix):
            return stem
    return None


def _is_safe_family_stem(family: str) -> bool:
    """True when `family` is a plain `metadata/pinmux/<stem>.yaml` stem: ASCII
    letters/digits/`-`/`_`, non-empty (tan-cli#359).

    A CHARSET allowlist rather than a shape blocklist, the same trade
    `tan.core.flash_plan.validate_identifier` already makes in this tree for
    the same reason: every shape a hand-written blocklist would have to
    enumerate carries a character this charset already rejects -- a separator
    (`/` AND `\\`, checked in the raw string on EVERY host, because pathlib on
    POSIX does not treat `\\` as one, so an `os.sep`-based check would protect
    Linux and leave Windows open), a Windows drive or UNC prefix (`:`), a `.`
    or `..` component (`.`), a NUL or a newline. Deliberately tighter than
    `tan_core::path_guard::is_plain_relative`, which is a check for a plain
    RELATIVE PATH: `a/b` passes that and is still not a family stem, and on
    POSIX it also accepts `C:\\x` and `..\\..\\x` as ordinary filenames.

    No dot is admitted because no `metadata/pinmux/*.yaml` stem has ever
    contained one (`aen`, `imx93`, `v2n`); admitting one to be liberal would
    buy nothing and hand back the `.`/`..` component this rejects outright.
    """
    return bool(family) and all(c.isascii() and (c.isalnum() or c in "-_") for c in family)


@dataclass(frozen=True)
class PinmuxPad:
    e1m_pad: str
    e1m_function: str
    owner: str
    silicon_peripheral: str
    silicon_pad: str

    def as_dict(self) -> dict[str, str]:
        return {
            "e1mPad": self.e1m_pad,
            "e1mFunction": self.e1m_function,
            "owner": self.owner,
            "siliconPeripheral": self.silicon_peripheral,
            "siliconPad": self.silicon_pad,
        }


@dataclass(frozen=True)
class PinmuxTable:
    family: str
    display_name: str | None
    pads: list[PinmuxPad]


class PinmuxParseError(Exception):
    """The pinmux capability document itself did not parse, or its
    `schemaVersion` is not `pinmux-capability-v1` -- the two `Err` cases
    `parse_pinmux_table_checked` distinguishes from an ordinary fail-soft
    dropped row (see the module docstring)."""


#: tan-cli#408: one implementation, in `tan.core.shapes` -- `diff_cmd.py`
#: carried the same body with a docstring this copy had lost.
_yaml_kind = yaml_kind


def _pad_field(row: dict, field: str) -> str | None:
    """`row[field]` coerced to its YAML-spelled string, or `None` when the
    key is absent/null. Raises `PinmuxParseError` for a sequence/mapping
    value -- the one shape a `String` pad field can never absorb; every
    OTHER scalar (bool/int/float, in addition to an actual string) takes its
    YAML-spelled text instead, matching the oracle's own `String`-field
    coercion (measured: `owner: 7` -> `"7"`, `silicon_pad: true` -> `"true"`,
    both at exit 0 -- see the module docstring)."""
    value = row.get(field)
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        raise PinmuxParseError(f"pads[].{field}: expected a string, got {_yaml_kind(value)}")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return str(value)


def parse_pinmux_table_checked(text: str) -> PinmuxTable:
    """Parse a `pinmux-capability-v1` YAML document. Raises `PinmuxParseError`
    for a document that does not parse, is not a mapping, or does not declare
    the exact `schemaVersion` this parser accepts. Individual pad rows fail
    soft per the module docstring."""
    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError as err:
        raise PinmuxParseError(
            "this build of tan has no YAML support installed, so the pinmux table cannot "
            "be read."
        ) from err
    try:
        raw = yaml.safe_load(text)
    except Exception as err:  # noqa: BLE001 -- yaml.YAMLError and anything a loader raises
        raise PinmuxParseError(f"could not be parsed: {err}") from err

    if not isinstance(raw, dict):
        raw = {}
    schema_version = raw.get("schemaVersion")
    if schema_version != SCHEMA_VERSION:
        raise PinmuxParseError(
            f"unsupported pinmux capability schemaVersion {schema_version!r} "
            f"(expected {SCHEMA_VERSION!r})"
        )

    raw_pads = raw.get("pads")
    if raw_pads is not None and not isinstance(raw_pads, list):
        raise PinmuxParseError(f"pads: expected a sequence, got {_yaml_kind(raw_pads)}")

    pads: list[PinmuxPad] = []
    for row in raw_pads or []:
        if not isinstance(row, dict):
            raise PinmuxParseError(f"pads[]: expected a mapping, got {_yaml_kind(row)}")
        e1m_pad = _pad_field(row, "e1m_pad")
        e1m_function = _pad_field(row, "e1m_function")
        if e1m_pad is None or e1m_function is None:
            continue  # `p.e1m_pad?`/`p.e1m_function?` -- missing key, drop the row
        if e1m_pad == "TBD":
            continue  # sentinel: no E1M edge ball for this silicon pad
        pads.append(
            PinmuxPad(
                e1m_pad=e1m_pad,
                e1m_function=e1m_function,
                owner=_pad_field(row, "owner") or "",
                silicon_peripheral=_pad_field(row, "silicon_peripheral") or "",
                silicon_pad=_pad_field(row, "silicon_pad") or "",
            )
        )

    family = raw.get("family")
    display_name = raw.get("display_name")
    return PinmuxTable(
        family=family if isinstance(family, str) else "",
        display_name=display_name if isinstance(display_name, str) else None,
        pads=pads,
    )


_ResolveResult = tuple[
    ActiveSdk | None, str | None, str | None, list[PinmuxPad], list[Issue], ExitCode
]


def _resolve(
    sku: str | None, family: str | None, sdk_root: str | None, root: str
) -> _ResolveResult:
    """`(sdk, resolved_family, display_name, pads, issues, exit_code)` -- the
    whole family/table resolution, isolated from `pinmux()` so the command's
    own top-level `try`/`except` can wrap it once (matching `presets_cmd`'s
    own catch-all convention: an exception nobody enumerated must still reach
    the caller as one coded issue, never a bare traceback with an empty
    stdout).
    """
    issues: list[Issue] = []

    # Family resolution: explicit `--family` wins; else map `--sku` by prefix.
    # `--family` never even evaluates whether `--sku` maps -- no unknown-sku
    # warning fires when `--family` is also given (measured against the
    # oracle: `--sku E1M-BOGUS --family v2n` reports family "v2n", no
    # `pinmux.unknown-sku` issue).
    resolved_family: str | None
    if family is not None:
        resolved_family = family
    elif sku is not None:
        resolved_family = pinmux_family_for_sku(sku)
        if resolved_family is None:
            issues.append(
                Issue(
                    "pinmux.unknown-sku",
                    "warning",
                    f"SKU '{sku}' maps to no known pinmux family.",
                )
            )
    else:
        resolved_family = None
        issues.append(
            Issue("pinmux.no-target", "warning", "Provide --sku <sku> or --family <family>.")
        )

    exit_code = ExitCode.SUCCESS
    sdk = resolve_sdk(sdk_root, root)
    display_name: str | None = None
    pads: list[PinmuxPad] = []

    # tan-cli#359. `--family` is caller-controlled and reaches this join
    # unvalidated on the oracle: an ABSOLUTE value discards the SDK prefix
    # entirely (`Path("/sdk-a") / "/sdk-b/metadata/pinmux/aen"` IS
    # `/sdk-b/...`, same as Rust's `Path::join`) and `..` walks out of it, so
    # the envelope reported `sdkRoot` = the checkout it never read. Checked
    # here, at the ONE place a family becomes a path, so the `--sku` route is
    # covered by construction too -- and BEFORE `sdk`/`resolved_family` are
    # even paired, so a rejected family never reaches the read.
    #
    # TWO INDEPENDENT checks, because each has a hole the other covers: the
    # stem charset cannot see a SYMLINK planted inside `metadata/pinmux/`
    # (`aen.yaml` -> elsewhere is a perfectly plain stem), and a containment
    # re-check alone would wave through a `../pinmux/aen`-shaped family that
    # merely happens to land back inside. Exactly one coded issue either way;
    # nothing else can have been appended yet at the first check (`--family`
    # short-circuits the `--sku` lookup, and `pinmux.no-target` only fires
    # when `resolved_family is None`).
    if resolved_family is not None and not _is_safe_family_stem(resolved_family):
        issues.append(
            Issue(
                "pinmux.family-invalid",
                "error",
                f"Pinmux family '{resolved_family}' is not a plain family stem "
                "(ASCII letters, digits, '-' and '_' only) -- refusing to read a "
                "table from outside <sdkRoot>/metadata/pinmux.",
            )
        )
        return sdk, resolved_family, None, [], issues, ExitCode.VALIDATION_FAILURE

    # tan-cli#468: `resolve_sdk` now always returns an `ActiveSdk`, never a
    # bare `None` -- `sdk.path` is what says whether a usable checkout
    # resolved, so both branches below guard on that instead of `sdk`'s own
    # (now-unconditional) truthiness. `Path(sdk.path)` would otherwise raise
    # on a `None` the moment `resolved_family` alone gated this branch.
    if sdk.path is not None and resolved_family is not None:
        table_dir = Path(sdk.path) / "metadata" / "pinmux"
        try:
            # `resolve_confined` resolves BOTH sides and compares components,
            # so it is correct where a `str.startswith` prefix test is not
            # (Windows case folding, `C:\proj` vs `C:\project2`) -- the same
            # shared guard `init`/`generate`/`scaffold` use, not a fourth
            # hand-rolled copy of the predicate. `OSError`/`ValueError` are
            # caught per its own docstring: a path shape the host rejects
            # outright (a Windows device-namespace path, one embedding a NUL)
            # raises there and is a refusal just the same.
            table_path = resolve_confined(table_dir, table_dir / f"{resolved_family}.yaml")
        except (PathEscapeError, OSError, ValueError):
            issues.append(
                Issue(
                    "pinmux.family-invalid",
                    "error",
                    f"Pinmux capability table for family '{resolved_family}' resolves "
                    "outside <sdkRoot>/metadata/pinmux -- refusing to read it.",
                )
            )
            return sdk, resolved_family, None, [], issues, ExitCode.VALIDATION_FAILURE
        try:
            text = table_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # tan-cli#415: `UnicodeDecodeError` is a `ValueError`, not an
            # `OSError`, so `except OSError` alone let a non-UTF-8 pinmux
            # table escape `_resolve` uncaught -- caught upstream only by
            # `pinmux()`'s generic backstop, which reported it as an `error`-
            # severity `pinmux.internal-failure` at a non-zero exit instead of
            # this SAME `warning`-severity "I don't know" every other
            # unreadable table already gets at exit 0.
            issues.append(
                Issue(
                    "pinmux.table-not-found",
                    "warning",
                    f"No pinmux capability table for family '{resolved_family}' "
                    f"(metadata/pinmux/{resolved_family}.yaml).",
                )
            )
        else:
            try:
                table = parse_pinmux_table_checked(text)
            except PinmuxParseError as err:
                issues.append(
                    Issue(
                        "pinmux.schema-version-unsupported",
                        "error",
                        f"Pinmux capability table for family '{resolved_family}' failed to "
                        f"parse (metadata/pinmux/{resolved_family}.yaml): {err}",
                    )
                )
                exit_code = ExitCode.VALIDATION_FAILURE
            else:
                display_name = table.display_name
                pads = table.pads
                if not pads:
                    # `pinmux-capability-v1.schema.json` requires `minItems: 1`:
                    # a successful parse of a real v1 table is never
                    # legitimately empty.
                    issues.append(
                        Issue(
                            "pinmux.table-empty",
                            "error",
                            f"Pinmux capability table for family '{resolved_family}' parsed "
                            f"with zero pads (metadata/pinmux/{resolved_family}.yaml).",
                        )
                    )
                    exit_code = ExitCode.VALIDATION_FAILURE
    elif sdk.path is None:
        # tan-cli#497 defect 7: when `--sdk-root` WAS given and the loader-marker
        # check rejected it, name the value. The bare string below is the answer
        # for BOTH cases today, so a typo'd flag read as "alp-sdk root is
        # unresolved" with the path the user had just typed nowhere in the
        # envelope (`data.sdkRoot` is `null` on this branch) and nowhere in the
        # stderr text either.
        issues.append(
            Issue(
                "pinmux.sdk-root-unresolved",
                "warning",
                rejected_sdk_root_message(sdk_root, "Cannot read the pinmux table.")
                if sdk_root
                else "alp-sdk root is unresolved; cannot read the pinmux table.",
            )
        )

    return sdk, resolved_family, display_name, pads, issues, exit_code


def pinmux(
    ctx: typer.Context,
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to current directory)."
    ),
    sku: str = typer.Option(
        None,
        "--sku",
        # tan-cli#720: `--som` accepted as an alias -- `tan init` spells this
        # same value `--som`, and being rejected here for the flag `init` just
        # taught is the trap this closes.
        "--som",
        metavar="SKU",
        help="SoM SKU to resolve the pinmux family from (e.g. `E1M-AEN701`) (alias: --som).",
    ),
    board_yaml: str = typer.Option(
        None,
        "--board-yaml",
        metavar="PATH",
        help="Explicit board.yaml path (overrides project resolution).",
    ),
    family: str = typer.Option(
        None,
        "--family",
        metavar="FAMILY",
        help="Pinmux family stem directly (e.g. `aen`, `v2n`); overrides `--sku`.",
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    target: str = typer.Option(  # accepted, not read
        None,
        "--target",
        metavar="EMIT",
        help="Generation target (e.g. zephyr-conf, dts-overlay, cmake-args, yocto-conf).",
    ),
    all_targets: bool = typer.Option(  # accepted, not read
        False, "--all", help="Run command against all relevant targets."
    ),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
    verbose: bool = typer.Option(  # accepted, not read
        False, "--verbose", help="Emit additional diagnostic detail."
    ),
    quiet: bool = typer.Option(  # accepted, not read; pinmux's text line is unconditional
        False, "--quiet", help="Suppress non-essential output."
    ),
    no_color: bool = typer.Option(  # accepted, not read; pinmux emits no ANSI color
        False, "--no-color", help="Disable ANSI color in text output."
    ),
    non_interactive: bool = typer.Option(  # accepted, not read; pinmux never prompts
        False, "--non-interactive", help="Never prompt."
    ),
    ci: bool = typer.Option(  # accepted, not read
        False, "--ci", help="CI mode: implies non-interactive and disables color."
    ),
) -> None:
    """Show the E1M pinmux capability table (E1M pad -> silicon function) for
    a SoM family.

    `--target`/`--all`/`--verbose`/`--quiet`/`--no-color`/`--non-interactive`/
    `--ci` are declared, not consumed: `pinmux` reads only `--sku`/`--family`
    plus the resolved SDK root (`crates/tan-cli/src/commands/pinmux.rs` never
    touches `GlobalArgs::target`/`all`/`verbose`/`quiet`), but the oracle's
    clap `GlobalArgs` are `global = true`, so every verb accepts all of them.
    """
    del target, all_targets, verbose, quiet, no_color, non_interactive, ci
    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    json_mode = resolved_format == "json"

    root, board_path = resolve_project_paths(project, board_yaml)
    try:
        sdk, resolved_family, display_name, pads, issues, exit_code = _resolve(
            sku, family, sdk_root, root
        )
    except Exception as err:  # noqa: BLE001 -- the envelope IS the error contract
        sdk, resolved_family, display_name, pads = None, family, None, []
        issues = [
            Issue(
                "pinmux.internal-failure",
                "error",
                f"pinmux failed unexpectedly: {err.__class__.__name__}: {err}",
            )
        ]
        exit_code = ExitCode.INTERNAL_FAILURE

    data: dict[str, Any] = {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "sdkRoot": sdk.path if sdk is not None else None,
    }
    if sku is not None:
        data["sku"] = sku
    data["family"] = resolved_family
    if display_name is not None:
        data["displayName"] = display_name
    data["pads"] = [p.as_dict() for p in pads]

    # Built ONCE, for both formats: `Envelope.__init__` appends the
    # tan-cli#407 `sdk.discovery-divergent` warning to a NEW issues list
    # (`_with_sdk_divergence`) rather than mutating the one passed in, so a
    # text branch that kept iterating the pre-envelope `issues` local (as
    # this one used to) would build its summary+issues lines correctly for
    # every issue `_resolve` raised, but silently never print a divergence
    # warning appended at the envelope seam -- reproduced live: two alp-sdk
    # checkouts resolving from one directory with no `--sku`/`--family` at
    # all printed only `pinmux: family=- pads=0` in text mode while the JSON
    # envelope carried `sdk.discovery-divergent` alongside `pinmux.no-target`.
    envelope = Envelope(
        "pinmux",
        Project.resolved(root, board_path),
        data,
        issues,
        exit_code,
        # tan-cli#468: `sdk` (from `_resolve`) is now a real `ActiveSdk` even
        # when nothing usable resolved -- `.path` is what says whether there
        # is a checkout to report, `sdk is not None` alone no longer does.
        sdk=SdkInfo.from_resolution(sdk.path, sdk)
        if sdk is not None and sdk.path is not None
        else None,
    )
    if json_mode:
        emit(envelope)
    else:
        stream = typer.get_text_stream("stderr")
        stream.write(f"pinmux: family={resolved_family or '-'} pads={len(pads)}\n")
        # tan-cli#458: the summary line above answers "what happened" but never
        # "why" -- an exit-2 `pinmux.table-empty`/`pinmux.family-invalid`/
        # `pinmux.schema-version-unsupported` used to print a plausible
        # `pads=0` and nothing else, with the envelope's own issue never
        # reaching a terminal. `generate_cmd`/`monitor_cmd`/`build_cmd`/
        # `doctor_cmd`/`examples_cmd` already render issues in text mode this
        # same way, one `f"{command}: {issue.message}"` line per issue on
        # stderr -- pinmux was an outlier among THOSE that built a summary
        # line and stopped (`size_cmd`/`image_cmd` are a separate, still-open
        # gap: they build a distinct text list that drops issues from it
        # entirely, out of scope here).
        for issue in envelope.issues:
            stream.write(f"pinmux: {issue.message}\n")
    raise typer.Exit(int(exit_code))
