# SPDX-License-Identifier: Apache-2.0
"""Diagnostic data type + Rust-style renderer for the in-process board validator.

PORTED from alp-sdk `scripts/alp_cli/diagnostic.py` (pinned by
`tests/gates/test_planner_relocation_freshness.py::BOARD_VALIDATOR_HASHES`).

One deliberate departure: the SDK file takes its colour from `colorama`
(`Fore.RED`, `Style.RESET_ALL`, ...), an undeclared optional dependency that
`colorama.init()` additionally wraps `sys.stderr` with, stripping every escape
when stderr is not a tty. tan declares neither, so the same three SGR
sequences are spelled out here. The in-process engine always renders with
`color=False` (`board_validator_run`): the bytes the SDK script's stderr carries
under a pipe, which is the only way tan ever ran it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Severity = Literal["error", "warning", "note"]

# colorama's Fore.RED / Fore.YELLOW / Fore.CYAN / Style.RESET_ALL.
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_CYAN = "\x1b[36m"
_RESET = "\x1b[0m"


@dataclass(slots=True)
class Diagnostic:
    severity: Severity
    path: Path
    line: int
    col: int
    span: int
    code: str
    message: str
    hint: str | None = None
    doc_url: str | None = None


def _doc_url(code: str) -> str:
    base = os.environ.get("ALP_DIAG_BASE_URL", "docs/diagnostics")
    return f"{base}/{code}.md"


def _use_color(color: bool | None) -> bool:
    if color is False:
        return False
    if color is True:
        return True
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def render(diag: Diagnostic, source_text: str, color: bool | None = None) -> str:
    """Render a diagnostic as a multi-line Rust-style block."""
    use_color = _use_color(color)

    def paint(s: str, hue: str) -> str:
        return f"{hue}{s}{_RESET}" if use_color else s

    sev_hue = {"error": _RED, "warning": _YELLOW, "note": _CYAN}[diag.severity]
    header = f"{paint(f'{diag.severity}[{diag.code}]', sev_hue)}: {diag.message}"
    arrow = f"  --> {diag.path}:{diag.line}:{diag.col}"

    lines = source_text.splitlines()
    if 1 <= diag.line <= len(lines):
        src_line = lines[diag.line - 1]
    else:
        src_line = ""

    gutter_w = max(2, len(str(diag.line)))
    blank_gutter = " " * gutter_w
    src_block = (
        f"{blank_gutter} |\n"
        f"{str(diag.line).rjust(gutter_w)} | {src_line}\n"
        f"{blank_gutter} | {' ' * (diag.col - 1)}{paint('^' * max(1, diag.span), sev_hue)}"
    )

    tail: list[str] = []
    if diag.hint:
        tail.append(f"{blank_gutter} = hint: {diag.hint}")
    tail.append(f"{blank_gutter} = see: {diag.doc_url or _doc_url(diag.code)}")

    return "\n".join([header, arrow, src_block, *tail, ""])


class DiagnosticCollector:
    """Collect diagnostics across multiple validation passes."""

    def __init__(self) -> None:
        self._items: list[Diagnostic] = []

    def add(self, diag: Diagnostic) -> None:
        self._items.append(diag)

    def __iter__(self) -> Iterator[Diagnostic]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self._items)
