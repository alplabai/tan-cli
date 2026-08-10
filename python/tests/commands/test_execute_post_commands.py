# SPDX-License-Identifier: Apache-2.0
"""tan-cli#550 at the `execute_slices` layer: a baremetal slice must actually
BUILD, and must not be reported built when it produced no artefact.

The defect, verbatim from the issue: `tan build` "prints `ok: m55_hp
[baremetal]` / `1 of 1 slice(s) built` and exits 0. The build tree contains
only `Makefile`, `CMakeCache.txt`, `cmake_install.cmake`, `CMakeFiles/` -- no
`.o`, no `.a`, no `.elf`."

alp-sdk #1344 (re-synced into `tan/planner/` by tan-cli#608) emits the missing
`cmake --build .` as a `postCommands` step and names the linked output's home
as `artifacts.outputDir`. Both were inert until this change: `tan.core.
build_plan` did not parse the key and `execute.py` did not run it. These tests
pin both halves -- the steps RUN, and an empty `outputDir` is refused.

Every process spawned here is this interpreter (`sys.executable`), not a real
`cmake`: the point under test is the executor's dispatch and status rules, and
a test that needed a toolchain installed would be a test of the host.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

import tan.commands.build.execute as execute_module
from tan.commands.build.execute import execute_slices
from tan.commands.build.manifest import PostBuildManifest
from tan.core.build_plan import parse_build_plan

PYTHON = json.dumps(sys.executable)

_SENTINEL = object()


def _plan(
    post_commands: str = "[]",
    *,
    backend: str = "baremetal",
    artifacts: str = '{"outputDir": "build/c1/output"}',
    command: str | None = None,
    missing_tool: str = "skip",
    policy: object = _SENTINEL,
) -> str:
    """`policy=None` omits `executionPolicy` entirely, so the CLI's built-in
    defaults apply -- distinct from the default here, which pins it."""
    cmd = command or f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "build/c1"}}'
    if policy is _SENTINEL:
        policy_json = (
            f'{{"missingTool": "{missing_tool}", "nullCommand": "skip", '
            f'"unknownBackend": "fail"}}'
        )
    else:
        policy_json = json.dumps(policy)
    return f"""{{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml", "sku": "S",
      "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "executionPolicy": {policy_json},
      "slices": [{{
        "coreId": "c1", "backend": "{backend}", "buildDir": "build/c1", "appDir": "app",
        "configArtefacts": [], "toolchain": null, "artifacts": {artifacts}, "debug": {{}},
        "command": {cmd}, "env": {{}}, "envAppendPath": {{}},
        "postCommands": {post_commands}
      }}]
    }}"""


def _step(script: str, cwd: str = "build/c1") -> str:
    return json.dumps({"tool": sys.executable, "args": ["-c", script], "cwd": cwd})


def _run(plan_text: str, tmp_path, lines: list[str] | None = None):
    return execute_slices(
        parse_build_plan(plan_text),
        build_root=tmp_path,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=(lines.append if lines is not None else (lambda s: None)),
    )


def _seed_zephyr_evidence(tmp_path) -> None:
    """`zephyr_boilerplate_loaded` looks under west's own nested `build/`
    inside the slice cwd, not at the slice cwd itself."""
    nested = tmp_path / "build" / "c1" / "build"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "CMakeCache.txt").write_text("ZEPHYR_BASE:PATH=/z\n")
    (nested / "zephyr").mkdir(exist_ok=True)


def _links(name: str = "firmware.elf") -> str:
    """A step that writes a file into the slice's `artifacts.outputDir` --
    what a real `cmake --build .` does for a baremetal slice, and the only
    thing the evidence guard accepts as proof it happened."""
    return (
        "import pathlib; p = pathlib.Path('output'); p.mkdir(exist_ok=True); "
        f"(p / {name!r}).write_text('elf')"
    )


def test_the_defect_a_post_command_that_never_runs_leaves_nothing_built(tmp_path):
    """The tan-cli#550 defect itself, reproduced through the plan: with the
    build step present in the plan, the slice's output directory must not be
    empty when the run reports success."""
    out = _run(_plan(f"[{_step(_links())}]"), tmp_path)
    assert out[0].status == "succeeded"
    assert (tmp_path / "build" / "c1" / "output" / "firmware.elf").is_file()


def test_a_failing_post_command_fails_the_slice_and_carries_its_exit_code(tmp_path):
    """The configure exits 0 and the build step exits 3 -- before this change
    the slice reported `ok`, because nothing ran the build step at all."""
    out = _run(_plan(f"[{_step('import sys; sys.exit(3)')}]"), tmp_path)
    assert out[0].status == "failed"
    assert out[0].exit_code == 3
    assert out[0].message == (
        "slice `c1` post-build step 1 of 1 "
        f"(`{sys.executable} -c import sys; sys.exit(3)`) terminated with exit code: 3"
    )


def test_post_commands_run_in_plan_order_and_stop_at_the_first_failure(tmp_path):
    """Ordered, and short-circuiting: a step after a failed one must not run,
    the same way `make` does not link after a failed compile."""
    marker = tmp_path / "build" / "c1" / "third-ran"
    out = _run(
        _plan(
            "["
            + _step("import pathlib; pathlib.Path('first-ran').write_text('1')")
            + ","
            + _step("import sys; sys.exit(7)")
            + ","
            + _step(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('3')")
            + "]"
        ),
        tmp_path,
    )
    assert out[0].status == "failed"
    assert out[0].exit_code == 7
    assert "post-build step 2 of 3" in out[0].message
    assert (tmp_path / "build" / "c1" / "first-ran").is_file()
    assert not marker.exists()


def test_a_post_command_is_not_run_when_the_slices_own_command_failed(tmp_path):
    """Nothing to build on top of a failed configure -- and running the build
    step anyway would report the WRONG failure to the user."""
    step = _step("import pathlib; pathlib.Path('post-ran').write_text('1')")
    failing_configure = json.dumps(
        {"tool": sys.executable, "args": ["-c", "import sys; sys.exit(1)"], "cwd": "build/c1"}
    )
    out = _run(_plan(f"[{step}]", command=failing_configure), tmp_path)
    assert out[0].status == "failed"
    assert out[0].message == "slice `c1` terminated with exit code: 1"
    assert not (tmp_path / "build" / "c1" / "post-ran").exists()


def test_a_post_commands_output_is_streamed_to_the_caller(tmp_path):
    """A build step's compiler diagnostics are the most useful output the
    whole run produces -- swallowing them would trade one silence for
    another."""
    lines: list[str] = []
    step = _step("print('compiling main.c')")
    _run(_plan(f"[{step}]"), tmp_path, lines)
    assert any("compiling main.c" in line for line in lines)


@pytest.mark.parametrize(
    "policy,expected",
    [("skip", "skipped"), ("fail", "failed")],
)
def test_a_missing_post_command_tool_honours_the_plans_missing_tool_policy(
    tmp_path, policy, expected
):
    """The plan's `executionPolicy.missingTool` decides, for a post-build step
    exactly as for the slice's own command.

    alp-sdk's `build-plan-v1.schema.json` says so in `postCommands`' own
    description -- "`executionPolicy` applies to each step exactly as it does
    to `command`" -- and `docs/adr/0001-pmt-contract-decoupling.md` says tan
    "applies the policy the plan declares; it does not hardcode a copy of the
    planner's skip rules". A first round of this fix hardcoded `failed` here
    (tan-cli#615 review, MAJOR 3)."""
    step = json.dumps({"tool": "tan-no-such-build-tool", "args": [], "cwd": "build/c1"})
    out = _run(_plan(f"[{step}]", missing_tool=policy), tmp_path)
    assert out[0].status == expected
    assert out[0].exit_code is None


def test_a_plan_with_no_execution_policy_falls_back_to_skip_for_a_post_step(tmp_path):
    """The CLI's built-in default, the same one `resolve_action` applies to the
    slice's own command when the plan omits the entry."""
    step = json.dumps({"tool": "tan-no-such-build-tool", "args": [], "cwd": "build/c1"})
    out = _run(_plan(f"[{step}]", policy=None), tmp_path)
    assert out[0].status == "skipped"


def test_the_missing_post_tool_message_names_that_the_configure_already_ran(tmp_path):
    """What the safety argument for hardcoding `failed` was really about is
    kept: a `skipped` here must not read as "never attempted"."""
    step = json.dumps({"tool": "tan-no-such-build-tool", "args": [], "cwd": "build/c1"})
    out = _run(_plan(f"[{step}]"), tmp_path)
    assert out[0].message.startswith(
        "slice `c1` post-build step 1 of 1 (`tan-no-such-build-tool`) cannot run: "
        "tool `tan-no-such-build-tool` not found -- searched "
    )
    assert out[0].message.endswith("The slice's own configure already ran.")


def test_the_missing_post_tool_refusal_keeps_the_searched_path_out_of_the_manifest(
    tmp_path, monkeypatch
):
    """tan-cli#615 review, MAJOR 1. `message` carries the full searched PATH so
    the customer's own terminal shows a fix they can apply; `manifest_message`
    is the short form, and it is what `system-manifest.yaml`'s
    `slices[].reason` persists -- that file is a build ARTEFACT that outlives
    the run and gets forwarded with support tickets, and every PATH entry on it
    is machine layout (private directory names, the login name).

    Exactly the split tan-cli#510 review round 3 made for the slice command's
    identical refusal; this path regressed it.

    The PERSISTED value is captured at `write_post_build_manifest` rather than
    read back off disk: the real write needs an SDK `--emit system-manifest`
    projection, which a hermetic test has no checkout for, so the file is never
    created here. What matters is which string is handed to the writer, and
    that is exactly what this intercepts."""
    captured: list = []
    monkeypatch.setattr(
        execute_module,
        "write_post_build_manifest",
        lambda **kw: captured.append(kw["results"]) or PostBuildManifest(None, None),
    )
    step = json.dumps({"tool": "tan-no-such-build-tool", "args": [], "cwd": "build/c1"})
    out = _run(_plan(f"[{step}]"), tmp_path)

    assert out[0].manifest_message == (
        "slice `c1` post-build step 1 of 1 (`tan-no-such-build-tool`) cannot run: "
        "tool `tan-no-such-build-tool` not found"
    )
    assert "searched" not in out[0].manifest_message
    persisted = captured[0][0].reason
    assert persisted == out[0].manifest_message
    assert "searched" not in persisted
    assert str(Path.home()) not in persisted
    # ...while the customer's own terminal still gets the actionable form.
    assert "searched PATH" in out[0].message or "searched " in out[0].message


def test_a_post_command_cwd_that_escapes_the_build_root_is_refused(tmp_path):
    """Plans are trusted input, but never trusted enough to run a process
    outside the build root -- the same guard the slice's own `command.cwd`
    gets."""
    out = _run(_plan(f"[{_step('pass', cwd='../escape')}]"), tmp_path)
    assert out[0].status == "failed"
    assert out[0].exit_code is None
    assert "was refused: path `../escape` would escape the build root" in out[0].message


@pytest.mark.parametrize("backend", ["zephyr", "yocto"])
def test_a_non_baremetal_slice_carries_no_post_commands_and_is_untouched(tmp_path, backend):
    """`west build` and `bitbake` each configure AND build in one invocation,
    so their `postCommands` is `[]`. Pinned so a future change cannot start
    requiring the key on every backend.

    Parametrized over BOTH, not just yocto: an earlier version of this test was
    named for zephyr and only ever ran yocto, so the backend its name describes
    was never exercised (tan-cli#615 review, NIT)."""
    if backend == "zephyr":
        _seed_zephyr_evidence(tmp_path)
    out = _run(_plan("[]", backend=backend, artifacts='{"outputDir": null}'), tmp_path)
    assert out[0].status == "succeeded"
    assert out[0].message is None


# --------------------------------------------------------------------------
# tan-cli#550's headline: an exit code is not evidence that firmware exists.
# --------------------------------------------------------------------------


def test_a_baremetal_slice_with_an_empty_output_dir_is_not_reported_built(tmp_path):
    """The issue's own words: "The customer gets a green build and an empty
    output directory." Every tool exited 0 and the output directory is empty,
    so the slice is `failed`, not `succeeded`."""
    step = _step("import pathlib; pathlib.Path('output').mkdir(exist_ok=True)")
    out = _run(_plan(f"[{step}]"), tmp_path)
    assert out[0].status == "failed"
    assert (tmp_path / "build" / "c1" / "output").is_dir()


def test_the_baremetal_refusal_message_is_pinned(tmp_path):
    out = _run(_plan(), tmp_path)
    assert out[0].message == (
        "core `c1` is declared `os: baremetal`, but its build produced no artefact in "
        "`build/c1/output` -- the configure and every post-build step exited 0, yet "
        "nothing was linked there. tan pins `CMAKE_RUNTIME_OUTPUT_DIRECTORY` at that "
        "directory so `tan flash`/`size`/`image` can find the firmware, so an app whose "
        "CMakeLists.txt defines no executable target (`add_executable(...)`) builds "
        "nothing this core can run."
    )


def test_the_refused_slice_keeps_the_tools_real_exit_code(tmp_path):
    """The guard refuses the RESULT, not the exit -- the same rule the
    `os: zephyr` boilerplate guard follows."""
    out = _run(_plan(), tmp_path)
    assert out[0].status == "failed"
    assert out[0].exit_code == 0


def test_a_baremetal_slice_that_linked_something_is_reported_built(tmp_path):
    """The guard must not fire on the working case -- a false failure here
    would be worse than the defect it closes."""
    out = _run(_plan(f"[{_step(_links())}]"), tmp_path)
    assert out[0].status == "succeeded"
    assert out[0].message is None


def test_the_guard_is_silent_when_the_plan_names_no_output_dir(tmp_path):
    """A plan emitted by an alp-sdk predating #1344 gives this guard nothing
    to judge by. Those plans get the `postCommands` half of the fix and not
    this half -- stated, not papered over."""
    out = _run(_plan(artifacts="{}"), tmp_path)
    assert out[0].status == "succeeded"
    assert out[0].message is None


def test_the_guard_does_not_apply_to_a_zephyr_slice(tmp_path):
    """A zephyr slice's evidence is `zephyr_boilerplate_loaded`, not an
    output directory -- applying this guard there would double-refuse."""
    _seed_zephyr_evidence(tmp_path)
    out = _run(_plan(backend="zephyr"), tmp_path)
    assert out[0].status == "succeeded"


@pytest.mark.parametrize("bad", ["../escape", "/absolute"])
def test_an_output_dir_that_escapes_the_build_root_refuses_the_slice(tmp_path, bad):
    """An `outputDir` outside the build root cannot be checked, and silently
    disabling a safety guard on a malformed plan is how the guard stops being
    one."""
    out = _run(_plan(artifacts=json.dumps({"outputDir": bad})), tmp_path)
    assert out[0].status == "failed"
    assert "the output directory its plan names cannot be checked" in out[0].message


# --------------------------------------------------------------------------
# tan-cli#615 review, MAJOR 2, answered by DISCLOSURE rather than by the
# requested freshness check -- with the measurement that ruled it out. See the
# block comment above `execute._output_dir_has_a_file`.
# --------------------------------------------------------------------------


def _age(path: Path, seconds: float) -> None:
    """Backdate `path` by `seconds`, the way a previous build's output sits on
    disk when this run starts."""
    when = time.time() - seconds
    os.utime(path, (when, when))


def test_a_stale_artefact_from_a_previous_run_still_satisfies_the_guard(tmp_path):
    """The DISCLOSED limit, pinned as a test so it is a recorded behaviour
    rather than an accident nobody measured.

    A "the artefact must be newer than this run" rule catches this, and was
    implemented -- but it cannot tell this case from an ordinary incremental
    rebuild, where `cmake --build .` correctly relinks nothing and the current
    binary keeps its old mtime. Measured end to end, that version reported
    `failed` for a second `tan build` with no source change at all. The
    limit is documented on `_baremetal_artefact_refusal`; if it is ever closed,
    it will be by asking the build system, not by comparing timestamps."""
    out_dir = tmp_path / "build" / "c1" / "output"
    out_dir.mkdir(parents=True)
    stale = out_dir / "bm"
    stale.write_text("a previous build's binary")
    _age(stale, 3600)

    step = _step("import pathlib; pathlib.Path('output').mkdir(exist_ok=True)")
    out = _run(_plan(f"[{step}]"), tmp_path)

    assert out[0].status == "succeeded"


def test_an_incremental_rebuild_that_relinks_nothing_is_still_reported_built(tmp_path):
    """The case the withdrawn freshness check false-failed, and the reason this
    guard is presence-based. `cmake --build .` exiting 0 over an up-to-date
    binary IS the build system saying the artefact is current; refusing it
    would break the commonest workflow there is."""
    out_dir = tmp_path / "build" / "c1" / "output"
    out_dir.mkdir(parents=True)
    current = out_dir / "firmware.elf"
    current.write_text("elf from the previous run, still up to date")
    _age(current, 600)

    out = _run(_plan(f"[{_step('pass')}]"), tmp_path)
    assert out[0].status == "succeeded"
    assert out[0].message is None


def test_a_directory_alone_is_not_an_artefact(tmp_path):
    """`rglob` walks directories too; only FILES are evidence."""
    (tmp_path / "build" / "c1" / "output" / "CMakeFiles").mkdir(parents=True)
    out = _run(_plan(), tmp_path)
    assert out[0].status == "failed"
    assert "produced no artefact" in out[0].message


# --------------------------------------------------------------------------
# tan-cli#615 review, MINOR 2: the cancellation path through a post-build step.
# --------------------------------------------------------------------------


def test_cancelling_during_a_post_build_step_stops_it(tmp_path):
    """The stated reason `_spawn_step` was extracted at all: a post-build
    `cmake --build` must honour Ctrl-C, not keep compiling while tan reports
    the slice cancelled. Nothing pinned that until this test.

    `cancelled()` goes true only once the step has actually started (it writes
    a marker first), so the slice's own command runs to completion normally and
    the cancellation observed is unambiguously the POST step's."""
    marker = tmp_path / "build" / "c1" / "post-started"
    script = (
        f"import pathlib, time; pathlib.Path({str(marker)!r}).write_text('1'); "
        "time.sleep(60)"
    )
    started = time.monotonic()
    out = execute_slices(
        parse_build_plan(_plan(f"[{_step(script)}]")),
        build_root=tmp_path,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
        cancelled=marker.exists,
    )
    elapsed = time.monotonic() - started
    assert marker.is_file(), "the post step never started -- this tests the wrong thing"
    assert out[0].status == "cancelled"
    assert out[0].message == "slice `c1` cancelled"
    assert elapsed < 20, f"cancellation took {elapsed:.1f}s -- the step was waited out"
