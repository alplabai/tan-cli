# SPDX-License-Identifier: Apache-2.0
"""`tan.core.setools` -- tan-cli#353's remaining half: SETOOLS `app-gen-toc`
integration for the AEN801 Flow D slot0 sign step.

Every subprocess spawn here drives a FAKE `app-gen-toc` -- a script this file
writes, never a real SETOOLS install (license-gated, not redistributed, and
not required to prove the wiring: see `tan/commands/doctor_cmd.py`'s own
`setools` check, which treats a real install as Linux-only). `.bat` on
Windows, a POSIX shebang script elsewhere -- picked because a batch-content
file with NO extension is not directly spawnable via `subprocess.run(...,
shell=False)` (measured: `WinError 193`), while a POSIX shebang script is
spawnable extension-less. `sign_slot0` itself takes an explicit
`app_gen_toc` path, so most tests never need `find_app_gen_toc`'s own
bare-name lookup at all; the one test that drives the FULL
`resolve -> find -> sign` path monkeypatches `APP_GEN_TOC` for the Windows
case only, so `find_app_gen_toc`'s real lookup logic still runs, just against
the one filename this host can actually execute.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tan.core.flash_plan import FlashPlanError
from tan.core import setools as setools_module
from tan.core.setools import (
    SetoolsSource,
    find_app_gen_toc,
    missing_tool_message,
    read_atoc_address,
    resolve_setools_dir,
    sign_slot0,
    slot0_config,
    unresolved_message,
)

#: The maintainer's own measured value (tan-cli#353) -- kept verbatim rather
#: than a made-up placeholder, so a fixture typo can never look plausible.
_REAL_ATOC_ADDRESS = "0x8057ea50"


# ── resolve_setools_dir ──────────────────────────────────────────────────────


def test_resolve_setools_dir_prefers_the_explicit_flash_args_value():
    resolved = resolve_setools_dir(
        {"setools_dir": "/from/manifest"}, {"SETOOLS_DIR": "/from/env"}
    )
    assert resolved == SetoolsSource("/from/manifest", "flash_args.setools_dir")


def test_resolve_setools_dir_falls_back_to_the_environment_variable():
    resolved = resolve_setools_dir({}, {"SETOOLS_DIR": "/from/env"})
    assert resolved == SetoolsSource("/from/env", "the SETOOLS_DIR environment variable")


def test_resolve_setools_dir_is_none_when_neither_is_set():
    assert resolve_setools_dir({}, {}) is None
    assert resolve_setools_dir({"setools_dir": ""}, {"SETOOLS_DIR": ""}) is None


def test_resolve_setools_dir_ignores_a_non_string_flash_args_value():
    """A malformed `flash_args` (e.g. the SDK's `TBD` placeholder, or a bare
    `setools_dir: true`) must fall through to the env var, not raise --
    `fa_str` already treats non-string as absent, and this is not a
    behaviour-affecting field worth a stricter accessor."""
    resolved = resolve_setools_dir({"setools_dir": True}, {"SETOOLS_DIR": "/from/env"})
    assert resolved == SetoolsSource("/from/env", "the SETOOLS_DIR environment variable")
    assert resolve_setools_dir("TBD", {}) is None


# ── find_app_gen_toc ─────────────────────────────────────────────────────────


def test_find_app_gen_toc_finds_the_bare_name(tmp_path):
    (tmp_path / setools_module.APP_GEN_TOC).write_text("", encoding="utf-8")
    found = find_app_gen_toc(str(tmp_path))
    assert found == str(tmp_path / setools_module.APP_GEN_TOC)


def test_find_app_gen_toc_is_none_when_absent(tmp_path):
    assert find_app_gen_toc(str(tmp_path)) is None


def test_find_app_gen_toc_is_none_for_a_hostile_path():
    """A NUL byte or similar must read as "not found", never raise -- this
    runs on customer-supplied paths (`flash_args.setools_dir` or an env
    var)."""
    assert find_app_gen_toc("bad\x00path") is None


# ── guidance messages -- remedy first, blame never ──────────────────────────


def test_unresolved_message_names_the_remedy():
    msg = unresolved_message()
    assert "SETOOLS" in msg
    assert "license-gated" in msg
    assert "app-gen-toc" in msg
    assert "SETOOLS_DIR=" in msg
    assert "flash_args.setools_dir" in msg
    # No blame: never says the customer did anything wrong.
    assert "you " not in msg.lower()


def test_missing_tool_message_names_the_source():
    source = SetoolsSource("/opt/bad-install", "flash_args.setools_dir")
    msg = missing_tool_message(source)
    assert "/opt/bad-install" in msg
    assert "flash_args.setools_dir" in msg
    assert "app-gen-toc" in msg


# ── slot0_config ─────────────────────────────────────────────────────────────


def test_slot0_config_matches_the_measured_bench_shape():
    """The exact shape the AEN801 bench flow signs by hand (tan-cli#353) --
    no top-level "DEVICE" key (see the module docstring: an app-only ATOC
    must not overwrite the on-module factory device config)."""
    config = slot0_config("m55_he", "m55_he.bin", "0x80010000", "M55_HE")
    assert config == {
        "m55_he": {
            "binary": "m55_he.bin",
            "version": "1.0.0",
            "mramAddress": "0x80010000",
            "cpu_id": "M55_HE",
            "flags": ["boot"],
            "signed": True,
        }
    }
    assert "DEVICE" not in config


# ── read_atoc_address ────────────────────────────────────────────────────────


def test_read_atoc_address_parses_a_real_report(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    (build / "app-package-map.txt").write_text(
        f"Device Algorithm Package\nAPP Package Start Address: {_REAL_ATOC_ADDRESS}\n",
        encoding="utf-8",
    )
    assert read_atoc_address(str(tmp_path)) == _REAL_ATOC_ADDRESS


def test_read_atoc_address_is_none_when_the_report_is_missing(tmp_path):
    assert read_atoc_address(str(tmp_path)) is None


# ── sign_slot0 -- the real (fake) app-gen-toc spawn ─────────────────────────


def _script_name() -> str:
    """`.bat` on Windows (needs the extension to be directly spawnable, see
    the module docstring), the real bare name elsewhere."""
    return "app-gen-toc.bat" if os.name == "nt" else setools_module.APP_GEN_TOC


def _write_fake_app_gen_toc(
    dest: Path,
    *,
    exit_code: int = 0,
    map_line: str | None = f"APP Package Start Address: {_REAL_ATOC_ADDRESS}",
    write_blob: bool = True,
    stderr_text: str = "",
) -> str:
    """A fake `app-gen-toc` at `dest`, genuinely spawnable on THIS host with
    no `shell=True` -- it writes `build/app-package-map.txt` (with or
    without the marker line) and `build/AppTocPackage.bin` under its OWN
    cwd (`sign_slot0` always spawns with `cwd=setools_dir`, matching the
    bench's own `cd $SETOOLS_DIR && ./app-gen-toc ...`), then exits
    `exit_code`. Proves the WIRING, not a real SETOOLS."""
    if os.name == "nt":
        lines = ["@echo off", "if not exist build mkdir build"]
        if map_line is not None:
            lines.append(f">build\\app-package-map.txt echo {map_line}")
        else:
            lines.append("type nul > build\\app-package-map.txt")
        if write_blob:
            lines.append("echo fake-atoc-bytes> build\\AppTocPackage.bin")
        if stderr_text:
            lines.append(f"echo {stderr_text} 1>&2")
        lines.append(f"exit /b {exit_code}")
        dest.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    else:
        lines = ["#!/bin/sh", "mkdir -p build"]
        if map_line is not None:
            lines.append(f'printf "%s\\n" "{map_line}" > build/app-package-map.txt')
        else:
            lines.append(": > build/app-package-map.txt")
        if write_blob:
            lines.append('printf "fake-atoc-bytes\\n" > build/AppTocPackage.bin')
        if stderr_text:
            lines.append(f'echo "{stderr_text}" >&2')
        lines.append(f"exit {exit_code}")
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(dest, 0o755)
    return str(dest)


def _artefact_bin(tmp_path: Path) -> Path:
    artefact = tmp_path / "zephyr.bin"
    artefact.write_bytes(b"fake-app-image-bytes")
    return artefact


def test_sign_slot0_copies_writes_and_derives_the_address(tmp_path):
    """The end-to-end happy path: copy the raw `.bin` into
    `build/images/<id>.bin`, write `build/config/<id>-slot0.json`, run
    `app-gen-toc`, and return the derived `(atoc_path, atoc_address)` --
    tan-cli#353's requirement (a)."""
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    script = setools_dir / _script_name()
    _write_fake_app_gen_toc(script)
    artefact = _artefact_bin(tmp_path)

    atoc_path, address = sign_slot0(
        str(setools_dir), str(script), str(artefact), "m55_he", "0x80010000"
    )

    assert address == _REAL_ATOC_ADDRESS
    assert Path(atoc_path).samefile(setools_dir / "build" / "AppTocPackage.bin")

    copied = setools_dir / "build" / "images" / "m55_he.bin"
    assert copied.read_bytes() == artefact.read_bytes()

    config = json.loads((setools_dir / "build" / "config" / "m55_he-slot0.json").read_text())
    assert config == slot0_config("m55_he", "m55_he.bin", "0x80010000", "M55_HE")


def test_sign_slot0_surfaces_a_nonzero_exit(tmp_path):
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    script = setools_dir / _script_name()
    _write_fake_app_gen_toc(script, exit_code=7, stderr_text="DEVICE mismatch")
    artefact = _artefact_bin(tmp_path)

    with pytest.raises(FlashPlanError) as raised:
        sign_slot0(str(setools_dir), str(script), str(artefact), "m55_he", "0x80010000")
    msg = str(raised.value)
    assert "app-gen-toc" in msg
    assert "7" in msg
    assert "DEVICE mismatch" in msg


def test_sign_slot0_raises_when_the_report_has_no_marker(tmp_path):
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    script = setools_dir / _script_name()
    _write_fake_app_gen_toc(script, map_line="nothing useful here")
    artefact = _artefact_bin(tmp_path)

    with pytest.raises(FlashPlanError) as raised:
        sign_slot0(str(setools_dir), str(script), str(artefact), "m55_he", "0x80010000")
    assert "APP Package Start Address" in str(raised.value)


def test_sign_slot0_raises_when_the_blob_was_not_produced(tmp_path):
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    script = setools_dir / _script_name()
    _write_fake_app_gen_toc(script, write_blob=False)
    artefact = _artefact_bin(tmp_path)

    with pytest.raises(FlashPlanError) as raised:
        sign_slot0(str(setools_dir), str(script), str(artefact), "m55_he", "0x80010000")
    assert "AppTocPackage.bin" in str(raised.value)


def test_sign_slot0_guards_the_entry_id_charset(tmp_path):
    """`entry_id` becomes a filename AND a JSON key -- the same
    `validate_identifier` charset guard `flash_plan.py` uses everywhere else
    a manifest value is interpolated into a spawned tool's inputs."""
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    script = setools_dir / _script_name()
    _write_fake_app_gen_toc(script)
    artefact = _artefact_bin(tmp_path)

    with pytest.raises(FlashPlanError):
        sign_slot0(str(setools_dir), str(script), str(artefact), "a;b", "0x80010000")


# ── the full resolve -> find -> sign path, via find_app_gen_toc itself ──────


def test_find_app_gen_toc_then_sign_slot0_end_to_end(tmp_path, monkeypatch):
    """Proves `find_app_gen_toc`'s OWN lookup (not just `sign_slot0` given an
    already-known path) chains into a real sign. `APP_GEN_TOC` is
    monkeypatched to the platform-spawnable name ONLY on Windows (see the
    module docstring); `find_app_gen_toc`'s lookup logic itself is
    untouched, real, and runs unmodified either way."""
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    name = _script_name()
    if name != setools_module.APP_GEN_TOC:
        monkeypatch.setattr(setools_module, "APP_GEN_TOC", name)
    script_path = setools_dir / name
    _write_fake_app_gen_toc(script_path)
    artefact = _artefact_bin(tmp_path)

    found = find_app_gen_toc(str(setools_dir))
    assert found == str(script_path)

    atoc_path, address = sign_slot0(
        str(setools_dir), found, str(artefact), "m55_he", "0x80010000"
    )
    assert address == _REAL_ATOC_ADDRESS
    assert os.path.isfile(atoc_path)
