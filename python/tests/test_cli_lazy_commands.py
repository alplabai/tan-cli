# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1505: `tan.cli` must not import every command module up front.

Each case runs in a FRESH interpreter (`sys.modules` of this pytest process is
polluted by every other test) and reports which `tan.commands.*` modules the
invocation pulled in.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tan.command_table import COMMAND_TABLE

_PROBE = """
import json, sys
sys.argv = ["tan", *json.loads(sys.argv[1])]
from tan.cli import main
try:
    main()
except SystemExit:
    pass
print(json.dumps(sorted(m for m in sys.modules if m.startswith("tan.commands."))))
"""


def _loaded_after(argv: list[str]) -> set[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(argv)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


def test_importing_tan_cli_loads_no_command_module():
    assert _loaded_after([]) - {"tan.commands"} == set()


def test_version_loads_no_command_module():
    assert _loaded_after(["--version"]) - {"tan.commands"} == set()


@pytest.mark.parametrize("spec", COMMAND_TABLE, ids=lambda s: s.name)
def test_command_help_loads_its_own_module_and_not_the_rest(spec):
    """Some commands legitimately import siblings (`bootstrap` -> `doctor`), so
    the bound is "well under the full set", not "exactly one"."""
    loaded = _loaded_after([spec.name, "--help"]) - {"tan.commands"}
    own = _modules_imported_by_loader(spec.name)
    others = _modules_imported_by_all_loaders_except(spec.name)
    assert own <= loaded, f"{spec.name}: its own module did not load"
    assert len(loaded & others) <= len(others) // 2, sorted(loaded & others)


def test_size_help_skips_the_heavy_modules():
    loaded = _loaded_after(["size", "--help"])
    assert not loaded & {
        "tan.commands.bootstrap_cmd",
        "tan.commands.doctor_cmd",
        "tan.commands.model_cmd",
        "tan.commands.sdk_cmd",
        "tan.commands.build_cmd",
        "tan.commands.flash_cmd",
    }


def _loader_modules() -> dict[str, set[str]]:
    src = Path(__file__).resolve().parents[1] / "tan" / "command_table.py"
    out: dict[str, set[str]] = {}
    for spec in COMMAND_TABLE:
        fn = "_load_" + spec.name.replace("-", "_")
        body = re.search(rf"def {fn}\(\).*?(?=\n\ndef |\n\nCOMMAND_TABLE)", src.read_text(), re.S)
        assert body, fn
        out[spec.name] = set(re.findall(r"from (tan\.commands\.\w+) import", body.group(0)))
    return out


def _modules_imported_by_loader(name: str) -> set[str]:
    return _loader_modules()[name]


def _modules_imported_by_all_loaders_except(name: str) -> set[str]:
    own = _loader_modules()[name]
    return {m for n, mods in _loader_modules().items() if n != name for m in mods} - own


def test_every_loader_is_a_static_function_local_import():
    """PyInstaller sees only static imports: each loader must name its module."""
    mods = _loader_modules()
    assert all(mods[spec.name] for spec in COMMAND_TABLE)


def test_lazy_and_eager_trees_agree():
    from typer.main import get_command

    from tan.cli import app

    lazy = app.lazy_group()
    eager = get_command(app)
    assert list(lazy.commands) == list(eager.commands)
    assert [c.name for c in lazy.commands.values()] == [c.name for c in eager.commands.values()]
