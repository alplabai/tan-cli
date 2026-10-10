# SPDX-License-Identifier: Apache-2.0
"""`scripts/ci/apt-bounded.sh`'s tan-cli#1257 OPTION 2 fallback: bullseye-only,
reactive, PER-SOURCE repointing of apt sources from `deb.debian.org` to
`archive.debian.org` once the former has genuinely stopped serving a specific
repository URI.

Split out of `test_apt_bounded_wrapper.py` (rather than appended there) so
neither file trips `tests/gates/test_module_size_budget.py`'s 800-line cap.
`sources.list.d`/deb822 coverage lives in
`test_apt_bounded_archive_fallback_formats.py`, which imports the shared
fixtures from here, for the same reason.

REVIEW HISTORY WORTH KNOWING: the first cut of this fallback (i) rewrote
EVERY `deb.debian.org` source line as one blanket operation regardless of
which one actually 404'd, and (ii) matched on any `404 Not Found` substring
anywhere in apt's output. A review caught both as HIGH-severity: (i) could
turn a recoverable state (only `main` down, `security` still serving) into a
guaranteed FATAL by needlessly moving `security` onto an archive that does
not carry it; (ii) could fire on a by-hash package-index 404 or a pool `.deb`
404 during `install`, neither of which means the repository itself is gone.
The tests below are shaped around proving BOTH are fixed, not just that the
happy path still works.
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
# The fallback's OWN announcement text -- distinct from the Check-Valid-Until
# waiver's NOTICE (landed separately in #1273), which ALSO names "1257" and
# "NOTICE" on every bullseye run regardless of whether this fallback ever
# fires. Asserting on this marker, not on "1257"/"NOTICE" alone, is what a
# reviewer asked for (a bare "1257" check is satisfied by the waiver's own
# message and proves nothing about this fallback specifically).
_ARCHIVE_FALLBACK_MARKER = "archive.debian.org fallback"

# The real, measured contents of python:3.12-slim-bullseye's ONLY flat sources
# file (measured 2026-09-25 against
# `python@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db`,
# the exact digest release.yml/clean-host.yml pin).
_REAL_BULLSEYE_SOURCES_LIST = (
    "deb http://deb.debian.org/debian bullseye main\n"
    "deb http://deb.debian.org/debian-security bullseye-security main\n"
    "deb http://deb.debian.org/debian bullseye-updates main\n"
)


def _apt_sources_root(tmp_path: Path, *, name: str = "apt-root") -> Path:
    """A throwaway `/etc/apt`-shaped tree: `<root>/sources.list` only."""
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "sources.list").write_text(_REAL_BULLSEYE_SOURCES_LIST, encoding="utf-8")
    return root


def _write_apt_shim(tmp_path: Path, body: str, *, bindir_name: str = "archive-bin") -> Path:
    """PATH shims for `apt-get`/`sudo` running the caller-supplied POSIX `sh`
    `body`. Every invocation is counted in `<bindir>/calls.log` (one line
    each), so a test can assert exactly how many attempts actually ran --
    written from Python, so `body`'s own single-quoted heredocs (matching
    apt's real, single-quoted error text) need no shell-escaping at all.
    """
    bindir = tmp_path / bindir_name
    bindir.mkdir(exist_ok=True)
    calls_log = bindir / "calls.log"
    apt = bindir / "apt-get"
    apt.write_text(f'#!/bin/sh\necho called >> "{calls_log}"\n{body}\n', encoding="utf-8")
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
) -> dict[str, str]:
    env = _env(tmp_path, step=step)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["APT_OS_RELEASE_FILE"] = str(os_release)
    env["APT_SOURCES_ROOT"] = str(sources_root)
    env["FAKE_APT_SOURCES_FILE"] = str(sources_root / "sources.list")
    return env


# All three suites 404 on deb.debian.org together (Debian's own usual pattern
# for retiring an EOL release), matching the real, line-scoped apt error text
# measured live against python:3.12-slim-bullseye. Once every occurrence is
# rewritten to archive.debian.org, a second call succeeds -- unless the
# archive-security continuation branch below is also reached.
_ALL_THREE_GONE_ON_DEB = """
src="$FAKE_APT_SOURCES_FILE"
if grep -q 'deb\\.debian\\.org/debian ' "$src" 2>/dev/null; then
  cat <<'EOF'
Ign:1 http://deb.debian.org/debian bullseye InRelease
Ign:2 http://deb.debian.org/debian-security bullseye-security InRelease
Ign:3 http://deb.debian.org/debian bullseye-updates InRelease
Err:4 http://deb.debian.org/debian bullseye Release
  404  Not Found [IP: 127.0.0.1 8081]
Err:5 http://deb.debian.org/debian-security bullseye-security Release
  404  Not Found [IP: 127.0.0.1 8081]
Err:6 http://deb.debian.org/debian bullseye-updates Release
  404  Not Found [IP: 127.0.0.1 8081]
Reading package lists...
E: The repository 'http://deb.debian.org/debian bullseye Release' does not have a Release file.
E: The repository 'http://deb.debian.org/debian-security bullseye-security Release' does not have a Release file.
E: The repository 'http://deb.debian.org/debian bullseye-updates Release' does not have a Release file.
EOF
  exit 100
fi
if [ "${FAKE_APT_SECURITY_STILL_404:-0}" = "1" ] && grep -q 'archive\\.debian\\.org/debian-security' "$src" 2>/dev/null; then
  cat <<'EOF'
Get:1 http://archive.debian.org/debian bullseye InRelease [116 kB]
Err:2 http://archive.debian.org/debian-security bullseye-security Release
  404  Not Found [IP: 151.101.130.132 80]
E: The repository 'http://archive.debian.org/debian-security bullseye-security Release' does not have a Release file.
EOF
  exit 100
fi
echo "Fetched 8706 kB in 2s (1000 kB/s)"
echo "Reading package lists..."
exit 0
"""

# H1: ONLY main 404s; security is still served (a `Hit:` line, no error) --
# the reviewer's own example of the blanket-rewrite defect. `bullseye-updates`
# shares the IDENTICAL URI field with `bullseye` in the real sources.list
# (both are `http://deb.debian.org/debian`, differing only in the suite
# keyword), so it legitimately moves together with `main` under a per-URI
# match -- that is the granularity a sources.list line offers, not a leak of
# the per-source fix. `security` uses a DIFFERENT URI field
# (`.../debian-security`) and must stay completely alone.
_ONLY_MAIN_GONE_SECURITY_STILL_SERVES = """
src="$FAKE_APT_SOURCES_FILE"
if grep -q 'deb\\.debian\\.org/debian ' "$src" 2>/dev/null; then
  cat <<'EOF'
Err:1 http://deb.debian.org/debian bullseye Release
  404  Not Found [IP: 127.0.0.1 8081]
Hit:2 http://deb.debian.org/debian-security bullseye-security InRelease
Hit:3 http://deb.debian.org/debian bullseye-updates InRelease
Reading package lists...
E: The repository 'http://deb.debian.org/debian bullseye Release' does not have a Release file.
EOF
  exit 100
fi
echo "Fetched 8706 kB in 2s (1000 kB/s)"
echo "Reading package lists..."
exit 0
"""

# H2: a by-hash package-INDEX 404 during `update` -- a real, distinct apt
# failure shape that is NOT "the repository has no Release file". Must not
# rewrite.
_BY_HASH_404_DURING_UPDATE = """
cat <<'EOF'
Hit:1 http://deb.debian.org/debian bullseye InRelease
Hit:2 http://deb.debian.org/debian-security bullseye-security InRelease
Hit:3 http://deb.debian.org/debian bullseye-updates InRelease
Err:4 http://deb.debian.org/debian bullseye/main amd64 Packages
  404  Not Found [IP: 127.0.0.1 8081]
E: Failed to fetch http://deb.debian.org/debian/dists/bullseye/main/binary-amd64/by-hash/SHA256/deadbeef  404  Not Found [IP: 127.0.0.1 8081]
E: Some index files failed to download. They have been ignored, or old ones used instead.
EOF
exit 100
"""

# H2: a pool `.deb` 404 during `install` -- neither the Release-file message
# nor the `update` subcommand this fallback requires. Must not rewrite.
_POOL_DEB_404_DURING_INSTALL = """
cat <<'EOF'
Err:1 http://deb.debian.org/debian bullseye/main amd64 binutils amd64 2.35.2-2
  404  Not Found [IP: 127.0.0.1 8081]
E: Failed to fetch http://deb.debian.org/debian/pool/main/b/binutils/binutils_2.35.2-2_amd64.deb  404  Not Found [IP: 127.0.0.1 8081]
E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?
EOF
exit 100
"""

# H2: a `Hit:` (success) line for deb.debian.org sitting right next to a 404
# for something else entirely -- proves the trigger no longer matches "any
# `Hit: deb.debian.org` line plus an unrelated 404 anywhere in the output",
# only the exact "does not have a Release file" line.
_HIT_PLUS_UNRELATED_404 = """
cat <<'EOF'
Hit:1 http://deb.debian.org/debian bullseye InRelease
Hit:2 http://deb.debian.org/debian-security bullseye-security InRelease
Hit:3 http://deb.debian.org/debian bullseye-updates InRelease
Err:4 http://example.invalid/unrelated/thing.deb
  404  Not Found [IP: 203.0.113.1 80]
E: Failed to fetch http://example.invalid/unrelated/thing.deb  404  Not Found [IP: 203.0.113.1 80]
E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?
EOF
exit 100
"""

# A generic transient failure: rc=100, apt-shaped, names neither host nor the
# Release-file phrasing.
_TRANSIENT_100_NO_SIGNATURE = """
echo "W: Failed to fetch some/index/file  Connection timed out"
exit 100
"""


@needs_the_wrappers_own_tools
def test_all_three_suites_gone_triggers_rewrite_then_retry_then_success(tmp_path: Path) -> None:
    """The fix itself, end to end: every deb.debian.org suite 404s, the
    wrapper repoints every one of them, retries, and succeeds -- and both the
    stderr NOTICE and the GitHub `::warning::` carry the fallback's own,
    distinct marker (not just a bare "1257"/"NOTICE", which the pre-existing
    Check-Valid-Until waiver also produces on every bullseye run).
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _ALL_THREE_GONE_ON_DEB)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="all-three-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    rewritten = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert _DEB_HOST not in rewritten, f"deb.debian.org survived the rewrite:\n{rewritten}"
    assert rewritten.count(_ARCHIVE_HOST) == 3, f"got:\n{rewritten}"
    assert _ARCHIVE_FALLBACK_MARKER in proc.stderr, f"stderr:\n{proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert "::warning::" in combined and _ARCHIVE_FALLBACK_MARKER in combined, f"combined:\n{combined}"


@needs_the_wrappers_own_tools
def test_only_the_reported_uri_is_rewritten_security_stays_on_deb(tmp_path: Path) -> None:
    """H1 (the more serious of the two HIGH findings): only `main` 404s,
    `security` still serves on deb.debian.org -- and must stay there
    untouched, not get swept onto an archive that (today) does not carry it.
    A blanket rewrite would have moved security too and turned this
    recoverable state into a guaranteed FATAL.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _ONLY_MAIN_GONE_SECURITY_STILL_SERVES)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="only-main-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"main+updates should have succeeded from archive.debian.org (both "
        f"carry it, measured 2026-09-25). stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    rewritten = (sources_root / "sources.list").read_text(encoding="utf-8")
    lines = rewritten.splitlines()
    security_line = next(line for line in lines if "security" in line)
    assert security_line == "deb http://deb.debian.org/debian-security bullseye-security main", (
        f"security moved even though it was never reported as gone -- exactly "
        f"the blanket-rewrite defect a reviewer caught. sources.list:\n{rewritten}"
    )
    assert rewritten.count(_ARCHIVE_HOST) == 2, (
        f"main and bullseye-updates share the identical URI field in the real "
        f"sources.list, so both move together under a per-URI match -- that "
        f"is the granularity a sources.list line offers. got:\n{rewritten}"
    )


@needs_the_wrappers_own_tools
def test_a_by_hash_404_during_update_does_not_rewrite(tmp_path: Path) -> None:
    """H2: a by-hash package-index 404 is a real, different apt failure shape
    -- it must not be mistaken for "the repository has no Release file".
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _BY_HASH_404_DURING_UPDATE)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="by-hash-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 100, f"stderr:\n{proc.stderr}"
    untouched = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert untouched == _REAL_BULLSEYE_SOURCES_LIST, f"got:\n{untouched}"
    assert _ARCHIVE_FALLBACK_MARKER not in proc.stderr, f"stderr:\n{proc.stderr}"


@needs_the_wrappers_own_tools
def test_a_pool_deb_404_during_install_does_not_rewrite(tmp_path: Path) -> None:
    """H2: a pool `.deb` 404 during `install` fails BOTH the message-shape
    gate and the subcommand gate -- a rewrite here could not even help the
    invocation that triggered it, since `install` never re-fetches Release
    files the way `update` does.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _POOL_DEB_404_DURING_INSTALL)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="pool-deb-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "install", "-y", "binutils"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 100, f"stderr:\n{proc.stderr}"
    untouched = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert untouched == _REAL_BULLSEYE_SOURCES_LIST, f"got:\n{untouched}"
    assert _ARCHIVE_FALLBACK_MARKER not in proc.stderr, f"stderr:\n{proc.stderr}"


@needs_the_wrappers_own_tools
def test_a_hit_line_alongside_an_unrelated_404_does_not_rewrite(tmp_path: Path) -> None:
    """H2: a successful `Hit:` line for deb.debian.org sitting next to some
    OTHER host's 404 must not be read as "deb.debian.org is gone" -- the
    trigger is the exact "does not have a Release file" line, never a raw
    substring scan for a host name plus a 404 anywhere in the blob.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _HIT_PLUS_UNRELATED_404)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="hit-plus-unrelated-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 100, f"stderr:\n{proc.stderr}"
    untouched = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert untouched == _REAL_BULLSEYE_SOURCES_LIST, f"got:\n{untouched}"
    assert _ARCHIVE_FALLBACK_MARKER not in proc.stderr, f"stderr:\n{proc.stderr}"


@needs_the_wrappers_own_tools
def test_a_transient_100_without_the_release_file_signature_does_not_rewrite(tmp_path: Path) -> None:
    """rc=100 alone is not the trigger -- apt reports an ordinary transient
    failure the same way. Only the exact "does not have a Release file" line
    is.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _TRANSIENT_100_NO_SIGNATURE)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="transient-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 100, f"stderr:\n{proc.stderr}"
    untouched = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert untouched == _REAL_BULLSEYE_SOURCES_LIST, f"got:\n{untouched}"
    assert _ARCHIVE_FALLBACK_MARKER not in proc.stderr, f"stderr:\n{proc.stderr}"


@needs_the_wrappers_own_tools
def test_a_hung_apt_get_on_bullseye_does_not_rewrite(tmp_path: Path) -> None:
    """rc=124 (timeout) is explicitly NOT the trigger. A hang must exhaust via
    `timeout` exactly as before, with `sources.list` untouched.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, "sleep 999\n")
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="hang-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    env["APT_STEP_BUDGET"] = "15"
    env["APT_ATTEMPT_TIMEOUT"] = "3"
    env["APT_ATTEMPTS"] = "1"
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update"],
        env=env, capture_output=True, text=True, timeout=25,
    )
    assert proc.returncode == 124, f"stderr:\n{proc.stderr}"
    untouched = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert untouched == _REAL_BULLSEYE_SOURCES_LIST, f"got:\n{untouched}"
    assert _ARCHIVE_FALLBACK_MARKER not in proc.stderr, f"stderr:\n{proc.stderr}"


@needs_the_wrappers_own_tools
def test_a_crlf_os_release_still_gets_the_archive_fallback(tmp_path: Path) -> None:
    """The CRLF trim that makes the Check-Valid-Until waiver work on a CRLF
    `os-release` must equally cover this fallback -- it shares the same
    `$APT_OS_CODENAME` derivation, but this pins it rather than assuming.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _ALL_THREE_GONE_ON_DEB)
    osr = _bullseye_os_release(tmp_path, crlf=True)
    env = _archive_env(tmp_path, step="crlf-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}"
    rewritten = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert _DEB_HOST not in rewritten, f"CRLF os-release left the fallback a no-op:\n{rewritten}"


@needs_the_wrappers_own_tools
def test_a_non_bullseye_host_never_rewrites_even_given_the_release_file_signature(tmp_path: Path) -> None:
    """The gate is the codename, not the message shape. A bookworm/ubuntu
    host that somehow saw this exact apt error (a broken mirror, say) must
    still not acquire the bullseye-only fallback.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _ALL_THREE_GONE_ON_DEB)
    osr = _os_release(tmp_path, _OS_RELEASE_BASE + "VERSION_CODENAME=bookworm\n")
    env = _archive_env(tmp_path, step="non-bullseye-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    env["APT_ATTEMPTS"] = "2"
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 100, f"stderr:\n{proc.stderr}"
    untouched = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert untouched == _REAL_BULLSEYE_SOURCES_LIST, f"got:\n{untouched}"
    combined = proc.stdout + proc.stderr
    assert _ARCHIVE_FALLBACK_MARKER not in combined, f"combined:\n{combined}"


@needs_the_wrappers_own_tools
def test_the_rewrite_is_idempotent_across_two_wrapper_invocations(tmp_path: Path) -> None:
    """A real step calls this wrapper TWICE -- `update` then `install` -- as
    two SEPARATE processes sharing the same on-disk sources.list.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _ALL_THREE_GONE_ON_DEB)
    osr = _bullseye_os_release(tmp_path)

    env1 = _archive_env(tmp_path, step="idempotent-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    proc1 = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env1, capture_output=True, text=True, timeout=120,
    )
    assert proc1.returncode == 0, f"stderr:\n{proc1.stderr}"
    after_first = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert after_first.count(_ARCHIVE_HOST) == 3

    env2 = _archive_env(tmp_path, step="idempotent-step", sources_root=sources_root, os_release=osr, bindir=bindir)
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
    assert "archive.archive" not in after_second, f"got:\n{after_second}"
    assert _ARCHIVE_FALLBACK_MARKER not in proc2.stderr, (
        f"the second invocation re-announced a fallback it never needed to "
        f"perform. stderr:\n{proc2.stderr}"
    )


@needs_the_wrappers_own_tools
def test_security_still_404_on_archive_fails_loudly_and_names_the_exact_uri(tmp_path: Path) -> None:
    """M1 + THE DECISION for bullseye-security still being 404 on
    archive.debian.org (measured 2026-09-25): fail loudly, name the exact
    failing URI, never silently drop the suite, and never exhaust every
    remaining attempt retrying a mirror that will not answer.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _ALL_THREE_GONE_ON_DEB)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="security-404-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    env["FAKE_APT_SECURITY_STILL_404"] = "1"
    env["APT_ATTEMPTS"] = "5"  # generous, so exhaustion isn't what stops it
    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 100, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "FATAL" in proc.stderr and "1257" in proc.stderr, f"stderr:\n{proc.stderr}"
    assert "http://archive.debian.org/debian-security" in proc.stderr, (
        f"the FATAL message must name the EXACT failing URI, not just the "
        f"host -- a bare 'archive.debian.org' substring check would also "
        f"pass on the rewrite NOTICE alone. stderr:\n{proc.stderr}"
    )
    calls = (bindir / "calls.log").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2, (
        f"expected exactly 2 apt-get invocations before the wrapper refused "
        f"to keep retrying a dead mirror -- got {len(calls)} against "
        f"APT_ATTEMPTS=5, meaning it kept retrying instead of failing "
        f"immediately. stderr:\n{proc.stderr}"
    )


@needs_the_wrappers_own_tools
def test_the_shared_step_budget_still_governs_the_archive_fallback(tmp_path: Path) -> None:
    """The fallback must not grant itself extra time outside the wrapper's
    existing shared-deadline accounting.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _ALL_THREE_GONE_ON_DEB)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="budget-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    deadline = tmp_path / "apt-bounded.budget-step.deadline"
    deadline.write_text(str(int(time.time()) - 1), encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "step budget" in proc.stderr, f"stderr:\n{proc.stderr}"
    assert not (bindir / "calls.log").exists(), (
        f"apt-get was invoked at all despite an already-spent budget. "
        f"stderr:\n{proc.stderr}"
    )
    untouched = (sources_root / "sources.list").read_text(encoding="utf-8")
    assert untouched == _REAL_BULLSEYE_SOURCES_LIST, f"got:\n{untouched}"


@needs_the_wrappers_own_tools
def test_apt_own_output_reaches_stdout_on_both_success_and_failure(tmp_path: Path) -> None:
    """M2, mutation-sensitive: deleting the wrapper's
    `printf '%s\\n' "$attempt_output"` must turn THIS test red. Every other
    test in this file only checks the wrapper's OWN messages (which go to
    stderr via `echo ... >&2` regardless of that printf), so none of them
    would notice apt's own output silently vanishing.
    """
    sources_root = _apt_sources_root(tmp_path)
    bindir = _write_apt_shim(tmp_path, _ALL_THREE_GONE_ON_DEB)
    osr = _bullseye_os_release(tmp_path)

    env_fail = _archive_env(tmp_path, step="stdout-fail-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    env_fail["APT_ATTEMPTS"] = "1"
    proc_fail = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env_fail, capture_output=True, text=True, timeout=120,
    )
    assert "does not have a Release file" in proc_fail.stdout, (
        f"apt's own failure text never reached stdout. stdout:\n{proc_fail.stdout}"
    )
    assert "does not have a Release file" not in proc_fail.stderr, (
        f"apt's own text should be on stdout, not duplicated onto stderr by "
        f"the wrapper's own messages. stderr:\n{proc_fail.stderr}"
    )

    sources_root2 = _apt_sources_root(tmp_path, name="apt-root-2")
    bindir2 = _write_apt_shim(tmp_path, "echo 'Fetched 1 kB in 1s (1 kB/s)'\nexit 0\n", bindir_name="archive-bin-2")
    env_ok = _archive_env(tmp_path, step="stdout-ok-step", sources_root=sources_root2, os_release=osr, bindir=bindir2)
    proc_ok = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env_ok, capture_output=True, text=True, timeout=120,
    )
    assert proc_ok.returncode == 0
    assert "Fetched 1 kB" in proc_ok.stdout, f"stdout:\n{proc_ok.stdout}"


@needs_the_wrappers_own_tools
def test_an_empty_sources_root_does_not_crash_on_bash_3_2(tmp_path: Path) -> None:
    """Regression for a follow-up review finding: `local candidates=()`
    followed by a bare `for f in "${candidates[@]}"` raises a raw
    `unbound variable` under `set -u` on bash < 4.4 (this repo's own macOS
    test host ships 3.2.57) whenever the array stays empty -- no
    `sources.list`, no `sources.list.d` entry at all. Reproduced directly on
    this host before the fix (`bash: candidates[@]: unbound variable`, rc
    127); must now fall through to the L2 "nothing was rewritten" NOTICE
    instead of crashing the wrapper.
    """
    empty_root = tmp_path / "apt-root-empty"
    empty_root.mkdir()
    # Unconditional, unlike `_ALL_THREE_GONE_ON_DEB`: this fixture has no
    # `FAKE_APT_SOURCES_FILE` for a gate to read at all (there is nothing
    # under `empty_root` to grep), so the shim just always reports gone --
    # the point here is the WRAPPER's own search over an empty candidate
    # list, not apt's output shape.
    body = """
cat <<'EOF'
Err:4 http://deb.debian.org/debian bullseye Release
  404  Not Found [IP: 127.0.0.1 8081]
E: The repository 'http://deb.debian.org/debian bullseye Release' does not have a Release file.
EOF
exit 100
"""
    bindir = _write_apt_shim(tmp_path, body)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="empty-root-step", sources_root=empty_root, os_release=osr, bindir=bindir)
    env["APT_ATTEMPTS"] = "2"

    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert "unbound variable" not in proc.stderr, (
        f"the bash 3.2 empty-array crash is back. stderr:\n{proc.stderr}"
    )
    assert proc.returncode == 100, (
        f"an empty sources root should exhaust normally (nothing to rewrite), "
        f"not crash. stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "nothing was rewritten" in proc.stderr, f"stderr:\n{proc.stderr}"


@needs_the_wrappers_own_tools
def test_a_trailing_slash_in_sources_list_still_matches_apts_unslashed_uri(tmp_path: Path) -> None:
    """apt's own error line never carries a trailing slash (measured), but a
    sources-file field authored with one names the same repository. The
    comparison must tolerate exactly that one-character difference, and the
    rewritten field must keep its OWN trailing slash rather than adopt
    whatever the (unslashed) apt-reported URI happened to look like.
    """
    root = tmp_path / "apt-root-trailing-slash"
    root.mkdir()
    (root / "sources.list").write_text(
        "deb http://deb.debian.org/debian/ bullseye main\n", encoding="utf-8"
    )
    body = """
src="$FAKE_APT_SOURCES_FILE"
if grep -qF -- 'deb.debian.org/debian' "$src" 2>/dev/null; then
  cat <<'EOF'
Err:4 http://deb.debian.org/debian bullseye Release
  404  Not Found [IP: 127.0.0.1 8081]
E: The repository 'http://deb.debian.org/debian bullseye Release' does not have a Release file.
EOF
  exit 100
fi
echo "Fetched ok"
exit 0
"""
    bindir = _write_apt_shim(tmp_path, body)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="trailing-slash-step", sources_root=root, os_release=osr, bindir=bindir)

    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    rewritten = (root / "sources.list").read_text(encoding="utf-8")
    assert rewritten == "deb http://archive.debian.org/debian/ bullseye main\n", (
        f"expected the host swapped and the field's OWN trailing slash kept. "
        f"got:\n{rewritten!r}"
    )
