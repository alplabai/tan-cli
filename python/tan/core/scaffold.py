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
prefix to a core id, and `_family_bucket` maps one to a vendored tree. Both
descend from the Rust, both are the *only* hardware knowledge in the port, and
both exist because a scaffold has to name a core before any SDK is reachable --
`tan validate` re-checks the guess once one is. They now read ONE table
(`_SOM_FAMILIES`), which is what makes "the two derivations can never disagree"
true rather than aspirational: tan-cli#579 is exactly the bug where they did,
`app_core_for_sku` having grown an `E1M-NX9` arm that `_family_bucket` never
got, so an NXP `--som` silently rendered the ALIF tree's content. A family the
table knows has no vendored tree is now REFUSED (`UnsupportedSomError` ->
`init.som-unsupported`), never rendered against another vendor's. Do not grow
this: no SKU list, no addresses, no pin names. Note what is NOT here as a result: the
OS is never selected (I-01/I-02) -- every generated core entry is `os: zephyr`
because the app source this scaffold writes is Zephyr source, and a scaffolded
`board.yaml` carries no top-level `os:` key at all.
"""

from __future__ import annotations

from tan.core.os_class import infer_runtime_for_core_id

import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from tan.core.fs_confine import PathEscapeError, resolve_confined
from tan.core.scaffold_selftest_identity import retarget_example_build_target_comment
from tan.core.scaffold_selftest_identity import retarget_selftest_som_identity
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
    "multicore-mailbox",
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
    # The only id that matches its SDK catalog id verbatim (tan-cli#864).
    "multicore-mailbox": "multicore-mailbox",
}


def is_family_gated(template_id: str) -> bool:
    """Whether `template_id` renders a family-specific vendored tree at all --
    `False` for `minimal-app` ALONE, tan's one hand-generated, vendor-neutral
    template (`plan_template_files`'s `template_id == "minimal-app"` early
    return; it never reaches `_vendored_family`/`UnsupportedSomError`, so no
    SoM family can ever refuse it). A public read of `_VENDORED_TEMPLATE_DIR`
    rather than a second copy of its key set -- `explain_cmd` uses this to
    decide whether `UNSUPPORTED_SOM_FAMILY_PREFIXES` applies to a given
    template's `data.som` (tan-cli#866)."""
    return template_id in _VENDORED_TEMPLATE_DIR


#: The SKU assumed when `--som` is absent (`tan_core::DEFAULT_SOM_SKU`).
DEFAULT_SOM_SKU = "E1M-AEN801"

#: Templates whose SDK catalog entry gates `supported.som_skus` to fewer SKUs
#: than tan vendors family trees for. Consulted BEFORE anything is planned, so
#: an unsupported `--som` is refused rather than quietly rendered against the
#: wrong tree.
#:
#: Was a single `IOT_STARTER_SUPPORTED_SKU` constant plus one hard-coded `if`
#: in `init_cmd`. tan-cli#864 added a second such template and measured both
#: ways the one-off failed to generalise: `--som E1M-AEN301` rendered the
#: AEN801 tree at `exitCode 0` (`_family_bucket` maps every unrecognised AEN
#: prefix onto the default family), and `--som E1M-V2N101` surfaced as
#: `init.template-unreadable` exit 5 -- "your tan installation is broken" for
#: a user whose `--som` was simply wrong.
#:
#: `iot-starter`: the CC3501E Wi-Fi transport is silicon-validated on this SKU
#: alone. `multicore-mailbox`: the SDK refuses to emit it for anything else --
#: `alp_project: multicore-mailbox: sku 'E1M-AEN301' is not supported
#: (supported: ['E1M-AEN801'])`, rc=1.
TEMPLATE_SUPPORTED_SKUS: dict[str, tuple[str, ...]] = {
    "iot-starter": ("E1M-AEN801",),
    "multicore-mailbox": ("E1M-AEN801",),
}

#: Vendored directory name per SoM family, `(alif_ensemble, renesas_v2n)` --
#: the two representative SKUs the SDK catalog declares.
_FAMILY_TREES = ("E1M-AEN801", "E1M-V2N101")

#: SoM family table: `(SKU prefix, app-core id, vendored tree)`, consulted in
#: order. A `None` tree means tan vendors NO scaffold for that family.
#:
#: ONE table, read by BOTH `app_core_for_sku` and `_family_bucket`, which is
#: what makes this module's docstring claim -- "the two derivations can never
#: disagree" -- true by construction instead of by two hand-synced prefix
#: tests. tan-cli#579: they DID disagree. `app_core_for_sku` grew an
#: `E1M-NX9` -> `m33` arm and `_family_bucket` did not, so every NXP SKU took
#: the latter's `else` arm onto the Alif tree.
_SOM_FAMILIES: tuple[tuple[str, str, str | None], ...] = (
    ("E1M-V2N", "m33_sm", _FAMILY_TREES[1]),   # Renesas RZ/V2N
    ("E1M-V2M", "m33_sm", _FAMILY_TREES[1]),   # Renesas RZ/V2M -- shares the V2N tree
    ("E1M-NX9", "m33", None),                  # NXP -- alp-sdk's catalog ships no tree
)

#: SKU prefixes `_SOM_FAMILIES` declares NO vendored tree for -- the exact
#: fact `_family_bucket`/`UnsupportedSomError`/`init.som-unsupported` gate a
#: `--som` on, DERIVED from that table rather than retyped, so a family added
#: to (or removed from) `_SOM_FAMILIES` updates this automatically instead of
#: needing a second hand-edit. tan-cli#866: `explain_cmd` reads this to
#: publish the SAME family-level refusal `tan init` already enforces as
#: structured `data.som` on `tan explain --template`, instead of the
#: hand-written "(E1M-AEN801 only)" prose that gave #866 its name -- the
#: exact drift `_SOM_FAMILIES` vs. `app_core_for_sku` risked before tan-cli#579
#: unified them into one table, now risked again between this table and any
#: description string that repeats a fact it already states.
UNSUPPORTED_SOM_FAMILY_PREFIXES: tuple[str, ...] = tuple(
    prefix for prefix, _core, tree in _SOM_FAMILIES if tree is None
)

#: `(app core, tree)` for E1M-AEN* AND for any prefix `_SOM_FAMILIES` does not
#: recognise. Deliberately still a guess rather than a refusal: `tan init` is
#: SDK-free and cannot tell a future SKU from a typo, and guessing Alif is
#: what every version of this module has done. What matters is that BOTH
#: derivations take this arm together, so an unrecognised SKU still gets a
#: self-consistent scaffold; `tan validate` re-checks the guess once an SDK
#: resolves.
_DEFAULT_FAMILY: tuple[str, str] = ("m55_hp", _FAMILY_TREES[0])


def _som_family(sku: str) -> tuple[str, str | None]:
    """`(app-core id, vendored tree)` for `sku` -- the single lookup both
    hardware-adjacent derivations below are built on."""
    for prefix, core, tree in _SOM_FAMILIES:
        if sku.startswith(prefix):
            return core, tree
    return _DEFAULT_FAMILY


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

    Raises `OSError` for a non-UTF-8 file too (tan-cli#415): `UnicodeDecodeError`
    is a `ValueError`, not an `OSError`, so a bare `except OSError` around a
    call to this function -- both call sites below use exactly that -- would
    otherwise let a corrupt vendored tree escape as an unhandled traceback
    instead of the `TemplateDataError` they already raise for every other way
    the read can fail. Folded in here, once, rather than duplicated at each
    call site: mirrors Rust's own `read_to_string`, which returns an
    `io::Error` (kind `InvalidData`) for invalid UTF-8 rather than a distinct
    error type (the same equivalence `kconfig_cmd.py`'s `_resolve_core`
    documents for the identical reason).

    The synthesised `OSError` NAMES THE FILE (tan-cli#494 defect 4). Python's
    own `open()` interpolates the filename into every `OSError` it raises, so
    the permission-denied branch always reported a path; the codec branch --
    the one a stray binary actually trips -- raised a bare
    `UnicodeDecodeError` whose `str()` is only `'utf-8' codec can't decode
    byte 0xff in position 88: invalid start byte`. The caller wraps that
    verbatim into `init.example-unreadable`, leaving the customer with a
    600-file tree and nothing to act on. The oracle's own message
    (`read_to_string` -> `io::Error`) names the path, so this restores parity
    as well as sense.
    """
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as err:
        raise OSError(f"{path}: {err}") from err


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


class UnsupportedSomError(Exception):
    """`--som` names a SoM family tan vendors no scaffold tree for, so the
    requested template cannot be rendered for it (tan-cli#579).

    A USER error (exit 2), not a broken installation: the tree is not missing,
    it was never captured, because alp-sdk's own scaffold catalog declares no
    entry for that family. `init_cmd` reports it as `init.som-unsupported`.

    Refusing is the whole point, and it is a DELIBERATE divergence from the
    frozen v0.4.1 oracle -- measured, `target/debug/tan init --som E1M-NX9101
    --template sensor-starter` exits 0 with `issues: []` and writes the Alif
    tree. The two alternatives were weighed and rejected:

    * **Vendor an NXP tree.** Not tan's to write. `templates/vendored/` is a
      byte-for-byte capture of alp-sdk's `--emit scaffold` output (see its
      `MANIFEST.md`), and `tests/parity/scaffold_byte_parity.py` re-runs the
      live emit against a reachable checkout and fails on drift. A tree
      hand-authored here would be tan inventing hardware facts (I-26) AND
      would fail that gate the moment an SDK is reachable. When alp-sdk adds
      the catalog entry, this refusal retires by filling in one table row in
      `_SOM_FAMILIES`.
    * **Fall back with a loud advisory.** The wrong-vendor project still
      lands on disk, gets committed, and stays wrong: its `CMakeLists.txt`
      asks the SDK loader for the tree family's core while the `board.yaml`
      beside it declares the target's, its README's `west build -b ...` line
      names another vendor's board target, and its `chips:`/`preset:` describe
      another module's BOM. A one-line warning on one terminal, once, does not
      undo that.

    Scoped to the VENDORED templates: `minimal-app` is tan's own
    hand-generated, vendor-neutral stub and stays available for every SKU --
    which is what makes refusing cheap rather than a dead end.
    """

    def __init__(self, template_id: str, sku: str) -> None:
        super().__init__(
            f"Template '{template_id}' has no vendored scaffold for SoM '{sku}'. "
            f"tan ships scaffold trees for the E1M-AEN* and E1M-V2N*/E1M-V2M* "
            f"families only; rendering one of those for this SoM would write "
            f"another vendor's content under your SKU -- its board.yaml "
            f"`preset:`/`chips:`, its README's `west build -b ...` target, and a "
            f"CMakeLists.txt pinned to that family's core. Use `--template "
            f"minimal-app`, which is vendor-neutral and scaffolds any SoM, or "
            f"`--from-example <example>` with an alp-sdk checkout."
        )
        self.template_id = template_id
        self.sku = sku


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
    so this maps by SKU prefix (`_SOM_FAMILIES`); `tan validate` re-checks it
    against the real SoM catalogue once an SDK resolves. Read by the
    hand-generated `minimal-app` template and by `retarget_board_yaml_cores`,
    and derived from the SAME table `_family_bucket` reads, so the two
    derivations cannot disagree (tan-cli#579)."""
    return _som_family(sku)[0]


def _family_bucket(sku: str) -> str | None:
    """The vendored tree directory for `sku`, or `None` when tan vendors no
    scaffold for that SoM family -- `_vendored_files` turns that into an
    `UnsupportedSomError`.

    tan-cli#579: this used to be
    `_FAMILY_TREES[1] if sku.startswith(("E1M-V2N","E1M-V2M")) else
    _FAMILY_TREES[0]`, so E1M-NX9* -- a family `app_core_for_sku` right above
    already knows -- fell down the `else` arm and got the ALIF tree's content,
    at `ok: true` / exit 0 / `issues: []`. An unrecognised prefix still takes
    the Alif default, in both derivations at once (see `_DEFAULT_FAMILY`);
    only a family tan positively knows it has no tree for is refused."""
    return _som_family(sku)[1]


# ---------------------------------------------------------------------------
# board.yaml SoM retargeting
# ---------------------------------------------------------------------------


def _split_cr(line: str) -> tuple[str, str]:
    """Split a `content.split("\\n")` element into `(body, cr)`, where `cr` is
    the `"\\r"` a CRLF source leaves on the end of every line and `""` for LF.

    tan-cli#404: the `\\r` is a LINE TERMINATOR, never content, and every
    character-level decision below (is the tail a comment? where does the value
    token end? is there anything after `sku:` at all?) got it wrong when it was
    left attached. Stripping it once, up front, is what makes those decisions
    terminator-insensitive; the caller re-appends it verbatim, so a CRLF file
    goes back out CRLF and byte-parity with `_read_verbatim` holds.
    """
    return (line[:-1], "\r") if line.endswith("\r") else (line, "")


def _retargeted_sku_line(indent: str, trimmed: str, sku: str) -> tuple[str, bool]:
    """Rewrite one `sku:` line body onto `sku`. Returns the new body (no line
    terminator -- see [`_split_cr`]) and whether a trailing COMMENT was dropped,
    which is what tells the caller a wrapped comment block has been opened and
    its continuation lines must go too.
    """
    after_key = trimmed[len("sku:") :]
    stripped = after_key.lstrip(" \t")
    leading_ws = after_key[: len(after_key) - len(stripped)]
    if not stripped or stripped.startswith("#"):
        # `sku:` with nothing after it, or `sku:  # comment` with a comment but
        # no value. Splicing at the first whitespace run would either glue the
        # value onto `sku:` (read back as a scalar, not a mapping entry) or eat
        # the `#` into it. A `sku:` with no value names no SoM, so its comment
        # is not stale and stays.
        return f"{indent}sku: {sku}{after_key}", False
    # `[ \t]`, not `\s`: the value token ends at a space or a tab and at nothing
    # else. `\s` also matched the `\r` of a CRLF line, so the terminator was
    # captured into `tail` and thrown away with it -- one mixed-EOL line in the
    # very file the customer is about to edit (tan-cli#404).
    match = re.search(r"[ \t]", stripped)
    value = stripped[: match.start()] if match else stripped
    tail = stripped[match.start() :] if match else ""
    if value == sku:
        return f"{indent}sku:{leading_ws}{sku}{tail}", False
    return f"{indent}sku:{leading_ws}{sku}", tail.lstrip(" \t").startswith("#")


def _is_wrapped_comment_line(body: str, sku_indent: str) -> bool:
    """True when `body` is a comment-only line indented PAST `sku_indent` --
    i.e. a continuation of the comment the `sku:` line opened, not a comment
    documenting whatever comes next (tan-cli#404). A blank line ends the block:
    it is not a comment, and a comment resuming after one is a new thought."""
    stripped = body.lstrip(" \t")
    if not stripped.startswith("#"):
        return False
    return len(body) - len(stripped) > len(sku_indent)


def retarget_board_yaml_som(content: str, sku: str) -> str:
    """Rewrite the FIRST `som:` -> `sku:` value to `sku`, leaving the rest of
    that line byte-for-byte alone -- UNLESS the value is actually changing and
    a trailing comment is present, in which case the comment is dropped, all of
    it, however many physical lines it spans.

    `wizard::retarget_board_yaml_som`. Only the value token moves: the gap
    before a trailing comment is preserved, so a column-aligned inline comment
    (the vendored `iot` scaffold's `sku:` line has one) survives, and passing a
    tree its OWN vendored SKU is a byte-exact no-op (`--template
    iot-starter` always does: its `--som` is validated equal to
    `TEMPLATE_SUPPORTED_SKUS` before this ever runs). Reconstructing the tail
    as a fixed two-space gap silently collapsed that alignment even in the
    no-op case.

    A DIFFERENT `sku` is the one case that tail must NOT survive: an
    `--from-example` board.yaml's inline comment routinely names the ORIGINAL
    SoM's vendor/silicon (e.g. `sku: E1M-AEN801  # Alif Ensemble E8 SoM`), and
    there is no SKU->vendor-name table here to rewrite it correctly for the new
    one -- retargeting onto `E1M-V2N101` left that Alif comment on a Renesas
    SKU. Dropping it is honest; inventing a new one is not this function's job.

    tan-cli#404 -- that drop used to be strictly line-local, and 12 of the 100
    `board.yaml` files in the SDK example catalogue carry a `sku:` comment that
    WRAPS. `aen/edgeai-vision-aen` retargeted onto `E1M-V2N101` kept four of its
    five comment lines: a Renesas RZ/V2N project whose `board.yaml` documented a
    pair of Alif Ethos-U55s, an Ethos-U85 and a VeriSilicon ISP Pico
    (`vsi,isp-pico`) attached to nothing -- in a file the project ships as
    teaching material, with nothing in `tan init`'s output saying a comment had
    been rewritten at all. So the drop now consumes the block: the inline
    comment plus every following comment-only line indented past the `sku:` key.
    Deliberately anchored to THAT comment, not to any comment near a changed
    `sku:` -- a `sku:` line with no comment of its own opens no block, so a
    comment documenting the next key is never swallowed.
    """
    out: list[str] = []
    in_som = False
    rewritten = False
    # The `sku:` line's own indent while a dropped comment's continuation lines
    # are still being consumed; `None` at every other point.
    consuming: str | None = None
    for line in content.split("\n"):
        body, cr = _split_cr(line)
        if consuming is not None:
            if _is_wrapped_comment_line(body, consuming):
                continue
            consuming = None
        if not rewritten:
            trimmed = body.lstrip(" \t")
            if body and body[0] not in " \t":
                # A new top-level key: entering `som:`, or leaving it.
                in_som = trimmed.startswith("som:")
            elif in_som and trimmed.startswith("sku:"):
                indent = body[: len(body) - len(trimmed)]
                new_body, comment_dropped = _retargeted_sku_line(indent, trimmed, sku)
                out.append(new_body + cr)
                rewritten = True
                consuming = indent if comment_dropped else None
                continue
        out.append(line)
    return "\n".join(out)


def _indent_of(body: str) -> int:
    return len(body) - len(body.lstrip(" \t"))


def _cores_block_span(lines: list[str]) -> tuple[int, int] | None:
    """`(start, end)` half-open line indices of the top-level `cores:` block's
    CHILDREN, or `None` when there is no such block. `start` is the line after
    `cores:`; `end` is the next column-0 key (or EOF)."""
    start: int | None = None
    for index, line in enumerate(lines):
        body, _ = _split_cr(line)
        if not body or body[0] in " \t":
            continue
        if start is not None:
            return (start, index)
        if body.lstrip(" \t").startswith("cores:"):
            start = index + 1
    return None if start is None else (start, len(lines))


def retarget_board_yaml_cores(content: str, sku: str, source_sku: str) -> str:
    """Re-derive a vendored `cores:` block for `sku` (tan-cli#494 defect 2).

    `tan init` picks a vendored tree by FAMILY (`_family_bucket`) and then
    retargeted only the `som: sku:` line, so every SKU outside the two
    representative ones (`E1M-AEN801` / `E1M-V2N101`) inherited that tree's
    core ids verbatim. `tan init --template edge-ai-starter --som E1M-AEN301`
    wrote `cores: a32_cluster:` for an Ensemble E3, which has no Cortex-A32 --
    reported `ok:true` / `exitCode 0` / `issues:[]`, and `tan validate` then
    hard-errored (exit 2) on the very next command. `--som E1M-NX9101` landed
    on the Alif tree and got `m55_hp` against a topology of
    `a55_cluster`/`m33`, contradicting this same module's `app_core_for_sku`.

    Two edits, both of which can only REMOVE wrong facts, never invent new
    ones -- `tan init` is SDK-free and has no SoM topology to consult:

    * the APP core (the one entry declaring `app:`) is renamed to
      `app_core_for_sku(sku)`, tan's own family mapping, the same one
      `minimal-app` already renders and `tan validate` re-checks against the
      real catalogue;
    * every OTHER entry is DROPPED. Those are the `os: "off"` secondary
      cluster declarations (`a32_cluster` on the E8, `a55_cluster` on the
      V2N/i.MX 93) which exist only on the tree's own representative SKU. A
      core absent from `cores:` is simply not built, so dropping is always
      sound; keeping a made-up id, or guessing the target's cluster id from a
      table tan cannot verify, is not.

    NO-OP by construction when `sku == source_sku` (the tree's own SKU, which
    is what `E1M-AEN801`/`E1M-V2N101` always hit) -- byte-exact passthrough,
    so the parity and vendored-capture fixtures are untouched. Also a no-op
    when the block has no unambiguous single `app:` entry: an unrecognised
    shape is left verbatim rather than half-rewritten.
    """
    if sku == source_sku:
        return content
    lines = content.split("\n")
    span = _cores_block_span(lines)
    if span is None:
        return content
    start, end = span
    entry_indent: int | None = None
    entries: list[tuple[int, int]] = []  # (key line index, entry end index)
    for index in range(start, end):
        body, _ = _split_cr(lines[index])
        if not body.strip():
            continue
        indent = _indent_of(body)
        if entry_indent is None:
            entry_indent = indent
        if indent == entry_indent:
            if entries:
                entries[-1] = (entries[-1][0], index)
            entries.append((index, end))
    if not entries:
        return content
    entries[-1] = (entries[-1][0], end)

    def declares_app(entry: tuple[int, int]) -> bool:
        key, stop = entry
        return any(
            _split_cr(lines[i])[0].lstrip(" \t").startswith("app:")
            for i in range(key + 1, stop)
        )

    app_entries = [e for e in entries if declares_app(e)]
    if len(app_entries) != 1:
        return content
    app_key, app_stop = app_entries[0]
    key_body, key_cr = _split_cr(lines[app_key])
    trimmed = key_body.lstrip(" \t")
    if ":" not in trimmed:
        return content
    indent = key_body[: len(key_body) - len(trimmed)]
    tail = trimmed[trimmed.index(":") :]
    kept = [f"{indent}{app_core_for_sku(sku)}{tail}{key_cr}"]
    kept.extend(lines[app_key + 1 : app_stop])
    # The block's own trailing blank line separates `cores:` from the next
    # top-level key. It rides along with whichever entry came last, so it is
    # re-appended by hand when that entry was one of the dropped ones.
    if end > start and not lines[end - 1].strip() and kept[-1].strip():
        kept.append(lines[end - 1])
    return "\n".join([*lines[:start], *kept, *lines[end:]])


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
        # `off` is a YAML 1.1 boolean keyword (`yaml.safe_load("os: off")` ->
        # `{"os": False}`), so an unquoted companion entry writes a bool where
        # the schema requires the string `"off"` -- confirmed against
        # `metadata/schemas/board-v2.schema.json`'s `os` enum, which rejects
        # `False` with "False is not of type 'string'". Every vendored
        # scaffold already quotes it this way (e.g.
        # `tan/templates/vendored/edge-ai/E1M-AEN801/board.yaml`); the other
        # three enum values (`zephyr`/`yocto`/`baremetal`) are not YAML
        # keywords and stay bare to match that same convention.
        os_literal = '"off"' if os_value == "off" else os_value
        companion_lines.append(f"    os: {os_literal}")
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
        # tan-cli#925: never append over a board that already declares
        # `ipc:`. PyYAML accepts a duplicate top-level key and keeps the
        # LAST, so an unconditional append does not fail -- it silently
        # discards the project's own channel. alp-sdk's multicore-mailbox
        # scaffold declares `alp_shmem0`, and both its `src/main.c` and
        # `peer/main.c` `#define SHMEM_REGION_NAME "alp_shmem0"`.
        declares_ipc = any(line.startswith("ipc:") for line in _rust_lines(board_yaml))
        if companion is not None and not declares_ipc:
            result += (
                "\nipc:\n"
                "  - kind: rpmsg\n"
                "    name: alp_default_rpmsg\n"
                f"    endpoints: [{app_core}, {companion}]\n"
                "    carve_out_kb: 256\n"
            )
    return result


# ---------------------------------------------------------------------------
# Template planning
# ---------------------------------------------------------------------------


def plan_template_files(template_id: str, sku: str) -> list[PlannedFile]:
    """The files `template_id` lays down for `sku`.

    Raises `TemplateDataError` when a vendored tree cannot be read,
    `UnsupportedSomError` when tan vendors no tree for `sku`'s SoM family
    (tan-cli#579), and is `KeyError`-free otherwise: the caller has already
    validated `template_id` against `TEMPLATE_IDS`.

    `minimal-app` can never raise the second: it is hand-generated here and
    vendor-neutral, so it scaffolds every SKU -- see `UnsupportedSomError`.
    """
    if template_id == "minimal-app":
        return _minimal_app_files(sku)
    return _vendored_files(_VENDORED_TEMPLATE_DIR[template_id], template_id, sku)


def _vendored_family(template_id: str, sku: str) -> str:
    """Which family subdirectory `template_id` reads for `sku` -- and the one
    place that refuses a SoM family tan vendors no tree for (tan-cli#579).

    Split out of `_vendored_files` (which does the IO) so the family choice --
    the decision tan-cli#579 got wrong -- is one small pure lookup with its
    refusal beside it. Raising HERE means the caller's run fails before a
    single byte of another vendor's tree is read, so no half-planned project
    can escape.
    """
    # `iot` has exactly one vendored tree, no family split (its caller rejects
    # any other SKU first); every other template has two.
    restricted = TEMPLATE_SUPPORTED_SKUS.get(template_id)
    family = restricted[0] if restricted else _family_bucket(sku)
    if family is None:
        raise UnsupportedSomError(template_id, sku)
    return family


def _vendored_files(tree: str, template_id: str, sku: str) -> list[PlannedFile]:
    """Read a vendored scaffold tree and retarget its `board.yaml` onto `sku`.

    Files come back sorted by their relative POSIX path (`CMakeLists.txt`,
    `README.md`, `board.yaml`, ... -- uppercase first), the order the Rust
    `vendored_tree!` macro lists them in, so `data.fileChanges[]` matches the
    shipped binary's without a hand-kept list here to drift out of step with
    it. `iot` is the one tree where the two LISTS differ, not just their order:
    it carries a `native_sim.conf` the frozen Rust tree never got (tan-cli#379,
    declared in `test_scaffold_content_oracle_parity.py`'s
    `FILE_SET_DIVERGENCE` until tan-cli#269 deleted it with the oracle axis;
    `tests/core/test_template_integrity.py` pins the FILE now, not the diff),
    so `tan init --template iot-starter --format json` returns one
    `fileChanges[]` entry more than the oracle did. Sorting is what keeps
    every file the two trees DO share in the same relative order. Sorted on
    that STRING, never on the `Path`: `PurePath.__lt__` compares a
    case-FOLDED key on Windows, so sorting paths ordered `board.yaml` before
    `CMakeLists.txt` there and after it on Linux -- the same command emitting a
    different `fileChanges[]` order per platform.
    """
    family = _vendored_family(template_id, sku)
    root = VENDORED_ROOT / tree / family
    try:
        paths = _example_source_files(root) if root.is_dir() else []
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
                content = retarget_board_yaml_som(content, sku)  # tan-cli#494 defect 2
                content = retarget_board_yaml_cores(content, sku, family)
            content = retarget_selftest_som_identity(content, sku, family)
            content = retarget_example_build_target_comment(content, sku, family)
            files.append(PlannedFile(relative, content))
        _require_complete_tree(template_id, root, files)
    except OSError as err:
        raise TemplateDataError(
            f"tan's vendored template tree for '{template_id}' could not be read at "
            f"'{root}': {err}"
        ) from err
    return files


#: Files every vendored scaffold tree must carry, whatever the template: the
#: build script, the SoM declaration, and the Zephyr config. A tree missing any
#: of them is not a scaffold.
_VENDORED_BASELINE_FILES = ("CMakeLists.txt", "board.yaml", "prj.conf")

#: `target_sources(app PRIVATE <path> ...)`, across line breaks -- `edge-ai`
#: spells it over three lines, every other template on one.
_TARGET_SOURCES_RE = re.compile(r"target_sources\s*\(\s*app\s+PRIVATE\s+(.*?)\)", re.DOTALL)


def _require_complete_tree(template_id: str, root: Path, files: list[PlannedFile]) -> None:
    """Refuse a PARTIALLY delivered vendored tree (tan-cli#494 defect 3).

    The `if not paths:` guard above is all-or-nothing: it fires only when the
    tree yields ZERO files, so any non-empty SUBSET read as complete. With
    `src/main.c` absent -- a mis-scoped PyInstaller `--add-data`, a partial
    copy, an extracted onedir whose `src/` lost `+x` -- `tan init` wrote five
    files, reported `ok:true` / `issues:[]`, and the `CMakeLists.txt` it DID
    write still said `target_sources(app PRIVATE src/main.c)`. The customer's
    first `tan build` then died inside CMake with "Cannot find source file",
    reported to them as their project's problem. `TemplateDataError` /
    `init.template-unreadable` is exactly the coded outcome that broken-
    installation population is owed.

    The expected set is DERIVED, never a hand-kept list that would drift out
    of step with the trees: a baseline every scaffold has by definition
    (`_VENDORED_BASELINE_FILES`) plus every relative path the tree's own
    `CMakeLists.txt` hands to `target_sources(app PRIVATE ...)`. That second
    half is what makes the check track a template that gains a `.c` file with
    no edit here -- and it is the precise set whose absence breaks the build.
    """
    present = {f.relative_path for f in files}
    expected = set(_VENDORED_BASELINE_FILES)
    cmake = next((f.content for f in files if f.relative_path == "CMakeLists.txt"), None)
    if cmake is not None:
        for block in _TARGET_SOURCES_RE.findall(cmake):
            expected.update(tok for tok in block.split() if not tok.startswith("$"))
    missing = sorted(expected - present)
    if missing:
        raise TemplateDataError(
            f"tan's vendored template tree for '{template_id}' at '{root}' is "
            f"incomplete -- missing {', '.join(missing)}. This is a broken tan "
            f"installation, not a project problem -- reinstall tan, or rebuild the "
            f"binary with the template data (scripts/build_binary.sh)."
        )


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


#: Directory names under an SDK example that hold BUILD OUTPUT, never example
#: source -- pruned by [`read_example_tree`] (tan-cli#494 defect 1).
#:
#: Transcribed from alp-sdk's OWN `.gitignore`, which is the authority on what
#: is untracked artifact in that repo: `tan init --from-example` copies an
#: example into a NEW project, and a `west build` run in place turns a 6-file
#: example into 613 files of `CMakeCache.txt`/`.ninja_deps`/`libapp.a`. Those
#: are not the customer's project, and the first binary among them aborts the
#: whole command with `init.example-unreadable`.
#:
#: The transcription is of TWO blocks, and the round of this fix that shipped
#: with tan-cli#583 took only part of the first -- it claimed in this very
#: comment to be "EXACTLY the five `.gitignore` patterns" while covering five
#: of the SEVEN directory patterns alp-sdk declares:
#:
#: * `.gitignore:1-6`, the `# Build directories` block -- `build/`, `build_*/`,
#:   `out/`, `cmake-build-*/`, `bwdt/`. `out/` and `bwdt/` were BOTH missed.
#: * `.gitignore:36-37`, the west/twister block -- `twister-out/`,
#:   `twister-out.*/`. Both were taken.
#:
#: `out/` is not hypothetical and is not only a `west build -d out` spelling:
#: `examples/camera-vision/ai-object-detection-realtime/README.md:83` tells the
#: customer, in the example's own instructions, to run `dxcom -m yolov8n.onnx
#: -c yolov8n_config.json -o out/` INSIDE the example. Measured with that `out/`
#: present: a `.dxnn` blob there fails the whole command with
#: `init.example-unreadable`, and an all-text `out/` (Intel HEX is ASCII, so
#: `out/zephyr/zephyr.hex` qualifies) is copied silently at `ok:true` / exit 0 /
#: `issues: []` -- a stale artefact from someone else's build landing in a brand
#: new project. `bwdt/` carries no explanation anywhere in alp-sdk, but it sits
#: inside that `# Build directories` block, so the same authority covers it.
#:
#: Pruning by a `.gitignore`d NAME can only ever drop artefact, never source: a
#: name alp-sdk declares untracked cannot BE tracked example content there, and
#: `--from-example` reads an alp-sdk checkout by construction
#: (`init_cmd._plan_from_example` resolves under `<sdk>/examples`). Measured
#: against alp-sdk `origin/dev` 7d58ef32: ZERO tracked files anywhere in that
#: repo lie under an `out/` or `bwdt/` path segment. That invariant is what
#: makes ADDING a declared pattern safe and INVENTING one unsafe -- an earlier
#: cut also pruned `build-`, in no pattern there, which would have silently
#: dropped a hand-written `build-utils/` from a customer's new project.
#:
#: FILE patterns are deliberately out of scope: `.gitignore:55` also declares
#: `*.out`, but this mechanism prunes DIRECTORY names off `os.walk`'s
#: `dirnames`, and a file-level filter is a different change with a different
#: blast radius. No shipped example carries one.
#:
#: `tests/core/test_scaffold.py::test_the_prune_list_still_covers_alp_sdk_s_own_
#: build_directory_gitignore_blocks` re-reads BOTH source blocks out of a bound
#: `ALP_SDK_ROOT` checkout and fails when alp-sdk declares a directory that is
#: neither pruned here nor recorded there as deliberately kept -- so the next
#: one is caught rather than transcribed short a second time.
#:
#: DELIBERATE DIVERGENCE from the v0.4.1 Rust oracle, which HAD the same missing
#: exclusion (`crates/tan-core/src/wizard/filesystem.rs::collect_example_files`,
#: deleted along with the rest of `crates/` by tan-cli#269) and failed
#: identically -- the oracle was a fixed point for BEHAVIOUR THAT IS RIGHT, and
#: copying a build tree into a new project is not. No parity capture pins
#: `--from-example` content (`tests/fixtures/oracle_captures/
#: PARITY-COVERAGE.txt` covers `scaffold` refusals only), so nothing frozen
#: moves for this.
#:
#: Three more references to the now-deleted `crates/` tree survive in this file
#: (lines 5, 933 and 1048) as history of where this module was ported from;
#: they are pre-existing and repo-wide, and are left for a sweep of their own.
_EXAMPLE_BUILD_OUTPUT_DIRS = ("build", "out", "bwdt", "twister-out")
_EXAMPLE_BUILD_OUTPUT_PREFIXES = ("build_", "cmake-build-", "twister-out.")


def _is_build_output_dir(name: str) -> bool:
    """Whether a directory NAME under an example is untracked build output.

    Exact-match names and `startswith` prefixes are kept apart on purpose: the
    `.gitignore` patterns behind the first group carry no `*`, so `out/` must
    NOT match `outputs/` or `outbox/` and `build/` must not match
    `build-utils/`. Only the three patterns that really are globs
    (`build_*/`, `cmake-build-*/`, `twister-out.*/`) get prefix treatment.
    """
    return name in _EXAMPLE_BUILD_OUTPUT_DIRS or name.startswith(
        _EXAMPLE_BUILD_OUTPUT_PREFIXES
    )


def _example_source_files(source_dir: Path) -> list[Path]:
    """Every regular file under `source_dir` that is example SOURCE: build-output
    directories pruned, symlinks skipped on BOTH the file and the directory side.

    `os.walk`, not `Path.rglob("*")`, for two reasons the walk needs and the
    glob cannot give: `dirnames` is mutable, which is what makes pruning a
    build tree cost nothing instead of stat-ing all 607 files inside it; and
    `onerror` surfaces a directory that could not be listed instead of
    `rglob`'s silent skip (`_vendored_files` needs the same guarantee -- see
    tan-cli#494 defect 3).

    Symlinks are skipped, matching the oracle's `DirEntry::file_type()`, whose
    own doc comment says "Symlinks and other non-regular entries are skipped":
    `Path.is_file()` FOLLOWS a symlinked file, so a link pointing outside the
    example would inline that outside file's content into the customer's new
    project. `followlinks` stays at its `False` default for the directory side.
    """

    def _raise(err: OSError) -> None:
        raise err

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(source_dir, onerror=_raise):
        dirnames[:] = [d for d in dirnames if not _is_build_output_dir(d)]
        here = Path(dirpath)
        for name in filenames:
            candidate = here / name
            # `is_symlink()` FIRST: a dangling link is not `is_file()` either,
            # and the two must not be distinguishable here.
            if candidate.is_symlink() or not candidate.is_file():
                continue
            found.append(candidate)
    return sorted(found, key=lambda p: p.relative_to(source_dir).as_posix())


def read_example_tree(source_dir: Path) -> list[PlannedFile]:
    """Read every regular file under `source_dir` verbatim as UTF-8 text, paths
    forward-slash normalised and sorted by that relative path (NOT by `Path` --
    see `_vendored_files` for why that ordering is platform-dependent).

    Build output is NOT example source and never reaches the new project --
    see `_example_source_files`, which also does the sorting and the
    symlink/unlistable-directory handling.

    UTF-8 only, like the Rust: a binary-carrying example surfaces as an
    `ExampleReadError` instead of being copied corrupt. All shipped examples are
    text today -- and with build output pruned, that is true of a built-in-place
    checkout too.
    """
    if not source_dir.is_dir():
        raise ExampleReadError(f"'{source_dir}' is not a directory.", not_found=True)
    files: list[PlannedFile] = []
    try:
        for path in _example_source_files(source_dir):
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
