# SPDX-License-Identifier: Apache-2.0
"""`tan build` -- the one command that turns a build plan into firmware.

Composition, not logic: this module resolves the project, ACQUIRES a plan (from
`--plan-from`, else by running the planner in-process -- there is no other way any
more; see `_emit_plan`), then hands it in turn to the four sub-project-1
libraries -- `parse_build_plan`, `apply_plan_token_substitution`,
`materialise_plan`, `execute_slices` -- and
folds the result into one envelope. Every decision those modules own stays
theirs; what lives here is the ORDER they run in, the mapping from their
`(code, message)` failures to an exit code, and the guarantee that not one of
them can escape as a traceback.

Three properties this file exists to hold:

**Order is the contract (I-20).** `materialise_plan` writes every
`sharedArtefacts` entry AND every slice's `configArtefacts` before
`execute_slices` is called at all. The plan carries no `sequential` flag and
no `inputHash`; the only thing making its slices independent is that all their
inputs are on disk before the first one starts. `tests/commands/
test_build_command.py` proves it by making the first slice's own command
assert the OTHER slice's config artefact already exists.

**Nothing but the envelope on stdout.** Slice output streams to stderr (see
`_stream`), the text-mode recap goes to stderr, and the JSON envelope is the
only thing ever written to stdout. The extension parses stdout whole; one
stray byte and it renders nothing, with no error on either side.

**`--plan-from` is a plan SOURCE, not a build trigger (v0.4.1 §gate).** The
oracle's `--plan-from` help says "Implies `--plan`", and measured against the
shipped `tan 0.4.1-dev` binary in fresh temp dirs, on
`tests/parity/oracle/multicore_rpmsg-aen.build-plan.json`, it means it -- even
against an EXPLICIT `--native`::

    build --plan-from p.json                -> rc 0, 0 files, data = the plan
    build --plan-from p.json --native       -> rc 0, 0 files, data = the plan
    build --plan-from p.json --materialise  -> rc 0, 6 files,
                                               data = {schemaVersion, baseDir, written}

All three are reproduced here exactly (`_MODE_PLAN` / `_MODE_MATERIALISE`),
`--native` included. Two earlier revisions of this file each redefined one of
those argvs -- a bare `--plan-from` used to materialise AND dispatch, and then
`--plan-from --native` did -- so a v0.4.1 script that only ever INSPECTED a
plan silently began writing six files into its own tree. Changing what a
v0.4.1 argv MEANS is the whole surprise this gate exists to remove.

**`--execute` is a deliberate PORT ADDITION the oracle has no flag for.**
Taking a pinned, reviewed plan file and running it reproducibly is a normal
hermetic-CI request; v0.4.1's inability to do it is a LIMITATION of that CLI,
not a contract worth preserving -- there `--plan-from`'s implied `--plan`
outranks `--native`, so a file-supplied plan cannot be dispatched at all.
`--execute` supplies exactly that capability without changing what any v0.4.1
argv means. It is not a parity bug and not a test hook: a future reader who
"fixes" it by deleting it removes a supported capability. Its `data` is the
ORDINARY build shape -- `{schemaVersion, baseDir, slices[], warnings[]}` --
because it IS a native build and only the plan's SOURCE differs; `written` is
deliberately omitted, being byte-for-byte the pinned plan's own declared
artefact paths, which the one caller who has that file already holds. A
conflicting combination is refused with its own code, never resolved by silent
precedence (`--execute --materialise` -> `build.conflicting-flags`, exit 2;
`--execute` with a deferred flag -> `cli.command-deferred`, exit 1, because a
flag this build does not implement at all cannot be honoured either way).
`--execute` is ADDED BY THIS PORT, not a v0.4.1 flag: there `--plan-from`
implies `--plan` and outranks `--native`, so a file-supplied plan cannot be
dispatched at all. Deliberate, not a parity gap. `--native` keeps v0.4.1's
behaviour of NOT overriding the `--plan` that `--plan-from` implies, which
`test_plan_from_shows_the_plan_and_writes_nothing_even_with_native` pins.

**The OS is not an option (I-01/I-02).** There is deliberately no `--os` and
no `--backend` flag. A core's runtime is derived from its Cortex class by the
planner (`tan/planner/topology.py`), and `slices[].backend`
arrives already resolved. Nor does this command filter the plan's slices: a
one-core `board.yaml` legitimately plans three (I-04), and "helpfully"
dropping the ones the customer did not name is how a Yocto slice silently
stops being built.
"""
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

import typer

from tan.commands.build.execute import (
    KNOWN_BACKENDS,
    SliceOutcome,
    execute_slices,
    last_manifest_write_failure,
    last_sdk_switch_issues,
    reset_last_manifest_write,
)
from tan.commands.build.materialise import MaterialiseError, materialise_plan
from tan.commands.build.token_substitution import (
    TokenSubstitutionError,
    apply_plan_token_substitution,
)
from tan.commands.deferred_cmd import DEFERRED_ISSUE_CODE, DEFERRED_ISSUE_URL
from tan.commands.sdk_cmd import (
    global_default_foreign_project_issue,
    project_pin_issue,
    resolve_sdk_tiered,
)
from tan.core.build_plan import BuildPlan, PlanParseError, parse_build_plan
from tan.core.plan_exec import PolicyAction, normalize_path, resolve_action
from tan.core.shapes import SDK_MARKER, is_sdk_root
from tan.core.venv import venv_python
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

#: `scripts/alp_project.py` is THE marker for an alp-sdk checkout -- the same
#: literal `tan_core::project` hardcodes (I-31). Renaming or relocating it
#: breaks every `--sdk-root` resolution in the CLI, so it is spelled once --
#: in `tan.core.shapes` since tan-cli#408. The import below re-exports it
#: here because a dozen modules already reach it through this one.

#: Envelope `data.slices[].status`, from `SliceOutcome.status`. The wire
#: vocabulary is the Rust one (`ok` / `skipped` / `failed`), NOT the executor's
#: internal spelling -- `succeeded` on the wire would break a consumer written
#: against the shipped binary. `cancelled` is unreachable from this command
#: (nothing here supplies a cancellation source) but maps to `failed` rather
#: than falling through to a KeyError if one is ever wired up.
_WIRE_STATUS = {"succeeded": "ok", "skipped": "skipped"}

#: What this run does with the plan it acquires -- the oracle's own precedence,
#: measured flag-combination by flag-combination against `target/debug/tan.exe`
#: on the AEN fixture: `--materialise` outranks everything (`--plan-from
#: --materialise --native` -> six files, `written` shape), the `--plan` implied
#: by `--plan-from` outranks even an explicit `--native` (`--plan-from
#: --native` -> the plan, zero files), and only a run naming NEITHER plan-mode
#: flag builds. `_MODE_NATIVE` is therefore both the default and what the
#: port-added `--execute` selects -- deliberately the SAME mode, so `--execute`
#: reports the same `data` shape a plain `tan build` does rather than a fourth
#: one no consumer is written for.
_MODE_PLAN = "plan"
_MODE_MATERIALISE = "materialise"
_MODE_NATIVE = "native"

#: Flags the oracle's `tan build` declares and this port does not implement,
#: in the order a run naming several of them is refused. Declaring them is the
#: whole point: an undeclared flag is a Click `UsageError` -- exit 2,
#: `cli.parse-error`, and a message indistinguishable from `tan build
#: --pristien`. Registering them turns "known, deferred" into its own coded
#: answer at exit 1, exactly as `deferred_cmd` does for the seven deferred
#: VERBS, whose `cli.command-deferred` code and issue URL are reused
#: rather than forked (a caller special-casing deferral needs one code to
#: match, not two).
#:
#: `--verbose`/`--quiet`/`--no-color`/`--non-interactive`/`--ci` are declared
#: HERE, build-local, only because this port accepts none of them at the root
#: today (measured: `tan --verbose build ...` -> exit 2, "No such option");
#: in the oracle all five are clap `global = true` args declared once on the
#: root and propagated. The moment `cli.py` grows a real root-level
#: implementation these five must be DELETED from here, or a build-local
#: declaration would shadow it.
_DEFERRED_FLAGS = (
    "--plan",
    "--target",
    "--all",
    "--manifest",
    "--manifest-from",
    "--no-auto-bootstrap",
    "--pristine",
    "--verbose",
    "--quiet",
    "--no-color",
    "--non-interactive",
    "--ci",
)

#: `--help` text for every one of them -- one string, because they all report
#: the same fact and a per-flag reason would be twelve things to keep true.
# No version number: this said "Deferred to v0.6.0" while the release it
# meant was renumbered to 0.5.0, and a help string that names the release a
# flag will appear in is a promise tan cannot keep true. The issue link is
# the durable pointer.
_DEFERRED_HELP = "Accepted by other commands; not implemented for `build` yet (tan-cli#427)."


class BuildError(Exception):
    """A build failure with its issue code and exit code already decided --
    the shape every step in `_build` reports through, so the caller never has
    to guess a code from an exception type."""

    def __init__(self, code: str, message: str, exit_code: ExitCode) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _stream(line: str) -> None:
    """Slice output goes to stderr -- ALWAYS, both formats. stdout is the
    envelope channel and carries nothing else; stderr carries no contract at
    all, so there is nothing to keep separate between the two modes."""
    print(line, file=sys.stderr, flush=True)


#: Idle time -- since the last line ANY slice printed, or since dispatch
#: began if none has arrived yet -- before [`_Heartbeat`] starts announcing
#: it is still alive. Measured against tan-cli#287: a first-build CMake
#: configure sat fully silent for 234.2s; a warm-cache rebuild (21.1s) prints
#: output throughout and never crosses this. Short enough to catch the
#: former, long enough that ordinary tool-startup chatter never trips it.
_HEARTBEAT_SILENCE_THRESHOLD_S = 5.0
#: Refresh cadence once armed -- a live, in-place counter (`\r`, no
#: trailing newline), not a growing log.
_HEARTBEAT_TICK_S = 1.0
#: `shutil.get_terminal_size`'s own fallback when stderr's column count
#: can't be read. Should not be reachable -- `_Heartbeat` is only ever armed
#: behind an `isatty()` gate -- but naming a value here is still cheaper
#: than trusting that gate never has a gap.
_HEARTBEAT_WIDTH_FALLBACK = 80


def _heartbeat_line_width() -> int:
    """Usable columns for one self-overwriting heartbeat line: the
    terminal's own width, minus one.

    tan-cli#287 blocker: the erase pad used to be a HARDCODED 88 columns
    while the message it erased ran 114+ chars (more once elapsed hits
    1000s). On a terminal narrower than 114 columns -- 80 is the common
    case -- `\r` rewinds only the terminal's OWN last row, so a message
    that wraps grows a fresh row every tick instead of overwriting one:
    ~234 stacked "still building" rows over the measured 234.2s configure.
    Sizing the message AND its erase pad off the actual terminal width,
    every tick, removes the mismatch instead of guessing a bigger constant.
    The `- 1` leaves the last column unwritten: filling a line to the exact
    terminal width is what triggers autowrap on many terminals, which would
    make the fix for the wrap defect one more way to trigger it.

    # ponytail: re-reads the width every tick rather than caching it, so a
    # terminal resized mid-build can leave a stale tick's tail past the
    # newly narrower edge; not the reported defect (a STATIC undersized
    # pad on an unchanged terminal) and not measured here -- revisit if a
    # resize-mid-build report ever lands.
    """
    columns = shutil.get_terminal_size(fallback=(_HEARTBEAT_WIDTH_FALLBACK, 24)).columns
    return max(columns - 1, 1)


class _Heartbeat:
    """The `on_output` callback `_dispatch` constructs and hands
    `execute_slices` directly: relays every line to [`_stream`] like
    before, and -- TTY + text-mode only -- prints a single self-overwriting
    "still building" line once the child has gone quiet for
    `_HEARTBEAT_SILENCE_THRESHOLD_S` (tan-cli#287: a four-minute silent
    CMake configure read as a hang with nothing to say otherwise). The
    instant real output resumes, the line is blanked so it never mixes
    with the stream it was standing in for.

    Armed by [`_dispatch`] itself, not handed down by its caller: `_build`'s
    two callers -- `build_cmd.build` and `run_cmd._build_then_run` -- both
    bottleneck through `_dispatch` before any slice runs, so constructing
    the heartbeat there covers `tan run`'s identical silent-CMake window
    too, not just a direct `tan build` (tan-cli#287 follow-up).

    `execute_slices` calls `on_output` once per line and has no hook for
    "a slice started" or "configure ended, compile began" -- adding one
    would mean editing `tan.commands.build.execute`, outside this file's
    scope -- so this cannot name PHASES, only measure silence across the
    whole dispatch and say tan is still alive.

    # ponytail: one wall-clock timer for the whole dispatch, not a per-slice
    # or per-phase one -- upgrade to a per-slice "building <core>" line if
    # `execute_slices` ever grows a slice-start hook.

    A disabled instance (`enabled=False` -- not a TTY, or `--format json`)
    still relays every line via `__call__`, but never starts its thread and
    so never prints anything of its own: behaviourally identical to passing
    `_stream` straight through."""

    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled
        self._start = time.monotonic()
        self._last_output = self._start
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._line_up = False

    def __call__(self, line: str) -> None:
        # Clear any live heartbeat line FIRST -- `_stream`'s `print` commits
        # a trailing newline, so blanking AFTER it would scroll the
        # heartbeat text away merged onto the same line as real output
        # instead of erasing it.
        self.note_output()
        _stream(line)

    def note_output(self) -> None:
        with self._lock:
            self._last_output = time.monotonic()
            self._clear_locked()

    def _clear_locked(self) -> None:
        if self._line_up:
            width = _heartbeat_line_width()
            print("\r" + " " * width + "\r", end="", file=sys.stderr, flush=True)
            self._line_up = False

    def _tick(self) -> None:
        while not self._stop.wait(_HEARTBEAT_TICK_S):
            with self._lock:
                idle = time.monotonic() - self._last_output
                if idle < _HEARTBEAT_SILENCE_THRESHOLD_S:
                    continue
                elapsed = time.monotonic() - self._start
                width = _heartbeat_line_width()
                # Truncated AND right-padded to `width`, both to the SAME
                # value: truncation keeps this tick's own text off the
                # terminal's last column (see `_heartbeat_line_width`), and
                # the pad overwrites a longer PRIOR tick's tail (elapsed/
                # idle growing from single to double digits, say) -- both
                # ticks compute the same width, so the pad always covers it
                # on an unchanged terminal.
                message = (
                    f"... still building ({elapsed:.0f}s elapsed, no output for "
                    f"{idle:.0f}s -- CMake configure can take several minutes on a "
                    "first build)"
                )[:width]
                print("\r" + message.ljust(width), end="", file=sys.stderr, flush=True)
                self._line_up = True

    def __enter__(self) -> "_Heartbeat":
        if self._enabled:
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_HEARTBEAT_TICK_S * 2)
        with self._lock:
            self._clear_locked()


def _abs_posix(path: str) -> str:
    """Rust's `normalize_path(cwd.join(p))` + `to_posix`: cwd-anchored,
    lexically normalised, forward slashes.

    `os.path.abspath`, NOT `Path.resolve()` -- abspath is purely lexical, so a
    project reached through a symlink keeps the name the user typed, and a path
    that does not exist yet still resolves. `resolve()` would rewrite both, and
    the divergence guard compares STRINGS: rewriting one side's symlink but not
    the other's is exactly how that guard starts firing on a healthy project.

    Forward slashes because the result is substituted into `${PROJECT_ROOT}`
    and lands in a CMake `-D` argument, where a Windows backslash is an escape.
    """
    return os.path.abspath(path).replace("\\", "/")


#: tan-cli#408: one implementation, in `tan.core.shapes`. Three modules
#: carried a private copy of this and they had already drifted in TYPE --
#: this one took a `Path`, `flash_cmd`'s and `renode_cmd`'s took a `str`.
#: Imported under the same private name so no call site here moves.
_is_sdk_root = is_sdk_root


def discover_sdk_root(workspace_root: Path) -> Path | None:
    """Find an alp-sdk checkout near `workspace_root`, mirroring Rust's
    `discover_sdk_root` (`crates/tan-cli/src/util.rs`) candidate for
    candidate: the root itself, then its CHILD `alp-sdk/`, then the sibling
    `../alp-sdk`, then `../alp-sdk-upstream`, and only if none of those hit,
    the nearest ENCLOSING checkout.

    The child comes before the siblings deliberately (tan-cli #218): `tan
    bootstrap` clones into `<ws>/alp-sdk`, so at that moment the checkout is a
    CHILD of the cwd -- it only becomes the documented sibling once the user
    has cd'd into a project. Discovery that checks root, siblings and
    ancestors but not a child reports "alp-sdk root is unresolved" with the
    checkout sitting right there.

    A fixed candidate list, never a directory scan: probing `iterdir()` up the
    whole ancestor chain reads a developer's entire home and drive root, which
    is both slow and non-hermetic (it lets an unrelated checkout elsewhere on
    the machine decide what a test resolves).

    Deliberately just the positional walk, not the full ladder -- the project
    pin and machine-global default tiers that outrank this one live in
    [`resolve_sdk_root_ladder`], below, which every `--sdk-root`-less caller
    should use instead of calling this directly (this function remains that
    ladder's own final fallback tier, and the one place that still calls it
    alone is `tan-cli#218`'s own regression test).
    """
    parent = workspace_root.parent
    for candidate in (
        workspace_root,
        workspace_root / "alp-sdk",
        parent / "alp-sdk",
        parent / "alp-sdk-upstream",
    ):
        if _is_sdk_root(candidate):
            return candidate
    for ancestor in workspace_root.parents:
        if _is_sdk_root(ancestor):
            return ancestor
    return None


@dataclass(frozen=True)
class SdkRootResolution:
    """`resolve_sdk_root_ladder`/`resolve_sdk_root_wide`'s own return type --
    a NAMED result rather than a tuple, so a fact one caller needs (e.g.
    `foreign_global_default_for`, tan-cli#464) never forces every OTHER
    caller's unpacking to grow a matching positional slot. A four-element
    tuple was tried here first and rejected on review: it would have
    reproduced the exact silent-drop shape tan-cli#464 exists to close, at
    the next field this ladder needs to carry.

    Mirrors `sdk_cmd.ActiveSdk`'s same four facts (this IS `ActiveSdk` for the
    two tiers it cannot see -- `discovery`/`none` -- carried through as a
    distinct type only because `.path` here stays `Path`, the shape every
    filesystem-touching caller of these two ladders already expects, where
    `ActiveSdk.path` is the raw `str` its own callers compare and serialize
    verbatim)."""

    path: Path | None
    tier: str
    broken_project_pin: str | None = None
    foreign_global_default_for: str | None = None


def resolve_sdk_root_ladder(sdk_root_arg: str | None, workspace_root: Path) -> SdkRootResolution:
    """The NARROW of this port's TWO discovery ladders,
    shared by the thirteen commands the oracle resolves narrowly (`build`,
    `doctor`, `clean`, `run`, `flash`, `size`, `image`, `kconfig`, `validate`,
    `presets`, `inspect`, `trace`, `sdk current`). The other four -- `init`,
    `generate`, `examples`, `renode` -- take [`resolve_sdk_root_wide`], below.
    The measurement that splits 13 from 4 is the last paragraph here, and is
    deliberately not repeated there.

    `--sdk-root` (terminal, I-31) > the project's own pin (`.alp/sdk-path`,
    written by `tan init` / relocated by `tan bootstrap`) > the machine-global
    default (`~/.alp/sdk-default`) > `resolve_sdk_tiered`'s own NARROW
    discovery probe > the wide positional walk (`discover_sdk_root`, above) as
    the tail -- exactly the Rust oracle's closed `SdkSourceTier`
    (`crates/tan-core/src/sdk.rs`): `SdkRootFlag`, `ProjectPin`,
    `GlobalDefault`, `Discovery`, `None`. No sixth tier. Both ladders report
    those same five values; only the discovery tier differs between them.

    This is the fix for the worst reported CX defect in this port: `tan init`
    writes `.alp/sdk-path` into the project it just scaffolded, but every
    command below used to jump straight from `--sdk-root` to the positional
    walk, skipping `resolve_sdk_tiered` (`sdk_cmd.py`) entirely -- so that
    pointer was silently ignored the moment `tan build` ran in that SAME
    directory, unless the checkout also happened to sit beside the project by
    coincidence. `resolve_sdk_tiered` already IS the oracle's `--sdk-root` >
    project pin > global default > discovery chain; the only thing missing
    from it here is the WIDER positional walk as one more fallback below
    `discovery`, for callers ported before `resolve_sdk_tiered` existed.

    An `ALP_SDK_ROOT` env-var tier was tried here and reverted: the oracle's
    `util.rs::resolve_sdk_root` only ever WRITES that variable into a build
    slice's env, never reads it back for discovery, and `SdkSourceTier` has no
    slot for it -- a sixth `sourceTier` value in the envelope is a wire
    contract change no consumer (the vscode extension, `tan sdk current
    --json`) expects. The project-pin tier above already makes `tan init &&
    tan build` compose without it.

    **The narrow tier short-circuiting the wide one is deliberate, and
    measured** (tan-cli#263, which proposed inverting it): the wide walk puts
    the CHILD `<ws>/alp-sdk` ahead of the lateral `../alp-sdk`, so hoisting it
    above `resolve_sdk_tiered` would flip every workspace holding both. Driven
    through the oracle binary in constructed layouts, thirteen of its commands
    -- `build`, `doctor`, `clean`, `run`, `flash`, `size`, `image`, `kconfig`,
    `validate`, `presets`, `inspect`, `trace`, `sdk current` -- resolve the
    LATERAL one there, i.e. the narrow order this ladder already has; only
    `init`, `generate`, `examples` and `renode` take the child. The oracle
    carries two resolutions, not one, and this ladder mirrors the majority
    (narrow) one plus the wide walk as its tail. Inverting it to match the
    other four would move the SDK root under thirteen commands, and a moved
    root is what `plan_exec.sdk_stamp_action` reads as a switch: every existing
    such workspace would take the `build.sdk-switch-pristine` branch on its
    next build and lose every slice's build dir. The four wide commands were
    the real gap; they now have their own helper below, which is why this one
    stays exactly as it is.

    `SdkRootResolution.broken_project_pin` (tan-cli#263 review) is
    `ActiveSdk.broken_project_pin` carried straight through: the raw
    `.alp/sdk-path` target when that file exists but its target failed the
    loader check, `None` otherwise -- even when a LOWER tier (global default,
    discovery, or nothing) is what this call actually returns.
    `sdk_cmd.project_pin_issue` turns it into the shared
    `sdk.project-pin-unresolved` warning; every caller here should surface it,
    not just `sdk current` -- `tan build` is the one that matters most, since
    it is the command that silently builds against whichever SDK the pin's
    fallthrough landed on. `foreign_global_default_for` (tan-cli#464) is the
    same carry-through for `ActiveSdk.foreign_global_default_for`, and
    `sdk_cmd.global_default_foreign_project_issue` is its sibling warning.
    """
    flag = (sdk_root_arg or "").strip()
    if flag:
        return SdkRootResolution(Path(flag), "sdkRootFlag")

    tiered = resolve_sdk_tiered(None, workspace_root)
    if tiered.path is not None:
        return SdkRootResolution(
            Path(tiered.path),
            tiered.tier,
            tiered.broken_project_pin,
            tiered.foreign_global_default_for,
        )

    found = discover_sdk_root(workspace_root)
    if found is not None:
        return SdkRootResolution(found, "discovery", tiered.broken_project_pin)
    return SdkRootResolution(None, "none", tiered.broken_project_pin)


def resolve_sdk_root_wide(sdk_root_arg: str | None, workspace_root: Path) -> SdkRootResolution:
    """The WIDE ladder, for the four commands the oracle routes through
    `util.rs`'s `resolve_sdk_root`: `init`, `generate`, `examples`, `renode`.
    Same [`SdkRootResolution`] shape as [`resolve_sdk_root_ladder`], same
    field meanings.

    Same tiers and the same five `SdkSourceTier` values as
    [`resolve_sdk_root_ladder`] above -- `--sdk-root` > project pin > global
    default > discovery -- with exactly one difference: the discovery tier is
    the WIDE positional walk (`discover_sdk_root`) rather than
    `resolve_sdk_tiered`'s narrow probe, so the CHILD `<ws>/alp-sdk` wins over
    the lateral `../alp-sdk` and over an enclosing checkout. The measurement
    behind which command belongs to which ladder lives in that docstring; two
    copies of it would be two things to keep true.

    `tan init` is why this is a second helper and not a tidy-up waiting to be
    folded back in (tan-cli#263). In a workspace holding both a child checkout
    and a competing sibling, the oracle pins the CHILD into `.alp/sdk-path`;
    the narrow ladder pinned the sibling. That pointer then outranks discovery
    for every later command in that project, so the wrong SDK is not a
    one-command wobble -- it is bound permanently, by the command whose whole
    job was to bind the right one.

    Skipping the narrow discovery tier can never resolve LESS than the narrow
    ladder would: its candidates (the workspace root itself, `../alp-sdk`, the
    nearest enclosing checkout) are a subset of the wide walk's, so wherever
    narrow hits, wide hits too -- it may only pick a nearer checkout.
    """
    flag = (sdk_root_arg or "").strip()
    if flag:
        return SdkRootResolution(Path(flag), "sdkRootFlag")

    tiered = resolve_sdk_tiered(None, workspace_root)
    if tiered.path is not None and tiered.tier != "discovery":
        return SdkRootResolution(
            Path(tiered.path),
            tiered.tier,
            tiered.broken_project_pin,
            tiered.foreign_global_default_for,
        )

    found = discover_sdk_root(workspace_root)
    if found is not None:
        return SdkRootResolution(found, "discovery", tiered.broken_project_pin)
    return SdkRootResolution(None, "none", tiered.broken_project_pin)


#: tan-cli#407's warning code. Namespaced `sdk.` and not `build.`/`generate.`
#: on purpose: a caller on EITHER ladder emits this identical string, so a
#: consumer holding one envelope from each side matches them on the code, and
#: a per-command spelling would make the pair it has to correlate the one
#: thing that differs.
SDK_DISCOVERY_DIVERGENT = "sdk.discovery-divergent"

#: Both command groups spelled out in the warning itself, because the reader
#: is being told their toolchain is split across two checkouts and the useful
#: next question -- "which of my commands went where?" -- must not require
#: reading this source.
_NARROW_COMMANDS = (
    "build, doctor, clean, run, flash, size, image, kconfig, validate, "
    "presets, inspect, trace and sdk current"
)
_WIDE_COMMANDS = "init, generate, examples and renode"


def sdk_ladder_divergence_issue(
    sdk_root_arg: str | None, workspace_root: Path, *, wide: bool
) -> Issue | None:
    """The tan-cli#407 warning for a workspace where the two ladders above
    answer DIFFERENT checkouts -- `None` (the overwhelmingly common case)
    when they agree, so every call site can do `issue =
    sdk_ladder_divergence_issue(...); if issue: issues.append(issue)`
    unconditionally, exactly like `sdk_cmd.project_pin_issue`.

    The divergence itself is deliberate and oracle-measured (see
    [`resolve_sdk_root_ladder`]'s own docstring, and
    `tests/commands/test_build_manifest.py`), and this does not try to remove
    it. What it removes is the SILENCE: both ladders label their answer with
    the same `SdkSourceTier` string, `"discovery"`, so in a workspace holding
    both a child `<ws>/alp-sdk` and a lateral `../alp-sdk`, `tan generate`
    emits `build/generated/alp.conf`, the DTS overlays and
    `alp_hw_info_build.h` from one checkout's `metadata/` while `tan build`,
    `tan size` and `tan trace` plan against the other -- and both envelopes
    say `discovery`, leaving no field a consumer could compare. `sdk: {root,
    sourceTier}` exists (#110) precisely so the vscode extension can tell
    which SDK produced a result instead of guessing; one tier name for two
    answers is that key failing at its only job.

    A sixth `SdkSourceTier` value would be the other way to say this and is
    rejected upstream for the same reason [`resolve_sdk_root_ladder`] rejects
    an `ALP_SDK_ROOT` tier: the closed enum is a wire contract no consumer
    expects to grow, and `test_build_manifest.py` pins the exact
    `SdkRootResolution(path, "discovery", None, None)` both ladders return
    here because that is the oracle's own answer. An `issues[]` entry adds a
    fact without moving a reported one -- the same trade `broken_project_pin`
    already makes.

    `wide` only picks which side of the pair is labelled "this command"; both
    sides name both checkouts, in the same order, so two envelopes describing
    one collision read as one collision rather than two unrelated warnings.

    Both roots are compared as `_abs_posix` strings rather than as `Path`s
    because that is the spelling the message quotes and the spelling
    `sdk.root` carries on the wire -- comparing anything else risks reporting
    a "divergence" between two names for one directory. Safe here precisely
    because both ladders derive every candidate from the same
    `workspace_root`: the tiers ABOVE discovery (`--sdk-root`, project pin,
    global default) are shared verbatim, so they can never be the pair that
    differs.
    """
    narrow_path = resolve_sdk_root_ladder(sdk_root_arg, workspace_root).path
    wide_path = resolve_sdk_root_wide(sdk_root_arg, workspace_root).path
    # One side unresolved is not a collision to disambiguate: the two
    # envelopes already differ by `sourceTier` (`none` vs something), which is
    # the very signal this warning exists to supply.
    if narrow_path is None or wide_path is None:
        return None

    narrow = _abs_posix(str(narrow_path))
    wider = _abs_posix(str(wide_path))
    if narrow == wider:
        return None

    narrow_label = _NARROW_COMMANDS if wide else "this command"
    wide_label = "this command" if wide else _WIDE_COMMANDS
    return Issue(
        SDK_DISCOVERY_DIVERGENT,
        "warning",
        f"two alp-sdk checkouts resolve from this directory and both report "
        f'sourceTier "discovery": {narrow_label} uses "{narrow}", while '
        f'{wide_label} uses "{wider}". Generated files and the build plan can '
        f"therefore come from different SDK versions -- pin the one you mean "
        f"with --sdk-root, or with `tan init --sdk-root <path>` to write it "
        f"into .alp/sdk-path, which outranks both discovery tiers.",
    )


def _planner_python(start: str, sdk_root: str | None) -> str:
    """The interpreter a SPAWNED build step runs under.

    Two callers remain now that planning and every `generate` target run
    in-process: `${PYTHON}` token substitution (below), and
    `generate_cmd`'s `TAN_GENERATE_EXECUTOR=subprocess` escape hatch.

    Prefers the west-capable workspace venv's `python`
    (`tan.core.venv.venv_python`, mirroring Rust's `venv_python`/
    `resolved_planner_python`), because the SDK planner bakes ITS
    `sys.executable` into every Zephyr slice as `-DPython3_EXECUTABLE=<...>`
    (alp-sdk#787) -- a bare PATH `python3` may lack the `west` module
    entirely, which surfaced as the planner's own `ModuleNotFoundError: No
    module named 'west'` inside CMake configure (tan-cli, the
    documented-install-path bug) rather than a working build.

    Falls back to a bare PATH name -- `python3` off Windows, `python` on it,
    mirroring `tan_core::project::resolve_python_binary` -- only when no
    workspace venv resolves. NOT `sys.executable`: frozen by PyInstaller,
    `sys.executable` is `tan` itself, so spawning it would just re-enter this
    CLI; and this value is also the `${PYTHON}` substituted into the plan, so
    it has to name an interpreter the slice can find, not this process.
    """
    return venv_python(start, sdk_root) or ("python" if os.name == "nt" else "python3")


def _emit_plan(sdk_root: str | None, board_yaml: str | None) -> str:
    """Plan `board_yaml` against the SDK at `sdk_root`.

    IN-PROCESS, always. `tan.planner_root.emit` binds the SDK root -- which is
    where `metadata/**` and the fact-reader modules still live (ADR-0017) -- and
    renders through the same `emit_artefact` dispatch `python -m tan.planner`
    uses. No interpreter on PATH, no PYTHONPATH, no process, and nothing under
    `<sdk>/scripts` imported or spawned.

    **The `<python> -m alp_orchestrate --emit build-plan` fallback was RETIRED
    here, deliberately.** It fired when this build of `tan` could not import the
    planner and quietly planned through the SDK's own copy instead -- and it
    reported nothing at all when it did. Three reasons it had to go, any one
    sufficient:

    * It was SILENT. A run that fell back looked exactly like a run that did
      not, which is the same defect class that let the planner relocation be
      believed before it happened.
    * It could only fire on a broken install. `jsonschema` and `PyYAML` are
      declared `dependencies` in `pyproject.toml` and installed into the clean
      venv `scripts/build_binary.sh` freezes, so the trigger is not a supported
      layout -- and a missing dependency deserves `pip install jsonschema`, not
      a different planner.
    * It kept alp-sdk's `scripts/alp_orchestrate/` load-bearing forever. As long
      as any tan path can reach it, it can never be deleted, and deleting it is
      the point.

    What replaces it is a coded refusal naming the missing dependency -- which is
    strictly more actionable than a silent success against a second planner whose
    version nothing checks.
    """
    if sdk_root is None:
        raise BuildError(
            "build.plan-unavailable",
            "no alp-sdk checkout found -- pass `--sdk-root <PATH>` or run from a project "
            "beside one. Planning reads the SDK's `metadata/**`. Run `tan doctor` to see "
            "which checkout tan resolves, if any.",
            ExitCode.RUNTIME_FAILURE,
        )
    if board_yaml is None:
        raise BuildError(
            "build.plan-unavailable",
            "no board.yaml found -- pass `--board-yaml <PATH>` or run from a project. "
            "Run `tan init` to create one, or `tan examples` to list ready-made projects.",
            ExitCode.RUNTIME_FAILURE,
        )
    # `--sdk-root` is terminal and returned as-is even when wrong (I-31), so the
    # first thing planning reads is checked here rather than surfacing as a
    # loader stack trace. The board schema, not the planner: the planner is ours
    # now, and requiring the SDK to still carry `alp_orchestrate` would refuse
    # precisely the releases this relocation exists to enable.
    if not (Path(sdk_root) / "metadata" / "schemas" / "board.schema.json").is_file():
        raise BuildError(
            "build.plan-unavailable",
            f"the SDK at `{sdk_root}` has no `metadata/schemas/board.schema.json` -- "
            f"that path is not an alp-sdk checkout, or the checkout is incomplete.",
            ExitCode.RUNTIME_FAILURE,
        )

    try:
        from tan.planner_root import emit as _plan_emit
    except ImportError as err:  # pragma: no cover -- planner absent from this build
        raise BuildError(
            "build.plan-unavailable",
            f"this build of `tan` cannot import its own planner ({err}). "
            "Reinstall it, or install the planner's dependencies "
            "(`pip install pyyaml jsonschema`).",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    try:
        return _plan_emit("build-plan", root=sdk_root, board_yaml=Path(board_yaml))
    except ImportError as err:
        # A planner dependency (`jsonschema`, `PyYAML`) is missing from THIS
        # build. Named, not worked around: see the docstring on why the
        # spawn-the-SDK's-planner fallback was retired.
        raise BuildError(
            "build.plan-unavailable",
            f"the planner could not be imported ({err}) -- install its "
            "dependencies (`pip install pyyaml jsonschema`) or reinstall `tan`.",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    except SystemExit as err:
        # `tan.planner.loader` / `kconfig_symbols` call `sys.exit()` on a missing
        # dependency or an unbootstrapped Zephyr workspace. In a subprocess that
        # was a non-zero rc; in-process it would tear down the whole CLI past
        # every envelope guarantee.
        raise BuildError(
            "build.plan-unavailable",
            f"the build-plan emit exited early (code {err.code}).",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    except Exception as err:
        # Every planner failure is an envelope, never a traceback -- the same
        # thing the subprocess boundary used to buy for free. Includes the bare
        # `ValueError`s the Ethos-U sizing path still raises (I-48).
        raise BuildError(
            "build.plan-unavailable",
            f"the build-plan emit failed: {type(err).__name__}: {err}",
            ExitCode.RUNTIME_FAILURE,
        ) from err


def _acquire_plan(
    plan_from: str | None, sdk_root: str | None, board_yaml: str | None
) -> tuple[str, BuildPlan]:
    """`(source text, parsed plan)`. The TEXT comes back too because
    `_MODE_PLAN` echoes it: the oracle's plan output is a raw passthrough, not
    a round-trip through its own structs -- measured by adding an unknown
    top-level key to the fixture, which survives verbatim into `data`. Echoing
    a re-serialised `BuildPlan` would silently drop exactly the forward-compat
    keys a newer SDK adds, which is the drift the version-skew guard exists to
    make loud."""
    if plan_from is not None:
        try:
            text = Path(plan_from).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            # UnicodeDecodeError is a ValueError, NOT an OSError, so `except
            # OSError` alone let it fall through to the catch-all and report a
            # bad INPUT as a tan bug (`build.internal-failure`, exit 5).
            # Reachable on the exact flow `--plan-from` exists for: `tan build
            # --plan --format json > plan.json` in Windows PowerShell 5.1
            # writes UTF-16LE, and this is the replay half of that
            # capture-then-replay loop.
            raise BuildError(
                "build.plan-unavailable",
                f"failed to read plan file `{plan_from}`: {err}",
                ExitCode.RUNTIME_FAILURE,
            ) from err
    else:
        text = _emit_plan(sdk_root, board_yaml)

    try:
        return text, parse_build_plan(text)
    except PlanParseError as err:
        # Exit 1, NOT 2. A malformed plan reads like a validation failure and
        # the semantic pull toward `ValidationFailure` is real -- but the
        # shipped binary reports RuntimeFailure here
        # (`crates/tan-cli/src/commands/build/plan_modes.rs:140-146` and
        # `native.rs:110-116` both call `plan_error_run(..., "build.plan-
        # invalid", ..., ExitCode::RuntimeFailure)`), and the exit ladder is a
        # frozen contract. The consumer makes it worse than cosmetic:
        # `alp-sdk-vscode/src/alpCli/service.ts:253-259` renders exit 2 as
        # `severity: "warning"` and exit 1 as `"error"`, so "2" would downgrade
        # a plan that will not parse -- a hard failure -- to a yellow banner.
        # 2 stays for a genuine validation failure in Rust's own terms, e.g.
        # `validate`'s schema violation, whose fixture pins exit 2.
        raise BuildError(err.code, err.message, ExitCode.RUNTIME_FAILURE) from err


def _slice_result(core_id: str, backend: str, outcome: SliceOutcome) -> dict:
    """One `data.slices[]` entry. `rc`/`reason` are OMITTED when absent, not
    null -- Rust's `skip_serializing_if = "Option::is_none"`."""
    result = {
        "coreId": core_id,
        "backend": backend,
        "status": _WIRE_STATUS.get(outcome.status, "failed"),
    }
    if outcome.exit_code is not None:
        result["rc"] = outcome.exit_code
    if outcome.message is not None:
        result["reason"] = outcome.message
    return result


def _dispatch(
    plan: BuildPlan,
    demotions,
    build_root: Path,
    sdk_root: str | None,
    sdk_root_for_stamp: str | None,
    *,
    json_mode: bool = False,
) -> tuple[list[SliceOutcome], list[Issue]]:
    """Run the plan's slices, holding back the ones token substitution demoted
    -- and, since tan-cli#483, the ones whose `cores.<id>.app` resolved to a
    directory that does not exist ([`_missing_app_dirs`]) -- both HELD
    per-slice, matching the same per-slice fail/skip shape
    `execute_slices`'s own unknown-backend/missing-tool checks already use,
    rather than refusing the whole plan over one bad core: a two-core project
    with one good `app:` and one bad one still builds the good core.

    Arms its own [`_Heartbeat`] around the dispatch (tan-cli#287), rather
    than taking one from the caller: `execute_slices` (below) is a single,
    EAGER call that can run for minutes -- the loop after it only walks
    results `execute_slices` already computed -- and this function is the
    one seam every route into the build engine shares. `_build`'s two
    callers, `build_cmd.build` (`tan build`) and `run_cmd._build_then_run`
    (`tan run`), both bottleneck through here before any slice runs, so
    arming the heartbeat at THIS level, not one up, is what covers `tan
    run`'s identical silent-CMake window too -- the gap the previous
    revision (a `_Heartbeat` built in `build()` and threaded down as
    `on_output`) left, since `run_cmd.py:258` calls `_build` with no
    `on_output` of its own.

    `json_mode` is the one thing this function cannot discover for itself
    (`sys.stderr.isatty()` it checks directly, same as `build()` used to).
    `build()` passes its real value straight through. `run_cmd.
    _build_then_run` has its own `--format json` support but does not yet
    thread it here (out of this file's scope to add -- see `run_cmd.py:258`),
    so its call keeps the default `False`.
    # ponytail: the one live gap this leaves -- `tan run --format json` with
    # a REAL terminal still attached to stderr would arm the heartbeat where
    # it should stay silent. stdout (the envelope channel) is untouched
    # either way; thread `json_mode` through `run_cmd._build_then_run` if
    # that combination is ever hit for real.

    A demoted slice still names a literal `${TOOLCHAIN_ROOT}` in its own
    fields because this host resolved no toolchain. `execute_slices` does not
    know about demotions, so dispatching one would spawn a command with an
    unsubstituted token in its argv -- a silent wrong-path build, the exact
    failure I-29 says a naive substitution reintroduces. Instead the demotion
    is routed to `executionPolicy.missingTool` (default skip) here, which is
    the seam the Rust executor uses for it too: it is a host-provisioning
    fact, not a plan bug.

    Precedence is preserved: a demoted slice that ALSO names an unknown
    backend or carries no command is left to `execute_slices`, because both of
    those outrank a provisioning fact and are checked first there.

    `sdk_root` is threaded straight through to `execute_slices` -- it is
    THIS run's already-resolved `--sdk-root`/discovered checkout (`_build`'s
    own parameter), the identity `execute_slices`'s sdk-switch-pristine guard
    (issue #52) keys its stamp comparison on. Without it every stamp
    comparison degrades to "unresolved", which never wipes anything -- see
    `tan.core.plan_exec.sdk_stamp_key`. `sdk_root_for_stamp` is the SAME
    checkout, NORMALIZED and anchored on the workspace root (`build()`'s own
    `tan.core.plan_exec.normalize_path(workspace_root / sdk_root)`) -- passed
    separately so a relative `--sdk-root ../alp-sdk` keys the stamp
    IDENTICALLY to the absolute form `tan sdk switch` would pin for the same
    checkout (tan-cli#163), without changing what `sdk_root` itself
    substitutes for `${SDK_ROOT}` or reports as `sdk.root`.

    Held slices are also threaded into `execute_slices` as `held_outcomes`
    (tan-cli #89 MINOR 4, oracle `execute/mod.rs:379-400`): `execute_slices`
    only ever sees the RUNNABLE subset of the plan (`replace(plan,
    slices=runnable)` below), so its own post-build manifest overlay would
    otherwise never learn a demoted slice existed at all -- leaving that
    core's `system-manifest.yaml` entry at its plan-time (or a previous
    run's) `status`/`output_artefact` forever, which `tan size`/
    `tan debug-config` read with no status check of their own. Each held
    outcome's `output_artefact` is the explicit empty string, never `None`:
    the overlay (`overlay_run_results_raw`) treats `None` as "preserve
    whatever is already there", which is correct for a slice this run never
    touched but wrong for one THIS run knows never dispatched.
    """
    held = {
        d.slice_index: d
        for d in demotions
        if plan.slices[d.slice_index].backend in KNOWN_BACKENDS
        and plan.slices[d.slice_index].command is not None
    }
    action = resolve_action(plan.execution_policy, "missing_tool", PolicyAction.SKIP)
    failed = action is PolicyAction.FAIL

    # tan-cli#483: a slice already held for `${TOOLCHAIN_ROOT}` keeps that
    # verdict -- a bad `app:` is checked below only for the slices demotion
    # left alone, so the two never disagree about the same index.
    missing_app_dirs = {i: msg for i, msg in _missing_app_dirs(plan).items() if i not in held}

    held_outcomes = {
        i: SliceOutcome(
            plan.slices[i].core_id,
            "failed" if failed else "skipped",
            None,
            d.reason,
            output_artefact="",
        )
        for i, d in held.items()
    }
    # A bad app dir is not a host-provisioning gap `executionPolicy` was ever
    # meant to arbitrate (contrast the demotion above) -- it is the plan
    # naming a path that does not exist, so it always FAILS the one slice
    # rather than silently skipping it forward, matching the Expected
    # section of tan-cli#483 ("not a warning that lets the build proceed").
    held_outcomes.update(
        {
            i: SliceOutcome(plan.slices[i].core_id, "failed", None, msg, output_artefact="")
            for i, msg in missing_app_dirs.items()
        }
    )

    runnable = [
        sl for i, sl in enumerate(plan.slices) if i not in held and i not in missing_app_dirs
    ]
    # tan-cli#287: TTY + text-mode only (never `--format json` -- stdout is
    # untouched either way, this is belt-and-suspenders for a machine caller
    # that also inherited the terminal's stderr). `__enter__`/`__exit__` run
    # regardless of what happens inside, so a dispatch that raises still
    # stops the thread and blanks any line it had printed.
    with _Heartbeat(enabled=not json_mode and sys.stderr.isatty()) as heartbeat:
        dispatched = iter(
            execute_slices(
                replace(plan, slices=runnable),
                build_root=build_root,
                env_lookup=os.environ.get,
                # tan-cli#308: no build_cmd.py-level gap fillers of our own --
                # `execute_slices` fills ZEPHYR_BASE/EXTRA_ZEPHYR_MODULES
                # itself, PER SLICE, from the west workspace it resolves
                # internally (`tan.core.zephyr_env.zephyr_env_overrides`),
                # exactly where the Rust oracle's own `execute_slices`
                # computes them (`execute/mod.rs`, inside its own per-slice
                # loop) -- not from an outer caller. This parameter stays for
                # a caller-supplied override this port has none of yet.
                gap_fillers=(),
                on_output=heartbeat,
                sdk_root=sdk_root,
                sdk_root_for_stamp=sdk_root_for_stamp,
                held_outcomes=held_outcomes.values(),
            )
        )

        outcomes: list[SliceOutcome] = []
        issues: list[Issue] = []
        for i, sl in enumerate(plan.slices):
            demotion = held.get(i)
            if demotion is not None:
                outcomes.append(held_outcomes[i])
                issues.append(
                    Issue(
                        # Same code as the plan-fatal sibling on purpose: the
                        # extension needs no new vocabulary, only a severity
                        # that says whether this stopped the build.
                        "build.toolchain-root-unresolved",
                        "error" if failed else "warning",
                        f"slice `{sl.core_id}` {'failed' if failed else 'skipped'}: "
                        f"{demotion.reason}",
                    )
                )
                continue
            app_dir_message = missing_app_dirs.get(i)
            if app_dir_message is not None:
                outcomes.append(held_outcomes[i])
                issues.append(Issue("build.app-dir-missing", "error", app_dir_message))
                continue
            outcomes.append(next(dispatched))
    return outcomes, issues


#: `os` values the schema requires `cores.<id>.app` to resolve to a real
#: directory for (`metadata/schemas/board.schema.json`
#: `$defs/core_entry/properties/app`) -- `os: yocto` dispatches by
#: `recipe:` (a bitbake recipe name), never by this directory
#: (`tan/planner/orchestrator.py:338-353`), so checking it there would
#: refuse a buildable yocto slice over a path the build never reads.
_APP_DIR_REQUIRED_BACKENDS = frozenset({"zephyr", "baremetal"})


def _missing_app_dirs(plan: BuildPlan) -> dict[int, str]:
    """Slice indices whose `cores.<id>.app` resolved to a directory that does
    not exist on disk (tan-cli#483), each mapped to the refusal message.

    Checked HERE, against the plan `_dispatch` already has in hand, not
    inside `execute_slices` (`tan/commands/build/execute.py`): by the time a
    real `tan build` reaches this function, `apply_plan_token_substitution`
    has already resolved `appDir` to a real absolute path -- or held the
    slice back as a `${TOOLCHAIN_ROOT}` demotion before `_dispatch` ever
    computes `runnable` below. `execute_slices`'s own extensive unit-test
    suite dispatches synthetic plans with a placeholder `appDir` (`"app"`,
    never meant to exist on disk) straight through, so an existence check
    inside that lower-level primitive would misfire on every one of them;
    this level only ever sees a plan that went through the real
    substitution pass.

    Neither `tan/planner/orchestrator.py`'s `_zephyr_app_dir` (the west
    command's own app-dir resolution) nor `_resolve_app_path` checks
    existence -- both are FROZEN to this unit (`tan/planner/**`). Left
    unfixed there, a nonexistent `cores.<id>.app` makes `_zephyr_app_dir`'s
    own CMakeLists.txt-fallback probe find the PROJECT ROOT's CMakeLists.txt
    (the parent of the nonexistent dir) and hand `west build` that instead,
    silently, naming neither the configured path nor the substitution --
    this check is what stops that plan ever reaching dispatch.

    EXISTENCE only, not contents. The schema text also requires a zephyr app
    dir to contain `prj.conf` or `CMakeLists.txt` (`CMakeLists.txt` for
    baremetal) -- deliberately NOT enforced here: audited against every
    `examples/**/board.yaml` on alp-sdk `dev` (tan-cli#483 comments), 96 of
    104 enabled zephyr/baremetal core entries name `app: ./src`, a
    sources-only directory holding neither file, because the real
    `CMakeLists.txt` sits at the example root and pulls `src/` in via
    `target_sources(app PRIVATE src/main.c)` -- that is the SHIPPED
    convention `_zephyr_app_dir`'s own fallback exists to serve, not an
    aberration, and a contents check would refuse the majority of the SDK's
    own examples over their normal shape."""
    missing: dict[int, str] = {}
    for i, sl in enumerate(plan.slices):
        if sl.backend not in _APP_DIR_REQUIRED_BACKENDS or sl.app_dir is None:
            continue
        if not Path(sl.app_dir).is_dir():
            missing[i] = f"core `{sl.core_id}`: app directory does not exist: {sl.app_dir}"
    return missing


def _backend_issues(plan: BuildPlan, outcomes: list[SliceOutcome]) -> list[Issue]:
    """Name the backend string the plan sent that this CLI does not know.
    Recomputed from the slice rather than sniffed out of the outcome message:
    `executionPolicy.unknownBackend` already decided skip-vs-fail inside
    `execute_slices`, and the outcome's status is the honest read of it."""
    issues = []
    for sl, outcome in zip(plan.slices, outcomes, strict=True):
        if sl.backend in KNOWN_BACKENDS:
            continue
        failed = outcome.status == "failed"
        issues.append(
            Issue(
                "build.unknown-backend",
                "error" if failed else "warning",
                f"slice `{sl.core_id}` names unrecognised backend `{sl.backend}`"
                + ("" if failed else " -- skipped"),
            )
        )
    return issues


def _missing_tool_issues(plan: BuildPlan, outcomes: list[SliceOutcome]) -> list[Issue]:
    """Name the tool a skipped (or, under `executionPolicy.missingTool: fail`,
    failed) slice could not find on this host.

    Matched on `outcome.message` rather than re-probing PATH a second time:
    `execute_slices`' missing-tool branch (`build/execute.py`) is the ONLY
    skip/fail reason shaped exactly `` tool `{tool}` not found `` -- a
    null-command skip reads `` has no command `` (and its reason already
    lives in `plan.warnings`, I-11), and an unknown-backend one is caught
    structurally by `_backend_issues` above -- so this recovers exactly the
    missing-tool cases and nothing else.

    tan-cli#283: without this, `tan build` on a host missing `west`/`bitbake`
    reported each slice's specific reason only in `data.slices[].reason` --
    never in `issues[]`, the field a JSON consumer (the VS Code extension,
    CI) actually reads to decide whether anything needs flagging."""
    issues = []
    for sl, outcome in zip(plan.slices, outcomes, strict=True):
        message = outcome.message
        if message is None or not (
            message.startswith("tool `") and message.endswith("` not found")
        ):
            continue
        failed = outcome.status == "failed"
        issues.append(
            Issue(
                "build.missing-tool",
                "error" if failed else "warning",
                f"slice `{sl.core_id}` {'failed' if failed else 'skipped'}: {message}",
            )
        )
    return issues


def _build(
    *,
    # Defaulted so `mode` stays optional for the OTHER caller of this engine:
    # `run_cmd._build_then_run` builds unconditionally (there is no `tan run
    # --plan`/`--materialise`) and has no mode to pass.
    mode: str = _MODE_NATIVE,
    plan_from: str | None,
    build_root: str,
    sdk_root: str | None,
    sdk_root_for_stamp: str | None,
    board_yaml: str | None,
    # Defaulted for the same reason as `mode`: `run_cmd._build_then_run`
    # calls this with no format preference of its own today. Threaded
    # straight through to `_dispatch`, which is where the heartbeat this
    # gates is actually armed -- see that function's own docstring.
    json_mode: bool = False,
) -> tuple[ExitCode, dict, list[Issue]]:
    text, plan = _acquire_plan(plan_from, sdk_root, board_yaml)

    if mode == _MODE_PLAN:
        # Parsed (so a plan that will not load is still refused with its own
        # code -- measured: the oracle rejects `schemaVersion: 2` and an
        # unknown `executionPolicy` action in THIS mode too), then echoed
        # verbatim. No token substitution: the oracle shows a `planPathMode:
        # tokened` plan with no SDK resolvable at exit 0, unsubstituted, and
        # only refuses it once `--materialise` asks for it on disk.
        return ExitCode.SUCCESS, json.loads(text), []

    # Substitution runs on the in-memory plan BEFORE materialise writes
    # anything and before any command is assembled, so an unresolvable token
    # can never reach disk or an argv. A no-op on an untokened plan (every
    # plan the SDK emits today).
    try:
        plan, demotions = apply_plan_token_substitution(
            plan,
            board_yaml_path=board_yaml,
            exec_base=build_root,
            sdk_root=sdk_root,
            python=_planner_python(build_root, sdk_root),
            # NOT YET PORTED: `crate::toolchain::resolve_toolchain_root`. Left
            # unresolved rather than guessed -- resolution is lazy, so a plan
            # that never names ${TOOLCHAIN_ROOT} (every SDK plan today) is
            # unaffected, and one that does is demoted per its own
            # executionPolicy instead of built against the host root.
            toolchain_root=None,
        )
    except TokenSubstitutionError as err:
        # RuntimeFailure for every code this pass raises, `build.plan-invalid`
        # (an unknown `planPathMode`) included -- same ladder position the Rust
        # oracle gives them (`native.rs:132-142`), and the same code must not
        # mean two different exits depending on which module raised it.
        raise BuildError(err.code, err.message, ExitCode.RUNTIME_FAILURE) from err

    # I-20. ALL of them, then dispatch -- never interleaved.
    try:
        materialise_plan(plan, Path(build_root))
    except MaterialiseError as err:
        raise BuildError(
            "build.materialise-failed", err.message, ExitCode.WRITE_FAILURE
        ) from err

    if mode == _MODE_MATERIALISE:
        # Write and stop -- the oracle dispatches nothing here, and its `data`
        # carries no `slices` at all. `written` is rebuilt from the plan's own
        # (post-substitution) artefact paths in `materialise_plan`'s documented
        # order, shared first then per slice, because those are the RELATIVE
        # strings the oracle reports; `materialise_plan` itself hands back
        # resolved absolute paths, and relativising those back re-introduces
        # every symlink/case difference `_abs_posix` exists to avoid.
        written = [a["path"] for a in plan.shared_artefacts]
        for sl in plan.slices:
            written.extend(a["path"] for a in sl.config_artefacts)
        return (
            ExitCode.SUCCESS,
            {"schemaVersion": "1", "baseDir": build_root, "written": written},
            [],
        )

    # Cleared before dispatch, mirroring `run_cmd.py`'s own pattern, so a
    # leftover signal from an earlier in-process `_build` (e.g. a test
    # harness calling `build()` more than once) never gets misread as THIS
    # run's own write outcome below.
    reset_last_manifest_write()
    outcomes, issues = _dispatch(
        plan, demotions, Path(build_root), sdk_root, sdk_root_for_stamp, json_mode=json_mode
    )

    any_failed = any(o.status not in ("succeeded", "skipped") for o in outcomes)
    # tan-cli#283: a plan whose EVERY slice was skipped (e.g. `west`/
    # `bitbake` absent from PATH) used to fall straight through this `if
    # any_failed` gate -- `skipped` is deliberately not a failure status on
    # its own line above -- and report `ok: true, exitCode: 0, issues: []`.
    # A JSON consumer cannot tell that from a real build. A PARTIAL build
    # (some slice built -- e.g. only the Zephyr cores, on a host with no
    # Yocto toolchain) is a legitimate, deliberate outcome and must stay
    # `ok: true`; refusing outright only when NOTHING built at all is the
    # narrower fix, not "any skip is now a failure".
    nothing_built = (
        bool(outcomes)
        and not any_failed
        and not any(o.status == "succeeded" for o in outcomes)
    )
    exit_code = ExitCode.RUNTIME_FAILURE if (any_failed or nothing_built) else ExitCode.SUCCESS
    if any_failed:
        issues.insert(
            0, Issue("build.slice-failed", "error", "one or more slices failed to build")
        )
    elif nothing_built:
        issues.insert(
            0,
            Issue(
                "build.nothing-built",
                "error",
                "no slice was built -- every slice was skipped",
            ),
        )
    issues.extend(_backend_issues(plan, outcomes))
    # tan-cli#283: named per skipped/failed slice, even in the PARTIAL case
    # `nothing_built` does not cover -- a consumer reading only `issues[]`
    # (not `data.slices[]`) must still be able to tell "2 of 3 slices built"
    # from "3 of 3".
    issues.extend(_missing_tool_issues(plan, outcomes))
    # The sdk-switch-pristine wipe (issue #52) must not be stderr-only in
    # JSON mode -- the VS Code extension only ever sees the envelope, not
    # `_stream`'s output. Verbatim oracle codes/severity
    # (`crates/tan-cli/src/commands/build/execute/mod.rs:591,617`):
    # `build.sdk-switch-pristine` for the wipe itself, `-failed` when the
    # wipe could not fully land.
    issues.extend(last_sdk_switch_issues())

    manifest_reason = last_manifest_write_failure()
    if manifest_reason is not None:
        # A failed manifest write used to report only through `on_output`
        # (stderr) -- so the envelope said `ok: true, issues: []` while
        # `system-manifest.yaml` still named the PREVIOUS run's status and
        # artefact, which `tan size` re-derives and `tan flash` would go on
        # to program. Verbatim oracle code/message
        # (`crates/tan-cli/src/commands/build/execute/mod.rs:809-814`):
        # dropping this half is exactly how a stale manifest used to look
        # identical to `ok: true`.
        issues.append(
            Issue(
                "build.manifest-write-failed",
                "warning",
                f"system-manifest.yaml was not updated — {manifest_reason}",
            )
        )

    data = {
        "schemaVersion": "1",
        "baseDir": build_root,
        "slices": [
            _slice_result(sl.core_id, sl.backend, outcome)
            for sl, outcome in zip(plan.slices, outcomes, strict=True)
        ],
        # The plan's own warnings, verbatim. A `command: null` slice is a
        # legitimate skip whose REASON lives only here (I-11) -- dropping the
        # list leaves the user a slice that silently did not build. Passed
        # through untyped on purpose: the schema says new warning codes may
        # appear without a schemaVersion bump, so a consumer must not treat
        # them as a closed set, and neither may this.
        "warnings": plan.warnings,
    }
    return exit_code, data, issues


def _refuse(code: str, message: str, exit_code: ExitCode, json_mode: bool) -> None:
    """Report a refusal this command makes UP FRONT -- before any project is
    resolved, any plan read, and any byte written -- and exit.

    One helper for both of them (a deferred flag, and a conflicting flag pair)
    because they differ only in code, message and exit code. `project` is left
    unresolved, as `deferred_cmd`'s verb-level stubs leave it: nothing was
    read, so reporting a root would be reporting work this run did not do."""
    issue = Issue(code, "error", message)
    if json_mode:
        emit(
            Envelope(
                "build",
                Project(root=None, board_yaml=None),
                {"message": message},
                [issue],
                exit_code,
            )
        )
    else:
        print(f"error: {message}", file=sys.stderr)
    raise typer.Exit(int(exit_code))


def build(
    plan_from: str = typer.Option(
        None,
        "--plan-from",
        metavar="FILE",
        help="Read the build plan from a JSON file instead of invoking the SDK planner. "
        "Implies --plan: shows the plan and exits unless --materialise or --execute "
        "is given.",
    ),
    materialise: bool = typer.Option(
        False,
        "--materialise",
        help="Write the plan's generated files (shared artefacts + per-slice config) "
        "under the build root and stop, instead of just showing the plan.",
    ),
    # Accepted and INERT, exactly as in the oracle ("This is the default; the
    # flag is kept as an explicit opt-in" -- its own `--native` help). Never
    # read below on purpose: a bare build is already native, and letting it
    # override the `--plan` implied by `--plan-from` is the divergence
    # `test_plan_from_shows_the_plan_and_writes_nothing_even_with_native`
    # exists to keep out. `--execute` is the flag that dispatches a
    # file-supplied plan.
    native: bool = typer.Option(
        False,
        "--native",
        help="Build natively: materialise the plan, then run each slice's command. "
        "The default when no plan-mode flag is given. Does NOT override the --plan "
        "implied by --plan-from -- use --execute for that.",
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Materialise the plan AND run each slice's command, even when the plan "
        "came from --plan-from -- run a pinned, reviewed plan file reproducibly. "
        "Implies --materialise (nothing can run that was never written); reports the "
        "ordinary build result.",
    ),
    build_root: str = typer.Option(
        None,
        "--build-root",
        metavar="DIR",
        help="Project tree the slices run under and artefacts are written below "
        "(default: the board.yaml's directory, else the current directory).",
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
    # --- declared so they are refused as DEFERRED, never as a typo ----------
    # See `_DEFERRED_FLAGS`. Each is a real, working flag of the v0.4.1 oracle
    # that this port does not implement. LISTED in `--help` rather than hidden,
    # for the same reason `deferred_cmd` registers its seven verbs in the
    # command list: a reader comparing this surface to v0.4.1's needs to see
    # that tan knows the flag and is refusing it, which absence cannot say.
    plan: bool = typer.Option(False, "--plan", help=_DEFERRED_HELP),
    target: str = typer.Option(None, "--target", metavar="EMIT", help=_DEFERRED_HELP),
    all_targets: bool = typer.Option(False, "--all", help=_DEFERRED_HELP),
    manifest: bool = typer.Option(False, "--manifest", help=_DEFERRED_HELP),
    manifest_from: str = typer.Option(
        None, "--manifest-from", metavar="FILE", help=_DEFERRED_HELP
    ),
    no_auto_bootstrap: bool = typer.Option(False, "--no-auto-bootstrap", help=_DEFERRED_HELP),
    pristine: bool = typer.Option(False, "--pristine", help=_DEFERRED_HELP),
    verbose: bool = typer.Option(False, "--verbose", help=_DEFERRED_HELP),
    quiet: bool = typer.Option(False, "--quiet", help=_DEFERRED_HELP),
    no_color: bool = typer.Option(False, "--no-color", help=_DEFERRED_HELP),
    non_interactive: bool = typer.Option(False, "--non-interactive", help=_DEFERRED_HELP),
    ci: bool = typer.Option(False, "--ci", help=_DEFERRED_HELP),
) -> None:
    """Build every slice of the project's build plan."""
    json_mode = output_format == "json"

    # Before anything is resolved or read: a run naming a flag this port does
    # not implement does no work at all, and says which flag and why.
    given = dict(
        zip(
            _DEFERRED_FLAGS,
            (
                plan,
                target is not None,
                all_targets,
                manifest,
                manifest_from is not None,
                no_auto_bootstrap,
                pristine,
                verbose,
                quiet,
                no_color,
                non_interactive,
                ci,
            ),
            strict=True,
        )
    )
    # FIRST, ahead of the conflict check below: a flag this build does not
    # implement at all cannot be honoured in any combination, so `--execute
    # --plan` gets the accurate answer (that `--plan` is deferred) rather than
    # a conflict message for a flag that would not have worked anyway. Either
    # way it is a coded refusal, never a silent precedence win.
    for flag, was_given in given.items():
        if was_given:
            _refuse(
                DEFERRED_ISSUE_CODE,
                f"`tan build {flag}` is deferred and not available in this "
                f"build (see {DEFERRED_ISSUE_URL}).",
                ExitCode.RUNTIME_FAILURE,
                json_mode,
            )

    if execute and materialise:
        # Two different answers to "what does this run leave behind" -- one
        # stops after writing, one goes on to dispatch. Refused with its own
        # code at exit 2 (this CLI's position for an invalid invocation, the
        # same `--format bogus` takes) instead of letting either win quietly:
        # a silent precedence win here is precisely what `--execute` exists to
        # replace.
        _refuse(
            "build.conflicting-flags",
            "`--execute` and `--materialise` cannot be combined: `--materialise` "
            "writes the plan's files and stops, `--execute` writes them and then "
            "runs each slice. Pick one (`--execute` already implies writing).",
            ExitCode.VALIDATION_FAILURE,
            json_mode,
        )

    # The oracle's own precedence, measured, reproduced exactly -- `--native`
    # included: `--plan-from` implies `--plan`, and that implied `--plan` beats
    # an explicit `--native`, so `--plan-from --native` shows the plan and
    # writes nothing here too. `--execute` is the port's ADDED capability (see
    # the module docstring) and the only thing that dispatches a file-supplied
    # plan; it selects the same `_MODE_NATIVE` a plain `tan build` runs, so it
    # reports the same `data` rather than a shape of its own. With no
    # `--plan-from` it is a no-op -- that run already builds.
    if execute:
        mode = _MODE_NATIVE
    elif materialise:
        mode = _MODE_MATERIALISE
    elif plan_from is not None:
        mode = _MODE_PLAN
    else:
        mode = _MODE_NATIVE

    # `util::cli_workspace_root`: `--project` joined to the (real) cwd, THEN
    # everything below anchors on this instead of the bare cwd -- board.yaml
    # discovery, the default build root, and SDK discovery. Was previously
    # missing entirely (this command had no `--project` at all): the extension
    # falls back to it when no project is active in the workspace
    # (`alp-sdk-vscode/src/west.ts`'s `alpBuild`, `--project <example> build`).
    # A Typer option Typer does not declare is a Click parse error (exit 2),
    # so the missing flag REFUSED outright rather than building silently; the
    # actual wrong-project risk is the relative `--board-yaml` anchor fixed
    # below. `project is None` (the overwhelming common case, no flag given)
    # makes `workspace_root == cwd`, byte-for-byte the prior behaviour -- this
    # only changes anything when `--project` is actually given.
    cwd = Path.cwd()
    workspace_root = cwd if project is None else Path(os.path.join(str(cwd), project))

    # Anchor an EXPLICIT `--board-yaml` on `workspace_root`, not the real cwd,
    # before anything derives from it (the default build root below included).
    # `_abs_posix` is purely lexical (`os.path.abspath`, which anchors on
    # `os.getcwd()`), so a relative `--board-yaml` left untouched re-anchors on
    # the real cwd instead -- matching Rust's `resolve_board_yaml_path`
    # (`crates/tan-core/src/project.rs:198-208`, which joins a relative
    # configured path onto `workspace_root`). Verified against the oracle in a
    # scratch tree (`tmp/app/board.yaml`, cwd=`tmp`): `tan --project app build
    # --board-yaml board.yaml --format json` must report `project.root` /
    # `project.boardYaml` under `tmp/app`, not `tmp` -- with a board.yaml also
    # sitting in the real cwd, the pre-fix anchor planned and built the WRONG
    # project without a word, exactly the failure class this port exists to
    # remove.
    if board_yaml is not None and not os.path.isabs(board_yaml):
        board_yaml = os.path.join(str(workspace_root), board_yaml)

    # Resolution, in one place, before anything can fail -- and to ABSOLUTE,
    # BOTH sides, from the same anchor.
    #
    # The divergence guard in `apply_plan_token_substitution` compares these
    # two lexically (as the Rust oracle does), so they must move together: this
    # resolves both or neither, never one. But they must also both be absolute,
    # because `${PROJECT_ROOT}` is substituted from `board_yaml` and then
    # CONSUMED from a different directory -- each slice runs its command in its
    # own `command.cwd` (`<root>/build/<slice>`), and Zephyr resolves
    # `-DEXTRA_CONF_FILE` against the APPLICATION source dir, which for the
    # stock-shim slice is inside the SDK checkout entirely. Left relative, the
    # default `cd <project> && tan build` resolves `${PROJECT_ROOT}` to `"."`
    # and every zephyr slice dies -- `<sdk>/firmware/alp-stock-shim/./build/
    # <slice>/alp.conf: File not found`, and `ERROR: . doesn't contain a
    # CMakeLists.txt` -- which is the whole documented happy path. Rust never
    # hits this because it anchors first: `resolve_cli_project_context_inner`
    # builds `workspace_root = normalize_path(cwd.join(--project))` and derives
    # `board_yaml_path` by joining onto THAT, so both are absolute before the
    # guard ever runs (`crates/tan-cli/src/util.rs`, `crates/tan-core/src/
    # project.rs::resolve_board_yaml_path`).
    if board_yaml is None and (workspace_root / "board.yaml").is_file():
        # Already absolute (anchored on `workspace_root`, not a bare
        # relative "board.yaml") so `_abs_posix` below is a no-op
        # normalisation rather than a re-anchor onto the real cwd -- the
        # divergence that would reappear the moment `--project` differs
        # from cwd.
        board_yaml = str(workspace_root / "board.yaml")
    if build_root is None:
        build_root = str(Path(board_yaml).parent) if board_yaml else str(workspace_root)
    build_root = _abs_posix(build_root)
    if board_yaml is not None:
        board_yaml = _abs_posix(board_yaml)

    # `--sdk-root` > the project's own `.alp/sdk-path` pin > the machine-global
    # default > the positional walk (`resolve_sdk_root_ladder`, above) --
    # previously this skipped straight from `--sdk-root` to the positional
    # walk, so `tan init`'s own pointer went unread the moment `tan build` ran
    # in the same directory.
    sdk_resolution = resolve_sdk_root_ladder(sdk_root, workspace_root)
    resolved_sdk_root = sdk_resolution.path
    sdk_tier = sdk_resolution.tier
    sdk_broken_pin = sdk_resolution.broken_project_pin
    sdk_foreign_default = sdk_resolution.foreign_global_default_for
    # tan-cli#407: computed HERE, while `sdk_root` still holds the raw
    # `--sdk-root` flag -- it is reassigned to the RESOLVED root a few lines
    # below, and a bogus flag is blanked to `None` in between, which would
    # make this warn about a discovery collision the user never reached
    # because they named a root explicitly.
    sdk_divergence = sdk_ladder_divergence_issue(sdk_root, workspace_root, wide=False)
    # tan-cli#257/#258: `resolve_sdk_root_ladder` returns an explicit
    # `--sdk-root` UNVALIDATED (I-31 terminal-for-REPORTING, matching the
    # oracle's `resolve_sdk_tiered`) -- fine for a caller that only reports
    # the tier, but `build` also ACTS on `resolved_sdk_root`, so a bogus flag
    # used to sail through as `sdk.sourceTier: "sdkRootFlag"`, reach
    # `_emit_plan` as a non-None `sdk_root`, and get refused for the NEXT
    # missing thing (`no board.yaml found`) instead -- telling the customer
    # their project is broken when the `--sdk-root` they just typed is what's
    # wrong, and reporting an `sdk` key the oracle never emits on this path.
    # Validated here, at the flag's own entry point, rather than in the
    # shared ladder (which every other caller also relies on staying
    # unvalidated) -- same shape as `clean_cmd.sdk_root_resolves` and
    # `flash_cmd._resolve_sdk`, the two callers that already guard their own
    # explicit `--sdk-root`. An unresolvable explicit root is treated as no
    # root at all: `_emit_plan` then gives its own "no alp-sdk checkout
    # found" refusal, and no `sdk` key is reported, matching the oracle.
    if sdk_tier == "sdkRootFlag" and not _is_sdk_root(resolved_sdk_root):
        resolved_sdk_root = None
    sdk_root = str(resolved_sdk_root) if resolved_sdk_root is not None else None
    sdk = SdkInfo(sdk_root, sdk_tier) if sdk_root is not None else None
    # Absolute, `.`/`..`-collapsed, anchored on `workspace_root` -- what the
    # sdk-switch-pristine guard actually compares (tan-cli#163), kept
    # SEPARATE from `sdk_root` itself: an explicit `--sdk-root` is a
    # documented, supported relative form, and `sdk_root` unchanged still
    # feeds `${SDK_ROOT}` token substitution and the `sdk.root` envelope
    # field verbatim, matching the Rust oracle's own split between
    # `normalized_sdk_root_str` (stamp-only) and the raw `resolve_sdk_root`
    # result used everywhere else.
    sdk_root_for_stamp = (
        str(normalize_path(workspace_root / sdk_root)) if sdk_root is not None else None
    )
    # tan-cli#236: `boardYaml` reported only when the file really exists --
    # `board_yaml` itself stays unfiltered for `_build` below, which needs the
    # unconditional path.
    project = Project.resolved(build_root, board_yaml)

    # tan-cli#287: the heartbeat that covers a silent slice (a first-build
    # CMake configure, measured at 234.2s with nothing printed) is armed
    # inside `_dispatch`, not here -- `_build`'s OTHER caller,
    # `run_cmd._build_then_run`, bottlenecks through the identical
    # `_dispatch` call and needs the same cover for a silent `tan run`. See
    # `_dispatch`'s own docstring for the TTY/`json_mode` gating (unchanged:
    # still never armed for `--format json`, stdout is untouched either way)
    # and for the mode-gating this also fixes for free -- `_MODE_PLAN`/
    # `_MODE_MATERIALISE` return from `_build` before `_dispatch` is ever
    # reached, so neither ticks a heartbeat for a slow in-process planner
    # that never runs CMake at all.
    try:
        exit_code, data, issues = _build(
            mode=mode,
            plan_from=plan_from,
            build_root=build_root,
            sdk_root=sdk_root,
            sdk_root_for_stamp=sdk_root_for_stamp,
            board_yaml=board_yaml,
            json_mode=json_mode,
        )
    except BuildError as err:
        exit_code, data, issues = err.exit_code, None, [Issue(err.code, "error", err.message)]
    except Exception as err:  # noqa: BLE001 -- see below
        # The port's most-repeated defect class: an uncaught exception
        # escapes as a raw traceback, stdout stays empty, and the
        # extension renders nothing at all with no error on either side.
        # Anything that reaches here is a tan bug, so it is reported as
        # one -- with an envelope.
        exit_code = ExitCode.INTERNAL_FAILURE
        data = None
        issues = [Issue("build.internal-failure", "error", f"{type(err).__name__}: {err}")]

    # These facts are all about how the SDK ROOT was RESOLVED, so all are
    # prepended ahead of whatever the build itself reported, and all survive a
    # run that never planned anything -- a refusal is exactly when knowing
    # which checkout answered matters most.
    #
    # tan-cli#263 review: `tan build` is the command a silently-missed
    # `.alp/sdk-path` pin hurts most -- it builds against whichever tier the
    # ladder fell through to and says nothing, where `sdk current` alone only
    # helps someone already suspicious enough to run it. tan-cli#407 is the
    # same argument for the two-checkout collision one tier below it, and
    # tan-cli#464 the same again for a `globalDefault` answer a DIFFERENT
    # project's bootstrap relocation actually decided -- `sdk current` was the
    # only place this warning reached before; `tan build` is the one that
    # silently compiles against the wrong checkout because of it.
    resolution_issues = [
        issue
        for issue in (
            project_pin_issue(sdk_broken_pin, sdk_tier),
            sdk_divergence,
            global_default_foreign_project_issue(sdk_foreign_default),
        )
        if issue is not None
    ]
    if resolution_issues:
        issues = [*resolution_issues, *issues]

    if json_mode:
        emit(Envelope("build", project, data, issues, exit_code, sdk=sdk))
    else:
        for issue in issues:
            print(f"{issue.severity}: {issue.message}", file=sys.stderr)
        _text_recap(mode, data)
    raise typer.Exit(int(exit_code))


def _text_recap(mode: str, data: dict | None) -> None:
    """The stderr recap for whichever mode ran. Three shapes because `data`
    has three shapes -- a plan-mode `data.slices[]` entry is a PLAN slice with
    no `status` at all, so the build recap below would `KeyError` on it."""
    if data is None:
        return
    if mode == _MODE_MATERIALISE:
        print(
            f"materialised {len(data['written'])} file(s) under {data['baseDir']}:",
            file=sys.stderr,
        )
        for rel in data["written"]:
            print(f"  {rel}", file=sys.stderr)
        return
    if mode == _MODE_PLAN:
        print(
            f"build plan (schema v{data.get('schemaVersion')}) -- {data.get('sku')}",
            file=sys.stderr,
        )
        print(f"  board.yaml: {data.get('boardYaml')}", file=sys.stderr)
        print(f"  build root: {data.get('buildRoot')}", file=sys.stderr)
        slices = data.get("slices") or []
        print(f"  slices ({len(slices)}):", file=sys.stderr)
        for sl in slices:
            print(
                f"    - {sl.get('coreId')} [{sl.get('backend')}] -> {sl.get('buildDir')}",
                file=sys.stderr,
            )
        print(
            f"  shared artefacts: {len(data.get('sharedArtefacts') or [])}",
            file=sys.stderr,
        )
        print(f"  warnings: {len(data.get('warnings') or [])}", file=sys.stderr)
        return
    slices = data.get("slices", [])
    for result in slices:
        reason = f" -- {result['reason']}" if "reason" in result else ""
        print(
            f"{result['status']}: {result['coreId']} [{result['backend']}]{reason}",
            file=sys.stderr,
        )
    # tan-cli#283: a one-line summary AFTER every per-slice line, so a
    # partial or all-skipped build cannot be scrolled past -- the per-slice
    # lines above already carry this same count, but only for a reader who
    # counts them by hand.
    if slices:
        built = sum(1 for result in slices if result["status"] == "ok")
        print(f"{built} of {len(slices)} slice(s) built", file=sys.stderr)
