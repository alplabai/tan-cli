#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Cross-check `.github/zizmor.yml`'s INVENTORY against zizmor's own verdict.

`ci.yml`'s `workflow-security` job runs `zizmor --no-ignores --format json`
(every inline `# zizmor: ignore[...]` stripped) and hands this script that
JSON plus `.github/zizmor.yml`'s own text. Two DIFFERENT kinds of drift can
put the INVENTORY out of sync with the tree, and this script exists because
one of them went unnoticed for a whole issue's lifetime.

DRIFT A (tan-cli#899, caught since tan-cli#919): a suppression is added or
removed with no matching edit to INVENTORY. `python/tests/gates/
test_zizmor_inventory_count.py` used to catch this by RE-IMPLEMENTING
zizmor's own inline-ignore-to-step association from YAML indentation --
deleted in tan-cli#919 round 4 after three rounds of closing three evasions
and opening five more, because zizmor associates a suppression with a
finding's LINE RANGE, not indentation, and a second model of "what counts as
a step" can only ever approximate the real one. Replaced with exactly this:
ask zizmor itself (`--no-ignores`), and compare its verdict's COUNT to
INVENTORY's row count. This script's `found != inventoried` branch is that
check, unchanged in spirit.

DRIFT B (tan-cli#929, this file's reason for existing): Drift A's count
compare cannot see a step get RENAMED. Two sites that swapped names still
count the same, so `.github/zizmor.yml` can go on naming "the `Check out
tan-cli dev` step" long after that step is called something else, and
nothing goes red -- measured directly in tan-cli#929: renaming the step to
`Fetch the resync target` left the INVENTORY prose stale and the gate green
at rc=0. A count is not an identity.

The fix is tan-cli#929's own suggestion, modelled on `parity.yml`'s
`notify-planner-drift` job matching a hidden `<!-- tan-cli-planner-drift-
tracker -->` body marker instead of a PR title specifically so a rename
cannot break the lookup (`parity.yml:2417-2427`): each of the two inventoried
steps now carries a stable `id:` (`artipacked-inventory-1` /
`artipacked-inventory-2`) that is NEVER part of a step's human-facing
`name:` and has no reason to change when `name:` does. `.github/zizmor.yml`'s
INVENTORY rows are keyed to `(job key, id)` PAIRS -- via a fixed-shape
`anchor: job=<job> id=<id>` trailer line per row -- rather than to prose.
Renaming a step's DISPLAY name no longer touches its identity; renaming or
dropping its `id:` -- the thing a row actually depends on now -- surfaces
immediately as an anchor zizmor's real findings no longer contain.

This still asks zizmor for the verdict, not a re-implementation of it: the
`(job, id)` pairs are read straight out of zizmor's own JSON (the `route`
gives the job key, the captured `feature` text gives the `id:` line, because
it sits inside the same step body zizmor already sliced out for the
finding). Nothing here re-derives which lines belong to which step --
that is still entirely zizmor's own span logic, exactly the tan-cli#919
round-4 lesson this must not re-litigate.

Usage:
    check_zizmor_inventory.py <no-ignores.json> <zizmor.yml> <rc>

`<rc>` is zizmor's own exit code from the `--no-ignores` run, used only to
make the "the JSON never parsed" error message name what actually happened;
control flow below never branches on it directly (a `--no-ignores` run over
a tree with real suppressions to strip is EXPECTED to exit non-zero -- that
is the success path for the probe, not a failure of it).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# The row's OWN indent (`#     N. `), not `^#\s+\d+\.\s` -- the looser form
# false-positives on a legitimate numbered sub-list nested under a row
# (measured, pre-tan-cli#929: `[1, 2, 1, 2] != [1, 2, 3, 4]` on a tree with
# one). Carried over unchanged from the Drift-A check this replaces.
_ROW_RE = re.compile(r"^#     (\d+)\. ")

# The anchor trailer line a row carries once it names a site:
#   #        anchor: job=propose id=artipacked-inventory-1
# Indented one level deeper than the row's own `#     N. ` marker, matching
# every other continuation line already in this file's prose.
_ANCHOR_RE = re.compile(r"^#        anchor: job=(\S+) id=(\S+)\s*$")

# zizmor's captured `feature` text for a step includes every key literally
# written in that step's YAML block (route + span, not re-derived) -- `id:`
# among them when the step has one. Anchored to start-of-line so a `with:`
# value or comment that happens to contain the substring "id:" is not
# mistaken for the step's own key.
_STEP_ID_RE = re.compile(r"^\s*id:\s*(\S+)\s*$", re.MULTILINE)


def parse_inventory_rows(zizmor_yml_text: str) -> dict[int, tuple[str, str] | None]:
    """Return {row number: (job, id) or None} for the INVENTORY section.

    `None` means the row has no `anchor:` trailer at all -- a row this old
    (pre-tan-cli#929) or hand-added without one. Reported as a distinct
    error from a MISMATCHED anchor so a reviewer sees "this row was never
    keyed" rather than a confusing pairing failure against nothing.
    """
    if "INVENTORY --" not in zizmor_yml_text:
        raise LookupError(
            "no `INVENTORY --` heading -- nothing to compare zizmor's own "
            "artipacked count against"
        )
    after = zizmor_yml_text.split("INVENTORY --", 1)[1]
    if "DRIFT THIS INVENTORY" not in after:
        raise LookupError(
            "the `INVENTORY --` heading has no matching `DRIFT THIS "
            "INVENTORY` end marker -- without it this would count rows out "
            "of the rest of the file, not just the INVENTORY section"
        )
    section = after.split("DRIFT THIS INVENTORY", 1)[0]

    rows: dict[int, tuple[str, str] | None] = {}
    current: int | None = None
    for line in section.splitlines():
        row_match = _ROW_RE.match(line)
        if row_match:
            current = int(row_match.group(1))
            rows[current] = None
            continue
        anchor_match = _ANCHOR_RE.match(line)
        if anchor_match and current is not None:
            rows[current] = (anchor_match.group(1), anchor_match.group(2))
    return rows


def _job_key(route: list[dict]) -> str | None:
    """Pull the job id out of a finding's `symbolic.route.route` list.

    The route is `[{"Key": "jobs"}, {"Key": <job id>}, {"Key": "steps"},
    {"Index": N}]` -- the job id is the `Key` immediately after `"jobs"`.
    """
    for i, segment in enumerate(route):
        if segment.get("Key") == "jobs" and i + 1 < len(route):
            nxt = route[i + 1]
            if "Key" in nxt:
                return nxt["Key"]
    return None


def parse_artipacked_sites(findings: list[dict]) -> list[tuple[str, str, str | None]]:
    """Return (file, job, step id-or-None) for every `artipacked` finding.

    `step id-or-None` is `None` when the step carries no `id:` at all --
    a site with no stable anchor to key an INVENTORY row to yet.
    """
    sites: list[tuple[str, str, str | None]] = []
    for finding in findings:
        if finding.get("ident") != "artipacked":
            continue
        for loc in finding.get("locations", []):
            symbolic = loc.get("symbolic", {})
            if symbolic.get("kind") != "Primary":
                continue
            path = symbolic.get("key", {}).get("Local", {}).get("verbatim_path", "?")
            job = _job_key(symbolic.get("route", {}).get("route", [])) or "?"
            feature = loc.get("concrete", {}).get("feature", "")
            id_match = _STEP_ID_RE.search(feature)
            sites.append((path, job, id_match.group(1) if id_match else None))
    return sites


def check(no_ignores_json_path: Path, zizmor_yml_path: Path, rc: str) -> int:
    try:
        with open(no_ignores_json_path, encoding="utf-8") as f:
            findings = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"::error::zizmor --no-ignores exited {rc} without producing "
            f"parseable JSON ({exc}) -- this probe cannot run, see the log "
            "above"
        )
        return 1

    sites = parse_artipacked_sites(findings)
    found_pairs = {(job, step_id) for _, job, step_id in sites if step_id is not None}
    unanchored = [(path, job) for path, job, step_id in sites if step_id is None]

    zizmor_yml_text = zizmor_yml_path.read_text(encoding="utf-8")
    try:
        rows = parse_inventory_rows(zizmor_yml_text)
    except LookupError as exc:
        print(f"::error::{zizmor_yml_path}: {exc}")
        return 1

    numbers = sorted(rows)
    if numbers != list(range(1, len(numbers) + 1)):
        print(
            f"::error::{zizmor_yml_path}'s INVENTORY rows are numbered "
            f"{numbers}, not a clean 1..{len(numbers)} sequence -- a "
            "duplicated or skipped number usually means a row was edited "
            "in place instead of renumbered"
        )
        return 1

    unanchored_rows = [n for n, anchor in rows.items() if anchor is None]
    if unanchored_rows:
        print(
            f"::error::{zizmor_yml_path}'s INVENTORY row(s) "
            f"{sorted(unanchored_rows)} carry no `anchor: job=... id=...` "
            "trailer -- tan-cli#929 keys every row to a (job, step id) pair, "
            "not to prose; add the trailer naming the step's `id:`"
        )
        return 1

    declared_pairs = {anchor for anchor in rows.values() if anchor is not None}

    if unanchored:
        sites_desc = ", ".join(f"{path} ({job} job)" for path, job in unanchored)
        print(
            f"::error::zizmor --no-ignores reports (an) `artipacked` "
            f"finding(s) with no `id:` on the step: {sites_desc}. Every "
            "inventoried site needs a stable `id:` (tan-cli#929) -- add one "
            "and an `anchor:` row to `.github/zizmor.yml`'s INVENTORY."
        )
        return 1

    if found_pairs != declared_pairs:
        missing = declared_pairs - found_pairs
        extra = found_pairs - declared_pairs
        detail = []
        if missing:
            detail.append(
                "INVENTORY names (job, id) pair(s) zizmor no longer reports "
                f"as artipacked: {sorted(missing)} -- the step's `id:` was "
                "renamed or removed, or the suppression itself is gone "
                "(tan-cli#929's Drift B)"
            )
        if extra:
            detail.append(
                "zizmor reports (job, id) pair(s) with no matching "
                f"INVENTORY row: {sorted(extra)} -- a suppression was added "
                "without an INVENTORY row (tan-cli#899's Drift A)"
            )
        print("::error::" + " | ".join(detail))
        return 1

    print(
        f"{len(found_pairs)} inline artipacked suppression(s), matching "
        f"{zizmor_yml_path}'s INVENTORY of {len(declared_pairs)}: "
        f"{sorted(found_pairs)}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print(
            "usage: check_zizmor_inventory.py <no-ignores.json> "
            "<zizmor.yml> <rc>",
            file=sys.stderr,
        )
        return 2
    no_ignores_json, zizmor_yml, rc = args
    return check(Path(no_ignores_json), Path(zizmor_yml), rc)


if __name__ == "__main__":
    raise SystemExit(main())
