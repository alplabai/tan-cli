#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compute + apply the development-version bump owed after a tag publishes
(tan-cli#770).

`release.yml`'s `verify-version` job asserts the tree matches the tag being
cut, but nothing asserted the MIRROR of that: that `dev` moves OFF the tag
once it exists. `dev`'s tip carries the exact version the release-prep commit
was tagged at (tags are cut from `main`, and `dev` merges into it), so from
the moment a tag is pushed, `dev`'s `TAN_VERSION` equals a published release
-- the precise state `version_check.py --not-released` exists to refuse.
Every PR opened against `dev` then fails `version-identity.yml`'s
`not-a-released-version` job on a check unrelated to its own diff, until a
human notices and bumps four files by hand. That has happened twice
(tan-cli#479 -> `0.5.2-rc1.dev0`, tan-cli#768 -> `0.6.0-rc2.dev0`), both by
hand, both the same recurrence one release apart.

This script is the arithmetic + the four-file edit those two fixes each did
by hand, computed from ONE input: the tag `release.yml` just published. It
calls into `version_check` for the SemVer<->PEP 440 rendering and the
CHANGELOG target (`release_target()`) rather than re-deriving either -- that
module already computes and validates every one of those relationships.

THE RULE, made explicit (the same one both precedents used):

* the published tag carries a pre-release kind (`-rc.N` / `-rcN` / ...) ->
  the SAME core version, that kind's number +1, a `.dev0` tail
  (`0.6.0-rc1` -> `0.6.0-rc2.dev0`);
* the published tag is a bare final release (no pre-release kind) ->
  the PATCH incremented, `-rc1.dev0` appended (`0.5.1` -> `0.5.2-rc1.dev0`).

Neither promises the guessed next version will actually be cut under that
exact name. `X.devN` reads as "before X" in SemVer and PEP 440 alike, so
whatever the real next tag turns out to be -- see `0.6.0-rc1` itself, cut
directly over a `0.5.2-rc1.dev0` dev line and never a `0.5.2` -- this dev
version still sorts below it. Only the CORE (`version_check.release_target()`)
has to be right, because that is the one thing the CHANGELOG's
`## [<core>] -- Unreleased` heading and the not-released gate both key on.

Usage:

    python scripts/bump_dev_version.py --tag v0.6.0-rc1          # dry run
    python scripts/bump_dev_version.py --tag v0.6.0-rc1 --apply  # write

No third-party imports, matching `version_check.py`'s own reasoning: this
runs as an early CI step, and a version-bump gate that needs a dependency
resolved is a gate that can fail for a reason unrelated to versions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# `version_check.py` lives beside this file; import it from there rather than
# re-deriving the SemVer/PEP 440 mapping or the CHANGELOG-target rule it
# already owns. Tests rebind its `PYTHON_ROOT`/`REPO_ROOT` globals to point
# this whole module at a synthetic tree -- see
# `test_version_check_refuses_an_empty_changelog_section.py` for the same
# technique applied to `version_check` directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import version_check as vc  # noqa: E402

#: Cited in every generated comment/PR body so a reader lands on the issue
#: that explains why this script exists, not just what it did.
_ISSUE = "tan-cli#770"

_TAN_VERSION_LINE = re.compile(r'^TAN_VERSION\s*=\s*"[^"]+"', re.MULTILINE)
_PYPROJECT_VERSION_LINE = re.compile(r'^version\s*=\s*"[^"]+"', re.MULTILINE)
_CHANGELOG_FIRST_HEADING = re.compile(r"^## \[", re.MULTILINE)


def next_dev_version(released: str) -> str:
    """The `.devN`-tail-on-the-next-pre-release this project bumps to once
    ``released`` (a tag's version -- e.g. ``0.6.0-rc1`` or ``0.5.1``, never
    with a leading ``v``) has published. See the module docstring for the
    rule; this is its one implementation.

    Raises `version_check.VersionError` on anything `version_check`'s own
    SemVer grammar does not recognise, and refuses a ``released`` that
    already carries a development spelling (a bare ``dev`` pre-release kind,
    or any `.devN` tail) -- a just-published release tag should never BE a
    development version, so there is nothing to bump it FROM.
    """
    released = released.strip()
    match = vc._SEMVER.match(released)
    if match is None:
        raise vc.VersionError(
            f"{released!r} is not a SemVer this project understands; cannot "
            f"compute the development version that follows it"
        )
    core, kind, num, dev = match.group("core", "kind", "num", "dev")
    if dev is not None:
        raise vc.VersionError(
            f"{released!r} already carries a `.devN` tail -- a just-published "
            f"release tag should never be a development version"
        )
    if kind is None:
        major, minor, patch = (int(part) for part in core.split("."))
        return f"{major}.{minor}.{patch + 1}-rc1.dev0"
    if kind == "dev":
        raise vc.VersionError(
            f"{released!r}'s pre-release kind is `dev`, which this project "
            f"never publishes a release tag under"
        )
    number = int(num) if num is not None else 0
    return f"{core}-{kind}{number + 1}.dev0"


@dataclass(frozen=True)
class BumpPlan:
    """The computed edit to the four files `version_check.py` cross-checks,
    not yet written. Kept apart from `apply_plan()` so a caller can inspect
    or diff the plan before committing to it (and so tests can assert on the
    computed text without touching a filesystem twice)."""

    released: str
    next_version: str
    target: str
    version_py: Path
    pyproject: Path
    npm_shim: Path
    changelog: Path
    version_py_text: str
    pyproject_text: str
    npm_shim_text: str
    changelog_text: str


def _bump_version_py(
    text: str, *, released: str, next_version: str, target: str, today: str
) -> str:
    if _TAN_VERSION_LINE.search(text) is None:
        raise vc.VersionError(
            "no `TAN_VERSION = \"...\"` assignment found in python/tan/version.py"
        )
    paragraph = (
        f"v{released} published {today} ({_ISSUE}'s automated post-release "
        f"bump): the development line still carried the published tag's exact "
        f"version, the state `version_check.py --not-released` exists to "
        f"refuse. TAN_VERSION moves to `{next_version}` -- a `.devN` tail on "
        f"the next pre-release -- which promises neither that release nor "
        f"that pre-release number, only that both sort above it in SemVer "
        f"and PEP 440 alike. CHANGELOG home is `## [{target}] — Unreleased`, "
        f"per `release_target()`."
    )
    # Wrapped to match the hand-written comment blocks already in this file
    # (~78 columns, one `#` per line) rather than emitting one unreadable
    # 400-column line -- `textwrap` does the wrapping, this does not
    # re-derive it.
    wrapped = "\n".join(f"# {line}" for line in textwrap.wrap(paragraph, width=76))
    note = f"#\n{wrapped}\n"
    return _TAN_VERSION_LINE.sub(
        lambda _m: f'{note}TAN_VERSION = "{next_version}"', text, count=1
    )


def _bump_pyproject(text: str, *, next_version: str) -> str:
    if _PYPROJECT_VERSION_LINE.search(text) is None:
        raise vc.VersionError(
            'no top-level `version = "..."` line found in python/pyproject.toml'
        )
    pep440 = vc.semver_to_pep440(next_version)
    return _PYPROJECT_VERSION_LINE.sub(f'version = "{pep440}"', text, count=1)


def _bump_npm_shim(text: str, *, next_version: str) -> str:
    data = json.loads(text)
    if "version" not in data:
        raise vc.VersionError(
            'npm-shim/package.json has no top-level "version" key'
        )
    data["version"] = next_version
    # `ensure_ascii=False`: the file's own `description` carries a literal
    # em dash (the only non-ASCII character in the file), and the default
    # `ensure_ascii=True` would silently turn it into a `\uXXXX` escape on
    # every bump -- a diff on a field this script has no business touching.
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _insert_changelog_section(
    text: str, *, target: str, released: str, next_version: str
) -> str:
    """A brand-new `## [<target>] — Unreleased` heading, right after the
    file's intro (ahead of the first existing `## [` heading).

    Refuses outright if a `## [<target>]` heading already exists, dated or
    not -- by the time this script runs, the release-prep commit that got
    tagged has already renamed (an RC) or dated (a final) the PREVIOUS
    Unreleased heading, so a heading for the NEW target existing already
    means something about this repeat run does not match what this script
    assumes, and duplicating or silently reusing it is worse than refusing.

    Never empty: `version_check.changelog_problems()` fails a development
    version whose target section has nothing under it (tan-cli#500), so the
    one bullet recording what this script did doubles as the required body.
    """
    heading = re.compile(rf"^## \[{re.escape(target)}\]", re.MULTILINE)
    if heading.search(text) is not None:
        raise vc.VersionError(
            f"CHANGELOG.md already has a `## [{target}]` section -- refusing "
            f"to guess whether to extend it or add a duplicate"
        )
    first = _CHANGELOG_FIRST_HEADING.search(text)
    if first is None:
        raise vc.VersionError("CHANGELOG.md has no `## [` heading to insert ahead of")
    # `### Fixed`, not a bare bullet directly under the `## [...]` heading:
    # `release.yml` slices a published release's notes on an exact
    # `^## [<version>]` match and publishes everything up to the next such
    # heading verbatim, so a heading-less bullet here would open the next
    # release's body above its own `### Added`/`### Fixed` sections. It also
    # lets `assemble_changelog.splice()` (which looks for an existing
    # `### <Category>` heading under Unreleased before creating one) append
    # later fragments into this section instead of duplicating it.
    # Wrapped at ~78 columns like every hand-written CHANGELOG bullet
    # (`_bump_version_py` above uses the same `textwrap.wrap`, not
    # reimplemented here) -- the unwrapped bullet runs past 110 columns.
    bullet_text = (
        f"`TAN_VERSION` moved to `{next_version}` off the published "
        f"`v{released}` tag ({_ISSUE}'s automated post-release bump)."
    )
    bullet = "\n".join(
        textwrap.wrap(
            bullet_text, width=78, initial_indent="- ", subsequent_indent="  "
        )
    )
    section = f"## [{target}] — Unreleased\n\n### Fixed\n\n{bullet}\n\n"
    return text[: first.start()] + section + text[first.start() :]


def build_plan(released: str, *, today: str | None = None) -> BumpPlan:
    """The four-file edit `next_dev_version(released)` implies, read from and
    validated against `python/tan/version.py`, `python/pyproject.toml`,
    `npm-shim/package.json` and `CHANGELOG.md` under `version_check`'s
    CURRENT `PYTHON_ROOT`/`REPO_ROOT` -- rebind those to point this whole
    function at a synthetic tree, the same technique
    `test_version_check_refuses_an_empty_changelog_section.py` already uses
    on `version_check` directly."""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    next_version = next_dev_version(released)
    target, is_dev = vc.release_target(next_version)
    if not is_dev:  # pragma: no cover -- next_dev_version always returns one
        raise vc.VersionError(f"computed {next_version!r} does not read as development")

    version_py = vc.PYTHON_ROOT / "tan" / "version.py"
    pyproject = vc.PYTHON_ROOT / "pyproject.toml"
    npm_shim = vc.REPO_ROOT / "npm-shim" / "package.json"
    changelog = vc.REPO_ROOT / "CHANGELOG.md"

    version_py_text = _bump_version_py(
        version_py.read_text(encoding="utf-8"),
        released=released,
        next_version=next_version,
        target=target,
        today=today,
    )
    pyproject_text = _bump_pyproject(
        pyproject.read_text(encoding="utf-8"), next_version=next_version
    )
    npm_shim_text = _bump_npm_shim(
        npm_shim.read_text(encoding="utf-8"), next_version=next_version
    )
    changelog_text = _insert_changelog_section(
        changelog.read_text(encoding="utf-8"),
        target=target,
        released=released,
        next_version=next_version,
    )

    return BumpPlan(
        released=released,
        next_version=next_version,
        target=target,
        version_py=version_py,
        pyproject=pyproject,
        npm_shim=npm_shim,
        changelog=changelog,
        version_py_text=version_py_text,
        pyproject_text=pyproject_text,
        npm_shim_text=npm_shim_text,
        changelog_text=changelog_text,
    )


def apply_plan(plan: BumpPlan) -> None:
    """Write the four files. Separate from `build_plan()` so a caller (or a
    test) can inspect the computed text before anything touches disk."""
    plan.version_py.write_text(plan.version_py_text, encoding="utf-8")
    plan.pyproject.write_text(plan.pyproject_text, encoding="utf-8")
    plan.npm_shim.write_text(plan.npm_shim_text, encoding="utf-8")
    plan.changelog.write_text(plan.changelog_text, encoding="utf-8")


def _emit_status(status: str) -> None:
    """`status=<applied|skip|error>` to `$GITHUB_OUTPUT` when set, the same
    convention `planner_resync.py` uses for its own `verdict=`/`sdk_head=` --
    so the workflow step that shells this script can tell a real bump apart
    from a legitimate no-op without re-parsing stdout."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"status={status}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tag", required=True, metavar="vX.Y.Z",
        help="the tag release.yml just published",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="write the four files (default: report the computed plan only)",
    )
    args = parser.parse_args(argv)

    released = args.tag[1:] if args.tag.startswith("v") else args.tag
    current = vc.read_tan_version()

    # The one legitimate no-op: `dev` has already moved off the tag (a
    # previous run of this same job already applied the bump, or a human
    # got there first). Distinguished EXPLICITLY from "nothing happened for
    # an unknown reason" -- tan-cli#770 requires the caller be able to fail
    # loudly on the latter, and a status the caller can branch on is what
    # makes that possible without re-deriving this check from a git diff.
    if current != released:
        print(
            f"TAN_VERSION is already {current!r}, not the just-published "
            f"tag's {released!r} -- nothing to bump."
        )
        _emit_status("skip")
        return 0

    try:
        plan = build_plan(released)
    except vc.VersionError as err:
        print(f"::error::{err}", file=sys.stderr)
        _emit_status("error")
        return 1

    print(
        f"TAN_VERSION {current!r} -> {plan.next_version!r} "
        f"(CHANGELOG target `## [{plan.target}]`)"
    )
    if args.apply:
        apply_plan(plan)
        print("applied.")
        _emit_status("applied")
    else:
        print("(dry run -- pass --apply to write the four files)")
        _emit_status("would-apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
