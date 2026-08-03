# SPDX-License-Identifier: Apache-2.0
"""What files a `tan init` template lays down, and the disk diff/write around it.

Pure domain plus reads of tan's OWN template data. Mirrors
`crates/tan-core/src/wizard/` (`service/c_project.rs`, `service/vendored.rs`,
`service/example_catalog.rs`, `filesystem.rs`); the command shell that turns
these results into an envelope is `tan.commands.init_cmd`.

**No SDK checkout is consulted anywhere in here (I-32).** Five of the six
templates render a byte-for-byte capture of the SDK's own `--emit scaffold`
output, checked in under `tan/templates/vendored/` (see that package's
docstring); the sixth, `minimal-app`, is tan's OWN hand-generated stub and has
no SDK catalog entry at all. So a fresh customer with no alp-sdk anywhere gets
a real project, and -- the reason the contract fixture can exist -- `tan init
--template minimal-app --preview` is deterministic in an empty temp directory.
`--from-example` is the one init path that genuinely needs a checkout, because
it copies a directory out of one; it reports `init.sdk-root-unresolved`.

Deliberately NOT reimplemented here: the SDK's own
`scripts/alp_template.py::_scaffold_cmakelists()` regex rewrite of a scaffolded
`CMakeLists.txt`. That rewrite matches the *current* boilerplate text and
returns its input unchanged when it does not recognise the shape, so a second
implementation of it in tan is a silent `ALP_SDK_ROOT`-guess bug waiting for
the next SDK CMake change. It ran ONCE, at vendor time, and its output is what
`vendored/` holds.

**Hardware facts (the one documented exception).** `app_core_for_sku` maps a SKU
prefix to a core id, and `_family_bucket` maps one to a vendored tree. Both are
ported verbatim from the Rust, both are the *only* hardware knowledge in the
port, and both exist because a scaffold has to name a core before any SDK is
reachable -- `tan validate` re-checks the guess once one is. Do not grow this:
no SKU list, no addresses, no pin names. Note what is NOT here as a result: the
OS is never selected (I-01/I-02) -- every generated core entry is `os: zephyr`
because the app source this scaffold writes is Zephyr source, and a scaffolded
`board.yaml` carries no top-level `os:` key at all.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from tan.core.fs_confine import PathEscapeError, resolve_confined
from tan.core.timestamp import generated_at_iso
from tan.templates import VENDORED_ROOT

#: Template ids, in registry order (`WizardTemplateId::as_str`). Wire contract:
#: `data.templateId` echoes one of these verbatim.
TEMPLATE_IDS = (
    "minimal-app",
    "zephyr-app",
    "sensor-starter",
    "iot-starter",
    "edge-ai-starter",
    "board-diagnostics",
)

#: The template a non-interactive `tan init` with no `--template` gets.
#: `zephyr-app`, NOT `minimal-app` (tan-cli #97). Until tan-cli#309, TWO bugs
#: compounded: `board.yaml`'s `app: ./src` sent the planner's `_zephyr_app_dir`
#: straight at `src/CMakeLists.txt` (it has a `CMakeLists.txt` of its own, so
#: the parent-fallback that would have reached the real one never fired), and
#: THAT file called plain `add_executable(alp_app ...)` with no `find_package(
#: Zephyr ...)` at all -- so `west build -b <board> <project>/src` configured
#: and linked a genuine x86-64 host binary, silently, for a core declared
#: `os: zephyr`; the root `CMakeLists.txt` (dead code the whole time) was never
#: even the file at fault. tan-cli#309 fixed both: the generator itself
#: (`_minimal_app_root_cmake`/`_minimal_app_src_cmake` below) and `board.yaml`'s
#: `app:` (`_minimal_app_board_yaml`). zephyr-app stays the default regardless
#: -- that is a separate, still-live product choice (vendored from a real SDK
#: catalog entry vs. tan's own hand-generated stub), not something this fix
#: revisits. Do not "simplify" this back to the first registry entry.
DEFAULT_TEMPLATE_ID = "zephyr-app"

#: tan template id -> its vendored SDK scaffold-catalog directory.
#: `minimal-app` is absent deliberately: it is the one template with no SDK
#: catalog entry (`vendored/MANIFEST.md`, "minimal-app stays hand-generated").
_VENDORED_TEMPLATE_DIR = {
    "zephyr-app": "minimal",
    "sensor-starter": "sensor",
    "iot-starter": "iot",
    "edge-ai-starter": "edge-ai",
    "board-diagnostics": "diagnostics",
}

#: The SKU assumed when `--som` is absent (`tan_core::DEFAULT_SOM_SKU`).
DEFAULT_SOM_SKU = "E1M-AEN801"

#: `iot-starter`'s only supported SKU. Its Wi-Fi transport is the CC3501E
#: bridge, silicon-validated on this SKU alone, so the SDK catalog's `iot`
#: entry gates `supported.som_skus` to exactly this one-item set and only ONE
#: family tree was vendored. Any other `--som` is refused up front rather than
#: silently rendered against it.
IOT_STARTER_SUPPORTED_SKU = "E1M-AEN801"

#: Vendored directory name per SoM family, `(alif_ensemble, renesas_v2n)` --
#: the two representative SKUs the SDK catalog declares.
_FAMILY_TREES = ("E1M-AEN801", "E1M-V2N101")


def _read_verbatim(path: Path) -> str:
    """Read `path` as UTF-8 text with newlines UNTRANSLATED.

    Every scaffold byte round-trips through here. Universal-newline mode would
    fold a CRLF file's `\\r\\n` to `\\n` on read and expand it back on write, so on
    Windows an LF template would be written CRLF (breaking byte-parity with the
    Rust binary's output and with the vendored LF capture) and an existing CRLF
    file would compare EQUAL to LF planned content -- a real on-disk difference
    reported as `unchanged`.

    `open(newline="")`, not `Path.read_text(newline=...)`: that keyword landed in
    3.13 and this package's floor is 3.12.
    """
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_verbatim(path: Path, content: str) -> None:
    """The write half of [`_read_verbatim`] -- same reasoning, same keyword."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


class TemplateDataError(Exception):
    """tan's own vendored template tree is missing or unreadable.

    Not a user error and not reachable from a correct build: the tree ships
    inside the package. It IS reachable from a frozen binary built without the
    `--add-data` that carries it, which is why this is an exception with its own
    issue code instead of an `IOError` escaping as a traceback.
    """


class ExampleReadError(Exception):
    """`--from-example`'s source directory is missing, or a file in it is not
    readable UTF-8 text. `not_found` separates the two: a missing example is
    the user's typo (exit 2), an unreadable one is a runtime failure (exit 1)."""

    def __init__(self, message: str, *, not_found: bool) -> None:
        super().__init__(message)
        self.not_found = not_found


@dataclass(frozen=True)
class PlannedFile:
    """One file the template will lay down: its project-relative, forward-slash
    path and its exact content."""

    relative_path: str
    content: str


@dataclass(frozen=True)
class FileChange:
    #: `new` | `update` | `unchanged` -- `WizardFileChangeKind::as_str`, wire
    #: contract via `data.fileChanges[].kind`.
    relative_path: str
    kind: str


@dataclass(frozen=True)
class WriteResult:
    written: list[str]
    unchanged: list[str]


class ScaffoldWriteError(Exception):
    """A write failed part-way. Carries the `partial` result accumulated before
    the failure: reporting `written: []` for a project that really is half on
    disk leaves a consumer with no idea what to clean up or reopen."""

    def __init__(self, message: str, partial: WriteResult) -> None:
        super().__init__(message)
        self.partial = partial


# ---------------------------------------------------------------------------
# Path guard
# ---------------------------------------------------------------------------


def is_plain_relative(raw: str) -> bool:
    """True when `raw` is a plain relative path: non-empty, no `.`/`..`, not
    absolute, not drive-rooted or root-relative.

    `tan_core::is_plain_relative`. Guards the two inputs that decide WHERE
    files come from and go to: `--name` (joined onto the destination, so an
    unchecked `..` or absolute value put the project root -- and with `--force`
    an overwrite target -- anywhere the process can write) and `--from-example`
    (joined onto the SDK `examples/` root).

    Evaluated with WINDOWS path semantics on every platform: `C:foo` and `\\x`
    are not `is_absolute()` on Windows yet still escape a join, and being
    stricter than POSIX needs to be costs nothing -- no template or example
    path legitimately contains a backslash or a drive letter.

    A bare `.` and a leading `./` are rejected, not normalised away: `.` made
    `--from-example .` clear this guard, join back to `examples/` itself, and
    copy the SDK's entire examples tree in as one "example".
    """
    if not raw or raw[0] in "/\\":
        return False
    if PureWindowsPath(raw).drive:
        return False
    segments = [s for s in re.split(r"[\\/]", raw) if s]
    if not segments:
        return False
    return all(s not in (".", "..") for s in segments)


# ---------------------------------------------------------------------------
# Hardware-adjacent derivations (see the module docstring)
# ---------------------------------------------------------------------------


def app_core_for_sku(sku: str) -> str:
    """Canonical Zephyr app-core id for a SoM family. `tan init` is SDK-free,
    so this maps by SKU prefix; `tan validate` re-checks it against the real SoM
    catalogue once an SDK resolves. Used ONLY by the hand-generated
    `minimal-app` template -- every vendored template's core id comes from its
    own vendored `board.yaml`, so the two derivations can never disagree."""
    if sku.startswith(("E1M-V2N", "E1M-V2M")):
        return "m33_sm"  # Renesas RZ/V2N, RZ/V2M
    if sku.startswith("E1M-NX9"):
        return "m33"  # NXP
    return "m55_hp"  # Alif Ensemble (E1M-AEN*) + default


def _family_bucket(sku: str) -> str:
    """The vendored tree directory for `sku`, mirroring `app_core_for_sku`'s own
    family split. An unrecognised family (E1M-NX9* included -- the SDK catalog
    ships no NXP scaffold) defaults to the Alif tree rather than inventing
    content or erroring; `retarget_board_yaml_som` still puts the caller's real
    SKU in the rendered `board.yaml`."""
    return _FAMILY_TREES[1] if sku.startswith(("E1M-V2N", "E1M-V2M")) else _FAMILY_TREES[0]


# ---------------------------------------------------------------------------
# board.yaml SoM retargeting
# ---------------------------------------------------------------------------


def retarget_board_yaml_som(content: str, sku: str) -> str:
    """Rewrite the FIRST `som:` -> `sku:` value to `sku`, leaving the rest of
    that line byte-for-byte alone -- UNLESS the value is actually changing and
    a trailing comment is present, in which case the comment is dropped.

    `wizard::retarget_board_yaml_som`. Only the value token moves: the gap
    before a trailing comment is preserved, so a column-aligned inline comment
    (the vendored `iot` scaffold's `sku:` line has one) survives, and passing a
    tree its OWN vendored SKU is a byte-exact no-op (`--template
    iot-starter` always does: its `--som` is validated equal to
    `IOT_STARTER_SUPPORTED_SKU` before this ever runs). Reconstructing the tail
    as a fixed two-space gap silently collapsed that alignment even in the
    no-op case.

    A DIFFERENT `sku` is the one case that tail must NOT survive: an
    `--from-example` board.yaml's inline comment routinely names the ORIGINAL
    SoM's vendor/silicon (e.g. `sku: E1M-AEN801  # Alif Ensemble E8 SoM`), and
    there is no SKU->vendor-name table here to rewrite it correctly for the new
    one -- retargeting onto `E1M-V2N101` left that Alif comment on a Renesas
    SKU. Dropping it is honest; inventing a new one is not this function's job.
    """
    out: list[str] = []
    in_som = False
    done = False
    for line in content.split("\n"):
        if not done:
            trimmed = line.lstrip(" \t")
            if line and not line[0] in " \t":
                # A new top-level key: entering `som:`, or leaving it.
                in_som = trimmed.startswith("som:")
            elif in_som and trimmed.startswith("sku:"):
                indent = line[: len(line) - len(trimmed)]
                after_key = trimmed[len("sku:") :]
                stripped = after_key.lstrip(" \t")
                leading_ws = after_key[: len(after_key) - len(stripped)]
                if not stripped or stripped.startswith("#"):
                    # `sku:` with nothing after it, or `sku:  # comment` with a
                    # comment but no value. Splicing at the first whitespace run
                    # would either glue the value onto `sku:` (read back as a
                    # scalar, not a mapping entry) or eat the `#` into it.
                    out.append(f"{indent}sku: {sku}{after_key}")
                else:
                    match = re.search(r"\s", stripped)
                    value = stripped[: match.start()] if match else stripped
                    tail = stripped[match.start() :] if match else ""
                    if value != sku:
                        tail = ""
                    out.append(f"{indent}sku:{leading_ws}{sku}{tail}")
                done = True
                continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# `--cores`: heterogeneous scaffolding
# ---------------------------------------------------------------------------

#: Accepted per-core OS values for a `--cores` entry (`id:os`). Mirrors
#: `tan-cli::commands::init::resolve::CORE_OS_CHOICES`.
_CORE_OS_CHOICES = ("zephyr", "yocto", "baremetal", "off")


class CoresError(Exception):
    """`--cores` failed to parse or validate; carries the user-facing message
    `init_cmd` wraps as `init.invalid-cores` (exit 2 -- validation)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def infer_runtime_for_core_id(core_id: str) -> str:
    """Best-effort runtime for a `--cores` entry with no `:os` given: an
    `a<digit>` at a word start (e.g. `a55_cluster`) runs `yocto`; everything
    else defaults to `zephyr`. Mirrors
    `tan_core::wizard::infer_runtime_for_core_id` -- KEEP IN SYNC with
    alp-sdk-vscode's ConfiguratorView `coreSiliconClass` (same word-start
    test; the one intentional difference is the fallback, since a CLI must
    pick a concrete runtime where the IDE can offer "unknown")."""
    lower = core_id.lower()
    word_start = True
    for i, ch in enumerate(lower):
        if word_start and ch == "a" and i + 1 < len(lower) and lower[i + 1].isdigit():
            return "yocto"
        word_start = ch in ("_", "-")
    return "zephyr"


def _is_valid_core_id(core_id: str) -> bool:
    """`^[a-z][a-z0-9_]+$`, hand-checked (ASCII only) to mirror the Rust
    byte-level test exactly rather than trust `str.isalpha`/`isdigit`, which
    are Unicode-aware and would accept an id `board.yaml`'s own schema (and
    the Rust CLI) both reject."""
    if len(core_id) < 2 or not ("a" <= core_id[0] <= "z"):
        return False
    return all(("a" <= c <= "z") or ("0" <= c <= "9") or c == "_" for c in core_id)


def parse_cores(raw: str | None) -> list[tuple[str, str]]:
    """Parse + validate `--cores` (`id[:os],...`) into `(id, os)` pairs. OS is
    inferred from the core-id silicon class when omitted. Raises `CoresError`
    on an id outside `^[a-z][a-z0-9_]+$`, an unknown OS, or a duplicate id --
    invalid values would otherwise flow verbatim into board.yaml. `None`/empty
    -> no cores (single-core default). Mirrors
    `tan-cli::commands::init::resolve::parse_cores`."""
    if not raw:
        return []
    cores: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        id_part, _sep, os_part = entry.partition(":")
        core_id = id_part.strip()
        if not core_id:
            continue
        if not _is_valid_core_id(core_id):
            raise CoresError(
                f"Invalid core id '{core_id}' in --cores (expected lowercase id "
                f"matching ^[a-z][a-z0-9_]+$, e.g. m33_sm)."
            )
        os_value = os_part.strip()
        if os_value:
            if os_value not in _CORE_OS_CHOICES:
                raise CoresError(
                    f"Invalid OS '{os_value}' for core '{core_id}' in --cores "
                    f"(expected one of: zephyr, yocto, baremetal, off)."
                )
        else:
            os_value = infer_runtime_for_core_id(core_id)
        if any(existing == core_id for existing, _ in cores):
            raise CoresError(f"Duplicate core id '{core_id}' in --cores.")
        cores.append((core_id, os_value))
    return cores


def _rust_lines(text: str) -> list[str]:
    """`text.split("\\n")`, minus a trailing empty element from a trailing
    `\\n` -- what Rust's `str::lines()` yields for the same LF-only text, and
    what `vendored_app_core_key`/`vendored_core_ids`/`splice_companion_cores`
    below are ported line-for-line against."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def vendored_app_core_key(board_yaml: str) -> str | None:
    """The `cores:` block's APP-core key (e.g. `"m33_sm"`) -- the core whose
    block owns an `app:` line, NOT simply the first core listed. A
    heterogeneous scaffold (`edge-ai`) lists its companion core FIRST
    (`os: "off"`, no `app:`); trusting position would return the companion.
    Works uniformly on every template this port plans (including the
    hand-generated `minimal-app`, whose sole core also carries `app:`), so
    unlike the Rust port there is no need for a per-template
    `vendored_*_app_core_for_sku` family -- this reads the ACTUAL planned
    content instead of a second, independently-derived lookup. Mirrors
    `wizard::service::vendored::vendored_app_core_key`."""
    in_cores = False
    current_core: str | None = None
    for line in _rust_lines(board_yaml):
        if not in_cores:
            in_cores = line == "cores:"
            continue
        if line and not line[0].isspace():
            break  # The next top-level key ends the cores: block.
        if line.startswith("  ") and not line[2:3].isspace() and line.endswith(":"):
            current_core = line[2:-1]  # A `  <core>:` entry key line.
        elif line.startswith("    app:"):
            if current_core is not None:
                return current_core
    return None


def vendored_core_ids(board_yaml: str) -> list[tuple[str, str]]:
    """Every `(core id, os)` pair declared in a board.yaml's `cores:` block,
    in file order -- the app core AND any pre-declared companions (e.g.
    edge-ai's `a55_cluster`/`a32_cluster`). `os` is that core's own `os:`
    child line verbatim (quotes and all, e.g. `"off"`), or `"zephyr"` when
    absent (every vendored app core omits `os:` -- implicitly Zephyr).
    Mirrors `wizard::service::vendored::vendored_core_ids`."""
    in_cores = False
    ids: list[list[str]] = []
    for line in _rust_lines(board_yaml):
        if not in_cores:
            in_cores = line == "cores:"
            continue
        if line and not line[0].isspace():
            break
        if line.startswith("  ") and not line[2:3].isspace() and line.endswith(":"):
            ids.append([line[2:-1], "zephyr"])
        elif line.startswith("    os: "):
            if ids:
                ids[-1][1] = line[len("    os: ") :]
    return [(core_id, os_value) for core_id, os_value in ids]


def splice_companion_cores(board_yaml: str, cores: list[tuple[str, str]]) -> str:
    """Splice `--cores` companions (and a default RPMsg channel to the first
    ACTIVE one, `os != "off"`) into `board_yaml`, after the sole app-core
    entry already inside its `cores:` block. A no-op when `cores` is empty or
    the content has no `cores:` block. Skips any id already declared in the
    `cores:` block (not just the app core) so this can never emit a
    duplicate mapping key -- belt-and-suspenders behind `init_cmd`'s own
    upfront guard. Mirrors
    `wizard::service::vendored::splice_companion_cores`."""
    if not cores:
        return board_yaml
    app_core = vendored_app_core_key(board_yaml)
    if app_core is None:
        return board_yaml
    existing_ids = {core_id for core_id, _os in vendored_core_ids(board_yaml)}

    companion_lines: list[str] = []
    for core_id, os_value in cores:
        if core_id == app_core or core_id in existing_ids:
            continue
        companion_lines.append(f"  {core_id}:")
        companion_lines.append(f"    os: {os_value}")
        if os_value == "yocto":
            companion_lines.append("    image: alp-image-edge")
    if not companion_lines:
        return board_yaml

    out: list[str] = []
    in_cores = False
    inserted = False
    for line in _rust_lines(board_yaml):
        is_top_level = bool(line) and line[0] not in (" ", "\t")
        if is_top_level:
            if in_cores and not inserted:
                out.extend(companion_lines)
                inserted = True
            in_cores = line == "cores:"
        out.append(line)
    if in_cores and not inserted:
        out.extend(companion_lines)
        inserted = True

    result = "\n".join(out) + "\n"
    if inserted:
        companion = next(
            (
                core_id
                for core_id, os_value in cores
                if core_id != app_core and os_value != "off" and core_id not in existing_ids
            ),
            None,
        )
        if companion is not None:
            result += (
                "\nipc:\n"
                "  - kind: rpmsg\n"
                "    name: alp_default_rpmsg\n"
                f"    endpoints: [{app_core}, {companion}]\n"
                "    carve_out_kb: 512\n"
            )
    return result


# ---------------------------------------------------------------------------
# Template planning
# ---------------------------------------------------------------------------


def plan_template_files(template_id: str, sku: str) -> list[PlannedFile]:
    """The files `template_id` lays down for `sku`.

    Raises `TemplateDataError` when a vendored tree cannot be read, and
    `KeyError`-free otherwise: the caller has already validated `template_id`
    against `TEMPLATE_IDS`.
    """
    if template_id == "minimal-app":
        return _minimal_app_files(sku)
    return _vendored_files(_VENDORED_TEMPLATE_DIR[template_id], template_id, sku)


def _vendored_files(tree: str, template_id: str, sku: str) -> list[PlannedFile]:
    """Read a vendored scaffold tree and retarget its `board.yaml` onto `sku`.

    Files come back sorted by their relative POSIX path, which is byte-for-byte
    the order the Rust `vendored_tree!` macro lists them in for every tree
    (`CMakeLists.txt`, `README.md`, `board.yaml`, ... -- uppercase first) -- so
    `data.fileChanges[]` matches the shipped binary's without a hand-kept list
    here to drift out of step with it.

    Sorted on that STRING, never on the `Path`: `PurePath.__lt__` compares a
    case-FOLDED key on Windows, so sorting paths ordered `board.yaml` before
    `CMakeLists.txt` there and after it on Linux -- the same command emitting a
    different `fileChanges[]` order per platform.
    """
    # `iot` has exactly one vendored tree, no family split (its caller rejects
    # any other SKU first); every other template has two.
    family = IOT_STARTER_SUPPORTED_SKU if template_id == "iot-starter" else _family_bucket(sku)
    root = VENDORED_ROOT / tree / family
    try:
        paths = sorted(
            (p for p in root.rglob("*") if p.is_file()),
            key=lambda p: p.relative_to(root).as_posix(),
        )
        if not paths:
            raise TemplateDataError(
                f"tan's vendored template tree for '{template_id}' is empty at "
                f"'{root}'. This is a broken tan installation, not a project problem "
                f"-- reinstall tan, or rebuild the binary with the template data "
                f"(scripts/build_binary.sh)."
            )
        files = []
        for path in paths:
            relative = path.relative_to(root).as_posix()
            content = _read_verbatim(path)
            if relative == "board.yaml":
                content = retarget_board_yaml_som(content, sku)
            files.append(PlannedFile(relative, content))
    except OSError as err:
        raise TemplateDataError(
            f"tan's vendored template tree for '{template_id}' could not be read at "
            f"'{root}': {err}"
        ) from err
    return files


def vendored_library_names_for(template_id: str) -> list[str] | None:
    """The library names `tan explain --template <id>` reports for `template_id`.

    `None` for `minimal-app` -- the one template with no vendored tree, whose
    caller keeps reading the registry's own (empty) `libs` field. A list
    otherwise, read straight from that template's Alif-Ensemble-family
    `board.yaml`: `Some(vec![])` in Rust terms, so an EMPTY list means "this
    scaffold genuinely ships no libraries", never "unknown".

    Mirrors `crates/tan-core/src/wizard/service/vendored.rs`'s
    `vendored_library_names_for`, including its choice of family tree: the
    `libraries:` block is identical across the AEN/V2N pair of every
    family-split template (Rust asserts that in
    `vendored_library_names_matches_across_families`) and `tan explain` has no
    `--som` to pick one with, so the AEN tree stands in for both.

    Fixes tan-cli#124 by construction: the summary derives from the scaffold
    bytes `tan init` actually writes, not from a second hand-synced registry
    field that went stale (`edge-ai-starter` reported no libraries while its
    vendored board.yaml declares `tflite-micro`).

    Raises `TemplateDataError` when the tree will not read, so `tan explain`
    can report a coded issue instead of letting an `OSError` escape as a
    traceback -- the Rust `expect()`s here and panics.
    """
    tree = _VENDORED_TEMPLATE_DIR.get(template_id)
    if tree is None:
        return None
    path = VENDORED_ROOT / tree / _FAMILY_TREES[0] / "board.yaml"
    try:
        text = _read_verbatim(path)
    except OSError as err:
        raise TemplateDataError(
            f"tan's vendored board.yaml for '{template_id}' could not be read at "
            f"'{path}': {err}. This is a broken tan installation, not a project "
            f"problem -- reinstall tan, or rebuild the binary with the template "
            f"data (scripts/build_binary.sh)."
        ) from err
    return _library_names(text)


def _library_names(board_yaml: str) -> list[str]:
    """Every name in a board.yaml's top-level `libraries:` block, in file order.

    A line scan, where Rust parses the whole document through `BoardModel`. The
    input is not a customer's file -- it is tan's own vendored capture, LF, with
    a shape `tests/core/test_scaffold.py` byte-diffs against the Rust trees --
    so a YAML parser would only buy generality this never needs, and PyYAML is
    an OPTIONAL runtime import in this package (see `validate_cmd`): depending
    on it here would make `tan explain --template edge-ai-starter` answer
    differently depending on whether a wheel happened to be installed.

    Both `LibraryEntry` spellings Rust's model accepts are covered, not just the
    scoped one every vendored tree uses today: the bare shorthand
    (`- tflite-micro`) and the scoped object (`- name: tflite-micro`, whose
    sibling keys like `cores: [m55_hp]` are skipped as they are not list items).
    Comments and blank lines inside the block are skipped; the first column-0
    line ends it, exactly as Rust's own `vendored_core_ids` line scan does.
    """
    names: list[str] = []
    in_block = False
    for line in board_yaml.splitlines():
        if not in_block:
            in_block = line.rstrip() == "libraries:"
            continue
        if line and not line[0].isspace():
            break  # The next top-level key ends the libraries: block.
        entry = line.strip()
        if not entry.startswith("- "):
            continue  # Blank line, comment, or a scoped entry's sibling key.
        entry = entry[2:].strip()
        key, sep, value = entry.partition(":")
        raw = value.strip() if sep and key.strip() == "name" else entry
        names.append(raw.strip("\"'"))
    return names


# ---------------------------------------------------------------------------
# `minimal-app`: the one hand-generated template
# ---------------------------------------------------------------------------
#
# tan's OWN content, not a copy of anything the SDK ships -- the SDK catalog has
# no `minimal-app` entry (its `minimal` entry is what `zephyr-app` vendors). Its
# `board.yaml` (below) declares `os: zephyr` and its README says so too -- it
# was always meant to build as a real Zephyr app; "hand-generated" describes
# where the content comes from (tan's own generator, not a vendored SDK
# capture), not a licence to skip Zephyr's own boilerplate.
#
# tan-cli#309 -- two bugs, not one, and fixing only the first makes the second
# worse (a CMake configure error instead of a silent host binary):
#
# 1. `board.yaml`'s `app:` decides which CMakeLists.txt `west build` actually
#    configures, via the planner's `_zephyr_app_dir`
#    (`tan/planner/orchestrator.py`): it resolves `app:` to a directory, and
#    picks that directory ITSELF whenever it holds a `CMakeLists.txt` of its
#    own, falling back to the PARENT only when it does not. This template's
#    `src/` deliberately keeps its own `CMakeLists.txt` (the two-file split
#    below), so `app: ./src` (through v0.5.0-rc3) sent `west build` straight at
#    `src/CMakeLists.txt` -- the root `CMakeLists.txt` (`project()` +
#    `add_subdirectory(src)`, never `add_executable`) was dead code the whole
#    time, not the file at fault.
# 2. `src/CMakeLists.txt` -- the file actually configured -- called plain
#    `add_executable(alp_app ${ALP_APP_SOURCES})`, no `find_package(Zephyr
#    ...)` anywhere in either file. CMake configures and links that shape fine
#    (measured: a real `alp_app.exe`, PE32+ x86-64, built from `CMakeFiles/
#    alp_app.dir/{main,features/app_bootstrap}.obj` -- app_bootstrap.c WAS
#    compiled and linked, just into a host binary Zephyr's build never ran at
#    all), so `tan build` reported success for a project that was never Zephyr.
#
# `_minimal_app_root_cmake`/`_minimal_app_src_cmake` below fix (2): the root
# file now carries `find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})`
# before `project()`, and `src/CMakeLists.txt` contributes to Zephyr's own
# `app` target via `target_sources(app ...)` instead of a second
# `add_executable` -- the same KIND of CMake every vendored template already
# writes (e.g. `templates/vendored/minimal/*/CMakeLists.txt`), while keeping
# its own hand-generated CONTENT. `_minimal_app_board_yaml` fixes (1): `app: .`
# (the project root) so `_zephyr_app_dir` resolves to the root file directly,
# without ever consulting `src/`. Measured after both fixes: a real CMake +
# Ninja + Zephyr-SDK configure+build compiles `src/features/app_bootstrap.c`
# into `app/libapp.a` alongside `src/main.c`, and Zephyr's own link step pulls
# `libapp.a` in whole (`-Wl,--whole-archive app/libapp.a`) on the way to a real
# `zephyr.elf`.
#
# Ported from `wizard/service/c_project.rs`/`gen_board_yaml`'s `app: ./src` up
# through both defects (`crates/` is FROZEN -- see `docs/ROADMAP.md`'s standing
# rule -- so tan-cli#309 is fixed here only, not there); `minimal-app` still
# is not the non-interactive default (see `DEFAULT_TEMPLATE_ID`), a separate,
# independent choice. `contract/envelopes/init-preview-minimal-app` pins its
# exact eight-file list/order (path + change-kind only, never file content or
# `board.yaml`'s `app:` value), which neither fix touches.

#: minimal-app's one feature file: `(path, unit name, TODO line)`.
_MINIMAL_APP_FEATURE_FILE = (
    "src/features/app_bootstrap.c",
    "app_bootstrap",
    "TODO: register app services and initialize runtime modules.",
)

_MINIMAL_APP_EXPLANATION = (
    "Minimal template keeps generated code intentionally small and neutral.",
    "Use this baseline when you want full control over feature bring-up order.",
)

_MINIMAL_APP_BODY_LINES = (
    "Alp minimal starter boot",
    "TODO: add your application logic",
)


def _minimal_app_files(sku: str) -> list[PlannedFile]:
    feature_path, unit_name, todo_line = _MINIMAL_APP_FEATURE_FILE
    return [
        PlannedFile("board.yaml", _minimal_app_board_yaml(sku)),
        PlannedFile("README.md", _minimal_app_readme(sku)),
        # No `prj_conf_extras`: minimal-app declares none.
        PlannedFile("prj.conf", "CONFIG_ASSERT=y\nCONFIG_NEWLIB_LIBC=y\n"),
        PlannedFile("CMakeLists.txt", _minimal_app_root_cmake()),
        PlannedFile("src/CMakeLists.txt", _minimal_app_src_cmake()),
        PlannedFile(
            "include/app/app.h",
            "// SPDX-License-Identifier: Apache-2.0\n"
            "\n"
            "#ifndef ALP_APP_APP_H\n"
            "#define ALP_APP_APP_H\n"
            "\n"
            "int alp_app_init(void);\n"
            "int alp_app_run(void);\n"
            "\n"
            "#endif /* ALP_APP_APP_H */\n",
        ),
        PlannedFile("src/main.c", _minimal_app_main_c()),
        PlannedFile(feature_path, _feature_file(unit_name, todo_line)),
    ]


def _minimal_app_board_yaml(sku: str) -> str:
    """A board.yaml conforming to the SDK board schema: `som` + `cores` are the
    only required top-level keys, and the OS is per-core. There is deliberately
    NO top-level `os:` key (I-02) and no way to ask for one -- a core's runtime
    follows its Cortex class, and this scaffold's app source is Zephyr.

    `app: .` (the PROJECT ROOT), not `./src` (tan-cli#309 round 2):
    `_zephyr_app_dir` (`tan/planner/orchestrator.py`) resolves `app:` to the
    directory holding the CMakeLists.txt `west build` actually configures, and
    picks the `app:` path ITSELF whenever that path has its own
    `CMakeLists.txt` -- falling back to its parent only when it does not. This
    template's `src/` deliberately keeps a `CMakeLists.txt` of its own (the
    two-file split `_minimal_app_root_cmake`/`_minimal_app_src_cmake` write),
    so `app: ./src` sent `west build` straight at `src/CMakeLists.txt` --
    a bare `target_sources(app ...)` with no `find_package(Zephyr ...)`
    of its own -- and skipped the root file (with the REAL `find_package`/
    `project()`) entirely. `app: .` resolves to the project root directly:
    `_zephyr_app_dir` finds `CMakeLists.txt` right there and returns it
    without ever consulting `src/`."""
    return (
        "# Generated by `tan init`.\n"
        "# board.yaml describes hardware: the SoM SKU + per-core app map.\n"
        "# Validate it with `tan validate` once an SDK is resolved.\n"
        "\n"
        "som:\n"
        f"  sku: {sku}\n"
        "cores:\n"
        f"  {app_core_for_sku(sku)}:\n"
        "    os: zephyr\n"
        "    app: .\n"
    )


def _minimal_app_readme(sku: str) -> str:
    notes = "".join(f"- {line}\n" for line in _MINIMAL_APP_EXPLANATION)
    return (
        "# Alp Starter Project\n"
        "\n"
        "Template: minimal-app\n"
        f"SoM: {sku}\n"
        f"App core: {app_core_for_sku(sku)} (Zephyr)\n"
        "\n"
        "## Generated Starter Notes\n"
        "\n"
        f"{notes}"
        "\n"
        "## Next Steps\n"
        "\n"
        "- Run Alp: Validate board.yaml.\n"
        "- Run Alp: Generate all to produce derived outputs under build/generated/.\n"
        "- Extend source files under src/features/ for your target behavior.\n"
        "\n"
        "This workspace was generated by Alp: New Project Wizard.\n"
        "Use Alp commands to validate, generate, and build outputs.\n"
    )


def _minimal_app_root_cmake() -> str:
    """tan-cli#309: the file `board.yaml`'s `app: .` now points `west build`
    at directly, so THIS is the file that has to carry Zephyr's boilerplate --
    before the fix it was `add_subdirectory(src)` with nothing before it, and
    `board.yaml`'s `app: ./src` skipped straight past it to `src/CMakeLists.txt`
    (`_minimal_app_board_yaml`'s docstring has the full mechanism).
    `find_package(Zephyr ...)` has to run before `project()` -- Zephyr's own
    convention (every vendored template does the same, e.g.
    `templates/vendored/minimal/*/CMakeLists.txt`) -- because `find_package`
    is what resolves the toolchain/board machinery `project()` consumes when it
    enables the C language; reversing the order leaves `project()` running
    before Zephyr's own CMake modules are even on `CMAKE_MODULE_PATH`."""
    return (
        "cmake_minimum_required(VERSION 3.20.0)\n"
        "find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})\n"
        "project(alp_starter C)\n"
        "\n"
        "add_subdirectory(src)\n"
    )


def _minimal_app_src_cmake() -> str:
    """Contributes to Zephyr's own `app` target via `target_sources`/
    `target_include_directories` -- never a second `add_executable`, which
    Zephyr's build never links in (tan-cli#309)."""
    feature_path = _MINIMAL_APP_FEATURE_FILE[0]
    rel = feature_path[len("src/") :] if feature_path.startswith("src/") else feature_path
    return (
        "target_sources(app PRIVATE\n"
        "  main.c\n"
        f"  {rel}\n"
        ")\n"
        "target_include_directories(app PRIVATE ../include)\n"
    )


def _minimal_app_main_c() -> str:
    line1, line2 = _MINIMAL_APP_BODY_LINES
    return (
        "// SPDX-License-Identifier: Apache-2.0\n"
        "\n"
        '#include "app/app.h"\n'
        "#include <stdio.h>\n"
        "\n"
        "int alp_app_init(void) {\n"
        "  // TODO: initialize app-level services.\n"
        "  return 0;\n"
        "}\n"
        "\n"
        "int alp_app_run(void) {\n"
        "  // TODO: execute one app cycle.\n"
        "  return 0;\n"
        "}\n"
        "\n"
        "int main(void) {\n"
        "  if (alp_app_init() != 0) {\n"
        '    puts("alp_app_init failed");\n'
        "    return 1;\n"
        "  }\n"
        "\n"
        "  if (alp_app_run() != 0) {\n"
        '    puts("alp_app_run failed");\n'
        "    return 1;\n"
        "  }\n"
        "\n"
        f'  puts("{line1}");\n'
        f'  puts("{line2}");\n'
        "  return 0;\n"
        "}\n"
    )


def _feature_file(unit_name: str, todo_line: str) -> str:
    return (
        "// SPDX-License-Identifier: Apache-2.0\n"
        "\n"
        "#include <stdio.h>\n"
        "\n"
        f"int {unit_name}_step(void) {{\n"
        f"  // {todo_line}\n"
        "  return 0;\n"
        "}\n"
    )


# ---------------------------------------------------------------------------
# `--from-example`: copy an SDK example verbatim
# ---------------------------------------------------------------------------


def read_example_tree(source_dir: Path) -> list[PlannedFile]:
    """Read every regular file under `source_dir` verbatim as UTF-8 text, paths
    forward-slash normalised and sorted by that relative path (NOT by `Path` --
    see `_vendored_files` for why that ordering is platform-dependent).

    UTF-8 only, like the Rust: a binary-carrying example surfaces as an
    `ExampleReadError` instead of being copied corrupt. All shipped examples are
    text today.
    """
    if not source_dir.is_dir():
        raise ExampleReadError(f"'{source_dir}' is not a directory.", not_found=True)
    files: list[PlannedFile] = []
    try:
        for path in sorted(
            (p for p in source_dir.rglob("*") if p.is_file()),
            key=lambda p: p.relative_to(source_dir).as_posix(),
        ):
            files.append(
                PlannedFile(path.relative_to(source_dir).as_posix(), _read_verbatim(path))
            )
    except (OSError, UnicodeDecodeError) as err:
        # UnicodeDecodeError is a ValueError, not an OSError -- catching only
        # OSError would let a binary file in an example escape as a traceback.
        raise ExampleReadError(str(err), not_found=False) from err
    return files


# ---------------------------------------------------------------------------
# Disk: diff, then write
# ---------------------------------------------------------------------------


def _existing_content(path: Path) -> str | None:
    """The file's content, or None when it is absent/unreadable -- an unreadable
    file compares unequal to anything, so it is planned as an `update`."""
    try:
        return _read_verbatim(path)
    except (OSError, UnicodeDecodeError):
        return None


def collect_file_changes(project_root: Path, files: list[PlannedFile]) -> list[FileChange]:
    """Classify each planned file as `new` / `update` / `unchanged` relative to
    `project_root`. Read-only: `--preview` stops after this."""
    changes = []
    for planned in files:
        path = project_root / planned.relative_path
        if not path.exists():
            kind = "new"
        else:
            existing = _existing_content(path)
            kind = "unchanged" if existing == planned.content else "update"
        changes.append(FileChange(planned.relative_path, kind))
    return changes


def write_files(project_root: Path, files: list[PlannedFile]) -> WriteResult:
    """Write every planned file under `project_root`, creating parents as
    needed. A file whose content already matches is skipped and counted
    `unchanged`.

    Every planned target is confined to `project_root` -- after symlink
    resolution -- BEFORE anything is written: a pre-existing symlinked (or
    junctioned) parent directory, or a symlinked existing leaf, that would
    carry a write outside the project refuses the WHOLE run rather than
    writing through it and reporting the in-project logical path as written
    (tan-cli#325). A symlinked project ROOT itself is unaffected -- see
    `resolve_confined`.

    Raises `ScaffoldWriteError` carrying the partial result on the first
    failure -- an unwritable destination, a path component that is a file
    rather than a directory, a read-only tree, a full disk, or the escape
    guard above. Every one of those is the user's environment, not a tan bug,
    so none may escape as a traceback.
    """
    targets: list[tuple[PlannedFile, Path]] = []
    for planned in files:
        try:
            target = resolve_confined(project_root, project_root / planned.relative_path)
        except (PathEscapeError, OSError, ValueError) as err:
            raise ScaffoldWriteError(
                f"refusing to write '{planned.relative_path}': {err}",
                WriteResult([], []),
            ) from err
        targets.append((planned, target))

    written: list[str] = []
    unchanged: list[str] = []
    for planned, target in targets:
        if target.exists() and _existing_content(target) == planned.content:
            unchanged.append(planned.relative_path)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_verbatim(target, planned.content)
        except OSError as err:
            raise ScaffoldWriteError(str(err), WriteResult(written, unchanged)) from err
        written.append(planned.relative_path)
    return WriteResult(written, unchanged)


def scaffold_tree_preview(files: list[PlannedFile]) -> str:
    """The planned files as an ASCII `tree`-style listing, paths sorted, last
    entry marked ``\\`-- ``. Text mode only -- JSON callers read
    `data.fileChanges`."""
    paths = sorted(f.relative_path for f in files)
    lines = ["."]
    for i, path in enumerate(paths):
        lines.append(f"{'`--' if i + 1 == len(paths) else '|--'} {path}")
    return "\n".join(lines) + "\n"


def sdk_pointer_json(sdk_path: str) -> str:
    """The `.alp/sdk-path` pointer file's contents, so a new project is
    reproducible without a separate `tan sdk switch`. Shape and two-space indent
    from `commands::sdk::write_sdk_pointer`; the path is recorded exactly as the
    caller resolved it.

    `SOURCE_DATE_EPOCH` wins over the clock, so a captured pointer is
    reproducible -- through `tan.core.timestamp`, which NEVER raises. This runs
    AFTER the customer's project files already landed, and an out-of-range epoch
    (the MILLISECONDS case) used to kill `tan init` with a traceback here.
    """
    import json  # noqa: PLC0415 -- one call site, not worth a module-level import

    return (
        json.dumps({"sdkPath": sdk_path, "updatedAt": generated_at_iso()}, indent=2)
        + "\n"
    )


def posix(path: Path) -> str:
    """`path` as a string with forward slashes -- what the envelope's path-shaped
    fields carry (the conformance harness normalises `\\` -> `/` on those keys,
    and a golden is authored in the normalised form)."""
    return str(path).replace(os.sep, "/")
