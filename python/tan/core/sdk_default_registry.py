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
`writtenFor` -- the directory bootstrap ran in. `sdk_cmd.resolve_sdk_tiered`
then picks the DEEPEST registry key that CONTAINS the caller's
`workspace_root`, using the same containment test
(`sdk_cmd._workspace_under`) stage 1 already uses to decide "foreign". This is
NOT zero filesystem probing (an earlier version of this docstring, and of
`changelog.d/466.fixed.md`, claimed it was, and was wrong -- caught in review,
#904): `covers` and `has_loader_script` each touch the filesystem (`.resolve()`,
`.exists()`) for every candidate, and `deepest_covering_entry`'s own ranking
now does too (see below). What IS true, and what the issue actually promised,
is that there is no directory WALK -- the candidate set is closed (only
directories a real `tan bootstrap` explicitly ran in can ever appear as a
key), so resolution cost is O(registry size), not O(filesystem depth). A's
subdirectory then resolves A's own entry before the legacy pointer is ever
consulted, at any depth, regardless of which project bootstrapped last.

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
are `sdk_cmd._workspace_under`, `sdk_cmd._has_loader_script`, and
`sdk_cmd._resolved_origin_depth_key` -- injected into `deepest_covering_entry`
as callables rather than imported, so this module stays free of any
`tan.commands.*` import, the same convention every other `tan/core` module
here already follows. `sdk_cmd.py` and `bootstrap_cmd.py` both import this
module instead of duplicating its parse/format logic.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

#: `~/.alp/<REGISTRY_FILENAME>` -- named distinctly from the legacy
#: `sdk-default` (singular, no extension) so a directory listing of `~/.alp`
#: cannot mistake one file for the other, and so a plain grep for
#: `sdk-default` (the legacy pointer's own name) never silently matches this
#: file too.
REGISTRY_FILENAME = "sdk-defaults.json"


def registry_path(home_alp_dir: Path) -> Path:
    """`<home_alp_dir>/sdk-defaults.json`. `home_alp_dir` is the caller's own
    `~/.alp` (`sdk_cmd._home_alp_dir()`), passed in rather than recomputed
    here -- this module has no opinion on `HOME`/`USERPROFILE` resolution,
    exactly like it has no opinion on filesystem containment."""
    return home_alp_dir / REGISTRY_FILENAME


def parse_registry(raw: str | None) -> dict[str, str]:
    """`origin -> sdkPath`, best-effort. `raw` is the file's text, or `None`
    when it could not be read at all (missing, permission-denied, non-UTF-8 --
    `sdk_cmd._read_file`'s own contract).

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


def load_raw(raw: str | None) -> dict[str, Any]:
    """The WRITE side's counterpart to `parse_registry`: the registry's own
    parsed-JSON shape, untouched, or `{}` on any read/parse failure (missing,
    truncated, not even an object).

    Deliberately NOT `parse_registry`'s flattened `origin -> sdkPath` view --
    that view already discards the per-entry object wrapper
    (`{"sdkPath": ...}`) an entry is stored as, and future fields on an
    entry (this issue adds none, but the shape leaves room), so a writer
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
    """Mirrors `sdk_cmd._pointer_written_for`'s own cross-platform absolute
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
) -> tuple[str, str] | None:
    """The deepest `(origin, sdkPath)` entry whose `origin` CONTAINS
    `workspace_root` (`covers`) and whose `sdkPath` still resolves
    (`has_loader_script`), or `None` when nothing qualifies.

    "Deepest" is measured by the length of the origin's RESOLVED path
    (`resolve_origin`), NOT the raw registry key string. That distinction is
    load-bearing, found by review (#904): `covers` (`sdk_cmd._workspace_under`
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
    """
    best: tuple[str, str] | None = None
    best_depth = -1
    for origin, sdk_path in registry.items():
        if not _is_absolute_either_platform(origin):
            continue
        if not covers(workspace_root, origin):
            continue
        if not has_loader_script(Path(sdk_path)):
            continue
        depth = len(resolve_origin(origin).replace("\\", "/"))
        if depth > best_depth:
            best_depth = depth
            best = (origin, sdk_path)
    return best


def with_entry(registry: dict[str, Any], *, origin: str, sdk_root: str) -> dict[str, str]:
    """`registry`, a raw parsed-JSON dict (may hold entries in any shape --
    this is the WRITE side, applied to whatever `parse_registry`-adjacent
    reading a writer already did), with `origin` keyed to `{"sdkPath":
    sdk_root}`. Every other key is left untouched, so one `tan bootstrap` run
    updating its own origin never drops another project's entry.

    **This registry is append-only; nothing here prunes a dead origin**
    (review, #904, nit 1). An origin whose project directory or checkout was
    later deleted or moved without a further `tan bootstrap` stays in the
    file forever -- not a correctness bug (`deepest_covering_entry` simply
    never matches it, or skips it via `has_loader_script` if its `sdkPath`
    also went stale), but every future `resolve_sdk_tiered` call still pays a
    `covers`/`has_loader_script` filesystem check for it, and the file only
    grows. No `bootstrap`/`doctor`/`clean` path prunes an entry today.
    Deliberately out of scope for tan-cli#466/#904 -- pruning needs its own
    design (age-based? existence-based? on which command?) and its own
    review, not a rider on this one. Follow-up: tan-cli#905.
    """
    updated = dict(registry)
    updated[origin] = {"sdkPath": sdk_root}
    return updated


def registry_text(registry: dict[str, Any]) -> str:
    """The committed-to-disk rendering: sorted keys (a deterministic diff
    across runs, and a stable read order for a human `cat`ing the file), two-
    space indent matching every other pointer file this repo writes, a
    trailing newline."""
    return json.dumps(registry, indent=2, sort_keys=True) + "\n"
