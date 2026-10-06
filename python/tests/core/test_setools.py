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
import subprocess
import sys
from pathlib import Path

import pytest

import tan
from tan.core.flash_plan import FlashPlanError
from tan.core import setools as setools_module
from tan.core import setools_scratch
from tan.core.setools import (
    SetoolsSource,
    SignedSlot0,
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


def test_resolve_setools_dir_precedence_is_flag_then_env_then_manifest():
    """tan-cli#368: `--setools-dir` outranks `SETOOLS_DIR`, which outranks
    `flash_args.setools_dir` -- the OPPOSITE of most `flash_args` accessors in
    this codebase, and deliberately so: the manifest field is rebuilt over by
    every `tan build`, so it is the LEAST durable of the three, not the most.
    All three set, with all three DIFFERENT, proves the full chain in one
    call each."""
    all_three = resolve_setools_dir(
        {"setools_dir": "/from/manifest"}, {"SETOOLS_DIR": "/from/env"}, "/from/flag"
    )
    assert all_three == SetoolsSource("/from/flag", "the --setools-dir flag")

    flag_absent = resolve_setools_dir(
        {"setools_dir": "/from/manifest"}, {"SETOOLS_DIR": "/from/env"}, None
    )
    assert flag_absent == SetoolsSource("/from/env", "the SETOOLS_DIR environment variable")


def test_resolve_setools_dir_falls_back_to_the_manifest_field():
    resolved = resolve_setools_dir({"setools_dir": "/from/manifest"}, {})
    assert resolved == SetoolsSource("/from/manifest", "flash_args.setools_dir")


def test_resolve_setools_dir_is_none_when_none_of_the_three_is_set():
    assert resolve_setools_dir({}, {}) is None
    assert resolve_setools_dir({"setools_dir": ""}, {"SETOOLS_DIR": ""}, "") is None


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
    runs on customer-supplied paths (`--setools-dir`, `flash_args.setools_dir`
    or an env var)."""
    assert find_app_gen_toc("bad\x00path") is None


def test_find_app_gen_toc_also_tries_the_exe_suffix_on_windows(tmp_path, monkeypatch):
    """tan-cli#369: a genuine Windows SETOOLS install ships `app-gen-toc.exe`,
    and the bare-name-only lookup never found it -- `missing_tool_message`
    then told a real Windows customer their install "did not look like" one.
    `os.name` is faked to `"nt"` so the `.exe` branch is exercised on every
    host running this suite, not only on Windows -- `os.path.isfile` itself
    is unaffected by `os.name` (resolved once at interpreter start, not
    re-dispatched per call), so this only exercises the candidate list."""
    monkeypatch.setattr(os, "name", "nt")
    exe = tmp_path / f"{setools_module.APP_GEN_TOC}.exe"
    exe.write_text("", encoding="utf-8")
    assert find_app_gen_toc(str(tmp_path)) == str(exe)


# ── guidance messages -- remedy first, blame never ──────────────────────────


def test_unresolved_message_names_the_remedy():
    msg = unresolved_message()
    assert "SETOOLS" in msg
    assert "license-gated" in msg
    assert "app-gen-toc" in msg
    assert "--setools-dir" in msg
    assert "SETOOLS_DIR=" in msg
    assert "flash_args.setools_dir" in msg
    # Precedence order, flag first (tan-cli#368).
    assert msg.index("--setools-dir") < msg.index("SETOOLS_DIR=")
    assert msg.index("SETOOLS_DIR=") < msg.index("flash_args.setools_dir")
    # No blame: never says the customer did anything wrong.
    assert "you " not in msg.lower()


def test_unresolved_message_names_the_sku_and_device_from_the_manifest():
    """tan-cli#1319: the subject is the manifest's own SKU / J-Link part
    profile, never a hardcoded AEN801."""
    msg = unresolved_message("E1M-AEN803", "AE822FA0E5597LS0_M55_HE")
    assert "the E1M-AEN803 slot0 image (AE822FA0E5597LS0_M55_HE) needs a SIGNED ATOC" in msg
    assert "AEN801" not in msg
    assert "the E1M-V2N101 slot0 image needs" in unresolved_message("E1M-V2N101", None)
    # Nothing known: a SKU-free noun, still no invented SKU.
    bare = unresolved_message(None, "  ")
    assert "an Alif Ensemble MRAM slot0 image needs" in bare
    assert "AEN801" not in bare


def test_missing_tool_message_names_the_source():
    source = SetoolsSource("/opt/bad-install", "flash_args.setools_dir")
    msg = missing_tool_message(source)
    assert "/opt/bad-install" in msg
    assert "flash_args.setools_dir" in msg
    assert "app-gen-toc" in msg


def test_missing_tool_message_names_what_was_checked_not_a_conclusion(tmp_path):
    """tan-cli#369: no more "this does not look like an Alif Security Toolkit
    install" verdict -- the message must name the exact candidate path(s)
    tried, and say so differently depending on whether `setools.path` is
    even a real directory."""
    # A directory that genuinely does not exist.
    missing = SetoolsSource(str(tmp_path / "nope"), "the SETOOLS_DIR environment variable")
    msg = missing_tool_message(missing)
    assert "does not look like an Alif Security Toolkit install" not in msg
    assert "not a directory at all" in msg
    assert setools_module.APP_GEN_TOC in msg

    # A path pointed at the app-gen-toc BINARY itself, not its parent.
    binary_path = tmp_path / setools_module.APP_GEN_TOC
    binary_path.write_text("", encoding="utf-8")
    pointed_at_binary = SetoolsSource(str(binary_path), "flash_args.setools_dir")
    msg = missing_tool_message(pointed_at_binary)
    assert "PARENT directory" in msg

    # A real directory that simply holds no app-gen-toc.
    (tmp_path / "empty").mkdir()
    empty_dir = SetoolsSource(str(tmp_path / "empty"), "--setools-dir")
    msg = missing_tool_message(empty_dir)
    assert "the directory exists but holds none of them" in msg


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
    append: bool = False,
) -> str:
    """A fake `app-gen-toc` at `dest`, genuinely spawnable on THIS host with
    no `shell=True` -- it writes `build/app-package-map.txt` (with or
    without the marker line) and `build/AppTocPackage.bin` under its OWN
    cwd (`sign_slot0` always spawns with `cwd=setools_dir`, matching the
    bench's own `cd $SETOOLS_DIR && ./app-gen-toc ...`), then exits
    `exit_code`. Proves the WIRING, not a real SETOOLS.

    `append` (tan-cli#373): the real `app-gen-toc` behaviour
    `parse_atoc_start_address`'s docstring documents -- ADDS a fresh block to
    `app-package-map.txt` rather than truncating it. `False` (the default)
    matches every OTHER test here, which starts from an empty/absent map and
    so cannot tell append from overwrite; `True` is for the one test that
    specifically proves a PRIOR entry survives a real sign untouched."""
    redirect = ">>" if append else ">"
    if os.name == "nt":
        lines = ["@echo off", "if not exist build mkdir build"]
        if map_line is not None:
            lines.append(f"{redirect}build\\app-package-map.txt echo {map_line}")
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
            lines.append(f'printf "%s\\n" "{map_line}" {redirect} build/app-package-map.txt')
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


def _write_noop_app_gen_toc(dest: Path, *, exit_code: int = 0) -> str:
    """A fake `app-gen-toc` that touches NOTHING under its cwd -- simulates a
    SOFT FAILURE (tan-cli#365): a real spawn that exits 0 without actually
    (re)writing `build/app-package-map.txt` / `build/AppTocPackage.bin`.
    Whatever those already held before the spawn is left completely
    untouched, so a caller that trusts their post-spawn presence alone would
    happily report a PREVIOUS run's stale ATOC as this run's result."""
    if os.name == "nt":
        dest.write_text(f"@echo off\r\nexit /b {exit_code}\r\n", encoding="utf-8")
    else:
        dest.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
        os.chmod(dest, 0o755)
    return str(dest)


def _artefact_bin(tmp_path: Path) -> Path:
    artefact = tmp_path / "zephyr.bin"
    artefact.write_bytes(b"fake-app-image-bytes")
    return artefact


def _tree_digest(root: Path) -> str:
    """sha256 over every path, file content and symlink target under `root`."""
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            digest.update(f"L {rel} {os.readlink(path)}\n".encode())
        elif path.is_dir():
            digest.update(f"D {rel}\n".encode())
        else:
            digest.update(f"F {rel}\n".encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _sign(setools_dir, script, artefact, scratch_parent, **kwargs) -> SignedSlot0:
    scratch_parent.mkdir(exist_ok=True)
    return sign_slot0(
        str(setools_dir), str(script), str(artefact), "m55_he", "0x80010000",
        scratch_parent=str(scratch_parent), **kwargs,
    )


def test_sign_slot0_copies_writes_and_derives_the_address(tmp_path):
    """The end-to-end happy path, in a SCRATCH tree (tan-cli#1325): the raw
    `.bin` is copied into the scratch `build/images/<id>.bin`, the config is
    written to the scratch `build/config/<id>-slot0.json`, `app-gen-toc` runs
    there, and the derived address and the blob's scratch path come back --
    tan-cli#353's requirement (a). The shared install is byte-identical."""
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    script = setools_dir / _script_name()
    _write_fake_app_gen_toc(script)
    artefact = _artefact_bin(tmp_path)
    before = _tree_digest(setools_dir)

    signed = _sign(setools_dir, script, artefact, tmp_path / "scratch")
    try:
        assert signed.atoc_address == _REAL_ATOC_ADDRESS
        scratch = Path(signed.scratch_dir)
        assert scratch.parent == tmp_path / "scratch"
        assert Path(signed.atoc_path) == scratch / "build" / "AppTocPackage.bin"
        assert signed.atoc_size == Path(signed.atoc_path).stat().st_size > 0
        assert (scratch / "build" / "images" / "m55_he.bin").read_bytes() == artefact.read_bytes()
        config = json.loads((scratch / "build" / "config" / "m55_he-slot0.json").read_text())
        assert config == slot0_config("m55_he", "m55_he.bin", "0x80010000", "M55_HE")
        # The shared install: untouched, no `build/`, no lock file, no copy-out.
        assert _tree_digest(setools_dir) == before
        assert not (setools_dir / "build").exists()
    finally:
        assert setools_scratch.cleanup_scratch(signed.scratch_dir)
    assert not Path(signed.scratch_dir).exists()
    assert _tree_digest(setools_dir) == before


def test_sign_slot0_surfaces_a_nonzero_exit(tmp_path):
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    script = setools_dir / _script_name()
    _write_fake_app_gen_toc(script, exit_code=7, stderr_text="DEVICE mismatch")
    artefact = _artefact_bin(tmp_path)

    with pytest.raises(FlashPlanError) as raised:
        _sign(setools_dir, script, artefact, tmp_path / "scratch")
    msg = str(raised.value)
    assert (tmp_path / "scratch").is_dir() and not list((tmp_path / "scratch").iterdir()), (
        "a failed sign must remove its scratch tree"
    )
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
        _sign(setools_dir, script, artefact, tmp_path / "scratch")
    assert "APP Package Start Address" in str(raised.value)


def test_sign_slot0_raises_when_the_blob_was_not_produced(tmp_path):
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    script = setools_dir / _script_name()
    _write_fake_app_gen_toc(script, write_blob=False)
    artefact = _artefact_bin(tmp_path)

    with pytest.raises(FlashPlanError) as raised:
        _sign(setools_dir, script, artefact, tmp_path / "scratch")
    assert "AppTocPackage.bin" in str(raised.value)


def test_sign_slot0_does_not_report_a_stale_atoc_from_a_soft_failing_respawn(tmp_path):
    """tan-cli#365 / #373 (hardware-destructive), re-proved for tan-cli#1325.
    The shared install already holds a well-formed `app-package-map.txt` and
    `AppTocPackage.bin` from an EARLIER run; a fake `app-gen-toc` then exits 0
    without writing anything. Pre-#1325 that needed a snapshot-compare guard
    (and deleting the append-mode map was a bug of its own, #373). The scratch
    tree starts EMPTY, so the stale pair is simply not visible: the sign
    refuses, and the shared stale files are still there byte for byte."""
    setools_dir = tmp_path / "setools"
    (setools_dir / "build").mkdir(parents=True)
    stale_report = f"APP Package Start Address: {_REAL_ATOC_ADDRESS}\n"
    (setools_dir / "build" / "app-package-map.txt").write_text(stale_report, encoding="utf-8")
    (setools_dir / "build" / "AppTocPackage.bin").write_bytes(b"stale-atoc-bytes")

    script = setools_dir / _script_name()
    _write_noop_app_gen_toc(script)
    artefact = _artefact_bin(tmp_path)
    before = _tree_digest(setools_dir)

    with pytest.raises(FlashPlanError) as raised:
        _sign(setools_dir, script, artefact, tmp_path / "scratch")
    assert "carries no 'APP Package Start Address:'" in str(raised.value)
    assert _tree_digest(setools_dir) == before
    assert (setools_dir / "build" / "app-package-map.txt").read_text(
        encoding="utf-8"
    ) == stale_report


def test_sign_slot0_never_touches_the_shared_append_mode_map(tmp_path):
    """tan-cli#373 / #1325: `app-package-map.txt` is APPEND-mode, the
    accumulated sign record of the whole install including hand-runs. A real
    sign must neither delete it nor add to it -- the new block lands in the
    scratch report -- and the address handed back is THIS run's."""
    setools_dir = tmp_path / "setools"
    (setools_dir / "build").mkdir(parents=True)
    stale_line = "APP Package Start Address: 0x8000F000"
    (setools_dir / "build" / "app-package-map.txt").write_text(
        stale_line + "\n", encoding="utf-8"
    )

    script = setools_dir / _script_name()
    _write_fake_app_gen_toc(script, append=True)
    artefact = _artefact_bin(tmp_path)
    before = _tree_digest(setools_dir)

    signed = _sign(setools_dir, script, artefact, tmp_path / "scratch")
    try:
        assert signed.atoc_address == _REAL_ATOC_ADDRESS
        assert _tree_digest(setools_dir) == before
        scratch_map = Path(signed.scratch_dir) / "build" / "app-package-map.txt"
        assert scratch_map.read_text(encoding="utf-8").count("APP Package Start Address:") == 1
    finally:
        setools_scratch.cleanup_scratch(signed.scratch_dir)


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
    assert not list(tmp_path.glob("tan-setools-*"))


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

    signed = _sign(setools_dir, found, artefact, tmp_path / "scratch")
    try:
        assert signed.atoc_address == _REAL_ATOC_ADDRESS
        assert os.path.isfile(signed.atoc_path)
    finally:
        setools_scratch.cleanup_scratch(signed.scratch_dir)


# ── tan-cli#380 -> tan-cli#1325: two real processes, one SETOOLS install ────
#
# tan-cli#380 serialised two signs with a cross-process lock because they
# shared `build/AppTocPackage.bin`. tan-cli#1325 removes the shared output: each
# run signs in its own scratch overlay, so there is nothing to lock. The fake
# `app-gen-toc` below is Python (it must RENDEZVOUS with its sibling) behind a
# one-line wrapper the host can spawn.

#: A fake `app-gen-toc` that stamps its own MARKER into the blob and its own
#: ADDRESS into the map under ITS cwd. The rendezvous directory is OUTSIDE the
#: install, so the two runs provably overlap in time without sharing a byte.
_FAKE_GEN_TOC_PY = '''\
"""Fake `app-gen-toc` for tan-cli#1325's overlap test. argv: MARKER ADDRESS
RENDEZVOUS_S RENDEZVOUS_DIR."""
import os
import sys
import time

MARKER, ADDRESS, RENDEZVOUS_S, RDV = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]

os.makedirs(RDV, exist_ok=True)
with open(os.path.join(RDV, "ready-" + MARKER), "wb"):
    pass
deadline = time.monotonic() + RENDEZVOUS_S
while time.monotonic() < deadline:
    if any(n.startswith("ready-") and n != "ready-" + MARKER for n in os.listdir(RDV)):
        break
    time.sleep(0.01)

os.makedirs("build", exist_ok=True)
with open(os.path.join("build", "AppTocPackage.bin"), "wb") as fh:
    fh.write(MARKER.encode("ascii"))
time.sleep(0.3)
with open(os.path.join("build", "app-package-map.txt"), "a", encoding="utf-8") as fh:
    fh.write("APP Package Start Address: " + ADDRESS + "\\n")
'''

_CHILD_SIGN = '''\
import json, sys
from tan.core.setools import sign_slot0
signed = sign_slot0(sys.argv[1], sys.argv[2], sys.argv[3], "m55_he", "0x80010000",
                    scratch_parent=sys.argv[4])
sys.stdout.write(json.dumps({"path": signed.atoc_path, "address": signed.atoc_address,
                             "scratch": signed.scratch_dir}))
'''

_RENDEZVOUS_S = 2.0


def _child_env() -> dict[str, str]:
    """`PYTHONPATH` pinned to the package root of the `tan` this test itself
    imported, so the children test THIS `setools.py`."""
    root = str(Path(tan.__file__).resolve().parent.parent)
    return {**os.environ, "PYTHONPATH": root}


def _write_marker_app_gen_toc(setools_dir: Path, marker: str, address: str, rdv: Path) -> str:
    fake = setools_dir / "fake_gen_toc.py"
    if not fake.exists():
        fake.write_text(_FAKE_GEN_TOC_PY, encoding="utf-8", newline="\n")
    args = f'"{fake}" {marker} {address} {_RENDEZVOUS_S} "{rdv}"'
    if os.name == "nt":
        wrapper = setools_dir / f"app-gen-toc-{marker}.bat"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" {args}\r\nexit /b %errorlevel%\r\n',
            encoding="utf-8",
        )
    else:
        wrapper = setools_dir / f"app-gen-toc-{marker}"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" {args}\n', encoding="utf-8", newline="\n"
        )
        os.chmod(wrapper, 0o755)
    return str(wrapper)


def test_two_processes_signing_one_setools_dir_never_cross_pair(tmp_path):
    """tan-cli#380 (HARDWARE SAFETY), now solved by isolation (tan-cli#1325)
    instead of a lock: two REAL processes sign into one install with the SAME
    `entry_id`, forced to overlap by a rendezvous OUTSIDE the install. Each must
    get back its OWN bytes at its OWN address from its OWN scratch path -- and
    the shared install is byte-identical afterwards, with no lock file in it."""
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    rdv = tmp_path / "rdv"
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    runs = [("ALPHA", "0x8001a000"), ("BETA", "0x8001b000")]

    tools = {m: _write_marker_app_gen_toc(setools_dir, m, a, rdv) for m, a in runs}
    before = _tree_digest(setools_dir)

    procs = []
    for marker, _address in runs:
        artefact = tmp_path / f"zephyr-{marker}.bin"
        artefact.write_bytes(f"app-image-{marker}".encode("ascii"))
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", _CHILD_SIGN, str(setools_dir), tools[marker],
                 str(artefact), str(scratch_parent)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_child_env(),
            )
        )

    results = []
    for (marker, address), proc in zip(runs, procs):
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, f"{marker} run failed ({proc.returncode}): {err or out}"
        results.append((marker, address, json.loads(out)))

    # Both really overlapped (each saw the other's rendezvous flag).
    assert sorted(p.name for p in rdv.iterdir()) == ["ready-ALPHA", "ready-BETA"]
    for marker, address, got in results:
        assert got["address"] == address, f"{marker} was handed another run's address: {got}"
        blob = Path(got["path"])
        assert blob.read_bytes() == marker.encode("ascii"), (
            f"{marker}'s returned ATOC holds another run's bytes: {blob.read_bytes()!r}"
        )
    assert results[0][2]["scratch"] != results[1][2]["scratch"]
    assert results[0][2]["path"] != results[1][2]["path"]
    for _m, _a, got in results:
        assert setools_scratch.cleanup_scratch(got["scratch"])
    assert _tree_digest(setools_dir) == before
    assert not (setools_dir / ".tan-setools-sign.lock").exists()


# ── the scratch overlay itself ──────────────────────────────────────────────


def test_scratch_overlay_copies_small_dirs_symlinks_big_ones_and_never_the_build_dir(
    tmp_path, monkeypatch
):
    """The measured SETOOLS shape: the tool `chdir`s into a subdirectory and
    addresses `../build`, so a SYMLINKED small directory would write through to
    the shared install. Small directories are copied, a large one is linked, the
    top-level files are linked, and `build/` is a fresh real directory that does
    NOT inherit the shared one's contents."""
    if os.name == "nt":
        pytest.skip("symlink semantics are POSIX; Windows copies instead")
    shared = tmp_path / "setools"
    (shared / "utils").mkdir(parents=True)
    (shared / "utils" / "cfg").write_text("cfg", encoding="utf-8")
    (shared / "alif").mkdir()
    (shared / "alif" / "SP.bin").write_bytes(b"x" * 4096)
    (shared / "app-gen-toc").write_text("#!/bin/sh\n", encoding="utf-8")
    (shared / "build" / "config").mkdir(parents=True)
    (shared / "build" / "config" / "stock.json").write_text("{}", encoding="utf-8")
    (shared / "build" / "AppTocPackage.bin").write_bytes(b"shared-atoc")
    monkeypatch.setattr(setools_scratch, "_COPY_DIR_LIMIT_BYTES", 1024)
    before = _tree_digest(shared)

    (tmp_path / "parent").mkdir()
    root = Path(setools_scratch.make_scratch(str(shared), str(tmp_path / "parent")))
    try:
        assert not (root / "utils").is_symlink()  # small: copied
        assert (root / "utils" / "cfg").read_text(encoding="utf-8") == "cfg"
        assert (root / "alif").is_symlink()  # large: linked
        assert (root / "app-gen-toc").is_symlink()  # top-level file: linked
        assert not (root / "build").is_symlink()
        assert sorted(p.name for p in (root / "build").iterdir()) == ["config", "images", "logs"]
        assert not (root / "build" / "AppTocPackage.bin").exists()
        assert not (root / "build" / "config" / "stock.json").exists()
    finally:
        assert setools_scratch.cleanup_scratch(str(root))
    # Removing the overlay unlinked the symlinks and never followed them.
    assert _tree_digest(shared) == before


def test_parse_atoc_report_reads_the_entry_list_and_size():
    text = (
        "APP TOC entry for DEVICE   obj_address 0x8057f450\n"
        "APP TOC entry for m55_he        obj_address 0x8057ea50\n"
        " - APP Package total size: 5552 bytes\n"
        " - APP Package Start Address: 0x8057ea50\n"
    )
    report = setools_scratch.parse_atoc_report(text)
    assert report.entries == (("DEVICE", "0x8057f450"), ("m55_he", "0x8057ea50"))
    assert report.package_size == 5552
    assert setools_scratch.parse_atoc_report("nothing here") == setools_scratch.AtocReport()
