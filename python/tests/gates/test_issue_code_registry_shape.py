# SPDX-License-Identifier: Apache-2.0
"""Every `contract/issue-codes.json` `code` must be lowercase-kebab:
`^[a-z][a-z0-9-]*\\.[a-z0-9-]+$`.

v0.5.0 shipped 27 camelCase codes (`doctor.boardYaml`, `support-
bundle.gdbserverBackend`, ...) alongside 253 correctly kebab-cased ones --
`doctor_cmd.checks_to_issues`/`support_bundle_cmd._doctor_issues` derived the
issue code straight from `Check.name`, which is camelCase (it doubles as a
JSON data key and a display string, and stays that way on purpose -- see
`Check`'s own docstring). alp-sdk-vscode's `findFrozenCodes`
(`test/tanContract.test.js`) hard-asserts every published `issueCodes[]`
entry against this exact shape regex, so a camelCase code here REDS the
consumer's contract gate outright -- the opposite of the fail-open
`test_frozen_issue_codes.py`'s own module docstring warns about for a
renamed/dropped code, which is a different mechanism (a `===` match on
spelling, not this shape check). That is exactly how this landed: v0.5.0's
27 camelCase codes reded `findFrozenCodes` and blocked the extension's pin
to that release. Nothing caught the drift before it shipped: this is that
catch.

`kebab_check_name` (`tan/commands/doctor_cmd.py`) is now the single place a
check name becomes an issue-code suffix; this gate does not re-derive it, it
only asserts the REGISTRY -- the published contract -- matches the shape the
consumer regex expects, regardless of how a future code gets constructed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: Same resolution `test_frozen_issue_codes.py` uses: `contract/` sits one
#: level above `python/`.
REGISTRY = Path(__file__).resolve().parents[3] / "contract" / "issue-codes.json"

#: Verbatim copy of alp-sdk-vscode's `ISSUE_CODE_SHAPE` -- the actual consumer
#: regex this gate exists to keep the registry ahead of. Duplicated rather
#: than imported: alp-sdk-vscode is a separate repository/language (TypeScript),
#: not a Python dependency this one could import.
ISSUE_CODE_SHAPE = re.compile(r"^[a-z][a-z0-9-]*\.[a-z0-9-]+$")


def _codes() -> list[str]:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    return [entry["code"] for entry in data["issueCodes"]]


def test_every_registered_issue_code_is_lowercase_kebab():
    codes = _codes()
    # Non-vacuity (tan-cli#275's standing lesson): the registry emptying out
    # or losing its `issueCodes` key would otherwise make this pass by
    # checking nothing.
    assert len(codes) > 100, (
        f"only {len(codes)} codes found in {REGISTRY} -- the registry shape "
        f"changed and this gate is now checking almost nothing"
    )
    offenders = [c for c in codes if not ISSUE_CODE_SHAPE.match(c)]
    assert offenders == [], (
        "contract/issue-codes.json carries code(s) that are not lowercase-kebab "
        "(^[a-z][a-z0-9-]*\\.[a-z0-9-]+$) -- alp-sdk-vscode's findFrozenCodes "
        "hard-asserts every issueCodes[] entry against this shape, so a camelCase "
        "code here REDS the consumer's contract gate and blocks the version "
        "pin:\n  " + "\n  ".join(offenders)
    )


def test_the_shape_regex_actually_rejects_camelcase():
    """Non-vacuity, the other direction: a regex that would also PASS the
    broken input is worthless -- this whole gate exists because nothing
    caught 27 camelCase codes shipping in v0.5.0. Drives the exact pre-fix
    offenders (measured against v0.5.0's `contract/issue-codes.json` before
    this branch's rename) through `ISSUE_CODE_SHAPE` directly, with no
    dependency on the registry's current (already-fixed) content, so this
    stays a real tripwire even if every code above is renamed again later."""
    pre_fix_offenders = [
        "doctor.boardYaml",
        "doctor.zephyrSdkAvailableForHost",
        "support-bundle.gdbserverBackend",
    ]
    rejected = [c for c in pre_fix_offenders if not ISSUE_CODE_SHAPE.match(c)]
    assert rejected == pre_fix_offenders, (
        "ISSUE_CODE_SHAPE no longer rejects the camelCase codes v0.5.0 shipped "
        f"-- it would have passed them too: {sorted(set(pre_fix_offenders) - set(rejected))}"
    )
    # And the positive control: today's real (kebab) codes must still pass,
    # so the regex is not simply rejecting everything.
    accepted = ["doctor.board-yaml", "support-bundle.gdbserver-backend", "bootstrap.yocto-host"]
    for code in accepted:
        assert ISSUE_CODE_SHAPE.match(code), f"{code!r} should be accepted -- ISSUE_CODE_SHAPE is now too strict"
