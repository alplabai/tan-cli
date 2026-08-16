# SPDX-License-Identifier: Apache-2.0
"""`tan run` -- build the project, then run it: execute the produced
`native_sim` binary for a host target, or flash a hardware target.

Port of `crates/tan-cli/src/commands/run/mod.rs`. A THIN ORCHESTRATOR: it
reuses `tan build`'s engine (`build_cmd._build`, the same engine
`build_cmd.build` calls) and, on a hardware target with `--flash`,
`tan flash`'s engine (`flash_cmd._run`) -- never re-deriving either. The pure
decision -- execute vs flash vs short-circuit -- lives in `tan.core.run`; this
file resolves paths, probes the filesystem for a runnable `native_sim`
binary, and spawns it.

**`run` is a DISTINCT command, not an alias for `build` or `flash`.**
`tan build --help` and `tan flash --help` list a disjoint option set from
`tan run --help` (verified against the released oracle binary: `run` has
`--flash`/`--core` and neither `--plan`/`--materialise`/`--native` from
`build` nor `--dry-run`/`--helper`/`--skip-missing-tools` from `flash`), and
`crates/tan-cli/src/cli.rs` declares `Run(RunArgs)` as its own `Commands`
variant dispatched to its own module -- never routed through `Command::Build`
or `Command::Flash`. What IS shared is the *engine*: `run` composes the same
two engines `build` and `flash` already own, exactly once each, rather than
re-implementing a third copy of either.

**The `native_sim_target`/`manifest_written` signal is now real.**
`tan.commands.build.execute.execute_slices` writes the post-build
`system-manifest.yaml` as a side effect of every dispatch (`tan build`'s own
CLI invocation gets it too, not just `run`'s), and records the two signals
`decide_run_action` needs via `execute.last_manifest_write()` -- a
same-process recorder, not a widened return value, because `execute_slices`
is reached only through `tan.commands.build_cmd._dispatch` / `_build`, both
out of THIS module's ownership for this change (a disjoint parallel-unit
split, not an architecture choice); see `execute.py`'s own module docstring
for the full reasoning and why reading the recorder here is still safe
against the R1 staleness defect (three attempts in the Rust oracle) that the
module doc below and `test_execute_native_arm_refuses_stale_exe_when_
manifest_write_unconfirmed` both pin. `_run` resets the recorder immediately
before calling the build engine (`execute.reset_last_manifest_write()`) so a
build that never reaches dispatch -- an early coded refusal, or a
monkeypatched `_build` stub in a test -- reads the honest default
`(False, None)` rather than a previous invocation's leftover.

**`--flash`/`--core` reach the flash engine once the build confirms a
hardware target.** `decide_run_action`'s own decision table still makes a
silent `BUILD_ONLY` no-op unreachable under `flash_requested=True` regardless
of `native_sim_target`/`manifest_written` (`tan.core.run`'s own tests pin
this) -- a hardware target without a confirmed manifest write still refuses
via `MANIFEST_STALE`, never guesses. `--core` is forwarded verbatim to the
native flash path's `--core` once the `FLASH` arm is actually reached
(`_flash_args_for` is unit-tested in isolation, the same way the Rust oracle
tests `flash_args_for`).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer

from tan.commands import flash_cmd
from tan.commands.build import execute
from tan.commands.build_cmd import (
    BuildError,
    _abs_posix,
    _build,
    _is_sdk_root,
    resolve_sdk_root_ladder,
)
from tan.commands.sdk_cmd import global_default_foreign_project_issue, project_pin_issue
from tan.core.flash_plan import resolve_artefact_path
from tan.core.global_flags import accept_global_flags
from tan.core.plan_exec import normalize_path
from tan.core.run import RunAction, decide_run_action, native_sim_exe_beside, native_sim_slice
from tan.core.system_manifest import SystemManifestError, parse_system_manifest
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

#: `data.exec` skip reason JSON mode reports when a `native_sim` binary was
#: found but not executed (never spawned under `--format json`: a process
#: that never closes stdout would hang the one-envelope-per-invocation
#: contract). Verbatim from the Rust oracle's `NATIVE_SIM_JSON_SKIP_REASON`.
_NATIVE_SIM_JSON_SKIP_REASON = (
    "native_sim exec skipped in --format json (run in text mode to execute)"
)

_MANIFEST_STALE_MESSAGE = (
    "run: --flash refused — this build's own outcome does not confirm it "
    "is safe to flash (either the target could not be determined, or this "
    "run's system-manifest.yaml write failed). Check the build output "
    "above, then retry `tan run --flash`."
)

_NATIVE_SIM_UNAVAILABLE_MESSAGE = (
    "run: native_sim target, but this build produced no runnable "
    "zephyr.exe (slice skipped/failed, or artefact missing) — see build "
    "output above."
)


def _find_native_sim_exe(base: str, sdk_root: str | None) -> str | None:
    """Locate the produced `native_sim` executable from the post-build
    `system-manifest.yaml` under `<base>/build`. `None` when there's no
    manifest, no native_sim slice, the slice didn't build `ok` THIS run, or
    the binary isn't on disk -- so an unbuilt/absent binary and a
    skipped/failed slice (which would otherwise resolve to a STALE
    `zephyr.exe` left from a previous run) both fall through to a refusal
    instead of silently executing old firmware. Mirrors
    `run/mod.rs::find_native_sim_exe`, including its `<base>/build` anchor
    (`base.join("build")`, run/mod.rs:232) -- `base` is the PROJECT root
    (`_run`'s `build_root` argument), never the `build/` dir itself, so this
    function does the same `os.path.join(base, "build")` the oracle does
    rather than assuming its caller already appended it."""
    build_root = os.path.join(base, "build")
    try:
        text = Path(build_root, "system-manifest.yaml").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    try:
        manifest = parse_system_manifest(text)
    except SystemManifestError:
        return None
    slice_ = native_sim_slice(manifest)
    if slice_ is None or slice_.get("status") != "ok":
        return None
    artefact = slice_.get("output_artefact") or ""
    if not artefact:
        return None
    elf_path = resolve_artefact_path(artefact, build_root, sdk_root, os.path.isfile)
    exe_path = native_sim_exe_beside(elf_path)
    return exe_path if os.path.isfile(exe_path) else None


def _exec_native_sim(exe: str) -> tuple[bool, int | None]:
    """Run the `native_sim` binary with inherited stdio (it streams live), and
    report `(ok, returncode)`. Only called in text mode -- see the module
    doc."""
    print(f"run: executing {exe}", file=sys.stderr)
    try:
        proc = subprocess.run([exe])
        return proc.returncode == 0, proc.returncode
    except OSError as err:
        print(f"run: failed to launch {exe}: {err}", file=sys.stderr)
        return False, None


def _with_exec(
    build_data: dict[str, Any] | None, exec_payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Nest `exec_payload` under `data.exec`, mirroring the oracle's own guard
    (`and_then(Value::as_object_mut)`, run/mod.rs:337-341 and :415): only when
    `build_data` is already an object. `build_data: None` -- unreachable today
    since a successful `_build` always returns a dict, but a shape the
    envelope contract allows -- passes through unchanged rather than
    synthesising `{"exec": ...}` the oracle would never emit for `data:
    null`."""
    if not isinstance(build_data, dict):
        return build_data
    return {**build_data, "exec": exec_payload}


def _flash_args_for(build_root: str, core: str | None) -> dict[str, Any]:
    """The `flash_cmd._run` kwargs for `run --flash`, anchored on the resolved
    project `build_root` (`_run`'s own project-root argument, matching the
    Rust oracle's `base`) -- never `"."`. `flash_cmd._run` derives
    `<app_path>/build` itself when `build_root_arg` is `None`
    (`_abs_join(app_dir, "build")`, flash_cmd.py:752), exactly the way
    `flash::run` derives `build_root` from `FlashArgs { build_root: None,
    .. }` -- so passing the project root as `app_path` with
    `build_root_arg: None` makes `run --flash` probe `<build_root>/build/
    system-manifest.yaml`, the SAME file this run's own `tan build` step just
    wrote, rather than `<build_root>/system-manifest.yaml` (nothing writes
    that) or a bare `"."` resolved against a different `cwd` under
    `--project <dir>`. Mirrors the Rust oracle's `flash_args_for`
    (run/mod.rs:168-177)."""
    return {"app_path": build_root, "build_root_arg": None, "core": core}


def _execute_native_arm(
    build_root: str,
    sdk_root: str | None,
    manifest_written: bool,
    build_exit: ExitCode,
    build_data: dict[str, Any] | None,
    build_issues: list[Issue],
    json_mode: bool,
) -> tuple[ExitCode, dict[str, Any] | None, list[Issue], list[str]]:
    """The `RunAction.EXECUTE_NATIVE` arm: run this build's `native_sim`
    binary, or report that there isn't a trustworthy one. Mirrors the Rust
    oracle's `execute_native_arm` (run/mod.rs:140-150), including its
    `manifest_written` gate: `_find_native_sim_exe` trusts an on-disk
    `zephyr.exe` from the manifest's `status: ok`, but that manifest is only
    THIS run's when the post-build write actually succeeded. An unconfirmed
    write (a Windows sharing violation, a failed emit) means a PREVIOUS run's
    ok-status manifest and its `zephyr.exe` may still be on disk; executing
    that would report success for an edit this run never compiled -- so the
    probe is never even attempted when the write is unconfirmed."""
    exe = _find_native_sim_exe(build_root, sdk_root) if manifest_written else None
    if exe is None:
        issues = [
            *build_issues,
            Issue("run.native-sim-unavailable", "error", _NATIVE_SIM_UNAVAILABLE_MESSAGE),
        ]
        text = _build_text_lines(build_data, build_issues) + [_NATIVE_SIM_UNAVAILABLE_MESSAGE]
        return ExitCode.RUNTIME_FAILURE, build_data, issues, text
    if json_mode:
        # Never spawned under `--format json` -- see the module doc.
        data = _with_exec(
            build_data,
            {
                "executed": False,
                "reason": _NATIVE_SIM_JSON_SKIP_REASON,
                "binary": exe,
            },
        )
        return build_exit, data, build_issues, []
    ok, rc = _exec_native_sim(exe)
    exit_code = ExitCode.SUCCESS if ok else ExitCode.RUNTIME_FAILURE
    data = _with_exec(build_data, {"binary": exe, "ok": ok, "rc": rc})
    text = _build_text_lines(build_data, build_issues)
    issues = list(build_issues)
    if not ok:
        message = (
            f"{exe} exited with code {rc}"
            if rc is not None
            else f"{exe} did not run to completion"
        )
        issues.append(Issue("run.exec-failed", "error", message))
        text.append(f"run: {message}")
    return exit_code, data, issues, text


def _build_text_lines(data: dict[str, Any] | None, issues: list[Issue]) -> list[str]:
    """The same text-mode recap `build_cmd.build` prints, reused here so a
    `tan run` that stops at the build step reads identically to `tan build`
    would have."""
    lines = [f"{issue.severity}: {issue.message}" for issue in issues]
    for result in (data or {}).get("slices", []):
        reason = f" — {result['reason']}" if "reason" in result else ""
        lines.append(f"{result['status']}: {result['coreId']} [{result['backend']}]{reason}")
    return lines


def _run(
    *,
    build_root: str,
    sdk_root: str | None,
    sdk_root_for_stamp: str | None,
    board_yaml: str | None,
    flash: bool,
    core: str | None,
    json_mode: bool,
    # Defaulted, not required: several existing unit tests call `_run`
    # directly with the pre-tan-cli#809 kwarg set, exercising paths this flag
    # never reaches (`BUILD_ONLY`/`MANIFEST_STALE`/`EXECUTE_NATIVE`) or
    # stubbing `flash_cmd._run` themselves. `False` preserves the exact
    # pre-#809 behaviour for every one of them.
    confirm: bool = False,
) -> tuple[ExitCode, dict[str, Any] | None, list[Issue], list[str]]:
    """Everything between the resolved paths and the envelope. Returns
    `(exit_code, data, issues, text_lines)`."""
    # Reset BEFORE calling the build engine, not after: see the module doc
    # and `execute.reset_last_manifest_write`'s own docstring for why an
    # unreset recorder would leak a PREVIOUS invocation's signal into a build
    # that never reaches dispatch (an early `BuildError`, or a monkeypatched
    # `_build` stub in a test).
    execute.reset_last_manifest_write()
    try:
        build_exit, build_data, build_issues = _build(
            plan_from=None,
            build_root=build_root,
            sdk_root=sdk_root,
            sdk_root_for_stamp=sdk_root_for_stamp,
            board_yaml=board_yaml,
        )
    except BuildError as err:
        # A CODED refusal (no SDK, no board.yaml, an unparsable plan, ...),
        # not a tan bug -- `build_cmd.build` catches this the same way, and
        # `run` must retag it identically: same issue code, same exit code,
        # only `command` differs.
        build_exit, build_data, build_issues = (
            err.exit_code,
            None,
            [Issue(err.code, "error", err.message)],
        )
    build_ok = build_exit == ExitCode.SUCCESS

    # The real signal from THIS build's own dispatch (see the module doc) --
    # never a re-read of `system-manifest.yaml` off disk afterward.
    manifest_written, native_sim_target = execute.last_manifest_write()

    action = decide_run_action(build_ok, native_sim_target, flash, manifest_written)

    if action in (RunAction.BUILD_FAILED, RunAction.BUILD_ONLY):
        text = _build_text_lines(build_data, build_issues)
        if action is RunAction.BUILD_ONLY and not json_mode:
            text.append("run: built; pass --flash to program the board.")
        return build_exit, build_data, build_issues, text

    if action is RunAction.MANIFEST_STALE:
        issues = [*build_issues, Issue("run.manifest-stale", "error", _MANIFEST_STALE_MESSAGE)]
        text = _build_text_lines(build_data, build_issues) + [_MANIFEST_STALE_MESSAGE]
        return ExitCode.RUNTIME_FAILURE, build_data, issues, text

    if action is RunAction.EXECUTE_NATIVE:
        return _execute_native_arm(
            build_root, sdk_root, manifest_written, build_exit, build_data, build_issues, json_mode
        )

    # RunAction.FLASH: hardware target, `--flash`, this run's manifest write
    # confirmed -- reuse the native flash path, targeting the SAME project
    # `build_root` this run just built (not a bare "." under a different cwd).
    flash_exit, flash_data, flash_issues, flash_text, _flash_sdk = flash_cmd._run(
        **_flash_args_for(build_root, core),
        sdk_root_arg=sdk_root,
        board_yaml=board_yaml,
        helper=None,
        dry_run=False,
        skip_missing_tools=False,
        capture=json_mode,
        cwd=build_root,
        # tan-cli#809: `run` had no way to arm the flash confirm gate, so a
        # hardware target (e.g. E1M-AEN801's Flow D) always landed every
        # slice as `planned` and exited 1 with `flash.nothing-flashed` --
        # whose own remedy names `--confirm`, a flag `run` rejected. The gate
        # itself stays deliberate (see flash_plan.py); this only gives `run`
        # the same opt-in `tan flash --confirm` already has.
        confirm_flag=confirm,
    )
    return flash_exit, flash_data, flash_issues, flash_text


def run(
    flash: bool = typer.Option(
        False,
        "--flash",
        help="Program the board after building (hardware targets only). Required "
        "opt-in: without it, `run` on a hardware project builds and reports but "
        "never flashes. Ignored for a native_sim/host target, which always runs "
        "the produced binary and never flashes.",
    ),
    core: str = typer.Option(
        None,
        "--core",
        metavar="CORE_ID",
        help="With --flash, flash only the slice with this core_id (forwarded "
        "verbatim to the native flash path's --core).",
    ),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="With --flash, arm the confirm gate and actually write the device. "
        "Without it (and without ALP_FLASH_FORCE=1 or flash_args.confirm: true "
        "in the manifest) a hardware target is only previewed -- every slice "
        "comes back `planned`, nothing reaches the device, and the run exits "
        "non-zero (tan-cli#809). Ignored without --flash.",
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
) -> None:
    """Build the project, then run it: execute the produced native_sim binary
    for a host target, or (with --flash) program a hardware target."""
    json_mode = output_format == "json"

    # Same resolution `build_cmd.build` performs (`run` builds via the same
    # engine, so it must anchor on the same project) -- see that function for
    # the reasoning behind each step.
    cwd = Path.cwd()
    workspace_root = cwd if project is None else Path(os.path.join(str(cwd), project))
    if board_yaml is not None and not os.path.isabs(board_yaml):
        board_yaml = os.path.join(str(workspace_root), board_yaml)
    if board_yaml is None and (workspace_root / "board.yaml").is_file():
        board_yaml = str(workspace_root / "board.yaml")
    build_root = str(Path(board_yaml).parent) if board_yaml else str(workspace_root)
    build_root = _abs_posix(build_root)
    if board_yaml is not None:
        board_yaml = _abs_posix(board_yaml)

    # Same ladder `build_cmd.build` resolves -- `--sdk-root` > `.alp/sdk-path`
    # project pin > the machine-global default (`~/.alp/sdk-default`) > the
    # positional walk (`resolve_sdk_root_ladder`); `run` builds via the same
    # engine, so it must agree with `build` on which checkout that is. No
    # `ALP_SDK_ROOT` tier (tried and reverted -- see `resolve_sdk_root_ladder`'s
    # own docstring).
    sdk_resolution = resolve_sdk_root_ladder(sdk_root, workspace_root)
    resolved_sdk_root = sdk_resolution.path
    sdk_tier = sdk_resolution.tier
    sdk_broken_pin = sdk_resolution.broken_project_pin
    sdk_foreign_default = sdk_resolution.foreign_global_default_for
    # tan-cli#257/#258 -- the exact guard `build_cmd.build` applies, for the
    # exact same reason: this line was a VERBATIM COPY of the one that carried
    # the defect, so fixing only `build` would have left its twin here.
    # `resolve_sdk_root_ladder` returns an explicit `--sdk-root` UNVALIDATED
    # (I-31 terminal-for-REPORTING, matching the oracle's
    # `resolve_sdk_tiered`), which is correct for a caller that only reports
    # the tier and wrong for one that ACTS on the path: a bogus `--sdk-root`
    # sailed through as `sdk.sourceTier: "sdkRootFlag"` and was then refused
    # for the NEXT missing thing, telling the customer their project is broken
    # when the flag they had just typed is what was wrong.
    #
    # Guarded HERE rather than inside the shared ladder because every other
    # caller depends on it staying unvalidated -- the same placement
    # `build_cmd`, `clean_cmd.sdk_root_resolves` and `flash_cmd._resolve_sdk`
    # already chose. An unresolvable explicit root is treated as no root at
    # all, so the refusal downstream is the honest "no alp-sdk checkout found"
    # and no `sdk` key is emitted, matching the oracle.
    if sdk_tier == "sdkRootFlag" and not _is_sdk_root(resolved_sdk_root):
        resolved_sdk_root = None
    sdk_root = str(resolved_sdk_root) if resolved_sdk_root is not None else None
    sdk = SdkInfo(sdk_root, sdk_tier) if sdk_root is not None else None
    # Same normalized, workspace-root-anchored stamp identity `build_cmd.build`
    # computes (tan-cli#163) -- `_build` now requires it (the sdk-switch-
    # pristine guard's stamp comparison, threaded through from `execute_slices`
    # rather than self-discovered), and `run` reuses the same engine so it must
    # resolve it the same way, not just `sdk_root` itself.
    sdk_root_for_stamp = (
        str(normalize_path(workspace_root / sdk_root)) if sdk_root is not None else None
    )
    # tan-cli#236: `boardYaml` reported only when the file really exists -- an
    # explicit `--board-yaml` skips the `is_file()` discovery guard above.
    project_obj = Project.resolved(build_root, board_yaml)

    try:
        exit_code, data, issues, text_lines = _run(
            build_root=build_root,
            sdk_root=sdk_root,
            sdk_root_for_stamp=sdk_root_for_stamp,
            board_yaml=board_yaml,
            flash=flash,
            core=core,
            confirm=confirm,
            json_mode=json_mode,
        )
    except Exception as err:  # noqa: BLE001 -- see build_cmd.build's identical guard
        exit_code = ExitCode.INTERNAL_FAILURE
        data = None
        issues = [Issue("run.internal-failure", "error", f"{type(err).__name__}: {err}")]
        text_lines = [f"run: internal failure: {type(err).__name__}: {err}"]

    # tan-cli#263 review: `run` builds via the same engine as `build` and must
    # disclose the same silently-missed project pin.
    pin_issue = project_pin_issue(sdk_broken_pin, sdk_tier)
    if pin_issue is not None:
        issues = [pin_issue, *issues]
    # tan-cli#464: same argument, one tier down -- `run` must disclose a
    # `globalDefault` answer a DIFFERENT project's bootstrap relocation
    # actually decided, the same as `build` now does.
    foreign_issue = global_default_foreign_project_issue(sdk_foreign_default)
    if foreign_issue is not None:
        issues = [foreign_issue, *issues]
    # tan-cli#497 defect 4: both of the above went into `issues` and NOWHERE
    # else, while text mode prints `text_lines` -- so the warnings reached
    # `--format json` and were silently discarded on the DEFAULT path. `tan
    # build` in the identical workspace printed the pin line and `tan run` did
    # not, which is precisely the silence tan-cli#263 exists to remove.
    # Prefixed `{severity}: {message}`, the shape `_build_text_lines` already
    # uses for a build issue, and taken from the head of `issues` so the two
    # channels can never disagree about which warnings applied or in what
    # order.
    warning_count = (pin_issue is not None) + (foreign_issue is not None)
    if warning_count:
        text_lines = [
            f"{issue.severity}: {issue.message}" for issue in issues[:warning_count]
        ] + text_lines

    # Built ONCE, for both formats: `Envelope.__init__` appends the
    # tan-cli#407 `sdk.discovery-divergent` warning at the shared seam
    # (`_with_sdk_divergence`), beyond the pin/foreign pair this function
    # already prepends by hand above (tan-cli#464). Text mode used to render
    # `text_lines`, built before any `Envelope` existed, so a seam-appended
    # divergence issue reached `--format json` and was silent on the default
    # channel (tan-cli#799) -- diffed against the pre-envelope `issues` list
    # (by value: `Issue` is a frozen dataclass) so only what the seam ADDED
    # is rendered, never a duplicate of the pin/foreign pair already in
    # `text_lines`.
    envelope = Envelope("run", project_obj, data, issues, exit_code, sdk=sdk)
    seam_extra = [issue for issue in envelope.issues if issue not in issues]
    if seam_extra:
        text_lines = [
            f"{issue.severity}: {issue.message}" for issue in seam_extra
        ] + text_lines

    if json_mode:
        emit(envelope)
    else:
        for line in text_lines:
            print(line, file=sys.stderr)
    raise typer.Exit(int(exit_code))


# tan-cli#261: adds the seven oracle `GlobalArgs` flags this command was
# still missing (`--all`/`--ci`/`--no-color`/`--non-interactive`/`--quiet`/
# `--target`/`--verbose`) on top of `--board-yaml`, already declared and read
# above; see `tan.core.global_flags`.
run = accept_global_flags(run)
