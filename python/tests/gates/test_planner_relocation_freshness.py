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
#: `f4d87a1f` by the tan-cli #485 re-sync (#320 recurred: the #320 pin bump
#: held two days before alp-sdk landed two more contract-surface commits).
#: This bump carries alp-sdk #1169 (`_DRIVER_STATUS_SUFFIX` -- a
#: `<x>_driver_status` on_module field is a maturity tier, never a chip
#: slug; without the filter the literal string "none" reached
#: `CONFIG_ALP_SDK_CHIP_NONE=y`, an undeclared Kconfig symbol that aborted
#: Zephyr's CMake configure on every V2N-family SKU) and #1088 (`kind:
#: rpmsg` refuses `cacheable: true` -- `<alp/rpc.h>` has no
#: cache-maintenance implementation -- plus the companion
#: `_emit_cross_core_shmem_cache` widening to `entry.kind in ("raw_shmem",
#: "rpmsg")`) into `tan/planner/`. Also carries alp-sdk #1069's `carveout:
#: false` memory_map exclusion (`carveout.py`, already ported ahead of this
#: bump) and doc-only wording drift in `buildplan.py`/`orchestrator.py`
#: (alp-sdk #1214, no behavioural delta). Deliberately NOT bumped to
#: alp-sdk `origin/dev` tip: this exact commit is the last one before
#: `validate.py`'s curated-library-registry derivation (alp-sdk #1197,
#: `f7c69cea`) starts drifting that file too, which is unrelated to
#: tan-cli#485 and stays for its own audited bump.
PINNED_SDK_COMMIT = "f30f4d4ba76c235cbb2b2cc713666cb858677e03"  # alp-sdk origin/dev

#: sha256 of every `scripts/alp_orchestrate/<name>.py` at PINNED_SDK_COMMIT,
#: for every upstream module that has a same-named relocated counterpart
#: under `tan/planner/` (i.e. the actual drift surface -- files renamed on
#: relocation, like `alp_project_loader.py` -> `som_metadata.py`, have no
#: single upstream file to pin and are out of scope for this hash check).
PINNED_HASHES: dict[str, str] = {
    "__main__.py": "77b98caf27ba425b888a19f8727683bba23e7c24ebb4b6aa1874e5316a291d27",
    "__init__.py": "739a0288487997d6f1be7dc1f47fcf05b34a16386c0c81e8fe4eaadcfb84e3f0",
    "buildplan.py": "50f740465b23bd2aebfdd1f8dc210126b0da6e8d0600f944beb17889d16cbd65",
    "carveout.py": "cdc9d78544441ae6b05dbdb12c5790d2bf4d5cb97fed481df14b57f215265d18",
    "cli.py": "b2d9e82d62c5dd1668d4d893e148fb66efc50825b465c8f8385f9bf668572419",
    "headers.py": "9a9cc0ca4801b2bdb7a551662e4dddf27c47bb42fad06939c92a8c95b221156b",
    "kconfig.py": "d80ab84bec3bc1aefe8640a6d5d1b43334447cb4ea87d4c25cee927ebfc2bf17",
    "kconfig_symbols.py": "fe3a3df4aa00db808ce8443548d113b4a97cf600b5fda106d075e8d071243729",
    "libraries.py": "47b823e0fc06cc657a3c3068598b953e342720cf359443651a9996b93be7aaa5",
    "loader.py": "bd5177c0a9fcb8973fe7f899ef5ec91a2b386ea51a58bd1dc263b2e0c8efb183",
    "manifest.py": "930aa9c453fd86b487f66ec84be8f074a53f22a6077b0310390e176fee7918ba",
    "memregion.py": "f3e62050172bb1500e98d0023eda7408a67e1085a70a4acd92f45f08213ebfa3",
    "models.py": "6d33258874d6f732d66668d30c22fd644e02de2fb9f35e7b497ddd2d81164109",
    "orchestrator.py": "01c7d90fc50bf85974fdbd228fac8319d687a121f45bc64c9528dd1fed3debff",
    "partition.py": "97e7646ab7f8692919d1cd529d42be453d7fc3240416f98e202a5920cb6cb029",
    "paths.py": "a2d8b74570f88ad223d797d6428a58fc3851dad6bb9a1ae2c2aa109db789bc93",
    "sdk_compat.py": "ae8e5244877d3e9ad35f89e6b7782861f810825321052ed9f5770b9762c0281a",
    "secure.py": "a6a5762fbac2f99fc4356f01b3ffedd15d366af6d2ddde0042223b3da749cbe6",
    "slugs.py": "5c0481e2375bfc0ece96960e6a3c5f51d1fcd1e629ea7a2a50e6581765f6657d",
    "topology.py": "12f5f62d3adeb9e935594934fd2fc2b1fbeaec6f466d6dd89c329c54e844f3b1",
    "validate.py": "2dbe9dcb36ff0ebe4c968ef120983342aa00f02f32b9166f9c1608d1578495e7",
}

#: alp-sdk commit the SDK-SIDE SOURCE FILES in HAND_PORT_HASHES were last
#: audited against -- the commit `v0.15.0-rc1` names, not the tag itself, so a
#: tag that is later moved cannot silently repoint what this pins.
#: `zephyr_board.py` and `project_loader.py` were re-vendored to this point in
#: 947f3d0 / 52fdd01.  This pin does NOT claim the `tan/planner/` SIDE is
#: frozen at that same point, and it is not: tan-cli#485 forward-ported alp-sdk
#: #1048's case-insensitive TBD match into `zephyr_board.py:109` and
#: `som_metadata.py:140`, and #1025's status-gate half into
#: `project_loader.py:196` -- both strictly AFTER 996937ac, both ported
#: without moving this pin. This test HASHES THE SDK SIDE ONLY
#: (`upstream = root / rel_path`, never the `tan/planner/` file), so a
#: hand-port racing ahead of its own recorded audit point like this is
#: invisible to it by construction -- a real, standing gap in this gate's
#: coverage that a future audit pass should account for, not a claim that it
#: doesn't exist.
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
    # strict_loaders.py is intentionally NOT in HAND_PORT_HASHES -- see the
    # STRICT_LOADERS_* block below for why it needs its own pin/root/test
    # rather than joining this bundle.
    "strict_loaders.py": "scripts/strict_loaders.py",
}


def test_hand_port_sources_declares_its_one_strict_loaders_exception():
    """Every `HAND_PORT_SOURCES` value is a key into `HAND_PORT_HASHES` --
    EXCEPT `scripts/strict_loaders.py`, deliberately (see the STRICT_LOADERS_*
    block below). Without this assertion, deleting
    `test_strict_loaders_matches_its_pinned_sdk_source` (or its
    `STRICT_LOADERS_*` pin) would leave `strict_loaders.py` in
    `HAND_PORT_SOURCES` -- satisfying `test_every_planner_module_is_tracked_
    or_declared_exempt`'s coverage check -- with NO staleness hash anywhere
    checking it, and neither coverage gate would notice: this is the one
    assertion that catches that specific silent regression."""
    unhashed = set(HAND_PORT_SOURCES.values()) - set(HAND_PORT_HASHES)
    assert unhashed == {"scripts/strict_loaders.py"}, (
        "HAND_PORT_SOURCES names a hand-port source with no staleness hash "
        f"anywhere: {sorted(unhashed)}. Every HAND_PORT_SOURCES value must be "
        "a key in HAND_PORT_HASHES, except scripts/strict_loaders.py (which "
        "has its own STRICT_LOADERS_HASH instead -- see that block's own "
        "comment for why). If this assertion fails because the set is EMPTY, "
        "scripts/strict_loaders.py's own STRICT_LOADERS_* pin was deleted or "
        "renamed -- restore it, don't just widen this assertion."
    )


#: tan-cli#485: `tan/planner/strict_loaders.py` (the alp-sdk #1127
#: duplicate-key-rejecting YAML/JSON loaders, wired into `loader.py`'s
#: `_load_yaml`/`_load_json` to close the silent-sku-retarget hazard) was
#: hand-ported from `scripts/strict_loaders.py` -- a file that does not
#: exist at all at HAND_PORT_PINNED_SDK_COMMIT (996937ac predates alp-sdk
#: #1127). It gets its OWN pin/root/test rather than joining
#: HAND_PORT_HASHES/HAND_PORT_PINNED_SDK_COMMIT, deliberately: auditing the
#: rest of that bundle between 996937ac and here turned up a REAL, narrower
#: gap this fix does not close. `scripts/alp_template.py` gained `_safe_join`/
#: `PathEscapeError` (alp-sdk #1125/#1126); `tan/planner/template.py` has its
#: own parallel `tan.core.fs_confine.PathEscapeError`/`resolve_confined`,
#: wired into every catalog-driven WRITE (`scaffold.py:1049`,
#: `init_cmd.py:804`, `generate_cmd.py:488`, `pinmux_cmd.py:347`) -- so no
#: write can escape its destination root. What it is NOT wired into is
#: `template.py`'s catalog-driven READS: `_rendered_bytes` (`:210` joins
#: `base_dir / record["example"]`, `:214` joins that onto each `rel` and
#: calls `.read_bytes()`) and `render_to_envelope` (`:1091`, the same join
#: onto `board.yaml`). Measured with a traversal `rel` against a shared
#: catalog fixture: `tan`'s `_rendered_bytes` returns the escaped file's
#: bytes; alp-sdk's raises `PathEscapeError`. Those bytes reach the caller
#: through `emit_scaffold`'s `[{path, contents}]` envelope, so a malicious or
#: compromised `--metadata-root`/catalog entry can read an arbitrary file
#: readable by the `tan` process and hand it back as scaffold content.
#: Threat model, scoped honestly: this requires a hostile SDK/catalog
#: checkout, which is already trusted to run arbitrary CMake/west during a
#: normal build -- meaningfully narrower than #1126's write-bug severity (a
#: write escape corrupts files outside any project the caller chose; this
#: read escape discloses them), but real, and not closed by this change.
#: Folding strict_loaders.py into HAND_PORT_HASHES would force a choice
#: between silently re-freezing the whole bundle past this gap unaudited
#: (the exact "bare bump" tan-cli#485 exists to name) or fixing it as a
#: drive-by inside an unrelated change -- filed separately instead (see
#: tan-cli#485's own report). (alp-sdk #1069's disjoint per-core slot0
#: memory layout, the OTHER large delta in this bundle's window, is NOT a
#: gap: tan-cli#432 already ported it into `zephyr_board.py` byte-for-byte,
#: confirmed by re-reading the whole file, not just the diff.) Same
#: tan-cli#296 rationale that split PINNED_SDK_COMMIT from
#: HAND_PORT_PINNED_SDK_COMMIT in the first place: two audits that drifted
#: at different rates need two pins, not one shared one.
STRICT_LOADERS_PINNED_SDK_COMMIT = "26b0040e9a762c16aff5c7c53b2e19cc7583b2a4"  # alp-sdk origin/dev, introduces #1127

#: sha256 of `scripts/strict_loaders.py` at STRICT_LOADERS_PINNED_SDK_COMMIT.
STRICT_LOADERS_HASH = "29cd2c62836e70abf2fa3f4e8c0939b406bd8cb6b976d9e97bc75d4180e38eef"

#: The env var carrying a checkout pinned at STRICT_LOADERS_PINNED_SDK_COMMIT.
#: Its own name for the same tan-cli#296 reason HAND_PORT_SDK_ROOT_ENV is not
#: reused: three different pins now exist in this file (PINNED_SDK_COMMIT,
#: HAND_PORT_PINNED_SDK_COMMIT, STRICT_LOADERS_PINNED_SDK_COMMIT), and
#: sharing a root between any two of them silently measures one against the
#: wrong oracle.
STRICT_LOADERS_SDK_ROOT_ENV = "ALP_SDK_STRICT_LOADERS_ROOT"


def _resolve_strict_loaders_sdk_root() -> pathlib.Path | None:
    raw = os.environ.get(STRICT_LOADERS_SDK_ROOT_ENV)
    if raw and (pathlib.Path(raw) / "scripts" / "alp_project.py").is_file():
        return pathlib.Path(raw).resolve()
    return None


#: Resolved at import time, same reasoning as `SDK` / `HAND_PORT_SDK` above.
STRICT_LOADERS_SDK: pathlib.Path | None = _resolve_strict_loaders_sdk_root()


def test_strict_loaders_matches_its_pinned_sdk_source():
    """The `strict_loaders.py` half of tan-cli#279's hand-port staleness gate.

    Same shape as `test_hand_ported_planner_modules_match_their_pinned_sdk_source`,
    scaled down to the one file that needs its own pin -- see the
    STRICT_LOADERS_PINNED_SDK_COMMIT block above for why. Skips (rather than
    fails) without STRICT_LOADERS_SDK_ROOT_ENV bound, same posture as the
    other two freshness tests: a run not set up to do this audit is not
    broken for not doing it.
    """
    if STRICT_LOADERS_SDK is None:
        pytest.skip(
            f"{STRICT_LOADERS_SDK_ROOT_ENV} is not set (or does not point "
            "at a real alp-sdk checkout) -- no bound alp-sdk checkout to "
            "compare tan/planner/strict_loaders.py against, so this "
            "staleness gate cannot run. This is a SKIP about the missing "
            f"root, not a pass: set {STRICT_LOADERS_SDK_ROOT_ENV} to an "
            f"alp-sdk checkout at STRICT_LOADERS_PINNED_SDK_COMMIT "
            f"({STRICT_LOADERS_PINNED_SDK_COMMIT}) to actually exercise "
            "the gate."
        )
    upstream = STRICT_LOADERS_SDK / "scripts" / "strict_loaders.py"
    assert upstream.is_file(), (
        f"scripts/strict_loaders.py: gone from the bound SDK checkout"
    )
    current_hash = hashlib.sha256(upstream.read_bytes()).hexdigest()
    assert current_hash == STRICT_LOADERS_HASH, (
        "scripts/strict_loaders.py moved past the alp-sdk commit "
        f"({STRICT_LOADERS_PINNED_SDK_COMMIT}) tan/planner/strict_loaders.py "
        f"was last audited against: sha256 {current_hash} != pinned "
        f"{STRICT_LOADERS_HASH}. Diff it in the bound alp-sdk checkout, port "
        "the behavioural delta into tan/planner/strict_loaders.py, then "
        "update STRICT_LOADERS_HASH (and STRICT_LOADERS_PINNED_SDK_COMMIT) "
        "in this file to re-pin the audit."
    )


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
