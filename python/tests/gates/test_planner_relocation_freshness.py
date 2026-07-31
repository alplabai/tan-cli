# SPDX-License-Identifier: Apache-2.0
"""Staleness gate: catch `tan/planner/**` falling behind alp-sdk's
`scripts/alp_orchestrate/**` a second time.

`tan/planner/` is alp-sdk's `scripts/alp_orchestrate/` relocated. The first
relocation was cut from a branch 19 commits behind alp-sdk main, so the port
carried an OLDER shape than the SDK actually ships (five modules had moved
upstream in that window: `__init__.py`, `loader.py`, `manifest.py`,
`models.py`, and the then-brand-new `sdk_compat.py`). Every parity number the
port produced before that fix compared tan against its own stale copy of the
oracle, proving self-consistency, not fidelity.

This gate is the guard against that happening silently again. It pins the
SHA-256 of every upstream `scripts/alp_orchestrate/*.py` file this port was
last audited against -- `PINNED_SDK_COMMIT` below names it, and is the only
place that commit is written down. When `ALP_SDK_ROOT` is bound, the gate re-hashes the SAME
files out of the bound checkout; a mismatch means the SDK moved and this port
has NOT been re-audited against the new shape -- the fix is to diff the
changed file(s), port the behavioural delta into `tan/planner/`, and update
`PINNED_HASHES` (and `PINNED_SDK_COMMIT`) to match, the same way this gate's
own history was produced.

This is deliberately a content hash, not a byte-identical port comparison:
`tan/planner/paths.py` and a few import lines legitimately differ from their
alp-sdk counterparts for relocation reasons (a path that became a bound SDK
root, an import that changed shape) and always will -- see each file's own
"RELOCATED" docstring. What must NOT drift unnoticed is the upstream side of
that comparison: if `scripts/alp_orchestrate/loader.py` changes again, this
gate fails even though nothing in `tan` changed, because the fact this port depends on -- "this is what alp-sdk's loader.py did as of
the last audit" -- is no longer true.

Without `ALP_SDK_ROOT` there is no oracle to compare against, so the gate
SKIPS -- visibly, naming the missing env var, never a silent pass.

The root is resolved through `tests.conftest.sdk_root()` AT MODULE IMPORT
TIME, and that is load-bearing, not style. This gate used to read
`os.environ["ALP_SDK_ROOT"]` from inside the test body -- where the autouse
`_scrub_sdk_discovery_env` fixture has already deleted it -- so it skipped on
EVERY run, including runs with the variable exported. It had therefore never
once compared a hash, which is how the fork drifted eleven alp-sdk commits
without a word (tan-cli#275). A gate that cannot fail is not a gate.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from tests.conftest import sdk_root

#: alp-sdk commit this port was last audited against. Bumped from
#: `ef79eab0` (origin/main) by the tan-cli #275 re-sync, which carried
#: alp-sdk #1057 (`jlink_flash_device` -> `flash_args`, without which Flow D
#: can never arm) and #1054/#1025 (refuse an hw_rev absent from the resolved
#: `hw_revisions:` table) into `tan/planner/`.
PINNED_SDK_COMMIT = "0f3cefbec9a8b2467875c57b76451a1f7cf695b9"  # alp-sdk origin/dev

#: sha256 of every `scripts/alp_orchestrate/<name>.py` at PINNED_SDK_COMMIT,
#: for every upstream module that has a same-named relocated counterpart
#: under `tan/planner/` (i.e. the actual drift surface -- files renamed on
#: relocation, like `alp_project_loader.py` -> `som_metadata.py`, have no
#: single upstream file to pin and are out of scope for this hash check).
PINNED_HASHES: dict[str, str] = {
    "__main__.py": "77b98caf27ba425b888a19f8727683bba23e7c24ebb4b6aa1874e5316a291d27",
    "__init__.py": "2393cab9e971145889488a8049df48da7df24ece19b041d5a93c575edb3ceb04",
    "buildplan.py": "1d8d27be880b876b7c0fa386e2f46317377e30899d02164e3fdd189ad55be438",
    "carveout.py": "2fc94f4b39c3357f3d5e68122aa1dadddfd7fcb8653d1c1a2268f0b9dba35145",
    "cli.py": "b2d9e82d62c5dd1668d4d893e148fb66efc50825b465c8f8385f9bf668572419",
    "headers.py": "9a9cc0ca4801b2bdb7a551662e4dddf27c47bb42fad06939c92a8c95b221156b",
    "kconfig.py": "a94d7db046cc1c7e57b6ef97914357bff19ef3908a364057d81f091144b6a176",
    "kconfig_symbols.py": "fe3a3df4aa00db808ce8443548d113b4a97cf600b5fda106d075e8d071243729",
    "libraries.py": "47b823e0fc06cc657a3c3068598b953e342720cf359443651a9996b93be7aaa5",
    "loader.py": "b4db9e97410956f44fce4a1d11e57deb3d9ec66c2acc0491a87554a420bbd483",
    "manifest.py": "930aa9c453fd86b487f66ec84be8f074a53f22a6077b0310390e176fee7918ba",
    "memregion.py": "f3e62050172bb1500e98d0023eda7408a67e1085a70a4acd92f45f08213ebfa3",
    "models.py": "56f6800dd6a382ab6fa65dd82219216fa975fe065ad3e4106d28fb8cbba7f19b",
    "orchestrator.py": "7d8891d4781b6bdf809340d8428d2b374d3f62fdf6730acc93761093d0943d3d",
    "partition.py": "d1783b95948c12b08dd02493c33613f8bc2b133cc893125c7dc33cd21e0681e3",
    "paths.py": "a2d8b74570f88ad223d797d6428a58fc3851dad6bb9a1ae2c2aa109db789bc93",
    "sdk_compat.py": "145868e163de709463496fe17b9a3a1596c37075efb89d5de0dc05ef9172b719",
    "secure.py": "a6a5762fbac2f99fc4356f01b3ffedd15d366af6d2ddde0042223b3da749cbe6",
    "slugs.py": "16295edbb9ded47eba1063f86a52ce20990d7284ef3ad0365c7f952aec4031f9",
    "topology.py": "12f5f62d3adeb9e935594934fd2fc2b1fbeaec6f466d6dd89c329c54e844f3b1",
    "validate.py": "2dbe9dcb36ff0ebe4c968ef120983342aa00f02f32b9166f9c1608d1578495e7",
}


#: Resolved at import time -- see the module docstring on why that matters.
SDK: pathlib.Path | None = sdk_root()


def _sdk_root() -> pathlib.Path:
    if SDK is None:
        pytest.skip(
            "ALP_SDK_ROOT is not set -- no bound alp-sdk checkout to compare "
            "tan/planner/ against, so this staleness gate cannot run. This is "
            "a SKIP about the missing root, not a pass: set ALP_SDK_ROOT to a "
            "real alp-sdk checkout to actually exercise the gate."
        )
    return SDK


def test_relocated_planner_modules_match_the_pinned_sdk_audit():
    orchestrate = _sdk_root() / "scripts" / "alp_orchestrate"
    drifted: list[str] = []
    for name, pinned_hash in PINNED_HASHES.items():
        upstream = orchestrate / name
        if not upstream.is_file():
            drifted.append(f"{name}: gone from the bound SDK checkout")
            continue
        current_hash = hashlib.sha256(upstream.read_bytes()).hexdigest()
        if current_hash != pinned_hash:
            drifted.append(
                f"{name}: sha256 {current_hash} != pinned {pinned_hash}"
            )
    # The hash loop above only sees files it already knows about, so a BRAND-NEW
    # upstream module is invisible to it -- and that is precisely the drift that
    # caused this incident: `sdk_compat.py` arrived upstream and tan simply did
    # not have it. Pin the SET as well as the contents.
    upstream_names = sorted(q.name for q in orchestrate.glob("*.py"))
    if upstream_names != sorted(PINNED_HASHES):
        added = sorted(set(upstream_names) - set(PINNED_HASHES))
        removed = sorted(set(PINNED_HASHES) - set(upstream_names))
        if added:
            drifted.append(
                "NEW upstream module(s) with no counterpart audited here: "
                + ", ".join(added)
            )
        if removed:
            drifted.append(
                "module(s) removed upstream but still pinned here: " + ", ".join(removed)
            )

    assert not drifted, (
        "scripts/alp_orchestrate/ moved past the alp-sdk commit "
        f"({PINNED_SDK_COMMIT}) this port was last audited against. Diff each "
        "file below in the bound alp-sdk checkout, port the behavioural delta "
        "into the matching tan/planner/ module, then update PINNED_HASHES "
        "(and PINNED_SDK_COMMIT) in this file to re-pin the audit:\n  "
        + "\n  ".join(drifted)
    )
