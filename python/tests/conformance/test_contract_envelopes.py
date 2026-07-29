# SPDX-License-Identifier: Apache-2.0
"""Run the committed ``contract/envelopes`` fixtures against the PYTHON tan and
assert byte-compatibility with the recorded expectations.

These are the same goldens the Rust binary is held to by
``crates/tan-cli/tests/contract.rs`` -- this is the cross-language conformance
gate. The harness below mirrors that Rust one exactly; every deviation would
produce a false diff rather than a real one:

* ``args.txt`` is **one argv token per line**, deliberately NOT shell-split
  (``contract/README.md``: "avoids quoting ambiguity across platforms"). Blank
  lines are dropped and each line is trimmed.
* Each case runs in a fresh scratch directory nested under its OWN fresh
  parent, ``<temp>/tan-contract-<case>-<pid>/root`` -- never the checkout and
  never directly under the shared temp root, because ``discover_workspace_sdk``
  probes the working directory's PARENT for a sibling ``alp-sdk/``.
* ``HOME``/``USERPROFILE`` point at a second fresh directory so a developer's
  real ``~/.alp/sdk-default`` cannot change what ``sdk current`` reports, and
  ``SOURCE_DATE_EPOCH=0`` pins any timestamped output.
* Fixture inputs are copied into the scratch dir RECURSIVELY (that is what lets
  a case ship a synthetic ``sdk/`` checkout and pass ``--sdk-root ./sdk``); only
  the three harness metadata files are skipped, and only at the top level.
* Normalisation is SCOPED to the path-shaped keys in ``PATH_KEYS``: ``\\`` ->
  ``/`` and then the absolute scratch path down to ``__WORKDIR__``. A blanket
  rewrite over every string leaf would launder a real drift inside
  ``issues[].message``.

Key ORDER is deliberately not asserted -- the Rust side diffs two
``serde_json::Value``s whose map equality is order-insensitive, and Python dict
equality is too. Pin key order in the owning module's own tests, not here.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

#: The package root, pinned onto the subprocess's ``PYTHONPATH``. Each case runs
#: from an isolated scratch directory, so ``python -m tan`` cannot find the
#: package via the cwd -- this is the analogue of the Rust harness's
#: ``CARGO_BIN_EXE_tan`` absolute binary path, and it keeps the suite runnable
#: without a ``pip install``.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

CONTRACT = Path(__file__).resolve().parents[3] / "contract" / "envelopes"
FIXTURES = sorted(p for p in CONTRACT.iterdir() if p.is_dir()) if CONTRACT.is_dir() else []

#: Envelope fields that carry a filesystem path and so need separator
#: normalisation. Verbatim from ``PATH_KEYS`` in ``crates/tan-cli/tests/contract.rs``.
PATH_KEYS = frozenset(
    {
        "root",
        "boardYaml",
        "boardYamlPath",
        "destination",
        "relativePath",
        "sdkPath",
        "sdkPinned",
        "written",
        "unchanged",
        "launchJsonPath",
    }
)

#: The placeholder a golden spells the case's own scratch directory as.
WORK_DIR_TOKEN = "__WORKDIR__"

#: Harness metadata, skipped when copying fixture inputs -- top level only, so a
#: fixture ``sdk/`` subtree containing its own ``args.txt`` is still copied.
CASE_METADATA = frozenset({"args.txt", "expected.json", "expected.exit"})

#: Fixtures whose COMMAND the Python port has not landed yet. The MVP's scope is
#: ``build``; nothing in the committed golden set exercises ``build`` (see
#: ``contract/README.md`` -- ``build --materialise``'s ``data.written`` is
#: explicitly "NOT COVERED" there because reaching it needs a resolvable alp-sdk
#: checkout and a Python spawn). So every case here is pending a later
#: sub-project, and each is listed BY NAME: an unported command must show up as
#: a known gap, never as a skipped suite or a weakened assertion.
#:
#: ``strict=True``: this dict is the port's BACKLOG, so a stale entry is a lost
#: signal. Under ``strict=False`` a fixture that starts genuinely passing reports
#: XPASS and the run stays green -- the command lands, its fixture stays
#: mis-classified as "not ported", and nothing ever forces the correction. Strict
#: turns that XPASS into a FAILURE, so landing a command forces the one-line
#: promotion: delete its entry here. Costs nothing while a case genuinely fails.
NOT_PORTED = {
    "debug-config-preview-baremetal-mcu": "debug-config lands in a later sub-project",
    "debug-config-preview-native-host": "debug-config lands in a later sub-project",
    "debug-config-preview-yocto-userspace": "debug-config lands in a later sub-project",
    "debug-config-preview-zephyr-mcu": "debug-config lands in a later sub-project",
    "examples-catalog": "examples lands in a later sub-project",
    "explain-overview": "explain lands in a later sub-project",
    "generate-board-yaml-missing": "generate lands in a later sub-project",
    "init-invalid-template": "init lands in a later sub-project",
    "init-preview-minimal-app": "init lands in a later sub-project",
    "presets-heterogeneous-som": "presets lands in a later sub-project",
    "presets-no-sdk": "presets lands in a later sub-project",
    "sdk-current-no-sdk": "sdk lands in a later sub-project",
    "sdk-unknown-subcommand": "sdk lands in a later sub-project",
}


def normalise(value, key, work_dir_marker):
    """Scoped ``\\`` -> ``/`` plus ``__WORKDIR__`` substitution on path-shaped
    fields only. ``key`` is the enclosing object field name (``None`` at the
    root); an array inherits its own key, so every string in ``written: [...]``
    is still recognised.

    The marker is the case's unique scratch-dir tail rather than the whole
    absolute prefix: on macOS ``$TMPDIR`` is a symlink that ``getcwd()`` resolves
    through (``/var/...`` -> ``/private/var/...``), so a whole-prefix comparison
    would silently stop matching there and only there.
    """
    if isinstance(value, str):
        if key not in PATH_KEYS:
            return value
        value = value.replace("\\", "/")
        at = value.find(work_dir_marker)
        if at != -1:
            value = WORK_DIR_TOKEN + value[at + len(work_dir_marker) :]
        return value
    if isinstance(value, list):
        return [normalise(item, key, work_dir_marker) for item in value]
    if isinstance(value, dict):
        return {k: normalise(v, k, work_dir_marker) for k, v in value.items()}
    return value


def fresh_dir(tag):
    """``<temp>/tan-contract-<tag>-<pid>/root`` -- an empty scratch directory
    under an empty parent nothing else can plausibly populate."""
    parent = Path(tempfile.gettempdir()) / f"tan-contract-{tag}-{os.getpid()}"
    shutil.rmtree(parent, ignore_errors=True)
    work = parent / "root"
    work.mkdir(parents=True)
    return work


def copy_fixture_inputs(case_dir, work_dir):
    for entry in case_dir.iterdir():
        if entry.name in CASE_METADATA:
            continue
        if entry.is_dir():
            shutil.copytree(entry, work_dir / entry.name)
        else:
            shutil.copy2(entry, work_dir / entry.name)


@pytest.mark.parametrize(
    "fixture",
    [
        pytest.param(
            f,
            id=f.name,
            marks=(
                [pytest.mark.xfail(reason=NOT_PORTED[f.name], strict=True)]
                if f.name in NOT_PORTED
                else []
            ),
        )
        for f in FIXTURES
    ],
)
def test_envelope_matches_expected(fixture):
    case = fixture.name
    argv = [line.strip() for line in (fixture / "args.txt").read_text().splitlines()]
    argv = [tok for tok in argv if tok]
    expected_exit = int((fixture / "expected.exit").read_text().strip())
    expected = json.loads((fixture / "expected.json").read_text())

    work_dir = fresh_dir(case)
    home_dir = fresh_dir(f"{case}-home")
    copy_fixture_inputs(fixture, work_dir)

    env = {
        **os.environ,
        "SOURCE_DATE_EPOCH": "0",
        "HOME": str(home_dir),
        "USERPROFILE": str(home_dir),
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "tan", *argv],
            capture_output=True,
            text=True,
            # Match the Rust harness's `String::from_utf8_lossy`. Bare
            # `text=True` decodes with the platform locale encoding, so Click's
            # stderr on a non-UTF-8-locale Windows runner could raise
            # UnicodeDecodeError -- a harness CRASH masquerading as a contract
            # failure, instead of a clean assertion diff.
            encoding="utf-8",
            errors="replace",
            cwd=work_dir,
            env=env,
        )
    finally:
        shutil.rmtree(work_dir.parent, ignore_errors=True)
        shutil.rmtree(home_dir.parent, ignore_errors=True)

    # Nothing but JSON on stdout under `--format json`; a stray write to either
    # stream is itself a contract break (the extension parses stdout whole).
    assert proc.stderr.strip() == "", f"{case}: unexpected stderr under --format json:\n{proc.stderr}"
    assert proc.returncode == expected_exit, f"{case}: exit code mismatch\nstdout:\n{proc.stdout}"

    actual = json.loads(proc.stdout.strip())
    marker = f"tan-contract-{case}-{os.getpid()}/root"
    actual = normalise(actual, None, marker)

    assert actual == expected, (
        f"{case}: envelope drifted from the committed golden -- if this is a "
        "deliberate contract change, regenerate the fixture (see "
        "contract/README.md), don't just fix the assertion"
    )
