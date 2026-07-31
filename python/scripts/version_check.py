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
* ``Cargo.toml`` is deliberately NOT read. It versions the Rust crates, which no
  longer produce the release assets; release.yml used to gate the tag on it
  (`grep -m1 '^version = ' Cargo.toml`), which is why a v0.5.0 tag failed before
  a single asset was built while three files said 0.5.0 and one said 0.4.1-dev.

Usage:

    python scripts/version_check.py --self          # the three files agree
    python scripts/version_check.py --tag v0.5.0    # ... and the tag matches
    python scripts/version_check.py --selftest      # the mapping itself

No third-party imports: this runs as a bare CI step before anything is
installed, and a version gate that needs a dependency resolved is a gate that
can fail for reasons unrelated to versions.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

#: `python/` -- this script lives in `python/scripts/`.
PYTHON_ROOT = Path(__file__).resolve().parent.parent
#: The repo root, for `npm-shim/`.
REPO_ROOT = PYTHON_ROOT.parent

#: SemVer, restricted to the pre-release spellings this project actually uses.
#: Deliberately NOT the full SemVer grammar: build metadata (`+sha`) and
#: multi-part pre-releases have no PEP 440 rendering that round-trips, so they
#: are rejected loudly here rather than mistranslated at release time.
_SEMVER = re.compile(
    r"^(?P<core>\d+\.\d+\.\d+)"
    r"(?:-(?P<kind>dev|alpha|beta|rc)(?:\.?(?P<num>\d+))?)?$"
)

#: PEP 440, same restricted shape, in any of its legal separator spellings
#: (`0.5.0.dev0`, `0.5.0dev0`, `0.5.0-dev0`, `0.5.0.rc1`, ...).
_PEP440 = re.compile(
    r"^(?P<core>\d+\.\d+\.\d+)"
    r"(?:[-_.]?(?P<kind>a|alpha|b|beta|c|rc|pre|preview|dev)[-_.]?(?P<num>\d+)?)?$",
    re.IGNORECASE,
)

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
    """`0.5.0-dev` -> `0.5.0.dev0`, `0.5.0-rc.2` -> `0.5.0rc2`, `0.5.0` -> `0.5.0`.

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
            f"-rc.N (build metadata is not supported)."
        )
    core, kind, num = match.group("core", "kind", "num")
    if kind is None:
        return core
    pep_kind = _SEMVER_TO_PEP440_KIND[kind]
    number = int(num) if num is not None else 0
    return f"{core}.dev{number}" if pep_kind == "dev" else f"{core}{pep_kind}{number}"


def canonical_pep440(version: str) -> str:
    """The canonical spelling of a PEP 440 version, so `0.5.0-dev0`,
    `0.5.0dev0` and `0.5.0.dev0` compare equal. Mirrors the subset of PEP 440's
    normalisation rules this project can produce, and raises on anything
    outside it rather than guessing."""
    match = _PEP440.match(version.strip())
    if match is None:
        raise VersionError(f"{version!r} is not a PEP 440 version this project uses")
    core, kind, num = match.group("core", "kind", "num")
    if kind is None:
        return core
    pep_kind = _PEP440_KIND_ALIASES[kind.lower()]
    number = int(num) if num is not None else 0
    return f"{core}.dev{number}" if pep_kind == "dev" else f"{core}{pep_kind}{number}"


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


def check(tag: str | None) -> list[str]:
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

    if tag is not None:
        wanted = tag[1:] if tag.startswith("v") else tag
        if wanted != tan_version:
            problems.append(
                f"tag v{wanted} != TAN_VERSION {tan_version!r} in "
                f"python/tan/version.py -- the tag must name the version the "
                f"binary reports, since that is what the extension compares"
            )

    print(f"TAN_VERSION (source of truth) : {tan_version}")
    print(f"python/pyproject.toml         : {pyproject_version} (want {expected_pep440})")
    print(f"npm-shim/package.json         : {shim_version} (want {tan_version})")
    if tag is not None:
        print(f"git tag                       : {tag}")
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

    # Refused rather than mistranslated.
    for bad in ("0.5", "0.5.0+abc123", "0.5.0-dev.1.2", "v0.5.0", "0.5.0-nightly"):
        try:
            semver_to_pep440(bad)
        except VersionError:
            continue
        raise AssertionError(f"{bad!r} should not translate")

    # A SemVer pre-release and its PEP 440 rendering are NOT string-equal --
    # the whole reason this file is not a `grep` in a shell step.
    assert "0.5.0-dev" != semver_to_pep440("0.5.0-dev")
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
        "--selftest", action="store_true", help="check the SemVer/PEP 440 mapping"
    )
    args = parser.parse_args(argv)

    if args.selftest:
        selftest()
        if not (args.self or args.tag):
            return 0

    try:
        problems = check(args.tag)
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
