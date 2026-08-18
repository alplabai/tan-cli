# SPDX-License-Identifier: Apache-2.0
"""Freshness gate for `pin_move_verify.py`'s MIRRORED `alpe2e.pinverify`
contract (PR #823 review, finding 6): `_REF`/`_SKU`/`_REPO`/`_SHA`,
`MAX_SOMS`, and `PinMoveTuple.check_name`'s format string are duplicated by
hand from alp-e2e (private repo, `.github/workflows/pin-move-verify.yml`'s
receiver) for cost reasons -- see `pin_move_verify.py`'s own module
docstring, "The dispatch contract -- mirrored, not reinvented". An ungated
second copy of a contract is tan-cli#358's exact defect shape: the receiver
already enforces `MAX_SOMS = 8` and this sender had no cap at all until this
gate's own PR, silently accepting a 9-SoM request the receiver's `plan` job
would refuse -- whose `report` job (`if: always() && needs.plan.result ==
'success'`) then never posts a Check Run, so the sender burns its full
90-minute poll deadline discovering that. Client-side validation only pays
for itself if it stays IN STEP with what it mirrors.

Two checks, the same PINNED_HASHES / `ALP_SDK_ROOT` shape
`test_planner_relocation_freshness.py` uses against alp-sdk, pointed at
alp-e2e instead:

1. A SELF-check, unconditional, no checkout needed: `pmv.MIRRORED_CONTRACT_HASH`
   must equal the live sha256 of `pmv.MIRRORED_CONTRACT_TEXT`. Catches a local
   edit to any of the four regexes / `MAX_SOMS` / `check_name`'s format string
   that forgot to bump the pinned hash alongside it.
2. A LIVE-DRIFT check, `ALP_E2E_ROOT`-gated: dynamically loads the real
   `alpe2e/pinverify.py` out of a local alp-e2e checkout and re-derives the
   SAME text from the LIVE objects (not a source-text regex scrape -- a
   value-level comparison is the right level for a cost-saving mirror; an
   incidental formatting change upstream that leaves every value identical
   should not force a re-audit here). A mismatch means the receiver's
   contract moved and this mirror has NOT been re-audited against the new
   shape. This SKIPS -- loudly, never a silent pass, the exact trap
   tan-cli#275 closed for the planner-relocation gate (a gate that cannot
   fail is not a gate) -- along exactly two paths: `ALP_E2E_ROOT` is unset,
   or it is bound at a real alp-e2e checkout that genuinely does not carry
   the receiver's contract anywhere in its tree yet (tan-cli#835 review:
   pre-receiver `main`, today). Every OTHER shape that reaches
   `_load_alp_e2e_pinverify` -- including the contract having moved to a
   different file inside `alpe2e/` -- is a FAIL, not a skip: see that
   function's own comments for why "the file isn't at the canonical path"
   is not by itself grounds to skip.

When either check fails: diff `alpe2e/pinverify.py`'s `_REF`/`_SKU`/`_REPO`/
`_SHA`/`MAX_SOMS`/`check_name` against `pin_move_verify.py`'s copies, port the
delta into `pin_move_verify.py`, and update `MIRRORED_CONTRACT_HASH` (and its
own "last re-audited against" comment) to match -- tan-cli#835 tracks this
re-audit and is the place to open the next one from.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import types

import pytest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import pin_move_verify as pmv  # noqa: E402


def test_mirrored_contract_hash_matches_the_live_text():
    live_hash = hashlib.sha256(pmv.MIRRORED_CONTRACT_TEXT.encode("utf-8")).hexdigest()
    assert live_hash == pmv.MIRRORED_CONTRACT_HASH, (
        "pin_move_verify.py's mirrored contract (the four regexes, MAX_SOMS, "
        "check_name's format string) changed locally without "
        "MIRRORED_CONTRACT_HASH being updated to match. If this was a "
        "deliberate re-audit against a newer alp-e2e, update the pinned hash "
        "and its 'last re-audited against' comment; if not, this is an "
        "accidental drift between the mirror and what it is pinned to."
    )


# ---------------------------------------------------------------------------
# _load_alp_e2e_pinverify's FAIL/SKIP/AUDIT decision, tan-cli#835 review
# ---------------------------------------------------------------------------
#
# Three questions, asked in this exact order, because the first version of
# this gate (tan-cli#835's first commit) got the order wrong and both
# consequences were real defects, caught in review before they reached
# `dev`:
#
#   1. Is `alpe2e/pinverify.py` there, AT ALL, regardless of `.git`? If so,
#      audit it -- full stop. A `.git`-less tree (a `git archive` export, a
#      vendored copy, an unpacked tarball) that carries the file can run
#      this check exactly as well as a live clone can; refusing it on a
#      `.git` technicality (MAJOR 2) is the identical "misdiagnoses a
#      checkout that can run the audit" defect class this whole gate exists
#      to fix, just relocated from the FAIL arm to a precondition that now
#      runs even earlier.
#   2. If it is NOT at the canonical path, has the CONTRACT moved somewhere
#      else under `alpe2e/`? A rename or a package split
#      (`alpe2e/pin_move_verify.py`, `alpe2e/pinverify/__init__.py`, ...) is
#      drift, not absence -- "the file I expected isn't there" must never
#      silently read as "there is nothing to audit" (MAJOR 1), or this gate
#      goes green on the exact upstream refactor it exists to catch.
#   3. Only once both of those are ruled out: is `root` a real alp-e2e
#      checkout that simply predates the receiver (a `main` checkout today,
#      per tan-cli#835), or is it not an alp-e2e checkout at all? The
#      "is this even alp-e2e" test itself needs a marker that is not
#      trivially satisfied by an empty `mkdir alpe2e && git init` -- see
#      `_looks_like_alp_e2e_checkout` below.
_RELOCATED_CONTRACT_MARKERS = (
    re.compile(r"^_REF\s*=", re.MULTILINE),
    re.compile(r"^MAX_SOMS\s*=", re.MULTILINE),
    re.compile(r"^class PinMoveRequest\b", re.MULTILINE),
)


def _find_relocated_contract_module(package_dir: pathlib.Path) -> pathlib.Path | None:
    """Best-effort scan for the contract having MOVED rather than vanished
    (tan-cli#835 review, MAJOR 1). A source-text scan, not an import --
    the relocated module may need dependencies this environment does not
    have, and the only claim being made here is "these three names are
    still DEFINED somewhere in this checkout", which is enough to tell
    "moved" apart from "genuinely absent" without needing to actually
    execute the candidate.
    """
    for candidate in sorted(package_dir.glob("**/*.py")):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        if all(pattern.search(text) for pattern in _RELOCATED_CONTRACT_MARKERS):
            return candidate
    return None


def _git_remote_matches_alp_e2e(root: pathlib.Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and "alplabai/alp-e2e" in proc.stdout


def _looks_like_alp_e2e_checkout(root: pathlib.Path) -> bool:
    """Whether `root` is a real alp-e2e checkout, just one that predates
    the receiver -- as opposed to not being an alp-e2e checkout at all.

    `alpe2e/` existing plus SOME `.git` entry is necessary but not
    sufficient (tan-cli#835 review, minor): a bare `git init` next to an
    empty `mkdir alpe2e` satisfies both trivially. A STABLE marker is
    required too -- `alpe2e/runner.py`, one of the modules this repo's own
    module docstring already names as living at alp-e2e `main` today, or a
    `origin` remote that names `alplabai/alp-e2e`. `.git` is checked with
    `.exists()`, not `.is_dir()`: a git WORKTREE's `.git` is a small
    pointer FILE (`gitdir: ...`), and a git-worktree checkout of alp-e2e
    (the shape a `git worktree add` checkout of this repo's receiver
    branch actually has, and the only shape that can run this audit
    today) is exactly that.
    """
    package_dir = root / "alpe2e"
    if not package_dir.is_dir():
        return False
    if not (root / ".git").exists():
        return False
    if (package_dir / "runner.py").is_file():
        return True
    return _git_remote_matches_alp_e2e(root)


def _load_alp_e2e_pinverify(root: pathlib.Path) -> types.ModuleType:
    package_dir = root / "alpe2e"
    path = package_dir / "pinverify.py"

    # (1) Present at the canonical path: audit it, unconditionally. No
    # `.git` precondition here -- see the module comment above, MAJOR 2.
    if not path.is_file():
        # (2) Not at the canonical path -- but did the CONTRACT move
        # somewhere else, rather than vanish? tan-cli#835 review, MAJOR 1:
        # this must be a FAIL, not a path that can ever resolve to a SKIP.
        if package_dir.is_dir():
            relocated = _find_relocated_contract_module(package_dir)
            if relocated is not None:
                pytest.fail(
                    f"ALP_E2E_ROOT={root} has no alpe2e/pinverify.py, but "
                    f"{relocated.relative_to(root)} defines the same "
                    "contract objects (_REF / MAX_SOMS / PinMoveRequest) -- "
                    "the receiver's module MOVED. That is exactly the drift "
                    "this gate exists to catch, not grounds to skip: update "
                    "this test's import path (and pin_move_verify.py's own "
                    "comments naming alpe2e/pinverify.py) to the new "
                    "location, then re-run this check against it."
                )

        # (3) Genuinely nothing named the contract anywhere in the tree.
        # Real alp-e2e checkout that predates the receiver -> SKIP. Not an
        # alp-e2e checkout at all -> FAIL.
        if _looks_like_alp_e2e_checkout(root):
            pytest.skip(
                f"ALP_E2E_ROOT={root} is an alp-e2e checkout, but nothing "
                "under alpe2e/ defines the pin-move-verify contract yet. "
                "As of 2026-08-18 (tan-cli#835) the receiver (alp-e2e PR #1) "
                "lives only on the unmerged branch "
                "'feat/pin-move-verify-receiver' -- if that has since "
                "merged to 'main' this note is stale, re-run this test "
                "against a fresh checkout; if not, a checkout that predates "
                "the merge genuinely cannot audit this contract. This is a "
                "SKIP, not a failure of tan-cli's own code."
            )
        pytest.fail(
            f"ALP_E2E_ROOT={root} has no alpe2e/pinverify.py, no relocated "
            "equivalent, and no alp-e2e checkout marker (alpe2e/runner.py, "
            "or an 'origin' remote naming alplabai/alp-e2e) -- this does "
            "not look like an alp-e2e checkout at all"
        )

    spec = importlib.util.spec_from_file_location("_alp_e2e_pinverify_live", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered in sys.modules BEFORE exec: `pinverify.py` uses `from
    # __future__ import annotations`, so its `@dataclass` needs
    # `sys.modules[cls.__module__]` to resolve `PinMoveRequest`'s field
    # types at class-creation time -- without this line that lookup finds
    # nothing and dataclass's own `_process_class` raises `AttributeError`
    # on `None.__dict__` (measured).
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


def _make_checkout(root: pathlib.Path, *, git_as_file: bool = False) -> None:
    """A minimal but REAL-looking alp-e2e checkout: `alpe2e/runner.py`
    (the stable marker `_looks_like_alp_e2e_checkout` accepts) plus a
    `.git` entry. `git_as_file=True` shapes `.git` as a worktree pointer
    FILE rather than a directory -- the actual shape of a `git worktree
    add` checkout of alp-e2e (tan-cli#835 review, minor: nothing
    previously pinned this half of `.exists()` vs `.is_dir()`).
    """
    (root / "alpe2e").mkdir()
    (root / "alpe2e" / "runner.py").write_text("# stand-in\n", encoding="utf-8")
    if git_as_file:
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
    else:
        (root / ".git").mkdir()


def test_missing_pinverify_on_a_real_checkout_skips_not_fails(tmp_path):
    """tan-cli#835 regression: a `root` that IS an alp-e2e checkout (has
    the `alpe2e/runner.py` marker and `.git`) but has no
    `alpe2e/pinverify.py` anywhere -- exactly the shape of a checkout
    sitting on alp-e2e's `main`, where the pin-move-verify receiver has
    not merged yet -- must SKIP with a message that names the branch the
    receiver actually lives on (`feat/pin-move-verify-receiver`), never
    the generic "this does not look like an alp-e2e checkout" wording that
    misdiagnoses a correct checkout as bogus.
    """
    _make_checkout(tmp_path)

    with pytest.raises(pytest.skip.Exception) as exc_info:
        _load_alp_e2e_pinverify(tmp_path)

    message = str(exc_info.value)
    assert "feat/pin-move-verify-receiver" in message
    assert "does not look like an alp-e2e checkout" not in message


def test_missing_pinverify_still_skips_when_dotgit_is_a_worktree_pointer_file(tmp_path):
    """tan-cli#835 review, minor: `.git` as a FILE (a git-worktree pointer,
    the actual shape of the one checkout that can run this gate today --
    `alp-e2e-pinverify`) must be recognised exactly like `.git` as a
    directory. A later tighten of `_looks_like_alp_e2e_checkout` from
    `.exists()` to `.is_dir()` would silently push every worktree and
    submodule checkout into the FAIL arm instead of the SKIP arm; this
    pins the file shape so that regression cannot land silently.
    """
    _make_checkout(tmp_path, git_as_file=True)

    with pytest.raises(pytest.skip.Exception) as exc_info:
        _load_alp_e2e_pinverify(tmp_path)

    assert "feat/pin-move-verify-receiver" in str(exc_info.value)


def test_a_root_with_git_but_no_alpe2e_package_still_fails(tmp_path):
    """One half of the "not a checkout at all" arm: `.git` present, but no
    `alpe2e/` package whatsoever -- not even an empty one.
    """
    (tmp_path / ".git").mkdir()

    with pytest.raises(pytest.fail.Exception) as exc_info:
        _load_alp_e2e_pinverify(tmp_path)

    assert "does not look like an alp-e2e checkout" in str(exc_info.value)


def test_an_empty_alpe2e_package_with_bare_git_init_still_fails(tmp_path):
    """tan-cli#835 review, minor: `mkdir alpe2e && git init` in an
    otherwise empty tree must NOT be told it "IS an alp-e2e checkout" --
    that pair is trivially satisfiable and proves nothing. Neither
    `alpe2e/runner.py` nor a matching `origin` remote is present here, so
    this must FAIL, not skip.
    """
    (tmp_path / "alpe2e").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    with pytest.raises(pytest.fail.Exception) as exc_info:
        _load_alp_e2e_pinverify(tmp_path)

    assert "does not look like an alp-e2e checkout" in str(exc_info.value)


def test_file_present_without_dotgit_is_still_audited_not_failed(tmp_path):
    """tan-cli#835 review, MAJOR 2 regression: a tree that carries
    `alpe2e/pinverify.py` but has no `.git` at all -- a `git archive`
    export, a vendored copy, an unpacked tarball -- must still be
    AUDITED, not rejected on a `.git` technicality. `dev`'s version of
    this test loaded exactly this shape and ran the comparison fine; the
    first cut of this gate's fix regressed it by checking `.git` before
    file presence.
    """
    (tmp_path / "alpe2e").mkdir()
    (tmp_path / "alpe2e" / "pinverify.py").write_text(
        "_REF = None\nMAX_SOMS = 8\n"
        "class PinMoveRequest:\n    pass\n"
        "def check_name(self):\n    return ''\n",
        encoding="utf-8",
    )
    assert not (tmp_path / ".git").exists()

    module = _load_alp_e2e_pinverify(tmp_path)

    assert module.MAX_SOMS == 8


def test_relocated_contract_module_fails_not_skips(tmp_path):
    """tan-cli#835 review, MAJOR 1 regression, the exact reproduction from
    review: the receiver's module renamed from `alpe2e/pinverify.py` to
    `alpe2e/pin_move_verify.py`, with `MAX_SOMS` also changed (8 -> 99).
    Before this fix, `_load_alp_e2e_pinverify` saw only "pinverify.py is
    not there", concluded "checkout predates the receiver", and SKIPPED --
    green, under a confidently false reason, on a checkout that `dev`
    correctly FAILS. A rename/move is drift this gate exists to catch; it
    must always FAIL, and must name the file it found so a human can fix
    the import path.
    """
    _make_checkout(tmp_path)
    (tmp_path / "alpe2e" / "pin_move_verify.py").write_text(
        "_REF = None\nMAX_SOMS = 99\n"
        "class PinMoveRequest:\n    pass\n",
        encoding="utf-8",
    )

    with pytest.raises(pytest.fail.Exception) as exc_info:
        _load_alp_e2e_pinverify(tmp_path)

    message = str(exc_info.value)
    assert "pin_move_verify.py" in message
    assert "MOVED" in message


def _resolve_alp_e2e_root() -> pathlib.Path | None:
    raw = os.environ.get("ALP_E2E_ROOT")
    if not raw:
        return None
    return pathlib.Path(raw).resolve()


def test_mirrored_contract_matches_the_live_alp_e2e_receiver():
    root = _resolve_alp_e2e_root()
    if root is None:
        pytest.skip(
            "ALP_E2E_ROOT is not set -- cannot compare pin_move_verify.py's "
            "mirrored contract against the live alp-e2e receiver. This is a "
            "SKIP, not a pass: bind ALP_E2E_ROOT to a checkout of "
            "alplabai/alp-e2e to actually run this check."
        )
    live = _load_alp_e2e_pinverify(root)

    for name in ("_REF", "_SKU", "_REPO", "_SHA"):
        assert hasattr(live, name), f"alpe2e.pinverify no longer defines {name}"
    assert hasattr(live, "MAX_SOMS"), "alpe2e.pinverify no longer defines MAX_SOMS"
    assert hasattr(live, "PinMoveRequest"), "alpe2e.pinverify no longer defines PinMoveRequest"

    live_example = live.PinMoveRequest(
        tan_ref="TAN_REF",
        sdk_ref="SDK_REF",
        soms=("EXAMPLE",),
        source_repo="owner/name",
        source_sha="0" * 40,
        source_pr=None,
    )
    live_text = (
        f"_REF = {live._REF.pattern!r}\n"
        f"_SKU = {live._SKU.pattern!r}\n"
        f"_REPO = {live._REPO.pattern!r}\n"
        f"_SHA = {live._SHA.pattern!r}\n"
        f"MAX_SOMS = {live.MAX_SOMS!r}\n"
        f"check_name = {live_example.check_name!r}\n"
    )

    assert live_text == pmv.MIRRORED_CONTRACT_TEXT, (
        "pin_move_verify.py's mirrored alpe2e.pinverify contract has drifted "
        "from the live receiver:\n\n"
        f"tan-cli's copy:\n{pmv.MIRRORED_CONTRACT_TEXT}\n"
        f"alp-e2e's live contract:\n{live_text}\n"
        "Diff the two, port the delta into pin_move_verify.py's _REF/_SKU/"
        "_REPO/_SHA/MAX_SOMS/PinMoveTuple.check_name, and update "
        "MIRRORED_CONTRACT_HASH (and its own 'last re-audited against' "
        "comment) to match -- tan-cli#835 tracks this re-audit."
    )
