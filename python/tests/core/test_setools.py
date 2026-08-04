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
    # tan-cli#380: the returned path is an IMMUTABLE per-run COPY, never the
    # shared `build/AppTocPackage.bin` the next sign in this install
    # overwrites -- same bytes, different file.
    shared = setools_dir / "build" / "AppTocPackage.bin"
    assert not Path(atoc_path).samefile(shared)
    assert Path(atoc_path).read_bytes() == shared.read_bytes()
    assert Path(atoc_path).parent == setools_dir / "build" / "tan-atoc"

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


def test_sign_slot0_does_not_report_a_stale_atoc_from_a_soft_failing_respawn(tmp_path):
    """tan-cli#365 (BLOCKER, hardware-destructive) / tan-cli#373 (BLOCKER
    regression in #365's own fix). `build/app-package-map.txt` and
    `build/AppTocPackage.bin` are FIXED, SETOOLS-wide paths (not
    per-`entry_id`) that a PREVIOUS run may have already left well-formed --
    parsing/`isfile`-checking them after THIS spawn proves nothing about THIS
    spawn unless a soft failure (app-gen-toc exits 0 without actually
    writing) can be told apart from a real one. Seed both with a
    stale-but-well-formed report/blob, then drive a fake `app-gen-toc` that
    exits 0 WITHOUT touching either: `sign_slot0` must refuse rather than
    silently hand back the stale pair -- `plan_alif_mram_jlink` would burn it
    into on-die MRAM alongside a fresh app image, and recovery from that is
    re-provisioning over SE-UART.

    **#373: the map must survive the refusal untouched.** #365's own first
    fix told the soft failure apart by DELETING the map first -- which meant
    a soft-failing re-sign destroyed every PRIOR entry too (a real defect,
    not just this stale one): a second Flow D entry pointing its own
    `flash_args.atoc_map` at this same file would lose its true address the
    moment this deletion ran. This is the positive proof of the fix: the
    seeded stale line is still readable, byte for byte, AFTER the raise --
    `sign_slot0` refuses without deleting anything."""
    setools_dir = tmp_path / "setools"
    (setools_dir / "build").mkdir(parents=True)
    stale_report = f"APP Package Start Address: {_REAL_ATOC_ADDRESS}\n"
    (setools_dir / "build" / "app-package-map.txt").write_text(stale_report, encoding="utf-8")
    (setools_dir / "build" / "AppTocPackage.bin").write_bytes(b"stale-atoc-bytes")

    script = setools_dir / _script_name()
    _write_noop_app_gen_toc(script)
    artefact = _artefact_bin(tmp_path)

    with pytest.raises(FlashPlanError) as raised:
        sign_slot0(str(setools_dir), str(script), str(artefact), "m55_he", "0x80010000")
    assert "was not updated" in str(raised.value)

    # The stale map is NEVER deleted -- it is APPEND-mode, the accumulated
    # sign record for the whole install, not per-run scratch (tan-cli#373).
    assert (setools_dir / "build" / "app-package-map.txt").read_text(
        encoding="utf-8"
    ) == stale_report


def test_sign_slot0_never_deletes_the_append_mode_map_on_a_real_sign(tmp_path):
    """tan-cli#373 (BLOCKER regression in #365's own fix): the positive half
    of the guard above. `app-package-map.txt` is APPEND-mode -- a real
    `app-gen-toc` run adds a new block, it never truncates the file (per
    `flash_plan.parse_atoc_start_address`'s own docstring, citing the
    measured bench scripts) -- so a prior entry (this entry's own earlier
    run, another entry's, or a hand-run done outside tan) must survive a
    fresh, SUCCESSFUL sign untouched, and `sign_slot0` must return the LAST
    (this run's) address, not the first."""
    setools_dir = tmp_path / "setools"
    (setools_dir / "build").mkdir(parents=True)
    stale_line = "APP Package Start Address: 0x8000F000"
    (setools_dir / "build" / "app-package-map.txt").write_text(
        stale_line + "\n", encoding="utf-8"
    )

    script = setools_dir / _script_name()
    _write_fake_app_gen_toc(script, append=True)
    artefact = _artefact_bin(tmp_path)

    _atoc_path, address = sign_slot0(
        str(setools_dir), str(script), str(artefact), "m55_he", "0x80010000"
    )

    assert address == _REAL_ATOC_ADDRESS
    map_text = (setools_dir / "build" / "app-package-map.txt").read_text(encoding="utf-8")
    assert stale_line in map_text, "a prior entry is not per-run scratch -- it must survive"
    assert map_text.count("APP Package Start Address:") == 2


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


# ── tan-cli#380: two real processes, one SETOOLS install ────────────────────
#
# The fake `app-gen-toc` above is a shell/batch script; these two tests need
# one that can also RENDEZVOUS with a concurrent copy of itself, so it is
# written in Python and reached through a one-line wrapper the host can
# actually spawn. One implementation, both hosts.

#: A fake `app-gen-toc` that stamps its own MARKER into the shared blob and
#: its own ADDRESS into the shared map -- the distinct markers tan-cli#380's
#: acceptance criteria ask for, so a cross-paired result is visible rather
#: than inferred. Reads no `-f` config: the wrapper hard-codes which run it is.
_FAKE_GEN_TOC_PY = '''\
"""Fake `app-gen-toc` for tan-cli#380's overlap test. argv: MARKER ADDRESS
RENDEZVOUS_S (the real tool's own `-f <config>` is passed too and ignored --
this fake's identity comes from its wrapper, not from the config)."""
import os
import sys
import time

MARKER, ADDRESS, RENDEZVOUS_S = sys.argv[1], sys.argv[2], float(sys.argv[3])

os.makedirs("build", exist_ok=True)
with open(os.path.join("build", "ready-" + MARKER), "wb"):
    pass

# BOUNDED rendezvous: wait for another copy of this fake to announce itself.
# This is what makes the UNSERIALIZED failure deterministic instead of a
# timing coincidence -- when two runs are not locked apart, both are provably
# inside their signing window at the same instant. When they ARE locked apart
# no partner can ever appear, so this costs the first holder RENDEZVOUS_S
# once and nothing after that.
deadline = time.monotonic() + RENDEZVOUS_S
while time.monotonic() < deadline:
    if any(n.startswith("ready-") and n != "ready-" + MARKER for n in os.listdir("build")):
        break
    time.sleep(0.01)

# Blob first, map second, with a gap between them: that gap is exactly where
# an unserialized sibling overwrites the shared blob, leaving the address
# appended below paired with bytes that are no longer the ones this run made.
with open(os.path.join("build", "AppTocPackage.bin"), "wb") as fh:
    fh.write(MARKER.encode("ascii"))
time.sleep(0.3)
with open(os.path.join("build", "app-package-map.txt"), "a", encoding="utf-8") as fh:
    fh.write("APP Package Start Address: " + ADDRESS + "\\n")
'''

#: What each child process runs: one real `sign_slot0` in its own OS process
#: (two real processes is the acceptance criterion -- threads would share the
#: lock's own file descriptor and prove nothing about the cross-process case),
#: reporting the (path, address) pair it was handed.
_CHILD_SIGN = '''\
import json, sys
from tan.core.setools import sign_slot0
path, address = sign_slot0(sys.argv[1], sys.argv[2], sys.argv[3], "m55_he", "0x80010000")
sys.stdout.write(json.dumps({"path": path, "address": address}))
'''

#: A child that must REFUSE rather than wait out the full `_LOCK_WAIT_S`
#: (180s) -- it shortens its own copy of the constant first.
_CHILD_SIGN_IMPATIENT = '''\
import sys
from tan.core import setools
from tan.core.flash_plan import FlashPlanError
setools._LOCK_WAIT_S = 0.3
try:
    setools.sign_slot0(sys.argv[1], sys.argv[2], sys.argv[3], "m55_he", "0x80010000")
except FlashPlanError as err:
    sys.stdout.write(str(err))
    sys.exit(3)
'''

#: Long enough to absorb a cold interpreter start on the slower of the two
#: children (measured worst case here is well under a second) -- the margin is
#: what the rendezvous above spends to be deterministic, and the whole test
#: pays it exactly once because the lock is what keeps the partner away.
_RENDEZVOUS_S = 2.0


def _child_env() -> dict[str, str]:
    """`PYTHONPATH` pinned to the package root of the `tan` this test itself
    imported -- an editable install elsewhere on the machine would otherwise
    decide which `setools.py` the children get, and they must be testing THIS
    one."""
    root = str(Path(tan.__file__).resolve().parent.parent)
    return {**os.environ, "PYTHONPATH": root}


def _write_marker_app_gen_toc(setools_dir: Path, marker: str, address: str) -> str:
    """A spawnable `app-gen-toc-<marker>` wrapper around `_FAKE_GEN_TOC_PY`,
    bound to one marker/address pair. `sign_slot0` takes the tool path
    explicitly, so two wrappers in one install is how each concurrent run gets
    a distinguishable identity without the fake having to parse a config."""
    fake = setools_dir / "fake_gen_toc.py"
    if not fake.exists():
        fake.write_text(_FAKE_GEN_TOC_PY, encoding="utf-8", newline="\n")
    args = f'"{fake}" {marker} {address} {_RENDEZVOUS_S}'
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
    """tan-cli#380 (BLOCKER, HARDWARE SAFETY). `build/AppTocPackage.bin` and
    `build/app-package-map.txt` are FIXED, install-wide paths. Two `tan flash`
    processes sharing one `$SETOOLS_DIR` used to interleave on them freely:
    one could unlink or overwrite the blob while the other signed, or after
    the other had already paired an address with that path -- and the path was
    handed back MUTABLE, so even a perfectly-signed run could be corrupted
    between `sign_slot0` returning and J-Link reading it. What lands in on-die
    MRAM is then another run's ATOC at this run's address; recovery is
    re-provisioning over SE-UART.

    Two REAL processes, both signing into one install with the SAME `entry_id`
    (`m55_he` -- the realistic case: two boards, same core, one SETOOLS
    install), each with its own marker blob and its own address. Every run
    must get back its OWN bytes at its OWN address, from a path that still
    exists and still holds them after both runs are finished.

    The overlap is FORCED, not hoped for: each fake `app-gen-toc` announces
    itself and waits (bounded, `_RENDEZVOUS_S`) for its sibling before writing
    anything, so if the two are not serialized they are provably mid-sign at
    the same instant. Serialized, the handshake simply times out for the first
    holder and the second finds a flag already there. The pass is
    deterministic (mutual exclusion is); the pre-fix failure is deterministic
    down to the sub-millisecond interleaving of two writes that are, without
    the lock, aimed at the same file.
    """
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    runs = [("ALPHA", "0x8001a000"), ("BETA", "0x8001b000")]

    procs = []
    for marker, address in runs:
        artefact = tmp_path / f"zephyr-{marker}.bin"
        artefact.write_bytes(f"app-image-{marker}".encode("ascii"))
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _CHILD_SIGN,
                    str(setools_dir),
                    _write_marker_app_gen_toc(setools_dir, marker, address),
                    str(artefact),
                ],
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

    for marker, address, got in results:
        assert got["address"] == address, f"{marker} was handed another run's address: {got}"
        blob = Path(got["path"])
        assert blob.is_file(), f"{marker}'s ATOC was deleted by the other run: {got['path']}"
        assert blob.read_bytes() == marker.encode("ascii"), (
            f"{marker}'s returned ATOC holds another run's bytes: {blob.read_bytes()!r}"
        )
    assert results[0][2]["path"] != results[1][2]["path"], "both runs got the same mutable path"

    # The append-mode map keeps BOTH runs' records -- #373's guarantee has to
    # survive #380's fix, so the serialization must not have eaten either.
    map_text = (setools_dir / "build" / "app-package-map.txt").read_text(encoding="utf-8")
    assert map_text.count("APP Package Start Address:") == 2, map_text


def test_a_second_process_cannot_enter_the_sign_step_while_the_lock_is_held(tmp_path):
    """The deterministic half of the proof above: with the lock held here, a
    real second process must not touch the install AT ALL -- not prepare it,
    not spawn `app-gen-toc`, not read an address. It refuses with the sign
    lock named (its own `_LOCK_WAIT_S` shortened so the test does not sit out
    the real 180s), and `build/` -- everything `sign_slot0` creates -- is
    still absent afterwards."""
    setools_dir = tmp_path / "setools"
    setools_dir.mkdir()
    script = _write_fake_app_gen_toc(setools_dir / _script_name())
    artefact = _artefact_bin(tmp_path)

    with setools_module._setools_lock(str(setools_dir)):
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_SIGN_IMPATIENT, str(setools_dir), script, str(artefact)],
            capture_output=True,
            text=True,
            env=_child_env(),
            timeout=120,
        )

    assert proc.returncode == 3, f"the second process was not held off: {proc.stdout}{proc.stderr}"
    assert "sign lock" in proc.stdout, proc.stdout
    assert not (setools_dir / "build").exists(), "the blocked run still touched the install"
