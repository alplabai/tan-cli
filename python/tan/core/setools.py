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
*when* to call this module (never under `--dry-run`, never off the
`alif_mram_jlink` path, never once `atoc`/`atoc_address` are already
resolved) stays in `tan.commands.flash_cmd`, which is also the only caller.

**No new hardware fact (ADR-0017 / I-26).** `mramAddress` is
`flash_args.slot0_load_address` verbatim -- already a documented Flow D key
(`flash_plan.plan_alif_mram_jlink`) -- and `cpu_id` is the manifest's own
`core_id` upper-cased (`m55_he` -> `M55_HE`); neither is invented here. The
written config also omits SETOOLS' own `"DEVICE"` key on purpose:
alp-sdk's `docs/aen-provisioning.md` §4 (not a path in THIS repo) is explicit
that "the on-module factory DEVICE config is already correct for your part, so
write an app-only ATOC (don't overwrite the device config)" -- inventing a
device profile here would be exactly the new hardware fact ADR-0017 forbids,
for no documented benefit.

ponytail: `cpu_id = core_id.upper()` is a naming-convention bet, not a
metadata fact -- correct for every AEN `core_id` measured so far (`m55_he` /
`m55_hp`). Upgrade path if a future `core_id` spelling ever diverges from
SETOOLS' own `cpu_id` vocabulary: a `flash_args.setools_cpu_id` override,
added once a real manifest needs one -- not added speculatively here.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from tan.core.flash_plan import (
    FLOW_D_METHOD,
    FlashPlanError,
    fa_str,
    parse_atoc_start_address,
    validate_identifier,
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

#: Where [`_copy_out_atoc`] parks the IMMUTABLE per-run copy of the shared
#: blob above (tan-cli#380). Under `$SETOOLS_DIR/build/` on purpose: this
#: module already writes `build/images/` and `build/config/` there, so the
#: copy adds no NEW writability requirement -- by the time it runs, that tree
#: is proven writable. tan's own directory, nothing else reads it, and it is
#: safe to delete wholesale between flashes; nothing here prunes it, since a
#: prune racing a concurrent run is exactly the class of bug #380 is about.
_ATOC_COPY_REL = os.path.join("build", "tan-atoc")

#: The cross-process sign lock, one per resolved `$SETOOLS_DIR` (tan-cli#380).
#: At the install ROOT, beside `app-gen-toc` itself, not under `build/`: the
#: `build/` tree is created BY the step this serializes, so the lock has to
#: exist before it does. NEVER unlinked -- see [`_setools_lock`].
_LOCK_REL = ".tan-setools-sign.lock"

#: How long a queued sign step waits for that lock before refusing. DERIVED,
#: not picked: the longest a well-behaved holder can hold it is one
#: `APP_GEN_TOC_TIMEOUT_S` spawn (after which it is killed and the lock
#: released) plus its file copies, so a shorter wait would refuse a perfectly
#: healthy queued flash. The extra minute is that copy slack.
_LOCK_WAIT_S = APP_GEN_TOC_TIMEOUT_S + 60.0

#: Poll interval while waiting. Short enough that back-to-back board flashes
#: do not visibly stall, long enough not to spin a core for two minutes.
_LOCK_POLL_S = 0.05


@dataclass(frozen=True)
class SetoolsSource:
    """A resolved `$SETOOLS_DIR`, plus WHERE it came from -- every refusal
    downstream names `source`, so a customer juggling both an explicit
    manifest value and a shell export knows which one tan actually read."""

    path: str
    source: str


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
        return SetoolsSource(explicit, "flash_args.setools_dir")
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


def unresolved_message() -> str:
    """The guidance for `resolve_setools_dir` answering `None` -- names EVERY
    accepted source, in PRECEDENCE ORDER, flag first (tan-cli#368): the flag
    is the one source visible in `tan flash --help` and pinnable per
    invocation, so it leads; the manifest field is named last and flagged as
    build-owned, since `tan build` silently overwrites a hand-edit there on
    the customer's next build. Modeled on `sdk_cmd.NO_SDK_NEXT_STEPS`/
    `doctor_cmd.setools_check`'s own tone: remedy first, blame never."""
    return (
        f"{FLOW_D_METHOD}: an AEN801 slot0 image needs a SIGNED ATOC, which only "
        f"Alif's SETOOLS `{APP_GEN_TOC}` step can produce. SETOOLS is license-gated "
        "and alp-sdk does not redistribute it -- install it from Alif, then point "
        "tan at it, most-specific first: --setools-dir <path> on the command line, "
        "SETOOLS_DIR=<path> in the environment, or flash_args.setools_dir in the "
        "manifest (lowest precedence, and OVERWRITTEN by the next `tan build` -- "
        "prefer the flag or the environment variable for a durable setting)."
    )


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


def _map_stat(atoc_map_path: str) -> tuple[int, int] | None:
    """`(st_mtime_ns, st_size)` for `atoc_map_path`, or `None` when it does not
    exist (yet). The before/after snapshot [`sign_slot0`] compares to detect a
    soft failure WITHOUT deleting the file -- see its own docstring
    (tan-cli#373). Both fields, not either alone: an APPEND changes both, so
    the pair survives a coarse-mtime filesystem landing on a same-size
    coincidence, or vice versa."""
    try:
        st = os.stat(atoc_map_path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _try_lock(fd: int) -> bool:
    """ONE non-blocking attempt at an exclusive OS lock on `fd`'s first byte;
    `True` when it is ours (tan-cli#380).

    The two platform primitives and nothing else. Python's stdlib has no
    portable advisory lock, and the portable third option -- an
    `O_CREAT|O_EXCL` lockfile -- can only answer "is this lock STALE?" with a
    heuristic (an mtime threshold, or a PID the OS may already have reused),
    and getting that heuristic wrong on THIS path either wedges every future
    flash or hands two processes the same SETOOLS install. An OS lock has no
    stale state to reason about at all: **the kernel owns it, and drops it the
    moment the holding fd closes -- including when the holder is killed, panics
    or is `SIGKILL`ed.** A process that dies holding this lock therefore
    releases it immediately, leaving nothing to clean up and no lockfile to
    reap. What remains bounded by [`_LOCK_WAIT_S`] is only a LIVE holder that
    hangs, which the caller's own `APP_GEN_TOC_TIMEOUT_S` already bounds.

    Locking one byte at offset 0 of an empty file is deliberate and legal on
    both platforms (`LockFile` may lock a range past EOF; `flock` is
    whole-file regardless of the length argument).
    """
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    """Drop [`_try_lock`]'s lock. Closing `fd` releases it too on both
    platforms -- this is the EXPLICIT half, because Windows documents the
    release-on-close path as eventual ("the time it takes ... depends upon
    available system resources") while the next queued `tan flash` is already
    polling for it. Swallows `OSError`: this only ever runs on the way out of
    [`_setools_lock`], where the interesting exception is the caller's."""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def _setools_lock(setools_dir: str) -> Iterator[None]:
    """Serialize the whole `sign_slot0` critical section -- preparation,
    spawn, AND output capture -- against one resolved `$SETOOLS_DIR`
    (tan-cli#380, HARDWARE SAFETY).

    `app-gen-toc`'s outputs (`_ATOC_MAP_REL`, `_ATOC_BLOB_REL`) are FIXED and
    install-wide, so two `tan flash` processes sharing a SETOOLS install
    interleave on them: one can unlink or overwrite the blob while the other
    is signing, or after the other has already paired an address with it. The
    result programmed into on-die MRAM is then a DIFFERENT run's ATOC at this
    run's address, recoverable only by re-provisioning over SE-UART. Locking
    only the subprocess would not be enough -- the pairing is what must be
    atomic -- which is why the copy-out ([`_copy_out_atoc`]) happens inside
    this block too, and why the address is read inside it as well.

    The lock file is CREATED but never unlinked, deliberately: deleting it
    would let the next process create a fresh inode and take a second
    "exclusive" lock on a file the current holder no longer shares. It is an
    empty 0-byte marker; nothing is ever written into it.
    """
    lock_path = os.path.join(setools_dir, _LOCK_REL)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    except OSError as err:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: could not open the SETOOLS sign lock '{lock_path}': {err}"
        ) from err
    try:
        deadline = time.monotonic() + _LOCK_WAIT_S
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise FlashPlanError(
                    f"{FLOW_D_METHOD}: another flash has held the SETOOLS sign lock "
                    f"'{lock_path}' for more than {_LOCK_WAIT_S:.0f}s. {APP_GEN_TOC}'s "
                    "outputs are install-wide, so tan signs one image at a time per "
                    "SETOOLS install -- wait for the other flash to finish, or point "
                    "this one at its own SETOOLS install with --setools-dir."
                )
            time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _copy_out_atoc(setools_dir: str, atoc_blob_path: str, entry_id: str) -> str:
    """Copy the shared `build/AppTocPackage.bin` to a UNIQUE per-run path and
    return that one instead (tan-cli#380).

    The lock above cannot end at `sign_slot0`'s return: the caller hands the
    returned path to J-Link, which reads it minutes later, and the shared blob
    stays mutable that whole time -- the next run unlinks and rewrites it. So
    the guarantee is made immutable rather than long-lived: copy inside the
    lock, hand back the copy. Holding the lock through programming instead
    would serialize unrelated boards for the length of an MRAM write.

    `mkstemp` for the unique name rather than a hand-rolled pid/counter
    scheme: uniqueness against a concurrent run is the entire point, and
    `entry_id` alone is NOT unique across runs -- it is the core id (`m55_he`),
    identical on two boards flashed side by side, which is the likeliest
    concurrency case there is. It is only a prefix here; `validate_identifier`
    has already vetted its charset.
    """
    copy_dir = os.path.join(setools_dir, _ATOC_COPY_REL)
    try:
        os.makedirs(copy_dir, exist_ok=True)
        handle, dest = tempfile.mkstemp(prefix=f"{entry_id}-", suffix=".bin", dir=copy_dir)
        os.close(handle)
        shutil.copyfile(atoc_blob_path, dest)
    except OSError as err:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: {APP_GEN_TOC} produced {atoc_blob_path}, but tan could not "
            f"copy it to a per-run path under '{copy_dir}': {err} -- tan will not hand back "
            "the shared blob, which the next sign in this SETOOLS install overwrites."
        ) from err
    return dest


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
    read back the ATOC placement. Returns `(atoc_copy_path, atoc_address)` --
    an IMMUTABLE per-run copy of the blob, NOT the shared
    `build/AppTocPackage.bin` the tool wrote (tan-cli#380, below).

    Raises `FlashPlanError` -- naming `app-gen-toc`'s own captured output
    where there is any -- on: a filesystem failure preparing the inputs, a
    spawn failure or timeout, a non-zero exit, a successful exit whose
    `app-package-map.txt` was not updated (see tan-cli#373 below) or carries
    no `'APP Package Start Address:'` line, or a successful exit that did not
    actually produce the ATOC blob. SETOOLS' own diagnostic is the
    authoritative one; this does not try to reproduce it, only to surface it.

    **tan-cli#365 (BLOCKER) / tan-cli#373 (BLOCKER regression in #365's own
    fix).** `_ATOC_MAP_REL`/`_ATOC_BLOB_REL` are FIXED, SETOOLS-wide paths --
    not per-`entry_id` like the config/image above -- so a PREVIOUS sign (this
    entry, another entry, a hand-run by the customer) may already have left a
    well-formed report and blob sitting there. Presence/parses-fine after the
    spawn proves nothing about THIS spawn unless a soft failure (app-gen-toc
    exits 0 without actually writing, or dies after partially running) can be
    told apart from a real one.

    #365's own fix told them apart by DELETING both files first -- correct for
    `AppTocPackage.bin` (below), wrong for `app-package-map.txt`:
    `flash_plan.parse_atoc_start_address`'s own docstring documents it as
    **APPEND-mode**, citing the measured bench scripts -- the accumulated sign
    record for the whole SETOOLS install, including hand-runs done outside
    tan, not per-run scratch. Deleting it destroyed that history the moment
    app-gen-toc recreated it holding only THIS run's block: a manifest with a
    second Flow D entry pointing its own `flash_args.atoc_map` at this same
    file would then read back THIS entry's address paired with THAT entry's
    own blob -- a mismatched ATOC burned into on-die MRAM, recoverable only by
    re-provisioning over SE-UART. #373 replaces the unlink with a snapshot
    ([`_map_stat`]): an append changes both `mtime` and `size`, so an
    UNCHANGED snapshot after a zero exit is the same soft-failure signal,
    without deleting anything.

    **tan-cli#380 (BLOCKER, the concurrency half #373 left open).** That
    snapshot guard -- and every other check here -- assumes THIS process is the
    only one touching those install-wide outputs. Two `tan flash` processes
    sharing one `$SETOOLS_DIR` broke that assumption outright: one could unlink
    the blob while the other signed, or overwrite it after the other had
    already paired an address with the path, and the mismatched ATOC went into
    on-die MRAM. Fixed in two halves, both required:
    [`_setools_lock`] makes preparation + spawn + capture one cross-process
    critical section per install, and [`_copy_out_atoc`] takes an immutable
    per-run copy BEFORE that section ends -- so the path handed back is one
    nothing else can touch, carrying the address read in the same section from
    the same signing run.
    """
    validate_identifier(entry_id, "the flash target id")
    setools_dir = os.path.abspath(setools_dir)
    app_gen_toc = os.path.abspath(app_gen_toc)
    images_dir = os.path.join(setools_dir, "build", "images")
    config_dir = os.path.join(setools_dir, "build", "config")
    binary_name = f"{entry_id}.bin"
    config_name = f"{entry_id}-slot0.json"
    atoc_map_path = os.path.join(setools_dir, _ATOC_MAP_REL)
    atoc_blob_path = os.path.join(setools_dir, _ATOC_BLOB_REL)
    # #380: EVERYTHING below is inside the lock -- the prepare, the spawn, the
    # map read and the copy-out. Splitting any of it out re-opens the window.
    with _setools_lock(setools_dir):
        try:
            os.makedirs(images_dir, exist_ok=True)
            os.makedirs(config_dir, exist_ok=True)
            shutil.copyfile(artefact_bin, os.path.join(images_dir, binary_name))
            config_path = os.path.join(config_dir, config_name)
            with open(config_path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(
                    slot0_config(entry_id, binary_name, mram_address, entry_id.upper()),
                    fh,
                    indent=2,
                )
                fh.write("\n")
            # #373: NEVER deleted -- see the docstring above. Snapshotting (not
            # removing) is what lets the post-spawn check below tell "app-gen-toc
            # appended a fresh block" from "app-gen-toc touched nothing" without
            # destroying whatever a prior run (this one's own, another entry's, or
            # a hand-run) already left behind.
            map_before = _map_stat(atoc_map_path)
            # AppTocPackage.bin, unlike the map, is NOT append-mode: app-gen-toc
            # (over)writes the one current blob whole every run, so there is no
            # history in it to lose -- removing it beforehand is a safe
            # presence-after-spawn check on THIS spawn, not a destructive one.
            # #380: and it is now genuinely a check on THIS spawn -- under the
            # lock no other tan process can recreate it between here and the
            # `isfile` below.
            try:
                os.remove(atoc_blob_path)
            except FileNotFoundError:
                pass
        except OSError as err:
            raise FlashPlanError(
                f"{FLOW_D_METHOD}: could not prepare the SETOOLS sign step under "
                f"'{setools_dir}': {err}"
            ) from err

        config_rel = os.path.join("build", "config", config_name)
        try:
            # tan-cli#992: this signs the ATOC that gets written to real
            # silicon -- the same reasoning `flash_cmd._child_env` documents
            # applies here verbatim, so `env=` is never left to inherit this
            # process's (possibly bundle-poisoned) environment.
            proc = subprocess.run(
                [app_gen_toc, "-f", config_rel],
                cwd=setools_dir,
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

        # #373: the soft-failure guard's other half -- a PRE-EXISTING map whose
        # snapshot did not move despite a zero exit was not appended to by THIS
        # spawn, so trusting its last line would report an earlier run's address
        # as this one's. Checked before parsing, so the message names the real
        # problem instead of silently handing back a stale-but-well-formed value.
        if map_before is not None and _map_stat(atoc_map_path) == map_before:
            raise FlashPlanError(
                f"{FLOW_D_METHOD}: {APP_GEN_TOC} exited 0 but {atoc_map_path} was not "
                "updated (size and mtime unchanged) -- the sign step likely did not "
                "actually run; check the SETOOLS config, or sign by hand."
            )
        address = read_atoc_address(setools_dir)
        if address is None:
            raise FlashPlanError(
                f"{FLOW_D_METHOD}: {APP_GEN_TOC} exited 0 but "
                f"{atoc_map_path} carries no 'APP Package Start "
                "Address:' line -- check the SETOOLS config, or sign by hand."
            )
        if not os.path.isfile(atoc_blob_path):
            raise FlashPlanError(
                f"{FLOW_D_METHOD}: {APP_GEN_TOC} exited 0 and reported an address, but "
                f"{atoc_blob_path} was not produced -- check the SETOOLS output."
            )
        # #380: the address above and this copy come from the SAME signing run,
        # both inside the lock -- pairing them is the whole point.
        return _copy_out_atoc(setools_dir, atoc_blob_path, entry_id), address
