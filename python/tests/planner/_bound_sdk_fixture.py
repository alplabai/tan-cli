# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1081: the shared `SDK` / `_bound_sdk` pair for a real-SDK-gated
test module that needs nothing more than "some alp-sdk checkout bound before
`tan.planner` is imported" -- no synthetic-stub fallback, no baremetal
project scaffolding, just the binding.

`SDK = sdk_root()` MUST be captured at MODULE import time, not from inside a
fixture body: `tests/conftest.py`'s autouse, function-scoped
`_scrub_sdk_discovery_env` deletes `ALP_SDK_ROOT` from the process
environment before every test function runs, and fixture bodies execute
per-test, after that scrub has already fired (`tests/conftest.py::sdk_root`'s
own docstring says so explicitly). Import happens once at collection, before
any fixture runs, which is what makes this module-level call see the real
environment. Because THIS module captures `SDK` once, at its own first
import, every consumer that does `from tests.planner._bound_sdk_fixture
import SDK, _bound_sdk` inherits a value captured at the right time -- a
consumer is not re-deriving it inside a fixture, which would silently
reintroduce the bug this note exists to rule out.

`_bound_sdk` is deliberately the thinnest possible binder: it calls
`bind_sdk_root(SDK)` and nothing else. It is NOT `bind_planner_sdk_root`
(`tests/planner/_baremetal_support.py`) and is not a substitute for it --
that one binds a REAL-OR-SYNTHETIC root for tests that must exercise the
baremetal arms with no alp-sdk checkout at all, and getting that dance right
(preferring the real bound checkout, never forcing an unbind that can't
un-freeze an already-imported `tan.planner.paths`) took three review rounds
on tan-cli#1044. This module's job is narrower on purpose: every consumer
below already SKIPS via `pytestmark = pytest.mark.skipif(SDK is None, ...)`
when no real checkout is bound, so `_bound_sdk` never has to choose a
fallback -- there is no case where `SDK` is `None` and this fixture still
runs.

WHY THIS FILE EXISTS. Fifteen test modules under `python/tests/**` each
defined their own `_bound_sdk` -- byte-identical, `bind_sdk_root(SDK);
yield` -- because pytest fixture registration is per-module (or per
`conftest.py`), so "reuse" without a shared conftest.py meant either
literally retyping the body or importing a shared definition for its side
effect. A `tests/planner/conftest.py` autouse fixture was rejected for the
same reason `_baremetal_support.py` rejects it for `bound_sdk_root`: it
would make the binding automatic for every module under `tests/planner/`,
including ones that bind differently on purpose (e.g.
`test_kconfig_nonstring_core_type.py`'s module-scoped `planner` fixture,
which SKIPS rather than binds when `SDK is None`) -- a behaviour change this
consolidation does not own. Importing this module's `_bound_sdk` into a
consumer's namespace registers it as that module's own autouse fixture
(the same `import a fixture for its side effect` idiom
`tests/commands/test_build_streaming.py` uses for `project`, and
`_baremetal_support.py` uses for `bound_sdk_root`) without touching any
module that does not opt in.

`tests/gates/test_shared_test_helpers_have_one_definition.py` seeds
`_bound_sdk` into its allow-list and fails if a second module-level
definition (in either spelling) appears anywhere under `python/tests/**`
(tan-cli#1081) -- the same protection `bind_planner_sdk_root` already had.
"""
from __future__ import annotations

import pytest

from tan.planner_root import bind_sdk_root
from tests.conftest import sdk_root

#: Captured at collection time -- see the module docstring for why this
#: cannot move into `_bound_sdk`'s body.
SDK = sdk_root()


@pytest.fixture(autouse=True)
def _bound_sdk():
    """Bind the planner's SDK root to the real bound checkout before the
    first `tan.planner` import in the session freezes `paths.REPO` /
    `paths.METADATA_ROOT` for the rest of the process.

    Every consumer of this fixture gates itself with
    `pytestmark = pytest.mark.skipif(SDK is None, ...)`, so `SDK` is never
    `None` when this body actually runs.
    """
    bind_sdk_root(SDK)
    yield
