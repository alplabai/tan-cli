# SPDX-License-Identifier: Apache-2.0
"""`tan renode` -- boot the built system-manifest's single Zephyr slice in
headless Renode as a no-hardware smoke test.

Port of `crates/tan-cli/src/commands/renode/mod.rs`: the IO/subprocess half.
Every pre-flight *decision* (SKU -> descriptor, slice selection, argv, the two
console-line classifiers) is pure in `tan.core.renode_plan`; this module
resolves paths, probes PATH for the `renode` binary, and spawns + tees it.

**Renode is an external tool tan does not vendor.** How its ABSENCE is
handled is most of this command's value: a missing `renode` on PATH is a
CODED, ACTIONABLE `renode.binary-missing` issue naming what to install
(https://renode.io) -- never a traceback, never a silent `ok: true`. Mirrors
`flash_cmd.py`'s own refusal shape for a missing hardware tool.

**SCOPE: this file is the PLAIN (non-`--sim-mode`) headless smoke only.**
`--sim-mode` -- the studio hardware-simulator gateway that serves a control +
UART socket pair for `alp-sdk-vscode`'s `RealRenodeAdapter` -- is a
substantial separate subsystem in the oracle (`crates/tan-cli/src/commands/
renode/sim.rs` + `monitor.rs`, ~1100 lines, plus its own pure half in
`crates/tan-core/src/renode/sim.rs`). It is deliberately NOT ported here: the
flag is simply not declared, so `tan renode --sim-mode` is a Click usage error
(exit 2) rather than a half-working or silently-wrong gateway. Refusing at the
parser is the honest shape for an unported subsystem -- an ACCEPTED flag that
quietly did nothing would be worse, because the customer would believe the
gateway came up.

(An earlier draft of this paragraph justified the cut by claiming
`doctor_cmd.py` likewise does not declare `tan doctor`'s `--build`. That was
false -- `doctor_cmd.py:894` declares `--build` -- and the claim is removed
rather than corrected, because the cut stands on its own reasoning above and
did not need a precedent.)
Porting `--sim-mode` is its own bounded unit of work. This is a DELIBERATE,
NAMED gap, not an oversight: the follow-up unit is "port `--sim-mode`" --
`crates/tan-cli/src/commands/renode/sim.rs` + `monitor.rs` (the socket
gateway) and `crates/tan-core/src/renode/sim.rs` (its pure half).

Divergences from the Rust oracle worth flagging, both verified against the
shipped `tan.exe` rather than inferred from source:
  * `data.repl`/`data.resc`/`data.elf`/`data.logPath` and `project.root` are
    all reported in the HOST's NATIVE path style (backslashes on Windows,
    unconverted), NOT forward-slash-normalised -- unlike most other `tan`
    commands (`flash`, `doctor`, `build`). This is the oracle's own behaviour
    (`Path::to_string_lossy`, no `to_posix`), reproduced faithfully rather
    than "fixed" to match the other commands' convention.
  * `project.root` anchors on the `APP_PATH` positional (default `.`), NOT on
    the global `--project` flag -- another oracle divergence from `flash`/
    `doctor`/`build`, which all anchor `project.root` on `--project`. The
    alp-sdk checkout resolution (`--sdk-root`'s fallback chain) DOES honour
    `--project`, exactly as the oracle's own `cli_workspace_root(g)` does.
  * `sdk.root` in the envelope is the RAW `--sdk-root` value as typed, never
    absolutised -- verified: `tan renode --sdk-root ./sdk --format json`
    reports `"sdk":{"root":"./sdk", ...}`.
  * `data.renodeArgv[0]` carries `on_path()`'s own PATHEXT-match casing (e.g.
    `...\\fakebin\\renode.CMD`), where the oracle's `where`-based
    `resolve_renode_binary` reports the on-disk casing (`...\\fakebin\\
    renode.cmd`). `on_path` is shared with `flash_cmd.py`/`doctor_cmd.py`, so
    this is pre-existing there too, not renode-specific; not fixed here to
    avoid touching a helper three commands depend on for one byte-level
    diff on one field.
  * With no `--sdk-root` and `--project .`, the discovery-tier `sdk.root` is
    lexically normalised (`.../renodefx/alp-sdk`) because it is resolved
    through `pathlib.Path`, which collapses a `.` path segment on
    construction; the oracle's raw `PathBuf::join` does not normalise, and
    reports the unnormalised `.../renodefx/./alp-sdk`. Only the `--project .`
    discovery path is affected -- `--sdk-root` itself is reported raw (see
    above).
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import typer

from tan.commands.build_cmd import resolve_sdk_root_wide
from tan.commands.sdk_cmd import project_pin_issue
from tan.commands.build_output import ManifestInvalid, ManifestUnavailable, load_manifest
from tan.commands.doctor_cmd import on_path
from tan.core.renode_plan import (
    RenodeError,
    build_renode_argv,
    elf_vector_table_base,
    platform_files_for_sku,
    platform_stem_for_sku,
    renode_cpu_halted,
    renode_rejected_argv,
    select_sku,
    zephyr_elf_from_manifest,
)
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode

#: The `SystemManifestError` message prefix `parse_system_manifest` raises
#: for a `schema_version` mismatch -- distinguishing it from every other
#: parse failure is what picks `renode.manifest-schema` (ValidationFailure)
#: over `renode.manifest-invalid` (RuntimeFailure), mirroring the oracle's
#: `SystemManifestError::UnsupportedSchemaVersion` match arm.
_SCHEMA_VERSION_PREFIX = "unsupported system-manifest schema_version"


def _normalize_join(base: str, path: str) -> str:
    """`normalize_path(base.join(path))`: cwd-anchored (unless `path` is
    already absolute, matching `PathBuf::join`'s replace-on-absolute rule),
    lexically normalised, in the HOST's own separators -- deliberately NOT
    forward-slash-forced (see the module docstring)."""
    return os.path.normpath(os.path.join(base, path))


def _resolve_root_arg(raw: str | None, cwd: str, default: str) -> str:
    """A `--build-root`/`--log`-shaped override: absolute as typed
    (normalised), relative anchored on the real CWD; `default` when absent."""
    if raw is None:
        return default
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return _normalize_join(cwd, raw)


def _is_sdk_root(path: str) -> bool:
    """`util.rs::has_loader_script`, string-based and incapable of raising --
    a path with an embedded NUL or a permission-denied parent reads as "not
    an SDK root" rather than throwing out of the pre-flight guard."""
    try:
        return os.path.isfile(os.path.join(path, "scripts", "alp_project.py"))
    except (OSError, ValueError):
        return False


def _resolve_sdk_root_and_tier(
    sdk_root_arg: str | None, workspace_root: str
) -> tuple[str | None, str | None, str | None]:
    """`--sdk-root` (TERMINAL -- returned AS TYPED when it has the loader
    script, `None` otherwise, never falling through to a lower tier) > the
    project's own `.alp/sdk-path` pin > the machine-global default
    (`~/.alp/sdk-default`) > the WIDE positional walk
    (`build_cmd.resolve_sdk_root_wide`) -- wide, not the narrow ladder thirteen
    other commands take, because the oracle's `renode` reads its platform
    descriptors out of the child `<ws>/alp-sdk` over a competing `../alp-sdk`
    (tan-cli#263). No `ALP_SDK_ROOT` tier (tried and reverted -- see
    `resolve_sdk_root_ladder`'s own docstring).

    Third element (tan-cli#263 review): the broken project pin carried through
    from `resolve_sdk_root_wide`, `None` on the `--sdk-root` branch.
    """
    if sdk_root_arg is not None:
        return (
            (sdk_root_arg, "sdkRootFlag", None) if _is_sdk_root(sdk_root_arg) else (None, None, None)
        )
    found, tier, broken_pin = resolve_sdk_root_wide(None, Path(workspace_root))
    return (str(found), tier, broken_pin) if found is not None else (None, None, broken_pin)


def _data(**overrides: Any) -> dict[str, Any]:
    """The `tan renode` JSON `data` payload, camelCase, every key always
    present (mirrors the Rust `RenodeReport`'s `#[derive(Default)]`)."""
    out: dict[str, Any] = {
        "sku": "",
        "platformStem": "",
        "repl": "",
        "resc": "",
        "elf": "",
        "logPath": "",
        "timeout": 0,
        "expect": None,
        "expectFound": False,
        "renodeArgv": [],
        # `--sim-mode` only (not ported here -- see the module docstring):
        # always present and empty/zero on the plain smoke, matching the
        # oracle's own `RenodeReport::default()` fields.
        "descriptor": "",
        "controlPort": 0,
        "uartPort": 0,
    }
    out.update(overrides)
    return out


def _issue(code: str, severity: str, message: str) -> Issue:
    return Issue(code, severity, message)


# ── spawning + teeing ────────────────────────────────────────────────────────

#: Posted by each reader thread on EOF. `run_renode` treats "both readers
#: posted their sentinel" as the child's own pipes having closed -- the same
#: signal Rust's `mpsc::RecvTimeoutError::Disconnected` carries once every
#: `Sender` clone (one per pipe) has dropped.
_EOF = object()


def _pump_lossy_lines(stream, q: "queue.Queue[object]") -> None:
    """Read `\\n`-delimited lines from `stream` (binary) into `q`, lossily
    replacing invalid UTF-8 instead of dying on the first bad line -- a
    stray byte (reset framing noise, a half-initialised UART) must not
    truncate the tee log or a later `--expect` match."""
    try:
        while True:
            raw = stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace")
            while line.endswith(("\n", "\r")):
                line = line[:-1]
            q.put(line)
    except (OSError, ValueError):
        pass
    finally:
        q.put(_EOF)


def run_renode(
    argv: list[str], log_path: str, timeout_s: float, expect: str | None, echo_stdout: bool
) -> tuple[bool, int | None, bool, bool]:
    """Run Renode, tee-ing its (stdout+stderr) console to `log_path`.
    Terminates on: the `expect` marker appearing, the child exiting (both
    pipes EOF), or `timeout_s` elapsing -- the deadline fires even on a
    silent child via a reader-thread + queue-with-timeout, never a blocking
    line-iterator that would hang forever on a wedged-but-alive Renode.

    Returns `(expect_found, natural_exit, argv_rejected, cpu_halted)`:
    `natural_exit` is the child's own return code, but ONLY when it exited on
    its OWN before the deadline/kill -- a forced-kill-for-timeout child
    always reports `None` here, so a plain timeout is never mistaken for a
    self-inflicted crash. `argv_rejected`/`cpu_halted` are latched from the
    console text (Renode exits 0 in both failure shapes, so the exit status
    alone carries no signal for either).

    Raises `OSError` when the process cannot even be spawned (the binary
    vanished between the PATH gate and here, or is not executable) --
    the caller maps that to `renode.run-failed`.
    """
    parent = os.path.dirname(log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    logf = open(log_path, "w", encoding="utf-8", newline="\n")
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        logf.close()
        raise

    q: "queue.Queue[object]" = queue.Queue()
    t_out = threading.Thread(target=_pump_lossy_lines, args=(proc.stdout, q), daemon=True)
    t_err = threading.Thread(target=_pump_lossy_lines, args=(proc.stderr, q), daemon=True)
    t_out.start()
    t_err.start()

    found = False
    natural_exit: int | None = None
    argv_rejected = False
    cpu_halted = False
    eof_count = 0
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break  # deadline reached; child may still be alive (silent hang)
            try:
                item = q.get(timeout=remaining)
            except queue.Empty:
                break
            if item is _EOF:
                eof_count += 1
                if eof_count >= 2:
                    # Both readers hit EOF -- the child's own pipes closed,
                    # which happens at process exit. Capture the REAL status
                    # now, before the unconditional kill() below turns every
                    # outcome (including this one) into an indistinguishable
                    # "we killed it".
                    try:
                        natural_exit = proc.wait()
                    except OSError:
                        natural_exit = None
                    break
                continue
            line = item  # type: ignore[assignment]
            logf.write(line + "\n")
            logf.flush()
            if echo_stdout:
                print(line)
            if renode_rejected_argv(line):
                argv_rejected = True
            if renode_cpu_halted(line):
                cpu_halted = True
            if expect is not None and expect in line:
                found = True
                break
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait()
        except OSError:
            pass
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)
        logf.close()

    return found, natural_exit, argv_rejected, cpu_halted


def _rust_debug_str(text: str) -> str:
    """Rust's `{:?}` for a `&str`: double-quoted, with `\\` and `"` escaped.
    Used for `--expect`'s value in the `renode.expect-not-found` message,
    where Python's own `repr()` (single-quoted) would read differently."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _exit_status_desc(code: int) -> str:
    """A short description for a log message. Matches the oracle's
    `exit_status_desc`, which falls through to Rust `ExitStatus`'s own
    `Display` for anything that isn't a plain exit code -- on Unix that's
    `"signal: N (NAME)"` for a signal death, verified against the shipped
    `tan.exe`. A negative `returncode` is Python's own (POSIX-only) spelling
    of "killed by signal N"."""
    if code >= 0:
        return f"exit code {code}"
    try:
        import signal  # noqa: PLC0415 -- POSIX-only, kept out of module scope

        name = signal.Signals(-code).name
    except ValueError:
        return f"signal: {-code}"
    return f"signal: {-code} ({name})"


def _best_effort_vtor(elf_path: str) -> int | None:
    """Read `elf_path` purely to learn where its vector table really is, so
    the descriptor can point the CPU at it instead of letting Renode guess
    from the lowest vaddr -- which halts an MRAM-linked image before it
    executes a single instruction. Best-effort: an unreadable or unexpected
    ELF returns `None` and the caller injects nothing, leaving Renode exactly
    as it was."""
    try:
        with open(elf_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return elf_vector_table_base(data)


# ── the command ─────────────────────────────────────────────────────────────


def _run(
    app_path_arg: str,
    build_root_arg: str | None,
    board_arg: str | None,
    core_arg: str | None,
    sdk_root_arg: str | None,
    project_arg: str | None,
    image_bundle_arg: str | None,
    log_arg: str | None,
    timeout: int,
    expect: str | None,
    json_mode: bool,
    cwd: str,
) -> tuple[ExitCode, dict[str, Any], list[Issue], list[str], SdkInfo | None, Project]:
    """Everything between argument parsing and the envelope. Returns
    `(exit_code, data, issues, text_lines, sdk, project)`."""
    app_path = _normalize_join(cwd, app_path_arg)
    board_yaml_path = os.path.join(app_path, "board.yaml")
    # tan-cli#236: routed through the shared seam, though this site already
    # existence-checked by hand.
    project = Project.resolved(app_path, board_yaml_path)

    build_root = _resolve_root_arg(build_root_arg, cwd, os.path.join(app_path, "build"))
    log_path = _resolve_root_arg(log_arg, cwd, os.path.join(build_root, "renode.log"))

    # Filled in incrementally as each pre-flight step resolves, mirroring the
    # oracle's `RenodeReport`, which is one mutable struct written to as
    # `run()` progresses -- so a LATER failure (e.g. `renode.binary-missing`)
    # still reports the sku/platformStem/repl/resc/elf a caller resolved
    # before it (verified against the shipped binary).
    known: dict[str, Any] = {}

    def data(**overrides: Any) -> dict[str, Any]:
        return _data(logPath=log_path, timeout=timeout, expect=expect, **known, **overrides)

    def fail(code: str, message: str, exit_code: ExitCode = ExitCode.RUNTIME_FAILURE):
        return exit_code, data(), [_issue(code, "error", message)], [f"renode: {message}"], None, project

    # SDK-root guard: `cli_workspace_root(g)` is `cwd` joined with `--project`
    # (the GLOBAL flag) -- NOT `app_path`. See the module docstring for why
    # these two deliberately diverge in the oracle.
    workspace_root = os.path.join(cwd, project_arg) if project_arg else cwd
    sdk_root, sdk_tier, sdk_broken_pin = _resolve_sdk_root_and_tier(sdk_root_arg, workspace_root)
    if sdk_root is None:
        return fail("renode.sdk-root-not-found", "Cannot locate alp-sdk root.")
    sdk = SdkInfo(sdk_root, sdk_tier or "none")

    def fail_sdk(code: str, message: str, exit_code: ExitCode = ExitCode.RUNTIME_FAILURE):
        return exit_code, data(), [_issue(code, "error", message)], [f"renode: {message}"], sdk, project

    manifest_path = os.path.join(build_root, "system-manifest.yaml")
    try:
        _text, manifest = load_manifest(build_root)
    except ManifestUnavailable as err:
        return fail_sdk(
            "renode.manifest-unavailable",
            f"no system-manifest.yaml at {manifest_path}; run `tan build --project "
            f"{app_path}` first ({err.detail}).",
        )
    except ManifestInvalid as err:
        message = f"{err.path}: {err.detail}"
        if err.detail.startswith(_SCHEMA_VERSION_PREFIX):
            return fail_sdk("renode.manifest-schema", message, ExitCode.VALIDATION_FAILURE)
        return fail_sdk("renode.manifest-invalid", message)

    try:
        sku = select_sku(manifest, board_arg)
    except RenodeError as err:
        return fail_sdk("renode.sku-unresolved", err.message)
    known["sku"] = sku

    try:
        elf = zephyr_elf_from_manifest(manifest, build_root, core_arg)
    except RenodeError as err:
        return fail_sdk("renode.slice", err.message)
    if not os.path.isfile(elf):
        # Deliberately BEFORE `known["elf"]` is set: the oracle's own
        # `report.elf` assignment sits after this check too, so an
        # `elf-missing` failure reports `data.elf: ""`, not the unbuilt path
        # -- verified against the shipped binary.
        return fail_sdk(
            "renode.elf-missing",
            f"Zephyr ELF not found at {elf}; run `tan build --project {app_path}` first.",
        )
    known["elf"] = elf

    try:
        repl, resc = platform_files_for_sku(sku, sdk_root)
    except RenodeError as err:
        return fail_sdk("renode.descriptor", err.message)
    for descriptor in (repl, resc):
        if not os.path.isfile(descriptor):
            # Same ordering rule as `elf` above: `platformStem`/`repl`/`resc`
            # stay empty in `data` on this failure.
            return fail_sdk(
                "renode.descriptor-missing", f"missing Renode descriptor {descriptor}."
            )
    known["platformStem"] = platform_stem_for_sku(sku)
    known["repl"] = repl
    known["resc"] = resc

    renode_bin = on_path("renode")
    if renode_bin is None:
        return fail_sdk(
            "renode.binary-missing",
            "`renode` binary not found on PATH. Install Renode (https://renode.io). "
            "tan renode does not silently pass when Renode is missing.",
        )

    issues: list[Issue] = []
    text: list[str] = []
    pin_issue = project_pin_issue(sdk_broken_pin, sdk_tier or "none")
    if pin_issue is not None:
        issues.append(pin_issue)
    if image_bundle_arg is not None:
        msg = (
            f"renode: --image-bundle {image_bundle_arg} accepted but unused by the "
            "single-slice smoke."
        )
        issues.append(_issue("renode.image-bundle-unused", "info", msg))
        if not json_mode:
            text.append(msg)

    vtor = _best_effort_vtor(elf)
    argv = build_renode_argv(renode_bin, repl, resc, elf, vtor)
    # Set BEFORE `run_renode` is called (mirrors the oracle's
    # `report.renode_argv = argv.clone()` at crates/tan-cli/src/commands/
    # renode/mod.rs:271, also before its own `run_renode` call): a spawn
    # failure below must still report the exact command line that could not
    # be started, not an empty `renodeArgv`.
    known["renodeArgv"] = argv

    if not json_mode:
        text.append(
            f"renode: booting {elf} on {os.path.basename(repl)} (log -> {log_path})"
        )

    try:
        found, natural_exit, argv_rejected, cpu_halted = run_renode(
            argv, log_path, float(timeout), expect, not json_mode
        )
    except OSError as err:
        return fail_sdk("renode.run-failed", f"failed to run renode: {err}")

    exit_code = ExitCode.SUCCESS
    if argv_rejected:
        exit_code = ExitCode.RUNTIME_FAILURE
        msg = (
            "renode: rejected the command line and printed its usage instead of booting "
            f"— nothing was simulated (see {log_path})."
        )
        issues.append(_issue("renode.argv-rejected", "error", msg))
        if not json_mode:
            text.append(msg)
    elif cpu_halted:
        exit_code = ExitCode.RUNTIME_FAILURE
        msg = (
            "renode: the CPU halted on its first instruction fetch — no firmware code "
            f"ever ran, even though the process exited cleanly (see {log_path})."
        )
        issues.append(_issue("renode.cpu-halted", "error", msg))
        if not json_mode:
            text.append(msg)
    elif expect is not None:
        if not found:
            exit_code = ExitCode.RUNTIME_FAILURE
            # `{expect:?}` in Rust -- `Debug` for `&str`, which is double-quoted,
            # not Python `repr()`'s single quotes.
            msg = (
                f"renode: console did not contain {_rust_debug_str(expect)} within "
                f"{timeout}s (see {log_path})."
            )
            issues.append(_issue("renode.expect-not-found", "error", msg))
            if not json_mode:
                text.append(msg)
    elif natural_exit is not None and natural_exit != 0:
        exit_code = ExitCode.RUNTIME_FAILURE
        msg = (
            f"renode: exited early ({_exit_status_desc(natural_exit)}) before the "
            f"{timeout}s timeout (see {log_path})."
        )
        issues.append(_issue("renode.exited-nonzero", "error", msg))
        if not json_mode:
            text.append(msg)

    return (
        exit_code,
        data(expectFound=found),
        issues,
        text,
        sdk,
        project,
    )


def renode(
    app_path: str = typer.Argument(
        ".",
        metavar="APP_PATH",
        help="Application source directory (default: `.`). Used to derive the default "
        "build root (`<app_path>/build`) and the Envelope project context.",
    ),
    build_root: str = typer.Option(
        None,
        "--build-root",
        metavar="DIR",
        help="Override the build root holding `system-manifest.yaml` "
        "(default: `<app_path>/build`).",
    ),
    board: str = typer.Option(
        None,
        "--board",
        metavar="SKU",
        help="Override the SoM SKU used to pick the Renode platform descriptor "
        "(default: `hw_info.sku` from the manifest).",
    ),
    core: str = typer.Option(
        None,
        "--core",
        metavar="CORE_ID",
        help="Boot the Zephyr slice with this core_id. Needed when the manifest carries "
        "more than one Zephyr slice; optional when the project has exactly one.",
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(  # noqa: ARG001 -- accepted for GlobalArgs parity, unused below
        None,
        "--board-yaml",
        metavar="PATH",
        help="Explicit board.yaml path. Accepted for parity with every other command's "
        "global flag; `tan renode` itself derives `project.boardYaml` from "
        "`<APP_PATH>/board.yaml`'s own existence, matching the oracle.",
    ),
    image_bundle: str = typer.Option(
        None,
        "--image-bundle",
        metavar="DIR",
        help="Directory of pre-built per-slice artefacts. Accepted for parity with the "
        "dual-OS flow; unused by the single-Zephyr-slice smoke.",
    ),
    log: str = typer.Option(
        None,
        "--log",
        metavar="FILE",
        help="Tee the Renode UART/console output to this file "
        "(default: `<build_root>/renode.log`).",
    ),
    timeout: int = typer.Option(
        120,
        "--timeout",
        min=0,
        metavar="SECS",
        help="Wall-clock cap for the Renode run, in seconds.",
    ),
    expect: str = typer.Option(
        None,
        "--expect",
        metavar="STR",
        help="If set, stop early (exit 0) when this substring appears in any console "
        "line; exit 1 if the run ends without it.",
    ),
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """Boot the built system manifest's single Zephyr slice in headless Renode as a
    no-hardware smoke test (native)."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    try:
        cwd = os.getcwd()
    except OSError:
        cwd = "."

    sdk: SdkInfo | None = None
    project_obj = Project(root=None, board_yaml=None)
    try:
        exit_code, data, issues, text_lines, sdk, project_obj = _run(
            app_path_arg=app_path,
            build_root_arg=build_root,
            board_arg=board,
            core_arg=core,
            sdk_root_arg=sdk_root,
            project_arg=project,
            image_bundle_arg=image_bundle,
            log_arg=log,
            timeout=timeout,
            expect=expect,
            json_mode=json_mode,
            cwd=cwd,
        )
    except Exception as err:  # noqa: BLE001 -- the whole point of this guard
        # Anything reaching here is a tan bug, reported as one with an
        # envelope: a raw traceback means an empty stdout and an extension
        # that renders nothing, with no error visible on either side.
        exit_code = ExitCode.INTERNAL_FAILURE
        data = _data()
        issues = [Issue("renode.internal-failure", "error", f"{type(err).__name__}: {err}")]
        text_lines = ["renode: internal failure"]

    if json_mode:
        emit(Envelope("renode", project_obj, data, issues, exit_code, sdk=sdk))
    else:
        for line in text_lines:
            print(line, file=sys.stderr)
    raise typer.Exit(int(exit_code))
