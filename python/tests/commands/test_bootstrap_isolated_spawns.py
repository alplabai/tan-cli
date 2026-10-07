# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1331: the `python -m` spawns `tan bootstrap` makes run from an EMPTY
directory with ABSOLUTE path arguments, so a `pip.py` / `venv.py` planted in the
project cannot hijack them (the #1317 class, for the sites #1326 did not reach).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tan.commands import bootstrap_cmd
from tan.commands.bootstrap_cmd import HostPython, Runner, VenvBin, Workspace
from tan.core.bootstrap import parse_bootstrap_manifest

MANIFEST = (
    Path(__file__).resolve().parents[3] / "contract" / "fixtures" / "bootstrap" / "manifest.json"
).read_text(encoding="utf-8")


def plant(project: Path, module: str) -> Path:
    """A `<module>.py` in `project` that records that it ran and then exits 0."""
    marker = project / f"{module}.ran"
    (project / f"{module}.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('hijacked')\n", encoding="utf-8"
    )
    return marker


def test_the_planted_module_really_hijacks_an_unisolated_spawn(tmp_path, monkeypatch):
    # Guard the guard: without isolation the same plant DOES run, so the
    # isolated assertions below can fail.
    marker = plant(tmp_path, "pip")
    monkeypatch.chdir(tmp_path)
    Runner(json=True).run([sys.executable, "-m", "pip", "--version"])
    assert marker.exists()


@pytest.mark.parametrize("module", ["pip", "venv"])
def test_an_isolated_spawn_ignores_a_planted_module(tmp_path, monkeypatch, module):
    project = tmp_path / "project"
    project.mkdir()
    marker = plant(project, module)
    monkeypatch.chdir(project)
    argv = [sys.executable, "-m", module, "--version"] if module == "pip" else [
        sys.executable, "-m", "venv", str(tmp_path / "newvenv")
    ]
    assert Runner(json=True).run(argv, isolated=True) is None  # the REAL module ran
    assert not marker.exists()


def _workspace(facts) -> Workspace:
    # Deliberately RELATIVE paths: every spawn must absolutise them itself.
    return Workspace(
        is_windows=False, facts=facts, repo_root=Path("sdk"), workspace_dir=Path("."),
        venv_dir=Path(".venv"),
    )


def test_every_python_m_spawn_is_isolated_with_absolute_paths(tmp_path, monkeypatch):
    facts = parse_bootstrap_manifest(MANIFEST)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sdk").mkdir()
    ws = _workspace(facts)
    req = ws.workspace_dir / facts.zephyr_requirements_path
    req.parent.mkdir(parents=True)
    req.write_text("pyyaml\n", encoding="utf-8")
    seen: list[tuple[list[str], bool]] = []

    def fake_run(self, argv, cwd=None, extra_env=None, tail_lines=4, *, isolated=False):
        seen.append((list(argv), isolated))
        return None

    monkeypatch.setattr(Runner, "run", fake_run)
    log = bootstrap_cmd.Log(json_mode=True)
    runner = Runner(json=True)
    bootstrap_cmd._create_venv(ws, log, runner, HostPython((sys.executable,), (3, 12)))
    venv = VenvBin(Path(".venv/bin/python"), Path(".venv/bin/west"), "bin")
    bootstrap_cmd.pip_phase(ws, venv, log, runner, "linux")
    ws_missing_west = ws  # the `pip install west` site lives in west_phase
    bootstrap_cmd.west_phase(ws_missing_west, venv, log, runner, reuse=True)

    pip_m = [(a, iso) for a, iso in seen if "-m" in a and a[a.index("-m") + 1] in ("pip", "venv")]
    assert len(pip_m) >= 4  # venv, requirements, extras, editable (+ west install)
    for argv, isolated in pip_m:
        assert isolated, argv
        for arg in argv:
            if os.sep in arg:
                assert os.path.isabs(arg), f"relative path argument {arg!r} in {argv}"
