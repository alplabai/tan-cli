# SPDX-License-Identifier: Apache-2.0
"""`tan size` end to end: the envelope, the exit ladder, the text table, and the
hostile inputs.

`size` has NO committed conformance fixture -- `contract/README.md` records it in
neither the frozen list nor the stated-uncovered rows (tan-cli#200) -- so this
file plus `tests/parity/test_image_size_oracle.py` are the whole gate. Every
expectation below was diffed against the compiled Rust binary; the parity file
re-runs that comparison whenever the binary is present.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def run_cli(cwd, *argv, env_extra=None):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
        **(env_extra or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", "size", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
        timeout=180,
    )


def envelope(result):
    """The ONE JSON document on stdout, and nothing else. A stray byte here is
    the break the whole port exists to prevent: the extension parses stdout whole
    and renders nothing at all, with no error, when it cannot."""
    assert result.stdout.count("\n") == 1, result.stdout
    return json.loads(result.stdout)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def fake_sdk(root: Path, sku: str, soc: str) -> None:
    write(root / "scripts" / "alp_project.py", "")
    write(
        root / "metadata" / "e1m_modules" / f"{sku}.yaml",
        f"schema_version: 1\nsku: {sku}\nsilicon: test:fam:part\n",
    )
    write(root / "metadata" / "socs" / "test" / "fam" / "part.json", soc)


def footprint_project(root: Path, sku: str, rom: int, ram: int, soc: str) -> None:
    fake_sdk(root / "sdk", sku, soc)
    write(
        root / "br" / "system-manifest.yaml",
        f"schema_version: 1\nhw_info:\n  sku: {sku}\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n",
    )
    write(root / "br" / "m55_hp-zephyr" / "rom.json", '{"symbols":{"size":%d}}' % rom)
    write(root / "br" / "m55_hp-zephyr" / "ram.json", '{"symbols":{"size":%d}}' % ram)


SOC_5M5 = '{"soc_flash_mb": 5.5, "cores": [{"id": "m55_hp", "tcm_kb": 1280}]}'


# --------------------------------------------------------------- happy paths


def test_measured_slice_reports_a_full_row(tmp_path):
    footprint_project(tmp_path, "E1M-TEST", 4096, 2048, SOC_5M5)
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br", "--sdk-root", "sdk")
    assert result.returncode == 0
    doc = envelope(result)
    assert doc["command"] == "size"
    assert doc["ok"] is True
    assert doc["exitCode"] == 0
    assert doc["data"]["schema"] == "alp-size/1"
    row = doc["data"]["slices"][0]
    assert row["core_id"] == "m55_hp"
    assert row["status"] == "ok"
    assert row["source"] == "rom/ram.json"
    assert row["flash"] == {"used": 4096, "total": 5_767_168, "pct": 0.1}
    assert row["budget_note"] == "flash=soc_flash_mb; ram=core tcm_kb (ITCM+DTCM)"
    assert doc["issues"] == []


def test_sdk_root_is_reported_forward_slashed_and_never_null(tmp_path):
    footprint_project(tmp_path, "E1M-TEST", 4096, 2048, SOC_5M5)
    doc = envelope(
        run_cli(tmp_path, "--format", "json", "--build-root", "br", "--sdk-root", "sdk")
    )
    # `sdk.root` is the raw `--sdk-root` value, posix-normalised at the one shared
    # seam (`SdkInfo.as_dict`) -- NEVER the platform-native form, and never made
    # absolute. Asserting the native spelling here is a mistake that already
    # shipped once in this port.
    assert doc["sdk"] == {"root": "sdk", "sourceTier": "sdkRootFlag"}
    assert "/" in doc["project"]["root"] and "\\" not in doc["project"]["root"]


def test_sdk_key_is_absent_not_null_without_a_checkout(tmp_path):
    write(tmp_path / "br" / "system-manifest.yaml", "schema_version: 1\nslices: []\n")
    doc = envelope(run_cli(tmp_path, "--format", "json", "--build-root", "br"))
    assert "sdk" not in doc


def test_sdk_key_is_absent_when_sdk_root_is_not_a_checkout(tmp_path):
    # `--sdk-root` is terminal (I-31): a bad path resolves to NOTHING rather than
    # falling through to some other checkout, and the envelope must not advertise
    # a path no command could use.
    (tmp_path / "notsdk").mkdir()
    footprint_project(tmp_path, "E1M-TEST", 4096, 2048, SOC_5M5)
    doc = envelope(
        run_cli(tmp_path, "--format", "json", "--build-root", "br", "--sdk-root", "notsdk")
    )
    assert "sdk" not in doc
    assert doc["data"]["slices"][0]["budget_note"] == "no SoM preset for E1M-TEST"


def test_board_overrides_the_manifest_sku(tmp_path):
    footprint_project(tmp_path, "E1M-TEST", 4096, 2048, SOC_5M5)
    fake_sdk(tmp_path / "sdk", "E1M-OTHER",
             '{"soc_flash_mb": 2.0, "cores": [{"id": "m55_hp", "tcm_kb": 512}]}')
    doc = envelope(
        run_cli(tmp_path, "--format", "json", "--build-root", "br", "--sdk-root", "sdk",
                "--board", "E1M-OTHER")
    )
    assert doc["data"]["slices"][0]["flash"]["total"] == 2 * 1024 * 1024


def test_non_zephyr_slice_is_n_a_and_never_measured(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: a32_cluster\n  os: yocto\n",
    )
    doc = envelope(run_cli(tmp_path, "--format", "json", "--build-root", "br"))
    row = doc["data"]["slices"][0]
    assert row["status"] == "n/a"
    assert row["budget_note"] == "no Zephyr image (Yocto/baremetal)"
    # `n/a` never counts as an unknown budget: it has no image to measure.
    assert doc["data"]["summary"]["unknown_budget"] == []


def test_format_json_is_accepted_before_the_subcommand(tmp_path):
    # clap's `--format` is `global = true`, so the extension may put it either
    # side of the command name.
    write(tmp_path / "br" / "system-manifest.yaml", "schema_version: 1\nslices: []\n")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    result = subprocess.run(
        [sys.executable, "-m", "tan", "--format", "json", "size", "--build-root", "br"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(tmp_path), env=env,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["command"] == "size"


# ------------------------------------------------------------ fail-over-budget


def test_fail_over_budget_exits_one_when_over(tmp_path):
    footprint_project(
        tmp_path, "E1M-TEST", 100_000, 100_000,
        '{"soc_flash_mb": 0.0001, "cores": [{"id": "m55_hp", "tcm_kb": 1280}]}',
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br",
                     "--sdk-root", "sdk", "--fail-over-budget")
    assert result.returncode == 1
    doc = envelope(result)
    assert doc["ok"] is False
    assert doc["issues"][0]["code"] == "size.over-budget"
    assert doc["issues"][0]["message"] == "size: over budget: [m55_hp]."
    assert doc["data"]["summary"]["over_budget"] == ["m55_hp"]


def test_fail_over_budget_skips_an_unknown_budget_and_says_so(tmp_path):
    # Never guessed: a slice whose budget did not resolve is reported and skipped,
    # at exit 0.
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info:\n  sku: E1M-NOPRESET\nslices:\n"
        "- core_id: m55_hp\n  os: zephyr\n",
    )
    write(tmp_path / "br" / "m55_hp-zephyr" / "rom.json", '{"symbols":{"size":4096}}')
    write(tmp_path / "br" / "m55_hp-zephyr" / "ram.json", '{"symbols":{"size":2048}}')
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br",
                     "--fail-over-budget")
    assert result.returncode == 0
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "size.budget-unknown"
    assert doc["issues"][0]["severity"] == "info"
    assert doc["data"]["summary"]["unknown_budget"] == ["m55_hp"]


def test_without_fail_over_budget_no_issue_is_emitted_at_all(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: m55_hp\n  os: zephyr\n",
    )
    write(tmp_path / "br" / "m55_hp-zephyr" / "rom.json", '{"symbols":{"size":4096}}')
    write(tmp_path / "br" / "m55_hp-zephyr" / "ram.json", '{"symbols":{"size":2048}}')
    doc = envelope(run_cli(tmp_path, "--format", "json", "--build-root", "br"))
    assert doc["issues"] == []
    assert doc["data"]["summary"]["unknown_budget"] == ["m55_hp"]


# ----------------------------------------------------------------- I-18


def test_i18_nested_west_output_is_measured_not_reported_not_built(tmp_path):
    """DELIBERATE DIVERGENCE from the oracle, pinned here so it cannot regress
    silently in either direction.

    `west build` is emitted with NO `-d` (I-18), so its tree lands at
    `<build_dir>/build/`. The shipped binary reconciles that at BUILD time --
    `resolve_zephyr_artefact` records the nested paths into the manifest it
    rewrites -- and this port's `tan build` does not write that manifest yet, so
    against a plan-time manifest the oracle reports every such slice `not-built`.
    Reading the nesting here is the whole point: the alternative is `tan size`
    answering "not built" about firmware that is sitting on disk.
    """
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: m55_hp\n  os: zephyr\n",
    )
    nested = tmp_path / "br" / "m55_hp-zephyr" / "build"
    write(nested / "rom.json", '{"symbols":{"size":4096}}')
    write(nested / "ram.json", '{"symbols":{"size":2048}}')
    doc = envelope(run_cli(tmp_path, "--format", "json", "--build-root", "br"))
    row = doc["data"]["slices"][0]
    assert row["status"] == "no-budget"
    assert row["source"] == "rom/ram.json"
    assert row["flash"]["used"] == 4096


def test_the_un_nested_path_still_wins_when_both_exist(tmp_path):
    # The ordering is what guarantees that every input the oracle measures is
    # measured identically here.
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: m55_hp\n  os: zephyr\n",
    )
    plain = tmp_path / "br" / "m55_hp-zephyr"
    write(plain / "rom.json", '{"symbols":{"size":11}}')
    write(plain / "ram.json", '{"symbols":{"size":22}}')
    write(plain / "build" / "rom.json", '{"symbols":{"size":33}}')
    write(plain / "build" / "ram.json", '{"symbols":{"size":44}}')
    doc = envelope(run_cli(tmp_path, "--format", "json", "--build-root", "br"))
    assert doc["data"]["slices"][0]["flash"]["used"] == 11


# ------------------------------------------------------------- hostile inputs


def test_missing_manifest_is_a_coded_error_at_exit_one(tmp_path):
    (tmp_path / "br").mkdir()
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "size.manifest-unavailable"
    assert "run `tan build` first" in doc["issues"][0]["message"]
    # The shape stays stable even on the error path.
    assert doc["data"]["schema"] == "alp-size/1"
    assert doc["data"]["slices"] == []


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("schema_version: 2\nslices: []\n", "unsupported system-manifest schema_version 2"),
        ("schema_version: 1\nslices: [\n", "not valid YAML"),
        ("slices: []\n", "missing field `schema_version`"),
        ("schema_version: 1\nslices:\n- os: zephyr\n", "missing field `core_id`"),
        ("just-a-string\n", "expected struct SystemManifest"),
    ],
)
def test_every_malformed_manifest_is_manifest_invalid(tmp_path, document, expected):
    write(tmp_path / "br" / "system-manifest.yaml", document)
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "size.manifest-invalid"
    assert expected in doc["issues"][0]["message"]


def test_non_utf8_manifest_is_unavailable_not_invalid(tmp_path):
    # Rust's `read_to_string` fails with an io error, so the parser is never
    # reached and the code is `*-unavailable`. Python's UnicodeDecodeError is a
    # ValueError, not an OSError, so catching only OSError here would report a bad
    # INPUT as a tan bug at exit 5.
    (tmp_path / "br").mkdir()
    (tmp_path / "br" / "system-manifest.yaml").write_bytes(
        b"schema_version: 1\nhw_info:\n  sku: \xff\xfe\nslices: []\n"
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 1
    assert envelope(result)["issues"][0]["code"] == "size.manifest-unavailable"


def test_manifest_path_that_is_a_directory_is_unavailable(tmp_path):
    (tmp_path / "br" / "system-manifest.yaml").mkdir(parents=True)
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 1
    assert envelope(result)["issues"][0]["code"] == "size.manifest-unavailable"


def test_unreadable_footprint_and_elf_degrade_to_not_built(tmp_path):
    # A build_dir whose rom.json is a DIRECTORY, and an output_artefact that is
    # one too: both are "no measurement", never an exception.
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n"
        "- core_id: a\n  os: zephyr\n  build_dir: a\n"
        "- core_id: b\n  os: zephyr\n  output_artefact: bdir\n",
    )
    (tmp_path / "br" / "a" / "rom.json").mkdir(parents=True)
    (tmp_path / "br" / "bdir").mkdir(parents=True)
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 0
    doc = envelope(result)
    assert [r["status"] for r in doc["data"]["slices"]] == ["not-built", "not-built"]


def test_a_directory_named_zephyr_elf_does_not_crash_the_size_tool_rung(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n- core_id: c\n  os: zephyr\n",
    )
    (tmp_path / "br" / "c-zephyr" / "zephyr" / "zephyr.elf").mkdir(parents=True)
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 0
    assert envelope(result)["data"]["slices"][0]["status"] == "not-built"


def test_a_size_tool_that_is_a_directory_falls_through_to_the_next_rung(tmp_path):
    # A PATH entry holding a DIRECTORY named `size` (or `size.exe`): spawning it
    # raises, and the middle ELF rung must answer instead of the command dying.
    from tests.core.test_size_model import make_elf

    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n"
        "- core_id: c\n  os: zephyr\n  output_artefact: z.elf\n",
    )
    (tmp_path / "br" / "z.elf").write_bytes(make_elf())
    fake_path = tmp_path / "fakebin"
    for name in ("size", "size.exe", "size.EXE"):
        (fake_path / name).mkdir(parents=True, exist_ok=True)
    result = run_cli(
        tmp_path, "--format", "json", "--build-root", "br",
        env_extra={"PATH": str(fake_path)},
    )
    assert result.returncode == 0
    row = envelope(result)["data"]["slices"][0]
    assert row["source"] == "pyelftools"
    assert row["flash"]["used"] == 160


def test_a_size_tool_printing_garbage_falls_through_to_the_next_rung(tmp_path):
    from tests.core.test_size_model import make_elf

    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n"
        "- core_id: c\n  os: zephyr\n  output_artefact: z.elf\n",
    )
    (tmp_path / "br" / "z.elf").write_bytes(make_elf())
    fake_path = tmp_path / "garbagebin"
    fake_path.mkdir()
    script = fake_path / ("size.bat" if os.name == "nt" else "size")
    if os.name == "nt":
        script.write_text("@echo off\r\necho not berkeley output at all\r\n")
    else:
        script.write_text("#!/bin/sh\necho not berkeley output at all\n")
        script.chmod(0o755)
    result = run_cli(
        tmp_path, "--format", "json", "--build-root", "br",
        env_extra={"PATH": str(fake_path), "PATHEXT": ".BAT;.EXE;.CMD"},
    )
    assert result.returncode == 0
    assert envelope(result)["data"]["slices"][0]["source"] == "pyelftools"


def test_no_size_tool_on_path_says_so_in_text_mode(tmp_path):
    from tests.core.test_size_model import make_elf

    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n"
        "- core_id: c\n  os: zephyr\n  output_artefact: z.elf\n",
    )
    (tmp_path / "br" / "z.elf").write_bytes(make_elf())
    empty = tmp_path / "emptybin"
    empty.mkdir()
    result = run_cli(tmp_path, "--build-root", "br", "--no-color",
                     env_extra={"PATH": str(empty)})
    assert result.returncode == 0
    assert "no size tool on PATH" in result.stderr
    assert result.stdout == ""  # stdout is the envelope channel; text mode writes none


def test_a_tool_in_the_current_directory_is_never_resolved(tmp_path, monkeypatch):
    """A `;;`/trailing-`;` PATH (routine on Windows) yields an EMPTY entry, and
    joining onto it resolves against the process CWD -- so a project checked out
    with its own `size.exe` at its root would be reported available and then
    SPAWNED. The oracle walks PATH by hand for exactly this reason, and
    `shutil.which` reintroduces it on Windows: it always inserts `os.curdir` ahead
    of the search list, even when given an explicit `path=`."""
    from tan.commands.size_cmd import _find_on_path

    marker = "tan-size-cwd-probe"
    for name in (marker, f"{marker}.exe", f"{marker}.EXE", f"{marker}.bat"):
        path = tmp_path / name
        path.write_text("")
        path.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["", str(tmp_path / "nope"), ""]))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    assert _find_on_path(marker) is False
    # ...and it IS found once the directory is named on PATH, so the negative
    # above is about the CWD, not about the probe being broken.
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _find_on_path(marker) is True


def test_an_unset_path_resolves_nothing_rather_than_raising(monkeypatch):
    from tan.commands.size_cmd import _find_on_path

    monkeypatch.delenv("PATH", raising=False)
    assert _find_on_path("size") is False


def test_absurd_core_id_and_os_values_do_not_crash(tmp_path):
    write(
        tmp_path / "br" / "system-manifest.yaml",
        "schema_version: 1\nhw_info: {}\nslices:\n"
        '- core_id: ""\n  os: zephyr\n'
        "- core_id: with space\n  os: zephyr\n"
        '- core_id: "m55/hp"\n  os: zephyr\n'
        '- core_id: "../../escape"\n  os: zephyr\n',
    )
    result = run_cli(tmp_path, "--format", "json", "--build-root", "br")
    assert result.returncode == 0
    assert len(envelope(result)["data"]["slices"]) == 4
