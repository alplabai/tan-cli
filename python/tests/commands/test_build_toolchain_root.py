# SPDX-License-Identifier: Apache-2.0
"""tan-cli#547: `${TOOLCHAIN_ROOT}` had a recogniser and no resolver.

`build_cmd` passed `toolchain_root=None` unconditionally, so EVERY slice
naming the token demoted on EVERY host -- not, as ADR 0021 and the stub's own
comment both claim, only where the host has no detectable toolchain.

Every expectation below is MEASURED against the frozen Rust oracle
(`target/debug/tan`, `tan 0.4.1`) by materialising a `planPathMode: tokened`
plan whose one `configArtefacts[].contents` is
`TOOLCHAIN_ROOT=${TOOLCHAIN_ROOT}` and reading the file back -- never by
reading `crates/`. The two places this port deliberately does NOT match the
oracle are marked as such, with the reason, in
`tan/commands/build/toolchain.py`'s module docstring and in the two tests
named `..._diverges_from_the_oracle_...` below.

The resolver's own branches are exercised against a monkeypatched
`_scan_roots`, not the real one: `/opt` is a real scan root on the host
running this suite and a developer box with `/opt/zephyr-sdk-0.16.5` would
otherwise flip half of these. `_scan_roots` itself is pinned separately, by
composition.
"""
import json

import pytest

from tan.commands.build.toolchain import (
    NO_TOOLCHAIN_ADVICE,
    ToolchainResolution,
    _scan_roots,
    resolve_toolchain_root,
)
from tan.commands.build_cmd import _toolchain_for_plan


def _install(parent, name="zephyr-sdk-1.0.1"):
    """A toolchain install as the oracle recognises one: a DIRECTORY whose
    name starts `zephyr-sdk`, with no contents requirement whatsoever
    (measured -- an empty `zephyr-sdk-9.9.9` resolved)."""
    root = parent / name
    root.mkdir(parents=True)
    return root


@pytest.fixture
def scan(monkeypatch, tmp_path):
    """Bind `_scan_roots` to one controlled directory and return it."""
    root = tmp_path / "scan"
    root.mkdir()
    monkeypatch.setattr("tan.commands.build.toolchain._scan_roots", lambda: [root])
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    return root


# --- the scan ---------------------------------------------------------------


def test_exactly_one_install_resolves_to_it(scan):
    """The case tan-cli#547 asks for by name: a host that HAS a toolchain
    must RESOLVE, not demote. Measured on the oracle with a real
    `$HOME/zephyr-sdk-1.0.1`: `TOOLCHAIN_ROOT=/home/<user>/zephyr-sdk-1.0.1`.

    PRE-FIX this module does not exist at all, so this test errors on the
    import; the behavioural pre-fix proof is the end-to-end case in
    `test_build_command.py`."""
    install = _install(scan)
    assert resolve_toolchain_root() == ToolchainResolution(install.as_posix())


def test_no_install_is_unresolved_with_the_no_toolchain_advice(scan):
    """Measured: with `$HOME` pointed at a directory holding no install, the
    oracle demoted the slice and its reason was verbatim this advice."""
    resolved = resolve_toolchain_root()
    assert resolved.root is None
    assert resolved.advice == NO_TOOLCHAIN_ADVICE


def test_several_installs_are_unresolved_with_their_own_sharper_advice(scan):
    """The branch the port had NO equivalent for. Measured on the oracle with
    two installs under `$HOME` and no `ZEPHYR_SDK_INSTALL_DIR`:

        this host has several toolchain installs and no
        `ZEPHYR_SDK_INSTALL_DIR` to choose between them (<a>, <b>) -- set
        `ZEPHYR_SDK_INSTALL_DIR` to the one this build should use

    Distinct from the no-toolchain wording because the FIX is distinct:
    nothing needs installing here, one of these needs choosing. Collapsing
    both onto "no toolchain install is detectable" -- which is what a naive
    port of the value alone would have done -- tells a user with two SDKs
    installed to install an SDK."""
    a = _install(scan, "zephyr-sdk-0.16.5")
    b = _install(scan, "zephyr-sdk-1.0.1")
    resolved = resolve_toolchain_root()
    assert resolved.root is None
    assert resolved.advice != NO_TOOLCHAIN_ADVICE
    assert "several toolchain installs" in resolved.advice
    assert a.as_posix() in resolved.advice
    assert b.as_posix() in resolved.advice


def test_the_several_installs_advice_lists_them_sorted(scan):
    """Measured: created in readdir order `zzz, mmm, aaa`, the oracle
    reported them `aaa, mmm, zzz`. A set-ordered list would make the same
    host produce a different message run to run."""
    for name in ("zephyr-sdk-zzz", "zephyr-sdk-mmm", "zephyr-sdk-aaa"):
        _install(scan, name)
    advice = resolve_toolchain_root().advice
    assert advice.index("zephyr-sdk-aaa") < advice.index("zephyr-sdk-mmm") < advice.index(
        "zephyr-sdk-zzz"
    )


def test_the_name_match_is_a_prefix_not_a_version_shape(scan):
    """Measured: a directory named `zephyr-sdkXYZ` -- no dash, no version --
    resolved."""
    install = _install(scan, "zephyr-sdkXYZ")
    assert resolve_toolchain_root().root == install.as_posix()


def test_an_install_one_level_deeper_is_not_found(scan):
    """Measured: `$HOME/tools/zephyr-sdk-1.0.1` did NOT resolve. Direct
    children of a scan root only -- this is the guard that keeps the scan
    from becoming a whole-home-directory walk."""
    _install(scan / "tools", "zephyr-sdk-1.0.1")
    assert resolve_toolchain_root().root is None


# ---------------------------------------------------------------------------
# The ADR 0021 artifact-keyed store (tan-cli#990 review MAJOR)
# ---------------------------------------------------------------------------
#
# `scan` above does NOT neutralise this -- only `_scan_roots`. Hermeticity
# instead comes from `tests/conftest.py`'s autouse `_scrub_sdk_discovery_env`,
# which points `HOME`/`USERPROFILE` at a fresh, empty `tmp_path_factory`
# directory for every test and scrubs `ALP_TOOLCHAIN_ROOT`, so
# `_toolchain_store_scan_root()` resolves to a `.alp/toolchains` that does
# not exist yet unless a test below creates it.


def test_a_toolchain_bootstrap_installed_is_found_without_any_env_var(monkeypatch):
    """The gap this whole section closes: before it, a customer who ran
    NOTHING but `tan bootstrap` (issue #474's entire point) still could not
    get `${TOOLCHAIN_ROOT}` to resolve without ALSO hand-exporting
    `ZEPHYR_SDK_INSTALL_DIR` -- `tan.core.toolchain_provision.store_dir_name`
    nests two directory levels below every root `_scan_roots` looked at."""
    from tan.commands.build import toolchain as bt

    home = bt._toolchain_store_scan_root()
    install = home / "zephyr-sdk-1.0.1-arm-zephyr-eabi"
    install.mkdir(parents=True)
    monkeypatch.setattr(bt, "_scan_roots", lambda: [])
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)

    assert resolve_toolchain_root() == ToolchainResolution(install.as_posix())


def test_a_tmp_wreckage_sibling_in_the_store_is_never_a_candidate(monkeypatch):
    """A `.tmp-<pid>` sibling of an interrupted `tan bootstrap` acquisition
    also starts with `zephyr-sdk` -- without the exclusion this would fake a
    SECOND, ambiguous candidate next to the one real, verified store entry
    on a host where an acquisition was interrupted and not yet reclaimed."""
    from tan.commands.build import toolchain as bt

    home = bt._toolchain_store_scan_root()
    install = home / "zephyr-sdk-1.0.1-arm-zephyr-eabi"
    install.mkdir(parents=True)
    wreckage = home / "zephyr-sdk-1.0.1-arm-zephyr-eabi.tmp-99999"
    wreckage.mkdir(parents=True)
    monkeypatch.setattr(bt, "_scan_roots", lambda: [])
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)

    assert resolve_toolchain_root() == ToolchainResolution(install.as_posix())


def test_alp_toolchain_root_override_is_scanned_too(monkeypatch, tmp_path):
    """`$ALP_TOOLCHAIN_ROOT` (ADR 0021's own escape hatch for bench/CI
    machines) redirects the store scan exactly the way it redirects
    `tan bootstrap`'s own install -- `_toolchain_store_scan_root` delegates
    to the SAME `toolchain_provision.resolve_toolchain_root` bootstrap
    calls, so the two can never name a different root."""
    from tan.commands.build import toolchain as bt

    adopted_root = tmp_path / "bench-cache"
    install = adopted_root / "zephyr-sdk-1.0.1-arm-zephyr-eabi"
    install.mkdir(parents=True)
    monkeypatch.setenv("ALP_TOOLCHAIN_ROOT", str(adopted_root))
    monkeypatch.setattr(bt, "_scan_roots", lambda: [])
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)

    assert resolve_toolchain_root() == ToolchainResolution(install.as_posix())


def test_a_candidate_is_never_validated_for_contents(scan):
    """Measured: an EMPTY `zephyr-sdk-9.9.9` -- no `gnu/arm-zephyr-eabi/bin/
    arm-zephyr-eabi-gcc`, nothing at all -- resolved on the oracle.

    `doctor_cmd._zephyr_sdk_root_valid`'s stricter probe is deliberately NOT
    reused here. Doctor answers a yes/no health question where a false PASS
    is the harm (tan-cli#286); here the harm runs the other way -- a Zephyr
    SDK installed with a non-ARM toolchain subset is a real install, and
    refusing it would demote every slice on that host, which is the exact
    defect tan-cli#547 reports."""
    install = _install(scan, "zephyr-sdk-9.9.9")
    assert not (install / "gnu").exists()
    assert resolve_toolchain_root().root == install.as_posix()


def test_a_plain_file_is_not_a_candidate_diverges_from_the_oracle(scan):
    """DELIBERATE DIVERGENCE. Measured: the oracle accepts a plain FILE named
    `zephyr-sdk-1.0.1` as a toolchain root.

    Not a harmless wart. `"zephyr-sdk-0.16.5_linux-x86_64.tar.xz".startswith(
    "zephyr-sdk")` is true, so the downloaded ARCHIVE left in `$HOME` beside
    the install extracted from it makes the host look like it has TWO
    installs -- and the several-installs branch then demotes every slice on a
    host with exactly one working toolchain, which is the failure this whole
    issue is about. Requiring a directory can only REMOVE a false candidate,
    never add one, so it cannot itself cause a demotion."""
    (scan / "zephyr-sdk-0.16.5_linux-x86_64.tar.xz").write_text("", encoding="utf-8")
    install = _install(scan, "zephyr-sdk-0.16.5")
    assert resolve_toolchain_root().root == install.as_posix()


def test_the_same_install_reached_twice_is_one_candidate(monkeypatch, tmp_path):
    """`_scan_roots` deliberately OVERLAPS -- `$HOME`, `%USERPROFILE%` and
    `Path.home()` routinely name the same directory. Without a
    candidate-level dedup that overlap alone reports "several installs" on a
    host with exactly one, i.e. the fix would recreate the bug it closes."""
    home = tmp_path / "home"
    home.mkdir()
    install = _install(home)
    link = tmp_path / "home-alias"
    try:
        link.symlink_to(home, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover -- Windows w/o privilege
        pytest.skip("this host cannot create a directory symlink")
    monkeypatch.setattr("tan.commands.build.toolchain._scan_roots", lambda: [home, link])
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    assert resolve_toolchain_root().root == install.as_posix()


def test_an_unreadable_scan_root_is_nothing_found_not_a_failure(monkeypatch, tmp_path):
    """A missing or unreadable scan root must never take a build down: `/opt`
    does not exist on every host."""
    monkeypatch.setattr(
        "tan.commands.build.toolchain._scan_roots", lambda: [tmp_path / "does-not-exist"]
    )
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    assert resolve_toolchain_root().root is None


# --- ZEPHYR_SDK_INSTALL_DIR -------------------------------------------------


def test_the_env_var_wins_over_the_scan(scan, monkeypatch, tmp_path):
    """Measured: with two installs under `$HOME` -- an ambiguous host that
    would otherwise demote -- and `ZEPHYR_SDK_INSTALL_DIR` naming one of
    them, the oracle resolved that one."""
    _install(scan, "zephyr-sdk-0.16.5")
    _install(scan, "zephyr-sdk-1.0.1")
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(pinned))
    assert resolve_toolchain_root().root == str(pinned)


def test_the_env_var_is_returned_verbatim_trailing_slash_and_all(scan, monkeypatch, tmp_path):
    """Measured: `ZEPHYR_SDK_INSTALL_DIR=/home/<user>/zephyr-sdk-1.0.1/`
    substituted with the trailing slash intact. An operator who pinned this
    variable pinned a literal path; rewriting it would make the substituted
    value disagree with what they set."""
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    raw = f"{pinned}/"
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", raw)
    assert resolve_toolchain_root().root == raw


def test_the_env_var_is_honoured_on_existence_alone(scan, monkeypatch, tmp_path):
    """Measured: an EXISTING but completely empty directory was accepted
    verbatim -- the oracle probes existence, not contents. Same reasoning as
    `test_a_candidate_is_never_validated_for_contents`, and here it matters
    more: this value was set deliberately."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(empty))
    assert resolve_toolchain_root().root == str(empty)


def test_a_nonexistent_env_var_falls_through_to_the_scan(scan, monkeypatch, tmp_path):
    """Measured: with `ZEPHYR_SDK_INSTALL_DIR` naming a directory that does
    not exist and a real install under `$HOME`, the oracle resolved the
    SCANNED one -- a stale variable left over from an uninstalled SDK does
    not blind the host to the SDK it does have."""
    install = _install(scan)
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(tmp_path / "gone"))
    assert resolve_toolchain_root().root == install.as_posix()


def test_an_empty_env_var_falls_through_to_the_scan(scan, monkeypatch):
    """Measured: `ZEPHYR_SDK_INSTALL_DIR=` (exported empty, which a shell
    profile does by accident all the time) resolved the scanned install."""
    install = _install(scan)
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", "")
    assert resolve_toolchain_root().root == install.as_posix()


# --- the scan roots themselves ---------------------------------------------


def test_scan_roots_covers_opt_home_userprofile_and_path_home(monkeypatch, tmp_path):
    """DELIBERATE DIVERGENCE, pinned by composition rather than by outcome.

    Measured, the oracle reads `$HOME` ONLY: with `HOME` on an SDK-less
    directory and `USERPROFILE` on the real one it resolved nothing.
    Reproducing that exactly would re-open the defect
    `doctor_cmd._zephyr_sdk_scan_roots` was written for -- under Git
    Bash/MSYS on Windows `HOME` is a POSIX-translated path (`/c/Users/dev`)
    while the SDK sits under the native `%USERPROFILE%`, and a real host was
    measured reporting "no SDK" with the SDK installed. On any ordinary POSIX
    host the two sets are identical, so the measured Linux parity is intact.
    """
    home = tmp_path / "home"
    profile = tmp_path / "profile"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(profile))
    roots = [str(r) for r in _scan_roots()]
    assert "/opt" in roots or "\\opt" in roots
    assert str(home) in roots
    assert str(profile) in roots


def test_scan_roots_does_not_repeat_a_root_named_twice(monkeypatch, tmp_path):
    """`HOME == USERPROFILE` is the norm on POSIX once the autouse conftest
    fixture sets both. A repeated root is not a correctness bug by itself
    (candidates dedup downstream) but it doubles the syscalls on every
    resolution, so it is pinned here."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    roots = [str(r) for r in _scan_roots()]
    assert roots.count(str(home)) == 1


# --- laziness ---------------------------------------------------------------


_PLAN_WITHOUT_TOKEN = {
    "schemaVersion": 1,
    "generatedBy": "tests",
    "boardYaml": "board.yaml",
    "sku": "E1M-TEST",
    "buildRoot": "build",
    "planPathMode": "tokened",
    "sharedArtefacts": [],
    "warnings": [],
    "slices": [],
}


def _explode():  # pragma: no cover -- the point is that it is never called
    raise AssertionError("resolve_toolchain_root ran for a plan that names no token")


def test_a_plan_naming_no_token_never_resolves_a_toolchain(monkeypatch):
    """The lazy-resolution property tan-cli#547 asks to be PRESERVED by the
    port, not just claimed: every plan the SDK emits today names no
    `${TOOLCHAIN_ROOT}`, and none of those builds may pay for a filesystem
    walk of `/opt` and `$HOME` -- nor inherit the resolver's failure modes
    (an ambiguous host) for a value they would never have consumed."""
    monkeypatch.setattr("tan.commands.build_cmd.resolve_toolchain_root", _explode)
    assert _toolchain_for_plan(json.dumps(_PLAN_WITHOUT_TOKEN)) == ToolchainResolution(None)


def test_a_plan_naming_the_token_does_resolve(monkeypatch):
    sentinel = ToolchainResolution("/sentinel/zephyr-sdk-1.0.1")
    monkeypatch.setattr("tan.commands.build_cmd.resolve_toolchain_root", lambda: sentinel)
    doc = dict(_PLAN_WITHOUT_TOKEN, boardYaml="${TOOLCHAIN_ROOT}/board.yaml")
    assert _toolchain_for_plan(json.dumps(doc)) is sentinel


def test_the_token_is_found_through_a_json_escaped_dollar(monkeypatch):
    """A raw substring search over the plan TEXT would miss `\\u0024`, while
    `substitute_plan_tokens` -- which sees the DECODED string -- would not,
    leaving the slice demoted on a host that has a toolchain. The check runs
    over the re-serialised document for exactly this reason."""
    sentinel = ToolchainResolution("/sentinel/zephyr-sdk-1.0.1")
    monkeypatch.setattr("tan.commands.build_cmd.resolve_toolchain_root", lambda: sentinel)
    text = json.dumps(_PLAN_WITHOUT_TOKEN).replace(
        '"boardYaml": "board.yaml"', '"boardYaml": "\\u0024{TOOLCHAIN_ROOT}/board.yaml"'
    )
    assert '\\u0024' in text
    assert _toolchain_for_plan(text) is sentinel
