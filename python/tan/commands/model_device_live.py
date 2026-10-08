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
from dataclasses import dataclass
from pathlib import Path

from tan.commands import flash_cmd
from tan.commands.build_output import ProjectContext
from tan.core.jlink_probe import is_valid_usb_path
from tan.core.link_refusal import RAM_RUN_ONLY_METHOD
from tan.core.flash_plan import ManifestError, parse_system_manifest
from tan.envelope import Issue

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

    def any_set(self) -> bool:
        return self != LiveOptions(sdk_root=self.sdk_root)


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


def live_console(context: ProjectContext, label: str | None, live: LiveOptions) -> str | Issue:
    """RAM-run the project and return its RAM-console text, or the refusal."""
    refused = _preflight(context, live)
    if refused is not None:
        return refused
    code, data, issues, _lines, _sdk = flash_cmd._run(
        app_path=context.workspace_root,
        build_root_arg=None,
        sdk_root_arg=live.sdk_root,
        board_yaml=None,
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
    if entry.get("status") == "planned":
        return Issue(
            "model.device-confirm-required", "error",
            "A live device run RAM-loads the image and resets the whole device (including the Secure "
            "Enclave); pass --confirm (or set ALP_FLASH_FORCE=1). Nothing was run.",
        )
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        return errors[0]
    if int(code) != 0 or entry.get("status") == "failed":
        return Issue(
            "model.device-flash-failed", "error",
            f"The Flow C RAM-run failed: {entry.get('message') or 'no detail reported'}",
        )
    console = entry.get("ramConsole") or {}
    text = console.get("text")
    if not isinstance(text, str) or not text.strip():
        which = console.get("selected")
        why = (
            "the build selected the UART console, which a RAM-run cannot read"
            if which == "uart" else f"nothing was printed in the {live.wait:g}s wait"
        )
        return Issue(
            "model.device-console-empty", "error",
            f"The RAM console is empty ({why}); raise --wait or check the benchmark app.",
        )
    return text
