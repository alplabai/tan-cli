# SPDX-License-Identifier: Apache-2.0
"""The ALP **system manifest** (`build/system-manifest.yaml`) -- the derived
projection of a `board.yaml` that `--emit system-manifest` writes
(`tan/planner/manifest.py`, relocated from alp-sdk's `alp_orchestrate`), and the
tool CONTRACT for a (possibly multi-image) project. `tan image` and `tan size` both read THIS instead of re-deriving
anything from `board.yaml` + the SoM presets.

Port of `crates/tan-core/src/system_manifest.rs` (read half) plus the two
path rules its consumers share.

TOLERANT READER (the stability policy negotiated in alp-sdk#106):
`schema_version` 1 is additive-only, so an unknown key is IGNORED, never
rejected -- a newer SDK must not break an older `tan`. What IS rejected is a
document the Rust `serde` model could not deserialise at all: a non-mapping
root, a missing `schema_version`, or a `slices[]`/`ipc[]`/`helper_mcus[]` entry
missing a field that is non-`Option` there. Those are hard errors in the
oracle, and a partial read here would let `tan` act on a manifest the shipped
binary refuses.

**Nothing here shells the SDK.** Every input is a file this project's own build
wrote under its build root; the alp-sdk checkout is never invoked. That is
invariant I-32 and its anti-pattern #22 in
`docs/superpowers/specs/2026-07-29-tan-port-invariants.md`: giving a command an
alp-sdk-checkout dependency it deliberately does not have is a regression no
gate catches. `size` does READ `<sdk>/metadata/**` for the memory budget (the
oracle does too, and a missing SDK is non-fatal there -- the budget resolves
`unknown`), but it never executes anything in it, and no hardware fact -- SKU,
address, pin name -- is spelled in this port. Those live in alp-sdk
`metadata/` (I-26).
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: The schema major this CLI consumes. A different value is REFUSED rather than
#: silently mis-applied -- `SYSTEM_MANIFEST_SCHEMA_VERSION` in the Rust.
SYSTEM_MANIFEST_SCHEMA_VERSION = 1

#: The single input document, read from the build root.
MANIFEST_FILE = "system-manifest.yaml"

#: Subdirectory `west build` writes into when it is emitted with NO `-d` --
#: which is exactly how the SDK's build plan emits it (I-18). See
#: [`slice_elf_candidates`].
WEST_NESTED_DIR = "build"


class SystemManifestError(Exception):
    """Why a system-manifest could not be consumed. `message` is the text the
    oracle puts after `<path>: ` in its `*.manifest-invalid` issue."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _parse_error(detail: str) -> SystemManifestError:
    # Verbatim `SystemManifestError::Parse`'s `#[error(...)]` prefix. The
    # `detail` tail comes from a different YAML library than the oracle's
    # `serde_yaml`, so a syntax-error tail differs in wording; the shapes that
    # matter to a reader (`missing field \`x\``, `invalid type: ...`) are
    # reproduced below.
    return SystemManifestError(f"system-manifest is not valid YAML: {detail}")


def _unsupported_version_error(found: Any) -> SystemManifestError:
    # Verbatim `SystemManifestError::UnsupportedSchemaVersion`'s `#[error(...)]`.
    return SystemManifestError(
        f"unsupported system-manifest schema_version {found} "
        f"(this CLI consumes v{SYSTEM_MANIFEST_SCHEMA_VERSION}); "
        "upgrade the CLI or the SDK so the versions match"
    )


def _serde_type_name(value: Any) -> str:
    """serde's own spelling of an unexpected value's type, for the
    `invalid type: <this>, expected struct SystemManifest` message."""
    if value is None:
        return "unit value"
    if isinstance(value, bool):
        return f"boolean `{str(value).lower()}`"
    if isinstance(value, str):
        return f'string "{value}"'
    if isinstance(value, int):
        return f"integer `{value}`"
    if isinstance(value, float):
        return f"floating point `{value}`"
    if isinstance(value, (list, tuple)):
        return "sequence"
    return "map"


#: YAML **1.2 core-schema** implicit resolvers -- what serde_yaml applies, and
#: NOT what PyYAML's `SafeLoader` applies (that is YAML 1.1). Verified case by
#: case against the compiled binary; every row below is a document where the two
#: disagree and the difference reaches `data.hw_info` verbatim:
#:   `yes`/`no`/`on`/`off`  1.1 boolean   -> 1.2 plain STRING
#:   `007`                  1.1 int 7     -> 1.2 plain STRING
#:   `1:30`                 1.1 int 90    -> 1.2 plain STRING
#:   `2024-01-01`           1.1 date      -> 1.2 plain STRING (and a `date` is not
#:                                          JSON-serializable, so PyYAML's answer
#:                                          made `tan image` exit 3 where the
#:                                          oracle exits 0)
#:   `0o17`                 1.1 string    -> 1.2 int 15
#: `0x…` is an int in both. The regexes are the core schema's own.
#:
#: tan-cli#499: the `int` row is the oracle's ACTUAL resolver
#: (`serde_yaml::parse_signed_int`), not the YAML 1.2 spec core schema this
#: comment's header claims to transcribe -- `parse_signed_int` strips a
#: leading `+`/`-` BEFORE testing the radix prefixes (the spec-literal regex
#: below applied the sign only to the bare-decimal branch, leaving a signed
#: `0x`/`0o` value a STRING) and also accepts `0b`, which the spec regex has
#: no branch for at all. Verified against the compiled binary:
#: `mask: 0b1010` -> int `10`, `off: -0x1E` -> int `-30`, `pos: +0x1E` -> int
#: `30`, `noct: -0o17` -> int `-15`. Kept as a REGEX gate rather than handed
#: to PyYAML's own `construct_yaml_int` on a match: the oracle leaves an
#: uppercase prefix (`0B11`, `0XA5`) or an underscore-grouped literal
#: (`0x1_F`, `0b1_0`, `0o1_7`, `1_000`) as a STRING, where PyYAML's
#: constructor strips underscores and accepts uppercase prefixes and would
#: resolve every one of those to an int the oracle does not.
_CORE_RESOLVERS = (
    ("tag:yaml.org,2002:null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    (
        "tag:yaml.org,2002:bool",
        r"^(?:true|True|TRUE|false|False|FALSE)$",
        list("tTfF"),
    ),
    (
        "tag:yaml.org,2002:int",
        r"^[-+]?(?:0|[1-9][0-9]*|0b[01]+|0o[0-7]+|0x[0-9a-fA-F]+)$",
        list("-+0123456789"),
    ),
    (
        "tag:yaml.org,2002:float",
        r"^(?:[-+]?(?:\.[0-9]+|(?:0|[1-9][0-9]*)(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        list("-+.0123456789"),
    ),
)


#: The infinity/not-a-number spellings serde_yaml's `parse_f64` special-cases
#: BEFORE it ever tries a numeric parse, matched against the scalar with any
#: leading sign stripped. Only these become non-finite floats; see
#: [`_construct_core_float`].
_YAML_INF_SPELLINGS = (".inf", ".Inf", ".INF")
_YAML_NAN_SPELLINGS = (".nan", ".NaN", ".NAN")


def _construct_core_float(loader, node):
    """A float-tagged plain scalar as serde_yaml's `parse_f64` resolves it --
    which is NOT what `float()` does for an overflowing exponent (tan-cli#387).

    serde_yaml accepts a parsed number only `if float.is_finite()`, and returns
    `None` otherwise; a `serde_yaml::Value` then falls through to the next
    candidate type and the scalar stays a STRING. So `soc_flash_mb: 1e400` is
    the string `"1e400"` on the oracle, while PyYAML's own float constructor
    resolves it to `inf`. That diverged twice over: the wrong TYPE in
    `data.hw_info`, and then a value `json.dumps` wrote as the non-JSON literal
    `Infinity` into both stdout and the persisted `bundle-manifest.json`.

    The three EXPLICIT spellings are the deliberate exception -- `.inf`, `-.inf`
    and `.nan` really do resolve to non-finite `f64` on the oracle. Turning them
    into `null` is `serde_json`'s job at serialise time
    (`tan.envelope.json_safe_floats`), not this loader's: doing it here would
    emit `null` where the oracle emits the string `"1e400"`, and would lose the
    distinction between the two cases entirely.
    """
    text = loader.construct_scalar(node)
    unsigned = text[1:] if text[:1] in ("-", "+") else text
    if unsigned in _YAML_INF_SPELLINGS:
        return float("-inf") if text.startswith("-") else float("inf")
    if unsigned in _YAML_NAN_SPELLINGS:
        return float("nan")
    try:
        value = float(text)
    except ValueError:
        # Only reachable through an EXPLICIT `!!float` tag -- the implicit
        # resolver's regex admits nothing `float()` refuses. The raw text is
        # what serde_yaml is left holding too.
        return text
    return value if math.isfinite(value) else text


def _core_schema_loader(yaml_module):
    """A `SafeLoader` resolving plain scalars by the YAML 1.2 CORE schema, so a
    value carried verbatim into the bundle manifest matches what serde_yaml's
    `Value` would have produced. See [`_CORE_RESOLVERS`]."""

    class CoreSchemaLoader(yaml_module.SafeLoader):
        pass

    # A fresh dict on the subclass -- never a mutation of SafeLoader's own, which
    # would silently change every other `safe_load` in the process.
    CoreSchemaLoader.yaml_implicit_resolvers = {}
    for tag, pattern, first in _CORE_RESOLVERS:
        CoreSchemaLoader.add_implicit_resolver(tag, re.compile(pattern), first)
    # `add_constructor` copies `yaml_constructors` onto the subclass first, so
    # this is scoped the same way the resolvers above are.
    CoreSchemaLoader.add_constructor("tag:yaml.org,2002:float", _construct_core_float)
    return CoreSchemaLoader


def _literal_scalar_loader(yaml_module):
    """A `SafeLoader` whose implicit tag resolution is switched OFF entirely, so
    every plain scalar comes back as its RAW TEXT.

    This is not a nicety. serde_yaml resolves a plain scalar against the TARGET
    type, so a `String` field receives the scalar's raw text -- `os: ~` is the
    string `"~"`, `build_dir: 007` is the path `007`, `sku: 0x10` is `"0x10"`.
    Resolving first and stringifying after gives `"None"`, `"7"` and `"16"`: three
    different values than the oracle for the same document, and `build_dir` /
    `output_artefact` / `firmware_path` are PATHS.

    An explicit tag (`!!int 5`) still resolves, which is why `_scalar_str` below
    keeps its coercion arm.
    """

    class LiteralScalarLoader(yaml_module.SafeLoader):
        pass

    LiteralScalarLoader.yaml_implicit_resolvers = {}
    return LiteralScalarLoader


def _import_yaml():
    """PyYAML, or a [`SystemManifestError`] naming why it isn't available --
    the one import guard [`load_yaml_document`] and [`serialize_system_manifest_raw`]
    both funnel through, so a frozen `tan` built from a stale venv reports the
    SAME coded envelope on either the read or the write path rather than one
    of them tracebacking."""
    try:
        import yaml  # noqa: PLC0415  (declared nowhere; imported where needed)
    except ImportError as err:
        raise _parse_error(
            f"no YAML parser available ({err}); install PyYAML "
            "(`pip install pyyaml`) so tan can read system-manifest.yaml"
        ) from err
    return yaml


def load_yaml_document(text: str, *, literal_scalars: bool = False) -> Any:
    """Parse a manifest document, with PyYAML's absence and every parse failure
    funnelled into [`SystemManifestError`].

    Two scalar policies, both required and neither PyYAML's default:
    `literal_scalars` keeps every plain scalar as its raw text
    ([`_literal_scalar_loader`]); otherwise the YAML 1.2 core schema applies
    ([`_core_schema_loader`]). `yaml.safe_load` is used NOWHERE -- its YAML 1.1
    resolution disagrees with serde_yaml on five shapes that reach the wire.

    PyYAML IS a declared runtime dependency (`pyproject.toml` records why: a
    frozen `tan` built without it has a `size`/`image` that can never read a
    manifest). The import is still guarded, because a declared dependency is not
    a present one -- a frozen artifact built from a stale venv, or a broken
    source install, must still emit an envelope. The oracle cannot reach this
    branch at all (serde_yaml is compiled in), so the code reused is
    `*.manifest-invalid`: an existing spelling with an honest message beats
    inventing one the extension has never heard of.
    """
    yaml = _import_yaml()
    try:
        loader = (
            _literal_scalar_loader(yaml)
            if literal_scalars
            else _core_schema_loader(yaml)
        )
        return yaml.load(text, Loader=loader)  # noqa: S506 -- SafeLoader-derived
    except Exception as err:  # noqa: BLE001 -- any PyYAML failure is bad input
        # Includes ConstructorError for an unhashable YAML key (a sequence or
        # mapping used as a key), which the oracle tolerates on read and only
        # fails on later, at serialize time. Both ends emit a coded envelope;
        # only the code differs, and neither ever tracebacks.
        raise _parse_error(str(err).replace("\n", " ")) from err


# The string-typed fields of each modeled struct, split by whether the Rust models
# them `String` or `Option<String>`. Enumerated rather than sniffed because the
# CHECK is what makes this reader agree with serde on which documents are legal --
# and the SPLIT decides what a null means, which is not the same on both sides:
#
#   `os: ~`          `String`          -> the string "~"  (serde_yaml hands a
#                                        `String` the scalar's RAW TEXT)
#   `build_dir: ~`   `Option<String>`  -> ABSENT           (`deserialize_option`
#                                        sees the null first and yields `None`)
#
# Getting that backwards puts a slice's build dir at the literal path `~`, or
# loses a real `os` value. Both verified against the compiled binary.
#
# `flash_args` is a `serde_yaml::Value` in both structs and is deliberately absent
# from every tuple: it is opaque, and any shape is legal.
_SLICE_REQUIRED_STRINGS = ("core_id", "os", "status")
_SLICE_OPTIONAL_STRINGS = (
    "app", "image", "machine", "board", "toolchain", "build_dir",
    "output_artefact", "log_path", "reason", "flash_method",
)
_HELPER_REQUIRED_STRINGS = ("name", "chip")
_HELPER_OPTIONAL_STRINGS = ("firmware_path", "flash_method", "update_channel")
_IPC_REQUIRED_STRINGS = ("name", "kind")
_IPC_OPTIONAL_STRINGS = ("status", "reason")
_HW_INFO_REQUIRED_STRINGS = ("sku",)
_HW_INFO_OPTIONAL_STRINGS = ("som_hw_rev", "board_name", "board_hw_rev", "silicon")

#: Every root key the Rust models as a `Vec<_>`. Present-but-not-a-sequence is a
#: hard error, including `~`: unlike a string field, serde_yaml does NOT accept a
#: null there (verified against the binary -- `slices: ~` is refused while
#: `hw_info.sku: ~` is accepted).
_SEQUENCE_KEYS = ("slices", "ipc", "helper_mcus", "boot_order", "storage")


def _scalar_str(value: Any, field: str, where: str) -> str:
    """A YAML scalar as the string serde_yaml produces for a `String` field.

    Reached with the LITERAL tree (see [`_literal_scalar_loader`]), so a plain
    scalar is already its raw text and this is a pass-through: `build_dir: 7` is
    the path `7`, `os: ~` is the string `"~"`, `sku: 0x10` is `"0x10"` -- all
    verified against the compiled binary, and all wrong if the resolved value is
    stringified instead.

    The coercion arm covers an EXPLICIT tag (`!!int 5`), which resolves even with
    implicit resolution off. A sequence or mapping is refused, as serde does.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise _parse_error(
        f"{where}.{field}: invalid type: {_serde_type_name(value)}, expected a string"
    )


def _coerce_strings(
    entry: dict,
    core: Any,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    where: str,
) -> None:
    """Validate + normalise every present string-typed field of `entry` in place.

    `core` is the same mapping out of the CORE-schema tree, consulted for one
    thing only: whether the scalar was a YAML null. An `Option<String>` field that
    was is DELETED (serde yields `None`, i.e. absent); a plain `String` keeps its
    raw text.
    """
    core = core if isinstance(core, dict) else {}
    for field in required:
        if field in entry:
            entry[field] = _scalar_str(entry[field], field, where)
    for field in optional:
        if field not in entry:
            continue
        if core.get(field, "") is None:
            del entry[field]
            continue
        entry[field] = _scalar_str(entry[field], field, where)


def _sequence(root: dict, key: str) -> list:
    """A root `Vec<_>` field: absent is `[]`, present-but-not-a-sequence is a hard
    error (`~` included)."""
    if key not in root:
        return []
    raw = root[key]
    if not isinstance(raw, list):
        raise _parse_error(
            f"{key}: invalid type: {_serde_type_name(raw)}, expected a sequence"
        )
    return raw


def _entries(
    root: dict,
    core_root: Any,
    key: str,
    required_present: tuple[str, ...],
    required_strings: tuple[str, ...],
    optional_strings: tuple[str, ...],
) -> list[dict]:
    """One of the manifest's three lists of mappings, with every field that is
    non-`Option` in the Rust struct enforced and every string-typed field
    normalised. A non-list value, a non-mapping entry, or a missing required field
    fails the WHOLE document -- serde does too, and reading half of it would make
    `tan` act on a manifest the shipped binary rejects."""
    core_entries = core_root.get(key) if isinstance(core_root, dict) else None
    core_entries = core_entries if isinstance(core_entries, list) else []
    out: list[dict] = []
    for index, entry in enumerate(_sequence(root, key)):
        where = f"{key}[{index}]"
        if not isinstance(entry, dict):
            raise _parse_error(
                f"{where}: invalid type: {_serde_type_name(entry)}, expected a map"
            )
        for field in required_present:
            if field not in entry:
                raise _parse_error(f"{where}: missing field `{field}`")
        core = core_entries[index] if index < len(core_entries) else {}
        _coerce_strings(entry, core, required_strings, optional_strings, where)
        out.append(entry)
    return out


class SystemManifest:
    """A parsed, version-guarded manifest over the LITERAL-scalar tree (see
    [`_literal_scalar_loader`]). Entries stay raw `dict`s -- with their
    string-typed fields validated in place -- because the fields this family reads
    are few, and a typed projection is exactly what makes the Rust reader lossy on
    write (`serialize_system_manifest`'s own warning)."""

    __slots__ = ("root", "schema_version", "sku", "slices", "helper_mcus")

    def __init__(self, root: dict, core_root: Any) -> None:
        self.root = root
        # From the CORE tree: it is a `u32` in the Rust, and the literal tree holds
        # the scalar's text (`"1"`, not `1`).
        self.schema_version = core_root["schema_version"]
        if "hw_info" in root:
            hw_info = root["hw_info"]
            if not isinstance(hw_info, dict):
                raise _parse_error(
                    f"hw_info: invalid type: {_serde_type_name(hw_info)}, "
                    "expected struct HwInfo"
                )
            _coerce_strings(
                hw_info,
                core_root.get("hw_info") if isinstance(core_root, dict) else None,
                _HW_INFO_REQUIRED_STRINGS,
                _HW_INFO_OPTIONAL_STRINGS,
                "hw_info",
            )
        else:
            hw_info = {}
        if "generated_by" in root:
            root["generated_by"] = _scalar_str(
                root["generated_by"], "generated_by", "SystemManifest"
            )
        self.sku: str = hw_info.get("sku") or ""
        self.slices = _entries(
            root,
            core_root,
            "slices",
            ("core_id", "os"),
            _SLICE_REQUIRED_STRINGS,
            _SLICE_OPTIONAL_STRINGS,
        )
        self.helper_mcus = _entries(
            root,
            core_root,
            "helper_mcus",
            ("name", "chip"),
            _HELPER_REQUIRED_STRINGS,
            _HELPER_OPTIONAL_STRINGS,
        )
        # `ipc[]` and the remaining sequence fields are validated but not otherwise
        # read here: a document the oracle rejects must fail this parse too, or
        # `tan image` would bundle a manifest `tan flash` then refuses.
        ipc = _entries(
            root,
            core_root,
            "ipc",
            ("name", "kind"),
            _IPC_REQUIRED_STRINGS,
            _IPC_OPTIONAL_STRINGS,
        )
        for entry in ipc:
            if "endpoints" in entry:
                raw = entry["endpoints"]
                if not isinstance(raw, list):
                    raise _parse_error(
                        f"ipc.endpoints: invalid type: {_serde_type_name(raw)}, "
                        "expected a sequence"
                    )
                entry["endpoints"] = [
                    _scalar_str(e, "endpoints", "ipc") for e in raw
                ]
        for key in _SEQUENCE_KEYS:
            _sequence(root, key)


def parse_system_manifest(text: str) -> SystemManifest:
    """Parse + version-guard a `system-manifest.yaml` document. No IO.

    Order matters and mirrors serde: the document's SHAPE is checked before its
    `schema_version` value, because serde fails deserialization outright on a
    malformed `slices[]` and never reaches the version comparison.

    TWO parses of the same text, deliberately, because serde resolves a scalar
    against its TARGET type and no single Python tree can stand in for all of them:
      * `schema_version` is a `u32`, so its check needs real type resolution --
        `schema_version: '1'` is a hard error and `schema_version: 1` is not, and a
        literal-scalar tree cannot tell those apart;
      * a `String` field takes the scalar's RAW TEXT ([`_literal_scalar_loader`]);
      * an `Option<String>` field takes `None` for a null, which only the resolving
        tree can identify.
    Both trees come from the same document, so they have the same structure and the
    shape checks are identical whichever one runs them.
    """
    core = load_yaml_document(text)
    root = load_yaml_document(text, literal_scalars=True)
    if not isinstance(root, dict):
        raise _parse_error(
            f"invalid type: {_serde_type_name(core)}, expected struct SystemManifest"
        )
    if "schema_version" not in root:
        raise _parse_error("missing field `schema_version`")
    version = core["schema_version"] if isinstance(core, dict) else None
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise _parse_error(
            f"invalid type: {_serde_type_name(version)}, expected u32"
        )
    manifest = SystemManifest(root, core)
    # AFTER the shape check, and comparing the full value -- a `schema_version`
    # of 2**32 + 1 is congruent to 1 mod 2**32, so a truncating guard would
    # wrongly accept it (`parse_system_manifest_raw_rejects_schema_version_
    # beyond_u32` in the oracle).
    if version != SYSTEM_MANIFEST_SCHEMA_VERSION:
        raise _unsupported_version_error(version)
    return manifest


# --------------------------------------------------------------------------
# The write path: overlay this run's per-slice outcomes onto a freshly
# emitted system-manifest and serialize it back out. Port of the RAW half of
# `tan_core::system_manifest` (`parse_system_manifest_raw` /
# `overlay_run_results_raw` / `serialize_system_manifest_raw`) -- the half
# `crates/tan-cli/src/commands/build/execute/manifest.rs` uses for the
# post-build rewrite, never the typed `SystemManifest`/`serialize_system_
# manifest` pair (the Rust module doc explains why: the typed struct there
# only models the fields it reads, so re-serializing it silently drops any
# field it doesn't model -- an rpmsg `IpcLink` carve-out, `hw_info.eeprom`,
# anything a newer SDK adds).
#
# That specific defect does not reproduce here: [`SystemManifest`]'s own
# `slices`/`helper_mcus` entries above are already plain `dict`s carrying
# every key the document had (`_entries` normalises known string fields IN
# PLACE and never drops the rest) -- so a plain re-dump of `SystemManifest.root`
# would not lose an additive field either. This module still keeps a distinct
# `parse_system_manifest_raw` entry point rather than reusing `parse_system_
# manifest`, for a different reason: the typed reader does TWO parses of the
# same text (a literal-scalar tree for `String` fields, a core-schema tree for
# everything else -- see `parse_system_manifest`'s own docstring) specifically
# to interpret an ARBITRARY, possibly hand-authored document the way serde_yaml
# would. The write path's input is never that: it is the SDK's own YAML,
# freshly emitted and immediately rewritten in the same step, so the single
# core-schema parse `load_yaml_document` already performs is sufficient and
# this avoids reasoning about which of the two trees a write should mutate.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SliceRunResult:
    """One slice's post-build outcome, as the executor reports it -- the
    write-side counterpart of a manifest `slices[]` entry. Port of
    `tan_core::system_manifest::SliceRunResult` (a 5-tuple there; a dataclass
    here for readability -- same fields, same order, same "`None` leaves the
    plan-time value untouched" meaning for every optional one). Pass an
    EXPLICIT empty string (never `None`) to actively CLEAR a stale
    `output_artefact`/`build_dir` for a slice this run knows never dispatched
    (mirrors the Rust oracle's `Some(String::new())` convention for a demoted
    slice)."""

    core_id: str
    status: str
    output_artefact: str | None = None
    build_dir: str | None = None
    reason: str | None = None


def parse_system_manifest_raw(text: str) -> dict:
    """Parse a `system-manifest.yaml` document into a raw mapping (str-in /
    dict-out) -- the entry point the write path uses instead of
    [`parse_system_manifest`]; see the section docstring above for why a
    single core-schema parse is enough here. Still version-guarded
    identically to the typed reader -- but with a DIFFERENT, collapsed error
    message on a missing/malformed `schema_version`: the oracle's raw parser
    (`tan_core::system_manifest::parse_system_manifest_raw`,
    system_manifest.rs:294-310) extracts it with a single
    `.get("schema_version").and_then(as_u64)` and reports every failure --
    a missing key, a non-mapping root, a non-numeric, negative, or
    out-of-`u64`-range value -- as the one string "missing or non-numeric
    schema_version", not the typed reader's two-branch "missing field" /
    "invalid type" pair. This string reaches a build's `issues[].message`
    verbatim (a failed post-build manifest write folds it in), so the exact
    wording is part of the wire contract, not cosmetic."""
    root = load_yaml_document(text)
    version = root.get("schema_version") if isinstance(root, dict) else None
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not (0 <= version <= _SERDE_INT_MAX)
    ):
        raise _parse_error("missing or non-numeric schema_version")
    if version != SYSTEM_MANIFEST_SCHEMA_VERSION:
        raise _unsupported_version_error(version)
    return root


def overlay_run_results_raw(raw: dict, results: Sequence[SliceRunResult]) -> None:
    """Overlay this run's per-slice outcomes onto the RAW manifest mapping (as
    returned by [`parse_system_manifest_raw`]) so the written `system-
    manifest.yaml` reflects what actually built. Slices are matched by
    `core_id`; a slice with no matching result (e.g. an `off` core) is left
    untouched. `output_artefact`/`build_dir`/`reason` are each written only
    when the result carries one -- a `None` never wipes a plan-time value.
    Port of `tan_core::system_manifest::overlay_run_results_raw`. Pure: no IO,
    mutates `raw` in place."""
    slices = raw.get("slices")
    if not isinstance(slices, list):
        return
    # FIRST match wins, not last -- `results.iter().find(...)` in the oracle
    # (system_manifest.rs:332-333). Unreachable with today's callers (one
    # result per slice), but `setdefault` keeps a silent semantic flip out of
    # a ported pure function rather than relying on "no caller ever hits it".
    by_core: dict[str, SliceRunResult] = {}
    for r in results:
        by_core.setdefault(r.core_id, r)
    for entry in slices:
        if not isinstance(entry, dict):
            continue
        result = by_core.get(entry.get("core_id"))
        if result is None:
            continue
        entry["status"] = result.status
        if result.output_artefact is not None:
            entry["output_artefact"] = result.output_artefact
        if result.build_dir is not None:
            entry["build_dir"] = result.build_dir
        if result.reason is not None:
            entry["reason"] = result.reason


def serialize_system_manifest_raw(raw: dict) -> str:
    """Serialize the RAW manifest mapping (as returned by
    [`parse_system_manifest_raw`], possibly overlaid by
    [`overlay_run_results_raw`]) back to YAML. Port of `tan_core::
    system_manifest::serialize_system_manifest_raw`."""
    yaml = _import_yaml()
    return yaml.safe_dump(raw, sort_keys=False, default_flow_style=False, allow_unicode=True)


#: `serde_yaml::Value`'s integer range: `i64::MIN ..= u64::MAX`. A YAML integer
#: outside it cannot be represented, so the WHOLE `Value` parse fails and
#: `raw_passthrough` yields its defaults -- silently dropping `hw_info` and
#: `boot_order`. Reproduced rather than "improved": Python's unbounded int would
#: otherwise put a number in `bundle-manifest.json` that no `JSON.parse` consumer
#: can hold without losing digits, which is a worse answer than the oracle's.
_SERDE_INT_MIN = -(2**63)
_SERDE_INT_MAX = 2**64 - 1


def _representable_by_serde_yaml(node: Any) -> bool:
    """Whether serde_yaml could hold this whole tree in a `Value` -- an integer
    bound only.

    A non-finite FLOAT is deliberately representable here and must stay that
    way (tan-cli#387). `.inf` / `-.inf` / `.nan` are perfectly good
    `serde_yaml::Value::Number`s; returning False for them would make
    `raw_passthrough` yield its empty defaults and drop the WHOLE `hw_info` and
    `boot_order` on the floor, where the oracle emits every key with the
    non-finite ones as `null`. The JSON side of that problem is fixed where
    JSON is written (`tan.envelope.json_safe_floats`), not by refusing the
    document.
    """
    if isinstance(node, bool):
        return True
    if isinstance(node, int):
        return _SERDE_INT_MIN <= node <= _SERDE_INT_MAX
    if isinstance(node, dict):
        return all(
            _representable_by_serde_yaml(k) and _representable_by_serde_yaml(v)
            for k, v in node.items()
        )
    if isinstance(node, (list, tuple)):
        return all(_representable_by_serde_yaml(v) for v in node)
    return True


def raw_passthrough(text: str) -> tuple[dict, list]:
    """The manifest's `hw_info` (default `{}`) + `boot_order` (default `[]`),
    straight off the YAML -- bypassing any typed projection so additive schema
    keys (`hw_info.eeprom`, an rpmsg carve-out, anything a newer SDK adds) pass
    through unchanged into the bundle manifest.

    Never fails: unparseable or non-mapping input yields the empty defaults,
    matching `image_bundle::raw_passthrough`. That is what makes it safe to call
    on the error path with an empty string.
    """
    try:
        root = load_yaml_document(text)
    except SystemManifestError:
        root = None
    if not isinstance(root, dict) or not _representable_by_serde_yaml(root):
        return {}, []
    hw_info = root.get("hw_info")
    boot_order = root.get("boot_order")
    return (
        hw_info if isinstance(hw_info, dict) else {},
        list(boot_order) if isinstance(boot_order, list) else [],
    )


# --------------------------------------------------------------------------
# The two path rules `tan image` and `tan size` share.
# --------------------------------------------------------------------------
#
# Paths here are STRINGS joined with `os.path.join`, not `pathlib.Path`.
# `Path("C:/a/b") / "c"` re-renders the whole path in the platform separator,
# and Rust's `PathBuf::join` does not: it appends and leaves what was already
# there alone. Both values end up in `data.slices[].notes[]` and in issue
# messages, so the difference is visible in the machine contract -- a
# `/`-authored workspace root keeps its slashes with a `\` tail, exactly as the
# shipped binary reports it.


def anchor_under(path: str, build_root: str) -> str:
    """A manifest path string as an absolute path: absolute as-is, relative
    under `build_root` (where the manifest itself lives). Verbatim
    `size.rs::anchor_under` / `image.rs`'s inline twin."""
    return path if os.path.isabs(path) else os.path.join(build_root, path)


def slice_build_dir(slice_: dict, build_root: str) -> str | None:
    """A slice's recorded build directory, or `None` when the manifest declares
    none (a plan-time manifest, before any build ran).

    `tan image` treats `None` as "skip this slice"; `tan size` falls back to the
    canonical name via [`slice_build_dir_or_default`]. Both anchor the same way,
    which is why the rule lives here once.
    """
    raw = slice_.get("build_dir")
    if not isinstance(raw, str) or raw == "":
        return None
    return anchor_under(raw, build_root)


def slice_build_dir_or_default(slice_: dict, build_root: str) -> str:
    """[`slice_build_dir`], falling back to the canonical `<core_id>-<os>`."""
    recorded = slice_build_dir(slice_, build_root)
    if recorded is not None:
        return recorded
    return os.path.join(build_root, f"{slice_.get('core_id')}-{slice_.get('os')}")


def _nested_variants(base: str, *, include_nested: bool = True) -> list[str]:
    """`base`, then `base/build` -- the I-18 pair. `west build` is emitted with
    NO `-d`, so its tree lands at `<slice-cwd>/build/` while the plan's
    `artifacts` block still names `<slice-cwd>/...`; the consumer reconciles
    that off-by-one directory.

    The un-nested path is tried FIRST, always: for every input where the oracle
    finds something, this finds the identical thing, and the nested probe only
    ever converts an oracle `not-built` into a real measurement. See
    `resolve_zephyr_artefact` (`crates/tan-cli/src/commands/build/execute/
    manifest.rs`), which is where the shipped binary resolves this -- at BUILD
    time, by recording the nested paths into the manifest it rewrites. This
    port's `tan build` does not yet write that manifest, so the reconciliation
    has to happen on the read side or every slice of a real nested build reports
    `not-built`.

    `include_nested=False` (tan-cli#499) drops the `/build` candidate entirely
    -- reserved for a slice whose recorded `status` is NOT `"ok"` and carries
    no `output_artefact`: with no output_artefact, the only reason a file
    would sit at the NESTED path is a build this manifest itself says did not
    (or no longer) succeed. Widening the search there converts an oracle
    `not-built` into a stale measurement reported as current -- the un-nested
    candidate stays in either way, since the oracle's own base-path lookup is
    unaffected by this gate and this is strictly a narrowing, never a new
    match the oracle would refuse.
    """
    if not include_nested:
        return [base]
    return [base, os.path.join(base, WEST_NESTED_DIR)]


def _nested_probe_ok(slice_: dict) -> bool:
    """tan-cli#499: whether the I-18 nested `/build` probe should run for
    `slice_` at all, when no `output_artefact` is recorded.

    `True` for an ABSENT `status` (a plan-time manifest, before any build
    ran -- the case [`_nested_variants`]'s own docstring exists for: a real
    `west build` tree sitting on disk with nothing yet written back) AND for
    `"ok"` (the ordinary just-built case). `False` for any OTHER explicit
    status (`failed`/`skipped`/`pending`/anything else a future SDK adds):
    a manifest that names the outcome of a real run and says it was not `ok`
    is exactly the case that must not have its own leftover artefact from a
    DIFFERENT, earlier run picked back up one directory deeper and reported
    as current.
    """
    status = slice_.get("status")
    return status is None or status == "ok"


def slice_elf_candidates(slice_: dict, build_root: str) -> list[str]:
    """Where this slice's `zephyr.elf` may be, best first.

    A recorded `output_artefact` is authoritative and used ALONE -- `tan build`
    only ever writes it already-resolved, so second-guessing it would ignore a
    deliberate `-d`/`--build-dir` override.

    tan-cli#499: with no recorded `output_artefact`, the I-18 nested `/build`
    probe is gated by [`_nested_probe_ok`]. A slice with an explicit non-`ok`
    status can still be measured off the UN-nested path (unchanged,
    oracle-matching); it just cannot pick up a leftover artefact that only
    exists one directory deeper.
    """
    artefact = slice_.get("output_artefact")
    if isinstance(artefact, str) and artefact != "":
        return [anchor_under(artefact, build_root)]
    build_dir = slice_build_dir_or_default(slice_, build_root)
    return [
        os.path.join(base, "zephyr", "zephyr.elf")
        for base in _nested_variants(build_dir, include_nested=_nested_probe_ok(slice_))
    ]


def slice_footprint_dirs(slice_: dict, build_root: str) -> list[str]:
    """Where this slice's `rom.json` + `ram.json` may be, best first (same I-18
    pair as [`slice_elf_candidates`], including the same [`_nested_probe_ok`]
    gate, tan-cli#499)."""
    return _nested_variants(
        slice_build_dir_or_default(slice_, build_root),
        include_nested=_nested_probe_ok(slice_),
    )
