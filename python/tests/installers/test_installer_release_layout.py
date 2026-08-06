# SPDX-License-Identifier: Apache-2.0
"""The installers must install whatever shape the RESOLVED release publishes.

tan-cli#356. #349 switched the release to PyInstaller ``--onedir`` archives and
both installers then requested the new names UNCONDITIONALLY --
``tan-<triple>.tar.gz`` from ``install.sh``, ``tan-<triple>.zip`` from
``install.ps1``. No published tag has those assets, so the documented install
command 404'd on every tag that exists: ``v0.4.1`` (what ``latest`` resolves to)
and the ``v0.5.0-rc4`` pre-release both publish RAW binaries.

The fixture releases below mirror the REAL published asset lists name for name
(``gh release view <tag> --repo alplabai/tan-cli --json assets``, read while
writing this), so a pass here is a claim about the real thing rather than about
a shape invented for the test:

===============  ===========================================================
``v0.4.1``       8 raw assets -- the last Rust release, and today's ``latest``
``v0.5.0-rc4``   4 raw assets -- the ``--onefile`` freeze; no musl, no
                 linux/arm64
``v0.5.0``       4 ARCHIVES -- the first tag that publishes them. **Not cut
                 yet**, which is exactly why archive extraction is covered by a
                 fixture and not by a live download.
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
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"

#: The FIRST tag that publishes ``--onedir`` archives. It is a real, planned
#: tag that has not been cut -- deliberately NOT ``v0.5.0-rc4``, whose published
#: assets are raw (that mistake is the documentation half of #356). When v0.5.0
#: ships, this constant is the one thing that has to stay true.
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
