# SPDX-License-Identifier: Apache-2.0
"""A comment-preserving, text-level append to `board.yaml`'s `models:` list
(tan-cli#1286, `tan model add`).

Customers hand-write and annotate `board.yaml`; a `yaml.safe_load` /
`yaml.dump` round-trip deletes every comment. Rather than take a new
dependency (`ruamel.yaml`), this splices TEXT: it finds the one top-level
`models:` list with a small line-oriented scan, inserts the new entry's lines
right after that list's last content line, and returns every other byte of the
file untouched -- the same targeted-splice shape `tan.core.scaffold.
splice_companion_cores` uses for `cores:`.

**It refuses rather than guesses.** `BoardEditRefused` is raised for every
shape the scan cannot place an insertion in with certainty: a flow-style
`models: [...]`/`{...}` (only the empty `[]` is rewritten), an anchor/alias/tag
or scalar on the `models:` line, a quoted `models` key, a second top-level
`models:`, a multi-document file, a tab in the list's indentation, or a list
whose lines do not read as a block sequence. A final guard re-parses the old
and the new text with `yaml.safe_load` and refuses unless the new document is
EXACTLY the old one plus the one appended entry -- so a shape the scan
misjudged cannot reach disk as a silently different document.

The only bytes that ever change: the inserted lines, the `[]` of an empty
flow list, and a missing final newline on the line the insertion follows.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+")
_MODELS_KEY_RE = re.compile(r"^models[ ]*:(?P<rest>([ \t].*)?)$")
_QUOTED_MODELS_RE = re.compile(r"""^["']models["']\s*:""")
_DOC_START_RE = re.compile(r"^---(\s|$)")
_DOC_END_RE = re.compile(r"^\.\.\.(\s|$)")
_COMPLEX_KEY_RE = re.compile(r"^\?(\s|$)")
_EMPTY_VALUES = ("[]", "~", "null", "Null", "NULL")
_BOM = "\ufeff"

#: Indent for a `models:` list this module creates itself.
DEFAULT_ITEM_INDENT = 2


class BoardEditRefused(Exception):
    """The `models:` list cannot be appended to safely; `str(err)` says why."""


def _split_lines(text: str) -> list[str]:
    # Not `str.splitlines`: it also breaks on form feed, NEL and U+2028, none
    # of which are YAML line breaks.
    return _LINE_RE.findall(text)


def _content(line: str) -> str:
    return line.rstrip("\r\n")


def _text(lines: list[str], n: int) -> str:
    """Line `n` without its line ending, and (line 0 only) without a leading
    BOM -- YAML ignores a BOM, so a column-0 key behind one is still column 0."""
    line = _content(lines[n]).rstrip("\r")
    return line[1:] if n == 0 and line.startswith(_BOM) else line


def _strip_comment(rest: str) -> tuple[str, str]:
    """`(value, comment)` of the text after a `key:` -- `comment` keeps its
    leading `#` (and the whitespace before it is dropped from `value`)."""
    match = re.search(r"(^|\s)#", rest)
    if match is None:
        return rest.strip(), ""
    return rest[: match.start()].strip(), rest[match.start() :].strip()


def _is_seq_item(stripped: str) -> bool:
    return stripped == "-" or stripped.startswith("- ")


def _scalar(value: Any) -> str:
    import yaml  # noqa: PLC0415 (declared dependency)

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        out = yaml.safe_dump(value, default_flow_style=True, width=2**30, allow_unicode=True)
        out = out.rstrip("\n")
        if out.endswith("\n..."):
            out = out[:-4]
        if "\n" in out:
            raise BoardEditRefused(f"cannot render {value!r} as a single-line YAML scalar")
        return out
    raise BoardEditRefused(f"cannot render a {type(value).__name__} value in a `models:` entry")


def _render_mapping(mapping: Mapping, indent: int, first_prefix: str | None) -> list[str]:
    """Block-style lines for `mapping`; `first_prefix` (`"- "`) replaces the
    first line's leading indent for a sequence item."""
    lines: list[str] = []
    pad = " " * indent
    for n, (key, value) in enumerate(mapping.items()):
        lead = first_prefix if (n == 0 and first_prefix is not None) else pad
        k = _scalar(key)
        if isinstance(value, Mapping):
            if not value:
                raise BoardEditRefused(f"cannot render empty mapping under `{key}`")
            lines.append(f"{lead}{k}:")
            lines.extend(_render_mapping(value, indent + 2, None))
        elif isinstance(value, (list, tuple)):
            if not all(isinstance(v, (int, str)) and not isinstance(v, bool) for v in value):
                raise BoardEditRefused(f"cannot render the list under `{key}`")
            lines.append(f"{lead}{k}: [{', '.join(_scalar(v) for v in value)}]")
        else:
            lines.append(f"{lead}{k}: {_scalar(value)}")
    return lines


def render_entry(entry: Mapping, item_indent: int = DEFAULT_ITEM_INDENT) -> list[str]:
    """The block-sequence lines (no line endings) for one `models:` entry."""
    return _render_mapping(entry, item_indent + 2, " " * item_indent + "- ")


def _scan(lines: list[str]) -> tuple[int | None, int | None]:
    """`(models key line | None, line of a trailing `...` | None)`. Refuses a
    complex `? key`, a quoted `models` key, a duplicate key and a second YAML
    document (a second `---`, or content after `...`); a leading `%` directive
    plus one `---`, and a trailing `...`, are fine."""
    seen_content = False
    seen_start = False
    end_marker: int | None = None
    found: list[int] = []
    for n in range(len(lines)):
        line = _text(lines, n)
        if line[:1] in (" ", "\t", "#", ""):
            continue
        if end_marker is not None:
            raise BoardEditRefused("board.yaml has content after a `...` document end")
        if line[0] == "%" and not seen_content and not seen_start:
            continue
        if _DOC_START_RE.match(line):
            if seen_content or seen_start:
                raise BoardEditRefused("board.yaml has more than one YAML document")
            seen_start = True
            continue
        if _DOC_END_RE.match(line):
            end_marker = n
            continue
        seen_content = True
        if _COMPLEX_KEY_RE.match(line):
            raise BoardEditRefused("board.yaml uses a complex `? key`")
        if _QUOTED_MODELS_RE.match(line):
            raise BoardEditRefused("board.yaml quotes its `models` key")
        if _MODELS_KEY_RE.match(line):
            found.append(n)
    if len(found) > 1:
        raise BoardEditRefused("board.yaml has more than one top-level `models:` key")
    return (found[0] if found else None), end_marker


def _list_extent(lines: list[str], key_n: int) -> tuple[int, int | None]:
    """`(last content line, item indent)` of the block list under line
    `key_n`; `(key_n, None)` for an empty one. Trailing blank/comment lines
    are not part of the list, so the insertion lands before them."""
    last = key_n
    item_indent: int | None = None
    for n in range(key_n + 1, len(lines)):
        line = _text(lines, n)
        stripped = line.lstrip(" ")
        if stripped == "" or stripped.startswith("#"):
            continue
        ind = len(line) - len(stripped)
        if stripped[:1] == "\t" or "\t" in line[:ind]:
            raise BoardEditRefused("`models:` list is indented with a tab")
        if ind == 0 and not _is_seq_item(stripped):
            break
        if item_indent is None:
            if not _is_seq_item(stripped):
                raise BoardEditRefused("`models:` is not a block list")
            item_indent = ind
        elif ind < item_indent or (ind == item_indent and not _is_seq_item(stripped)):
            raise BoardEditRefused("`models:` list has inconsistent indentation")
        last = n
    return last, item_indent


def append_models_entry(text: str, entry: Mapping) -> str:
    """`text` (a `board.yaml`) with `entry` appended to its `models:` list,
    created at the end of the file when absent. Raises `BoardEditRefused`."""
    old_doc = _strict_load(text, "board.yaml")
    if old_doc is None:
        old_doc = {}
    if not isinstance(old_doc, dict):
        raise BoardEditRefused("board.yaml is not a YAML mapping")

    lines = _split_lines(text)
    eol = "\r\n" if "\r\n" in text else "\n"
    key_n, end_n = _scan(lines)
    new_lines = list(lines)
    if key_n is None:
        at = len(new_lines) if end_n is None else end_n
        if at and not new_lines[at - 1].endswith("\n"):
            new_lines[at - 1] += eol
        block = ["models:", *render_entry(entry, DEFAULT_ITEM_INDENT)]
        new_lines[at:at] = [ln + eol for ln in block]
    else:
        key_raw = lines[key_n]
        rest = _MODELS_KEY_RE.match(_text(lines, key_n)).group("rest")  # type: ignore[union-attr]
        value, comment = _strip_comment(rest)
        if value in _EMPTY_VALUES:
            new_key = ("\ufeff" if key_n == 0 and key_raw.startswith(_BOM) else "") + "models:"
            new_lines[key_n] = (
                new_key + (f" {comment}" if comment else "") + key_raw[len(_content(key_raw)) :]
            )
        elif value:
            raise BoardEditRefused(f"`models:` is written as `{value}`, not a block list")
        last, item_indent = _list_extent(lines, key_n)
        if value in _EMPTY_VALUES and item_indent is not None:
            raise BoardEditRefused(f"`models: {value}` is followed by list items")
        if not new_lines[last].endswith("\n"):
            new_lines[last] += eol
        indent = DEFAULT_ITEM_INDENT if item_indent is None else item_indent
        new_lines[last + 1 : last + 1] = [ln + eol for ln in render_entry(entry, indent)]

    new_text = "".join(new_lines)
    _verify(old_doc, new_text, entry)
    return new_text


def _strict_load(text: str, what: str) -> Any:
    """`yaml.safe_load` that also rejects a repeated mapping key -- the check
    `tan build`'s own strict loader applies, which plain `safe_load` (last
    key wins) hides. Refuses on any failure, recursion included."""
    import yaml  # noqa: PLC0415 (declared dependency)

    class Strict(yaml.SafeLoader):
        pass

    def construct(loader: Any, node: Any) -> dict:
        seen: set = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = loader.construct_object(key_node, deep=True)
            if key in seen:
                raise BoardEditRefused(f"{what} repeats the mapping key {key!r}")
            seen.add(key)
        loader.flatten_mapping(node)
        return {
            loader.construct_object(k, deep=True): loader.construct_object(v, deep=True)
            for k, v in node.value
        }

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct)
    try:
        return yaml.load(text, Loader=Strict)  # noqa: S506 (SafeLoader subclass)
    except BoardEditRefused:
        raise
    except (yaml.YAMLError, RecursionError, TypeError) as err:
        raise BoardEditRefused(f"{what} does not parse cleanly: {err}") from err


def _same(a: Any, b: Any) -> bool:
    """Structural equality that treats NaN as equal to NaN (`.nan` in a board
    would otherwise make every edit look like a corruption)."""
    if isinstance(a, float) and isinstance(b, float) and a != a and b != b:
        return True
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _verify(old_doc: dict, new_text: str, entry: Mapping) -> None:
    import yaml  # noqa: PLC0415 (declared dependency)

    new_doc = _strict_load(new_text, "the edited board.yaml")
    expected = copy.deepcopy(old_doc)
    existing = expected.get("models")
    if existing is None:
        existing = []
    if not isinstance(existing, list):
        raise BoardEditRefused("`models:` is not a list")
    expected["models"] = [*existing, yaml.safe_load(yaml.safe_dump(dict(entry)))]
    try:
        same = _same(new_doc, expected)
    except RecursionError:
        same = False
    if not same:
        raise BoardEditRefused(
            "the edit would change more than the appended entry (unusual `models:` layout)"
        )
