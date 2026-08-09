# SPDX-License-Identifier: Apache-2.0
"""tan-cli#567: no spawn on the write or size path may get a bare `argv[0]`.

`CreateProcess` with `lpApplicationName=NULL` -- which is what
`subprocess.run`/`Popen` use on Windows for a bare program name -- searches
*the current directory for the parent process* BEFORE `%PATH%`. tan's cwd
during a flash is the customer's project. So a project carrying its own
`openocd.exe`/`dd.exe`/`size.exe` at its root got that binary spawned, having
passed a tool gate that had deliberately walked `%PATH%` and nothing else.

Every test here is PORTABLE, following
`test_execute.py::test_resolve_tool_never_resolves_the_current_directory`'s
reasoning: a `skipif(os.name != "nt")` test never runs for the overwhelming
majority of contributors, so the invariant is stated where it is platform-free
-- "what reaches `argv[0]`" -- rather than by asking a Linux CI to reproduce a
Windows search order. Two of them additionally reproduce that search order in a
pure function ([`createprocess_would_load`]) so the *consequence* is pinned and
not only the mechanism.
"""
from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest

from tan.commands import flash_cmd, size_cmd
from tan.commands.build import execute as execute_module
from tan.core.flash_plan import FlashInputs, FlashPlan
from tan.core.tool_lookup import ToolResolution


def createprocess_would_load(argv0: str, cwd: str, path: str) -> str:
    """Which file Windows' `CreateProcess` would load for `argv0`, per its
    documented search order -- the parent process's CURRENT DIRECTORY ahead of
    every `PATH` entry. Pure, so it states the hazard on every platform; POSIX
    `execvp` never searches cwd, which is why the hijack itself is Windows-only
    and why this emulation, not the OS, is what shows it here."""
    if os.path.isabs(argv0):
        return argv0
    for directory in [cwd, *[d for d in path.split(os.pathsep) if d]]:
        candidate = Path(directory) / argv0
        if candidate.is_file():
            return str(candidate)
    return "<not found>"


def _executable(directory: Path, name: str) -> Path:
    """An executable file named `name` in `directory`. `.exe` on Windows, where
    an extensionless file is not a program at all and the hardened `%PATH%`
    walk correctly refuses to see one."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (f"{name}.exe" if os.name == "nt" else name)
    path.write_text("", encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o755)
    return path


@pytest.fixture
def hostile(tmp_path, monkeypatch):
    """The #567 scenario, set up once: a `realbin` directory that IS on PATH
    holding the real tools, a `hostile-project` directory that is the process's
    cwd holding a same-named decoy of each, and nothing else on PATH.

    Yields `(project_dir, realbin_dir)`."""
    realbin = tmp_path / "realbin"
    project = tmp_path / "hostile-project"
    project.mkdir()
    for name in ("dd", "gunzip", "size", "taskkill", "JLinkExe"):
        _executable(realbin, name)
        _executable(project, name)
    monkeypatch.setenv("PATH", str(realbin))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.chdir(project)
    return project, realbin


class _SpawnSpy:
    """Records every argv handed to `subprocess.run`/`Popen` and returns a
    stand-in process, so no test here starts a real one."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def run(self, argv, *_a, **_kw):
        self.argvs.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def popen(self, argv, *_a, **_kw):
        self.argvs.append(list(argv))
        return _FakeProc()


class _FakeProc:
    """A process stand-in. `stdout`/`stderr` are real empty binary streams, not
    `None`: `flash_cmd._Tee` reads them on the text-mode branch, and handing it
    `None` would turn this into a test of the stub rather than of the argv."""

    returncode = 0
    pid = 4242

    def __init__(self) -> None:
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")

    def wait(self, timeout=None):
        return 0

    def communicate(self, timeout=None):
        return (b"", b"")

    def poll(self):
        return 0

    def kill(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


@pytest.fixture
def spy(monkeypatch):
    spawn_spy = _SpawnSpy()
    monkeypatch.setattr(subprocess, "run", spawn_spy.run)
    monkeypatch.setattr(subprocess, "Popen", spawn_spy.popen)
    return spawn_spy


# ── tan flash: the single-process spawn ─────────────────────────────────────


@pytest.mark.parametrize("capture", [True, False], ids=["json", "text"])
def test_flash_spawns_the_path_copy_not_the_one_in_the_project(hostile, spy, capture):
    """The headline defect. `--format json` (`capture=True`) and text mode take
    two different branches of `_spawn` -- `subprocess.run` and `Popen` -- and
    BOTH handed the platform the bare `dd`.

    Fails before the fix on every platform: `argv[0]` is `'dd'`, so the
    `CreateProcess` emulation loads the project's decoy."""
    project, realbin = hostile
    flash_cmd._execute(FlashPlan(argv=("dd", "if=x", "of=/dev/null"), ok_message="ok"), capture)

    argv0 = spy.argvs[-1][0]
    loaded = createprocess_would_load(argv0, str(project), os.environ["PATH"])
    assert os.path.isabs(argv0), f"argv[0] is the bare identity {argv0!r}"
    assert not loaded.startswith(str(project)), (
        f"CreateProcess would load the project's own decoy ({loaded}) -- "
        f"argv[0] was {argv0!r}"
    )
    assert Path(argv0).parent == realbin


def test_flash_pipeline_resolves_BOTH_halves(hostile, spy):
    """`gunzip | dd` -- the `.wic.gz` image path. The decompressor is not in any
    backend's `requires` list, so it reaches the spawn with no tool gate in
    front of it at all; the right half writes to a real block device."""
    project, realbin = hostile
    flash_cmd._execute(
        FlashPlan(argv=("gunzip", "-c", "i.gz", "|", "dd", "of=/dev/null"), ok_message="ok"), True
    )

    assert len(spy.argvs) == 2, spy.argvs
    for argv in spy.argvs:
        loaded = createprocess_would_load(argv[0], str(project), os.environ["PATH"])
        assert Path(argv[0]).parent == realbin, f"pipeline half spawned {argv[0]!r}"
        assert not loaded.startswith(str(project)), loaded


def test_flash_refuses_a_program_that_is_on_neither_path_nor_the_venv(hostile, spy):
    """"Not on PATH and not in the workspace venv" leaves the current directory
    as the only place `CreateProcess` could still find that name -- so the entry
    fails instead, naming what was searched.

    Before the fix this spawned the bare name and reported the OS's own
    `[Errno 2] No such file or directory` (POSIX) or ran the project's copy
    (Windows); either way `searched` never appeared and `_spawn` was reached."""
    project, _realbin = hostile
    outcome = flash_cmd._execute(
        FlashPlan(argv=("no-such-flasher", "--go"), ok_message="ok"), True
    )

    assert outcome.success is False
    assert "no-such-flasher" in outcome.stderr
    assert "searched" in outcome.stderr, outcome.stderr
    assert spy.argvs == [], f"a program that did not resolve was still spawned: {spy.argvs}"


def test_flash_resolves_against_the_env_the_child_will_get(tmp_path, monkeypatch, spy):
    """tan-cli#510's MAJOR 2, restated for the write path: the venv's bin dir is
    prepended onto the CHILD's PATH, so the lookup has to see that same PATH or
    the check and the spawn are again looking at two different files.

    `venv_bin` holds the only copy of `flasher`; `os.environ["PATH"]` does not
    have it. The spawn must still resolve, and to the venv's copy."""
    venv_bin = tmp_path / "wsvenv" / "bin"
    tool = _executable(venv_bin, "flasher")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.chdir(tmp_path)

    outcome = flash_cmd._execute(
        FlashPlan(argv=("flasher", "--go"), ok_message="ok"), True, venv_bin=venv_bin
    )

    assert outcome.success is True, outcome.stderr
    assert spy.argvs[-1][0] == str(tool)


def test_flash_leaves_an_absolute_program_alone(hostile, spy):
    """A program a caller already resolved -- `_programs_resolved_in_venv`'s
    venv rewrite -- must not be re-searched, and must reach the spawn byte-for-
    byte. Guards the fix against over-reach: resolving an absolute path through
    a PATH walk would answer `None` for anything outside PATH and turn a working
    plan into a refusal."""
    _project, realbin = hostile
    absolute = str(realbin / ("dd.exe" if os.name == "nt" else "dd"))

    outcome = flash_cmd._execute(FlashPlan(argv=(absolute, "if=x"), ok_message="ok"), True)

    assert outcome.success is True, outcome.stderr
    assert spy.argvs[-1][0] == absolute


def test_flash_dpidr_preflight_spawns_the_resolved_jlink(hostile, monkeypatch):
    """The read-only preflight is what decides WHICH BOARD the write goes to. A
    project-supplied `JLinkExe` there answers the identity question with a value
    of its own choosing and then hands tan a green light for the MRAM write."""
    _project, realbin = hostile
    seen: list[list[str]] = []

    def _fake_spawn_jlink(argv, script, capture, timeout, venv_bin=None, workspace=None):
        seen.append(list(argv))
        # A banner carrying the expected ID, so the preflight answers "proceed"
        # and the test is measuring the spawn, not the mismatch reporting.
        return flash_cmd._Outcome(success=True, stdout="Found SW-DP with ID 0x6BA02477")

    monkeypatch.setattr(flash_cmd, "_spawn_jlink", _fake_spawn_jlink)

    inputs = FlashInputs(
        artefact="app.bin",
        flash_args={"expect_dpidr": "0x6BA02477", "jlink_device": "AE822F80F55D5XX"},
        core_id="app",
        sku="E1M-AEN801",
    )
    assert flash_cmd._flow_d_preflight(inputs) is None
    assert seen, "the preflight never spawned"
    assert Path(seen[-1][0]).parent == realbin, f"preflight spawned {seen[-1][0]!r}"


def test_flash_tool_available_is_still_a_gate_the_project_dir_cannot_satisfy(hostile):
    """A standing pin, NOT a fix: the go/no-go gate was already hardened, and
    passes before and after. It is here because #567 is precisely the case of a
    correct check whose answer was discarded -- if a future simplification
    swaps this walk for `shutil.which`, this goes red on Windows and the two
    spawn tests above stay green, which is the exact split that produced the
    defect."""
    project, _realbin = hostile
    only_in_project = _executable(project, "project-only-flasher")
    assert only_in_project.is_file()
    assert flash_cmd._tool_available("project-only-flasher") is False


# ── the shared lookup ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path_value",
    ["", os.pathsep, f"{os.pathsep}/nonexistent-dir{os.pathsep}"],
    ids=["empty", "separator-only", "empty-entries-around-a-real-one"],
)
def test_an_empty_path_entry_is_never_the_current_directory(tmp_path, monkeypatch, path_value):
    """A defect FOUND while consolidating, not one #567 reported: an empty
    `PATH` entry means "the current directory" to every POSIX PATH consumer,
    and `shutil.which(tool, path=":/nope:")` duly joins `""` with the name and
    probes it relative to cwd. Measured on `dev`,
    `_resolve_tool("cwdprobe", {"PATH": ":/nope:"})` answered `'cwdprobe'` --
    a RELATIVE path, for a file that exists only in the process's own working
    directory -- which `execute_slices` then spawned as `argv[0]`.

    `PATH="$PATH:"` / `PATH=":$PATH"` are entirely routine, so this was live on
    the build path from tan-cli#510 onward. The Windows branch of the same
    function already skipped empty entries (`if not directory: continue`) and
    `size_cmd`'s private copy did too; only the POSIX branch did not, which is
    precisely the drift tan-cli#532 describes."""
    from tan.core.tool_lookup import resolve_tool

    probe = _executable(tmp_path, "cwdprobe")
    assert probe.is_file()
    monkeypatch.chdir(tmp_path)

    resolution = resolve_tool(probe.stem if os.name == "nt" else "cwdprobe", {"PATH": path_value})
    assert resolution.resolved is None, (
        f"resolved to {resolution.resolved!r} from an empty PATH entry -- that "
        "is the current directory, which the hardened walk exists to exclude"
    )


# ── tan size ────────────────────────────────────────────────────────────────


def test_size_find_on_path_answers_the_path_not_a_bool(hostile):
    """`_find_on_path` walked `%PATH%` correctly and then answered `True`,
    throwing the one value the spawn needed away. Fails before the fix, where
    this is `True`."""
    _project, realbin = hostile
    found = size_cmd._find_on_path("size")
    assert isinstance(found, str), f"_find_on_path answered {found!r}"
    assert Path(found).parent == realbin


def test_size_spawns_the_path_copy_not_the_one_in_the_project(hostile, spy):
    """End to end through the selection `size_cmd._run` performs: the tool the
    spawn gets must be the resolved path."""
    project, realbin = hostile
    size_bin = next(filter(None, (size_cmd._find_on_path(t) for t in size_cmd.SIZE_TOOLS)), None)
    assert size_bin is not None

    size_cmd._sizes_from_size_tool(size_bin, "app.elf")

    argv0 = spy.argvs[-1][0]
    loaded = createprocess_would_load(argv0, str(project), os.environ["PATH"])
    assert Path(argv0).parent == realbin
    assert not loaded.startswith(str(project)), loaded


def test_size_refuses_a_bare_size_bin_rather_than_spawning_it(hostile, spy):
    """The other direction: `_sizes_from_size_tool` is never allowed to accept
    an identity and let the platform resolve it. Falling through costs only the
    size-tool rung -- the ELF-section-header measurement below it needs no
    subprocess."""
    assert size_cmd._sizes_from_size_tool("size", "app.elf") is None
    assert spy.argvs == [], f"a bare size_bin was spawned: {spy.argvs}"


# ── tan build: the cancel path ──────────────────────────────────────────────


def test_terminate_spawns_the_resolved_taskkill(monkeypatch, spy):
    """`taskkill` lives in `%SystemRoot%\\System32`, which `CreateProcess`
    consults AFTER the current directory -- so a project carrying
    `taskkill.exe` got it run, with tan's privileges, the moment a build was
    cancelled. Fails before the fix, where `argv[0]` is the literal
    `'taskkill'`.

    `os.name` is patched rather than skipped-unless-Windows so this runs
    everywhere. What is stubbed is the LOOKUP, not the decision under test:
    `pathlib.Path` dispatches on `os.name` at construction, so any real
    resolution under the patch would raise `NotImplementedError` ("cannot
    instantiate 'WindowsPath'") on a POSIX host. `raising=False` so the stub
    installs against the pre-fix module too -- the point is that this then fails
    on the ASSERTION below (`argv[0]` is `'taskkill'`), not on a missing
    attribute. The real lookup is pinned unpatched by the test after this one."""
    resolved = "/abs/System32/taskkill.exe"
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        execute_module,
        "resolve_tool",
        lambda *_a, **_k: ToolResolution(resolved, "stub"),
        raising=False,
    )

    execute_module._terminate(_FakeProc())

    assert spy.argvs, "cancel spawned nothing"
    assert os.path.isabs(spy.argvs[-1][0]), (
        f"the cancel path spawned the bare identity {spy.argvs[-1][0]!r} -- "
        "CreateProcess would search the project directory for it first"
    )
    assert spy.argvs[-1][0] == resolved, spy.argvs[-1]


def test_taskkill_program_never_resolves_the_current_directory(hostile):
    """The resolution half, portable and unpatched: a `taskkill` that exists
    ONLY in the process's own cwd must never be what `_terminate` would spawn.

    PATH here holds a real `taskkill` (the fixture's `realbin`), so this pins
    the positive answer too -- it resolves, and to the PATH copy."""
    project, realbin = hostile
    resolved = execute_module._taskkill_program()
    assert resolved is not None
    assert Path(resolved).parent == realbin
    assert not str(resolved).startswith(str(project))
