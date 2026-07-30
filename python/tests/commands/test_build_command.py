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
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

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
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

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
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert env["data"]["slices"][0]["status"] == "failed"
    assert any(i["code"] == "build.slice-failed" for i in env["issues"]), env["issues"]


def test_slice_output_goes_to_stderr_and_stdout_stays_one_envelope(project):
    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

    # `json.loads` on the WHOLE of stdout: a second document, a log line or a
    # stray progress byte all fail here, which is exactly the break that
    # renders nothing in the extension.
    envelope_of(proc)
    assert "present " in proc.stderr, "slice output must be on stderr, not swallowed"


# --- the real captured plans ------------------------------------------------


def test_a_null_command_slice_survives_with_its_warning(project):
    # I-11, on the real plan that has this shape: `m33` carries `command:
    # null` plus a `board-tree-missing` warning naming why. Neither the slice
    # nor the warning may be dropped.
    plan_doc = real_plan("multicore_rpmsg-imx93")
    plan = write_plan(project, plan_doc)
    proc = run_tan(
        "build", "--plan-from", str(plan), "--format", "json", cwd=project, scrub_path=True
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env

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
    # board.yaml need never have mentioned. The CLI must report all three.
    plan = write_plan(project, real_plan("multicore_rpmsg-aen"))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--format", "json", cwd=project, scrub_path=True
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    slices = env["data"]["slices"]
    assert [s["coreId"] for s in slices] == ["a32_cluster", "m55_he", "m55_hp"]
    assert [s["backend"] for s in slices] == ["yocto", "zephyr", "zephyr"]
    assert all(s["status"] == "skipped" for s in slices), slices


def test_materialise_writes_every_artefact_of_a_real_plan(project):
    plan_doc = real_plan("multicore_rpmsg-aen")
    plan = write_plan(project, plan_doc)
    proc = run_tan(
        "build", "--plan-from", str(plan), "--format", "json", cwd=project, scrub_path=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    expected = [a["path"] for a in plan_doc["sharedArtefacts"]]
    for sl in plan_doc["slices"]:
        expected.extend(a["path"] for a in sl["configArtefacts"])
    assert expected, "fixture carries no artefacts; this case would pass vacuously"
    for rel in expected:
        assert (project / rel).is_file(), f"{rel} was not materialised"


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
    doc = two_slice_plan(ALL_ARTEFACTS)
    doc["planPathMode"] = "legacy"
    plan = write_plan(project, doc)
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

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
        "build", "--plan-from", str(plan), "--sdk-root", str(sdk), "--format", "json",
        cwd=project,
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
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

    env = envelope_of(proc)
    assert proc.returncode == 3, env
    assert [i["code"] for i in env["issues"]] == ["build.materialise-failed"]
    assert "Traceback" not in proc.stderr


def test_an_artefact_escaping_the_build_root_is_refused(project):
    doc = two_slice_plan(ALL_ARTEFACTS)
    doc["sharedArtefacts"] = [{"path": "../escaped.h", "contents": "x\n"}]
    plan = write_plan(project, doc)
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

    env = envelope_of(proc)
    assert proc.returncode == 3, env
    assert [i["code"] for i in env["issues"]] == ["build.materialise-failed"]
    assert not (project.parent / "escaped.h").exists()


def test_an_unknown_backend_is_named_and_fails_by_default(project):
    doc = two_slice_plan(ALL_ARTEFACTS)
    doc["slices"][0]["backend"] = "native"
    plan = write_plan(project, doc)
    proc = run_tan("build", "--plan-from", str(plan), "--format", "json", cwd=project)

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


# --- an unresolved ${TOOLCHAIN_ROOT} is never dispatched --------------------


@pytest.mark.parametrize(
    "policy,expected_exit,expected_status,expected_severity",
    [("skip", 0, "skipped", "warning"), ("fail", 1, "failed", "error")],
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


def test_text_mode_puts_nothing_on_stdout(project):
    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan("build", "--plan-from", str(plan), cwd=project)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"text mode leaked to stdout:\n{proc.stdout}"
    assert "aaa_probe" in proc.stderr
