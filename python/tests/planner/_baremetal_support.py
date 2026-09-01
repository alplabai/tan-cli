# SPDX-License-Identifier: Apache-2.0
"""Shared scaffolding for tan-cli#1042's two `os: baremetal` planner-branch
coverage modules.

Both of them need the same two things, and neither may own a private copy:

  * the SDK-root binding dance `tests/planner/
    test_baremetal_slice_post_commands_coverage.py` arrived at over three
    review rounds on tan-cli#1044 -- see `bind_planner_sdk_root` below, and
    that module's own fixture docstring for the full account of what each
    round got wrong;
  * a synthetic `BoardProject`/`Slice` pair that reaches the baremetal arms
    with NO alp-sdk checkout, no `board.yaml` and no `metadata/**` tree on
    disk -- see `baremetal_project`.

A second private copy of the binding logic is exactly the drift
`tests/gates/test_shared_helpers_have_one_definition.py` exists to prevent
one directory over, so both live here once.

Nothing in this module may import `tan.planner` (or any submodule of it) at
module scope: `tan/planner/paths.py` evaluates `REPO = sdk_root()` at ITS OWN
import time and raises when no root is bound yet. Every planner import in
this file and in its two consumers is therefore deliberately made INSIDE a
function, after `bind_planner_sdk_root` has run.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from tan import planner_root
from tests.conftest import sdk_root as _real_sdk_root

# Captured at COLLECTION time (module import), before `tests/conftest.py`'s
# per-test, autouse `_scrub_sdk_discovery_env` fixture deletes `ALP_SDK_ROOT`
# from the process environment -- `sdk_root()`'s own docstring requires the
# module-level call for exactly this reason.
SDK = _real_sdk_root()


def bind_planner_sdk_root(tmp_path: Path) -> None:
    """Bind the planner's SDK root, preferring the REAL bound checkout.

    Verbatim in behaviour with `test_baremetal_slice_post_commands_coverage.
    py::_bound_sdk_root`'s round-3 shape (tan-cli#1044), which is the only
    shape that survived review; the short version of why:

      * NEVER force an unbind. `monkeypatch.setattr(planner_root, "_BOUND",
        None)` restores the VARIABLE at teardown but cannot un-freeze an
        already-imported `tan.planner.paths`, whose module-level `REPO` /
        `METADATA_ROOT` were evaluated once, at import, from whatever was
        bound at that instant. Round 1 did that and reddened 14 sibling
        tests.
      * "Reuse whatever is already bound" is not enough either. `tests/core`
        sorts before `tests/planner`, and `tests/core/
        test_flow_d_manifest_fields.py::_bind_stub_sdk_root` leaves a NON-SDK
        stub bound (harmlessly, for itself -- it never imports the real
        `tan.planner`). Reusing that stub and then importing the real
        `tan.planner` under it froze `REPO` at the stub for the whole
        process, after which every later `bind_sdk_root(<real SDK>)` raised
        `PlannerRootError`. Round 2 did that and produced `18 passed,
        122 errors`.
      * So: bind the REAL checkout FIRST when there is one and
        `tan.planner` has not been imported yet. `bind_sdk_root` permits
        exactly this rebind -- it only refuses a rebind to a DIFFERENT root
        once `"tan.planner" in sys.modules` -- so it wins over a stub bound
        earlier in the session and is a correct no-op when the real root is
        already bound. Only with no real root (the default, unbound `gates`
        job) or with `tan.planner` already imported does this fall back to
        reusing what is bound, or to a throwaway `tmp_path` stub.

    Which of the three roots wins is inert to every assertion in the two
    consumer modules: each one pins `BoardProject.metadata_root` explicitly
    (see `baremetal_project`), and every path they assert on is anchored on
    the test's own `base_dir`, so `_tokenize` resolves it against
    `${PROJECT_ROOT}` and never consults `REPO` at all.
    """
    if SDK is not None and "tan.planner" not in sys.modules:
        planner_root.bind_sdk_root(SDK)
        return
    try:
        planner_root.sdk_root()
    except planner_root.PlannerRootError:
        root = SDK if SDK is not None else tmp_path / "fake-sdk-root"
        root.mkdir(parents=True, exist_ok=True)
        planner_root.bind_sdk_root(root)


def baremetal_project(
    base_dir: Path,
    *,
    os_: str = "baremetal",
    sku: str = "E1M-AEN801",
    family: str = "aen",
    board_name: Optional[str] = None,
    capabilities: Optional[dict[str, Any]] = None,
    unpopulated: Optional[list[str]] = None,
    core_id: str = "m55_he",
    app: Optional[str] = "./src",
    toolchain: Optional[str] = None,
    build_dir: Optional[str] = "build/m55_he-baremetal",
):
    """A synthetic `(BoardProject, Slice)` that reaches the planner's
    baremetal arms with nothing on disk but `base_dir/src/`.

    `metadata_root` is pinned to `base_dir / "metadata"` -- a directory that
    deliberately does NOT exist. Everything the baremetal arms read out of it
    tolerates that by design and this is checked, not assumed:
    `resolve_capabilities` resolves the SoC JSON through `resolve_soc_path`,
    which returns None for a preset with no `silicon:` at all (so `soc_caps`
    is `{}` and only the `capabilities:` passed here survive), and
    `libraries.baremetal_cmake_args` short-circuits on an empty selection
    before it loads a single manifest. Pinning it matters even so: without
    it, `BoardProject.effective_metadata_root()` falls through to
    `paths.METADATA_ROOT` -- the process-global frozen at import time -- and
    the emitted cache args would depend on WHICH alp-sdk checkout (if any)
    the session happened to bind. That is the pollution hazard
    `bind_planner_sdk_root` above documents, arriving by the other door.

    `build_dir` is RELATIVE on purpose: it is the shape a real plan carries,
    and it is what makes `_baremetal_output_dir` /
    `_baremetal_project_include_arg`'s `base_dir` anchoring (issue #596)
    observable at all -- an absolute one would pass whether they anchored on
    `base_dir` or on `Path.cwd()`.
    """
    from tan.planner.models import BoardProject, Slice

    som_preset: dict[str, Any] = {"family": family}
    if capabilities is not None:
        som_preset["capabilities"] = capabilities
    if unpopulated is not None:
        som_preset["silicon_capabilities"] = {"unpopulated": unpopulated}
    slice_ = Slice(
        core_id=core_id,
        os=os_,
        app=app,
        toolchain=toolchain,
        build_dir=None if build_dir is None else Path(build_dir),
    )
    project = BoardProject(
        sku=sku,
        hw_rev=None,
        board_name=board_name,
        board_hw_rev=None,
        cores={core_id: slice_},
        ipc=[],
        soc_spec={},
        som_preset=som_preset,
        board_preset=None,
        metadata_root=base_dir / "metadata",
    )
    return project, slice_
