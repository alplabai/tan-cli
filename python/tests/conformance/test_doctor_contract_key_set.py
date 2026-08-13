# SPDX-License-Identifier: Apache-2.0
"""tan-cli#664: keep ``contract/doctor-data-keys.json`` -- the ``doctor``
family the release workflow folds into ``envelope-contract.json`` -- in
lockstep with what ``tan doctor --format json`` actually emits.

Every other family in ``envelope-contract.json`` is a byte golden, checked
against the shipping CLI by ``test_contract_envelopes.py``. ``doctor`` cannot
be: its ``data`` VALUES are host facts (installed tool versions, absolute
paths, which checks even apply on this machine), so the published contract
pins the ``data`` KEY SET only (``contract/doctor-data-keys.json``'s
``dataKeys``), never a value -- see that file's own ``_comment`` for how it
was enumerated.

A key set nobody checks against a real run is exactly the "tautology wearing
a gate's name" tan-cli#664 itself was filed to avoid on the CONSUMER side --
this is the PRODUCER-side half: run the real command, and fail if the
published file over- or under-states what it emits.

Every required/optional key set this test asserts against -- including
``checks[]``'s and ``missingPrerequisites[]``'s -- is read FROM
``contract/doctor-data-keys.json`` itself, never hardcoded here. A prior
revision hardcoded the ``checks[]`` key sets as module-level constants, which
meant the published file's ``checks`` entry could be corrupted in either
direction (a declared key deleted, an unemitted key added) with this test
still green -- the exact drift a lockstep gate exists to catch. Deriving both
from the file is what makes it one.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = Path(__file__).resolve().parents[3] / "contract"
DOCTOR_DATA_KEYS = json.loads(
    (CONTRACT_ROOT / "doctor-data-keys.json").read_text(encoding="utf-8")
)

#: ``checks[]``'s two shapes: always-present fields, and the fields
#: ``Check.as_dict()`` omits rather than nulls when they don't apply (today,
#: only ``fix``). Read from the published file's own ``checks.requiredKeys``/
#: ``checks.optionalKeys`` -- NOT a constant of this test's own -- so a key
#: added, dropped, or moved between the two in the file changes what this
#: test enforces without a second edit here.
_CHECK_REQUIRED_KEYS = frozenset(DOCTOR_DATA_KEYS["dataKeys"]["checks"]["requiredKeys"].keys())
_CHECK_OPTIONAL_KEYS = frozenset(DOCTOR_DATA_KEYS["dataKeys"]["checks"]["optionalKeys"].keys())

#: ``missingPrerequisites[]`` entries' keys, same reasoning: read from the
#: file's own ``missingPrerequisites.items``, not hardcoded.
_MISSING_PREREQ_KEYS = frozenset(DOCTOR_DATA_KEYS["dataKeys"]["missingPrerequisites"]["items"].keys())


def _fresh_dir(tag: str) -> Path:
    """Mirrors ``test_contract_envelopes.py``'s ``fresh_dir``: an isolated
    scratch directory under its own fresh, uniquely named parent, so a real
    ``~/.alp/sdk-default`` or a sibling ``alp-sdk`` checkout in the shared temp
    root can never change what this run reports."""
    parent = Path(tempfile.gettempdir()) / f"tan-doctor-keys-{tag}-{os.getpid()}"
    shutil.rmtree(parent, ignore_errors=True)
    work = parent / "root"
    work.mkdir(parents=True)
    return work


def _run_doctor() -> dict:
    work_dir = _fresh_dir("work")
    home_dir = _fresh_dir("home")
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
            [sys.executable, "-m", "tan", *DOCTOR_DATA_KEYS["args"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=work_dir,
            env=env,
        )
    finally:
        shutil.rmtree(work_dir.parent, ignore_errors=True)
        shutil.rmtree(home_dir.parent, ignore_errors=True)

    assert proc.stderr.strip() == "", (
        f"tan doctor --format json: unexpected stderr:\n{proc.stderr}"
    )
    # exit 0 (healthy) or 4 (doctor.internal-failure aside, an unhealthy host)
    # are both legitimate outcomes on an arbitrary CI runner -- this test cares
    # about the SHAPE of `data`, not the verdict.
    assert proc.returncode in (0, 4), (
        f"tan doctor --format json: unexpected exit {proc.returncode}, "
        f"envelope may not carry the usual data shape\nstdout:\n{proc.stdout}"
    )
    return json.loads(proc.stdout.strip())


def test_doctor_data_key_set_matches_contract_dataKeys():
    envelope = _run_doctor()
    assert envelope["command"] == "doctor"
    data = envelope["data"]
    assert isinstance(data, dict), f"doctor: data is not an object: {data!r}"

    declared_top = set(DOCTOR_DATA_KEYS["dataKeys"].keys())
    assert set(data.keys()) == declared_top, (
        f"tan doctor --format json emitted data keys {sorted(data.keys())}, "
        f"but contract/doctor-data-keys.json declares {sorted(declared_top)} -- "
        "the published contract has drifted from the shipping command. Update "
        "contract/doctor-data-keys.json (and say why in the commit) rather "
        "than loosening this assertion."
    )

    assert isinstance(data["generatedAt"], str)

    declared_summary = set(DOCTOR_DATA_KEYS["dataKeys"]["summary"].keys())
    assert set(data["summary"].keys()) == declared_summary == {"pass", "warn", "fail"}
    for key in declared_summary:
        assert isinstance(data["summary"][key], int) and not isinstance(
            data["summary"][key], bool
        )

    checks = data["checks"]
    assert isinstance(checks, list) and checks, (
        "doctor emitted no checks at all -- the per-check assertions below "
        "would be vacuous, and a real host always has at least one askable "
        "question (hostPython, at minimum)"
    )
    for check in checks:
        keys = set(check.keys())
        missing_required = _CHECK_REQUIRED_KEYS - keys
        assert not missing_required, f"checks[] entry missing {missing_required}: {check}"
        undeclared = keys - _CHECK_REQUIRED_KEYS - _CHECK_OPTIONAL_KEYS
        assert not undeclared, (
            f"checks[] entry carries undeclared key(s) {undeclared} not in "
            f"contract/doctor-data-keys.json: {check}"
        )
        assert isinstance(check["name"], str)
        assert isinstance(check["status"], str)
        assert isinstance(check["scope"], str)
        assert isinstance(check["detail"], str)
        if "fix" in check:
            assert isinstance(check["fix"], str)

    missing_prereqs = data["missingPrerequisites"]
    assert missing_prereqs is None or isinstance(missing_prereqs, list), (
        f"missingPrerequisites must be null or an array, got {missing_prereqs!r} "
        "-- tan.core.bootstrap.reported_missing's own contract is '[] is NEVER "
        "a value here'"
    )
    if isinstance(missing_prereqs, list):
        for entry in missing_prereqs:
            assert set(entry.keys()) == _MISSING_PREREQ_KEYS, entry
            assert isinstance(entry["tool"], str)
            assert entry["command"] is None or isinstance(entry["command"], str)

    next_steps = data["nextSteps"]
    assert isinstance(next_steps, list)
    for step in next_steps:
        assert isinstance(step, str)
