# SPDX-License-Identifier: Apache-2.0
"""The alp-sdk discovery ladders (tan-cli#408) -- `--sdk-root` > project pin >
machine-global default > positional discovery, in the NARROW and WIDE
flavours documented on `resolve_sdk_root_ladder` / `resolve_sdk_root_wide`
below -- plus the divergence warning between them and the interpreter
resolution that rides on the same tiers.

**Moved here from `tan/commands/build_cmd.py`, verbatim logic, tan-cli#408.**
Nineteen modules resolved the SDK through this code while it lived in a
2000+-line COMMAND module, and one of them was `tan/envelope.py` -- core
infrastructure every command's envelope passes through -- reaching back into
a command for it via a function-scoped import whose own comment said why:
"`build_cmd` imports this module at module level, so a top-level import here
would be circular." That is the dependency inverted: core importing from a
command. This module is the fix -- `tan.core` importing no command module
(the same invariant `tan.core.shapes` already states and relies on), so
`envelope.py` can import [`sdk_ladder_divergence_issue`] at the top of the
file rather than inside the one method that needed it.

Two names this module still reaches for are themselves only resolvable from
a command module -- `resolve_sdk_tiered` (`tan.commands.sdk_cmd`, the single
NARROW tiered resolver both ladders below share) and `Issue`
(`tan.envelope`, the wire type [`sdk_ladder_divergence_issue`] returns).
Importing either at MODULE level here would recreate exactly the cycle this
extraction exists to remove, just with a new module on one end -- `sdk_cmd`
already imports these ladders back (for `sdk current`'s own wide-walk
fallback and its own divergence check), and `envelope` is the module this
whole move is FOR. Both stay function-scoped, deferred to the moment the
enclosing function actually runs, by which point the caller (always a
command, or `envelope` acting on a command's behalf) has necessarily already
imported the module it is calling into -- so the deferred import always
resolves. This module's own top-level imports stay command-free either way;
what moved is WHERE the deferral lives, not whether the cycle is real.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tan.core.shapes import is_sdk_root
from tan.core.venv import venv_python


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
        if is_sdk_root(candidate):
            return candidate
    for ancestor in workspace_root.parents:
        if is_sdk_root(ancestor):
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
    `presets`, `inspect`, `trace`, `sdk current`). The other three -- `init`,
    `generate` and `examples` -- take [`resolve_sdk_root_wide`], below.
    The measurement that splits 13 from 3 is the last paragraph here, and is
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
    `init`, `generate` and `examples` take the child. The oracle
    carries two resolutions, not one, and this ladder mirrors the majority
    (narrow) one plus the wide walk as its tail. Inverting it to match the
    other three would move the SDK root under thirteen commands, and a moved
    root is what `plan_exec.sdk_stamp_action` reads as a switch: every existing
    such workspace would take the `build.sdk-switch-pristine` branch on its
    next build and lose every slice's build dir. The three wide commands were
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

    # Function-scoped: `sdk_cmd` imports THIS module's ladders back (for `sdk
    # current`'s own wide-walk fallback and divergence check), so a top-level
    # import here would recreate the very cycle tan-cli#408 removes -- see
    # the module docstring.
    from tan.commands.sdk_cmd import resolve_sdk_tiered

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
    """The WIDE ladder, for the three commands the oracle routes through
    `util.rs`'s `resolve_sdk_root`: `init`, `generate`, `examples`.
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

    # Function-scoped: see `resolve_sdk_root_ladder`, above.
    from tan.commands.sdk_cmd import resolve_sdk_tiered

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
_WIDE_COMMANDS = "init, generate and examples"


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

    # Function-scoped: `tan.envelope` imports THIS function at module level
    # (that top-level import is the whole point of tan-cli#408), so a
    # top-level import back would be circular. Deferred to call time, by
    # which point `tan.envelope` has necessarily already finished defining
    # `Issue` -- see the module docstring.
    from tan.envelope import Issue

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


def _planner_python_resolution(start: str, sdk_root: str | None) -> tuple[str, bool]:
    """Like [`_planner_python`], but also reports whether it resolved a
    `tan bootstrap` workspace venv (`True`) or fell back to a bare PATH name
    (`False`).

    tan-cli#652: `validate_cmd`/`diff_cmd` need this flag to tell "this
    interpreter is missing a dependency because no workspace venv exists yet
    -- run `tan bootstrap`" from "this interpreter is missing a dependency
    for some other reason" when a spawned SDK script dies importing one. A
    bare fallback interpreter that HAPPENS to already have the SDK's
    dependencies (the common from-source-install shape: `jsonschema` is also
    one of tan's own declared dependencies, so it lands on the same
    interpreter `pip install` used) must not be refused pre-emptively --
    only a run that actually fails gets the remedy attached, and only when
    this flag says there was no venv to blame instead.
    """
    resolved = venv_python(start, sdk_root)
    fallback = "python" if os.name == "nt" else "python3"
    return resolved or fallback, resolved is not None


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

    A thin wrapper over [`_planner_python_resolution`] -- kept as its own
    name because every existing caller wants only the interpreter path, not
    the venv flag, and a second copy of the resolution logic is exactly the
    kind of drift this repo's conventions warn against.
    """
    return _planner_python_resolution(start, sdk_root)[0]
