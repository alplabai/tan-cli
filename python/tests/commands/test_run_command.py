# SPDX-License-Identifier: Apache-2.0
"""`tan run` -- CLI-level tests.

Two things this file exists to prove, per the task that produced `run_cmd.py`:

1. **`run` is a distinct command, not an alias for `build` or `flash`.**
   Verified against the released oracle binary (`tan.exe run --help` vs.
   `tan.exe build --help` vs. `tan.exe flash --help`: disjoint option sets)
   and pinned two ways here -- structurally (the registered callable is not
   `build`'s or `flash`'s) and by option set (`run --help` carries `run`'s own
   flags and neither `build`'s nor `flash`'s own-only ones). A future refactor
   that silently turned `run` into `app.command("run")(build)` would fail the
   structural pin even though nothing about `run`'s own help text changed.

2. **The dispatch in `_run()` reaches every `RunAction` correctly.** Today
   `tan build`'s engine cannot supply a real `native_sim_target` signal (see
   `run_cmd`'s module doc), so `EXECUTE_NATIVE`/`FLASH` are unreachable via a
   real end-to-end invocation in this checkout. These cases monkeypatch
   `decide_run_action` directly -- the same isolation the Rust oracle's own
   tests use (`execute_native_arm`, `manifest_stale_refusal` called with a
   synthetic outcome, not a live build) -- so the WIRING is proven correct
   independently of when the upstream signal lands.

Uses a throwaway local `typer.Typer()` with only `run` registered, rather than
spawning `python -m tan run ...`: `run` is not yet wired into
`tan/cli.py` (that file is a shared registration point another workflow step
owns), so a real subprocess invocation would 404 until that one-line
registration lands. Exercising the exact same `run_cmd.run` callable through
`CliRunner` is the same test either way -- only the transport differs -- and
lets this suite pass today.
"""
import json

import typer
from typer.testing import CliRunner

from tan.commands import flash_cmd, run_cmd
from tan.commands.build_cmd import build as build_fn
from tan.commands.flash_cmd import flash as flash_fn
from tan.commands.run_cmd import run as run_fn
from tan.core.run import RunAction
from tan.exit_codes import ExitCode


def _app() -> typer.Typer:
    app = typer.Typer(add_completion=False)
    app.command("run")(run_fn)
    # A single-command Typer app collapses into a bare CLI (Click's own
    # behaviour: the one command becomes the whole program, so "run" itself
    # then parses as an unexpected extra argument). The real `tan.cli.app`
    # never collapses -- it registers many commands -- so a second, unused
    # one here keeps this throwaway app dispatching "run" as a SUBCOMMAND,
    # matching the real app's shape.
    app.command("_unused")(lambda: None)
    return app


# --- distinct, not an alias -------------------------------------------------


def test_run_is_not_an_alias_for_build_or_flash():
    assert run_fn is not build_fn
    assert run_fn is not flash_fn


def test_run_help_lists_its_own_flags_not_builds_or_flashs():
    result = CliRunner().invoke(_app(), ["run", "--help"])
    assert result.exit_code == 0, result.output
    for flag in ("--flash", "--core", "--project", "--board-yaml", "--sdk-root", "--format"):
        assert flag in result.output, result.output
    # `build`-only flags (crates/tan-cli/src/cli.rs BuildArgs) must not leak in.
    for flag in ("--plan", "--materialise", "--native", "--manifest", "--pristine",
                 "--no-auto-bootstrap"):
        assert flag not in result.output, result.output
    # `flash`-only flags (crates/tan-cli/src/cli.rs FlashArgs) must not leak in.
    for flag in ("--dry-run", "--helper", "--skip-missing-tools", "--build-root"):
        assert flag not in result.output, result.output


# --- end to end: the one path this checkout's build engine can reach -------


def test_run_reports_build_failed_when_no_sdk_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(_app(), ["run", "--format", "json"])
    payload = json.loads(result.output)
    assert payload["command"] == "run"
    assert payload["ok"] is False
    assert payload["exitCode"] == 1
    # Same code `tan build` itself would report -- `run` retags the delegated
    # build's envelope's `command`, never its issue codes.
    assert payload["issues"][0]["code"] == "build.plan-unavailable"


def test_run_help_text_mode_reaches_a_command_error_free(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(_app(), ["run"])
    assert result.exit_code == 1
    assert result.stdout == ""  # text mode: nothing but stderr


# --- internal dispatch: every RunAction, isolated from the build engine ----


def _stub_build(**_kwargs):
    return ExitCode.SUCCESS, {"schemaVersion": "1", "slices": [], "warnings": []}, []


def test_internal_run_reaches_build_only_when_no_flash(tmp_path, monkeypatch):
    monkeypatch.setattr(run_cmd, "_build", _stub_build)
    exit_code, data, issues, text = run_cmd._run(
        build_root=str(tmp_path), sdk_root=None, sdk_root_for_stamp=None, board_yaml=None,
        flash=False, core=None, json_mode=False,
    )
    assert exit_code == ExitCode.SUCCESS
    assert issues == []
    assert text[-1] == "run: built; pass --flash to program the board."


def test_internal_run_refuses_flash_when_target_unconfirmed(tmp_path, monkeypatch):
    """The current, honest behaviour: `native_sim_target` is always `None`
    until `tan build` gains a post-build manifest write, so `--flash` always
    refuses rather than guessing -- see `run_cmd`'s module doc."""
    monkeypatch.setattr(run_cmd, "_build", _stub_build)
    exit_code, data, issues, text = run_cmd._run(
        build_root=str(tmp_path), sdk_root=None, sdk_root_for_stamp=None, board_yaml=None,
        flash=True, core=None, json_mode=True,
    )
    assert exit_code == ExitCode.RUNTIME_FAILURE
    assert issues[0].code == "run.manifest-stale"
    assert text[-1] == issues[0].message
    # Finding 6: the oracle's message spells an em dash (run/mod.rs:189-192),
    # not an ASCII "--" -- it ships verbatim into `issues[].message`, which
    # the extension renders.
    assert "run: --flash refused — this build's own outcome" in issues[0].message
    assert "--flash refused --" not in issues[0].message


def test_internal_run_build_failed_short_circuits(tmp_path, monkeypatch):
    def failing_build(**_kwargs):
        from tan.envelope import Issue

        return ExitCode.RUNTIME_FAILURE, None, [Issue("build.plan-unavailable", "error", "no sdk")]

    monkeypatch.setattr(run_cmd, "_build", failing_build)
    exit_code, data, issues, text = run_cmd._run(
        build_root=str(tmp_path), sdk_root=None, sdk_root_for_stamp=None, board_yaml=None,
        flash=True, core=None, json_mode=True,
    )
    # `--flash` is irrelevant: a failed build never reaches the flash decision.
    assert exit_code == ExitCode.RUNTIME_FAILURE
    assert [i.code for i in issues] == ["build.plan-unavailable"]
    assert data is None


def test_internal_run_flash_delegates_to_the_flash_engine_at_the_built_root(tmp_path, monkeypatch):
    """`run --flash` must target the SAME `build_root` this run just built --
    never a bare `"."` under a different cwd (mirrors the Rust oracle's
    `flash_args_target_the_project_base_not_cwd` regression test), and must
    let `flash_cmd._run` derive `<build_root>/build` itself (`build_root_arg:
    None`) rather than handing it the project root as `build_root_arg`
    (Finding 3: that made `run --flash` probe `<project>/system-manifest.yaml`
    instead of `<project>/build/system-manifest.yaml`, the file a real build
    actually writes)."""
    monkeypatch.setattr(run_cmd, "_build", _stub_build)
    monkeypatch.setattr(run_cmd, "decide_run_action", lambda *a, **k: RunAction.FLASH)
    calls = {}

    def fake_flash_run(**kwargs):
        calls.update(kwargs)
        return (
            ExitCode.SUCCESS,
            {"schemaVersion": "1", "buildRoot": kwargs["app_path"], "entries": []},
            [],
            ["flash: 0 failure(s)."],
            None,
        )

    monkeypatch.setattr(flash_cmd, "_run", fake_flash_run)
    exit_code, data, issues, text = run_cmd._run(
        build_root=str(tmp_path), sdk_root="/sdk", sdk_root_for_stamp="/sdk", board_yaml=None,
        flash=True, core="m55_hp", json_mode=False,
    )
    assert exit_code == ExitCode.SUCCESS
    # `flash_cmd._run` derives `<app_path>/build` itself -- passing the
    # project root as `build_root_arg` too (the old bug) would instead make it
    # probe `<project root>/system-manifest.yaml`.
    assert calls["build_root_arg"] is None
    assert calls["app_path"] == str(tmp_path)
    assert calls["app_path"] != "."
    assert calls["core"] == "m55_hp"
    assert calls["sdk_root_arg"] == "/sdk"
    assert text == ["flash: 0 failure(s)."]


def test_internal_run_reaches_flash_via_the_real_recorded_signal_not_a_stub(tmp_path, monkeypatch):
    """The wiring this unit exists for: `--core`/`--flash` reach the flash
    engine driven by the REAL `tan.commands.build.execute.last_manifest_write`
    recorder + the REAL `decide_run_action` -- neither monkeypatched here,
    unlike `test_internal_run_flash_delegates_to_the_flash_engine_at_the_
    built_root` above (which replaces `decide_run_action` itself). Simulates
    what a hardware build's OWN dispatch would have just recorded
    (`manifest_written=True`, `native_sim_target=False`) rather than spawning
    a real `west build`."""
    from tan.commands.build import execute as execute_module

    def _stub_build_and_record(**_kwargs):
        # Stands in for `execute_slices` having just run to completion inside
        # `_build` -- the recorder is set exactly the way `execute_slices`
        # itself sets it (see `execute.py`'s own module doc for why reading
        # it afterward is safe).
        execute_module._last_manifest_write = execute_module._ManifestWriteSignal(
            manifest_written=True, native_sim_target=False
        )
        return ExitCode.SUCCESS, {"schemaVersion": "1", "slices": [], "warnings": []}, []

    monkeypatch.setattr(run_cmd, "_build", _stub_build_and_record)
    calls = {}

    def fake_flash_run(**kwargs):
        calls.update(kwargs)
        return (
            ExitCode.SUCCESS,
            {"schemaVersion": "1", "buildRoot": kwargs["app_path"], "entries": []},
            [],
            ["flash: 0 failure(s)."],
            None,
        )

    monkeypatch.setattr(flash_cmd, "_run", fake_flash_run)
    exit_code, data, issues, text = run_cmd._run(
        build_root=str(tmp_path), sdk_root="/sdk", sdk_root_for_stamp="/sdk", board_yaml=None,
        flash=True, core="m55_hp", json_mode=False,
    )
    assert exit_code == ExitCode.SUCCESS
    assert issues == []
    assert calls["app_path"] == str(tmp_path)
    assert calls["core"] == "m55_hp"
    assert text == ["flash: 0 failure(s)."]


def test_internal_run_reset_before_build_stops_a_stale_signal_reaching_flash(tmp_path, monkeypatch):
    """A previous invocation's leftover recorder state must never leak into a
    build that never reaches dispatch. Seed the recorder as if a PRIOR
    hardware build had just written its manifest, then call `_run` with a
    stubbed `_build` that does NOT touch the recorder (mirrors a real
    early-refusal build, e.g. no SDK found) -- the reset in `_run` must make
    this refuse via `MANIFEST_STALE`, not fall through to `FLASH` on the
    stale leftover."""
    from tan.commands.build import execute as execute_module

    execute_module._last_manifest_write = execute_module._ManifestWriteSignal(
        manifest_written=True, native_sim_target=False
    )
    monkeypatch.setattr(run_cmd, "_build", _stub_build)
    exit_code, data, issues, text = run_cmd._run(
        build_root=str(tmp_path), sdk_root=None, sdk_root_for_stamp=None, board_yaml=None,
        flash=True, core=None, json_mode=True,
    )
    assert exit_code == ExitCode.RUNTIME_FAILURE
    assert issues[0].code == "run.manifest-stale"


def test_flash_args_target_the_project_base_not_cwd():
    """Ported from the Rust oracle's own
    `flash_args_target_the_project_base_not_cwd` (run/mod.rs:743-749)."""
    fa = run_cmd._flash_args_for("/some/project/dir", "m33")
    assert fa["app_path"] == "/some/project/dir"
    assert fa["app_path"] != "."
    assert fa["build_root_arg"] is None
    assert fa["core"] == "m33"


# --- EXECUTE_NATIVE arm: isolated from `_run`, the same way the Rust oracle's
# own tests exercise `execute_native_arm` directly ---------------------------


def _built(**overrides):
    base = {
        "build_exit": ExitCode.SUCCESS,
        "build_data": {"schemaVersion": "1", "slices": [], "warnings": []},
        "build_issues": [],
    }
    base.update(overrides)
    return base


def test_with_exec_leaves_a_null_build_data_untouched():
    """Finding 8: the oracle only nests `exec` under `data` when `data` is
    already an object (`and_then(Value::as_object_mut)`, run/mod.rs:337-341,
    :415) -- `data: null` must stay `null`, not become a synthesised
    `{"exec": ...}` the oracle would never emit."""
    assert run_cmd._with_exec(None, {"executed": False}) is None
    assert run_cmd._with_exec({"a": 1}, {"executed": False}) == {
        "a": 1,
        "exec": {"executed": False},
    }


def test_execute_native_arm_json_mode_skips_the_spawn(monkeypatch):
    monkeypatch.setattr(run_cmd, "_find_native_sim_exe", lambda *_a: "/build/zephyr/zephyr.exe")
    b = _built()
    exit_code, data, issues, text = run_cmd._execute_native_arm(
        "unused", None, True, b["build_exit"], b["build_data"], b["build_issues"],
        json_mode=True,
    )
    assert exit_code == ExitCode.SUCCESS
    assert issues == []
    assert data["exec"] == {
        "executed": False,
        "reason": "native_sim exec skipped in --format json (run in text mode to execute)",
        "binary": "/build/zephyr/zephyr.exe",
    }


def test_execute_native_arm_reports_unavailable_when_no_exe(monkeypatch):
    monkeypatch.setattr(run_cmd, "_find_native_sim_exe", lambda *_a: None)
    b = _built()
    exit_code, data, issues, text = run_cmd._execute_native_arm(
        "unused", None, True, b["build_exit"], b["build_data"], b["build_issues"],
        json_mode=True,
    )
    assert exit_code == ExitCode.RUNTIME_FAILURE
    assert issues[0].code == "run.native-sim-unavailable"
    # Finding 6: the oracle's message spells an em dash (run/mod.rs:292-294).
    assert "artefact missing) — see build" in issues[0].message
    assert "artefact missing) --" not in issues[0].message


def test_execute_native_arm_refuses_stale_exe_when_manifest_write_unconfirmed(tmp_path):
    """R1 regression, ported from the Rust oracle's own
    `execute_native_arm_refuses_stale_exe_when_manifest_write_unconfirmed`
    (run/mod.rs): a stale native_sim manifest (`status: ok`) and its
    `zephyr.exe` sit on disk from an earlier run; THIS run's manifest write
    failed (`manifest_written=False`). The arm must refuse rather than execute
    last run's binary, even though the exe is genuinely resolvable on disk --
    and must accept the SAME exe once the write is confirmed."""
    zephyr_dir = tmp_path / "build" / "native_sim-zephyr" / "build" / "zephyr"
    zephyr_dir.mkdir(parents=True)
    elf = zephyr_dir / "zephyr.elf"
    elf.write_text("", encoding="utf-8")
    (zephyr_dir / "zephyr.exe").write_text("", encoding="utf-8")
    manifest = (
        "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: native_sim\n"
        "  os: zephyr\n  board: native_sim\n  status: ok\n  output_artefact: "
        f"{elf.as_posix()}\nipc: []\nhelper_mcus: []\nboot_order: []\n"
    )
    (tmp_path / "build" / "system-manifest.yaml").write_text(manifest, encoding="utf-8")

    # The exe IS resolvable on disk -- the only thing that changes the
    # outcome below is the write-confirmation flag.
    assert run_cmd._find_native_sim_exe(str(tmp_path), None) is not None

    b = _built()
    refused = run_cmd._execute_native_arm(
        str(tmp_path), None, False, b["build_exit"], b["build_data"], b["build_issues"],
        json_mode=True,
    )
    assert refused[0] == ExitCode.RUNTIME_FAILURE
    assert refused[2][0].code == "run.native-sim-unavailable"

    accepted = run_cmd._execute_native_arm(
        str(tmp_path), None, True, b["build_exit"], b["build_data"], b["build_issues"],
        json_mode=True,
    )
    assert accepted[0] == ExitCode.SUCCESS
    assert accepted[1]["exec"]["executed"] is False


# --- `_find_native_sim_exe`: real tmp_path trees, oracle layout -------------
# Ported from the Rust oracle's own `find_native_sim_exe_*` tests
# (run/mod.rs), which this port had zero coverage for (every CLI test
# monkeypatched the function away) -- including
# `find_native_sim_exe_none_when_slice_skipped_even_with_stale_exe_on_disk`,
# which pins the exact stale-binary defect `_execute_native_arm`'s
# `manifest_written` gate exists to catch one layer up.


def _write_manifest(base, *, status: str, elf) -> None:
    (base / "build").mkdir(parents=True, exist_ok=True)
    manifest = (
        "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: native_sim\n"
        f"  os: zephyr\n  board: native_sim\n  status: {status}\n  output_artefact: "
        f"{elf.as_posix()}\nipc: []\nhelper_mcus: []\nboot_order: []\n"
    )
    (base / "build" / "system-manifest.yaml").write_text(manifest, encoding="utf-8")


def test_find_native_sim_exe_from_manifest_sibling_of_elf(tmp_path):
    zephyr_dir = tmp_path / "build" / "native_sim-zephyr" / "build" / "zephyr"
    zephyr_dir.mkdir(parents=True)
    elf = zephyr_dir / "zephyr.elf"
    elf.write_text("", encoding="utf-8")
    exe = zephyr_dir / "zephyr.exe"
    exe.write_text("", encoding="utf-8")
    _write_manifest(tmp_path, status="ok", elf=elf)

    # The manifest carries the posix-spelled artefact path (the manifest
    # writer's convention, mirrored in the fixture's `.as_posix()`); the
    # resolver splits on whichever separator the input actually uses rather
    # than normalising through `pathlib`, so the result keeps that spelling
    # too (see `native_sim_exe_beside`'s docstring).
    assert run_cmd._find_native_sim_exe(str(tmp_path), None) == exe.as_posix()


def test_find_native_sim_exe_none_for_hardware_manifest(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: m55_hp\n  os: zephyr\n  "
        "board: alp_e1m_aen701_m55_hp\n  status: ok\nipc: []\nhelper_mcus: []\nboot_order: "
        "[]\n",
        encoding="utf-8",
    )

    assert run_cmd._find_native_sim_exe(str(tmp_path), None) is None


def test_find_native_sim_exe_none_when_slice_skipped_even_with_stale_exe_on_disk(tmp_path):
    """Regression for the stale-binary defect: a slice that THIS run left
    `status: skipped` must NOT resolve to a `zephyr.exe` still sitting on disk
    from a previous successful build."""
    zephyr_dir = tmp_path / "build" / "native_sim-zephyr" / "build" / "zephyr"
    zephyr_dir.mkdir(parents=True)
    elf = zephyr_dir / "zephyr.elf"
    elf.write_text("", encoding="utf-8")
    (zephyr_dir / "zephyr.exe").write_text("", encoding="utf-8")
    # Last run's exe is still on disk, but THIS run skipped the slice.
    _write_manifest(tmp_path, status="skipped", elf=elf)

    assert run_cmd._find_native_sim_exe(str(tmp_path), None) is None


def test_find_native_sim_exe_none_when_manifest_absent(tmp_path):
    assert run_cmd._find_native_sim_exe(str(tmp_path), None) is None


# --------------------------------------------------------------------------
# tan-cli#497 defect 4 -- the SDK-resolution warnings reach TEXT mode too
# --------------------------------------------------------------------------


def _broken_pin_project(tmp_path):
    """A project whose `.alp/sdk-path` names a checkout that does not resolve,
    beside a sibling `alp-sdk` that discovery DOES find -- so resolution
    answers with a DIFFERENT checkout than the pin names. `conftest.py`'s
    autouse fixture has already repointed HOME, so `~/.alp/sdk-default` cannot
    interfere."""
    sdk = tmp_path / "alp-sdk" / "scripts"
    sdk.mkdir(parents=True)
    (sdk / "alp_project.py").write_text("", encoding="utf-8")
    project = tmp_path / "proj"
    (project / ".alp").mkdir(parents=True)
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    (project / ".alp" / "sdk-path").write_text(
        json.dumps({"sdkPath": str(tmp_path / "gone-checkout")}), encoding="utf-8"
    )
    return project


def test_the_sdk_pin_warning_reaches_run_text_mode_not_only_json(tmp_path, monkeypatch):
    """tan-cli#497 defect 4. `run` COMPUTED `sdk.project-pin-unresolved` and
    prepended it to `issues`, but text mode prints `text_lines` -- a separate
    list the warning never reached -- so the DEFAULT path discarded it
    silently. `tan build` in the identical workspace printed the line and `tan
    run` did not, which is exactly the silence tan-cli#263 exists to remove
    and which `run_cmd`'s own comment says `run` must not repeat.

    Fails against dev: stderr carries the build refusal and nothing else."""
    project = _broken_pin_project(tmp_path)
    monkeypatch.chdir(project)
    result = CliRunner().invoke(_app(), ["run"])
    assert result.exit_code != 0
    lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
    assert lines[0].startswith("warning: .alp/sdk-path names")
    assert "gone-checkout" in lines[0]
    # The build refusal is still reported after it -- the warning is prepended,
    # never a replacement.
    assert len(lines) > 1


def test_the_run_text_warnings_are_the_same_ones_json_reports_in_the_same_order(
    tmp_path, monkeypatch
):
    """The two channels are composed from ONE list, so they cannot disagree
    about which warnings applied. Pinned because the defect was precisely a
    second, hand-maintained rendering that had drifted to empty."""
    project = _broken_pin_project(tmp_path)
    monkeypatch.chdir(project)
    text = CliRunner().invoke(_app(), ["run"]).stderr
    doc = json.loads(CliRunner().invoke(_app(), ["run", "--format", "json"]).stdout)
    warnings = [i for i in doc["issues"] if i["severity"] == "warning"]
    assert [i["code"] for i in warnings] == ["sdk.project-pin-unresolved"]
    for issue in warnings:
        assert f"{issue['severity']}: {issue['message']}" in text


def test_a_clean_workspace_prints_no_run_resolution_warning(tmp_path, monkeypatch):
    """The negative control: with no pin and no foreign global default nothing
    extra may be printed, or a fix that emitted unconditionally would look
    identical to the cases above."""
    sdk = tmp_path / "alp-sdk" / "scripts"
    sdk.mkdir(parents=True)
    (sdk / "alp_project.py").write_text("", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    monkeypatch.chdir(project)
    result = CliRunner().invoke(_app(), ["run"])
    assert "warning: .alp/sdk-path names" not in result.stderr
