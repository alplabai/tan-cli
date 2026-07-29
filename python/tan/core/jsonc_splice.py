# SPDX-License-Identifier: Apache-2.0
"""Locate WHERE inside a launch.json's own raw text a single ``configurations``
entry lives, so a write can splice just that entry instead of parsing the whole
document and re-serialising it (tan-cli#182).

Port of ``crates/tan-core/src/jsonc_splice.rs``. ``debug_launch`` already
answers "what does this file MEAN" (``strip_jsonc`` + ``json.loads``); this
module answers the narrower "where, in the ORIGINAL text, do the characters for
element N of the top-level ``configurations`` array begin and end". The scan
mirrors ``strip_jsonc``'s string/comment tracking so the two can never disagree
about what counts as structure -- but nothing here builds a value; it only
returns offsets into the caller's own string.

The payoff: a caller that copies ``original[:start]`` and ``original[end:]``
verbatim around a replacement never touches a comment, a trailing comma, or a
leading BOM sitting outside the edited span -- there is no re-serialisation step
for those characters to survive.

[`locate_configuration_edit`] returns ``None`` whenever the raw text does not
confidently resolve to a ``"configurations": [ ... ]`` array (missing key, wrong
shape, any scan mismatch). The caller's documented fallback is the old
whole-document re-serialise, which is always safe if lossy of comments, so a
locator miss degrades gracefully instead of risking a malformed write.

One deliberate simplification against the Rust: that `Scanner` carries a
`Vec<(byte, char)>` and converts token index -> byte offset at every boundary,
because Rust strings are byte-indexed and a multi-byte character would
otherwise desynchronise the two. Python strings are indexed by code point and
slice by code point, so index and offset are the same number here and the
conversion layer has nothing to do. Same semantics, no `byte_at`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: U+FEFF, spelled out rather than embedded: a literal BOM in source is
#: invisible and the first editor or codec to normalise it away would silently
#: turn the guard below into a no-op.
BOM = "\ufeff"


@dataclass(frozen=True)
class SpliceEdit:
    """Where to make the edit, as offsets into the ORIGINAL text.

    ``kind == "replace"``: replace the configuration object spanning
    ``[start, end)`` (``start`` is its ``{``, ``end`` one past the matching
    ``}``) with the new entry, reindented using ``indent``.

    ``kind == "append"``: insert a new entry immediately after offset ``start``
    (the end of the array's last element, or just past its ``[`` when the array
    is empty). ``needs_leading_comma`` is ``True`` when the element already
    there is not followed by a trailing comma. ``closing_indent`` is set only
    when appending into an array collapsed onto one line (``"configurations":
    []`` -- VS Code's own stock template, the single most common real input):
    ``original`` supplies no line break of its own before the ``]`` there, so
    without it the appended entry's trailing newline drags the ``]`` down to
    column 0 (tan-cli#182 review finding #5).
    """

    kind: str
    start: int
    indent: str
    end: int = 0
    needs_leading_comma: bool = False
    closing_indent: str | None = None


def locate_configuration_edit(original: str, index: int | None) -> SpliceEdit | None:
    """Find where the ``index``-th configuration entry lives in ``original``
    (``index is None`` means "no match -- append a fresh entry instead").

    ``index`` counts ONLY object-shaped elements of the top-level
    ``configurations`` array, in order -- the same filter
    ``parse_launch_json_or_default`` applies before handing the array to
    ``create_launch_json_write_plan``, so an index computed against that
    filtered list passes straight through.
    """
    s = _Scanner(original)
    array_open = _find_configurations_array(s)
    if array_open is None:
        return None

    cursor = array_open + 1
    object_spans: list[tuple[int, int]] = []
    last_element_end: int | None = None
    # Whether the LAST element seen was followed by a comma before `]`, and
    # where that comma left the cursor -- an existing comma already separates
    # "the last element" from "whatever comes next", so appending there reuses
    # it instead of emitting a second one (`,,`).
    trailing_comma = False
    after_trailing_comma = 0
    while True:
        cursor = s.skip_ws_and_comments(cursor)
        if s.char_at(cursor) == "]":
            break
        elem_start = cursor
        is_object = s.char_at(cursor) == "{"
        elem_end = s.skip_value(cursor)
        if elem_end is None:
            return None
        if is_object:
            object_spans.append((elem_start, elem_end))
        last_element_end = elem_end
        cursor = s.skip_ws_and_comments(elem_end)
        char = s.char_at(cursor)
        if char == ",":
            cursor += 1
            # Provisional: disproved below if another element follows -- the
            # next iteration's own comma-or-`]` check overwrites both before
            # they are ever read again.
            trailing_comma = True
            after_trailing_comma = cursor
        elif char == "]":
            trailing_comma = False
            break
        else:
            return None

    if index is not None:
        if index >= len(object_spans):
            return None
        start, end = object_spans[index]
        return SpliceEdit(
            kind="replace", start=start, end=end, indent=_indent_before(original, start)
        )

    if last_element_end is not None:
        # A pre-existing trailing comma already separates the last element from
        # our new one, so insert AFTER it -- inserting before would leave the
        # two elements adjacent with no separator and strand the comma as a
        # second, now-orphaned trailing comma of its own.
        if trailing_comma:
            after, needs_leading_comma = after_trailing_comma, False
        else:
            after, needs_leading_comma = last_element_end, True
        indent = (
            _indent_before(original, object_spans[-1][0])
            if object_spans
            else _DEFAULT_INDENT
        )
        return SpliceEdit(
            kind="append",
            start=after,
            indent=indent,
            needs_leading_comma=needs_leading_comma,
        )

    # Empty array: insert right after the `[`. Indent one level deeper than the
    # array's OWN line (not the flat default), so the entry lines up under
    # whatever indentation width the rest of the file already uses.
    array_line_indent = _indent_before(original, array_open)
    # `cursor` still holds the position of the closing `]` (the loop broke on it
    # before ever entering the element branch): collapsed onto one line (`[]`,
    # no whitespace between the brackets) means `original` supplies no line
    # break for `]` to land on, so supply the array's own indent explicitly. An
    # already-multi-line empty array (`[\n]`) keeps its own formatting.
    closing_indent = array_line_indent if cursor == array_open + 1 else None
    return SpliceEdit(
        kind="append",
        start=array_open + 1,
        indent=f"{array_line_indent}    ",
        needs_leading_comma=False,
        closing_indent=closing_indent,
    )


def apply_edit(original: str, edit: SpliceEdit, entry: Any) -> str:
    """Apply a located edit, inserting ``entry`` (pretty-printed and reindented
    to match where it lands) and returning the full new document. Every
    character of ``original`` outside the edited span is copied through
    unchanged -- that is the whole point: no re-serialisation pass runs over
    them, so a comment, a trailing comma or a BOM elsewhere survives verbatim.
    """
    pretty = pretty_json(entry)
    eol = _dominant_eol(original)
    if edit.kind == "replace":
        return (
            original[: edit.start]
            + _reindent(pretty, edit.indent, eol)
            + original[edit.end :]
        )
    out = original[: edit.start]
    if edit.needs_leading_comma:
        out += ","
    out += eol + edit.indent + _reindent(pretty, edit.indent, eol) + eol
    if edit.closing_indent is not None:
        out += edit.closing_indent
    return out + original[edit.start :]


def pretty_json(value: Any) -> str:
    """`serde_json::to_string_pretty`'s shape: two-space indent, ``": "`` after
    a key, no space after a comma, and non-ASCII emitted as itself rather than
    escaped (serde does not escape it, and this text lands in a file a human
    reads). ``json.dumps`` with an ``indent`` already defaults the item
    separator to ``","``, so the two agree without spelling it out -- it IS
    spelled out, because the default differs when ``indent`` is ``None`` and a
    reader should not have to know which branch applies.
    """
    return json.dumps(value, indent=2, separators=(",", ": "), ensure_ascii=False)


def _dominant_eol(original: str) -> str:
    """The line ending already dominant in ``original`` -- ``\\r\\n`` if it holds
    at least one, else ``\\n`` -- so a spliced-in entry's own newlines match its
    neighbours instead of leaving a mixed-EOL file behind on a CRLF-authored
    (Windows-default) launch.json (tan-cli#182 review finding #4).
    """
    return "\r\n" if "\r\n" in original else "\n"


#: A pretty printer starts every nested line 2 spaces deeper than its parent; a
#: brand-new array element one level under `"configurations": [` (itself one
#: level under the document root) lands at 4 spaces with no existing sibling to
#: copy a style from.
_DEFAULT_INDENT = "    "


def _reindent(pretty: str, indent: str, eol: str) -> str:
    """Re-prefix every line of ``pretty`` AFTER the first with ``indent``,
    joined with ``eol`` rather than a bare ``\\n``. The first line is left
    alone: for a replace it takes the position of the old ``{``, already after
    whatever indentation preceded it; for an append the caller pushes ``indent``
    itself before calling.
    """
    lines = pretty.split("\n")
    return lines[0] + "".join(f"{eol}{indent}{line}" for line in lines[1:])


def _indent_before(text: str, pos: int) -> str:
    """The whitespace-only run from the start of ``pos``'s line up to ``pos``,
    if it IS whitespace-only; otherwise [`_DEFAULT_INDENT`]. Matches a
    new/replaced entry's indentation to its neighbours."""
    before = text[:pos]
    line_start = before.rfind("\n") + 1  # -1 + 1 == 0 when there is no newline
    candidate = before[line_start:]
    if candidate and all(c in " \t" for c in candidate):
        return candidate
    return _DEFAULT_INDENT


def _find_configurations_array(s: _Scanner) -> int | None:
    """Scan the top-level object for a ``"configurations"`` key and return the
    offset of its value's opening ``[``, or ``None`` if the key is absent, is
    not the very next thing after ``:``, or the document does not open with
    ``{`` at all (every case the caller treats as "fall back to full
    re-serialise").

    A SECOND top-level ``"configurations"`` key also returns ``None``: JSON does
    not forbid a duplicate key, and ``json.loads`` (like VS Code's own
    jsonc-parser) resolves one to its LAST occurrence, but this scan would hand
    back the FIRST -- splicing into the array nothing downstream reads, while
    the caller's index (computed against the parsed, last-wins document)
    silently addresses the other one (tan-cli#182 review finding #3).
    """
    p = s.skip_ws_and_comments(0)
    if s.char_at(p) != "{":
        return None
    p += 1
    found: int | None = None
    while True:
        p = s.skip_ws_and_comments(p)
        char = s.char_at(p)
        if char == "}":
            return found
        if char == ",":
            p += 1
            continue
        if char != '"':
            return None
        key_start = p
        key_end = s.skip_string(p)
        if key_end is None:
            return None
        key_text = s.text[key_start + 1 : key_end - 1]
        p = s.skip_ws_and_comments(key_end)
        if s.char_at(p) != ":":
            return None
        p = s.skip_ws_and_comments(p + 1)
        if key_text == "configurations":
            if found is not None:
                return None
            if s.char_at(p) != "[":
                return None
            found = p
        nxt = s.skip_value(p)
        if nxt is None:
            return None
        p = nxt


class _Scanner:
    """Char-offset view over the source text, with the same string/comment
    tracking ``strip_jsonc`` uses -- so the two can never disagree about what
    counts as JSON structure vs quoted text vs a comment."""

    def __init__(self, text: str) -> None:
        self.text = text

    def char_at(self, p: int) -> str | None:
        """The character at ``p``, or ``None`` past the end. Guarded rather than
        sliced blindly: a NEGATIVE index would wrap to the end of the string and
        silently restart the scan there."""
        if 0 <= p < len(self.text):
            return self.text[p]
        return None

    def skip_ws_and_comments(self, p: int) -> int:
        """Skip whitespace, a stray BOM, and ``//`` / ``/* */`` comments."""
        while True:
            char = self.char_at(p)
            if char is None:
                return p
            if char.isspace() or char == BOM:
                p += 1
            elif char == "/" and self.char_at(p + 1) == "/":
                p += 2
                while (c := self.char_at(p)) is not None:
                    p += 1
                    if c == "\n":
                        break
            elif char == "/" and self.char_at(p + 1) == "*":
                p += 2
                while True:
                    c = self.char_at(p)
                    if c is None:
                        break
                    if c == "*" and self.char_at(p + 1) == "/":
                        p += 2
                        break
                    p += 1
            else:
                return p

    def skip_string(self, p: int) -> int | None:
        """Skip a JSON string starting at its opening ``"``; returns the offset
        just past the closing ``"``, or ``None`` if it never closes."""
        q = p + 1
        while True:
            char = self.char_at(q)
            if char is None:
                return None
            if char == "\\":
                q += 2
            elif char == '"':
                return q + 1
            else:
                q += 1

    def skip_balanced(self, p: int) -> int | None:
        """Skip a balanced ``{...}``/``[...]`` region starting at its opening
        bracket, tracking nested brackets/strings/comments so one inside a
        string or comment is never mistaken for structure. ``None`` if it never
        balances."""
        depth = 0
        q = p
        while True:
            char = self.char_at(q)
            if char is None:
                return None
            if char == '"':
                nxt = self.skip_string(q)
                if nxt is None:
                    return None
                q = nxt
            elif char == "/" and self.char_at(q + 1) in ("/", "*"):
                q = self.skip_ws_and_comments(q)
            elif char in "{[":
                depth += 1
                q += 1
            elif char in "}]":
                depth -= 1
                q += 1
                if depth == 0:
                    return q
            else:
                q += 1

    def skip_value(self, p: int) -> int | None:
        """Skip one JSON value (string/object/array/number/true/false/null);
        returns the offset just past it, or ``None`` on a value that never
        terminates."""
        char = self.char_at(p)
        if char is None:
            return None
        if char == '"':
            return self.skip_string(p)
        if char in "{[":
            return self.skip_balanced(p)
        # number / true / false / null: run until a structural delimiter. A `/`
        # is checked separately so a comment with no preceding space
        # (`nullfoo//c`) is never folded into it.
        q = p
        while (c := self.char_at(q)) is not None:
            if c in ",}]" or c.isspace():
                break
            if c == "/" and self.char_at(q + 1) in ("/", "*"):
                break
            q += 1
        return q if q != p else None
