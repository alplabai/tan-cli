# SPDX-License-Identifier: Apache-2.0
"""tan-cli#846: `tan/planner/template.py::_docs_ref` must not pin a scaffolded
README's doc links to a `v<version>` tag that does not actually resolve in the
bound checkout.

Measured drift: alp-sdk's own `scripts/alp_template.py::_docs_ref` (issue
#1508 / alp-sdk#1535, landed BETWEEN the tan-cli parity job's pinned SDK
commit and its dispatched ref) added a `_tag_resolves()` guard -- a
`metadata/sdk_version.yaml` that declares `version: 0.16.0` / `status:
released` while only the `v0.16.0-rc1` tag has been cut (the window between an
rc cut and its GA tag) must degrade to `main`, not emit a 404 link. `tan`'s
relocated copy of the same function (ported from `scripts/alp_template.py`,
NOT from `scripts/alp_orchestrate/`, which is why `parity.yml`'s planner-
relocation-freshness hash never saw this drift) lacked the guard and pinned to
`v0.16.0` regardless -- exactly the divergence
`test_the_scaffold_mode_agrees_on_stdout_through_argv` caught in CI run
32063286933 (three `[minimal]`/`[peripheral]`/`[sensor]` failures, the ref
inside the scaffolded README the only byte that differed).

This file is the hermetic, no-`ALP_SDK_ROOT`-content-dependent unit-level
proof: it builds its own throwaway git checkout under `tmp_path` rather than
reading the bound SDK's real `metadata/sdk_version.yaml` (whose `version:`/
`status:`/tag state drifts over time and would make this test's outcome
depend on when it runs). `tests/parity/test_planner_emit_parity.py::
test_the_scaffold_mode_agrees_on_stdout_through_argv` is the byte-identical,
end-to-end regression proof against the real oracle.

Importing `tan.planner.template` still needs SOME bound alp-sdk root (its
package `__init__` reads `metadata/registries/*` at import time) -- same
requirement as `test_chip_symbol_declared_guard.py` -- even though the
function under test here never touches that checkout's content.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tan.planner_root import bind_sdk_root
from tests.conftest import sdk_root

SDK = sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.template requires SOME bound "
           "root (tan/planner_root.py). A SKIP about the missing root, not a "
           "pass.",
)


@pytest.fixture(autouse=True)
def _bound_sdk():
    bind_sdk_root(SDK)
    yield


def _tmpl():
    """Imported inside the call so the module is not imported before
    `bind_sdk_root` has run (collection order)."""
    import tan.planner.template as m
    return m


def _git_checkout(tmp_path: Path, version: str, status: str, tags: list[str]) -> Path:
    """A throwaway git repo with a `metadata/sdk_version.yaml` and exactly
    `tags` cut on its single commit -- the minimum `_docs_ref` /
    `_tag_resolves` need, independent of anything the bound `ALP_SDK_ROOT`
    checkout happens to declare today."""
    root = tmp_path / "fake-sdk"
    (root / "metadata").mkdir(parents=True)
    (root / "metadata" / "sdk_version.yaml").write_text(
        f"version: {version}\nstatus:  {status}\n", encoding="utf-8")
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=root, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "test")
    run("add", "-A")
    run("commit", "-q", "-m", "seed")
    for tag in tags:
        run("tag", tag)
    return root


def test_a_released_version_whose_tag_has_not_been_cut_yet_falls_back_to_main(tmp_path):
    """The exact tan-cli#846 shape: `status: released` + `version: 0.16.0`,
    but only `v0.16.0-rc1` (an rc, not the GA tag) exists. Fails against the
    unfixed `_docs_ref` (no `_tag_resolves` guard), which returned `v0.16.0`
    unconditionally once `status == released` and `version` was set."""
    tmpl = _tmpl()
    root = _git_checkout(tmp_path, "0.16.0", "released", tags=["v0.16.0-rc1"])
    assert tmpl._docs_ref(root) == "main"


def test_a_released_version_whose_tag_has_been_cut_pins_to_it(tmp_path):
    """The tag resolving is the point of the guard -- once `v0.16.0` itself
    exists, pin to it, exactly as before the fix for the case it was always
    meant to serve."""
    tmpl = _tmpl()
    root = _git_checkout(tmp_path, "0.16.0", "released", tags=["v0.16.0"])
    assert tmpl._docs_ref(root) == "v0.16.0"


def test_a_development_checkout_stays_on_main_regardless_of_tags(tmp_path):
    """`status: development` never pins to a version tag, tag or no tag --
    unchanged by this fix, kept here so the guard's ADDITION is proven not to
    have widened what pins, only narrowed the `released` case."""
    tmpl = _tmpl()
    root = _git_checkout(tmp_path, "0.16.0", "development", tags=["v0.16.0"])
    assert tmpl._docs_ref(root) == "main"


def test_tag_resolves_is_local_only_no_network(tmp_path):
    """`_tag_resolves` must never shell a network call (`git ls-remote`, a
    fetch) -- scaffolding has to work offline. A repo with no remote at all
    proves this: if the guard tried to reach one, this would hang or error
    rather than returning a clean boolean."""
    tmpl = _tmpl()
    root = _git_checkout(tmp_path, "0.15.0", "released", tags=["v0.15.0"])
    assert tmpl._tag_resolves(root, "v0.15.0") is True
    assert tmpl._tag_resolves(root, "v9.9.9-does-not-exist") is False
