# SPDX-License-Identifier: Apache-2.0
"""A module-scope capability probe must never raise (tan-cli#725).

These probes are called from `@pytest.mark.skipif(...)` decorators, i.e. at
import time. An exception escaping one does not fail a test -- it aborts
COLLECTION of the entire file, and pytest exits 2 having run nothing while
printing zero `FAILED` lines. Comparing "failures on this branch" against a
baseline then reports every known failure as newly passing, so the damage is
silent and points the wrong way.

`_bash_available` and `_noexec_probe` both bounded their subprocess with
`timeout=`, then caught only `OSError` -- and `subprocess.TimeoutExpired`
derives from `SubprocessError`, not `OSError`, so the single failure mode the
budget existed to bound was the one that escaped. Observed for real: a cold
Git Bash spawn on a loaded Windows host took ~10.6s against a 10s budget.

The probes are driven with a patched `subprocess.run` rather than a genuinely
slow command, so the test is hermetic and costs no wall-clock: what is under
test is the handler, not the host.
"""
import os
import shutil
import subprocess

import pytest

from tests.commands import test_completion_command as completion_mod
from tests.installers import test_installer_release_layout as installer_mod

PROBES = [
    pytest.param(completion_mod, "_bash_available", "bash", None,
                 id="bash_available"),
    pytest.param(installer_mod, "_noexec_probe", "unshare", "posix",
                 id="noexec_probe"),
]


def _raise_timeout(*_a, **kwargs):
    raise subprocess.TimeoutExpired(cmd=["probe"], timeout=kwargs.get("timeout", 10))


def _fresh(module, func_name):
    """The probe, guaranteed to actually RUN rather than answer from cache.

    `_bash_available` is `@lru_cache(maxsize=1)` and is already called at
    import time by the `skipif` decorators, so without this every assertion
    below would read that one cached value and pass or fail for reasons
    having nothing to do with the patched `subprocess.run`. Caught exactly
    that way: two of these tests initially passed against a cached `False`
    while exercising none of the code they name. `cache_clear` is also called
    on the way out so a probe cached under a patched `subprocess` cannot leak
    a bogus verdict into the rest of the session.
    """
    probe = getattr(module, func_name)
    if hasattr(probe, "cache_clear"):
        probe.cache_clear()
    return probe


@pytest.fixture(autouse=True)
def _no_cached_verdict_escapes():
    """Clear every cached probe verdict on the way OUT as well as in.

    Without this, the last test to run leaves `_bash_available` holding a
    result computed against a FAKED `subprocess.run` -- a cached `True` would
    then let the real bash completion tests run on a host whose bash was
    never actually consulted, and a cached `False` would skip them silently.
    The probes re-run for real on next use.
    """
    yield
    for param in PROBES:
        module, func_name = param.values[0], param.values[1]
        probe = getattr(module, func_name)
        if hasattr(probe, "cache_clear"):
            probe.cache_clear()


@pytest.mark.parametrize("module,func_name,tool,os_name", PROBES)
def test_probe_returns_false_instead_of_raising_on_timeout(
    module, func_name, tool, os_name, monkeypatch
):
    """The regression itself: a timed-out probe answers False, it does not
    propagate. Pre-fix this raised `TimeoutExpired` out of the call."""
    if os_name is not None:
        monkeypatch.setattr(os, "name", os_name)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", _raise_timeout)

    probe = _fresh(module, func_name)
    assert probe() is False, (
        f"{func_name} must report the host as incapable when its probe times "
        f"out, so the tests it guards SKIP -- returning anything truthy would "
        f"run them against a host that never answered")


@pytest.mark.parametrize("module,func_name,tool,os_name", PROBES)
def test_probe_still_returns_false_when_the_tool_is_absent(
    module, func_name, tool, os_name, monkeypatch
):
    """The pre-existing contract must survive the fix -- a probe that started
    absorbing timeouts is no use if it stopped detecting a missing tool."""
    if os_name is not None:
        monkeypatch.setattr(os, "name", os_name)
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)

    assert _fresh(module, func_name)() is False


@pytest.mark.parametrize("module,func_name,tool,os_name", PROBES)
def test_probe_reports_true_when_the_host_can(
    module, func_name, tool, os_name, monkeypatch
):
    """And the fix must not make every host look incapable, which would skip
    these suites everywhere and hide real breakage behind a green run."""
    if os_name is not None:
        monkeypatch.setattr(os, "name", os_name)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=["probe"], returncode=0, stdout="tan-bash-ok\n", stderr=""))

    assert _fresh(module, func_name)() is True
