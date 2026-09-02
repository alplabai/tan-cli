#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Propose the `tan/planner/` re-sync an alp-sdk planner change owes.

`python/tests/gates/test_planner_relocation_freshness.py` catches drift AFTER
it has already diverged: it re-hashes alp-sdk's `scripts/alp_orchestrate/**`
(and nine hand-port sources elsewhere in `scripts/`) against three pinned
audits and goes red. It has caught real drift three times (tan-cli#320, #485,
#543) and its strictness is correct. What it has never done is say what the
fix IS -- so every catch has been hand-carried: read the upstream diff, retype
the delta into `tan/planner/`, recompute two hash tables by hand, move two
commit pins. ADR-0020's own remediation asks for this half and it was never
built (alp-sdk#855).

This script is that half. Given a bound alp-sdk checkout and a target ref it
classifies every tracked file, applies what can be applied, refuses what
cannot, and reports both. `.github/workflows/planner-resync.yml` runs it and
turns the result into a PR against `dev`. It never merges anything.

WHY A 3-WAY MERGE AND NOT A COPY -- the load-bearing design fact
---------------------------------------------------------------
`tan/planner/` is described in places as a "verbatim mirror". Measured against
alp-sdk `7d58ef32`, it is not: 16 of the 20 relocated modules differ from their
upstream counterpart, by 2 lines (`__init__.py`) to 329 (`kconfig_symbols.py`),
and the differences are real tan-side adaptations -- docstrings naming tan's
own test files, a PyInstaller-extraction hazard that does not exist upstream,
`paths.py`'s bound-SDK-root resolution. Only the UPSTREAM side of the
comparison is hash-pinned; the tan side moves on its own. So `cp` would
silently delete those adaptations, which is exactly the "conflating the two
halves would discard tan-side adaptations" failure this tool must not commit.

The mechanism is therefore a 3-way merge per file:

    base   = the upstream blob at the pinned audit commit
    theirs = the upstream blob at the target ref
    ours   = the current `tan/planner/<name>.py`

A clean merge means the upstream delta landed on tan's copy without touching a
region tan had adapted. A conflict means it did, and the file is left ALONE
with the conflict recorded -- never written half-merged, never written with
conflict markers.

WHAT IT REFUSES TO DO
---------------------
* **Hand-ports are never merged, only flagged.** `gen_zephyr_board.py`,
  `alp_template.py`, `alp_project_loader.py` (which becomes TWO tan modules),
  `alp_project_emit/**` and `sentinels.py` were hand-ported, not relocated:
  their tan counterparts are restructured, renamed, split, or inlined, so
  there is no base/ours/theirs triple a merge could be correct over. When one
  of their sources moves, this script attaches the upstream diff and stops. It
  does not move `HAND_PORT_PINNED_SDK_COMMIT`, so the gate stays RED until a
  human ports it -- which is the honest state.
* **`STRICT_LOADERS_PINNED_SDK_COMMIT` is checked but never moved.** It is not
  an "audited against the latest SDK" pin like the other two: it names the
  commit that INTRODUCED `scripts/strict_loaders.py` (`26b0040e`, older than
  both other pins), and the gate file's own block at that constant records a
  KNOWN OPEN GAP measured against it -- `template.py`'s `_rendered_bytes` /
  `render_to_envelope` catalog-driven READS are not confined, so a hostile
  catalog can read an arbitrary file and hand it back as scaffold content.
  Advancing that pin automatically would re-freeze that recorded gap under a
  newer commit and erase the only place it is written down. So: detected,
  reported, never moved.
* **A lying pin aborts the whole run.** If the base blob's sha256 does not
  match the hash the gate pins for it, the pin and the table disagree about
  what was audited -- someone edited one without the other. Merging from a
  base that was never the audited text would produce a plausible-looking diff
  built on a false premise, so this refuses to do anything at all.
* **A new or deleted upstream module is reported, never invented.** A module
  appearing in `scripts/alp_orchestrate/` needs a relocation decision (does it
  belong in tan at all, under what name); a module disappearing needs a
  deletion decision. Both block the pin move.

EXIT CODES
----------
0  nothing to do, or a fully clean re-sync (pins moved, files merged)
1  a re-sync is owed but part of it needs a human (partial or nothing applied)
2  refused: a pin and its hash table disagree, or the SDK checkout is unusable
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

#: Repo-relative paths. Resolved against `--repo-root`, which defaults to the
#: checkout this script lives in (`python/scripts/` -> two parents up).
GATE_REL = "python/tests/gates/test_planner_relocation_freshness.py"
PLANNER_REL = "python/tan/planner"
#: The one upstream directory whose modules relocated 1:1 into `tan/planner/`.
MIRROR_DIR = "scripts/alp_orchestrate"

#: The three (pin constant, hash table) pairs the gate file carries, and
#: whether this script is allowed to advance the pin. See the module docstring
#: for why `STRICT_LOADERS_*` is False rather than absent -- absent would mean
#: "not checked", which is a different and worse thing.
PIN_MOVABLE = {
    "PINNED_SDK_COMMIT": True,
    "HAND_PORT_PINNED_SDK_COMMIT": True,
    "STRICT_LOADERS_PINNED_SDK_COMMIT": False,
}


class Refused(Exception):
    """Raised for exit-2 conditions: the inputs are not fit to reason over."""


# --------------------------------------------------------------------------
# Pure: parsing and rewriting the gate file
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    """The gate file's pins and hash tables, read without importing it.

    Deliberately `ast`-parsed rather than imported: the gate module imports
    `tests.conftest`, resolves an SDK root at import time, and is collected by
    pytest -- importing it from a CI script would couple this tool to the test
    package's own import graph for no gain. Everything needed here is a plain
    module-level literal.
    """

    pinned_sdk_commit: str
    pinned_hashes: dict[str, str]
    hand_port_pinned_sdk_commit: str
    hand_port_hashes: dict[str, str]
    hand_port_sources: dict[str, str]
    strict_loaders_pinned_sdk_commit: str
    strict_loaders_hash: str


_WANTED = {
    "PINNED_SDK_COMMIT": "pinned_sdk_commit",
    "PINNED_HASHES": "pinned_hashes",
    "HAND_PORT_PINNED_SDK_COMMIT": "hand_port_pinned_sdk_commit",
    "HAND_PORT_HASHES": "hand_port_hashes",
    "HAND_PORT_SOURCES": "hand_port_sources",
    "STRICT_LOADERS_PINNED_SDK_COMMIT": "strict_loaders_pinned_sdk_commit",
    "STRICT_LOADERS_HASH": "strict_loaders_hash",
}


def parse_gate(text: str) -> Gate:
    """Read the seven constants the gate file records its audits in."""
    tree = ast.parse(text)
    found: dict[str, object] = {}
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        for name in targets:
            if name in _WANTED and node.value is not None:
                found[_WANTED[name]] = ast.literal_eval(node.value)
    missing = sorted(set(_WANTED.values()) - set(found))
    if missing:
        raise Refused(
            f"{GATE_REL} is missing constant(s) this tool re-pins: {missing}. "
            "Either the gate was restructured or a pin was deleted; re-sync by "
            "hand and teach this script the new shape rather than guessing."
        )
    return Gate(**found)  # type: ignore[arg-type]


def rewrite_pin(text: str, name: str, new_commit: str) -> str:
    """Move one `NAME = "<40hex>"` pin, preserving its trailing comment.

    The anchored `^NAME = "<40hex>"` form is not incidental: `parity.yml` greps
    `HAND_PORT_PINNED_SDK_COMMIT` and `STRICT_LOADERS_PINNED_SDK_COMMIT` with
    that exact regex and fails when it does not match exactly once, so the
    rewrite must keep the line shape byte-for-byte apart from the hex.
    """
    pattern = re.compile(rf'^({re.escape(name)} = ")[0-9a-f]{{40}}(")', re.M)
    new_text, n = pattern.subn(rf"\g<1>{new_commit}\g<2>", text)
    if n != 1:
        raise Refused(
            f"expected exactly ONE `^{name} = \"<40hex>\"` line in {GATE_REL}, "
            f"found {n}. Refusing to rewrite a file whose pin shape this tool "
            "does not recognise."
        )
    return new_text


def rewrite_hash_table(text: str, name: str, hashes: dict[str, str]) -> str:
    """Replace a `NAME: dict[str, str] = { ... }` literal wholesale.

    Rewrites the whole block rather than patching individual entries so an
    entry that must be ADDED or REMOVED (a new or deleted upstream module) is
    expressible. Key order is preserved for keys that survive, with new keys
    appended -- an alphabetical re-sort would make every re-sync diff
    unreadable.
    """
    start = re.compile(rf"^{re.escape(name)}: dict\[str, str\] = \{{$", re.M)
    m = start.search(text)
    if m is None:
        raise Refused(
            f"could not find `^{name}: dict[str, str] = {{` in {GATE_REL} -- "
            "the table's declaration shape changed; re-sync by hand."
        )
    end = text.index("\n}\n", m.end())
    body = "\n".join(f'    "{k}": "{v}",' for k, v in hashes.items())
    return text[: m.end()] + "\n" + body + text[end:]


def audit_note(
    kind: str, base: str, head: str, commits: list[str], detail: list[str]
) -> str:
    """The machine-written block inserted above a pin this script moves.

    It records WHAT moved and refuses to characterise it. The audited
    narrative -- which upstream commit is behavioural, what was ported, what
    was re-measured rather than assumed -- is what every existing block above
    these pins contains, and it is the reviewer's to write. Saying so in the
    file is the point: a re-sync PR whose only comment is this block has not
    been reviewed yet.
    """
    lines = [
        f"#: AUTOMATED RE-SYNC ({kind}): `{base[:8]}` -> `{head[:8]}`, proposed by",
        "#: `python/scripts/planner_resync.py`. THIS BLOCK IS MACHINE-WRITTEN and",
        "#: records only WHAT moved -- it makes no claim about behaviour, and no",
        "#: claim that anything was audited. Replace it with the audited narrative",
        "#: (which of the commits below are behavioural, what was ported, what was",
        "#: re-measured rather than taken on a subject line) before merging. That",
        "#: narrative is the reviewer's job and is the reason this PR does not",
        "#: auto-merge.",
        "#:",
    ]
    lines += [f"#:   {line}" for line in detail]
    if detail:
        lines.append("#:")
    lines.append("#: Upstream commits in range touching this table's files:")
    lines += [f"#:   - {c}" for c in commits] or ["#:   (none)"]
    return "\n".join(lines) + "\n"


def insert_note_above_pin(text: str, name: str, note: str) -> str:
    """Put `note` immediately above the `NAME = "..."` line."""
    pattern = re.compile(rf'^{re.escape(name)} = "[0-9a-f]{{40}}"', re.M)
    m = pattern.search(text)
    if m is None:
        raise Refused(f"no `^{name} = \"<40hex>\"` line to anchor the note above")
    return text[: m.start()] + note + text[m.start() :]


# --------------------------------------------------------------------------
# Pure-ish: the 3-way merge
# --------------------------------------------------------------------------


def three_way_merge(ours: bytes, base: bytes, theirs: bytes) -> tuple[bytes, int]:
    """`git merge-file` over three in-memory blobs.

    Returns `(merged_bytes, conflicts)`. `conflicts > 0` means the upstream
    delta touched a region tan had adapted; the caller must NOT write the
    result (it carries conflict markers). `git merge-file` reports the conflict
    count as its exit status and 255 on error.
    """
    if base == theirs:
        return ours, 0
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        (d / "ours").write_bytes(ours)
        (d / "base").write_bytes(base)
        (d / "theirs").write_bytes(theirs)
        proc = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "-L",
                "tan/planner (ours)",
                "-L",
                "alp-sdk @ pinned audit (base)",
                "-L",
                "alp-sdk @ target ref (theirs)",
                str(d / "ours"),
                str(d / "base"),
                str(d / "theirs"),
            ],
            capture_output=True,
        )
    if proc.returncode == 255 or proc.returncode < 0:
        raise Refused(
            "git merge-file failed: " + proc.stderr.decode("utf-8", "replace")
        )
    return proc.stdout, proc.returncode


# --------------------------------------------------------------------------
# IO: reading the bound alp-sdk checkout
# --------------------------------------------------------------------------


def _git(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False
    )


def git_show(root: pathlib.Path, ref: str, path: str) -> bytes | None:
    """The blob at `ref:path`, or None when the path does not exist there."""
    proc = _git(root, "show", f"{ref}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def git_resolve(root: pathlib.Path, ref: str) -> str:
    proc = _git(root, "rev-parse", f"{ref}^{{commit}}")
    if proc.returncode != 0:
        raise Refused(
            f"cannot resolve `{ref}` in the bound alp-sdk checkout at {root}: "
            + proc.stderr.decode("utf-8", "replace").strip()
        )
    return proc.stdout.decode().strip()


def git_log_subjects(
    root: pathlib.Path, base: str, head: str, paths: list[str]
) -> list[str]:
    proc = _git(root, "log", "--format=%h %s", f"{base}..{head}", "--", *paths)
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln]


def git_diff(
    root: pathlib.Path, base: str, head: str, path: str, max_lines: int
) -> str:
    proc = _git(root, "diff", "--no-color", f"{base}..{head}", "--", path)
    out = proc.stdout.decode("utf-8", "replace")
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [
            f"... [{len(out.splitlines()) - max_lines} more lines -- "
            f"`git -C <alp-sdk> diff {base[:8]}..{head[:8]} -- {path}`]"
        ]
    return "\n".join(lines)


def list_mirror_modules(root: pathlib.Path, ref: str) -> list[str]:
    proc = _git(root, "ls-tree", "--name-only", f"{ref}:{MIRROR_DIR}")
    if proc.returncode != 0:
        raise Refused(
            f"`{MIRROR_DIR}` does not exist at {ref} in the bound alp-sdk "
            "checkout -- the relocation source is gone, which is not something "
            "this tool may paper over."
        )
    return sorted(
        n for n in proc.stdout.decode().split() if n.endswith(".py")
    )


# --------------------------------------------------------------------------
# The classification
# --------------------------------------------------------------------------


@dataclass
class FileVerdict:
    path: str  #: alp-sdk-relative source path
    target: str | None  #: repo-relative tan file, when there is exactly one
    status: str
    detail: str = ""
    merged: bytes | None = None  #: written only when status == "merged"
    new_hash: str | None = None
    diff: str = ""


@dataclass
class Report:
    sdk_head: str
    mirror_base: str
    hand_port_base: str
    strict_base: str
    mirror: list[FileVerdict] = field(default_factory=list)
    hand_port: list[FileVerdict] = field(default_factory=list)
    strict: list[FileVerdict] = field(default_factory=list)
    mirror_commits: list[str] = field(default_factory=list)
    hand_port_commits: list[str] = field(default_factory=list)

    @property
    def blocked_mirror(self) -> list[FileVerdict]:
        return [v for v in self.mirror if v.status not in ("unchanged", "merged")]

    @property
    def blocked_hand_port(self) -> list[FileVerdict]:
        return [v for v in self.hand_port + self.strict if v.status != "unchanged"]

    @property
    def mirror_moves(self) -> bool:
        """The mirror pin may advance only when EVERY mirror file resolved."""
        return not self.blocked_mirror and any(
            v.status == "merged" for v in self.mirror
        )

    @property
    def hand_port_moves(self) -> bool:
        """The hand-port pin advances only when NOTHING in its table moved,
        and only as part of a re-sync the mirror half is already carrying.

        There is no "clean" hand-port re-sync: a changed hand-port source is
        by construction a human's job, so the pin stays where it is -- which
        keeps this script reporting `partial` (and `planner-resync.yml`
        re-running and exiting 1) on every subsequent run. `strict`
        counts as a blocker here because advancing
        HAND_PORT_PINNED_SDK_COMMIT past an unported `strict_loaders.py`
        change would be the same re-freeze in a different table.

        The `mirror_moves` conjunct is what stops this pin ratcheting forward
        on its own every time alp-sdk lands a commit that touches neither
        table: that would be a daily no-op PR, and tan-cli#296's whole point
        is that these two audits drift at different rates and must not be
        bumped for each other's reasons. Bundled into a real re-sync, both
        pins land on ONE commit, which is what makes the range narratives
        above them (`<a> -> <b>`) readable at all.
        """
        return (
            not self.blocked_hand_port
            and self.mirror_moves
            and self.hand_port_base != self.sdk_head
        )

    @property
    def verdict(self) -> str:
        if self.blocked_mirror or self.blocked_hand_port:
            return "partial"
        return "clean" if self.mirror_moves else "up-to-date"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def classify(
    sdk: pathlib.Path,
    repo: pathlib.Path,
    gate: Gate,
    head: str,
    diff_lines: int = 400,
) -> Report:
    mirror_base = gate.pinned_sdk_commit
    hp_base = gate.hand_port_pinned_sdk_commit
    st_base = gate.strict_loaders_pinned_sdk_commit
    rep = Report(sdk_head=head, mirror_base=mirror_base, hand_port_base=hp_base,
                 strict_base=st_base)

    # --- mirror half -------------------------------------------------------
    upstream_now = list_mirror_modules(sdk, head)
    for name in sorted(set(gate.pinned_hashes) | set(upstream_now)):
        rel = f"{MIRROR_DIR}/{name}"
        tan_file = repo / PLANNER_REL / name
        tan_rel = f"{PLANNER_REL}/{name}"
        if name not in gate.pinned_hashes:
            rep.mirror.append(
                FileVerdict(
                    rel,
                    None,
                    "new-upstream",
                    "a module exists upstream with no counterpart pinned here. "
                    "Relocating it needs a decision this tool must not make "
                    "(does it belong in tan at all, under what name, wired "
                    "where) -- port it by hand and add it to PINNED_HASHES.",
                )
            )
            continue
        pinned = gate.pinned_hashes[name]
        base_blob = git_show(sdk, mirror_base, rel)
        if base_blob is None:
            raise Refused(
                f"{rel} does not exist at the pinned audit commit "
                f"{mirror_base} -- the bound checkout cannot be the one this "
                "gate was pinned against."
            )
        if _sha(base_blob) != pinned:
            raise Refused(
                f"{rel}: PINNED_HASHES says {pinned} but the blob at "
                f"PINNED_SDK_COMMIT ({mirror_base}) hashes to {_sha(base_blob)}. "
                "The pin and the table disagree about what was audited, so "
                "every merge base here would be a false premise. Refusing to "
                "propose anything."
            )
        head_blob = git_show(sdk, head, rel)
        if head_blob is None:
            rep.mirror.append(
                FileVerdict(
                    rel, tan_rel, "removed-upstream",
                    "gone from alp-sdk at the target ref. Deleting the tan "
                    "counterpart is a decision with call sites; do it by hand.",
                )
            )
            continue
        if head_blob == base_blob:
            rep.mirror.append(
                FileVerdict(rel, tan_rel, "unchanged", new_hash=pinned)
            )
            continue
        if not tan_file.is_file():
            rep.mirror.append(
                FileVerdict(rel, tan_rel, "missing-locally",
                            f"{tan_rel} does not exist in this checkout.")
            )
            continue
        merged, conflicts = three_way_merge(
            tan_file.read_bytes(), base_blob, head_blob
        )
        if conflicts:
            rep.mirror.append(
                FileVerdict(
                    rel, tan_rel, "conflict",
                    f"{conflicts} conflicting hunk(s): the upstream change "
                    "overlaps a region tan had adapted, so no merge of it is "
                    "correct without reading both. Nothing was written.",
                    diff=git_diff(sdk, mirror_base, head, rel, diff_lines),
                )
            )
            continue
        rep.mirror.append(
            FileVerdict(
                rel, tan_rel, "merged",
                f"upstream delta applied cleanly onto {tan_rel}",
                merged=merged,
                new_hash=_sha(head_blob),
                diff=git_diff(sdk, mirror_base, head, rel, diff_lines),
            )
        )
    rep.mirror_commits = git_log_subjects(sdk, mirror_base, head, [MIRROR_DIR])

    # --- hand-port half ----------------------------------------------------
    # Never merged. `alp_project_loader.py` alone feeds TWO tan modules
    # (`project_loader.py` and `som_metadata.py`), which is the shape argument
    # against a merge in one line: there is no single `ours` to merge into.
    reverse: dict[str, list[str]] = {}
    for tan_name, src in gate.hand_port_sources.items():
        reverse.setdefault(src, []).append(tan_name)
    for rel, pinned in gate.hand_port_hashes.items():
        base_blob = git_show(sdk, hp_base, rel)
        if base_blob is None or _sha(base_blob) != pinned:
            got = "absent" if base_blob is None else _sha(base_blob)
            raise Refused(
                f"{rel}: HAND_PORT_HASHES says {pinned} but the blob at "
                f"HAND_PORT_PINNED_SDK_COMMIT ({hp_base}) is {got}. Pin and "
                "table disagree; refusing to propose anything."
            )
        head_blob = git_show(sdk, head, rel)
        targets = ", ".join(
            f"{PLANNER_REL}/{n}" for n in sorted(reverse.get(rel, []))
        ) or "(no tan module names this source)"
        if head_blob is None:
            rep.hand_port.append(
                FileVerdict(rel, targets, "removed-upstream",
                            "gone from alp-sdk at the target ref.")
            )
        elif head_blob == base_blob:
            rep.hand_port.append(
                FileVerdict(rel, targets, "unchanged", new_hash=pinned)
            )
        else:
            rep.hand_port.append(
                FileVerdict(
                    rel, targets, "hand-port-changed",
                    "HAND-PORTED, not mirrored -- the tan counterpart is "
                    "restructured/renamed/split, so no automatic merge of this "
                    "is correct. Port it by hand into the module(s) named, "
                    "then re-pin HAND_PORT_HASHES.",
                    new_hash=_sha(head_blob),
                    diff=git_diff(sdk, hp_base, head, rel, diff_lines),
                )
            )
    rep.hand_port_commits = git_log_subjects(
        sdk, hp_base, head, sorted(gate.hand_port_hashes)
    )

    # --- strict_loaders (third pin, detected, never moved) -----------------
    rel = "scripts/strict_loaders.py"
    base_blob = git_show(sdk, st_base, rel)
    if base_blob is None or _sha(base_blob) != gate.strict_loaders_hash:
        got = "absent" if base_blob is None else _sha(base_blob)
        raise Refused(
            f"{rel}: STRICT_LOADERS_HASH says {gate.strict_loaders_hash} but "
            f"the blob at STRICT_LOADERS_PINNED_SDK_COMMIT ({st_base}) is "
            f"{got}. Pin and hash disagree; refusing to propose anything."
        )
    head_blob = git_show(sdk, head, rel)
    if head_blob is None:
        rep.strict.append(
            FileVerdict(rel, f"{PLANNER_REL}/strict_loaders.py",
                        "removed-upstream", "gone from alp-sdk at the target ref.")
        )
    elif head_blob == base_blob:
        rep.strict.append(
            FileVerdict(rel, f"{PLANNER_REL}/strict_loaders.py", "unchanged",
                        new_hash=gate.strict_loaders_hash)
        )
    else:
        rep.strict.append(
            FileVerdict(
                rel, f"{PLANNER_REL}/strict_loaders.py", "hand-port-changed",
                "hand-ported AND carrying its own pin, which this tool never "
                "advances (see the module docstring: that pin names the "
                "introducing commit and its block records a known open gap). "
                "Port by hand and move STRICT_LOADERS_HASH deliberately.",
                new_hash=_sha(head_blob),
                diff=git_diff(sdk, st_base, head, rel, diff_lines),
            )
        )
    return rep


# --------------------------------------------------------------------------
# Rendering + applying
# --------------------------------------------------------------------------

_HEADLINE = {
    "up-to-date": "UP TO DATE -- `tan/planner/` is level with the bound alp-sdk; nothing to propose.",
    "clean": "RE-SYNC APPLIED -- every tracked file resolved; the pin(s) below moved. A human still reads the diff: a clean merge can carry behavioural change.",
    # tan-cli#1109: the pin(s) covering the unresolved half staying put does
    # NOT mean `test_planner_relocation_freshness.py` itself goes red -- it
    # doesn't: `planner-resync.yml`'s "Run the freshness gate" step binds
    # each pin's checkout to a worktree pinned at that SAME unmoved commit,
    # so the gate compares the pin to itself and passes by construction
    # (measured during #1103's review). What stays red is this WORKFLOW's
    # own run: a blocked pin means the next scheduled/dispatched run
    # recomputes the same "needs a human" verdict and exits 1 again -- so the
    # reminder persists (merging a partial proposal does not silence it,
    # since a partial re-sync never moves the blocked pin either).
    "partial": "NEEDS A HUMAN -- part of this re-sync could not be applied. The pin(s) covering it did NOT move -- not because the freshness gate goes red (it doesn't: it re-hashes against a worktree pinned at that same unmoved commit, so it trivially passes), but because THIS workflow keeps re-running and exiting 1 on every scheduled/dispatched run until someone ports the work, which merging this proposal does not silence.",
}


def render_markdown(rep: Report, applied: bool) -> str:
    out: list[str] = []
    out.append(
        f"## Planner re-sync: alp-sdk `{rep.mirror_base[:8]}` -> `{rep.sdk_head[:8]}`"
    )
    out.append("")
    out.append(f"**{_HEADLINE[rep.verdict]}**")
    out.append("")
    if rep.verdict != "up-to-date":
        out.append(
            "Proposed by `python/scripts/planner_resync.py` "
            "(`.github/workflows/planner-resync.yml`). "
            "**Do not merge without reading the diff** -- a re-sync can carry "
            "behavioural change, and the audit narrative above each moved pin "
            "is a machine-written placeholder that a reviewer must replace."
        )
        out.append("")

    def table(rows: list[FileVerdict], title: str, empty: str) -> None:
        out.append(f"### {title}")
        out.append("")
        if not rows:
            out.append(empty)
            out.append("")
            return
        out.append("| alp-sdk source | tan target | status |")
        out.append("| --- | --- | --- |")
        for v in rows:
            out.append(f"| `{v.path}` | `{v.target or '--'}` | **{v.status}** |")
        out.append("")
        for v in rows:
            if v.detail and v.status != "unchanged":
                out.append(f"- `{v.path}` -- {v.detail}")
        out.append("")

    changed_mirror = [v for v in rep.mirror if v.status != "unchanged"]
    table(
        changed_mirror,
        "Mirror (`scripts/alp_orchestrate/` -> `tan/planner/`), 3-way merged",
        "No mirrored module moved upstream in this range.",
    )
    changed_hp = [v for v in rep.hand_port + rep.strict if v.status != "unchanged"]
    table(
        changed_hp,
        "Hand-ports -- FLAGGED ONLY, never copied",
        "No hand-port source moved upstream in this range.",
    )

    out.append("### Pins")
    out.append("")
    out.append("| pin | from | to | moved by this change |")
    out.append("| --- | --- | --- | --- |")
    out.append(
        f"| `PINNED_SDK_COMMIT` | `{rep.mirror_base[:8]}` | `{rep.sdk_head[:8]}` | "
        f"{'yes' if rep.mirror_moves and applied else 'no'} |"
    )
    out.append(
        f"| `HAND_PORT_PINNED_SDK_COMMIT` | `{rep.hand_port_base[:8]}` | "
        f"`{rep.sdk_head[:8]}` | {'yes' if rep.hand_port_moves and applied else 'no'} |"
    )
    out.append(
        f"| `STRICT_LOADERS_PINNED_SDK_COMMIT` | `{rep.strict_base[:8]}` | -- | "
        "no (never automatic -- see `planner_resync.py`'s docstring) |"
    )
    out.append("")

    if rep.blocked_mirror or rep.blocked_hand_port:
        out.append("### What a human must do")
        out.append("")
        for v in rep.blocked_mirror + rep.blocked_hand_port:
            out.append(f"1. `{v.path}` (**{v.status}**) -- {v.detail}")
        out.append("")

    if rep.mirror_commits or rep.hand_port_commits:
        out.append("### Upstream commits in range")
        out.append("")
        for c in dict.fromkeys(rep.mirror_commits + rep.hand_port_commits):
            out.append(f"- {c}")
        out.append("")

    diffs = [v for v in rep.mirror + rep.hand_port + rep.strict if v.diff]
    if diffs:
        out.append("### Upstream diffs")
        out.append("")
        for v in diffs:
            out.append(f"<details><summary><code>{v.path}</code> "
                       f"({v.status})</summary>")
            out.append("")
            out.append("```diff")
            out.append(v.diff)
            out.append("```")
            out.append("")
            out.append("</details>")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def apply(repo: pathlib.Path, gate: Gate, rep: Report) -> list[str]:
    """Write the merged files and move whichever pins earned it."""
    touched: list[str] = []
    for v in rep.mirror:
        if v.status == "merged" and v.merged is not None and v.target:
            (repo / v.target).write_bytes(v.merged)
            touched.append(v.target)

    text = (repo / GATE_REL).read_text(encoding="utf-8")
    if rep.mirror_moves:
        new_hashes = {
            name: (
                next(v.new_hash for v in rep.mirror if v.path.endswith(f"/{name}"))
                or gate.pinned_hashes[name]
            )
            for name in gate.pinned_hashes
        }
        text = rewrite_hash_table(text, "PINNED_HASHES", new_hashes)
        text = rewrite_pin(text, "PINNED_SDK_COMMIT", rep.sdk_head)
        text = insert_note_above_pin(
            text,
            "PINNED_SDK_COMMIT",
            audit_note(
                "mirror / PINNED_HASHES",
                rep.mirror_base,
                rep.sdk_head,
                rep.mirror_commits,
                [
                    f"{v.path}: {v.status}"
                    for v in rep.mirror
                    if v.status != "unchanged"
                ],
            ),
        )
    if rep.hand_port_moves:
        text = rewrite_pin(text, "HAND_PORT_PINNED_SDK_COMMIT", rep.sdk_head)
        text = insert_note_above_pin(
            text,
            "HAND_PORT_PINNED_SDK_COMMIT",
            audit_note(
                "hand-port / HAND_PORT_HASHES",
                rep.hand_port_base,
                rep.sdk_head,
                rep.hand_port_commits,
                ["no hand-port source moved in this range; hashes unchanged"],
            ),
        )
    if rep.mirror_moves or rep.hand_port_moves:
        (repo / GATE_REL).write_text(text, encoding="utf-8")
        touched.append(GATE_REL)
    return touched


def up_to_date_reason(rep: Report) -> str:
    """The explicit "why" for an `up-to-date` verdict -- tan-cli#1109 fault 1.

    `render_markdown`'s headline already says "UP TO DATE" in the job
    summary, but the step that decides whether to push anything used to log
    only a generic "Nothing to propose" with no reason attached -- legible
    only by clicking into the summary. This is printed straight to the run
    log (stderr, so it survives even when `--markdown`/`--json` redirect the
    report elsewhere) so a silent run reads as a measurement, not an absence
    of one.

    Names BOTH bases (tan-cli#1109 review): the mirror half and the
    hand-port half are audited from two DIFFERENT pins
    (`PINNED_SDK_COMMIT` / `HAND_PORT_PINNED_SDK_COMMIT`) that drift at
    different rates by design (`Report.hand_port_moves`'s own docstring,
    tan-cli#296) -- attributing "nothing changed" to a single base would
    misreport the hand-port half whenever the two pins differ.
    """
    return (
        f"planner_resync: up to date -- no file under {MIRROR_DIR}/ changed "
        f"between the pinned mirror audit {rep.mirror_base[:8]} and the "
        f"target {rep.sdk_head[:8]}, and no tracked hand-port source changed "
        f"between the pinned hand-port audit {rep.hand_port_base[:8]} and "
        "that same target; nothing to propose."
    )


def to_json(rep: Report, applied: bool, touched: list[str]) -> dict:
    return {
        "verdict": rep.verdict,
        "applied": applied,
        "sdkHead": rep.sdk_head,
        "pins": {
            "PINNED_SDK_COMMIT": {
                "from": rep.mirror_base,
                "to": rep.sdk_head if (rep.mirror_moves and applied) else rep.mirror_base,
                "moved": rep.mirror_moves and applied,
            },
            "HAND_PORT_PINNED_SDK_COMMIT": {
                "from": rep.hand_port_base,
                "to": rep.sdk_head if (rep.hand_port_moves and applied) else rep.hand_port_base,
                "moved": rep.hand_port_moves and applied,
            },
            "STRICT_LOADERS_PINNED_SDK_COMMIT": {
                "from": rep.strict_base,
                "to": rep.strict_base,
                "moved": False,
            },
        },
        "files": [
            {"path": v.path, "target": v.target, "status": v.status,
             "newHash": v.new_hash}
            for v in rep.mirror + rep.hand_port + rep.strict
        ],
        "touched": touched,
        "blocked": [
            {"path": v.path, "status": v.status, "detail": v.detail}
            for v in rep.blocked_mirror + rep.blocked_hand_port
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Propose the tan/planner re-sync an alp-sdk change owes."
    )
    ap.add_argument("--sdk-root", required=True, type=pathlib.Path,
                    help="an alp-sdk git checkout with full history")
    ap.add_argument("--to", default="origin/dev",
                    help="the alp-sdk ref to re-sync TO (default origin/dev)")
    ap.add_argument("--repo-root", type=pathlib.Path,
                    default=pathlib.Path(__file__).resolve().parents[2])
    ap.add_argument("--apply", action="store_true",
                    help="write the merged files and move the earned pins "
                         "(default is a dry run that writes nothing)")
    ap.add_argument("--markdown", type=pathlib.Path,
                    help="write the report markdown here")
    ap.add_argument("--json", type=pathlib.Path,
                    help="write the machine-readable report here")
    ap.add_argument("--diff-lines", type=int, default=400,
                    help="truncate each attached upstream diff at N lines")
    args = ap.parse_args(argv)

    sdk = args.sdk_root.resolve()
    repo = args.repo_root.resolve()
    try:
        if not (sdk / "scripts" / "alp_project.py").is_file():
            raise Refused(
                f"{sdk} has no scripts/alp_project.py -- that file is tan's "
                "canonical alp-sdk-root marker, so this is not an alp-sdk "
                "checkout."
            )
        gate_path = repo / GATE_REL
        if not gate_path.is_file():
            raise Refused(f"{gate_path} not found; --repo-root is wrong.")
        gate = parse_gate(gate_path.read_text(encoding="utf-8"))
        head = git_resolve(sdk, args.to)
        rep = classify(sdk, repo, gate, head, diff_lines=args.diff_lines)
        touched = apply(repo, gate, rep) if args.apply else []
    except Refused as exc:
        sys.stderr.write(f"planner_resync: REFUSED: {exc}\n")
        if args.markdown:
            args.markdown.write_text(
                "## Planner re-sync REFUSED\n\n"
                "This tool refused to propose anything, which is a real "
                "finding, not a no-op:\n\n"
                f"> {exc}\n",
                encoding="utf-8",
            )
        return 2

    if rep.verdict == "up-to-date":
        sys.stderr.write(up_to_date_reason(rep) + "\n")

    md = render_markdown(rep, args.apply)
    if args.markdown:
        args.markdown.write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    if args.json:
        args.json.write_text(
            json.dumps(to_json(rep, args.apply, touched), indent=2) + "\n",
            encoding="utf-8",
        )
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"verdict={rep.verdict}\n")
            fh.write(f"sdk_head={rep.sdk_head}\n")
            fh.write(f"touched={len(touched)}\n")
    return 0 if rep.verdict in ("up-to-date", "clean") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
