# SPDX-License-Identifier: Apache-2.0
"""`~/.alp/sdk-defaults.json` -- the origin-keyed sibling of the single
machine-global `~/.alp/sdk-default` pointer (tan-cli#466, stage 2 of
tan-cli#464).

**The problem stage 1 (tan-cli#464) left open.** `~/.alp/sdk-default` is one
file, last-writer-wins across every project that ever relocates a checkout on
this host: bootstrap project A, then bootstrap project B, and A's very next
`tan build` silently resolves B's checkout through the `globalDefault` tier.
Stage 1 taught every ladder to DISCLOSE that (`sdk.global-default-foreign-
project`), which stops the silence but not the wrong answer -- A still builds
against B's SDK, now with a warning attached.

**The fix.** `tan bootstrap` records `origin -> sdkPath` in this registry,
where `origin` is the same absolute project root stage 1 already computes as
`writtenFor` -- the directory bootstrap ran in. `sdk_discovery.resolve_sdk_tiered`
then picks the DEEPEST registry key that CONTAINS the caller's
`workspace_root`, using the same containment test
(`sdk_discovery._workspace_under`) stage 1 already uses to decide "foreign". This is
NOT zero filesystem probing (an earlier version of this docstring, and of
`changelog.d/466.fixed.md`, claimed it was, and was wrong -- caught in review,
#904).

**What actually touches the filesystem, and how much (corrected a second time
in review, #904 second round -- the FIRST correction above was itself
wrong on two counts, and is superseded by this paragraph, not by the one
above it).** `covers` (`sdk_discovery._workspace_under`) runs once per registry
entry, unconditionally -- it IS the filter. `has_loader_script` and
`resolve_origin` (the ranking) run ONLY for a candidate that already passed
`covers`, i.e. once per COVERING entry, not once per registry entry.
Measured directly (call counts, not syscalls): a 201-entry registry with a
single covering entry among 200 non-covering siblings makes 201 `covers`
calls and exactly 1 `has_loader_script` call and 1 `resolve_origin` call --
"each candidate" was true only of `covers`.

The registry-size claim was ALSO wrong: resolution cost is
**O(registry size x path depth), not O(registry size) alone** -- every
`covers`/`resolve_origin` call resolves a real path, and `Path.resolve()`
`lstat`s every path COMPONENT, so a deeper caller costs more at the same
registry size. Measured (counting `os.lstat` calls, real directories, the
production `sdk_discovery._workspace_under`/`_has_loader_script`/
`_resolved_origin_depth_key` triple, 21 registry entries that ALL cover the
workspace -- the worst case, nothing skipped by `covers` or
`has_loader_script`): appending 20 extra path components to the SAME
workspace, at an unchanged registry size, costs exactly **420** more
`lstat` calls (21 entries x 1 workspace-resolving call site -- `covers`,
the only one of the two whose argument the measurement deepens -- x 20
components a component-`lstat`ing `Path.resolve()` now walks through).
`resolve_origin` resolves the *origin*, not the workspace, and the
measurement never deepens the origin, so it contributes zero to the delta
-- caught in review, #904 final round, major: an earlier revision of this
paragraph double-counted `resolve_origin` into the factorization (21 x 2 x
20 = 840) despite the measured delta being 420, and a re-derivation at a
different base depth (1,050 -> 1,470, same `420` delta) confirmed 420, not
840, is correct. The delta is reported ALONE, not alongside a base-count
absolute, because the absolute moves with the base depth chosen for the
measurement and an earlier revision of this paragraph cited one (**1,092**
vs **1,512**) without saying what that base depth was -- caught in review,
#904 third round, nit: an independent re-derivation at a different (but
equally unstated) base depth landed on **1,050**/**1,470**, the same `420`
delta but a different pair of absolutes, for no reason a reader of either
number alone could tell. Cite neither this number nor any other syscall
count for this function without also stating the registry shape (size, how
many entries cover the caller) and the base path depth it was measured at --
both factors independently move any ABSOLUTE count; the delta above is the
one number in this paragraph that does not need one, because it was measured
as a difference at two path depths under the same registry shape and cancels
out.

What IS true, and what the issue actually promised, is that there is no
directory WALK -- the candidate set is closed (only directories a real
`tan bootstrap` explicitly ran in can ever appear as a key), so resolution
cost does not grow with how deep the caller's workspace sits BELOW its
covering origin, only with the registry's own size and the ABSOLUTE depth of
the paths involved. A's subdirectory then resolves A's own entry before the
legacy pointer is ever consulted, at any depth below its own origin,
regardless of which project bootstrapped last.

The legacy single pointer is NOT retired. `tan bootstrap` keeps writing it
unconditionally, for skew safety: an old tan that predates this file reads
only `~/.alp/sdk-default` and never opens this one, so a mixed-version fleet
degrades to stage 1's disclosed-but-wrong behaviour, never to a hard failure.
A new tan consults the registry FIRST and falls back to the legacy pointer
only when no entry here covers the caller -- which is also when the stage-1
foreign-project warning still fires, now for a genuinely unregistered caller
rather than for every non-last bootstrap on the host.

**A pure `tan.core` module, deliberately.** The functions that need
filesystem judgement calls (whether one path contains another; whether an
`sdkPath` still names a real checkout; what a raw origin string resolves to)
are `sdk_discovery._workspace_under`, `sdk_discovery._has_loader_script`, and
`sdk_discovery._resolved_origin_depth_key` -- injected into `deepest_covering_entry`
as callables rather than imported, so this module stays free of any
`tan.commands.*` import, the same convention every other `tan/core` module
here already follows. `sdk_discovery.py` and `bootstrap_cmd.py` both import
this module instead of duplicating its parse/format logic.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

#: Both wire shapes this registry's `updatedAt` can carry --
#: `generated_at_iso`/`wall_clock_iso`'s seconds form (`...:00Z`) and
#: milliseconds form (`...:00.000Z`) -- sort correctly against EACH OTHER
#: (millis form always sorts later within the same second, since `.` <
#: any digit... except it doesn't: `'Z'` (0x5A) sorts AFTER `'.'` (0x2E),
#: so a bare seconds-precision stamp outranks a same-second millis stamp as
#: a raw string, backwards from wall-clock order). Only a HAND-EDITED
#: registry can produce this today -- `with_entry`'s one production caller
#: always writes `millis=True` -- but this module explicitly anticipates
#: hand-edited registries (`parse_registry_updated_at`'s own docstring), so
#: the tie-break normalises rather than leaving mixed precision undefined.
_SECONDS_PRECISION_UPDATED_AT = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z$")


def _comparable_updated_at(value: str) -> str:
    """`value`, widened to millisecond precision if it is a bare
    seconds-precision ISO-8601 stamp, so `>` on the result orders by actual
    wall-clock recency instead of by which precision a hand-edited entry
    happened to use. Anything that is not exactly that seconds-precision
    shape -- already-millis, empty (`parse_registry_updated_at`'s
    no-timestamp default), or not a recognisable stamp at all -- passes
    through unchanged: this only closes the one concrete mis-order
    (review, #904 final round, nit), it does not attempt to validate or
    reformat arbitrary hand-edited garbage."""
    match = _SECONDS_PRECISION_UPDATED_AT.match(value)
    return f"{match.group(1)}.000Z" if match else value

#: `~/.alp/<REGISTRY_FILENAME>` -- named distinctly from the legacy
#: `sdk-default` (singular, no extension) so a directory listing of `~/.alp`
#: cannot mistake one file for the other, and so a plain grep for
#: `sdk-default` (the legacy pointer's own name) never silently matches this
#: file too.
REGISTRY_FILENAME = "sdk-defaults.json"


def registry_path(home_alp_dir: Path) -> Path:
    """`<home_alp_dir>/sdk-defaults.json`. `home_alp_dir` is the caller's own
    `~/.alp` (`sdk_discovery._home_alp_dir()`), passed in rather than recomputed
    here -- this module has no opinion on `HOME`/`USERPROFILE` resolution,
    exactly like it has no opinion on filesystem containment."""
    return home_alp_dir / REGISTRY_FILENAME


def parse_registry(raw: str | None) -> dict[str, str]:
    """`origin -> sdkPath`, best-effort. `raw` is the file's text, or `None`
    when it could not be read at all (missing, permission-denied, non-UTF-8 --
    `sdk_discovery._read_file`'s own contract).

    Every failure shape degrades to `{}`, the empty registry -- a truncated
    write from a concurrent `tan bootstrap`, a hand-edited syntax error, a
    JSON array instead of an object, a value that is not itself an object, an
    `sdkPath` that is missing or not a string. `{}` is exactly what "no
    registry file at all" already means to every caller, so a malformed
    registry degrades to the legacy single pointer -- never to a crash, and
    never to a partial/best-guess reading of a corrupt file.
    """
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, str] = {}
    for origin, entry in parsed.items():
        if not isinstance(origin, str) or not origin:
            continue
        sdk_path = entry.get("sdkPath") if isinstance(entry, dict) else None
        if isinstance(sdk_path, str) and sdk_path:
            result[origin] = sdk_path
    return result


def parse_registry_updated_at(raw: str | None) -> dict[str, str]:
    """`origin -> updatedAt`, the recency companion of `parse_registry` --
    feeds `deepest_covering_entry`'s tie-break (review, #904 second round,
    major: two origins that resolve to the SAME directory tie on depth, and
    `depth > best_depth` alone keeps whichever one the sorted-key loop visits
    first, i.e. the lexicographically smallest raw origin -- not the more
    recent bootstrap).

    A SEPARATE function rather than widening `parse_registry`'s own return
    shape, for the same reason `load_raw` is separate from it: `parse_registry`
    is the flattened `origin -> sdkPath` view several callers already depend
    on staying exactly that shape, and every one of its existing callers has
    no use for a timestamp.

    Degrades exactly like `parse_registry` -- `{}` for a missing/malformed
    file, and an origin whose ENTRY is not itself a dict-shaped object is
    DROPPED, the identical shape `parse_registry` gives it (a bare string
    value, `{"/proj/a": "/sdk/a"}`, is not an entry either function
    recognises). Within a dict-shaped entry, a missing or non-string
    `updatedAt` degrades to `""` instead -- an entry with no timestamp
    (written by a pre-tie-break tan, or hand-edited) must still be RANKABLE
    by depth, it only forfeits ties -- `""` sorts before every real
    ISO-8601 stamp, so it never wins one.
    """
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, str] = {}
    for origin, entry in parsed.items():
        if not isinstance(origin, str) or not origin:
            continue
        if not isinstance(entry, dict):
            continue
        updated_at = entry.get("updatedAt")
        result[origin] = updated_at if isinstance(updated_at, str) else ""
    return result


def load_raw(raw: str | None) -> dict[str, Any]:
    """The WRITE side's counterpart to `parse_registry`: the registry's own
    parsed-JSON shape, untouched, or `{}` on any read/parse failure (missing,
    truncated, not even an object).

    Deliberately NOT `parse_registry`'s flattened `origin -> sdkPath` view --
    that view already discards the per-entry object wrapper
    (`{"sdkPath": ..., "updatedAt": ...}`) an entry is stored as, so a writer
    that read the FLATTENED view, patched one key, and wrote it back would
    silently downgrade every OTHER origin's entry to a bare string the next
    read still tolerates (`parse_registry` accepts only dict-shaped entries)
    but which no longer round-trips its own wrapper. `with_entry` is written
    against THIS shape for that reason.
    """
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_absolute_either_platform(value: str) -> bool:
    """Mirrors `sdk_discovery._pointer_written_for`'s own cross-platform absolute
    check, for the same reason: this registry is one file shared by every
    `tan` on the host, and a bare `Path(value).is_absolute()` is answered by
    whichever pathlib flavour the READING platform picked. A relative,
    empty, or drive-relative origin (a hand-edited registry, or a future
    writer bug) is rejected under BOTH flavours rather than accepted under
    whichever one the reader happens to be running."""
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def deepest_covering_entry(
    registry: dict[str, str],
    workspace_root: Path,
    *,
    covers: Callable[[Path, str], bool],
    has_loader_script: Callable[[Path], bool],
    resolve_origin: Callable[[str], str],
    updated_at: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """The deepest `(origin, sdkPath)` entry whose `origin` CONTAINS
    `workspace_root` (`covers`) and whose `sdkPath` still resolves
    (`has_loader_script`), or `None` when nothing qualifies.

    "Deepest" is measured by the length of the origin's RESOLVED path
    (`resolve_origin`), NOT the raw registry key string. That distinction is
    load-bearing, found by review (#904): `covers` (`sdk_discovery._workspace_under`
    in production) decides containment on `.resolve()`d paths, so a symlinked
    origin makes the raw key's string length disagree with `covers`'s own
    notion of "deeper" -- two origins reached through a symlink can be
    identical, or even INVERTED in order, once resolved, while differing in
    raw length. (A path-segment COUNT of the raw key does not fix this
    either: a symlink can collapse or expand the segment count exactly as it
    can the character count.) Ranking by the SAME resolved value `covers`
    uses restores the exactness this function claims: every candidate this
    loop ranks has already passed `covers(workspace_root, origin)`, so any
    two of them are both (resolved) ancestors of the SAME resolved
    `workspace_root` -- one ancestor of two ancestors of one point is always
    an ancestor of the other (or they are the same directory), which makes
    "contains the other's RESOLVED string as a prefix" and "is the shorter
    RESOLVED string" the identical fact for this already-filtered set.

    `resolve_origin` is injected the same way `covers` and `has_loader_script`
    are, for the same reason: this module stays free of any direct filesystem
    touch, even one as innocuous-looking as `Path.resolve()`.

    A covering entry whose `sdkPath` fails `has_loader_script` is SKIPPED,
    not merely deprioritised: the next-deepest covering entry (if any) is
    tried instead, the same way the single legacy pointer degrades to a lower
    tier today when ITS target fails the identical check -- a stale entry
    left behind by a checkout that moved or was deleted must not block a
    shallower, still-valid one from answering.

    **A resolved-depth TIE is broken by `updated_at` recency, most-recent
    wins** (review, #904 second round, major). Two DISTINCT raw origins can
    resolve to the exact SAME directory -- a symlinked alias bootstrapped
    once, then the same directory bootstrapped AGAIN later through its real
    path -- and then tie on `depth` exactly. The pre-fix `depth > best_depth`
    strict inequality never replaces a tie, so whichever origin
    `registry.items()` visits FIRST keeps the win; since the file is written
    with `sort_keys=True` (`registry_text`), that is the lexicographically
    SMALLEST raw origin string, an accident of spelling with no relationship
    to which bootstrap is authoritative. `updated_at` (origin -> its entry's
    `updatedAt`, `sdk_default_registry.parse_registry_updated_at`) is
    threaded through the same way `resolve_origin` is -- plain data, not a
    filesystem touch -- and an origin missing a timestamp (a registry
    written before this fix, or hand-edited) degrades to `""`, which never
    outranks a real stamp and never breaks an existing tie either, so it is
    exactly as decided as it always was. The comparison itself runs on
    `_comparable_updated_at`'s output, not the raw string (review, #904
    final round, nit): a raw string compare ranks a bare seconds-precision
    stamp AHEAD of a same-second milliseconds one (`'Z'` > `'.'`), which
    only a hand-edited entry can produce today, but this module already
    anticipates hand-edited registries, so the tie-break normalises
    precision rather than leaving that case undefined.

    **Why recency, not collapsing the registry KEY onto its resolved origin
    at write time (the other fix considered, and rejected, in review #904
    second round):** resolving at WRITE time and storing that resolved value
    AS the key would reintroduce, for the key, precisely the staleness this
    function's own `resolve_origin` injection exists to avoid for the
    RANKING -- a value baked in once at write time that the live filesystem
    can silently outgrow (a symlink repointed after the fact, without a new
    `tan bootstrap` ever touching that origin again), with no
    `has_loader_script`-style fallback able to catch it, because the
    original raw origin -- the only thing a future write could still
    recognise -- would already be gone, overwritten by whatever it resolved
    to at the moment it was written. Recency needs none of that: `origin`
    keeps meaning exactly what `tan bootstrap` was run in for every entry,
    forever, matching what `written_for`/`global_default_pointer_fix_hint`
    already report elsewhere in this system, and the tie-break is decided by
    a plain, append-only timestamp instead of a second filesystem-truth
    baked into the registry's own keys.
    """
    updated_at = updated_at or {}
    best: tuple[str, str] | None = None
    best_depth = -1
    best_updated_at = ""
    for origin, sdk_path in registry.items():
        if not _is_absolute_either_platform(origin):
            continue
        if not covers(workspace_root, origin):
            continue
        if not has_loader_script(Path(sdk_path)):
            continue
        depth = len(resolve_origin(origin).replace("\\", "/"))
        entry_updated_at = _comparable_updated_at(updated_at.get(origin, ""))
        if depth > best_depth or (depth == best_depth and entry_updated_at > best_updated_at):
            best_depth = depth
            best_updated_at = entry_updated_at
            best = (origin, sdk_path)
    return best


def with_entry(
    registry: dict[str, Any], *, origin: str, sdk_root: str, updated_at: str
) -> dict[str, str]:
    """`registry`, a raw parsed-JSON dict (may hold entries in any shape --
    this is the WRITE side, applied to whatever `parse_registry`-adjacent
    reading a writer already did), with `origin` keyed to `{"sdkPath":
    sdk_root, "updatedAt": updated_at}`. Every other key is left untouched,
    so one `tan bootstrap` run updating its own origin never drops another
    project's entry.

    **`updated_at` is the caller's to compute, not this function's** (review,
    #904 second round, major -- `deepest_covering_entry`'s recency
    tie-break): this module stays free of the wall clock the same way it
    stays free of the filesystem, so every OTHER function here is a plain,
    deterministic function of its arguments and stays that way, testable
    without freezing time. The production caller
    (`bootstrap_cmd._write_global_sdk_registry`) passes
    `tan.core.timestamp.wall_clock_iso(millis=True)` -- **not**
    `generated_at_iso`, and that distinction is load-bearing (review, #904
    third round, major): `generated_at_iso` lets `SOURCE_DATE_EPOCH` win over
    the clock, which is exactly right for a reproducible envelope and exactly
    wrong here, because this field's entire job is to ORDER two writes
    against each other on one host -- a value every bootstrap inside one
    `SOURCE_DATE_EPOCH`-pinned shell would render identically, silently
    reopening the very tie this tie-break exists to close. Millisecond
    precision, deliberately finer than the legacy pointer's own `updatedAt`
    (seconds, `_global_sdk_pointer_json`), because a whole SDK relocation
    plus this write easily completing inside one second is not a corner case
    worth losing the tie-break to.

    **This function alone is still append-only** (review, #904, nit 1): it
    only ever adds or overwrites `origin`, never drops another entry, even a
    dead one. Pruning is a SEPARATE function, `prune_dead_origins` below
    (tan-cli#905) -- the production writer
    (`bootstrap_cmd._write_global_sdk_registry`) runs it right alongside this
    one, on every relocating `tan bootstrap`, so the two together keep the
    registry both current (this function) and bounded (that one) in the same
    read-modify-write. See `prune_dead_origins`'s own docstring for what
    "dead" means and why.
    """
    updated = dict(registry)
    updated[origin] = {"sdkPath": sdk_root, "updatedAt": updated_at}
    return updated


def prune_dead_origins(
    registry: dict[str, Any], *, origin_exists: Callable[[str], bool]
) -> dict[str, Any]:
    """`registry` with every entry whose ORIGIN directory no longer exists on
    this host dropped -- `with_entry`'s "append-only; nothing here prunes"
    gap, closed (tan-cli#905).

    **What "dead" means here, and why this test and no other.** The module
    docstring above already establishes the candidate set is closed: a key
    can only be here because a real `tan bootstrap` ran in that directory.
    `covers` (`sdk_discovery._workspace_under`) decides a match by STRING
    containment on `.resolve()`d paths and does not itself require either
    side to exist -- so the one fact that makes an origin permanently
    unmatchable, not merely unmatched by the CURRENT invocation, is that its
    own directory is gone: no process can set its current working directory
    to a path that does not exist, so no future `workspace_root` can ever
    again be produced that resolves under a deleted origin (barring the
    coincidence of something unrelated being created at that exact path
    later, at which point the next real `tan bootstrap` there overwrites
    this entry anyway, exactly as it would for a brand-new origin).

    **Explicitly NOT the criterion: a dead `sdkPath`.**
    `deepest_covering_entry` already SKIPS a covering entry whose `sdkPath`
    fails `has_loader_script`, deliberately, because that origin's PROJECT
    may simply be between bootstraps -- a checkout mid-move, or a project
    that has not re-bootstrapped since its SDK relocated. That entry is
    "merely unused right now", not dead: the very next `tan bootstrap` run
    from that origin repairs it for free, and pruning it first would only
    force a needless rediscovery. Origin existence is the only test this
    module can run that tells the two apart without guessing at intent
    (age-based pruning was considered and rejected for the same reason --
    see the issue -- a long-idle but still-real project origin is
    indistinguishable from an abandoned one by timestamp alone, and getting
    that wrong deletes a live project's entry, not a stale one).

    Not a filesystem touch itself: `origin_exists` is injected the same way
    `covers`/`has_loader_script`/`resolve_origin` are injected into
    `deepest_covering_entry`, so this module stays free of any direct `Path`
    IO. The production caller (`bootstrap_cmd._write_global_sdk_registry`)
    degrades an INCONCLUSIVE check (a `PermissionError`, or any other
    exception) to "exists" -- an entry is dropped only on a confirmed
    absence, never on an inability to check one, the same conservative bias
    every other best-effort read in this module already applies.

    Applies to every key in `registry`, including one shaped too oddly for
    `parse_registry`/`parse_registry_updated_at` to have kept (a bare string
    value, a non-dict entry): a malformed entry whose origin is gone is
    exactly as dead as a well-formed one, and skipping it would leave the one
    thing this function exists to bound -- the registry's own unbounded
    growth (tan-cli#905's own complaint) -- unfixed for exactly the shapes
    most likely to belong to a hand-edited or long-abandoned entry.

    No concurrency story of its own: called from the SAME read-modify-write
    `with_entry` already is, so a prune racing a second `tan bootstrap`'s
    write carries the identical last-writer-wins tolerance
    `_write_global_sdk_registry` already accepts for that write -- the
    LOSER's entry (add or prune) is simply overwritten by the winner's next
    write, never corrupted or partially applied.
    """
    return {origin: entry for origin, entry in registry.items() if origin_exists(origin)}


def prune_entries_by_sdk_path(registry: dict[str, Any], *, sdk_path: str) -> dict[str, Any]:
    """`registry` with every entry whose `sdkPath` equals `sdk_path` dropped --
    `tan sdk remove`'s own honesty obligation (tan-cli#790), the sibling of
    [`prune_dead_origins`] pruning on the OTHER axis: that function drops an
    entry whose ORIGIN (the project that registered it) no longer exists;
    this one drops an entry whose TARGET (the SDK checkout it points at) was
    just deleted by this very command.

    Without this, a removed install stays registered as SOME other project's
    machine-global default: that project's very next `sdk current`/`tan
    build` would still answer `sourceTier: "globalDefault"` naming a path
    that is now gone, degrading only as far as `check_sdk_readiness`'s
    `state: "missing"` -- correctly REPORTED, but never repaired, and never
    falling through to a lower tier the way a dead `~/.alp/sdk-default`
    pointer already does (`resolve_sdk_tiered`'s `_has_loader_script` gate on
    that pointer). `deepest_covering_entry` applies the identical
    `has_loader_script` gate to a registry hit too, so leaving a dead entry
    in place would not silently mis-resolve anyone -- it would just leave a
    permanently-unusable entry sitting in a file tan-cli#905 already exists to
    keep bounded, for a project whose bootstrap the very next re-run repairs
    for free regardless. This function closes the gap anyway, on the same
    reasoning tan-cli#905's own docstring gives for pruning eagerly rather
    than waiting for staleness to matter: the registry is honest about what
    still resolves, not just eventually self-correcting about it.

    `sdk_path` is the caller's to normalise (`sdk_discovery._abs_posix`,
    posix-normalised the same way [`with_entry`]'s own `sdk_root` argument
    already is) -- this module has no opinion on path normalisation, the same
    division every other function here draws.

    Compares only entries whose `sdkPath` is present and a string; anything
    shaped too oddly for `parse_registry` to have kept either cannot name
    `sdk_path` at all (no string to compare) or is exactly as findable by
    [`prune_dead_origins`]'s own broader sweep -- this function narrows on
    purpose, to the one axis its caller actually knows just changed.
    """
    result: dict[str, Any] = {}
    for origin, entry in registry.items():
        path = entry.get("sdkPath") if isinstance(entry, dict) else None
        if isinstance(path, str) and path == sdk_path:
            continue
        result[origin] = entry
    return result


def registry_text(registry: dict[str, Any]) -> str:
    """The committed-to-disk rendering: sorted keys (a deterministic diff
    across runs, and a stable read order for a human `cat`ing the file), two-
    space indent matching every other pointer file this repo writes, a
    trailing newline."""
    return json.dumps(registry, indent=2, sort_keys=True) + "\n"
