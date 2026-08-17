#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The pin-move-verify SENDER: turn "does this tan build with this alp-sdk"
from a spelling check into a property gate this repo's own CI can require.

ADR-0029 (`alp-sdk` docs/adr/0029-cross-repo-pins-are-typed-lock-entries-with-
a-property-gate.md, alp-sdk#1474) names the defect: `SUPPORTED_CLI_VERSION =
"0.5.1"` named a published tan release that could not build ANY Renesas or
Alif SoM (tan-cli#639), and the gate that was supposed to catch it --
`check-cli-pin.mjs` -- was green the entire time, because it checked that the
pin *named* a real release, never that the release *worked*. The ADR's fix is
a cross-repo contract: dispatch a proposed `(tan_ref, sdk_ref, soms)` tuple to
`alplabai/alp-e2e`, build it on a clean machine, and report a Check Run back
on the commit that proposed it.

The RECEIVER (alp-e2e, private repo, PR #1: `.github/workflows/pin-move-verify.yml`
+ `alpe2e/pinverify.py` + `journeys/build-tuple.sh`) already exists and is
proven on two real tuples. This module is the SENDER's pure logic: which
pin sites in THIS repo name an alp-sdk ref, whether a given PR touched one,
how to build a dispatch the receiver's own `alpe2e.pinverify.parse_payload`
will accept, and how to turn a polled Check Run into a pass/fail this repo's
CI can require. Everything here is pure and unit-tested (see
`python/tests/scripts/test_pin_move_verify.py`); the workflow that shells
`git diff`, mints a token, calls `gh api`, and polls is the thin, deliberately
under-tested impure boundary -- same split `alpe2e.pinverify` itself
documents, and for the same reason: the judgement is worth testing offline,
40-90 minutes of a CI job is not a place to discover a logic bug.

PIN SITES THIS REPO CARRIES, AND WHY ONLY THREE FILES ARE IN `PIN_SITES`
-------------------------------------------------------------------------

A cross-repo alp-sdk-ref pin is a literal value (a tag, branch, or 40-char
commit SHA) that says "this is the alp-sdk this was built or audited
against" -- exactly the kind of claim `(tan_ref, sdk_ref, soms)` can put
through a build. Swept across this repo (2026-08, see the PR that added this
module for the exact commands run; re-swept 2026-08 during PR #823's
review):

  * `.github/workflows/parity.yml` -- `PINNED_SDK_TAG` (workflow-level
    `env:`). The alp-sdk ref `seam1`/`seam2` build tan's OWN checkout
    against on every PR -- getting-started.yml's own comment calls this "the
    single source for the alp-sdk pin". IN `PIN_SITES`.
    CERTIFIABILITY: partial. It IS the literal `sdk_ref` value this sender
    dispatches, but per "WHAT `tan_ref` MEANS FOR THESE PINS" below, the
    dispatched tuple always tests the latest PUBLISHED tan release against
    it, never this PR's own code -- a PR that bumps this pin AND ports
    `tan/planner/` in the same change (the normal shape: see `ci.yml`'s own
    multi-paragraph pin history below, where all three pins move together)
    is graded on a tan that structurally lacks its own adaptation, clearable
    only by cutting a release first. That is a deadlock, and it is why this
    job is NOT on branch protection (see the workflow's own header and
    `$GITHUB_STEP_SUMMARY` line).
  * `.github/workflows/ci.yml` -- the `sdk_parity` job's `alp-sdk` checkout
    `ref:`, a literal SHA that MUST move in lockstep with `PINNED_SDK_TAG`
    (that file's own multi-paragraph comment documents two REAL mid-review
    divergences this exact fact caused, tan-cli#485 and tan-cli#639 -- both
    caught only by human review, neither by a gate). IN `PIN_SITES` for that
    demonstrated drift risk. CERTIFIABILITY: same partial grade as
    `PINNED_SDK_TAG` above -- this ref always mirrors it, so the dispatch
    cannot certify this site any more independently than that one.
  * `python/tests/gates/test_planner_relocation_freshness.py` --
    `PINNED_SDK_COMMIT`, `HAND_PORT_PINNED_SDK_COMMIT`,
    `STRICT_LOADERS_PINNED_SDK_COMMIT`. Fork-audit bookkeeping: the alp-sdk
    commit `tan/planner/`'s hand-relocated/hand-ported code was last diffed
    against. IN `PIN_SITES`: `ci.yml`'s own pin history names this exact
    trio moving out of step with `PINNED_SDK_TAG` mid-review too (the
    tan-cli#485 entry: "moving THIS ref and ... `PINNED_SDK_COMMIT` ...
    together" while `PINNED_SDK_TAG` itself lagged behind) -- real,
    demonstrated drift risk, the same bar `ci.yml`'s ref is held to.
    CERTIFIABILITY: none. A build tuple proves the PUBLISHED tan builds
    against `sdk_ref`; that says nothing about whether `tan/planner/`'s
    hand-port was RE-AUDITED against that commit's shape -- the published
    tan predates whatever this PR just ported by construction. That claim
    is fully owned by this gate's own sha256 re-hash (`ALP_SDK_ROOT`-gated),
    a strictly more direct check than any build tuple could be. Kept in
    `PIN_SITES` anyway, not because the dispatch certifies the audit claim,
    but because touching this file is exactly the kind of event that has
    historically needed a cross-repo build check triggered alongside it.

Considered, and deliberately NOT in `PIN_SITES`, either because the tuple
this sender can send cannot verify what the site claims, or because the
site cannot change without ALSO touching one of the three sites above in
the SAME commit -- so `touched_pin_sites` already fires by construction, and
listing it again would be a second detector for the same event, not new
coverage:

  * `python/tan/templates/vendored/**`, INCLUDING `MANIFEST.md`'s own
    `Ref:`/`Commit:` lines -- these DO carry a literal alp-sdk tag and
    8-char SHA (verified: `MANIFEST.md` carries explicit "Ref: `v0.15.0`" /
    "Commit: **`f30f4d4b`**" lines for one of its historical vendor-point
    entries, not just prose). An earlier
    version of this list excluded this whole tree on the claim that nothing
    here "carries a literal tag/branch/SHA a payload's `sdk_ref` could be
    extracted from" -- that claim was false for this specific file and is
    corrected here (PR #823 review, finding 3). Excluded for the TRUE reason
    instead: every historical re-vendor of this tree landed in the SAME
    commit as a `PINNED_SDK_TAG` bump (`MANIFEST.md`'s own history,
    verbatim: "This bump lands in the SAME commit as the `PINNED_SDK_TAG`
    move ... because either half alone reds a seam"), and every one of
    those re-vendors was independently re-verified against the live emit
    before being committed ("Verified, not assumed: `scaffold_byte_parity.py
    --sdk <ref>` is rc 0, 9/9 PASS", repeated at every touch point in that
    file's own history) -- a self-checking, regenerate-and-diff mechanism
    with ZERO documented drift incidents, unlike `ci.yml`'s hand-typed ref
    and the freshness gate's hand-typed hashes above, which have each
    drifted for real at least once. `PIN_SITES` already fires whenever this
    tree legitimately changes, via the `.github/workflows/parity.yml` entry
    above. And even where it fires, a build tuple proves "does it compile",
    never "are these vendored bytes byte-identical to the live emit" --
    `scaffold_byte_parity.py`'s own byte-diff is the right, and
    already-present, tool for that claim.
  * `tests/parity/scaffold_byte_parity.py`'s `_SDK_DOC_LINK_REF` /
    `un_edit_doc_link_ref` -- the doc-link-ref transform ADR-0029 observation
    7 names by example (alp-sdk#1474; tan-cli#756/#766: `PINNED_SDK_TAG` was
    deliberately parked one commit short of alp-sdk `dev`'s tip because
    re-vendoring past it would ship scaffold READMEs whose doc links 404
    against a `v0.16.0` tag that does not exist yet -- see that changelog's
    own "Move it the rest of the way when v0.16.0 ships" note). Named here
    explicitly rather than silently missing from this sweep (PR #823 review,
    finding 2). CURRENTLY DEAD: the regex is still frozen at the OLD
    `v0.15.0-rc1` literal from tan-cli#384 and is exercised only by
    `self_check()`'s own hardcoded self-test (fed a synthetic string, not a
    file), not by any live `DELIBERATE_EDITS` entry -- alp-sdk has since cut
    the real `v0.15.0` tag, which retired all seven README entries that used
    to apply this transform (see that file's own comment immediately above
    `DELIBERATE_EDITS`). It is kept only as a template for the NEXT
    pre-release vendor point (`v0.16.0`), per that same comment. When it
    next goes live, the same reasoning as `python/tan/templates/vendored/**`
    above applies: the bump only happens as part of a `PINNED_SDK_TAG` move
    (the tan-cli#756/#766 changelog's own words: "re-vendor the seven
    READMEs in that same change"), and it asserts doc-link reachability, a
    claim no build tuple can test either way. Excluded for that reason, not
    because it was overlooked.
  * `contract/fixtures/bootstrap/manifest.json`,
    `contract/fixtures/toolchains/toolchains.json`,
    `tests/fixtures/kconfig-contract/emit-kconfig.golden.json` -- the other
    three of the "FOUR parity gates" `MANIFEST.md`'s own history says move
    together with the vendored tree above; same self-checking mechanism,
    same reasoning. Unlike the vendored tree, none of these three carries a
    literal alp-sdk tag/branch/SHA at all (checked: only Zephyr-SDK sha256
    values and prose) -- so the original "not a literal ref" reasoning
    stays TRUE for these three specifically. It was only ever false for
    `python/tan/templates/vendored/**`'s own `MANIFEST.md`.
  * `ZEPHYR_SDK_INSTALL_VERSION` (`python/tan/commands/doctor_cmd.py`) --
    names a Zephyr SDK toolchain release, not an alp-sdk ref. There is no
    `toolchain_ref` field in the dispatch contract, and the receiver's
    journey installs whatever `alp-sdk/metadata/toolchains.json` says at the
    dispatched `sdk_ref` -- it never reads this constant at all. Guarded
    instead by `tests/parity/toolchain_lock_parity.py`
    (byte-diff against `metadata/toolchains.json`) -- ADR-0029's "propagated
    constant" model, not "compatibility declaration".
  * `python/tan/version.py`'s `TAN_VERSION` -- this repo's OWN version, not
    a reference to another repo. Not a cross-repo pin at all.

WHAT `tan_ref` MEANS FOR THESE PINS, STATED HONESTLY
-----------------------------------------------------

The receiver's journey installs `tan_ref` through the PUBLISHED `install.sh`
one-liner (`journeys/build-tuple.sh`: `curl .../install.sh | sh -s --
--version "$TAN_REF"`) -- it cannot build an unreleased PR branch. So a PR
that re-audits `tan/planner/` against a new alp-sdk commit is proposing a
change to CODE THIS TUPLE CANNOT DIRECTLY TEST: the dispatch below always
names the latest PUBLISHED tan-cli release as `tan_ref`, which answers "does
the tan a customer can install TODAY still build against the alp-sdk ref
this PR wants to pin against", not "does the code this PR just changed
build against it". That is a real, narrower question than the one the
freshness gate's own hash check answers -- and it is exactly the class of
question `install.sh`-based verification can ask, per the receiver's own
design (see its header: "the customer's path, not a checkout"). Recorded
here rather than glossed over, and restated in `judge_polled_check_run`'s
own PASS message so a reader of a green `$GITHUB_STEP_SUMMARY` sees it too,
not just this docstring or the workflow's header comment.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PayloadError(ValueError):
    """A tuple this sender refuses to send."""


# ---------------------------------------------------------------------------
# The dispatch contract -- mirrored, not reinvented
#
# These four patterns, MAX_SOMS, and PinMoveTuple.check_name's format string
# below are byte-for-byte `alpe2e.pinverify`'s own `_REF` / `_SKU` / `_REPO` /
# `_SHA` / `MAX_SOMS` / `check_name` (alp-e2e, private repo,
# `.github/workflows/pin-move-verify.yml`'s receiver). Duplicating them here
# is NOT a second schema: the receiver re-validates independently and stays
# authoritative (`parse_payload` there is what actually decides what runs).
# The point of validating client-side too is cost -- a malformed payload
# caught here fails in seconds, not after burning a dispatch and however
# many of the receiver's own runner-minutes it takes to notice.
#
# An ungated second copy of a contract is tan-cli#358's exact defect shape,
# so this one is pinned by hash (MIRRORED_CONTRACT_HASH, below the SoM cap)
# with an issue-linked re-audit note (tan-cli#835) -- the same PINNED_HASHES
# discipline `test_planner_relocation_freshness.py` applies to alp-sdk,
# pointed at alp-e2e instead. `python/tests/scripts/
# test_pin_move_verify_contract_mirror.py` re-extracts and re-hashes the
# same block from a local alp-e2e checkout bound at `$ALP_E2E_ROOT`; without
# it the gate SKIPS, loudly, never a silent pass.
# ---------------------------------------------------------------------------
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,99}$")
_SKU = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")

#: Mirrors `alpe2e.pinverify.MAX_SOMS`. The receiver's `plan` job refuses a
#: payload over this cap outright, and its `report` job is
#: `if: always() && needs.plan.result == 'success'` -- so a payload this
#: sender lets through but the receiver refuses never gets a Check Run
#: posted at all, and this job burns its full poll deadline (up to 90
#: minutes) discovering that, contradicting the whole reason client-side
#: validation exists (see the module docstring above: "a malformed payload
#: caught here fails in seconds"). tan-cli#358's shape, avoided by mirroring
#: the cap rather than omitting it.
MAX_SOMS = 8

#: This repo, as the receiver's contract spells it (`source_repo`).
SOURCE_REPO = "alplabai/tan-cli"

#: Both SoM families the receiver's own journey is proven against today (its
#: header: "Both SoM families this receiver is used for today (Renesas
#: RZ/V2N M33, Alif Ensemble M55)"). Two SoMs is also the receiver's own
#: worked cost example -- "about 10 runner-minutes" -- so this is the
#: cheapest tuple that still exercises both toolchains it currently proves.
DEFAULT_SOMS: tuple[str, ...] = ("E1M-V2N101", "E1M-AEN801")

#: repo-relative path -> the pin constant(s) it carries, informational only
#: (used for the human-readable `detect` report). See this module's own
#: docstring for the full sweep, including the sites deliberately excluded.
PIN_SITES: dict[str, tuple[str, ...]] = {
    "python/tests/gates/test_planner_relocation_freshness.py": (
        "PINNED_SDK_COMMIT",
        "HAND_PORT_PINNED_SDK_COMMIT",
        "STRICT_LOADERS_PINNED_SDK_COMMIT",
    ),
    ".github/workflows/parity.yml": ("PINNED_SDK_TAG",),
    ".github/workflows/ci.yml": ("the `sdk_parity` job's alp-sdk checkout `ref:`",),
}


def touched_pin_sites(changed_files: Iterable[str]) -> tuple[str, ...]:
    """Which `PIN_SITES` keys appear in `changed_files`. Pure.

    Paths are normalised to POSIX separators before comparison so this
    agrees whether `git diff` (or a test) hands it Windows or POSIX
    separators -- the sender's own CI runs on `ubuntu-latest`, but the unit
    tests run wherever a developer's machine is.
    """
    changed = {pathlib.PurePosixPath(f.replace("\\", "/")).as_posix() for f in changed_files}
    return tuple(site for site in PIN_SITES if site in changed)


_PINNED_SDK_TAG_LINE = re.compile(r"^\s*PINNED_SDK_TAG:\s*(\S+)\s*$", re.MULTILINE)


def extract_pinned_sdk_tag(parity_yml_text: str) -> str:
    """The one `PINNED_SDK_TAG:` value in `parity.yml`'s workflow-level
    `env:` block -- getting-started.yml's own comment names this "the single
    source for the alp-sdk pin". Refuses anything but exactly one match, the
    same discipline `parity.yml`'s own `grep -oE '^PINNED_SDK_COMMIT = ...'`
    steps already apply to the freshness gate's pins.
    """
    matches = _PINNED_SDK_TAG_LINE.findall(parity_yml_text)
    if len(matches) != 1:
        raise PayloadError(
            "expected exactly ONE '^PINNED_SDK_TAG:' line in parity.yml, "
            f"found {len(matches)} -- refusing to guess which one is live"
        )
    return matches[0]


def _field(value: str | None, name: str, pattern: re.Pattern[str], hint: str) -> str:
    got = (value or "").strip()
    if not got:
        raise PayloadError(f"{name} is required")
    if not pattern.match(got):
        raise PayloadError(f"{name}={got!r} is not acceptable: {hint}")
    return got


def _soms(values: Sequence[str]) -> tuple[str, ...]:
    if not values:
        raise PayloadError("soms is empty -- there is nothing to verify")
    seen: list[str] = []
    for v in values:
        sku = _field(v, "soms[]", _SKU, "expected a SoM SKU such as E1M-V2N101")
        if sku not in seen:
            seen.append(sku)
    if len(seen) > MAX_SOMS:
        raise PayloadError(
            f"{len(seen)} distinct SoMs requested, cap is {MAX_SOMS} -- mirrors "
            "the receiver's own alpe2e.pinverify.MAX_SOMS (each SoM costs "
            "roughly 5 runner-minutes there, so an uncapped request is an "
            "uncapped bill). The receiver's `plan` job refuses a payload over "
            "this cap before its `report` job (which posts the Check Run) ever "
            "runs, so a tuple this sender let through would silently burn this "
            "job's whole poll deadline waiting for a Check Run that will never "
            "appear -- refusing here fails in seconds instead."
        )
    return tuple(seen)


def _pr(value: str | int | None) -> int | None:
    if value in (None, "", "null", "none"):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise PayloadError(f"source_pr={value!r} is not an integer") from exc
    if n < 1:
        raise PayloadError(f"source_pr={value!r} is not a valid PR number")
    return n


@dataclass(frozen=True, slots=True)
class PinMoveTuple:
    """One proposed `(tan_ref, sdk_ref, soms)` tuple, validated -- field for
    field the same shape `alpe2e.pinverify.PinMoveRequest` parses.
    """

    tan_ref: str
    sdk_ref: str
    soms: tuple[str, ...]
    source_sha: str
    source_pr: int | None = None
    source_repo: str = SOURCE_REPO

    @property
    def check_name(self) -> str:
        """Byte-for-byte `alpe2e.pinverify.PinMoveRequest.check_name`
        (note the `·` and `×`, not ASCII lookalikes). The sender's poll loop
        searches `source_sha`'s check runs for this EXACT string; a spelling
        drift here makes the sender wait out its whole deadline for a check
        that already posted under the receiver's own spelling.
        """
        return f"pin-verify · {self.tan_ref} × {self.sdk_ref}"

    def as_client_payload(self) -> dict[str, Any]:
        return {
            "tan_ref": self.tan_ref,
            "sdk_ref": self.sdk_ref,
            "soms": list(self.soms),
            "source_repo": self.source_repo,
            "source_sha": self.source_sha,
            "source_pr": self.source_pr,
        }

    def as_dispatch_body(self) -> dict[str, Any]:
        """The exact `POST /repos/alplabai/alp-e2e/dispatches` body."""
        return {"event_type": "pin-move-verify", "client_payload": self.as_client_payload()}


def _mirrored_contract_text() -> str:
    """The exact block that must match `alpe2e.pinverify`'s own copy --
    the four regex patterns, `MAX_SOMS`, and `check_name`'s format string,
    all read from the live objects above rather than retyped, so this text
    cannot itself drift from the code it describes.
    """
    example = PinMoveTuple(
        tan_ref="TAN_REF", sdk_ref="SDK_REF", soms=("EXAMPLE",), source_sha="0" * 40
    )
    return (
        f"_REF = {_REF.pattern!r}\n"
        f"_SKU = {_SKU.pattern!r}\n"
        f"_REPO = {_REPO.pattern!r}\n"
        f"_SHA = {_SHA.pattern!r}\n"
        f"MAX_SOMS = {MAX_SOMS!r}\n"
        f"check_name = {example.check_name!r}\n"
    )


#: See "The dispatch contract -- mirrored, not reinvented" above.
#: `python/tests/scripts/test_pin_move_verify_contract_mirror.py` re-hashes
#: this SAME text after re-extracting the equivalent block from a local
#: alp-e2e checkout bound at `$ALP_E2E_ROOT`; a mismatch means either side
#: moved and this constant needs a hand re-audit (diff `alpe2e/pinverify.py`,
#: port the delta, update this hash and the "last audited" note below).
#: Without `$ALP_E2E_ROOT` that test SKIPS, loudly, never a silent pass.
#:
#: Last re-audited by hand against alp-e2e commit
#: `fe8202890afb405e132482d9b7a23a76348bd710` (2026-08-16, alp-e2e PR #1).
#: tan-cli#835 tracks the next re-audit.
MIRRORED_CONTRACT_TEXT = _mirrored_contract_text()
MIRRORED_CONTRACT_HASH = "62ebc38153106a76b095670645e594394104534d5e35bb10250f9013260b48be"


def build_tuple(
    *,
    tan_ref: str,
    sdk_ref: str,
    source_sha: str,
    source_pr: int | str | None = None,
    soms: Sequence[str] = DEFAULT_SOMS,
    source_repo: str = SOURCE_REPO,
) -> PinMoveTuple:
    """Validate and assemble one dispatch. Raises `PayloadError`, never
    guesses -- refusing loudly here is cheaper than a dispatch the receiver
    would refuse anyway (`alpe2e.pinverify.parse_payload`'s own module
    docstring: "a receiver that quietly coerces a malformed payload reports
    a verdict about some OTHER tuple ... which is worse than not reporting
    at all" -- the same argument applies to sending one).
    """
    return PinMoveTuple(
        tan_ref=_field(tan_ref, "tan_ref", _REF, "expected a tag or branch, e.g. v0.5.1"),
        sdk_ref=_field(sdk_ref, "sdk_ref", _REF, "expected a tag or branch, e.g. v0.16.0-rc1"),
        soms=_soms(list(soms)),
        source_sha=_field(
            source_sha,
            "source_sha",
            _SHA,
            "expected a full 40-character commit SHA -- the Check Runs API "
            "will not attach a check to an abbreviation",
        ),
        source_pr=_pr(source_pr),
        source_repo=_field(source_repo, "source_repo", _REPO, "expected owner/name"),
    )


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"


def judge_polled_check_run(
    check_run: Mapping[str, Any] | None,
    *,
    timed_out: bool,
    expected_check_name: str,
    expected_source_sha: str,
    tan_ref: str,
) -> tuple[Verdict, str]:
    """Fail-closed, on purpose: the ONLY shape that returns PASS is a
    completed check run whose `name`/`head_sha` match this dispatch and
    whose `conclusion` is the literal string `"success"`. Every other shape
    -- not found, a different check's name or commit, still in progress at
    the deadline, `neutral`, `failure`, cancelled, an unrecognised
    conclusion -- returns FAIL.

    This is deliberately STRICTER than GitHub's own required-status-check
    semantics. The receiver's own header states plainly: "GitHub treats a
    neutral check as satisfying a required status check -- so `neutral` does
    not BLOCK a pin move, it withholds the proof that would allow it. The
    sender is expected to require `success`." This function is that
    requirement, encoded, not left to whoever reads the log -- the same
    fail-closed discipline `pr-bootstrap-distro-install.yml`'s `summary` job
    applies to a `skipped` result: never trust a non-`success` shape at face
    value.

    `expected_check_name`/`expected_source_sha` close a narrower gap:
    `check_name` (`pin-verify · <tan_ref> × <sdk_ref>`) encodes only
    `tan_ref`/`sdk_ref`, not `soms` -- a first-class member of ADR-0029's
    tuple. The receiver's `workflow_dispatch` entrypoint means a hand-run
    with the same two refs but a DIFFERENT SoM set lands under the same
    name on the same commit; without this check that unrelated run's
    conclusion would be graded as this dispatch's own verdict.
    """
    if not check_run:
        if timed_out:
            return Verdict.FAIL, (
                "receiver timeout: no completed pin-verify Check Run appeared "
                "on the source commit before the deadline. This could mean "
                "the dispatch never reached a listener, the receiver's run is "
                "still queued behind other work, or the App has no "
                "Checks:write grant on this repo. A timeout proves nothing "
                "about the tuple, so it is treated as a failure to move the "
                "pin, not a pass."
            )
        return Verdict.FAIL, (
            "no check run was polled and no timeout was recorded -- this is "
            "an internal error in the poll loop, not a receiver verdict; "
            "failing closed rather than guessing which it was."
        )
    name = check_run.get("name")
    head_sha = check_run.get("head_sha")
    if name != expected_check_name or head_sha != expected_source_sha:
        return Verdict.FAIL, (
            f"the polled check run's identity does not match this dispatch -- "
            f"name={name!r} (expected {expected_check_name!r}), "
            f"head_sha={head_sha!r} (expected {expected_source_sha!r}). `soms` "
            "is not part of `check_name`, so an unrelated run (e.g. a hand-run "
            "workflow_dispatch with the same two refs but a different SoM set) "
            "could otherwise be graded as this dispatch's own verdict; failing "
            "closed rather than trusting an unverified match."
        )
    status = check_run.get("status")
    if status != "completed":
        return Verdict.FAIL, (
            f"the polled check run is still {status!r}, not completed -- the "
            "poll loop must not report a verdict before the receiver "
            "finished judging; failing closed rather than reading a partial "
            "result as any particular answer."
        )
    conclusion = check_run.get("conclusion")
    if conclusion == "success":
        output = check_run.get("output")
        title = (output or {}).get("title") if isinstance(output, Mapping) else None
        # The qualifier a reader of a green check needs, not just this
        # module's docstring or the workflow's header comment (PR #823
        # review, finding 5): this dispatch always installs the latest
        # PUBLISHED tan release, never this PR's own code -- see "WHAT
        # tan_ref MEANS FOR THESE PINS" above for the full reasoning.
        headline = (
            f"the PUBLISHED tan {tan_ref} builds against the proposed sdk_ref "
            "via the customer install path -- this is NOT proof that this "
            "PR's own code builds against it"
        )
        return Verdict.PASS, f"{headline}: {title}" if title else headline
    return Verdict.FAIL, (
        f"the receiver's conclusion is {conclusion!r}, not 'success'. "
        "ADR-0029 clause 2: no pin entry moves without a green run on the "
        "new value, and a `neutral` conclusion (an infrastructure abort, or "
        "a run where nothing built) proves nothing either way -- so this "
        "sender requires a literal success, never merely 'not failure'."
    )


# ---------------------------------------------------------------------------
# The impure boundary -- everything above is pure and unit-tested; everything
# below touches git, the filesystem, argv, and GITHUB_OUTPUT, and is kept
# deliberately thin. Same split `alpe2e.pinverify` documents for itself.
# ---------------------------------------------------------------------------


def _emit(key: str, value: str) -> None:
    """Write one GitHub Actions step output, if running in one."""
    dest = os.environ.get("GITHUB_OUTPUT")
    if not dest:
        return
    with open(dest, "a", encoding="utf-8") as fh:
        if "\n" in value:
            fh.write(f"{key}<<__PIN_MOVE_VERIFY_EOF__\n{value}\n__PIN_MOVE_VERIFY_EOF__\n")
        else:
            fh.write(f"{key}={value}\n")


def _changed_files(repo_root: pathlib.Path, base: str, head: str) -> list[str]:
    """`git diff --name-only base...head` (merge-base diff -- the PR's own
    changes, not "everything base has done since"), run against `repo_root`.
    """
    out = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--name-only", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def _cmd_detect(args: argparse.Namespace) -> int:
    repo_root = pathlib.Path(args.repo_root).resolve()
    changed = _changed_files(repo_root, args.base, args.head)
    sites = touched_pin_sites(changed)
    doc = {"touched": bool(sites), "sites": list(sites), "changed_files": changed}
    print(json.dumps(doc, indent=2))
    _emit("touched", "true" if sites else "false")
    _emit("sites", ",".join(sites))
    return 0


def _cmd_sdk_ref(args: argparse.Namespace) -> int:
    text = pathlib.Path(args.parity_yml).read_text(encoding="utf-8")
    try:
        ref = extract_pinned_sdk_tag(text)
    except PayloadError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2
    print(ref)
    _emit("sdk_ref", ref)
    return 0


def _cmd_dispatch_body(args: argparse.Namespace) -> int:
    try:
        t = build_tuple(
            tan_ref=args.tan_ref,
            sdk_ref=args.sdk_ref,
            source_sha=args.source_sha,
            source_pr=args.source_pr,
            soms=args.som or list(DEFAULT_SOMS),
            source_repo=args.source_repo or SOURCE_REPO,
        )
    except PayloadError as exc:
        print(f"!! refusing this tuple: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(t.as_dispatch_body(), indent=2))
    _emit("check_name", t.check_name)
    _emit("tan_ref", t.tan_ref)
    _emit("sdk_ref", t.sdk_ref)
    _emit("soms_csv", ",".join(t.soms))
    return 0


def _cmd_judge(args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if args.check_run == "-" else pathlib.Path(args.check_run).read_text(
        encoding="utf-8"
    )
    doc: Any = json.loads(raw) if raw.strip() else None
    if isinstance(doc, Mapping) and not doc:
        doc = None
    verdict, reason = judge_polled_check_run(
        doc,
        timed_out=args.timed_out,
        expected_check_name=args.expected_check_name,
        expected_source_sha=args.expected_source_sha,
        tan_ref=args.tan_ref,
    )
    print(f"{verdict.value}: {reason}")
    _emit("verdict", verdict.value)
    _emit("reason", reason)
    return 0 if verdict is Verdict.PASS else 1


def cli(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="pin_move_verify.py",
        description="the pin-move-verify SENDER: propose a tuple, then judge alp-e2e's reply",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="which pin sites a base..head diff touched")
    d.add_argument("--base", required=True)
    d.add_argument("--head", required=True)
    d.add_argument("--repo-root", default=".")
    d.set_defaults(fn=_cmd_detect)

    s = sub.add_parser("sdk-ref", help="the live PINNED_SDK_TAG value out of parity.yml")
    s.add_argument("--parity-yml", default=".github/workflows/parity.yml")
    s.set_defaults(fn=_cmd_sdk_ref)

    p = sub.add_parser("dispatch-body", help="build + validate the repository_dispatch POST body")
    p.add_argument("--tan-ref", required=True)
    p.add_argument("--sdk-ref", required=True)
    p.add_argument("--source-sha", required=True)
    p.add_argument("--source-pr", default=None)
    p.add_argument("--som", action="append", default=None)
    p.add_argument(
        "--source-repo",
        default=None,
        help=f"owner/name this dispatch claims as its source (default: {SOURCE_REPO}); "
        "pass ${{ github.repository }} from the workflow rather than relying on the "
        "hardcoded default, so a fork of this repo does not silently claim to be it",
    )
    p.set_defaults(fn=_cmd_dispatch_body)

    j = sub.add_parser("judge", help="turn a polled Check Run into a pass/fail")
    j.add_argument("--check-run", required=True, help="JSON file, or - for stdin")
    j.add_argument("--timed-out", action="store_true")
    j.add_argument(
        "--expected-check-name",
        required=True,
        help="the check_name this dispatch itself proposed -- a polled run whose "
        "name doesn't match is not this dispatch's verdict",
    )
    j.add_argument(
        "--expected-source-sha",
        required=True,
        help="the source_sha this dispatch itself proposed -- a polled run whose "
        "head_sha doesn't match is not this dispatch's verdict",
    )
    j.add_argument(
        "--tan-ref",
        required=True,
        help="the tan_ref this dispatch proposed, echoed into a PASS verdict's own "
        "reason so a reader of a green check sees the published-vs-PR-code caveat",
    )
    j.set_defaults(fn=_cmd_judge)

    args = ap.parse_args(list(argv))
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1:]))
