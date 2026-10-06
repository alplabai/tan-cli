# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1312: `tan flash --probe-serial` / `--probe-usb-path` on Flow D.

No hardware and no real USB: enumeration is an injected list, every spawn is
stubbed. The property under test is that a write never reaches a probe the
operator did not unambiguously select, and that the selection is echoed."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tan.commands import flash_cmd
from tan.core.jlink_probe import JLinkProbe
from tests.commands.test_flash_command import _flow_d_run

SHARED = "000603000869"
A = JLinkProbe("3-4.1", SHARED)
B = JLinkProbe("3-4.2", SHARED)
C = JLinkProbe("3-4.3", "000999000001")

_NO_SERIAL_ARGS = (
    '{jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000", '
    'atoc: atoc.bin, atoc_address: "0x8057F5B0", confirm: true, '
    "atoc_unqueryable: true}"
)
_SERIAL_ARGS = _NO_SERIAL_ARGS[:-1] + f", jlink_serial: '{SHARED}'}}"


def _probes(*probes):
    return {"enumerate_probes": lambda: list(probes)}


def _capture_scripts(monkeypatch):
    scripts: list[str] = []

    def fake(argv, script, *a, **k):
        scripts.append(script)
        return flash_cmd._Outcome(success=True, stdout="", stderr="")

    monkeypatch.setattr(flash_cmd, "_spawn_jlink", fake)
    return scripts


def _codes(issues):
    return [i.code for i in issues]


def test_usb_path_with_unique_serial_overrides_flash_args_and_is_echoed(tmp_path, monkeypatch):
    scripts = _capture_scripts(monkeypatch)
    rc, data, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, flash_args=_SERIAL_ARGS,
        probe_kwargs={**_probes(A, C), "probe_usb_path": "3-4.3"},
    )
    assert rc == 0, (data, issues)
    entry = data["entries"][0]
    assert entry["status"] == "ok"
    assert entry["probe"] == {
        "source": "cli-usb-path", "serial": "000999000001", "usbPath": "3-4.3",
        "visibleProbes": 2, "overridesFlashArgsSerial": SHARED,
    }
    assert scripts and scripts[0].startswith("SelectEmuBySN 000999000001\n")
    assert SHARED not in scripts[0]


def test_probe_serial_overrides_flash_args_serial(tmp_path, monkeypatch):
    scripts = _capture_scripts(monkeypatch)
    rc, data, _i, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, flash_args=_SERIAL_ARGS,
        probe_kwargs={**_probes(A, C), "probe_serial": "000999000001"},
    )
    assert rc == 0
    assert data["entries"][0]["probe"]["source"] == "cli-serial"
    assert scripts[0].startswith("SelectEmuBySN 000999000001\n")


@pytest.mark.parametrize("args", [_NO_SERIAL_ARGS, _SERIAL_ARGS])
def test_shared_serial_refuses_before_any_spawn(tmp_path, monkeypatch, args):
    spawned: list = []
    rc, data, issues, lines, _s = _flow_d_run(
        tmp_path, monkeypatch, flash_args=args, spawned=spawned,
        probe_kwargs={**_probes(A, B, C), "probe_serial": SHARED},
    )
    assert rc == 1
    assert spawned == []
    entry = data["entries"][0]
    assert entry["status"] == "failed"
    assert _codes(issues) == ["flash.probe-ambiguous"]
    assert "3-4.1" in entry["message"] and "3-4.2" in entry["message"]
    assert entry["probe"]["candidates"] == ["3-4.1", "3-4.2"]
    assert any("FAIL" in line for line in lines)


def test_usb_path_on_shared_serial_refuses_and_names_the_collision(tmp_path, monkeypatch):
    spawned: list = []
    rc, data, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, spawned=spawned,
        probe_kwargs={**_probes(A, B), "probe_usb_path": "3-4.2"},
    )
    assert rc == 1 and spawned == []
    assert _codes(issues) == ["flash.probe-ambiguous"]
    assert "shared by 2 visible probes" in data["entries"][0]["message"]


def test_usb_path_on_shared_serial_runs_when_only_the_target_is_visible(tmp_path, monkeypatch):
    """Inside a masking wrapper's namespace sysfs shows ONE probe: the shared
    serial is then unambiguous and the run proceeds."""
    scripts = _capture_scripts(monkeypatch)
    rc, data, _i, _l, _s = _flow_d_run(
        tmp_path, monkeypatch,
        probe_kwargs={**_probes(B), "probe_usb_path": "3-4.2"},
    )
    assert rc == 0
    assert scripts[0].startswith(f"SelectEmuBySN {SHARED}\n")
    assert data["entries"][0]["probe"]["visibleProbes"] == 1


def test_isolation_env_relaxes_only_the_named_path_case(tmp_path, monkeypatch):
    monkeypatch.setenv("TAN_PROBE_ISOLATED", "1")
    scripts = _capture_scripts(monkeypatch)
    rc, data, _i, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, probe_kwargs={**_probes(A, B), "probe_usb_path": "3-4.2"},
    )
    assert rc == 0
    assert data["entries"][0]["probe"]["isolationAsserted"] is True
    assert scripts[0].startswith(f"SelectEmuBySN {SHARED}\n")
    # A bare multi-probe run (no selector) still refuses.
    rc2, _d, issues2, _l2, _s2 = _flow_d_run(tmp_path, monkeypatch, probe_kwargs=_probes(A, C))
    assert rc2 == 1 and _codes(issues2) == ["flash.probe-ambiguous"]


def test_several_probes_and_no_selector_refuses(tmp_path, monkeypatch):
    spawned: list = []
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, spawned=spawned, probe_kwargs=_probes(A, C),
    )
    assert rc == 1 and spawned == []
    assert _codes(issues) == ["flash.probe-ambiguous"]


def test_not_found_and_conflict_have_their_own_codes(tmp_path, monkeypatch):
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, probe_kwargs={**_probes(A, C), "probe_usb_path": "9-9"},
    )
    assert rc == 1 and _codes(issues) == ["flash.probe-not-found"]
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch,
        probe_kwargs={**_probes(A, C), "probe_usb_path": "3-4.3", "probe_serial": SHARED},
    )
    assert rc == 1 and _codes(issues) == ["flash.probe-selector-conflict"]


def test_dry_run_reports_the_would_be_selection_and_spawns_nothing(tmp_path, monkeypatch):
    spawned: list = []
    rc, data, _i, lines, _s = _flow_d_run(
        tmp_path, monkeypatch, dry_run=True, spawned=spawned,
        probe_kwargs={**_probes(A, C), "probe_usb_path": "3-4.3"},
    )
    assert rc == 0 and spawned == []
    entry = data["entries"][0]
    assert entry["status"] == "ok"
    assert "serial 000999000001 at USB path 3-4.3 via cli-usb-path" in entry["message"]
    assert entry["probe"]["usbPath"] == "3-4.3"
    assert any("3-4.3" in line for line in lines)


def test_dry_run_still_refuses_an_ambiguous_selection(tmp_path, monkeypatch):
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, dry_run=True, probe_kwargs=_probes(A, B),
    )
    assert rc == 1 and _codes(issues) == ["flash.probe-ambiguous"]


def test_entry_shape_is_unchanged_without_selector_and_without_probes(tmp_path, monkeypatch):
    rc, data, _i, _l, _s = _flow_d_run(tmp_path, monkeypatch, probe_kwargs=_probes())
    assert rc == 0
    assert "probe" not in data["entries"][0]


def test_enumeration_never_opens_anything_but_sysfs_attributes(tmp_path, monkeypatch):
    """Read-only by construction: the only IO is listing the directory and
    reading `idVendor`/`serial`; pin that no write mode is ever requested."""
    import builtins

    modes: list[str] = []
    real_open = builtins.open

    def spy(file, mode="r", *a, **k):
        modes.append(mode)
        return real_open(file, mode, *a, **k)

    (tmp_path / "3-4.2").mkdir()
    (tmp_path / "3-4.2" / "idVendor").write_text("1366")
    monkeypatch.setattr(builtins, "open", spy)
    from tan.core.jlink_probe import enumerate_jlinks

    enumerate_jlinks(str(tmp_path))
    assert modes and all(set(m) <= {"r", "t"} for m in modes), modes


def test_cli_rejects_a_malformed_usb_path():
    from tan.cli import app

    result = CliRunner().invoke(app, ["flash", "--probe-usb-path", "not-a-path"])
    assert result.exit_code == 2
