# SPDX-License-Identifier: Apache-2.0
"""Gate: the inline `artipacked` suppression count matches `.github/zizmor.yml`'s
own INVENTORY (tan-cli#899 review, Drift A).

Moving both `artipacked` ignores from `<file>:<line>` config pins to inline
`# zizmor: ignore[artipacked]` comments (tan-cli#899) fixed the loud rot
(a moved pin makes the finding come back, gate goes red) but opened a QUIET
one: a THIRD inline ignore added anywhere under `.github/workflows/` needs
no edit to `.github/zizmor.yml` at all. Measured: planting a third ignore on
a duplicated `release.yml` checkout leaves `zizmor --min-severity medium
--no-online-audits .github/workflows/` at `rc=0, 15 ignored, 54 suppressed`
-- unchanged, nothing names the new suppression escaping the reviewed
baseline the header's INVENTORY claims to be complete.

This does not need the inventory rewritten in a parseable form to catch
that: counting is enough. It counts inline `artipacked` ignores under
`.github/workflows/` and compares that count against the number of
numbered rows under `.github/zizmor.yml`'s own `INVENTORY --` heading.
Deliberately narrow to `artipacked` -- that is the INVENTORY's own stated
scope; `release.yml:945`'s `cache-poisoning` ignore is a different rule
this gate says nothing about.

Renaming an inventoried step (rather than changing the COUNT) is a
different drift this gate does not catch -- it would need the inventory in
a machine-parseable form, a bigger lift filed as a follow-up rather than
built here (see `.github/zizmor.yml`'s own header)."""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ZIZMOR_CONFIG = REPO_ROOT / ".github" / "zizmor.yml"

#: A REAL suppression, not prose about one: the ignore comment must share a
#: `uses:` line with the step it excuses. A bare substring match would also
#: count a workflow's own explanatory comment ABOUT the pattern (this repo
#: has one, in ci.yml) as a third suppression -- caught by this gate's own
#: mutation proof before the anchor was added.
_INLINE_ARTIPACKED_IGNORE = re.compile(r"^.*\buses:.*#\s*zizmor:\s*ignore\[artipacked\]", re.MULTILINE)
_INVENTORY_ROW = re.compile(r"^#\s+\d+\.\s", re.MULTILINE)


def test_inline_artipacked_ignore_count_matches_the_zizmor_yml_inventory():
    actual = sum(
        len(_INLINE_ARTIPACKED_IGNORE.findall(path.read_text(encoding="utf-8")))
        for path in sorted(WORKFLOWS.glob("*.yml"))
    )
    header = ZIZMOR_CONFIG.read_text(encoding="utf-8")
    inventory_section = header.split("INVENTORY --", 1)[1].split("DRIFT THIS INVENTORY", 1)[0]
    inventoried = len(_INVENTORY_ROW.findall(inventory_section))

    assert actual == inventoried, (
        f"{actual} inline `# zizmor: ignore[artipacked]` comment(s) found under "
        f"{WORKFLOWS.relative_to(REPO_ROOT)}/, but {ZIZMOR_CONFIG.relative_to(REPO_ROOT)}'s "
        f"INVENTORY lists {inventoried}. A suppression was added or removed without "
        "updating the header's hand-maintained count and numbered rows "
        "(tan-cli#899 review, Drift A)."
    )
