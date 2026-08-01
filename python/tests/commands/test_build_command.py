# SPDX-License-Identifier: Apache-2.0
"""``tan build`` end to end, driven as a real subprocess.

Every case here spawns ``python -m tan`` rather than calling the command
function, for the same reason ``crates/tan-cli/tests/contract.rs`` spawns
``CARGO_BIN_EXE_tan``: the load-bearing part of this command is not its return
value but its *framing* -- one JSON document on stdout and nothing else, the
exit code, and the guarantee that no failure escapes as a traceback. An
in-process call exercises none of those.

The plan fixtures are the REAL ones the Rust parity harness uses
(``tests/parity/oracle/*.build-plan.json`` at the repo root, captured from a
live ``alp_orchestrate --emit build-plan``), not hand-written stand-ins --
``multicore_rpmsg-imx93`` already IS the shape the ordering/skip cases need: two
slices, one with ``command: null``, and a matching ``warnings[]`` entry. Only
the case that must actually SPAWN something uses a synthetic plan, because it
needs a tool that exists on every host (``sys.executable``).

**Every case names its MODE.** ``--plan-from`` is a plan SOURCE, not a build
trigger: on its own -- and with ``--native`` too, which does NOT override it --
it shows the plan and writes nothing (v0.4.1's implied ``--plan``),
``--materialise`` writes and stops, and ``--execute`` (this port's own added
flag, which v0.4.1 has no equivalent of) writes and then dispatches. A case
that dropped its mode flag would silently stop testing what its name says --
so the write cases carry ``--materialise``, the dispatch cases carry
``--execute``, and the gate cases in the ``--plan-from`` section below pin the
modes themselves against the captured oracle measurements.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

#: ``python/`` -- pinned onto the child's PYTHONPATH so ``python -m tan``
#: resolves from a scratch cwd without a ``pip install``.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

#: Repo root; the captured real plans live under ``tests/parity/oracle/``.
REPO_ROOT = PACKAGE_ROOT.parent
ORACLE_PLANS = REPO_ROOT / "tests" / "parity" / "oracle"


def real_plan(name: str) -> dict:
    """Load a captured real build plan by name. A missing fixture RAISES --
    a silently skipped case would let this suite certify nothing."""
    path = ORACLE_PLANS / f"{name}.build-plan.json"
    if not path.is_file():
        raise RuntimeError(
            f"missing oracle plan {path}; this suite is grounded in the real "
            "captured plans and must not fall back to a synthetic one"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def run_tan(*argv, cwd, scrub_path=False):
    """Spawn the port. ``scrub_path`` empties ``PATH`` so no plan tool
    (``west``/``bitbake``) can possibly resolve -- a slice must then take the
    ``executionPolicy.missingTool`` branch instead of launching a real
    multi-minute Zephyr/Yocto build on whichever developer machine runs this."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    if scrub_path:
        env["PATH"] = ""
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
    )


def envelope_of(proc):
    """Parse the ONE envelope stdout is allowed to carry, with the failure
    message a bare ``json.loads`` would not give."""
    assert proc.stdout.strip(), f"no envelope on stdout; stderr was:\n{proc.stderr}"
    try:
        return json.loads(proc.stdout)
    except ValueError as err:  # noqa: TRY003 -- diagnostic, not control flow
        raise AssertionError(
            f"stdout is not exactly one JSON document ({err})\nstdout:\n{proc.stdout}"
        ) from err


def write_plan(directory: Path, plan: dict) -> Path:
    path = directory / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8", newline="")
    return path


@pytest.fixture
def project(tmp_path):
    """A scratch project root: the build root slices run under."""
    root = tmp_path / "proj"
    root.mkdir()
    return root


# --- the two-slice case the ordering contract is stated on ------------------

#: A slice command that asserts every path handed to it already exists, and
#: prints what it found. Run as the FIRST slice's tool, it is the direct
#: observation of I-20: the process that checks is the first thing this
#: command ever spawns, so anything it sees was written before dispatch began.
ARTEFACT_PROBE = (
    "import sys;"
    "missing=[p for p in sys.argv[1:] if not __import__('os').path.exists(p)];"
    "print('\\n'.join('present '+p for p in sys.argv[1:] if p not in missing));"
    "print('\\n'.join('MISSING '+p for p in missing));"
    "sys.exit(1 if missing else 0)"
)


def two_slice_plan(probe_args):
    """Two slices: one that really runs (the artefact probe) and one carrying
    ``command: null`` plus its matching ``warnings[]`` entry -- I-11's shape.
    Slice order is `sorted(coreId)` as the SDK emits it (I-06); `aaa_probe`
    sorts first, so the probe IS the first dispatch."""
    def slice_(core_id, command, artefacts):
        return {
            "coreId": core_id,
            "backend": "zephyr",
            "buildDir": f"build/{core_id}-zephyr",
            "appDir": None,
            "configArtefacts": artefacts,
            "toolchain": {"id": "zephyr"},
            "artifacts": {"elf": None},
            "debug": {"console": "rtt"},
            "command": command,
            "env": {},
            "envAppendPath": {},
        }

    return {
        "schemaVersion": 1,
        "generatedBy": "tests/commands/test_build_command.py",
        "boardYaml": "board.yaml",
        "sku": "E1M-TEST",
        "buildRoot": "build",
        "executionPolicy": {
            "unknownBackend": "fail",
            "missingTool": "skip",
            "nullCommand": "skip",
        },
        "sharedArtefacts": [
            {"path": "build/generated/alp/system_ipc.h", "contents": "/* shared */\n"}
        ],
        "slices": [
            slice_(
                "aaa_probe",
                {"tool": sys.executable, "args": ["-c", ARTEFACT_PROBE, *probe_args], "cwd": None},
                [{"path": "build/aaa_probe-zephyr/alp.conf", "contents": "CONFIG_A=y\n"}],
            ),
            slice_(
                "zzz_nocmd",
                None,
                [{"path": "build/zzz_nocmd-zephyr/alp.conf", "contents": "CONFIG_Z=y\n"}],
            ),
        ],
        "warnings": [
            {
                "code": "no-command",
                "coreId": "zzz_nocmd",
                "message": "no build command for core 'zzz_nocmd'",
            }
        ],
    }


#: Everything the plan promises will be on disk. The probe is handed ALL of
#: them -- including the OTHER slice's config artefact, which is the half of
#: I-20 a per-slice "materialise just before you run me" implementation would
#: still get wrong.
ALL_ARTEFACTS = (
    "build/generated/alp/system_ipc.h",
    "build/aaa_probe-zephyr/alp.conf",
    "build/zzz_nocmd-zephyr/alp.conf",
)


def test_a_two_slice_plan_reports_one_executed_and_one_skipped(project):
    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json", cwd=project
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    assert env["command"] == "build"
    assert env["ok"] is True
    assert env["exitCode"] == 0

    slices = env["data"]["slices"]
    assert [s["coreId"] for s in slices] == ["aaa_probe", "zzz_nocmd"]
    assert slices[0]["status"] == "ok"
    assert slices[1]["status"] == "skipped"


def test_every_artefact_is_on_disk_before_the_first_slice_is_spawned(project):
    # I-20. The probe IS the first slice dispatched; it exits non-zero the
    # moment any artefact -- shared, its own, or the OTHER slice's -- is not
    # yet on disk, which turns a late materialise into a failed slice and a
    # non-zero build.
    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json", cwd=project
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    assert env["data"]["slices"][0]["status"] == "ok", env

    # The probe's own report, proving it actually looked rather than passing
    # vacuously on an empty argv.
    assert "MISSING" not in proc.stderr, proc.stderr
    for artefact in ALL_ARTEFACTS:
        assert f"present {artefact}" in proc.stderr, proc.stderr


def test_the_ordering_probe_can_actually_fail(project):
    # A check that cannot go red is not evidence. Point the probe at a path
    # the plan never materialises: the slice must fail and the build exit 1.
    plan = write_plan(project, two_slice_plan(("build/never-written.conf",)))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json", cwd=project
    )

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert env["data"]["slices"][0]["status"] == "failed"
    assert any(i["code"] == "build.slice-failed" for i in env["issues"]), env["issues"]


def test_slice_output_goes_to_stderr_and_stdout_stays_one_envelope(project):
    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json", cwd=project
    )

    # `json.loads` on the WHOLE of stdout: a second document, a log line or a
    # stray progress byte all fail here, which is exactly the break that
    # renders nothing in the extension.
    envelope_of(proc)
    assert "present " in proc.stderr, "slice output must be on stderr, not swallowed"


# --- the real captured plans ------------------------------------------------


def test_a_null_command_slice_survives_with_its_warning(project):
    # I-11, on the real plan that has this shape: `m33` carries `command:
    # null` plus a `board-tree-missing` warning naming why. Neither the slice
    # nor the warning may be dropped. `a55_cluster` carries a REAL `bitbake`
    # command, which also skips under `scrub_path` (tan-cli#283: this fixture
    # therefore has NOTHING built), so the envelope is a coded refusal now --
    # see `test_a_wholly_skipped_build_refuses_not_reports_success` for the
    # dedicated coverage of that half; this test stays about I-11 alone.
    plan_doc = real_plan("multicore_rpmsg-imx93")
    plan = write_plan(project, plan_doc)
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json",
        cwd=project, scrub_path=True,
    )

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert env["ok"] is False

    slices = env["data"]["slices"]
    assert [s["coreId"] for s in slices] == ["a55_cluster", "m33"]
    m33 = next(s for s in slices if s["coreId"] == "m33")
    assert m33["status"] == "skipped"

    warnings = env["data"]["warnings"]
    assert [w["code"] for w in warnings] == ["board-tree-missing"]
    assert warnings[0]["coreId"] == "m33"


def test_no_slice_is_filtered_out_however_few_cores_the_customer_named(project):
    # I-04. The planner fans out over the SoC's cores; this AEN plan carries
    # three slices, one of them a Yocto slice on the A-cluster the customer's
    # board.yaml need never have mentioned. The CLI must report all three --
    # tan-cli#283: this fixture is also the exact all-skipped repro (every
    # slice's tool absent under `scrub_path`), covered end to end by
    # `test_a_wholly_skipped_build_refuses_not_reports_success` below; this
    # test stays about I-04 (all three slices present, none filtered).
    plan = write_plan(project, real_plan("multicore_rpmsg-aen"))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json",
        cwd=project, scrub_path=True,
    )

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert env["ok"] is False
    slices = env["data"]["slices"]
    assert [s["coreId"] for s in slices] == ["a32_cluster", "m55_he", "m55_hp"]
    assert [s["backend"] for s in slices] == ["yocto", "zephyr", "zephyr"]
    assert all(s["status"] == "skipped" for s in slices), slices


# --- tan-cli#283: zero-built is a refusal, a partial build still succeeds ---


def test_a_wholly_skipped_build_refuses_not_reports_success(project):
    """The exact tan-cli#283 repro: every slice of a real captured plan
    skipped for a missing tool (`bitbake`/`west` absent from `scrub_path`'s
    emptied PATH) used to report `ok: true, exitCode: 0, issues: []` -- a
    JSON consumer could not tell that from a real build. Now a coded refusal:
    `exitCode` 1, `ok: false`, `build.nothing-built` plus one `build.missing-
    tool` issue PER skipped slice, each naming its tool."""
    plan = write_plan(project, real_plan("multicore_rpmsg-aen"))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json",
        cwd=project, scrub_path=True,
    )

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert env["ok"] is False
    assert env["exitCode"] == 1

    codes = [i["code"] for i in env["issues"]]
    assert codes[0] == "build.nothing-built", env["issues"]
    assert codes.count("build.missing-tool") == 3, env["issues"]

    missing_tool_issues = [i for i in env["issues"] if i["code"] == "build.missing-tool"]
    assert any("bitbake" in i["message"] and "a32_cluster" in i["message"]
               for i in missing_tool_issues), missing_tool_issues
    assert any("west" in i["message"] and "m55_he" in i["message"]
               for i in missing_tool_issues), missing_tool_issues
    assert any("west" in i["message"] and "m55_hp" in i["message"]
               for i in missing_tool_issues), missing_tool_issues
    assert all(i["severity"] == "warning" for i in missing_tool_issues), missing_tool_issues


def test_a_partial_build_where_only_some_slices_are_skipped_still_reports_ok(project):
    """A user deliberately building only the Zephyr side on a host with no
    Yocto toolchain must NOT need a flag: at least one slice built, so this
    stays `ok: true` -- but the skipped slice(s) still land in `issues[]`,
    naming their missing tool, so a consumer reading only `issues[]` can
    still tell "2 of 3" from "3 of 3"."""
    plan_doc = two_slice_plan(ALL_ARTEFACTS)
    # Swap the null-command second slice for a real one naming a tool that
    # cannot exist on any host, so it takes the missing-tool branch (not the
    # null-command one) while the first slice (sys.executable) still runs.
    plan_doc["slices"][1]["command"] = {
        "tool": "definitely-not-a-real-tool-tan-cli-283",
        "args": [],
        "cwd": None,
    }
    plan = write_plan(project, plan_doc)
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json", cwd=project
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    assert env["ok"] is True
    assert env["exitCode"] == 0

    statuses = {s["coreId"]: s["status"] for s in env["data"]["slices"]}
    assert statuses == {"aaa_probe": "ok", "zzz_nocmd": "skipped"}, env["data"]

    assert not any(i["code"] == "build.nothing-built" for i in env["issues"]), env["issues"]
    missing_tool = [i for i in env["issues"] if i["code"] == "build.missing-tool"]
    assert len(missing_tool) == 1, env["issues"]
    assert "definitely-not-a-real-tool-tan-cli-283" in missing_tool[0]["message"]
    assert "zzz_nocmd" in missing_tool[0]["message"]
    assert missing_tool[0]["severity"] == "warning"


def test_text_mode_summary_line_cannot_be_scrolled_past(project):
    """`tan build --format text` (the default) prints one status line per
    slice, then a summary line naming built-vs-total -- tan-cli#283's human-
    path half, so the all-skipped repro's own text-mode transcript (the
    original bug report) is not just three easy-to-miss `skipped:` lines with
    a `0` exit code nowhere in sight."""
    plan = write_plan(project, real_plan("multicore_rpmsg-aen"))
    proc = run_tan("build", "--plan-from", str(plan), "--execute", cwd=project, scrub_path=True)

    assert proc.returncode == 1, proc.stderr
    lines = proc.stderr.splitlines()
    assert "0 of 3 slice(s) built" in lines, proc.stderr


def declared_artefacts(plan_doc: dict) -> list[str]:
    """Every path the plan itself says will be written, in materialise order
    (shared first, then per slice). Read out of the fixture rather than
    hardcoded, so a re-captured plan cannot leave these cases asserting paths
    the SDK no longer emits."""
    paths = [a["path"] for a in plan_doc["sharedArtefacts"]]
    for sl in plan_doc["slices"]:
        paths.extend(a["path"] for a in sl["configArtefacts"])
    assert paths, "fixture carries no artefacts; these cases would pass vacuously"
    return paths


# --- the `--plan-from` mode gate (measured against the v0.4.1 oracle) --------
#
# ORACLE MEASUREMENTS, re-taken in FRESH temp dirs against the shipped
# `tan 0.4.1-dev` binary on this same `multicore_rpmsg-aen` fixture:
#
#   build --plan-from p.json                -> rc 0, 0 files, data = the plan
#   build --plan-from p.json --native       -> rc 0, 0 files, data = the plan
#   build --plan-from p.json --materialise  -> rc 0, 6 files,
#                                              data = {schemaVersion, baseDir, written}
#
# All three are pinned below as PARITY assertions. `--execute` (the section
# after) is this port's own added flag and deliberately has no oracle row.


@pytest.mark.parametrize("also", [(), ("--native",)], ids=["bare", "with-native"])
def test_plan_from_shows_the_plan_and_writes_nothing_even_with_native(project, also):
    """ORACLE-PARITY ASSERTION. Do NOT "fix" this by making ``--native`` win.

    v0.4.1's ``--plan-from`` help says "Implies ``--plan``", and the oracle
    means it -- against an EXPLICIT ``--native`` too. Both rows above: rc=0,
    ZERO files, ``data`` IS the plan.

    Two earlier revisions of this port each broke one of those rows. First a
    bare ``--plan-from`` materialised AND dispatched, so a v0.4.1 script that
    only ever INSPECTED a plan silently wrote six files into the customer's
    tree. Then ``--native`` was given precedence, so ``--plan-from --native``
    -- which also wrote nothing under v0.4.1 -- wrote six files and ran the
    slices. Same class of surprise, same answer: a v0.4.1 argv keeps its
    v0.4.1 meaning. The capability the second revision was reaching for is
    real, and is now ``--execute`` (next section) -- a flag of its own, not a
    redefinition of an argv that already meant something else.
    """
    plan_doc = real_plan("multicore_rpmsg-aen")
    plan = write_plan(project, plan_doc)
    proc = run_tan(
        "build", "--plan-from", str(plan), *also, "--format", "json",
        cwd=project, scrub_path=True,
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    # Not "no build/ dir" -- NOTHING new on disk beside the plan file itself.
    assert sorted(p.name for p in project.iterdir()) == ["plan.json"], sorted(
        str(p.relative_to(project)) for p in project.rglob("*")
    )
    # `data` is the plan, verbatim: same keys, same order, same values. The
    # oracle passes the document straight through rather than round-tripping
    # it through its own structs (measured: an unknown top-level key survives
    # into `data`), so a consumer reading a newer SDK's plan through
    # `tan build --plan-from` loses nothing.
    assert env["data"] == plan_doc
    assert list(env["data"]) == list(plan_doc)


def test_plan_from_with_materialise_writes_the_plans_files_and_stops(project):
    """The oracle's ``--materialise``: rc=0, the plan's six files on disk, and
    ``data`` = ``{schemaVersion, baseDir, written}`` -- no ``slices``, because
    nothing is dispatched. This is where the port's write path now lives."""
    plan_doc = real_plan("multicore_rpmsg-aen")
    plan = write_plan(project, plan_doc)
    proc = run_tan(
        "build", "--plan-from", str(plan), "--materialise", "--format", "json",
        cwd=project, scrub_path=True,
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    assert list(env["data"]) == ["schemaVersion", "baseDir", "written"], env["data"]

    expected = declared_artefacts(plan_doc)
    assert env["data"]["written"] == expected
    for rel in expected:
        assert (project / rel).is_file(), f"{rel} was not materialised"
    # Materialise is not a build: the executor never ran, so the envelope
    # carries no slice results to mistake for one.
    assert "slices" not in env["data"]


# --- `--execute`: this port's own addition, not a v0.4.1 flag ---------------
#
# The oracle cannot dispatch a plan read from a file AT ALL -- `--plan-from`'s
# implied `--plan` outranks `--native` there (pinned above). That is a
# LIMITATION of v0.4.1, not a contract: taking a pinned, reviewed plan and
# running it reproducibly is an ordinary hermetic-CI request. `--execute`
# serves it without touching what any v0.4.1 argv means, which is exactly why
# these cases have no oracle row to match and must not be "fixed" into one.


def test_execute_materialises_and_dispatches_a_file_supplied_plan(project):
    plan_doc = two_slice_plan(ALL_ARTEFACTS)
    plan = write_plan(project, plan_doc)
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json", cwd=project
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    # Materialised: `--execute` IMPLIES writing -- nothing can run that was
    # never written, so the caller does not also have to pass `--materialise`
    # (and passing both is refused, below).
    for rel in declared_artefacts(plan_doc):
        assert (project / rel).is_file(), f"{rel} was not materialised"
    # AND dispatched -- the half `--materialise` stops short of, and the half
    # the oracle has no argv for at all: the probe slice really ran.
    statuses = {s["coreId"]: s["status"] for s in env["data"]["slices"]}
    assert statuses == {"aaa_probe": "ok", "zzz_nocmd": "skipped"}, env["data"]


def test_execute_reports_the_ordinary_build_shape_not_a_fourth_one(project):
    """``--execute``'s ``data`` is the SAME shape a plain ``tan build``
    returns -- ``{schemaVersion, baseDir, slices, warnings}``. It IS a native
    build; only the plan's SOURCE differs, so no consumer should have to
    switch on a fourth ``data`` shape just to read slice outcomes.

    ``written`` is deliberately absent rather than merged in from
    ``--materialise``: it would be byte-for-byte the pinned plan's own
    declared artefact paths, which the one caller who supplied that file
    already holds.
    """
    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json", cwd=project
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    assert list(env["data"]) == ["schemaVersion", "baseDir", "slices", "warnings"], env["data"]
    # `ok`/`exitCode` agree with each other and with the process.
    assert env["ok"] is True
    assert env["exitCode"] == 0


def test_execute_without_plan_from_changes_nothing(project):
    # `--execute` only overrides the `--plan` IMPLIED by `--plan-from`; with no
    # plan file the run already builds, so the flag must be a no-op rather than
    # a second code path. Whatever a bare `tan build` does in this scratch tree
    # (no SDK resolves here, so: refuses), `--execute` must do identically.
    bare = run_tan("build", "--format", "json", cwd=project)
    with_execute = run_tan("build", "--execute", "--format", "json", cwd=project)
    assert bare.returncode == with_execute.returncode, (bare.stdout, with_execute.stdout)
    assert envelope_of(bare)["issues"] == envelope_of(with_execute)["issues"]


def test_execute_with_materialise_is_a_coded_refusal_not_a_precedence_win(project):
    """Two different answers to "what does this run leave behind": one stops
    after writing, one goes on to dispatch. Letting either win silently is the
    exact defect ``--execute`` exists to replace, so the pair is refused with
    its own code at exit 2 -- this CLI's position for an invalid invocation,
    the same one ``--format bogus`` takes."""
    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--materialise",
        "--format", "json", cwd=project,
    )

    env = envelope_of(proc)
    assert proc.returncode == 2, env
    assert env["command"] == "build"
    assert env["ok"] is False
    assert env["exitCode"] == 2
    assert [i["code"] for i in env["issues"]] == ["build.conflicting-flags"], env["issues"]
    # Refused before any work: neither mode half-ran.
    assert not (project / "build").exists()
    assert "Traceback" not in proc.stderr


def test_execute_with_a_deferred_flag_is_refused_as_deferred(project):
    """The other conflicting shape. ``--plan`` is not implemented in this
    build at all, so it cannot be honoured in ANY combination -- the deferral
    is the accurate answer and is checked FIRST, ahead of the conflict pair
    above. Still a coded refusal, still never a silent precedence win."""
    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--plan",
        "--format", "json", cwd=project,
    )

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert [i["code"] for i in env["issues"]] == ["cli.command-deferred"], env["issues"]
    assert not (project / "build").exists()


# --- every failure is an envelope, never a traceback ------------------------


def test_a_missing_plan_file_is_a_coded_envelope(project):
    proc = run_tan(
        "build", "--plan-from", str(project / "nope.json"), "--format", "json", cwd=project
    )
    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert env["ok"] is False
    assert [i["code"] for i in env["issues"]] == ["build.plan-unavailable"]
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize(
    "encode",
    [
        # What Windows PowerShell 5.1 actually writes for `tan build --plan
        # --format json > plan.json`: UTF-16 with a BOM, whose leading 0xFF is
        # not valid UTF-8.
        lambda doc: json.dumps(doc).encode("utf-16"),
        # A lone invalid byte anywhere in the file -- a truncated download, a
        # file mangled by a transfer.
        lambda doc: b"\xff" + json.dumps(doc).encode("utf-8"),
    ],
    ids=["utf-16-bom", "invalid-byte"],
)
def test_an_undecodable_plan_file_is_a_bad_input_not_a_tan_bug(project, encode):
    # `UnicodeDecodeError` is a ValueError, NOT an OSError, so the original
    # `except OSError` around `read_text` missed it and the catch-all reported
    # a bad INPUT as `build.internal-failure` at exit 5 -- tan blaming itself
    # for a file it was handed.
    #
    # Not an exotic path: `--plan-from` is the replay half of the
    # capture-then-replay loop whose capture half is the redirect above. Rust
    # answers `build.plan-unavailable` at exit 1.
    plan = project / "plan.json"
    plan.write_bytes(encode(two_slice_plan(ALL_ARTEFACTS)))
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert [i["code"] for i in env["issues"]] == ["build.plan-unavailable"]
    assert "Traceback" not in proc.stderr


def _mutate(doc, path, value):
    """Set a dotted `path` inside a plan doc, so a case can name the field it
    breaks instead of rebuilding the whole document."""
    node = doc
    *parents, leaf = [int(k) if k.isdigit() else k for k in path.split(".")]
    for key in parents:
        node = node[key]
    node[leaf] = value
    return doc


@pytest.mark.parametrize(
    "field,value",
    [
        # `enumerate(raw["slices"])` on a scalar -> TypeError -> exit 5.
        ("slices", 5),
        ("slices", {"m33": {}}),
        # A slice that is not an object: `"coreId" not in 5` -> TypeError.
        ("slices.0", 5),
        # The forward-compatibility trap: `PolicyAction(raw)` -> ValueError.
        # The day the SDK adds a fourth action, EVERY build on that SDK
        # reported a tan bug instead of a coded refusal.
        ("executionPolicy.missingTool", "retry"),
        ("executionPolicy", "skip-everything"),
        # `warnings` is copied verbatim to `data.warnings`: a consumer doing
        # `data.warnings ?? []` then `.map()` breaks on a string.
        ("warnings", "oops"),
        # Scalars Rust types as String. `boardYaml` as an int reaches
        # `str.replace` in the token pass; an unhashable `backend` reaches
        # `in KNOWN_BACKENDS`. Both were AttributeError/TypeError at exit 5.
        ("boardYaml", 7),
        ("sdkCommit", 12345),
        ("slices.0.backend", ["zephyr"]),
        ("slices.0.coreId", None),
    ],
    ids=lambda v: str(v)[:24],
)
def test_a_structurally_broken_plan_is_refused_not_reported_as_a_tan_bug(project, field, value):
    # Every one of these used to land in the exit-5 `build.internal-failure`
    # catch-all -- tan blaming itself for a plan it was handed. Rust's serde
    # refuses each as `build.plan-invalid` at exit 1, and so must this: the
    # guard belongs at the shared parse site, not in each caller.
    plan = write_plan(project, _mutate(two_slice_plan(ALL_ARTEFACTS), field, value))
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert [i["code"] for i in env["issues"]] == ["build.plan-invalid"], env["issues"]
    assert "Traceback" not in proc.stderr


def test_an_unparseable_plan_exits_1_not_2(project):
    # DO NOT "fix" this to 2. A plan that will not parse reads like a
    # validation failure, but the shipped binary reports RuntimeFailure --
    # `crates/tan-cli/src/commands/build/plan_modes.rs:140-146` and
    # `native.rs:110-116` -- and the exit ladder is frozen for this port.
    # The consumer makes the difference load-bearing rather than cosmetic:
    # `alp-sdk-vscode/src/alpCli/service.ts:253-259` renders exit 2 as
    # `severity: "warning"` and exit 1 as `"error"`, so 2 would show a plan
    # that cannot be built as a yellow banner instead of a failure.
    plan = project / "plan.json"
    plan.write_text("{not json at all", encoding="utf-8", newline="")
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert env["exitCode"] == 1
    assert [i["code"] for i in env["issues"]] == ["build.plan-invalid"]
    assert "Traceback" not in proc.stderr


def test_an_unknown_plan_path_mode_exits_1_from_the_substitution_pass_too(project):
    # The SAME `build.plan-invalid` code, raised by a different module
    # (`token_substitution.py`, not `build_plan.py`). One code must not mean
    # two different exit codes depending on which module raised it.
    #
    # `--materialise`, because the substitution pass only runs for a mode that
    # puts something on disk or in an argv -- the oracle shows a `planPathMode:
    # legacy` plan at exit 0 under a bare `--plan-from` (measured) and only
    # refuses it once asked to materialise it.
    doc = two_slice_plan(ALL_ARTEFACTS)
    doc["planPathMode"] = "legacy"
    plan = write_plan(project, doc)
    proc = run_tan(
        "build", "--plan-from", str(plan), "--materialise", "--format", "json", cwd=project
    )

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert [i["code"] for i in env["issues"]] == ["build.plan-invalid"]


def test_an_unsupported_schema_version_is_refused_not_hand_ported(project):
    doc = two_slice_plan(ALL_ARTEFACTS)
    doc["schemaVersion"] = 2
    plan = write_plan(project, doc)
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

    env = envelope_of(proc)
    # Exit 1 for the same reason as the case above: a refused schemaVersion
    # takes Rust's `build.plan-invalid` ladder position, not `validate`'s.
    assert proc.returncode == 1, env
    assert [i["code"] for i in env["issues"]] == ["build.plan-unsupported-schema"]


def test_a_tokened_plan_with_no_sdk_refuses_and_writes_nothing(project):
    doc = two_slice_plan(ALL_ARTEFACTS)
    doc["planPathMode"] = "tokened"
    doc["sharedArtefacts"] = [
        {"path": "build/generated/alp/system_ipc.h", "contents": "SDK=${SDK_ROOT}\n"}
    ]
    plan = write_plan(project, doc)
    proc = run_tan(
        "build",
        "--plan-from",
        str(plan),
        "--materialise",
        "--board-yaml",
        "board.yaml",
        "--format",
        "json",
        cwd=project,
    )

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert [i["code"] for i in env["issues"]] == ["build.sdk-root-unresolved"]
    # Substituting an unresolved ${SDK_ROOT} with "" would sail past the
    # leftover-token guard; nothing may reach disk instead.
    assert not (project / "build" / "generated" / "alp" / "system_ipc.h").exists()


def test_the_default_invocation_substitutes_an_absolute_project_root(project, tmp_path):
    """`cd <project> && tan build` -- the documented happy path, with no
    `--board-yaml` and no `--build-root`.

    ${PROJECT_ROOT} must come out ABSOLUTE. It is substituted here but CONSUMED
    somewhere else entirely: a slice runs its command in `<root>/build/<slice>`,
    and Zephyr resolves `-DEXTRA_CONF_FILE` against the APPLICATION source dir,
    which for a stock-shim slice is inside the SDK checkout. While these
    defaults were kept in their as-passed form, ${PROJECT_ROOT} resolved to the
    literal `"."` and a real `tan build` in exactly this shape produced
    `<sdk>/firmware/alp-stock-shim/./build/<slice>/alp.conf: File not found`
    and `ERROR: . doesn't contain a CMakeLists.txt` -- every zephyr slice dead,
    on the one invocation a customer actually types.
    """
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (project / "board.yaml").write_text("som:\n  sku: E1M-TEST\n", encoding="utf-8")

    doc = two_slice_plan(())
    doc["planPathMode"] = "tokened"
    doc["sharedArtefacts"] = []
    # Only the `command: null` slice, so this case spawns nothing at all; its
    # config artefact is the readout of what ${PROJECT_ROOT} became.
    doc["slices"] = [doc["slices"][1]]
    doc["slices"][0]["configArtefacts"] = [
        {"path": "build/zzz_nocmd-zephyr/alp.conf", "contents": "ROOT=${PROJECT_ROOT}\n"}
    ]
    plan = write_plan(project, doc)

    proc = run_tan(
        "build", "--plan-from", str(plan), "--materialise", "--sdk-root", str(sdk),
        "--format", "json", cwd=project,
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    written = (project / "build" / "zzz_nocmd-zephyr" / "alp.conf").read_text(encoding="utf-8")
    root = written.split("=", 1)[1].strip()
    assert Path(root).is_absolute(), f"${{PROJECT_ROOT}} must be absolute, got {root!r}"
    # Both sides of the divergence guard resolve together, so they stay
    # lexically equal -- resolving only one is how that guard starts firing on
    # a project that is perfectly fine.
    assert env["project"]["root"] == root


def test_relative_board_yaml_anchors_on_project_not_the_real_cwd(project):
    """`--project app --board-yaml board.yaml` must resolve `board.yaml`
    relative to `<cwd>/app` (the `--project`-joined workspace root), NEVER the
    real cwd -- matching the Rust oracle's `resolve_board_yaml_path`
    (`crates/tan-core/src/project.rs:198-208`, which joins a relative
    configured path onto `workspace_root`).

    A board.yaml sits in BOTH the real cwd and `app/` here, so a re-anchor
    onto the real cwd (the pre-fix bug: `_abs_posix` is purely lexical
    `os.path.abspath`, which anchors on `os.getcwd()`) would silently resolve
    to the WRONG one without any error -- planning and building the wrong
    project. Verified against the oracle in exactly this shape: `tan --project
    app build --board-yaml board.yaml --format json` reports `project.root` /
    `project.boardYaml` under `<cwd>/app`, not `<cwd>`.
    """
    app = project / "app"
    app.mkdir()
    (app / "board.yaml").write_text("som:\n  sku: E1M-TEST-APP\n", encoding="utf-8")
    # The decoy: a DIFFERENT board.yaml in the real cwd, so a re-anchor onto it
    # would silently succeed instead of loudly failing.
    (project / "board.yaml").write_text("som:\n  sku: E1M-TEST-CWD-DECOY\n", encoding="utf-8")

    proc = run_tan(
        "--project", "app", "build", "--board-yaml", "board.yaml", "--format", "json",
        cwd=project,
    )
    env = envelope_of(proc)
    # No SDK is configured, so this refuses at the planning step -- but
    # `project` is resolved and reported BEFORE that refusal (build_cmd.build),
    # which is exactly the field this anchor bug corrupted.
    assert env["issues"][0]["code"] == "build.plan-unavailable", env
    # `os.path.abspath`, not `.resolve()`: matches `_abs_posix`, which is
    # deliberately lexical (see its docstring) so a symlinked tmp dir cannot
    # make this assertion diverge from what the command itself computes.
    expected_root = os.path.abspath(str(app)).replace("\\", "/")
    assert env["project"]["root"] == expected_root, env["project"]
    assert env["project"]["boardYaml"] == f"{expected_root}/board.yaml", env["project"]


def test_an_unwritable_build_root_is_a_write_failure(project):
    doc = two_slice_plan(ALL_ARTEFACTS)
    # A shared artefact whose parent directory is an existing FILE: mkdir of
    # the parent raises OSError on every platform.
    (project / "blocker").write_text("", encoding="utf-8", newline="")
    doc["sharedArtefacts"] = [{"path": "blocker/system_ipc.h", "contents": "x\n"}]
    plan = write_plan(project, doc)
    proc = run_tan(
        "build", "--plan-from", str(plan), "--materialise", "--format", "json", cwd=project
    )

    env = envelope_of(proc)
    assert proc.returncode == 3, env
    assert [i["code"] for i in env["issues"]] == ["build.materialise-failed"]
    assert "Traceback" not in proc.stderr


def test_an_artefact_escaping_the_build_root_is_refused(project):
    doc = two_slice_plan(ALL_ARTEFACTS)
    doc["sharedArtefacts"] = [{"path": "../escaped.h", "contents": "x\n"}]
    plan = write_plan(project, doc)
    proc = run_tan(
        "build", "--plan-from", str(plan), "--materialise", "--format", "json", cwd=project
    )

    env = envelope_of(proc)
    assert proc.returncode == 3, env
    assert [i["code"] for i in env["issues"]] == ["build.materialise-failed"]
    assert not (project.parent / "escaped.h").exists()


def test_an_unknown_backend_is_named_and_fails_by_default(project):
    doc = two_slice_plan(ALL_ARTEFACTS)
    doc["slices"][0]["backend"] = "native"
    plan = write_plan(project, doc)
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json", cwd=project
    )

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    backend_issue = next(i for i in env["issues"] if i["code"] == "build.unknown-backend")
    assert backend_issue["severity"] == "error"
    assert "native" in backend_issue["message"]
    # Never dropped -- still reported, as failed.
    assert env["data"]["slices"][0]["status"] == "failed"


def test_no_plan_and_no_sdk_is_a_coded_envelope_not_a_traceback(project):
    # The planner path with nothing to plan from. The message must name the
    # way out, and the failure must still be an envelope.
    proc = run_tan("build", "--format", "json", cwd=project)
    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert [i["code"] for i in env["issues"]] == ["build.plan-unavailable"]
    assert "--sdk-root" in env["issues"][0]["message"]
    assert "Traceback" not in proc.stderr


def test_build_resolves_the_sdk_tan_init_pinned_with_no_sdk_root_flag_and_no_env_var(
    project, monkeypatch
):
    # The worst reported CX defect: `tan init` writes `<project>/.alp/sdk-path`
    # naming the SDK it just pinned, but `tan build` used to jump straight
    # from `--sdk-root` to the positional (sibling/child/ancestor) walk and
    # never read that pointer at all -- so a customer running `tan init` then
    # `tan build` in the SAME directory, with no `--sdk-root` and even with
    # `ALP_SDK_ROOT` exported, got "no alp-sdk checkout found" with the
    # checkout sitting right there in `.alp/sdk-path`.
    monkeypatch.delenv("ALP_SDK_ROOT", raising=False)

    # An SDK checkout that is deliberately NOT a sibling/child/ancestor of
    # `project` -- the positional walk must be unable to find it any other
    # way, so a passing assertion proves the `.alp/sdk-path` pin was read.
    sdk = project.parent / "elsewhere-sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8", newline="")

    alp_dir = project / ".alp"
    alp_dir.mkdir()
    (alp_dir / "sdk-path").write_text(
        json.dumps({"sdkPath": str(sdk)}), encoding="utf-8", newline=""
    )

    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan(
        "build",
        "--plan-from",
        str(plan),
        "--execute",
        "--board-yaml",
        "board.yaml",
        "--format",
        "json",
        cwd=project,
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    assert env["sdk"]["root"].replace("\\", "/") == str(sdk).replace("\\", "/")
    assert env["sdk"]["sourceTier"] == "projectPin"


def test_alp_sdk_root_env_is_not_a_discovery_tier(project, monkeypatch):
    # `ALP_SDK_ROOT` is deliberately NOT read back for discovery: the oracle's
    # `SdkSourceTier` (`crates/tan-core/src/sdk.rs`) is a closed five-value
    # enum with no slot for it, and `util.rs::resolve_sdk_root` only ever
    # WRITES that variable into a build slice's env. An earlier version of
    # this port's ladder invented a sixth `envAlpSdkRoot` tier to read it
    # back -- reverted, because it changed `sdk.sourceTier`'s wire values to
    # something no consumer (the vscode extension, `tan sdk current --json`)
    # knows. The `.alp/sdk-path` project pin (see the test above) is the
    # tier that makes `tan init && tan build` compose; this proves an
    # exported `ALP_SDK_ROOT` alone -- no pin, no positional match -- still
    # resolves nothing.
    sdk = project.parent / "env-only-sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8", newline="")
    monkeypatch.setenv("ALP_SDK_ROOT", str(sdk))

    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan(
        "build",
        "--plan-from",
        str(plan),
        "--execute",
        "--board-yaml",
        "board.yaml",
        "--format",
        "json",
        cwd=project,
    )

    env = envelope_of(proc)
    # The build itself still succeeds -- `--plan-from` needs no SDK -- but
    # `sdk` is OMITTED (never a phantom `envAlpSdkRoot` value), and the
    # post-build system-manifest emit, which DOES need a resolved root,
    # reports exactly the "nothing resolved" reason.
    assert proc.returncode == 0, env
    assert "sdk" not in env
    assert "no alp-sdk checkout resolved" in proc.stderr


# --- an unresolved ${TOOLCHAIN_ROOT} is never dispatched --------------------


@pytest.mark.parametrize(
    "policy,expected_exit,expected_status,expected_severity",
    # tan-cli#283: BOTH slices of this fixture end up not-succeeded here --
    # slice 0 is held/skipped by the demotion this test exists to check,
    # slice 1 (`zzz_nocmd`) is the plan's own null-command skip -- so under
    # missingTool=skip too this is a wholly-skipped build (nothing built) and
    # refuses, exit 1, same as the missingTool=fail row already did.
    [("skip", 1, "skipped", "warning"), ("fail", 1, "failed", "error")],
    ids=["missingTool=skip", "missingTool=fail"],
)
def test_a_slice_still_naming_toolchain_root_is_routed_not_spawned(
    project, policy, expected_exit, expected_status, expected_severity
):
    # This port resolves no toolchain root, so a tokened plan naming
    # ${TOOLCHAIN_ROOT} in a slice's own fields leaves the literal token
    # behind. Dispatching that slice would run a command with an
    # unsubstituted token in its argv -- a silent wrong-path build. It must
    # instead take `executionPolicy.missingTool`, whose default is skip: a
    # host-provisioning fact, not a plan bug (and so NOT plan-fatal, which
    # would take every OTHER slice down with it).
    sdk = project / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8", newline="")

    doc = two_slice_plan(ALL_ARTEFACTS)
    doc["planPathMode"] = "tokened"
    doc["executionPolicy"]["missingTool"] = policy
    doc["slices"][0]["command"]["args"] = [
        "-c",
        "print('THIS SLICE MUST NOT RUN')",
        "${TOOLCHAIN_ROOT}/bin/cmake",
    ]
    plan = write_plan(project, doc)
    proc = run_tan(
        "build",
        "--plan-from",
        str(plan),
        "--execute",
        "--board-yaml",
        "board.yaml",
        "--sdk-root",
        str(sdk),
        "--format",
        "json",
        cwd=project,
    )

    env = envelope_of(proc)
    assert proc.returncode == expected_exit, env
    assert env["data"]["slices"][0]["status"] == expected_status
    assert "THIS SLICE MUST NOT RUN" not in proc.stderr

    issue = next(i for i in env["issues"] if i["code"] == "build.toolchain-root-unresolved")
    assert issue["severity"] == expected_severity
    assert "TOOLCHAIN_ROOT" in issue["message"]

    # The OTHER slice is untouched by one slice's provisioning gap.
    assert env["data"]["slices"][1]["status"] == "skipped"


# --- the OS is derived, never selectable (I-01/I-02) ------------------------


@pytest.mark.parametrize("flag", ["--os", "--backend"])
def test_the_os_is_never_a_flag(project, flag):
    help_text = run_tan("build", "--help", cwd=project).stdout
    assert flag not in help_text, (
        f"`tan build {flag}` must never exist: the OS is derived from the core's "
        "Cortex class by the planner and is not selectable (I-01/I-02)"
    )
    rejected = run_tan("build", flag, "zephyr", "--plan-from", "x", cwd=project)
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr


# --- text mode keeps stdout clean too ---------------------------------------


@pytest.mark.parametrize(
    "mode",
    [(), ("--native",), ("--materialise",), ("--execute",)],
    ids=lambda m: "".join(m) or "plan",
)
def test_text_mode_puts_nothing_on_stdout(project, mode):
    # Every mode: stdout is the envelope channel and text mode has no
    # envelope, so it must stay empty whatever the run did. `aaa_probe` proves
    # each recap actually said something -- the plan listing (twice: `--native`
    # does not override the implied `--plan`), the written artefact paths, and
    # the slice results.
    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan("build", "--plan-from", str(plan), *mode, cwd=project)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"text mode leaked to stdout:\n{proc.stdout}"
    assert "aaa_probe" in proc.stderr


# --- an unported oracle flag reads as deferred, never as a typo -------------


#: Every flag `tan build` declares in the v0.4.1 oracle that this port does not
#: implement -- read off `tan.exe build --help`, minus the six the port already
#: has (`--plan-from --project --board-yaml --sdk-root --format` + the port-only
#: `--build-root` and `--execute`) and minus the two it now implements
#: (`--materialise`, `--native`).
#: `--verbose`/`--quiet`/`--no-color`/`--non-interactive`/`--ci`
#: are clap GLOBALS in the oracle, declared on its root and propagated; they are
#: listed here only because this port accepts none of them at its own root
#: today. If `cli.py` ever grows real root-level handling for them, they must
#: leave `build_cmd.py` (and this list) or the build-local declaration shadows
#: it.
DEFERRED_BUILD_FLAGS = [
    "--plan",
    ("--target", "zephyr-conf"),
    "--all",
    "--manifest",
    ("--manifest-from", "manifest.yaml"),
    "--no-auto-bootstrap",
    "--pristine",
    "--verbose",
    "--quiet",
    "--no-color",
    "--non-interactive",
    "--ci",
]


@pytest.mark.parametrize(
    "flag", DEFERRED_BUILD_FLAGS, ids=lambda f: (f[0] if isinstance(f, tuple) else f)
)
def test_an_unported_oracle_flag_is_refused_as_deferred_not_as_a_typo(project, flag):
    """`tan build --pristine` used to exit 2 with Click's "No such option",
    which is indistinguishable from `tan build --pristien`. Every flag the
    v0.4.1 oracle has and this port does not is DECLARED, so the answer is the
    same coded refusal `deferred_cmd` gives the seven deferred verbs: exit 1,
    `cli.command-deferred`, and a message naming the flag, v0.6.0 and the
    tan-cli#260 URL a caller can grep for.

    Exit 1 is the load-bearing half. Reusing 2 would put "known but deferred"
    back at the exact exit code the typo case already occupies, which is the
    distinction this whole treatment exists to make.
    """
    argv = flag if isinstance(flag, tuple) else (flag,)
    proc = run_tan("build", *argv, "--format", "json", cwd=project)

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert env["command"] == "build"
    assert [i["code"] for i in env["issues"]] == ["cli.command-deferred"], env["issues"]
    message = env["issues"][0]["message"]
    assert argv[0] in message
    assert "v0.6.0" in message
    assert "tan-cli/issues/260" in message
    assert "Traceback" not in proc.stderr


def test_a_deferred_flag_does_no_work_before_refusing(project):
    # The refusal happens before the plan is read, so a run naming one cannot
    # half-build: nothing on disk, and nothing on stdout in text mode either.
    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan("build", "--plan-from", str(plan), "--pristine", cwd=project)
    assert proc.returncode == 1, proc.stderr
    assert proc.stdout == "", proc.stdout
    assert not (project / "build").exists()


def test_a_real_typo_still_exits_2(project):
    # The other half of the distinction: a flag that is NOT a deferred oracle
    # flag must keep Click's parse error, or "deferred" would just be the new
    # name for every mistyped option.
    proc = run_tan("build", "--pristien", "--format", "json", cwd=project)
    assert proc.returncode == 2, proc.stdout + proc.stderr
