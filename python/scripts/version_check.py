#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""One version, three files, and one explicit SemVer <-> PEP 440 mapping.

THE SOURCE OF TRUTH IS ``python/tan/version.py``'s ``TAN_VERSION``. Not
pyproject.toml, not Cargo.toml, not npm-shim/package.json: ``TAN_VERSION`` is
the string the shipped artifact PRINTS (``tan --version``), it is what
alp-sdk-vscode parses and compares against its ``SUPPORTED_CLI_VERSION``, and a
release whose binary reports a version nobody else agrees with is the one
failure that reaches a customer instead of CI. It is also SemVer, which is what
the git tag is. Everything else derives from it or is checked against it:

* ``python/pyproject.toml``'s ``version`` must be the PEP 440 RENDERING of it
  (``0.5.0-dev`` -> ``0.5.0.dev0``). The two spellings are not interchangeable
  and cannot be string-compared, which is exactly why this file exists.
* ``npm-shim/package.json``'s ``version`` must equal it EXACTLY: postinstall.js
  derives the asset tag as ``TAG = `v${pkg.version}` `` (npm-shim/postinstall.js:25),
  so a stale shim downloads a tag that does not exist. npm ships SemVer, so no
  translation applies here.
* ``Cargo.toml`` is not read, and as of tan-cli#269 does not exist: it versioned
  the Rust crates, which stopped producing the release assets at the port.
  release.yml used to gate the tag on it (`grep -m1 '^version = ' Cargo.toml`),
  which is why a v0.5.0 tag failed before a single asset was built while three
  files said 0.5.0 and one said 0.4.1-dev. Recorded so a future reader does not
  re-add a fourth version file on the theory that one was overlooked.
* ``CHANGELOG.md`` must carry the section ``TAN_VERSION`` will be released
  under. release.yml slices the GitHub Release body out of it by an EXACT
  ``^## \\[<tag minus v>\\]`` match and hard-fails the release job when it
  misses (#212) -- but that is AFTER four PyInstaller freezes, and the tag is
  already pushed and immutable by then. Checked HERE it is a PR-time failure,
  and it is the same fact release.yml needs, read the same way. See
  ``release_target()`` for the one rule that covers both a development version
  and a releasable one.

Two identities that are NOT the same thing (tan-cli#377):

* a RELEASABLE version -- what a tag names, what a customer's binary prints;
* a DEVELOPMENT version -- what an untagged tree between two releases prints.

``dev`` carried the release's number for the whole v0.6 wave, so ``dev`` and the
published ``v0.5.0-rc4`` both answered ``tan 0.5.0-rc4`` while producing
different behaviour, and no bug report could tell them apart. ``--not-released``
is the durable half of that fix: it refuses a tree whose ``TAN_VERSION`` equals
an EXISTING release tag that does not point at HEAD. Bumping without it just
repeats the defect next cycle.

Usage:

    python scripts/version_check.py --self          # the files agree
    python scripts/version_check.py --tag v0.5.0    # ... and the tag matches
    python scripts/version_check.py --not-released  # ... and it is not a tag's
    python scripts/version_check.py --selftest      # the mapping itself

No third-party imports: this runs as a bare CI step before anything is
installed, and a version gate that needs a dependency resolved is a gate that
can fail for reasons unrelated to versions.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

#: `python/` -- this script lives in `python/scripts/`.
PYTHON_ROOT = Path(__file__).resolve().parent.parent
#: The repo root, for `npm-shim/` and `CHANGELOG.md`.
REPO_ROOT = PYTHON_ROOT.parent

#: SemVer, restricted to the pre-release spellings this project actually uses,
#: plus ONE two-part form: a `.devN` tail on a pre-release (`0.5.0-rc5.dev0`).
#: Deliberately still NOT the full SemVer grammar -- build metadata (`+sha`) is
#: rejected, because SemVer 2.0.0 sec. 10 says precedence MUST IGNORE it, so
#: `0.5.0-rc4+dev` would sort EQUAL to the published `0.5.0-rc4` in every
#: SemVer consumer and reproduce exactly the indistinguishability tan-cli#377
#: is about.
#:
#: The `.devN` tail is admitted because it is the only spelling that is legal
#: and correctly ordered in BOTH ecosystems for "development after a published
#: pre-release": `dev` sorts BELOW `rc` in SemVer and in PEP 440 alike, so
#: `0.5.0-dev1` sorts BELOW the already-published `0.5.0-rc4` -- a dev tree
#: claiming to predate the release it was cut from. `0.5.0-rc5.dev0` sorts
#: rc4 < rc5.dev0 < rc5 < 0.5.0 in both. It names its nearest upper bound, not
#: a promise that an rc5 will be cut: PEP 440 reads `X.devN` as "before X", and
#: 0.5.0 is above rc5, so a release cut as 0.5.0 is still above this tree.
_SEMVER = re.compile(
    r"^(?P<core>\d+\.\d+\.\d+)"
    r"(?:-(?P<kind>dev|alpha|beta|rc)(?:\.?(?P<num>\d+))?"
    r"(?:\.dev(?P<dev>\d+))?)?$"
)

#: PEP 440, same restricted shape, in any of its legal separator spellings
#: (`0.5.0.dev0`, `0.5.0dev0`, `0.5.0-dev0`, `0.5.0.rc1`, `0.5.0rc5.dev0`, ...).
#: The trailing dev segment REQUIRES its number here even though PEP 440 would
#: read a bare `.dev` as `.dev0`: nothing in this repo produces the bare form,
#: and matching it would silently swallow the segment (`0.5.0rc5.dev` reading
#: as `0.5.0rc5`) rather than refusing a spelling nobody meant.
_PEP440 = re.compile(
    r"^(?P<core>\d+\.\d+\.\d+)"
    r"(?:[-_.]?(?P<kind>a|alpha|b|beta|c|rc|pre|preview|dev)[-_.]?(?P<num>\d+)?)?"
    r"(?:[-_.]?dev[-_.]?(?P<dev>\d+))?$",
    re.IGNORECASE,
)

#: `## [0.5.0] — 2026-08-02` / `## [0.5.0] — Unreleased`. Only the bracketed
#: version is load-bearing -- release.yml matches `^## \[<version>\]` and stops
#: there, so whatever follows is prose to everything except the Unreleased
#: check below.
_CHANGELOG_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?P<rest>.*)$")

_SEMVER_TO_PEP440_KIND = {"dev": "dev", "alpha": "a", "beta": "b", "rc": "rc"}
_PEP440_KIND_ALIASES = {
    "a": "a",
    "alpha": "a",
    "b": "b",
    "beta": "b",
    "c": "rc",
    "rc": "rc",
    "pre": "rc",
    "preview": "rc",
    "dev": "dev",
}


class VersionError(Exception):
    """A version string this project refuses to translate."""


def semver_to_pep440(version: str) -> str:
    """`0.5.0-dev` -> `0.5.0.dev0`, `0.5.0-rc.2` -> `0.5.0rc2`, `0.5.0` -> `0.5.0`,
    `0.5.0-rc5.dev0` -> `0.5.0rc5.dev0`.

    A missing pre-release number means 0, matching how both ecosystems read a
    bare `dev`/`rc`. `.devN` keeps its dot separator and `aN`/`bN`/`rcN` do not,
    because that is PEP 440's own canonical form -- the point of this function is
    to produce the ONE spelling `pip`/`setuptools` normalise to, so the value in
    pyproject.toml can be compared literally.
    """
    match = _SEMVER.match(version.strip())
    if match is None:
        raise VersionError(
            f"{version!r} is not a SemVer this project renders to PEP 440. "
            f"Expected X.Y.Z optionally followed by -dev / -alpha.N / -beta.N / "
            f"-rc.N, optionally with a .devN development tail on a pre-release "
            f"(-rc5.dev0). Build metadata is not supported."
        )
    core, kind, num, dev = match.group("core", "kind", "num", "dev")
    if kind is None:
        return core  # the regex nests `dev` inside the pre-release group
    pep_kind = _SEMVER_TO_PEP440_KIND[kind]
    number = int(num) if num is not None else 0
    if pep_kind == "dev":
        if dev is not None:
            # `0.5.0-dev1.dev2` has no PEP 440 rendering: a version carries at
            # most one dev segment. Refuse rather than pick one of the two.
            raise VersionError(f"{version!r} carries two development segments")
        return f"{core}.dev{number}"
    rendered = f"{core}{pep_kind}{number}"
    return rendered if dev is None else f"{rendered}.dev{int(dev)}"


def canonical_pep440(version: str) -> str:
    """The canonical spelling of a PEP 440 version, so `0.5.0-dev0`,
    `0.5.0dev0` and `0.5.0.dev0` compare equal. Mirrors the subset of PEP 440's
    normalisation rules this project can produce, and raises on anything
    outside it rather than guessing."""
    match = _PEP440.match(version.strip())
    if match is None:
        raise VersionError(f"{version!r} is not a PEP 440 version this project uses")
    core, kind, num, dev = match.group("core", "kind", "num", "dev")
    if kind is None:
        # `0.5.0.dev0` matches the KIND group, not this one, so a bare core with
        # a trailing dev cannot reach here -- but a future regex edit could make
        # it, and dropping the segment silently is the failure mode this
        # function exists to prevent.
        if dev is not None:
            raise VersionError(f"{version!r} has a development segment and no release")
        return core
    pep_kind = _PEP440_KIND_ALIASES[kind.lower()]
    number = int(num) if num is not None else 0
    if pep_kind == "dev":
        if dev is not None:
            raise VersionError(f"{version!r} carries two development segments")
        return f"{core}.dev{number}"
    rendered = f"{core}{pep_kind}{number}"
    return rendered if dev is None else f"{rendered}.dev{int(dev)}"


def read_tan_version() -> str:
    """`TAN_VERSION` by regex, not by import: this must work in a checkout whose
    dependencies are not installed (and `tan/__init__` may import them)."""
    path = PYTHON_ROOT / "tan" / "version.py"
    match = re.search(
        r'^TAN_VERSION\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.MULTILINE
    )
    if match is None:
        raise VersionError(f"no TAN_VERSION assignment found in {path}")
    return match.group(1)


def read_pyproject_version() -> str:
    path = PYTHON_ROOT / "pyproject.toml"
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    try:
        return data["project"]["version"]
    except KeyError as err:
        raise VersionError(f"no [project] version in {path}") from err


def read_npm_shim_version() -> str:
    path = REPO_ROOT / "npm-shim" / "package.json"
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def release_target(version: str) -> tuple[str, bool]:
    """`(the CHANGELOG version this tree's notes live under, is_development)`.

    A DEVELOPMENT version documents the release it is heading for, not itself:
    `0.5.0-rc5.dev0` and `0.5.0-dev1` both belong under `## [0.5.0]`, because
    nobody writes a changelog section for a snapshot nobody can install. A
    RELEASABLE version is its own section, verbatim -- `v0.5.0-rc4` published a
    body sliced out of `## [0.5.0-rc4]`, and release.yml's regex is anchored,
    so `## [0.5.0]` would not have matched that tag.
    """
    match = _SEMVER.match(version.strip())
    if match is None:
        raise VersionError(f"{version!r} is not a SemVer this project understands")
    core, kind, dev = match.group("core", "kind", "dev")
    if kind == "dev" or dev is not None:
        return core, True
    return version.strip(), False


def read_changelog_headings() -> list[tuple[str, str]]:
    """`[("0.5.0", " — Unreleased"), ("0.5.0-rc4", " — 2026-08-02"), ...]`, in
    file order. Parsed with the same anchored `## [` shape release.yml uses, so
    a heading this function cannot see is one that release cannot slice."""
    path = REPO_ROOT / "CHANGELOG.md"
    headings = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _CHANGELOG_HEADING.match(line)
        if match is not None:
            headings.append((match.group("version"), match.group("rest")))
    if not headings:
        raise VersionError(f"no `## [version]` headings found in {path}")
    return headings


def changelog_problems(tan_version: str) -> list[str]:
    """The section release.yml will slice must exist, and a development tree's
    must still be open. Both halves failed silently before: #212 made a missing
    section fail the RELEASE job, which is four freezes and an immutable tag too
    late, and nothing at all checked that TAN_VERSION and the top section were
    talking about the same release (tan-cli#377: `dev` said `0.5.0-rc4` while
    the changelog said `0.6.0`)."""
    target, is_development = release_target(tan_version)
    for version, rest in read_changelog_headings():
        if version != target:
            continue
        if is_development and "unreleased" not in rest.lower():
            return [
                f"CHANGELOG.md's `## [{target}]` section is already dated "
                f"({rest.strip() or '<no date>'}), but TAN_VERSION "
                f"{tan_version!r} is a development version heading for it. "
                f"Either that release already shipped and TAN_VERSION must move "
                f"on, or the section was dated too early."
            ]
        return []
    return [
        f"CHANGELOG.md has no `## [{target}]` section, so a v{target} tag would "
        f"publish with an empty body -- release.yml slices the release notes by "
        f"an exact `^## [{target}]` match and fails the release job when it "
        f"misses, after the assets are built and the tag is already immutable."
    ]


def git_tags(*args: str) -> list[str]:
    """`git tag --list <args>`, as a list. Raises rather than returning `[]` on
    a git that is absent or angry: an empty tag list is a legitimate answer
    (`--points-at HEAD` on an untagged commit) and must not be confused with
    "git did not run"."""
    try:
        completed = subprocess.run(
            ["git", "tag", "--list", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError as err:
        raise VersionError(f"could not run git in {REPO_ROOT}: {err}") from err
    except subprocess.CalledProcessError as err:
        raise VersionError(
            f"`git tag --list {' '.join(args)}` failed: {err.stderr.strip()}"
        ) from err
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def released_version_problems(
    tan_version: str, all_tags: list[str], head_tags: list[str]
) -> list[str]:
    """An untagged tree must not claim the version of a tag that already
    published (tan-cli#377).

    Pure, so `--selftest` can exercise every branch without building a git
    repository -- the caller supplies the two tag lists.

    An EMPTY `all_tags` is a HARD failure, not a pass. `actions/checkout` fetches
    with `--no-tags` unless asked otherwise, so a workflow that forgets
    `fetch-depth: 0` / `fetch-tags: true` sees no tags at all -- and a check that
    reads that as "no release has ever claimed this version" is a check that
    passes for the one reason it must not: it cannot see its own evidence.
    """
    if not all_tags:
        return [
            "no git tags are visible, so nothing can be proved about which "
            "versions have already been released. A shallow CI checkout fetches "
            "with --no-tags; use `fetch-depth: 0` or `fetch-tags: true`. "
            "Refusing to certify rather than passing blind."
        ]
    tag = f"v{tan_version}"
    if tag in all_tags and tag not in head_tags:
        return [
            f"TAN_VERSION {tan_version!r} is the version of the EXISTING release "
            f"tag {tag}, which does not point at this commit. Two different "
            f"builds would answer `tan {tan_version}` -- the published one and "
            f"this tree -- so no bug report could tell them apart. Give the "
            f"development line its own version in python/tan/version.py -- a "
            f"`.devN` tail on the NEXT pre-release (0.5.0-rc4 -> "
            f"0.5.0-rc5.dev0), which is the only spelling that sorts above the "
            f"published tag in SemVer and PEP 440 alike -- and bring "
            f"pyproject.toml and npm-shim/package.json with it."
        ]
    return []


def check(tag: str | None, *, against_tags: bool = False) -> list[str]:
    """Every disagreement, not just the first: a release engineer fixing one
    file at a time and re-pushing a tag pays a full CI round trip per problem."""
    problems: list[str] = []

    tan_version = read_tan_version()
    expected_pep440 = semver_to_pep440(tan_version)

    pyproject_version = read_pyproject_version()
    try:
        actual_pep440 = canonical_pep440(pyproject_version)
    except VersionError as err:
        problems.append(f"python/pyproject.toml: {err}")
    else:
        if actual_pep440 != expected_pep440:
            problems.append(
                f"python/pyproject.toml version {pyproject_version!r} "
                f"(canonical {actual_pep440!r}) is not the PEP 440 rendering of "
                f"TAN_VERSION {tan_version!r} -- expected {expected_pep440!r}"
            )

    shim_version = read_npm_shim_version()
    if shim_version != tan_version:
        problems.append(
            f"npm-shim/package.json version {shim_version!r} != TAN_VERSION "
            f"{tan_version!r}; postinstall.js downloads assets from tag "
            f"v{shim_version} (see npm-shim/postinstall.js:25)"
        )

    problems.extend(changelog_problems(tan_version))

    if tag is not None:
        wanted = tag[1:] if tag.startswith("v") else tag
        if wanted != tan_version:
            problems.append(
                f"tag v{wanted} != TAN_VERSION {tan_version!r} in "
                f"python/tan/version.py -- the tag must name the version the "
                f"binary reports, since that is what the extension compares"
            )

    # Deliberately opt-in rather than part of the default set: this is the one
    # check that needs the repository's TAGS, and `actions/checkout` fetches
    # with --no-tags by default. Running it unconditionally would fail every
    # workflow that has not asked for them (and passing blind instead is the
    # failure `released_version_problems` refuses).
    all_tags: list[str] = []
    if against_tags:
        all_tags = git_tags()
        problems.extend(
            released_version_problems(
                tan_version, all_tags, git_tags("--points-at", "HEAD")
            )
        )

    changelog_target, _ = release_target(tan_version)
    print(f"TAN_VERSION (source of truth) : {tan_version}")
    print(f"python/pyproject.toml         : {pyproject_version} (want {expected_pep440})")
    print(f"npm-shim/package.json         : {shim_version} (want {tan_version})")
    print(f"CHANGELOG.md section          : ## [{changelog_target}]")
    if tag is not None:
        print(f"git tag                       : {tag}")
    if against_tags:
        print(f"release tags visible          : {len(all_tags)}")
    return problems


def selftest() -> None:
    """The mapping is the part with edge cases, so it gets the assertions. Kept
    in this file rather than the pytest suite so that a CI step which has not
    installed anything can still prove the translation before it uses it."""
    cases = {
        "0.5.0": "0.5.0",
        "0.5.0-dev": "0.5.0.dev0",
        "0.5.0-dev1": "0.5.0.dev1",
        "0.5.0-dev.2": "0.5.0.dev2",
        "0.5.0-rc.1": "0.5.0rc1",
        "0.5.0-rc2": "0.5.0rc2",
        "0.5.0-alpha.3": "0.5.0a3",
        "0.5.0-beta.4": "0.5.0b4",
        # Development after a published pre-release (tan-cli#377). The dot is
        # kept on the way across because PEP 440's canonical form keeps it.
        "0.5.0-rc5.dev0": "0.5.0rc5.dev0",
        "0.5.0-rc.5.dev1": "0.5.0rc5.dev1",
        "0.5.0-beta.4.dev2": "0.5.0b4.dev2",
        "10.20.30": "10.20.30",
    }
    for semver, expected in cases.items():
        actual = semver_to_pep440(semver)
        assert actual == expected, f"{semver} -> {actual}, expected {expected}"

    # Every rendering must survive the canonicaliser unchanged: that identity is
    # what lets `check()` compare a pyproject value literally.
    for expected in cases.values():
        assert canonical_pep440(expected) == expected, expected

    # Separator spellings PEP 440 treats as the same version.
    for spelling in ("0.5.0.dev0", "0.5.0dev0", "0.5.0-dev0", "0.5.0_DEV0"):
        assert canonical_pep440(spelling) == "0.5.0.dev0", spelling
    for spelling in ("0.5.0rc1", "0.5.0.rc1", "0.5.0-c1", "0.5.0preview1"):
        assert canonical_pep440(spelling) == "0.5.0rc1", spelling

    # Refused rather than mistranslated. `0.5.0+abc123` stays refused with the
    # `.devN` tail admitted: SemVer ignores build metadata for precedence, so
    # `+dev` would sort EQUAL to the tag it is trying to be distinguishable
    # from. `0.5.0-dev1.dev2` and `0.5.0-rc5.dev` are the two shapes the wider
    # grammar could otherwise swallow -- two dev segments, and one with no
    # number that would silently read as plain `0.5.0-rc5`.
    for bad in (
        "0.5",
        "0.5.0+abc123",
        "0.5.0-rc4+dev",
        "0.5.0-dev.1.2",
        "0.5.0-dev1.dev2",
        "0.5.0-rc5.dev",
        "v0.5.0",
        "0.5.0-nightly",
    ):
        try:
            semver_to_pep440(bad)
        except VersionError:
            continue
        raise AssertionError(f"{bad!r} should not translate")
    for bad in ("0.5.0rc5.dev", "0.5.0.dev0.dev1", "0.5.0+abc123"):
        try:
            canonical_pep440(bad)
        except VersionError:
            continue
        raise AssertionError(f"{bad!r} should not canonicalise")

    # A SemVer pre-release and its PEP 440 rendering are NOT string-equal --
    # the whole reason this file is not a `grep` in a shell step.
    assert "0.5.0-dev" != semver_to_pep440("0.5.0-dev")

    # Which CHANGELOG section each identity is released under. A development
    # version documents the release it is heading FOR; a releasable one is its
    # own section, verbatim, because release.yml's slice regex is anchored.
    for version, expected in {
        "0.5.0-rc5.dev0": ("0.5.0", True),
        "0.5.0-dev1": ("0.5.0", True),
        "0.5.0-rc4": ("0.5.0-rc4", False),
        "0.5.0": ("0.5.0", False),
    }.items():
        actual = release_target(version)
        assert actual == expected, f"{version} -> {actual}, expected {expected}"

    # tan-cli#377: the untagged-tree-claiming-a-released-version gate. Pure, so
    # every branch is exercised here rather than against a built git repo.
    tags = ["v0.4.1", "v0.5.0-rc4"]
    assert released_version_problems("0.5.0-rc5.dev0", tags, []) == []
    # The defect itself: `dev` sat on the published rc4's version, untagged.
    assert released_version_problems("0.5.0-rc4", tags, [])
    # ... and the SAME version is fine on the commit the tag points at, which is
    # what release.yml's own `--tag` run is.
    assert released_version_problems("0.5.0-rc4", tags, ["v0.5.0-rc4"]) == []
    # A release being PREPARED claims a version no tag has yet. Allowed: the tag
    # is pushed after this gate is green, not before.
    assert released_version_problems("0.5.0", tags, []) == []
    # No tags visible is a REFUSAL, never a pass -- see the function's docstring.
    assert released_version_problems("0.5.0-rc4", [], [])
    print("selftest: OK")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tag", metavar="vX.Y.Z", help="also require this git tag to match"
    )
    parser.add_argument(
        "--self", action="store_true", help="check the files against each other"
    )
    parser.add_argument(
        "--not-released",
        action="store_true",
        dest="not_released",
        help="also require TAN_VERSION not to be an existing release tag's "
        "version (needs the repo's tags: fetch-depth 0 / fetch-tags true)",
    )
    parser.add_argument(
        "--selftest", action="store_true", help="check the SemVer/PEP 440 mapping"
    )
    args = parser.parse_args(argv)

    if args.selftest:
        selftest()
        if not (args.self or args.tag or args.not_released):
            return 0

    try:
        problems = check(args.tag, against_tags=args.not_released)
    except VersionError as err:
        print(f"::error::{err}", file=sys.stderr)
        return 1

    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    if problems:
        print(
            f"\n{len(problems)} version disagreement(s). The source of truth is "
            f"TAN_VERSION in python/tan/version.py; change that, then bring the "
            f"others in line.",
            file=sys.stderr,
        )
        return 1
    print("versions agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
