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

A declared-but-never-emitted key is the same drift wearing a different shape,
and an arbitrary host's PATH cannot be trusted to exercise it: a real run's
``checks[]`` carries ``optionalKeys`` (today just ``fix``) only when
something actually failed or warned, and ``missingPrerequisites`` only when
``hostPrerequisites`` found a tool missing -- both are host-verdict-dependent,
so a run on a fully healthy, fully provisioned host would silently never
exercise either path and this test would still pass with `checks.optionalKeys`
or `missingPrerequisites.items` holding a key nothing ever emits (measured:
adding a key to either passed unnoticed -- see tan-cli#664 round-5 review).
A SECOND, deterministic subprocess run (``_run_doctor(force_missing=True)``)
strips ``PATH`` entirely, so every one of the built-in fallback manifest's
required tools (``git``/``cmake``/``python3``/``ninja`` on POSIX,
``git``/``cmake``/``python``/``ninja`` on Windows -- ``_load_manifest``'s own
fallback when no ``alp-sdk`` checkout resolves, which nothing in this
isolated environment does) is unconditionally reported missing:
``hostPrerequisites`` always fails with a ``fix``, and
``data.missingPrerequisites`` is always a non-empty list. That run is what
lets this test assert ``optionalKeys`` were actually observed at least once,
and validate ``missingPrerequisites[]``'s item shape against real data
instead of skipping the assertion whenever this host happened to have
everything installed.
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

#: Whether the file's own ``missingPrerequisites.nullable`` token permits a
#: ``null`` value on the wire. Read, not hardcoded, so flipping it in the
#: file changes what this test enforces without a second edit here.
_MISSING_PREREQ_NULLABLE = bool(DOCTOR_DATA_KEYS["dataKeys"]["missingPrerequisites"]["nullable"])


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


def _run_doctor(*, force_missing: bool = False) -> dict:
    """Spawn a real ``tan doctor --format json``.

    ``force_missing=True`` additionally sets ``PATH`` to empty so
    ``on_path()`` (``doctor_cmd.py`` -- PATH-only, deliberately not
    ``shutil.which``) resolves nothing: every tool the built-in fallback
    manifest requires reports missing, so ``hostPrerequisites`` always fails
    (with a ``fix``) and ``data.missingPrerequisites`` is always a non-empty
    list -- the two paths a healthy or partially-provisioned host might
    never exercise on its own. ``sys.executable`` is still an absolute path
    in ``argv[0]``, so clearing ``PATH`` does not stop the child from
    starting.
    """
    work_dir = _fresh_dir("work-forced" if force_missing else "work")
    home_dir = _fresh_dir("home-forced" if force_missing else "home")
    env = {
        **os.environ,
        "SOURCE_DATE_EPOCH": "0",
        "HOME": str(home_dir),
        "USERPROFILE": str(home_dir),
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    if force_missing:
        env["PATH"] = ""
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


def _assert_check_shape(check: dict) -> set:
    """Both directions of the ``checks[]`` per-entry contract: every
    ``requiredKeys`` entry present, and nothing outside
    ``requiredKeys | optionalKeys``. Returns the entry's own key set so a
    caller can accumulate what got OBSERVED across one or more runs -- the
    half this assertion alone cannot check, since a declared ``optionalKeys``
    member that no entry here ever carries looks identical to one that does.
    """
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
    return keys


def _assert_missing_prereq_entry(entry: dict) -> None:
    assert set(entry.keys()) == _MISSING_PREREQ_KEYS, (
        f"missingPrerequisites[] entry key set {sorted(entry.keys())} != "
        f"contract/doctor-data-keys.json's declared "
        f"missingPrerequisites.items {sorted(_MISSING_PREREQ_KEYS)}: {entry}"
    )
    assert isinstance(entry["tool"], str)
    assert entry["command"] is None or isinstance(entry["command"], str)


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

    # Derived from the file, not hardcoded here -- see the module docstring.
    # The literal {"pass", "warn", "fail"} vocabulary is pinned separately, via
    # dict-literal equality, in
    # test_doctor_command.py::test_unknown_is_counted_in_no_summary_bucket;
    # this assertion only proves this run matches the published contract.
    declared_summary = set(DOCTOR_DATA_KEYS["dataKeys"]["summary"].keys())
    assert set(data["summary"].keys()) == declared_summary
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
        _assert_check_shape(check)

    missing_prereqs = data["missingPrerequisites"]
    assert missing_prereqs is None or isinstance(missing_prereqs, list), (
        f"missingPrerequisites must be null or an array, got {missing_prereqs!r} "
        "-- tan.core.bootstrap.reported_missing's own contract is '[] is NEVER "
        "a value here'"
    )
    if missing_prereqs is None:
        assert _MISSING_PREREQ_NULLABLE, (
            "tan doctor emitted data.missingPrerequisites: null on this host, but "
            "contract/doctor-data-keys.json declares missingPrerequisites.nullable: "
            "false -- either the command must never emit null here, or the "
            "declared nullable flag is wrong"
        )
    else:
        for entry in missing_prereqs:
            _assert_missing_prereq_entry(entry)

    next_steps = data["nextSteps"]
    assert isinstance(next_steps, list)
    for step in next_steps:
        assert isinstance(step, str)


def test_doctor_forced_missing_prerequisites_exercises_optional_and_missing_prereq_paths():
    """The half a healthy-host run cannot prove: that every declared
    ``checks.optionalKeys`` member is actually emitted somewhere, and that
    ``missingPrerequisites[]``'s declared ``items`` shape matches a REAL
    populated entry rather than only ever matching vacuously against `None`.

    ``_run_doctor(force_missing=True)`` strips ``PATH`` so every fallback-
    manifest tool is unconditionally reported missing (see its docstring) --
    deterministic regardless of what is actually installed on the host
    running this test.
    """
    envelope = _run_doctor(force_missing=True)
    assert envelope["command"] == "doctor"
    data = envelope["data"]

    checks = data["checks"]
    assert isinstance(checks, list) and checks
    observed_keys: set = set()
    for check in checks:
        observed_keys |= _assert_check_shape(check)

    declared_optional = _CHECK_OPTIONAL_KEYS
    unobserved_optional = declared_optional - observed_keys
    assert not unobserved_optional, (
        f"contract/doctor-data-keys.json declares checks.optionalKeys "
        f"{sorted(declared_optional)}, but key(s) {sorted(unobserved_optional)} "
        "were never carried by any checks[] entry even on a run with every "
        "prerequisite forced missing (PATH stripped) -- a declared optional "
        "key nothing ever emits is the same drift a lockstep gate exists to "
        "catch, just the other direction. Either the command needs to emit "
        "it, or contract/doctor-data-keys.json should stop declaring it."
    )

    # `hostPrerequisites` fails when PATH resolves nothing (on_path() is
    # PATH-only), which is the ONE path that ever populates
    # `missingPrerequisites` -- see doctor_cmd.py's `prerequisites_check`.
    missing_prereqs = data["missingPrerequisites"]
    assert isinstance(missing_prereqs, list) and missing_prereqs, (
        "forcing every prerequisite missing (PATH stripped) did not produce a "
        f"populated data.missingPrerequisites list -- got {missing_prereqs!r}; "
        "either doctor's prerequisite-detection changed shape, or this "
        "forcing mechanism no longer works and needs a new approach"
    )
    for entry in missing_prereqs:
        _assert_missing_prereq_entry(entry)
