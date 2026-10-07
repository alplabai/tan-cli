# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1336: where the Flow D J-Link binary may come from."""
from __future__ import annotations

import os

import pytest

from tan.core import jlink_binary
from tan.core.jlink_binary import resolve_jlink

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executables")


def _exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_path_is_searched_and_reported(tmp_path):
    exe = _exe(tmp_path / "bin" / "JLinkExe")
    found = resolve_jlink(None, {"PATH": str(tmp_path / "bin")})
    assert found.path == exe and found.source == "PATH"


def test_cli_then_env_outrank_path_and_never_fall_through(tmp_path):
    on_path = _exe(tmp_path / "bin" / "JLinkExe")
    cli = _exe(tmp_path / "mine" / "JLinkExe")
    env_one = _exe(tmp_path / "env" / "JLinkExe")
    env = {"PATH": str(tmp_path / "bin"), "TAN_JLINK": env_one}
    assert resolve_jlink(cli, env).path == cli
    assert resolve_jlink(None, env).path == env_one
    assert resolve_jlink(None, env).source == "TAN_JLINK"
    # A named-but-missing override is NOT replaced by the PATH binary.
    assert resolve_jlink(str(tmp_path / "nope"), env) is None
    assert resolve_jlink(None, {"PATH": str(tmp_path / "bin"), "TAN_JLINK": "/nope"}) is None
    assert on_path


def test_a_segger_install_root_is_the_last_resort(tmp_path, monkeypatch):
    exe = _exe(tmp_path / "opt" / "JLink_V950" / "JLinkExe")
    monkeypatch.setattr(
        jlink_binary, "_install_roots", lambda env, platform: [str(tmp_path / "opt" / "JLink_V950")]
    )
    found = resolve_jlink(None, {"PATH": ""})
    assert found.path == exe and found.source.startswith("SEGGER install")


def test_install_roots_per_platform(tmp_path, monkeypatch):
    pf = tmp_path / "PF"
    (pf / "SEGGER" / "JLink_V810").mkdir(parents=True)
    monkeypatch.undo()
    roots = jlink_binary.__dict__["_install_roots"]
    # (the autouse fixture is undone above so the real function runs)
    assert roots({"ProgramFiles": str(pf)}, "win32") == [str(pf / "SEGGER" / "JLink_V810")]
