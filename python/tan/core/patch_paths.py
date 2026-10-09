# SPDX-License-Identifier: Apache-2.0
"""Repo-relative paths a unified diff / `git format-patch` file touches.

Pure text in, paths out (tan-cli#1384). Used to fingerprint the working-tree
files alp-sdk's `zephyr/patches/` write, so the set must include every path a
patch can change on disk, not only the ones with a `+++ b/` line:

* `diff --git a/<old> b/<new>` names BOTH sides, which is the only place a pure
  rename, a mode-only change or a binary patch is visible (none of them carry
  `---`/`+++` lines);
* `--- a/<path>` names a file the patch deletes (`+++ /dev/null`);
* `+++ b/<path>` names a file it writes (`--- /dev/null` when it creates it).

`---`/`+++` count only as headers, never as hunk content: after a
`diff --git` line and before the first `@@`, or (for a plain `diff -u` with no
`diff --git` line) as a `---`, `+++`, `@@` triple.
"""
from __future__ import annotations

import re

_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\S+')


def _unquote(raw: str) -> str:
    """Undo git's C-style quoting (`"a/caf\\303\\251"`); plain paths pass through."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            return (
                raw[1:-1].encode("utf-8").decode("unicode_escape")
                .encode("latin-1").decode("utf-8", "replace")
            )
        except (UnicodeError, ValueError):
            return raw[1:-1]
    return raw


def _side(raw: str, prefix: str) -> str | None:
    """The path on one side of a header, or `None` (`/dev/null`, no prefix)."""
    path = _unquote(raw.split("\t", 1)[0]).strip()
    if path == "/dev/null" or not path.startswith(prefix):
        return None
    return path[len(prefix):] or None


def _git_header(rest: str) -> list[str]:
    """Both paths of `diff --git a/<old> b/<new>` (the text after `diff --git `)."""
    if '"' in rest:
        tokens = _TOKEN.findall(rest)
        if len(tokens) == 2:
            return [p for p in (_side(tokens[0], "a/"), _side(tokens[1], "b/")) if p]
    half = (len(rest) - 1) // 2
    if len(rest) % 2 == 1 and rest[:half].startswith("a/") and rest[half] == " " \
            and rest[half + 1:] == "b/" + rest[:half][2:]:
        return [rest[:half][2:]]  # unrenamed: both sides are the same path
    old, sep, new = rest.partition(" b/")
    return [p for p in (_side(old, "a/"), new if sep else None) if p]


def patch_paths(text: str) -> set[str]:
    """Every repo-relative path `text` (one patch file) touches."""
    lines = text.splitlines()
    paths: set[str] = set()
    in_header = False
    for i, ln in enumerate(lines):
        if ln.startswith("diff --git "):
            in_header = True
            paths.update(_git_header(ln[len("diff --git "):]))
        elif ln.startswith("@@"):
            in_header = False
        elif ln.startswith(("--- ", "+++ ")):
            plain_triple = (
                ln.startswith("--- ")
                and i + 2 < len(lines)
                and lines[i + 1].startswith("+++ ")
                and lines[i + 2].startswith("@@")
            )
            if in_header or plain_triple:
                side = _side(ln[4:], "a/" if ln[0] == "-" else "b/")
                if side:
                    paths.add(side)
                if plain_triple:
                    plus = _side(lines[i + 1][4:], "b/")
                    if plus:
                        paths.add(plus)
    return paths
