# SPDX-License-Identifier: Apache-2.0
"""`tan.core.som_buildability.hw_rev_not_buildable`: direct unit coverage,
without going through `tan init`'s CLI plumbing.

tan-cli#743 (the original default-hw_rev warning) + tan-cli#1008 review
(explicit hw_rev, the malformed-entry divergence from
`alp_orchestrate/sdk_compat.revision_buildable`, and the
`has_buildable_alternative` fact the remediation text's second clause needs
to stay honest)."""

from __future__ import annotations

from pathlib import Path

from tan.core.som_buildability import hw_rev_not_buildable


def _sdk(tmp_path: Path, *, sku: str, family_dir: str, default_hw_rev: str, revisions: str) -> Path:
    sdk = tmp_path / "sdk"
    modules = sdk / "metadata" / "e1m_modules"
    modules.mkdir(parents=True, exist_ok=True)
    (modules / f"{sku}.yaml").write_text(
        f"sku: {sku}\ndefault_hw_rev: {default_hw_rev}\n", encoding="utf-8",
    )
    family = modules / family_dir
    family.mkdir(exist_ok=True)
    (family / "hw-revisions.yaml").write_text(
        f"family: {family_dir}\nhw_revisions:\n{revisions}", encoding="utf-8",
    )
    return sdk


def test_falls_back_to_the_preset_default_hw_rev_when_none_is_passed(tmp_path):
    sdk = _sdk(
        tmp_path, sku="E1M-NX9101", family_dir="imx93", default_hw_rev="r1",
        revisions="  r1:\n    status: tbd\n",
    )
    result = hw_rev_not_buildable(sdk, "E1M-NX9101")
    assert result is not None
    assert (result.sku, result.hw_rev, result.status) == ("E1M-NX9101", "r1", "tbd")


def test_uses_the_explicit_hw_rev_over_the_preset_default(tmp_path):
    """tan-cli#1008 review major 2: a scaffolded board.yaml's own explicit
    `hw_rev:` -- read off the file, not the preset -- is what gets judged."""
    sdk = _sdk(
        tmp_path, sku="E1M-NX9101", family_dir="imx93", default_hw_rev="r9",
        revisions="  r1:\n    status: tbd\n  r9:\n    status: production\n",
    )
    result = hw_rev_not_buildable(sdk, "E1M-NX9101", "r1")
    assert result is not None
    assert (result.hw_rev, result.status) == ("r1", "tbd")


def test_an_explicit_buildable_hw_rev_overrides_a_not_buildable_default(tmp_path):
    sdk = _sdk(
        tmp_path, sku="E1M-NX9101", family_dir="imx93", default_hw_rev="r1",
        revisions="  r1:\n    status: tbd\n  r2:\n    status: production\n",
    )
    assert hw_rev_not_buildable(sdk, "E1M-NX9101", "r2") is None


def test_an_hw_rev_unknown_to_the_family_table_is_nothing_to_judge(tmp_path):
    """tan-cli#1008 review major 2's stale-retarget case: a `hw_rev:` that
    is not even a declared key is `revision_known()`'s question, not this
    function's -- it must not be reported as "not buildable" (there is no
    status to have read), and must not raise."""
    sdk = _sdk(
        tmp_path, sku="E1M-NX9101", family_dir="imx93", default_hw_rev="r1",
        revisions="  r1:\n    status: tbd\n",
    )
    assert hw_rev_not_buildable(sdk, "E1M-NX9101", "r2") is None


def test_a_malformed_entry_present_as_a_key_is_not_buildable(tmp_path):
    """tan-cli#1008 review minor: mirrors
    `alp_orchestrate/sdk_compat.revision_buildable` verbatim -- a key
    present but not a dict is a malformed entry, not an absent one."""
    sdk = _sdk(
        tmp_path, sku="E1M-NX9101", family_dir="imx93", default_hw_rev="r1",
        revisions="  r1:\n",  # `r1:` with no mapping body -> null entry
    )
    result = hw_rev_not_buildable(sdk, "E1M-NX9101", "r1")
    assert result is not None
    assert (result.hw_rev, result.status) == ("r1", None)


def test_has_buildable_alternative_true_when_the_family_has_another_buildable_rev(tmp_path):
    sdk = _sdk(
        tmp_path, sku="E1M-NX9101", family_dir="imx93", default_hw_rev="r1",
        revisions="  r1:\n    status: tbd\n  r2:\n    status: production\n",
    )
    result = hw_rev_not_buildable(sdk, "E1M-NX9101", "r1")
    assert result is not None
    assert result.has_buildable_alternative is True


def test_has_buildable_alternative_false_when_it_is_the_familys_only_revision(tmp_path):
    """tan-cli#1008 review minor's exact repro: imx93 publishes exactly one
    hw_rev for E1M-NX9101 today, so "or name a buildable hw_rev explicitly"
    is not real advice there."""
    sdk = _sdk(
        tmp_path, sku="E1M-NX9101", family_dir="imx93", default_hw_rev="r1",
        revisions="  r1:\n    status: tbd\n",
    )
    result = hw_rev_not_buildable(sdk, "E1M-NX9101", "r1")
    assert result is not None
    assert result.has_buildable_alternative is False


def test_has_buildable_alternative_false_when_the_other_rev_is_also_not_buildable(tmp_path):
    sdk = _sdk(
        tmp_path, sku="E1M-NX9101", family_dir="imx93", default_hw_rev="r1",
        revisions="  r1:\n    status: tbd\n  r2:\n    status: reserved\n",
    )
    result = hw_rev_not_buildable(sdk, "E1M-NX9101", "r1")
    assert result is not None
    assert result.has_buildable_alternative is False
