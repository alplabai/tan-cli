# SPDX-License-Identifier: Apache-2.0
"""`tan sdk remove` (tan-cli#790) -- the pure/near-pure half: resolving
`<version|path>` against `--destination`, sizing a tree before it is gone,
and performing the removal itself with the same read-only-retry hardening
`tan clean` already carries (`tan.core.dir_removal`, tan-cli#790 same
change), plus this verb's OWN two failure classes clean's caller never had
to tell apart -- see [`classify_removal_error`].

**Why this exists at all, not just a hand-deleted tree.** alp-sdk-vscode's
own removal was one line, `fs.rmSync(target, { recursive: true, force: true
})` -- `force` swallows only `ENOENT`, so a locked file (a terminal cwd'd
into the tree, a build mid-flight, an indexer) or a permission it cannot
clear surfaces as a raw, unclassified `EPERM`/`EBUSY` with no verdict a
consumer can act on. Every OTHER consumer of a `tan`-managed SDK cache (a CI
script rotating versions, a clean-host reset) was left to reimplement the
identical judgement call by hand. This module is the ONE place that makes
it.

**`tan.core`, not `tan.commands.sdk_cmd`.** Keeps the command file a thin
orchestrator (arg parsing, the resolution-ladder call, the envelope) over
logic that is unit-testable without spawning a subprocess, the same split
`tan.core.sdk_discovery`'s own module docstring argues for. Filesystem IO
lives here anyway -- like `tan.core.dir_removal`'s own removal primitives,
and like `sdk_discovery`'s pointer/registry reads, "no direct command
import" is the invariant this module keeps, not "no IO at all".
"""
from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path

from tan.core.dir_removal import os_error_text, remove_dir
from tan.core.sdk_default_registry import is_absolute_either_platform


def looks_like_path(raw: str) -> bool:
    """Whether `raw` -- the caller's `<version|path>` argument, unstripped of
    its own meaning by this point -- names an explicit filesystem location
    rather than a bare cache-relative version name.

    A path SEPARATOR (either flavour, so a POSIX-spelled path is still
    recognised when typed on Windows and vice versa -- the same
    either-platform tolerance `sdk_default_registry.is_absolute_either_platform`
    already applies to a value this same registry stores), an absolute path on
    THIS platform, or the two relative-navigation tokens `.`/`..` (which
    contain no separator yet plainly do not name a released version) all
    count. Everything else -- `v0.15.0`, `0.15.0-rc1`, a bare filename with no
    separator -- is a version name, looked up under `--destination`.
    """
    return (
        "/" in raw
        or "\\" in raw
        or raw in (".", "..")
        or Path(raw).is_absolute()
    )


@dataclass(frozen=True)
class RemovalTarget:
    """What `<version|path>` resolved to, plus the two facts the caller needs
    to police it: whether it was a bare version name (always inside
    `destination` by construction, so the outside-root refusal can never
    apply to it) and the cache root it was resolved against."""

    target: Path
    destination: Path
    is_named_version: bool


def resolve_removal_target(raw_arg: str, destination: Path) -> RemovalTarget:
    """`raw_arg` (already stripped and confirmed non-empty by the caller)
    resolved against `destination` -- the `--destination` value, or
    `sdk_cmd._default_cache_root()`'s default, either way the caller's to
    compute and pass in, since this module has no opinion on `~/.alp`
    resolution (the same division `sdk_default_registry` already draws
    against `sdk_discovery._home_alp_dir`).

    A bare version name is joined onto `destination` UNCONDITIONALLY -- never
    checked against what is actually installed there, because "does this
    version exist" is exactly the idempotent-absence question the caller
    answers next, not this one.
    """
    if looks_like_path(raw_arg):
        return RemovalTarget(Path(raw_arg), destination, False)
    return RemovalTarget(destination / raw_arg, destination, True)


def is_outside_cache_root(target: Path, destination: Path) -> bool:
    """Whether `target` sits outside `destination` -- the tan-cli#790 footgun
    guard: "refuse anything outside the cache root unless an explicit path is
    given and confirmed by --force". A bare version name can never trip this
    (`resolve_removal_target` joins it onto `destination` itself), so callers
    only need to ask this for the explicit-path arm.

    Lexical containment (`os.path.abspath`, not `Path.resolve()`) for the same
    reason `sdk_discovery._abs_posix` gives: cwd-anchored and stable for a
    path that does not exist yet, which a removal target -- by definition,
    once it is gone -- must still compare correctly against on a re-run.
    `Path.is_relative_to` never touches the filesystem either (a PurePath-only
    comparison of the already-lexically-normalised `abspath` strings), so a
    Windows cross-drive pair (`C:\\x` vs `D:\\y`) answers plainly `False` --
    "not relative to" -- rather than raising, the way `os.path.commonpath`
    would on the same pair.
    """
    root = Path(os.path.abspath(str(destination)))
    candidate = Path(os.path.abspath(str(target)))
    return not (candidate == root or candidate.is_relative_to(root))


def is_cache_root_itself(target: Path, destination: Path) -> bool:
    """Whether `target` resolves to `destination` EXACTLY -- the case
    `is_outside_cache_root` treats as fine (`candidate == root` is
    deliberately not "outside") but that is in fact the single most
    destructive target this command can be pointed at: every install under
    the cache root at once, not one of them. Found live, by measurement, not
    by reading: `tan sdk remove .` from inside an otherwise-empty cwd, or
    `tan sdk remove <dest> --destination <dest>`, deleted a two-version cache
    root outright, `ok: true`, no `--force` required, because `--destination`
    itself is never checked against `_load_bearing_reasons` (that ladder only
    ever names a specific VERSION subdirectory, never the root that holds
    them) and `is_outside_cache_root` explicitly exempts the equal case so a
    caller CAN still name the root on purpose elsewhere.

    Same lexical-abspath comparison as `is_outside_cache_root`, for the same
    reason (stable for a target that will not exist once removed).
    """
    root = Path(os.path.abspath(str(destination)))
    candidate = Path(os.path.abspath(str(target)))
    return candidate == root


# ── would removing THIS take out THAT? (tan-cli#1053) ───────────────────────
#
# Every load-bearing `sdk remove` refusal asks one question -- "would deleting
# the target destroy this other thing?" -- and through `dev` asked it four
# separate times with a plain string `==` between two spellings. Every one of
# those failed in the UNSAFE direction: a miss does not merely skip a tidy-up,
# it means the refusal never fires, `remove` proceeds without `--force`, and
# the install another project still points at is silently orphaned -- exactly
# the outcome tan-cli#790's first design bar exists to prevent.
#
# The question is ASYMMETRIC, which is why the helper below is not called
# "same directory" and does not take two interchangeable operands. `remove_dir`
# unlinks a symlink it is handed and never follows it, so removing a LINK
# destroys nothing behind the link, while removing the real directory a link
# points AT does orphan whoever resolves through that link. One direction is
# load-bearing and the other is not, and a symmetric predicate gets one of them
# wrong -- measured, in review, on the first version of this code (see the
# `islink` arm below).
#
# THREE ARMS, in this order.
#
#   1. `os.path.normcase` -- the stdlib's own platform-aware spelling fold. On
#      Windows (`ntpath`) it lowercases AND flips separators, so
#      `C:/Users/Me/sdk` and `c:/users/me/sdk` compare equal there; on POSIX
#      (`posixpath`) it is the IDENTITY, so `/home/me/sdk` and `/home/Me/sdk`
#      stay the two genuinely different directories they really are. That is
#      why the fold belongs here and NOT inside
#      `sdk_default_registry.normalized_sdk_path`, whose own docstring makes
#      the same argument: that helper folds SEPARATORS, and a case fold there
#      would simply be wrong on POSIX. Checked FIRST and unconditionally, so
#      naming the identical path always answers True -- including when that
#      path is itself a link, which the next arm would otherwise veto.
#   2. `os.path.islink(target)` -- switch to `os.path.samestat` over two
#      `os.lstat`s, which follows NEITHER final component. Removing a symlink
#      unlinks the link and leaves everything behind it exactly where it was
#      (`dir_removal.remove_dir`, and `compute_tree_bytes` charging the link's
#      own `lstat` size, are already built on that same fact), so the only
#      thing such a removal CAN take out is that same link under another
#      spelling. Without this arm the `samefile` arm below fires in the wrong
#      direction: measured on the first version of this change, a cache
#      holding `v0.19.0` plus `current -> v0.19.0` with the workspace pinned
#      at the REAL directory refused `tan sdk remove <cache>/current` as "the
#      active alp-sdk for this workspace", and reported `data.wasActive:
#      true`, in the very same envelope whose `resolvesToAfter` said the
#      workspace still resolved at `projectPin` to a live SDK. `--force` then
#      proved the refusal empty: the link was unlinked and both the install
#      and the pin survived untouched.
#
#      A blanket `return False` here was the SECOND version, and it was too
#      wide in the other direction (review, round 2): with `cache/current ->
#      v0.19.0` and `alias -> cache`, a workspace pinned at
#      `<T>/alias/current` -- the SAME link, spelled through the alias -- lost
#      it to `tan sdk remove <cache>/current` with no `--force` at all, going
#      `projectPin` -> `none`. `samestat` over `lstat` keeps the harmless
#      case allowed and refuses that one: two spellings of one LINK share an
#      inode, a link and its own target do not.
#   3. `os.path.samefile` -- consulted only when the lexical arm missed and
#      the target is not a link. `normcase` alone is NOT the fix, because it
#      is the identity on darwin too while macOS's DEFAULT APFS volume is
#      case-INSENSITIVE: the maintainer laptop this was measured on would
#      still have orphaned the project. A blanket "fold on darwin" would be
#      wrong in the other direction (macOS can be formatted case-sensitively),
#      and probing the volume by writing a differently-cased test file mutates
#      a filesystem this command is about to delete from, needs write
#      permission it may not have, and cannot answer for a path that is
#      already gone. `samefile` IS that probe, narrowed to exactly the pair
#      being compared and mutating nothing: identical `st_dev`/`st_ino` is the
#      filesystem's own answer, correct on a case-sensitive volume (two real
#      directories have two inodes) and on a case-insensitive one alike.
#
# WHAT IT STILL GETS WRONG, stated rather than discovered later:
#
#   * `samefile` RAISES for a path that does not exist -- and a removal target
#     legitimately stops existing partway through this command -- so that arm
#     degrades to the lexical answer rather than propagating. On a
#     case-insensitive volume a comparison against an ALREADY-ABSENT path
#     still misses. Narrow by construction: `_run_remove` only reaches its
#     load-bearing ladder after `os.path.lexists(target)`, and a counterpart
#     that no longer exists is not load-bearing -- removing the target cannot
#     orphan it any further than it already is.
#   * The `samefile` arm is skipped entirely unless BOTH sides are absolute
#     (under either platform's flavour, since the registry it compares values
#     from is one file shared by every `tan` on the host). A relative stored
#     value would otherwise be anchored to the REMOVING process's cwd and
#     invent a match its writer never wrote -- the precise hazard
#     `normalized_sdk_path` refuses `_abs_posix` for.
#   * The `islink` veto looks only at the target's FINAL component, exactly as
#     `remove_dir` does. An INTERMEDIATE link in the target's path
#     (`<cache-link>/v0.19.0`, where `cache-link -> cache`) is therefore not
#     vetoed, and correctly so: removing that path really does delete the real
#     `v0.19.0` directory and really would orphan whoever points at it.
#   * `samefile` still FOLLOWS symlinks on the CANDIDATE side. An install
#     whose pin/registry entry is spelled through a link now matches the real
#     directory being removed -- a widening in the SAFE direction (removing it
#     really would orphan that workspace), but a behaviour change: `--force`
#     is required there where a plain `==` let it through.
#   * `ntpath.normcase` folds case unconditionally, including inside a
#     directory carrying Windows' per-directory case-sensitivity flag
#     (`fsutil file setCaseSensitiveInfo`). Two genuinely distinct
#     directories there compare equal and the removal refuses -- an
#     over-refusal, recoverable with `--force`, taken over the silent-orphan
#     alternative.


def removal_would_take_out(candidate: str, target: str) -> bool:
    """Whether removing `target` would destroy `candidate` -- the single
    comparison every load-bearing `sdk remove` refusal makes, replacing five
    independent `==`. ASYMMETRIC on purpose: `removal_would_take_out(link,
    real_dir)` is True while `removal_would_take_out(real_dir, link)` is
    False, because unlinking a link destroys nothing behind it -- while two
    spellings of the SAME link are still one thing, and still True. See the
    section banner above for the three arms and for what this still gets
    wrong.
    """
    if os.path.normcase(candidate) == os.path.normcase(target):
        return True
    if not (is_absolute_either_platform(candidate) and is_absolute_either_platform(target)):
        return False
    try:
        if os.path.islink(target):
            # Removing a link unlinks the LINK, so the only thing this
            # removal can take out is that same link under another spelling
            # -- `lstat` on BOTH sides, which follows neither final
            # component, and never `samefile`, which follows both.
            return os.path.samestat(os.lstat(candidate), os.lstat(target))
        return os.path.samefile(candidate, target)
    except (OSError, ValueError):
        # Missing, unreadable, or containing a NUL -- the same best-effort
        # degrade every other filesystem read in this module applies. `False`
        # here is the lexical arm's answer, already computed above.
        return False


def compute_tree_bytes(path: Path) -> int:
    """Best-effort total size of `path` -- a single file/symlink's own size, or
    the sum of every regular file under it. `0` for anything that does not
    exist or that this process cannot even list; a size this process could not
    fully account for is reported as the smaller, honest number rather than
    guessed upward.

    `os.lstat`, not `os.stat`: a symlink is charged for ITS OWN size, never
    the size of whatever it points at, which may sit outside this tree
    entirely (or, if it is a self-referencing loop, infinitely) and must not
    be charged against a removal that will not touch it either --
    `dir_removal.remove_dir` never follows a link out of the tree, and this
    accounting stays consistent with that.

    Per-entry errors (a permission-denied file, a vanished entry raced by a
    concurrent process) are swallowed via `onerror`/skip rather than aborting
    the whole walk -- this is an advisory `data.freedBytes` figure, not a
    number anything downstream keys a decision on, so a partial count beats no
    count.
    """
    try:
        if path.is_symlink() or path.is_file():
            return os.lstat(path).st_size
    except OSError:
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _err: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


#: `OSError.errno` values that mean "a process holds this open right now",
#: not "permission denied" -- POSIX's own distinction between the two verbs
#: the tan-cli#790 issue asks to keep apart. `ETXTBSY` is the specific case of
#: a running interpreter/executable inside the tree (a workspace venv's own
#: `python`), which is exactly the shape a `tan`-managed SDK cache can hit.
_IN_USE_ERRNOS = frozenset({errno.EBUSY, errno.ETXTBSY})

#: The Windows sibling: `ERROR_SHARING_VIOLATION` (another process opened the
#: file without share-delete) and `ERROR_LOCK_VIOLATION` (a byte-range lock
#: held on it). Both are "something has this open", never "the ACL forbids
#: it" -- `ERROR_ACCESS_DENIED` (5) is the permission arm and is NOT in this
#: set, so it falls through to the default classification below.
_IN_USE_WINERRORS = frozenset({32, 33})


def classify_removal_error(err: OSError) -> str:
    """`"in-use"` (something holds a handle) or `"permission"` (an ACL/mode
    bit `dir_removal.remove_dir`'s own read-only retry could not clear) --
    the tan-cli#790 issue's own two verdicts, and the whole reason this
    function exists rather than one generic `sdk.remove.failed`: "distinguish
    the two failure modes ... a wrong verdict sends them hunting a holder
    that does not exist" (the issue's own words, restated in its own
    follow-up comment as the part that survived the Windows-readonly
    correction unchanged).

    Checked in this order deliberately: `winerror`, when present, is the more
    specific signal a Windows `OSError` carries alongside a translated
    `errno` that may not agree with it (the same precedence
    `bootstrap_cmd._origin_exists` already gives `winerror` over `errno` for
    an unrelated classification, and `dir_removal.os_error_text` gives it for
    display) -- so it is consulted first, and only a `winerror` this function
    does not recognise falls through to the errno table, never the reverse.
    """
    winerror = getattr(err, "winerror", None)
    if winerror is not None:
        if winerror in _IN_USE_WINERRORS:
            return "in-use"
        return "permission"
    if err.errno in _IN_USE_ERRNOS:
        return "in-use"
    return "permission"


def _win_long_path(path: Path) -> str:
    """`path`, prefixed for Windows' `\\\\?\\` extended-length namespace,
    which lifts the legacy 260-character `MAX_PATH` ceiling outright instead
    of merely detecting it after the fact -- the tan-cli#790 issue's own
    closing note calls exactly this shape of tree ("about 3 GB and deep --
    `modules/`, `.venv`, `zephyr`") the one "exactly the shape that trips it
    on a host without long-path support", and a removal is the one operation
    where working around the limit is strictly better than reporting it: a
    `tan sdk install` that got the tree onto disk in the first place already
    proved the HOST can hold these paths, so what usually cannot re-open them
    afterwards is the WIN32 (not NT) API surface a bare `os.remove`/`os.rmdir`
    goes through -- the very thing this prefix routes around, before the fact,
    for every path this removal touches.

    A no-op on every other platform, and a no-op for a value that is already
    prefixed (a caller-supplied path already spelled in the extended-length
    or UNC-extended form) or is a UNC share (`\\\\server\\share\\...`, which
    gets its OWN prefix form, `\\\\?\\UNC\\server\\share\\...`, not a bare
    `\\\\?\\` glued onto two leading backslashes).
    """
    if os.name != "nt":
        return str(path)
    absolute = os.path.abspath(str(path))
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


@dataclass(frozen=True)
class RemovalOutcome:
    """What actually happened when [`remove_sdk_tree`] tried. `ok=False`
    always carries a `kind` (`"in-use"` | `"permission"`) and the OS-rendered
    `detail`; the failing `path`, when the underlying `OSError` named one
    (`err.filename`), else `None` -- a hook-driven `shutil.rmtree` failure
    does not always carry one."""

    ok: bool
    freed_bytes: int
    kind: str | None = None
    detail: str | None = None
    failing_path: str | None = None


def remove_sdk_tree(path: Path) -> RemovalOutcome:
    """Remove `path` (already confirmed to exist by the caller) with
    `dir_removal.remove_dir`'s read-only-retry/junction-safe hardening, sized
    before and (on a failure) after, so a partial removal still reports how
    much it actually freed rather than zero.

    Long-path-prefixed on Windows ([`_win_long_path`]) so the size this
    process just proved it could walk is also one this process can delete,
    without the caller ever seeing the raw `MAX_PATH` errno this would
    otherwise surface as an unclassified `"permission"` failure.
    """
    before = compute_tree_bytes(path)
    try:
        remove_dir(_win_long_path(path))
    except (OSError, ValueError) as err:
        after = compute_tree_bytes(path) if path.exists() else 0
        failing_path = getattr(err, "filename", None) if isinstance(err, OSError) else None
        kind = classify_removal_error(err) if isinstance(err, OSError) else "permission"
        detail = os_error_text(err) if isinstance(err, OSError) else str(err)
        return RemovalOutcome(
            ok=False,
            freed_bytes=max(before - after, 0),
            kind=kind,
            detail=detail,
            failing_path=failing_path,
        )
    return RemovalOutcome(ok=True, freed_bytes=before)
