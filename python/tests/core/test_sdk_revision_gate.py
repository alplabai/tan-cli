# SPDX-License-Identifier: Apache-2.0
"""`tan.planner.sdk_compat` -- the hw_rev <-> SDK-version gate ported from
alp-sdk's `scripts/alp_orchestrate/sdk_compat.py` (alp-sdk#1019), and
`tan.planner.loader._check_sdk_supports_hw_rev`, the loader-side call that
turns a `sdk_compat.check()` refusal into `SdkRevisionUnsupported`.

Before this port, the refusal had ZERO tan-side coverage: the 970-passing
parity run never exercises it because every fixture board sits inside its
declared range, so `check()` returns `None` on every path it runs and the
refusal branch never executes.

alp-sdk shipped `tests/scripts/test_sdk_revision_gate.py` in the same window;
this ports its behavioural half -- the comparison itself (pure, no metadata
tree) and the refusal reaching a caller through `load_board_yaml`. Not
ported: upstream's own byte-for-byte planner-parity concerns, which
`tests/parity/test_planner_emit_parity.py` already owns for this repo.

Importing `tan.planner.*` needs a bound alp-sdk root with real metadata (the
package's eager import chain reads several `metadata/registries/*` files at
import time) -- same requirement, and the same env vars, as
`tests/core/test_kconfig_symbols.py` and
`tests/parity/test_planner_emit_parity.py`. `sdk_compat` itself is pure and
data-only, but reaching it through `tan.planner.sdk_compat` still runs
`tan/planner/__init__.py` first (submodule imports always run the parent
package), so the whole module is gated the same way.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml


def _sdk_root() -> Path | None:
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


SDK = _sdk_root()

_SKIP_REASON = (
    "set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner can bind a "
    "root and import (same requirement as the parity suite)"
)


def _load_sdk_compat_standalone():
    """Load `sdk_compat.py` directly, bypassing `tan.planner`'s package
    `__init__` (whose eager import chain reads several
    `metadata/registries/*` files and therefore needs a bound SDK root).
    `sdk_compat` itself imports only re/pathlib/typing/yaml, so the pure
    comparison logic below needs no SDK, no env var, and no bound root --
    only the loader-level tests (through the `planner` fixture) do.
    """
    import importlib.util

    import tan

    spec = importlib.util.spec_from_file_location(
        "tan_sdk_compat_standalone",
        Path(tan.__file__).parent / "planner" / "sdk_compat.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sc():
    return _load_sdk_compat_standalone()


@pytest.fixture(scope="module")
def planner():
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    from tan.planner_root import bind_sdk_root
    bind_sdk_root(SDK)
    import tan.planner as planner_pkg
    return planner_pkg


# --------------------------------------------------------------------------
# parse_version -- pure, no metadata tree.
# --------------------------------------------------------------------------


def test_parse_version_reads_bare_and_v_prefixed(sc):
    assert sc.parse_version("1.2.3") == (1, 2, 3)
    assert sc.parse_version("v1.2.3") == (1, 2, 3)


def test_parse_version_is_none_for_unparseable_or_absent(sc):
    assert sc.parse_version(None) is None
    assert sc.parse_version("garbage") is None
    assert sc.parse_version("") is None


# --------------------------------------------------------------------------
# incompatibility -- pure, no metadata tree.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sdk,lo,hi", [
    ("0.13.0", "0.3.0", None),      # every range in tree today
    ("0.13.0", None, None),         # `status: reserved` revs declare neither
    ("0.3.0", "0.3.0", None),       # lower bound is INCLUSIVE
    ("0.5.0", "0.3.0", "0.5.0"),    # upper bound is INCLUSIVE
    ("0.13.0", "v0.3.0", None),     # a `v` prefix parses
])
def test_versions_inside_the_declared_range_are_allowed(sc, sdk, lo, hi):
    assert sc.incompatibility(sdk, lo, hi) is None


def test_both_bounds_are_inclusive_at_the_exact_boundary(sc):
    """Read the code, don't assume: the docstring says both bounds are
    inclusive -- a revision declaring `min_sdk_version: 0.3.0` is supported
    *by* 0.3.0, not merely after it, and likewise at the ceiling."""
    assert sc.incompatibility("0.3.0", "0.3.0", None) is None    # == min
    assert sc.incompatibility("0.5.0", "0.3.0", "0.5.0") is None  # == max


def test_below_the_lower_bound_refuses_and_names_both_versions(sc):
    why = sc.incompatibility("0.2.0", "0.3.0", None)
    assert why is not None
    assert "0.3.0" in why and "0.2.0" in why


def test_above_the_upper_bound_refuses_and_names_both_versions(sc):
    why = sc.incompatibility("0.13.0", "0.3.0", "0.5.0")
    assert why is not None
    assert "0.5.0" in why and "0.13.0" in why


def test_the_comparison_is_numeric_not_lexicographic(sc):
    """`0.13.0` must read as ABOVE `0.5.0`, not below it. String comparison
    puts "0.13.0" < "0.5.0" and would silently allow the exact upgrade this
    gate exists to catch."""
    assert sc.incompatibility("0.13.0", None, "0.5.0") is not None
    assert sc.incompatibility("0.5.0", None, "0.13.0") is None


def test_an_unreadable_sdk_version_stays_quiet(sc):
    """No version is not evidence of a mismatch -- mirrors
    `buildplan._sdk_version`, which returns None when there is no adjacent
    `metadata/` tree (a packaged wheel) rather than failing."""
    assert sc.incompatibility(None, "0.3.0", "0.5.0") is None


def test_a_malformed_bound_is_treated_as_absent_not_as_a_refusal(sc):
    """A typo in metadata must not become a refused build; that is
    `check_metadata`'s job, not this gate's."""
    assert sc.incompatibility("0.13.0", "garbage", None) is None
    assert sc.incompatibility("0.13.0", None, "not-a-version") is None


def test_an_absent_range_is_unbounded_not_zero(sc):
    """`reserved` revisions declare no range at all; reading an absent bound
    as a bound would refuse every one of them."""
    assert sc.incompatibility("0.13.0", None, None) is None


# --------------------------------------------------------------------------
# check -- SoM-then-board ordering, pure, no metadata tree.
# --------------------------------------------------------------------------


def test_check_is_none_when_neither_side_declares_a_range(sc):
    assert sc.check("0.13.0", som_revision={}, som_label="som",
                     board_revision_entry={}, board_label="board") is None


def test_both_sides_are_reported_when_both_refuse(sc):
    why = sc.check(
        "0.13.0",
        som_revision={"max_sdk_version": "0.5.0"},
        som_label="SoM E1M-AEN801 hw_rev r2",
        board_revision_entry={"min_sdk_version": "9.0.0"},
        board_label="board e1m-evk hw_rev r1",
    )
    assert why is not None
    assert "E1M-AEN801" in why and "e1m-evk" in why


def test_the_som_is_checked_before_the_board(sc):
    """`check`'s own docstring: "The SoM is checked first because its range
    is the one a customer changes by swapping modules." Pin the ORDER, not
    just that both names appear."""
    why = sc.check(
        "0.13.0",
        som_revision={"max_sdk_version": "0.5.0"},
        som_label="SOM_SIDE",
        board_revision_entry={"min_sdk_version": "9.0.0"},
        board_label="BOARD_SIDE",
    )
    assert why is not None
    assert why.index("SOM_SIDE") < why.index("BOARD_SIDE")


def test_check_reports_a_board_only_refusal(sc):
    why = sc.check(
        "0.13.0",
        som_revision={},
        som_label="som",
        board_revision_entry={"max_sdk_version": "0.5.0"},
        board_label="BOARD_SIDE",
    )
    assert why is not None
    assert "BOARD_SIDE" in why


# --------------------------------------------------------------------------
# The refusal reaching a caller through load_board_yaml.
# --------------------------------------------------------------------------


def _metadata_copy(tmp_path: Path) -> Path:
    assert SDK is not None
    dest = tmp_path / "metadata"
    shutil.copytree(SDK / "metadata", dest)
    return dest


def _cap_family_revision(meta: Path, family: str, rev: str, ceiling: str) -> None:
    path = meta / "e1m_modules" / family / "hw-revisions.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["hw_revisions"][rev]["max_sdk_version"] = ceiling
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _fixture_board_yaml(tmp_path: Path) -> Path:
    """A self-authored board.yaml -- not alp-sdk's own
    `examples/peripheral-io/gpio-button-led/board.yaml` -- so this suite's
    only coverage of `SdkRevisionUnsupported` actually firing does not
    depend on a file path inside a second, independently moving repo (a
    doc-example reshuffle over there must not silently turn this refusal
    proof into a skip over here). Same relevant shape as that example:
    SoM `hw_rev` is left unset, so `_check_sdk_supports_hw_rev` falls
    back to the SKU preset's `default_hw_rev` (r2) and the family table
    -- not the board table -- is the one consulted, matching
    alp-sdk's own `test_sdk_revision_gate.py`.
    """
    path = tmp_path / "fixture-board.yaml"
    path.write_text(
        "som:\n"
        "  sku: E1M-AEN801\n"
        "preset: e1m-evk\n"
        "cores:\n"
        "  m55_hp:\n"
        "    app: ./src\n",
        encoding="utf-8",
    )
    return path


def test_the_shipped_tree_loads_cleanly(planner, tmp_path):
    """The gate must not break anything currently in tree: every range
    shipped today is open-ended on the high side, so a live SDK version
    stays inside every declared range."""
    project = planner.load_board_yaml(_fixture_board_yaml(tmp_path))
    assert project.sku == "E1M-AEN801"


def test_an_out_of_range_som_revision_refuses_through_the_loader(planner, tmp_path):
    meta = _metadata_copy(tmp_path)
    _cap_family_revision(meta, "aen", "r2", "0.5.0")

    with pytest.raises(planner.SdkRevisionUnsupported) as excinfo:
        planner.load_board_yaml(_fixture_board_yaml(tmp_path), metadata_root=meta)

    message = str(excinfo.value)
    assert "0.5.0" in message          # the ceiling that was exceeded
    assert "hw_rev r2" in message      # which revision
    assert "E1M-AEN801" in message     # which SoM


def test_the_refusal_is_an_orchestrator_error_subclass(planner, tmp_path):
    """Existing `except OrchestratorError` handlers must keep catching it."""
    meta = _metadata_copy(tmp_path)
    _cap_family_revision(meta, "aen", "r2", "0.5.0")

    with pytest.raises(planner.OrchestratorError):
        planner.load_board_yaml(_fixture_board_yaml(tmp_path), metadata_root=meta)


# --------------------------------------------------------------------------
# revision_known -- the existence half (alp-sdk#1025), synced in for
# tan-cli#275. Pure, no metadata tree.
# --------------------------------------------------------------------------


def test_revision_known_is_none_when_the_table_has_no_hw_revisions(sc):
    """None, not False -- an inline board has nothing to check against, and
    a caller must be able to tell that apart from a genuine miss."""
    assert sc.revision_known({}, "r2") is None
    assert sc.revision_known({"hw_revisions": "not-a-map"}, "r2") is None
    assert sc.revision_known(None, "r2") is None


def test_revision_known_is_blind_to_status(sc):
    """A `status: reserved` revision EXISTS; whether it is buildable is a
    different question this predicate deliberately does not answer."""
    table = {"hw_revisions": {"r2": {}, "r3": {"status": "reserved"}}}
    assert sc.revision_known(table, "r2") is True
    assert sc.revision_known(table, "r3") is True
    assert sc.revision_known(table, "r99") is False
    assert sc.revision_known(table, None) is False


def test_an_unknown_som_revision_refuses_through_the_loader(planner, tmp_path):
    """The #1025 safe half: an hw_rev absent from the family table used to
    resolve to base-revision routing with a clean exit code -- a
    wrong-hardware emit. It must refuse, and name what IS available."""
    path = tmp_path / "unknown-rev-board.yaml"
    path.write_text(
        "som:\n"
        "  sku: E1M-AEN801\n"
        "  hw_rev: r99\n"
        "preset: e1m-evk\n"
        "cores:\n"
        "  m55_hp:\n"
        "    app: ./src\n",
        encoding="utf-8",
    )
    with pytest.raises(planner.SdkRevisionUnknown) as excinfo:
        planner.load_board_yaml(path)

    message = str(excinfo.value)
    assert "r99" in message
    assert "E1M-AEN801" in message
    assert "r2" in message  # the available list, not just the refusal


def test_the_unknown_revision_refusal_is_an_orchestrator_error_subclass(planner):
    assert issubclass(planner.SdkRevisionUnknown, planner.OrchestratorError)
    assert not issubclass(planner.SdkRevisionUnknown,
                          planner.SdkRevisionUnsupported)


# --------------------------------------------------------------------------
# jlink_flash_device -- alp-sdk#1057, synced in for tan-cli#275. Without
# this key in `flash_args`, `tan.core.flash_plan`'s FLOW_D_KEYS consumer
# hard-refuses and Flow D can never arm on a real AEN board.
# --------------------------------------------------------------------------


def test_an_aen_zephyr_slice_publishes_its_jlink_flash_device(planner, tmp_path):
    from tan.planner.orchestrator import _slice_flash_recipe

    project = planner.load_board_yaml(_fixture_board_yaml(tmp_path))
    method, args = _slice_flash_recipe(project.cores["m55_hp"])

    assert method == "zephyr_west_flash"
    assert args == {"jlink_flash_device": "AE822FA0E5597LS0_M55_HE"}


def test_a_slice_whose_variant_publishes_no_profile_keeps_empty_flash_args(planner):
    """No shape change for every non-AEN slice: an absent profile is a
    published "unknown", never a value to invent."""
    from tan.planner.models import Slice
    from tan.planner.orchestrator import _slice_flash_recipe

    method, args = _slice_flash_recipe(Slice(core_id="m33", os="zephyr"))
    assert (method, args) == ("zephyr_west_flash", {})
