# SPDX-License-Identifier: Apache-2.0
"""`tan support-bundle` against the Rust oracle, on the same failing host.

tan-cli#357. The port used to declare its bundled doctor section a deliberate
divergence -- it substituted `doctor_cmd._collect`'s build/flash-readiness
checklist for the oracle's DEBUG-focused report and pinned the exit code at
`SUCCESS`. Measured, that produced:

    Rust:   rc=4, ok=false, exitCode=4, issues=[support-bundle.sdkRoot,
            support-bundle.hostPrerequisites]  (both error)
    Python: rc=0, ok=true,  exitCode=0, issues=[workspace, westResolved,
            hostPython, hostPrerequisites, zephyrSdk, ...]

so automation read `ok: true` next to error-severity issues, and a bundle
attached to a debug failure carried no debugger state at all. This file is the
LIVE cross-check that closed it. The whole `inspect.context` and the whole
doctor check list are compared element-for-element, with two DECLARED
exceptions -- `GIT_LONGPATHS_WIDENED`/`PORT_ONLY_CHECKS` below -- neither of
which used to be honest about what it actually covered (tan-cli#374 findings
2/5, see both constants' own docstrings).

**tan-cli#374 finding 2, the gap this file used to have.** The single
failing-host case below (`test_support_bundle_matches_the_oracle_on_a_failing_
host`) pins its whole `(rc, ok, exitCode)` assertion on a host where
`sdkRoot` already fails on BOTH sides -- so it could not have noticed
`longPaths` ALSO independently flipping the port's exit code, which is
exactly finding 1's bug (`doctor_cmd`'s Windows-only `longPaths` `fail` arm,
tan-cli#306, is not one of the oracle's fail axes at all). Re-running this
file's own comparison with a resolvable `--sdk-root` -- so `sdkRoot` no
longer masks anything -- gave `RUST rc=0 / PY rc=4` before finding 1's fix.
`test_support_bundle_matches_the_oracle_with_a_resolved_sdk` below is that
second, previously-missing case; it is what actually exercises whether the
`GIT_LONGPATHS_WIDENED` exemption two paragraphs down is safe, which the
original single case could not.

**Its own file, not a case in `test_oracle_parity.py`.** Same convention as
`test_flash_oracle_parity.py`/`test_run_oracle_parity.py`/
`test_clean_parity.py`/`test_image_size_oracle.py` -- one command's live
comparison per module.

**Spawns the oracle directly, every run** (like that file's "known divergence"
section), rather than going through `compare()`/`rust_run()`: those replay a
COMMITTED fixture unless `TAN_PARITY_LIVE=1`, and this case's whole value is
that both binaries run on THIS host, whose tool inventory and registry decide
half the verdicts. Skipped only when no oracle binary is built -- `ci.yml`'s
`python` job never runs `cargo build`, `parity.yml`'s `python-tests` job does.
The session-scoped `pinned_oracle` fixture in `conftest.py` still FAILS the run
if the resolved binary is the wrong version.

**`empty_tool_inventory` is applied to BOTH sides**, which that helper's own
docstring permits precisely here: one identical override applied twice,
symmetrically, is not the one-side-pinned trap tan-cli#313/#324 closed. It is
what makes "this host is missing its prerequisites" true on a developer laptop
that has all of them.

**Both cases here are LIVE-ONLY, and that is a hole in the freeze**
(tan-cli#409). They are gated on binary PRESENCE (`_ORACLE_REQUIRED`), not on
fixture availability like `oracle.missing_for_live`, so the moment
tan-cli#269 deletes `crates/` they become passing SKIPS -- a green run that
measures nothing, on the one comparison that pins doctor's check NAMES and
POSITIONS. Both are named in `oracle_fixtures/PARITY-COVERAGE.txt`, the
committed ledger of what this package does not measure, and both are declared
in `test_parity_freeze_completeness.py`'s `_PLATFORM_BOUND` register, which is
the machine-checked half: adding a THIRD live-only case here without declaring
it fails that check rather than passing quietly.

They are not frozen HERE because a fixture would be wrong everywhere but the
capture host: what these compare is `doctor.checks` and the whole
`inspect.context`, and the check list itself is platform-shaped (`longPaths`
exists on Windows and nowhere else) while the statuses read this machine's
tool inventory and, on Windows, its registry. `empty_tool_inventory` pins the
PATH for the first case and cannot pin the platform; the second deliberately
runs on the real PATH. Replaying one OS's capture against another OS's live
port diffs two platforms' genuinely different -- and both correct -- behaviour
(`oracle_fixtures.CAPTURE_PLATFORM` exists for exactly that hazard). A real
freeze means capturing on that platform with the inventory
`oracle_fixtures/PROVENANCE.txt` records and gating the replay on it, i.e.
trading a binary-absence skip for a declared PLATFORM skip. The ledger states
that; nothing here should be read as saying these two are covered once
`crates/` is gone.
"""
import json
from pathlib import Path

import pytest

from . import oracle_fixtures, platform_fixtures
from .oracle import _run, empty_tool_inventory, python_command, rust_binary

#: Read only on the LIVE/CAPTURE path now (tan-cli#409): the oracle side of
#: both cases below REPLAYS from `platform_fixtures`, keyed by this host's
#: own `sys.platform`, so a missing binary is not a reason to skip anything.
RUST = rust_binary()


def _oracle_side(case_id, live_fn, *scrub_roots):
    """The oracle's `[exit_code, envelope, bundle]` for `case_id` on THIS
    platform, or a skip NAMING the platform when it has not been captured
    yet.

    A skip rather than a failure while the store is being filled -- the three
    captures arrive from three different CI runners and cannot land in one
    commit -- but `test_parity_freeze_completeness.py` FAILS the whole time
    any target platform is missing, so "partially captured" cannot quietly
    become the permanent state. That is precisely how this hole persisted.
    """
    try:
        return platform_fixtures.resolve_for_platform(
            case_id, live_fn, scrub_roots=scrub_roots
        )
    except platform_fixtures.Absent as absent:
        pytest.skip(str(absent))

#: The one check whose STATUS may legitimately differ, and the issue that owns
#: the difference. tan-cli#306 widened `doctor_cmd.long_paths_check` to read
#: git's own `core.longpaths` beside the registry, because `west update`
#: clones with git and git ignores `LongPathsEnabled` entirely -- so on
#: Windows the port can `warn` (or, before tan-cli#374 finding 1, `fail`) a
#: host the oracle passes. That is a deliberate, already-shipped improvement
#: in `doctor_cmd`, not a `support-bundle` divergence, and it is NOT an
#: exemption from the check-NAME/POSITION comparison this file exists for:
#: `longPaths` must still be PRESENT on both sides, in the same position --
#: only its STATUS (and the wire issue that follows from it) is excused.
#:
#: **That excusal used to be a real gap, not a cosmetic one** (tan-cli#374
#: finding 2): before `support_bundle_cmd._demote_long_paths_fail` existed,
#: the excused STATUS was exactly the thing that could drive
#: `doctor_cmd.exit_code_for` -- i.e. the `(rc, ok, exitCode)` triple this
#: file's OTHER assertions treat as load-bearing -- and the one scenario
#: this file ran never surfaced it, because `sdkRoot` was already failing
#: there regardless of what `longPaths` did. Now that the port caps
#: `longPaths` at `warn` before it ever reaches this command's verdict (see
#: that function's own docstring), the excusal is back to being purely
#: cosmetic: on the ONE cell where Rust and the pre-cap port disagreed
#: (registry on, git's own flag not), the WORST the port can report post-cap
#: is `warn`, which -- like every OTHER `longPaths` cell -- can never by
#: itself take the port's exit code somewhere the oracle's isn't.
GIT_LONGPATHS_WIDENED = "longPaths"

#: Checks the port emits with NO oracle counterpart at all (tan-cli#374
#: finding 5) -- not a status difference under a shared name, a check the
#: oracle's own bundle never has. Rust folds a rejected
#: `metadata/bootstrap.json` into `hostPrerequisites`'s own `detail` via
#: `manifest_error`; this port reports the same fact as a sibling check
#: instead (`support_bundle_cmd._HOST_CHECK_ORDER`'s own docstring explains
#: why). Declared here, not silently dropped from the comparison: every
#: scenario in this file that resolves an SDK with no real
#: `metadata/bootstrap.json` -- which includes every stub `--sdk-root` this
#: suite uses, since none of them ships a `metadata/` tree -- would otherwise
#: fail the check-NAME-list assertion below for a reason that is not a port
#: bug, or (worse) silently stop comparing names at all if the assertion
#: were loosened the wrong way.
PORT_ONLY_CHECKS = frozenset({"bootstrapManifest"})


@pytest.fixture
def work_dir(tmp_path):
    """A scratch cwd nested under its OWN parent -- `discover_workspace_sdk`
    probes the cwd's PARENT for a sibling `alp-sdk/`, so running directly in
    `tmp_path` would let the `home`/destination directories below decide
    whether either binary finds an SDK."""
    work = tmp_path / "root"
    work.mkdir()
    (work / "board.yaml").write_text("board: {}\n", encoding="utf-8", newline="\n")
    return work


def _bundle(payload: dict) -> dict:
    return json.loads(Path(payload["data"]["outputPath"]).read_text(encoding="utf-8"))


def _statuses(bundle: dict) -> list[tuple[str, str]]:
    return [(c["name"], c["status"]) for c in bundle["doctor"]["checks"]]


def _issue_pairs(payload: dict) -> list[tuple[str, str]]:
    return [(i["code"], i["severity"]) for i in payload["issues"]]


def _expected_issues(checks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """A check list's own warn/fail rows, translated into the wire issue
    shape `_doctor_issues` produces -- shared by both tests below so the two
    issue lists agree wherever the checks do, without either test having to
    restate them."""
    return [
        (f"support-bundle.{name}", "error" if status == "fail" else "warning")
        for name, status in checks
        if status in ("warn", "fail")
    ]


def _assert_doctor_sections_match(r_bundle: dict, p_bundle: dict, r_out: dict, p_out: dict) -> None:
    """The doctor section, checks + wire issues, compared with exactly the
    two declared exceptions this file owns (`GIT_LONGPATHS_WIDENED`,
    `PORT_ONLY_CHECKS` -- see both constants' own docstrings for what each
    excuses and why neither is a blanket pass). Shared by every case in this
    file so a third one does not have to re-derive this."""
    r_checks = _statuses(r_bundle)
    p_checks_raw = _statuses(p_bundle)
    # `PORT_ONLY_CHECKS` first: those names have no oracle counterpart to
    # line up against at all, so they must not even reach the name-list
    # comparison below.
    p_checks = [pair for pair in p_checks_raw if pair[0] not in PORT_ONLY_CHECKS]
    assert [name for name, _ in p_checks] == [name for name, _ in r_checks]
    assert [pair for pair in p_checks if pair[0] != GIT_LONGPATHS_WIDENED] == [
        pair for pair in r_checks if pair[0] != GIT_LONGPATHS_WIDENED
    ]

    assert _issue_pairs(r_out) == _expected_issues(r_checks)
    # The port's own issues are derived from its RAW (unfiltered) check list
    # -- `_doctor_issues` never heard of `PORT_ONLY_CHECKS`, so a
    # `bootstrapManifest` warn genuinely rides on the wire and belongs in
    # this expectation, unlike in the oracle-shaped comparison above.
    assert _issue_pairs(p_out) == _expected_issues(p_checks_raw)


def test_support_bundle_matches_the_oracle_on_a_failing_host(work_dir, tmp_path):
    """The exact case tan-cli#357 reported: same project, no resolvable SDK,
    an empty PATH so the prerequisite probe genuinely finds nothing, separate
    bundle destinations."""
    home = tmp_path / "home"
    path = empty_tool_inventory(tmp_path)
    rust_dest, python_dest = tmp_path / "rust-bundle", tmp_path / "python-bundle"

    def _live_rust():
        code, out = _run(
            [RUST],
            ["support-bundle", "--format", "json", "--destination", str(rust_dest)],
            work_dir,
            home,
            env_overrides={"PATH": path},
        )
        # The bundle FILE is part of the frozen answer, not just the
        # envelope: in replay mode nothing writes `rust_dest`, so a disk read
        # after the fact would find nothing and report the divergence
        # backwards.
        return [code, out, _bundle(out)]

    r_code, r_out, r_bundle_frozen = _oracle_side(
        "failing-host", _live_rust, work_dir, home, tmp_path
    )
    p_code, p_out = _run(
        python_command(),
        ["support-bundle", "--format", "json", "--destination", str(python_dest)],
        work_dir,
        home,
        env_overrides={"PATH": path},
    )

    # The verdict, and the CLI-wide invariant that goes with it: the process
    # exit code, `envelope.exitCode` and `ok` are one fact in three places.
    assert (r_code, r_out["ok"], r_out["exitCode"]) == (4, False, 4)
    assert (p_code, p_out["ok"], p_out["exitCode"]) == (r_code, r_out["ok"], r_out["exitCode"])

    # Both sides take the identical scrub: `_oracle_side` already applied it
    # to the frozen half, so the live half needs it too or `inspect.context`
    # diffs a live scratch path against the recorded placeholder.
    r_bundle = r_bundle_frozen
    p_bundle = oracle_fixtures.scrub(_bundle(p_out), work_dir, home, tmp_path)
    p_out = oracle_fixtures.scrub(p_out, work_dir, home, tmp_path)

    # The resolved debug context, whole -- `projectSelected` and
    # `debuggerExtensions` included, which the port omitted before #357.
    # `generatedAt` is the only key that cannot match: two runs, two clocks.
    assert {k: v for k, v in p_bundle["inspect"]["context"].items() if k != "generatedAt"} == {
        k: v for k, v in r_bundle["inspect"]["context"].items() if k != "generatedAt"
    }

    # The doctor section: same checks, same order, same verdicts -- except the
    # one check tan-cli#306 deliberately widened (see GIT_LONGPATHS_WIDENED).
    _assert_doctor_sections_match(r_bundle, p_bundle, r_out, p_out)
    # The two facts the issue report named by hand, pinned literally so a
    # future refactor cannot satisfy the structural assertions above with an
    # empty check list.
    assert ("support-bundle.sdkRoot", "error") in _issue_pairs(p_out)
    assert ("support-bundle.hostPrerequisites", "error") in _issue_pairs(p_out)


def test_support_bundle_matches_the_oracle_with_a_resolved_sdk(work_dir, tmp_path):
    """tan-cli#374 finding 2: the failing-host case above pins its exit-code
    assertion on the one scenario where `sdkRoot` already fails on BOTH
    sides, which cannot notice `longPaths` ALSO independently flipping the
    port's exit code -- exactly finding 1's bug. This case resolves an SDK
    instead, so nothing else forces a fail, and the oracle answers `rc=0`.

    Real PATH, not `empty_tool_inventory`: the point of this case is a
    HEALTHY host, and an empty PATH would fail `hostPrerequisites` on BOTH
    sides, forcing exitCode 4 regardless of whether finding 1's bug is
    present -- the exact masking this case exists to remove, just from a
    different check. Every `python-tests` CI runner (`ubuntu`/`windows`/
    `macos` `-latest`) ships git/cmake/python/ninja, so this is not
    host-dependent in the environment that actually runs it.

    The stub SDK (`scripts/alp_project.py` only, no `metadata/`) is
    resolvable but ships no `metadata/bootstrap.json` -- which is exactly
    the shape `PORT_ONLY_CHECKS` exists for (tan-cli#374 finding 5): the
    port's `bootstrapManifest` warn fires here on every run.
    """
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("# stub\n", encoding="utf-8", newline="\n")
    home = tmp_path / "home"
    rust_dest, python_dest = tmp_path / "rust-bundle", tmp_path / "python-bundle"

    def argv(dest: Path) -> list[str]:
        return [
            "support-bundle",
            "--format",
            "json",
            "--sdk-root",
            str(sdk),
            "--destination",
            str(dest),
        ]

    def _live_rust():
        code, out = _run([RUST], argv(rust_dest), work_dir, home)
        return [code, out, _bundle(out)]

    r_code, r_out, r_bundle_frozen = _oracle_side(
        "resolved-sdk", _live_rust, work_dir, home, tmp_path
    )
    p_code, p_out = _run(python_command(), argv(python_dest), work_dir, home)
    # NOT scrubbed yet: `_bundle` reads `data.outputPath` off this dict, and a
    # redacted path resolves to nothing on disk. Scrubbed further down, after
    # the bundle has been read.
    p_bundle_raw = _bundle(p_out)
    p_out = oracle_fixtures.scrub(p_out, work_dir, home, tmp_path)

    # The verdict, and the CLI-wide invariant that goes with it -- see the
    # failing-host case above for why these three are one fact, not three.
    # This is the assertion tan-cli#374 finding 1 broke: before its fix, the
    # port answered `(4, False, 4)` here while the oracle answered this.
    assert (r_code, r_out["ok"], r_out["exitCode"]) == (0, True, 0)
    assert (p_code, p_out["ok"], p_out["exitCode"]) == (r_code, r_out["ok"], r_out["exitCode"])
    # The oracle has no fail axis reachable in this scenario at all.
    assert r_out["issues"] == []

    r_bundle = r_bundle_frozen
    p_bundle = oracle_fixtures.scrub(p_bundle_raw, work_dir, home, tmp_path)
    _assert_doctor_sections_match(r_bundle, p_bundle, r_out, p_out)
    # Pinned literally, so a future refactor cannot satisfy the structural
    # assertion above by dropping the check `PORT_ONLY_CHECKS` declares.
    assert ("support-bundle.bootstrapManifest", "warning") in _issue_pairs(p_out)
