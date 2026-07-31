# SPDX-License-Identifier: Apache-2.0
"""`tan examples` -- the SDK's ready-made example projects, as a catalogue the
New Project flow scaffolds from via `tan init --from-example <sourceDir>`.

Mirrors `crates/tan-cli/src/commands/examples.rs` plus the two `tan-core`
helpers it composes (`wizard/filesystem.rs::discover_examples`,
`wizard/service/example_catalog.rs`). Kept in ONE module rather than split
across a `tan.core` twin: the whole thing is a directory walk and two README
scans, and a second file would only add an import.

**Which rule decides where the catalogue comes from.** The example list is
alp-sdk content, so the two candidate readings are "vendor a capture like
`tan init` does" and "read the resolved checkout". This reads the checkout, on
two invariants from
`alp-sdk/docs/superpowers/specs/2026-07-29-tan-port-invariants.md`:

* **I-32** bans *shelling* the SDK -- `tan init`/`tan scaffold` read a vendored
  `--emit scaffold` tree "without ever shelling the SDK" because they must work
  with NO checkout present at all. Nothing here spawns a subprocess; a
  `read_dir` over `<sdk>/examples` is not a shell-out, so I-32 is untouched. Note
  the anti-pattern it names (#22 in that doc) is *shelling* init instead of
  reading the vendored tree -- not "reading a checkout".
* **I-26** ("tan must never learn a hardware fact ... `metadata/**` stays in
  alp-sdk") is what forbids the vendored alternative HERE. A baked example
  catalogue is a copy of alp-sdk content that drifts from the customer's actual
  checkout, and every `sourceDir` it hands back is a path
  `tan init --from-example` then cannot find. `tan examples`' requirement is the
  inverse of `tan init`'s: with no checkout resolvable the catalogue is
  legitimately EMPTY (exit 0, `examples: []`), which is exactly what the Rust
  oracle does -- so it has no reason to carry a capture.

**Every failure is an envelope, never a traceback.** Discovery swallows the IO
errors the Rust swallows (an unreadable root or category is skipped, a README
that will not read or is not UTF-8 is treated as absent -- `read_to_string().ok()`
on the Rust side), and the catch-all in `examples` is the backstop for whatever
nobody enumerated. There is no subprocess to time out: this command spawns
nothing.

No `--os`, no `--backend`, no SKU list, no hardware facts -- the catalogue is
whatever directories carry a `board.yaml`, and nothing here reads one.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from tan.commands.build_cmd import resolve_sdk_root_ladder
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

#: First bytes that mark a README line as markdown STRUCTURE rather than prose.
#: Verbatim from `is_readme_prose` (`wizard/service/example_catalog.rs`) --
#: headings, blockquotes, HTML, links, badges, tables, list/rule markers, code.
_NON_PROSE_PREFIXES = "#><[!|-*+=`"


@dataclass(frozen=True)
class Example:
    """One catalogue entry. Field order is the emitted JSON order, matching
    `ExampleEntry`'s declaration order in `examples.rs` (`id`, `sourceDir`,
    `title`, `description`)."""

    id: str
    source_dir: str
    title: str
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "sourceDir": self.source_dir,
            "title": self.title,
            "description": self.description,
        }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _read_readme(example_dir: Path) -> str | None:
    """A `README.md`'s text, or `None` when there is not a usable one.

    Missing, unreadable, and non-UTF-8 all collapse to `None`, matching Rust's
    `std::fs::read_to_string(...).ok()`. `encoding="utf-8"` is explicit (I-27):
    the default would decode with the host locale, so a README carrying a `⚠️`
    -- alp-sdk already ships several -- raises `UnicodeDecodeError` on a cp1252
    Windows host and passes on ubuntu CI.
    """
    try:
        return (example_dir / "README.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _subdirectories(root: Path) -> list[Path]:
    """`root`'s immediate subdirectories, or `[]` when it will not read.

    `follow_symlinks=False` deliberately: Rust's `DirEntry::file_type()` does
    not traverse a symlink, so a symlinked example directory is not a directory
    to the oracle and is skipped. (The `board.yaml` probe below DOES follow, as
    `Path::is_file()` does.) Order is not relied on -- results are sorted.
    """
    try:
        with os.scandir(root) as entries:
            return [
                Path(entry.path)
                for entry in entries
                if _is_dir_no_follow(entry)
            ]
    except OSError:
        return []


def _is_dir_no_follow(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def discover_examples(examples_root: Path) -> list[Example]:
    """Scan `<sdk>/examples` exactly two levels deep for `<category>/<name>`
    directories carrying a `board.yaml`, sorted by source dir.

    A missing or unreadable root is an EMPTY list, never an error: "no SDK" and
    "no examples" are the same renderable answer, and `tan examples` returning
    exit 0 with `examples: []` is what lets the New Project flow say "no
    examples available" instead of showing a failure.
    """
    found: list[Example] = []
    for category in _subdirectories(examples_root):
        for project in _subdirectories(category):
            try:
                has_board = (project / "board.yaml").is_file()
            except OSError:
                has_board = False
            if not has_board:
                continue
            # Always `/`-joined: `sourceDir` is handed straight back to
            # `tan init --from-example`, and a Windows `\` there is not a path
            # the SDK examples tree can be indexed by.
            source_dir = f"{category.name}/{project.name}"
            readme = _read_readme(project)
            found.append(
                Example(
                    id=example_id_from_source_dir(source_dir),
                    source_dir=source_dir,
                    title=example_title_from_readme(readme, source_dir),
                    description=example_description_from_readme(readme),
                )
            )
    found.sort(key=lambda e: e.source_dir)
    return found


# ---------------------------------------------------------------------------
# README-derived metadata (pure)
# ---------------------------------------------------------------------------


def example_id_from_source_dir(source_dir: str) -> str:
    """The source dir is already unique across the catalogue, so it doubles as
    the id. Kept as a named function because `id == sourceDir` is a contract the
    extension may not assume forever, not an accident to inline away."""
    return source_dir


def example_title_from_readme(readme: str | None, source_dir: str) -> str:
    """The README's first markdown heading, else a title-cased leaf name."""
    for line in (readme or "").splitlines():
        trimmed = line.strip()
        if trimmed.startswith("#"):
            title = trimmed.lstrip("#").strip()
            if title:
                return title
    return _title_case_leaf(source_dir)


def example_description_from_readme(readme: str | None) -> str:
    """The README's first prose line -- skipping blanks, headings, blockquotes,
    badges/links, HTML, tables, list markers and rules. `""` when there is
    none."""
    for line in (readme or "").splitlines():
        trimmed = line.strip()
        if trimmed and trimmed[0] not in _NON_PROSE_PREFIXES:
            return trimmed
    return ""


def _title_case_leaf(source_dir: str) -> str:
    """`audio/i2s-tone` -> `I2s Tone`: split the leaf on `-`/`_`, upper-case each
    word's first character only, and leave the rest of the word as found (so it
    really is `I2s`, not `I2S` -- the fallback is deliberately dumb, a README
    heading is how an example spells its own name)."""
    leaf = source_dir.rsplit("/", 1)[-1]
    words = [w for w in leaf.replace("_", "-").split("-") if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


def strip_markdown_noise(text: str) -> str:
    """Drop `![alt](url)` image/badge spans and `**` emphasis markers.

    TEXT MODE ONLY -- `data.examples[]` keeps the raw README-derived value, so
    the JSON a consumer parses is unaffected. A `![` with no closing `)` is not
    markdown: keep it verbatim rather than eating the rest of the line.
    """
    out: list[str] = []
    rest = text
    while True:
        start = rest.find("![")
        if start == -1:
            break
        out.append(rest[:start])
        end = rest.find(")", start)
        if end == -1:
            out.append(rest[start:])
            rest = ""
            break
        rest = rest[end + 1 :]
    out.append(rest)
    return "".join(out).replace("**", "").strip()


# ---------------------------------------------------------------------------
# Filtering and text rendering
# ---------------------------------------------------------------------------


def example_matches_filter(entry: Example, needle: str) -> bool:
    """Case-insensitive substring of `id` or `title`. Deliberately NOT
    `sourceDir`/`description`: the former is the same string as `id`, the latter
    is too free-form for a quick catalogue scan to key on."""
    lowered = needle.lower()
    return lowered in entry.id.lower() or lowered in entry.title.lower()


def render_examples_text(
    examples: list[Example], filter_: str | None, verbose: bool
) -> list[str]:
    """One `id  title` line per example, `id` column aligned to the longest id in
    THIS (possibly filtered) result set; `--verbose` appends the description.

    Regression (tan-cli#164): this used to print only the count line, never the
    entries `--from-example` needs a source dir from.
    """
    if not examples:
        if filter_ is not None:
            # `ensure_ascii=False` so a non-ASCII filter is echoed as typed,
            # which is the closest one-liner to Rust's `{:?}` quoting.
            return [
                f"examples: no example projects match --filter "
                f"{json.dumps(filter_, ensure_ascii=False)}."
            ]
        return [
            "examples: no example projects found (is the alp-sdk checkout "
            "resolvable? use --sdk-root)."
        ]
    id_width = max(len(e.id) for e in examples)
    lines = [f"examples: {len(examples)} example project(s) available"]
    for e in examples:
        line = f"  {strip_markdown_noise(e.id):<{id_width}}  {strip_markdown_noise(e.title)}"
        if verbose:
            description = strip_markdown_noise(e.description)
            if description:
                line += f"   -- {description}"
        lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _resolve_sdk(sdk_root: str | None, workspace_root: Path) -> tuple[Path, str, str] | None:
    """`(path, display, sourceTier)` for the resolved checkout, or `None`.

    `--sdk-root` is TERMINAL (I-31): a value that is not a real checkout resolves
    to nothing rather than falling through to discovery, so a typo'd flag yields
    an empty catalogue instead of silently listing some other checkout's
    examples.

    `display` is separate from `path` because they are not interchangeable:
    `Path("./sdk")` stringifies to `sdk`, dropping the `./` the caller typed,
    while Rust records `--sdk-root` as-is -- and `sdk.root` is on the wire.
    """
    if sdk_root:
        path = Path(sdk_root)
        if (path / "scripts" / "alp_project.py").is_file():
            return path, sdk_root, "sdkRootFlag"
        return None
    # `.alp/sdk-path` project pin > the machine-global default
    # (`~/.alp/sdk-default`) > the positional walk (`resolve_sdk_root_ladder`)
    # -- previously this went straight to the positional walk, silently
    # ignoring `tan init`'s pin. No `ALP_SDK_ROOT` tier (tried and reverted --
    # see `resolve_sdk_root_ladder`'s own docstring).
    found, tier = resolve_sdk_root_ladder(None, workspace_root)
    if found is None:
        return None
    return found, str(found).replace("\\", "/"), tier


def examples(
    filter_: str = typer.Option(
        None, "--filter", metavar="TEXT", help="Only examples whose id or title contains TEXT."
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Append each example's description."),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    sdk_root: str = typer.Option(None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."),
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """List the SDK's ready-made example projects."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    sdk: tuple[Path, str, str] | None = None
    found: list[Example] = []
    issues: list[Issue] = []
    exit_code = ExitCode.SUCCESS
    try:
        # Unnormalised join, matching `cli_workspace_root` -- discovery probes
        # this path's own parent, so collapsing a trailing `..` here would move
        # which directory the sibling `alp-sdk` is looked for beside.
        workspace_root = Path.cwd() / project if project else Path.cwd()
        sdk = _resolve_sdk(sdk_root, workspace_root)
        if sdk is not None:
            found = discover_examples(sdk[0] / "examples")
        if filter_ is not None:
            found = [e for e in found if example_matches_filter(e, filter_)]
    except Exception as err:  # noqa: BLE001 -- the backstop; see the module docstring
        # No registry entry applies (`contract/issue-codes.json` covers
        # `bootstrap.*`/`debug-config.*` only), so this follows the port's
        # existing `<command>.internal-failure` spelling. Unreachable through
        # any enumerated path -- every IO error above is already swallowed the
        # way the oracle swallows it -- and here so that the one nobody thought
        # of still arrives as an envelope instead of a traceback on stderr with
        # an empty stdout the extension renders as nothing.
        found = []
        issues = [
            Issue(
                "examples.internal-failure",
                "error",
                f"examples failed unexpectedly: {err.__class__.__name__}: {err}",
            )
        ]
        exit_code = ExitCode.INTERNAL_FAILURE

    if json_mode:
        emit(
            Envelope(
                "examples",
                # Both null, always: the catalogue is an SDK-wide fact, so there
                # is no project this answer belongs to. Pinned by
                # `contract/envelopes/examples-catalog`.
                Project(root=None, board_yaml=None),
                {
                    "schemaVersion": DATA_SCHEMA_VERSION,
                    "examples": [e.as_dict() for e in found],
                },
                issues,
                exit_code,
                sdk=SdkInfo(sdk[1], sdk[2]) if sdk is not None else None,
            )
        )
    else:
        for line in render_examples_text(found, filter_, verbose):
            print(line, file=sys.stderr)
        for issue in issues:
            print(f"examples: {issue.message}", file=sys.stderr)
    raise typer.Exit(int(exit_code))
