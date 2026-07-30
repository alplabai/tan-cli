# SPDX-License-Identifier: Apache-2.0
"""Pure post-build decision logic for `tan run` -- port of
`crates/tan-core/src/run.rs`. No IO: the subprocess spawn and the filesystem
probe live in `tan.commands.run_cmd`.

`tan run` is a THIN ORCHESTRATOR over `tan build`'s engine and (on a hardware
target, with `--flash`) `tan flash`'s engine -- it never re-derives either.
The only thing this module owns is the decision of WHICH of those two to
reach, from signals the build step that just ran produced: whether the build
succeeded, whether it could establish a host (`native_sim`) vs. hardware
target, whether `--flash` was requested, and whether the post-build
`system-manifest.yaml` write is confirmed to have succeeded. Flashing real
silicon is the dangerous path, so it is never the default action -- a
hardware target flashes only on an explicit `--flash`.

**`native_sim_target`/`manifest_written` must come from the SAME build run
`tan run` just performed, in memory -- never from re-reading
`system-manifest.yaml` off disk afterward.** The Rust oracle's own module doc
records why (three attempts, the last two of which re-read a stale on-disk
manifest and let a `--flash` slip through against the WRONG target). This
port's `tan build` does not write a system manifest yet
(`tan.commands.build.execute`'s own docstring names the same gap), so
`tan.commands.run_cmd` currently calls this with `native_sim_target=None` --
"could not be established", which is the honest reading, never "not
native_sim" -- until that gap closes.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

#: The Zephyr board target of a `native_sim` slice -- the robust
#: discriminator for the host build (an actual `board` value from the
#: manifest, not a build-dir-name guess). Verbatim from `tan_core::run`.
NATIVE_SIM_BOARD = "native_sim"

#: The literal binary Zephyr's `native_sim` target produces -- a fixed name on
#: every host OS. It sits beside `zephyr.elf` in the build's `zephyr/` output
#: dir.
#:
#: Module-private (the oracle's equivalent is `pub(crate)`, run.rs:166-169 --
#: the #83 fix): [`native_sim_exe_beside`] is the ONLY sanctioned way to spell
#: the swap. A third caller importing this constant to hand-roll
#: `parent().join(NATIVE_SIM_EXE)` again would silently reopen the door #83
#: closed -- see that function's docstring.
_NATIVE_SIM_EXE = "zephyr.exe"


class RunAction(StrEnum):
    """What `tan run` does after its build step -- the pure decision,
    decoupled from the subprocess/filesystem work so it is unit-testable
    without a real build."""

    #: The build failed -- short-circuit: never run or flash, report the build.
    BUILD_FAILED = "build_failed"
    #: A host target -- execute the produced `native_sim` binary.
    EXECUTE_NATIVE = "execute_native"
    #: A hardware target with an explicit `--flash`, and THIS run's manifest
    #: write is confirmed to have succeeded -- flash the built image.
    FLASH = "flash"
    #: A hardware target WITHOUT `--flash` -- build succeeded, but programming
    #: the board needs explicit consent: report + stop, leave hardware
    #: untouched.
    BUILD_ONLY = "build_only"
    #: `--flash` was requested, but this run's OWN build outcome does not
    #: confirm it is safe to flash: either the target could not be
    #: established at all (`native_sim_target is None`), or it is hardware
    #: but THIS run's manifest file write failed (`manifest_written` is
    #: `False`). Refuse rather than flash from a build that can't vouch for
    #: itself. Board untouched, same as `BUILD_ONLY`, but reported as an
    #: error: the caller explicitly asked to flash and that request could not
    #: be honored safely.
    MANIFEST_STALE = "manifest_stale"


def decide_run_action(
    build_ok: bool,
    native_sim_target: bool | None,
    flash_requested: bool,
    manifest_written: bool,
) -> RunAction:
    """Decide what `tan run` does once the build step finished -- entirely
    from signals THIS build produced, never from a post-hoc disk read (see
    the module doc for why that used to be the actual defect).

    - `native_sim_target` is `True`/`False` when this run's build established
      a target (from the SDK's freshly-emitted system-manifest projection,
      independent of whether the on-disk file write then succeeded), or
      `None` when it could not (the emit itself failed/didn't parse) -- an
      unconfirmed target, not "not native_sim".
    - `manifest_written` is whether THIS run's `system-manifest.yaml` file
      write actually succeeded -- the "write definitely succeeded" signal
      that gates `--flash` for a confirmed hardware target (a failed write
      means `flash` would next read a stale/wrong file).

    Rules (verbatim from `tan_core::run::decide_run_action`):
    - build failed                                     -> BUILD_FAILED
    - host target (`True`)                             -> EXECUTE_NATIVE
      (never FLASH: native_sim is not flashable, regardless of `--flash`/
      `manifest_written`)
    - hardware (`False`) + `--flash` + write ok         -> FLASH
    - hardware (`False`) + `--flash`, write failed      -> MANIFEST_STALE
    - hardware (`False`), no `--flash`                  -> BUILD_ONLY
    - unknown (`None`) + `--flash`                      -> MANIFEST_STALE (refuse)
    - unknown (`None`), no `--flash`                    -> BUILD_ONLY
    """
    if not build_ok:
        return RunAction.BUILD_FAILED
    if native_sim_target is True:
        # A host target is never flashable -- settled by THIS run's own build
        # outcome, so a failed/absent manifest FILE write can't flip a host
        # build into FLASH.
        return RunAction.EXECUTE_NATIVE
    if native_sim_target is False:
        if not flash_requested:
            return RunAction.BUILD_ONLY
        return RunAction.FLASH if manifest_written else RunAction.MANIFEST_STALE
    # native_sim_target is None: this run's build could not confirm host vs.
    # hardware at all. Flashing blind is never acceptable; a bare run still
    # gets to report + stop like any other hardware-shaped build.
    return RunAction.MANIFEST_STALE if flash_requested else RunAction.BUILD_ONLY


def is_native_sim_board(board: Any) -> bool:
    """True for the bare `native_sim` board AND Zephyr's qualified board form
    (`native_sim/native/64`, SoC/variant-qualified). Exact-matching only the
    bare name let a qualified board name defeat the host-vs-hardware
    discriminator on a perfectly fresh manifest. `"native_simulated_foo"`
    deliberately does NOT match -- the required `/` after the prefix anchors
    it to Zephyr's actual board-qualifier syntax instead of an arbitrary
    substring."""
    return isinstance(board, str) and (
        board == NATIVE_SIM_BOARD or board.startswith(f"{NATIVE_SIM_BOARD}/")
    )


def native_sim_slice(manifest: Any) -> dict[str, Any] | None:
    """The `native_sim` slice in a post-build manifest, if any -- identified
    by its Zephyr board target ([`is_native_sim_board`]). `manifest` is a
    `tan.core.system_manifest.SystemManifest`; its `slices` are raw dicts
    (the manifest reader's own lossless-round-trip convention). Pure: no IO.
    """
    return next(
        (s for s in manifest.slices if is_native_sim_board(s.get("board"))), None
    )


def native_sim_exe_beside(elf: str) -> str:
    """Swap a native_sim slice's `output_artefact` for the runnable
    [`_NATIVE_SIM_EXE`] that sits beside it. A manifest NEVER records the
    `.exe`: `output_artefact` always names `zephyr.elf`, for every zephyr
    slice including native_sim, so every consumer that wants the host
    runnable has to make this swap -- and must all make the SAME one. This is
    THE one spelling of the rule: `tan.commands.debug_config_cmd` calls this
    function rather than carrying its own copy (the #83 fix this module's
    `_NATIVE_SIM_EXE` docstring records).

    Splits on the separator the path itself uses rather than normalising via
    a path library, which on Windows would rejoin with `\\` and turn a
    `/`-authored manifest path into a mixed `a/b\\zephyr.exe`."""
    cut = max(elf.rfind("/"), elf.rfind("\\"))
    return _NATIVE_SIM_EXE if cut < 0 else elf[: cut + 1] + _NATIVE_SIM_EXE
