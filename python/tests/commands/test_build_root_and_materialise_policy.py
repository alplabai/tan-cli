# SPDX-License-Identifier: Apache-2.0
"""Two safety gaps in ``tan build``'s pre-dispatch guards, driven as real
subprocesses (the framing -- one envelope on stdout, the exit code, and what
is on disk afterwards -- is the load-bearing part, which an in-process call
exercises none of).

**tan-cli#566** -- a plan whose ``buildRoot`` is not ``build`` was accepted,
materialised and dispatched, and its ``system-manifest.yaml`` written under
that other root, while ``tan flash`` / ``size`` / ``image`` all
anchor on ``<project>/build/system-manifest.yaml``. Measured on ``dev``
before the fix, twice against a real alp-sdk checkout: run 1
(``buildRoot: build``) wrote ``build/system-manifest.yaml``; run 2
(``buildRoot: out``) was accepted at the same exit code, wrote
``out/system-manifest.yaml``, and left ``build/system-manifest.yaml``
byte-identical -- so the next ``tan flash`` programs the PREVIOUS run's
artefact onto silicon.

**tan-cli#565** -- ``--materialise`` bound ``demotions`` and never read it, so
a ``${TOOLCHAIN_ROOT}``-demoted slice had its ``configArtefacts`` silently
dropped from both ``written`` and the disk at exit 0 with ``issues: []``, and
``executionPolicy.missingTool`` was not consulted at all: ``skip`` and
``fail`` produced byte-identical envelopes.

Both accept/refuse contracts below were established by RUNNING
``target/debug/tan`` (``tan 0.4.1``), never by reading ``crates/``. #566's
native path is unreachable in the oracle through ``--plan-from`` (there
``--plan-from`` implies ``--plan``, which outranks ``--native``), so it was
measured by handing the oracle a stub ``alp_orchestrate`` on its LIVE-emit
path that printed an arbitrary plan.
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


def run_tan(*argv, cwd, env_overrides=None):
    """Spawn the port. ``env_overrides`` sets -- or, on a ``None`` value,
    DELETES -- variables in the child, which the ``${TOOLCHAIN_ROOT}`` cases
    below need: the resolver reads ``ZEPHYR_SDK_INSTALL_DIR`` and scans
    ``$HOME``/``%USERPROFILE%``/``Path.home()``, so a test that wants a
    specific host shape has to build it in the CHILD.

    ``PATH`` is emptied for every case here: no slice may launch a real
    multi-minute Zephyr/Yocto build on whichever machine runs this suite.
    """
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
        "PATH": "",
    }
    for key, value in (env_overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
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
    assert proc.stdout.strip(), f"no envelope on stdout; stderr was:\n{proc.stderr}"
    return json.loads(proc.stdout)


def codes(env):
    return [i["code"] for i in env["issues"]]


@pytest.fixture
def project(tmp_path):
    """A scratch project + a minimal SDK root beside it. Both are needed
    before token substitution will resolve ``${SDK_ROOT}`` at all."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "board.yaml").write_text("som:\n  sku: E1M-TEST\n", encoding="utf-8")
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    return root, sdk


def _slice(core_id, *, backend="baremetal", command="ok", contents="CONFIG_A=y\n"):
    """One plan slice. ``command="ok"`` spawns nothing that matters -- every
    case here runs with an EMPTY ``PATH``, so a dispatching run takes the
    ``missingTool`` branch instead of launching a tool."""
    return {
        "coreId": core_id,
        "backend": backend,
        "buildDir": f"build/{core_id}",
        "appDir": None,
        "configArtefacts": [
            {"path": f"build/{core_id}-zephyr/alp.conf", "contents": contents}
        ],
        "toolchain": {"targetTriple": None, "compiler": None, "sysroot": None, "id": "x"},
        "artifacts": {
            "elf": None, "map": None, "bin": None,
            "sizeReport": None, "symbols": None, "compileCommands": None,
        },
        "debug": {"console": "rtt", "probe": None},
        "command": (
            None if command is None
            else {"tool": "west", "args": ["build"], "cwd": f"build/{core_id}"}
        ),
        "env": {},
        "envAppendPath": {},
    }


def plan_doc(*, build_root="build", missing_tool="skip", tokened=False, slices=None):
    doc = {
        "schemaVersion": 1,
        "generatedBy": "tests/commands/test_build_root_and_materialise_policy.py",
        "boardYaml": "board.yaml",
        "sku": "E1M-TEST",
        "buildRoot": build_root,
        "executionPolicy": {
            "unknownBackend": "fail",
            "missingTool": missing_tool,
            "nullCommand": "skip",
        },
        "sharedArtefacts": [
            {"path": "build/generated/alp/system_ipc.h", "contents": "/* shared */\n"}
        ],
        "slices": slices if slices is not None else [_slice("aaa_probe")],
        "warnings": [],
    }
    if tokened:
        doc["planPathMode"] = "tokened"
    return doc


def write_plan(directory: Path, doc: dict) -> Path:
    path = directory / "plan.json"
    path.write_text(json.dumps(doc), encoding="utf-8", newline="")
    return path


def written_tree(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "plan.json" and p.name != "board.yaml"
    )


# --- tan-cli#566: the plan's own build root ---------------------------------
#
# `--execute` is how the PORT reaches `_MODE_NATIVE` with a file-supplied plan
# (the oracle cannot: there `--plan-from` implies `--plan`). It selects the
# same mode a plain `tan build` does, which is the mode the guard is scoped to.


def test_a_non_build_build_root_is_refused_before_anything_is_written(project):
    """The headline. Oracle, native path, `buildRoot: "out"`:

        rc 1, data null, issues[0].code == "build.unsupported-build-root",
        and nothing at all under `out/`.
    """
    root, sdk = project
    plan = write_plan(root, plan_doc(build_root="out"))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute",
        "--sdk-root", str(sdk), "--format", "json", cwd=root,
    )

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert env["exitCode"] == 1
    assert env["data"] is None, env["data"]
    assert codes(env) == ["build.unsupported-build-root"], env["issues"]
    # Verbatim the oracle's own sentence -- it names the offending value AND
    # the file the hazard is about, which is what makes it actionable.
    assert env["issues"][0]["message"] == (
        "plan buildRoot `out` is not `build`; tan's flash/size/image "
        "read `<project>/build/system-manifest.yaml`, so building elsewhere "
        "would leave them reading a stale or missing manifest"
    )
    assert env["issues"][0]["severity"] == "error"
    # The whole point of refusing ABOVE `materialise_plan` (which runs before
    # the mode check): the refused run leaves NO half-written tree behind.
    assert written_tree(root) == [], written_tree(root)
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize("build_root", ["out", "build/sub", "BUILD", "", "..", "../build"])
def test_every_measured_refused_spelling_is_refused(project, build_root):
    """The oracle's refuse set, measured spelling by spelling. `BUILD` is in
    it because the comparison is case-SENSITIVE, and `""` because an empty
    `buildRoot` has no components at all -- neither would be caught by an
    "is it under the project" check."""
    root, sdk = project
    plan = write_plan(root, plan_doc(build_root=build_root))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute",
        "--sdk-root", str(sdk), "--format", "json", cwd=root,
    )
    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert codes(env) == ["build.unsupported-build-root"], env["issues"]


@pytest.mark.parametrize("build_root", ["build", "build/", "./build"])
def test_every_measured_accepted_spelling_still_builds(project, build_root):
    """The other half, and the reason this is a path-component comparison
    rather than `!= "build"`: the oracle ACCEPTS `build/` and `./build`, and a
    string compare would refuse a plan it has always built."""
    root, sdk = project
    plan = write_plan(root, plan_doc(build_root=build_root))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute",
        "--sdk-root", str(sdk), "--format", "json", cwd=root,
    )
    env = envelope_of(proc)
    assert "build.unsupported-build-root" not in codes(env), env["issues"]
    # It really did reach dispatch: `west` cannot resolve on an empty PATH, so
    # the slice is skipped by `executionPolicy.missingTool` -- which is a
    # verdict only a run that got past this guard can produce.
    assert "build.missing-tool" in codes(env), env["issues"]


@pytest.mark.parametrize("mode_flag", ["--materialise", None])
def test_the_refusal_is_scoped_to_the_dispatching_mode(project, mode_flag):
    """Measured: the oracle accepts `buildRoot: out` under `--materialise`
    AND under a bare `--plan-from` (its implied `--plan`), and refuses only on
    the path that goes on to dispatch. That scoping is deliberate -- neither
    of those modes writes `system-manifest.yaml`, so neither carries the
    stale-manifest hazard the refusal exists for. Widening it would refuse
    plan inspection, which the oracle allows."""
    root, sdk = project
    plan = write_plan(root, plan_doc(build_root="out"))
    argv = ["build", "--plan-from", str(plan)]
    if mode_flag:
        argv.append(mode_flag)
    proc = run_tan(*argv, "--sdk-root", str(sdk), "--format", "json", cwd=root)

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    assert "build.unsupported-build-root" not in codes(env), env["issues"]


def test_the_refused_code_is_registered():
    """tan-cli#224's gate would catch an unregistered code, but only as
    "some code is missing" across the whole tree. This says which one, at the
    site that introduced it."""
    registry = json.loads(
        (PACKAGE_ROOT.parent / "contract" / "issue-codes.json").read_text(encoding="utf-8")
    )
    entry = next(
        (e for e in registry["issueCodes"] if e["code"] == "build.unsupported-build-root"),
        None,
    )
    assert entry is not None, "build.unsupported-build-root is not in contract/issue-codes.json"
    assert entry["severity"] == "error"
    assert entry["emittedBy"] == "python/tan/commands/build_cmd.py"


# --- tan-cli#565: --materialise vs its own demotions ------------------------


@pytest.fixture
def toolchainless_host(tmp_path):
    """Environment overrides that make `${TOOLCHAIN_ROOT}` resolution fail
    DETERMINISTICALLY, on any host, including one with a real Zephyr SDK
    installed (this repo's own bench box has one under `$HOME`).

    Two `zephyr-sdk*` directories in the scratch home, not zero: "no
    candidates" is only unresolvable while nothing else is found, and `/opt`
    is a scan root nothing here controls. Two candidates make the host
    AMBIGUOUS, and an ambiguous host stays unresolved however many more
    `/opt` contributes -- so the demotion this file is about happens for a
    reason the test itself created, never by accident of the machine.
    """
    home = tmp_path / "home"
    (home / "zephyr-sdk-1.0.1").mkdir(parents=True)
    (home / "zephyr-sdk-0.16.8").mkdir(parents=True)
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "ZEPHYR_SDK_INSTALL_DIR": None,
    }


def _tokened_plan(missing_tool):
    """Three slices: two demoted (each names `${TOOLCHAIN_ROOT}` in its own
    `configArtefacts[].contents`) and one clean, plus a shared artefact that
    names no token."""
    return plan_doc(
        missing_tool=missing_tool,
        tokened=True,
        slices=[
            _slice("m55_he", contents="TC=${TOOLCHAIN_ROOT}\n"),
            _slice("m55_hp", contents="TC=${TOOLCHAIN_ROOT}\n"),
            _slice("a32", contents="CLEAN=1\n"),
        ],
    )


def test_materialise_warns_once_per_demoted_slice_under_missing_tool_skip(
    project, toolchainless_host
):
    """Oracle, `--materialise`, `missingTool: skip`: exit 0, the demoted
    slices' artefacts absent from `written` AND from disk, and ONE `warning`
    per demoted slice naming what was not written. Before the fix the port
    produced the same `written` and the same disk with `issues: []`."""
    root, sdk = project
    plan = write_plan(root, _tokened_plan("skip"))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--materialise",
        "--sdk-root", str(sdk), "--format", "json",
        cwd=root, env_overrides=toolchainless_host,
    )

    env = envelope_of(proc)
    assert proc.returncode == 0, env
    # The half-materialised tree is the OUTCOME the oracle also produces --
    # what was missing is the signal, not the behaviour.
    assert env["data"]["written"] == [
        "build/generated/alp/system_ipc.h",
        "build/a32-zephyr/alp.conf",
    ], env["data"]
    assert codes(env) == [
        "build.toolchain-root-unresolved",
        "build.toolchain-root-unresolved",
    ], env["issues"]
    assert [i["severity"] for i in env["issues"]] == ["warning", "warning"]
    assert env["issues"][0]["message"].startswith(
        "slice `m55_he`: configArtefacts not materialised — "
    ), env["issues"][0]
    assert env["issues"][1]["message"].startswith(
        "slice `m55_hp`: configArtefacts not materialised — "
    ), env["issues"][1]
    assert written_tree(root) == [
        "build/a32-zephyr/alp.conf",
        "build/generated/alp/system_ipc.h",
    ], written_tree(root)


def test_materialise_refuses_up_front_under_missing_tool_fail(project, toolchainless_host):
    """The half `executionPolicy` was not consulted for at all. Oracle,
    `--materialise`, `missingTool: fail`: exit 1, `data: null`, ONE `error`
    whose message carries every demoted slice's sentence newline-joined --
    and NOTHING on disk, not even the clean slice's artefact or the shared
    one. That last part is why the guard sits above `materialise_plan`
    (which runs before the mode check) rather than inside the materialise
    branch."""
    root, sdk = project
    plan = write_plan(root, _tokened_plan("fail"))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--materialise",
        "--sdk-root", str(sdk), "--format", "json",
        cwd=root, env_overrides=toolchainless_host,
    )

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    assert env["exitCode"] == 1
    assert env["data"] is None, env["data"]
    assert codes(env) == ["build.toolchain-root-unresolved"], env["issues"]
    assert env["issues"][0]["severity"] == "error"
    lines = env["issues"][0]["message"].split("\n")
    assert len(lines) == 2, env["issues"][0]["message"]
    assert lines[0].startswith("slice `m55_he`: "), lines[0]
    assert lines[1].startswith("slice `m55_hp`: "), lines[1]
    assert written_tree(root) == [], written_tree(root)


def test_the_two_missing_tool_actions_no_longer_produce_the_same_answer(
    project, tmp_path, toolchainless_host
):
    """The issue's sharpest symptom stated directly: before the fix `skip`
    and `fail` produced BYTE-IDENTICAL envelopes and identical files, so the
    policy was provably ignored rather than merely under-reported.

    Compared on the run's own OUTCOME -- exit code, `written`, `issues`, and
    what landed on disk -- deliberately not on the whole `data` object: that
    carries an absolute `baseDir`, which differs between these two scratch
    projects for a reason that has nothing to do with the policy and would
    make this case pass against the unfixed tree it exists to fail against.
    """
    _root, sdk = project
    outcomes = {}
    for action in ("skip", "fail"):
        root = tmp_path / f"proj-{action}"
        root.mkdir()
        (root / "board.yaml").write_text("som:\n  sku: E1M-TEST\n", encoding="utf-8")
        plan = write_plan(root, _tokened_plan(action))
        proc = run_tan(
            "build", "--plan-from", str(plan), "--materialise",
            "--sdk-root", str(sdk), "--format", "json",
            cwd=root, env_overrides=toolchainless_host,
        )
        env = envelope_of(proc)
        outcomes[action] = (
            proc.returncode,
            (env["data"] or {}).get("written"),
            env["issues"],
            written_tree(root),
        )

    assert outcomes["skip"] != outcomes["fail"], (
        "`missingTool: skip` and `missingTool: fail` produced the same exit "
        "code, the same `data` and the same files -- the policy is not being "
        "consulted (tan-cli#565)"
    )


def test_a_demoted_slice_still_dispatches_through_the_execute_path(project, toolchainless_host):
    """The guard must not leak into `_MODE_NATIVE`, which already routes
    demotions per slice through `_dispatch`'s own `missingTool` seam. A run
    that materialises AND dispatches keeps reporting one issue per demoted
    slice from there -- not the newline-joined materialise shape."""
    root, sdk = project
    plan = write_plan(root, _tokened_plan("fail"))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute",
        "--sdk-root", str(sdk), "--format", "json",
        cwd=root, env_overrides=toolchainless_host,
    )

    env = envelope_of(proc)
    assert proc.returncode == 1, env
    demoted = [
        i for i in env["issues"] if i["code"] == "build.toolchain-root-unresolved"
    ]
    assert len(demoted) == 2, env["issues"]
    assert all("\n" not in i["message"] for i in demoted), demoted
    # All three `failed`: the two demoted ones because `missingTool: fail`
    # applies to a demotion, the clean one because the SAME action applies to
    # `west` being absent from this run's empty PATH.
    assert [s["status"] for s in env["data"]["slices"]] == ["failed", "failed", "failed"], (
        env["data"]["slices"]
    )
    # And -- the part this case exists for -- the tree WAS materialised: a
    # dispatching run does not take tan-cli#565's write-nothing refusal.
    assert "build/a32-zephyr/alp.conf" in written_tree(root), written_tree(root)
