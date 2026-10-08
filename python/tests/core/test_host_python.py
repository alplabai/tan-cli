# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1317: one host-interpreter resolver for doctor and build."""
import os
import stat

import pytest

from tan.commands import doctor_cmd
from tan.core import host_python as hp

posix_only = pytest.mark.skipif(os.name == "nt", reason="shell-script fake interpreters")


def _fake(bindir, name, version, west=True):
    path = bindir / name
    path.write_text(
        f"#!/bin/sh\nprintf '{version}\\n{path}\\nwest={1 if west else 0}\\n'\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def path_of(monkeypatch):
    def _set(*dirs):
        monkeypatch.setenv("PATH", os.pathsep.join(str(d) for d in dirs))

    return _set


def _dirs(tmp_path, *names):
    out = []
    for n in names:
        d = tmp_path / n
        d.mkdir()
        out.append(d)
    return out


@posix_only
def test_first_qualifying_absolute_path(tmp_path, path_of):
    old, new = _dirs(tmp_path, "old", "new")
    _fake(old, "python3", "3.10")
    good = _fake(new, "python", "3.14")
    path_of(old, new)
    py, refusal = hp.build_interpreter(None, "python3", True, (3, 12))
    assert refusal is None and py == good and os.path.isabs(py)


@posix_only
def test_venv_python_without_west_first_on_path_is_skipped(tmp_path, path_of):
    venv, usr = _dirs(tmp_path, "venv", "usr")
    _fake(venv, "python3", "3.13", west=False)
    good = _fake(usr, "python3", "3.12", west=True)
    path_of(venv, usr)
    assert hp.build_interpreter(None, "python3", True, (3, 12)) == (good, None)


@posix_only
def test_refuses_naming_each_candidate_and_why(tmp_path, path_of):
    a, b = _dirs(tmp_path, "a", "b")
    _fake(a, "python3.10", "3.10")
    _fake(b, "python3", "3.14", west=False)
    path_of(a, b)
    _, refusal = hp.build_interpreter(None, "python3", True, (3, 12))
    assert "3.10" in refusal and "too old" in refusal
    assert "3.14" in refusal and "no `west` module" in refusal


@posix_only
def test_refuses_when_nothing_runs(tmp_path, path_of):
    path_of(tmp_path)
    _, refusal = hp.build_interpreter(None, "python3", True, (3, 12))
    assert refusal and "no runnable" in refusal


@posix_only
def test_venv_and_tokenless_plans_are_untouched(tmp_path, path_of):
    path_of(tmp_path)
    assert hp.build_interpreter("/v/bin/python", "python3", True, (3, 12)) == ("/v/bin/python", None)
    assert hp.build_interpreter(None, "python3", False, (3, 12)) == ("python3", None)


@posix_only
def test_doctor_picks_the_same_interpreter_as_build(tmp_path, path_of):
    venv, usr = _dirs(tmp_path, "venv", "usr")
    _fake(venv, "python3", "3.13", west=False)
    good = _fake(usr, "python3", "3.12")
    path_of(venv, usr)
    assert doctor_cmd._probe_host_python((3, 12))[2] == good
    assert hp.build_interpreter(None, "python3", True, (3, 12))[0] == good


@posix_only
def test_doctor_warns_when_no_candidate_is_floor_and_west(tmp_path, path_of):
    (d,) = _dirs(tmp_path, "d")
    _fake(d, "python3", "3.14", west=False)
    path_of(d)
    c = doctor_cmd.host_python_check(("python3", (3, 14)), (3, 12), "x", False)
    assert (c.name, c.status, c.code) == ("hostPython", "warn", None)
    assert "no `west` module" in c.detail and "tan bootstrap" in c.fix


@posix_only
def test_doctor_passes_with_west_capable_candidate_or_workspace_venv(tmp_path, path_of):
    (d,) = _dirs(tmp_path, "d")
    _fake(d, "python3", "3.14", west=True)
    path_of(d)
    assert doctor_cmd.host_python_check(("python3", (3, 14)), (3, 12), "x", False).status == "pass"
    (e,) = _dirs(tmp_path, "e")
    _fake(e, "python3", "3.14", west=False)
    path_of(e)
    assert doctor_cmd.host_python_check(("python3", (3, 14)), (3, 12), "x", True).status == "pass"


@posix_only
def test_a_planted_west_py_in_the_cwd_is_never_executed(tmp_path, path_of, monkeypatch):
    """Module-hijack regression: `python -c` puts the cwd on sys.path, so a
    `west.py` in the project dir must not run during the probe."""
    import sys

    marker = tmp_path / "PWNED"
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "west.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    (proj / "ensurepip.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    real = os.path.realpath(sys.executable)
    d = tmp_path / "bin"
    d.mkdir()
    (d / "python3").symlink_to(real)  # a real interpreter, run for real
    path_of(d)
    monkeypatch.chdir(proj)
    hp.probe_all_host_pythons()
    from tan.core.probe import probe

    probe([str(d / "python3"), "-c", "import west"], executable=str(d / "python3"))
    assert not marker.exists()
    doctor_cmd._posix_venv_capable([str(d / "python3")], executable=str(d / "python3"))
    assert not marker.exists()


# --- platform-neutral (no shell scripts): run on Windows too -----------------


def _hp(interp, version=(3, 14), west=True):
    return hp.HostPython("python", version, interp, interp, west)


def test_baked_interpreter_is_forward_slashed():
    py, refusal = hp.build_interpreter(
        None, "python", True, (3, 12), probe_all=lambda: [_hp("C:\\Python314\\python.exe")]
    )
    assert refusal is None and py == "C:/Python314/python.exe"


def test_floor_is_the_callers_not_a_constant():
    found = [_hp("/usr/bin/python3", (3, 13))]
    assert hp.build_interpreter(None, "p", True, (3, 14), probe_all=lambda: found)[1]
    assert hp.build_interpreter(None, "p", True, (3, 13), probe_all=lambda: found)[1] is None


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 000 does not deny root / Windows",
)
def test_unreadable_path_dir_is_skipped(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        got = hp._posix_path_pythons({"PATH": os.pathsep.join([str(tmp_path / "nope"), str(locked)])})
    finally:
        locked.chmod(0o700)
    assert got == []


def test_python_dash_c_detection_handles_bundles_and_free_threaded_names():
    from tan.core.probe import is_python_dash_c as d

    assert d(["python3", "-c", "x"]) and d(["python3.13t", "-Ic", "x"]) and d(["/u/python3", "-Sc", "x"])
    assert d(["py", "-3", "-c", "x"])
    assert not d(["git", "-c", "x"]) and not d(["python3", "-m", "pip"])


def test_relative_program_path_still_resolves_under_the_empty_cwd(tmp_path, monkeypatch):
    import sys

    from tan.core.probe import probe

    rel = os.path.relpath(sys.executable, tmp_path)
    monkeypatch.chdir(tmp_path)
    out = probe([rel, "-c", "print(1)"], executable=rel)
    assert out is not None and out.strip() == "1"


def test_materialise_refusal_is_a_warning_and_native_is_per_slice():
    from types import SimpleNamespace as NS

    from tan.commands import build_cmd as b
    from tan.commands.build.host_python import BuildPython

    bp = BuildPython("python3", "no python", frozenset({"m55", "m33"}))
    # --materialise never runs CMake: warning only, no per-slice refusal
    assert [i.severity for i in b._host_python_issues(bp, b._MODE_MATERIALISE)] == ["warning"]
    assert b._python_refusals(bp, [], b._MODE_MATERIALISE) == {}
    # native: no global issue; refusals only for non-demoted users
    assert b._host_python_issues(bp, b._MODE_NATIVE) == []
    assert set(b._python_refusals(bp, [NS(core_id="m55")], b._MODE_NATIVE)) == {"m33"}
    ok = BuildPython("p", None, frozenset({"m55"}))
    assert b._python_refusals(ok, [], b._MODE_NATIVE) == {}


def _plan_json(tool):
    return (
        '{"schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml", "sku": "S",'
        ' "buildRoot": "build", "sharedArtefacts": [], "warnings": [],'
        ' "executionPolicy": {"missingTool": "skip", "nullCommand": "skip", "unknownBackend": "fail"},'
        ' "slices": [{"coreId": "c1", "backend": "baremetal", "buildDir": "build/c1", "appDir": "app",'
        ' "configArtefacts": [], "toolchain": null, "artifacts": [], "debug": {},'
        f' "command": {{"tool": "{tool}", "args": ["-c", "print(1)"], "cwd": null}},'
        ' "env": {}, "envAppendPath": {}}]}'
    )


def test_native_python_refusal_skips_when_the_tool_is_missing_and_fails_otherwise(tmp_path):
    """executionPolicy contract: a slice that would be SKIPPED (west absent)
    stays skipped even when no host Python qualifies; a slice that would run
    FAILS with the refusal."""
    import sys

    from tan.commands.build.execute import execute_slices
    from tan.core.build_plan import parse_build_plan

    refusals = {"c1": "no usable python"}

    def run(tool):
        return execute_slices(
            parse_build_plan(_plan_json(tool)),
            build_root=tmp_path,
            env_lookup=lambda k: None,
            gap_fillers=[],
            on_output=lambda s: None,
            slice_refusals=refusals,
        )[0]

    assert run("definitely-not-a-real-tool-xyz").status == "skipped"
    out = run(sys.executable.replace("\\", "/"))
    assert out.status == "failed" and out.message == "no usable python"


@posix_only
def test_usrmerge_alias_dirs_fold_but_venv_stays_separate(tmp_path):
    real = tmp_path / "usr_bin"
    real.mkdir()
    _fake(real, "python3", "3.13")
    alias = tmp_path / "bin"
    alias.symlink_to(real)
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "python3").symlink_to(real / "python3")
    got = hp._posix_path_pythons({"PATH": os.pathsep.join(str(d) for d in (venv, alias, real))})
    assert got == [str(venv / "python3"), str(alias / "python3")]


def test_doctor_and_build_floor_agree_over_manifests(tmp_path, monkeypatch):
    import json

    from tan.core import python_floor as pf

    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    for name, doc in {
        "ok": {"schemaVersion": 1, "prerequisites": {"pythonMinVersion": "3.11"}, "zephyr": {"pythonMinVersion": "3.13"}},
        "nozephyr": {"schemaVersion": 1, "prerequisites": {"pythonMinVersion": "3.14"}},
        "badschema": {"schemaVersion": 9, "prerequisites": {"pythonMinVersion": "3.14"}},
    }.items():
        sdk = tmp_path / name
        (sdk / "metadata").mkdir(parents=True)
        (sdk / "metadata" / "bootstrap.json").write_text(json.dumps(doc))
        loaded = doctor_cmd._load_manifest(str(sdk))
        d_m = doctor_cmd._manifest_floor_from_facts(loaded.facts)
        d_z = doctor_cmd._zephyr_manifest_floor_from_facts(loaded.facts)
        assert pf.read_manifest_floors(str(sdk)) == (d_m, d_z), name
