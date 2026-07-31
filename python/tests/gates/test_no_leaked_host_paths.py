# SPDX-License-Identifier: Apache-2.0
"""Gate: no tracked file may name a real developer's machine.

This is a PUBLIC repo. An absolute path out of a maintainer's checkout is not a
style nit -- it discloses a directory layout and often a login name, permanently,
because a public git history cannot be un-published. It is also simply wrong for
the reader: `ALP_SDK_ROOT=E:/GitHub/alp-sdk/...` in a doc is an instruction that
works on exactly one computer.

Everything `tan` resolves at RUNTIME is already token-based -- `ALP_SDK_ROOT`,
the `.alp/sdk-path` project pin, `~/.alp/sdk-default`, `Path.home()`, and
`sys._MEIPASS` for the frozen bundle. Nothing is hardcoded and this gate does not
police that. What leaks is PROSE: a docstring or a doc paragraph that pasted a
real invocation instead of a placeholder one.

`.gitignore` already blocks `.alp-support/`, whose bundles snapshot the
developer's machine wholesale. That rule is about a directory. Two leaks landed
anyway in ordinary source and docs, which is the hole this closes.

Illustration paths are the point of the ban, not a casualty of it, so the
placeholder home names below stay legal: a comment explaining that
`C:\\Users\\Jane Doe` has a space in it is the correct way to write that comment.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]

#: Home directory names that are OBVIOUSLY not a real person's account. A path
#: under one of these is an illustration and stays legal.
PLACEHOLDER_HOMES = frozenset(
    {
        "...",
        "alice",
        "bob",
        "dev",
        "jane",
        "jane doe",
        "me",
        "runner",
        "runner~1",
        "ubuntu",
        "user",
        "you",
        "<name>",
    }
)


def _is_real_account(home: str) -> bool:
    """Could `home` be a real login name, or is it plainly a stand-in?

    The list above cannot be the whole answer -- the Rust tests alone use
    `/home/u` and `/home/.alp` as throwaway roots, and `crates/` is frozen, so a
    gate that only knows named placeholders would demand an edit that is not
    allowed to happen. Judge the SHAPE instead:

      * 1-2 characters is nobody's account, it is a test fixture (`/home/u`);
      * a leading `.` is a dotfile the pattern ran into, not a user
        (`/home/.alp` is `$HOME/.alp` with the home elided);
      * `<`, `{`, `$` and `%` open a placeholder or a shell/format expansion
        (`/home/<name>`, `/home/{user}`, `C:\\Users\\%USERNAME%`).

    Anything else -- `caner`, `jsmith` -- is treated as a real account and fails.
    Erring toward false positives is correct here: a wrong flag costs one edit,
    a miss is published forever.
    """
    stripped = home.strip()
    if len(stripped) < 3 or stripped[0] in ".<{$%":
        return False
    return stripped.lower() not in PLACEHOLDER_HOMES

#: Each entry: (compiled pattern, what it means). A match is a leak unless the
#: home-name exemption above applies.
#:
#: Deliberately NOT a generic "absolute path" match. `/usr/bin/env`,
#: `/dev/ttyUSB1` and `C:/proj/build` are all legitimate and would drown the
#: signal. These four shapes each name a place that exists on ONE machine.
LEAKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"[A-Za-z]:[\\/]GitHub\b"),
        "a drive-letter path into a personal checkout root",
    ),
    (
        re.compile(r"/mnt/[a-z]/GitHub\b"),
        "the WSL view of that same personal checkout root",
    ),
    (
        re.compile(r"OneDrive"),
        "a synced corporate drive; also where the private SoM documents live",
    ),
    (
        # Captures the home NAME so PLACEHOLDER_HOMES can clear it.
        re.compile(r"(?:/home/|[A-Za-z]:[\\/]Users[\\/])([^\\/\s\"'`,)]+)"),
        "a home directory naming a real account",
    ),
)

#: Extensions worth reading. Anything else is either binary or generated, and a
#: leak in a `.png` is not a thing that happens.
TEXT_SUFFIXES = frozenset(
    {
        "",
        ".c",
        ".cfg",
        ".cmake",
        ".conf",
        ".dts",
        ".h",
        ".json",
        ".md",
        ".overlay",
        ".ps1",
        ".py",
        ".rs",
        ".sh",
        ".toml",
        ".ts",
        ".txt",
        ".yaml",
        ".yml",
    }
)

#: This file quotes every shape it bans, so it would fail itself.
EXEMPT = frozenset({"python/tests/gates/test_no_leaked_host_paths.py"})


def _tracked_text_files() -> list[str]:
    """Ask git, not the filesystem: only TRACKED files can leak into history.

    An untracked scratch file or a `.gitignore`d support bundle is exactly the
    thing that is allowed to hold a real path -- it is never published.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def test_no_tracked_file_names_a_real_machine() -> None:
    findings: list[str] = []

    for rel in _tracked_text_files():
        if rel in EXEMPT or pathlib.Path(rel).suffix.lower() not in TEXT_SUFFIXES:
            continue
        path = REPO / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: not a prose leak

        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern, why in LEAKS:
                for match in pattern.finditer(line):
                    home = match.groups()[0] if match.groups() else None
                    if home is not None and not _is_real_account(home):
                        continue
                    findings.append(f"{rel}:{lineno}: {match.group(0)!r} -- {why}")

    assert not findings, (
        "Tracked files name a real machine. This repo is public and its history "
        "is permanent; replace each with a placeholder or a token "
        "(`ALP_SDK_ROOT`, `~/.alp/sdk-default`, `/srv/alp-sdk`, `C:\\Users\\dev`):"
        "\n  " + "\n  ".join(findings)
    )
