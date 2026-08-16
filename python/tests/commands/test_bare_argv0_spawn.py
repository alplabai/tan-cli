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

from tan.commands import doctor_cmd, flash_cmd, size_cmd
from tan.commands.build import execute as execute_module
from tan.core.bootstrap import venv_layout
from tan.core.flash_plan import FlashInputs, FlashPlan
from tan.core.tool_lookup import ToolResolution, windows_candidate_names


def createprocess_would_load(argv0: str, executable: str | None, cwd: str, path: str) -> str:
    """Which file Windows' `CreateProcess` would load, per its documented rules.

    A non-`None` `executable` is `lpApplicationName`, and with that set
    `CreateProcess` performs NO SEARCH AT ALL -- it loads exactly that file.
    With it `NULL`, the search order applies and it puts the parent process's
    CURRENT DIRECTORY ahead of every `PATH` entry.

    Pure, so it states the hazard on every platform; POSIX `execvp` never
    searches cwd, which is why the hijack itself is Windows-only and why this
    emulation, not the OS, is what shows it here.

    `.exe` is appended to an extension-less name, because that is the other
    half of the documented rule and it is what the fixtures on disk look
    like: `CreateProcess` appends `.exe` (and ONLY `.exe` -- it never reads
    `%PATHEXT%`) to an unqualified program name carrying no extension, which
    is why `_executable` seeds `foo.exe` on Windows. Without this the
    emulation would answer `<not found>` for every Windows fixture and the
    file's negative assertions would pass vacuously there."""
    if executable is not None:
        return executable
    if os.path.isabs(argv0):
        return argv0
    names = [argv0] if Path(argv0).suffix else [argv0, f"{argv0}.exe"]
    for directory in [cwd, *[d for d in path.split(os.pathsep) if d]]:
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file():
                return str(candidate)
    return "<not found>"


def _executable(directory: Path, name: str) -> Path:
    """An executable file named `name` in `directory`. `.exe` on Windows,
    where a bare extensionless file is not a `%PATH%` candidate for that
    identity at all -- neither for `CreateProcess`, which appends only `.exe`
    to an unqualified name, nor for the hardened walk, which matches the
    oracle's `%PATHEXT%`-only candidate set (pinned by
    `test_the_windows_walk_never_considers_the_bare_extensionless_name`)."""
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
    # "git"/"py"/"python"/"python3" added for tan-cli#797: `doctor_cmd`'s SDK-
    # provenance + host-Python probes join the scenario the rest of this file
    # already set up for `dd`/`gunzip`/`size`/`taskkill`/`JLinkExe`.
    for name in ("dd", "gunzip", "size", "taskkill", "JLinkExe", "git", "py", "python", "python3"):
        _executable(realbin, name)
        _executable(project, name)
    monkeypatch.setenv("PATH", str(realbin))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.chdir(project)
    return project, realbin


class _SpawnSpy:
    """Records every `(argv, executable)` handed to `subprocess.run`/`Popen`
    and returns a stand-in process, so no test here starts a real one.

    BOTH halves are recorded because both matter and they say different things:
    `argv` is what the child sees as its own `argv[0]` (and therefore what the
    tool prints in its own diagnostics -- the frozen oracle envelope pins that),
    while `executable` is what the OS actually loads."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.executables: list[str | None] = []

    def _record(self, argv, kwargs) -> None:
        self.argvs.append(list(argv))
        self.executables.append(kwargs.get("executable"))

    def run(self, argv, *_a, **kwargs):
        self._record(argv, kwargs)
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def popen(self, argv, *_a, **kwargs):
        self._record(argv, kwargs)
        return _FakeProc()

    def loaded(self, index: int, cwd: str) -> str:
        """What `CreateProcess` would load for spawn `index`."""
        return createprocess_would_load(
            self.argvs[index][0], self.executables[index], cwd, os.environ["PATH"]
        )


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
    BOTH handed the platform the bare `dd` with no `executable=`.

    Fails before the fix on every platform: `executable` is `None`, so the
    `CreateProcess` emulation falls back to its search order and loads the
    project's decoy.

    The child's own `argv[0]` must STAY `'dd'`. That is not incidental: `dd`
    prints its `argv[0]` in its own failure text, which lands verbatim in
    `data.entries[].message`, and the frozen oracle envelope
    (`test_a_real_spawn_diffs_including_the_captured_failure_tail`) says
    `dd: failed to open ...`. Rewriting `argv[0]` to the resolved path -- the
    shape tan-cli#510 used for the build spawn -- makes that read
    `/usr/bin/dd: failed to open ...`, which is both an envelope regression and
    an absolute-host-path leak."""
    project, realbin = hostile
    flash_cmd._execute(FlashPlan(argv=("dd", "if=x", "of=/dev/null"), ok_message="ok"), capture)

    argv0, executable = spy.argvs[-1][0], spy.executables[-1]
    loaded = spy.loaded(-1, str(project))
    assert executable is not None, "the spawn pinned no executable -- argv[0] is bare"
    assert Path(executable).parent == realbin
    assert not loaded.startswith(str(project)), (
        f"CreateProcess would load the project's own decoy ({loaded})"
    )
    assert argv0 == "dd", (
        f"the child's own argv[0] became {argv0!r} -- the tool prints that in "
        "its diagnostics and the frozen oracle envelope pins the bare name"
    )


def test_flash_pipeline_resolves_BOTH_halves(hostile, spy):
    """`gunzip | dd` -- the `.wic.gz` image path, whose right half writes to a
    real block device.

    Both halves ARE checked before the spawn today, contrary to this fix's own
    first telling: `dd` is in `flash_plan._REGISTRY["yocto_wic"].requires`
    (`('bmaptool', 'dd')`) so `tool_gate` covers it, and `plan_yocto_wic`
    `which`-checks `gunzip`/`gzip` itself and raises `FlashPlanError` when
    neither answers. What this pins is the SPAWN end: the file the check
    approved and the file the OS loads must be the same one, for the token
    after the `"|"` as well as for `argv[0]` -- the only remaining opening is
    the plan-time-to-spawn-time window, which is what a resolved
    `executable=` closes."""
    project, realbin = hostile
    flash_cmd._execute(
        FlashPlan(argv=("gunzip", "-c", "i.gz", "|", "dd", "of=/dev/null"), ok_message="ok"), True
    )

    assert len(spy.argvs) == 2, spy.argvs
    assert [argv[0] for argv in spy.argvs] == ["gunzip", "dd"], spy.argvs
    for index, half in enumerate(("gunzip", "dd")):
        executable = spy.executables[index]
        assert executable is not None, f"the {half} half pinned no executable"
        assert Path(executable).parent == realbin, f"{half} half loaded {executable!r}"
        assert not spy.loaded(index, str(project)).startswith(str(project))


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
    have it. The spawn must still resolve, and to the venv's copy.

    The bin dir is named after the layout of the host it runs on, not `bin`
    everywhere: `tool_in_venv` appends `.exe` keyed on the DIRECTORY that won
    (`bin_dir.name == venv_layout(True).bin_dir`, i.e. `Scripts`), not on
    `os.name` -- tan-cli#291, because `_resolve_layout`'s probe can pick
    `Scripts/` on a POSIX-reporting host. A `Scripts`-less `bin/` holding
    `flasher.exe` is therefore a venv shape `tool_in_venv` cannot resolve:
    it looks for `bin/flasher`, finds nothing, and `_execute` refuses. That
    is the FIXTURE being wrong about what a Windows venv looks like, not a
    defect in the resolution -- measured red on windows-latest at 01b2e73
    (`could not spawn: 'flasher' was not found`) at both commits of this
    branch, i.e. it was never about the seeding fix."""
    venv_bin = tmp_path / "wsvenv" / venv_layout(os.name == "nt").bin_dir
    tool = _executable(venv_bin, "flasher")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.chdir(tmp_path)

    outcome = flash_cmd._execute(
        FlashPlan(argv=("flasher", "--go"), ok_message="ok"), True, venv_bin=venv_bin
    )

    assert outcome.success is True, outcome.stderr
    # The venv rewrite already put the absolute path in `argv[0]` (that is the
    # oracle's own behaviour, unchanged here), and the resolution agrees.
    assert spy.argvs[-1][0] == str(tool)
    assert spy.executables[-1] == str(tool)


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
    assert spy.executables[-1] == absolute


def test_flash_dpidr_preflight_spawns_the_resolved_jlink(hostile, monkeypatch):
    """The read-only preflight is what decides WHICH BOARD the write goes to. A
    project-supplied `JLinkExe` there answers the identity question with a value
    of its own choosing and then hands tan a green light for the MRAM write."""
    _project, realbin = hostile
    seen: list[list[str]] = []

    def _fake_spawn_jlink(
        argv, script, capture, timeout, venv_bin=None, workspace=None, executable=None
    ):
        seen.append([executable, *argv])
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
    executable, argv0 = seen[-1][0], seen[-1][1]
    assert executable is not None, "the preflight pinned no executable"
    assert Path(executable).parent == realbin, f"preflight loaded {executable!r}"
    assert argv0 == "JLinkExe", f"the preflight child's argv[0] became {argv0!r}"


def test_flash_dpidr_preflight_refuses_rather_than_spawning_an_unresolved_jlink(
    tmp_path, monkeypatch
):
    """The other half of the preflight fix, and the one with the hardware
    consequence: when the go/no-go gate says "JLinkExe is available" but the
    resolution finds nothing, the preflight REFUSES the write instead of
    handing `CreateProcess` the bare name -- where the customer's project
    directory is the only remaining supplier of a binary whose output tan is
    about to trust as the answer to "which board is attached".

    The gate/resolution disagreement is forced here rather than waited for
    (in production it is the window between the two: a tool removed, a `PATH`
    rewritten, a venv torn down). It is deliberately the SAME shape that
    `test_flash_command.py::_stub_flow_d_probe` used to set up by accident --
    a patched `_tool_available` over an empty PATH -- so the arm those seven
    banner tests were incidentally exercising keeps a pin that asserts the
    refusal ON PURPOSE. `tan-cli#520`'s "refusing to write MRAM without
    confirming which board is attached" is the string a loosening of the
    resolution would have to delete, and this is what would go red.

    Two details make this die on its OWN assertions rather than incidentally.

    The stub returns a real, SUCCEEDING `_Outcome` carrying the expected
    DPIDR banner, the way its sibling above does. A stub returning `None`
    would make the most likely loosening (`if unresolved is not None:` ->
    `if False:`) die at `flash_cmd.py`'s `outcome.stdout` with
    `AttributeError: 'NoneType' object has no attribute 'stdout'` -- inside
    production code, before `assert spawned == []` is ever reached. That
    points the diagnostic at the wrong file. With a real outcome the
    loosening spawns, answers "proceed", and this fails on `spawned == []`:
    the deleted guard, named. (Measured under `if unresolved is not None:` ->
    `if False:`.)

    The cwd carries a DECOY `JLinkExe`, because "refuses" is only half the
    property. What makes the refusal worth having is that the alternative is
    not "no probe" but "the customer's own project directory answers the
    question of which board is attached" -- `createprocess_would_load` shows
    that a bare `JLinkExe` handed to `CreateProcess` with no `executable=`
    loads exactly that decoy, ahead of every `%PATH%` entry.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    project = tmp_path / "hostile-project"
    decoy = _executable(project, "JLinkExe")
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.chdir(project)
    monkeypatch.setattr(flash_cmd, "_tool_available", lambda *_a, **_k: True)
    spawned: list[object] = []

    def _spawn_jlink(*args, **_kwargs):
        spawned.append(args)
        return flash_cmd._Outcome(success=True, stdout="Found SW-DP with ID 0x6BA02477")

    monkeypatch.setattr(flash_cmd, "_spawn_jlink", _spawn_jlink)

    message = flash_cmd._flow_d_preflight(
        FlashInputs(
            artefact="app.bin",
            flash_args={"expect_dpidr": "0x6BA02477", "jlink_device": "AE822F80F55D5XX"},
            core_id="app",
            sku="E1M-AEN801",
        )
    )

    # The no-spawn property goes FIRST: it is the one with the hardware
    # consequence, and asserting it before the message means a deleted guard
    # is diagnosed as "it spawned" rather than as "the text changed".
    assert spawned == [], "the preflight spawned a program it could not resolve"
    # And that spawn would not have been harmless: with no `executable=`,
    # THIS is the file `CreateProcess` would have loaded to answer "which
    # board is attached" -- the decoy in the customer's own project dir,
    # ahead of every `%PATH%` entry.
    assert createprocess_would_load("JLinkExe", None, str(project), str(empty)) == str(decoy), (
        "the decoy is not where CreateProcess would find it -- this assertion "
        "is what makes the no-spawn assertion above worth making"
    )
    assert message is not None, "an unresolvable J-Link produced no refusal"
    assert "JLinkExe" in message
    assert "could not be resolved" in message, message
    assert "refusing to write MRAM without confirming which board is attached" in message, message


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


@pytest.mark.parametrize(
    "tool, expected",
    [
        ("npm", ["npm.COM", "npm.EXE", "npm.BAT", "npm.CMD"]),
        ("JLinkExe", ["JLinkExe.COM", "JLinkExe.EXE", "JLinkExe.BAT", "JLinkExe.CMD"]),
        ("dd.exe", ["dd.exe"]),
        ("west.cmd", ["west.cmd"]),
    ],
    ids=["bare", "bare-mixed-case", "already-exe", "already-cmd"],
)
def test_the_windows_walk_never_considers_the_bare_extensionless_name(tool, expected):
    """The ONE deliberate Windows-arm behaviour change tan-cli#567 makes,
    pinned so the next reader cannot mistake it for an accident.

    `doctor_cmd.on_path`, the last of the five hand-rolled lookups #532
    consolidates, USED TO try `exts = [""] + PATHEXT` -- the bare,
    EXTENSIONLESS name first, ahead of every suffixed sibling. `tool_lookup`
    does not, and that is not an oversight in the consolidation. Since #532
    `on_path` delegates here, so this is now the behaviour the doctor and
    `faultdecode_cmd` get as well:

    * It is what the ORACLE does. `crates/tan-cli/src/util.rs::find_on_path`
      is `if has_ext { dir.join(command) } else { for ext in &exts { ... } }`
      -- no bare candidate anywhere. The oracle is the fixed point; a port
      that is more permissive than it, on the one arm nobody here can run, is
      drift. `build/execute.py::_resolve_tool`, the copy this module IS,
      agreed with the oracle already; only `on_path` did not.
    * It is what WINDOWS does. `CreateProcess` with `lpApplicationName=NULL`
      appends only `.exe` to an unqualified name and never reads `%PATHEXT%`
      at all, so a bare-name hit is a file the platform's own resolver would
      never have selected for that identity; `cmd.exe` and PowerShell resolve
      a typed command name through `%PATHEXT%` too.
    * Admitting it would be actively unsafe NOW, in a way it was not while
      the answer was only a bool. The extensionless files really found on a
      Windows `%PATH%` are POSIX shims (`npm`, `yarn`, `git-*`: sh scripts
      shipped beside their `.cmd` sibling in the same directory), and `""`
      going first means the sh script WINS. Since #567 that value is handed
      to `subprocess` as `executable=` -- `lpApplicationName`, no search --
      where a non-PE file is `[WinError 193] %1 is not a valid Win32
      application`. So the leniency would convert a working `.cmd` spawn into
      a hard spawn failure, and make the go/no-go gate approve a file the
      spawn cannot launch: the same check/spawn disagreement #567 closes,
      re-entered from the other side.

    Nothing is lost. Windows WILL execute an extensionless valid PE handed to
    it as a path (`.exe` is appended only to a name carrying no path), and
    `resolve_tool` still answers an absolute `tool` by existence alone and
    spawns it verbatim -- a plan or a venv rewrite may still name one. Only
    bare-identity DISCOVERY of such a file on `%PATH%` is refused, with the
    walked `%PATH%` named in the refusal.

    Portable by construction: the walk's candidate names are a pure function
    precisely so this runs on Linux CI, where `resolve_tool`'s Windows branch
    cannot be reached at all (`Path` dispatches on `os.name` at construction,
    so patching `os.name` raises rather than reaching it)."""

    names = windows_candidate_names(tool, ".COM;.EXE;.BAT;.CMD")
    assert names == expected
    assert tool not in names or Path(tool).suffix, (
        f"the bare, extensionless {tool!r} is a candidate again -- `CreateProcess` "
        "would never select it for that identity, and on a Windows PATH it is "
        "almost always a POSIX shim that `executable=` cannot launch"
    )


def test_an_empty_pathext_entry_never_becomes_the_bare_name_by_accident():
    """The back door into the case above: `%PATHEXT%` legitimately arrives
    with a trailing or doubled `;` (`.COM;.EXE;;`), and `""` joined to the
    tool is the bare name again. Dropping empty entries is what keeps the
    invariant above from depending on how tidy the host's `%PATHEXT%` is --
    the same reason the `%PATH%` walk itself skips empty directory entries,
    and the same class of defect as
    `test_an_empty_path_entry_is_never_the_current_directory` above."""

    assert windows_candidate_names("npm", ".COM;;.EXE;") == ["npm.COM", "npm.EXE"]
    assert windows_candidate_names("npm", "") == []


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
    # `tan size` keeps tan-cli#510's own shape -- the resolved path IS `argv[0]`
    # -- because nothing here echoes the size tool's own `argv[0]` anywhere: its
    # stdout is parsed for Berkeley columns on rc 0 and discarded otherwise, so
    # there is no envelope string for an absolute path to leak into.
    assert Path(argv0).parent == realbin
    assert not spy.loaded(-1, str(project)).startswith(str(project))


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


# ── tan doctor: SDK-provenance git + host-Python probes (tan-cli#797) ───────


def test_doctor_resolve_git_executable_answers_the_path_copy(hostile):
    """`_resolve_git_executable` is the ONE lookup all four doctor git call
    sites below now share -- it must answer the PATH copy, never the
    project's own decoy `git`."""
    _project, realbin = hostile
    resolved = doctor_cmd._resolve_git_executable()
    assert resolved is not None
    assert Path(resolved).parent == realbin


def test_doctor_is_own_git_checkout_spawns_the_resolved_git(hostile, spy):
    """Fails before the fix, where `argv[0]` is the bare `'git'` and no
    `executable=` is pinned -- `CreateProcess` would then load the project's
    own decoy to answer "is this SDK checkout its own git repo"."""
    project, realbin = hostile
    git_exe = doctor_cmd._resolve_git_executable()

    doctor_cmd._is_own_git_checkout(str(project), git_exe)

    assert spy.argvs, "_is_own_git_checkout never spawned git"
    argv0, executable = spy.argvs[-1][0], spy.executables[-1]
    assert executable is not None, "the spawn pinned no executable"
    assert Path(executable).parent == realbin
    assert not spy.loaded(-1, str(project)).startswith(str(project))
    assert argv0 == "git", f"the child's own argv[0] became {argv0!r}"


def test_doctor_git_short_commit_and_behind_upstream_spawn_the_resolved_git(hostile, spy):
    """The two callers layered on top of `_is_own_git_checkout` -- each is its
    own `probe()` call and must resolve independently rather than falling
    back to a bare spawn once the guard above has passed."""
    project, realbin = hostile
    git_exe = doctor_cmd._resolve_git_executable()

    doctor_cmd._git_short_commit(str(project), git_exe)
    doctor_cmd._git_behind_upstream(str(project), git_exe)

    assert len(spy.argvs) >= 2, spy.argvs
    for argv, executable in zip(spy.argvs, spy.executables):
        assert argv[0] == "git", argv
        assert executable is not None, "a git spawn pinned no executable"
        assert Path(executable).parent == realbin


def test_doctor_git_core_longpaths_spawns_the_resolved_git(hostile, spy):
    """`_git_core_longpaths` is the one site that was spawned raw via
    `subprocess.run`, with no `probe()` and no `executable=` at all -- fails
    before the fix on the same `executable is None` assertion as the rest."""
    project, realbin = hostile
    git_exe = doctor_cmd._resolve_git_executable()

    doctor_cmd._git_core_longpaths(git_exe)

    assert spy.argvs, "_git_core_longpaths never spawned git"
    argv0, executable = spy.argvs[-1][0], spy.executables[-1]
    assert executable is not None, "the spawn pinned no executable"
    assert Path(executable).parent == realbin
    assert not spy.loaded(-1, str(project)).startswith(str(project))
    assert argv0 == "git", f"the child's own argv[0] became {argv0!r}"


def test_doctor_git_functions_degrade_gracefully_when_git_is_absent(tmp_path, monkeypatch, spy):
    """The not-found case: `git` on nobody's PATH must never crash `tan
    doctor`, and must never fall back to spawning the bare identity -- the
    exact hazard the resolution exists to close. A doctor that raises because
    git is missing is worse than the bug this fixes."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    git_exe = doctor_cmd._resolve_git_executable()
    assert git_exe is None

    assert doctor_cmd._is_own_git_checkout(str(tmp_path), git_exe) is False
    assert doctor_cmd._git_short_commit(str(tmp_path), git_exe) is None
    assert doctor_cmd._git_behind_upstream(str(tmp_path), git_exe) is None
    assert doctor_cmd._git_core_longpaths(git_exe) is None
    assert spy.argvs == [], f"a bare git was spawned despite resolving to nothing: {spy.argvs}"

    # And the whole envelope-facing check degrades to a plain "pass" with no
    # commit/skew attributed, rather than raising out of `_collect`.
    check = doctor_cmd.sdk_provenance_check(str(tmp_path))
    assert check.status == "pass"
    assert "@" not in check.detail, check.detail


def test_doctor_probe_host_python_spawns_the_resolved_interpreter(hostile, spy):
    """`_python_candidates()` used to hand `probe()` the bare `'py'`/`'python'`/
    `'python3'` identity; fails before the fix on the same `executable is
    None` assertion the rest of this file uses. Every candidate the fixture
    made resolvable gets spawned (empty stdout never parses as a version, so
    `_probe_host_python` tries them all) -- checked, not just the last."""
    project, realbin = hostile

    doctor_cmd._probe_host_python((0, 0))

    assert spy.argvs, "_probe_host_python never spawned an interpreter"
    for argv, executable in zip(spy.argvs, spy.executables):
        assert argv[0] in ("py", "python", "python3"), (
            f"the child's own argv[0] became {argv[0]!r}"
        )
        assert executable is not None, "an interpreter spawn pinned no executable"
        assert Path(executable).parent == realbin
    assert not spy.loaded(-1, str(project)).startswith(str(project))


def test_doctor_probe_host_python_skips_a_candidate_that_does_not_resolve(tmp_path, monkeypatch, spy):
    """No candidate on PATH at all: every one is skipped outright rather than
    handed to `subprocess` unresolved, and the function answers `None` instead
    of raising."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert doctor_cmd._probe_host_python((0, 0)) is None
    assert spy.argvs == [], f"an unresolved interpreter candidate was spawned: {spy.argvs}"
