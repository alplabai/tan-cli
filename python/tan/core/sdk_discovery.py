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

**Finished here, tan-cli#408 review follow-up.** The first pass of this
extraction left `resolve_sdk_tiered` -- the single NARROW tiered resolver
both ladders share -- behind in `tan.commands.sdk_cmd`, reached through a
function-scoped import in each ladder function, on the reasoning that it was
"a much larger, separately-scoped change." Review of that first pass found
the opposite: `resolve_sdk_tiered` and the dozen filesystem-primitive
functions under it (the tier-pointer reads, the registry lookup, the two
positional-discovery walks) touch none of `typer`, `tan.envelope`,
`tan.output_format` or `tan.exit_codes` -- their only non-stdlib dependencies
were already `tan.core.shapes` and `tan.core.sdk_default_registry`, both
core. The whole cluster moved here in the same shape, verbatim, along with
`ActiveSdk` and the two `Issue` builders (`project_pin_issue`,
`global_default_foreign_project_issue`) `envelope.py` needs through
[`sdk_resolution_issues`]. Both ladder functions call `resolve_sdk_tiered`
directly now -- no function-scoped import left inside either one, because
the cycle it used to dodge (`sdk_discovery` -> `sdk_cmd` -> back into this
module's own ladders) no longer exists once `resolve_sdk_tiered` lives here
too.

`Issue` (`tan.envelope`, the wire type every `*_issue`/`*_issues` function
below returns) is the one name this module still cannot import at module
level: `tan.envelope` imports THIS module at module level
(`sdk_ladder_divergence_issue`, `sdk_resolution_issues`), so a top-level
import back would recreate exactly the cycle this extraction removes, just
with the two modules swapped. `TYPE_CHECKING` supplies the name for every
annotation below, which is what a real type checker (mypy, an IDE) resolves
against -- `from __future__ import annotations` defers every annotation to
string form, so this costs nothing at runtime, and it is the same idiom
`sdk_cmd._releases_opener` already uses for `OpenerDirector` one file over.
Each function that actually CONSTRUCTS an `Issue` imports it again,
function-scoped, at the point it runs. The deferral is safe NOT because
some caller has necessarily already imported `tan.envelope` first (tan-cli#408
review, nit: an earlier draft of this paragraph claimed exactly that, and it
does not hold -- measured in a process that imported ONLY this module,
`'tan.envelope' in sys.modules` is `False` right up to the call, and the
deferred import still succeeds). It is safe because by the time any function
in this module RUNS, this module's OWN top-level execution has already
finished and `tan.core.sdk_discovery` is fully registered in `sys.modules` --
so `tan.envelope`'s own top-level `from tan.core.sdk_discovery import
sdk_ladder_divergence_issue, sdk_resolution_issues` finds a complete module,
never a partially-initialized one, however `tan.envelope` first got
imported. A genuine cycle can only happen while ONE of the two modules is
still in the middle of its OWN top-level execution; nothing here runs at
that time.

**`typing.get_type_hints()` still raises on these annotations, and that is
not fixed by the `TYPE_CHECKING` guard -- it cannot be, and no wrapper
should try (tan-cli#408 review, minor 2 follow-up).** `TYPE_CHECKING` is
`False` at runtime, so the guarded import never executes and `Issue` is
never bound in this module's real `__dict__`; `get_type_hints()` resolves a
string annotation by `eval`-ing it against `func.__globals__`, which IS that
same `__dict__`, so `NameError: name 'Issue' is not defined` is still the
measured result of calling it on any function below that names `Issue`.
Making it resolve for real would mean binding the real `Issue` class at
module level, which is the exact cycle this whole module exists to avoid --
there is no way to have both. This is the identical tradeoff
`_releases_opener`'s own docstring already makes for `OpenerDirector`, for
the identical reason: `get_type_hints()` is live in this codebase
(`tan.core.global_flags.accept_global_flags`), but it is called ONLY on each
`*_cmd.py` module's top-level typer command callback -- never on an internal
helper in `tan/core/`, here or in `sdk_cmd.py` -- so the two never meet in
production. A guard test asserting `get_type_hints()` resolves across this
module's public functions was considered and rejected for the same reason:
it would be false by construction for every `Issue`-returning one, so it
would not catch a real regression, only encode this already-understood and
already-accepted limitation as a failing assertion.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    # Type-checker only -- see the module docstring for why this cannot be a
    # runtime import. `get_type_hints()` needs this name in the module
    # namespace to resolve the `Issue | None` / `list[Issue]` annotations
    # below; it is never evaluated at import time (`TYPE_CHECKING` is False
    # in a real run), so it adds no runtime dependency on `tan.envelope`.
    from tan.envelope import Issue

from tan.core.sdk_default_registry import (
    deepest_covering_entry,
    parse_registry,
    parse_registry_updated_at,
    registry_path,
)
from tan.core.shapes import SDK_MARKER, is_sdk_root
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


# ── filesystem primitives (every failure is a value, never an exception) ────
# Moved verbatim from `tan/commands/sdk_cmd.py`, tan-cli#408 review follow-up
# -- see the module docstring. `check_sdk_readiness`/`cached_sdk_versions`,
# which stay in `sdk_cmd.py` (they are readiness REPORTING, not resolution),
# import `_read_file`/`_has_loader_script`/`_home_alp_dir` back from here.


def _read_file(path: Path) -> str | None:
    """`tan_core`'s injected `read_file`: contents, or `None` on ANY read
    failure -- missing, a directory, permission-denied, non-UTF-8 bytes.

    `encoding="utf-8"` explicitly (I-27): a bare `read_text()` decodes with the
    host locale, so a pointer file or `sdk_version.yaml` carrying one non-ASCII
    byte raises `UnicodeDecodeError` on a cp1252 Windows host and passes on
    ubuntu CI. Swallowing it to `None` is what the Rust does and is what keeps
    an unreadable file a reported fact instead of a traceback.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _has_loader_script(root: Path) -> bool:
    """True when `root/scripts/alp_project.py` exists -- `util.rs`'s
    `has_loader_script`. `Path.exists()` swallows its own `OSError`/`ValueError`
    (a too-long path, an illegal name), so a pathological pointer value reads as
    "not an SDK" rather than raising out of a tier lookup."""
    return root.joinpath(*SDK_MARKER).exists()


def _to_posix(path: Path) -> str:
    """`tan_core::project::to_posix`. The discovery tier's result is a reported
    path, and every golden-pinned path field is platform-identical forward
    slashes; `Path` renders `\\` on Windows."""
    return str(path).replace("\\", "/")


def _home_alp_dir() -> Path:
    """`~/.alp` -- `USERPROFILE` on Windows else `HOME`, falling back to `.`
    when neither is set (`util.rs`'s `home_alp_dir`). Home of the global default
    pointer and the install cache, and the reason the conformance harness
    overrides BOTH variables: a developer's real `~/.alp/sdk-default` would
    otherwise decide what `sdk current` reports."""
    home = os.environ.get("USERPROFILE" if os.name == "nt" else "HOME")
    return Path(home or ".") / ".alp"


def _read_pointer_json(pointer: Path) -> dict[str, Any] | None:
    """Parse a pointer file (`.alp/sdk-path`, `~/.alp/sdk-default`) into its
    dict, or `None` on ANY failure -- missing, unreadable, invalid JSON, or a
    non-dict shape (a list). Shared by every field reader over this shape so
    `_pointer_target` and `_pointer_written_for` can never disagree about what
    counts as an unreadable pointer.
    """
    if not pointer.exists():
        return None
    raw = _read_file(pointer)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _pointer_target(pointer: Path) -> str | None:
    """The `sdkPath` out of a `{"sdkPath": ..., "updatedAt": ...}` pointer file.

    One function for both pointers -- `tan_core`'s `resolve_active_sdk`
    (`<workspace>/.alp/sdk-path`) and `resolve_global_default_sdk`
    (`~/.alp/sdk-default`) are the same read of the same shape at two paths.
    Every failure is `None`, matching the Rust's `.ok()?` chain: a hand-edited
    pointer holding invalid JSON, a list, or no `sdkPath` at all must fall
    through to the next tier, not abort the command.
    """
    parsed = _read_pointer_json(pointer)
    value = parsed.get("sdkPath") if parsed is not None else None
    return value if isinstance(value, str) else None


def _pointer_written_for(pointer: Path) -> str | None:
    """The optional `writtenFor` field alongside `sdkPath`/`updatedAt` in
    `~/.alp/sdk-default` -- which project's bootstrap relocation last wrote
    this machine-global pointer (tan-cli#464).

    `None` covers both "no opinion" cases the same way, on purpose: a pointer
    written by an older tan that predates this field, and one this tan wrote
    for a run that never relocated a checkout FOR a project at all. Neither is
    a claim that no other project wrote the pointer -- it is the absence of
    evidence, and `resolve_sdk_tiered` must never manufacture a warning out of
    it.

    A non-`str`, empty, or non-absolute value is ALSO `None` (measured
    tan-cli#464 review regression): `writtenFor: ""` used to pass the bare
    `isinstance(value, str)` check here and reach `_workspace_under(ws, "")`,
    which resolves `Path("")` to the process's cwd -- so whether the foreign
    warning fired depended on where the caller happened to be standing, not
    on anything the pointer actually recorded. Every OTHER project root this
    field can legitimately hold is written by `bootstrap_cmd._run` as an
    already-absolute `root` (`Project.resolved`'s own contract), so rejecting
    a relative or blank one here is not narrowing real coverage ON THE
    WRITER'S OWN PLATFORM -- but `~/.alp/sdk-default` is one pointer shared by
    every tan on the host, and a bare `Path(value).is_absolute()` is answered
    by whichever pathlib flavour the READER's OS picked: `PureWindowsPath`
    needs a drive letter, so a legitimate `"/home/u/projB"` written by a
    Linux/macOS tan degraded to "no opinion" the moment a Windows tan read it
    back, and symmetrically `"C:/projB"` from a Windows writer is non-absolute
    to `PurePosixPath` on the other two. Accepted here when EITHER
    `PurePosixPath` or `PureWindowsPath` calls it absolute, so a value either
    platform's tan legitimately wrote still counts, while `""`/`"."`/a bare
    relative segment (`"projB/ws"`)/a drive-relative `"C:projB"` stay `None`
    under both -- the safe degradation is unchanged, only which absolute
    shapes clear it.
    """
    parsed = _read_pointer_json(pointer)
    value = parsed.get("writtenFor") if parsed is not None else None
    if not isinstance(value, str) or not value:
        return None
    if not PurePosixPath(value).is_absolute() and not PureWindowsPath(value).is_absolute():
        return None
    return value


def _workspace_under(workspace_root: Path, root: str) -> bool:
    """Whether `workspace_root` IS `root`, or sits somewhere below it --
    resolved on both sides so a `..`, a symlink, or Windows' case-folding
    cannot spoof a match (mirrors `tan.core.fs_confine.resolve_confined`'s own
    reasoning for the same comparison). Any resolution failure (a path shape
    the host rejects outright) reads as "not under it" -- the caller treats
    that as grounds for a WARNING, never a hard failure.

    `RuntimeError` is caught alongside `OSError`/`ValueError` (review, #904,
    minor 1): `Path.resolve()` re-raises an `ELOOP` (a symlink loop) as a
    `RuntimeError` ("Symlink loop from ...") rather than an `OSError` --
    pathlib's own choice, not this module's -- so a registry origin or a
    legacy `writtenFor` naming a symlink loop must degrade the same as any
    other unresolvable path, not raise out of a tier lookup. Pre-existing
    from tan-cli#464 (both pointer tiers shared this gap before #466 added a
    third caller of the same pattern); left uncaught here would contradict
    `parse_registry`'s own "never raises" contract one function over.
    """
    try:
        return workspace_root.resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def _resolved_origin_depth_key(root: str) -> str:
    """`root`, resolved the same way `_workspace_under` resolves it for
    containment -- reused as `sdk_default_registry.deepest_covering_entry`'s
    depth-ranking key so ranking can never diverge from `covers`'s own notion
    of "contains" (review, #904, major 1: a symlinked origin made the raw
    registry-key string length disagree with `_workspace_under`'s resolved
    containment test, so the WRONG registered SDK could win the "deepest"
    tie-break with no warning at all).

    Any resolution failure (including a symlink loop, `RuntimeError`) falls
    back to the raw string rather than raising. IN PRODUCTION, via
    `deepest_covering_entry`, this is only ever called after `covers` has
    already resolved this SAME `root` successfully for this SAME candidate,
    so a failure here would mean the filesystem changed between the two
    calls -- treated as a same-caliber non-event as every other best-effort
    read in this module, not a crash out of a tier lookup. That reasoning
    made the `RuntimeError` arm of this guard genuinely hard to reach through
    the production call graph alone (review, #904 second round, minor: a
    narrowed `except (OSError, ValueError)` left the entire existing suite
    green). `test_resolved_origin_depth_key_degrades_a_symlink_loop_instead_
    of_raising` closes that by calling this function DIRECTLY, with no prior
    `covers` call in front of it -- proving the degrade independent of
    whether any production call order happens to exercise it.
    """
    try:
        return str(Path(root).resolve())
    except (OSError, ValueError, RuntimeError):
        return root


def _read_global_sdk_registry_raw() -> str | None:
    """`~/.alp/sdk-defaults.json`'s raw text, or `None` when unreadable --
    read ONCE and shared by both views `resolve_sdk_tiered` needs
    (`parse_registry`'s flattened `sdkPath` view and
    `parse_registry_updated_at`'s recency view, review #904 second round,
    major): reading the file twice would let a concurrent `tan bootstrap`'s
    write land BETWEEN the two reads, so one view could see an origin the
    other one does not -- not a crash (`deepest_covering_entry`'s
    `updated_at.get(origin, "")` tolerates a missing key the same as a
    pre-tie-break registry), but an avoidable extra race window for a file
    this module already goes out of its way to read atomically-consistent
    once.

    Reuses `_read_file` -- the same swallow-every-read-failure primitive
    every pointer read in this file already goes through -- rather than a
    second bespoke try/except, so a non-UTF-8 registry degrades exactly like
    a non-UTF-8 pointer does.
    """
    return _read_file(registry_path(_home_alp_dir()))


def _nearest_ancestor_sdk(start: Path) -> str | None:
    """The nearest ENCLOSING checkout, walking `start`'s parents upward.
    `start` itself is deliberately not probed -- every caller checks it first
    (`tan_core::project::nearest_ancestor_sdk`).

    This is what makes the documented Quickstart resolve: `tan --project
    examples/<cat>/<name>` puts the workspace root levels BELOW the checkout it
    was invoked from (tan-cli #101). The walk yields at most ONE path, so it can
    never turn an otherwise-unambiguous resolution into an ambiguous `None`.
    """
    for ancestor in start.parents:
        if _has_loader_script(ancestor):
            return _to_posix(ancestor)
    return None


def discover_workspace_sdk(workspace_root: Path) -> str | None:
    """Auto-discovery for the `discovery` tier: the workspace root itself or its
    SIBLING `../alp-sdk`, else the nearest enclosing checkout. Two or more
    candidates is ambiguous, which is `None` -- not a choice.

    **Deliberately NOT [`discover_sdk_root`], above**, which is a different
    Rust function (`util.rs`'s `discover_sdk_root`) with a WIDER candidate set:
    it also probes the child `<ws>/alp-sdk` and `../alp-sdk-upstream`, and takes
    the first match rather than requiring uniqueness. `sdk current` must mirror
    `tan_core::discover_workspace_sdk` instead, per `resolve_sdk_tiered`'s own
    doc comment: the tier it reports has to be what build/validate/doctor would
    actually resolve here, or `sourceTier: "discovery"` names a path no other
    command agrees with. Reusing the build-side helper would be the tempting
    de-duplication and it would make the report lie.
    """
    candidates: list[str] = []
    lateral_hit = False

    if _has_loader_script(workspace_root):
        # Normalised BEFORE the dedup below: on Windows a root spelled with
        # backslashes and its `parent/alp-sdk` sibling can name the same
        # directory yet compare unequal as strings, which counted one SDK twice
        # and reported "ambiguous".
        candidates.append(_to_posix(workspace_root))
        lateral_hit = True

    # `parent != self` is pathlib's spelling of Rust's `Path::parent()` returning
    # `None`: at a filesystem/drive root `Path("C:/").parent` is `Path("C:/")`
    # itself, so an unguarded probe would invent a `C:/alp-sdk` candidate the
    # Rust never considers -- flipping `sourceTier` from `none` to `discovery`
    # for anyone whose cwd is a drive root.
    parent = workspace_root.parent
    if parent != workspace_root:
        sibling = _to_posix(parent / "alp-sdk")
        if _has_loader_script(Path(sibling)):
            if sibling not in candidates:
                candidates.append(sibling)
            lateral_hit = True

    # A strict fallback, gated on whether THIS folder's lateral probes answered
    # -- never on the candidate count, which stays unchanged for a folder that
    # resolved perfectly well but deduped against an earlier one.
    if not lateral_hit:
        ancestor = _nearest_ancestor_sdk(workspace_root)
        if ancestor is not None:
            candidates.append(ancestor)

    return candidates[0] if len(candidates) == 1 else None


# ── the four-tier precedence chain ──────────────────────────────────────────


@dataclass(frozen=True)
class ActiveSdk:
    """What `sdk current` reports: the active path (or `None`) and the tier that
    produced it. `tier` is the wire string, camelCase, from `SdkSourceTier`.

    `broken_project_pin` (tan-cli#263) is the raw `sdkPath` a workspace
    `.alp/sdk-path` pointer held when that file existed but its target failed
    the loader-script check -- `None` on every other path, including "no
    pointer file at all". Set once, in the one tier that can discover it,
    then carried through every LOWER tier this call falls through to: a caller
    that reports the tier which finally answered must still be able to say a
    pin existed and did not, rather than silently looking as deliberate as a
    workspace that was never pinned in the first place. Distinct from a
    SIXTH `sourceTier` value on purpose -- the tier that actually supplied
    `path` stays accurate; this is a supplementary fact about a REJECTED
    candidate, reported by the caller via `issues[]` instead.

    `foreign_global_default_for` (tan-cli#464) is the SAME shape of fact for a
    different silence: `~/.alp/sdk-default` is machine-global and
    last-writer-wins across every project that ever relocated a checkout on
    this host, so a caller resolving through the `globalDefault` tier from a
    workspace that is neither the project the pointer was last written for
    nor under the SDK it names is reading an answer left behind by SOMEONE
    ELSE'S bootstrap -- silently, before this. Set only on the `globalDefault`
    tier (the only one this ambiguity can apply to); `None` covers both "the
    pointer was written for (or covers) this workspace" and "the pointer
    predates `writtenFor` and carries no opinion at all" -- deliberately the
    same as `broken_project_pin`'s "no pointer" case, since neither is
    evidence of a mismatch."""

    path: str | None
    tier: str
    broken_project_pin: str | None = None
    foreign_global_default_for: str | None = None


def resolve_sdk_tiered(sdk_root: str | None, workspace_root: Path) -> ActiveSdk:
    """`--sdk-root` > project pin > global default > discovery > nothing
    (`util.rs`'s `resolve_sdk_tiered` + `tan_core`'s `resolve_sdk_source_tier`).

    `--sdk-root` is TERMINAL and returned as-is **even when it is not a
    checkout** (I-31). That is not an oversight to tidy: a bad `--sdk-root`
    surfaces as a `missing` readiness naming the path the user typed, where the
    Pythonic `if not valid: continue` would silently fall through to a lower
    tier and report a DIFFERENT SDK than the one they asked for.

    Both pointer tiers are best-effort by contrast -- each is used only while it
    still points at a real checkout -- so a stale pointer falls through instead
    of locking the user out of every command. The project pin's own fallthrough
    is not silent, though (tan-cli#263): its raw target survives on the
    returned `ActiveSdk.broken_project_pin` no matter which lower tier ends up
    answering, so a caller can report "this workspace IS pinned, and the pin
    does not resolve" instead of looking indistinguishable from a workspace
    that was never pinned at all -- exactly what let a `tan init` run under a
    since-moved project silently re-resolve a DIFFERENT alp-sdk checkout with
    `ok: true`, `issues: []`.

    The `globalDefault` tier carries the SAME kind of silence one tier down
    (tan-cli#464): it is one pointer shared by every project on the host, so a
    caller resolving through it from a workspace the pointer was not written
    for -- and that is not even under the SDK it names -- gets no signal that
    a DIFFERENT project's bootstrap relocation is what actually decided this
    answer. `foreign_global_default_for` names that project when this run hit
    exactly that case; `None` when the pointer covers this caller, or predates
    `writtenFor` and has no opinion.

    tan-cli#466 makes that answer CORRECT, not just disclosed, without moving
    this tier or adding a sixth `SdkSourceTier`: before falling to the single
    legacy pointer, this tier first consults `~/.alp/sdk-defaults.json`, an
    origin-keyed registry every relocating `tan bootstrap` writes alongside
    (never instead of) the legacy file, and picks the DEEPEST registry key
    that contains `workspace_root` (`sdk_default_registry.
    deepest_covering_entry`, using this same `_workspace_under` containment
    test, with a resolved-depth TIE broken by which entry's `updatedAt` is
    most recent -- review, #904 second round, major: two distinct raw
    origins that alias the same directory, e.g. a symlink bootstrapped once
    and the same directory bootstrapped again later through its real path,
    resolve to identical depth). A registry hit still reports `sourceTier:
    "globalDefault"` -- it IS the machine-default mechanism, keyed -- and
    never carries `foreign_global_default_for`: a caller a registry entry
    was written FOR is, by construction, not reading someone else's answer.
    Only when NO registry entry covers the caller does resolution fall
    through to the legacy pointer exactly as it did before this issue,
    foreign-warning included.
    """
    flag = (sdk_root or "").strip()
    if flag:
        return ActiveSdk(flag, "sdkRootFlag")

    broken_project_pin: str | None = None
    pin = _pointer_target(workspace_root / ".alp" / "sdk-path")
    if pin is not None:
        if _has_loader_script(Path(pin)):
            return ActiveSdk(pin, "projectPin")
        broken_project_pin = pin

    registry_raw = _read_global_sdk_registry_raw()
    registry_hit = deepest_covering_entry(
        parse_registry(registry_raw),
        workspace_root,
        covers=_workspace_under,
        has_loader_script=_has_loader_script,
        resolve_origin=_resolved_origin_depth_key,
        updated_at=parse_registry_updated_at(registry_raw),
    )
    if registry_hit is not None:
        _origin, registry_sdk_path = registry_hit
        return ActiveSdk(registry_sdk_path, "globalDefault", broken_project_pin, None)

    default_pointer = _home_alp_dir() / "sdk-default"
    default = _pointer_target(default_pointer)
    if default is not None and _has_loader_script(Path(default)):
        written_for = _pointer_written_for(default_pointer)
        foreign = (
            written_for
            if written_for is not None
            and not _workspace_under(workspace_root, written_for)
            and not _workspace_under(workspace_root, default)
            else None
        )
        return ActiveSdk(default, "globalDefault", broken_project_pin, foreign)

    discovered = discover_workspace_sdk(workspace_root)
    if discovered is not None:
        return ActiveSdk(discovered, "discovery", broken_project_pin)

    return ActiveSdk(None, "none", broken_project_pin)


def project_pin_issue(broken_project_pin: str | None, tier: str) -> Issue | None:
    """The tan-cli#263 warning for an unresolvable `.alp/sdk-path` project pin
    -- shared by EVERY caller of `resolve_sdk_tiered` (directly, or through
    `resolve_sdk_root_ladder`/`resolve_sdk_root_wide`, below), not just `sdk
    current`. `None` when nothing was rejected, so every call site can do
    `issue = project_pin_issue(broken, tier); if issue: issues.append(issue)`
    unconditionally.

    `tan build` is the caller this matters most for: a workspace whose pin
    silently misses still gets a real build, against whichever SDK the ladder
    fell through to, with no signal it was not the one `.alp/sdk-path` names
    -- `sdk current` alone only helps someone already suspicious enough to run
    it."""
    if broken_project_pin is None:
        return None
    # Function-scoped: see the module docstring -- `tan.envelope` imports this
    # module at module level, so a top-level import back would be circular.
    from tan.envelope import Issue

    return Issue(
        "sdk.project-pin-unresolved",
        "warning",
        f'.alp/sdk-path names "{broken_project_pin}", which does not resolve '
        f"to an alp-sdk checkout from the current directory -- falling "
        f"through to the {tier} tier instead.",
    )


def global_default_foreign_project_issue(foreign_global_default_for: str | None) -> Issue | None:
    """The tan-cli#464 warning for a `globalDefault` answer that a DIFFERENT
    project's bootstrap relocation actually decided: `~/.alp/sdk-default` is
    one pointer, shared and last-writer-wins across every project that ever
    relocates a checkout on this host, so the earlier of two projects can
    silently start resolving the later one's SDK the moment the later one
    bootstraps -- `ok: true`, `issues: []`, same as a caller that was never
    pinned at all (the maintainer's own #464 repro).

    `None` when `resolve_sdk_tiered` found nothing to warn about -- the
    pointer covers this caller, or it predates `writtenFor` and carries no
    opinion -- so every call site can do `issue =
    global_default_foreign_project_issue(active.foreign_global_default_for);
    if issue: issues.append(issue)` unconditionally, exactly like
    `project_pin_issue`.

    tan-cli#466: `resolve_sdk_tiered` sets `foreign_global_default_for` ONLY
    when resolution fell all the way through to this single legacy pointer
    -- never for a `~/.alp/sdk-defaults.json` registry hit, which by
    construction was written FOR the resolving workspace. Resolution itself
    is unchanged by THIS warning specifically: the root `sdk current`
    reports here is the same root it would have reported before this
    function existed -- #466 is what changed the root itself, for the
    workspaces a registry entry now covers.
    """
    if foreign_global_default_for is None:
        return None
    # Function-scoped: see `project_pin_issue`, above.
    from tan.envelope import Issue

    return Issue(
        "sdk.global-default-foreign-project",
        "warning",
        f'The machine-global default SDK (~/.alp/sdk-default) was last set by '
        f'a bootstrap relocation in "{foreign_global_default_for}", not by one '
        f"here -- this workspace is falling through to that project's SDK, "
        f"which may not be the checkout you expect. Pin this workspace "
        f"explicitly with `--sdk-root <path>`, or bootstrap here, to stop "
        f"relying on the shared default.",
    )


def sdk_resolution_issues(
    broken_project_pin: str | None, tier: str, foreign_global_default_for: str | None
) -> list[Issue]:
    """`project_pin_issue` + `global_default_foreign_project_issue`, together,
    in the order `flash`/`size`/`image` -- the three callers this actually
    has -- append them: each used to compute this pair only on the happy
    path and skip it on a manifest-gate early return.

    **Not the only copy.** Twelve other modules still hand-copy the pair
    directly rather than through here -- most fold in a third, caller-
    specific issue or use a different order, not the mechanical swap it
    looks like; left as-is (tan-cli#464 review).

    `[]` when neither fires -- a caller can `issues.extend(...)` unconditionally."""
    issues: list[Issue] = []
    pin_issue = project_pin_issue(broken_project_pin, tier)
    if pin_issue is not None:
        issues.append(pin_issue)
    foreign_issue = global_default_foreign_project_issue(foreign_global_default_for)
    if foreign_issue is not None:
        issues.append(foreign_issue)
    return issues


@dataclass(frozen=True)
class SdkRootResolution:
    """`resolve_sdk_root_ladder`/`resolve_sdk_root_wide`'s own return type --
    a NAMED result rather than a tuple, so a fact one caller needs (e.g.
    `foreign_global_default_for`, tan-cli#464) never forces every OTHER
    caller's unpacking to grow a matching positional slot. A four-element
    tuple was tried here first and rejected on review: it would have
    reproduced the exact silent-drop shape tan-cli#464 exists to close, at
    the next field this ladder needs to carry.

    Mirrors [`ActiveSdk`]'s same four facts (this IS `ActiveSdk` for the
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
    walk, skipping `resolve_sdk_tiered` entirely -- so that pointer was
    silently ignored the moment `tan build` ran in that SAME directory,
    unless the checkout also happened to sit beside the project by
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
    [`project_pin_issue`] turns it into the shared `sdk.project-pin-unresolved`
    warning; every caller here should surface it, not just `sdk current` --
    `tan build` is the one that matters most, since it is the command that
    silently builds against whichever SDK the pin's fallthrough landed on.
    `foreign_global_default_for` (tan-cli#464) is the same carry-through for
    `ActiveSdk.foreign_global_default_for`, and
    [`global_default_foreign_project_issue`] is its sibling warning.
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
    unconditionally, exactly like [`project_pin_issue`].

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

    # Function-scoped: see the module docstring -- `tan.envelope` imports
    # THIS function at module level (that top-level import is the whole
    # point of tan-cli#408), so a top-level import back would be circular.
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
