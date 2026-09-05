# SPDX-License-Identifier: Apache-2.0
"""`--emit scaffold` text rewrites: board.yaml/CMakeLists.txt/README.md.

SPLIT out of `tan/planner/template.py` (tan-cli#1142; see `template_pins.py`'s
module docstring for why the split is allowed under `HAND_PORT_SOURCES` and
why `MIRRORED_PREFIX` does not bar it -- both files are the same story, not
repeated here).

WHAT'S HERE: every function that takes a rendered file's TEXT and a rename
(sku, core, pin, macro, doc, docs ref) and returns rewritten text -- the
`board.yaml` sku/core/pin substitutions, the CMakeLists.txt ALP_SDK_ROOT
hardening, and the README.md link/board/pin rewrites -- plus the two small
metadata-adjacent lookups those rewrites need (`_tag_resolves`, `_docs_ref`).
What is NOT here: anything that reads `metadata/e1m_modules/**` or
`metadata/boards/**` to DERIVE a rename in the first place -- that's
`template_pins.py` -- and `_cmake_core_map`, which stayed in `template.py`
itself (it needs `_safe_join`/`_require_field`/`_require_key` and
`orchestrator._zephyr_app_dir`, none of which this module or `template_pins.py`
otherwise touches).

Every docstring below is UNCHANGED from `template.py` -- issue numbers,
tan-cli numbers and worked examples all still refer to that module's own
history, because this split moved code, not its provenance.

Imports `TemplateError` back FROM `.template`, the same circular-at-the-
module-object-level direction `template_pins.py` uses and explains at length
in its own docstring -- read that one first if this import looks backwards.
It works here for the same reason: `template.py` binds `TemplateError` (its
exception classes, near the top of the file) before its own
`from .template_rewrite import ...` line runs.

Also imports `_pin_pad_and_macro` from `.template_pins` -- NOT circular: by
the time `template.py` reaches its own `from .template_rewrite import ...`
line, its preceding `from .template_pins import ...` line (tan-cli#1142's
NameError fix) has already run `template_pins.py` to completion, so this
module sees a fully-built `tan.planner.template_pins`, same as any ordinary
import.
"""
from __future__ import annotations

import posixpath
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from tan.core.subprocess_env import spawn_env

from .template import TemplateError
from .template_pins import _pin_pad_and_macro


# Matches board.yaml's `som:\n  sku: E1M-...` line and the top-level
# `preset: <name>` line -- through end-of-line (incl. any trailing inline
# comment), so a value CHANGE can drop a comment describing the OLD SoM
# (e.g. `sku: E1M-AEN801   # Alif Ensemble E8 SoM` must not survive as a
# stale label once the value becomes E1M-V2N101). Unbounded (no count=):
# every match is inspected so a board.yaml with more than one matching
# `sku:`/`preset:` line -- ambiguous, could silently rewrite a decoy while
# the real som.sku/preset line survives untouched -- hard-errors instead
# of guessing which one is real.
_SOM_SKU_RE = re.compile(r"(?m)^(\s*sku:\s*)(E1M-[A-Z0-9]+)[^\n]*$")
_PRESET_RE = re.compile(r"(?m)^(preset:\s*)(\S+)[^\n]*$")


def _substitute_board_yaml_sku(text: str, sku: str, preset: str) -> str:
    def _sub_sku(m: re.Match[str]) -> str:
        # Value unchanged -> leave the WHOLE line (incl. any comment)
        # untouched: this is the byte-passthrough guarantee for sku ==
        # the example's own default.
        return m.group(0) if m.group(2) == sku else f"{m.group(1)}{sku}"

    text, n_sku = _SOM_SKU_RE.subn(_sub_sku, text)
    if n_sku != 1:
        raise TemplateError(
            f"board.yaml must have exactly one `som.sku:` line to "
            f"substitute (found {n_sku})")

    def _sub_preset(m: re.Match[str]) -> str:
        return m.group(0) if m.group(2) == preset else f"{m.group(1)}{preset}"

    text, n_preset = _PRESET_RE.subn(_sub_preset, text)
    if n_preset != 1:
        raise TemplateError(
            f"board.yaml must have exactly one top-level `preset:` line "
            f"to substitute (found {n_preset})")
    return text


_LIBRARY_CORE_SCOPE_RE = re.compile(r"(cores:\s*\[)([^\]]*)(\])")


def _strip_stale_core_prose(text: str, old: str) -> str:
    """Delete any full comment LINE naming `old` in PROSE form (issue
    #864 Fable-review MINOR F) -- e.g. gpio-button-led's board.yaml
    carries `# Single-core slice: M55-HP runs the demo.  M55-HE
    inherits...` directly above `cores:\\n  m55_hp:`, which the plain
    `m55_hp:` key-line regex below never touches (different case,
    hyphen instead of underscore). Matches case-insensitively with `_`
    /`-` interchangeable. A hardware-specific sentence about the
    canonical SoM's OTHER core/topology doesn't have a sensible
    equivalent on a different SoM family, so deleting the line is
    safer than guessing a replacement."""
    prose = re.escape(old).replace("_", "[_-]")
    line_re = re.compile(rf"(?mi)^[ \t]*#.*\b{prose}\b.*\n?")
    return line_re.sub("", text)


def _substitute_board_yaml_core(text: str, old: str, new: str) -> str:
    """Rewrite the `cores:` mapping's single top-level `<old>:` key to
    `<new>:`. The per-core content underneath (`app:`, `peripherals:`)
    is core-id-agnostic -- metadata/schemas/board.schema.json's
    `core_entry` says every field is optional and inherits the SoM
    preset's `topology.<core_id>` default, so only the KEY changes.

    Also renames `old` wherever a top-level `libraries:` entry scopes
    itself to this core via a `cores: [<id>, ...]` flow list (e.g.
    cold-chain-monitor's `libraries: [{name: tflite-micro, cores:
    [m55_hp]}]`) -- `alp_orchestrate.loader._normalize_libraries` hard-
    errors if that list still names a core id that no longer exists
    once the `cores:` mapping key above is renamed ("libraries: entry
    '<name>' is scoped to core '<old>', which is not declared under
    `cores:`"). Also strips any comment line describing `old` in prose
    (see `_strip_stale_core_prose`)."""
    text = _strip_stale_core_prose(text, old)
    pattern = re.compile(rf"(?m)^(\s*){re.escape(old)}:([ \t]*)$")
    new_text, n = pattern.subn(lambda m: f"{m.group(1)}{new}:{m.group(2)}", text)
    if n != 1:
        raise TemplateError(
            f"board.yaml must have exactly one `cores.{old}:` line to "
            f"re-derive to {new!r} (found {n})")

    def _fix_scope_list(m: re.Match[str]) -> str:
        inner = re.sub(rf"\b{re.escape(old)}\b", new, m.group(2))
        return f"{m.group(1)}{inner}{m.group(3)}"

    return _LIBRARY_CORE_SCOPE_RE.sub(_fix_scope_list, new_text)


def _substitute_board_yaml_pins(
    text: str, renames: dict[str, str], original_pins: list[Any],
) -> str:
    """Rewrite each renamed pad wherever a `pins:` entry names it --
    scoped to the two shapes a `pins:` list item can take (issue #876
    review MINOR 3), not a blanket `\\b<pad>\\b` replace over the whole
    file (a pad name can also appear in unrelated prose, e.g. gpio-
    button-led's `preset:` header comment -- see `_strip_stale_core_
    prose`, reused for pins in `render_to_envelope`):

    * the dict form's `e1m: <old>` field (`(e1m:\\s*)<pad>\\b`), and
    * the bare pad-string list-item form (`- <old>`,
      `([ \\t]*-[ \\t]*)<pad>\\b`) the schema also allows -- a template
      using this form had its pad left stale by the dict-only regex
      (silent `--emit zephyr-conf` failure downstream: the exact class
      of bug #876 exists to kill), and a MIXED bare + dict entry for
      the same pad hid it entirely (the dict match alone satisfied the
      old "at least one occurrence" guard).

    `original_pins` supplies the EXPECTED occurrence count per pad (how
    many entries -- bare or dict -- actually name it), so the rewrite
    is verified exact rather than "found at least one"."""
    for old, new in renames.items():
        expected = sum(
            1 for item in original_pins
            if _pin_pad_and_macro(item)[0] == old
        )
        dict_pattern = re.compile(rf"(e1m:\s*){re.escape(old)}\b")
        text, n_dict = dict_pattern.subn(lambda m: f"{m.group(1)}{new}", text)
        bare_pattern = re.compile(rf"(?m)^([ \t]*-[ \t]*){re.escape(old)}\b")
        text, n_bare = bare_pattern.subn(lambda m: f"{m.group(1)}{new}", text)
        if n_dict + n_bare != expected:
            raise TemplateError(
                f"board.yaml `pins:` re-derive of `{old}` -> {new!r}: "
                f"expected {expected} occurrence(s), rewrote "
                f"{n_dict + n_bare}")
    return text


def _substitute_board_yaml_pin_macros(text: str, renames: dict[str, str]) -> str:
    """Companion to `_substitute_board_yaml_pins`: rewrite each `pins:`
    entry's `macro:` field per `_derive_pin_macro_renames`'s map, the
    same scoped-to-the-key approach (`(macro:\\s*)<old>\\b`) -- `macro:`
    only ever appears in the dict form (a bare pad-string entry has
    no `macro:` at all)."""
    for old, new in renames.items():
        pattern = re.compile(rf"(macro:\s*){re.escape(old)}\b")
        new_text, n = pattern.subn(lambda m: f"{m.group(1)}{new}", text)
        if n < 1:
            raise TemplateError(
                f"board.yaml `pins:` has no `macro: {old}` entry to "
                f"re-derive to {new!r}")
        text = new_text
    return text


def _substitute_board_yaml_pin_docs(text: str, renames: dict[str, str | None]) -> str:
    """Companion to `_substitute_board_yaml_pins`: rewrite (or drop) a
    `pins:` entry's `doc:` field per `_derive_pin_doc_renames`'s map
    (issue #876 review MAJOR 2) -- `doc:` only ever appears in the
    dict form. A `None` value means the target route has no `doc:` of
    its own; the loader falls back to the resolved board's own `doc:`
    in that case, so the field is dropped entirely rather than left
    describing the wrong board."""
    for old_doc, new_doc in renames.items():
        old_quoted = re.escape(f'"{old_doc}"')
        if new_doc is not None:
            pattern = re.compile(rf"(doc:\s*){old_quoted}")
            new_text, n = pattern.subn(lambda m: f'{m.group(1)}"{new_doc}"', text)
        else:
            pattern = re.compile(rf",\s*doc:\s*{old_quoted}")
            new_text, n = pattern.subn("", text)
        if n < 1:
            raise TemplateError(
                f'board.yaml `pins:` has no `doc: "{old_doc}"` entry to '
                f"re-derive")
        text = new_text
    return text


def _substitute_cmake_core(text: str, old: str, new: str) -> str:
    """Rewrite CMakeLists.txt's `alp_sdk_zephyr_conf(<old> ...)` core
    argument to the re-derived core id. Still accepts the pre-helper
    `alp_project.py --emit zephyr-conf --core <old>` spelling -- the
    only one any real example carries today, since the shared
    `cmake/alp.cmake` helper that would define `alp_sdk_zephyr_conf()`
    is itself PLANNED and unmerged (tan-cli#825) -- so an example
    re-derives on that spelling rather than scaffolding the wrong
    core."""
    pattern = re.compile(
        rf"(alp_sdk_zephyr_conf\(\s*|--core\s+){re.escape(old)}\b")
    new_text, n = pattern.subn(lambda m: f"{m.group(1)}{new}", text)
    if n != 1:
        raise TemplateError(
            f"CMakeLists.txt must name core {old!r} exactly once (as "
            f"`alp_sdk_zephyr_conf({old} ...)` or `--core {old}`) to "
            f"re-derive to {new!r} (found {n})")
    return new_text


# ---------------------------------------------------------------------
# --emit scaffold content adaptation (issue #864 follow-up)
# ---------------------------------------------------------------------
#
# Every catalog template's user_owned files are the SDK's own example,
# verbatim -- correct for render()'s documented byte-for-byte contract
# (validate()'s in-tree twister self-test relies on exactly that), but
# wrong for a scaffold a customer unpacks OUTSIDE the SDK tree: a
# `west build ... examples/<...>` argument naming a path that doesn't
# exist in their project, `../`-relative links that only resolve
# inside the SDK checkout, and a CMakeLists.txt that silently guesses
# `../../..` for ALP_SDK_ROOT (correct only for the in-tree example,
# never a copied-out scaffold -- the retired tan-cli generator hard-
# failed on exactly this: "ALP_SDK_ROOT is not set"). These transforms
# run ONLY in render_to_envelope() (the `--emit scaffold` path, for
# EVERY sku including the canonical example's own) -- render()/
# validate() stay byte-for-byte faithful to the real example, since
# that's what validate()'s temp-dir twister run is proving builds.

_ALP_SDK_ROOT_GUESS_RE = re.compile(
    r"if\(DEFINED ENV\{ALP_SDK_ROOT\}\)\n"
    r"    set\(ALP_SDK_ROOT \$ENV\{ALP_SDK_ROOT\}\)\n"
    r"else\(\)\n"
    r"    get_filename_component\(ALP_SDK_ROOT \$\{CMAKE_CURRENT_SOURCE_DIR\}(?:/\.\.)+ ABSOLUTE\)\n"
    r"endif\(\)"
)
# Was `cold-chain-monitor`'s own shape until alp-sdk#1400 converted it to the
# guess shape above: no ALP_SDK_ROOT resolution at all, just a hardcoded
# in-tree-relative path straight to `alp_project.py` (worse than the guess --
# no override was even possible). No example carries it at the pinned SDK
# commit any more; kept as a defensive branch, not a live path.
_HARDCODED_ALP_PROJECT_PY_RE = re.compile(
    r"\$\{CMAKE_CURRENT_SOURCE_DIR\}(?:/\.\.)+/scripts/alp_project\.py"
)
# Anything that only resolves against a real alp-sdk checkout, i.e. that a
# scaffold copied OUT of the SDK tree cannot satisfy unless ALP_SDK_ROOT has
# been rewritten into a hard requirement: the shared `cmake/alp.cmake`
# include, either helper it would define, or a direct `alp_project.py` shell
# (the `cmake/alp.cmake` include and its helpers are PLANNED and unmerged,
# tan-cli#825 -- `alp_project.py` is the only spelling any real example
# carries today; the other two are matched so this stays correct once that
# helper ships).
_SDK_ROOT_DEPENDENT_RE = re.compile(
    r"cmake/alp\.cmake|alp_sdk_zephyr_conf|alp_sdk_ipc_contract_header"
    r"|alp_project\.py")
_ALP_SDK_ROOT_REQUIRED_BLOCK = (
    # Issue #864 Fable-review MAJOR E: the ORIGINAL block here checked
    # only `ENV{ALP_SDK_ROOT}` while the message also advertised
    # `-DALP_SDK_ROOT=...` -- a customer passing ONLY the -D cache
    # variable still hit the FATAL_ERROR (ENV{} was never set), and
    # even a customer setting BOTH had the -D value silently clobbered
    # by `set(ALP_SDK_ROOT $ENV{ALP_SDK_ROOT})`. Check + prefer
    # whichever is actually DEFINED; only fall back to the env var when
    # the cache variable itself isn't set.
    "if(NOT DEFINED ALP_SDK_ROOT AND NOT DEFINED ENV{ALP_SDK_ROOT})\n"
    "    message(FATAL_ERROR\n"
    "        \"ALP_SDK_ROOT is not set -- point it at your alp-sdk checkout, \"\n"
    "        \"e.g. `export ALP_SDK_ROOT=/path/to/alp-sdk` or "
    "`-DALP_SDK_ROOT=/path/to/alp-sdk`.\")\n"
    "endif()\n"
    "if(NOT DEFINED ALP_SDK_ROOT)\n"
    "    set(ALP_SDK_ROOT $ENV{ALP_SDK_ROOT})\n"
    "endif()"
)

# The guess block does not stand alone: most examples introduce it with
# a comment paragraph that TEACHES the in-tree `../../..` fallback --
# hello-world/cold-chain-monitor's "In-tree the SDK is the example's
# grandparent directory; out-of-tree customers point ALP_SDK_ROOT at
# their checkout", gpio-button-led's "in-tree we resolve it as the
# example's grandparent directory". Substituting only the code left
# that prose above a block that has NO fallback and hard-fails instead,
# so the emitted scaffold documented behaviour it did not have. Rewrite
# the paragraph with the code it describes.
_STALE_SDK_ROOT_PROSE_RE = re.compile(r"ALP_SDK_ROOT|grandparent", re.IGNORECASE)
_ALP_SDK_ROOT_ACCURATE_COMMENT = (
    "# Resolve the alp-sdk root.  This project lives OUTSIDE the SDK\n"
    "# tree, so there is nothing to guess: ALP_SDK_ROOT must name your\n"
    "# alp-sdk checkout, set in the environment or passed as\n"
    "# `-DALP_SDK_ROOT=/path/to/alp-sdk`."
)


def _rewrite_stale_sdk_root_comment(head: str) -> str:
    """Rewrite the comment paragraph introducing the ALP_SDK_ROOT block.

    `head` is everything in the CMakeLists.txt BEFORE the guess block.
    Its trailing run of `#` lines (optionally separated from the block
    by blank lines) is that block's prose. The run is split into
    paragraphs on bare `#` separator lines, and the first paragraph
    naming `ALP_SDK_ROOT` or the grandparent fallback is replaced with
    `_ALP_SDK_ROOT_ACCURATE_COMMENT`; any further matching paragraph is
    dropped rather than duplicating it. Paragraphs about anything else
    are kept verbatim -- gpio-button-led's run leads with a "board.yaml
    -> build/generated/alp.conf at configure time." banner that stays
    true. A file whose block has no comment run above it (i2c-master,
    mproc-mailbox) is returned unchanged.
    """
    lines = head.split("\n")
    i = len(lines) - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    end = i + 1
    while i >= 0 and lines[i].lstrip().startswith("#"):
        i -= 1
    start = i + 1
    if start >= end:
        return head

    out: list[str] = []
    para: list[str] = []
    replaced = False

    def _flush() -> None:
        nonlocal replaced
        if not para:
            return
        if _STALE_SDK_ROOT_PROSE_RE.search("\n".join(para)):
            if not replaced:
                out.extend(_ALP_SDK_ROOT_ACCURATE_COMMENT.split("\n"))
                replaced = True
        else:
            out.extend(para)
        para.clear()

    for line in lines[start:end]:
        if line.strip() == "#":
            _flush()
            out.append(line)
        else:
            para.append(line)
    _flush()
    lines[start:end] = out
    return "\n".join(lines)


def _scaffold_cmakelists(text: str) -> str:
    """Replace an in-tree-relative ALP_SDK_ROOT guess with a hard
    requirement, and rewrite the comment paragraph that describes it.

    One shape is live across the catalog's example CMakeLists.txt files
    today: the `if(DEFINED ENV{ALP_SDK_ROOT}) ... else()
    get_filename_component(...)` guess most examples carry immediately
    above a direct `execute_process(... scripts/alp_project.py ...)`
    call (PLANNED to become `include(${ALP_SDK_ROOT}/cmake/alp.cmake)`
    once that helper merges -- unmerged, tan-cli#825). A second,
    defensive-only shape is also matched -- see `_HARDCODED_ALP_PROJECT_PY_RE`
    above for why it is no longer live and why the branch stays anyway. The
    guess shape resolves only for the in-tree example; a scaffold a customer
    unpacks elsewhere needs the value supplied, so it becomes a
    FATAL_ERROR-if-unset block -- the guess shape's `include()` line
    already names `${ALP_SDK_ROOT}` and needs no further rewriting; the
    (defensive-only) hardcoded shape's path would be rewritten to
    `${ALP_SDK_ROOT}/scripts/alp_project.py` alongside inserting the
    block if it were ever matched.

    Each guess-block hit is substituted through a loop rather than
    `subn`: the block's own preceding comment run has to be rewritten
    with it (`_rewrite_stale_sdk_root_comment`, alp-sdk#1390), and the
    replacement block is not itself a guess block, so the next `search`
    cannot re-find what was just substituted.

    A CMakeLists.txt with no SDK-root-dependent line at all (e.g.
    multicore-rpmsg's `linux/CMakeLists.txt`) is legitimately returned
    unchanged. One that DOES depend on the SDK root but carries an
    unrecognised resolution shape raises: this used to be a silent
    best-effort no-op, which shipped every scaffolded project an
    `include()`/`alp_project.py` path that resolves only inside an SDK
    checkout -- broken on the very first thing a new customer does, with
    nothing failing here to say so."""
    pos, hit = 0, False
    while True:
        m = _ALP_SDK_ROOT_GUESS_RE.search(text, pos)
        if not m:
            break
        hit = True
        head = _rewrite_stale_sdk_root_comment(text[: m.start()])
        text = head + _ALP_SDK_ROOT_REQUIRED_BLOCK + text[m.end():]
        pos = len(head) + len(_ALP_SDK_ROOT_REQUIRED_BLOCK)
    if hit:
        return text
    if _ALP_SDK_ROOT_REQUIRED_BLOCK in text:
        return text  # already hardened (idempotent)
    if _HARDCODED_ALP_PROJECT_PY_RE.search(text):
        text = _HARDCODED_ALP_PROJECT_PY_RE.sub(
            "${ALP_SDK_ROOT}/scripts/alp_project.py", text)
        return text.replace(
            "execute_process(\n",
            _ALP_SDK_ROOT_REQUIRED_BLOCK + "\n\nexecute_process(\n", 1)
    dependent = _SDK_ROOT_DEPENDENT_RE.search(text)
    if dependent:
        raise TemplateError(
            f"CMakeLists.txt depends on the SDK root (`{dependent.group(0)}`) "
            f"but carries no recognised ALP_SDK_ROOT resolution block to "
            f"rewrite into a hard requirement -- a scaffold of it would ship "
            f"a path that only resolves inside an alp-sdk checkout. Use the "
            f"`if(DEFINED ENV{{ALP_SDK_ROOT}}) ... else() "
            f"get_filename_component(...) endif()` shape the other examples "
            f"use, or teach `_scaffold_cmakelists` the new one.")
    return text


_RELATIVE_LINK_RE = re.compile(r"\]\((\.\./[^)\s]+)\)")

# alp-sdk#1855: `_RELATIVE_LINK_RE` above only rewrites a MARKDOWN-style
# `](../docs/x.md)` link, and only inside README.md (the one file
# `_scaffold_readme` runs on). A board.yaml/src/main.c comment names the
# same kind of alp-sdk-tree-only path in prose instead -- no `[...](...)`
# around it at all -- so it never matches and survives a scaffold
# verbatim (e.g. i2c-master's board.yaml/src/main.c both say "see
# examples/v2n/v2n-temp-sensor for ..." with no disclaimer that it isn't
# part of this scaffolded project, unlike the DELIBERATELY-disclaimed
# i2c-scanner mention two paragraphs above it in the same file). Narrow
# on purpose: only the two prefixes actually found bare like this
# (`docs/*.md`, `examples/<category>/<name>[/<subpath>]`) -- a
# `scripts/`/`metadata/` mention is left alone, since every such mention
# in the catalog today is either already `${ALP_SDK_ROOT}`-qualified in
# a CMakeLists.txt or purely descriptive prose that names a script/file
# by convention rather than telling the customer to open it.
_BARE_REPO_PATH_RE = re.compile(
    r"\b(?:docs/[\w./-]+\.md|examples/[\w-]+/[\w-]+(?:/[\w./-]+)?)\b")


def _scaffold_bare_repo_paths(text: str, docs_ref: str) -> str:
    """Rewrite a bare `docs/*.md` or `examples/<category>/<name>
    [/<subpath>]` mention into the same absolute GitHub URL form
    `_scaffold_readme`'s `_fix_link` gives a markdown-style link --
    see `_BARE_REPO_PATH_RE`'s comment for why this exists as a
    SEPARATE pass rather than widening that one. Best-effort / no-op
    when neither prefix appears."""
    def _sub(m: re.Match[str]) -> str:
        target = m.group(0)
        kind = "blob" if "." in target.rsplit("/", 1)[-1] else "tree"
        return f"https://github.com/alplabai/alp-sdk/{kind}/{docs_ref}/{target}"

    return _BARE_REPO_PATH_RE.sub(_sub, text)


def _tag_resolves(base_dir: Path, tag: str) -> bool:
    """Whether `tag` exists in `base_dir`'s git checkout.

    Local-only: `git rev-parse` against the checkout's own refs, never a
    network call -- scaffolding must work offline, and a scaffold that
    stalled on `git ls-remote` would be a worse defect than the dead link
    this guards. A checkout that fetched from origin has origin's tags, so
    "resolves here" is the closest offline proxy for "resolves on GitHub"
    available, and every way it can be wrong (no git binary, tarball
    export, `--no-tags` clone, shallow CI checkout) fails the same
    direction: no tag found, pin to `main`, links stay live.

    Ported verbatim from alp-sdk `scripts/alp_template.py::_tag_resolves`
    (issue #1508 / alp-sdk#1535), except for one RELOCATED divergence:
    `env=spawn_env()` below is a tan-only addition (tan-cli#992) -- alp-sdk's
    own copy never restores it because alp-sdk never ships as a frozen
    PyInstaller bundle whose LD_LIBRARY_PATH would otherwise leak into the
    spawned `git`."""
    try:
        return subprocess.run(
            ["git", "-C", str(base_dir), "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
            capture_output=True,
            env=spawn_env(),
            check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):  # no git binary, not a repo
        return False


def _docs_ref(base_dir: Path) -> str:
    """The GitHub ref a scaffolded README's doc links should pin to
    (issue #864 Fable-review MINOR H): `metadata/sdk_version.yaml`'s
    own `v<version>` tag when `status: released` (a released checkout's
    docs are stable at that tag; linking `main` could point at docs
    that have since changed or moved), else `main` -- an unreleased/
    development checkout has no matching tag yet to pin to.

    The tag has to RESOLVE, not merely be declared (tan-cli#846, porting
    alp-sdk#1535). Between an rc cut and its GA tag `sdk_version.yaml`
    says `version: 0.16.0` / `status: released` while only
    `v0.16.0-rc1` exists on the bound checkout -- branching on the
    declared pair alone put a dead
    `https://github.com/alplabai/alp-sdk/blob/v0.16.0/docs/...` link in
    every project scaffolded in that window. A missing tag degrades to
    `main` instead of shipping a 404.

    A `sdk_version.yaml` that parses but is not itself a mapping (e.g.
    a bare list) degrades to `main` the same way a missing file does,
    rather than raising: this is a README doc-link decision, not a
    fatal `--emit scaffold` input, so a malformed version file should
    cost a stale-but-safe link, not the whole scaffold (tan-cli#1037 --
    a PR #1034 round-two review nit had described this read as safe on
    the reasoning that `yaml.safe_load(...) or {}` only ever produces a
    dict-or-`{}` result; a non-empty bare-list/scalar document is
    neither, and reached `doc.get(...)` bare, `AttributeError: 'list'
    object has no attribute 'get'`).

    tan-cli#1116 review round 2: `UnicodeDecodeError` and `yaml.YAMLError`
    are caught alongside `OSError` -- neither is an `OSError`, so a non-UTF-8
    or syntactically-invalid `sdk_version.yaml` used to raise raw past this
    same "cost a stale-but-safe link, not the whole scaffold" contract
    (both measured escaping before this fix)."""
    try:
        doc = yaml.safe_load(
            (base_dir / "metadata" / "sdk_version.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return "main"
    if not isinstance(doc, dict):
        return "main"
    version = doc.get("version")
    if doc.get("status") == "released" and version and _tag_resolves(base_dir, f"v{version}"):
        return f"v{version}"
    return "main"


def _substitute_readme_pins(text: str, renames: dict[str, str]) -> str:
    """Rewrite a scaffolded README's `ALP_<old_pad>` mentions to
    `ALP_<new_pad>` for every renamed pin (issue #876 review MINOR 4)
    -- e.g. gpio-button-led's README teaches `ALP_E1M_GPIO_IO4` as THE
    button pin, which becomes actively wrong prose once the pad itself
    has changed for a cross-family sku.

    Paragraph-scoped (split on blank lines): a paragraph that ALREADY
    mentions BOTH the old and the new `ALP_<pad>` form (e.g. i2c-
    master's "resolves to `ALP_E1M_I2C0` on the E1M EVK and
    `ALP_E1M_X_I2C0` on the E1M-X EVK" cross-EVK teaching sentence) is
    left alone -- it's correct, portable prose about the alias
    mechanism itself, not a stale claim about which pad THIS scaffold
    uses, and blindly substituting would turn it into a duplicate,
    factually wrong statement ("... on the E1M EVK" would then name
    the E1M-X pad).

    A `` `ALP_<old_pad>` (index N) `` parenthetical (e.g. gpio-button-
    led's "`ALP_E1M_GPIO_PWM0` (index 26)") names N per `old_pad`'s
    OWN family's `ALP_E1M_GPIO_<class><N>` numbering
    (`include/alp/e1m_pinout.h`'s canonical IO0..25 = 0..25, PWM0..7 =
    26..33 order) -- a DIFFERENT numbering on a cross-family target
    (E1M-X's `include/alp/e1m_x_pinout.h` has 36 IOs, so its PWM0..7
    sits at 36..43, not the source family's index at all). The route
    data available here (`metadata/boards/*.yaml` `e1m_routes:`
    entries: `e1m`/`macro`/`board_alias`/`doc`, no index column) can't
    re-derive `new_pad`'s own index, so rather than carry the stale
    source-family number forward as if it were true of the target,
    drop the parenthetical along with the pad it was describing."""
    if not renames:
        return text
    paragraphs = text.split("\n\n")
    for i, para in enumerate(paragraphs):
        changed = para
        for old, new in renames.items():
            old_tok, new_tok = f"ALP_{old}", f"ALP_{new}"
            if old_tok in para and new_tok in para:
                continue  # already-correct dual-EVK teaching prose
            changed = re.sub(
                rf"`{re.escape(old_tok)}`(?:\s*\(index\s+\d+\))?",
                f"`{new_tok}`", changed)
            changed = re.sub(rf"\b{re.escape(old_tok)}\b", new_tok, changed)
        paragraphs[i] = changed
    return "\n\n".join(paragraphs)


def _scaffold_readme(
    text: str,
    example_path: str,
    docs_ref: str,
    example_sku: str = "",
    sku: str = "",
    source_board: str | None = None,
    target_board: str | None = None,
    pin_renames: dict[str, str] | None = None,
) -> str:
    """Every vendored README's `../`-relative links (`../../../docs/
    x.md`, a sibling example's `../i2c-scanner/`, ...) resolve against
    the CANONICAL example's OWN position inside the alp-sdk tree --
    dangling once copied out as a standalone scaffold. Rewrite each to
    an absolute GitHub URL (pinned to `docs_ref` -- see `_docs_ref`)
    instead. Also rewrites the one non-existent-once-copied-out token
    every Build section carries: a `west build ...` invocation naming
    THIS template's own repo-relative example path -- the scaffold IS
    the project root wherever the customer unpacks it, so that argument
    becomes `.`. Best-effort (neither pattern found -> text returned
    unchanged); per-template narrative prose (e.g. `tan build
    alp-sdk/examples/...` invocations, cross-references phrased as
    prose rather than a link) is intentionally not scaffold-normalised
    by this pass.

    Two more issue #864 Fable-review fixes, both applied unconditionally
    (best-effort, no-op when the pattern is absent):

    * MAJOR B -- `-DEXTRA_ZEPHYR_MODULES=$(pwd)` only registers the
      alp-sdk checkout as a Zephyr module when `$(pwd)` IS that
      checkout (true in-tree); in a copied-out scaffold `$(pwd)` is the
      SCAFFOLD dir, so the module never registers and the documented
      `west build` fails (`CONFIG_ALP_*` unset, `<alp/*.h>`
      unresolvable). Rewritten to `$ALP_SDK_ROOT`, the same var the
      hardened CMakeLists.txt now requires (`_scaffold_cmakelists`).

    * MAJOR C -- the canonical example's own SoM label ("# Example for
      E1M-AEN801:") and qualified Zephyr board target
      (`alp_e1m_aen801_m55_hp/ae822fa0e5597ls0/rtss_hp`) otherwise
      survive a cross-family sku swap untouched (a V2N101 scaffold
      shipping `-b alp_e1m_aen801_m55_hp/...`; the real
      `alp_e1m_v2n101_m33_sm/r9a09g056n48gbg/cm33` appears nowhere).
      `source_board`/`target_board` are the qualified board id
      (`_core_board`) for the example's own sku / the requested sku's
      re-derived app core respectively. Every source README carries
      the full `/<soc>/<core>` suffix (issue #720), so the exact
      qualified `source_board` string is matched first, consuming that
      suffix along with the short prefix; a SHORT board-id-prefix
      (before the first `/`) word-boundary match then ALSO runs
      unconditionally, for any remaining bare mention that names only
      the board directory (no soc/core), e.g. a `zephyr/boards/alp/
      <board>/` doc link -- a README carrying both shapes gets both
      rewritten, not just whichever one matches first.

    * `_m33_sm` (RZ/V2N system-manager) scaffold targets -- that board
      family's DEFAULT flasher is `rzv2n_mtd_flash`
      (zephyr/boards/alp/e1m_v2n101_m33_sm/board.cmake,
      e1m_v2m101_m33_sm/board.cmake), which is SSH-to-the-booted-A55
      and always needs `--host`/`ALP_V2N_SSH_HOST` -- a bare `west
      flash` carried over verbatim from an AEN801 (JLink) source
      README silently can't reach the board. Every `west flash` line
      immediately following one of THIS scaffold's own board-target
      lines is rewritten to `west flash --host <board-ip>`; an
      unrelated `west flash` elsewhere in the prose is left alone.

    `pin_renames` (issue #876 review MINOR 4) is `_derive_pin_renames`'s
    map -- see `_substitute_readme_pins`.
    """
    def _fix_link(m: re.Match[str]) -> str:
        target = posixpath.normpath(f"{example_path}/{m.group(1)}")
        kind = "blob" if "." in target.rsplit("/", 1)[-1] else "tree"
        return f"](https://github.com/alplabai/alp-sdk/{kind}/{docs_ref}/{target})"

    text = _RELATIVE_LINK_RE.sub(_fix_link, text)
    # A bare (non-link) mention of the example's OWN path -- e.g. a
    # `west build -b <board> <example_path>` argument -- becomes `.`
    # (the scaffold IS the project root wherever it lands). alp-sdk#1855:
    # a MULTI-slice template's README also names a bare SUBPATH of its
    # own example dir this way (mproc-mailbox's `west build -b
    # <board> examples/multicore/mproc-mailbox/peer`, for the HE-side
    # peer/ core) -- the plain `example_path` match above never fires
    # for that (its `(?!\S)` boundary fails on the following `/peer`),
    # so the `/peer` suffix survived verbatim, naming a path that
    # exists only inside the alp-sdk tree. Capture + keep any trailing
    # `/<subpath>` so it becomes `./<subpath>` instead of vanishing.
    text = re.sub(
        rf"(?<!\S){re.escape(example_path)}(/\S+)?(?!\S)",
        lambda m: "." + (m.group(1) or ""), text)
    text = text.replace(
        "-DEXTRA_ZEPHYR_MODULES=$(pwd)", "-DEXTRA_ZEPHYR_MODULES=$ALP_SDK_ROOT")
    if source_board and target_board:
        # Every source README carries the full `/<soc>/<core>` suffix
        # (issue #720), so match the exact qualified string first --
        # its `/<soc>/<core>` suffix is consumed along with the short
        # prefix, avoiding the OLD soc/core suffix being left dangling
        # after the NEW (already fully qualified) `target_board`, e.g.
        # `alp_e1m_v2n101_m33_sm/r9a09g056n48gbg/cm33/ae822fa0e5597ls0/rtss_hp`.
        # The short board-id-prefix (before the first `/`) word-
        # boundary match then ALSO runs, unconditionally -- not only
        # as a fallback when the qualified string is absent -- so a
        # README naming the board BOTH ways (a qualified `west build`
        # line and a separate bare `zephyr/boards/alp/<board>/` doc
        # link) gets both rewritten. `(?!/)` keeps it from re-matching
        # the prefix of a string that's ALREADY (still) fully
        # qualified -- either one this same call just substituted in
        # (leaving `target_board` intact) or, in the sku==example_sku
        # passthrough case, `source_board` itself, still present
        # verbatim after the no-op `replace` above -- which would
        # otherwise get its own `/<soc>/<core>` suffix duplicated onto
        # the end a second time.
        if source_board in text:
            text = text.replace(source_board, target_board)
        source_marker = source_board.split("/", 1)[0]
        text = re.sub(rf"\b{re.escape(source_marker)}\b(?!/)", target_board, text)
        # The `_m33_sm` (RZ/V2N system-manager) board family's DEFAULT
        # flasher is `rzv2n_mtd_flash` (zephyr/boards/alp/
        # e1m_v2n101_m33_sm/board.cmake, e1m_v2m101_m33_sm/board.cmake),
        # which is SSH-to-the-booted-A55 and always needs `--host`/
        # `ALP_V2N_SSH_HOST` -- a bare `west flash` carried over
        # verbatim from an AEN801 (JLink) source README silently can't
        # reach the board. Every `west flash` line immediately
        # following one of THIS scaffold's own board-target lines is
        # rewritten (a multi-core README can carry more than one), so
        # a two-core scaffold doesn't leave its second flash line
        # bare; an unrelated `west flash` elsewhere in the prose is
        # left alone.
        if target_board.split("/", 1)[0].endswith("_m33_sm"):
            marker = re.escape(target_board)
            text = re.sub(
                rf"({marker}[^\n]*\n)west flash\b",
                r"\1west flash --host <board-ip>",
                text)
    if example_sku and sku and example_sku != sku:
        text = text.replace(example_sku, sku)
    text = _substitute_readme_pins(text, pin_renames or {})
    return text
