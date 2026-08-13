# SPDX-License-Identifier: Apache-2.0
"""README's install section must name what the INSTALLER needs, not what a BUILD needs.

tan-cli#687. `README.md`'s first instruction is

    curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh

and a pristine `docker.io/library/ubuntu:24.04` has no `curl`. Measured in a
clean-room `podman` run against that image, with no hand modifications:

    MISSING curl        MISSING wget        PRESENT sha256sum   MISSING shasum
    PRESENT tar         PRESENT gzip        MISSING python3     MISSING pip3
    MISSING git         MISSING cmake       MISSING ninja       MISSING unzip
    MISSING file        (Ubuntu 24.04.4 LTS)

So the documented first step of the recommended path dies with
`bash: curl: command not found`, before the reader can reach `tan doctor`,
which is the thing that would have told them what was missing.

Two independent claims are asserted here, both derived rather than restated:

1. **The README must not be NARROWER than the script.** `install.sh` has taken
   `wget` since long before #687 -- it probes `command -v curl` then
   `command -v wget` and refuses only when BOTH are absent
   (`install.sh: need curl or wget on PATH`) -- and the same shape guards the
   digest tool (`sha256sum` or `shasum`). A reader told only about `curl`
   installs a package they may not need. The alternatives are read OUT OF
   `install.sh` at test time by `_required_any_groups()`, so adding a third
   downloader, or dropping one, moves this expectation with the script.

   Proven end to end while writing this, in the same clean room, with `wget`
   as the ONLY package added to a stock `ubuntu:24.04` (no `curl`, no
   `python3`, no `git`):

       install.sh: sha256 OK (0f7e5eaa9082328fb2e7c7b80341c617e85f882d6954afbedbeafe2c6102da88)
       install.sh: staged binary verified: tan 0.5.1
       install.sh: installed tan -> /root/.local/bin/tan (runtime: /root/.local/bin/tan-cli-lib)

2. **Installer needs and build needs must stay DISTINGUISHED.** They are
   different sets, and conflating them is half of what makes this confusing:
   the release asset is a PyInstaller freeze, so the installed `tan` needs
   neither `python3` nor `git`, while a BUILD needs the whole
   `BootstrapFacts.prerequisites_posix` list. `tan doctor`'s
   `bootstrap.prerequisites-missing` check already draws that line correctly
   (`missing from PATH: cmake, ninja`); this gate holds the README to it, by
   refusing to let a build-only tool be named as something the installer
   requires.

The build list is read from `tan.core.bootstrap.fallback_facts()`. That
constant is stale-by-default (the manifest wins whenever an SDK is present),
which is precisely why the README should agree with it: it is what `tan`
itself falls back on when there is no `metadata/bootstrap.json` to read, and a
first-time reader has no SDK checked out yet.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

from tan.core.bootstrap import fallback_facts

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
INSTALL_SH = REPO_ROOT / "install.sh"

#: The heading the installer instructions live under, and the one that ends the
#: section. Scope matters: "From source" below it legitimately names `git` and
#: `python3` as things the READER must have, and those are not claims about the
#: prebuilt installer.
_INSTALLER_HEADING = "### Installer (recommended)"
_SECTION_END = "### From source"

_COMMAND_V = re.compile(r"command -v ([A-Za-z0-9_.-]+)")
_IF = re.compile(r"^(if|elif)\b")


@functools.cache
def _install_sh() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


@functools.cache
def _required_any_groups() -> frozenset[frozenset[str]]:
    """Tool sets `install.sh` refuses to run without ANY member of.

    Walks the script's `if`/`elif`/`fi` nesting and reports every chain that
    (a) probes for tools with `command -v` and (b) refuses -- `exit 1` -- from
    inside its own body. That is exactly the two shapes the script uses:

        if ! command -v curl ... && ! command -v wget ...; then ... exit 1; fi
        if command -v sha256sum ...; then ... elif command -v shasum ...; then ... else ... exit 1; fi

    A `command -v` chain with no refusal in it is a CAPABILITY probe, not a
    requirement -- `ldd`, `realpath`, `timeout` and the `tan`-shadow check all
    degrade quietly -- and correctly produces no group.
    """
    stack: list[dict] = []
    groups: set[frozenset[str]] = set()
    for raw in _install_sh().splitlines():
        line = raw.strip()
        if _IF.match(line):
            tools = set(_COMMAND_V.findall(line))
            if line.startswith("if"):
                stack.append({"tools": tools, "refuses": False})
            elif stack:
                stack[-1]["tools"] |= tools
            continue
        if line.startswith("fi"):
            if stack:
                frame = stack.pop()
                if frame["tools"] and frame["refuses"]:
                    groups.add(frozenset(frame["tools"]))
            continue
        if re.search(r"\bexit 1\b", line) and stack:
            stack[-1]["refuses"] = True
    return frozenset(groups)


@functools.cache
def _installer_tools() -> frozenset[str]:
    return frozenset().union(*_required_any_groups())


@functools.cache
def _build_tools() -> tuple[str, ...]:
    """What a BUILD needs on POSIX -- `tan doctor`'s `hostPrerequisites` list."""
    return fallback_facts((3, 10)).prerequisites_posix


@functools.cache
def _readme() -> str:
    return README.read_text(encoding="utf-8")


@functools.cache
def _installer_section() -> str:
    text = _readme()
    assert _INSTALLER_HEADING in text, f"{_INSTALLER_HEADING} is gone from README.md"
    body = text.split(_INSTALLER_HEADING, 1)[1]
    return body.split(_SECTION_END, 1)[0]


def _names(text: str, tool: str) -> bool:
    """`tool` appears as a word, not as a substring of a longer name."""
    return re.search(rf"(?<![\w-]){re.escape(tool)}(?![\w-])", text) is not None


#: How far back from a tool name a negation may sit and still be about it. One
#: clause: "runs on a host with no `python3`, no `git` and no compiler".
_NEGATION_WINDOW = 45
_NEGATION = re.compile(r"\b(no|not|without|neither|nor|never)\b", re.I)


def _names_as_a_requirement(text: str, tool: str) -> bool:
    """`tool` is named as something the reader must HAVE, not as something they
    need not have.

    Saying a tool is NOT needed is the opposite of conflating the two lists --
    it is the distinction being drawn out loud -- so a mention carrying a
    negation within `_NEGATION_WINDOW` characters does not count. Same shape as
    `test_release_docs_match_the_workflow.py`'s `_UNUSABLE_MARKERS` window,
    which excuses a command a doc records precisely in order to say it does not
    work.

    A mention with the negation AFTER it ("you need `cmake`, but not `ninja`")
    is excused too only for the negated tool; the deliberate limit is that a
    single sentence mixing both directions is read charitably. The drift this
    guards is a positive requirement list, which has no negation in it at all.
    """
    for match in re.finditer(rf"(?<![\w-]){re.escape(tool)}(?![\w-])", text):
        before = text[max(0, match.start() - _NEGATION_WINDOW): match.start()]
        if not _NEGATION.search(before):
            return True
    return False


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_the_installer_section_names_every_tool_the_installer_refuses_without():
    """Every ALTERNATIVE, not just the one the copy-paste command happens to use.

    `curl` alone was the whole of #687: the script has always accepted `wget`,
    so the doc was narrower than the thing it documents, and a reader on a host
    that has `wget` was told to install a second downloader.
    """
    section = _installer_section()
    missing = sorted(t for t in _installer_tools() if not _names(section, t))
    assert not missing, (
        f"`{_INSTALLER_HEADING}` does not name {missing}, which install.sh "
        f"refuses to run without.\n"
        f"install.sh's required-any groups (read from the script): "
        f"{sorted(sorted(g) for g in _required_any_groups())}\n"
        f"A pristine ubuntu:24.04 has none of curl/wget, so the documented "
        f"first command dies with `bash: curl: command not found` and the "
        f"README names no prerequisite at all."
    )


def test_the_installer_section_does_not_present_a_build_tool_as_an_installer_need():
    """The release asset is a frozen binary: installing it needs no toolchain.

    Naming `cmake`/`ninja`/`git` here would send a first-time reader to install
    a build environment before they can run `tan --version`, and would erase the
    distinction `tan doctor` gets right.
    """
    section = _installer_section()
    build_only = [t for t in _build_tools() if t not in _installer_tools()]
    conflated = sorted(t for t in build_only if _names_as_a_requirement(section, t))
    assert not conflated, (
        f"`{_INSTALLER_HEADING}` names {conflated}, which a BUILD needs "
        f"({list(_build_tools())}) but the installer does not: the release "
        f"asset is a PyInstaller freeze and install.sh only ever probes "
        f"{sorted(_installer_tools())}.\n"
        f"Keep the two lists apart, the way `tan doctor`'s "
        f"`bootstrap.prerequisites-missing` check does."
    )


def test_the_readme_names_every_tool_a_build_needs_somewhere():
    """The other half of #687: the reader must be able to find the build list.

    Not scoped to the installer section -- `xz` belongs wherever the build is
    described, not next to the one-line install command. What is refused is the
    build list being undocumented, which is how a reader ends up at
    `bootstrap.prerequisites-missing` with no idea it was foreseeable.
    """
    text = _readme()
    missing = sorted(t for t in _build_tools() if not _names(text, t))
    assert not missing, (
        f"README.md never names {missing}. A build needs "
        f"{list(_build_tools())} on POSIX (tan.core.bootstrap.fallback_facts()"
        f".prerequisites_posix, which is what `tan doctor` checks when no SDK "
        f"manifest has been read yet)."
    )


def test_the_facts_are_actually_being_read():
    """Anti-tautology. Every assertion above is "the README does not omit X"; if
    X came back empty -- a rewritten install.sh, a renamed heading, a
    restructured `BootstrapFacts` -- this file would pass having compared
    nothing. tan-cli#485's lesson, applied here."""
    groups = _required_any_groups()
    assert frozenset({"curl", "wget"}) in groups, sorted(sorted(g) for g in groups)
    assert frozenset({"sha256sum", "shasum"}) in groups, sorted(sorted(g) for g in groups)
    # A probe the script degrades from must NOT be read as a requirement, or
    # the README would be told to document tools nobody needs.
    assert "realpath" not in _installer_tools(), sorted(_installer_tools())
    assert "ldd" not in _installer_tools(), sorted(_installer_tools())
    assert {"git", "cmake", "ninja", "python3"} <= set(_build_tools()), _build_tools()
    # The two sets have to actually differ, or check 2 above compares nothing.
    assert set(_build_tools()) - _installer_tools(), (_build_tools(), _installer_tools())
    section = _installer_section()
    assert "install.sh" in section and len(section) < len(_readme()), len(section)
    # `_names` is a word match, not a substring one: without that, "wget" inside
    # a URL query or "tar" inside "start" would satisfy the gate silently.
    assert _names("need curl or wget on PATH", "wget")
    assert not _names("restart the shell", "tar")
    # And the negation window excuses only a denial. If it excused everything,
    # check 2 would be vacuous -- a README could list `cmake` as an installer
    # prerequisite and stay green.
    assert _names_as_a_requirement("a build also needs cmake and ninja", "cmake")
    assert not _names_as_a_requirement("runs on a host with no cmake at all", "cmake")
