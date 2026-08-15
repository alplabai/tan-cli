# SPDX-License-Identifier: Apache-2.0
"""`tan.model` is self-contained: it imports nothing from an alp-sdk checkout.

Two of its eleven modules are the deliberate exception, and this file proves
both halves of that split rather than assuming it.

`targets.py` pulls `resolve_soc_path` from `tan.planner.som_metadata` -- the
ONE external symbol the relocated engine needs (ADR-0028 Task 2) -- and
`build.py` imports `targets`, so both are transitively a submodule of the
`tan.planner` PACKAGE. That package's `__init__` reads REAL files off a bound
alp-sdk checkout at IMPORT time, not just at call time -- e.g.
`tan/planner/slugs.py`'s module-scope `_PERIPHERAL_KCONFIG =
peripheral_kconfig()` reads `metadata/registries/peripheral-kconfig.json`
the instant the package is imported (measured: binding to a throwaway
`tmp_path` with no real metadata content raises `FileNotFoundError` at
import, not at any function call). So importing `tan.model.targets` or
`tan.model.build` needs SOME bound, real alp-sdk checkout -- exactly the same
requirement `tests/planner/test_sdk_capability.py` documents for
`tan.planner.sdk_capability`, which is otherwise just as self-contained as
this package's non-planner modules.

Every other module here needs no SDK checkout at all, bound or on
`sys.path`, to import -- that part of the original claim holds and is
asserted unconditionally.
"""
from __future__ import annotations

import importlib

import pytest

from tan.planner_root import bind_sdk_root
from tests.conftest import sdk_root

#: No dependency on `tan.planner`, direct or transitive -- import with no SDK
#: checkout involved at all, bound or on `sys.path`.
_SELF_CONTAINED_MODULES = [
    "tan.model.manifest", "tan.model.package", "tan.model.tensorio",
    "tan.model.adapters", "tan.model.adapters.cpu", "tan.model.adapters.deepx",
    "tan.model.adapters.drpai", "tan.model.adapters.ethos_u",
    "tan.model.adapters.executorch",
]

#: Pull `resolve_soc_path` from `tan.planner.som_metadata`, directly
#: (`targets`) or transitively (`build` imports `targets`) -- importing
#: either needs a bound, real alp-sdk checkout.
_PLANNER_COUPLED_MODULES = ["tan.model.targets", "tan.model.build"]

SDK = sdk_root()

_NO_SDK_REASON = (
    "ALP_SDK_ROOT is not set (or does not point at a real alp-sdk checkout) "
    "-- tan.model.targets/build import tan.planner.som_metadata, and "
    "tan.planner's __init__ reads real metadata/registries/* content at "
    "import time, so a throwaway bind is not enough. A SKIP about the "
    "missing root, not a pass."
)


@pytest.mark.parametrize("name", _SELF_CONTAINED_MODULES)
def test_module_imports_without_an_sdk_on_syspath(name):
    """No `PYTHONPATH=<sdk>/scripts` anywhere: the engine is tan's now."""
    assert importlib.import_module(name) is not None


@pytest.mark.skipif(SDK is None, reason=_NO_SDK_REASON)
@pytest.mark.parametrize("name", _PLANNER_COUPLED_MODULES)
def test_planner_coupled_module_imports_against_a_bound_sdk(name):
    bind_sdk_root(SDK)
    assert importlib.import_module(name) is not None


@pytest.mark.skipif(SDK is None, reason=_NO_SDK_REASON)
def test_targets_uses_tans_own_soc_path_resolver():
    bind_sdk_root(SDK)
    from tan.model import targets
    from tan.planner.som_metadata import resolve_soc_path
    assert targets.resolve_soc_path is resolve_soc_path
