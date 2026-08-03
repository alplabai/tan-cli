# SPDX-License-Identifier: Apache-2.0
"""SETOOLS integration for the Flow D (`alif_mram_jlink`) slot0 sign step --
tan-cli#353's remaining half.

Both host paths that put a signed image into an Alif Ensemble part's MRAM
(`tan.core.flash_plan`'s Flow A/Flow D) need Alif's SETOOLS `app-gen-toc` step
to sign the ATOC first; alp-sdk's own manifest never carries a signed blob --
measured on a fresh AEN801 emit, `flash_args` holds only
`jlink_flash_device`. Before this module, that meant a customer signed
OUTSIDE tan (`docs/aen-provisioning.md` §3-4) and hand-edited the manifest
with the resulting `atoc`/`atoc_address` before `tan flash` would do anything
-- `flash_plan.plan_alif_mram_jlink`'s "both required" refusal names the
missing fields, not the vendor tool that produces them.

**SETOOLS is license-gated and Alp Lab does not redistribute it** -- the same
stance `tan doctor`'s own `setools` check already takes. What this module
adds is: given a SETOOLS install the customer already has on disk, drive its
`app-gen-toc` step for them -- copy the build's raw `.bin`, write the JSON
config it wants, run it, and read back the ATOC placement it prints -- so
`tan flash` can complete end to end. RESOLVING that install is also this
module's job ([`resolve_setools_dir`]): an explicit `flash_args.setools_dir`,
then `SETOOLS_DIR`, in that order, and NOTHING ELSE -- no filesystem search --
because a WRONG SETOOLS silently signing against the wrong part is worse than
tan refusing outright.

**Not `tan.core.flash_plan`.** That module is pure/no-IO by its own
docstring; this one is not -- it copies a file, writes a config, and spawns
`app-gen-toc`, the same real-filesystem-work exception
`tan.core.venv`/`tan.core.bootstrap` already carry. Every DECISION about
*when* to call this module (never under `--dry-run`, never off the
`alif_mram_jlink` path, never once `atoc`/`atoc_address` are already
resolved) stays in `tan.commands.flash_cmd`, which is also the only caller.

**No new hardware fact (ADR-0017 / I-26).** `mramAddress` is
`flash_args.slot0_load_address` verbatim -- already a documented Flow D key
(`flash_plan.plan_alif_mram_jlink`) -- and `cpu_id` is the manifest's own
`core_id` upper-cased (`m55_he` -> `M55_HE`); neither is invented here. The
written config also omits SETOOLS' own `"DEVICE"` key on purpose:
`docs/aen-provisioning.md` §4 is explicit that "the on-module factory DEVICE
config is already correct for your part, so write an app-only ATOC (don't
overwrite the device config)" -- inventing a device profile here would be
exactly the new hardware fact ADR-0017 forbids, for no documented benefit.

ponytail: `cpu_id = core_id.upper()` is a naming-convention bet, not a
metadata fact -- correct for every AEN `core_id` measured so far (`m55_he` /
`m55_hp`). Upgrade path if a future `core_id` spelling ever diverges from
SETOOLS' own `cpu_id` vocabulary: a `flash_args.setools_cpu_id` override,
added once a real manifest needs one -- not added speculatively here.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from tan.core.flash_plan import (
    FLOW_D_METHOD,
    FlashPlanError,
    fa_str,
    parse_atoc_start_address,
    validate_identifier,
)

#: The one SETOOLS executable this module drives. Bare name, no extension --
#: the Alif Security Toolkit bundle (`app-release-exec-linux-SE_FW_x.y.z`) is
#: a Linux tool; nothing here guesses a `.exe`/`.bat` variant, matching `tan
#: doctor`'s own `setools_check` (`tan/commands/doctor_cmd.py`).
APP_GEN_TOC = "app-gen-toc"

#: Seconds `app-gen-toc` may run before it is killed -- a local sign step over
#: one small binary, generous mainly against a hung/misconfigured SETOOLS
#: install (e.g. waiting on an interactive prompt an app-only ATOC should
#: never need).
APP_GEN_TOC_TIMEOUT_S = 120.0

#: SETOOLS' own fixed output locations, always relative to `$SETOOLS_DIR` and
#: never configurable -- reading these back is not "searching the
#: filesystem": they are the ONE place `app-gen-toc` itself writes, per
#: `docs/aen-provisioning.md` and every bench script under
#: `scripts/bench/aen/`.
_ATOC_BLOB_REL = os.path.join("build", "AppTocPackage.bin")
_ATOC_MAP_REL = os.path.join("build", "app-package-map.txt")


@dataclass(frozen=True)
class SetoolsSource:
    """A resolved `$SETOOLS_DIR`, plus WHERE it came from -- every refusal
    downstream names `source`, so a customer juggling both an explicit
    manifest value and a shell export knows which one tan actually read."""

    path: str
    source: str


def resolve_setools_dir(flash_args: Any, env: dict[str, str]) -> SetoolsSource | None:
    """Most-explicit-first: `flash_args.setools_dir`, then `$SETOOLS_DIR`.
    `None` when neither is set. Never a filesystem search and never a guess --
    a wrong SETOOLS signing against the wrong part is worse than refusing."""
    explicit = fa_str(flash_args, "setools_dir")
    if explicit:
        return SetoolsSource(explicit, "flash_args.setools_dir")
    from_env = env.get("SETOOLS_DIR")
    if from_env:
        return SetoolsSource(from_env, "the SETOOLS_DIR environment variable")
    return None


def find_app_gen_toc(setools_dir: str) -> str | None:
    """`APP_GEN_TOC` inside `setools_dir`, or `None`. Incapable of raising --
    `setools_dir` is a customer-supplied path (`flash_args.setools_dir` or an
    env var) that may hold anything."""
    candidate = os.path.join(setools_dir, APP_GEN_TOC)
    try:
        return candidate if os.path.isfile(candidate) else None
    except (OSError, ValueError):
        return None


def unresolved_message() -> str:
    """The guidance for `resolve_setools_dir` answering `None` -- names what
    is needed, why, and the exact fix, never a bare field name. Modeled on
    `sdk_cmd.NO_SDK_NEXT_STEPS`/`doctor_cmd.setools_check`'s own tone: remedy
    first, blame never."""
    return (
        f"{FLOW_D_METHOD}: an AEN801 slot0 image needs a SIGNED ATOC, which only "
        f"Alif's SETOOLS `{APP_GEN_TOC}` step can produce. SETOOLS is license-gated "
        "and alp-sdk does not redistribute it -- download it from the Alif developer "
        "portal, then point tan at your install: SETOOLS_DIR=<path> in the "
        "environment, or flash_args.setools_dir in the manifest."
    )


def missing_tool_message(setools: SetoolsSource) -> str:
    """The guidance when `setools.path` resolved (from `setools.source`) but
    does not look like a real SETOOLS install -- distinct from
    [`unresolved_message`] because the customer already told tan where to
    look; the problem is what tan found there, not that nothing was named."""
    return (
        f"{FLOW_D_METHOD}: SETOOLS_DIR resolved to '{setools.path}' (via "
        f"{setools.source}), but no '{APP_GEN_TOC}' was found there -- this does not "
        "look like an Alif Security Toolkit install. Check the path, or re-download "
        "SETOOLS from the Alif developer portal."
    )


def slot0_config(name: str, binary: str, mram_address: str, cpu_id: str) -> dict[str, Any]:
    """The `app-gen-toc` JSON config for one app-only slot0 ATOC -- the exact
    shape the AEN801 bench flow signs by hand today (measured, tan-cli#353).
    No top-level `"DEVICE"` key -- see the module docstring."""
    return {
        name: {
            "binary": binary,
            "version": "1.0.0",
            "mramAddress": mram_address,
            "cpu_id": cpu_id,
            "flags": ["boot"],
            "signed": True,
        }
    }


def read_atoc_address(setools_dir: str) -> str | None:
    """The ATOC placement `app-gen-toc` just wrote, out of its own
    `build/app-package-map.txt` report. Reuses `flash_plan
    .parse_atoc_start_address`'s parse (byte-identical to every bench
    script's own `awk .../app-package-map.txt | tail -1`) -- the read half
    lives here, not there, since `flash_plan` stays no-IO. `None` when the
    report is not there; a caller decides what that means."""
    map_path = os.path.join(setools_dir, _ATOC_MAP_REL)
    try:
        with open(map_path, encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()
    except OSError:
        return None
    return parse_atoc_start_address(text)


def _tail(stdout: str, stderr: str) -> str:
    """The last 4 non-empty lines of whichever stream carries something --
    mirrors `flash_cmd._capture_tail`'s shape (not imported: that helper
    reads a `flash_cmd._Outcome`, a shape this module has no reason to
    depend on)."""
    text = stderr if stderr.strip() else stdout
    lines = [line for line in text.splitlines() if line.strip()][-4:]
    return " | ".join(lines) if lines else "no output"


def sign_slot0(
    setools_dir: str,
    app_gen_toc: str,
    artefact_bin: str,
    entry_id: str,
    mram_address: str,
) -> tuple[str, str]:
    """Run one `app-gen-toc` sign step inside `setools_dir`: copy
    `artefact_bin` into `build/images/`, write
    `build/config/<entry_id>-slot0.json`, spawn
    `app_gen_toc -f build/config/<entry_id>-slot0.json` with
    `cwd=setools_dir` (its config path is relative to it, matching the
    bench's own `cd $SETOOLS_DIR && ./app-gen-toc -f build/config/...`), then
    read back the ATOC placement. Returns `(atoc_blob_path, atoc_address)`.

    Raises `FlashPlanError` -- naming `app-gen-toc`'s own captured output
    where there is any -- on: a filesystem failure preparing the inputs, a
    spawn failure or timeout, a non-zero exit, a successful exit whose
    `app-package-map.txt` carries no `'APP Package Start Address:'` line, or
    a successful exit that did not actually produce the ATOC blob. SETOOLS'
    own diagnostic is the authoritative one; this does not try to reproduce
    it, only to surface it.
    """
    validate_identifier(entry_id, "the flash target id")
    setools_dir = os.path.abspath(setools_dir)
    app_gen_toc = os.path.abspath(app_gen_toc)
    images_dir = os.path.join(setools_dir, "build", "images")
    config_dir = os.path.join(setools_dir, "build", "config")
    binary_name = f"{entry_id}.bin"
    config_name = f"{entry_id}-slot0.json"
    try:
        os.makedirs(images_dir, exist_ok=True)
        os.makedirs(config_dir, exist_ok=True)
        shutil.copyfile(artefact_bin, os.path.join(images_dir, binary_name))
        config_path = os.path.join(config_dir, config_name)
        with open(config_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(
                slot0_config(entry_id, binary_name, mram_address, entry_id.upper()), fh, indent=2
            )
            fh.write("\n")
    except OSError as err:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: could not prepare the SETOOLS sign step under "
            f"'{setools_dir}': {err}"
        ) from err

    config_rel = os.path.join("build", "config", config_name)
    try:
        proc = subprocess.run(
            [app_gen_toc, "-f", config_rel],
            cwd=setools_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=APP_GEN_TOC_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as err:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: {APP_GEN_TOC} timed out after "
            f"{APP_GEN_TOC_TIMEOUT_S:.0f}s signing {config_rel}"
        ) from err
    except OSError as err:
        raise FlashPlanError(f"{FLOW_D_METHOD}: could not run {app_gen_toc}: {err}") from err
    if proc.returncode != 0:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: {APP_GEN_TOC} -f {config_rel} exited {proc.returncode}: "
            f"{_tail(proc.stdout, proc.stderr)}"
        )

    address = read_atoc_address(setools_dir)
    if address is None:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: {APP_GEN_TOC} exited 0 but "
            f"{os.path.join(setools_dir, _ATOC_MAP_REL)} carries no 'APP Package Start "
            "Address:' line -- check the SETOOLS config, or sign by hand."
        )
    atoc_path = os.path.join(setools_dir, _ATOC_BLOB_REL)
    if not os.path.isfile(atoc_path):
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: {APP_GEN_TOC} exited 0 and reported an address, but "
            f"{atoc_path} was not produced -- check the SETOOLS output."
        )
    return atoc_path, address
