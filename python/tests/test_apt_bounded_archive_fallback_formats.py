# SPDX-License-Identifier: Apache-2.0
"""M3: `scripts/ci/apt-bounded.sh`'s tan-cli#1257 archive fallback must find
and rewrite a failing URI regardless of WHICH sources-file format names it --
a flat `sources.list.d/*.list` entry, or a deb822 `*.sources` stanza's
`URIs:` field, including one authored with CRLF line endings.

Split from `test_apt_bounded_archive_fallback.py` to keep both files under
`tests/gates/test_module_size_budget.py`'s 800-line cap; shared fixtures are
imported from there rather than duplicated.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.test_apt_bounded_archive_fallback import (
    _ALL_THREE_GONE_ON_DEB,
    _ARCHIVE_HOST,
    _DEB_HOST,
    _archive_env,
    _bullseye_os_release,
    _write_apt_shim,
)
from tests.test_apt_bounded_wrapper import _WRAPPER, needs_the_wrappers_own_tools


# `_ALL_THREE_GONE_ON_DEB`'s own precondition grep requires a TRAILING SPACE
# after `deb.debian.org/debian` -- correct for a flat `deb <URI> <suite> ...`
# line, where the URI is followed by a space, but wrong for a deb822
# `URIs: <uri>` line, where the URI is the LAST token before the newline (no
# trailing space at all). This variant drops that requirement; it is only
# ever used with fixtures where every gone URI is expected to move together
# in one event anyway (no need to distinguish "/debian" from
# "/debian-security" in the precondition itself, only in the real rewrite
# logic under test).
_ALL_THREE_GONE_ON_DEB822 = _ALL_THREE_GONE_ON_DEB.replace(
    "grep -q 'deb\\.debian\\.org/debian '", "grep -q 'deb\\.debian\\.org'"
)


def _apt_root_with_list_d_entry(tmp_path: Path, *, name: str = "apt-root-list-d") -> Path:
    """An `/etc/apt`-shaped tree with an EMPTY `sources.list` and the real
    three bullseye lines living in `sources.list.d/debian.list` instead --
    the shape `debian-archive-keyring`-managed images sometimes use.
    """
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "sources.list").write_text("", encoding="utf-8")
    list_d = root / "sources.list.d"
    list_d.mkdir()
    (list_d / "debian.list").write_text(
        "deb http://deb.debian.org/debian bullseye main\n"
        "deb http://deb.debian.org/debian-security bullseye-security main\n"
        "deb http://deb.debian.org/debian bullseye-updates main\n",
        encoding="utf-8",
    )
    return root


def _apt_root_with_deb822_sources(tmp_path: Path, *, crlf: bool, name: str) -> Path:
    """An `/etc/apt`-shaped tree with the real three bullseye suites as ONE
    deb822 `sources.list.d/debian.sources` stanza-per-suite file, the modern
    format `debian-archive-keyring` >= 12.9 installs by default. `crlf`
    reproduces a Windows-authored or otherwise CRLF-line-ended `.sources`
    file -- measured (`od -c`): without the wrapper's own CR trim, the LAST
    field on a `URIs: <uri>` line carries a trailing `\\r` baked into its
    value, so a byte-exact field match silently never fires.
    """
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "sources.list").write_text("", encoding="utf-8")
    list_d = root / "sources.list.d"
    list_d.mkdir()
    body = (
        "Types: deb\n"
        "URIs: http://deb.debian.org/debian\n"
        "Suites: bullseye bullseye-updates\n"
        "Components: main\n"
        "\n"
        "Types: deb\n"
        "URIs: http://deb.debian.org/debian-security\n"
        "Suites: bullseye-security\n"
        "Components: main\n"
    )
    if crlf:
        body = body.replace("\n", "\r\n")
    (list_d / "debian.sources").write_bytes(body.encode("utf-8"))
    return root


@needs_the_wrappers_own_tools
def test_a_sources_list_d_list_file_is_rewritten(tmp_path: Path) -> None:
    """The failing URI lives in `sources.list.d/debian.list`, not the flat
    (here empty) `sources.list` -- the `.d` directory must be searched too.
    """
    sources_root = _apt_root_with_list_d_entry(tmp_path)
    bindir = _write_apt_shim(tmp_path, _ALL_THREE_GONE_ON_DEB)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="list-d-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    # The fake apt-get greps the flat `sources.list` by default; point it at
    # the `.list` file that actually carries the URIs for this fixture.
    env["FAKE_APT_SOURCES_FILE"] = str(sources_root / "sources.list.d" / "debian.list")

    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    rewritten = (sources_root / "sources.list.d" / "debian.list").read_text(encoding="utf-8")
    assert _DEB_HOST not in rewritten, f"got:\n{rewritten}"
    assert rewritten.count(_ARCHIVE_HOST) == 3, f"got:\n{rewritten}"


@needs_the_wrappers_own_tools
def test_a_deb822_sources_file_uris_field_is_rewritten(tmp_path: Path) -> None:
    """The failing URI lives in a deb822 `URIs:` field, not a one-line `deb`
    entry -- both source-file syntaxes must be handled.
    """
    sources_root = _apt_root_with_deb822_sources(tmp_path, crlf=False, name="apt-root-deb822")
    bindir = _write_apt_shim(tmp_path, _ALL_THREE_GONE_ON_DEB822)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="deb822-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    env["FAKE_APT_SOURCES_FILE"] = str(sources_root / "sources.list.d" / "debian.sources")

    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    rewritten = (sources_root / "sources.list.d" / "debian.sources").read_text(encoding="utf-8")
    assert _DEB_HOST not in rewritten, f"got:\n{rewritten}"
    assert "URIs: http://archive.debian.org/debian\n" in rewritten, f"got:\n{rewritten}"
    assert "URIs: http://archive.debian.org/debian-security\n" in rewritten, f"got:\n{rewritten}"
    # The rest of each stanza (Types/Suites/Components) must survive untouched.
    assert "Suites: bullseye bullseye-updates" in rewritten, f"got:\n{rewritten}"
    assert "Suites: bullseye-security" in rewritten, f"got:\n{rewritten}"


def _apt_root_with_deb822_two_uris_one_stanza(tmp_path: Path, *, name: str = "apt-root-deb822-two-uris") -> Path:
    """A single deb822 stanza whose `URIs:` field names TWO mirrors -- a real,
    supported shape (apt tries each URI in order as a fallback), distinct
    from the two-STANZA fixture above where each URI has its own field.
    """
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "sources.list").write_text("", encoding="utf-8")
    list_d = root / "sources.list.d"
    list_d.mkdir()
    body = (
        "Types: deb\n"
        "URIs: http://deb.debian.org/debian http://mirror.example.invalid/debian\n"
        "Suites: bullseye\n"
        "Components: main\n"
    )
    (list_d / "debian.sources").write_text(body, encoding="utf-8")
    return root


@needs_the_wrappers_own_tools
def test_a_deb822_uris_field_with_two_mirrors_touches_only_the_gone_one(tmp_path: Path) -> None:
    """tan-cli#1257 review, test gap #5: a `URIs:` line naming two mirrors
    where only ONE is reported gone -- the untouched mirror must survive the
    rewrite verbatim, in the SAME field as the one that moves.
    """
    sources_root = _apt_root_with_deb822_two_uris_one_stanza(tmp_path)
    # Unconditional and always-404, deliberately: this fixture is about the
    # REWRITE mechanics on one attempt, not a realistic multi-attempt apt
    # session (a real apt would try the second, still-`deb.debian.org`-free
    # mirror next and may never re-report the first at all). rc after the
    # second attempt is not asserted for that reason -- only that the file
    # ends up correct and the wrapper never crashes getting there.
    body = """
cat <<'EOF'
Err:1 http://deb.debian.org/debian bullseye Release
  404  Not Found [IP: 127.0.0.1 8081]
E: The repository 'http://deb.debian.org/debian bullseye Release' does not have a Release file.
EOF
exit 100
"""
    bindir = _write_apt_shim(tmp_path, body)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="deb822-two-uris-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    env["FAKE_APT_SOURCES_FILE"] = str(sources_root / "sources.list.d" / "debian.sources")
    env["APT_ATTEMPTS"] = "2"

    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 100, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "unbound variable" not in proc.stderr, f"stderr:\n{proc.stderr}"
    rewritten = (sources_root / "sources.list.d" / "debian.sources").read_text(encoding="utf-8")
    assert "URIs: http://archive.debian.org/debian http://mirror.example.invalid/debian" in rewritten, (
        f"the gone mirror must move and the untouched one must survive "
        f"verbatim, in the same field. got:\n{rewritten}"
    )
    assert "mirror.example.invalid" in rewritten, (
        f"the second mirror must not have been dropped or altered. got:\n{rewritten}"
    )


@needs_the_wrappers_own_tools
def test_a_crlf_deb822_sources_file_is_still_rewritten(tmp_path: Path) -> None:
    """M3's sharpest case: a CRLF-authored deb822 file. Without stripping the
    trailing `\\r` before comparing fields, the URI's last field would carry
    a dangling `\\r` and never byte-compare equal to the clean target -- the
    fix would silently become a no-op on this file specifically.
    """
    sources_root = _apt_root_with_deb822_sources(tmp_path, crlf=True, name="apt-root-deb822-crlf")
    bindir = _write_apt_shim(tmp_path, _ALL_THREE_GONE_ON_DEB822)
    osr = _bullseye_os_release(tmp_path)
    env = _archive_env(tmp_path, step="deb822-crlf-step", sources_root=sources_root, os_release=osr, bindir=bindir)
    env["FAKE_APT_SOURCES_FILE"] = str(sources_root / "sources.list.d" / "debian.sources")

    proc = subprocess.run(
        ["bash", str(_WRAPPER), "update", "-qq"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    rewritten = (sources_root / "sources.list.d" / "debian.sources").read_text(encoding="utf-8")
    assert _DEB_HOST not in rewritten, (
        f"a CRLF deb822 file left the fallback a silent no-op. got:\n{rewritten!r}"
    )
    assert "URIs: http://archive.debian.org/debian" in rewritten, f"got:\n{rewritten!r}"
    assert "URIs: http://archive.debian.org/debian-security" in rewritten, f"got:\n{rewritten!r}"
