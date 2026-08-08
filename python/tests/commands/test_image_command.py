# SPDX-License-Identifier: Apache-2.0
"""`tan image` end to end: the bundle on disk, the envelope, the exit ladder, and
the hostile inputs.

`image --manifest*`/the bundle path has no committed conformance fixture either
(`contract/README.md`, tan-cli#200), so this file plus
`tests/parity/test_image_size_oracle.py` are the whole gate.
"""
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def run_cli(cwd, *argv):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", "image", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
        timeout=180,
    )


def envelope(result):
    """The ONE JSON document on stdout, and nothing else."""
    assert result.stdout.count("\n") == 1, result.stdout
    return json.loads(result.stdout)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def wbytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def make_sdk_root(base: Path) -> Path:
    """A minimal alp-sdk checkout: just enough for `scripts/alp_project.py`, the
    loader marker `resolve_project_context` checks for."""
    sdk = base / "sdk"
    (sdk / "scripts").mkdir(parents=True, exist_ok=True)
    (sdk / "scripts" / "alp_project.py").write_text("# marker\n", encoding="utf-8")
    return sdk


# ---------------------------------------------------------------- happy paths


def test_ok_slice_is_tarred_hashed_and_recorded(tmp_path):
    wbytes(tmp_path / "br" / "m55_hp-zephyr" / "zephyr" / "zephyr.elf", b"ELFDATA")
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-AEN701\n  eeprom:\n    magic: keep\n"
        "slices:\n- core_id: m55_hp\n  os: zephyr\n  build_dir: m55_hp-zephyr\n"
        "  status: ok\nhelper_mcus: []\nboot_order: [m55_hp]\n",
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 0
    doc = envelope(result)
    assert doc["command"] == "image"
    assert doc["data"]["schema_version"] == 1
    assert doc["data"]["generated_by"] == "tan image"
    row = doc["data"]["slices"][0]
    assert row["artefact"] == "slices/m55_hp-zephyr.tar.gz"
    assert len(row["sha256"]) == 64
    # The additive `eeprom` key survived the raw passthrough -- a typed projection
    # would have dropped it.
    assert doc["data"]["hw_info"]["eeprom"]["magic"] == "keep"
    assert doc["data"]["boot_order"] == ["m55_hp"]
    assert doc["issues"] == []

    bundle_root = tmp_path / "br" / "image-bundle"
    archive = bundle_root / "slices" / "m55_hp-zephyr.tar.gz"
    assert archive.is_file()
    # The recorded hash matches the bytes actually on disk at that artefact path.
    # Byte-identity with the Rust `tar`+`flate2` stream is impossible (member
    # order, header fields, gzip metadata), so SELF-consistency is the contract.
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == row["sha256"]
    assert archive.stat().st_size == row["size"]
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "m55_hp-zephyr" in names
    assert "m55_hp-zephyr/zephyr/zephyr.elf" in names


def test_bundle_manifest_json_is_written_with_the_exact_key_order_and_one_newline(
    tmp_path,
):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus: []\nboot_order: []\n",
    )
    assert run_cli(tmp_path, "--format", "json", "--build-root", "br").returncode == 0
    raw = (tmp_path / "br" / "image-bundle" / "bundle-manifest.json").read_bytes()
    # Exactly one trailing \n, and never CRLF: `Path.write_text` would translate
    # every \n to os.linesep on a Windows host (I-27), and .gitattributes hides it
    # from every gate.
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert b"\r\n" not in raw
    document = json.loads(raw)
    assert list(document) == [
        "schema_version",
        "generated_by",
        "hw_info",
        "slices",
        "helper_mcus",
        "boot_order",
    ]
    # 2-space indent + `": "` separators, matching serde_json's to_string_pretty.
    assert raw.decode().startswith('{\n  "schema_version": 1,\n')


def test_non_finite_hw_info_values_write_valid_json_matching_the_stdout_envelope(tmp_path):
    """tan-cli#387's lesser half: the envelope's own `_serialise` retry already
    nulled a non-finite float on STDOUT, but this file was written through its
    own bare `json.dumps(bundle, indent=2)`, `allow_nan` left at its default --
    so the SAME `hw_info` reached disk with the literal `Infinity`/`-Infinity`/
    `NaN` tokens RFC 8259 has no production for (measured: `json.loads` on the
    written file raised `ValueError: Expecting value` at the first one).

    `soc_flash_mb: 1e400` is the other half of the same measurement, at the
    YAML-read layer rather than the JSON-write one: PyYAML's own float
    constructor resolves an overflowing decimal-with-exponent literal to a
    silently non-finite `float`, same as `.inf`/`.nan` -- but serde_yaml only
    actually produces Infinity for the EXPLICIT sentinel spellings, leaving an
    overflowing generic literal as its raw string (`target/debug/tan.exe`,
    measured). Both are pinned here together because they land in the same
    `hw_info` dict and the fix has to get both right at once, not trade one
    for the other.
    """
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n"
        "  a: .inf\n  b: -.inf\n  c: .nan\n  soc_flash_mb: 1e400\n"
        "slices: []\nhelper_mcus: []\nboot_order: []\n",
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 0, result.stderr
    stdout_doc = envelope(result)
    assert stdout_doc["data"]["hw_info"] == {
        "a": None, "b": None, "c": None, "soc_flash_mb": "1e400",
    }

    raw = (tmp_path / "br" / "image-bundle" / "bundle-manifest.json").read_bytes()
    assert b"Infinity" not in raw and b"NaN" not in raw, raw

    def _strict(literal):
        raise AssertionError(f"non-JSON literal {literal!r} reached the file: {raw!r}")

    file_doc = json.loads(raw, parse_constant=_strict)
    assert file_doc["hw_info"] == {"a": None, "b": None, "c": None, "soc_flash_mb": "1e400"}


def test_present_helper_firmware_is_copied_and_hashed(tmp_path):
    wbytes(tmp_path / "br" / "gd32_bridge.bin", b"BRIDGEFW")
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: gd32_bridge\n  chip: gd32g553\n  firmware_path: gd32_bridge.bin\n"
        "boot_order: []\n",
    )
    doc = envelope(run_cli(tmp_path, "--format", "json", "--build-root", "br"))
    helper = doc["data"]["helper_mcus"][0]
    assert helper["name"] == "gd32_bridge"
    # `chip`, deliberately -- the retired Python emitted a perpetually-null `role`.
    assert helper["chip"] == "gd32g553"
    assert "role" not in helper
    assert helper["artefact"] == "helper-mcus/gd32_bridge.bin"
    assert helper["size"] == 8
    # A plain copy, so this one IS byte-identical to the oracle's hash.
    assert helper["sha256"] == hashlib.sha256(b"BRIDGEFW").hexdigest()


def test_helper_basename_collision_does_not_overwrite(tmp_path):
    # Zephyr's default layout puts EVERY helper's firmware at `zephyr.bin`, so two
    # helpers routinely flatten to the same destination. Before the fix the second
    # copy overwrote the first and the first's recorded sha256 matched nothing.
    wbytes(tmp_path / "br" / "gd32" / "zephyr.bin", b"GD32FIRMWARE")
    wbytes(tmp_path / "br" / "cc35" / "zephyr.bin", b"CC3501EFIRMWARE")
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: gd32_bridge\n  chip: gd32g553\n  firmware_path: gd32/zephyr.bin\n"
        "- name: cc3501e_otp\n  chip: cc3501e\n  firmware_path: cc35/zephyr.bin\n"
        "boot_order: []\n",
    )
    doc = envelope(run_cli(tmp_path, "--format", "json", "--build-root", "br"))
    helpers = doc["data"]["helper_mcus"]
    assert [h["artefact"] for h in helpers] == [
        "helper-mcus/zephyr.bin",
        "helper-mcus/2-zephyr.bin",
    ]
    bundle_root = tmp_path / "br" / "image-bundle"
    for helper in helpers:
        raw = (bundle_root / helper["artefact"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == helper["sha256"]


def test_re_running_overwrites_the_archive_rather_than_appending(tmp_path):
    wbytes(tmp_path / "br" / "bd" / "zephyr.elf", b"ELFDATA")
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: c\n  os: zephyr\n"
        "  build_dir: bd\n  status: ok\nhelper_mcus: []\nboot_order: []\n",
    )
    first = envelope(run_cli(tmp_path, "--format", "json", "--build-root", "br"))
    second = envelope(run_cli(tmp_path, "--format", "json", "--build-root", "br"))
    archive = tmp_path / "br" / "image-bundle" / "slices" / "c-zephyr.tar.gz"
    with tarfile.open(archive) as tar:
        assert len(tar.getnames()) == 2  # `bd` + `bd/zephyr.elf`, not doubled
    assert second["data"]["slices"][0]["size"] == first["data"]["slices"][0]["size"]


def test_re_running_a_read_only_helper_copy_still_succeeds(tmp_path):
    # `shutil.copy` copies permission bits too, so a read-only source firmware
    # leaves the bundle copy unwritable and the NEXT run fails to overwrite it.
    source = tmp_path / "br" / "fw.bin"
    wbytes(source, b"FW")
    os.chmod(source, 0o444)
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: h\n  chip: c\n  firmware_path: fw.bin\nboot_order: []\n",
    )
    assert run_cli(tmp_path, "--format", "json", "--build-root", "br").returncode == 0
    assert run_cli(tmp_path, "--format", "json", "--build-root", "br").returncode == 0


# ------------------------------------------------- alp-sdk#330: --sdk-root resolution


def test_sdk_relative_helper_firmware_resolves_against_sdk_root(tmp_path):
    # The real fixture shape from alp-sdk#330: cc3501e_otp's firmware_path is
    # repository-relative per som-preset-v1.schema.json, and the SDK genuinely
    # ships the file -- but not under this project's build/.
    sdk = make_sdk_root(tmp_path)
    wbytes(
        sdk / "firmware" / "cc3501e" / "prebuilt" / "cc3501e-v0.2.0.bin",
        b"CC3501E-FIRMWARE",
    )
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: cc3501e_otp\n  chip: cc3501e\n"
        "  firmware_path: firmware/cc3501e/prebuilt/cc3501e-v0.2.0.bin\n"
        "boot_order: []\n",
    )
    result = run_cli(
        tmp_path, "--format", "json", "--build-root", "br", "--sdk-root", str(sdk)
    )
    assert result.returncode == 0
    doc = envelope(result)
    assert doc["ok"] is True
    helpers = doc["data"]["helper_mcus"]
    assert len(helpers) == 1
    assert helpers[0]["name"] == "cc3501e_otp"
    assert helpers[0]["chip"] == "cc3501e"
    assert helpers[0]["artefact"] == "helper-mcus/cc3501e-v0.2.0.bin"
    assert helpers[0]["sha256"] == hashlib.sha256(b"CC3501E-FIRMWARE").hexdigest()
    assert doc["issues"] == []


def test_a_genuinely_absent_helper_firmware_still_refuses_and_names_every_root_tried(
    tmp_path,
):
    sdk = make_sdk_root(tmp_path)
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: cc3501e_otp\n  chip: cc3501e\n"
        "  firmware_path: firmware/cc3501e/prebuilt/cc3501e-v0.2.0.bin\n"
        "boot_order: []\n",
    )
    result = run_cli(
        tmp_path, "--format", "json", "--build-root", "br", "--sdk-root", str(sdk)
    )
    assert result.returncode == 1
    doc = envelope(result)
    assert doc["ok"] is False
    issue = doc["issues"][0]
    assert issue["code"] == "image.helper-missing"
    message = issue["message"]
    # The old message named only the raw relative string; a reporter had to go
    # find the file by hand to prove it existed. Now every root tried, and the
    # absolute path each one produced, is in the message.
    assert "build root" in message
    assert "sdk root" in message
    # Both candidate absolute paths end in the same relative artefact string --
    # the one at the raw manifest path, plus one per root tried.
    assert message.count("cc3501e-v0.2.0.bin") >= 3


def test_absolute_helper_firmware_path_resolves_directly_unchanged(tmp_path):
    outside = tmp_path / "outside" / "fw.bin"
    wbytes(outside, b"OUTSIDE-FW")
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        f"- name: h\n  chip: c\n  firmware_path: {json.dumps(str(outside))}\n"
        "boot_order: []\n",
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 0
    doc = envelope(result)
    helper = doc["data"]["helper_mcus"][0]
    assert helper["sha256"] == hashlib.sha256(b"OUTSIDE-FW").hexdigest()


def test_build_produced_helper_artefact_still_resolves_when_sdk_root_is_also_given(
    tmp_path,
):
    # --sdk-root must not regress the already-working case: a helper artefact
    # this build itself produced, absent from the sdk root entirely.
    sdk = make_sdk_root(tmp_path)
    wbytes(tmp_path / "br" / "gd32_bridge.bin", b"BRIDGEFW")
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: gd32_bridge\n  chip: gd32g553\n  firmware_path: gd32_bridge.bin\n"
        "boot_order: []\n",
    )
    result = run_cli(
        tmp_path, "--format", "json", "--build-root", "br", "--sdk-root", str(sdk)
    )
    assert result.returncode == 0
    helper = envelope(result)["data"]["helper_mcus"][0]
    assert helper["sha256"] == hashlib.sha256(b"BRIDGEFW").hexdigest()


def test_build_root_wins_the_precedence_when_both_roots_have_the_file(tmp_path):
    # The deliberate precedence: build_root first, sdk_root as fallback -- an
    # artefact THIS run built wins over a same-named file the SDK ships.
    sdk = make_sdk_root(tmp_path)
    wbytes(sdk / "fw.bin", b"SDK-COPY")
    wbytes(tmp_path / "br" / "fw.bin", b"BUILD-COPY")
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: h\n  chip: c\n  firmware_path: fw.bin\nboot_order: []\n",
    )
    result = run_cli(
        tmp_path, "--format", "json", "--build-root", "br", "--sdk-root", str(sdk)
    )
    helper = envelope(result)["data"]["helper_mcus"][0]
    assert helper["sha256"] == hashlib.sha256(b"BUILD-COPY").hexdigest()


# ------------------------------------------------------------ notice paths


def test_a_pending_slice_and_a_tbd_helper_yield_an_empty_bundle_at_rc_zero(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-AEN701\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n  build_dir: m55_hp-zephyr\n  status: pending\n"
        "helper_mcus:\n- name: cc3501e_otp\n  chip: cc3501e\n  firmware_path: TBD\n"
        "boot_order: []\n",
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 0
    doc = envelope(result)
    assert doc["data"]["slices"] == []
    assert doc["data"]["helper_mcus"] == []
    # tan-cli#499: a `pending` slice now gets its OWN notice too -- the
    # exclusion that used to report nothing at all. Slice notices are
    # appended before helper notices in `_assemble_bundle`, so it is first.
    assert doc["issues"][0]["code"] == "image.slice-not-built"
    assert doc["issues"][0]["severity"] == "warning"
    assert "m55_hp" in doc["issues"][0]["message"]
    # The `TBD` sentinel is the LEGITIMATE not-yet-built state: a warning, never
    # the error a genuinely missing concrete path gets.
    assert doc["issues"][1]["code"] == "image.helper-skipped"
    assert doc["issues"][1]["severity"] == "warning"
    assert (tmp_path / "br" / "image-bundle" / "bundle-manifest.json").is_file()


def test_a_concrete_missing_helper_firmware_is_a_hard_error(tmp_path):
    # A downstream consumer keying on ok/exit code must not see a "complete" bundle
    # that silently omits a promised artefact.
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: gd32_bridge\n  chip: gd32g553\n  firmware_path: firmware/gd32.bin\n"
        "boot_order: []\n",
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 1
    doc = envelope(result)
    assert doc["ok"] is False
    assert doc["issues"][0]["code"] == "image.helper-missing"
    assert doc["issues"][0]["severity"] == "error"
    assert doc["data"]["helper_mcus"] == []
    # Assembly still completed, so the rest of the bundle is inspectable.
    assert (tmp_path / "br" / "image-bundle" / "bundle-manifest.json").is_file()


def test_an_absent_or_empty_firmware_path_is_a_silent_skip(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: h1\n  chip: c1\n- name: h2\n  chip: c2\n  firmware_path: ''\n"
        "boot_order: []\n",
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 0
    assert envelope(result)["issues"] == []


def test_a_missing_build_dir_is_a_visible_skip_in_json_not_only_in_text(tmp_path):
    # ok:true is still right -- a missing build_dir is a soft skip, not a command
    # failure -- but `issues` used to be hard-coded empty on this branch, so an
    # incomplete bundle was indistinguishable from a complete one in the only mode
    # the extension consumes.
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: m55_hp\n  os: zephyr\n"
        "  build_dir: does-not-exist\n  status: ok\nhelper_mcus: []\nboot_order: []\n",
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["issues"][0]["code"] == "image.slice-skipped"
    assert doc["issues"][0]["severity"] == "warning"


def test_a_slice_with_no_build_dir_key_is_skipped_not_defaulted(tmp_path):
    # `image` deliberately does NOT fall back to `<core>-<os>` the way `size` does:
    # changing which slices get archived is not an off-by-one-directory fix.
    (tmp_path / "br" / "m55_hp-zephyr").mkdir(parents=True)
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: m55_hp\n  os: zephyr\n"
        "  status: ok\nhelper_mcus: []\nboot_order: []\n",
    )
    doc = envelope(run_cli(tmp_path, "--format", "json", "--build-root", "br"))
    assert doc["data"]["slices"] == []
    assert doc["issues"][0]["code"] == "image.slice-skipped"


def test_an_unsafe_core_id_writes_nothing_outside_the_bundle(tmp_path):
    wbytes(tmp_path / "br" / "bd" / "zephyr" / "zephyr.elf", b"ELFDATA")
    canary = tmp_path / "escape-zephyr.tar.gz"
    canary.write_bytes(b"DO NOT TOUCH")
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n"
        '- core_id: "../../escape"\n  os: zephyr\n  build_dir: bd\n  status: ok\n'
        "helper_mcus: []\nboot_order: []\n",
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 0
    doc = envelope(result)
    assert doc["data"]["slices"] == []
    assert doc["issues"][0]["code"] == "image.slice-unsafe-name"
    assert list((tmp_path / "br" / "image-bundle" / "slices").iterdir()) == []
    assert canary.read_bytes() == b"DO NOT TOUCH"


# ------------------------------------------------------------ hostile inputs


def test_missing_manifest_is_a_coded_error_at_exit_one(tmp_path):
    (tmp_path / "br").mkdir()
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "image.manifest-unavailable"
    assert "run `tan build` first." in doc["issues"][0]["message"]
    # The `data` shape stays stable on every failure path.
    assert list(doc["data"]) == [
        "schema_version",
        "generated_by",
        "hw_info",
        "slices",
        "helper_mcus",
        "boot_order",
    ]


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("schema_version: 2\nslices: []\n", "unsupported system-manifest schema_version 2"),
        ("schema_version: 1\nslices: [\n", "not valid YAML"),
        ("slices: []\n", "missing field `schema_version`"),
        ("schema_version: 1\nhelper_mcus:\n- name: x\n", "missing field `chip`"),
        ("just-a-string\n", "expected struct SystemManifest"),
    ],
)
def test_every_malformed_manifest_is_manifest_invalid(tmp_path, document, expected):
    write(tmp_path / "br" / "system-manifest.yaml", document)
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "image.manifest-invalid"
    assert expected in doc["issues"][0]["message"]


def test_non_utf8_manifest_is_unavailable_not_invalid(tmp_path):
    (tmp_path / "br").mkdir()
    (tmp_path / "br" / "system-manifest.yaml").write_bytes(
        b"schema_version: 1\nhw_info:\n  sku: \xff\xfe\nslices: []\n"
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 1
    assert envelope(result)["issues"][0]["code"] == "image.manifest-unavailable"


def test_an_unwritable_bundle_dir_is_write_failure_three(tmp_path):
    # A FILE already sitting where `image-bundle/` must be: mkdir cannot succeed,
    # and the failure is a WriteFailure(3), not a traceback at rc 1.
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus: []\nboot_order: []\n",
    )
    (tmp_path / "br" / "image-bundle").write_text("i am not a directory")
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 3
    doc = envelope(result)
    assert doc["exitCode"] == 3
    assert doc["issues"][0]["code"] == "image.bundle-write-failed"


def test_a_bundle_manifest_that_is_a_directory_is_write_failure_three(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus: []\nboot_order: []\n",
    )
    (tmp_path / "br" / "image-bundle" / "bundle-manifest.json").mkdir(parents=True)
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 3
    assert envelope(result)["issues"][0]["code"] == "image.bundle-write-failed"


def test_a_helper_firmware_that_is_a_directory_is_reported_missing(tmp_path):
    (tmp_path / "br" / "fw.bin").mkdir(parents=True)
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices: []\nhelper_mcus:\n"
        "- name: h\n  chip: c\n  firmware_path: fw.bin\nboot_order: []\n",
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 1
    assert envelope(result)["issues"][0]["code"] == "image.helper-missing"


def test_text_mode_writes_nothing_to_stdout_and_always_names_the_bundle(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: c\n  os: zephyr\n"
        "  build_dir: nope\n  status: ok\nhelper_mcus: []\nboot_order: []\n",
    )
    result = run_cli(tmp_path, "--build-root", "br")
    assert result.returncode == 0
    assert result.stdout == ""
    assert "image: skipping c (build_dir missing)" in result.stderr
    assert "image: bundle ready at" in result.stderr
