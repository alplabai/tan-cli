# SPDX-License-Identifier: Apache-2.0
"""Debug launch-config generation: the target/server vocabulary, the launch
draft per target class, the resolution overlay, and the ``launch.json`` merge
plan. Port of ``crates/tan-core/src/debug{,_launch}.rs``.

Two things carry over from the Rust as invariants rather than incidentals:

* **Key order is contract.** The emitted configuration must match the TS CLI's
  key order, which is why the Rust needs serde_json's ``preserve_order``
  feature and calls ``shift_remove`` (never the order-scrambling swap-``remove``)
  when it drops a key. A Python ``dict`` is insertion-ordered and ``del``
  removes in place, so the trap does not exist here -- but the reason a key is
  BUILT and then deleted, rather than conditionally inserted, is exactly this:
  ``preLaunchTask`` and the two svd keys sit in the MIDDLE of the order.
* **The OS/target class is derived, never selected.** ``--target-kind`` names a
  debug target CLASS (zephyr-mcu / baremetal-mcu / yocto-userspace /
  native-host); each class implies its adapter, its artefact shape and its
  server set. Nothing here knows a SKU, an address, a pin name or a vendor --
  those live in alp-sdk ``metadata/``. There is no ``--os`` and no
  ``--backend``.

Target/server kinds are plain wire strings, not an enum: the Rust needs
``DebugTargetKind`` + ``as_str()`` + a serde rename to get "kebab-case on the
wire from a typed value in the code", and every one of those three renders the
same literal. Here the literal IS the value, so the envelope cannot disagree
with the parser about how a kind is spelled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from tan.core import jsonc_splice
from tan.core.run import is_native_sim_board
from tan.core.system_manifest import SYSTEM_MANIFEST_SCHEMA_VERSION

ZEPHYR_MCU = "zephyr-mcu"
BAREMETAL_MCU = "baremetal-mcu"
YOCTO_USERSPACE = "yocto-userspace"
NATIVE_HOST = "native-host"

JLINK = "jlink"
OPENOCD = "openocd"
PYOCD = "pyocd"
GDBSERVER = "gdbserver"
SERVER_NONE = "none"

#: Allowed `--target-kind` values, in the order the error message lists them.
TARGET_KINDS = (ZEPHYR_MCU, BAREMETAL_MCU, YOCTO_USERSPACE, NATIVE_HOST)
#: Allowed `--server` values, in the order the error message lists them.
SERVER_KINDS = (JLINK, OPENOCD, PYOCD, GDBSERVER, SERVER_NONE)

_SERVER_LABELS = {
    JLINK: "J-Link",
    OPENOCD: "OpenOCD",
    PYOCD: "pyOCD",
    GDBSERVER: "gdbserver",
    SERVER_NONE: "local",
}

#: The debug servers valid for each target kind.
_SERVER_CHOICES = {
    ZEPHYR_MCU: (JLINK, OPENOCD, PYOCD),
    BAREMETAL_MCU: (JLINK, OPENOCD, PYOCD),
    YOCTO_USERSPACE: (GDBSERVER,),
    NATIVE_HOST: (SERVER_NONE,),
}


class DebugConfigError(Exception):
    """A refusal whose ``str()`` is the user-facing message, verbatim from the
    Rust. Raised instead of returning ``Result<_, String>``; every caller turns
    it into a coded envelope, never a traceback."""


def parse_target_kind(raw: str | None) -> str:
    """``--target-kind``: an absent/empty value defaults to ``native-host`` (TS
    ``parseTargetKind``); an unknown value is an error."""
    value = raw or ""
    if value == "":
        return NATIVE_HOST
    if value in TARGET_KINDS:
        return value
    raise DebugConfigError(
        f"Unsupported --target-kind '{value}'. Allowed values: "
        f"{', '.join(TARGET_KINDS)}."
    )


def parse_server_kind(raw: str | None) -> str:
    """``--server``: absent/empty defaults to ``none``; unknown is an error."""
    value = raw or ""
    if value == "":
        return SERVER_NONE
    if value in SERVER_KINDS:
        return value
    raise DebugConfigError(
        f"Unsupported --server '{value}'. Allowed values: {', '.join(SERVER_KINDS)}."
    )


def server_choices_for_target(target: str) -> tuple[str, ...]:
    return _SERVER_CHOICES.get(target, ())


def is_server_supported_for_target(target: str, server: str) -> bool:
    return server in server_choices_for_target(target)


#: The ``build/system-manifest.yaml`` slice ``os`` a debug target class runs
#: on, or absent for a target with no per-core build slice keyed by ``os``.
#: ``native-host`` is exactly that case -- its slice is picked by BOARD target
#: instead (:func:`tan.core.run.is_native_sim_board`), never by this map.
MANIFEST_OS_BY_TARGET = {
    ZEPHYR_MCU: "zephyr",
    BAREMETAL_MCU: "baremetal",
    YOCTO_USERSPACE: "yocto",
}


def manifest_slices(manifest: Any) -> list[dict[str, Any]]:
    """``build/system-manifest.yaml``'s slices, or ``[]`` when the document is
    not a v1 manifest. A TOLERANT reader, deliberately distinct from
    :func:`tan.core.system_manifest.parse_system_manifest`'s strict one:
    every caller here treats the file as a best-effort enrichment (pre-build,
    or against a reshaped manifest, nothing resolves rather than the command
    failing), so a structurally wrong document degrades to "no slices" rather
    than raising.

    The schema-major guard mirrors the strict reader, which REFUSES a
    manifest whose ``schema_version`` is not 1 rather than reading it as if it
    were. A slice missing ``core_id`` or ``os`` also disqualifies the whole
    document: both fields are non-``Option`` in the Rust struct, so serde
    fails the entire parse there, and a partial read here would resolve
    against a manifest the oracle rejects.
    """
    if not isinstance(manifest, dict):
        return []
    if manifest.get("schema_version") != SYSTEM_MANIFEST_SCHEMA_VERSION:
        return []
    raw = manifest.get("slices")
    if not isinstance(raw, list):
        return []
    slices = []
    for entry in raw:
        if not isinstance(entry, dict):
            return []
        if not isinstance(entry.get("core_id"), str) or not isinstance(entry.get("os"), str):
            return []
        slices.append(entry)
    return slices


def _core_not_in_manifest_message(core: str) -> str:
    return (
        f"--target-kind was not given, and --core {core} does not match any "
        "slice in this project's build/system-manifest.yaml; pass "
        "--target-kind explicitly, or a --core value this project's build "
        "actually produced."
    )


def _ambiguous_target_classes_message(targets: set[str]) -> str:
    # The mapped --target-kind SPELLINGS (`zephyr-mcu`), never the raw
    # manifest `os` value (`zephyr`) -- pasting the bare `os` value into
    # --target-kind fails with "Unsupported --target-kind 'zephyr'".
    classes = ", ".join(sorted(targets))
    return (
        "--target-kind was not given, and this project's "
        f"build/system-manifest.yaml names more than one target class ({classes}); "
        "pass --target-kind (and --core, on a mixed-core board) to say which "
        "one to debug."
    )


def _no_debuggable_class_message(oses: set[Any]) -> str:
    # Distinct from `_ambiguous_target_classes_message`: a single unrecognised
    # `os` (e.g. a `linux` slice) is not "more than one" of anything.
    unmapped = ", ".join(sorted(oses))
    return (
        "--target-kind was not given, and this project's "
        f"build/system-manifest.yaml names no debuggable target class for os: "
        f"{unmapped}; pass --target-kind explicitly."
    )


def _pre_build_hardware_message(som_sku: str) -> str:
    return (
        f"--target-kind was not given, and board.yaml declares som.sku: {som_sku} (a "
        "real hardware project), but no build/system-manifest.yaml exists yet to say "
        "which target class it builds. Run `tan build` first, or pass --target-kind "
        "explicitly."
    )


def infer_target_kind(
    manifest: Any, core: str | None, som_sku: str | None
) -> tuple[str | None, str | None]:
    """tan-cli#456: the default ``--target-kind`` when the flag is omitted, so
    a hardware project never silently gets a native-host draft its build
    cannot produce. Shared, not duplicated per command: ``debug_config_cmd``
    and (once routed through this rather than its own bare
    ``parse_target_kind(None)``) ``support_bundle_cmd`` face the identical
    question. Pure -- ``manifest``/``som_sku`` are already read; no file IO.

    Returns ``(target, None)`` once confidently derived; ``(None, None)`` with
    no signal at all (native-host survives, the pre-#456 default); ``(None,
    reason)`` on more than one class, none at all, or a ``--core`` matching
    nothing -- refuse rather than guess.

    ``--core`` filters the WHOLE slice list first, before native_sim is
    excluded -- never only the hardware slices: that was the review-round bug,
    where ``--core <the native_sim slice's own core id>`` on a mixed manifest
    silently fell back to the OTHER core's class and wrote a J-Link session
    pointed at the native_sim binary. Hardware also outvotes a CO-BUILT
    native_sim slice whenever ``--core`` is absent -- the #83
    ``_select_slice`` NATIVE_HOST arm needs an explicit ``--target-kind
    native-host`` (or ``--core`` naming the native_sim slice) to reach.
    """
    all_slices = manifest_slices(manifest)
    if core is not None and all_slices:
        matched = [s for s in all_slices if s.get("core_id") == core]
        if not matched:
            return None, _core_not_in_manifest_message(core)
        all_slices = matched

    hw_slices = [s for s in all_slices if not is_native_sim_board(s.get("board"))]
    if hw_slices:
        by_os = {v: k for k, v in MANIFEST_OS_BY_TARGET.items()}
        oses = {s.get("os") for s in hw_slices}
        targets = {by_os[o] for o in oses if o in by_os}
        if len(targets) == 1:
            return next(iter(targets)), None
        if targets:
            return None, _ambiguous_target_classes_message(targets)
        return None, _no_debuggable_class_message(oses)
    if all_slices:
        return NATIVE_HOST, None  # every (matching) slice built is native_sim -- a real default.

    if isinstance(som_sku, str) and som_sku != "":
        return None, _pre_build_hardware_message(som_sku)
    return None, None


#: The v0.3.1 default ``preLaunchTask``, restored per tan-cli#138. Keyed by
#: TARGET ALONE, never by server: ``crates/tan-core/src/debug_launch.rs``
#: hardcoded the identical literal in every one of ``ZephyrMcu``'s and
#: ``BaremetalMcu``'s server-branched arms before tan-cli#85 made the key
#: opt-in, so J-Link/OpenOCD/pyOCD all shared one string per target, not one
#: each. ``--pre-launch-task`` overrides a target's default; an EXPLICIT empty
#: string opts out of a ``preLaunchTask`` key entirely -- see
#: :func:`create_launch_draft`.
#:
#: **``YOCTO_USERSPACE`` is deliberately absent** -- three of the four targets
#: get a default, not four. v0.3.1 did hardcode
#: ``"alp: deploy and start gdbserver"`` for it, but restoring THAT one would
#: re-break what alp-sdk-vscode#406 deliberately fixed. That repo's
#: ``preLaunchTaskFor`` (``src/tasks/service.ts``) maps only the three build
#: kinds, and states why verbatim:
#:
#:     yocto-userspace deliberately gets NOTHING. The only task registered for
#:     it is the "deploy and start gdbserver" placeholder, which exits 1 by
#:     design (``vscodeAdapter.ts``) because the extension cannot deploy or
#:     start a remote gdbserver. Naming it would put VS Code's "the
#:     preLaunchTask terminated with exit code 1 -- Debug Anyway / Show
#:     Errors" dialog in front of EVERY F5, including one where the customer
#:     has already copied the binary across, started gdbserver by hand and
#:     filled in ``miDebuggerServerAddress`` -- the setup that works.
#:
#: So tan-cli#138 and tan-cli#321 pull opposite ways here, and only three of
#: the four labels are safe to restore. For the build kinds the extension DOES
#: register a working task, and omitting the key is precisely what left its
#: provider contribution dead. For yocto-userspace the only task that exists
#: fails by design, so naming it would degrade the one workflow that currently
#: succeeds. A user with their own deploy task still passes
#: ``--pre-launch-task`` explicitly.
DEFAULT_PRE_LAUNCH_TASK: dict[str, str] = {
    ZEPHYR_MCU: "alp: build active target",
    BAREMETAL_MCU: "alp: build baremetal target",
    NATIVE_HOST: "alp: build native_sim target",
}


def create_launch_draft(
    target: str, server: str, pre_launch_task: str | None
) -> dict[str, Any]:
    """The VS Code launch configuration draft for a target/server (TS
    ``createDebugProfile`` -> ``debugProfileToLaunchDraft``).

    ``pre_launch_task`` has three states, not two (tan-cli#138):

    * ``None`` -- the flag was not passed. Takes this target's restored
      v0.3.1 default from :data:`DEFAULT_PRE_LAUNCH_TASK`. Every draft used to
      carry this hardcoded string unconditionally; tan-cli#85 made the key
      opt-in because **nothing in any of the three repos defined the task
      it named** -- no ``tasks.json``, no ``TaskProvider`` registration --
      and VS Code resolves ``preLaunchTask`` before launching, fails to find
      the task, and aborts pre-launch, so the session never started: a
      launch.json that reads perfectly and cannot run. alp-sdk-vscode has
      since registered the THREE build labels as real, working tasks, so the
      default is restored for those three targets. The fourth label exists
      only as a placeholder that exits 1 by design, and
      ``preLaunchTaskFor`` maps three of four kinds for that reason -- see
      :data:`DEFAULT_PRE_LAUNCH_TASK` for the full quotation. A target with
      no entry there behaves exactly as it did before this restoration.
    * ``""`` (an explicitly empty string) -- opts OUT of a ``preLaunchTask``
      key entirely, even though a default now exists for this target. The
      only way left to reach the trailing ``del`` below.
    * Anything else -- emitted verbatim, overriding the default. Unchanged
      from before this restoration.
    """
    if not is_server_supported_for_target(target, server):
        raise DebugConfigError(
            f"Unsupported debug backend '{server}' for target '{target}'."
        )
    label = _SERVER_LABELS[server]

    if pre_launch_task is None:
        pre_launch_task = DEFAULT_PRE_LAUNCH_TASK.get(target)
    elif pre_launch_task == "":
        pre_launch_task = None

    if target == ZEPHYR_MCU:
        name = f"Alp: Zephyr Debug ({label})"
        common = {
            "name": name,
            "type": "cortex-debug",
            "request": "launch",
            "cwd": "${workspaceFolder}",
            "executable": "${workspaceFolder}/build/app/zephyr/zephyr.elf",
            "runToEntryPoint": "main",
            "preLaunchTask": pre_launch_task,
            "svdFile": "<resolved-svd>",
            "svdPath": "<resolved-svd>",
        }
        if server == OPENOCD:
            draft = {
                **common,
                "servertype": "openocd",
                "configFiles": ["<resolved-openocd-board-cfg>"],
            }
        elif server == PYOCD:
            draft = {**common, "servertype": "pyocd", "targetId": "<resolved-target-id>"}
        else:
            draft = {
                **common,
                "servertype": "jlink",
                "device": "<resolved-device>",
                "interface": "swd",
            }
    elif target == BAREMETAL_MCU:
        name = f"Alp: Baremetal Debug ({label})"
        # tan-cli#139: this used to be ONE un-branched object regardless of
        # `server`, so OpenOCD and pyOCD got `device`/`interface` (a J-Link-only
        # pair `apply_launch_resolution` never fills for them) and neither got
        # the `configFiles`/`targetId` key its own resolution computes --
        # `apply_launch_resolution` only replaces a key the draft already
        # carries, so OpenOCD shipped a resolved `serverpath`/`searchDir` with NO
        # `configFiles` to load, and pyOCD had no target to select.
        head = {
            "name": name,
            "type": "cortex-debug",
            "request": "launch",
            "servertype": server,
            "cwd": "${workspaceFolder}",
            "executable": "${workspaceFolder}/build/baremetal/app.elf",
        }
        if server == OPENOCD:
            draft = {
                **head,
                "preLaunchTask": pre_launch_task,
                "svdFile": "<resolved-svd>",
                "svdPath": "<resolved-svd>",
                "configFiles": ["<resolved-openocd-board-cfg>"],
            }
        elif server == PYOCD:
            draft = {
                **head,
                "preLaunchTask": pre_launch_task,
                "svdFile": "<resolved-svd>",
                "svdPath": "<resolved-svd>",
                "targetId": "<resolved-target-id>",
            }
        else:
            draft = {
                **head,
                "device": "<resolved-device>",
                "interface": "swd",
                "preLaunchTask": pre_launch_task,
                "svdFile": "<resolved-svd>",
                "svdPath": "<resolved-svd>",
            }
    elif target == YOCTO_USERSPACE:
        draft = {
            "name": "Alp: Yocto Remote Debug",
            "type": "cppdbg",
            "request": "launch",
            "program": "${workspaceFolder}/build/yocto/app",
            "cwd": "${workspaceFolder}",
            "MIMode": "gdb",
            "miDebuggerServerAddress": "<host>:<port>",
            "miDebuggerPath": "<resolved-gdb>",
            "setupCommands": [{"text": "-enable-pretty-printing"}],
            "preLaunchTask": pre_launch_task,
        }
    else:
        draft = {
            "name": "Alp: Native Sim Debug",
            # `lldb`, not `codelldb`, because CodeLLDB's own manifest says so:
            # `vadimcn.vscode-lldb` v1.12.2 declares
            # `contributes.debuggers[0].type = "lldb"`. `codelldb` is the
            # extension's marketplace NAME; no extension registers it as a debug
            # type, so VS Code refused every session outright with `Configured
            # debug type 'codelldb' is not supported.` (#104). native_sim is the
            # only target reachable with no probe and no board -- the first
            # debugging experience a customer has.
            "type": "lldb",
            "request": "launch",
            "program": "${workspaceFolder}/build/native_sim/zephyr/zephyr.exe",
            "cwd": "${workspaceFolder}",
            "preLaunchTask": pre_launch_task,
        }

    # A literal `"preLaunchTask": null` is worse than no key at all (VS Code's
    # schema rejects it). Built-then-deleted, not conditionally inserted,
    # because the key sits mid-order and the order is contract.
    if draft.get("preLaunchTask") is None:
        del draft["preLaunchTask"]
    return draft


@dataclass
class LaunchResolution:
    """What a real build knows about itself, filled in over the draft's
    ``<resolved-...>`` placeholders (#66). Every field is optional: pre-build, or
    against a Zephyr that reshaped ``runners.yaml``, nothing resolves and the
    draft keeps the placeholder it has always had."""

    #: The slice's real ELF -- per-core, not the fixed `build/app/...` guess.
    executable: str | None = None
    #: J-Link device name (`args.jlink --device`).
    device: str | None = None
    #: The toolchain GDB the build was made against (`config.gdb`).
    gdb_path: str | None = None
    #: The OpenOCD binary the build resolved (`config.openocd`).
    server_path: str | None = None
    #: OpenOCD script search directories (`config.openocd_search`).
    search_dirs: list[str] = field(default_factory=list)
    #: OpenOCD config files (`args.openocd --config`, all of them).
    config_files: list[str] = field(default_factory=list)
    #: pyOCD target id (`args.pyocd --target`).
    target_id: str | None = None
    #: SVD path. Produced ONLY by `tan debug-config --svd` (tan-cli#197): the
    #: SDK ships no SVD file, and alp-sdk#948's vendor-redistribution licence
    #: question may mean it never does.
    svd: str | None = None
    #: `host:port` for a yocto-userspace draft's `miDebuggerServerAddress`.
    #: Produced ONLY by `tan debug-config --gdbserver-address` (tan-cli#321):
    #: this is a runtime property of the DEPLOYED board -- which host it is
    #: reachable at and which port its gdbserver is listening on -- which no
    #: build, and no SDK-published metadata, can ever resolve. `None` unless
    #: the caller passed one.
    gdbserver_address: str | None = None


def fill_debug_probe_identity_gaps(
    resolution: LaunchResolution,
    core_id: str | None,
    jlink_device: dict[str, str],
    pyocd_target: str | None,
    openocd_config: str | None,
) -> None:
    """Fill ``resolution``'s ``device``/``target_id``/``config_files`` gaps from
    the SDK's published per-variant debug-probe identity (``variants[].debug``,
    alp-sdk#987 / alp-sdk#1026) -- the same three fields a real build's
    ``runners.yaml`` resolves, sourced instead from the SoC-JSON metadata that
    exists whether or not the project has been built yet. Port of
    ``tan_core::debug_launch::fill_debug_probe_identity_gaps``.

    **Fill-the-gap only, never override**: each field is written ONLY when
    ``resolution`` does not already carry a value for it. A real build's own
    resolution (Zephyr's own ``runners.yaml``, generated for THIS board) is
    strictly more specific than the SDK's generic per-variant identity and
    always wins where both exist.

    **No fabrication**: ``jlink_device`` is keyed by core id, so ``device``
    resolves only when ``core_id`` is not ``None`` AND that exact key is
    present -- there is no "the only entry" or "the first entry" guess.
    ``target_id``/``config_files`` resolve only when the corresponding SDK key
    is itself present; an absent ``openocd_config`` leaves ``config_files``
    empty, which is the correct published "unknown", not a bug.
    """
    if resolution.device is None:
        resolution.device = jlink_device.get(core_id) if core_id is not None else None
    if resolution.target_id is None:
        resolution.target_id = pyocd_target
    if not resolution.config_files and openocd_config is not None:
        resolution.config_files = [openocd_config]


def apply_launch_resolution(draft: dict[str, Any], resolution: LaunchResolution) -> None:
    """Overwrite a draft's placeholders with what ``resolution`` knows, in place.

    Only keys the draft already carries are replaced, so a target that never had
    a ``device`` does not grow one; the two genuinely new keys (``gdbPath``, and
    ``serverpath``/``searchDir``) are inserted only when they resolved and only
    for the adapter that understands them.

    The ``svdFile``/``svdPath`` placeholders are REMOVED when no SVD resolved. A
    missing key costs the peripheral view; a path that does not exist makes
    cortex-debug fail on start, which is strictly worse than not offering the
    view (alp-sdk#948).
    """
    is_cortex = draft.get("type") == "cortex-debug"

    # `executable` (cortex-debug) / `program` (cppdbg, lldb) name the same thing
    # under different adapters; replace whichever this draft uses.
    if resolution.executable is not None:
        for key in ("executable", "program"):
            if key in draft:
                draft[key] = resolution.executable
    if resolution.device is not None and "device" in draft:
        draft["device"] = resolution.device
    if resolution.target_id is not None and "targetId" in draft:
        draft["targetId"] = resolution.target_id
    if resolution.config_files and "configFiles" in draft:
        draft["configFiles"] = list(resolution.config_files)
    if resolution.gdbserver_address is not None and "miDebuggerServerAddress" in draft:
        # tan-cli#321: the ONLY source of this field's resolution -- see
        # `LaunchResolution.gdbserver_address`'s own docstring for why nothing
        # else (build or SDK metadata) can ever fill it.
        draft["miDebuggerServerAddress"] = resolution.gdbserver_address
    if resolution.gdb_path is not None:
        # cppdbg spells it `miDebuggerPath` and already carries the key;
        # cortex-debug's `gdbPath` is additive.
        if "miDebuggerPath" in draft:
            draft["miDebuggerPath"] = resolution.gdb_path
        elif is_cortex:
            draft["gdbPath"] = resolution.gdb_path
    if is_cortex and draft.get("servertype") == OPENOCD:
        if resolution.server_path is not None:
            draft["serverpath"] = resolution.server_path
        if resolution.search_dirs:
            draft["searchDir"] = list(resolution.search_dirs)
    if resolution.svd is not None:
        for key in ("svdFile", "svdPath"):
            if key in draft:
                draft[key] = resolution.svd
    else:
        for key in ("svdFile", "svdPath"):
            if draft.get(key) == "<resolved-svd>":
                del draft[key]


def sdk_identity_overwrites(
    existing_content: str | None, draft: dict[str, Any], filled_fields: list[str]
) -> list[tuple[str, str, str]]:
    """Whether writing ``draft`` (already ``apply_launch_resolution``'d) over
    ``existing_content`` would REPLACE an already-concrete value on any of
    ``filled_fields`` -- the exact launch-configuration JSON keys a caller's
    own resolution just populated from a source that is not a real build. Port
    of ``tan_core::debug_launch::sdk_identity_overwrites``.

    alp-sdk#1026 review finding #1: [`_merge_value`]'s only protection against
    clobbering a customer's hand-filled value is that the INCOMING value is
    unresolved (a ``<...>`` placeholder) -- by design, since a value resolved
    from a real build is supposed to overwrite unconditionally (see
    [`_merge_configuration`]'s doc comment). The SDK's published debug-probe
    identity (alp-sdk#987) resolves a device/target id/config PRE-build, so
    its values are never placeholders either -- reaching that same
    unconditional-overwrite branch for a source with materially less
    confidence than an actual build, silently. This function does not change
    that overwrite (a customer's real, in-repo launch.json is still allowed to
    go stale the same way it always could against a real build), it only lets
    the caller SAY so, the same way ``LaunchJsonWritePlan.comments_dropped``
    discloses a lossy write instead of hiding it behind ``ok: true``.

    Returns ``(field, existing, incoming)`` for each field this write is about
    to change from a concrete existing value to a DIFFERENT concrete one;
    empty when nothing concrete would be lost (no matching entry, the existing
    field is itself unresolved/absent, or the two values already agree).
    Matches the SAME entry [`create_launch_json_write_plan`] would merge into
    (current name, else its legacy counterpart) so this can never flag a field
    on an unrelated configuration.
    """
    out: list[tuple[str, str, str]] = []
    if not filled_fields:
        return out
    try:
        name = _configuration_name(draft)
        document = _parse_launch_json_or_default(existing_content)
    except DebugConfigError:
        return out
    configs = document.get("configurations")
    if not isinstance(configs, list):
        return out
    existing_entry = next(
        (c for c in configs if isinstance(c, dict) and c.get("name") == name), None
    )
    if existing_entry is None:
        legacy = _legacy_name(name)
        if legacy is not None:
            existing_entry = next(
                (c for c in configs if isinstance(c, dict) and c.get("name") == legacy),
                None,
            )
    if existing_entry is None:
        return out
    for field in filled_fields:
        if field not in existing_entry or field not in draft:
            continue
        existing_val = existing_entry[field]
        incoming_val = draft[field]
        if _value_is_concrete(existing_val) and existing_val != incoming_val:
            out.append((field, _display_value(existing_val), _display_value(incoming_val)))
    return out


def _value_is_concrete(value: Any) -> bool:
    """Whether ``value`` is a real, non-placeholder value a customer could
    have hand-filled -- mirrors the exact condition [`_merge_value`] treats as
    worth protecting (a string that isn't a ``<...>`` token; an array that
    isn't empty and isn't entirely placeholders)."""
    if isinstance(value, str):
        return not is_unresolved_placeholder(value)
    if isinstance(value, list):
        return len(value) > 0 and not all(_is_unresolved(v) for v in value)
    return False


def _display_value(value: Any) -> str:
    """Render a JSON value for an issue message: a string unquoted, anything
    else (an array, for ``configFiles``) via its normal compact JSON
    rendering -- matching serde_json::Value's ``Display`` impl, which
    ``sdk_identity_overwrites`` mirrors."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def launch_preview_notes() -> list[str]:
    """The static advisory notes attached to a launch preview (TS
    ``createLaunchPreview``)."""
    return [
        "This is a draft launch configuration generated by tan.",
        "Placeholder fields such as <resolved-device> still need project-specific resolution.",
        "The long-term target is to resolve these values from the shared debug model.",
    ]


def launch_preview_document(draft: dict[str, Any]) -> dict[str, Any]:
    """The launch.json-shaped preview document: ``{version, configurations:[draft]}``."""
    return {"version": "0.2.0", "configurations": [draft]}


def is_unresolved_placeholder(value: str) -> bool:
    """Whether a string is one of OUR "nobody filled this in yet" markers.

    The two brace styles in a launch configuration mean opposite things, and
    that is the trap:

    * ``${...}`` is a VS Code **variable substitution** (``${workspaceFolder}``,
      ``${env:HOME}``). VS Code expands it itself at launch, so it is fully
      resolved as far as we are concerned. No angle bracket is involved.
    * ``<...>`` is ours -- ``<resolved-device>``, ``<resolved-svd>``,
      ``<resolved-openocd-board-cfg>``, ``<resolved-gdb>``,
      ``<resolved-target-id>``, and the two-token ``<host>:<port>``. Nothing
      expands these; handed to a debug adapter verbatim they are a literal
      device name / path / TCP address.

    So the test is any angle-bracket token, NOT a ``<resolved-`` prefix. The
    prefix test passed ``<host>:<port>`` as a real address -- the hole
    alp-sdk-vscode found, where a yocto profile reported launchable with an
    unusable gdbserver address.
    """
    # Equivalent to the TS `/<[^<>]*>/`: after splitting on `<`, no fragment can
    # contain another `<`, so a fragment holding a `>` IS a `<...>` token.
    return any(">" in rest for rest in value.split("<")[1:])


def _is_unresolved(value: Any) -> bool:
    return isinstance(value, str) and is_unresolved_placeholder(value)


def _is_resolved(value: Any) -> bool:
    return isinstance(value, str) and not is_unresolved_placeholder(value)


def _merge_value(existing: Any, next_value: Any) -> Any:
    """Merge one incoming value over what the file already holds.

    The whole rule: **an incoming unresolved ``<...>`` placeholder never
    overwrites a concrete existing value.** That is also what tells "the
    customer set this deliberately" apart from "this is our old output" -- our
    output for a field we could not resolve is *literally* an angle-bracket
    token, so anything concrete in the file is either the customer's or a real
    value we computed. The inverse still works: whenever this run CAN resolve a
    field, the incoming value is concrete and overwrites unconditionally, so a
    stale value that is now wrong is still updateable.

    Rust distinguishes "key absent" (``None``) from "key present holding JSON
    null" (``Some(Value::Null)``); here both arrive as ``None``, and both take
    the same branch in the Rust too -- ``is_resolved(Some(Null))`` is false and
    ``Value::Null`` is not an array -- so the distinction has nothing to decide.
    """
    if isinstance(next_value, list) and isinstance(existing, list):
        # cortex-debug `configFiles`: an all-placeholder incoming list keeps the
        # existing list WHOLE, or a hand-added second `.cfg` is lost to a
        # per-index merge against a one-element draft. A mixed list still merges
        # per element, so an entry we did resolve wins.
        if next_value and existing and all(_is_unresolved(v) for v in next_value):
            return list(existing)
        return [
            _merge_value(existing[i] if i < len(existing) else None, item)
            for i, item in enumerate(next_value)
        ]
    if _is_unresolved(next_value) and _is_resolved(existing):
        return existing
    return next_value


def _merge_configuration(existing: Any, next_value: Any) -> Any:
    """Merge the freshly generated configuration OVER the one already in the
    file (see [`_merge_value`]) instead of replacing it.

    This runs before every session and the configuration names are fixed per
    target/server, so a wholesale replace meant a customer told to hand-fill
    ``"device": "AE822F4M55_HP"`` got it reset to ``"<resolved-device>"`` on
    their next F5 -- data loss on their own file, no confirm, no backup, and an
    unexitable loop around the advice we had just given them (#105).

    Key order follows the existing entry with new keys appended, and keys the
    customer added that we never write (``serverArgs``, ...) are left untouched
    because only the draft's own keys are visited.
    """
    if not isinstance(existing, dict) or not isinstance(next_value, dict):
        return next_value
    merged = dict(existing)
    for key, value in next_value.items():
        merged[key] = _merge_value(existing.get(key), value)
    return merged


@dataclass
class LaunchJsonWritePlan:
    """Result of merging a draft into launch.json."""

    #: Pretty-printed launch.json content, trailing newline included.
    content: str
    #: `True` if an existing same-named configuration was replaced; `False` if appended.
    replaced: bool
    #: The legacy `"ALP: ..."` name of an entry ADOPTED onto the current
    #: `"Alp: ..."` name this run. `None` on every other path, including when a
    #: current-named entry already existed (that case never looks for a legacy
    #: counterpart at all).
    migrated_from: str | None
    #: `True` when this write discarded a comment (or trailing comma) that sat
    #: inside the span actually rewritten -- the one maintained entry the splice
    #: replaced, or, on the whole-document fallback, anywhere in the original
    #: file. `False` on a fresh file, an append (nothing existing is ever
    #: touched), and the no-op path where `content` is `original` verbatim.
    comments_dropped: bool
    #: The legacy `"ALP: ..."` name of an entry that STILL sits in the file,
    #: untouched, because the ordinary same-name merge ran instead (an
    #: exact-name HIT against the current name). tan-cli#179: this used to be
    #: silent -- the customer's real hand-filled values can still be stranded on
    #: that leftover entry (the #133 symptom), with nothing pointing at it.
    legacy_entry_present: str | None
    #: The one configuration entry actually written this run -- the
    #: merged/migrated result for a replace, or the draft itself for an append.
    #: tan-cli#180: distinct from the DRAFT the caller passed in, which still
    #: carries its own fresh `<resolved-...>` placeholders even when this run
    #: merged over a customer's real, resolved values.
    written_configuration: Any


def _legacy_name(next_name: str) -> str | None:
    """The pre-#155 spelling of a current launch-configuration name, or ``None``
    if ``next_name`` does not use the current ``"Alp: "`` prefix (defensive:
    every name [`create_launch_draft`] emits does).

    Deliberately narrow: this computes the ONE legacy string corresponding to
    ``next_name`` -- one of the four names this module ever emits -- rather than
    matching any configuration whose name happens to start with ``"ALP: "``. A
    customer's own unrelated ``"ALP: My Custom Config"`` is never touched.
    """
    if next_name.startswith("Alp: "):
        return f"ALP: {next_name[len('Alp: '):]}"
    return None


def create_launch_json_write_plan(
    existing_content: str | None, draft: dict[str, Any]
) -> LaunchJsonWritePlan:
    """Merge ``draft`` into an existing launch.json (or a fresh document),
    merging key-by-key over any configuration with the same ``name``. Mirrors TS
    ``createLaunchJsonWritePlan``.

    #133 (reopened): the #155 rename to ``"Alp: ..."`` left any entry still
    spelled ``"ALP: ..."`` orphaned -- nothing matched it by exact name any more,
    so it silently stopped receiving merges. That is not cosmetic: the orphaned
    entry is exactly where a customer's own hand-resolved fields already lived,
    and the maintained entry kept its placeholder. So a MISS on the current name
    falls through to a search for that one legacy counterpart before giving up
    and appending fresh, and a hit there is folded in via the SAME
    [`_merge_configuration`] a same-named update already uses.

    This only ever fires on a MISS. When a current-named entry already exists --
    whether or not a legacy one *also* still sits in the file -- this takes the
    ordinary same-name-replace path and never looks for a legacy counterpart.
    The legacy entry is left exactly as it is: nothing decides which of two
    possibly-hand-edited entries is authoritative, so nothing is merged or
    deleted on this run's say-so (tan-cli#179 reports it instead).
    """
    document = _parse_launch_json_or_default(existing_content)
    next_name = _configuration_name(draft)
    configs = document["configurations"]

    existing_index = next(
        (i for i, c in enumerate(configs) if c.get("name") == next_name), None
    )

    replaced = False
    migrated_from: str | None = None
    legacy_entry_present: str | None = None
    # `splice_index` mirrors, into the ORIGINAL raw text, exactly which element
    # `entry` is replacing (`None` means append). It indexes the SAME filtered,
    # object-only ordering `_parse_launch_json_or_default` applied, so it lines
    # up with `jsonc_splice`'s own object-only element count without either side
    # re-deriving the other's filter.
    splice_index: int | None = None
    unchanged = False

    if existing_index is not None:
        pre_merge = configs[existing_index]
        entry = _merge_configuration(pre_merge, draft)
        unchanged = entry == pre_merge
        configs[existing_index] = entry
        legacy = _legacy_name(next_name)
        if legacy is not None and any(c.get("name") == legacy for c in configs):
            legacy_entry_present = legacy
        replaced = True
        splice_index = existing_index
    else:
        legacy = _legacy_name(next_name)
        legacy_index = (
            next((i for i, c in enumerate(configs) if c.get("name") == legacy), None)
            if legacy is not None
            else None
        )
        if legacy_index is not None:
            pre_merge = configs[legacy_index]
            migrated_from = pre_merge.get("name")
            entry = _merge_configuration(pre_merge, draft)
            unchanged = entry == pre_merge
            configs[legacy_index] = entry
            replaced = True
            splice_index = legacy_index
        else:
            entry = dict(draft)
            configs.append(entry)

    # tan-cli#182 review finding #1: a semantically no-op re-run (the merged
    # entry is identical, ignoring formatting, to what was already there) still
    # spliced the maintained entry back into itself, which reformats it and
    # discards any comment sitting inside -- on a file the extension re-runs
    # `debug-config` against on every session, "nothing changed" is the COMMON
    # case, not an edge case. The splice only ever touches the one entry that
    # changed, so an unchanged entry means an unchanged document: skip the write
    # entirely and hand back `original`'s own bytes.
    if unchanged:
        # An unchanged merge only happens against an existing entry, so
        # `existing_content` cannot be None here -- but assert it rather than
        # letting a future refactor turn it into a silent `"None"` write.
        assert existing_content is not None
        content, comments_dropped = existing_content, False
    else:
        content, comments_dropped = _write_content(
            existing_content, document, splice_index, entry
        )
    return LaunchJsonWritePlan(
        content=content,
        replaced=replaced,
        migrated_from=migrated_from,
        comments_dropped=comments_dropped,
        legacy_entry_present=legacy_entry_present,
        written_configuration=entry,
    )


def _write_content(
    existing_content: str | None,
    document: dict[str, Any],
    splice_index: int | None,
    entry: Any,
) -> tuple[str, bool]:
    """Render the write plan's final bytes.

    tan-cli#182: this used to be a whole-document re-serialise unconditionally,
    which destroyed every comment, trailing comma and leading BOM in a
    customer's hand-edited launch.json on every single run, not just the one
    entry being changed. Now the write is a targeted splice into
    ``existing_content``'s own text whenever
    ``jsonc_splice.locate_configuration_edit`` can confidently place it:
    everything outside the edited entry is copied through unconditionally,
    because there is no re-serialisation pass over it to lose anything. Only the
    entry actually being written is reformatted -- that entry's own prior
    comments are the one unavoidable casualty, the same way any tool that edits
    one JSON object's fields must discard stray comments sitting BETWEEN those
    fields; a comment ABOVE the entry survives with everything else.

    Falls back to the whole-document re-serialise when there is no original text
    to splice into (a fresh file) or the locator cannot confidently place the
    edit. That fallback is lossy of comments exactly as before, but never
    malformed: it is the documented safety net, not a second bug.

    Returns the content alongside whether the write dropped a comment (or
    trailing comma) the customer's file held -- disclosing that is the
    non-negotiable floor #182 named, not an optional nicety.
    """
    if existing_content is not None:
        edit = jsonc_splice.locate_configuration_edit(existing_content, splice_index)
        if edit is not None:
            if edit.kind == "replace":
                span = existing_content[edit.start : edit.end]
                dropped = strip_jsonc(span) != span
            else:
                # An append never rewrites any existing span.
                dropped = False
            return jsonc_splice.apply_edit(existing_content, edit, entry), dropped
    dropped = existing_content is not None and strip_jsonc(existing_content) != existing_content
    return jsonc_splice.pretty_json(document) + "\n", dropped


def strip_jsonc(text: str) -> str:
    """Strip a leading UTF-8 BOM plus ``//`` / ``/* */`` comments from JSONC
    text, then drop trailing commas before a closing ``}``/``]``, so the result
    parses as plain JSON. Scans character by character and tracks string state,
    so a ``//`` or trailing comma *inside* a quoted string value is never
    touched."""
    if text.startswith(jsonc_splice.BOM):
        text = text[1:]

    out: list[str] = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        char = text[i]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
        elif char == "/" and text[i + 1 : i + 2] == "/":
            i += 2
            while i < len(text):
                if text[i] == "\n":
                    out.append("\n")
                    i += 1
                    break
                i += 1
        elif char == "/" and text[i + 1 : i + 2] == "*":
            i += 2
            while i < len(text):
                if text[i] == "*" and text[i + 1 : i + 2] == "/":
                    i += 2
                    break
                i += 1
        else:
            out.append(char)
            i += 1

    no_comments = "".join(out)
    result: list[str] = []
    in_string = False
    escaped = False
    i = 0
    while i < len(no_comments):
        char = no_comments[i]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            i += 1
            continue
        if char == ",":
            j = i + 1
            while j < len(no_comments) and no_comments[j].isspace():
                j += 1
            if j < len(no_comments) and no_comments[j] in "}]":
                i += 1
                continue  # drop the trailing comma
        result.append(char)
        i += 1
    return "".join(result)


def _parse_launch_json_or_default(content: str | None) -> dict[str, Any]:
    """Parse an existing launch.json, or return a fresh empty document.

    launch.json is JSONC in the wild, not strict JSON: VS Code's own "Add
    Configuration" template opens with ``//`` comment lines, trailing commas are
    common, and a Windows-authored file routinely carries a UTF-8 BOM. A strict
    parse used to reject exactly the file VS Code itself writes, sending the user
    to hand-edit a "valid" file.
    """
    stripped = strip_jsonc(content).strip() if content is not None else ""
    if stripped == "":
        return {"version": "0.2.0", "configurations": []}

    try:
        parsed = json.loads(stripped)
    except ValueError as err:
        raise DebugConfigError("Alp: .vscode/launch.json is not valid JSON.") from err
    if not isinstance(parsed, dict):
        raise DebugConfigError("Alp: .vscode/launch.json must be a JSON object.")

    version = parsed.get("version")
    if not (isinstance(version, str) and version.strip()):
        version = "0.2.0"
    raw_configs = parsed.get("configurations")
    configurations = (
        [c for c in raw_configs if isinstance(c, dict)]
        if isinstance(raw_configs, list)
        else []
    )
    # {**parsed, version, configurations}: keep the file's own key order, override
    # the two keys. A dict assignment to an existing key updates it in place,
    # which is what the Rust's IndexMap insert does.
    parsed["version"] = version
    parsed["configurations"] = configurations
    return parsed


def _configuration_name(configuration: dict[str, Any]) -> str:
    name = configuration.get("name")
    if isinstance(name, str) and name.strip():
        return name
    raise DebugConfigError("Alp: debug launch draft is missing a valid name.")
