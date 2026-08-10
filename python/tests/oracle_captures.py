# SPDX-License-Identifier: Apache-2.0
"""The frozen Rust-oracle captures, after the oracle itself was retired
(tan-cli#269).

`crates/` is deleted. The binary those captures were taken from cannot be
rebuilt from this repository, and the parity suite that used to replay them
against a live port is gone with it. What survives is the store itself:
committed answers, every one written by an actual run of `tan 0.4.1` (see
`oracle_captures/PROVENANCE.txt` for the exact binary, the `crates/` commit it
was built from, and the capture host). A capture stays valid after its subject
is gone -- that is what a capture is for.

## Why the store MOVED, and what it is now

It lived at `tests/parity/oracle_fixtures/`, next to the ~13 modules that
replayed it. Those modules are deleted. `tests/parity/` still exists and is
still a real, running gate -- but the ONE comparison left in it is
`test_planner_emit_parity.py`, tan's relocated planner against alp-sdk's
`scripts/alp_orchestrate/`, an axis this store has nothing to do with. Leaving
a Rust-oracle store inside that directory would invite exactly one wrong
reading: that these files feed the planner parity. They do not, and never did.

So the store sits under `tests/fixtures/` with the repo's other committed test
data, and this module is the only thing that knows where -- two facts and a
reader, replacing 349 lines of replay/capture machinery whose live half no
longer has a binary to spawn.

## Read-only, permanently

There is no writer any more. `TAN_PARITY_LIVE` / `TAN_PARITY_CAPTURE` are
gone, and so is `.github/workflows/capture-platform-fixtures.yml`, which
existed solely to `cargo build --bin tan` on three runners and re-capture
`platform_bound.json`. Nothing in this repository can produce a new entry, and
a hand-edited one would be a fabricated oracle answer -- the single failure
mode `tests/gates/test_oracle_fixture_capture_platform_convention.py` was
written to catch (tan-cli#511, a win32 recording quietly rewritten to a POSIX
shape it never had). Treat every byte here as history: readable, citable, not
editable.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: One committed file per capture MODULE, named for the (now deleted) test
#: module whose answers it holds -- small enough to review a diff of, and the
#: name still says which surface each answer came from.
CAPTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "oracle_captures"

#: The OS every capture in this store was taken on -- `PROVENANCE.txt`'s own
#: `platform:` line, mirrored here as data rather than left for each call site
#: to hardcode separately. Still load-bearing with the oracle gone: a recorded
#: envelope can carry that host's native path separators (`buildRoot`,
#: per-slice paths, ...), so any check over those values has to know which
#: platform produced them. `test_oracle_fixture_capture_platform_convention.py`
#: imports this rather than writing `"win32"` a second time, so that gate
#: cannot drift from the constant it is actually checking.
CAPTURE_PLATFORM = "win32"


def load(module: str) -> dict[str, Any]:
    """The committed capture store for one (deleted) test module, by name --
    e.g. ``load("test_run_oracle_parity")``.

    Raises rather than returning ``{}`` for an absent name: every caller here
    cites a specific recorded answer, and a silently empty store would make a
    check over it vacuously green, which is the shape this whole area of the
    tree keeps being bitten by.
    """
    path = CAPTURES_DIR / f"{module}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no frozen oracle capture named {module!r} in {CAPTURES_DIR} -- "
            f"available: {sorted(p.stem for p in CAPTURES_DIR.glob('*.json'))}"
        )
    return json.loads(path.read_text(encoding="utf-8"))
