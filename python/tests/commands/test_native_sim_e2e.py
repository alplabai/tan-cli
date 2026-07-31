# SPDX-License-Identifier: Apache-2.0
"""``tan init`` -> ``tan generate`` -> ``west build -b native_sim/native/64``,
all the way to a real, on-disk compiled host binary.

Every other test under ``tests/commands/`` stops at a plan, a materialised
file, or a dry-run exit code (see ``test_build_command.py`` and
``test_execute.py``'s own docstrings). None of them takes a *scaffolded*
project all the way to a *compiled artefact*. ``native_sim`` is the one Zephyr
target that needs no hardware to run, which is why it is the one e2e that can
plausibly live in CI -- see the acceptance criteria in
``docs/superpowers/specs/2026-07-29-tan-python-executor-mvp-design.md``
(alp-sdk): "``tan build`` ... must compile a real ``zephyr.elf`` + ...".

**Why this does not call ``tan build``.** ``tan build``'s SDK-derived plan
always targets the project's REAL SoM board -- verified empirically against
the frozen Rust oracle: ``tan build --plan`` on ``examples/testing/
catch2-selftest/board.yaml`` (whose own ``testcase.yaml`` restricts it to
``native_sim``/``native_sim/native/64``!) still emits ``west build -b
alp_e1m_aen801_m55_hp/...``, never a ``native_sim`` board id. The west board
target (``-b``) and ``board.yaml``'s ``som.sku`` (which only feeds the
generated Kconfig fragment) are independent knobs; nothing in ``tan build``'s
plan ever sets the former to ``native_sim``. That is also how alp-sdk itself
reaches ``native_sim`` today -- never through ``tan build``, always through
twister (``docs/cross-platform-setup.md`` §6.3) or a direct ``west build -b
native_sim/native/64`` with a hand-supplied ``alp.conf``/overlay pair (see
``.github/workflows/pr-tier-a-libraries.yml``).

So this test drives the SAME two tan surfaces that path uses:

- ``tan init`` scaffolds a project (the default ``zephyr-app`` template: one
  Zephyr core, ``E1M-AEN801``). Its ``CMakeLists.txt`` already renders this
  project's own per-core ``alp.conf`` at CMake CONFIGURE time (an
  ``execute_process`` calling ``scripts/alp_project.py --emit zephyr-conf``)
  and layers it via ``EXTRA_CONF_FILE`` -- landing under ``generated/``, not
  the build-dir root, because (``cmake/alp.cmake``'s own documented reason)
  Zephyr's ``kconfig.cmake`` greps ``${APPLICATION_BINARY_DIR}/*.conf`` at the
  END of its merge list, so a same-level ``.conf`` file would silently beat
  every ``EXTRA_CONF_FILE`` overlay. Nothing to do here beyond scaffolding.
- ``tan generate --target native-sim-overlay`` renders the ONE artefact the
  scaffold does not produce itself: the per-example ``boards/
  native_sim_native_64.overlay`` convention (gpio-emul backed, so a
  GPIO-touching app resolves under ``native_sim`` with no silicon) -- Zephyr
  auto-discovers this from the app's own source tree, so it must exist
  *before* ``west build`` runs, and nothing renders it automatically today.

Then a real ``west build -b native_sim/native/64`` compiles it, and the test
asserts the produced ``zephyr.exe`` actually exists on disk (not just an exit
code of 0).

**Skip, never fail, when the toolchain is not here.** ``native_sim`` is
Linux/macOS only -- upstream Zephyr ships no native-Windows target
(``docs/cross-platform-setup.md`` §4.6) -- so this always skips on ``win32``.
Everywhere else it skips (loudly, with a specific reason) unless a real
``ZEPHYR_BASE`` workspace, a west-capable venv, a host C compiler, and an
alp-sdk checkout (``ALP_SDK_PARITY_ROOT``/``ALP_SDK_ROOT``, the same env pair
``tests/commands/test_build_manifest.py`` and the parity suite use) are ALL
resolvable. A red test on a laptop with no Zephyr workspace would be worse
than no test at all; this only ever goes red once every one of those
preconditions holds and the compile itself regresses.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tan.commands.build_cmd import _find_workspace_venv
from tan.core.bootstrap import venv_layout
from tan.core.plan_exec import apply_env_append
from tests.conftest import REAL_ENVIRON, sdk_root

#: ``python/`` -- pinned onto every spawned ``tan``'s PYTHONPATH so
#: ``python -m tan`` resolves from a scratch cwd with no ``pip install``.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

#: Captured at MODULE IMPORT time -- i.e. at collection, before pytest ever
#: runs a test function. `tests/conftest.py`'s autouse `_scrub_sdk_discovery_env`
#: deletes `ALP_SDK_ROOT` from the process environment ahead of every test
#: function (hermeticity for the tests that assert "nothing resolves"), so a
#: call to `sdk_root()` made from inside `_skip_reason()`/the test body -- i.e.
#: AFTER that fixture has already run -- always sees it gone, even on a host
#: where it was exported for real. `test_kconfig_symbols.py` and
#: `test_planner_emit_parity.py` hit the same fixture and use the identical
#: module-level-capture pattern.
SDK = sdk_root()


def _zephyr_base() -> Path | None:
    raw = os.environ.get("ZEPHYR_BASE")
    if not raw:
        return None
    base = Path(raw)
    return base if (base / "VERSION").is_file() else None


def _host_c_compiler_missing() -> bool:
    """``native_sim`` links with the HOST toolchain (not the Zephyr SDK's
    cross ``arm-zephyr-eabi-gcc``) -- ``cc``/``gcc``/``clang`` on PATH is what
    west's ``host`` toolchain variant actually spawns."""
    return not any(shutil.which(name) for name in ("cc", "gcc", "clang"))


def _skip_reason() -> str | None:
    """``None`` when every precondition holds; otherwise the ONE reason this
    run cannot proceed, checked cheapest/most-decisive first so the message
    is not just "something is missing" on a host missing everything."""
    if sys.platform == "win32":
        return (
            "native_sim has no native-Windows target "
            "(docs/cross-platform-setup.md §4.6) -- run under WSL2/Linux/macOS"
        )
    if SDK is None:
        return "no alp-sdk checkout resolvable (set ALP_SDK_ROOT)"
    zephyr_base = _zephyr_base()
    if zephyr_base is None:
        return "no ZEPHYR_BASE Zephyr workspace resolved"
    if _find_workspace_venv(str(zephyr_base), str(SDK)) is None:
        return "no west-capable .venv resolved near ZEPHYR_BASE/the SDK checkout"
    if _host_c_compiler_missing():
        return "no host C compiler (cc/gcc/clang) on PATH for native_sim's host toolchain"
    return None


def _run_tan(*argv: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
        timeout=120,
    )


def _envelope(proc: subprocess.CompletedProcess) -> dict:
    assert proc.stdout.strip(), f"no envelope on stdout; stderr:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_scaffold_then_native_sim_build_produces_a_real_zephyr_exe(tmp_path):
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    sdk = SDK
    assert sdk is not None  # narrowed by _skip_reason() above
    zephyr_base = _zephyr_base()
    venv = _find_workspace_venv(str(zephyr_base), str(sdk))
    assert venv is not None  # narrowed by _skip_reason() above
    layout = venv_layout(os.name == "nt")
    venv_bin = venv / layout.bin_dir
    west = venv_bin / layout.west
    assert west.is_file(), f"west resolved to a venv with no west executable: {west}"
    python_bin = venv_bin / layout.python
    assert python_bin.is_file(), f"west's venv has no python executable: {python_bin}"

    project = tmp_path / "proj"
    project.mkdir()

    init_proc = _run_tan(
        "init", "--template", "zephyr-app", "--format", "json", cwd=project
    )
    init_env = _envelope(init_proc)
    assert init_proc.returncode == 0, init_env
    assert init_env["ok"] is True, init_env
    assert (project / "board.yaml").is_file()
    assert (project / "CMakeLists.txt").is_file()

    # No `--output`: exercises `generate_cmd`'s own default destination for
    # this target (`boards/native_sim_native_64.overlay`, relative to the
    # `--board-yaml`'s directory) instead of re-hardcoding it here.
    gen_proc = _run_tan(
        "generate",
        "--target", "native-sim-overlay",
        "--board-yaml", str(project / "board.yaml"),
        "--sdk-root", str(sdk),
        "--format", "json",
        cwd=project,
    )
    gen_env = _envelope(gen_proc)
    assert gen_proc.returncode == 0, gen_env
    assert gen_env["ok"] is True, gen_env
    overlay = project / "boards" / "native_sim_native_64.overlay"
    assert overlay.is_file()

    build_dir = project / "build"
    # `envAppendPath`-style append+de-dup for EXTRA_ZEPHYR_MODULES (the
    # build-plan CONSUMER contract's own rule -- ``tan.core.plan_exec``),
    # not an overwrite: a real workspace's inherited EXTRA_ZEPHYR_MODULES
    # must survive, not get clobbered by the SDK checkout alone. The venv's
    # own bin dir goes on FRONT of PATH so `west` resolves ITS `python`
    # (with `west` importable) ahead of whatever bare `python`/`python3` is
    # already on PATH -- the same reason `-DPython3_EXECUTABLE` below is
    # passed explicitly rather than left to CMake's own `find_package`.
    #
    # Built from `REAL_ENVIRON` (captured at collection time in
    # `tests/conftest.py`), NOT `os.environ` read here -- by this point in
    # the test body, the autouse `_scrub_sdk_discovery_env` fixture has
    # already repointed `HOME`/`USERPROFILE` at a pytest tmp dir, and a real
    # `west build` needs the developer's genuine environment (toolchain/SDK
    # lookups keyed off the real `HOME`) to behave the way it would outside
    # the test.
    env_pairs = list(REAL_ENVIRON.items())
    apply_env_append(env_pairs, {"EXTRA_ZEPHYR_MODULES": [str(sdk)]})
    west_env = dict(env_pairs)
    west_env["ZEPHYR_BASE"] = str(zephyr_base)
    west_env["ALP_SDK_ROOT"] = str(sdk)
    west_env["PATH"] = os.pathsep.join([str(venv_bin), west_env.get("PATH", "")])
    west_proc = subprocess.run(
        [
            str(west), "build", "-p", "always",
            "-b", "native_sim/native/64",
            str(project), "-d", str(build_dir),
            "--", f"-DPython3_EXECUTABLE={python_bin}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=west_env,
        timeout=900,
    )
    assert west_proc.returncode == 0, (
        f"west build failed (rc={west_proc.returncode})\n"
        f"stdout:\n{west_proc.stdout}\nstderr:\n{west_proc.stderr}"
    )

    exe = build_dir / "zephyr" / "zephyr.exe"
    elf = build_dir / "zephyr" / "zephyr.elf"
    assert elf.is_file(), f"no zephyr.elf produced under {build_dir}"
    assert exe.is_file(), f"no zephyr.exe produced under {build_dir}"
    assert exe.stat().st_size > 0

    # The generated Kconfig fragment landed in `generated/`, not the build
    # root -- the one convention this whole path exists to respect (see the
    # module docstring / `cmake/alp.cmake`).
    assert (build_dir / "generated" / "alp.conf").is_file()
