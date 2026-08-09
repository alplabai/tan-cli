# SPDX-License-Identifier: Apache-2.0
"""Run the committed ``contract/envelopes`` fixtures against the PYTHON tan and
assert byte-compatibility with the recorded expectations.

These are the same golden FILES the frozen Rust binary is held to by
``crates/tan-cli/tests/contract.rs``, but this harness -- not that one -- is the
SHIPPING gate: the release assets are PyInstaller freezes of ``python/``
(tan-cli#271). Where the two implementations have parted ways the golden follows
the shipping Python CLI and ``contract.rs`` is allowed to go red; the five
``debug-config-preview-*`` cases are exactly that, re-recorded under tan-cli#502
(see each case's ``PROVENANCE.txt`` and ``contract/README.md``, "Known
divergence from ``crates/``").

The MECHANICS below still mirror the Rust harness deliberately, because a
deviation there would produce a false diff rather than a real one -- the one
exception is called out on ``CASE_METADATA``:

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
  the harness metadata files (``CASE_METADATA``) are skipped, and only at the
  top level.
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
#:
#: ``PROVENANCE.txt`` is the one entry the Rust harness's own list does not carry
#: (``crates/`` is frozen and cannot be updated to match). It is documentation --
#: why a golden was re-recorded, against which version, and what it now diverges
#: from -- not an input any case's command reads, so the deviation cannot produce
#: a false diff: the Rust side merely copies one extra text file into a scratch
#: directory whose contents ``debug-config --preview`` never looks at.
CASE_METADATA = frozenset({"args.txt", "expected.json", "expected.exit", "PROVENANCE.txt"})

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
}

#: Fixtures where the Python port DELIBERATELY does more than the frozen Rust
#: oracle, so the shared golden cannot describe both sides at once.
#:
#: This is the opposite direction from :data:`NOT_PORTED` above -- there the
#: port does LESS.
#:
#: **An entry here is a LAST RESORT, not the default handling for a divergence.**
#: A ``strict=True`` xfail fails only on XPASS, so it pins "this envelope differs
#: from its golden somehow" and nothing finer: every OTHER field in that envelope
#: loses its gate for as long as the entry stands. The default handling is to
#: RE-RECORD the golden against the shipping CLI and leave a ``PROVENANCE.txt``
#: beside it saying what moved, when, against which version, why the previous
#: recording was wrong, and that the frozen ``crates/`` harness now disagrees.
#: Five ``debug-config-preview-*`` entries lived here for exactly that reason and
#: were re-recorded under tan-cli#502 -- see those cases' ``PROVENANCE.txt``.
#:
#: The cost the remaining entry accepts, spelled out so the next one is chosen
#: with open eyes: the golden is the CROSS-LANGUAGE contract, the same file holds
#: the frozen Rust binary via ``crates/tan-cli/tests/contract.rs``, and
#: re-recording it reddens that harness (measured on tan-cli#502: the oracle's
#: envelope for each of the five equalled the OLD golden byte-for-byte, so
#: re-recording turns those five ``contract.rs`` cases red until tan-cli#269
#: deletes that gate). Declaring instead keeps ``crates/`` green -- at the price
#: of the SHIPPING side of that fixture having no gate at all. Weigh which side
#: you would rather have unguarded; for a ``data`` value a consumer writes
#: verbatim (``data.configuration``, alp-sdk-vscode#342) the answer is the
#: shipping side, every time.
#:
#: ``strict=True``: an XPASS means the divergence VANISHED -- the shipping
#: behaviour was reverted -- which is a regression that must fail loudly rather
#: than quietly re-green the suite.
DELIBERATE_DIVERGENCE = {
    "validate-offline-clean": (
        "tan-cli#498 defect 1: the two v2 structural checks in `validate_board_text` were "
        "gated on `schemaVersion >= 2`, a condition no conforming alp-sdk project can meet "
        "(alp-sdk pins `LATEST = 1` with an empty migration registry, and 0 of the 100 "
        "board.yaml files under `alp-sdk/examples` declare the key), so they never ran. "
        "They are unconditional now, because `metadata/schemas/board.schema.json` conditions "
        "NOTHING on `schemaVersion` -- its root carries `required: [som, cores]` and "
        "`not: {required: [os]}` outright. This case's INPUT board.yaml is `som:` + `preset:` "
        "with no `cores:`, which alp-sdk's own `scripts/validate_board_yaml.py` refuses with "
        "`error[ALP-B001]: required key 'cores' is missing` at exit 1 (measured, alp-sdk "
        "99e47476) -- so the golden records tan calling a board clean that the SDK rejects, "
        "and the port now answers exit 2 `validate.schema-violation`. Declared rather than "
        "re-recorded for this map's own reason: the file is the CROSS-LANGUAGE contract the "
        "frozen Rust binary is held to by `crates/tan-cli/tests/contract.rs`, and "
        "`crates/tan-core/src/validate.rs:282` carries the identical dead gate. The clean "
        "envelope's SHAPE is covered instead by "
        "`test_validate_command.py::test_text_and_json_formats_are_unchanged`, on a board.yaml "
        "that is genuinely clean."
    ),
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


def _marks_for(case: str) -> list:
    """The xfail marks for one case, from the two declared-exception maps.

    Both are `strict=True`, so an entry that stops applying FAILS rather than
    silently reporting XPASS -- see each map's own comment. A case may appear
    in only one: `NOT_PORTED` means the port does less, `DELIBERATE_DIVERGENCE`
    means it does more, and both at once would be incoherent.
    """
    if case in NOT_PORTED and case in DELIBERATE_DIVERGENCE:
        raise AssertionError(
            f"{case} is declared in BOTH NOT_PORTED and DELIBERATE_DIVERGENCE; "
            "a case cannot be simultaneously unported and deliberately ahead"
        )
    if case in NOT_PORTED:
        return [pytest.mark.xfail(reason=NOT_PORTED[case], strict=True)]
    if case in DELIBERATE_DIVERGENCE:
        return [pytest.mark.xfail(reason=DELIBERATE_DIVERGENCE[case], strict=True)]
    return []


@pytest.mark.parametrize(
    "fixture",
    [
        pytest.param(
            f,
            id=f.name,
            marks=_marks_for(f.name),
        )
        for f in FIXTURES
    ],
)
def test_envelope_matches_expected(fixture):
    case = fixture.name
    # `encoding=`, not the platform locale: these fixtures are the committed
    # contract and the child is decoded as UTF-8 twenty lines below, so reading
    # the expectation as cp1252/cp932 would diff two different decodings the
    # moment a fixture grows one non-ASCII character.
    argv = [line.strip() for line in (fixture / "args.txt").read_text(encoding="utf-8").splitlines()]
    argv = [tok for tok in argv if tok]
    expected_exit = int((fixture / "expected.exit").read_text(encoding="utf-8").strip())
    expected = json.loads((fixture / "expected.json").read_text(encoding="utf-8"))

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
        "deliberate contract change, re-record the fixture against the shipping "
        "CLI and write its PROVENANCE.txt (see contract/README.md, "
        "'Regenerating a golden'), don't just fix the assertion and don't "
        "declare it in DELIBERATE_DIVERGENCE to keep crates/ green"
    )
