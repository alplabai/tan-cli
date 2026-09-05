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
import shutil
import sys
from pathlib import Path

from tan.core.build_plan import parse_build_plan
from tan.commands.build.execute import execute_slices
from tan.core import toolchain_provision as tp

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


# --------------------------------------------------------------------------
# tan-cli#308 x tan-cli#336: the two fixes meet on the SAME `env` dict, and
# the naive composition silently cancels one of them.
#
# Every test above drives a `backend: baremetal` slice whose `tool` is the
# interpreter itself, so `is_west` is False and #336's `env.pop` never runs --
# which is exactly why the broken composition passed the whole suite. These
# two use `tool: "west"` (the only shape that reaches the pop) and assert the
# composed outcome, not either fix in isolation.
# --------------------------------------------------------------------------


def _west_plan(out_file: Path, env: str = "{}") -> str:
    """A `tool: "west"` slice -- the ONLY shape `is_west` is true for, and so
    the only one the tan-cli#336 `ZEPHYR_BASE` pop is reachable through. Kept
    `backend: baremetal` for the same reason the rest of this file is (the
    unrelated tan-cli#309 Zephyr guard owns its own suite), and `args[0]` is
    deliberately NOT `"build"` so tan-cli#307's `_pin_west_workspace` leaves
    cwd and args verbatim and the probe can just dump its env."""
    script = (
        "import json, os, sys\n"
        f"open({json.dumps(str(out_file))}, 'w').write(json.dumps(dict(os.environ)))\n"
    )
    return _plan(json.dumps({"tool": "west", "args": ["-c", script], "cwd": None}), env=env)


def _plant_west(build_root: Path) -> None:
    """`execute_slices` rewrites `tool == "west"` to the workspace venv's own
    `west`; plant a spawnable one there (a renamed copy of this interpreter,
    the same recipe `test_execute.py::_plant_spawnable_west` uses) so the
    slice actually dispatches instead of skipping on `missingTool`."""
    from tan.core.venv import venv_layout

    layout = venv_layout(os.name == "nt")
    west_path = build_root / ".venv" / layout.bin_dir / layout.west
    west_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        for dll in Path(sys.executable).parent.glob("*.dll"):
            shutil.copy(dll, west_path.parent / dll.name)
        shutil.copy(sys.executable, west_path)
    else:
        west_path.write_text(
            f'#!/bin/sh\nexec {json.dumps(sys.executable)} "$@"\n', encoding="utf-8"
        )
        os.chmod(west_path, 0o755)


def test_the_336_pop_does_not_strip_the_308_gap_filled_zephyr_base(tmp_path, monkeypatch):
    """The composition regression. tan-cli#336 pops an inherited
    `ZEPHYR_BASE` off a west slice's env; tan-cli#308 FILLS that same key
    from the resolved workspace. #308's fill lands via `assemble_slice_env`,
    so a pop keyed on the plan's `sl.env` alone cannot see it and strips it
    right back out -- on precisely the slices #308 exists to serve (the ones
    that do NOT pin the key themselves).

    Fails on the naive merge with `ZEPHYR_BASE` absent from the child's env
    entirely; passes once the pop is keyed on the assembled `slice_env`."""
    monkeypatch.setenv("ZEPHYR_BASE", str(tmp_path / "stale-ambient"))
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    _plant_west(build_root)
    out_file = tmp_path / "env.json"

    out = execute_slices(
        parse_build_plan(_west_plan(out_file)),
        build_root=build_root,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
        sdk_root=str(sdk_root),
    )

    assert out[0].status == "succeeded", out[0].message
    seen = json.loads(out_file.read_text(encoding="utf-8"))
    assert "ZEPHYR_BASE" in seen, "#336's pop stripped the value #308 had just filled"
    assert Path(seen["ZEPHYR_BASE"]).samefile(real_ws / "zephyr")


def test_a_stale_ambient_zephyr_base_is_still_dropped_when_308_cannot_fill(
    tmp_path, monkeypatch
):
    """The other half: #336 must still fire where #308 has nothing to give.
    A workspace that resolved but was never `west update`d has no `zephyr/`,
    so `zephyr_env_overrides` yields no `ZEPHYR_BASE` -- and without the pop
    the child inherits the stale ambient one and west trusts it unchecked
    (`west/app/main.py::set_zephyr_base` has no existence check)."""
    stale = tmp_path / "stale-ambient"
    stale.mkdir()
    monkeypatch.setenv("ZEPHYR_BASE", str(stale))
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    (real_ws / "zephyr").rmdir()  # resolved workspace, never `west update`d
    _plant_west(build_root)
    out_file = tmp_path / "env.json"

    out = execute_slices(
        parse_build_plan(_west_plan(out_file)),
        build_root=build_root,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
        sdk_root=str(sdk_root),
    )

    assert out[0].status == "succeeded", out[0].message
    seen = json.loads(out_file.read_text(encoding="utf-8"))
    assert "ZEPHYR_BASE" not in seen, f"stale ambient value survived: {seen.get('ZEPHYR_BASE')}"


def test_a_plan_pinned_zephyr_base_survives_both_the_fill_and_the_pop(tmp_path, monkeypatch):
    """"Plan wins" is the invariant BOTH fixes claim to respect, and it is
    the one a wrong pop condition breaks most visibly. A slice pinning
    `ZEPHYR_BASE` in its own `env` must reach the child with that exact
    value -- neither overwritten by #308's gap filler nor popped by #336."""
    monkeypatch.setenv("ZEPHYR_BASE", str(tmp_path / "stale-ambient"))
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    _plant_west(build_root)
    out_file = tmp_path / "env.json"
    pinned = str(tmp_path / "plan-pinned-zephyr")

    out = execute_slices(
        parse_build_plan(_west_plan(out_file, env=json.dumps({"ZEPHYR_BASE": pinned}))),
        build_root=build_root,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
        sdk_root=str(sdk_root),
    )

    assert out[0].status == "succeeded", out[0].message
    seen = json.loads(out_file.read_text(encoding="utf-8"))
    assert seen["ZEPHYR_BASE"] == pinned


# --------------------------------------------------------------------------
# tan-cli#1209: `ZEPHYR_SDK_INSTALL_DIR` handoff. `tan bootstrap` acquires
# the cross toolchain into `~/.alp/toolchains/zephyr-sdk-<v>-arm-zephyr-eabi/`
# and stamps it; CMake's own `FindZephyr-sdk.cmake` prefix scan never looks
# that deep under `$HOME`, so without this fill-in `tan build` fails to
# configure right after `tan bootstrap` reported success. Mirrors
# `test_doctor_toolchain_check.py`'s own manifest/stamp fixture shapes --
# the doctor `toolchain` check and this gap-filler both key off the exact
# same `stamp_matches_pin` predicate (`tan.commands.build.toolchain.
# verified_store_dir`), and must not independently drift on it.
# --------------------------------------------------------------------------


def _small_toolchain_manifest(*, version: str = "1.0.1") -> str:
    return json.dumps(
        {
            "zephyrSdk": {
                "version": version,
                "baseUrl": "https://example.invalid/",
                "artifacts": [
                    {
                        "host": "linux-x86_64", "component": "minimal-sdk",
                        "filename": "x.tar.xz", "sizeBytes": 1, "sha256": "a" * 64,
                    }
                ],
            }
        }
    )


def _write_toolchain_manifest(sdk_root: Path, manifest_text: str) -> None:
    metadata_dir = sdk_root / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "toolchains.json").write_text(manifest_text, encoding="utf-8")


def _write_verified_stamp(store_dir: Path, manifest: tp.ToolchainManifest) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    stamp = tp.ToolchainStamp(manifest.version, manifest.digest(), "arm-zephyr-eabi-gcc 14.3.0")
    (store_dir / tp.STAMP_FILENAME).write_text(tp.render_stamp(stamp), encoding="utf-8")


def _point_home_at(monkeypatch, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("ALP_TOOLCHAIN_ROOT", raising=False)
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)


def test_fills_zephyr_sdk_install_dir_from_tans_verified_store(tmp_path, monkeypatch):
    """tan-cli#1209's actual reported defect: `tan bootstrap` acquires the
    cross toolchain and stamps it, `tan doctor` reports it present, and `tan
    build`'s spawned `west`/CMake child still cannot find it -- CMake's own
    prefix scan looks one level too shallow under `$HOME`. Fails before the
    fix with a `KeyError` below (the key is simply absent from the child's
    environment); passes once the verified store is exported."""
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    manifest_text = _small_toolchain_manifest()
    _write_toolchain_manifest(sdk_root, manifest_text)
    manifest = tp.parse_toolchain_manifest(manifest_text)
    home = tmp_path / "home"
    _point_home_at(monkeypatch, home)
    store_dir = home / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    _write_verified_stamp(store_dir, manifest)
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
    assert Path(seen["ZEPHYR_SDK_INSTALL_DIR"]).samefile(store_dir)


def test_an_inherited_zephyr_sdk_install_dir_survives_verbatim_despite_a_valid_stamp(
    tmp_path, monkeypatch
):
    """Precedence: an inherited, non-blank `ZEPHYR_SDK_INSTALL_DIR` is the
    user's own deliberate choice and wins outright, even with a verified
    stamped store available -- taken verbatim, never probed for existence
    or overridden by tan's own store."""
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    manifest_text = _small_toolchain_manifest()
    _write_toolchain_manifest(sdk_root, manifest_text)
    manifest = tp.parse_toolchain_manifest(manifest_text)
    home = tmp_path / "home"
    _point_home_at(monkeypatch, home)
    store_dir = home / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    _write_verified_stamp(store_dir, manifest)
    user_sdk = tmp_path / "user-installed-sdk-9.9.9"
    user_sdk.mkdir()
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(user_sdk))
    out_file = tmp_path / "env.json"

    out = execute_slices(
        parse_build_plan(_plan(_probe_cmd(out_file))),
        build_root=build_root,
        env_lookup=os.environ.get,
        gap_fillers=[],
        on_output=lambda s: None,
        sdk_root=str(sdk_root),
    )

    assert out[0].status == "succeeded", out[0].message
    seen = json.loads(out_file.read_text(encoding="utf-8"))
    assert seen["ZEPHYR_SDK_INSTALL_DIR"] == str(user_sdk)


def test_a_stale_digest_stamp_exports_nothing(tmp_path, monkeypatch):
    """ADR 0021's own words: 'a stamped 1.0.1 store against a moved pin is a
    Fail with a fix, not "a toolchain exists"'. A stamp naming the right
    version but a manifest digest that no longer matches must not be
    trusted -- `ZEPHYR_SDK_INSTALL_DIR` stays unfilled, same as no stamp."""
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    manifest_text = _small_toolchain_manifest()
    _write_toolchain_manifest(sdk_root, manifest_text)
    manifest = tp.parse_toolchain_manifest(manifest_text)
    home = tmp_path / "home"
    _point_home_at(monkeypatch, home)
    store_dir = home / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    store_dir.mkdir(parents=True)
    stale_stamp = tp.ToolchainStamp(manifest.version, "f" * 64, "arm-zephyr-eabi-gcc 14.3.0")
    (store_dir / tp.STAMP_FILENAME).write_text(tp.render_stamp(stale_stamp), encoding="utf-8")
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
    assert "ZEPHYR_SDK_INSTALL_DIR" not in seen


def test_alp_toolchain_root_ancestor_with_unstamped_hand_install_exports_nothing(
    tmp_path, monkeypatch
):
    """The `$ALP_TOOLCHAIN_ROOT` ancestor trap: pointed at `$HOME`, tan's
    store root coincides with where a hand-installed `zephyr-sdk-1.0.1`
    legitimately lives. Unstamped, it must never be mistaken for tan's own
    verified store."""
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    _write_toolchain_manifest(sdk_root, _small_toolchain_manifest())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("ALP_TOOLCHAIN_ROOT", str(home))
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    (home / "zephyr-sdk-1.0.1").mkdir()
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
    assert "ZEPHYR_SDK_INSTALL_DIR" not in seen


def test_alp_toolchain_root_ancestor_with_stamped_leaf_exports_the_leaf_never_home(
    tmp_path, monkeypatch
):
    """Same ancestor override, but the leaf IS stamped: the fill-in exports
    that per-version leaf, never the ancestor root itself."""
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    manifest_text = _small_toolchain_manifest()
    _write_toolchain_manifest(sdk_root, manifest_text)
    manifest = tp.parse_toolchain_manifest(manifest_text)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("ALP_TOOLCHAIN_ROOT", str(home))
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    store_dir = home / tp.store_dir_name(manifest.version)
    _write_verified_stamp(store_dir, manifest)
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
    assert Path(seen["ZEPHYR_SDK_INSTALL_DIR"]).samefile(store_dir)
    assert not Path(seen["ZEPHYR_SDK_INSTALL_DIR"]).samefile(home)


def test_a_plan_pinned_zephyr_sdk_install_dir_is_not_overwritten(tmp_path, monkeypatch):
    """Plan wins: a slice pinning `ZEPHYR_SDK_INSTALL_DIR` in its own `env`
    keeps that exact value even with a verified store available."""
    real_ws, sdk_root, build_root = _make_workspace(tmp_path)
    manifest_text = _small_toolchain_manifest()
    _write_toolchain_manifest(sdk_root, manifest_text)
    manifest = tp.parse_toolchain_manifest(manifest_text)
    home = tmp_path / "home"
    _point_home_at(monkeypatch, home)
    store_dir = home / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    _write_verified_stamp(store_dir, manifest)
    pinned = str(tmp_path / "plan-pinned-sdk")
    out_file = tmp_path / "env.json"

    out = execute_slices(
        parse_build_plan(
            _plan(_probe_cmd(out_file), env=json.dumps({"ZEPHYR_SDK_INSTALL_DIR": pinned}))
        ),
        build_root=build_root,
        env_lookup=lambda k: None,
        gap_fillers=[],
        on_output=lambda s: None,
        sdk_root=str(sdk_root),
    )

    assert out[0].status == "succeeded", out[0].message
    seen = json.loads(out_file.read_text(encoding="utf-8"))
    assert seen["ZEPHYR_SDK_INSTALL_DIR"] == pinned
