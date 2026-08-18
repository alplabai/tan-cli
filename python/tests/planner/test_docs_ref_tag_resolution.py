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
relocated copy of the same function (ported from `scripts/alp_template.py`)
lacked the guard and pinned to `v0.16.0` regardless -- exactly the divergence
`test_the_scaffold_mode_agrees_on_stdout_through_argv` caught in CI run
32063286933 (three `[minimal]`/`[peripheral]`/`[sensor]` failures, the ref
inside the scaffolded README the only byte that differed).

`scripts/alp_template.py` IS watched -- it is pinned in
`HAND_PORT_HASHES` (`tests/gates/test_planner_relocation_freshness.py:667`)
and `parity.yml` runs a dispatch-only alarm over exactly that table
(`parity.yml:1038-1058`). The reason THIS drift still reached CI unflagged is
not "this file is unwatched"; it is threefold: (1) `parity.yml`'s always-run
`tests/gates` step (`parity.yml:904-906`) binds `ALP_SDK_HAND_PORT_ROOT` to a
checkout FROZEN at `HAND_PORT_PINNED_SDK_COMMIT`, so it can only catch the
table being internally inconsistent (a mistyped hash), never a real upstream
change -- it compares the pin against itself; (2) `python-tests`
(`parity.yml:1982`), the one job bound to the actually-dispatched ref, runs
`pytest --ignore=tests/gates`, so `test_planner_relocation_freshness.py`
never runs there at all; (3) the one step that DOES compare the live
dispatched ref against `HAND_PORT_HASHES` (`parity.yml:1038-1058`) is
`continue-on-error: true` and reports through `::warning::` -- a log
annotation, not a required check, so nobody was paged by it. All three
would have to fail to notice for this to have shipped unnoticed; they did.

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

import os
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
    """A minimal git checkout carrying `metadata/sdk_version.yaml` and
    `tags` -- enough for `_docs_ref` to read and for `git rev-parse` to
    answer against. One empty commit, because a tag needs an object.

    Mirrors alp-sdk `tests/scripts/test_alp_template.py::_fake_sdk_checkout`
    on purpose, `--allow-empty` included: `git add -A` + a real commit would
    stage `metadata/sdk_version.yaml` through whatever `core.excludesFile`
    the HOST git config declares, and a host that globally ignores
    `metadata/` or `*.yaml` (not implausible in this monorepo) would make
    `git commit` here silently commit nothing, or -- under `check=True` --
    raise on "nothing added to commit" instead of seeding the repo the test
    needs. `commit --allow-empty` never looks at the working tree or the
    index, so no exclude pattern can touch it. Identity comes from
    `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env vars rather than `git config
    user.*`, for the same reason: no dependency on what the host repo's own
    config (which `-C <path>` still inherits for anything not overridden
    locally) happens to already carry."""
    root = tmp_path / "fake-sdk"
    (root / "metadata").mkdir(parents=True)
    (root / "metadata" / "sdk_version.yaml").write_text(
        f"version: {version}\nstatus:  {status}\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", "x"],
                   check=True, env=env)
    for tag in tags:
        subprocess.run(["git", "-C", str(root), "tag", tag], check=True, env=env)
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


def test_docs_ref_is_main_outside_a_git_checkout(tmp_path):
    """Ported verbatim from alp-sdk `tests/scripts/test_alp_template.py::
    test_docs_ref_is_main_outside_a_git_checkout`. A tarball export or
    `--no-tags` clone has the metadata but no refs -- the commonest customer
    install shape, and the one `_git_checkout`'s helper above never exercises
    (it always seeds a real `.git`). `_tag_resolves` must degrade to `main`,
    never raise -- an exception here would abort the whole scaffold over a
    README link."""
    tmpl = _tmpl()
    root = tmp_path / "tarball"
    (root / "metadata").mkdir(parents=True)
    (root / "metadata" / "sdk_version.yaml").write_text(
        "version: 0.16.0\nstatus:  released\n", encoding="utf-8")
    assert tmpl._docs_ref(root) == "main"
