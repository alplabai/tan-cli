# SPDX-License-Identifier: Apache-2.0
"""Diff the Python `tan image` / `tan size` against the shipped Rust binary on
identical argv in the identical cwd.

**This file is the gate.** Neither command has a committed conformance fixture:
`contract/README.md` records `build --plan`, `build --manifest*` and `size` in
neither the frozen list nor the stated-uncovered rows (tan-cli#200), and the
bundle path has none either. A green `pytest` that never compared against the
oracle would prove very little here, so every behaviour the two commands are
claimed to reproduce is re-derived from the binary on every run.

Both sides run in the SAME directory, Rust first, with `image-bundle/` removed
in between. That is deliberate: comparing in one cwd means the envelopes' absolute
paths are literally the same strings, so nothing has to be path-normalised and no
normalisation can hide a divergence.

Three things ARE normalised, each an implementation difference rather than a
contract one, and each named at its call site:
  - the OS-error tail `size` interpolates into `size.manifest-unavailable`
    (Rust's `io::Error` Display vs Python's `OSError`);
  - the parser-detail tail of `*.manifest-invalid` (serde_yaml vs PyYAML);
  - `data.slices[].sha256`/`size` in an image bundle -- Python's `tarfile`+`gzip`
    cannot reproduce the Rust `tar`+`flate2` byte stream (member order, header
    fields, gzip metadata). `tests/commands/test_image_command.py` asserts the
    property that actually matters instead: each recorded hash matches the bytes
    this run wrote at that artefact path.

Two cases are xfail(strict=True) -- the deliberate I-18 divergence. Strict, so
if the oracle ever grows the same read-side reconciliation this file FAILS and
forces the divergence note to be retired rather than left as a lie.
"""
import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from . import oracle_fixtures
from .oracle import PACKAGE_ROOT, missing_for_live, python_command, rust_binary

RUST = rust_binary()

pytestmark = pytest.mark.skipif(
    missing_for_live(RUST),
    reason="TAN_PARITY_LIVE=1 needs a Rust tan; build it (cargo build) or set TAN_RUST_BINARY",
)

SOC_5M5 = '{"soc_flash_mb": 5.5, "cores": [{"id": "m55_hp", "tcm_kb": 1280}]}'


# ------------------------------------------------------------------ fixtures


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def wbytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def fake_sdk(root: Path, sku: str, soc: str) -> None:
    write(root / "scripts" / "alp_project.py", "")
    write(
        root / "metadata" / "e1m_modules" / f"{sku}.yaml",
        f"schema_version: 1\nsku: {sku}\nsilicon: test:fam:part\n",
    )
    write(root / "metadata" / "socs" / "test" / "fam" / "part.json", soc)


def make_elf(*, text=100, rodata=20, data=40, bss=200) -> bytes:
    """A minimal little-endian ELF64 whose Berkeley columns are text+rodata=120,
    data=40, bss=200 -> FLASH 160, RAM 240. Hand-built so BOTH implementations
    read the same bytes: the point is to exercise the size tool and the section
    reader, not to ship a real firmware image."""
    alloc, wr, ex = 0x2, 0x1, 0x4
    secs = [
        ("", 0, 0, 0),
        (".text", 1, alloc | ex, text),
        (".rodata", 1, alloc, rodata),
        (".data", 1, alloc | wr, data),
        (".bss", 8, alloc | wr, bss),
        (".shstrtab", 3, 0, 0),
    ]
    shstr = b"\0"
    offs = {}
    for name, *_ in secs:
        offs[name] = len(shstr) if name else 0
        if name:
            shstr += name.encode() + b"\0"
    ehsize = shentsize = 64
    body = b""
    placed = []
    for name, typ, flags, size in secs:
        if name == ".shstrtab":
            placed.append((name, typ, flags, len(shstr), ehsize + len(body)))
            body += shstr
        elif typ == 1:
            placed.append((name, typ, flags, size, ehsize + len(body)))
            body += b"\0" * size
        else:
            placed.append((name, typ, flags, size, ehsize + len(body)))
    shoff = ehsize + len(body)
    header = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8
    header += struct.pack("<HHI", 1, 183, 1)
    header += struct.pack("<QQQ", 0, 0, shoff)
    header += struct.pack(
        "<IHHHHHH", 0, ehsize, 0, 0, shentsize, len(placed), len(placed) - 1
    )
    table = b"".join(
        struct.pack("<IIQQQQIIQQ", offs[n], t, f, 0, o, s, 0, 0, 1, 0)
        for n, t, f, s, o in placed
    )
    return header + body + table


# ------------------------------------------------------------------ compare


def _env(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    inherited = os.environ.get("PYTHONPATH")
    return {
        **os.environ,
        # HOME/USERPROFILE redirected for the same reason the rest of the harness
        # does it: a developer's real `~/.alp/sdk-default` would otherwise decide
        # which SDK resolves, and the `sdk` envelope key with it.
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([inherited] if inherited else [])]
        ),
        **(extra or {}),
    }


def _run(command, argv, cwd: Path, home: Path, extra=None):
    proc = subprocess.run(
        [*command, *argv],
        capture_output=True,
        text=True,
        # UTF-8 explicitly: the platform locale decoder mangles the em-dash the
        # Rust writes raw into `size.budget-unknown`, which reads as a parity
        # failure and is a harness bug.
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=_env(home, extra),
    )
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = {"__raw__": proc.stdout.strip()}
    if not isinstance(payload, dict):
        payload = {"__raw__": proc.stdout.strip()}
    return proc.returncode, payload, proc.stderr


def _normalise(payload: dict) -> dict:
    payload = json.loads(json.dumps(payload))
    for issue in payload.get("issues") or []:
        message = issue.get("message")
        if not isinstance(message, str):
            continue
        message = re.sub(
            r"run `tan build` first \(.*\)\.\Z",
            "run `tan build` first (<OS-ERROR>).",
            message,
            flags=re.S,
        )
        message = re.sub(
            r"(system-manifest is not valid YAML: ).*\Z",
            r"\1<PARSER-DETAIL>",
            message,
            flags=re.S,
        )
        issue["message"] = message
    data = payload.get("data")
    if isinstance(data, dict):
        for entry in data.get("slices") or []:
            # An image bundle's slice entry: hash + size of a tar.gz this run
            # produced. A size report's slice entry has neither key.
            if isinstance(entry, dict) and "artefact" in entry and "sha256" in entry:
                entry["sha256"] = "<ARCHIVE-SHA256>"
                entry["size"] = "<ARCHIVE-SIZE>"
    return payload


def _rust_run(
    argv: list[str], work: Path, env_extra=None
) -> tuple[int, dict, str]:
    """The rust side of `assert_parity`, frozen by default (tan-cli#272) --
    see `oracle_fixtures.resolve`. Scrubbed on `work` (both sides run in the
    SAME directory here, so that is the only root either output can embed)
    so the frozen answer stays replayable from a different scratch dir."""

    def _live():
        code, payload, err = _run([RUST], argv, work, work, env_extra)
        return [code, oracle_fixtures.scrub(payload, work), oracle_fixtures.scrub(err, work)]

    code, payload, err = oracle_fixtures.resolve(_live)
    return code, payload, err


def assert_parity(
    work: Path, argv: list[str], *, text_mode: bool = False, env_extra=None
) -> None:
    """Run both binaries on `argv` in `work` and assert the exit code and the
    envelope agree. `text_mode` compares stderr verbatim instead -- the human
    table and the notice lines, which are a real user surface even though stdout
    carries no contract there."""
    r_code, r_out, r_err = _rust_run(argv, work, env_extra)
    # A previous run's bundle must not decide the next one's notices.
    for build_root in work.rglob("image-bundle"):
        shutil.rmtree(build_root, ignore_errors=True)
    p_code, p_out, p_err = _run(python_command(), argv, work, work, env_extra)
    p_out = oracle_fixtures.scrub(p_out, work)
    p_err = oracle_fixtures.scrub(p_err, work)
    # On a non-capture-platform replay, the separators inside any path
    # anchored at a `scrub` placeholder are the RECORDING host's, not either
    # binary's behaviour -- `size`/`image` reach that surface through
    # `issues[].message` ("no system-manifest.yaml at <ORACLE-ROOT-0>\br\
    # system-manifest.yaml"), not through a dedicated path field. A no-op on
    # Windows and under TAN_PARITY_LIVE=1; see the function for the trade.
    r_out = oracle_fixtures.normalise_scrubbed_path_separators(r_out)
    p_out = oracle_fixtures.normalise_scrubbed_path_separators(p_out)
    r_err = oracle_fixtures.normalise_scrubbed_path_separators(r_err)
    p_err = oracle_fixtures.normalise_scrubbed_path_separators(p_err)

    assert r_code == p_code, (
        f"exit code: rust={r_code} python={p_code}\n"
        f"rust stdout={r_out}\npython stdout={p_out}\npython stderr={p_err}"
    )
    if text_mode:
        assert r_out == {"__raw__": ""}, f"text mode wrote to stdout: {r_out}"
        assert p_out == {"__raw__": ""}, f"text mode wrote to stdout: {p_out}"
        assert r_err == p_err
        return
    assert _normalise(r_out) == _normalise(p_out), (
        f"envelope differs\nrust  ={json.dumps(_normalise(r_out), indent=1)}\n"
        f"python={json.dumps(_normalise(p_out), indent=1)}"
    )


# ------------------------------------------------------------------ size


def test_size_missing_manifest(tmp_path):
    (tmp_path / "br").mkdir()
    assert_parity(tmp_path, ["size", "--format", "json", "--build-root", "br"])


def test_size_measured_slice_and_the_sdk_envelope_key(tmp_path):
    fake_sdk(tmp_path / "sdk", "E1M-TEST", SOC_5M5)
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-TEST\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n",
    )
    write(tmp_path / "br" / "m55_hp-zephyr" / "rom.json", '{"symbols":{"size":4096}}')
    write(tmp_path / "br" / "m55_hp-zephyr" / "ram.json", '{"symbols":{"size":2048}}')
    assert_parity(
        tmp_path,
        ["size", "--format", "json", "--build-root", "br", "--sdk-root", "sdk"],
    )


def test_size_over_budget_and_n_a_and_not_built(tmp_path):
    fake_sdk(
        tmp_path / "sdk",
        "E1M-TEST",
        '{"soc_flash_mb": 0.0001, "cores": [{"id": "m55_hp", "tcm_kb": 1280}]}',
    )
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-TEST\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n"
        "- core_id: a32_cluster\n  os: yocto\n"
        "- core_id: m55_he\n  os: zephyr\n  build_dir: nope\n",
    )
    write(tmp_path / "br" / "m55_hp-zephyr" / "rom.json", '{"symbols":{"size":100000}}')
    write(tmp_path / "br" / "m55_hp-zephyr" / "ram.json", '{"symbols":{"size":100000}}')
    argv = [
        "size", "--format", "json", "--build-root", "br", "--sdk-root", "sdk",
        "--fail-over-budget",
    ]
    assert_parity(tmp_path, argv)
    # ...and the human table + the over-budget line, byte for byte.
    assert_parity(
        tmp_path,
        ["size", "--build-root", "br", "--sdk-root", "sdk", "--fail-over-budget",
         "--no-color"],
        text_mode=True,
    )


def test_size_unknown_budget_notice(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-NOPRESET\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n",
    )
    write(tmp_path / "br" / "m55_hp-zephyr" / "rom.json", '{"symbols":{"size":4096}}')
    write(tmp_path / "br" / "m55_hp-zephyr" / "ram.json", '{"symbols":{"size":2048}}')
    assert_parity(
        tmp_path,
        ["size", "--format", "json", "--build-root", "br", "--fail-over-budget"],
    )


def test_size_measures_a_real_elf(tmp_path):
    fake_sdk(
        tmp_path / "sdk",
        "E1M-TEST",
        '{"soc_flash_mb": 5.5, "cores": [{"id": "m55_hp", "tcm_kb": 1280}], "variants":'
        ' [{"order_code": "OC1", "alp_module_skus": ["E1M-TEST"], "mram_mb": 5.5,'
        ' "sram_banks_kb": {"SRAM2_M55_HP_ITCM": 256, "SRAM3_M55_HP_DTCM": 1024}}]}',
    )
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-TEST\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n  build_dir: m55_hp-zephyr\n",
    )
    wbytes(tmp_path / "br" / "m55_hp-zephyr" / "zephyr" / "zephyr.elf", make_elf())
    assert_parity(
        tmp_path,
        ["size", "--format", "json", "--build-root", "br", "--sdk-root", "sdk"],
    )


def test_size_garbage_artefact_is_not_built(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n  output_artefact: junk.elf\n",
    )
    write(tmp_path / "br" / "junk.elf", "not an elf at all")
    assert_parity(tmp_path, ["size", "--format", "json", "--build-root", "br"])


def test_size_bad_footprint_json_shapes(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n"
        "- core_id: a\n  os: zephyr\n  build_dir: a\n"
        "- core_id: b\n  os: zephyr\n  build_dir: b\n"
        "- core_id: c\n  os: zephyr\n  build_dir: c\n",
    )
    write(tmp_path / "br" / "a" / "rom.json", '{"symbols":{"size":"x"}}')
    write(tmp_path / "br" / "a" / "ram.json", '{"symbols":{"size":10}}')
    write(tmp_path / "br" / "b" / "rom.json", "{not json")
    write(tmp_path / "br" / "b" / "ram.json", '{"size":10}')
    write(tmp_path / "br" / "c" / "rom.json", '{"symbols":{"size":true}}')
    write(tmp_path / "br" / "c" / "ram.json", '{"size":-5}')
    assert_parity(tmp_path, ["size", "--format", "json", "--build-root", "br"])


def test_size_board_override_and_a_bogus_sdk_root(tmp_path):
    fake_sdk(tmp_path / "sdk", "E1M-OTHER",
             '{"soc_flash_mb": 2.0, "cores": [{"id": "m55_hp", "tcm_kb": 512}]}')
    (tmp_path / "notsdk").mkdir()
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-TEST\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n",
    )
    write(tmp_path / "br" / "m55_hp-zephyr" / "rom.json", '{"symbols":{"size":4096}}')
    write(tmp_path / "br" / "m55_hp-zephyr" / "ram.json", '{"symbols":{"size":2048}}')
    assert_parity(
        tmp_path,
        ["size", "--format", "json", "--build-root", "br", "--sdk-root", "sdk",
         "--board", "E1M-OTHER"],
    )
    # `--sdk-root` is terminal even when invalid (I-31): no `sdk` key, no budget,
    # and NOT a silent fall-through to a checkout the user did not name.
    assert_parity(
        tmp_path,
        ["size", "--format", "json", "--build-root", "br", "--sdk-root", "notsdk"],
    )


def test_size_preset_variant_resolution_corner_cases(tmp_path):
    write(tmp_path / "sdk" / "scripts" / "alp_project.py", "")
    write(
        tmp_path / "sdk" / "metadata" / "e1m_modules" / "E1M-TEST.yaml",
        "schema_version: 1\nsku: E1M-TEST\nsilicon: test:fam:part\nsilicon_variant: TBD\n",
    )
    write(
        tmp_path / "sdk" / "metadata" / "socs" / "test" / "fam" / "part.json",
        '{"soc_flash_mb": 4, "cores": [{"id": "m55_hp", "tcm_kb": 1280}], "variants":'
        ' [{"order_code": "OC1", "alp_module_skus": ["E1M-TEST"], "mram_mb": 2,'
        ' "sram_banks_kb": {"SRAM3_M55_HP_DTCM": 512}}, {"order_code": 17}]}',
    )
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-TEST\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n",
    )
    write(tmp_path / "br" / "m55_hp-zephyr" / "rom.json", '{"symbols":{"size":4096}}')
    write(tmp_path / "br" / "m55_hp-zephyr" / "ram.json", '{"symbols":{"size":2048}}')
    # `silicon_variant: TBD` is dropped, the sku reverse-match would answer -- but
    # ONE malformed variant empties the whole list (serde's `unwrap_or_default`),
    # so the budget falls back to `soc_flash_mb` + `tcm_kb`.
    assert_parity(
        tmp_path,
        ["size", "--format", "json", "--build-root", "br", "--sdk-root", "sdk"],
    )


def test_size_absurd_core_id_and_os_values(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n"
        '- core_id: ""\n  os: zephyr\n'
        "- core_id: with space\n  os: zephyr\n"
        '- core_id: "m55/hp"\n  os: zephyr\n',
    )
    assert_parity(tmp_path, ["size", "--format", "json", "--build-root", "br"])


# ------------------------------------------------------------------ image


def test_image_missing_manifest(tmp_path):
    (tmp_path / "br").mkdir()
    assert_parity(tmp_path, ["image", "--format", "json", "--build-root", "br"])
    assert_parity(tmp_path, ["image", "--build-root", "br"], text_mode=True)


def test_image_ok_slice_helper_and_hw_info_passthrough(tmp_path):
    wbytes(tmp_path / "br" / "m55_hp-zephyr" / "zephyr" / "zephyr.elf", b"ELFDATA")
    wbytes(tmp_path / "br" / "gd32" / "zephyr.bin", b"GD32FIRMWARE")
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-AEN701\n  eeprom:\n    magic: keep\n"
        "slices:\n- core_id: m55_hp\n  os: zephyr\n  build_dir: m55_hp-zephyr\n"
        "  status: ok\nhelper_mcus:\n- name: gd32_bridge\n  chip: gd32g553\n"
        "  firmware_path: gd32/zephyr.bin\nboot_order: [m55_hp]\n",
    )
    assert_parity(tmp_path, ["image", "--format", "json", "--build-root", "br"])


def test_image_helper_basename_collision(tmp_path):
    wbytes(tmp_path / "br" / "gd32" / "zephyr.bin", b"GD32FIRMWARE")
    wbytes(tmp_path / "br" / "cc35" / "zephyr.bin", b"CC3501EFIRMWARE")
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: gd32_bridge\n  chip: gd32g553\n  firmware_path: gd32/zephyr.bin\n"
        "- name: cc3501e_otp\n  chip: cc3501e\n  firmware_path: cc35/zephyr.bin\n"
        "boot_order: []\n",
    )
    assert_parity(tmp_path, ["image", "--format", "json", "--build-root", "br"])


def test_image_helper_states(tmp_path):
    # The concrete-but-missing path is the hard error; `TBD` and an absent/empty
    # `firmware_path` are not.
    write(
        tmp_path / "hard" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: gd32_bridge\n  chip: gd32g553\n  firmware_path: firmware/gd32.bin\n"
        "boot_order: []\n",
    )
    assert_parity(tmp_path, ["image", "--format", "json", "--build-root", "hard"])
    assert_parity(tmp_path, ["image", "--build-root", "hard"], text_mode=True)
    write(
        tmp_path / "soft" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-AEN701\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n  build_dir: m55_hp-zephyr\n  status: pending\n"
        "helper_mcus:\n- name: cc3501e_otp\n  chip: cc3501e\n  firmware_path: TBD\n"
        "- name: h1\n  chip: c1\n- name: h2\n  chip: c2\n  firmware_path: ''\n"
        "boot_order: []\n",
    )
    assert_parity(tmp_path, ["image", "--format", "json", "--build-root", "soft"])


def test_image_slice_skip_and_unsafe_name(tmp_path):
    write(
        tmp_path / "skip" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n  build_dir: does-not-exist\n  status: ok\n"
        "- core_id: m55_he\n  os: zephyr\n  status: ok\n"
        "helper_mcus: []\nboot_order: []\n",
    )
    assert_parity(tmp_path, ["image", "--format", "json", "--build-root", "skip"])
    assert_parity(tmp_path, ["image", "--build-root", "skip"], text_mode=True)

    wbytes(tmp_path / "unsafe" / "bd" / "zephyr" / "zephyr.elf", b"ELFDATA")
    write(
        tmp_path / "unsafe" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n"
        '- core_id: "../../../../escape"\n  os: zephyr\n  build_dir: bd\n  status: ok\n'
        "helper_mcus: []\nboot_order: []\n",
    )
    assert_parity(tmp_path, ["image", "--format", "json", "--build-root", "unsafe"])


# --------------------------------------------------- shared manifest failures


@pytest.mark.parametrize("command", ["size", "image"])
@pytest.mark.parametrize(
    "document",
    [
        "schema_version: 2\nslices: []\n",
        "schema_version: 1\nslices: [\n",
        "slices: []\n",
        "schema_version: 1\nslices:\n- os: zephyr\n",
        "just-a-string\n",
    ],
    ids=["bad-version", "bad-yaml", "no-version", "slice-no-core-id", "scalar-root"],
)
def test_malformed_manifest_parity(tmp_path, command, document):
    write(tmp_path / "br" / "system-manifest.yaml", document)
    assert_parity(tmp_path, [command, "--format", "json", "--build-root", "br"])


@pytest.mark.parametrize("command", ["size", "image"])
@pytest.mark.parametrize(
    "document",
    [
        # Refused: a `Vec<_>` field that is not a sequence -- `~` INCLUDED.
        "schema_version: 1\nslices: ~\n",
        "schema_version: 1\nboot_order: notalist\n",
        "schema_version: 1\nstorage: nope\n",
        "schema_version: 1\nipc:\n- name: n\n  kind: k\n  endpoints: nope\n",
        # Refused: `hw_info` is a struct, and `~` is NOT accepted for it.
        "schema_version: 1\nhw_info: notamapping\n",
        "schema_version: 1\nhw_info: ~\n",
        "schema_version: 1\nhw_info:\n  sku: [1]\n",
        # ACCEPTED: a `String` field takes the scalar's RAW TEXT -- `build_dir: 0o17`
        # is the path `0o17`, not `15`, and `os: yes` is `"yes"`, not `"true"`.
        "schema_version: 1\ngenerated_by: 7\nhw_info:\n  sku: 0x10\nslices:\n"
        "- core_id: 007\n  os: zephyr\n  build_dir: 0o17\n  status: yes\n",
        # ACCEPTED, and the two halves differ: `os` is a `String` so `~` is the
        # string "~"; `build_dir` is an `Option<String>` so `~` is ABSENT.
        "schema_version: 1\nhw_info:\n  sku: ~\nslices:\n- core_id: c\n  os: ~\n"
        "  build_dir: ~\n  output_artefact: null\n",
        # ACCEPTED, and `hw_info` is carried verbatim -- so its scalars must resolve
        # by the YAML 1.2 CORE schema, not PyYAML's 1.1. `2024-01-01` is the row that
        # matters: resolved as a date it is not JSON-serializable at all.
        "schema_version: 1\nhw_info:\n  a: yes\n  b: 007\n  c: 1:30\n"
        "  d: 2024-01-01\n  e: 0o17\n  f: 0xA5\n  g: 1.50\nslices: []\n"
        "helper_mcus: []\nboot_order: []\n",
        # ACCEPTED by the typed read, but an integer outside i64::MIN..=u64::MAX
        # cannot live in a `serde_yaml::Value`, so the passthrough silently yields
        # its defaults and `hw_info`/`boot_order` are both dropped.
        "schema_version: 1\nhw_info:\n  v: 123456789012345678901234567890\n"
        "slices: []\nhelper_mcus: []\nboot_order: [a]\n",
    ],
    ids=[
        "slices-null", "boot_order-scalar", "storage-scalar", "endpoints-scalar",
        "hw_info-scalar", "hw_info-null", "sku-sequence",
        "string-fields-keep-raw-text", "null-required-vs-optional",
        "hw_info-core-schema", "hw_info-int-beyond-serde-range",
    ],
)
def test_field_type_leniency_parity(tmp_path, command, document):
    """serde_yaml's leniency around string fields and sequences, in BOTH
    directions. Every case was determined by running the binary, not inferred --
    guessing gets both directions wrong, and each wrong guess is a document one
    implementation accepts and the other refuses."""
    write(tmp_path / "br" / "system-manifest.yaml", document)
    assert_parity(tmp_path, [command, "--format", "json", "--build-root", "br"])


@pytest.mark.parametrize("command", ["size", "image"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("build_dir", '"a\\0b"'), ("output_artefact", '"a\\0b.elf"')],
    ids=["build_dir", "output_artefact"],
)
def test_an_embedded_nul_in_a_path_field_parity(tmp_path, command, field, value):
    # An embedded NUL makes Python's `os.path.isfile` raise ValueError -- NOT an
    # OSError -- so a handler catching only OSError reports a bad INPUT as a tan
    # bug at exit 5. Rust's `is_file()` just answers false.
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: c\n  os: zephyr\n"
        f"  status: ok\n  {field}: {value}\nhelper_mcus: []\nboot_order: []\n",
    )
    assert_parity(tmp_path, [command, "--format", "json", "--build-root", "br"])


def test_an_embedded_nul_in_a_helper_firmware_path_parity(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n- name: h\n"
        '  chip: c\n  firmware_path: "a\\0b"\nboot_order: []\n',
    )
    assert_parity(tmp_path, ["image", "--format", "json", "--build-root", "br"])


@pytest.mark.parametrize("command", ["size", "image"])
@pytest.mark.parametrize("build_root", ["", ".", "./"], ids=["empty", "dot", "dot-slash"])
def test_odd_build_root_values_parity(tmp_path, command, build_root):
    write(
        tmp_path / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-X\nslices:\n- core_id: c\n  os: zephyr\n"
        "helper_mcus: []\nboot_order: []\n",
    )
    write(
        tmp_path / "build" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-X\nslices: []\nhelper_mcus: []\n"
        "boot_order: []\n",
    )
    assert_parity(
        tmp_path, [command, "--format", "json", "--build-root", build_root]
    )


@pytest.mark.parametrize("command", ["size", "image"])
def test_a_project_that_does_not_exist_parity(tmp_path, command):
    assert_parity(tmp_path, [command, "--format", "json", "--project", "nowhere"])


@pytest.mark.parametrize(
    ("home", "profile"),
    [("", ""), ("/does/not/exist", "Q:\\does\\not\\exist")],
    ids=["empty", "bogus"],
)
def test_a_hostile_home_does_not_break_sdk_resolution(tmp_path, home, profile):
    # `~/.alp/sdk-default` is one of the SDK precedence tiers, so HOME/USERPROFILE
    # are inputs this command reads. Neither may turn into an exception on either
    # side, and both sides must still agree.
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-X\nslices:\n- core_id: c\n  os: zephyr\n",
    )
    assert_parity(
        tmp_path,
        ["size", "--format", "json", "--build-root", "br"],
        env_extra={"HOME": home, "USERPROFILE": profile},
    )


def test_no_color_and_ci_flags_are_accepted_by_both(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: c\n  os: zephyr\n",
    )
    assert_parity(
        tmp_path,
        ["size", "--build-root", "br", "--no-color", "--ci"],
        text_mode=True,
    )
    assert_parity(
        tmp_path,
        ["size", "--build-root", "br"],
        text_mode=True,
        env_extra={"NO_COLOR": "1"},
    )


@pytest.mark.parametrize("command", ["size", "image"])
def test_non_utf8_manifest_parity(tmp_path, command):
    (tmp_path / "br").mkdir()
    (tmp_path / "br" / "system-manifest.yaml").write_bytes(
        b"schema_version: 1\nhw_info:\n  sku: \xff\xfe\nslices: []\n"
    )
    assert_parity(tmp_path, [command, "--format", "json", "--build-root", "br"])


@pytest.mark.parametrize("command", ["size", "image"])
def test_manifest_path_is_a_directory_parity(tmp_path, command):
    (tmp_path / "br" / "system-manifest.yaml").mkdir(parents=True)
    assert_parity(tmp_path, [command, "--format", "json", "--build-root", "br"])


@pytest.mark.parametrize("command", ["size", "image"])
@pytest.mark.parametrize(
    "argv_tail",
    [["app"], ["--project", "app"], []],
    ids=["positional", "project-flag", "bare"],
)
def test_path_resolution_parity(tmp_path, command, argv_tail):
    # A positional `app_path` does NOT move `project.root` (only `--project`
    # does), and both still find `app/build/system-manifest.yaml`.
    write(
        tmp_path / "app" / "build" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-X\nslices: []\nhelper_mcus: []\n"
        "boot_order: []\n",
    )
    assert_parity(tmp_path, [command, "--format", "json", *argv_tail])


@pytest.mark.parametrize("command", ["size", "image"])
def test_root_format_flag_position_parity(tmp_path, command):
    # clap's `--format` is `global = true`, so the extension may pass it before
    # the subcommand name; Click gives the group only what precedes it, which is a
    # separate code path in the port and not in Rust.
    write(tmp_path / "br" / "system-manifest.yaml", "schema_version: 1\nslices: []\n")
    assert_parity(tmp_path, ["--format", "json", command, "--build-root", "br"])


# ----------------------------------------------- the deliberate I-18 divergence


@pytest.mark.xfail(
    strict=True,
    reason="I-18, deliberate: `west build` is emitted with no `-d`, so its tree "
    "lands in <build_dir>/build/. The oracle reconciles that at BUILD time "
    "(resolve_zephyr_artefact rewrites the manifest); this port's `tan build` "
    "does not write that manifest yet, so `tan size` reconciles it on the read "
    "side and MEASURES what the oracle calls `not-built`. Strict: if the oracle "
    "ever grows the same read-side probe, this passes and fails the run, which "
    "is the signal to retire the divergence note.",
)
def test_i18_nested_elf_diverges_from_the_oracle(tmp_path):
    fake_sdk(tmp_path / "sdk", "E1M-TEST", SOC_5M5)
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-TEST\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n",
    )
    wbytes(
        tmp_path / "br" / "m55_hp-zephyr" / "build" / "zephyr" / "zephyr.elf", make_elf()
    )
    assert_parity(
        tmp_path,
        ["size", "--format", "json", "--build-root", "br", "--sdk-root", "sdk"],
    )


@pytest.mark.xfail(strict=True, reason="I-18, deliberate -- see the elf case above.")
def test_i18_nested_footprint_json_diverges_from_the_oracle(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: m55_hp\n  os: zephyr\n",
    )
    nested = tmp_path / "br" / "m55_hp-zephyr" / "build"
    write(nested / "rom.json", '{"symbols":{"size":4096}}')
    write(nested / "ram.json", '{"symbols":{"size":2048}}')
    assert_parity(tmp_path, ["size", "--format", "json", "--build-root", "br"])
