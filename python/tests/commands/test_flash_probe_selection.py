# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1312: `tan flash --probe-serial` / `--probe-usb-path` on Flow D.

No hardware and no real USB: enumeration is an injected list (or the real
enumerator over a tmp sysfs tree), every spawn is stubbed. The property under
test is that a write never reaches a probe the operator did not unambiguously
select, that the selection is verified against the JLinkExe that will write,
and that it is echoed."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tan.commands import flash_cmd
from tan.core import jlink_probe
from tan.core.jlink_probe import JLinkProbe
from tests.commands.test_flash_command import _flow_d_run

SHARED = "000603000869"
A = JLinkProbe("3-4.1", SHARED)
B = JLinkProbe("3-4.2", SHARED)
D = JLinkProbe("3-4.4.3", SHARED)
C = JLinkProbe("3-4.3", "000999000001")

_BASE = (
    '{jlink_flash_device: PART_PROFILE, slot0_load_address: "0x80010000", '
    'atoc: atoc.bin, atoc_address: "0x8057F5B0", confirm: true, '
    "atoc_unqueryable: true"
)
_NO_SERIAL_ARGS = _BASE + "}"
_SERIAL_ARGS = _BASE + f", jlink_serial: '{SHARED}'}}"
#: Unquoted, exactly as an operator writes it: PyYAML makes it the int 603000869.
_INT_SERIAL_ARGS = _BASE + ", jlink_serial: 000603000869}"


def _probes(*probes):
    return {"enumerate_probes": lambda: list(probes)}


class _Jlink:
    """A stand-in for the JLinkExe on PATH: answers `ShowEmuList` with the
    emulators IT can see (a masking wrapper shows one; plain JLinkExe shows
    all), and records every other script as a write."""

    def __init__(self, monkeypatch, emulators, *, handshake=None, outcome=None):
        self.emulators = emulators
        self.handshake = handshake
        self.outcome = outcome  # a canned _Outcome for the listing, if any
        self.writes: list[str] = []
        self.listings = 0
        self.envs: list[dict | None] = []
        monkeypatch.setattr(flash_cmd, "_spawn_jlink", self._spawn)

    def _spawn(self, argv, script, *args, **kwargs):
        self.envs.append(kwargs.get("extra_env"))
        if "ShowEmuList" in script:
            self.listings += 1
            if self.outcome is not None:
                return self.outcome
            lines = [
                f"J-Link[{i}]: Connection: USB, Serial number: {sn}, ProductName: J-Link"
                for i, sn in enumerate(self.emulators)
            ]
            if self.handshake:
                lines.insert(0, f"TAN_PROBE_ISOLATED_USB_PATH={self.handshake}")
            return flash_cmd._Outcome(success=True, stdout="\n".join(lines), returncode=0)
        self.writes.append(script)
        return flash_cmd._Outcome(success=True, returncode=0)


def _codes(issues):
    return [i.code for i in issues]


def test_usb_path_with_unique_serial_overrides_flash_args_verifies_and_echoes(
    tmp_path, monkeypatch
):
    jl = _Jlink(monkeypatch, ["000999000001"])
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
        "sysfsRoot": "injected", "isolation": "verified-single-emulator",
    }
    assert jl.writes and jl.writes[0].startswith("SelectEmuBySN 000999000001\n")
    assert SHARED not in jl.writes[0]
    # Verified before EACH JLinkExe spawn that is not itself the verification
    # (the early gate and the write; the preflight is stubbed out here), and
    # the resolved USB path is exported to every one of them.
    assert jl.listings == 2
    assert all(env == {"TAN_PROBE_USB_PATH": "3-4.3"} for env in jl.envs)


def test_probe_serial_overrides_flash_args_serial(tmp_path, monkeypatch):
    jl = _Jlink(monkeypatch, ["000999000001"])
    rc, data, _i, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, flash_args=_SERIAL_ARGS,
        probe_kwargs={**_probes(A, C), "probe_serial": "999000001"},
    )
    assert rc == 0
    assert data["entries"][0]["probe"]["source"] == "cli-serial"
    assert jl.writes[0].startswith("SelectEmuBySN 000999000001\n")


@pytest.mark.parametrize(
    ("args", "cli_serial"),
    [
        (_NO_SERIAL_ARGS, SHARED),
        (_NO_SERIAL_ARGS, "603000869"),
        (_SERIAL_ARGS, None),
        (_INT_SERIAL_ARGS, None),  # the manifest int 603000869 vs 3x 000603000869
    ],
)
def test_shared_serial_refuses_before_any_spawn_in_every_spelling(
    tmp_path, monkeypatch, args, cli_serial
):
    jl = _Jlink(monkeypatch, [SHARED])
    rc, data, issues, lines, _s = _flow_d_run(
        tmp_path, monkeypatch, flash_args=args,
        probe_kwargs={**_probes(A, B, D, C), "probe_serial": cli_serial},
    )
    assert rc == 1
    assert jl.writes == [] and jl.listings == 0
    entry = data["entries"][0]
    assert entry["status"] == "failed"
    assert _codes(issues) == ["flash.probe-ambiguous"]
    assert "3-4.1" in entry["message"] and "3-4.4.3" in entry["message"]
    assert "expect_dpidr" in entry["message"]
    assert entry["probe"]["candidates"] == ["3-4.1", "3-4.2", "3-4.4.3"]
    assert any("FAIL" in line for line in lines)


def test_zero_stripped_serial_and_usb_path_name_the_same_probe(tmp_path, monkeypatch):
    """Bench (d): the pair proceeds to verification -- ambiguous under plain
    JLinkExe, ok under a masking wrapper, whose ShowEmuList prints the
    zero-stripped serial. SelectEmuBySN gets the sysfs spelling (the one the
    bench's wrapper has always passed to J-Link)."""
    _Jlink(monkeypatch, [SHARED, SHARED, SHARED])
    kwargs = {"probe_usb_path": "3-4.2", "probe_serial": "603000869"}
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, probe_kwargs={**_probes(A, B, D), **kwargs},
    )
    assert rc == 1 and _codes(issues) == ["flash.probe-ambiguous"]
    jl = _Jlink(monkeypatch, ["603000869"], handshake="3-4.2")
    rc, _d, _i, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, probe_kwargs={**_probes(A, B, D), **kwargs},
    )
    assert rc == 0
    assert jl.writes[0].startswith("SelectEmuBySN 000603000869\n")


def test_quoted_zero_stripped_manifest_serial_is_ambiguous_not_passed_through(
    tmp_path, monkeypatch
):
    """Bench (f): `jlink_serial: '603000869'` with 3x 000603000869 visible."""
    jl = _Jlink(monkeypatch, [SHARED])
    args = _BASE + ", jlink_serial: '603000869'}"
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, flash_args=args, probe_kwargs=_probes(A, B, D),
    )
    assert rc == 1 and jl.writes == [] and _codes(issues) == ["flash.probe-ambiguous"]


def test_zero_stripped_probe_serial_on_the_cli_is_ambiguous_not_not_found(
    tmp_path, monkeypatch
):
    """Bench (e)."""
    _Jlink(monkeypatch, [SHARED])
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch,
        probe_kwargs={**_probes(A, B, D), "probe_serial": "603000869"},
    )
    assert rc == 1 and _codes(issues) == ["flash.probe-ambiguous"]


def test_usb_path_on_a_shared_serial_runs_under_a_masking_wrapper(tmp_path, monkeypatch):
    """The JLinkExe on PATH is a wrapper that masks the other probes, so its
    ShowEmuList shows exactly one: verification passes and the run proceeds."""
    jl = _Jlink(monkeypatch, [SHARED], handshake="3-4.2")
    rc, data, _i, _l, _s = _flow_d_run(
        tmp_path, monkeypatch,
        probe_kwargs={**_probes(A, B, D), "probe_usb_path": "3-4.2"},
    )
    assert rc == 0
    assert jl.writes[0].startswith(f"SelectEmuBySN {SHARED}\n")
    probe = data["entries"][0]["probe"]
    assert probe["candidates"] == ["3-4.1", "3-4.2", "3-4.4.3"]
    assert probe["isolation"] == "wrapper-attested:3-4.2"


def test_usb_path_on_a_shared_serial_refuses_under_plain_jlinkexe(tmp_path, monkeypatch):
    """Plain JLinkExe sees all three same-serial probes: tan cannot address the
    one at the path, and refuses before the write (and before the sign)."""
    jl = _Jlink(monkeypatch, [SHARED, SHARED, SHARED])
    rc, data, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch,
        probe_kwargs={**_probes(A, B, D), "probe_usb_path": "3-4.2"},
    )
    assert rc == 1
    assert jl.writes == []
    assert _codes(issues) == ["flash.probe-ambiguous"]
    assert "3 emulators" in data["entries"][0]["message"]
    assert "isolation" not in data["entries"][0]["probe"]


def test_verification_refuses_when_jlinkexe_does_not_list_the_selected_serial(
    tmp_path, monkeypatch
):
    jl = _Jlink(monkeypatch, [])
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, probe_kwargs={**_probes(A, C), "probe_usb_path": "3-4.3"},
    )
    assert rc == 1 and jl.writes == [] and _codes(issues) == ["flash.probe-verify-failed"]


def test_a_probe_set_that_changes_between_selection_and_spawn_refuses(tmp_path, monkeypatch):
    jl = _Jlink(monkeypatch, ["000999000001"])
    answers = [[A, C], [A, C, B]]  # a probe appears after selection

    def enumerate_probes():
        return list(answers.pop(0)) if len(answers) > 1 else list(answers[0])

    rc, data, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch,
        probe_kwargs={"enumerate_probes": enumerate_probes, "probe_usb_path": "3-4.3"},
    )
    assert rc == 1 and jl.writes == []
    assert _codes(issues) == ["flash.probe-verify-failed"]
    assert "changed between probe selection and the spawn" in data["entries"][0]["message"]


def test_several_probes_and_no_selector_refuses(tmp_path, monkeypatch):
    jl = _Jlink(monkeypatch, [SHARED])
    rc, _d, issues, _l, _s = _flow_d_run(tmp_path, monkeypatch, probe_kwargs=_probes(A, C))
    assert rc == 1 and jl.writes == []
    assert _codes(issues) == ["flash.probe-ambiguous"]


def test_a_sole_visible_probe_is_pinned_by_serial(tmp_path, monkeypatch):
    jl = _Jlink(monkeypatch, [SHARED])
    rc, data, _i, _l, _s = _flow_d_run(tmp_path, monkeypatch, probe_kwargs=_probes(B))
    assert rc == 0
    assert jl.writes[0].startswith(f"SelectEmuBySN {SHARED}\n")
    assert data["entries"][0]["probe"]["source"] == "sole-visible"


def test_not_found_and_conflict_have_their_own_codes(tmp_path, monkeypatch):
    _Jlink(monkeypatch, [SHARED])
    cases = [
        ({"probe_usb_path": "9-9"}, _NO_SERIAL_ARGS),
        ({"probe_usb_path": "3-4.3", "probe_serial": SHARED}, _NO_SERIAL_ARGS),
        ({"probe_serial": "nope"}, _NO_SERIAL_ARGS),
    ]
    got = []
    for kwargs, args in cases:
        rc, _d, issues, _l, _s = _flow_d_run(
            tmp_path, monkeypatch, flash_args=args, probe_kwargs={**_probes(A, C), **kwargs},
        )
        assert rc == 1
        got.append(_codes(issues))
    assert got == [
        ["flash.probe-not-found"],
        ["flash.probe-selector-conflict"],
        ["flash.probe-not-found"],
    ]


def test_a_manifest_serial_no_visible_probe_carries_is_not_found(tmp_path, monkeypatch):
    jl = _Jlink(monkeypatch, [SHARED])
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, flash_args=_SERIAL_ARGS, probe_kwargs=_probes(C),
    )
    assert rc == 1 and jl.writes == [] and _codes(issues) == ["flash.probe-not-found"]


def test_dry_run_reports_the_would_be_selection_and_spawns_nothing(tmp_path, monkeypatch):
    jl = _Jlink(monkeypatch, [])
    rc, data, _i, lines, _s = _flow_d_run(
        tmp_path, monkeypatch, dry_run=True,
        probe_kwargs={**_probes(A, B), "probe_usb_path": "3-4.2"},
    )
    assert rc == 0 and jl.writes == [] and jl.listings == 0
    entry = data["entries"][0]
    assert entry["status"] == "ok"
    assert f"serial {SHARED} at USB path 3-4.2 via cli-usb-path" in entry["message"]
    assert "serial shared by 3-4.1, 3-4.2" in entry["message"]
    assert entry["probe"]["usbPath"] == "3-4.2"
    assert any("3-4.2" in line for line in lines)


def test_dry_run_still_refuses_an_ambiguous_selection(tmp_path, monkeypatch):
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, dry_run=True, probe_kwargs=_probes(A, B),
    )
    assert rc == 1 and _codes(issues) == ["flash.probe-ambiguous"]


def test_entry_shape_is_unchanged_without_selector_and_without_probes(tmp_path, monkeypatch):
    _Jlink(monkeypatch, [])
    rc, data, _i, _l, _s = _flow_d_run(tmp_path, monkeypatch, probe_kwargs=_probes())
    assert rc == 0
    assert "probe" not in data["entries"][0]


def test_a_host_that_cannot_enumerate_warns_and_still_verifies(tmp_path, monkeypatch):
    jl = _Jlink(monkeypatch, [SHARED])
    rc, data, issues, lines, _s = _flow_d_run(
        tmp_path, monkeypatch, flash_args=_SERIAL_ARGS,
        probe_kwargs={"enumerate_probes": lambda: None},
    )
    assert rc == 0 and jl.listings == 2
    warned = [i for i in issues if i.code == "flash.probe-unverified"]
    assert len(warned) == 1 and warned[0].severity == "warning"
    assert data["entries"][0]["probe"]["visibleProbes"] is None
    assert any("cannot enumerate USB" in line for line in lines)


def test_a_probe_selector_on_another_method_refuses_the_whole_run(tmp_path, monkeypatch):
    (tmp_path / "build").mkdir()
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        "schema_version: 1\nhw_info: {sku: S}\nslices:\n"
        "- {core_id: m55_he, os: zephyr, output_artefact: a.bin, status: ok,\n"
        "   flash_method: zephyr_west_flash, flash_args: {}}\n"
        "helper_mcus: []\nboot_order: []\n",
        encoding="utf-8",
    )
    rc, data, issues, _l, _s = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=None, helper=None, dry_run=True,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
        probe_usb_path="3-4.3",
    )
    assert rc == 1
    assert _codes(issues) == ["flash.probe-selector-unsupported"]
    assert data["entries"][0]["status"] == "failed"
    assert "zephyr_west_flash" in data["entries"][0]["message"]


# ── the REAL enumerator over a tmp sysfs tree ───────────────────────────────


def _tree(root, serials):
    for name, serial in serials.items():
        d = root / name
        d.mkdir()
        (d / "idVendor").write_text("1366\n")
        (d / "serial").write_text(serial + "\n")


def test_real_enumerator_on_three_cloned_serial_probes_refuses_an_int_manifest_serial(
    tmp_path, monkeypatch
):
    """Three probes sharing 000603000869 (3-4.1, 3-4.2, 3-4.4.3) read from a
    sysfs-shaped tree by the REAL enumerator, and a manifest serial the
    operator wrote unquoted."""
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    _tree(sysfs, {"3-4.1": SHARED, "3-4.2": SHARED, "3-4.4.3": SHARED})
    monkeypatch.setattr(flash_cmd, "enumerate_jlinks", lambda: jlink_probe.enumerate_jlinks(str(sysfs)))
    jl = _Jlink(monkeypatch, [SHARED])
    rc, data, issues, _l, _s = _flow_d_run(tmp_path, monkeypatch, flash_args=_INT_SERIAL_ARGS)
    assert rc == 1 and jl.writes == []
    assert _codes(issues) == ["flash.probe-ambiguous"]
    entry = data["entries"][0]
    assert entry["probe"]["candidates"] == ["3-4.1", "3-4.2", "3-4.4.3"]
    assert entry["probe"]["sysfsRoot"] == "/sys/bus/usb/devices"


def test_real_enumerator_refuses_a_populated_unreadable_device_directory(tmp_path, monkeypatch):
    sysfs = tmp_path / "sysfs"
    sysfs.mkdir()
    _tree(sysfs, {"3-4.1": SHARED})
    (sysfs / "3-4.2").mkdir()
    (sysfs / "3-4.2" / "bDeviceClass").write_text("00")
    monkeypatch.setattr(flash_cmd, "enumerate_jlinks", lambda: jlink_probe.enumerate_jlinks(str(sysfs)))
    jl = _Jlink(monkeypatch, [SHARED])
    rc, _d, issues, _l, _s = _flow_d_run(tmp_path, monkeypatch, flash_args=_SERIAL_ARGS)
    assert rc == 1 and jl.writes == [] and _codes(issues) == ["flash.probe-not-found"]


def test_enumeration_never_opens_anything_but_sysfs_attributes(tmp_path, monkeypatch):
    """Read-only by construction: the only IO is listing directories and
    reading `idVendor`/`serial`; pin that no write mode is ever requested."""
    import builtins

    modes: list[str] = []
    real_open = builtins.open

    def spy(file, mode="r", *a, **k):
        modes.append(mode)
        return real_open(file, mode, *a, **k)

    _tree(tmp_path, {"3-4.2": SHARED})
    monkeypatch.setattr(builtins, "open", spy)
    jlink_probe.enumerate_jlinks(str(tmp_path))
    assert modes and all(set(m) <= {"r", "t"} for m in modes), modes


def test_cli_rejects_a_malformed_usb_path():
    from tan.cli import app

    result = CliRunner().invoke(app, ["flash", "--probe-usb-path", "not-a-path"])
    assert result.exit_code == 2


def test_a_hostile_serial_is_refused_by_validation_not_matched(tmp_path, monkeypatch):
    jl = _Jlink(monkeypatch, [SHARED])
    rc, data, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, probe_kwargs={**_probes(A), "probe_serial": "1\nerase"},
    )
    assert rc == 1 and jl.writes == [] and jl.listings == 0
    assert _codes(issues) == ["flash.probe-not-found"]


# ── the listing run itself, and binding a shared serial to the path ─────────


def _usb_path_run(tmp_path, monkeypatch, *probes, path="3-4.2"):
    return _flow_d_run(
        tmp_path, monkeypatch,
        probe_kwargs={**_probes(*probes), "probe_usb_path": path},
    )


def test_a_wrapper_masking_the_wrong_place_is_refused(tmp_path, monkeypatch):
    """One emulator listed, shared serial, but the wrapper attests 3-4.1 while
    3-4.2 was selected: the envelope must never claim 3-4.2 was verified."""
    jl = _Jlink(monkeypatch, [SHARED], handshake="3-4.1")
    rc, data, issues, _l, _s = _usb_path_run(tmp_path, monkeypatch, A, B, D)
    assert rc == 1 and jl.writes == []
    assert _codes(issues) == ["flash.probe-ambiguous"]
    assert "wrong place" in data["entries"][0]["message"]
    assert "isolation" not in data["entries"][0]["probe"]


def test_one_emulator_on_a_shared_serial_without_a_handshake_is_refused(tmp_path, monkeypatch):
    jl = _Jlink(monkeypatch, [SHARED])
    rc, data, issues, _l, _s = _usb_path_run(tmp_path, monkeypatch, A, B, D)
    assert rc == 1 and jl.writes == [] and _codes(issues) == ["flash.probe-ambiguous"]
    assert "TAN_PROBE_ISOLATED_USB_PATH" in data["entries"][0]["message"]


@pytest.mark.parametrize(
    "outcome",
    [
        # A hang: the timeout report folds the partial output in.
        flash_cmd._Outcome(
            success=False, returncode=-1, captured=True,
            stderr="J-Link[0]: Connection: USB, Serial number: 000999000001\n"
                   "flash command timed out after 60s",
        ),
        # A crash after printing one serial line.
        flash_cmd._Outcome(
            success=False, returncode=139, captured=True,
            stdout="J-Link[0]: Connection: USB, Serial number: 000999000001, ProductName: J",
        ),
        # rc != 0 although `success` was somehow reported.
        flash_cmd._Outcome(
            success=True, returncode=3, captured=True,
            stdout="J-Link[0]: Connection: USB, Serial number: 000999000001, ProductName: J",
        ),
        # Nothing at all.
        flash_cmd._Outcome(success=True, returncode=0, captured=True, stdout=""),
    ],
    ids=["timeout", "crash", "nonzero-rc", "empty"],
)
def test_an_incomplete_listing_run_refuses_with_verify_failed(tmp_path, monkeypatch, outcome):
    jl = _Jlink(monkeypatch, [], outcome=outcome)
    rc, _d, issues, _l, _s = _usb_path_run(tmp_path, monkeypatch, A, C, path="3-4.3")
    assert rc == 1 and jl.writes == []
    assert _codes(issues) == ["flash.probe-verify-failed"]


def test_an_unpinned_run_on_a_host_that_cannot_enumerate_wants_one_emulator(
    tmp_path, monkeypatch
):
    jl = _Jlink(monkeypatch, [SHARED, "000999000001"])
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, probe_kwargs={"enumerate_probes": lambda: None},
    )
    assert rc == 1 and jl.writes == [] and "flash.probe-ambiguous" in _codes(issues)
    jl = _Jlink(monkeypatch, [SHARED])
    rc, data, _i, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, probe_kwargs={"enumerate_probes": lambda: None},
    )
    assert rc == 0 and len(jl.writes) == 1
    assert data["entries"][0]["probe"]["isolation"] == "verified-single-emulator"


def test_every_jlink_script_disables_the_firmware_updater_and_listing_closes_stdin(
    tmp_path, monkeypatch
):
    seen: list[tuple[str, dict]] = []

    def fake_spawn(argv, *args, **kwargs):
        seen.append((Path(argv[-1]).read_text(encoding="utf-8"), kwargs))
        return flash_cmd._Outcome(success=True, returncode=0)

    monkeypatch.setattr(flash_cmd, "_spawn", fake_spawn)
    flash_cmd._spawn_jlink(["JLinkExe"], "ShowEmuList\nexit\n", True, 5, no_stdin=True)
    flash_cmd._spawn_jlink(["JLinkExe"], "SelectEmuBySN 1\nexit\n", True, 5)
    assert all(text.startswith("exec DisableAutoUpdateFW\n") for text, _ in seen)
    assert seen[0][1] == {"no_stdin": True} and seen[1][1] == {}


def test_the_stdin_of_the_listing_spawn_is_devnull(monkeypatch):
    import subprocess

    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    flash_cmd._spawn(["x"], True, 5, no_stdin=True)
    assert captured["stdin"] is subprocess.DEVNULL
    captured.clear()
    flash_cmd._spawn(["x"], True, 5)
    assert captured["stdin"] is None


def test_dry_run_on_a_shared_serial_warns_that_a_real_run_needs_isolation(
    tmp_path, monkeypatch
):
    jl = _Jlink(monkeypatch, [])
    rc, data, issues, lines, _s = _flow_d_run(
        tmp_path, monkeypatch, dry_run=True,
        probe_kwargs={**_probes(A, B, D), "probe_usb_path": "3-4.2"},
    )
    assert rc == 0 and jl.listings == 0 and jl.writes == []  # a preview spawns nothing
    assert _codes(issues) == ["flash.probe-isolation-required"]
    assert issues[0].severity == "warning"
    assert "TAN_PROBE_ISOLATED_USB_PATH=3-4.2" in issues[0].message
    assert any("does not run that check" in line for line in lines)
    # A unique serial has nothing to warn about.
    rc, _d, issues, _l, _s = _flow_d_run(
        tmp_path, monkeypatch, dry_run=True,
        probe_kwargs={**_probes(A, C), "probe_usb_path": "3-4.3"},
    )
    assert rc == 0 and issues == []


def test_the_preflight_reuses_a_verification_nothing_ran_since():
    listings = []

    def fake(argv, script, *a, **k):
        listings.append(script)
        return flash_cmd._Outcome(
            success=True, returncode=0,
            stdout=f"J-Link[0]: Connection: USB, Serial number: {C.serial}",
        )

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(flash_cmd, "_spawn_jlink", fake)
    mp.setattr(flash_cmd, "resolve_program_positions", lambda spawned, env, *a: (spawned, None))
    try:
        sel = jlink_probe.resolve_probe_selection(
            [A, C], cli_serial=None, cli_usb_path="3-4.3", manifest_serial=None
        )
        guard = flash_cmd._ProbeGuard(sel, jlink_probe.probe_set([A, C]), lambda: [A, C], {})
        assert flash_cmd._probe_guard_refusal(guard, "JLinkExe", None, None) is None
        assert len(listings) == 1
        guard.fresh = True  # what the pre-sign gate leaves behind
        assert flash_cmd._probe_guard_refusal(
            guard, "JLinkExe", None, None, reuse_fresh=True) is None
        assert len(listings) == 1 and guard.fresh is False  # skipped, and consumed
        # The write guard never reuses.
        guard.fresh = True
        assert flash_cmd._probe_guard_refusal(guard, "JLinkExe", None, None) is None
        assert len(listings) == 2
    finally:
        mp.undo()
