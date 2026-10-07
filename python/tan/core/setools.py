# SPDX-License-Identifier: Apache-2.0
"""SETOOLS integration for the Flow D (`alif_mram_jlink`) slot0 sign step --
tan-cli#353's remaining half.

Both host paths that put a signed image into an Alif Ensemble part's MRAM
(`tan.core.flash_plan`'s Flow A/Flow D) need Alif's SETOOLS `app-gen-toc` step
to sign the ATOC first; alp-sdk's own manifest never carries a signed blob --
measured on a fresh AEN801 emit, `flash_args` holds only
`jlink_flash_device`. Before this module, that meant a customer signed
OUTSIDE tan (alp-sdk's `docs/aen-provisioning.md` §3-4 -- that path is not in
THIS repo; alp-sdk is where it lives) and hand-edited the manifest
with the resulting `atoc`/`atoc_address` before `tan flash` would do anything
-- `flash_plan.plan_alif_mram_jlink`'s "both required" refusal names the
missing fields, not the vendor tool that produces them.

**SETOOLS is license-gated and Alp Lab does not redistribute it** -- the same
stance `tan doctor`'s own `setools` check already takes. What this module
adds is: given a SETOOLS install the customer already has on disk, drive its
`app-gen-toc` step for them -- copy the build's raw `.bin`, write the JSON
config it wants, run it, and read back the ATOC placement it prints -- so
`tan flash` can complete end to end. RESOLVING that install is also this
module's job ([`resolve_setools_dir`]): the `--setools-dir` flag, then
`SETOOLS_DIR`, then `flash_args.setools_dir`, in that order, and NOTHING
ELSE -- no filesystem search -- because a WRONG SETOOLS silently signing
against the wrong part is worse than tan refusing outright.

That ranking is deliberate and is the tan-cli#368 re-ranking; read
[`resolve_setools_dir`]'s own docstring before changing it. `flash_args` is
GENERATED -- every `tan build` overwrites it -- so a stale hand-edited
`setools_dir` must never outrank the `SETOOLS_DIR` an operator exported or a
flag they passed for this one invocation. This header previously stated the
reverse order (tan-cli#572), leaving two docstrings in ONE file disagreeing
about which SETOOLS signs the image.

**Not `tan.core.flash_plan`.** That module is pure/no-IO by its own
docstring; this one is not -- it copies a file, writes a config, and spawns
`app-gen-toc`, the same real-filesystem-work exception
`tan.core.venv`/`tan.core.bootstrap` already carry. Every DECISION about
*when* to call this module (never off the `alif_mram_jlink` path, never once
`atoc`/`atoc_address` are already resolved) stays in `tan.commands.flash_cmd`,
which is also the only caller.

**The sign never writes the customer's install (tan-cli#1325).** It runs in a
private scratch overlay of it (`tan.core.setools_scratch`), so it is
side-effect-free and `--dry-run` runs it too. The caller owns the scratch tree
and removes it when the entry ends.

**No new hardware fact (ADR-0017 / I-26).** `mramAddress` is
`flash_args.slot0_load_address` verbatim -- already a documented Flow D key
(`flash_plan.plan_alif_mram_jlink`) -- and `cpu_id` is the manifest's own
`core_id` upper-cased (`m55_he` -> `M55_HE`); neither is invented here.

ponytail: `cpu_id = core_id.upper()` is a naming-convention bet, not a
metadata fact -- correct for every AEN `core_id` measured so far (`m55_he` /
`m55_hp`). Upgrade path if a future `core_id` spelling ever diverges from
SETOOLS' own `cpu_id` vocabulary: a `flash_args.setools_cpu_id` override,
added once a real manifest needs one -- not added speculatively here.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tan.core.flash_plan import (
    FLOW_D_METHOD,
    FlashPlanError,
    fa_str,
    parse_atoc_start_address,
    validate_identifier,
)
from tan.core.setools_scratch import (
    DEVICE_ENTRY_VERSION,
    AtocReport,
    DeviceConfig,
    cleanup_scratch,
    make_scratch,
    parse_atoc_report,
)
from tan.core.subprocess_env import spawn_env

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
#: alp-sdk's `docs/aen-provisioning.md` and every bench script under alp-sdk's
#: `scripts/bench/aen/` -- NEITHER path exists in this repo (tan-cli); both
#: are alp-sdk paths, cited here only as the authority for the fixed shape.
_ATOC_BLOB_REL = os.path.join("build", "AppTocPackage.bin")
_ATOC_MAP_REL = os.path.join("build", "app-package-map.txt")

@dataclass(frozen=True)
class SetoolsSource:
    """A resolved `$SETOOLS_DIR`, plus WHERE it came from -- every refusal
    downstream names `source`, so a customer juggling both an explicit
    manifest value and a shell export knows which one tan actually read."""

    path: str
    source: str

    @property
    def operator_supplied(self) -> bool:
        """The path came from the operator -- `--setools-dir` or `$SETOOLS_DIR` --
        not from `flash_args.setools_dir`, which the PROJECT controls (a checkout's
        manifest). Only an operator-supplied install is one tan may EXECUTE for a
        preview (tan-cli#1343 review)."""
        return self.source != _MANIFEST_SOURCE


_MANIFEST_SOURCE = "flash_args.setools_dir"


def resolve_setools_dir(
    flash_args: Any, env: dict[str, str], flag: str | None = None
) -> SetoolsSource | None:
    """Most-explicit-first (tan-cli#368): the `--setools-dir` CLI flag, then
    `$SETOOLS_DIR`, then `flash_args.setools_dir`. `None` when none of the
    three is set. Never a filesystem search and never a guess -- a wrong
    SETOOLS signing against the wrong part is worse than refusing.

    The flag outranks the environment, which outranks the manifest --
    DELIBERATELY the opposite of most `flash_args` accessors in this codebase
    (which read the manifest as authoritative). `build/system-manifest.yaml`
    is regenerated by every `tan build` and alp-sdk's own emit carries no
    `setools_dir` key at all, so a hand-edit there is silently destroyed by
    the customer's next build (#368) -- it is the LEAST durable of the three,
    not the most, and is ranked accordingly. `SETOOLS_DIR` survives a build
    but is shell/session-scoped; `--setools-dir` is the one source pinnable
    per invocation regardless of either, so it wins outright.
    """
    if flag:
        return SetoolsSource(flag, "the --setools-dir flag")
    from_env = env.get("SETOOLS_DIR")
    if from_env:
        return SetoolsSource(from_env, "the SETOOLS_DIR environment variable")
    explicit = fa_str(flash_args, "setools_dir")
    if explicit:
        return SetoolsSource(explicit, _MANIFEST_SOURCE)
    return None


def _app_gen_toc_candidates(setools_dir: str) -> list[str]:
    """Every filename [`find_app_gen_toc`] tries, in order -- shared with
    [`missing_tool_message`] (tan-cli#369) so the diagnosis names EXACTLY
    what was checked, never a conclusion beyond it. Bare `APP_GEN_TOC`
    everywhere; also `APP_GEN_TOC + ".exe"` on Windows, since a genuine
    Windows SETOOLS install ships the executable with an extension and the
    bare name alone is never found there."""
    candidates = [os.path.join(setools_dir, APP_GEN_TOC)]
    if os.name == "nt":
        candidates.append(os.path.join(setools_dir, APP_GEN_TOC + ".exe"))
    return candidates


def find_app_gen_toc(setools_dir: str) -> str | None:
    """The first of [`_app_gen_toc_candidates`] that exists inside
    `setools_dir`, or `None`. Incapable of raising -- `setools_dir` is a
    customer-supplied path (`--setools-dir`, an env var, or
    `flash_args.setools_dir`) that may hold anything."""
    try:
        return next(
            (c for c in _app_gen_toc_candidates(setools_dir) if os.path.isfile(c)), None
        )
    except (OSError, ValueError):
        return None


def unresolved_message(sku: str | None = None, flash_device: str | None = None) -> str:
    """The guidance for `resolve_setools_dir` answering `None` -- names EVERY
    accepted source, in PRECEDENCE ORDER, flag first (tan-cli#368): the flag
    is the one source visible in `tan flash --help` and pinnable per
    invocation, so it leads; the manifest field is named last and flagged as
    build-owned, since `tan build` silently overwrites a hand-edit there on
    the customer's next build. Modeled on `sdk_cmd.NO_SDK_NEXT_STEPS`/
    `doctor_cmd.setools_check`'s own tone: remedy first, blame never.

    tan-cli#1319: the subject is NAMED from the manifest -- `sku` (its
    `hw_info.sku`) and `flash_device` (`flash_args.jlink_flash_device`, the
    SoC variant's J-Link part profile) -- never a hardcoded SKU. Both absent
    (a hand-written manifest) falls back to a SKU-free noun."""
    subject = _slot0_subject(sku, flash_device)
    return (
        f"{FLOW_D_METHOD}: {subject} needs a SIGNED ATOC, which only "
        f"Alif's SETOOLS `{APP_GEN_TOC}` step can produce. SETOOLS is license-gated "
        "and alp-sdk does not redistribute it -- install it from Alif, then point "
        "tan at it, most-specific first: --setools-dir <path> on the command line, "
        "SETOOLS_DIR=<path> in the environment, or flash_args.setools_dir in the "
        "manifest (lowest precedence, and OVERWRITTEN by the next `tan build` -- "
        "prefer the flag or the environment variable for a durable setting)."
    )


def _slot0_subject(sku: str | None, flash_device: str | None) -> str:
    """`the E1M-AEN803 slot0 image (AE822FA0E5597LS0_M55_HE)` -- whatever of
    the manifest's SKU / J-Link device profile is known, never invented."""
    parts = [p.strip() for p in (sku, flash_device) if isinstance(p, str) and p.strip()]
    if not parts:
        return "an Alif Ensemble MRAM slot0 image"
    if len(parts) == 1:
        return f"the {parts[0]} slot0 image"
    return f"the {parts[0]} slot0 image ({parts[1]})"


def missing_tool_message(setools: SetoolsSource) -> str:
    """The guidance when `setools.path` resolved (from `setools.source`) but
    [`find_app_gen_toc`] found nothing there -- distinct from
    [`unresolved_message`] because the customer already told tan where to
    look; the problem is what tan found there, not that nothing was named.

    **tan-cli#369.** Used to assert a CONCLUSION ("this does not look like an
    Alif Security Toolkit install") identically for a directory that does not
    exist at all, a path pointed at the `app-gen-toc` BINARY itself instead
    of its parent directory, and a genuine Windows install
    (`find_app_gen_toc` did not try `app-gen-toc.exe` -- fixed alongside
    this). Names only what was actually checked: every candidate filename
    [`_app_gen_toc_candidates`] tries, and whether `setools.path` is even a
    real directory -- never a verdict the check did not make.
    """
    candidates = _app_gen_toc_candidates(setools.path)
    tried = " or ".join(f"'{c}'" for c in candidates)
    try:
        is_dir = os.path.isdir(setools.path)
    except (OSError, ValueError):
        is_dir = False
    if is_dir:
        where = f"SETOOLS not found at {tried} -- the directory exists but holds none of them."
    else:
        where = (
            f"SETOOLS not found at {tried} -- '{setools.path}' is not a directory at "
            f"all. If it names the {APP_GEN_TOC} binary itself, point tan at its "
            "PARENT directory instead."
        )
    return (
        f"{FLOW_D_METHOD}: SETOOLS_DIR resolved to '{setools.path}' (via "
        f"{setools.source}), but {where} Check the path, or re-download SETOOLS from "
        "Alif."
    )


def slot0_config(
    name: str,
    binary: str,
    mram_address: str,
    cpu_id: str,
    device_binary: str | None = None,
) -> dict[str, Any]:
    """The `app-gen-toc` JSON config for one slot0 ATOC. With `device_binary`
    (the file name of the device configuration, tan-cli#1322) the table leads
    with a `DEVICE` entry in the exact shape alp-sdk's bench recipe signs
    (`scripts/bench/aen/flash-run.sh`: `binary`, version `0.5.00`, `signed`,
    `disabled: false`); without it the config is the app-only shape tan-cli#353
    first measured. The app entry itself is identical either way."""
    config: dict[str, Any] = {}
    if device_binary is not None:
        config["DEVICE"] = {
            "disabled": False,
            "binary": device_binary,
            "version": DEVICE_ENTRY_VERSION,
            "signed": True,
        }
    config[name] = {
        "binary": binary,
        "version": "1.0.0",
        "mramAddress": mram_address,
        "cpu_id": cpu_id,
        "flags": ["boot"],
        "signed": True,
    }
    return config


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


@dataclass(frozen=True)
class SignedSlot0:
    """One completed scratch sign (tan-cli#1325). The ATOC blob lives in
    `scratch_dir`, which the CALLER removes ([`cleanup_scratch`]) once J-Link has
    read it; the shared SETOOLS install was never written."""

    atoc_path: str
    atoc_address: str
    atoc_size: int
    scratch_dir: str
    report: AtocReport
    device_config: DeviceConfig | None
    #: Paths in the SHARED install newer than the start of this sign (at most 5).
    #: Expected empty; non-empty means another process wrote there meanwhile -- or
    #: that the overlay leaked. Reported, never a refusal (a concurrent raw bench
    #: recipe is legitimate).
    shared_touched: tuple[str, ...] = ()


def sign_slot0(
    setools_dir: str,
    app_gen_toc: str,
    artefact_bin: str,
    entry_id: str,
    mram_address: str,
    *,
    device_config: DeviceConfig | None = None,
    scratch_parent: str | None = None,
    on_scratch: Callable[[str], None] | None = None,
) -> SignedSlot0:
    """Run one `app-gen-toc` sign step in a PRIVATE scratch overlay of
    `setools_dir` ([`make_scratch`]): copy `artefact_bin` into the scratch
    `build/images/`, write `build/config/<entry_id>-slot0.json` (with a leading
    `DEVICE` entry when `device_config` is given, its file copied into the
    scratch `build/config/`), spawn the scratch copy of `app_gen_toc` with
    `cwd=<scratch>` (its config path is relative to it, matching the bench's own
    `cd $SETOOLS_DIR && ./app-gen-toc -f build/config/...`), then read back the
    ATOC placement and entry list from the scratch report.

    The shared install is READ ONLY here (tan-cli#1325): the previous design
    wrote `build/AppTocPackage.bin`, `build/images/`, `build/config/`, appended
    to `build/app-package-map.txt` and created `build/tan-atoc/` plus a lock file
    in it, so one `tan flash` changed the package every other user of that
    install found there. Because nothing shared is written, the cross-process
    lock tan-cli#380 needed for that shared output is gone too: two runs get two
    scratch trees and cannot cross-pair. A stale package or map cannot leak in
    either -- the scratch `build/` starts empty, so a tool that exits 0 without
    writing is caught by the plain "no report / no blob" checks below.

    Returns [`SignedSlot0`]; the caller owns `scratch_dir`. Raises
    `FlashPlanError` -- naming `app-gen-toc`'s own captured output where there is
    any -- on: a filesystem failure preparing the scratch tree, a spawn failure
    or timeout, a non-zero exit, a report with no `'APP Package Start Address:'`
    line, or a successful exit that did not produce the ATOC blob. SETOOLS' own
    diagnostic is the authoritative one; this only surfaces it. On any raise the
    scratch tree is already removed.
    """
    validate_identifier(entry_id, "the flash target id")
    setools_dir = os.path.abspath(setools_dir)
    app_gen_toc = os.path.abspath(app_gen_toc)
    try:
        scratch = make_scratch(setools_dir, scratch_parent)
    except OSError as err:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: could not prepare a scratch copy of the SETOOLS install "
            f"'{setools_dir}' for the sign step: {err}"
        ) from err
    started = time.time_ns()
    try:
        # BEFORE anything can be interrupted: the caller registers the scratch's
        # removal here, so a KeyboardInterrupt mid-sign cannot leak the tree.
        if on_scratch is not None:
            on_scratch(scratch)
        signed = _sign_in_scratch(
            scratch, setools_dir, app_gen_toc, artefact_bin, entry_id, mram_address,
            device_config,
        )
        return dataclasses.replace(signed, shared_touched=_newer_than(setools_dir, started))
    except BaseException:
        cleanup_scratch(scratch)
        raise


def _newer_than(root: str, since_ns: int, limit: int = 5) -> tuple[str, ...]:
    """Files under `root` modified after `since_ns` (cheap post-check that the
    overlay kept the shared install untouched). Never raises."""
    found: list[str] = []
    try:
        for base, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(base, name)
                try:
                    if os.lstat(path).st_mtime_ns > since_ns:
                        found.append(path)
                except OSError:
                    continue
                if len(found) >= limit:
                    return tuple(found)
    except OSError:
        pass
    return tuple(found)


def _sign_in_scratch(
    scratch: str,
    setools_dir: str,
    app_gen_toc: str,
    artefact_bin: str,
    entry_id: str,
    mram_address: str,
    device_config: DeviceConfig | None,
) -> SignedSlot0:
    binary_name = f"{entry_id}.bin"
    config_rel = os.path.join("build", "config", f"{entry_id}-slot0.json")
    atoc_map_path = os.path.join(scratch, _ATOC_MAP_REL)
    atoc_blob_path = os.path.join(scratch, _ATOC_BLOB_REL)
    try:
        shutil.copyfile(artefact_bin, os.path.join(scratch, "build", "images", binary_name))
        if device_config is not None:
            shutil.copyfile(
                device_config.path, os.path.join(scratch, "build", "config", device_config.name)
            )
        with open(os.path.join(scratch, config_rel), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(
                slot0_config(
                    entry_id, binary_name, mram_address, entry_id.upper(),
                    device_binary=device_config.name if device_config is not None else None,
                ),
                fh,
                indent=2,
            )
            fh.write("\n")
    except OSError as err:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: could not prepare the SETOOLS sign step under the scratch "
            f"copy '{scratch}' of '{setools_dir}': {err}"
        ) from err

    # The scratch copy of the tool, not the shared one: its own directory (and the
    # `../build` it addresses) is then the scratch root.
    inside = os.path.relpath(app_gen_toc, setools_dir)
    scratch_tool = os.path.join(scratch, inside) if not inside.startswith("..") else app_gen_toc
    try:
        # tan-cli#992: this signs the ATOC that gets written to real silicon --
        # the same reasoning `flash_cmd._child_env` documents applies here
        # verbatim, so `env=` is never left to inherit this process's (possibly
        # bundle-poisoned) environment.
        proc = subprocess.run(
            [scratch_tool, "-f", config_rel],
            cwd=scratch,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=spawn_env(),
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

    try:
        with open(atoc_map_path, encoding="utf-8", errors="replace", newline="") as fh:
            report_text = fh.read()
    except OSError:
        report_text = ""
    address = parse_atoc_start_address(report_text)
    if address is None:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: {APP_GEN_TOC} exited 0 but its report "
            f"({_ATOC_MAP_REL}, in the scratch copy) carries no 'APP Package Start "
            "Address:' line -- check the SETOOLS config, or sign by hand."
        )
    try:
        atoc_size = os.path.getsize(atoc_blob_path)
    except OSError:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: {APP_GEN_TOC} exited 0 and reported an address, but "
            f"{_ATOC_BLOB_REL} was not produced in the scratch copy -- check the SETOOLS "
            "output."
        ) from None
    return SignedSlot0(
        atoc_path=atoc_blob_path,
        atoc_address=address,
        atoc_size=atoc_size,
        scratch_dir=scratch,
        report=parse_atoc_report(report_text),
        device_config=device_config,
    )
