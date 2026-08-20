# SPDX-License-Identifier: Apache-2.0
"""`scripts/ci/apt-bounded.sh` must bound the STEP, not each invocation.

Ported from alp-sdk's tests/scripts/test_apt_bounded_wrapper.py (alp-sdk#1592).
A caller (a CI workflow step) calls the wrapper TWICE -- `update` then
`install`. A per-invocation budget would let a step spend 2x the intended wall
clock and let the JOB's own cap fire first, killing the wrapper before it
could report an attributed failure -- tan-cli#860's own trigger: PR #851, job
96014351754, `sudo apt-get update` printed its last output at 09:15:29 and sat
silent until the job's 60-minute cap killed it at 10:15:06, with no attributed
failure anywhere.

The deadline is therefore computed once per step and persisted in
`RUNNER_TEMP`, keyed by `GITHUB_ACTION`. These tests pin the two properties
that matter, without needing a network or a real apt.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WRAPPER = _REPO_ROOT / "scripts" / "ci" / "apt-bounded.sh"


def _why_the_wrapper_cannot_run_here() -> str:
    """Empty when this platform can execute the wrapper; else the reason.

    This suite runs on all three platforms `ci.yml`'s `python` job and
    `parity.yml`'s `python-tests` matrix cover, and the wrapper is a
    Linux-CI artefact, so two of them could never have passed:

    - `macos-latest` HAS bash but ships no `timeout` -- it is `gtimeout`, from
      coreutils, not installed by default. Surfaced as rc=127, command not
      found, on exactly the two tests that reach the timeout call.
    - `windows-latest` failed all four with an empty stderr and no deadline
      file. NOT for want of tools: Git for Windows puts both `bash.EXE` and a
      real GNU `timeout.EXE` on PATH, so a `shutil.which` check finds them and
      would run these anyway. What is missing is POSIX semantics -- the PATH
      shims below are `#!/bin/sh` files made executable with `chmod`, and NTFS
      has no executable bit for that to set.

    So the platform test is deliberately BOTH: `os.name` for the semantics the
    shims need, and `shutil.which` for the two binaries, because a Linux runner
    that silently lost `timeout` should skip loudly rather than pass vacuously.
    Neither half is redundant.

    Nothing about the wrapper's real behaviour goes unchecked either way: it
    only ever runs on `ubuntu-latest`, which is where apt exists at all and
    where every workflow it guards runs.
    """
    if os.name != "posix":
        return (
            "not a POSIX platform -- the PATH shims are `#!/bin/sh` scripts and "
            "rely on an executable bit this filesystem does not have"
        )
    missing = [tool for tool in ("bash", "timeout") if shutil.which(tool) is None]
    if missing:
        return " + ".join(missing) + " not installed"
    return ""


_CANNOT_RUN = _why_the_wrapper_cannot_run_here()
needs_the_wrappers_own_tools = pytest.mark.skipif(
    bool(_CANNOT_RUN),
    reason=f"scripts/ci/apt-bounded.sh cannot run here: {_CANNOT_RUN}",
)


def _env(tmp_path: Path, *, step: str = "teststep") -> dict[str, str]:
    env = dict(os.environ)
    env["RUNNER_TEMP"] = str(tmp_path)
    env["GITHUB_ACTION"] = step
    env["APT_STEP_BUDGET"] = "60"
    env["APT_ATTEMPT_TIMEOUT"] = "5"
    env["APT_ATTEMPTS"] = "2"
    return env


def _fake_apt(tmp_path: Path, exit_code: int = 0) -> Path:
    """PATH shims for apt-get and sudo, so no network, no root, no password.

    The `sudo` shim matters: the wrapper prefixes `sudo` whenever it is not
    root and sudo exists, and a developer box with a password-protected sudo
    would otherwise fail at the prompt (rc=1) before ever reaching apt-get --
    masking what these tests assert. The shim just execs its arguments, which
    is what passwordless sudo does on the runners.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    apt = bindir / "apt-get"
    apt.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    apt.chmod(0o755)
    sudo = bindir / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o755)
    return bindir


def test_the_wrapper_exists_and_is_executable() -> None:
    """Guard the guard: the tests below prove nothing if the path is wrong.

    `os.access(path, os.X_OK)` alone is VACUOUS on Windows: NTFS has no
    executable bit, so it returns True for an ordinary file too (README.md
    passes it just as readily as this script would) -- checked here only as
    an extra, POSIX-only signal. The real, OS-independent check is the mode
    bit git itself stores for this path in the index: a Windows-authored
    commit can silently drop +x (this repo has hit exactly that before), and
    `git ls-files -s` reports what actually ships, not what this filesystem
    happens to allow.
    """
    assert _WRAPPER.is_file(), f"{_WRAPPER} is missing"
    if os.name == "posix":
        assert os.access(_WRAPPER, os.X_OK), f"{_WRAPPER} is not executable"

    rel = _WRAPPER.relative_to(_REPO_ROOT).as_posix()
    proc = subprocess.run(
        ["git", "ls-files", "-s", rel],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0 and proc.stdout.strip(), (
        f"'git ls-files -s {rel}' found nothing -- not a tracked file in "
        f"this checkout, or git is unavailable. stderr:\n{proc.stderr}"
    )
    mode = proc.stdout.split()[0]
    assert mode == "100755", (
        f"{rel} is tracked with mode {mode}, not 100755 (executable) -- "
        f"git's own index, not os.access, is the source of truth here."
    )


@needs_the_wrappers_own_tools
def test_a_step_whose_budget_is_spent_fails_loudly_rather_than_silently(
    tmp_path: Path,
) -> None:
    """The regression that mattered most: it must NEVER exit 0 having done nothing.

    When an earlier invocation in the same step consumed the budget, the retry
    counter is untouched, so `rc` is still 0. Exiting with it would report
    SUCCESS for an apt-get that never ran -- a silent failure strictly worse
    than the hang the wrapper bounds.
    """
    # Pre-seed a deadline that has already passed, as a first invocation would
    # leave behind after spending the whole budget.
    deadline = tmp_path / "apt-bounded.teststep.deadline"
    deadline.write_text(str(int(time.time()) - 1), encoding="utf-8")

    env = _env(tmp_path)
    env["PATH"] = f"{_fake_apt(tmp_path, exit_code=0)}:{env['PATH']}"

    proc = subprocess.run(
        ["bash", str(_WRAPPER), "install", "-y", "some-package"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0, (
        "budget exhausted but the wrapper exited 0 -- a step whose install never "
        f"ran would go GREEN. stderr:\n{proc.stderr}"
    )
    assert "step budget" in proc.stderr, (
        f"the failure must say why, so CI names it. stderr:\n{proc.stderr}"
    )


@needs_the_wrappers_own_tools
def test_the_deadline_is_shared_across_invocations_in_one_step(
    tmp_path: Path,
) -> None:
    """The second invocation must inherit the first one's deadline, not restart it."""
    env = _env(tmp_path)
    env["PATH"] = f"{_fake_apt(tmp_path, exit_code=0)}:{env['PATH']}"

    subprocess.run(["bash", str(_WRAPPER), "update"], env=env,
                   capture_output=True, text=True, timeout=120, check=False)
    written = list(tmp_path.glob("apt-bounded.*.deadline"))
    assert len(written) == 1, f"expected exactly one deadline file, got {written}"
    first = written[0].read_text(encoding="utf-8")

    subprocess.run(["bash", str(_WRAPPER), "install", "-y", "pkg"], env=env,
                   capture_output=True, text=True, timeout=120, check=False)
    assert written[0].read_text(encoding="utf-8") == first, (
        "the second invocation rewrote the deadline -- each call would get a "
        "full budget again, which is the #1592 overrun"
    )


@needs_the_wrappers_own_tools
def test_a_different_step_gets_its_own_budget(tmp_path: Path) -> None:
    """Scoping is per step: one step's spent budget must not starve the next."""
    env_a = _env(tmp_path, step="step-a")
    env_a["PATH"] = f"{_fake_apt(tmp_path, exit_code=0)}:{env_a['PATH']}"
    subprocess.run(["bash", str(_WRAPPER), "update"], env=env_a,
                   capture_output=True, text=True, timeout=120, check=False)

    env_b = _env(tmp_path, step="step-b")
    env_b["PATH"] = env_a["PATH"]
    proc = subprocess.run(["bash", str(_WRAPPER), "update"], env=env_b,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"a fresh step must get a fresh budget. stderr:\n{proc.stderr}"
    )
    assert len(list(tmp_path.glob("apt-bounded.*.deadline"))) == 2


@needs_the_wrappers_own_tools
def test_a_real_apt_error_is_not_retried(tmp_path: Path) -> None:
    """Only timeout (124) and apt-transient (100) retry; a real error surfaces."""
    env = _env(tmp_path, step="step-err")
    env["PATH"] = f"{_fake_apt(tmp_path, exit_code=7)}:{env['PATH']}"
    proc = subprocess.run(["bash", str(_WRAPPER), "install", "-y", "pkg"],
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 7, (
        f"a non-transient exit must pass through unchanged, got {proc.returncode}"
    )
    assert "not retrying" in proc.stderr


def _hanging_apt(tmp_path: Path) -> Path:
    """An apt-get that never returns on its own -- the actual trickle case
    tan-cli#860/alp-sdk#1575 measured (a mirror sending one byte every 20s
    outlasts anything short of an external kill). Only `timeout` inside the
    wrapper can end this; nothing about the fake process exits by itself."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    apt = bindir / "apt-get"
    apt.write_text("#!/bin/sh\nsleep 999\n", encoding="utf-8")
    apt.chmod(0o755)
    sudo = bindir / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o755)
    return bindir


@needs_the_wrappers_own_tools
def test_a_hung_apt_get_is_actually_killed_by_the_deadline(tmp_path: Path) -> None:
    """The mutation-sensitive proof: this only passes if `timeout` in the
    wrapper is real and wired to `apt-get`, not merely present in a comment.

    Every other test in this file drives the wrapper through its own
    accounting (a pre-seeded deadline, a fast-exiting fake apt-get) without
    ever letting a real hang run long enough to prove the OS-level bound
    fires. This one does: `apt-get` sleeps 999s and NOTHING but the
    wrapper's `timeout --signal=TERM ...` call can end it early. Asserting
    wall-clock completion well under that 999s is the non-vacuous half --
    removing (or defanging) `timeout` from apt-bounded.sh turns this from a
    ~1s pass into a hang past the test's own 20s safety timeout.
    """
    env = _env(tmp_path, step="hang-step")
    # APT_STEP_BUDGET must clear the wrapper's own `remaining <= 10` pre-flight
    # guard (apt-bounded.sh's belt-and-braces check before even trying an
    # attempt) -- a budget at or under 10 exits via THAT branch without ever
    # invoking `timeout`/apt-get at all, which would make this test pass
    # whether or not `timeout` is wired up. 15/3/1 clears the guard (remaining
    # ~15 > 10) and still bounds the one real attempt to 3s.
    env["APT_STEP_BUDGET"] = "15"
    env["APT_ATTEMPT_TIMEOUT"] = "3"
    env["APT_ATTEMPTS"] = "1"
    env["PATH"] = f"{_hanging_apt(tmp_path)}:{env['PATH']}"

    started = time.monotonic()
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update"],
        env=env, capture_output=True, text=True, timeout=20,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 10, (
        f"apt-bounded.sh took {elapsed:.1f}s against a 999s-sleeping apt-get "
        f"and a 3s attempt timeout -- `timeout` is not actually bounding the "
        f"call. stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert proc.returncode == 124, (
        f"a killed attempt must report rc=124 (what `timeout` itself returns), "
        f"got {proc.returncode}. stderr:\n{proc.stderr}"
    )


def _fake_apt_and_hanging_dpkg(tmp_path: Path, apt_exit_code: int) -> Path:
    """apt-get exits (retryably) fast; dpkg --configure -a never returns on
    its own -- the recovery call tan-cli#860 review found unwrapped, able to
    block forever on the same dpkg lock apt-get just failed to get."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    apt = bindir / "apt-get"
    apt.write_text(f"#!/bin/sh\nexit {apt_exit_code}\n", encoding="utf-8")
    apt.chmod(0o755)
    dpkg = bindir / "dpkg"
    dpkg.write_text("#!/bin/sh\nsleep 999\n", encoding="utf-8")
    dpkg.chmod(0o755)
    sudo = bindir / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o755)
    return bindir


@needs_the_wrappers_own_tools
def test_a_hung_dpkg_recovery_is_actually_killed_by_the_deadline(tmp_path: Path) -> None:
    """The mutation-sensitive proof for the dpkg recovery specifically: this
    only passes if `timeout "$APT_DPKG_TIMEOUT"` in the wrapper is real and
    wired to `dpkg --configure -a`, not merely present in a comment.

    apt-get always exits 100 (retryable) so a second attempt -- and with it
    the pre-retry `dpkg --configure -a` -- is reached at all. `dpkg` sleeps
    999s; nothing but the wrapper's own `timeout` can end it. Asserting
    wall-clock completion well under that 999s is the non-vacuous half --
    removing (or defanging) `timeout` around the dpkg call turns this from a
    fast pass into a hang past the test's own safety timeout.
    """
    env = _env(tmp_path, step="dpkg-hang-step")
    env["APT_STEP_BUDGET"] = "30"
    env["APT_ATTEMPT_TIMEOUT"] = "2"
    env["APT_ATTEMPTS"] = "2"
    env["APT_DPKG_TIMEOUT"] = "2"
    env["PATH"] = f"{_fake_apt_and_hanging_dpkg(tmp_path, apt_exit_code=100)}:{env['PATH']}"

    started = time.monotonic()
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "install", "-y", "pkg"],
        env=env, capture_output=True, text=True, timeout=25,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 20, (
        f"apt-bounded.sh took {elapsed:.1f}s against a 999s-sleeping dpkg "
        f"and a 2s APT_DPKG_TIMEOUT -- the dpkg recovery's `timeout` is not "
        f"actually bounding the call. stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # apt-get itself is never retried past its own attempts here (both exit
    # 100), so the wrapper's final report is that exhaustion, not the dpkg
    # kill -- the point of this test is the ELAPSED bound above, not rc.
    assert proc.returncode == 100, (
        f"expected the exhausted-retries rc from the always-100 fake "
        f"apt-get, got {proc.returncode}. stderr:\n{proc.stderr}"
    )
