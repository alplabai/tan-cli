# SPDX-License-Identifier: Apache-2.0
"""Regression cover for the two `tan bootstrap` paths that destroy a user's work.

Both were found by a deep review of `dev` at `ac79d4c` and both are filed as
`safety`:

- **tan-cli#390** -- `ensure_venv` `rm -rf`'d the `.venv` inside a workspace
  `_select_workspace` had just ADOPTED, three lines under a comment promising
  never to modify the user's tree. The trigger is a venv with no usable pip,
  which is the ordinary shape of `uv venv` and `python -m venv --without-pip`.
- **tan-cli#389** -- `--workspace` short-circuited the adoption probe, so
  nothing noticed the checkout was the manifest repo of a LIVE west workspace;
  the relocation renamed it out from under `<ws>/.west/config`.

These live in their own module rather than `test_bootstrap_command.py` (already
2256 lines, tan-cli#408) so the destructive-path cover stays findable as a set.
The helpers are imported from that module deliberately -- a second copy of
`run_tan`/`make_sdk` is exactly the drift tan-cli#393 was filed for.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from tan.commands import bootstrap_cmd
from tan.commands.bootstrap_cmd import (
    PIP_ABSENT,
    PIP_INCONCLUSIVE,
    PIP_USABLE,
    HostPython,
    Log,
    Runner,
    Workspace,
    _probe_venv_pip,
    ensure_venv,
    fallback_facts,
)

from tests.commands.test_bootstrap_command import (
    PRESENT_TOOL,
    codes,
    envelope,
    make_sdk,
    run_tan,
)

#: The Zephyr the bundled test manifest pins. A tree on this version whose
#: `.west/config` names the checkout is what `decide_workspace_reuse` calls
#: REUSE -- the adoption that then made the venv delete reachable.
PINNED_VERSION_FILE = "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 1\nEXTRAVERSION =\n"

#: `venv_bin()` picks the directory that EXISTS, POSIX first.
BIN_DIR = "Scripts" if sys.platform == "win32" else "bin"
PYTHON_NAME = "python.exe" if sys.platform == "win32" else "python"


def _host() -> HostPython:
    """The interpreter running the suite -- above the effective floor by
    `pyproject.toml`'s `requires-python`, so no host gate fires on it."""
    return HostPython(argv=(sys.executable,), version=sys.version_info[:2])


def _live_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """An alp-sdk checkout that IS the manifest repo of a live west workspace.

    Returns `(sdk, topdir)`. This is the shape both defects need: `.west/config`
    recording `path = alp-sdk`, so `_manifest_points_at(topdir, sdk)` is true and
    every later `west` invocation in that topdir depends on the checkout staying
    exactly where it is.
    """
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    topdir = sdk.parent
    (topdir / ".west").mkdir(parents=True, exist_ok=True)
    (topdir / ".west" / "config").write_text(
        "[manifest]\npath = alp-sdk\nfile = west.yml\n", encoding="utf-8"
    )
    return sdk, topdir


def _pinned_zephyr(topdir: Path) -> Path:
    """A `<topdir>/zephyr` whose VERSION matches the pin, for `$ZEPHYR_BASE`."""
    zephyr = topdir / "zephyr"
    zephyr.mkdir(parents=True, exist_ok=True)
    (zephyr / "VERSION").write_text(PINNED_VERSION_FILE, encoding="utf-8")
    return zephyr


def _venv_without_pip(venv_dir: Path, *, marker: str = "my_private_pkg") -> Path:
    """A venv-shaped directory whose interpreter RUNS but has no pip.

    Byte-for-byte the situation `uv venv` leaves behind (uv installs no pip by
    default) and the one `python -m venv --without-pip` creates on purpose.
    `venv_present()` accepts it, and `python -m pip --version` exits non-zero --
    which is what `ensure_venv` used to read as "wreckage, delete it".

    Returns the marker path, so a caller can assert the user's own content
    survived rather than only that the directory still exists.
    """
    bin_dir = venv_dir / BIN_DIR
    bin_dir.mkdir(parents=True)
    python = bin_dir / PYTHON_NAME
    if sys.platform == "win32":
        # A .bat cannot stand in for `python.exe`; copy the real interpreter and
        # let the missing pip come from the empty site-packages beside it.
        python.write_bytes(Path(sys.executable).read_bytes())
    else:
        python.write_text(
            "#!/bin/sh\n# no pip in this venv, exactly like `uv venv`\nexit 1\n",
            encoding="utf-8",
        )
        python.chmod(0o755)
    held = venv_dir / "lib" / marker
    held.mkdir(parents=True)
    (held / "keep-me.txt").write_text("the user's own package\n", encoding="utf-8")
    return held


def _workspace(topdir: Path, sdk: Path) -> Workspace:
    return Workspace(
        is_windows=sys.platform == "win32",
        facts=fallback_facts((3, 12)),
        repo_root=sdk,
        workspace_dir=topdir,
        venv_dir=topdir / ".venv",
    )


# ---------------------------------------------------------------------------
# tan-cli#390 -- an ADOPTED tree's venv is never deleted
# ---------------------------------------------------------------------------


def test_an_adopted_workspaces_pipless_venv_is_refused_not_deleted(tmp_path):
    """The defect verbatim: `_select_workspace`'s REUSE branch repoints
    `paths.venv_dir` at the USER's `<topdir>/.venv` and returns
    `adopted=True`, and `ensure_venv` then deleted it because pip did not
    answer. `plan.adopted` was not read until after the delete."""
    sdk, topdir = _live_workspace(tmp_path)
    held = _venv_without_pip(topdir / ".venv")
    log = Log(json_mode=True)

    venv, error = ensure_venv(
        _workspace(topdir, sdk), log, Runner(json=True), _host(), adopted=True
    )

    assert venv is None
    assert error is not None
    assert str(topdir / ".venv") in error
    assert [code for code, _ in log.warnings] == ["adopted-venv-unusable"]
    # The whole point: the user's tree is untouched.
    assert (topdir / ".venv").is_dir()
    assert held.is_dir()
    assert (held / "keep-me.txt").read_text(encoding="utf-8") == "the user's own package\n"


def test_a_probe_that_never_answered_does_not_authorise_a_delete(tmp_path, monkeypatch):
    """`probe()`'s own docstring says `None` means "no answer", never "the
    answer is bad" -- so a probe that never ran must not delete a directory
    even in tan's OWN workspace, where the recreate is otherwise correct.

    The verdict is injected rather than provoked from the filesystem: the shapes
    that really produce it (an unspawnable interpreter, a permission error, a
    timeout) are covered directly against `_probe_venv_pip` below, and forcing
    one of them through `venv_present()` -- which tests `_is_file` -- would
    change which BRANCH runs instead of which verdict reaches it.
    """
    sdk, topdir = _live_workspace(tmp_path)
    held = _venv_without_pip(topdir / ".venv")
    monkeypatch.setattr(bootstrap_cmd, "_probe_venv_pip", lambda *_a, **_k: PIP_INCONCLUSIVE)
    log = Log(json_mode=True)

    ensure_venv(_workspace(topdir, sdk), log, Runner(json=True), _host(), adopted=False)

    # The branch deliberately does NOT fail: it declines to delete, reuses the
    # directory as-is, and leaves the real error to whichever install actually
    # needs the interpreter. What it must never do is `rmtree`.
    assert "venv-probe-inconclusive" in [code for code, _ in log.warnings]
    assert "venv-recreated" not in [code for code, _ in log.warnings]
    assert (topdir / ".venv").is_dir()
    assert held.is_dir()
    assert (held / "keep-me.txt").exists()


def test_tans_own_workspace_still_recreates_a_genuinely_pipless_venv(tmp_path):
    """The recreate is not removed, only gated: on a workspace tan built
    itself, a venv whose pip RAN and reported itself broken is still the
    wreckage the retry must not inherit -- and it now says so with a CODE, so
    a `--format json` consumer sees the deletion in `issues[]`."""
    sdk, topdir = _live_workspace(tmp_path)
    held = _venv_without_pip(topdir / ".venv")

    # Dry run first: the verdict is reached, but nothing is ever deleted.
    log = Log(json_mode=True)
    ensure_venv(
        _workspace(topdir, sdk), log, Runner(json=True, dry_run=True), _host(), adopted=False
    )
    assert held.is_dir()

    log = Log(json_mode=True)
    ensure_venv(_workspace(topdir, sdk), log, Runner(json=True), _host(), adopted=False)
    assert "venv-recreated" in [code for code, _ in log.warnings]
    assert not held.exists()


@pytest.mark.parametrize("returncode, expected", [(0, PIP_USABLE), (1, PIP_ABSENT)])
def test_the_pip_probe_separates_a_verdict_from_no_answer(
    tmp_path, monkeypatch, returncode, expected
):
    """`probe()` collapses both into `None`; this one must not."""
    sdk, topdir = _live_workspace(tmp_path)
    _venv_without_pip(topdir / ".venv")
    completed = subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")
    monkeypatch.setattr(bootstrap_cmd.subprocess, "run", lambda *a, **k: completed)
    assert _probe_venv_pip(_workspace(topdir, sdk).venv_bin(), Runner(json=True)) == expected


def test_the_pip_probe_reports_inconclusive_when_the_spawn_fails(tmp_path, monkeypatch):
    sdk, topdir = _live_workspace(tmp_path)
    _venv_without_pip(topdir / ".venv")

    def boom(*_a, **_k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(bootstrap_cmd.subprocess, "run", boom)
    assert _probe_venv_pip(_workspace(topdir, sdk).venv_bin(), Runner(json=True)) == PIP_INCONCLUSIVE


def test_a_timeout_is_inconclusive_not_a_verdict(tmp_path, monkeypatch):
    """`PROBE_TIMEOUT_S` is 120 s, so this is rare -- but a machine under load
    that blows it must not have its venv deleted for being slow."""
    sdk, topdir = _live_workspace(tmp_path)
    _venv_without_pip(topdir / ".venv")

    def slow(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="pip", timeout=120)

    monkeypatch.setattr(bootstrap_cmd.subprocess, "run", slow)
    assert _probe_venv_pip(_workspace(topdir, sdk).venv_bin(), Runner(json=True)) == PIP_INCONCLUSIVE


# ---------------------------------------------------------------------------
# tan-cli#389 -- `--workspace` never orphans a live west workspace
# ---------------------------------------------------------------------------


def test_workspace_override_refuses_to_orphan_a_live_west_workspace(tmp_path):
    """`guard_applies = workspace_override is not None or not
    _zephyr_base_will_adopt(...)` short-circuits, so with `--workspace` the
    adoption probe never ran and the relocation moved the manifest repo out
    from under `<ws>/.west/config`."""
    sdk, topdir = _live_workspace(tmp_path)
    newhome = tmp_path / "newhome"

    proc = run_tan(
        "bootstrap", "--no-pip", "--no-west", "--format", "json",
        "--sdk-root", str(sdk), "--workspace", str(newhome), cwd=topdir,
    )
    env = envelope(proc)

    assert proc.returncode == 2
    assert codes(env) == ["bootstrap.workspace-orphan-refused"]
    assert str(topdir) in env["issues"][0]["message"]
    # The checkout stays; the destination is never created.
    assert sdk.is_dir()
    assert not newhome.exists()
    # And the machine-global default is not repointed at a move that never happened.
    assert not (tmp_path / "fake-home" / ".alp" / "sdk-default").exists()


def test_the_orphan_guard_does_not_depend_on_zephyr_base(tmp_path):
    """The issue reproduced with `$ZEPHYR_BASE` both set and unset -- the
    `.west/config` at the SOURCE is what makes the move destructive, not the
    environment variable."""
    sdk, topdir = _live_workspace(tmp_path)
    zephyr = _pinned_zephyr(topdir)
    newhome = tmp_path / "newhome"

    proc = run_tan(
        "bootstrap", "--no-pip", "--no-west", "--format", "json",
        "--sdk-root", str(sdk), "--workspace", str(newhome), cwd=topdir,
        env_extra={"ZEPHYR_BASE": str(zephyr)},
    )
    env = envelope(proc)

    assert proc.returncode == 2
    assert codes(env) == ["bootstrap.workspace-orphan-refused"]
    assert sdk.is_dir()
    assert not newhome.exists()


def test_the_orphan_guard_does_not_fire_on_an_ordinary_checkout(tmp_path):
    """Over-refusal is the failure mode to avoid: a checkout whose parent is
    NOT a west workspace still relocates on `--workspace`, unchanged."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    newhome = tmp_path / "newhome"

    proc = run_tan(
        "bootstrap", "--no-pip", "--no-west", "--format", "json", "--dry-run",
        "--sdk-root", str(sdk), "--workspace", str(newhome), cwd=sdk.parent,
    )

    assert "bootstrap.workspace-orphan-refused" not in codes(envelope(proc))


def test_a_west_config_naming_a_different_repo_is_not_this_checkouts_workspace(tmp_path):
    """`_manifest_points_at` is the discriminator: a `.west/config` in the
    parent that names SOME OTHER directory is not a workspace this checkout is
    load-bearing for, so relocating it is safe and must not be refused."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    topdir = sdk.parent
    (topdir / ".west").mkdir(parents=True, exist_ok=True)
    (topdir / ".west" / "config").write_text(
        "[manifest]\npath = some-other-repo\nfile = west.yml\n", encoding="utf-8"
    )
    newhome = tmp_path / "newhome"

    proc = run_tan(
        "bootstrap", "--no-pip", "--no-west", "--format", "json", "--dry-run",
        "--sdk-root", str(sdk), "--workspace", str(newhome), cwd=topdir,
    )

    assert "bootstrap.workspace-orphan-refused" not in codes(envelope(proc))


def test_the_adoption_probe_is_evaluated_even_under_an_explicit_workspace(tmp_path, monkeypatch):
    """The mechanical half of the fix: `_zephyr_base_will_adopt` must be CALLED
    when `--workspace` is given, not short-circuited past by `or`."""
    calls: list[tuple] = []
    real = bootstrap_cmd._zephyr_base_will_adopt

    def spy(pin, repo_root):
        calls.append((pin, repo_root))
        return real(pin, repo_root)

    monkeypatch.setattr(bootstrap_cmd, "_zephyr_base_will_adopt", spy)
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    bootstrap_cmd._run(
        project=None,
        board_yaml=None,
        sdk_root_flag=str(sdk),
        no_pip=True,
        no_west=True,
        print_env=False,
        allow_partial=False,
        workspace=str(tmp_path / "newhome"),
        dry_run=True,
        json_mode=True,
    )
    assert calls, "_zephyr_base_will_adopt was short-circuited past (tan-cli#389)"
