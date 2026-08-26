# SPDX-License-Identifier: Apache-2.0
r"""Gate: the inline `artipacked` suppression count matches `.github/zizmor.yml`'s
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

THE FIRST VERSION OF THIS GATE WAS ITSELF EVADABLE, three ways, all found by
re-implementing zizmor's own comment-matching rather than asking zizmor what
it actually honours (a subprocess call to the real binary was considered and
rejected -- see below):

  1. zizmor associates an inline ignore with a STEP, not with the physical
     `uses:` line -- an ignore comment on `with:` or any other line inside
     the same step still suppresses it. The original anchor required the
     comment on the SAME line as `uses:` and missed this; a comment one line
     down (e.g. under `with:`) escaped uncounted while zizmor still honoured
     it. Fixed by scanning the step's whole YAML span (from its `- ` marker
     to the next line at or below that indentation), not one line.
  2. `# zizmor: ignore[artipacked,ref-confusion]` -- zizmor's multi-rule
     ignore form -- was not matched by a `ignore\[artipacked\]`-exact regex.
     Fixed with `ignore\[[^]]*\bartipacked\b[^]]*\]`.
  3. Only `*.yml` was globbed; zizmor itself scans `*.yaml` too, and a
     `.github/workflows/planted.yaml` ignore escaped the count entirely.
     Fixed by globbing both extensions.

All three were mutation-proved against a real zizmor run before the fix
(each left `zizmor`'s own verdict unchanged -- `rc=0`, ignored count up by
one -- while this gate stayed green) and after (each now fails this gate).

WHY A REGEX OVER THE YAML TEXT AND NOT `zizmor --no-ignores --format json`
counting the real `artipacked` findings it would otherwise honour: that
recipe exists and is more direct (it asks zizmor instead of re-implementing
its matching, which is exactly what produced the three evasions above), but
`zizmor` is a separate binary this suite cannot assume is on PATH -- the
`gates` CI job that runs this file (`ci.yml`'s `python` job, and
`parity.yml`'s `seam1-plan-shape`) installs only the declared Python package
plus `pytest`, deliberately: `tests/gates` is documented there as "pure
source parsing... with no OS-dependent behaviour", run identically on every
platform with no external tool. `workflow-security` (this same `ci.yml`)
already runs the real, authoritative `zizmor` over this exact tree on every
PR; this gate is the second, dependency-free line of defence that keeps
`.github/zizmor.yml`'s hand-written INVENTORY honest against that real run,
not a replacement for it. The span/multi-rule/extension fixes above close
the gap between this regex and zizmor's real behaviour without adding a
subprocess dependency to a suite that has never needed one.

Renaming an inventoried step (rather than changing the COUNT) is a
different drift this gate does not catch -- it would need the inventory in
a machine-parseable form, a bigger lift filed as tan-cli#929 rather than
built here (see `.github/zizmor.yml`'s own header)."""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ZIZMOR_CONFIG = REPO_ROOT / ".github" / "zizmor.yml"

#: A step-scoped list-item start: `  - ` (a step, a matrix entry, any YAML
#: block-sequence item). Used only to find span boundaries -- what closes a
#: span is the next line at or below this indentation, per the standard YAML
#: block-sequence-item rule.
_STEP_START = re.compile(r"^([ \t]*)-\s")

#: A real `uses:` key, anchored to the start of the (optionally dash-led)
#: line -- NOT a bare `\buses:` substring match, which also fires inside an
#: unrelated `run:` string that happens to contain the word (measured: a step
#: with `run: echo "a step writes uses: foo # zizmor: ignore[artipacked]..."`
#: no longer counts, because that line does not start with `uses:` once its
#: own `run:` key is stripped).
_USES_LINE = re.compile(r"^[ \t]*(?:-[ \t]*)?uses:\s", re.MULTILINE)

#: The ignore comment itself. `[^\]]*\bartipacked\b[^\]]*` (not
#: `ignore\[artipacked\]` exactly) matches zizmor's multi-rule form too, e.g.
#: `# zizmor: ignore[artipacked,ref-confusion]`.
_IGNORE_COMMENT = re.compile(r"#\s*zizmor:\s*ignore\[[^\]]*\bartipacked\b[^\]]*\]")

_INVENTORY_ROW = re.compile(r"^#\s+(\d+)\.\s", re.MULTILINE)


def _step_spans(text: str) -> list[str]:
    """Split `text` into the YAML block-sequence-item spans it contains.

    A span runs from a `- ` marker line to (but not including) the next line
    whose indentation is at or below that marker's -- the same rule that
    ends a YAML block-sequence item. Blank lines never end a span. This is
    a text-level approximation of "the step zizmor would associate an inline
    ignore comment with", deliberately generic (any block-sequence item, not
    just `steps:` entries) rather than requiring a full YAML+comment-aware
    parser.
    """
    lines = text.splitlines()
    spans: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        match = _STEP_START.match(lines[i])
        if not match:
            i += 1
            continue
        indent = len(match.group(1))
        span_lines = [lines[i]]
        j = i + 1
        while j < n:
            line = lines[j]
            if line.strip() == "":
                span_lines.append(line)
                j += 1
                continue
            if len(line) - len(line.lstrip(" \t")) <= indent:
                break
            span_lines.append(line)
            j += 1
        spans.append("\n".join(span_lines))
        i = j
    return spans


def _count_inline_artipacked_ignores(text: str) -> int:
    """Count step spans that carry BOTH a real `uses:` line and an inline
    `artipacked` ignore comment anywhere in the span -- not necessarily on
    the same physical line, matching zizmor's own step-scoped association."""
    return sum(1 for span in _step_spans(text) if _USES_LINE.search(span) and _IGNORE_COMMENT.search(span))


def test_inline_artipacked_ignore_count_matches_the_zizmor_yml_inventory():
    actual = sum(
        _count_inline_artipacked_ignores(path.read_text(encoding="utf-8"))
        for pattern in ("*.yml", "*.yaml")
        for path in sorted(WORKFLOWS.glob(pattern))
    )

    header = ZIZMOR_CONFIG.read_text(encoding="utf-8")
    assert "INVENTORY --" in header, (
        f"{ZIZMOR_CONFIG.relative_to(REPO_ROOT)} no longer has an `INVENTORY --` heading -- "
        "this gate has nothing to compare the inline count against."
    )
    after_heading = header.split("INVENTORY --", 1)[1]
    assert "DRIFT THIS INVENTORY" in after_heading, (
        f"{ZIZMOR_CONFIG.relative_to(REPO_ROOT)}'s `INVENTORY --` heading has no matching "
        "`DRIFT THIS INVENTORY` end marker any more -- without it this gate would silently "
        "count numbered rows out of the rest of the file, not just the INVENTORY section."
    )
    inventory_section = after_heading.split("DRIFT THIS INVENTORY", 1)[0]

    numbers = [int(n) for n in _INVENTORY_ROW.findall(inventory_section)]
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"{ZIZMOR_CONFIG.relative_to(REPO_ROOT)}'s INVENTORY rows are numbered {numbers}, not "
        f"a clean 1..{len(numbers)} sequence -- a duplicated or skipped number usually means a "
        "row was edited in place instead of renumbered, which can make two distinct sites count "
        "as one row or one site count as two."
    )
    inventoried = len(numbers)

    assert actual == inventoried, (
        f"{actual} inline `# zizmor: ignore[artipacked]` comment(s) found under "
        f"{WORKFLOWS.relative_to(REPO_ROOT)}/ (*.yml and *.yaml), but "
        f"{ZIZMOR_CONFIG.relative_to(REPO_ROOT)}'s INVENTORY lists {inventoried}. A suppression "
        "was added or removed without updating the header's hand-maintained count and numbered "
        "rows (tan-cli#899 review, Drift A)."
    )
