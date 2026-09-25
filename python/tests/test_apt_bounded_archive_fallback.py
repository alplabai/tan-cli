# SPDX-License-Identifier: Apache-2.0
"""`scripts/ci/apt-bounded.sh`'s tan-cli#1257 OPTION 2 fallback: bullseye-only,
reactive repointing of apt sources from `deb.debian.org` to
`archive.debian.org` once the former has genuinely stopped serving a suite.

Split out of `test_apt_bounded_wrapper.py` (rather than appended there) so
neither file trips `tests/gates/test_module_size_budget.py`'s 800-line cap --
this fallback is a distinct, self-contained concern with its own fixtures,
and shares only a handful of helpers with the original file, imported below
rather than duplicated.

Distinct from the Check-Valid-Until waiver tested in
`test_apt_bounded_wrapper.py` -- that one waives an EXPIRY; this one engages
only when deb.debian.org has genuinely stopped serving a suite (a 404 /
"does not have a Release file", confirmed against a live `apt-get update` in
`python:3.12-slim-bullseye` whose deb.debian.org traffic was proxied to a
404-only stub, 2026-09-25).

THE SEAM IS `APT_SOURCES_ROOT`, a directory, not a behaviour flag -- same
footing as `APT_OS_RELEASE_FILE`. It changes WHERE the rewrite looks, never
WHETHER it engages, so a non-bullseye host has no ambient-variable path to the
archive fallback.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from tests.test_apt_bounded_wrapper import (
    _OS_RELEASE_BASE,
    _WRAPPER,
    _env,
    _os_release,
    needs_the_wrappers_own_tools,
)

_ARCHIVE_HOST = "archive.debian.org"
_DEB_HOST = "deb.debian.org"
# Distinct from a bare "1257" substring check: the Check-Valid-Until waiver's
# own NOTICE (added by #1257 option 1, already landed) ALSO names the issue,
# on every bullseye run regardless of the archive fallback -- so "1257 not in
# stderr" is never a valid way to assert "the archive fallback did not fire".
# This is the fallback's OWN, distinct announcement text.
_ARCHIVE_FALLBACK_MARKER = "no longer serves this bullseye suite"

# The real, measured contents of python:3.12-slim-bullseye's ONLY sources
# file (`sources.list.d/` is empty there, no deb822 `.sources` -- measured
# 2026-09-25 against `python@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db`,
# the exact digest release.yml/clean-host.yml pin).
_REAL_BULLSEYE_SOURCES_LIST = (
    "deb http://deb.debian.org/debian bullseye main\n"
    "deb http://deb.debian.org/debian-security bullseye-security main\n"
    "deb http://deb.debian.org/debian bullseye-updates main\n"
)


def _apt_sources_root(tmp_path: Path, *, name: str = "apt-root") -> Path:
    """A throwaway `/etc/apt`-shaped tree: `<root>/sources.list` only.

    `APT_SOURCES_ROOT` points the wrapper at this instead of a real `/etc/apt`
    -- these tests never touch this host's actual apt configuration.
    """
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "sources.list").write_text(_REAL_BULLSEYE_SOURCES_LIST, encoding="utf-8")
    return root


def _fake_apt_archive(
    tmp_path: Path,
    *,
    mode: str,
    bindir_name: str = "archive-bin",
) -> Path:
    """PATH shims for `apt-get`/`sudo` that react to the CONTENTS of the
    sources file (read from `$FAKE_APT_SOURCES_FILE`), not a canned exit code
    -- so the wrapper's own rewrite is what has to make a second attempt
    succeed, not a test double pretending it did.

    `mode`:
      - "deb_404": deb.debian.org present -> the real 404-shaped apt output
        (see the module docstring) and rc=100. Once rewritten to
        archive.debian.org, succeeds -- UNLESS `security_still_404_...` (see
        below), which additionally 404s on the archive security line too, so
        the fail-loud path can be exercised.
      - "transient_100": always rc=100 with a generic, apt-shaped message
        that names neither host nor the 404/"no Release file" phrasing --
        the "rc 100 without the signature" case that must NOT rewrite.
      - "hang": never returns on its own (`sleep 999`); only the wrapper's
        own `timeout` can end it -- the rc=124 case that must NOT rewrite.

    Every invocation is counted in `<bindir>/calls.log` (one line each), so a
    test can assert exactly how many attempts actually ran.
    """
    bindir = tmp_path / bindir_name
    bindir.mkdir(exist_ok=True)
    calls_log = bindir / "calls.log"
    apt = bindir / "apt-get"
    if mode == "deb_404":
        apt.write_text(
            "#!/bin/sh\n"
            f'echo called >> "{calls_log}"\n'
            'src="$FAKE_APT_SOURCES_FILE"\n'
            "if grep -q 'deb\\.debian\\.org' \"$src\" 2>/dev/null; then\n"
            "  cat <<'EOF'\n"
            "Ign:1 http://deb.debian.org/debian bullseye InRelease\n"
            "Err:4 http://deb.debian.org/debian bullseye Release\n"
            "  404  Not Found [IP: 127.0.0.1 8081]\n"
            "Reading package lists...\n"
            "E: The repository 'http://deb.debian.org/debian bullseye Release' does not have a Release file.\n"
            "EOF\n"
            "  exit 100\n"
            "fi\n"
            "if [ \"${FAKE_APT_SECURITY_STILL_404:-0}\" = \"1\" ] && "
            "grep -q 'archive\\.debian\\.org/debian-security' \"$src\" 2>/dev/null; then\n"
            "  cat <<'EOF'\n"
            "Get:1 http://archive.debian.org/debian bullseye InRelease [75.1 kB]\n"
            "Err:2 http://archive.debian.org/debian-security bullseye-security Release\n"
            "  404  Not Found [IP: 151.101.2.132 80]\n"
            "E: The repository 'http://archive.debian.org/debian-security bullseye-security Release' does not have a Release file.\n"
            "EOF\n"
            "  exit 100\n"
            "fi\n"
            'echo "Fetched 8706 kB in 2s (1000 kB/s)"\n'
            'echo "Reading package lists..."\n'
            "exit 0\n",
            encoding="utf-8",
        )
    elif mode == "transient_100":
        apt.write_text(
            "#!/bin/sh\n"
            f'echo called >> "{calls_log}"\n'
            'echo "W: Failed to fetch some/index/file  Connection timed out"\n'
            "exit 100\n",
            encoding="utf-8",
        )
    elif mode == "hang":
        apt.write_text(
            f'#!/bin/sh\necho called >> "{calls_log}"\nsleep 999\n',
            encoding="utf-8",
        )
    else:
        raise ValueError(f"unknown mode {mode!r}")
    apt.chmod(0o755)
    sudo = bindir / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o755)
    return bindir


def _bullseye_os_release(tmp_path: Path, *, crlf: bool = False, name: str = "os-release-bullseye") -> Path:
    body = "ID=debian\nVERSION_CODENAME=bullseye\n"
    if crlf:
        body = body.replace("\n", "\r\n")
    path = tmp_path / name
    path.write_bytes(body.encode("utf-8"))
    return path


def _archive_env(
    tmp_path: Path,
    *,
    step: str,
    sources_root: Path,
    os_release: Path,
    bindir: Path,
    security_still_404: bool = False,
) -> dict[str, str]:
    env = _env(tmp_path, step=step)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["APT_OS_RELEASE_FILE"] = str(os_release)
    env["APT_SOURCES_ROOT"] = str(sources_root)
    env["FAKE_APT_SOURCES_FILE"] = str(sources_root / "sources.list")
    if security_still_404:
        env["FAKE_APT_SECURITY_STILL_404"] = "1"
    return env


@needs_the_wrappers_own_tools
def test_a_404_shaped_failure_on_bullseye_triggers_rewrite_then_retry_then_success(
    tmp_path: Path,
) -> None:
    """The fix itself, end to end: deb.debian.org 404s, the wrapper repoints
    `sources.list` at archive.debian.org, retries, and succeeds -- and both
    the plain-stderr NOTICE and the GitHub `::warning::` line name #1257.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _fake_apt_archive(tmp_path, mode="deb_404")
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(
        tmp_path, step="archive-404-step", sources_root=sources_root,
        os_release=osr, bindir=bindir,
    )
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"the retry against archive.debian.org should have succeeded. "
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    rewritten = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert _DEB_HOST not in rewritten, (
        f"deb.debian.org survived the rewrite:\n{rewritten}"
    )
    assert rewritten.count(_ARCHIVE_HOST) == 3, (
        f"expected all three `deb` lines repointed at archive.debian.org, "
        f"got:\n{rewritten}"
    )
    assert "1257" in proc.stderr and "NOTICE" in proc.stderr, (
        f"the fallback must announce itself on stderr, naming the issue. "
        f"stderr:\n{proc.stderr}"
    )
    combined = proc.stdout + proc.stderr
    assert "::warning::" in combined and "1257" in combined, (
        f"a GitHub `::warning::` line naming #1257 must be emitted. "
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


@needs_the_wrappers_own_tools
def test_a_crlf_os_release_still_gets_the_archive_fallback(tmp_path: Path) -> None:
    """The CRLF trim that makes the Check-Valid-Until waiver work on a CRLF
    `os-release` must equally cover the archive fallback -- it shares the
    same `$APT_OS_CODENAME` derivation, but this pins it rather than assuming
    the sharing holds.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _fake_apt_archive(tmp_path, mode="deb_404")
    osr = _bullseye_os_release(tmp_path, crlf=True)
    env = _archive_env(
        tmp_path, step="archive-crlf-step", sources_root=sources_root,
        os_release=osr, bindir=bindir,
    )
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}"
    rewritten = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert _DEB_HOST not in rewritten, f"CRLF os-release left the fallback a no-op:\n{rewritten}"


@needs_the_wrappers_own_tools
def test_a_hung_apt_get_on_bullseye_does_not_rewrite(tmp_path: Path) -> None:
    """rc=124 (timeout) is explicitly NOT the trigger -- only a genuine
    404-shaped rc=100 is. A hang must exhaust via `timeout` exactly as before,
    with `sources.list` untouched.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _fake_apt_archive(tmp_path, mode="hang")
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(
        tmp_path, step="archive-hang-step", sources_root=sources_root,
        os_release=osr, bindir=bindir,
    )
    env["APT_STEP_BUDGET"] = "15"
    env["APT_ATTEMPT_TIMEOUT"] = "3"
    env["APT_ATTEMPTS"] = "1"
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update"],
        env=env, capture_output=True, text=True, timeout=25,
    )
    assert proc.returncode == 124, f"stderr:\n{proc.stderr}"
    untouched = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert untouched == _REAL_BULLSEYE_SOURCES_LIST, (
        f"a plain timeout rewrote sources.list -- it must not. got:\n{untouched}"
    )
    assert _ARCHIVE_FALLBACK_MARKER not in proc.stderr, f"stderr:\n{proc.stderr}"


@needs_the_wrappers_own_tools
def test_a_transient_100_without_the_404_signature_does_not_rewrite(tmp_path: Path) -> None:
    """rc=100 alone is not the trigger -- apt reports an ordinary transient
    failure the same way. Only the specific "gone" phrasing is.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _fake_apt_archive(tmp_path, mode="transient_100")
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(
        tmp_path, step="archive-transient-step", sources_root=sources_root,
        os_release=osr, bindir=bindir,
    )
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 100, (
        f"a transient failure with no 404 signature must exhaust normally, "
        f"not be turned into a rewrite loop. stderr:\n{proc.stderr}"
    )
    untouched = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert untouched == _REAL_BULLSEYE_SOURCES_LIST, (
        f"a plain transient rc=100 rewrote sources.list -- it must not. "
        f"got:\n{untouched}"
    )
    assert _ARCHIVE_FALLBACK_MARKER not in proc.stderr, f"stderr:\n{proc.stderr}"


@needs_the_wrappers_own_tools
def test_a_non_bullseye_host_never_rewrites_even_given_404_shaped_output(
    tmp_path: Path,
) -> None:
    """The gate is the codename, not the output. A bookworm/ubuntu host that
    somehow saw this exact 404 phrasing (a broken mirror, say) must still not
    acquire the bullseye-only archive fallback -- there is no ambient-variable
    path to it (`APT_SOURCES_ROOT` only says WHERE to look, never WHETHER).
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _fake_apt_archive(tmp_path, mode="deb_404")
    osr = _os_release(tmp_path, _OS_RELEASE_BASE + "VERSION_CODENAME=bookworm\n")
    env = _archive_env(
        tmp_path, step="archive-non-bullseye-step", sources_root=sources_root,
        os_release=osr, bindir=bindir,
    )
    env["APT_ATTEMPTS"] = "2"
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 100, f"stderr:\n{proc.stderr}"
    untouched = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert untouched == _REAL_BULLSEYE_SOURCES_LIST, (
        f"a non-bullseye host rewrote sources.list -- it must never. "
        f"got:\n{untouched}"
    )
    combined = proc.stdout + proc.stderr
    assert _ARCHIVE_FALLBACK_MARKER not in combined and "::warning::" not in combined, (
        f"a non-bullseye host announced the #1257 archive fallback -- it "
        f"must not even consider it. stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


@needs_the_wrappers_own_tools
def test_the_rewrite_is_idempotent_across_two_wrapper_invocations(tmp_path: Path) -> None:
    """A real step calls this wrapper TWICE -- `update` then `install` -- as
    two SEPARATE processes sharing the same on-disk sources.list. The second
    invocation must see the first one's rewrite already in place and must not
    double-transform it (no `archive.archive.debian.org`, no re-announcement).
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _fake_apt_archive(tmp_path, mode="deb_404")
    osr = _bullseye_os_release(tmp_path)

    env1 = _archive_env(
        tmp_path, step="idempotent-step", sources_root=sources_root,
        os_release=osr, bindir=bindir,
    )
    proc1 = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env1, capture_output=True, text=True, timeout=120,
    )
    assert proc1.returncode == 0, f"stderr:\n{proc1.stderr}"
    after_first = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert after_first.count(_ARCHIVE_HOST) == 3

    # A fresh process, same step key -- exactly what a real `update`-then-
    # `install` step does. Sources are already archive.debian.org-only, so
    # the fake apt-get's own "if deb.debian.org present" branch never fires
    # and it succeeds on the FIRST call, unaided by any rewrite.
    env2 = _archive_env(
        tmp_path, step="idempotent-step", sources_root=sources_root,
        os_release=osr, bindir=bindir,
    )
    proc2 = subprocess.run(
        ["bash", str(_WRAPPER), "install", "-y", "binutils"],
        env=env2, capture_output=True, text=True, timeout=120,
    )
    assert proc2.returncode == 0, f"stderr:\n{proc2.stderr}"
    after_second = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert after_second == after_first, (
        f"the second invocation changed sources.list -- not idempotent.\n"
        f"after first update:\n{after_first}\nafter install:\n{after_second}"
    )
    assert "archive.archive" not in after_second, (
        f"a double rewrite produced a mangled host:\n{after_second}"
    )
    assert _ARCHIVE_FALLBACK_MARKER not in proc2.stderr, (
        f"the second invocation re-announced a fallback it never needed to "
        f"perform. stderr:\n{proc2.stderr}"
    )


@needs_the_wrappers_own_tools
def test_security_still_404_on_archive_fails_loudly_instead_of_dropping_it(
    tmp_path: Path,
) -> None:
    """THE DECISION for bullseye-security still being 404 on archive.debian.org
    (measured 2026-09-25): fail loudly, never silently drop the suite. After
    the rewrite, archive.debian.org itself has no Release file for
    bullseye-security -- the wrapper must stop immediately with a named
    error, not exhaust its remaining attempts retrying a dead mirror, and
    must not report bare exhaustion (which would obscure why).
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _fake_apt_archive(tmp_path, mode="deb_404")
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(
        tmp_path, step="archive-security-404-step", sources_root=sources_root,
        os_release=osr, bindir=bindir, security_still_404=True,
    )
    env["APT_ATTEMPTS"] = "5"  # generous, so exhaustion isn't what stops it
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0, (
        f"security still 404 on the archive but the wrapper exited 0. "
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "FATAL" in proc.stderr and "1257" in proc.stderr, (
        f"the failure must be named, not a generic exhaustion message. "
        f"stderr:\n{proc.stderr}"
    )
    assert "archive.debian.org" in proc.stderr, f"stderr:\n{proc.stderr}"
    calls = (bindir / "calls.log").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2, (
        f"expected exactly 2 apt-get invocations (the deb.debian.org 404, "
        f"then the archive.debian.org retry that also 404s) before the "
        f"wrapper refused to keep retrying a dead mirror -- got {len(calls)} "
        f"against APT_ATTEMPTS=5, meaning it kept retrying instead of "
        f"failing immediately. stderr:\n{proc.stderr}"
    )
    # rc=100 both times; the immediate-refusal branch re-exits with that rc
    # rather than inventing a new one.
    assert proc.returncode == 100, f"stderr:\n{proc.stderr}"


@needs_the_wrappers_own_tools
def test_the_shared_step_budget_still_governs_the_archive_fallback(
    tmp_path: Path,
) -> None:
    """The fallback must not grant itself extra time outside the wrapper's
    existing shared-deadline accounting. Pre-seeding an already-spent
    deadline (exactly `test_a_step_whose_budget_is_spent_fails_loudly...` in
    `test_apt_bounded_wrapper.py` does for the plain case) must make the
    wrapper give up at its very first budget check -- BEFORE even calling
    apt-get once -- even though this attempt would otherwise have hit the 404
    signature and tried to rewrite.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _fake_apt_archive(tmp_path, mode="deb_404")
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(
        tmp_path, step="archive-budget-step", sources_root=sources_root,
        os_release=osr, bindir=bindir,
    )
    deadline = tmp_path / "apt-bounded.archive-budget-step.deadline"
    deadline.write_text(str(int(time.time()) - 1), encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "step budget" in proc.stderr, (
        f"the pre-existing budget guard must still be what fires here, not "
        f"the archive fallback bypassing it. stderr:\n{proc.stderr}"
    )
    assert not (bindir / "calls.log").exists(), (
        f"apt-get was invoked at all despite an already-spent budget -- the "
        f"fallback logic let an attempt through the pre-flight guard. "
        f"stderr:\n{proc.stderr}"
    )
    untouched = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert untouched == _REAL_BULLSEYE_SOURCES_LIST, (
        f"sources.list was rewritten despite apt-get never having run. "
        f"got:\n{untouched}"
    )
