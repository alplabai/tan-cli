# SPDX-License-Identifier: Apache-2.0
"""`tan pinmux` -- the E1M pinmux capability table (E1M pad -> silicon
function) for a SoM family (tan-cli#257).

Mirrors `crates/tan-cli/src/commands/pinmux.rs` plus the `tan-core` helpers it
composes (`pinmux::{parse_pinmux_table_checked, pinmux_family_for_sku}`).
Resolves a `metadata/pinmux/<family>.yaml` family stem from an explicit
`--family` or a `--sku` prefix, reads that table out of the resolved SDK root,
and echoes it in the envelope -- the single source the extension/LSP consume
instead of parsing `metadata/pinmux/<family>.yaml` themselves.

**Fail-soft, deliberately, with one exception.** An unresolved SDK root, an
unknown SKU, no `--sku`/`--family` at all, or a family with no generated table
on disk are each a `warning`-severity issue at exit 0 -- `pinmux` answers "I
don't know" the same way for all of them, never a hard failure. A table that
DOES exist on disk but fails to parse (schema-version skew) or parses to ZERO
pads (`pinmux-capability-v1.schema.json` requires `minItems: 1`, so an empty
table is never a legitimate v1 document -- and, measured against the real
`metadata/pinmux/v2n.yaml` in this checkout, an all-`"TBD"` family genuinely
reaches this today) is the one case that is NOT fail-soft: `error` severity,
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
hypothetical one). A pad field present with the WRONG YAML kind (e.g. a
number where a string belongs) is treated as a document-level parse failure,
matching `serde_yaml`'s struct-typed deserialize -- one malformed field fails
the whole table, not just that row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.commands.presets_cmd import resolve_project_paths, resolve_sdk
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode

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

#: The pad struct fields, in `PinmuxPad`'s serialized wire order.
_PAD_FIELDS = ("e1m_pad", "e1m_function", "owner", "silicon_peripheral", "silicon_pad")


def pinmux_family_for_sku(sku: str) -> str | None:
    """The pinmux family stem for `sku`'s prefix, or `None` for an
    unrecognized SKU."""
    for prefix, stem in _FAMILY_PREFIX_TABLE:
        if sku.startswith(prefix):
            return stem
    return None


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


def _yaml_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a sequence"
    if isinstance(value, dict):
        return "a mapping"
    return type(value).__name__


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
        for field in _PAD_FIELDS:
            value = row.get(field)
            if value is not None and not isinstance(value, str):
                kind = _yaml_kind(value)
                raise PinmuxParseError(f"pads[].{field}: expected a string, got {kind}")
        e1m_pad = row.get("e1m_pad")
        e1m_function = row.get("e1m_function")
        if e1m_pad is None or e1m_function is None:
            continue  # `p.e1m_pad?`/`p.e1m_function?` -- missing key, drop the row
        if e1m_pad == "TBD":
            continue  # sentinel: no E1M edge ball for this silicon pad
        pads.append(
            PinmuxPad(
                e1m_pad=e1m_pad,
                e1m_function=e1m_function,
                owner=row.get("owner") or "",
                silicon_peripheral=row.get("silicon_peripheral") or "",
                silicon_pad=row.get("silicon_pad") or "",
            )
        )

    family = raw.get("family")
    display_name = raw.get("display_name")
    return PinmuxTable(
        family=family if isinstance(family, str) else "",
        display_name=display_name if isinstance(display_name, str) else None,
        pads=pads,
    )


_ResolvedSdk = tuple[str, str, str | None]
_ResolveResult = tuple[
    _ResolvedSdk | None, str | None, str | None, list[PinmuxPad], list[Issue], ExitCode
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

    if sdk is not None and resolved_family is not None:
        table_path = Path(sdk[0]) / "metadata" / "pinmux" / f"{resolved_family}.yaml"
        try:
            text = table_path.read_text(encoding="utf-8")
        except OSError:
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
    elif sdk is None:
        issues.append(
            Issue(
                "pinmux.sdk-root-unresolved",
                "warning",
                "alp-sdk root is unresolved; cannot read the pinmux table.",
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
        metavar="SKU",
        help="SoM SKU to resolve the pinmux family from (e.g. `E1M-AEN701`).",
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
    output_format: str = typer.Option(
        None, "--format", metavar="FORMAT", help="Output format: text or json."
    ),
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
    resolved_format = (
        output_format if output_format is not None else (ctx.obj or {}).get("format") or "text"
    )
    if resolved_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{resolved_format}' (choose from 'text', 'json')", param_hint="--format"
        )
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
        "sdkRoot": sdk[0] if sdk is not None else None,
    }
    if sku is not None:
        data["sku"] = sku
    data["family"] = resolved_family
    if display_name is not None:
        data["displayName"] = display_name
    data["pads"] = [p.as_dict() for p in pads]

    if json_mode:
        emit(
            Envelope(
                "pinmux",
                Project.resolved(root, board_path),
                data,
                issues,
                exit_code,
                sdk=SdkInfo(sdk[0], sdk[1]) if sdk is not None else None,
            )
        )
    else:
        stream = typer.get_text_stream("stderr")
        stream.write(f"pinmux: family={resolved_family or '-'} pads={len(pads)}\n")
    raise typer.Exit(int(exit_code))
