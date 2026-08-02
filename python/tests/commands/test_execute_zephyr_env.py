# SPDX-License-Identifier: Apache-2.0
"""tan-cli#308 end to end: `execute_slices` fills `ZEPHYR_BASE`/
`EXTRA_ZEPHYR_MODULES` for the spawned CMake/west child from the west
workspace it resolves itself (`tan.core.zephyr_env.zephyr_env_overrides`),
the same way `test_execute.py`'s own tan-cli#307
`test_west_build_pins_the_resolved_workspace_over_an_ancestor_west` proves
the workspace-pin wiring -- a manifest-verified `.west/config` naming the
fake `sdk_root`, not a bare directory, so `west_workspace_dir` actually
resolves it rather than silently no-op'ing to `None` (the pre-fix state,
which this suite's own `test_...` below reproduces to prove the fail-before/
pass-after ordering).

Slices here are declared `backend: baremetal`, not `zephyr`: the gap-filler
[`zephyr_env_overrides`] itself has no backend check (neither does the Rust
oracle's own call site, `execute/mod.rs`, inside its per-slice loop with no
guard before it) -- it is applied to every slice regardless. `zephyr` would
also work, but would additionally trip the UNRELATED tan-cli#309 Zephyr-
boilerplate guard for a probe command that (deliberately, for this file's own
purpose) never produces real Zephyr CMake evidence; `test_execute_zephyr_
guard.py` owns that guard's own coverage."""
import json
import os
import sys
from pathlib import Path

from tan.core.build_plan import parse_build_plan
from tan.commands.build.execute import execute_slices

PYTHON = json.dumps(sys.executable)
SEP = os.pathsep


def _plan(command: str, env: str = "{}", env_append_path: str = "{}") -> str:
    return f"""{{
      "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml", "sku": "S",
      "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
      "executionPolicy": {{"missingTool": "skip", "nullCommand": "skip", "unknownBackend": "fail"}},
      "slices": [{{
        "coreId": "c1", "backend": "baremetal", "buildDir": "build/c1", "appDir": "app",
        "configArtefacts": [], "toolchain": null, "artifacts": [], "debug": {{}},
        "command": {command}, "env": {env}, "envAppendPath": {env_append_path}
      }}]
    }}"""


def _make_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A manifest-verified west workspace (mirrors `test_execute.py`'s
    tan-cli#307 `real_ws` fixture): `real_ws/.west/config` names
    `real_ws/alp-sdk` as its manifest, and `real_ws/zephyr` stands in for the
    Zephyr checkout `resolve_zephyr_base` looks for. Returns
    `(real_ws, sdk_root, build_root)`."""
    real_ws = tmp_path / "real-ws"
    sdk_root = real_ws / "alp-sdk"
    sdk_root.mkdir(parents=True)
    (real_ws / ".west").mkdir()
    (real_ws / ".west" / "config").write_text("[manifest]\npath = alp-sdk\n", encoding="utf-8")
    (real_ws / "zephyr").mkdir()
    build_root = real_ws / "work" / "proj"
    build_root.mkdir(parents=True)
    return real_ws, sdk_root, build_root


def _probe_cmd(out_file: Path) -> str:
    script = (
        "import json, os\n"
        f"open({json.dumps(str(out_file))}, 'w').write(json.dumps(dict(os.environ)))\n"
    )
    return json.dumps({"tool": sys.executable, "args": ["-c", script], "cwd": None})


def test_fills_zephyr_base_and_extra_zephyr_modules_when_the_plan_carries_neither(
    tmp_path, monkeypatch
):
    """The behaviour tan-cli#308 reports missing: a plan slice with no
    `ZEPHYR_BASE`/`EXTRA_ZEPHYR_MODULES` pin gets both filled from the
    resolved workspace and `sdk_root`, not left to whatever the ambient
    process env happens to hold. Fails before the fix (both keys silently
    inherit whatever `dict(os.environ)` had -- `None`/unset in a scrubbed
    test env) and passes after."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    monkeypatch.delenv("EXTRA_ZEPHYR_MODULES", raising=False)
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    out_file = tmp_path / "env.json"

    out = execute_slices(
        parse_build_plan(_plan(_probe_cmd(out_file))),
        build_root=build_root,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
        sdk_root=str(sdk_root),
    )

    assert out[0].status == "succeeded", out[0].message
    seen = json.loads(out_file.read_text(encoding="utf-8"))
    assert Path(seen["ZEPHYR_BASE"]).samefile(real_ws / "zephyr")
    assert seen["EXTRA_ZEPHYR_MODULES"] == str(sdk_root)


def test_a_stale_ambient_zephyr_base_does_not_win_over_the_resolved_workspace(
    tmp_path, monkeypatch
):
    """tan-cli#308's actual reported defect: an exported `$ZEPHYR_BASE` left
    over from an unrelated tree (a `source zephyr-env.sh`, or an older `tan
    bootstrap` next-steps block) must not survive into the spawned child once
    `tan` has resolved a real workspace of its own. `execute_slices` seeds
    the child from `dict(os.environ)` first (line ~594) -- the ambient value
    -- so this genuinely exercises the override, not just the gap-fill."""
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    stale = tmp_path / "stale-unrelated-zephyr"
    stale.mkdir()
    monkeypatch.setenv("ZEPHYR_BASE", str(stale))
    out_file = tmp_path / "env.json"

    out = execute_slices(
        parse_build_plan(_plan(_probe_cmd(out_file))),
        build_root=build_root,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
        sdk_root=str(sdk_root),
    )

    assert out[0].status == "succeeded", out[0].message
    seen = json.loads(out_file.read_text(encoding="utf-8"))
    assert Path(seen["ZEPHYR_BASE"]).samefile(real_ws / "zephyr")
    assert not Path(seen["ZEPHYR_BASE"]).samefile(stale)


def test_a_plan_pinned_extra_zephyr_modules_env_append_path_is_not_clobbered(
    tmp_path, monkeypatch
):
    """Plan wins: an SDK-emitted plan's own `envAppendPath.EXTRA_ZEPHYR_MODULES`
    (the common case tan-cli#308's own severity note names) must survive
    untouched -- not get overwritten with just the hand-derived `sdk_root`,
    which would silently drop any OTHER module path the plan appended."""
    monkeypatch.delenv("EXTRA_ZEPHYR_MODULES", raising=False)
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    out_file = tmp_path / "env.json"

    out = execute_slices(
        parse_build_plan(
            _plan(
                _probe_cmd(out_file),
                env_append_path='{"EXTRA_ZEPHYR_MODULES": ["/plan/other-module"]}',
            )
        ),
        build_root=build_root,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
        sdk_root=str(sdk_root),
    )

    assert out[0].status == "succeeded", out[0].message
    seen = json.loads(out_file.read_text(encoding="utf-8"))
    assert seen["EXTRA_ZEPHYR_MODULES"] == "/plan/other-module"
    # ZEPHYR_BASE is independent of this key -- still filled.
    assert Path(seen["ZEPHYR_BASE"]).samefile(real_ws / "zephyr")


def test_an_inherited_pythonpath_is_still_extended_not_replaced(tmp_path, monkeypatch):
    """Confirms the pre-existing "plan wins / CLI fills gaps" seeding
    (`assemble_slice_env`, tan.core.plan_exec) still holds through
    `execute_slices` after wiring the new zephyr gap-fillers alongside it --
    the new per-slice `slice_gap_fillers` list must not disturb the
    envAppendPath-seeding path an unrelated var like `PYTHONPATH` takes."""
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    out_file = tmp_path / "env.json"

    out = execute_slices(
        parse_build_plan(
            _plan(
                _probe_cmd(out_file),
                env_append_path='{"PYTHONPATH": ["/plan/scripts"]}',
            )
        ),
        build_root=build_root,
        env_lookup=lambda k: "/inherited/scripts" if k == "PYTHONPATH" else None,
        gap_fillers=[],
        on_output=lambda s: None,
        sdk_root=str(sdk_root),
    )

    assert out[0].status == "succeeded", out[0].message
    seen = json.loads(out_file.read_text(encoding="utf-8"))
    assert seen["PYTHONPATH"] == f"/inherited/scripts{SEP}/plan/scripts"
