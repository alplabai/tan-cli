#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Regenerate `python/release-requirements.lock.txt`, the release input freeze.

tan-cli#437: the release build (`release.yml`'s `build` job and
`clean-host.yml`'s `freeze-and-smoke` job) used to run

    pip install ".[monitor]" "pyinstaller>=6.10"

against a live PyPI resolution, with no lock, no hashes, and an unbounded
`pyinstaller` floor. Re-running the SAME tag on a later day could freeze a
DIFFERENT dependency tree -- a different typer/click/rich/pyyaml/jsonschema/
truststore/certifi/pyinstaller -- and ship different executable bytes under an
identical version string. This script is how the lock that fixes that gets
REGENERATED; `python/release-requirements.lock.txt` is the artefact both
release workflows now install from with `pip install --require-hashes`.

Only third-party distributions go in the lock -- never `alp-tan` itself. The
local package is always installed from the checked-out commit
(`pip install --no-deps -e .` in the workflows), which is already exactly
reproducible (it's the git tree at the tag); locking it too would mean
publishing a hash of our own source tree we'd have to keep in sync by hand.

Universal resolution (`--universal`), not a per-platform lock: `uv` resolves
markers (`sys_platform == 'win32'`, `'darwin'`, ...) into ONE file and
attaches every wheel/sdist hash PyPI has for each pinned version, so the same
file verifies a `pip install --require-hashes` on Windows, both macOS legs,
and the glibc-floor Linux container -- matching the four release targets
`release.yml`'s `build` job actually freezes.

WHAT'S LOCKED: the runtime dependency set (`[project.dependencies]` +the
`monitor` extra`pyserial`, since a frozen binary bundles it -- see
`build_binary.sh`'s header) plus the two BUILD-TIME tools every freeze needs
that pyproject.toml deliberately does not declare as a runtime dependency:
`pyinstaller` (and its own transitive hooks/contrib/platform deps -- pulled in
automatically by resolving it here, not hand-listed) and `setuptools` (pip's
build-isolation floor for `[build-system] requires`, tan-cli#382).

USAGE

    uv pip install --quiet uv   # or: pip install uv
    python python/scripts/generate_release_lock.py                          # regenerate, pins held stable
    python python/scripts/generate_release_lock.py --upgrade                # regenerate, every pin moves to today's latest
    python python/scripts/generate_release_lock.py --upgrade-package typer  # regenerate, only the named package(s) move (repeatable)
    python python/scripts/generate_release_lock.py --check                  # verify only, never touches the committed file

Without `--upgrade`/`--upgrade-package`, `uv pip compile` reads the existing
`-o` output file as a PREFERENCE source, so a pin that is still valid against
`pyproject.toml`'s declared ranges is kept exactly as committed even when a
newer release exists on PyPI. That is deliberate for a plain regenerate (run
after editing `dependencies = [...]`, to pick up the new/removed entry
without moving anything else) -- but it means a plain regenerate is NOT how
a pin gets bumped to a newer version. `--upgrade` (every pin) or
`--upgrade-package NAME` (one distribution, repeatable) is what actually
moves a pin. `.github/workflows/release-lock-update.yml`, the
`workflow_dispatch`-only "explicit, reviewed dependency-update workflow"
tan-cli#437 asks for, always runs one of those two -- a plain regenerate
against an already-committed lock would silently re-write the exact bytes
already there and report "no change" even when every dependency has shipped
a newer release since (tan-cli#989 review, finding 1).

`--check` fails (exit 1) and prints a diff-shaped message when the committed
lock no longer matches a fresh resolution of `pyproject.toml`'s declared
dependencies -- e.g. after `dependencies = [...]` gained or lost an entry, or
after a hand-edit pinned something outside its declared range. It seeds its
scratch resolution with a COPY of the committed lock before compiling, so it
makes the exact same preference-vs-upgrade decision a plain (never
`--upgrade`) regenerate would; without that seed it would compile into an
empty scratch directory with no preferences at all and resolve every pin to
today's latest, the OPPOSITE of what a plain regenerate does, which would red
`--check` the day any locked dependency published a release and leave its own
prescribed remediation (re-running the plain regenerate) unable to ever clear
it (tan-cli#989 review, finding 2). Because of that seeding, a `--check`
failure means the INPUT actually changed, not merely that time passed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
PYTHON_DIR = HERE.parents[1]
PYPROJECT = PYTHON_DIR / "pyproject.toml"
LOCK = PYTHON_DIR / "release-requirements.lock.txt"
#: Committed, not generated -- see that file's own header. A tempfile here
#: used to work too, but its randomised name leaked into the lock's own
#: `# via -r <tmpname>.in` provenance comments, which made two back-to-back
#: runs with NO input change produce a spurious diff and made `--check`
#: worthless (a rerun could never match the committed file byte-for-byte).
BUILD_TIME_EXTRAS_IN = HERE.parent / "release-lock-build-time-extras.in"


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        sys.exit(
            "generate_release_lock.py: `uv` is not on PATH. Install it first "
            "(`pip install uv` or https://docs.astral.sh/uv/) -- this script "
            "shells out to `uv pip compile` rather than hand-rolling a "
            "resolver, so the lock it produces is exactly what a real "
            "resolution run gives."
        )
    return uv


def _compile(
    output: Path,
    *,
    upgrade: bool = False,
    upgrade_packages: list[str] | None = None,
) -> None:
    cmd = [
        _uv(),
        "pip",
        "compile",
        PYPROJECT.relative_to(PYTHON_DIR).as_posix(),
        "--extra",
        "monitor",
        BUILD_TIME_EXTRAS_IN.relative_to(PYTHON_DIR).as_posix(),
        "--universal",
        "--generate-hashes",
        "--python-version",
        "3.12",
        "--no-header",
        "-o",
        str(output),
    ]
    # `--upgrade` / `--upgrade-package` are how a pin actually moves -- see
    # the module docstring's USAGE section (tan-cli#989 review, finding 1).
    # Mutually exclusive at the uv level too, but callers here never pass
    # both (main() only ever sets one).
    if upgrade:
        cmd.append("--upgrade")
    for package in upgrade_packages or ():
        cmd.extend(["--upgrade-package", package])
    subprocess.run(cmd, cwd=PYTHON_DIR, check=True)

    header = (
        "# SPDX-License-Identifier: Apache-2.0\n"
        "# Generated by `python python/scripts/generate_release_lock.py`.\n"
        "# DO NOT hand-edit -- run the script again and commit its output.\n"
        "# Installed with `pip install --require-hashes -r <this file>` by\n"
        "# release.yml's `build` job and clean-host.yml's `freeze-and-smoke`\n"
        "# job (tan-cli#437). See that script's module docstring for what is\n"
        "# and is not covered.\n"
    )
    output.write_text(header + output.read_text(encoding="utf-8"), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed lock against a fresh (non-upgrading) resolution "
        "and diff; exit 1 (printing the diff) if they disagree, without touching "
        "the committed file. Not combinable with --upgrade/--upgrade-package.",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="re-resolve every pin to today's latest allowed release, instead of "
        "keeping the committed pins stable. This is how a pin actually moves -- "
        "see the module docstring's USAGE section.",
    )
    parser.add_argument(
        "--upgrade-package",
        dest="upgrade_packages",
        action="append",
        default=[],
        metavar="PACKAGE",
        help="re-resolve only the named distribution to its latest allowed "
        "release, keeping every other pin stable. Repeatable.",
    )
    args = parser.parse_args()

    if args.check and (args.upgrade or args.upgrade_packages):
        parser.error(
            "--check verifies the lock as committed; it never upgrades. "
            "Regenerate with --upgrade/--upgrade-package directly, then run "
            "--check separately to confirm the result was committed."
        )

    if args.check:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "release-requirements.lock.txt"
            if LOCK.exists():
                # Seed the scratch output with a copy of the committed lock so
                # uv reads it as a PREFERENCE source -- exactly like a plain
                # (non --upgrade) regenerate does against the real committed
                # file at `-o release-requirements.lock.txt`. Without this,
                # `--check` compiled into an empty tempdir where no output
                # file existed, got NO preferences, and resolved every pin to
                # today's latest -- the OPPOSITE of what a plain regenerate
                # does, so `--check` reddened on every dependency's routine
                # release and its own prescribed remediation (re-running the
                # plain regenerate) could never clear it (tan-cli#989 review,
                # finding 2).
                shutil.copyfile(LOCK, scratch)
            _compile(scratch)
            old = LOCK.read_text(encoding="utf-8") if LOCK.exists() else ""
            new = scratch.read_text(encoding="utf-8")
            if old == new:
                print(f"OK: {LOCK} matches what pyproject.toml resolves to today.")
                return 0
            import difflib

            diff = "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=str(LOCK),
                    tofile="freshly resolved",
                )
            )
            print(
                "STALE: python/release-requirements.lock.txt no longer matches "
                "a fresh resolution of pyproject.toml's declared dependencies. "
                "Run `python python/scripts/generate_release_lock.py` and "
                "commit the result.\n" + diff,
                file=sys.stderr,
            )
            return 1

    _compile(LOCK, upgrade=args.upgrade, upgrade_packages=args.upgrade_packages)
    print(f"wrote {LOCK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
