# SPDX-License-Identifier: Apache-2.0
"""The installers must install whatever shape the RESOLVED release publishes.

tan-cli#356. #349 switched the release to PyInstaller ``--onedir`` archives and
both installers then requested the new names UNCONDITIONALLY --
``tan-<triple>.tar.gz`` from ``install.sh``, ``tan-<triple>.zip`` from
``install.ps1``. At the time this was written, no published tag had those
assets, so the documented install command 404'd on every tag that existed:
``v0.4.1`` (what ``latest`` resolved to then) and the ``v0.5.0-rc4``
pre-release both published RAW binaries. ``v0.5.0`` and ``v0.5.1`` have since
been cut and both publish the archive shape; ``latest`` now resolves to
``v0.5.1``.

The fixture releases below mirror the REAL published asset lists name for name
(``gh release view <tag> --repo alplabai/tan-cli --json assets``, read while
writing this), so a pass here is a claim about the real thing rather than about
a shape invented for the test:

===============  ===========================================================
``v0.4.1``       8 raw assets -- the last Rust release
``v0.5.0-rc4``   4 raw assets -- the ``--onefile`` freeze; no musl, no
                 linux/arm64
``v0.5.0``       4 ARCHIVES -- the first published tag with this shape.
``v0.5.1``       4 ARCHIVES, same shape as ``v0.5.0``; today's ``latest``.
===============  ===========================================================

Everything is served from a local HTTP server through ``TAN_INSTALL_BASE_URL``,
so nothing here touches the network -- except the two bare-``latest`` tests,
which ask GitHub only which tag ``latest`` is and then fetch that tag from the
fixture. They skip cleanly when that answer cannot be had.

Both scripts are run for real, as scripts. Asserting on their transcripts would
not have caught #356 (the old code printed a perfectly well-formed "downloading"
line before 404ing); what is asserted is which files ended up in the install dir.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import uuid
import zipfile
from pathlib import Path
from typing import NamedTuple

import pytest

try:
    import winreg  # Windows-only stdlib module; used only by the registry-kind test below.
except ImportError:
    winreg = None

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"

#: The FIRST tag that publishes ``--onedir`` archives -- deliberately NOT
#: ``v0.5.0-rc4``, whose published assets are raw (that mistake is the
#: documentation half of #356). v0.5.0 has since been cut and does publish the
#: archive shape, matching this constant.
FIRST_ARCHIVE_TAG = "v0.5.0"

#: Every tag that exists today, all of which publish raw binaries.
RAW_TAGS = ("v0.4.1", "v0.5.0-rc4")

#: A made-up tag (never really published) whose raw asset for every platform is
#: `_write_garbage_executable` output rather than a working payload -- tan-cli#434:
#: the sha256 check passes (its checksums.txt entry is computed from these SAME
#: garbage bytes, deliberately, so the mismatch refusal never fires), and the
#: install.sh:381-403 / install.ps1:291-322 health check is the only thing left
#: to catch it. Built directly in `release_server` below, not through
#: `RELEASES`/`_build_release`, so those stay a pure mirror of real published tags.
BAD_PAYLOAD_TAG = "v9.9.9-badpayload"

#: tag -> the asset names that tag really publishes, verbatim. ``checksums.txt``
#: and ``envelope-contract.json`` are omitted from the values: the first is
#: generated below from these names, and the second is never fetched by either
#: installer.
RELEASES: dict[str, tuple[str, ...]] = {
    "v0.4.1": (
        "tan-aarch64-apple-darwin",
        "tan-aarch64-pc-windows-msvc.exe",
        "tan-aarch64-unknown-linux-gnu",
        "tan-aarch64-unknown-linux-musl",
        "tan-x86_64-apple-darwin",
        "tan-x86_64-pc-windows-msvc.exe",
        "tan-x86_64-unknown-linux-gnu",
        "tan-x86_64-unknown-linux-musl",
    ),
    "v0.5.0-rc4": (
        "tan-aarch64-apple-darwin",
        "tan-x86_64-apple-darwin",
        "tan-x86_64-pc-windows-msvc.exe",
        "tan-x86_64-unknown-linux-gnu",
    ),
    FIRST_ARCHIVE_TAG: (
        "tan-aarch64-apple-darwin.tar.gz",
        "tan-x86_64-apple-darwin.tar.gz",
        "tan-x86_64-pc-windows-msvc.zip",
        "tan-x86_64-unknown-linux-gnu.tar.gz",
    ),
    # v0.5.1 publishes the same four archives as v0.5.0 -- verified against the
    # real release, not assumed from the shape of its predecessor:
    #   gh release view v0.5.1 --repo alplabai/tan-cli --json assets
    # returns exactly these plus `checksums.txt` and `envelope-contract.json`,
    # which this dict omits by the convention documented above.
    #
    # This entry is what the bare-`latest` tests need: `latest` resolves against
    # the real GitHub, so the day a new tag ships they fail -- loudly and by
    # design -- until it is listed here. Leaving it out is not a skip, it is a
    # red on every open PR.
    "v0.5.1": (
        "tan-aarch64-apple-darwin.tar.gz",
        "tan-x86_64-apple-darwin.tar.gz",
        "tan-x86_64-pc-windows-msvc.zip",
        "tan-x86_64-unknown-linux-gnu.tar.gz",
    ),
}

#: What the fixture's POSIX executable prints, so a test can tell the payload
#: apart from the launcher script the archive layout installs in its place.
FIXTURE_VERSION_LINE = "tan 9.9.9-fixture"

_LATEST_RE = re.compile(r"latest is (\S+?)\.?$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Fixture release building
# ---------------------------------------------------------------------------
def _write_posix_executable(path: Path) -> None:
    """A shell script, not a copied binary: install.sh only ever has to *exec*
    it, and a script is the one payload that is guaranteed to run on whatever
    POSIX host the suite lands on."""
    path.write_text(f'#!/bin/sh\necho "{FIXTURE_VERSION_LINE}"\n', encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _write_windows_executable(path: Path) -> None:
    """A REAL console .exe is required, and there is no way around it:
    install.ps1 finishes by running ``& $dest --version`` and DELETES an install
    whose binary cannot run (that refusal is deliberate -- see the comment at
    the end of the script), so a batch file renamed ``.exe`` would fail the very
    check these tests need to reach.

    ``sys.executable`` is the one guaranteed-present real executable on the
    host, it answers ``--version`` with exit 0, and a bare copy of it -- no DLLs
    beside it -- still does (measured on CPython 3.12.10 for Windows). Its
    output is "Python X.Y.Z" rather than a tan version string, which is why the
    Windows assertions below are about which FILES landed, never about what the
    installed program printed.
    """
    shutil.copyfile(sys.executable, path)


def _stage_onedir(parent: Path, exe_name: str) -> Path:
    """The ``tan/`` tree a ``--onedir`` freeze archives: the executable plus
    ``_internal/``, matching ``build_binary.sh``'s
    ``shutil.make_archive(..., base_dir="tan")``."""
    root = parent / "tan"
    (root / "_internal").mkdir(parents=True)
    (root / "_internal" / "fixture-runtime.txt").write_text(
        "stands in for the PyInstaller runtime\n", encoding="utf-8", newline="\n"
    )
    exe = root / exe_name
    if exe_name.endswith(".exe"):
        _write_windows_executable(exe)
    else:
        _write_posix_executable(exe)
    return root


def _build_asset(path: Path, staging: Path) -> None:
    if path.name.endswith(".zip"):
        tree = _stage_onedir(staging, "tan.exe")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(tree.rglob("*")):
                zf.write(item, item.relative_to(tree.parent).as_posix())
    elif path.name.endswith(".tar.gz"):
        tree = _stage_onedir(staging, "tan")
        with tarfile.open(path, "w:gz") as tf:
            tf.add(tree, arcname="tan")
    elif path.name.endswith(".exe"):
        _write_windows_executable(path)
    else:
        _write_posix_executable(path)


def _write_garbage_executable(path: Path) -> None:
    """Bytes that are not a valid executable under any interpretation this
    suite's hosts understand: no ELF/PE magic and no `#!` shebang -- but
    ASCII (7-bit, no 0x80+ byte) on purpose, unlike a truly random corrupted
    download, so a `sh`/`dash` ENOEXEC fallback that echoes the "command"
    back into its "not found"/"Exec format error" message (measured: it does,
    on the raw offending bytes) can never hand this suite's own subprocess
    capture (`text=True`, strict UTF-8) something that is not valid UTF-8 --
    that would fail the TEST HARNESS with a `UnicodeDecodeError`, not exercise
    install.sh's health check. Stands in for a corrupted-or-tampered download
    -- the shape tan-cli#434's health check exists to catch -- not for a
    missing +x bit: both installers `chmod`/mark the staged file runnable
    themselves right before running it, regardless of what permission bits
    land on disk.
    """
    path.write_bytes(b"\x01\x02\x03NOT-A-VALID-EXECUTABLE\x04\x05\x06" * 8)
    path.chmod(0o755)


def _build_bad_release(tag_dir: Path, assets: tuple[str, ...]) -> None:
    """Like `_build_release`, but every asset is `_write_garbage_executable`
    output instead of a working payload -- see `BAD_PAYLOAD_TAG`."""
    tag_dir.mkdir(parents=True)
    lines = []
    for name in assets:
        asset = tag_dir / name
        _write_garbage_executable(asset)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    (tag_dir / "checksums.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _build_release(tag_dir: Path, assets: tuple[str, ...], staging: Path) -> None:
    tag_dir.mkdir(parents=True)
    lines = []
    for name in assets:
        asset = tag_dir / name
        work = staging / tag_dir.name / name
        work.mkdir(parents=True)
        _build_asset(asset, work)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        # Two spaces, exactly as sha256sum writes it and as the real published
        # checksums.txt has it -- both installers match on the SECOND
        # whitespace-separated field, so the separator is part of the contract.
        lines.append(f"{digest}  {name}")
    (tag_dir / "checksums.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A003 - silence one request per line
        pass


@pytest.fixture(scope="module")
def release_server(tmp_path_factory):
    """A local stand-in for ``https://github.com/<repo>/releases/download``.

    Module-scoped: building the fixtures copies ``sys.executable`` a handful of
    times, and every test wants the same three releases.
    """
    root = tmp_path_factory.mktemp("releases")
    staging = tmp_path_factory.mktemp("staging")
    for tag, assets in RELEASES.items():
        _build_release(root / tag, assets, staging)
    # Same asset names v0.4.1 (a RAW_TAGS entry) really publishes, one per
    # platform triple -- so BAD_PAYLOAD_TAG resolves on whichever host/arch
    # this suite happens to run on -- but every one of them is garbage bytes.
    _build_bad_release(root / BAD_PAYLOAD_TAG, RELEASES["v0.4.1"])

    handler = functools.partial(_QuietHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# Running the installers
# ---------------------------------------------------------------------------
PWSH = shutil.which("pwsh") or shutil.which("powershell")

windows_only = pytest.mark.skipif(
    os.name != "nt" or not PWSH, reason="install.ps1 needs Windows + PowerShell"
)
posix_only = pytest.mark.skipif(
    os.name == "nt", reason="install.sh refuses a Windows host and points at install.ps1"
)
# Unlike windows_only, this does NOT require os.name == "nt": Get-Win32ErrorCode
# and Test-AccessDeniedSignature are pure functions over a real
# System.ComponentModel.Win32Exception (a cross-platform .NET type -- its
# NativeErrorCode is just the int the constructor was given, no Win32 API
# involved), so they run correctly under pwsh on Linux/macOS too. GitHub-hosted
# ubuntu-latest/macos-latest/windows-latest runners all ship pwsh preinstalled.
pwsh_only = pytest.mark.skipif(not PWSH, reason="needs PowerShell (pwsh) on PATH")


def _run(
    argv: list[str], base_url: str, home: Path, extra_env: dict[str, str | None] | None = None
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "TAN_INSTALL_BASE_URL": base_url,
        # The conftest already repoints HOME/USERPROFILE at a tmp dir; pinned
        # again here because these two subprocesses would otherwise be the only
        # things in the suite that could write to a real dotfile.
        "HOME": str(home),
        "USERPROFILE": str(home),
    }
    # `None` deletes rather than sets -- the arm64-Windows Outcome-2 test needs
    # PROCESSOR_ARCHITEW6432 genuinely absent, not merely unset in this dict,
    # since install.ps1 prefers it over PROCESSOR_ARCHITECTURE when present and
    # a stray inherited value would silently pick a different arch than the one
    # the test is asking for.
    for key, value in (extra_env or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180)


def _install_ps1(base_url: str, dest: Path, home: Path, *args: str, extra_env: dict[str, str | None] | None = None):
    # -NoModifyPath, always: without it install.ps1 appends $Dir to the USER
    # Path, which is a persistent registry write on the developer's own machine
    # -- a test must not leave a pile of dead tmp dirs on someone's PATH.
    return _run(
        [
            PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(INSTALL_PS1), "-Dir", str(dest), "-NoModifyPath", *args,
        ],
        base_url,
        home,
        extra_env,
    )


def _install_sh(base_url: str, dest: Path, home: Path, *args: str, extra_env: dict[str, str | None] | None = None):
    # --no-modify-path for the same reason: the rc-file append is not what these
    # tests are about, and $HOME is redirected above anyway.
    return _run(
        ["sh", str(INSTALL_SH), "--dir", str(dest), "--no-modify-path", *args],
        base_url,
        home,
        extra_env,
    )


def _install_sh_modify_path(
    base_url: str, dest: Path, home: Path, *args: str, extra_env: dict[str, str | None] | None = None
):
    """Like `_install_sh`, but WITHOUT `--no-modify-path` -- used only by the
    two tan-cli#434 rollback tests below, which are precisely about whether
    the PATH-modifying rc-file write (install.sh:495-524) is ever reached.
    `SHELL` is pinned to `/bin/sh` so the rc file install.sh:507-513 picks
    (the `*)` default case -> `$HOME/.profile`) is deterministic regardless of
    the host's own login shell.
    """
    env: dict[str, str | None] = {"SHELL": "/bin/sh"}
    if extra_env:
        env.update(extra_env)
    return _run(["sh", str(INSTALL_SH), "--dir", str(dest), *args], base_url, home, env)


def _fake_uname(tmp_path: Path, os_name: str, arch: str) -> Path:
    """A minimal ``uname`` staged ahead of the real one on PATH, so a test can
    drive install.sh's OS/arch detection (``uname -s`` / ``uname -m``) to a
    combination the real CI runner is not -- e.g. aarch64 Linux -- without an
    actual arm64 runner. Used only to reach the tan-cli#356 Outcome-2 refusal
    for a real published tag that genuinely lacks that platform's asset; every
    OTHER test in this file exercises the real host's real arch."""
    bin_dir = tmp_path / "fake-uname-bin"
    bin_dir.mkdir()
    script = bin_dir / "uname"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  -s) echo "{os_name}" ;;\n'
        f'  -m) echo "{arch}" ;;\n'
        f'  *) echo "{os_name}" ;;\n'
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)
    return bin_dir


def _fake_sudo(unlock_target: Path, calls_log: Path) -> Path:
    """A `sudo` stub for exercising install.sh:375-393's elevated-permission
    branch (`as_root`) without real root, which is off the table in CI and in
    this sandbox alike. Real `sudo` on an unattended runner either hangs on a
    password prompt with no TTY to answer it or is not configured passwordless
    -- neither is something a test can drive.

    The trick this stub relies on needs no elevation at all: POSIX `chmod`
    requires only OWNERSHIP of the target, never the write permission bit
    itself. The same unprivileged test process that locked `unlock_target`
    down to 0555 to force the sudo branch can therefore legitimately hand its
    own write bit back and then run the wrapped command for real -- the same
    end state real `sudo` would produce, sourced from ownership instead of
    root. Every invocation (its full argv) is appended to `calls_log`, so a
    test can assert the elevation path actually ran rather than merely that
    the install succeeded.
    """
    bin_dir = unlock_target.parent / "fake-sudo-bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "sudo"
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$*" >> "{calls_log}"\n'
        f'chmod u+w "{unlock_target}" 2>/dev/null || true\n'
        'exec "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)
    return bin_dir


def _noexec_probe() -> bool:
    """Best-effort: mounting a `noexec` tmpfs needs `CAP_SYS_ADMIN`, which
    `unshare --map-root-user -m` grants inside a fresh, unprivileged user+mount
    namespace -- but some sandboxes/CI images block unprivileged user
    namespaces outright (or lack `unshare`/`mount` entirely, e.g. macOS). Used
    only to decide whether the tan-cli#490 noexec tests below can run for
    real; see their own docstrings for why a real mount is used instead of a
    permission-bit trick.
    """
    if os.name == "nt" or shutil.which("unshare") is None:
        return False
    try:
        probe = subprocess.run(
            [
                "unshare", "--map-root-user", "-m", "--", "sh", "-c",
                "d=$(mktemp -d) && mount -t tmpfs -o noexec tmpfs \"$d\"",
            ],
            capture_output=True, text=True, timeout=15,
        )
        return probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        # tan-cli#725: `TimeoutExpired` is a `SubprocessError`, NOT an
        # `OSError`, so `timeout=15` above could raise straight through this
        # handler. `_noexec_probe` runs at MODULE scope (`noexec_capable`
        # just below calls it), so that would abort COLLECTION of this whole
        # file rather than skip the tan-cli#490 tests -- pytest exiting 2
        # having run nothing and printing no `FAILED` lines at all. A
        # namespace/mount probe on a loaded or restricted host is exactly the
        # kind of call that hangs to its budget, which is why the budget is
        # here; absorbing the timeout is what makes it useful.
        #
        # Same reasoning as `_bash_available` in
        # `tests/commands/test_completion_command.py`, and the same narrowing
        # to `TimeoutExpired`: for a host-capability probe a timeout IS the
        # answer ("this host cannot, so skip"), whereas production code must
        # refuse on a timeout rather than fall back -- see
        # `test_diff_command.py::test_sdk_validator_timeout_refuses_instead_of_reporting_clean`.
        # `_bash_setlocale_warning_probe` below already had this covered via
        # `subprocess.SubprocessError`.
        return False


noexec_capable = pytest.mark.skipif(
    not _noexec_probe(),
    reason="host cannot mount a noexec tmpfs via unshare -- cannot simulate tan-cli#490's failure mode for real",
)


def _install_sh_under_noexec_tmpdir(
    base_url: str, dest: Path, home: Path, noexec_dir: Path, *args: str
) -> subprocess.CompletedProcess:
    """Runs `install.sh` with `$TMPDIR` pointed at a REAL `noexec`-mounted
    tmpfs (tan-cli#490), inside one `unshare --map-root-user -m` so the mount
    is unprivileged and vanishes with the process -- nothing persists on the
    host. A real mount is used rather than stripping the execute bit off a
    staging dir: the two are different kernel-level refusals (`noexec` blocks
    `exec(2)` outright, even for root, via the VFS mount flags; a missing
    directory search bit is a DAC permission check that root/`CAP_DAC_OVERRIDE`
    -- which `--map-root-user` grants inside the namespace -- bypasses), and
    only the mount reproduces the exact "Permission denied", exit-126 signature
    install.sh now keys off.
    """
    script = (
        "set -e\n"
        f'mount -t tmpfs -o noexec tmpfs "{noexec_dir}"\n'
        # `--map-root-user` maps this process to uid 0 inside the namespace but
        # `tar`'s default same-owner extraction still tries (and, on some
        # kernels, fails) to chown to the archive's recorded uid/gid -- a
        # userns-mapping artefact unrelated to noexec (also hit and noted the
        # same way in the tan-cli#490 report's own repro).
        'TAR_OPTIONS="--no-same-owner" '
        f'TMPDIR="{noexec_dir}" TAN_INSTALL_BASE_URL="{base_url}" '
        f'HOME="{home}" USERPROFILE="{home}" '
        f'sh "{INSTALL_SH}" --dir "{dest}" --no-modify-path {" ".join(args)}\n'
    )
    return subprocess.run(
        ["unshare", "--map-root-user", "-m", "--", "sh", "-c", script],
        capture_output=True, text=True, timeout=180,
    )


def _skip_unless_latest_is_a_fixture_tag(result: subprocess.CompletedProcess) -> None:
    """`latest` is resolved against the real GitHub, so which tag comes back is
    not this suite's to decide. SKIP -- never fail -- only when it could not be
    resolved at all (offline, or the API's 60/hr unauthenticated limit): that
    is a real external unavailability, not a gap in this suite.

    FAIL, loudly, when it resolved to a tag `RELEASES` (:69) does not carry.
    That dict is a hardcoded snapshot of what a handful of real tags publish;
    the day a new one ships and becomes `latest`, silently skipping forever is
    exactly the "a skip passes too" failure mode `ci.yml:84-87` warns about --
    both bare-`latest` tests would stop covering acceptance criterion 2 with
    nothing going red to say so.
    """
    match = _LATEST_RE.search(result.stdout)
    if not match:
        pytest.skip(f"could not resolve `latest` (offline?):\n{result.stdout}\n{result.stderr}")
    tag = match.group(1)
    if tag not in RELEASES:
        pytest.fail(
            f"`latest` now resolves to {tag}, which RELEASES "
            f"(test_installer_release_layout.py:69) does not carry -- bare-`latest` coverage "
            f"has gone stale. Add {tag} to RELEASES with its real published asset list "
            f"(`gh release view {tag} --repo alplabai/tan-cli --json assets`)."
        )


# ---------------------------------------------------------------------------
# install.ps1
# ---------------------------------------------------------------------------
@windows_only
@pytest.mark.parametrize("tag", RAW_TAGS)
def test_ps1_installs_the_raw_exe_for_a_pre_archive_tag(release_server, tmp_path, tag):
    """#356's repro, Windows half: every tag published so far ships a raw
    ``tan-<triple>.exe``, and asking for the ``.zip`` 404s."""
    dest = tmp_path / "prog"
    result = _install_ps1(release_server, dest, tmp_path, "-Version", tag)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (dest / "tan.exe").is_file()
    # No launcher and no runtime dir: there is nothing to launch on this layout,
    # and a stray tan.cmd beside tan.exe is dead weight PATHEXT never reaches.
    assert not (dest / "tan.cmd").exists()
    assert not (dest / "tan-cli-lib").exists()


@windows_only
def test_ps1_unpacks_the_archive_for_the_first_archive_tag(release_server, tmp_path):
    """The #349 layout, tested against the first tag that will actually publish
    it -- v0.5.0, not the v0.5.0-rc4 that shipped raw assets."""
    dest = tmp_path / "prog"
    result = _install_ps1(release_server, dest, tmp_path, "-Version", FIRST_ARCHIVE_TAG)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (dest / "tan.cmd").is_file()
    assert (dest / "tan-cli-lib" / "tan.exe").is_file()
    assert (dest / "tan-cli-lib" / "_internal").is_dir()
    # The archive's top-level `tan/` is RENAMED onto tan-cli-lib, never nested
    # inside it.
    assert not (dest / "tan-cli-lib" / "tan").exists()
    assert not (dest / "tan.exe").exists()


@windows_only
def test_ps1_switching_layouts_leaves_no_shadowing_leftovers(release_server, tmp_path):
    """Installing a raw tag over an archive install and back again.

    PATHEXT resolves a bare ``tan`` through .EXE before .CMD, so a tan.exe left
    beside a fresh tan.cmd would silently keep winning forever -- the reverse of
    the pre-#349 case install.ps1 already guarded. Whichever name is not being
    installed has to go, and so does the runtime dir it pointed at.
    """
    dest = tmp_path / "prog"
    assert _install_ps1(release_server, dest, tmp_path, "-Version", FIRST_ARCHIVE_TAG).returncode == 0
    assert (dest / "tan.cmd").is_file()

    result = _install_ps1(release_server, dest, tmp_path, "-Version", "v0.4.1")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (dest / "tan.exe").is_file()
    assert not (dest / "tan.cmd").exists()
    assert not (dest / "tan-cli-lib").exists()

    result = _install_ps1(release_server, dest, tmp_path, "-Version", FIRST_ARCHIVE_TAG)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (dest / "tan.cmd").is_file()
    assert (dest / "tan-cli-lib" / "tan.exe").is_file()
    assert not (dest / "tan.exe").exists()


@windows_only
def test_ps1_bare_latest_installs_whatever_shape_latest_is(release_server, tmp_path):
    """The documented one-liner takes no -Version at all. It has to work while
    `latest` is still a raw-binary release, and keep working the day it is not."""
    dest = tmp_path / "prog"
    result = _install_ps1(release_server, dest, tmp_path)
    _skip_unless_latest_is_a_fixture_tag(result)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (dest / "tan.exe").is_file() or (dest / "tan.cmd").is_file()


@windows_only
def test_ps1_refuses_when_the_checksums_cannot_be_fetched(release_server, tmp_path):
    """The fixture has no directory at all for this made-up tag, so
    `checksums.txt` 404s -- install.ps1's Outcome 1 (`could not fetch`), not
    Outcome 2 (`lists no asset ... under EITHER name`). Renamed from
    `test_ps1_refuses_a_release_with_no_asset_for_this_platform`, which is what
    this test was called before -- a name Outcome 2 never actually reaches
    through this repro, so it pinned the wrong branch under the right-sounding
    name (tan-cli#356 adversarial review, item 2). See
    `test_ps1_refuses_a_release_with_no_asset_for_this_platform` below for real
    Outcome-2 coverage.
    """
    dest = tmp_path / "prog"
    result = _install_ps1(release_server, dest, tmp_path, "-Version", "v9.9.9-does-not-exist")

    assert result.returncode != 0
    assert not dest.exists() or list(dest.iterdir()) == []
    assert "could not fetch" in (result.stdout + result.stderr)


@windows_only
def test_ps1_refuses_a_release_with_no_asset_for_this_platform(release_server, tmp_path):
    """The widened Outcome 2 (tan-cli#356), reached for real: `FIRST_ARCHIVE_TAG`
    (v0.5.0)'s real published asset list has no Windows arm64 entry under
    EITHER name -- no `tan-aarch64-pc-windows-msvc.zip`, no `.exe`. Overriding
    `PROCESSOR_ARCHITECTURE` (and clearing `PROCESSOR_ARCHITEW6432`, which
    install.ps1 prefers when set) drives the script's own arch detection there
    without an actual arm64 Windows runner -- the same mechanism a real one
    would use, since install.ps1 never probes hardware directly.

    Reverting install.ps1's whole Outcome-2 block to the pre-#356 single-name
    wording -- the defect this test exists to catch -- would leave this test
    (and its install.sh sibling) failing, unlike the misnamed test above,
    which stayed green either way.
    """
    dest = tmp_path / "prog"
    result = _install_ps1(
        release_server, dest, tmp_path, "-Version", FIRST_ARCHIVE_TAG,
        extra_env={"PROCESSOR_ARCHITECTURE": "ARM64", "PROCESSOR_ARCHITEW6432": None},
    )

    assert result.returncode != 0
    assert not dest.exists() or list(dest.iterdir()) == []
    combined = result.stdout + result.stderr
    assert "lists no asset for aarch64-pc-windows-msvc" in combined
    assert "tan-aarch64-pc-windows-msvc.zip" in combined
    assert "tan-aarch64-pc-windows-msvc.exe" in combined
    assert "there is no prebuilt Windows arm64 asset from v0.5.0 onward" in combined


@windows_only
def test_ps1_bad_payload_on_fresh_host_leaves_nothing_behind(release_server, tmp_path):
    """tan-cli#434 acceptance criterion 1, Windows half: `BAD_PAYLOAD_TAG`'s
    asset verifies (its checksums.txt digest is computed from the same garbage
    bytes) but cannot run, so the health check at install.ps1:291-322 has to
    refuse before anything lands under `$Dir` -- there is no launcher, no
    `tan.exe`, no `tan-cli-lib`. `-NoModifyPath` is used here exactly as every
    other install.ps1 test in this file uses it (see `_install_ps1`), so this
    run never touches the real User-Path registry key either way.
    """
    dest = tmp_path / "prog"
    result = _install_ps1(release_server, dest, tmp_path, "-Version", BAD_PAYLOAD_TAG)

    assert result.returncode != 0
    assert not (dest / "tan.exe").exists()
    assert not (dest / "tan.cmd").exists()
    assert not (dest / "tan-cli-lib").exists()
    combined = result.stdout + result.stderr
    assert "newly downloaded binary failed to run" in combined
    assert "no previous installation existed" in combined


@windows_only
def test_ps1_bad_payload_on_upgrade_leaves_previous_install_working(release_server, tmp_path):
    """tan-cli#434 acceptance criterion 2, Windows half: a bad upgrade over a
    GOOD install must leave the previous `tan.exe` byte-for-byte and still
    runnable, and must not leave any `.bak` behind -- the health check
    (install.ps1:291-322) refuses before the backup/commit block
    (install.ps1:324-424) ever runs, so there is nothing to roll back FROM.
    """
    dest = tmp_path / "prog"
    good = _install_ps1(release_server, dest, tmp_path, "-Version", "v0.4.1")
    assert good.returncode == 0, f"{good.stdout}\n{good.stderr}"
    exe = dest / "tan.exe"
    before_hash = hashlib.sha256(exe.read_bytes()).hexdigest()
    before_version = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30).stdout

    result = _install_ps1(release_server, dest, tmp_path, "-Version", BAD_PAYLOAD_TAG)

    assert result.returncode != 0
    assert exe.is_file()
    assert hashlib.sha256(exe.read_bytes()).hexdigest() == before_hash
    after_version = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30).stdout
    assert after_version == before_version
    assert not (dest / "tan.exe.bak").exists()
    assert not (dest / "tan.cmd.bak").exists()
    assert not (dest / "tan-cli-lib.bak").exists()
    combined = result.stdout + result.stderr
    assert "your existing installation" in combined
    assert "was never touched" in combined


@windows_only
def test_ps1_temp_execute_denied_retries_staging_inside_dest_dir(release_server, tmp_path):
    """tan-cli#490, Windows half: a host-level "no execute from here" refusal
    over the staging location -- the same shape an AppLocker / Software
    Restriction Policy "block execution from %TEMP%" rule produces
    (`CreateProcess` fails with `ERROR_ACCESS_DENIED`, which .NET surfaces as
    `Win32Exception: Access is denied`) -- must not sink the install.
    `icacls /deny (X)` reproduces that refusal for real on the staging
    directory (rather than mocking it), the same way install.sh's sibling
    test (`test_sh_noexec_tmpdir_retries_staging_inside_install_dir`) uses a
    real `noexec` mount instead of a permission-bit trick.
    """
    temp_root = tmp_path / "denied-temp"
    temp_root.mkdir()
    deny = subprocess.run(
        ["icacls", str(temp_root), "/deny", "Everyone:(OI)(CI)(X)"],
        capture_output=True, text=True, timeout=30,
    )
    assert deny.returncode == 0, f"could not set up the deny-execute ACE: {deny.stdout}\n{deny.stderr}"

    dest = tmp_path / "prog"
    result = _install_ps1(
        release_server, dest, tmp_path, "-Version", "v0.4.1",
        extra_env={"TEMP": str(temp_root), "TMP": str(temp_root)},
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (dest / "tan.exe").is_file()
    combined = result.stdout + result.stderr
    assert "security policy" in combined


@windows_only
def test_ps1_temp_execute_denied_distinguishes_from_a_broken_binary(release_server, tmp_path):
    """tan-cli#490's hard requirement, Windows half: when the retry ALSO
    fails (here, `-Dir` is put inside the SAME deny-execute directory, so
    there is no exec-able place left to stage), the failure has to say this
    is a host security policy, not the generic "missing runtime dependency /
    security software altered it" wording install.ps1 gives for an
    actually-corrupt payload.
    """
    denied_root = tmp_path / "denied"
    denied_root.mkdir()
    deny = subprocess.run(
        ["icacls", str(denied_root), "/deny", "Everyone:(OI)(CI)(X)"],
        capture_output=True, text=True, timeout=30,
    )
    assert deny.returncode == 0, f"could not set up the deny-execute ACE: {deny.stdout}\n{deny.stderr}"

    dest = denied_root / "prog"  # -Dir itself is under the same deny-execute ACE
    result = _install_ps1(
        release_server, dest, tmp_path, "-Version", "v0.4.1",
        extra_env={"TEMP": str(denied_root), "TMP": str(denied_root)},
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Access is denied" in combined
    assert "security policy" in combined
    # Must not misattribute a host security policy to a missing dependency.
    assert "may be missing a runtime dependency" not in combined


@pwsh_only
def test_ps1_access_denied_signature_accepts_the_applocker_policy_codes(tmp_path):
    """tan-cli#490 review, MAJOR 2: the two icacls-based tests above only
    reproduce an NTFS deny-execute ACE, which fails CreateProcess with
    ERROR_ACCESS_DENIED (5) -- they cannot exercise the scenario the issue
    actually names, a real AppLocker/Software Restriction Policy "block
    executables from %TEMP%" rule, because that needs live policy
    enforcement (the Application Identity service, or a Safer/SRP registry
    policy) that is not something a hosted CI runner should be made to carry
    for one assertion. That policy class fails CreateProcess with
    ERROR_ACCESS_DISABLED_BY_POLICY (1260) or, when configured with no
    user-facing notification, ERROR_ACCESS_DISABLED_NO_SAFER_UI_BY_POLICY
    (786) -- and pre-fix, Test-AccessDeniedSignature only ever matched 5, so
    neither the retry nor the "security policy" wording would ever fire
    against the issue's own named case.

    tan-cli#490 review, round 5 also paired a second, real symbol,
    ERROR_ACCESS_DISABLED_NO_SAFER_UI_BY_POLICY -- the same policy configured
    with no user-facing notification -- with the wrong number, 1261. Round 6
    checked Microsoft's system-error-codes table, found 1261 is
    ERROR_REG_NAT_CONSUMPTION (an unrelated Itanium invalid-register-value
    fault), and -- having only checked the 1000-1299 and 1300-1699 pages --
    concluded the symbol does not exist and dropped it entirely. It exists:
    it is 786 (0x312), on the 500-999 page
    (learn.microsoft.com/windows/win32/debug/system-error-codes--500-999-,
    "Access to %1 has been restricted by your Administrator by policy rule
    %2."), confirmed also against MS-ERREF
    (openspecs/windows_protocols/ms-erref, 0x00000312). Both 1260 and 786 are
    verified, real codes for this scenario and are probed below.

    This exercises the REAL discriminator function extracted verbatim out of
    install.ps1 (not a reimplementation that could silently drift from it),
    against a REAL System.ComponentModel.Win32Exception -- the same object
    type Get-Win32ErrorCode unwraps at install.ps1:381-388 -- rather than a
    bare integer, so a change to how the exception is unwrapped is covered
    too.
    """
    text = INSTALL_PS1.read_text()
    start = text.index("function Get-Win32ErrorCode(")
    end = text.index("function Invoke-HealthCheck(")
    assert start != -1 and end > start, "install.ps1's Get-Win32ErrorCode/Test-AccessDeniedSignature functions moved or were renamed"
    funcs_ps1 = tmp_path / "funcs.ps1"
    funcs_ps1.write_text(text[start:end])

    probe = tmp_path / "probe.ps1"
    probe.write_text(
        f'. "{funcs_ps1}"\n'
        'function Probe($code) {\n'
        '    if ($null -eq $code) { $exc = New-Object System.Exception "plain, no Win32Exception anywhere in the chain" }\n'
        '    else {\n'
        '        $inner = New-Object System.ComponentModel.Win32Exception -ArgumentList $code\n'
        '        $exc = New-Object System.Exception -ArgumentList "wrapped", $inner  # exercise the InnerException walk too\n'
        '    }\n'
        '    return Test-AccessDeniedSignature (Get-Win32ErrorCode $exc)\n'
        '}\n'
        'Write-Output ("5=" + (Probe 5))\n'
        'Write-Output ("1260=" + (Probe 1260))  # ERROR_ACCESS_DISABLED_BY_POLICY -- the AppLocker/SRP case #490 names\n'
        'Write-Output ("786=" + (Probe 786))    # ERROR_ACCESS_DISABLED_NO_SAFER_UI_BY_POLICY -- same policy, no user-facing notification\n'
        'Write-Output ("2="    + (Probe 2))     # ERROR_FILE_NOT_FOUND -- must NOT match\n'
        'Write-Output ("none=" + (Probe $null))\n'
    )

    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    lines = result.stdout.split()
    assert "5=True" in lines
    assert "1260=True" in lines
    assert "786=True" in lines
    assert "2=False" in lines
    assert "none=False" in lines


def test_ps1_broadcast_helper_compile_is_gated_and_guarded(tmp_path):
    """tan-cli#490 round 8 shipped `Add-Type -Namespace TanInstall ...
    -ErrorAction SilentlyContinue` at column 0 in the script -- unconditional,
    outside `if (-not $alreadyPresent)`, and outside any try/catch of its own.
    `-ErrorAction SilentlyContinue` only suppresses a NON-terminating error; a
    compile/assembly-load failure from `Add-Type` itself is TERMINATING (under
    this script's own `$ErrorActionPreference = "Stop"`), so on a host whose
    AppLocker DLL rule or Software Restriction Policy blocks compiling into
    %TEMP% -- precisely the host class the health-check retry earlier in this
    same script exists for -- `Add-Type` aborted the WHOLE script, even a run
    that never touched the Path at all (a fresh `-NoModifyPath` run, or one
    where `$Dir` was already on the Path). This is a pure text assertion --
    no `pwsh` needed, so it runs on every OS this suite runs on, including
    this Linux host that has none installed -- proving the shipped source has
    `Add-Type` (a) inside the `if (-not $alreadyPresent) { ... }` write branch
    and (b) inside its own `try { } catch { }`, both of which round 8's
    zero-test-coverage version lacked.
    """
    text = INSTALL_PS1.read_text()
    branch_start = text.index("if (-not $alreadyPresent) {")
    branch_end = text.index('if ($layout -eq "archive") {\n\tWrite-Host "install.ps1: installed tan')
    assert branch_end > branch_start, "install.ps1's Path-write branch moved or was renamed"
    branch = text[branch_start:branch_end]

    add_type_at = branch.index("Add-Type -Namespace TanInstall -Name NativeMethods")
    call_at = branch.index("[TanInstall.NativeMethods]::SendMessageTimeout")
    assert call_at > add_type_at, "the broadcast call must come after Add-Type defines the type"

    try_at = branch.rindex("try {", 0, add_type_at)
    catch_at = branch.index("} catch {", add_type_at)
    assert try_at < add_type_at < catch_at, (
        "Add-Type must be wrapped in its own try/catch -- SilentlyContinue alone does not "
        "suppress the TERMINATING error a blocked compile raises"
    )


@windows_only
def test_ps1_commit_resets_the_source_temp_acl_after_move(release_server, tmp_path):
    """tan-cli#490, MAJOR 2: `Move-Item` carries the SOURCE item's ACL into
    its new home rather than picking up the destination's -- under `-System`
    that would leave a machine-wide install writable by whichever
    unprivileged user ran the installer (a local-privilege-escalation
    shape). Repro's here without needing elevation: grant `Everyone` an
    explicit ACE on the staging %TEMP% dir (so the downloaded payload
    inherits it, the same way it would inherit the invoking user's own
    ownership/ACEs on a real host), then assert that ACE is gone from the
    installed file -- only whatever `dest`'s own parent grants should remain.
    """
    temp_root = tmp_path / "acl-temp"
    temp_root.mkdir()
    grant = subprocess.run(
        ["icacls", str(temp_root), "/grant", "Everyone:(OI)(CI)F"],
        capture_output=True, text=True, timeout=30,
    )
    assert grant.returncode == 0, f"could not set up the source ACE: {grant.stdout}\n{grant.stderr}"

    dest = tmp_path / "prog"
    result = _install_ps1(
        release_server, dest, tmp_path, "-Version", "v0.4.1",
        extra_env={"TEMP": str(temp_root), "TMP": str(temp_root)},
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    exe = dest / "tan.exe"
    assert exe.is_file()

    acl_out = subprocess.run(
        ["icacls", str(exe)], capture_output=True, text=True, timeout=30
    ).stdout
    assert "Everyone" not in acl_out, (
        f"the installed file still carries the staging %TEMP% dir's explicit ACE:\n{acl_out}"
    )


@windows_only
def test_ps1_registry_path_write_preserves_expand_sz_and_does_not_expand_vars(release_server, tmp_path):
    """tan-cli#490's highest-priority defect, and the one no test actually
    exercised: the fix reads the User/Machine Path UNEXPANDED
    (`DoNotExpandEnvironmentNames`) and writes it back with the SAME
    `RegistryValueKind` it already had, instead of round-tripping it through
    `[Environment]::GetEnvironmentVariable`/`SetEnvironmentVariable`, which
    would silently collapse a `REG_EXPAND_SZ` Path (one containing `%VAR%`
    references) to a `REG_SZ` one with those references expanded and frozen.

    Every OTHER test in this file passes `-NoModifyPath` (see `_install_ps1`)
    specifically so it never touches the real User/Machine Path on the
    machine running the suite -- which means none of them, across three
    rounds of fixes to this same file, ever ran this code at all. This test
    is the one that does, by pointing install.ps1's registry read/write at a
    disposable scratch HKCU subkey instead (a seam in `Get-PathRegistryKey`,
    `TAN_INSTALL_TEST_PATH_REGISTRY_KEY`, that exists only for this test) --
    exercising the exact registry mechanics (open, read raw, write back with
    the preserved kind) without permanently rewriting the real key in either
    hive, and without needing the elevation the Machine hive would require.
    """
    assert winreg is not None
    test_subkey = f"Software\\TanInstallTest\\{uuid.uuid4().hex}"
    hkcu = winreg.HKEY_CURRENT_USER
    seeded_path = r"%TAN_INSTALL_TEST_VAR%\bin;C:\Windows\system32"
    key = winreg.CreateKeyEx(hkcu, test_subkey, 0, winreg.KEY_ALL_ACCESS)
    try:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, seeded_path)
        winreg.CloseKey(key)

        dest = tmp_path / "prog"
        # No -NoModifyPath: this is the one test that must reach the write.
        result = _run(
            [
                PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(INSTALL_PS1), "-Dir", str(dest), "-Version", "v0.4.1",
            ],
            release_server,
            tmp_path,
            extra_env={"TAN_INSTALL_TEST_PATH_REGISTRY_KEY": test_subkey},
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert (dest / "tan.exe").is_file()

        read_key = winreg.OpenKey(hkcu, test_subkey, 0, winreg.KEY_READ)
        try:
            new_value, new_kind = winreg.QueryValueEx(read_key, "Path")
        finally:
            winreg.CloseKey(read_key)

        # The kind must survive the round-trip -- this is the defect itself:
        # SetEnvironmentVariable always writes REG_SZ regardless of what was
        # there before.
        assert new_kind == winreg.REG_EXPAND_SZ
        # The pre-existing %VAR% reference must still be LITERAL, not expanded
        # to whatever TAN_INSTALL_TEST_VAR happened to resolve to (usually
        # nothing, which GetEnvironmentVariable would have collapsed to "").
        assert "%TAN_INSTALL_TEST_VAR%" in new_value
        assert r"C:\Windows\system32" in new_value
        # And the install actually did its job: $Dir was appended.
        assert str(dest) in new_value.split(";")
    finally:
        try:
            winreg.DeleteKey(hkcu, test_subkey)
        except OSError:
            pass
        try:
            winreg.DeleteKey(hkcu, "Software\\TanInstallTest")
        except OSError:
            pass


@windows_only
def test_ps1_relative_dir_is_resolved_against_pwd_not_process_startup_dir(release_server, tmp_path):
    """tan-cli#490 review, round four's MAJOR-1: a relative `-Dir` used to be
    resolved via `[System.IO.Path]::GetFullPath($Dir)`, which anchors against
    `[Environment]::CurrentDirectory` -- the PROCESS's startup directory --
    not against `$PWD`, the directory a `Set-Location`/`cd` actually moves.
    `cd .\\tools; irm .../install.ps1 | iex -Dir .\\bin` would then silently
    install wherever the PowerShell process itself started (often `$HOME` for
    a fresh `irm | iex` invocation), never `.\\tools\\bin` as asked.

    This is reproduced here as a genuine divergence between the *process's
    own* starting directory and the directory a subsequent `Set-Location`
    leaves `$PWD` pointing at -- `subprocess.run(..., cwd=workdir)` starts the
    `pwsh` PROCESS in `workdir` (standing in for wherever `irm | iex` itself
    began), and the script passed via `-Command` then does the user's own
    `cd .\\tools` before invoking install.ps1 with a `-Dir` relative to
    THAT. A correct fix must install under `workdir/tools/bin`; the pre-fix
    behaviour installed under `workdir/bin` instead.

    install.ps1's own module comment once claimed PowerShell 7 keeps
    `[Environment]::CurrentDirectory` and `$PWD` in sync, so only Windows
    PowerShell 5.1 needed this fix -- that claim does not hold: measured
    directly against a real PowerShell 7 (`Set-Location` into a
    subdirectory, then compare `$PWD.Path` with
    `[Environment]::CurrentDirectory`), the two diverge on 7 exactly as they
    do on 5.1. This test therefore does not gate on the PowerShell version at
    all -- whatever `pwsh`/`powershell` the CI runner has must resolve `-Dir`
    correctly.
    """
    workdir = tmp_path / "work"
    subdir = workdir / "tools"
    subdir.mkdir(parents=True)
    ps_command = (
        f"Set-Location -LiteralPath '{subdir}'; "
        f"& '{INSTALL_PS1}' -Dir '.\\bin' -NoModifyPath -Version v0.4.1"
    )
    result = subprocess.run(
        [
            PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", ps_command,
        ],
        cwd=workdir,  # the pwsh PROCESS itself starts here, never moves
        env={
            **os.environ,
            "TAN_INSTALL_BASE_URL": release_server,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
        },
        capture_output=True, text=True, timeout=180,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    wrong_install = workdir / "bin" / "tan.exe"
    right_install = subdir / "bin" / "tan.exe"
    assert not wrong_install.is_file(), (
        f"installed at {wrong_install} -- resolved against the pwsh PROCESS's "
        f"own startup directory ({workdir}) instead of $PWD ({subdir}), the "
        f"exact tan-cli#490 regression"
    )
    assert right_install.is_file(), (
        f"expected the install under {subdir / 'bin'} (where Set-Location left "
        f"$PWD); {result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# install.sh
# ---------------------------------------------------------------------------
@posix_only
@pytest.mark.parametrize("tag", RAW_TAGS)
def test_sh_installs_the_raw_binary_for_a_pre_archive_tag(release_server, tmp_path, tag):
    """#356's repro, POSIX half -- literally `sh install.sh --version v0.4.1`,
    which 404'd on `tan-x86_64-unknown-linux-gnu.tar.gz` before this fix."""
    dest = tmp_path / "bin"
    result = _install_sh(release_server, dest, tmp_path, "--version", tag)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    installed = dest / "tan"
    assert installed.is_file()
    # The raw asset IS the program: what landed must be the payload itself, not
    # a launcher pointing at a runtime dir that this layout never creates.
    assert FIXTURE_VERSION_LINE in installed.read_text(encoding="utf-8")
    assert not (dest / "tan-cli-lib").exists()
    assert os.access(installed, os.X_OK)


@posix_only
def test_sh_unpacks_the_archive_for_the_first_archive_tag(release_server, tmp_path):
    dest = tmp_path / "bin"
    result = _install_sh(release_server, dest, tmp_path, "--version", FIRST_ARCHIVE_TAG)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    launcher = dest / "tan"
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert f'exec "{dest / "tan-cli-lib" / "tan"}"' in launcher.read_text(encoding="utf-8")
    payload = dest / "tan-cli-lib" / "tan"
    assert payload.is_file() and os.access(payload, os.X_OK)
    assert (dest / "tan-cli-lib" / "_internal").is_dir()
    # `mv src dst` RENAMES when dst is absent; it does not nest.
    assert not (dest / "tan-cli-lib" / "tan" / "tan").exists()


@posix_only
def test_sh_switching_layouts_leaves_no_orphaned_runtime(release_server, tmp_path):
    """Both layouts install to the same $INSTALL_DIR/tan, so the raw binary
    overwrites the launcher on its own -- but tan-cli-lib/ would survive as
    ~14 MB of runtime nothing points at."""
    dest = tmp_path / "bin"
    assert _install_sh(release_server, dest, tmp_path, "--version", FIRST_ARCHIVE_TAG).returncode == 0
    assert (dest / "tan-cli-lib").is_dir()

    result = _install_sh(release_server, dest, tmp_path, "--version", "v0.4.1")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert FIXTURE_VERSION_LINE in (dest / "tan").read_text(encoding="utf-8")
    assert not (dest / "tan-cli-lib").exists()


@posix_only
def test_sh_bare_latest_installs_whatever_shape_latest_is(release_server, tmp_path):
    dest = tmp_path / "bin"
    result = _install_sh(release_server, dest, tmp_path)
    _skip_unless_latest_is_a_fixture_tag(result)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert (dest / "tan").is_file()


@posix_only
def test_sh_refuses_when_the_checksums_cannot_be_fetched(release_server, tmp_path):
    """checksums.txt is now fetched FIRST and is both the manifest and the
    integrity source, so its absence has to refuse before anything downloads --
    and say so as a fetch failure, not as evidence about the release."""
    dest = tmp_path / "bin"
    result = _install_sh(release_server, dest, tmp_path, "--version", "v9.9.9-does-not-exist")

    assert result.returncode != 0
    assert not dest.exists() or list(dest.iterdir()) == []
    assert "could not fetch" in (result.stdout + result.stderr)


@posix_only
def test_sh_refuses_a_release_with_no_asset_for_this_platform(release_server, tmp_path):
    """The widened Outcome 2 (tan-cli#356), reached for real -- unlike
    `test_sh_refuses_when_the_checksums_cannot_be_fetched` above, which only
    ever reaches the EARLIER `could not fetch checksums.txt` branch.
    `v0.5.0-rc4`'s real published asset list (mirrored in `RELEASES`) has no
    aarch64-Linux entry under EITHER name. A fake `uname` ahead of the real one
    on PATH (`_fake_uname`) drives install.sh's own arch/OS detection there
    without an actual aarch64 Linux runner -- the same inputs a real one would
    produce, since install.sh never probes hardware beyond `uname`.

    Reverting install.sh's whole Outcome-2 block to the pre-#356 single-name
    wording would leave this test (and its install.ps1 sibling) failing.
    """
    dest = tmp_path / "bin"
    fake_uname_dir = _fake_uname(tmp_path, "Linux", "aarch64")
    env_path = f"{fake_uname_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    result = _install_sh(
        release_server, dest, tmp_path, "--version", "v0.5.0-rc4",
        extra_env={"PATH": env_path},
    )

    assert result.returncode != 0
    assert not dest.exists() or list(dest.iterdir()) == []
    combined = result.stdout + result.stderr
    assert "lists no asset for aarch64-unknown-linux-gnu" in combined
    assert "tan-aarch64-unknown-linux-gnu.tar.gz" in combined
    assert "tan-aarch64-unknown-linux-gnu" in combined
    assert "no prebuilt Linux arm64 asset" in combined


@posix_only
def test_sh_system_style_install_uses_sudo_when_the_dir_is_not_writable(release_server, tmp_path):
    """install.sh:375-393's `as_root`/sudo path -- plus the permission fixes it
    guards (`:353 chmod -R a+rX`, `:393 chmod 755`) -- is dead code under every
    OTHER test in this file: they all pass `--dir <tmp>/bin`, which `mkdir -p`
    always creates writable, so `as_root` is always the pass-through branch and
    real elevation is never exercised (tan-cli#356 adversarial review, item 5).

    This drives the OTHER branch without needing real root: a 0555 (no write
    bit) dir the test itself owns forces `[ -w "$INSTALL_DIR" ]` false, and
    `_fake_sudo` restores write access legitimately (via ownership, not
    elevation) before running the real command -- see its docstring.
    """
    dest = tmp_path / "system-style-bin"
    dest.mkdir()
    dest.chmod(0o555)
    calls_log = tmp_path / "sudo-calls.log"
    sudo_dir = _fake_sudo(dest, calls_log)
    env_path = f"{sudo_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    result = _install_sh(
        release_server, dest, tmp_path, "--version", FIRST_ARCHIVE_TAG,
        extra_env={"PATH": env_path},
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "running sudo" in (result.stdout + result.stderr)
    assert calls_log.is_file(), "as_root never shelled out to sudo -- the writable-dir branch ran instead"
    logged = calls_log.read_text(encoding="utf-8")
    assert "mkdir -p" in logged
    assert "chmod 755" in logged
    # Archive layout also exercises the tree-wide a+rX chmod (install.sh:353)
    # and the elevated mv into place (install.sh:387).
    launcher = dest / "tan"
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    payload = dest / "tan-cli-lib" / "tan"
    assert payload.is_file() and os.access(payload, os.X_OK)


@posix_only
def test_sh_bad_payload_on_fresh_host_leaves_nothing_behind(release_server, tmp_path):
    """tan-cli#434 acceptance criterion 1, POSIX half: `BAD_PAYLOAD_TAG`'s
    asset verifies (its checksums.txt digest is computed from the same garbage
    bytes, so the mismatch refusal at install.sh:317-324 never fires) but
    cannot run, so the health check at install.sh:381-403 has to refuse before
    `$INSTALL_DIR` is even `mkdir -p`'d (that only happens afterwards, at
    install.sh:410) -- and, since there is nothing to fall back to, before the
    PATH-modifying rc-file write at install.sh:495-524 either.
    `_install_sh_modify_path` is used here instead of `_install_sh`
    (unlike every other install.sh test in this file) precisely because that
    rc-file half is what would regress silently if the health check ran too
    late or the early-exit skipped past it.
    """
    dest = tmp_path / "bin"
    home = tmp_path
    result = _install_sh_modify_path(release_server, dest, home, "--version", BAD_PAYLOAD_TAG)

    assert result.returncode != 0
    assert not dest.exists() or list(dest.iterdir()) == []
    combined = result.stdout + result.stderr
    assert "newly downloaded binary failed to run" in combined
    assert "no previous installation existed" in combined
    assert "PATH was not modified" in combined
    for rc_name in (".zshrc", ".bashrc", ".bash_profile", ".profile"):
        rc = home / rc_name
        assert not rc.exists(), f"{rc} must not exist -- install.sh must not reach the rc-file write on a failed install"


@posix_only
def test_sh_bad_payload_on_upgrade_leaves_previous_install_working(release_server, tmp_path):
    """tan-cli#434 acceptance criterion 2, POSIX half: a bad upgrade over a
    GOOD install -- with the PATH-modifying rc-file write actually reached, via
    `_install_sh_modify_path` -- must leave the previous `tan` byte-for-byte,
    still runnable, and the rc file untouched, with no `.bak` left behind.
    The health check (install.sh:381-403) refuses before the backup/commit
    block (install.sh:418-480) ever runs, so there is nothing to roll back
    FROM.
    """
    dest = tmp_path / "bin"
    home = tmp_path
    good = _install_sh_modify_path(release_server, dest, home, "--version", "v0.4.1")
    assert good.returncode == 0, f"{good.stdout}\n{good.stderr}"
    installed = dest / "tan"
    before_hash = hashlib.sha256(installed.read_bytes()).hexdigest()
    rc = home / ".profile"
    assert rc.is_file(), "the good install above should have written the rc line -- nothing to compare the upgrade against otherwise"
    before_rc = rc.read_bytes()

    result = _install_sh_modify_path(release_server, dest, home, "--version", BAD_PAYLOAD_TAG)

    assert result.returncode != 0
    assert FIXTURE_VERSION_LINE in installed.read_text(encoding="utf-8")
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == before_hash
    assert rc.read_bytes() == before_rc
    assert not (dest / "tan.bak").exists()
    assert not (dest / "tan-cli-lib.bak").exists()
    combined = result.stdout + result.stderr
    assert "your existing installation" in combined
    assert "was never touched" in combined


@posix_only
@noexec_capable
def test_sh_noexec_tmpdir_retries_staging_inside_install_dir(release_server, tmp_path):
    """tan-cli#490: a `$TMPDIR` mounted `noexec` -- common on CIS/STIG-hardened
    images, which is exactly where customers install `tan` from via
    `curl | sh` -- must not sink the install. `$INSTALL_DIR` (here, a normal
    writable dir OUTSIDE the noexec mount) already has to be exec-able for
    `tan` to ever run once installed, so install.sh retries the health check
    staged there instead of refusing outright.
    """
    dest = tmp_path / "bin"
    home = tmp_path / "home"
    home.mkdir()
    noexec_dir = tmp_path / "noexec-tmp"
    noexec_dir.mkdir()

    result = _install_sh_under_noexec_tmpdir(
        release_server, dest, home, noexec_dir, "--version", "v0.4.1"
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    installed = dest / "tan"
    assert installed.is_file()
    assert FIXTURE_VERSION_LINE in installed.read_text(encoding="utf-8")
    assert os.access(installed, os.X_OK)
    # No retry staging directory left behind under $INSTALL_DIR.
    assert list(dest.iterdir()) == [installed]
    combined = result.stdout + result.stderr
    assert "noexec" in combined


class _BashLocaleProbe(NamedTuple):
    """What `_bash_setlocale_warning_probe` measured: whether the `bash` this
    host resolves can emit a `setlocale` warning at all, and which bash that
    was (both fields are quoted in the skip reason, so a skipped run names the
    exact binary that could not do it rather than just "no bash")."""

    warns: bool
    path: str
    version: str


@functools.lru_cache(maxsize=1)
def _bash_setlocale_warning_probe() -> _BashLocaleProbe:
    """`shutil.which("bash") is not None` is not proof this bash can perform
    the observation the mechanism test below makes -- it only proves SOME bash
    is on `PATH`.

    bash 3.2 has no setlocale warning at all. Its `locale.c:180` reads
    `r = *lc_all ? (setlocale (LC_ALL, lc_all) != 0) : reset_locale_vars ();`
    -- a bool, printed nowhere; the string "cannot change locale" does not
    occur anywhere in the bash-3.2 sources. bash 5.2.21 does carry
    `setlocale: %s: cannot change locale (%s)`. Which release in between
    introduced it is NOT established here, which is exactly why this is a
    behavioural probe and not a version comparison: pinning a version number
    as the gate would be inferring the behaviour instead of measuring it.

    macOS ships bash 3.2.57 as `/bin/bash` (the last GPLv2 release), so this
    is not a hypothetical host. It was masked on this repo's macOS CI for
    months: `actions-rust-lang/setup-rust-toolchain@v1` carries an internal
    step named "Unbork mac" that runs `brew install bash`, so every macOS job
    silently got Homebrew's bash 5.x on `PATH` as a side effect of a Rust
    toolchain it needed for something else entirely. Retiring the Rust oracle
    (tan-cli#269) removed that action, the jobs fell back to `/bin/bash`
    3.2.57, and this test failed with output exactly `'reached'` -- no warning
    in EITHER form, because the shell cannot produce one. The fix is a
    capability probe, not re-adding `brew install bash` to the workflow: that
    would pin CI to a host detail the test never intended to depend on and
    would leave the guard wrong for every other bash-3.2 host.

    Same shape as `_noexec_probe` above, and as
    `tests/commands/test_completion_command.py:_bash_available` (which spawns
    `bash -c` rather than trusting `which`, for the Windows WSL-launcher-stub
    reason), and the same lesson tan-cli#580 recorded when macOS/APFS refused
    a filename Linux accepted: probe the capability, do not infer it.
    """
    resolved = shutil.which("bash")
    if resolved is None:
        return _BashLocaleProbe(False, "<not on PATH>", "<not on PATH>")
    try:
        version_run = subprocess.run(
            ["bash", "--version"], capture_output=True, text=True, timeout=15
        )
        # The invalid-locale `export` form, run ONCE: the whole question is
        # whether calling this shell's own setlocale() with a value that
        # cannot resolve makes it say so.
        warn_run = subprocess.run(
            ["bash", "-c", "export LC_ALL=xx_YY.bogus"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        # NOT just OSError. This probe runs at MODULE SCOPE, so anything it
        # raises is an `Interrupted: 1 error during collection` that takes the
        # whole file's 48 tests with it -- including
        # `test_install_sh_pins_lc_all_as_an_export_inside_the_subshell`, which
        # was split out of the mechanism proof precisely so it would keep
        # running on a host the proof cannot measure. A hung `bash` defeating
        # that split is the exact outcome the split exists to prevent.
        # `subprocess.TimeoutExpired` is a `SubprocessError`, NOT an `OSError`,
        # so the narrower catch missed it: measured with a `bash` stub of
        # `sleep 30`, collection died with
        # `subprocess.TimeoutExpired: Command '['bash', '--version']' timed out
        # after 15 seconds` rather than skipping one test. `ValueError` covers
        # a `UnicodeDecodeError` out of `text=True` on a non-UTF-8 host.
        # An unmeasurable shell is exactly the "cannot be asked" case, which
        # this returns as "cannot warn" -> skip with a reason, never a silent
        # pass.
        return _BashLocaleProbe(False, resolved, "<unrunnable>")
    first_line = (version_run.stdout or version_run.stderr).splitlines()
    matched = re.search(r"version\s+([0-9][^\s(]*)", first_line[0]) if first_line else None
    version = matched.group(1) if matched else "<unparsed>"
    warns = "cannot change locale" in (warn_run.stdout + warn_run.stderr)
    return _BashLocaleProbe(warns, resolved, version)


_BASH_LOCALE_PROBE = _bash_setlocale_warning_probe()

setlocale_warning_capable = pytest.mark.skipif(
    not _BASH_LOCALE_PROBE.warns,
    reason=(
        f"the bash on PATH ({_BASH_LOCALE_PROBE.path}, {_BASH_LOCALE_PROBE.version}) "
        "emits no setlocale warning, so the mechanism proof cannot run -- the "
        "install.sh shape pin in "
        "test_install_sh_pins_lc_all_as_an_export_inside_the_subshell still runs "
        "and is the actual tan-cli#490 regression guard"
    ),
)


@posix_only
@setlocale_warning_capable
def test_sh_lc_all_c_reaches_the_shells_own_exec_failure_diagnostic(tmp_path):
    """tan-cli#490 review, MAJOR 1 (install.sh's `run_health_check`): a
    command-prefix assignment (`LC_ALL=C "$1" --version`) sets LC_ALL only in
    the ENVIRONMENT HANDED TO THE CHILD ABOUT TO BE EXEC'D. When exec(2)
    itself fails -- the noexec case this whole health check exists for -- no
    child ever replaces the running shell's image, so the "Permission
    denied" diagnostic is printed by the shell (or its command-substitution
    subshell) in whatever locale IT was already running in, not the
    temporarily-prefixed one. On a bash-as-/bin/sh host
    (RHEL/Rocky/Alma/Fedora/SLES -- exactly the hardened enterprise/
    government image class #490 targets) a localized ambient locale then
    gets a translated `strerror(EACCES)`, `is_noexec_signature`'s English
    substring match misses, and the retry silently never fires.

    Getting a REAL translated "Permission denied" onto a CI runner needs a
    glibc language pack that is not guaranteed present on
    ubuntu-latest/macos-latest (confirmed for real on a Fedora 40 container
    with `glibc-langpack-de` installed while writing this fix: under a
    de_DE.UTF-8 ambient locale, the pre-fix command-prefix form prints "Keine
    Berechtigung" for a real noexec exec failure; the fixed
    export-in-subshell form prints "Permission denied" -- not reproduced
    here for that reason). So this proves the underlying, portable half of
    the same mechanism instead: bash always warns on stderr when asked to
    set a locale that does not exist ("...: warning: setlocale: LC_ALL:
    cannot change locale ..."), on every host, no package install required.
    That warning is bash calling `setlocale()` on the CURRENT process as a
    side effect of the assignment -- which is exactly, and only, what
    `export` does; a command-prefix assignment builds an envp for the child
    about to be exec'd and never calls the current process's own
    `setlocale()` at all, so it produces no warning regardless of whether the
    value is valid. Once `setlocale()` has actually been called (the `export`
    form, always, even for a valid value like install.sh's real "C") the
    change is a property of the PROCESS and persists into every later
    statement in that same subshell -- including the next statement's own
    exec-failure message, which is the mechanism the Fedora repro above
    exercises for real. The warning is checked across the whole process's
    combined output rather than narrowly inside `$verify_out` on purpose: it
    is emitted by the `export` statement itself, a separate statement from
    the one `2>&1` is attached to, so it would never land inside
    `$verify_out` in EITHER form -- that is exactly why it is the right
    portable proxy for "did this reach the process's own setlocale()", not a
    claim about install.sh's own capture, which "C" never triggers because
    "C" is always a valid locale.

    The two forms compared below are the exact shapes install.sh had before
    and after this fix (with `C` swapped for an invalid locale value purely
    so the shell has something to warn about).

    This proof needs a bash that HAS the warning -- see
    `_bash_setlocale_warning_probe`, and note that bash 3.2 (macOS's
    `/bin/bash`) does not. The pin on install.sh's actual line lives in
    `test_install_sh_pins_lc_all_as_an_export_inside_the_subshell` below,
    deliberately split out of this test so that a bash-3.2 host still runs it:
    it is the real regression guard, needs no special shell, and used to be
    unreachable behind this test's assertions.
    """
    target = tmp_path / "target.sh"
    target.write_text("#!/bin/sh\necho reached\n")
    target.chmod(0o755)

    def run_form(body: str) -> str:
        probe = tmp_path / "probe.sh"
        probe.write_text(f'#!/bin/bash\n{body}\nprintf \'%s\' "$verify_out"\n')
        probe.chmod(0o755)
        result = subprocess.run(
            ["bash", str(probe), str(target)], capture_output=True, text=True, timeout=15
        )
        return result.stdout + result.stderr

    prefix_out = run_form('verify_out="$(LC_ALL=xx_YY.bogus "$1" 2>&1)"')
    export_out = run_form('verify_out="$(export LC_ALL=xx_YY.bogus; "$1" 2>&1)"')

    assert "cannot change locale" not in prefix_out, (
        "a command-prefix LC_ALL assignment was not expected to reach the "
        f"running shell's own setlocale(), but it did: {prefix_out!r}"
    )
    assert "cannot change locale" in export_out, (
        "export inside the command-substitution subshell was expected to "
        f"reach that subshell's own setlocale(): {export_out!r}"
    )


def test_install_sh_pins_lc_all_as_an_export_inside_the_subshell():
    """The half of tan-cli#490's guard that pins the ACTUAL line in
    install.sh, so a regression back to the command-prefix shape fails even
    where the mechanism proof above cannot run.

    Host-independent on purpose: it reads text, spawns nothing, and needs no
    particular bash -- not even a POSIX host, since `.gitattributes` pins
    `install.sh text eol=lf` so the substrings below are byte-identical in a
    Windows checkout. It was previously the tail of the mechanism test, where
    it never got to run on any host whose bash could not emit the setlocale
    warning (macOS's bash 3.2.57), losing the one assertion that does not
    depend on the shell at all.
    """
    text = INSTALL_SH.read_text()
    assert 'verify_out="$(export LC_ALL=C; "$1" --version 2>&1)"' in text, (
        "install.sh:run_health_check must set LC_ALL=C via `export` inside "
        "the $(...) subshell, not as a command-prefix assignment on the "
        "probed binary -- see "
        "test_sh_lc_all_c_reaches_the_shells_own_exec_failure_diagnostic's "
        "docstring for why the prefix form cannot reach the running shell's "
        "own exec-failure diagnostic."
    )
    assert 'verify_out="$(LC_ALL=C "$1" --version 2>&1)"' not in text


@posix_only
@noexec_capable
def test_sh_noexec_tmpdir_archive_layout_retries_staging_inside_install_dir(release_server, tmp_path):
    """Same as above, for the `--onedir` archive layout (tan-cli#349): the
    thing that has to move off the noexec mount is `$stage/tan/tan`, not the
    launcher (which is never executed while staged), and the retry has to
    carry the runtime's `_internal/` tree along with it.
    """
    dest = tmp_path / "bin"
    home = tmp_path / "home"
    home.mkdir()
    noexec_dir = tmp_path / "noexec-tmp"
    noexec_dir.mkdir()

    result = _install_sh_under_noexec_tmpdir(
        release_server, dest, home, noexec_dir, "--version", FIRST_ARCHIVE_TAG
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    launcher = dest / "tan"
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    payload = dest / "tan-cli-lib" / "tan"
    assert payload.is_file() and os.access(payload, os.X_OK)
    assert (dest / "tan-cli-lib" / "_internal").is_dir()
    # No retry staging directory left behind under $INSTALL_DIR.
    assert sorted(p.name for p in dest.iterdir()) == ["tan", "tan-cli-lib"]
    combined = result.stdout + result.stderr
    assert "noexec" in combined


@posix_only
@noexec_capable
def test_sh_noexec_tmpdir_distinguishes_noexec_from_a_broken_binary(release_server, tmp_path):
    """tan-cli#490's hard requirement: when the retry ALSO fails (here,
    `--dir` is put INSIDE the same noexec mount, so there is no exec-able
    place left to stage), the failure has to say this is a noexec mount, not
    the generic "your glibc may be too old" guidance install.sh gives for an
    actually-corrupt payload -- a customer who cannot tell those two apart
    from the message alone files a support ticket against the wrong thing.
    """
    noexec_dir = tmp_path / "noexec-tmp"
    noexec_dir.mkdir()
    dest = noexec_dir / "bin"  # $INSTALL_DIR itself is on the noexec mount too
    home = tmp_path / "home"
    home.mkdir()

    result = _install_sh_under_noexec_tmpdir(
        release_server, dest, home, noexec_dir, "--version", "v0.4.1"
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "noexec" in combined
    assert "Permission denied" in combined
    assert "PATH was not modified" in combined
    # Must not misattribute a mount option to a libc floor.
    assert "GLIBC" not in combined
    assert "glibc" not in combined


def _fake_noop_chmod(tmp_path: Path) -> Path:
    """A `chmod` stub that never actually sets any bit -- shadowing the real
    one reproduces the exact exit-126 "Permission denied" health-check
    signature (the staged payload simply never becomes executable) WITHOUT a
    real noexec mount, so `$INSTALL_DIR` can be an ordinary, directly
    inspectable path rather than something living inside an `unshare -m`
    mount namespace that vanishes -- taking any evidence written under it --
    the moment that subprocess exits.
    """
    bin_dir = tmp_path / "fake-noop-chmod-bin"
    bin_dir.mkdir()
    script = bin_dir / "chmod"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    script.chmod(0o755)
    return bin_dir


@posix_only
def test_sh_noop_chmod_retry_failure_leaves_no_empty_install_dir(release_server, tmp_path):
    """tan-cli#490 review finding (install.sh:428): the retry gate's
    `mkdir -p "$INSTALL_DIR"` ran even when the install was ultimately
    refused, leaving an empty directory behind where pre-fix nothing existed.
    A no-op `chmod` ahead of the real one on PATH means the staged (and,
    after the retry, re-staged) binary never actually becomes executable --
    the same exit-126 "Permission denied" signature a noexec mount produces,
    without one -- so `$INSTALL_DIR` here is a normal path this test can
    inspect directly after the subprocess exits (unlike the noexec-mount
    tests above, whose `$INSTALL_DIR` lives inside a private mount namespace
    that is torn down, and any evidence with it, the moment the `unshare`d
    process exits).
    """
    dest = tmp_path / "bin"  # must not exist before this run
    chmod_dir = _fake_noop_chmod(tmp_path)
    env_path = f"{chmod_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    result = _install_sh(
        release_server, dest, tmp_path, "--version", "v0.4.1",
        extra_env={"PATH": env_path},
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Permission denied" in combined
    assert not dest.exists(), "a refused install must not leave an empty install dir behind"


@posix_only
def test_sh_noop_chmod_retry_failure_leaves_no_orphaned_intermediate_parents(release_server, tmp_path):
    """tan-cli#490 round 9: the retry gate's `mkdir -p "$INSTALL_DIR"` creates
    every missing INTERMEDIATE parent too (like `mkdir -p` always does), but
    the refusal cleanup this test's sibling above covers
    (`test_sh_noop_chmod_retry_failure_leaves_no_empty_install_dir`) used to
    `rmdir "$INSTALL_DIR"`, which removes only the LEAF -- the same class of
    orphan round 8 already fixed once in the relative-`--dir` argument-parsing
    path, surviving here in the retry-gate path. MEASURED pre-fix:
    `--dir <tmp>/orph/deep/bin` refused left `<tmp>/orph` and
    `<tmp>/orph/deep` behind. `--dir` here is THREE levels deep so the fix has
    to walk, not just handle the immediate parent.
    """
    dest = tmp_path / "orph" / "deep" / "bin"  # none of these must exist before this run
    chmod_dir = _fake_noop_chmod(tmp_path)
    env_path = f"{chmod_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    result = _install_sh(
        release_server, dest, tmp_path, "--version", "v0.4.1",
        extra_env={"PATH": env_path},
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Permission denied" in combined
    assert not (tmp_path / "orph").exists(), (
        "a refused install must not leave any intermediate parent directory behind, "
        "not just the immediate --dir leaf"
    )


def _install_sh_noexec_with_exhausted_install_dir(
    base_url: str, dest: Path, home: Path, noexec_dir: Path, install_mount: Path, *args: str
) -> subprocess.CompletedProcess:
    """Reproduces the exact tan-cli#490 review scenario: `$TMPDIR` noexec AND
    `$INSTALL_DIR` on a filesystem that cannot stage a NEW directory. A real
    `nr_inodes=2` tmpfs at `$install_mount` (one inode for its own root, one
    for `$dest` pre-created below to stand in for whatever already lived
    there) leaves none for the retry's `mktemp -d` -- so `$INSTALL_DIR` itself
    stays writable (unlike the noexec-tmpdir tests above) while staging a
    retry inside it genuinely fails, the way an inode-exhausted filesystem
    would.
    """
    script = (
        "set -e\n"
        f'mount -t tmpfs -o noexec tmpfs "{noexec_dir}"\n'
        f'mkdir -p "{install_mount}"\n'
        f'mount -t tmpfs -o size=4k,nr_inodes=2 tmpfs "{install_mount}"\n'
        f'mkdir -p "{dest}"\n'
        'TAR_OPTIONS="--no-same-owner" '
        f'TMPDIR="{noexec_dir}" TAN_INSTALL_BASE_URL="{base_url}" '
        f'HOME="{home}" USERPROFILE="{home}" '
        f'sh "{INSTALL_SH}" --dir "{dest}" --no-modify-path {" ".join(args)}\n'
    )
    return subprocess.run(
        ["unshare", "--map-root-user", "-m", "--", "sh", "-c", script],
        capture_output=True, text=True, timeout=180,
    )


@posix_only
@noexec_capable
def test_sh_noexec_retry_staging_failure_is_reported_not_misattributed(release_server, tmp_path):
    """tan-cli#490 review, MAJOR finding (install.sh:429): `retried=1` used to
    be set BEFORE the retry was actually attempted, so a retry that could
    never even get staged was reported as one that RAN and failed -- blaming
    the wrong mount, exactly the misattributed-diagnostic class #490 itself is
    about. Reproduced for real (not mocked): `$TMPDIR` is a noexec tmpfs (the
    original failure) and `$INSTALL_DIR` sits on a SEPARATE tmpfs whose inode
    budget is already exhausted (the retry's own `mktemp -d` has nowhere to
    land), which is the report's own example ("$TMPDIR noexec + $INSTALL_DIR
    on an inode-exhausted fs").

    Against the unfixed script this prints "Retrying staged inside .../bin..."
    followed by "exit 126 (Permission denied) persisted even after staging
    inside" -- neither of which happened; the retry's own `mktemp` never even
    ran to completion. The fixed script must instead surface the real staging
    error and never claim the retry persisted when it never got staged.
    """
    home = tmp_path / "home"
    home.mkdir()
    noexec_dir = tmp_path / "noexec-tmp"
    noexec_dir.mkdir()
    install_mount = tmp_path / "install-mount"
    dest = install_mount / "bin"

    result = _install_sh_noexec_with_exhausted_install_dir(
        release_server, dest, home, noexec_dir, install_mount, "--version", "v0.4.1"
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Permission denied" in combined
    # The retry must never be reported as having actually run and failed --
    # it never got staged in the first place.
    assert "persisted even after staging inside" not in combined
    assert "could not stage a retry inside" in combined


@posix_only
def test_sh_no_curl_or_wget_prints_a_diagnostic(release_server, tmp_path):
    """tan-cli#490 review finding (install.sh:228): the FIRST `download` call
    (checksums.txt) redirected its own `need curl or wget on PATH` message to
    /dev/null, so a host with neither tool exited 1 with ZERO further output
    -- and because that call always runs before any other download, the
    message was unreachable on every code path. A PATH built from real
    coreutils but deliberately missing curl/wget reproduces the real failure
    (not a mock of `download`) -- everything upstream of the transport check
    (OS/arch/musl detection, the sha256-tool check) still runs for real and
    still has to fall through correctly to the actual missing-transport
    refusal.
    """
    dest = tmp_path / "bin"
    bin_dir = tmp_path / "no-curl-wget-bin"
    bin_dir.mkdir()
    needed = (
        "sh", "uname", "sha256sum", "shasum", "mktemp", "mkdir", "mv", "rm", "rmdir",
        "chmod", "grep", "awk", "sed", "basename", "dirname", "cat", "printf", "cut",
        "tar", "ls", "sort", "head", "tail", "ldd", "id",
    )
    for tool in needed:
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)

    result = _install_sh(
        release_server, dest, tmp_path, "--version", "v0.4.1",
        extra_env={"PATH": str(bin_dir)},
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "need curl or wget on PATH" in combined


@posix_only
def test_sh_no_curl_or_wget_prints_a_diagnostic_on_the_default_latest_invocation(tmp_path):
    """tan-cli#490 review, MAJOR finding (install.sh:184-192): the test above
    only covers `--version vX.Y.Z`, which skips the `latest` redirect
    resolution entirely -- but that block is what actually runs FIRST on the
    DEFAULT invocation (`curl ... | sh`, no `--version`, exactly the
    documented one-liner), and it has its own inline curl/wget branching that
    is not routed through `download()` at all: when neither tool exists it
    silently falls through both `if`/`elif` arms, `resolved` is never
    assigned, and the script prints "could not resolve which release
    'latest' points at" -- true in isolation, but not the actual problem, and
    it masks the real one. Confirmed against the real, unfixed script while
    writing this (2>&1 output):

        install.sh: resolving the latest release tag...
        install.sh: could not resolve which release 'latest' points at.
        install.sh: refusing to install -- without a tag there is no
        checksums.txt to verify against. Retry, or pass an explicit
        --version vX.Y.Z.

    instead of "need curl or wget on PATH". Deliberately does NOT use
    `release_server`/`TAN_INSTALL_BASE_URL` -- the whole point is that a host
    with neither transport must refuse before ever reaching the network, `latest`
    included, using the sha256-tool-check idiom (:234-246 today): check for
    the tool up front, before anything that assumes it exists.
    """
    dest = tmp_path / "bin"
    bin_dir = tmp_path / "no-curl-wget-bin"
    bin_dir.mkdir()
    needed = (
        "sh", "uname", "sha256sum", "shasum", "mktemp", "mkdir", "mv", "rm", "rmdir",
        "chmod", "grep", "awk", "sed", "basename", "dirname", "cat", "printf", "cut",
        "tar", "ls", "sort", "head", "tail", "ldd", "id",
    )
    for tool in needed:
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)

    result = subprocess.run(
        ["sh", str(INSTALL_SH), "--dir", str(dest), "--no-modify-path"],
        env={**os.environ, "PATH": str(bin_dir), "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "need curl or wget on PATH" in combined
    assert "could not resolve which release 'latest' points at" not in combined


@posix_only
def test_sh_system_style_install_attempts_to_chown_the_installed_files_to_root(release_server, tmp_path):
    """tan-cli#490 review finding (install.sh:459): `$payload`/`$stage` are
    staged UNPRIVILEGED (`mktemp`), and `mv` preserves ownership on both a
    same-filesystem rename and, as real root, a cross-filesystem copy -- so
    without a `chown`, a `--system`-shaped install would leave the whole tree
    owned by the invoking (non-root) user despite living in a root-owned dir
    early on root's own PATH. `_fake_sudo` cannot exercise a REAL ownership
    change (chowning to a different user needs real root/CAP_CHOWN, which is
    off the table here the same way elevation itself is -- see its own
    docstring), but it does prove the code path is REACHED: the logged sudo
    invocations must include a `chown` of both the launcher and the runtime
    dir.
    """
    dest = tmp_path / "system-style-bin"
    dest.mkdir()
    dest.chmod(0o555)
    calls_log = tmp_path / "sudo-calls.log"
    sudo_dir = _fake_sudo(dest, calls_log)
    env_path = f"{sudo_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    result = _install_sh(
        release_server, dest, tmp_path, "--version", FIRST_ARCHIVE_TAG,
        extra_env={"PATH": env_path},
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    logged = calls_log.read_text(encoding="utf-8")
    assert "chown root" in logged
    assert "tan-cli-lib" in logged.split("chown root", 1)[1] or "chown -R root" in logged


@posix_only
def test_sh_relative_install_dir_is_normalised_to_absolute(release_server, tmp_path):
    """tan-cli#490 review finding (install.sh:373): a relative `--dir` used to
    be baked verbatim into the archive layout's generated launcher
    (`exec "${LIB_DIR}/tan" "$@"`), which then only worked while the CWD was
    the one install.sh happened to run in. Running from a specific CWD with a
    relative `--dir` must still produce a launcher whose `exec` target is an
    ABSOLUTE path.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    result = subprocess.run(
        ["sh", str(INSTALL_SH), "--dir", "./mybin", "--no-modify-path", "--version", FIRST_ARCHIVE_TAG],
        cwd=workdir,
        env={
            **os.environ,
            "TAN_INSTALL_BASE_URL": release_server,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
        },
        capture_output=True, text=True, timeout=180,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    launcher = workdir / "mybin" / "tan"
    assert launcher.is_file()
    launcher_text = launcher.read_text(encoding="utf-8")
    assert "./mybin" not in launcher_text
    assert str((workdir / "mybin" / "tan-cli-lib").resolve()) in launcher_text
    # The launcher must work from a DIFFERENT cwd than the one install.sh ran in.
    run_elsewhere = subprocess.run([str(launcher), "--version"], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert run_elsewhere.returncode == 0, f"{run_elsewhere.stdout}\n{run_elsewhere.stderr}"
    assert FIXTURE_VERSION_LINE in run_elsewhere.stdout


@posix_only
def test_sh_relative_dir_normalisation_creates_nothing_on_a_refused_install(release_server, tmp_path):
    """tan-cli#490 review MINOR (install.sh:66-67): the relative-`--dir`
    normalisation block runs during ARGUMENT PARSING, before any network call
    or health check, and used to `mkdir -p` the not-yet-existing parent of a
    relative `--dir` just to resolve it to an absolute path -- e.g. `--dir
    new/deep/bin` created `./new/deep` in the CWD even when the install was
    then refused (here: an unknown --version, so `checksums.txt` 404s).
    Normalisation must not touch disk; only the later, deliberate
    `mkdir -p "$INSTALL_DIR"` -- reached only once an install is actually
    going ahead -- may create anything, and that path is never reached here.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    result = subprocess.run(
        ["sh", str(INSTALL_SH), "--dir", "new/deep/bin", "--no-modify-path", "--version", "v9.9.9-does-not-exist"],
        cwd=workdir,
        env={
            **os.environ,
            "TAN_INSTALL_BASE_URL": release_server,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
        },
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode != 0
    assert not (workdir / "new").exists(), "install.sh must not create any part of a relative --dir before the install actually proceeds"


@posix_only
def test_sh_relative_install_dir_rc_file_gets_an_absolute_path(release_server, tmp_path):
    """tan-cli#490 review finding (install.sh:373): the rc-file PATH line was
    built from the same un-normalised `$INSTALL_DIR`, permanently putting a
    CWD-relative entry first on PATH. `SHELL=/bin/sh` pins the rc file to
    `~/.profile` deterministically (matching `_install_sh_modify_path`'s own
    convention).
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["sh", str(INSTALL_SH), "--dir", "./mybin", "--version", FIRST_ARCHIVE_TAG],
        cwd=workdir,
        env={
            **os.environ,
            "TAN_INSTALL_BASE_URL": release_server,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "SHELL": "/bin/sh",
        },
        capture_output=True, text=True, timeout=180,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    rc = home / ".profile"
    assert rc.is_file()
    rc_text = rc.read_text(encoding="utf-8")
    assert "./mybin" not in rc_text
    assert str((workdir / "mybin").resolve()) in rc_text


@posix_only
def test_sh_fish_shell_gets_fish_syntax_in_its_own_config_file(release_server, tmp_path):
    """tan-cli#490 review finding (install.sh:507): fish never reads
    ~/.profile and does not understand POSIX `export FOO=bar` -- falling
    through to the `*)` default silently wrote a line fish can never source
    and then claimed 'tan' now worked "anywhere", which was false. fish gets
    its own rc file (`~/.config/fish/config.fish`, which does not exist yet on
    a fresh account) and its own `set -gx` syntax.
    """
    home = tmp_path / "home"
    home.mkdir()
    dest = tmp_path / "bin"

    result = _install_sh_modify_path(
        release_server, dest, home, "--version", "v0.4.1",
        extra_env={"SHELL": "/usr/bin/fish"},
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    rc = home / ".config" / "fish" / "config.fish"
    assert rc.is_file(), "install.sh must create ~/.config/fish/ if it does not exist yet"
    rc_text = rc.read_text(encoding="utf-8")
    assert "set -gx PATH" in rc_text
    assert "export PATH" not in rc_text
    combined = result.stdout + result.stderr
    assert "source" in combined  # the re-source hint must use fish's own command


@posix_only
def test_sh_tcsh_shell_gets_csh_syntax_in_tcshrc(release_server, tmp_path):
    """tan-cli#490 review finding (install.sh:507): tcsh/csh do not read
    ~/.profile and do not parse `export` -- they need `setenv` in
    ~/.tcshrc (or ~/.cshrc for plain csh).
    """
    home = tmp_path / "home"
    home.mkdir()
    dest = tmp_path / "bin"

    result = _install_sh_modify_path(
        release_server, dest, home, "--version", "v0.4.1",
        extra_env={"SHELL": "/bin/tcsh"},
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    rc = home / ".tcshrc"
    assert rc.is_file()
    rc_text = rc.read_text(encoding="utf-8")
    assert "setenv PATH" in rc_text
    assert "export PATH" not in rc_text


@posix_only
def test_sh_tcsh_shell_appends_to_existing_cshrc_instead_of_shadowing_it(release_server, tmp_path):
    """tan-cli#490 review MAJOR: tcsh(1), STARTUP AND SHUTDOWN -- on login it
    reads ~/.tcshrc OR, only if ~/.tcshrc is NOT found, ~/.cshrc, never both.
    A user whose whole csh config lives in ~/.cshrc would have it stop
    loading, silently and unrecoverably (the idempotency guard makes a
    re-run a no-op), the moment this installer created a bare ~/.tcshrc
    containing only the PATH line. When ~/.tcshrc does not exist yet but
    ~/.cshrc does, the PATH line must be appended to ~/.cshrc instead, so
    the user's existing config keeps loading.
    """
    home = tmp_path / "home"
    home.mkdir()
    cshrc = home / ".cshrc"
    cshrc.write_text("alias ll 'ls -l'\nsetenv EDITOR vim\n", encoding="utf-8")
    dest = tmp_path / "bin"

    result = _install_sh_modify_path(
        release_server, dest, home, "--version", "v0.4.1",
        extra_env={"SHELL": "/bin/tcsh"},
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not (home / ".tcshrc").exists(), "must not create ~/.tcshrc -- that would shadow the existing ~/.cshrc"
    cshrc_text = cshrc.read_text(encoding="utf-8")
    assert "alias ll 'ls -l'" in cshrc_text, "the user's pre-existing ~/.cshrc content must survive"
    assert "setenv EDITOR vim" in cshrc_text
    assert "setenv PATH" in cshrc_text
    assert "export PATH" not in cshrc_text


@posix_only
def test_sh_csh_shell_gets_tcshrc_when_neither_rc_file_exists(release_server, tmp_path):
    """tan-cli#490 round 9: on macOS, FreeBSD, and Debian/Ubuntu-via-
    alternatives, /bin/csh IS tcsh (a compat symlink/build, not a distinct
    binary) -- so SHELL=/bin/csh must get the exact same shadowing-aware
    logic as SHELL=/bin/tcsh, not a bare ~/.cshrc write. Shape 1 of 4: neither
    rc file exists yet -- nothing to shadow, so a fresh ~/.tcshrc is created
    (mirrors test_sh_tcsh_shell_gets_csh_syntax_in_tcshrc).
    """
    home = tmp_path / "home"
    home.mkdir()
    dest = tmp_path / "bin"

    result = _install_sh_modify_path(
        release_server, dest, home, "--version", "v0.4.1",
        extra_env={"SHELL": "/bin/csh"},
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    rc = home / ".tcshrc"
    assert rc.is_file()
    rc_text = rc.read_text(encoding="utf-8")
    assert "setenv PATH" in rc_text
    assert "export PATH" not in rc_text
    assert not (home / ".cshrc").exists()


@posix_only
def test_sh_csh_shell_appends_to_existing_cshrc_instead_of_shadowing_it(release_server, tmp_path):
    """tan-cli#490 round 9, the MEASURED bug this fix closes. Shape 2 of 4:
    only ~/.cshrc exists (the review's own repro: SHELL=/bin/csh, a real tcsh
    6.24.10 binary, both ~/.tcshrc and ~/.cshrc seeded). Pre-fix, the bare
    `csh)` arm always wrote ~/.cshrc no matter what, which happens to be
    right when ~/.tcshrc does not exist yet -- so this shape alone would not
    have caught the bug; it is here for completeness alongside the other
    three shapes and to guard the correct branch this fix takes when
    ~/.tcshrc is genuinely absent.
    """
    home = tmp_path / "home"
    home.mkdir()
    cshrc = home / ".cshrc"
    cshrc.write_text("alias ll 'ls -l'\nsetenv EDITOR vim\n", encoding="utf-8")
    dest = tmp_path / "bin"

    result = _install_sh_modify_path(
        release_server, dest, home, "--version", "v0.4.1",
        extra_env={"SHELL": "/bin/csh"},
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not (home / ".tcshrc").exists(), "must not create ~/.tcshrc -- that would shadow the existing ~/.cshrc"
    cshrc_text = cshrc.read_text(encoding="utf-8")
    assert "alias ll 'ls -l'" in cshrc_text, "the user's pre-existing ~/.cshrc content must survive"
    assert "setenv EDITOR vim" in cshrc_text
    assert "setenv PATH" in cshrc_text
    assert "export PATH" not in cshrc_text


@posix_only
def test_sh_csh_shell_appends_to_existing_tcshrc_when_only_tcshrc_exists(release_server, tmp_path):
    """tan-cli#490 round 9. Shape 3 of 4: only ~/.tcshrc exists. tcsh(1) reads
    ~/.tcshrc FIRST when it exists, so the PATH line has to land there, not in
    a freshly-created ~/.cshrc a real tcsh binary invoked as `csh` would never
    read on this host.
    """
    home = tmp_path / "home"
    home.mkdir()
    tcshrc = home / ".tcshrc"
    tcshrc.write_text("alias ll 'ls -l'\nsetenv EDITOR vim\n", encoding="utf-8")
    dest = tmp_path / "bin"

    result = _install_sh_modify_path(
        release_server, dest, home, "--version", "v0.4.1",
        extra_env={"SHELL": "/bin/csh"},
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not (home / ".cshrc").exists(), "must not create ~/.cshrc when ~/.tcshrc is the file actually read"
    tcshrc_text = tcshrc.read_text(encoding="utf-8")
    assert "alias ll 'ls -l'" in tcshrc_text, "the user's pre-existing ~/.tcshrc content must survive"
    assert "setenv EDITOR vim" in tcshrc_text
    assert "setenv PATH" in tcshrc_text
    assert "export PATH" not in tcshrc_text


@posix_only
def test_sh_csh_shell_prefers_tcshrc_when_both_rc_files_exist(release_server, tmp_path):
    """tan-cli#490 round 9. Shape 4 of 4 -- the exact scenario MEASURED
    against real tcsh 6.24.10: both ~/.tcshrc and ~/.cshrc exist, SHELL=
    /bin/csh. tcsh(1) reads ~/.tcshrc and STOPS -- it never reaches
    ~/.cshrc at all when ~/.tcshrc is present -- so the PATH line must land
    in ~/.tcshrc; writing it to ~/.cshrc (the pre-fix behaviour) is exactly
    what `tcsh -c 'echo $PATH' | grep -c binF` measured as 0 despite the
    installer's own success message.
    """
    home = tmp_path / "home"
    home.mkdir()
    tcshrc = home / ".tcshrc"
    tcshrc.write_text("setenv EDITOR vim\n", encoding="utf-8")
    cshrc = home / ".cshrc"
    cshrc.write_text("alias ll 'ls -l'\n", encoding="utf-8")
    dest = tmp_path / "bin"

    result = _install_sh_modify_path(
        release_server, dest, home, "--version", "v0.4.1",
        extra_env={"SHELL": "/bin/csh"},
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    tcshrc_text = tcshrc.read_text(encoding="utf-8")
    assert "setenv EDITOR vim" in tcshrc_text, "the user's pre-existing ~/.tcshrc content must survive"
    assert "setenv PATH" in tcshrc_text
    assert "export PATH" not in tcshrc_text
    # ~/.cshrc must be untouched -- tcsh with a ~/.tcshrc present never reads it.
    assert cshrc.read_text(encoding="utf-8") == "alias ll 'ls -l'\n"


@posix_only
def test_sh_fish_rerun_is_idempotent(release_server, tmp_path):
    """The idempotency guard (`grep -qF "$INSTALL_DIR" "$rc"`) must still
    recognise fish's own line format on a re-run, the same way it does for
    the POSIX `export` line -- otherwise every re-run duplicates the entry.
    """
    home = tmp_path / "home"
    home.mkdir()
    dest = tmp_path / "bin"
    extra_env = {"SHELL": "/usr/bin/fish"}

    first = _install_sh_modify_path(release_server, dest, home, "--version", "v0.4.1", extra_env=extra_env)
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    rc = home / ".config" / "fish" / "config.fish"
    before = rc.read_text(encoding="utf-8")

    second = _install_sh_modify_path(release_server, dest, home, "--version", "v0.4.1", extra_env=extra_env)
    assert second.returncode == 0, f"{second.stdout}\n{second.stderr}"
    assert rc.read_text(encoding="utf-8") == before
    assert "already referenced in" in (second.stdout + second.stderr)


# ---------------------------------------------------------------------------
# tan-cli#678 -- PATH-shadow warning
#
# install.ps1/install.sh finish with "staged binary verified: tan X.Y.Z" and
# exit 0, but on a host that already has a DIFFERENT `tan` earlier on PATH,
# a new shell runs that one instead -- the install is real, but invisible.
# The fix is one warning line, printed right after "installed tan -> ...",
# never a PATH reorder and never a non-zero exit (the install DID succeed).
#
# The decoy `tan` used below is a real shell script placed AHEAD of $dest on
# $PATH for the duration of the subprocess only -- these tests build $PATH by
# hand via `extra_env`, they never touch the real PATH the suite itself runs
# under.
# ---------------------------------------------------------------------------
def _write_decoy_tan(bin_dir: Path, version_line: str = "tan 0.1.0-decoy", exit_code: int = 0) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    decoy = bin_dir / "tan"
    if exit_code == 0:
        decoy.write_text(f'#!/bin/sh\necho "{version_line}"\n', encoding="utf-8", newline="\n")
    else:
        # A decoy that refuses to answer --version at all -- the "cannot run
        # the shadowing binary" half of tan-cli#678's rule 6, exercised
        # without needing a real 5s timeout to prove it.
        decoy.write_text(f'#!/bin/sh\nexit {exit_code}\n', encoding="utf-8", newline="\n")
    decoy.chmod(0o755)
    return decoy


@posix_only
def test_sh_warns_when_a_different_tan_shadows_the_new_install(release_server, tmp_path):
    """tan-cli#678's exact repro, POSIX half: a decoy `tan` sits earlier on
    PATH than the freshly installed one. install.sh must not stay silent
    about it -- and must still exit 0, because the install genuinely
    succeeded.
    """
    dest_dir = tmp_path / "bin"
    decoy_dir = tmp_path / "decoy"
    decoy = _write_decoy_tan(decoy_dir, "tan 0.1.0-decoy")
    # $INSTALL_DIR itself must be on $PATH (ahead of nothing else that
    # matters) for the "already on PATH" trap-4 guard to even look -- the
    # decoy dir comes first so it wins resolution, exactly reproducing the
    # issue's own `where tan` finding.
    path = f"{decoy_dir}{os.pathsep}{dest_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    result = _install_sh(release_server, dest_dir, tmp_path, "--version", "v0.4.1", extra_env={"PATH": path})

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    combined = result.stdout + result.stderr
    assert "WARNING: another tan is earlier on PATH and will shadow this install" in combined
    assert str(decoy) in combined
    assert "reports: tan 0.1.0-decoy" in combined
    assert f"installed here: {dest_dir / 'tan'}" in combined
    assert FIXTURE_VERSION_LINE in combined  # the newly-installed binary's own reported version


@posix_only
def test_sh_no_warning_when_our_own_install_wins_on_path(release_server, tmp_path):
    """The regression this fix must not introduce: a clean install where
    $INSTALL_DIR is on PATH and resolves FIRST (the common case -- most
    installs are not shadowed) must print no warning at all. A false warning
    on every ordinary install would be worse than the bug tan-cli#678 reports.
    """
    dest_dir = tmp_path / "bin"
    decoy_dir = tmp_path / "decoy"
    _write_decoy_tan(decoy_dir, "tan 0.1.0-decoy")
    # dest_dir now comes BEFORE decoy_dir -- our own install wins resolution.
    path = f"{dest_dir}{os.pathsep}{decoy_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    result = _install_sh(release_server, dest_dir, tmp_path, "--version", "v0.4.1", extra_env={"PATH": path})

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    combined = result.stdout + result.stderr
    assert "WARNING" not in combined


@posix_only
def test_sh_no_warning_when_install_dir_is_not_on_path_at_all(release_server, tmp_path):
    """tan-cli#678's named trap: when $INSTALL_DIR was never on PATH at all
    (here, simply because nothing put it there and --no-modify-path is the
    default for these tests), warning about "shadowing" would be wrong --
    something else winning is expected and unremarkable. The pre-existing
    "is not on PATH -- add it yourself" message already covers this state;
    the new warning must not duplicate or contradict it.
    """
    dest_dir = tmp_path / "bin"
    # A decoy IS on PATH (so there is genuinely something else a bare `tan`
    # would resolve to), but $INSTALL_DIR itself never is.
    decoy_dir = tmp_path / "decoy"
    _write_decoy_tan(decoy_dir, "tan 0.1.0-decoy")
    path = f"{decoy_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    result = _install_sh(release_server, dest_dir, tmp_path, "--version", "v0.4.1", extra_env={"PATH": path})

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    combined = result.stdout + result.stderr
    assert "WARNING: another tan is earlier on PATH" not in combined
    assert "is not on PATH -- add:" in combined


@posix_only
def test_sh_shadow_version_report_is_best_effort_on_an_unrunnable_decoy(release_server, tmp_path):
    """tan-cli#678 rule 6: reporting the shadowing binary's version is
    best-effort and must never break the install. A decoy that refuses to
    answer `--version` (exits non-zero, prints nothing) must still produce a
    warning -- with `(reports: could not run)` instead of a blank or a
    crash -- and the install must still exit 0.
    """
    dest_dir = tmp_path / "bin"
    decoy_dir = tmp_path / "decoy"
    decoy = _write_decoy_tan(decoy_dir, exit_code=7)
    path = f"{decoy_dir}{os.pathsep}{dest_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    result = _install_sh(release_server, dest_dir, tmp_path, "--version", "v0.4.1", extra_env={"PATH": path})

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    combined = result.stdout + result.stderr
    assert "WARNING: another tan is earlier on PATH and will shadow this install" in combined
    assert str(decoy) in combined
    assert "reports: could not run" in combined


# ---------------------------------------------------------------------------
# install.ps1's half of tan-cli#678
# ---------------------------------------------------------------------------
def _extract_ps1_shadow_functions() -> str:
    """`Find-FirstTanOnPath` / `Resolve-CanonicalPath` / `Get-TanVersionReport`
    extracted VERBATIM out of install.ps1 -- the same "run the real source,
    never a reimplementation that could silently drift from it" approach
    `test_ps1_access_denied_signature_accepts_the_applocker_policy_codes`
    already uses for `Get-Win32ErrorCode`/`Test-AccessDeniedSignature`. Stops
    before `Get-TanVersionReport`'s closing brace is followed by the
    registry-dependent `$winner = Find-FirstTanOnPath ...` driver code, which
    these tests supply their own (registry-free) inputs for instead.
    """
    text = INSTALL_PS1.read_text()
    start = text.index("function Find-FirstTanOnPath(")
    end = text.index("$winner = Find-FirstTanOnPath $effectiveDirs")
    assert start != -1 and end > start, "install.ps1's PATH-shadow helper functions moved or were renamed"
    return text[start:end]


@pwsh_only
def test_ps1_shadow_functions_detect_an_earlier_tan_on_path(tmp_path):
    """tan-cli#678, Windows half, at the resolution-logic level: given an
    effective PATH (Machine+User dirs, already resolved -- these tests do not
    touch the registry) where a DIFFERENT `tan.exe` sits in an earlier
    directory, `Find-FirstTanOnPath` must find IT, and comparing its
    canonicalised path against the freshly installed one must show a
    mismatch. Runs on every OS this suite runs on (`pwsh_only`, not
    `windows_only`) -- ProcessStartInfo/Get-Item/[System.IO.Path] all work
    identically on POSIX pwsh, and the fixture "tan.exe" files here are
    ordinary shell scripts, not Windows binaries.
    """
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy = decoy_dir / "tan.exe"
    decoy.write_text('#!/bin/sh\necho "tan 0.1.0-decoy"\n', encoding="utf-8", newline="\n")
    decoy.chmod(0o755)
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target = target_dir / "tan.exe"
    target.write_text('#!/bin/sh\necho "tan 0.5.1"\n', encoding="utf-8", newline="\n")
    target.chmod(0o755)

    funcs = _extract_ps1_shadow_functions()
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        'param([string]$DecoyDir, [string]$TargetDir, [string]$Dest)\n'
        '$env:PATHEXT = ".com;.exe;.bat;.cmd"\n'
        f'{funcs}\n'
        '$winner = Find-FirstTanOnPath @($DecoyDir, $TargetDir)\n'
        'if (-not $winner) { Write-Output "NO_WINNER"; exit 0 }\n'
        'Write-Output "WINNER=$winner"\n'
        '$winnerCanon = Resolve-CanonicalPath $winner\n'
        '$destCanon = Resolve-CanonicalPath $Dest\n'
        'if ($winnerCanon -ine $destCanon) {\n'
        '    $v = Get-TanVersionReport $winner\n'
        '    Write-Output "SHADOWED reports=$v"\n'
        '} else {\n'
        '    Write-Output "NOT_SHADOWED"\n'
        '}\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe),
         "-DecoyDir", str(decoy_dir), "-TargetDir", str(target_dir), "-Dest", str(target)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"WINNER={decoy}" in result.stdout

    # The load-bearing assertion is SHADOWED -- the warning must fire. The
    # version is best-effort by design (`Get-TanVersionReport` is timeout- and
    # failure-guarded precisely so a hung or unrunnable binary cannot break an
    # install), so both outcomes are correct and which one occurs is a property
    # of the HOST, not of the code under test:
    #
    #   POSIX pwsh -- a `#!/bin/sh` file named `tan.exe` is executable, so the
    #                 probe reads `tan 0.1.0-decoy`.
    #   real Windows -- a shell script named `tan.exe` is NOT executable, so
    #                 the probe correctly falls back to `could not run`.
    #
    # Asserting only the first spelling is what reddened windows-latest 2/4 on
    # tan-cli#678's first CI run, on a fixture whose own docstring says these
    # are "ordinary shell scripts, not Windows binaries". Pinning the fallback
    # too keeps the graceful-degradation path covered rather than deleting the
    # case on Windows.
    assert "SHADOWED reports=" in result.stdout, result.stdout
    assert (
        "SHADOWED reports=tan 0.1.0-decoy" in result.stdout
        or "SHADOWED reports=could not run" in result.stdout
    ), result.stdout


@pwsh_only
def test_ps1_shadow_functions_no_false_positive_when_our_install_wins(tmp_path):
    """The regression this fix must not introduce, at the same logic level as
    the test above: when the freshly-installed `tan.exe`'s OWN directory
    resolves first, `Find-FirstTanOnPath` must land on it, and the
    canonicalised-path comparison must show NOT shadowed -- the false-warning
    direction that would make a clean install noisy.
    """
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target = target_dir / "tan.exe"
    target.write_text('#!/bin/sh\necho "tan 0.5.1"\n', encoding="utf-8", newline="\n")
    target.chmod(0o755)
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy = decoy_dir / "tan.exe"
    decoy.write_text('#!/bin/sh\necho "tan 0.1.0-decoy"\n', encoding="utf-8", newline="\n")
    decoy.chmod(0o755)

    funcs = _extract_ps1_shadow_functions()
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        'param([string]$TargetDir, [string]$DecoyDir, [string]$Dest)\n'
        '$env:PATHEXT = ".com;.exe;.bat;.cmd"\n'
        f'{funcs}\n'
        '$winner = Find-FirstTanOnPath @($TargetDir, $DecoyDir)\n'
        'if (-not $winner) { Write-Output "NO_WINNER"; exit 0 }\n'
        'Write-Output "WINNER=$winner"\n'
        '$winnerCanon = Resolve-CanonicalPath $winner\n'
        '$destCanon = Resolve-CanonicalPath $Dest\n'
        'if ($winnerCanon -ine $destCanon) {\n'
        '    Write-Output "SHADOWED"\n'
        '} else {\n'
        '    Write-Output "NOT_SHADOWED"\n'
        '}\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe),
         "-TargetDir", str(target_dir), "-DecoyDir", str(decoy_dir), "-Dest", str(target)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"WINNER={target}" in result.stdout
    assert "NOT_SHADOWED" in result.stdout


def test_ps1_shadow_check_is_gated_on_effective_path_and_wrapped_non_fatal():
    """tan-cli#678's own trap 4 (never warn when $Dir is not on PATH at all --
    the -NoModifyPath/declined case, already covered by the pre-existing
    "is not on the ... Path -- add it yourself" message) and rule 6 (the
    whole check must be non-fatal) are both structural properties this test
    checks directly against the shipped source, the same pure-text approach
    `test_ps1_broadcast_helper_compile_is_gated_and_guarded` uses -- no pwsh
    needed, runs on every OS.
    """
    text = INSTALL_PS1.read_text()
    marker_start = text.index("# tan-cli#678: the health check above proves the STAGED binary runs")
    try_at = text.index("try {", marker_start)
    gate_at = text.index("$dirIsOnEffectivePath = $alreadyPresent -or (-not $NoModifyPath)", marker_start)
    warn_at = text.index('Write-Host "install.ps1: WARNING: another tan is earlier on PATH', gate_at)
    # The OUTER catch (rule 6) -- not one of Resolve-CanonicalPath's or
    # Get-TanVersionReport's own internal `} catch { }` blocks, several of
    # which appear (and close) BEFORE the warning text in file order. Found
    # via its own comment (which only the outer one carries), walking
    # backward to the `} catch {` immediately preceding it.
    outer_catch_comment_at = text.index("# Non-fatal, deliberately (tan-cli#678)", warn_at)
    catch_at = text.rindex("} catch {", 0, outer_catch_comment_at)
    assert try_at < gate_at < warn_at < catch_at, (
        "the PATH-shadow check must compute $dirIsOnEffectivePath and print its warning "
        "INSIDE one try/catch, so a bug in it can never fail an install that already succeeded"
    )
    # rule 4: the warning must be reached only when the effective-PATH gate is true --
    # i.e. the `if ($dirIsOnEffectivePath)` guard must wrap the warning, not just precede it.
    if_at = text.index("if ($dirIsOnEffectivePath) {", gate_at)
    assert if_at < warn_at < catch_at
