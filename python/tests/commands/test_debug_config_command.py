# SPDX-License-Identifier: Apache-2.0
"""`tan debug-config`: the write path, the resolution overlay, and the error
contract. The four `--preview` goldens are covered by
`tests/conformance/test_contract_envelopes.py`; everything here is the half of
the command no fixture reaches -- because reaching it means writing a file.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer

from tan.commands.debug_config_cmd import _resolve_from_build, _select_slice
from tan.core.debug_launch import (
    GDBSERVER,
    JLINK,
    NATIVE_HOST,
    YOCTO_USERSPACE,
    ZEPHYR_MCU,
    LaunchResolution,
    apply_launch_resolution,
    create_launch_draft,
    create_launch_json_write_plan,
    fill_debug_probe_identity_gaps,
    strip_jsonc,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

#: `configurations: []` collapsed onto one line with a `//` comment above it --
#: VS Code's own stock template, the single most common real input.
STOCK_TEMPLATE = (
    "{\n"
    "  // Use IntelliSense to learn about possible attributes.\n"
    '  "version": "0.2.0",\n'
    '  "configurations": []\n'
    "}\n"
)


def run_cli(cwd, *argv):
    env = {
        **os.environ,
        "SOURCE_DATE_EPOCH": "0",
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", "debug-config", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
    )


def envelope(proc):
    """The one JSON document on stdout. Asserts stdout carries NOTHING else: a
    stray byte makes the extension render an empty panel with no error."""
    assert proc.stderr.strip() == "", proc.stderr
    return json.loads(proc.stdout)


def launch_json(root):
    return Path(root, ".vscode", "launch.json")


def test_a_write_into_the_stock_template_keeps_every_byte_outside_the_entry(tmp_path):
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(STOCK_TEMPLATE, encoding="utf-8")

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json"))

    assert env["exitCode"] == 0 and env["data"]["replaced"] is False
    after = launch_json(tmp_path).read_text(encoding="utf-8")
    # The splice is the whole point of tan-cli#182: the comment sits outside the
    # entry's own span, so nothing may touch it.
    assert "// Use IntelliSense" in after
    # …and the file still parses once the comment is stripped, i.e. the splice
    # produced valid JSONC rather than merely leaving the comment somewhere.
    parsed = json.loads(strip_jsonc(after))
    assert parsed["configurations"][0]["name"] == "Alp: Zephyr Debug (J-Link)"
    assert env["issues"] == [], "a fresh append destroys nothing, so it reports nothing"


def test_a_semantic_no_op_rerun_leaves_the_file_byte_identical(tmp_path):
    """tan-cli#182 review finding #1. The extension re-runs `debug-config` on
    every session, so "nothing changed" is the COMMON case; re-splicing the entry
    into itself would reformat it and discard its comments every time."""
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(STOCK_TEMPLATE, encoding="utf-8")
    run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json")

    before = launch_json(tmp_path).read_bytes()
    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json"))

    assert env["exitCode"] == 0 and env["data"]["replaced"] is True
    assert launch_json(tmp_path).read_bytes() == before
    assert env["issues"] == []


def test_a_bom_and_crlf_authored_file_keeps_both(tmp_path):
    """tan-cli#182 review finding #4, and the Python-specific trap under it: the
    default universal-newlines READ translates every `\\r\\n` to `\\n` before the
    splice sees the text, so the dominant-EOL check finds no CRLF and re-writes a
    Windows-authored launch.json LF-only -- a whole-file diff on every run."""
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_bytes(
        '\ufeff{\r\n  // keep me\r\n  "version": "0.2.0",\r\n'
        '  "configurations": []\r\n}\r\n'.encode()
    )

    env = envelope(run_cli(tmp_path, "--target-kind", NATIVE_HOST, "--format", "json"))

    assert env["exitCode"] == 0
    raw = launch_json(tmp_path).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "the BOM must survive"
    assert b"keep me" in raw
    assert raw.replace(b"\r\n", b"") .count(b"\n") == 0, "no bare LF may be introduced"


def test_a_hand_filled_value_survives_a_rerun_and_is_reported(tmp_path):
    """#105 (data loss on the customer's own file) and tan-cli#180 (the envelope
    reported the pre-merge draft, so a fixed file read as still unresolved)."""
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "configurations": [
                    {
                        "name": "Alp: Zephyr Debug (J-Link)",
                        "type": "cortex-debug",
                        "servertype": "jlink",
                        "device": "AE822F4M55_HP",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json"))

    assert env["data"]["configuration"]["device"] == "AE822F4M55_HP"
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["configurations"][0]["device"] == "AE822F4M55_HP"


def test_a_legacy_entry_is_migrated_and_the_migration_is_reported(tmp_path):
    """#133 reopened: the #155 rename orphaned every `"ALP: ..."` entry, which is
    exactly where a customer's hand-resolved `device` lived."""
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "configurations": [
                    {
                        "name": "ALP: Zephyr Debug (J-Link)",
                        "type": "cortex-debug",
                        "servertype": "jlink",
                        "device": "AE822F4M55_HP",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json"))

    assert env["data"]["configuration"]["name"] == "Alp: Zephyr Debug (J-Link)"
    assert env["data"]["configuration"]["device"] == "AE822F4M55_HP"
    assert [(i["code"], i["severity"]) for i in env["issues"]] == [
        ("debug-config.legacy-entry-migrated", "info")
    ]
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert len(on_disk["configurations"]) == 1, "adopted in place, not duplicated"


def test_no_migration_is_reported_when_no_legacy_entry_exists(tmp_path):
    """The failing-case pairing: a version that unconditionally attaches the
    migration issue would pass the test above on its own."""
    env = envelope(run_cli(tmp_path, "--target-kind", NATIVE_HOST, "--format", "json"))
    assert env["issues"] == []


def test_a_leftover_legacy_entry_beside_the_maintained_one_is_reported(tmp_path):
    """tan-cli#179: the ordinary same-name merge leaves the legacy entry
    untouched -- deliberately, nothing decides which of two hand-edited entries
    is authoritative -- but silence there is the #133 symptom the customer hits
    next."""
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "configurations": [
                    {"name": "Alp: Zephyr Debug (J-Link)", "device": "<resolved-device>"},
                    {"name": "ALP: Zephyr Debug (J-Link)", "device": "AE822F4M55_HP"},
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json"))

    codes = [i["code"] for i in env["issues"]]
    assert "debug-config.legacy-entry-untouched" in codes
    assert "debug-config.legacy-entry-migrated" not in codes
    assert "ALP: Zephyr Debug (J-Link)" in env["issues"][codes.index(
        "debug-config.legacy-entry-untouched"
    )]["message"]


def test_a_dropped_comment_inside_the_edited_entry_is_disclosed(tmp_path):
    """tan-cli#182 review finding #2: reporting unqualified success on a write
    that destroyed user-authored content is the one thing never acceptable."""
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(
        "{\n"
        '  "version": "0.2.0",\n'
        '  "configurations": [\n'
        "    {\n"
        '      "name": "Alp: Zephyr Debug (J-Link)",\n'
        '      "type": "cortex-debug",\n'
        "      // hand-picked after bring-up\n"
        '      "servertype": "jlink",\n'
        '      "device": "OLD_DEVICE"\n'
        "    }\n"
        "  ]\n"
        "}\n",
        encoding="utf-8",
    )

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json"))

    assert env["exitCode"] == 0
    dropped = [i for i in env["issues"] if i["code"] == "debug-config.comments-dropped"]
    assert dropped and dropped[0]["severity"] == "info"
    after = launch_json(tmp_path).read_text(encoding="utf-8")
    assert "hand-picked after bring-up" not in after, (
        "the fixture must actually have dropped the comment for this to prove anything"
    )


def test_an_unreadable_existing_launch_json_refuses_to_write(tmp_path):
    """The data-loss regression: a READ error on an EXISTING file (non-UTF-8
    bytes, as PowerShell `>` redirection produces) once collapsed into the same
    `None` as "no file yet", and the write then overwrote every hand-written
    configuration wholesale at exit 0."""
    launch_json(tmp_path).parent.mkdir()
    not_utf8 = bytes([0xFF, 0xFE, 0x7B, 0x00, 0x7D, 0x00])
    launch_json(tmp_path).write_bytes(not_utf8)

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json"))

    assert env["exitCode"] == 5
    assert env["issues"][0]["code"] == "debug-config.internal-failure"
    assert launch_json(tmp_path).read_bytes() == not_utf8


def test_a_malformed_existing_launch_json_refuses_to_write(tmp_path):
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text("{ this is not json", encoding="utf-8")

    env = envelope(run_cli(tmp_path, "--target-kind", NATIVE_HOST, "--format", "json"))

    assert env["exitCode"] == 5
    assert "not valid JSON" in env["issues"][0]["message"]
    assert launch_json(tmp_path).read_text(encoding="utf-8") == "{ this is not json"


@pytest.mark.parametrize(
    "argv",
    [
        ("--target-kind", "bogus-kind"),
        ("--target-kind", ZEPHYR_MCU, "--server", "bogus-server"),
        # A legal server for the wrong target class: gdbserver is yocto-only.
        ("--target-kind", ZEPHYR_MCU, "--server", GDBSERVER),
    ],
)
def test_a_refused_selector_is_a_coded_envelope_at_exit_5(tmp_path, argv):
    env = envelope(run_cli(tmp_path, *argv, "--format", "json"))

    assert env["exitCode"] == 5 and env["ok"] is False
    assert env["issues"][0]["code"] == "debug-config.internal-failure"
    # The TS catch block never learned what was asked for, so the payload
    # reports the zephyr-mcu/none placeholder and a NULL configuration.
    assert env["data"]["targetKind"] == ZEPHYR_MCU and env["data"]["server"] == "none"
    assert env["data"]["configuration"] is None
    assert env["project"] == {"root": None, "boardYaml": None}
    assert not launch_json(tmp_path).exists()


def test_an_svd_path_that_cannot_be_read_fails_instead_of_writing(tmp_path):
    """Falling back to "no SVD" would make a typo indistinguishable from not
    passing the flag, and the failure would surface as an unexplained empty
    peripheral view."""
    env = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                "--svd", str(tmp_path / "nope.svd"), "--format", "json")
    )

    assert env["exitCode"] == 5
    assert "alp-sdk#948" in env["issues"][0]["message"]
    assert not launch_json(tmp_path).exists()


def test_an_svd_inside_the_project_is_emitted_workspace_relative(tmp_path):
    (tmp_path / "E8.svd").write_text("<device/>", encoding="utf-8")

    env = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                "--svd", str(tmp_path / "E8.svd"), "--preview", "--format", "json")
    )

    config = env["data"]["configuration"]
    # Both keys, because cortex-debug has spelled it both ways across versions.
    assert config["svdFile"] == "${workspaceFolder}/E8.svd"
    assert config["svdPath"] == "${workspaceFolder}/E8.svd"


def test_svd_on_a_target_kind_without_the_field_says_so(tmp_path):
    (tmp_path / "E8.svd").write_text("<device/>", encoding="utf-8")

    env = envelope(
        run_cli(tmp_path, "--target-kind", NATIVE_HOST,
                "--svd", str(tmp_path / "E8.svd"), "--preview", "--format", "json")
    )

    assert "svdFile" not in env["data"]["configuration"]
    assert any("--svd was given" in n for n in env["data"]["notes"]), (
        "accepting --svd here in silence is the no-op this note exists to prevent"
    )


def test_text_mode_writes_nothing_to_stdout(tmp_path):
    """stdout is the envelope channel in BOTH modes; the human preview is stderr."""
    proc = run_cli(tmp_path, "--target-kind", NATIVE_HOST, "--preview")

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "debug-config: preview target=native-host server=none" in proc.stderr


def test_a_bad_format_is_a_usage_error_not_a_traceback(tmp_path):
    proc = run_cli(tmp_path, "--target-kind", NATIVE_HOST, "--format", "yaml")

    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr


def test_a_command_that_ignores_the_root_format_refuses_it_rather_than_dropping_it(
    tmp_path,
):
    """`--format` is accepted before the subcommand for `debug-config` (four
    goldens invoke it that way, mirroring clap's `global = true`). A command that
    does not yet read it must REFUSE the pre-subcommand position: accepting it
    and running in text mode means exit 0 with nothing on stdout, which is an
    envelope-less `--format json` run -- the break this whole port guards."""
    proc = subprocess.run(
        [sys.executable, "-m", "tan", "--format", "json", "validate", "--offline"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
            ),
        },
    )

    assert proc.returncode == 2
    assert json.loads(proc.stdout)["issues"][0]["code"] == "cli.parse-error"


def test_no_os_or_backend_flag_exists(tmp_path):
    """The OS is derived from each core's Cortex class and is never selectable."""
    for flag in ("--os", "--backend"):
        proc = run_cli(tmp_path, flag, "zephyr", "--preview")
        assert proc.returncode == 2, f"{flag} must not be accepted"


def test_an_unexpected_exception_is_still_a_coded_envelope(monkeypatch, capsys):
    """The recurring break this port's guard exists for: an escaping traceback
    puts nothing parseable on stdout and the extension renders an empty panel
    with no error at all. Driven in-process because there is no argv that makes
    the command throw -- which is the point: the guard covers the failures nobody
    predicted."""
    import types

    from tan.commands import debug_config_cmd

    def boom(**_kwargs):
        raise RuntimeError("planted")

    monkeypatch.setattr(debug_config_cmd, "_run", boom)

    with pytest.raises(typer.Exit) as exit_info:
        debug_config_cmd.debug_config(
            types.SimpleNamespace(obj=None),
            target_kind=None,
            server=None,
            core=None,
            pre_launch_task=None,
            gdbserver_address=None,
            svd=None,
            preview=False,
            project=None,
            board_yaml=None,
            sdk_root=None,
            output_format="json",
            quiet=False,
        )

    assert exit_info.value.exit_code == 5
    env = json.loads(capsys.readouterr().out)
    assert env["issues"][0]["code"] == "debug-config.internal-failure"
    assert "planted" in env["issues"][0]["message"]
    assert env["data"]["configuration"] is None


def test_the_placeholder_note_survives_an_unresolved_host_port():
    """A yocto draft whose `<resolved-gdb>` DID resolve has no `<resolved-`
    string left, so a prefix-only predicate dropped the "still needs resolution"
    note while `miDebuggerServerAddress` was still the unusable `<host>:<port>`
    -- the note goes silent on exactly the config that cannot launch."""
    from tan.commands.debug_config_cmd import _preview_notes_for

    draft = create_launch_draft(YOCTO_USERSPACE, GDBSERVER, None)
    apply_launch_resolution(draft, LaunchResolution(gdb_path="/opt/gdb/bin/aarch64-poky-linux-gdb"))

    assert draft["miDebuggerServerAddress"] == "<host>:<port>"
    notes = _preview_notes_for(draft, [], GDBSERVER)
    assert any(n.startswith("Placeholder fields") for n in notes)


MANIFEST_MCU_THEN_NATIVE_SIM = """\
schema_version: 1
hw_info:
  sku: E1M-AEN701
slices:
- core_id: m55_hp
  os: zephyr
  board: alp_e1m_aen701_m55_hp
  status: ok
  output_artefact: {root}/build/m55_hp-zephyr/build/zephyr/zephyr.elf
- core_id: native_sim
  os: zephyr
  board: native_sim/native/64
  status: ok
  output_artefact: {root}/build/native_sim-zephyr/build/zephyr/zephyr.elf
ipc: []
helper_mcus: []
boot_order: []
"""


def write_manifest(root, yaml_text):
    build = Path(root, "build")
    build.mkdir(parents=True, exist_ok=True)
    build.joinpath("system-manifest.yaml").write_text(yaml_text, encoding="utf-8")


def test_native_host_resolves_the_native_sim_slice_not_the_first_zephyr_one(tmp_path):
    """#83: the old `os`-keyed match took the first `os: zephyr` slice, which on
    a mixed board is the MCU one -- pointing `Alp: Native Sim Debug` at a
    Cortex-M ELF. And the manifest records `zephyr.elf` for every zephyr slice
    (tan has no `.exe` branch), so `program` must be the sibling `zephyr.exe`."""
    pytest.importorskip("yaml")
    root = str(tmp_path).replace("\\", "/")
    write_manifest(tmp_path, MANIFEST_MCU_THEN_NATIVE_SIM.format(root=root))

    resolution, runners, _core_id = _resolve_from_build(root, NATIVE_HOST, "none", None)

    assert resolution.executable == (
        "${workspaceFolder}/build/native_sim-zephyr/build/zephyr/zephyr.exe"
    )
    assert runners == []


def test_zephyr_mcu_resolution_is_unchanged_by_the_native_host_rule(tmp_path):
    pytest.importorskip("yaml")
    root = str(tmp_path).replace("\\", "/")
    write_manifest(tmp_path, MANIFEST_MCU_THEN_NATIVE_SIM.format(root=root))

    bare, _, _ = _resolve_from_build(root, ZEPHYR_MCU, JLINK, None)
    pinned, _, _ = _resolve_from_build(root, ZEPHYR_MCU, JLINK, "m55_hp")

    expected = "${workspaceFolder}/build/m55_hp-zephyr/build/zephyr/zephyr.elf"
    assert bare.executable == expected and pinned.executable == expected


def test_a_manifest_with_no_native_sim_slice_resolves_nothing(tmp_path):
    """It must resolve NO executable rather than adopt the MCU ELF; the draft
    keeps its own placeholder `program`."""
    pytest.importorskip("yaml")
    root = str(tmp_path).replace("\\", "/")
    write_manifest(
        tmp_path,
        "schema_version: 1\nslices:\n- core_id: m55_hp\n  os: zephyr\n"
        f"  board: alp_e1m_aen701_m55_hp\n  output_artefact: {root}/b/zephyr.elf\n",
    )

    resolution, _, _ = _resolve_from_build(root, NATIVE_HOST, "none", None)

    assert resolution.executable is None


def test_a_wrong_schema_major_resolves_nothing_rather_than_being_misread(tmp_path):
    pytest.importorskip("yaml")
    root = str(tmp_path).replace("\\", "/")
    write_manifest(
        tmp_path,
        f"schema_version: 2\nslices:\n- core_id: m55_hp\n  os: zephyr\n"
        f"  output_artefact: {root}/b/zephyr.elf\n",
    )

    resolution, _, _ = _resolve_from_build(root, ZEPHYR_MCU, JLINK, None)

    assert resolution.executable is None


def test_a_missing_manifest_leaves_the_draft_untouched(tmp_path):
    """`debug-config` must still emit its draft before the first build."""
    resolution, runners, core_id = _resolve_from_build(str(tmp_path), ZEPHYR_MCU, JLINK, None)

    assert resolution == LaunchResolution() and runners == [] and core_id is None


def test_runners_yaml_fills_the_device_and_gdb_a_build_can_answer(tmp_path):
    pytest.importorskip("yaml")
    root = str(tmp_path).replace("\\", "/")
    build_dir = f"{root}/build/m55_hp-zephyr/build"
    write_manifest(
        tmp_path,
        "schema_version: 1\nslices:\n- core_id: m55_hp\n  os: zephyr\n"
        f"  board: alp_x\n  build_dir: {build_dir}\n"
        f"  output_artefact: {build_dir}/zephyr/zephyr.elf\n",
    )
    zephyr_dir = Path(build_dir, "zephyr")
    zephyr_dir.mkdir(parents=True)
    zephyr_dir.joinpath("runners.yaml").write_text(
        "runners:\n- jlink\n- openocd\n"
        "config:\n  gdb: /zephyr-sdk/arm-zephyr-eabi-gdb\n"
        "args:\n  jlink:\n  - --device=AE822F4M55_HP\n",
        encoding="utf-8",
    )

    resolution, runners, core_id = _resolve_from_build(root, ZEPHYR_MCU, JLINK, None)

    assert resolution.device == "AE822F4M55_HP"
    assert resolution.gdb_path == "/zephyr-sdk/arm-zephyr-eabi-gdb"
    assert runners == ["jlink", "openocd"]
    assert core_id == "m55_hp"

    # …and a server the board never registered keeps its placeholder AND says so.
    from tan.commands.debug_config_cmd import _preview_notes_for

    pyocd_resolution, pyocd_runners, _pyocd_core_id = _resolve_from_build(
        root, ZEPHYR_MCU, "pyocd", None
    )
    draft = create_launch_draft(ZEPHYR_MCU, "pyocd", None)
    apply_launch_resolution(draft, pyocd_resolution)
    assert draft["targetId"] == "<resolved-target-id>"
    notes = _preview_notes_for(draft, pyocd_runners, "pyocd")
    assert any('runners.yaml: ["jlink", "openocd"]' in n for n in notes)


def test_fill_debug_probe_identity_gaps_fills_an_unresolved_draft():
    """The pre-build gap-fill this function exists for: no build has run
    (`resolution` is fully unresolved), so metadata identity fills all three
    fields. Port of `debug_launch.rs`'s test of the same name."""
    resolution = LaunchResolution()
    jlink_device = {"m55_hp": "Cortex-M55", "m55_he": "Cortex-M55"}
    fill_debug_probe_identity_gaps(
        resolution,
        "m55_hp",
        jlink_device,
        "AE302F80F55D5AE",
        None,  # alp-sdk#987: no SoC family publishes openocd_config yet.
    )
    assert resolution.device == "Cortex-M55"
    assert resolution.target_id == "AE302F80F55D5AE"
    assert resolution.config_files == [], (
        "an absent openocd_config must stay the published unknown, never a guess"
    )


def test_fill_debug_probe_identity_gaps_never_overrides_an_already_resolved_field():
    """A real build's own resolution always wins: metadata must not overwrite
    a `device` `runners.yaml` already resolved, even if it disagrees."""
    resolution = LaunchResolution(
        device="Cortex-M55",
        target_id="already-resolved",
        config_files=["already/resolved.cfg"],
    )
    jlink_device = {"m55_hp": "SDK-DEVICE"}
    fill_debug_probe_identity_gaps(
        resolution, "m55_hp", jlink_device, "sdk-target", "sdk.cfg"
    )
    assert resolution.device == "Cortex-M55"
    assert resolution.target_id == "already-resolved"
    assert resolution.config_files == ["already/resolved.cfg"]


def test_fill_debug_probe_identity_gaps_never_guesses_a_device_without_a_matching_core_id():
    """`jlink_device` is keyed by core id: no `core_id` (or a core id the map
    doesn't carry) must resolve nothing rather than guess "the only entry"."""
    jlink_device = {"m55_hp": "Cortex-M55"}

    no_core = LaunchResolution()
    fill_debug_probe_identity_gaps(no_core, None, jlink_device, None, None)
    assert no_core.device is None

    wrong_core = LaunchResolution()
    fill_debug_probe_identity_gaps(wrong_core, "m55_he", jlink_device, None, None)
    assert wrong_core.device is None, (
        "a core id absent from the map must not fall back to the only entry"
    )


#: The E1M-AEN801 SoM preset + Alif Ensemble E8 SoC JSON fixture the
#: alp-sdk#1026 debug-config tests write under `<root>/sdk/metadata/**` --
#: identical to `contract/envelopes/debug-config-preview-zephyr-mcu-sdk-identity`'s
#: fixture, reused here for the write-path/no-core cases that fixture doesn't
#: reach (a hermetic conformance golden never writes to a customer's file).
def write_sdk_fixture(root):
    sdk = Path(root, "sdk")
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    som_dir = sdk / "metadata" / "e1m_modules"
    som_dir.mkdir(parents=True)
    (som_dir / "E1M-AEN801.yaml").write_text(
        "schema_version: 1\nsku: E1M-AEN801\nsilicon: alif:ensemble:e8\n"
        "silicon_variant: AE822FA0E5597LS0\n",
        encoding="utf-8",
    )
    soc_dir = sdk / "metadata" / "socs" / "alif" / "ensemble"
    soc_dir.mkdir(parents=True)
    (soc_dir / "e8.json").write_text(
        """{
            "soc_spec_version": 1,
            "ref": "alif:ensemble:e8",
            "vendor": "Alif Semiconductor",
            "family": "Ensemble",
            "part": "E8",
            "variants": [
                {
                    "order_code": "AE822FA0E5597LS0",
                    "debug": {
                        "pyocd_target": "AE822FA0E5597LS0",
                        "jlink_device": {"m55_hp": "Cortex-M55", "m55_he": "Cortex-M55"}
                    }
                }
            ]
        }""",
        encoding="utf-8",
    )


def test_jlink_device_stays_the_placeholder_with_no_core_and_no_build(tmp_path):
    """alp-sdk#1026 review finding #3: `jlink_device` is keyed BY core id, so
    on a project that has never been built AND passes no `--core`,
    `identity_core` is `None` and `device` must stay the placeholder -- there
    is no core to index the map with, and no "only entry" guess."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    write_sdk_fixture(tmp_path)

    env = envelope(
        run_cli(
            tmp_path,
            "--target-kind", ZEPHYR_MCU, "--server", JLINK,
            "--sdk-root", "./sdk", "--preview", "--format", "json",
        )
    )
    assert env["exitCode"] == 0
    assert env["data"]["configuration"]["device"] == "<resolved-device>"


def test_write_discloses_when_sdk_identity_overwrites_a_hand_filled_device(tmp_path):
    """alp-sdk#1026 review finding #1 (data loss): a WRITE, not a preview -- a
    customer's `.vscode/launch.json` already holds a concrete, hand-filled
    `device`; the SDK's generic `jlink_device` identity resolves and REPLACES
    it, same as a real build's resolution always has -- but this run must
    disclose that in `issues[]`, not report `ok: true` / `issues: []` as if
    nothing happened."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    vscode_dir = launch_json(tmp_path).parent
    vscode_dir.mkdir()
    launch_json(tmp_path).write_text(
        """{
            "version": "0.2.0",
            "configurations": [
                {
                    "name": "Alp: Zephyr Debug (J-Link)",
                    "type": "cortex-debug",
                    "request": "launch",
                    "servertype": "jlink",
                    "device": "AE822FA0E5597LS0_M55_HE"
                }
            ]
        }""",
        encoding="utf-8",
    )
    write_sdk_fixture(tmp_path)

    env = envelope(
        run_cli(
            tmp_path,
            "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--core", "m55_hp",
            "--sdk-root", "./sdk", "--format", "json",
        )
    )
    assert env["exitCode"] == 0

    # The overwrite happened (matches a real build's own resolution behaviour
    # -- unchanged by this fix).
    assert env["data"]["configuration"]["device"] == "Cortex-M55"
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["configurations"][0]["device"] == "Cortex-M55"

    # …and it was DISCLOSED, not silent.
    overwrite_issue = next(
        (i for i in env["issues"] if i["code"] == "debug-config.sdk-identity-overwrite"), None
    )
    assert overwrite_issue is not None, f"no overwrite issue in {env['issues']!r}"
    assert overwrite_issue["severity"] == "info"
    assert "AE822FA0E5597LS0_M55_HE" in overwrite_issue["message"]
    assert "Cortex-M55" in overwrite_issue["message"]


def test_an_all_placeholder_config_files_list_keeps_a_hand_added_second_entry():
    """A per-index merge against a one-element draft would silently drop the
    customer's second `.cfg` and attach to half a target."""
    draft = create_launch_draft(ZEPHYR_MCU, "openocd", None)
    existing = json.dumps(
        {
            "version": "0.2.0",
            "configurations": [
                {
                    "name": "Alp: Zephyr Debug (OpenOCD)",
                    "configFiles": ["board/alp.cfg", "interface/cmsis-dap.cfg"],
                }
            ],
        }
    )

    plan = create_launch_json_write_plan(existing, draft)

    assert plan.written_configuration["configFiles"] == [
        "board/alp.cfg",
        "interface/cmsis-dap.cfg",
    ]


def test_select_slice_ignores_a_qualified_native_sim_lookalike():
    """`native_simulated_foo` is not a native_sim board: the required `/` anchors
    the prefix test to Zephyr's actual board-qualifier syntax."""
    slices = [{"core_id": "c", "os": "zephyr", "board": "native_simulated_foo"}]

    assert _select_slice(slices, NATIVE_HOST, None) is None


@pytest.mark.parametrize(
    "epoch",
    ["1700000000000", "99999999999", "-99999999999", "253402300799"],
)
def test_an_out_of_range_source_date_epoch_still_emits_one_envelope(epoch, tmp_path):
    """A SOURCE_DATE_EPOCH outside the platform's time_t range must not throw.

    `_generated_at()` is called from the recovery path of the exception guard, so
    a throw there DOUBLE-FAULTS: the first failure is caught, the recovery
    re-raises, and the process dies with a raw traceback and EMPTY stdout -- the
    exact break the guard exists to prevent.

    Milliseconds is the realistic trigger (1700000000000 -> year 55838), and CI
    and reproducible-build environments are what set this variable. `time.gmtime`
    raises OverflowError or OSError (Errno 22 on Windows) past the range, and the
    range differs per platform.

    The pre-existing guard test plants a RuntimeError inside `_run`, so it cannot
    see this: the failure happens in the recovery itself, not in the body.
    """
    env = dict(
        os.environ,
        SOURCE_DATE_EPOCH=epoch,
        PYTHONPATH=os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "tan", "debug-config",
         "--target-kind", "native-host", "--preview", "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=tmp_path, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)          # exactly one parseable document
    assert payload["command"] == "debug-config"
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# tan-cli#138: the restored v0.3.1 preLaunchTask default, end to end.
# ---------------------------------------------------------------------------


def test_a_default_run_names_its_v031_pre_launch_task(tmp_path):
    """Formerly the CLI-level pairing of `no_profile_names_a_pre_launch_task_
    by_default`: a plain run with no `--pre-launch-task` used to emit NO
    key. tan-cli#138 (maintainer decision) restores the v0.3.1 default -- the
    pure-logic contract itself lives in `tests/core/test_debug_launch.py`;
    this proves the CLI actually wires it through end to end."""
    env = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                "--preview", "--format", "json")
    )
    assert env["data"]["configuration"]["preLaunchTask"] == "alp: build active target"


def test_pre_launch_task_empty_string_opts_out_over_the_cli(tmp_path):
    """`--pre-launch-task ''` reaches the same opt-out `create_launch_draft`
    exercises directly -- proven here through actual argv parsing, since an
    empty-string CLI value is its own trap (typer/click could plausibly treat
    it as "not passed")."""
    env = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                "--pre-launch-task", "", "--preview", "--format", "json")
    )
    assert "preLaunchTask" not in env["data"]["configuration"]


# ---------------------------------------------------------------------------
# tan-cli#321: miDebuggerServerAddress needs a hand-filled value.
# ---------------------------------------------------------------------------


def test_yocto_preview_reports_the_gdbserver_address_info_issue_by_default(tmp_path):
    env = envelope(
        run_cli(tmp_path, "--target-kind", YOCTO_USERSPACE, "--server", GDBSERVER,
                "--preview", "--format", "json")
    )
    assert env["exitCode"] == 0
    assert env["data"]["configuration"]["miDebuggerServerAddress"] == "<host>:<port>"
    # tan-cli#138 vs #321: yocto-userspace carries NO restored preLaunchTask
    # default (unlike the other three target classes -- DEFAULT_PRE_LAUNCH_
    # TASK in tan/core/debug_launch.py deliberately omits it), so the issue
    # message must say so rather than claiming a default that does not exist.
    assert "preLaunchTask" not in env["data"]["configuration"]
    issue = next(
        (i for i in env["issues"] if i["code"] == "debug-config.gdbserver-address-unresolved"),
        None,
    )
    assert issue is not None and issue["severity"] == "info"
    assert "--gdbserver-address" in issue["message"]
    assert "carries no `preLaunchTask` reminder" in issue["message"]
    assert "--pre-launch-task" in issue["message"]


def test_yocto_write_reports_the_gdbserver_address_info_issue_too(tmp_path):
    """The write-path counterpart of the preview test above (tan-cli#321): the
    issue is built from the FINAL `configuration` in both branches of the
    `success()` closure in `debug_config_cmd.py`, not only the `--preview`
    one -- a mutation collapsing the write branch's own check (`if target ==
    YOCTO_USERSPACE` -> `if False`) killed no test before this, because
    every assertion of this issue firing lived on the `--preview` case
    only."""
    env = envelope(
        run_cli(tmp_path, "--target-kind", YOCTO_USERSPACE, "--server", GDBSERVER, "--format", "json")
    )
    assert env["exitCode"] == 0
    assert env["data"]["preview"] is False
    assert env["data"]["configuration"]["miDebuggerServerAddress"] == "<host>:<port>"
    codes = [i["code"] for i in env["issues"]]
    assert "debug-config.gdbserver-address-unresolved" in codes
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert (
        on_disk["configurations"][0]["miDebuggerServerAddress"] == "<host>:<port>"
    )


def test_gdbserver_address_flag_fills_the_field_and_drops_the_issue(tmp_path):
    env = envelope(
        run_cli(tmp_path, "--target-kind", YOCTO_USERSPACE, "--server", GDBSERVER,
                "--gdbserver-address", "192.168.10.42:3333", "--preview", "--format", "json")
    )
    assert env["exitCode"] == 0
    assert env["data"]["configuration"]["miDebuggerServerAddress"] == "192.168.10.42:3333"
    codes = [i["code"] for i in env["issues"]]
    assert "debug-config.gdbserver-address-unresolved" not in codes


def test_gdbserver_address_on_a_target_kind_without_the_field_says_so(tmp_path):
    env = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                "--gdbserver-address", "192.168.10.42:3333", "--preview", "--format", "json")
    )
    assert "miDebuggerServerAddress" not in env["data"]["configuration"]
    assert any("--gdbserver-address was given" in n for n in env["data"]["notes"]), (
        "accepting --gdbserver-address here in silence is the no-op this note exists to prevent"
    )
    # Not a yocto-userspace draft, so the tan-cli#321 issue must not fire either.
    codes = [i["code"] for i in env["issues"]]
    assert "debug-config.gdbserver-address-unresolved" not in codes


def test_an_empty_gdbserver_address_fails_instead_of_writing(tmp_path):
    """The same floor `--svd` holds for its own path argument: falling back to
    "no address" on an explicitly empty value would make a typo (or a copy-
    paste mistake) indistinguishable from not passing the flag at all."""
    env = envelope(
        run_cli(tmp_path, "--target-kind", YOCTO_USERSPACE, "--server", GDBSERVER,
                "--gdbserver-address", "", "--preview", "--format", "json")
    )
    assert env["exitCode"] == 5
    assert "empty value" in env["issues"][0]["message"]
    assert not launch_json(tmp_path).exists()


def test_a_hand_typed_gdbserver_address_survives_a_rerun_and_is_not_re_nagged(tmp_path):
    """tan-cli#321's info issue is checked against what this run actually
    WRITES, not the pre-merge draft: a customer who already filled in the
    real address must not be nagged about it forever. Companion to the Rust
    `a_hand_typed_gdbserver_address_survives_the_host_port_placeholder`
    (`crates/tan-core/src/debug_launch.rs`), which covers the merge itself;
    this proves the ISSUE follows the same outcome."""
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "configurations": [
                    {
                        "name": "Alp: Yocto Remote Debug",
                        "type": "cppdbg",
                        "request": "launch",
                        "miDebuggerServerAddress": "192.168.10.42:3333",
                        "miDebuggerPath": "/opt/gdb/bin/aarch64-poky-linux-gdb",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    env = envelope(
        run_cli(tmp_path, "--target-kind", YOCTO_USERSPACE, "--server", GDBSERVER, "--format", "json")
    )

    assert env["exitCode"] == 0
    assert env["data"]["configuration"]["miDebuggerServerAddress"] == "192.168.10.42:3333"
    codes = [i["code"] for i in env["issues"]]
    assert "debug-config.gdbserver-address-unresolved" not in codes
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["configurations"][0]["miDebuggerServerAddress"] == "192.168.10.42:3333"
