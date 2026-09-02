# SPDX-License-Identifier: Apache-2.0
"""Never emit a `CONFIG_ALP_SDK_CHIP_*` the bound SDK cannot resolve (#728).

`_chip_has_driver` decides what to emit from `chips/<slug>/` on disk. That is
the right intent, but it is an INFERENCE, while Zephyr resolves the Kconfig
DECLARATION. When a tan and an alp-sdk disagree about which chips have
drivers, the emitted line reaches Kconfig as

    alp.conf:28: warning: attempt to assign the value 'y' to the undefined
    symbol ALP_SDK_CHIP_DP83825
    error: Aborting due to Kconfig warnings

taking out `tan build` for the whole SoM and blaming a generated file the
customer never wrote. That is tan-cli#728, measured with released `tan 0.5.1`
against alp-sdk `dev`.

The disagreement cannot be reproduced from tan `dev` against alp-sdk `dev` --
measured, the two agree exactly (80 `chips/<slug>/` directories, 80 declared
symbols, no divergence either way). So the skew is injected by patching the
declaration set, which is precisely the half that differs between an old
binary and a newer SDK.

The bound-root machinery mirrors `test_sdk_capability.py`: `tan.planner` is a
package whose `__init__` requires a bound root at import time
(`tan/planner_root.py`), so the module is imported lazily inside each test.
"""

from __future__ import annotations

import pytest

# `_bound_sdk` is a pytest fixture, imported for its side effect -- the
# same idiom `_baremetal_support`'s consumers use for `bound_sdk_root`.
from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.kconfig requires SOME bound "
           "root (tan/planner_root.py). A SKIP about the missing root, not a "
           "pass.",
)


def _kc():
    """Imported inside the call so the module is not imported before
    `bind_sdk_root` has run (collection order)."""
    import tan.planner.kconfig as m

    m._declared_chip_symbols.cache_clear()
    return m


@pytest.fixture(autouse=True)
def _no_patched_declaration_escapes():
    """`_declared_chip_symbols` is `@lru_cache(maxsize=1)` and every build in
    the session reads it -- clear on the way out too, so a set patched here
    cannot leak a bogus verdict into another test."""
    yield
    try:
        import tan.planner.kconfig as m

        m._declared_chip_symbols.cache_clear()
    except Exception:  # pragma: no cover -- import guarded by pytestmark
        pass


def test_undeclared_chip_symbol_is_refused_by_name(monkeypatch):
    """The refusal names the symbol AND the chip, so the reader knows which
    pair disagrees instead of hunting a CONFIG_ line they never wrote."""
    kc = _kc()
    from tan.planner.models import OrchestratorError

    monkeypatch.setattr(
        kc, "_declared_chip_symbols", lambda: frozenset({"ALP_SDK_CHIP_CC3501E"}))

    with pytest.raises(OrchestratorError) as excinfo:
        kc._assert_chip_symbols_declared(["cc3501e", "dp83825"])

    message = str(excinfo.value)
    assert "ALP_SDK_CHIP_DP83825" in message, message
    assert "dp83825" in message, message
    assert "ALP_SDK_CHIP_CC3501E" not in message, (
        f"only the offending symbol may be named, not a declared one: {message}")


def test_all_declared_symbols_pass_through(monkeypatch):
    """Invisible when tan and the SDK agree -- the normal case, 80/80 on dev."""
    kc = _kc()
    monkeypatch.setattr(
        kc, "_declared_chip_symbols",
        lambda: frozenset({"ALP_SDK_CHIP_CC3501E", "ALP_SDK_CHIP_DP83825"}))

    kc._assert_chip_symbols_declared(["cc3501e", "dp83825"])  # must not raise


def test_an_unreadable_kconfig_tree_does_not_refuse_every_build(monkeypatch):
    """Empty declaration set means "cannot verify", not "nothing is declared".

    Refusing every build on a layout assumption would be a worse failure than
    the one this guard prevents.
    """
    kc = _kc()
    monkeypatch.setattr(kc, "_declared_chip_symbols", lambda: frozenset())

    kc._assert_chip_symbols_declared(["dp83825"])  # must not raise


def test_the_real_sdk_declares_every_symbol_this_tan_would_emit():
    """Live check against the bound alp-sdk -- the assertion that would have
    caught #728 at plan time rather than in Zephyr."""
    kc = _kc()
    declared = kc._declared_chip_symbols()
    if not declared:
        pytest.skip("no alp-sdk kconfig declarations resolved -- nothing to verify against")
    chips_dir = kc.REPO / "chips"
    if not chips_dir.is_dir():
        pytest.skip("bound alp-sdk has no chips/ directory")

    emitted = [d.name for d in sorted(chips_dir.iterdir()) if d.is_dir()]
    kc._assert_chip_symbols_declared(emitted)  # must not raise
