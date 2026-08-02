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

tan-cli#279: PINNED_HASHES only ever looks inside `scripts/alp_orchestrate/`,
so a HAND-PORT of some OTHER alp-sdk file is invisible to it by construction --
`git log <pinned>..<new> -- scripts/alp_orchestrate` comes back empty and
reassuring while such a file drifts. `zephyr_board.py`
(`scripts/gen_zephyr_board.py`) and `project_loader.py`
(`scripts/alp_project_loader.py`) both did, and both shipped real defects
before anything noticed (a missing `CONFIG_USE_DT_CODE_PARTITION=y`, and an
unaudited unknown-`hw_rev` path). `HAND_PORT_HASHES` below closes that for the
ten known hand-ports; `test_every_planner_module_is_tracked_or_declared_exempt`
closes it for the CLASS, by asserting every `.py` file under `tan/planner/`
is named in one of PINNED_HASHES, HAND_PORT_SOURCES, or
EXEMPT_FROM_RELOCATION_TRACKING, so a future hand-port with none of the three
has nowhere to hide.

tan-cli#296: PINNED_HASHES and HAND_PORT_HASHES are two audits of two
different things, pinned at two different alp-sdk commits (PINNED_SDK_COMMIT
and HAND_PORT_PINNED_SDK_COMMIT). For a while `parity.yml` bound a single
alp-sdk checkout and pointed both tests at it, so the hand-port test was
silently measured against PINNED_SDK_COMMIT's tree -- a plan-shape-parity
commit, not the one HAND_PORT_HASHES was actually audited against -- and its
failures reported drift that was really just the wrong oracle. Per
`docs/ROADMAP.md`'s Standing Rule ("a frozen tree is measured at its own
freeze vendor point; a shipped Python surface is measured at PINNED_SDK_TAG"),
two audits with two pins need two checkouts. `test_hand_ported_planner_
modules_match_their_pinned_sdk_source` now reads its own root from
`ALP_SDK_HAND_PORT_ROOT`, never `ALP_SDK_ROOT`/`ALP_SDK_PARITY_ROOT` (which
stay the relocated-planner test's root, pinned at PINNED_SDK_COMMIT).

Like that test, a missing `ALP_SDK_HAND_PORT_ROOT` SKIPS rather than fails --
a run that never bound this root (ci.yml's `python` job, a local `pytest
tests/`, a contributor's checkout) is not set up to do this audit, not
broken, and must not go red for it. tan-cli#308 first made this branch
`pytest.fail` instead, on the reasoning that an invisible skip is how #275
drifted eleven commits unnoticed -- and that took ci.yml's `python` job down
with it, since it runs the whole suite with neither root bound. The
enforcement #308 wanted belongs where the root IS actually bound:
`parity.yml`'s seam1 job, the one place both roots are ever set, asserts BY
NODE ID that neither freshness test skipped there (see that job's
`python/tests/gates` step) -- so a skip in the job meant to run the audit is
still a hard CI failure, and this branch can go back to skipping everywhere
else without reopening #275.
"""

from __future__ import annotations

import hashlib
import os
import pathlib

import pytest

from tests.conftest import sdk_root

#: alp-sdk commit this port was last audited against. Bumped from
#: `0f3cefbe` (origin/dev) by the tan-cli #320 re-sync, which carried
#: alp-sdk #1080 (`CONFIG_DCACHE=n` for a `raw_shmem` cross-core carve-out,
#: not just the Ethos-U inference path) and #1025's status half
#: (`SdkRevisionNotBuildable` -- refuse an hw_rev that EXISTS but is
#: `status: reserved`/`status: tbd`/status-less) into `tan/planner/`, plus
#: #1048's `is_tbd()` case/whitespace normalisation (ported inline at each
#: call site rather than as a shared module -- see loader.py/kconfig.py/
#: slugs.py/carveout.py; a `tan/planner/sentinels.py` counterpart to
#: alp-sdk's new top-level `scripts/sentinels.py` has no clean home in this
#: gate's PINNED_HASHES/HAND_PORT_HASHES taxonomy without also re-auditing
#: the unrelated `zephyr_board.py`/`alp_project_loader.py` drift sitting
#: between HAND_PORT_PINNED_SDK_COMMIT and here).
PINNED_SDK_COMMIT = "f4d87a1f472dffe9fe7dffc247cc55bce71034ba"  # alp-sdk origin/dev

#: sha256 of every `scripts/alp_orchestrate/<name>.py` at PINNED_SDK_COMMIT,
#: for every upstream module that has a same-named relocated counterpart
#: under `tan/planner/` (i.e. the actual drift surface -- files renamed on
#: relocation, like `alp_project_loader.py` -> `som_metadata.py`, have no
#: single upstream file to pin and are out of scope for this hash check).
PINNED_HASHES: dict[str, str] = {
    "__main__.py": "77b98caf27ba425b888a19f8727683bba23e7c24ebb4b6aa1874e5316a291d27",
    "__init__.py": "739a0288487997d6f1be7dc1f47fcf05b34a16386c0c81e8fe4eaadcfb84e3f0",
    "buildplan.py": "1d8d27be880b876b7c0fa386e2f46317377e30899d02164e3fdd189ad55be438",
    "carveout.py": "1c62a7e8b9ac6fd4526e2e5bc5e6234c9d4ca5f0a2d019cc85f00392fff91198",
    "cli.py": "b2d9e82d62c5dd1668d4d893e148fb66efc50825b465c8f8385f9bf668572419",
    "headers.py": "9a9cc0ca4801b2bdb7a551662e4dddf27c47bb42fad06939c92a8c95b221156b",
    "kconfig.py": "2839a56f86a8ec831bbcdd7836a36a36865b137865c15003671b4ccb3fef3ad9",
    "kconfig_symbols.py": "fe3a3df4aa00db808ce8443548d113b4a97cf600b5fda106d075e8d071243729",
    "libraries.py": "47b823e0fc06cc657a3c3068598b953e342720cf359443651a9996b93be7aaa5",
    "loader.py": "61eb397822a75ec1a5d662ce888488d0f388bd692e2f26b5f88f305a8e5e9c97",
    "manifest.py": "930aa9c453fd86b487f66ec84be8f074a53f22a6077b0310390e176fee7918ba",
    "memregion.py": "f3e62050172bb1500e98d0023eda7408a67e1085a70a4acd92f45f08213ebfa3",
    "models.py": "6d33258874d6f732d66668d30c22fd644e02de2fb9f35e7b497ddd2d81164109",
    "orchestrator.py": "7d8891d4781b6bdf809340d8428d2b374d3f62fdf6730acc93761093d0943d3d",
    "partition.py": "97e7646ab7f8692919d1cd529d42be453d7fc3240416f98e202a5920cb6cb029",
    "paths.py": "a2d8b74570f88ad223d797d6428a58fc3851dad6bb9a1ae2c2aa109db789bc93",
    "sdk_compat.py": "ae8e5244877d3e9ad35f89e6b7782861f810825321052ed9f5770b9762c0281a",
    "secure.py": "a6a5762fbac2f99fc4356f01b3ffedd15d366af6d2ddde0042223b3da749cbe6",
    "slugs.py": "0cc4c7608a751aa65a8eb1cc2c4c50d868e908ff5d2e11657857e66be10f1b23",
    "topology.py": "12f5f62d3adeb9e935594934fd2fc2b1fbeaec6f466d6dd89c329c54e844f3b1",
    "validate.py": "2dbe9dcb36ff0ebe4c968ef120983342aa00f02f32b9166f9c1608d1578495e7",
}

#: alp-sdk commit the HAND-PORTED `tan/planner/` modules below (everything
#: pinned in HAND_PORT_HASHES) were last audited against -- the commit
#: `v0.15.0-rc1` names, not the tag itself, so a tag that is later moved
#: cannot silently repoint what this pins. `zephyr_board.py` and
#: `project_loader.py` were re-vendored to this point in 947f3d0 / 52fdd01.
HAND_PORT_PINNED_SDK_COMMIT = "996937ac4b452260628cf2c6adad4be31483a3d4"  # alp-sdk v0.15.0-rc1

#: sha256 of every alp-sdk source file a `tan/planner/**` module was
#: hand-ported from OUTSIDE `scripts/alp_orchestrate/`, keyed by its
#: alp-sdk-relative path -- NOT by filename: `project_loader.py` and
#: `som_metadata.py` are both ported out of the SAME
#: `scripts/alp_project_loader.py`, so a filename-keyed table (like
#: PINNED_HASHES above, where the relocation is 1:1) cannot hold this.
HAND_PORT_HASHES: dict[str, str] = {
    "scripts/gen_zephyr_board.py": "2fb2a991431494c41e0d9eac5541100d6159663ad525ac492819defbffa05467",
    "scripts/alp_project_loader.py": "7ae0c9aa135d1f39b6a2904f1b5b5ae12febdc13795b8bbe9a7580c5edd34ff7",
    "scripts/alp_template.py": "45efe843937faf44d31e6e6e382d4b793b76da3b6cc01d5a5c8edb45084aadc0",
    "scripts/alp_project_emit/__init__.py": "62c4742bc373e7fafcd8aa864ad7692d3c05b610c6d7457023aeb82c98847d88",
    "scripts/alp_project_emit/bom_netlist.py": "d2ccef0b4453aede2119cf9af1de7c1f97f2780f7cf1ec7e9b717aafaa8e32f8",
    "scripts/alp_project_emit/dts.py": "cb6d4278e2fc886a23c28f2ef30b4ae9714738071219f7c29cbccbbeb1bc1782",
    "scripts/alp_project_emit/hw_info.py": "e1ff1f64e16a275758bec27550d1811b3cc8d3ac73d32325e63f9402ec83715c",
    "scripts/alp_project_emit/native_sim.py": "24943e7099d745b254b853135ff0b4ae8415be7946d93170d479b637105f18c0",
    "scripts/alp_project_emit/west_libs.py": "0bfad8fb6c22b955d0554f8fffca8c1c9bf9f73d3c64778b9ba2de76eb6a972d",
}

#: `tan/planner/`-relative path -> the alp-sdk-relative source path it was
#: hand-ported from (a key into HAND_PORT_HASHES). Doubles as the coverage
#: list `test_every_planner_module_is_tracked_or_declared_exempt` checks
#: every on-disk `tan/planner/**.py` file against.
HAND_PORT_SOURCES: dict[str, str] = {
    "zephyr_board.py": "scripts/gen_zephyr_board.py",
    "project_loader.py": "scripts/alp_project_loader.py",
    "som_metadata.py": "scripts/alp_project_loader.py",
    "template.py": "scripts/alp_template.py",
    "project_emit/__init__.py": "scripts/alp_project_emit/__init__.py",
    "project_emit/bom_netlist.py": "scripts/alp_project_emit/bom_netlist.py",
    "project_emit/dts.py": "scripts/alp_project_emit/dts.py",
    "project_emit/hw_info.py": "scripts/alp_project_emit/hw_info.py",
    "project_emit/native_sim.py": "scripts/alp_project_emit/native_sim.py",
    "project_emit/west_libs.py": "scripts/alp_project_emit/west_libs.py",
}

#: `tan/planner/`-relative paths with no alp-sdk source at all -- original tan
#: code, not a port. Empty today: every current file traces to PINNED_HASHES
#: or HAND_PORT_SOURCES. A file belongs here only if it genuinely has no
#: alp-sdk counterpart; a real hand-port belongs in HAND_PORT_SOURCES instead.
EXEMPT_FROM_RELOCATION_TRACKING: frozenset[str] = frozenset()


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


#: The env var carrying a checkout pinned at HAND_PORT_PINNED_SDK_COMMIT.
#: Deliberately its OWN name -- NOT ALP_SDK_ROOT/ALP_SDK_PARITY_ROOT, which
#: `sdk_root()` above reads and which stay pinned to the DIFFERENT
#: PINNED_SDK_COMMIT. Reusing either here is the tan-cli#296 bug: two audits
#: of two different alp-sdk states sharing one checkout.
HAND_PORT_SDK_ROOT_ENV = "ALP_SDK_HAND_PORT_ROOT"


def _resolve_hand_port_sdk_root() -> pathlib.Path | None:
    """Same check `tests.conftest.sdk_root()` makes, for HAND_PORT_SDK_ROOT_ENV
    alone -- see the module docstring's tan-cli#296 note for why this cannot
    just be a second name added to that function's own env-var list."""
    raw = os.environ.get(HAND_PORT_SDK_ROOT_ENV)
    if raw and (pathlib.Path(raw) / "scripts" / "alp_project.py").is_file():
        return pathlib.Path(raw).resolve()
    return None


#: Resolved at import time, same reasoning as `SDK` above.
HAND_PORT_SDK: pathlib.Path | None = _resolve_hand_port_sdk_root()


def _hand_port_sdk_root() -> pathlib.Path:
    if HAND_PORT_SDK is None:
        # Skip, not fail -- same shape as `_sdk_root()` above, same reason: a
        # run that never bound this root (ci.yml's `python` job, a local
        # `pytest tests/`) is not "set up to do this audit", not broken.
        # tan-cli#308 made this branch `pytest.fail` instead, on the theory
        # that a skip here is how #275 drifted silently -- and that took
        # ci.yml's unbound `python` job down with it. The enforcement #308
        # wanted belongs where the root IS bound: `parity.yml`'s seam1 job
        # asserts BY NODE ID that this test did not skip there (see that
        # job's `python/tests/gates` step), so THIS branch can go back to
        # skipping without reopening #275 -- the one job meant to run the
        # audit can no longer silently no-op it.
        #
        # Still deliberately NOT a fallback to `_sdk_root()`
        # (ALP_SDK_ROOT/ALP_SDK_PARITY_ROOT, the relocated-planner root,
        # pinned at the DIFFERENT PINNED_SDK_COMMIT) -- that is the
        # tan-cli#296 bug: two audits, two pins, sharing one checkout.
        pytest.skip(
            f"{HAND_PORT_SDK_ROOT_ENV} is not set (or does not point at a "
            "real alp-sdk checkout) -- no bound alp-sdk checkout to compare "
            "the HAND-PORTED tan/planner/ modules against, so this "
            "staleness gate cannot run. This is a SKIP about the missing "
            f"root, not a pass: set {HAND_PORT_SDK_ROOT_ENV} to an alp-sdk "
            f"checkout at HAND_PORT_PINNED_SDK_COMMIT "
            f"({HAND_PORT_PINNED_SDK_COMMIT}) to actually exercise the gate."
        )
    return HAND_PORT_SDK


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


def test_hand_ported_planner_modules_match_their_pinned_sdk_source():
    """tan-cli#279: the HAND-PORT half of the staleness gate.

    Same shape as `test_relocated_planner_modules_match_the_pinned_sdk_audit`
    above, but keyed by the alp-sdk-relative source path rather than a
    `scripts/alp_orchestrate/`-relative name -- these nine files live all over
    alp-sdk's `scripts/` tree, not in one directory. Reads its OWN root
    (`_hand_port_sdk_root()`, ALP_SDK_HAND_PORT_ROOT) rather than `_sdk_root()`
    -- see the module docstring's tan-cli#296 note.
    """
    root = _hand_port_sdk_root()
    drifted: list[str] = []
    for rel_path, pinned_hash in HAND_PORT_HASHES.items():
        upstream = root / rel_path
        if not upstream.is_file():
            drifted.append(f"{rel_path}: gone from the bound SDK checkout")
            continue
        current_hash = hashlib.sha256(upstream.read_bytes()).hexdigest()
        if current_hash != pinned_hash:
            drifted.append(
                f"{rel_path}: sha256 {current_hash} != pinned {pinned_hash}"
            )
    assert not drifted, (
        "a tan/planner/ hand-port's alp-sdk source moved past the commit "
        f"({HAND_PORT_PINNED_SDK_COMMIT}) it was last audited against. Diff "
        "each file below in the bound alp-sdk checkout, port the behavioural "
        "delta into the tan/planner/ module(s) HAND_PORT_SOURCES names for it, "
        "then update HAND_PORT_HASHES (and HAND_PORT_PINNED_SDK_COMMIT) in "
        "this file to re-pin the audit:\n  "
        + "\n  ".join(drifted)
    )


def test_every_planner_module_is_tracked_or_declared_exempt():
    """tan-cli#279: the COVERAGE half -- no bound alp-sdk checkout needed.

    The two tests above only ever check files that are already keys in
    PINNED_HASHES / HAND_PORT_HASHES, so a brand-new hand-port added to
    `tan/planner/` from yet another alp-sdk source is invisible to both --
    which is exactly how `zephyr_board.py` and `project_loader.py` escaped
    for as long as they did. This asserts the SET instead: every `.py` file
    that exists on disk under `tan/planner/` must be named in one of
    PINNED_HASHES (relocated from `scripts/alp_orchestrate/`),
    HAND_PORT_SOURCES (hand-ported from elsewhere), or
    EXEMPT_FROM_RELOCATION_TRACKING (no alp-sdk source at all) -- so a future
    hand-port with none of the three has nowhere to hide.
    """
    planner = pathlib.Path(__file__).resolve().parents[2] / "tan" / "planner"
    tracked = set(PINNED_HASHES) | set(HAND_PORT_SOURCES) | EXEMPT_FROM_RELOCATION_TRACKING
    untracked = sorted(
        rel
        for p in planner.rglob("*.py")
        for rel in [str(p.relative_to(planner)).replace("\\", "/")]
        if rel not in tracked
    )
    assert not untracked, (
        "tan/planner/ has file(s) with no recorded alp-sdk origin: "
        + ", ".join(untracked)
        + ". If it is a hand-port of an alp-sdk file OUTSIDE "
        "scripts/alp_orchestrate/, add it to HAND_PORT_HASHES + "
        "HAND_PORT_SOURCES above (tan-cli#279). If it relocated FROM "
        "scripts/alp_orchestrate/, its basename belongs in PINNED_HASHES "
        "instead. If it has no alp-sdk source at all, declare it in "
        "EXEMPT_FROM_RELOCATION_TRACKING with a one-line reason."
    )
