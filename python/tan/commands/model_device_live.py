# SPDX-License-Identifier: Apache-2.0
"""The live tier of `tan model run --device` / `ab --device` (tan-cli#1287).

`live_console` RAM-runs an already-built `diagnostics.link: itcm` project through
AEN Flow C -- the same `flash_cmd._run(ram=True, ram_console=True)` that
`tan flash --ram --ram-console` calls, in-process, never a subprocess of tan --
and returns the `ram_console_buf` text for `model_device_cmd` to hand to the
capture parser. The benchmark app (alp-sdk `examples/aen/aen-inference-latency`
or `aen-inference-energy`) must have the model baked in: tan does not build,
convert or embed it here.

`--ram` is the only write path used, so MRAM is never touched. Every flash
refusal is passed through with its own `flash.*` code; the refusals this module
adds are plain `Issue(...)` returns so the registry gate sees the codes.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from tan.commands import flash_cmd
from tan.commands.build_output import ProjectContext
from tan.core.jlink_probe import is_valid_usb_path
from tan.core.link_refusal import RAM_RUN_ONLY_METHOD
from tan.core.flash_plan import ManifestError, confirm_gate_note, parse_system_manifest
from tan.envelope import Issue
from tan.exit_codes import ExitCode

DEFAULT_CORE = "m55_he"
DEFAULT_WAIT_S = 1.5


@dataclass(frozen=True)
class LiveOptions:
    """The flags a live device run forwards to Flow C."""

    core: str = DEFAULT_CORE
    wait: float = DEFAULT_WAIT_S
    confirm: bool = False
    probe_serial: str | None = None
    probe_usb_path: str | None = None
    jlink: str | None = None
    sdk_root: str | None = None
    against_project: str | None = None
    #: The user's own `--board-yaml`, forwarded to Flow C; `None` when left at the default.
    board_yaml: str | None = None

    def any_set(self) -> bool:
        return self != LiveOptions(sdk_root=self.sdk_root, board_yaml=self.board_yaml)


@dataclass(frozen=True)
class LiveRun:
    """A successful RAM-run: the console, EVERY `flash.*` issue Flow C raised
    (warnings such as `flash.probe-unverified` included -- they qualify the
    measurement), and the `flash` provenance block for the result row."""

    text: str
    issues: list[Issue]
    flash: dict | None = None
    #: Where the raw console was saved (`<build>/flash-logs/model-console-<ts>.txt`), if it was.
    console_path: str | None = None


@dataclass(frozen=True)
class LiveRefusal:
    """A refusal: the issues to report (Flow C's own, verbatim and in order) and
    the exit code -- Flow C's `ExitCode` is kept, a model-side refusal is a
    validation failure."""

    issues: list[Issue]
    exit_code: ExitCode = ExitCode.VALIDATION_FAILURE
    #: The `flash` provenance block when the image WAS RAM-loaded and reset before
    #: the refusal (tan-cli#1417); `None` when nothing reached the board.
    flash: dict | None = None


def _refuse(issue: Issue, *before: Issue) -> LiveRefusal:
    return LiveRefusal([*before, issue])


def _preflight(context: ProjectContext, live: LiveOptions) -> Issue | None:
    """The project must be built for RAM-run: board.yaml, a manifest, and a
    `ram_run_only` (ITCM-linked) slice for the chosen core."""
    if not Path(context.board_yaml).is_file():
        return Issue(
            "model.device-no-project", "error",
            f"No project at {context.workspace_root} (board.yaml not found at {context.board_yaml}); "
            "`--device` without --capture needs a built diagnostics.link: itcm project (--project).",
        )
    if live.probe_usb_path is not None and not is_valid_usb_path(live.probe_usb_path):
        return Issue(
            "model.device-probe-invalid", "error",
            f"{live.probe_usb_path!r} is not a USB port path like 3-4.2 (<bus>-<port>[.<port>...]).",
        )
    manifest_path = Path(context.workspace_root) / "build" / "system-manifest.yaml"
    try:
        manifest = parse_system_manifest(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ManifestError) as err:
        return Issue(
            "model.device-no-manifest", "error",
            f"No usable system-manifest.yaml at {manifest_path} ({err}); run `tan build --project "
            f"{context.workspace_root}` for the benchmark app first.",
        )
    if not any(s.core_id == live.core and s.flash_method == RAM_RUN_ONLY_METHOD for s in manifest.slices):
        return Issue(
            "model.device-no-itcm-slice", "error",
            f"The manifest has no RAM-run-only slice for core {live.core}: the benchmark app must be "
            "built with board.yaml `diagnostics.link: itcm` (an ITCM-linked image, the only kind "
            "`tan flash --ram` runs).",
        )
    return None


def _provenance(entry: dict, live: LiveOptions, console_path: str | None = None) -> dict:
    jlink = entry.get("jlink") or {}
    block = {
        "core": live.core,
        "wait": live.wait,
        "transcriptPath": jlink.get("transcriptPath"),
        "attachedCore": jlink.get("attachedCore"),
    }
    if jlink.get("dpidr"):
        block["dpidr"] = jlink["dpidr"]
    if console_path:
        block["consolePath"] = console_path
    return block


def _save_console(
    context: ProjectContext, text: object, build_root: object = None
) -> tuple[str | None, list[Issue]]:
    """Write the raw RAM console next to the Flow C transcript
    (`<build>/flash-logs/model-console-<UTC ts>.txt`, never overwritten) so
    `latencyMs` can be checked against the app's own `LATENCY-RESULT`. A write
    failure is a warning: the measurement is still valid. `build_root` is the one
    Flow C itself used (so the file sits beside its transcript, nested layouts
    included); a blank console is not saved."""
    if not isinstance(text, str) or not text.strip():
        return None, []
    root = Path(build_root) if isinstance(build_root, str) and build_root else Path(context.workspace_root) / "build"
    logs = root / "flash-logs"
    base = time.strftime("model-console-%Y%m%dT%H%M%SZ", time.gmtime())
    try:
        logs.mkdir(parents=True, exist_ok=True)
        n = 0
        while True:
            path = logs / (f"{base}.txt" if n == 0 else f"{base}-{n}.txt")
            try:
                with open(path, "x", encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
                return str(path), []
            except FileExistsError:
                n += 1
    except OSError as err:
        return None, [Issue(
            "model.device-console-unsaved", "warning",
            f"The raw RAM console could not be saved under {logs}: {err}",
        )]


def live_console(context: ProjectContext, label: str | None, live: LiveOptions) -> LiveRun | LiveRefusal:
    """RAM-run the project and return its console plus Flow C's issues, or the refusal."""
    refused = _preflight(context, live)
    if refused is not None:
        return _refuse(refused)
    code, data, issues, _lines, _sdk = flash_cmd._run(
        app_path=context.workspace_root,
        build_root_arg=None,
        sdk_root_arg=live.sdk_root,
        board_yaml=live.board_yaml,
        core=live.core,
        helper=None,
        dry_run=False,
        skip_missing_tools=False,
        capture=True,
        cwd=context.workspace_root,
        confirm_flag=live.confirm,
        probe_serial=live.probe_serial,
        probe_usb_path=live.probe_usb_path,
        jlink_path=live.jlink,
        ram=True,
        ram_console=True,
        ram_wait=live.wait,
    )
    entries = data.get("entries") or []
    entry = entries[0] if entries else {}
    flash_issues = list(issues)
    if entry.get("status") == "planned":
        # First, so a consumer reading issues[0] sees the actionable refusal; Flow C's
        # own `flash.nothing-flashed` / `flash.confirm-required` follow it.
        return LiveRefusal([Issue(
            "model.device-confirm-required", "error",
            "A live device run RAM-loads the image and resets the whole device (including the Secure "
            "Enclave); nothing was run: "
            f"{confirm_gate_note('--confirm was not given', 'run on the device')}.",
        ), *flash_issues])
    failed = int(code) != 0 or entry.get("status") == "failed"
    if failed:
        if not any(i.severity == "error" for i in flash_issues):
            flash_issues.append(Issue(
                "model.device-flash-failed", "error",
                f"The Flow C RAM-run failed: {entry.get('message') or 'no detail reported'}",
            ))
        # tan-cli#1486: the registry documents `model.device-flash-failed` as
        # VALIDATION_FAILURE; a Flow C error issue passed through keeps Flow C's own code.
        synthesised = not any(i.severity == "error" for i in issues)
        if synthesised:
            exit_code = ExitCode.VALIDATION_FAILURE
        else:
            exit_code = ExitCode(int(code)) if int(code) else ExitCode.RUNTIME_FAILURE
        # tan-cli#1497: a failure AFTER the image was loaded and the device reset
        # (Flow C's `ram.loaded`) still happened to the board: keep the provenance.
        loaded = bool((entry.get("ram") or {}).get("loaded"))
        return LiveRefusal(flash_issues, exit_code,
                           flash=_provenance(entry, live) if loaded else None)
    console = entry.get("ramConsole") or {}
    text = console.get("text")
    console_path, save_issues = _save_console(context, text, data.get("buildRoot"))
    flash = _provenance(entry, live, console_path)
    if not isinstance(text, str) or not text.strip():
        which = console.get("selected")
        why = (
            "the build selected the UART console, which a RAM-run cannot read"
            if which == "uart" else f"nothing was printed in the {live.wait:g}s wait"
        )
        return LiveRefusal([*flash_issues, Issue(
            "model.device-console-empty", "error",
            f"The RAM console is empty ({why}); raise --wait or check the benchmark app.",
        )], flash=flash)
    return LiveRun(text, [*flash_issues, *save_issues], flash, console_path)
