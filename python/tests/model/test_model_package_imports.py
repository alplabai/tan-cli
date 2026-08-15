# SPDX-License-Identifier: Apache-2.0
"""`tan.model` is self-contained: it imports nothing from an alp-sdk checkout.

`targets.py` pulls `resolve_soc_path` from the leaf module `tan.soc_ref` (ADR-0028
Task 2 follow-up) -- NOT from `tan.planner.som_metadata`, even though that module
re-exports the same function for its own four call sites. Importing
`tan.planner.som_metadata` would still initialise the `tan.planner` PACKAGE first
(Python always runs a parent package's `__init__` before a submodule), and that
`__init__` reads REAL files off a bound alp-sdk checkout at IMPORT time -- e.g.
`tan/planner/slugs.py`'s module-scope `_PERIPHERAL_KCONFIG = peripheral_kconfig()`
reads `metadata/registries/peripheral-kconfig.json` the instant the package is
imported (measured: binding to a throwaway `tmp_path` with no real metadata content
raises `FileNotFoundError` at import, not at any function call). `tan.soc_ref` is a
leaf with no such import, which is the whole point of routing through it instead:
`tan.model.targets` and `tan.model.build` (which imports `targets`) import with NO
SDK checkout involved at all, bound or on `sys.path` -- exactly like every other
module in this package.
"""
from __future__ import annotations

import importlib

import pytest

#: No dependency on `tan.planner`, direct or transitive -- import with no SDK
#: checkout involved at all, bound or on `sys.path`.
_SELF_CONTAINED_MODULES = [
    "tan.model.manifest", "tan.model.package", "tan.model.tensorio",
    "tan.model.adapters", "tan.model.adapters.cpu", "tan.model.adapters.deepx",
    "tan.model.adapters.drpai", "tan.model.adapters.ethos_u",
    "tan.model.adapters.executorch", "tan.model.targets", "tan.model.build",
]


@pytest.mark.parametrize("name", _SELF_CONTAINED_MODULES)
def test_module_imports_without_an_sdk_on_syspath(name):
    """No `PYTHONPATH=<sdk>/scripts` anywhere: the engine is tan's now."""
    assert importlib.import_module(name) is not None


def test_targets_uses_tans_own_soc_path_resolver():
    from tan.model import targets
    from tan.soc_ref import resolve_soc_path
    assert targets.resolve_soc_path is resolve_soc_path
