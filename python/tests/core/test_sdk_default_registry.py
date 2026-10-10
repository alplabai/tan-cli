# SPDX-License-Identifier: Apache-2.0
"""`tan.core.sdk_default_registry` -- the pure parse/pick/render logic behind
tan-cli#466's origin-keyed `~/.alp/sdk-defaults.json`.

Resolver-level coverage (wired into `sdk_discovery.resolve_sdk_tiered`, with real
`_workspace_under`/`_has_loader_script`) lives in `test_sdk_command.py`
alongside every other `globalDefault`-tier case; this file is the narrower,
faster unit layer underneath it -- pure functions, fake `covers`/
`has_loader_script` callables, no filesystem beyond what `tmp_path` needs for
`Path` objects to exist as strings.
"""
from __future__ import annotations

from pathlib import Path

from tan.core.sdk_default_registry import (
    deepest_covering_entry,
    load_raw,
    normalized_sdk_path,
    parse_registry,
    parse_registry_updated_at,
    prune_dead_origins,
    prune_entries_by_sdk_path,
    registry_path,
    registry_text,
    with_entry,
)


def test_registry_path_is_home_alp_dir_slash_sdk_defaults_json(tmp_path):
    home = tmp_path / "home" / ".alp"
    assert registry_path(home) == home / "sdk-defaults.json"


# ── parse_registry: the READ side, {} on any failure ─────────────────────────


def test_parse_registry_none_is_empty():
    assert parse_registry(None) == {}


def test_parse_registry_reads_origin_to_sdk_path():
    raw = '{"/proj/a": {"sdkPath": "/sdk/a"}, "/proj/b": {"sdkPath": "/sdk/b"}}'
    assert parse_registry(raw) == {"/proj/a": "/sdk/a", "/proj/b": "/sdk/b"}


def test_parse_registry_invalid_json_is_empty():
    assert parse_registry("not json at all") == {}


def test_parse_registry_truncated_json_is_empty():
    """The concurrency case named in the issue: a second `tan bootstrap`
    process's write overlapping this one's read is expected to (rarely)
    produce a partial file on a filesystem without atomic rename semantics
    for this write. It must degrade, never raise."""
    raw = '{"/proj/a": {"sdkPath": "/sdk/a"}, "/proj/b": {"sdkPa'
    assert parse_registry(raw) == {}


def test_parse_registry_json_array_is_empty():
    assert parse_registry("[1, 2, 3]") == {}


def test_parse_registry_drops_entries_missing_sdk_path():
    raw = '{"/proj/a": {"sdkPath": "/sdk/a"}, "/proj/b": {"noSuchKey": 1}}'
    assert parse_registry(raw) == {"/proj/a": "/sdk/a"}


def test_parse_registry_drops_entries_whose_sdk_path_is_not_a_string():
    raw = '{"/proj/a": {"sdkPath": 42}}'
    assert parse_registry(raw) == {}


def test_parse_registry_drops_a_non_dict_entry():
    raw = '{"/proj/a": "/sdk/a"}'
    assert parse_registry(raw) == {}


def test_parse_registry_drops_a_non_string_or_empty_origin_key():
    # JSON object keys are always strings, so the empty-string case is the
    # only one reachable through real `json.loads` -- covered anyway, since
    # `parse_registry`'s own guard is `not isinstance(origin, str) or not
    # origin` and only the second half is reachable via JSON.
    raw = '{"": {"sdkPath": "/sdk/a"}, "/proj/b": {"sdkPath": "/sdk/b"}}'
    assert parse_registry(raw) == {"/proj/b": "/sdk/b"}


# ── load_raw: the WRITE side's round-trip-preserving read ────────────────────


def test_load_raw_none_is_empty():
    assert load_raw(None) == {}


def test_load_raw_preserves_the_entry_wrapper_unlike_parse_registry():
    raw = '{"/proj/a": {"sdkPath": "/sdk/a", "extra": "kept"}}'
    assert load_raw(raw) == {"/proj/a": {"sdkPath": "/sdk/a", "extra": "kept"}}


def test_load_raw_malformed_is_empty():
    assert load_raw("{not json") == {}


def test_load_raw_non_dict_is_empty():
    assert load_raw("[1, 2]") == {}


# ── with_entry / registry_text: the WRITE side ────────────────────────────────


def test_with_entry_adds_a_new_origin_without_touching_others():
    existing = {"/proj/a": {"sdkPath": "/sdk/a", "updatedAt": "t0"}}
    updated = with_entry(existing, origin="/proj/b", sdk_root="/sdk/b", updated_at="t1")
    assert updated["/proj/a"] == {"sdkPath": "/sdk/a", "updatedAt": "t0"}
    assert updated["/proj/b"] == {"sdkPath": "/sdk/b", "updatedAt": "t1"}
    # The input is untouched -- callers snapshot-then-mutate.
    assert "/proj/b" not in existing


def test_with_entry_overwrites_an_existing_origin():
    existing = {"/proj/a": {"sdkPath": "/sdk/old", "updatedAt": "t0"}}
    updated = with_entry(existing, origin="/proj/a", sdk_root="/sdk/new", updated_at="t1")
    assert updated["/proj/a"] == {"sdkPath": "/sdk/new", "updatedAt": "t1"}


def test_with_entry_stamps_the_caller_supplied_updated_at_verbatim():
    """Review, #904 second round, major: `deepest_covering_entry`'s recency
    tie-break reads exactly this field back -- `with_entry` must not
    reformat, default, or otherwise second-guess whatever the caller (the
    wall clock, in production) computed."""
    stamp = "2026-01-01T00:00:00.000Z"
    updated = with_entry({}, origin="/proj/a", sdk_root="/sdk/a", updated_at=stamp)
    assert updated["/proj/a"]["updatedAt"] == stamp


def test_registry_text_round_trips_through_load_raw():
    doc = with_entry({}, origin="/proj/a", sdk_root="/sdk/a", updated_at="t0")
    text = registry_text(doc)
    assert text.endswith("\n")
    assert load_raw(text) == doc


def test_registry_text_sorts_keys_for_a_deterministic_diff():
    doc = with_entry(
        with_entry({}, origin="/z", sdk_root="/sdk/z", updated_at="t0"),
        origin="/a", sdk_root="/sdk/a", updated_at="t1",
    )
    text = registry_text(doc)
    assert text.index('"/a"') < text.index('"/z"')


# ── deepest_covering_entry: the PICK, no directory WALK (a closed candidate
#    set) -- but real filesystem work per candidate (review, #904, major 2:
#    "zero filesystem probing" was false and has been retired) ──────────────


def _covers_prefix(workspace_root: Path, origin: str) -> bool:
    """A fake `covers` -- pure string-prefix containment, standing in for
    `sdk_discovery._workspace_under` without touching a real filesystem."""
    ws = str(workspace_root).replace("\\", "/")
    root = origin.replace("\\", "/")
    return ws == root or ws.startswith(root + "/")


def _always_valid(_path: Path) -> bool:
    return True


def _never_valid(_path: Path) -> bool:
    return False


def _identity_resolve(root: str) -> str:
    """A fake `resolve_origin` for tests with no symlink in play: the raw
    key already IS its own resolved form, so ranking by it is exactly the
    old raw-string-length ranking. The symlink-specific test below supplies
    a fake that diverges from identity, since that divergence is the whole
    bug (tan-cli#904 review, major 1)."""
    return root


def test_deepest_covering_entry_none_when_registry_is_empty():
    assert (
        deepest_covering_entry(
            {},
            Path("/a/b"),
            covers=_covers_prefix,
            has_loader_script=_always_valid,
            resolve_origin=_identity_resolve,
        )
        is None
    )


def test_deepest_covering_entry_picks_the_containing_key(tmp_path):
    registry = {"/a": "/sdk/a"}
    hit = deepest_covering_entry(
        registry,
        Path("/a/b/c"),
        covers=_covers_prefix,
        has_loader_script=_always_valid,
        resolve_origin=_identity_resolve,
    )
    assert hit == ("/a", "/sdk/a")


def test_deepest_covering_entry_ignores_a_non_covering_key():
    """The closed-candidate-set property: a registry key that does NOT
    contain `workspace_root` must never answer for it, no matter what else
    is in the registry."""
    registry = {"/somewhere/else": "/sdk/other"}
    hit = deepest_covering_entry(
        registry,
        Path("/a/b"),
        covers=_covers_prefix,
        has_loader_script=_always_valid,
        resolve_origin=_identity_resolve,
    )
    assert hit is None


def test_deepest_covering_entry_prefers_the_more_specific_project_entry():
    """A `HOME`-keyed entry (the machine-wide default) and a deeper project-
    specific entry both cover the same workspace -- the more specific one
    must win, exactly the tan-cli#466 "a later bootstrap in `~/proj/B` still
    wins for everything under it" property."""
    registry = {
        "/home/u": "/sdk/home-default",
        "/home/u/proj/b": "/sdk/b-specific",
    }
    workspace = Path("/home/u/proj/b/firmware")
    hit = deepest_covering_entry(
        registry,
        workspace,
        covers=_covers_prefix,
        has_loader_script=_always_valid,
        resolve_origin=_identity_resolve,
    )
    assert hit == ("/home/u/proj/b", "/sdk/b-specific")


def test_deepest_covering_entry_falls_through_a_stale_entry_to_a_shallower_one():
    """Degrades safely (the issue's own acceptance property): the DEEPEST
    covering entry is stale (`has_loader_script` rejects it), so the next-
    deepest COVERING entry answers instead of the whole lookup going empty."""

    def loader_script(path: Path) -> bool:
        # `.as_posix()`, not `str(path)` (review, #904 second round, blocker
        # 1): `deepest_covering_entry` calls this with `Path(sdk_path)`, and
        # on Windows `str(WindowsPath("/sdk/b-specific"))` renders
        # backslashes (`"\\sdk\\b-specific"`), which never equals the
        # forward-slashed literal below -- so the stale entry this test means
        # to reject was never rejected there, and the assertion below failed
        # on the Windows shard while passing on Linux/macOS.
        # `WindowsPath("/sdk/b-specific").as_posix()` is forward-slashed on
        # every platform (a leading `/` parses as a valid root under
        # `PureWindowsPath` too), so this comparison is separator-free.
        return path.as_posix() != "/sdk/b-specific"

    registry = {
        "/home/u": "/sdk/home-default",
        "/home/u/proj/b": "/sdk/b-specific",  # stale
    }
    workspace = Path("/home/u/proj/b/firmware")
    hit = deepest_covering_entry(
        registry,
        workspace,
        covers=_covers_prefix,
        has_loader_script=loader_script,
        resolve_origin=_identity_resolve,
    )
    assert hit == ("/home/u", "/sdk/home-default")


def test_deepest_covering_entry_none_when_every_covering_entry_is_stale():
    registry = {"/home/u": "/sdk/gone"}
    hit = deepest_covering_entry(
        registry,
        Path("/home/u/proj"),
        covers=_covers_prefix,
        has_loader_script=_never_valid,
        resolve_origin=_identity_resolve,
    )
    assert hit is None


def test_deepest_covering_entry_rejects_a_relative_origin():
    """Mirrors `sdk_discovery._pointer_written_for`'s own defence: a registry is
    one file a hand edit (or a future writer bug) could corrupt into holding
    a relative key, and `_workspace_under`-style containment resolves a
    relative `root` against the CALLER's cwd -- exactly the tan-cli#464
    review regression (`writtenFor: ""`) this module must not reopen for the
    registry. `covers` is stubbed to ALWAYS say yes here, so the only thing
    that can be filtering the relative key out is `deepest_covering_entry`'s
    own absolute check -- a permissive `covers` would otherwise mask this
    guard doing nothing at all.
    """
    registry = {"relative/origin": "/sdk/a"}
    hit = deepest_covering_entry(
        registry,
        Path("/a/b"),
        covers=lambda *_a: True,
        has_loader_script=_always_valid,
        resolve_origin=_identity_resolve,
    )
    assert hit is None


def test_deepest_covering_entry_ranks_by_resolved_origin_not_raw_key_length():
    """tan-cli#904 review, major 1: a symlinked origin makes `covers`'s own
    notion of "deepest" (decided on RESOLVED paths -- `sdk_discovery.
    _workspace_under` calls `.resolve()` on both sides) disagree with a raw
    registry-key STRING-LENGTH ranking.

    `/base/work` is a symlink to `/base/projects/alpha`. Its RAW key
    (`"/base/work"`, 10 chars) is SHORTER than `/base/projects`'s raw key
    (`"/base/projects"`, 15 chars) -- so the pre-fix ranking (`len(origin...)`)
    picks `/base/projects` as "deepest" and answers `/sdk/WRONG`. But once
    resolved, `/base/work` becomes `/base/projects/alpha` (21 chars), which
    IS the true deepest (most specific) covering ancestor of the resolved
    workspace -- the correct answer is `/sdk/RIGHT`. Both origins cover the
    workspace under `covers`'s resolved comparison; only the RANKING must
    change to fix this, exactly what `resolve_origin` is for.
    """
    resolved_of = {
        "/base/work": "/base/projects/alpha",  # symlink target
        "/base/projects": "/base/projects",  # no symlink; resolves to itself
    }
    workspace_resolved = "/base/projects/alpha/ws"

    def covers(_workspace_root: Path, origin: str) -> bool:
        root_resolved = resolved_of[origin]
        return workspace_resolved == root_resolved or workspace_resolved.startswith(
            root_resolved + "/"
        )

    registry = {
        "/base/work": "/sdk/RIGHT",
        "/base/projects": "/sdk/WRONG",
    }
    hit = deepest_covering_entry(
        registry,
        Path("/base/work/ws"),
        covers=covers,
        has_loader_script=_always_valid,
        resolve_origin=lambda origin: resolved_of[origin],
    )
    assert hit == ("/base/work", "/sdk/RIGHT")


# ── deepest_covering_entry: the resolved-depth TIE-BREAK ────────────────────
# (review, #904 second round, major -- two distinct raw origins that RESOLVE
# to the same depth, e.g. an alias of the same directory, used to let the
# lexicographically smallest raw key win regardless of which was written
# more recently) ─────────────────────────────────────────────────────────────


def test_a_resolved_depth_tie_is_broken_by_the_more_recent_updated_at():
    """Two origins tie on resolved depth (both length 5, `_identity_resolve`)
    -- without a tie-break, `depth > best_depth` alone keeps whichever the
    dict visits first, `/proj/a`, purely because it sorts first. The MORE
    RECENT entry (`/proj/b`, `updatedAt` "t2" > "t1") must win instead."""
    registry = {"/proj/a": "/sdk/OLD", "/proj/b": "/sdk/NEW"}
    hit = deepest_covering_entry(
        registry,
        Path("/proj/a"),
        covers=lambda *_a: True,
        has_loader_script=_always_valid,
        resolve_origin=_identity_resolve,
        updated_at={"/proj/a": "t1", "/proj/b": "t2"},
    )
    assert hit == ("/proj/b", "/sdk/NEW")


def test_a_resolved_depth_tie_keeps_the_dict_visit_order_when_updated_at_is_missing():
    """No `updated_at` map at all (the default, and every EXISTING caller of
    this function before the tie-break existed) must behave exactly as
    before: a real depth difference still wins outright, and this test's
    registry has none -- it exists only to prove the DEFAULT parameter never
    raises and never changes an unambiguous (non-tied) answer."""
    registry = {"/a": "/sdk/a"}
    hit = deepest_covering_entry(
        registry,
        Path("/a/b"),
        covers=_covers_prefix,
        has_loader_script=_always_valid,
        resolve_origin=_identity_resolve,
    )
    assert hit == ("/a", "/sdk/a")


def test_a_missing_updated_at_never_wins_a_tie_against_a_real_timestamp():
    """An origin with no recorded `updatedAt` (a pre-tie-break registry, or a
    hand-edit) degrades to `""` -- which must never outrank a real ISO-8601
    stamp, no matter which origin the dict happens to visit first."""
    registry = {"/proj/z": "/sdk/UNDATED", "/proj/a": "/sdk/DATED"}
    hit = deepest_covering_entry(
        registry,
        Path("/proj/a"),
        covers=lambda *_a: True,
        has_loader_script=_always_valid,
        resolve_origin=_identity_resolve,
        updated_at={"/proj/a": "2026-01-01T00:00:00.000Z"},  # "/proj/z" absent
    )
    assert hit == ("/proj/a", "/sdk/DATED")


def test_a_real_depth_difference_still_wins_over_a_worse_updated_at():
    """The tie-break only fires ON a tie -- a shallower but more RECENT
    origin must never beat a deeper, correctly-more-specific one."""
    registry = {"/home/u": "/sdk/shallow-but-newer", "/home/u/proj": "/sdk/deep-but-older"}
    hit = deepest_covering_entry(
        registry,
        Path("/home/u/proj/sub"),
        covers=_covers_prefix,
        has_loader_script=_always_valid,
        resolve_origin=_identity_resolve,
        updated_at={"/home/u": "2026-06-01T00:00:00Z", "/home/u/proj": "2026-01-01T00:00:00Z"},
    )
    assert hit == ("/home/u/proj", "/sdk/deep-but-older"), (
        "depth must still outrank recency when the two are not actually tied"
    )


def test_a_real_depth_difference_still_wins_even_visited_deep_first():
    """The sibling of `test_a_real_depth_difference_still_wins_over_a_worse_
    updated_at`, with the registry's two keys in the OPPOSITE insertion
    order (review, #904 third round, minor 1) -- that test alone cannot
    fail for the reason its own docstring claims: deleting the
    `depth == best_depth` guard entirely (`if depth > best_depth or
    (entry_updated_at > best_updated_at):`) leaves it green, because its
    registry literal happens to list the SHALLOW key first, so the plain
    `depth > best_depth` branch alone already decides the winner on the
    very first comparison and the mutated OR-clause never gets a chance to
    fire.

    Visiting the DEEP, older-but-correct entry FIRST is production-reachable
    (a symlinked origin whose raw key sorts, or is otherwise inserted,
    AFTER a deeper real key -- `dict` iteration here follows insertion
    order, and the registry is written with `sort_keys=True`, so a raw
    origin string that happens to sort ahead of a deeper one is visited
    first): it sets `best_depth` to the deep entry's depth immediately, so
    the shallow-but-newer entry visited SECOND has `depth < best_depth` --
    the mutated clause then compares recency UNCONDITIONALLY, sees the
    shallow entry's newer stamp, and wrongly overwrites the correct,
    deeper answer. Mutation-proven: red with the guard deleted, green with
    it restored.
    """
    registry = {"/home/u/proj": "/sdk/deep-but-older", "/home/u": "/sdk/shallow-but-newer"}
    hit = deepest_covering_entry(
        registry,
        Path("/home/u/proj/sub"),
        covers=_covers_prefix,
        has_loader_script=_always_valid,
        resolve_origin=_identity_resolve,
        updated_at={"/home/u": "2026-06-01T00:00:00Z", "/home/u/proj": "2026-01-01T00:00:00Z"},
    )
    assert hit == ("/home/u/proj", "/sdk/deep-but-older"), (
        "depth must still outrank recency when visited deep-first too, not "
        "just when the registry happens to list the shallow key first"
    )


# ── prune_dead_origins: drops an origin whose directory is gone (tan-cli#905)
#    -- and ONLY that; a merely-unused-right-now origin (still exists, just
#    doesn't cover the current caller, or its sdkPath went stale) is untouched
#    ──────────────────────────────────────────────────────────────────────────


def test_prune_dead_origins_drops_an_entry_whose_directory_is_gone():
    registry = {"/proj/a": {"sdkPath": "/sdk/a", "updatedAt": "t0"}}
    pruned = prune_dead_origins(registry, origin_exists=lambda _origin: False)
    assert pruned == {}


def test_prune_dead_origins_keeps_an_entry_whose_directory_still_exists():
    registry = {"/proj/a": {"sdkPath": "/sdk/a", "updatedAt": "t0"}}
    pruned = prune_dead_origins(registry, origin_exists=lambda _origin: True)
    assert pruned == registry


def test_prune_dead_origins_only_drops_the_dead_ones_not_every_entry():
    """The property the issue's own acceptance bar names: a reader must be
    able to tell a dead entry from one that is merely unused right now.
    `/proj/live` is still a real directory (just not the caller's current
    workspace, and irrelevant to this function either way -- `covers` decides
    that, not this one); `/proj/gone` no longer exists at all."""
    registry = {
        "/proj/live": {"sdkPath": "/sdk/live", "updatedAt": "t0"},
        "/proj/gone": {"sdkPath": "/sdk/gone", "updatedAt": "t1"},
    }
    pruned = prune_dead_origins(
        registry, origin_exists=lambda origin: origin == "/proj/live"
    )
    assert pruned == {"/proj/live": {"sdkPath": "/sdk/live", "updatedAt": "t0"}}


def test_prune_dead_origins_does_not_prune_on_a_merely_stale_sdk_path():
    """The distinction the issue asks for, made concrete: `has_loader_script`
    -- not this function -- is what `deepest_covering_entry` already uses to
    skip a covering entry whose `sdkPath` no longer resolves, because that
    origin's PROJECT may simply be between bootstraps. `prune_dead_origins`
    has no `has_loader_script`-shaped parameter at all -- it can only ever
    see `origin_exists`, so a caller cannot accidentally wire sdkPath
    staleness into it even by mistake; this test pins that the ORIGIN alone
    decides the outcome regardless of how implausible the `sdkPath` value is.
    """
    registry = {"/proj/a": {"sdkPath": "/this/checkout/was/deleted", "updatedAt": "t0"}}
    pruned = prune_dead_origins(registry, origin_exists=lambda _origin: True)
    assert pruned == registry


def test_prune_dead_origins_drops_a_malformed_entry_whose_origin_is_gone():
    """A malformed entry (`parse_registry`/`parse_registry_updated_at` would
    both drop this shape on read) is exactly as prunable as a well-formed
    one -- `prune_dead_origins` operates on the WRITE-side `load_raw` shape,
    which preserves whatever a hand edit or an old writer left behind, and
    tan-cli#905's own complaint is the file growing unboundedly, which a
    malformed-but-undead entry would defeat if left untouched."""
    registry = {"/proj/a": "not-a-dict-entry"}
    pruned = prune_dead_origins(registry, origin_exists=lambda _origin: False)
    assert pruned == {}


def test_prune_dead_origins_empty_registry_is_empty():
    assert prune_dead_origins({}, origin_exists=lambda _origin: True) == {}


def test_prune_dead_origins_never_touches_the_filesystem_itself():
    """`origin_exists` is injected the same way `covers`/`has_loader_script`/
    `resolve_origin` are for `deepest_covering_entry` -- this test proves it
    by using an origin string no real filesystem call could answer for
    (`os.stat` on this would raise) and a fake that never inspects it."""
    registry = {"\0invalid\0path": {"sdkPath": "/sdk/a", "updatedAt": "t0"}}
    pruned = prune_dead_origins(registry, origin_exists=lambda _origin: True)
    assert pruned == registry


def _equal(stored: str, target: str) -> bool:
    """The plain string equality these cases were written against, injected
    explicitly now that `prune_entries_by_sdk_path` takes its comparison from
    the caller (tan-cli#1053 review). Keeping it here rather than defaulting
    it in the function means no PRODUCTION caller can reach this behaviour by
    omission -- which is exactly how the fifth site of that defect survived.
    """
    return stored == target


# ── prune_entries_by_sdk_path: keeping the registry honest after a removal ──
# (tan-cli#790 -- `sdk remove`'s own registry-honesty obligation, the sibling
# of prune_dead_origins pruning on the ORIGIN axis instead of this one.)


def test_prune_entries_by_sdk_path_drops_every_entry_naming_the_removed_path():
    registry = {
        "/proj/a": {"sdkPath": "/sdk/removed", "updatedAt": "t0"},
        "/proj/b": {"sdkPath": "/sdk/removed", "updatedAt": "t1"},
    }
    pruned = prune_entries_by_sdk_path(registry, matches=_equal, sdk_path="/sdk/removed")
    assert pruned == {}


def test_prune_entries_by_sdk_path_keeps_entries_naming_a_different_path():
    """The vacuity check: a prune that dropped EVERY entry regardless of
    `sdk_path` would also pass a test that only ever removed one. This one
    plants an unrelated entry and asserts it survives verbatim."""
    registry = {
        "/proj/a": {"sdkPath": "/sdk/removed", "updatedAt": "t0"},
        "/proj/keep": {"sdkPath": "/sdk/still-here", "updatedAt": "t1"},
    }
    pruned = prune_entries_by_sdk_path(registry, matches=_equal, sdk_path="/sdk/removed")
    assert pruned == {"/proj/keep": {"sdkPath": "/sdk/still-here", "updatedAt": "t1"}}


def test_prune_entries_by_sdk_path_empty_registry_is_empty():
    assert prune_entries_by_sdk_path({}, matches=_equal, sdk_path="/sdk/removed") == {}


def test_prune_entries_by_sdk_path_no_match_changes_nothing():
    registry = {"/proj/a": {"sdkPath": "/sdk/other", "updatedAt": "t0"}}
    assert prune_entries_by_sdk_path(registry, matches=_equal, sdk_path="/sdk/removed") == registry


def test_prune_entries_by_sdk_path_drops_a_malformed_entry_that_still_matches():
    """A bare-string entry (no `updatedAt` wrapper) is exactly as prunable as
    a well-formed one when its `sdkPath`-equivalent value matches -- but a
    bare string has no `sdkPath` KEY at all (`entry.get("sdkPath")` needs a
    dict), so this pins the actual, narrower contract: a non-dict entry is
    left alone by this function (nothing to compare), the same as
    `parse_registry` already drops it on READ regardless."""
    registry = {"/proj/a": "/sdk/removed"}
    pruned = prune_entries_by_sdk_path(registry, matches=_equal, sdk_path="/sdk/removed")
    assert pruned == registry


def test_normalized_sdk_path_folds_backslashes_and_leaves_posix_alone():
    """The write side (`bootstrap_cmd._write_global_sdk_registry`) stores every
    `sdkPath` posix-normalised, so this is the exact inverse of that write and
    nothing more: separators fold, everything else -- including case, which
    NTFS ignores but POSIX does not -- is left exactly as stored."""
    assert normalized_sdk_path("C:\\Users\\me\\sdk") == "C:/Users/me/sdk"
    assert normalized_sdk_path("/home/me/sdk") == "/home/me/sdk"
    assert normalized_sdk_path("C:/Users/me/sdk") == "C:/Users/me/sdk"
    assert normalized_sdk_path("C:\\Users\\ME\\Sdk") == "C:/Users/ME/Sdk"


def test_prune_entries_by_sdk_path_matches_a_backslash_spelled_entry():
    """A hand-edited registry -- a shape this module's own `parse_registry`
    contract accommodates -- spells a Windows path with backslashes, while
    every path this codebase computes is forward-slash. A raw `==` answered
    False for two names of ONE directory, leaving the entry behind after
    `tan sdk remove` deleted the tree it points at (tan-cli#790).

    The vacuity guard is the second entry: a genuinely DIFFERENT path, also
    backslash-spelled, must survive -- so a "fold everything to equal" bug
    fails this too.
    """
    registry = {
        "C:\\proj\\a": {"sdkPath": "C:\\sdk\\removed", "updatedAt": "t0"},
        "C:\\proj\\b": {"sdkPath": "C:\\sdk\\kept", "updatedAt": "t1"},
    }
    pruned = prune_entries_by_sdk_path(registry, matches=_equal, sdk_path="C:/sdk/removed")
    assert pruned == {"C:\\proj\\b": {"sdkPath": "C:\\sdk\\kept", "updatedAt": "t1"}}


def test_prune_entries_by_sdk_path_never_touches_the_filesystem():
    """Still a pure function, and now pure BY CONSTRUCTION: the comparison
    arrives as `matches` from outside (tan-cli#1053 review), so this module
    cannot reach a filesystem even by accident -- the real caller injects
    `sdk_removal.removal_would_take_out`, which does. Proven here by an
    origin/path pair no real filesystem call could answer for, which still
    resolves correctly under a caller-supplied string equality."""
    registry = {"\0invalid\0origin": {"sdkPath": "\0invalid\0sdk", "updatedAt": "t0"}}
    pruned = prune_entries_by_sdk_path(registry, matches=_equal, sdk_path="\0invalid\0sdk")
    assert pruned == {}


def test_prune_entries_by_sdk_path_uses_the_injected_comparison_not_equality():
    """`matches` is not decoration: an entry that string-equality would KEEP
    is dropped when the injected predicate says the removal took it out.

    This is the mechanism behind tan-cli#1053's fifth site -- an
    alias-spelled entry naming the just-removed install was refused and then,
    with a raw `==` here, not pruned. Required rather than defaulted for
    exactly this reason: a default would hand a caller back the defect.
    """
    registry = {
        "/proj/a": {"sdkPath": "/alias/spelling", "updatedAt": "t0"},
        "/proj/b": {"sdkPath": "/sdk/unrelated", "updatedAt": "t1"},
    }

    def alias_aware(stored: str, target: str) -> bool:
        return stored == target or stored == "/alias/spelling"

    pruned = prune_entries_by_sdk_path(
        registry, matches=alias_aware, sdk_path="/sdk/removed"
    )
    assert pruned == {"/proj/b": {"sdkPath": "/sdk/unrelated", "updatedAt": "t1"}}


# ── parse_registry_updated_at: the recency companion of parse_registry ──────


def test_parse_registry_updated_at_none_is_empty():
    assert parse_registry_updated_at(None) == {}


def test_parse_registry_updated_at_reads_the_field_per_origin():
    raw = (
        '{"/proj/a": {"sdkPath": "/sdk/a", "updatedAt": "t1"}, '
        '"/proj/b": {"sdkPath": "/sdk/b", "updatedAt": "t2"}}'
    )
    assert parse_registry_updated_at(raw) == {"/proj/a": "t1", "/proj/b": "t2"}


def test_parse_registry_updated_at_missing_field_degrades_to_empty_string():
    """An origin with no `updatedAt` at all (a registry written before this
    fix) must still be a KEY in the result, mapped to `""` -- so
    `deepest_covering_entry`'s `.get(origin, "")` and this function's own
    "absent" degrade to the identical value, never a `KeyError` either way."""
    raw = '{"/proj/a": {"sdkPath": "/sdk/a"}}'
    assert parse_registry_updated_at(raw) == {"/proj/a": ""}


def test_parse_registry_updated_at_non_string_field_degrades_to_empty_string():
    raw = '{"/proj/a": {"sdkPath": "/sdk/a", "updatedAt": 42}}'
    assert parse_registry_updated_at(raw) == {"/proj/a": ""}


def test_parse_registry_updated_at_invalid_json_is_empty():
    assert parse_registry_updated_at("not json") == {}


def test_parse_registry_updated_at_non_dict_entry_is_dropped():
    raw = '{"/proj/a": "/sdk/a"}'
    assert parse_registry_updated_at(raw) == {}
