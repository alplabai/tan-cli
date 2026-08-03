# SPDX-License-Identifier: Apache-2.0
"""`tan presets` -- what the New Project wizard is allowed to offer: the SoM
presets discovered in the resolved alp-sdk checkout, plus tan's own built-in
defaults.

Mirrors `crates/tan-cli/src/commands/presets.rs` and the three `tan-core`
helpers it composes (`presets::empty_preset_catalogue`,
`sdk_catalogue::parse_som_preset`, `bootstrap::runtime_for_topology_core`). One
module, deliberately: the whole command is two directory reads, a small YAML
read, and four constant lists.

**The customer never chooses an OS.** `data.soms[].cores[].os` is DERIVED, per
core, from the SoM's own topology in the SDK metadata -- a topology entry with a
`board:` is a Zephyr (Cortex-M) target, one with a `machine:` is a Yocto
(Cortex-A) one, and failing both the core-id class decides (`a<digit>` at a word
start -> yocto, else zephyr). It is a REPORT of what the silicon runs, which is
why there is no `--os` and no `--backend` flag here and why `osChoices` is a
built-in vocabulary list rather than a menu bound to any SoM. The heterogeneous
golden (`a55` -> yocto next to `m33` -> zephyr) is the case that proves it: both
values come back on one SoM, so a consumer cannot read the field as a choice.

**Which rule decides where the presets come from.** SoM presets are alp-sdk
content, so the two candidate readings are "vendor a capture like `tan init`
does" and "read the resolved checkout". This reads the checkout, on two
invariants from
`alp-sdk/docs/superpowers/specs/2026-07-29-tan-port-invariants.md`:

* **I-32** bans *shelling* the SDK -- `tan init`/`tan scaffold` read a vendored
  `--emit scaffold` tree "without ever shelling the SDK" because they must work
  with NO checkout present. Nothing here spawns a subprocess (there is no
  subprocess to time out); a `scandir` over `<sdk>/metadata/e1m_modules` is not a
  shell-out, so I-32 is untouched. Anti-pattern #22 in that doc is *shelling
  `init`* instead of reading the vendored tree -- not "reading a checkout".
* **I-26** ("tan must never learn a hardware fact ... `metadata/**` stays in
  alp-sdk") forbids the vendored alternative HERE, and forbids it hardest: a
  baked SKU list is precisely the hardware fact tan may not hold. Every SKU,
  family, core id and runtime below is read from the checkout at runtime; this
  file names no SKU, no address, no pin and no vendor. With no checkout
  resolvable the SDK-derived lists are legitimately EMPTY plus a warning, which
  is what the oracle does -- so it has no reason to carry a capture.

**An unresolved SDK is exit 0 with a warning, not a failure.** The built-in
defaults are still useful without a checkout, and `presets.sdk-root-unresolved`
is a FROZEN issue code (`contract/issue-codes.json`): the wizard matches it with
`===` to know its SoM list is empty for an environmental reason. Promote it to an
error and the wizard falls back to a static catalogue that carries no `cores`,
which scaffolds a heterogeneous SoM single-core with no IPC.

**Every failure is an envelope, never a traceback.** Both directory reads and
every file read swallow their IO errors exactly as the Rust's `read_dir(..)` /
`read_to_string(..).ok()` chains do -- a missing tree, an unreadable entry, a
non-UTF-8 byte or a wrong-shaped SoM is a skipped entry, never an abort -- and
the catch-all in `presets` is the backstop for whatever nobody enumerated.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from tan.commands.sdk_cmd import SDK_MARKER, project_pin_issue, resolve_sdk_tiered
from tan.core.global_flags import accept_global_flags
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

#: The one `schema_version:` a SoM preset may declare. `som-preset-v1.schema.json`
#: pins it `const: 1`, and `parse_som_preset` refuses anything else rather than
#: guessing at a shape it does not know -- so a v2 SoM is SKIPPED here, silently,
#: exactly as the oracle skips it (`parse_som_preset(..).ok()?`).
SOM_SCHEMA_VERSION = 1

#: The FROZEN issue code for "no checkout resolved" (`contract/issue-codes.json`,
#: severity `warning`). Spelled once; the extension matches it with `===`.
SDK_UNRESOLVED_CODE = "presets.sdk-root-unresolved"
SDK_UNRESOLVED_MESSAGE = (
    "alp-sdk root is unresolved. Returning built-in defaults and empty SDK preset lists."
)

#: Built-in, SDK-independent defaults, verbatim from
#: `tan_core::presets::empty_preset_catalogue`. Order is the wire order (these are
#: lists, not sets, in the envelope) and the golden pins it.
#:
#: `LIBRARIES` is the per-core `cores.<id>.libraries` token set; the ADR-0018
#: curated `boardLibraries` a top-level `libraries:` entry names are discovered
#: from the checkout instead -- two different vocabularies, which is why the
#: envelope carries both.
LIBRARIES = ["etl", "fmt", "nlohmann_json", "doctest", "lvgl", "mbedtls", "cmsis_dsp", "littlefs"]
INFERENCE_BACKENDS = ["auto", "cpu", "ethos_u", "drpai", "deepx_dxm1"]
LOG_LEVELS = ["error", "warn", "info", "debug", "trace"]
#: The OS VOCABULARY, not a picker: `cores[].os` is derived per core (see the
#: module docstring). Nothing may bind this list to a SoM or to a `--os` flag.
OS_CHOICES = ["zephyr", "yocto", "baremetal"]

#: SoM preset directory names/files this command considers, from `read_soms`.
SOM_PREFIX = "E1M-"
SOM_DIR_YAML = "som.yaml"

#: `board`/`machine` as a key of a FLOW mapping (`m33: { board: x }`), for the
#: no-PyYAML fallback. Anchored on a brace, comma or whitespace so `zephyr_board:`
#: is not mistaken for `board:`.
_FLOW_KEY_RE = re.compile(r"[{,\s](board|machine)\s*:")


@dataclass(frozen=True)
class SomCore:
    """One core of a SoM topology: its id and the runtime that core's class
    runs. Field order is the emitted JSON order (`SomCoreEntry` in
    `presets.rs`)."""

    id: str
    os: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "os": self.os}


@dataclass(frozen=True)
class Som:
    """One discovered SoM preset. Field order matches `SomEntry`."""

    sku: str
    display_name: str
    family: str
    cores: tuple[SomCore, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "sku": self.sku,
            "displayName": self.display_name,
            "family": self.family,
            "cores": [c.as_dict() for c in self.cores],
        }


class SomShapeError(Exception):
    """The text is not a v1 SoM preset. Caller SKIPS the entry, as the oracle's
    `parse_som_preset(..).ok()?` does -- one malformed SoM must not empty the
    catalogue or fail the command."""


# ---------------------------------------------------------------------------
# The derived runtime (tan_core::bootstrap::runtime_for_topology_core)
# ---------------------------------------------------------------------------


def infer_runtime_for_core_id(core_id: str) -> str:
    """`a` followed by a digit at a WORD start -> `yocto`, else `zephyr`
    (`tan_core::wizard::infer_runtime_for_core_id`).

    Word starts are the string start and any position after `_`/`-`, so
    `a55_cluster` and `cluster_a55` are both Cortex-A while `data55` is not. The
    heuristic is a last resort: a topology that declares `board:`/`machine:`
    never reaches it.
    """
    lowered = core_id.lower()
    word_start = True
    for i, ch in enumerate(lowered):
        if word_start and ch == "a" and i + 1 < len(lowered) and lowered[i + 1].isdigit():
            return "yocto"
        word_start = ch in "_-"
    return "zephyr"


def runtime_for_core(core_id: str, *, board: bool, machine: bool) -> str:
    """The runtime one topology entry naturally runs: `board:` -> zephyr,
    `machine:` -> yocto, else the core-id heuristic.

    `board` wins when both are present, matching the oracle's match arms. ONE
    owner of this mapping in the Rust (`tan presets` reports it, `tan bootstrap`
    gates on it) -- so when `bootstrap` lands in this port it must call THIS, not
    a copy, or the two can disagree about which host can build the project.
    """
    if board:
        return "zephyr"
    if machine:
        return "yocto"
    return infer_runtime_for_core_id(core_id)


# ---------------------------------------------------------------------------
# SoM preset reading (tan_core::sdk_catalogue::parse_som_preset, narrowed)
# ---------------------------------------------------------------------------


def _clean(value: object) -> str | None:
    """`str_clean`: a YAML scalar as a string, dropping the placeholder `TBD`.

    Non-strings are `None` (serde's `as_str()`), and the value is NOT trimmed --
    only the `TBD` comparison is -- because the oracle emits the scalar as
    written.
    """
    if not isinstance(value, str):
        return None
    return None if value.strip() == "TBD" else value


def parse_som_preset(text: str) -> Som:
    """The `sku`/`display_name`/`family`/`topology` slice of a SoM preset.

    Narrower than the oracle's `parse_som_preset` on purpose: the envelope
    carries exactly these four things, and reading `pad_routes`,
    `i2c_devices` or `capabilities` here would be tan learning hardware facts it
    has no field to report (I-26).

    `schema_version` is guarded FIRST, before any other key is trusted -- the
    same skew guard every other SDK-file consumer carries. Raises
    `SomShapeError` for every unusable shape; the caller skips the entry.
    """
    doc = _load_som_yaml(text)
    version = doc.get("schema_version")
    # `bool` is excluded explicitly: Python's `True == 1` is true and `isinstance(
    # True, int)` is too, so `schema_version: true` would pass the guard, where
    # serde's `as_i64()` on a bool is `None` and the oracle rejects the file.
    if not isinstance(version, int) or isinstance(version, bool) or version != SOM_SCHEMA_VERSION:
        raise SomShapeError(f"unsupported schema_version {version!r} (this CLI consumes v1)")
    sku = _clean(doc.get("sku")) or ""
    return Som(
        sku=sku,
        # `display_name` falls back to `sku`, then to `""`.
        display_name=_clean(doc.get("display_name")) or sku,
        family=_clean(doc.get("family")) or "",
        # `.get`, not `[..]`: the guard above happens to reject every shape that
        # reaches here without a `cores` key, but a KeyError is the one failure
        # this command may not have, so it does not lean on that.
        cores=tuple(doc.get("cores") or ()),
    )


def _load_som_yaml(text: str) -> dict[str, object]:
    """`{schema_version, sku, display_name, family, cores}` from a SoM preset.

    PyYAML when it is importable, else `scan_som_preset`. tan's runtime
    dependencies are `typer` + `rich` only -- and the frozen binary is built from
    a venv holding exactly those (`scripts/build_binary.sh`), so on the SHIPPED
    artifact the fallback is not a rare path, it is THE path. It therefore may
    not be a degraded answer: `cores` missing or empty is the one thing this
    command must never quietly report, because the wizard scaffolds IPC off it
    and a heterogeneous SoM would come out single-core.

    `cores` is already a `list[SomCore]` here rather than raw topology, because
    the two readers see topology differently (a parsed mapping vs. indented
    lines) and the runtime derivation must happen once, in `runtime_for_core`.
    """
    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError:
        return scan_som_preset(text)
    try:
        doc = yaml.safe_load(text)
    except Exception as err:  # noqa: BLE001 -- yaml.YAMLError and anything a loader raises
        raise SomShapeError(f"could not be parsed as YAML: {err}") from err
    if not isinstance(doc, dict):
        # `root.as_mapping().unwrap_or_default()` -> an empty map -> the
        # schema_version guard rejects it. Same outcome, stated here.
        return {}
    topology = doc.get("topology")
    cores: list[SomCore] = []
    if isinstance(topology, dict):
        for core_id, node in topology.items():
            if not isinstance(core_id, str):
                continue  # `id.as_str()?` -- a non-string key is not a core
            entry = node if isinstance(node, dict) else {}
            cores.append(
                SomCore(
                    core_id,
                    runtime_for_core(
                        core_id,
                        board=_clean(entry.get("board")) is not None,
                        machine=_clean(entry.get("machine")) is not None,
                    ),
                )
            )
    return {
        "schema_version": doc.get("schema_version"),
        "sku": doc.get("sku"),
        "display_name": doc.get("display_name"),
        "family": doc.get("family"),
        "cores": cores,
    }


def scan_som_preset(text: str) -> dict[str, object]:
    """The no-PyYAML reader: the four top-level scalars plus the `topology:`
    block's core ids and whether each declares `board:`/`machine:`.

    Deliberately NOT a YAML parser -- it answers only what the envelope carries,
    the same bargain `validate_cmd._load_yaml` and `generate_cmd._scan_som_sku`
    strike. What it must get right on the real files is the topology, which in
    every shipped SoM preset is a plain two-space block mapping with interleaved
    comments; the flow form (`m33: { board: x }`) is handled too because the
    oracle's own unit tests use it.

    Ponytail note: line-oriented, so a block scalar or an anchor in a SoM preset
    would read wrong. No shipped preset has either; PyYAML covers them wherever
    it is installed, and the upgrade path if one appears is to vendor a real
    parser, not to widen this.
    """
    out: dict[str, object] = {}
    cores: list[SomCore] = []
    # `None` until `topology:` opens; then the indent its core ids sit at.
    topology_indent: int | None = None
    in_topology = False
    # The core id being read, and which of `board:`/`machine:` it has declared.
    open_core: list = [None, False, False]

    def close_core() -> None:
        core_id, board, machine = open_core
        if core_id is not None:
            cores.append(
                SomCore(core_id, runtime_for_core(core_id, board=board, machine=machine))
            )
        open_core[:] = [None, False, False]

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        key, sep, value = stripped.partition(":")
        key = key.strip()

        if indent == 0:
            close_core()
            in_topology = bool(sep) and key == "topology"
            topology_indent = None
            if not sep:
                continue
            if key == "schema_version":
                out["schema_version"] = _scalar_int(value)
            elif key in ("sku", "display_name", "family"):
                out[key] = _scalar(value)
            continue

        if not in_topology or not sep:
            continue
        if topology_indent is None:
            topology_indent = indent
        if indent <= topology_indent:
            # A core id. Its value may be an inline flow mapping carrying the
            # very keys the block form puts on the following lines.
            close_core()
            open_core[0] = key
            for flow_key in _FLOW_KEY_RE.findall(f" {value}"):
                open_core[1 if flow_key == "board" else 2] = True
        elif open_core[0] is not None:
            # A key of the current core. `_scalar` so an empty or `TBD` value
            # does not count as declared -- `str_clean` drops both.
            if key == "board" and _scalar(value) is not None:
                open_core[1] = True
            elif key == "machine" and _scalar(value) is not None:
                open_core[2] = True
    close_core()
    out["cores"] = cores
    return out


def _scalar(value: str) -> str | None:
    """A block-mapping scalar: trailing ` #comment` dropped, surrounding quotes
    stripped, `TBD` and empty treated as absent (`str_clean`)."""
    text = value
    hashed = text.find(" #")
    if hashed != -1:
        text = text[:hashed]
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return _clean(text) or None


def _scalar_int(value: str) -> int | None:
    """A BARE integer scalar, or `None`. Quotes are not stripped first, so
    `schema_version: "1"` is `None` here exactly as serde's `as_i64()` on a
    string is -- a quoted version is not a v1 preset."""
    text = value.split(" #", 1)[0].strip()
    try:
        return int(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _entries(directory: Path) -> list[os.DirEntry]:
    """`directory`'s entries, or `[]` when it will not read -- `read_dir(..)`'s
    `else { return Vec::new() }`. A missing `metadata/` tree is an empty
    catalogue, not an error."""
    try:
        with os.scandir(directory) as it:
            return list(it)
    except OSError:
        return []


def read_soms(sdk_root: str) -> list[Som]:
    """SoM presets under `<sdk>/metadata/e1m_modules`, sorted by SKU.

    Both layouts the SDK has used are supported, as in the oracle: a flat
    `E1M-X.yaml`, or an `E1M-X/som.yaml` directory. An entry that is not
    `E1M-*`, carries no yaml, will not read, is not UTF-8, or is not a v1 preset
    is skipped -- never an error.
    """
    root = Path(sdk_root) / "metadata" / "e1m_modules"
    found: list[Som] = []
    for entry in _entries(root):
        if not entry.name.startswith(SOM_PREFIX):
            continue
        path = Path(entry.path)
        if _is_dir(entry):
            yaml_path = path / SOM_DIR_YAML
        elif entry.name.endswith(".yaml"):
            yaml_path = path
        else:
            continue
        try:
            text = yaml_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            # `read_to_string(..).ok()?`. `encoding="utf-8"` explicitly (I-27):
            # the default decodes with the host locale, and the shipped presets
            # carry non-ASCII prose, so a bare read raises on a cp1252 Windows
            # host and passes on ubuntu CI.
            continue
        try:
            found.append(parse_som_preset(text))
        except SomShapeError:
            continue
    found.sort(key=lambda s: s.sku)
    return found


def _is_dir(entry: os.DirEntry) -> bool:
    """`entry.path.is_dir()` -- symlinks FOLLOWED, matching the oracle's
    `Path::is_dir()` (not the non-following `DirEntry::file_type()` that
    `examples` mirrors). `OSError` -> not a directory."""
    try:
        return entry.is_dir()
    except OSError:
        return False


def read_board_libraries(sdk_root: str) -> list[str]:
    """The ADR-0018 curated library names: the stem of each
    `<sdk>/metadata/libraries/<name>.yaml`, sorted and de-duplicated.

    `README*` is skipped case-insensitively; a non-yaml entry is skipped. These
    are the values a board.yaml TOP-LEVEL `libraries:` entry names -- a different
    vocabulary from the built-in per-core `LIBRARIES` tokens.
    """
    root = Path(sdk_root) / "metadata" / "libraries"
    names: set[str] = set()
    for entry in _entries(root):
        name = entry.name
        if not name.endswith(".yaml") or name.lower().startswith("readme"):
            continue
        # `trim_end_matches(".yaml")` strips REPEATED suffixes, so `a.yaml.yaml`
        # is `a`; `removesuffix` would leave `a.yaml`.
        while name.endswith(".yaml"):
            name = name[: -len(".yaml")]
        names.add(name)
    return sorted(names)


# ---------------------------------------------------------------------------
# Project/SDK resolution (util.rs::resolve_cli_project_context)
# ---------------------------------------------------------------------------


def _posix(value: str) -> str:
    """`to_posix`: backslashes to forward slashes, nothing else.

    A string, never a `Path` round-trip: `str(Path("./sdk"))` is `sdk`, dropping
    the `./` the user typed, while Rust's `Path::new("./sdk").to_string_lossy()`
    keeps it -- and `./sdk` is what the golden pins for both `sdk.root` and
    `data.sdkRoot`.
    """
    return value.replace("\\", "/")


def resolve_project_paths(project: str | None, board_yaml: str | None) -> tuple[str, str]:
    """`(project.root, project.boardYaml)` as the envelope reports them: the
    normalised absolute workspace root, and `board.yaml` under it.

    Normalised (`.` dropped, `..` popped) like `normalize_path`, because the root
    is joined from the CWD and `--project`. The board path is then joined WITHOUT
    normalising, matching `resolve_board_yaml_path` -- `--board-yaml ../x.yaml`
    reports `<root>/../x.yaml` on both implementations.
    """
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = "."  # `current_dir().unwrap_or_else(|_| PathBuf::from("."))`
    root = _posix(os.path.normpath(os.path.join(cwd, project or ".")))
    leaf = board_yaml or "board.yaml"
    if os.path.isabs(leaf):
        return root, _posix(leaf)
    # No separator when the root already ends in one: at a drive root (`C:/`)
    # a blind join would emit `C://board.yaml`.
    sep = "" if root.endswith(("/", "\\")) else "/"
    return root, f"{root}{sep}{_posix(leaf)}"


def resolve_sdk(
    sdk_root: str | None, workspace_root: str
) -> tuple[str, str, str | None] | None:
    """`(root, sourceTier, brokenProjectPin)` for the resolved checkout, or
    `None`.

    `resolve_sdk_tiered` walks the four tiers (`--sdk-root` > project pin >
    global default > discovery) and is TERMINAL on `--sdk-root` (I-31): a typo'd
    flag never falls through to a lower tier and silently reports a different
    SDK. The loader-marker check here is what core's `resolve_project_context`
    re-applies on top of that -- so an invalid `--sdk-root` resolves to nothing
    (empty lists + the frozen warning), which is the oracle's behaviour and NOT
    the same as "resolved something else".

    `brokenProjectPin` (tan-cli#263 review) is `active.broken_project_pin`,
    carried through even when this call returns `None` overall -- both callers
    (`presets`, `clean_cmd.resolve_sdk`) surface it as `sdk.project-pin-
    unresolved` alongside whatever else they already report.
    """
    active = resolve_sdk_tiered(sdk_root, Path(workspace_root))
    if active.path is None or not Path(active.path).joinpath(*SDK_MARKER).exists():
        return None
    return _posix(active.path), active.tier, active.broken_project_pin


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def render_presets_text(skus: list[str], board_libraries: list[str], verbose: bool) -> list[str]:
    """The count line, plus one `sku: <id>` line per SKU under `--verbose`.
    Verbatim shape from `presets_text`."""
    lines = [
        f"presets: skus={len(skus)} libraries={len(LIBRARIES)} "
        f"boardLibraries={len(board_libraries)}"
    ]
    if verbose:
        lines.extend(f"sku: {sku}" for sku in skus)
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def presets(
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."),
    verbose: bool = typer.Option(False, "--verbose", help="List each discovered SKU."),
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """List the SDK's SoM presets plus tan's built-in defaults."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    root, board_path = ".", "./board.yaml"
    sdk: tuple[str, str, str | None] | None = None
    soms: list[Som] = []
    board_libraries: list[str] = []
    issues: list[Issue] = []
    exit_code = ExitCode.SUCCESS
    try:
        root, board_path = resolve_project_paths(project, board_yaml)
        sdk = resolve_sdk(sdk_root, root)
        if sdk is not None:
            soms = read_soms(sdk[0])
            board_libraries = read_board_libraries(sdk[0])
            pin_issue = project_pin_issue(sdk[2], sdk[1])
            if pin_issue is not None:
                issues = [pin_issue]
        else:
            issues = [Issue(SDK_UNRESOLVED_CODE, "warning", SDK_UNRESOLVED_MESSAGE)]
    except Exception as err:  # noqa: BLE001 -- the backstop; see the module docstring
        # No registry entry applies (`contract/issue-codes.json` covers
        # `bootstrap.*`/`debug-config.*` only), so this follows the port's
        # `<command>.internal-failure` spelling. Unreachable through any
        # enumerated path -- every IO and parse error above is already swallowed
        # the way the oracle swallows it -- and here so the one nobody thought of
        # arrives as an envelope instead of a traceback on stderr with an empty
        # stdout the extension renders as nothing.
        sdk, soms, board_libraries = None, [], []
        issues = [
            Issue(
                "presets.internal-failure",
                "error",
                f"presets failed unexpectedly: {err.__class__.__name__}: {err}",
            )
        ]
        exit_code = ExitCode.INTERNAL_FAILURE

    skus = [s.sku for s in soms]
    if json_mode:
        emit(
            Envelope(
                "presets",
                Project.resolved(root, board_path),
                {
                    "schemaVersion": DATA_SCHEMA_VERSION,
                    # `null`, not omitted, when unresolved: the wizard reads this
                    # key directly and the golden pins the null.
                    "sdkRoot": sdk[0] if sdk is not None else None,
                    "skus": skus,
                    "soms": [s.as_dict() for s in soms],
                    "libraries": LIBRARIES,
                    "boardLibraries": board_libraries,
                    "inferenceBackends": INFERENCE_BACKENDS,
                    "logLevels": LOG_LEVELS,
                    "osChoices": OS_CHOICES,
                },
                issues,
                exit_code,
                sdk=SdkInfo(sdk[0], sdk[1]) if sdk is not None else None,
            )
        )
    else:
        for line in render_presets_text(skus, board_libraries, verbose):
            print(line, file=sys.stderr)
    raise typer.Exit(int(exit_code))


# tan-cli#261: adds the six oracle `GlobalArgs` flags this command was still
# missing (`--all`/`--ci`/`--no-color`/`--non-interactive`/`--quiet`/
# `--target`) on top of `--board-yaml`/`--verbose`, already declared and read
# above; see `tan.core.global_flags`.
presets = accept_global_flags(presets)
