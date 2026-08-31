# SPDX-License-Identifier: Apache-2.0
"""`tan debug-config`: the write path, the resolution overlay, and the error
contract. The four `--preview` goldens are covered by
`tests/conformance/test_contract_envelopes.py`; everything here is the half of
the command no fixture reaches -- because reaching it means writing a file.
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import typer

from tan.commands.debug_config_cmd import (
    _resolve_from_build,
    _sdk_core_refusal_authority,
    _select_slice,
)
from tan.core import launch_provenance
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
    infer_target_kind,
    strip_jsonc,
)
from tan.core.debug_launch import BAREMETAL_MCU, OPENOCD, PYOCD, SERVER_NONE

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


def provenance_sidecar(root):
    """`tan-cli#518`'s own `.alp/` sidecar, mirroring `launch_json` above --
    both are keyed off the workspace root the real CLI resolves `--project`
    against."""
    return Path(root, ".alp", "debug-launch-provenance.json")


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


def test_a_write_failure_leaves_the_existing_launch_json_byte_identical(
    monkeypatch, capsys, tmp_path
):
    """tan-cli#489 (1+2): a failure between the temp write and the atomic
    replace must never touch the customer's real file. Before the fix,
    `open(launch_json_path, "w")` truncated the file to zero before a byte of
    `plan.content` was written -- any failure past that point (ENOSPC, a
    quota/RLIMIT_FSIZE hit, an I/O error, or the process dying) destroyed the
    customer's hand-authored configurations with no way for tan to repair it.
    Driven in-process (monkeypatching `os.replace`) rather than via subprocess
    + RLIMIT_FSIZE, which is POSIX-only and would not run on Windows/macOS the
    same way -- the property under test (the real path is never truncated) is
    identical either way, and this is portable."""
    import types

    from tan.commands import debug_config_cmd

    launch_json(tmp_path).parent.mkdir()
    original = (
        "{\n"
        '  "version": "0.2.0",\n'
        '  "configurations": [\n'
        "    {\n"
        '      "name": "My own hand-written config",\n'
        '      "type": "cppdbg",\n'
        '      "request": "launch"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )
    launch_json(tmp_path).write_text(original, encoding="utf-8")

    def boom_replace(_src, _dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(debug_config_cmd.os, "replace", boom_replace)

    with pytest.raises(typer.Exit) as exit_info:
        debug_config_cmd.debug_config(
            types.SimpleNamespace(obj=None),
            target_kind=ZEPHYR_MCU,
            server=JLINK,
            core=None,
            pre_launch_task=None,
            gdbserver_address=None,
            svd=None,
            preview=False,
            project=str(tmp_path),
            board_yaml=None,
            sdk_root=None,
            output_format="json",
            quiet=False,
        )

    assert exit_info.value.exit_code == 3
    env = json.loads(capsys.readouterr().out)
    assert env["issues"][0]["code"] == "debug-config.write-failure"
    # The whole point: the ORIGINAL file, byte for byte, not merely "still
    # parses" -- a truncating write followed by a failed re-populate could
    # still leave valid-but-wrong JSON.
    assert launch_json(tmp_path).read_text(encoding="utf-8") == original
    # No leftover temp file: the failure path cleans up after itself.
    leftovers = list(launch_json(tmp_path).parent.glob("*.tan-tmp"))
    assert leftovers == [], leftovers


def test_a_mid_write_failure_also_leaves_the_existing_launch_json_byte_identical(
    monkeypatch, capsys, tmp_path
):
    """tan-cli#489 review round: the test above only patches `os.replace`,
    which exercises the CLEANUP path -- the temp write itself already fully
    succeeded by the time it runs. This one fails INSIDE the write (`fsync`,
    after `handle.write` already put bytes in the temp file's own buffer but
    before the temp is durable or the rename happens), proving the real
    `launch.json` -- untouched at that point by construction, since
    `_atomic_write_launch_json` never opens it for writing at all -- survives
    a failure at that earlier point too, not just a failed rename."""
    import types

    from tan.commands import debug_config_cmd

    launch_json(tmp_path).parent.mkdir()
    original = '{\n  "version": "0.2.0",\n  "configurations": []\n}\n'
    launch_json(tmp_path).write_text(original, encoding="utf-8")

    def boom_fsync(_fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(debug_config_cmd.os, "fsync", boom_fsync)

    with pytest.raises(typer.Exit) as exit_info:
        debug_config_cmd.debug_config(
            types.SimpleNamespace(obj=None),
            target_kind=ZEPHYR_MCU,
            server=JLINK,
            core=None,
            pre_launch_task=None,
            gdbserver_address=None,
            svd=None,
            preview=False,
            project=str(tmp_path),
            board_yaml=None,
            sdk_root=None,
            output_format="json",
            quiet=False,
        )

    assert exit_info.value.exit_code == 3
    env = json.loads(capsys.readouterr().out)
    assert env["issues"][0]["code"] == "debug-config.write-failure"
    assert launch_json(tmp_path).read_text(encoding="utf-8") == original
    leftovers = list(launch_json(tmp_path).parent.glob("*.tan-tmp"))
    assert leftovers == [], leftovers


def test_a_symlinked_launch_json_keeps_the_link_and_updates_the_real_file(tmp_path):
    """tan-cli#489 review round, finding 4: `.vscode/launch.json` can be a
    symlink -- dotfile-managed, or a canonical file shared across worktrees.
    `os.replace` on a symlink replaces the LINK itself with a regular file
    (unlike the old `open(path, "w")`, which wrote THROUGH it) unless the
    real target is resolved first. FAILS against the pre-fix code: the link
    is gone (`is_symlink()` False) and the canonical file the customer
    actually edits never received the merge."""
    canonical = tmp_path / "dotfiles" / "launch.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        json.dumps({"version": "0.2.0", "configurations": []}), encoding="utf-8"
    )
    launch_json(tmp_path).parent.mkdir()
    try:
        launch_json(tmp_path).symlink_to(canonical)
    except OSError:
        pytest.skip("cannot create a file symlink on this host")

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json"))

    assert env["exitCode"] == 0, env
    assert launch_json(tmp_path).is_symlink(), "the symlink itself must survive the write"
    assert os.path.realpath(launch_json(tmp_path)) == os.path.realpath(canonical)
    on_disk = json.loads(canonical.read_text(encoding="utf-8"))
    assert on_disk["configurations"][0]["name"] == "Alp: Zephyr Debug (J-Link)"


def test_a_stray_tan_tmp_sibling_from_another_process_is_left_alone(tmp_path):
    """tan-cli#489 review round: `_atomic_write_launch_json` used to sweep
    (delete) every `*.tan-tmp` sibling in the target directory before
    writing its own -- measured to unlink a SECOND process's still-open
    `mkstemp` temp, whose own `os.replace` then failed `FileNotFoundError`
    (two concurrent `tan debug-config` runs, e.g. the extension re-running
    per session alongside a terminal invocation, becoming one baffling write
    failure), and -- worse, on a symlinked `launch.json` -- unlinking a
    pre-existing, unrelated `*.tan-tmp` OUTSIDE the project entirely, in
    whatever directory the symlink's real target happened to live in. The
    sweep bought nothing (a fresh `mkstemp` name never collides with a stale
    one regardless) and was pure risk, so it is gone: a `*.tan-tmp` sitting
    in `.vscode/` before a run must still be there, byte for byte, after
    one."""
    launch_json(tmp_path).parent.mkdir()
    other_processes_temp = launch_json(tmp_path).parent / "launch.json.abcd1234.tan-tmp"
    other_processes_temp.write_text("mid-write content from another run\n", encoding="utf-8")

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json"))

    assert env["exitCode"] == 0, env
    assert other_processes_temp.exists(), "a concurrent process's own temp must survive"
    assert other_processes_temp.read_text(encoding="utf-8") == "mid-write content from another run\n"


def test_a_first_write_respects_the_process_umask_not_mkstemps_0600(tmp_path):
    """tan-cli#489 review round: `mkstemp` hardcodes `0600` (POSIX designed
    it for secrets; a `launch.json` is not one), and every LATER run's
    mode-preservation would otherwise just copy that narrow mode forward --
    silently locking a shared checkout or a devcontainer running as a
    different uid out of a file the extension needs to read. A first-ever
    write must land at the umask-filtered default a plain `open(path, "w")`
    would have produced, `0o666 & ~umask`, not `mkstemp`'s own `0o600`."""
    if os.name == "nt":
        pytest.skip("POSIX permission bits only")
    old_umask = os.umask(0o022)
    try:
        env = envelope(
            run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json")
        )
    finally:
        os.umask(old_umask)

    assert env["exitCode"] == 0, env
    mode = stat.S_IMODE(launch_json(tmp_path).stat().st_mode)
    assert mode == 0o644, oct(mode)


def test_a_rewrite_preserves_the_existing_files_own_mode(tmp_path):
    """tan-cli#489 review round: the pairing case for the test above -- once
    a `launch.json` exists at some deliberate mode (a shared `.vscode/`
    convention, a `600` this process should not widen), a later run must
    carry that mode across the `os.replace` swap rather than reverting to
    whatever the fresh temp happened to get."""
    if os.name == "nt":
        pytest.skip("POSIX permission bits only")
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(
        json.dumps({"version": "0.2.0", "configurations": []}), encoding="utf-8"
    )
    launch_json(tmp_path).chmod(0o600)

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json"))

    assert env["exitCode"] == 0, env
    mode = stat.S_IMODE(launch_json(tmp_path).stat().st_mode)
    assert mode == 0o600, oct(mode)


@pytest.mark.parametrize(
    "argv,want_target,want_server,want_programs",
    [
        (("--target-kind", "bogus-kind"), ZEPHYR_MCU, "none", True),
        (("--target-kind", ZEPHYR_MCU, "--server", "bogus-server"), ZEPHYR_MCU, "none", True),
        # A legal server for the wrong target class: gdbserver is yocto-only.
        # #508 review, Major 4 follow-up (tan-cli#477): both locals ARE bound
        # by this point, so this reports the pairing it actually refused
        # (zephyr-mcu/gdbserver), not the placeholder the first two rows
        # still get -- neither of THOSE ever finished parsing.
        (("--target-kind", ZEPHYR_MCU, "--server", GDBSERVER), ZEPHYR_MCU, GDBSERVER, True),
    ],
    ids=["target-kind", "server", "pairing"],
)
def test_a_refused_selector_is_a_coded_envelope_at_exit_2(
    tmp_path, argv, want_target, want_server, want_programs
):
    """tan-cli#477: exit 2, not 5. A flag VALUE outside the accepted set is
    the caller's own input, and every one of these already answered with a
    complete, actionable message -- only the verdict said "tan crashed".
    tan-cli#462 made that argument for the four PRECONDITIONS; this is the
    argument-validation half it left behind.

    Everything else this case pinned is unchanged and still asserted: the
    null configuration, the null project, and that no launch.json is
    written. The reported `targetKind`/`server` are NOT unconditionally the
    placeholder any more -- see `test_a_refusal_reports_the_target_and_
    server_it_actually_knows` for the full split."""
    env = envelope(run_cli(tmp_path, *argv, "--format", "json"))

    assert env["exitCode"] == 2 and env["ok"] is False
    assert env["issues"][0]["code"] == "debug-config.invalid-argument"
    assert env["data"]["targetKind"] == want_target and env["data"]["server"] == want_server
    assert env["data"]["configuration"] is None
    assert env["project"] == {"root": None, "boardYaml": None}
    assert not launch_json(tmp_path).exists()
    # tan-cli#945: `programsDevice` is present -- and follows `targetKind` --
    # even on a refusal that never built a `configuration`. tan-cli#1020
    # review nit: pinned against the LITERAL `want_programs`, not
    # `programs_device(want_target)` -- comparing the CLI's output to the
    # very function under test is vacuous for the VALUE (a `programs_device`
    # mutated to always return `False` cancels out on both sides of `is` and
    # every case here still passes; verified while fixing this).
    assert env["data"]["programsDevice"] is want_programs


def test_an_svd_path_that_cannot_be_read_fails_instead_of_writing(tmp_path):
    """Falling back to "no SVD" would make a typo indistinguishable from not
    passing the flag, and the failure would surface as an unexplained empty
    peripheral view.

    tan-cli#477 moves the VERDICT from 5 to 2 -- an unreadable `--svd` path
    is the caller's own argument -- and leaves this test's actual point,
    that it refuses rather than silently continues, exactly as it was."""
    env = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                "--svd", str(tmp_path / "nope.svd"), "--format", "json")
    )

    assert env["exitCode"] == 2
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


# tan-cli#945: a consumer must be able to tell, from the envelope alone,
# whether starting the written profile programs the attached target -- see
# alp-sdk-vscode#586, which had no way to see that fact and shipped a flash
# consent dialog that could never trigger.
@pytest.mark.parametrize(
    "target,server,expect_programs",
    [
        (ZEPHYR_MCU, JLINK, True),
        (BAREMETAL_MCU, OPENOCD, True),
        (YOCTO_USERSPACE, GDBSERVER, False),
        (NATIVE_HOST, SERVER_NONE, False),
    ],
)
def test_the_preview_envelope_states_whether_the_profile_programs_the_device(
    tmp_path, target, server, expect_programs
):
    env = envelope(
        run_cli(tmp_path, "--target-kind", target, "--server", server, "--preview", "--format", "json")
    )

    assert env["data"]["programsDevice"] is expect_programs


def test_a_cortex_debug_preview_carries_an_explicit_load_files_key(tmp_path):
    """The second half of tan-cli#945's ask: the written `configuration`
    itself names the artefact it programs, rather than relying on
    `marus25.cortex-debug`'s own undocumented-on-the-wire schema default."""
    env = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--preview", "--format", "json")
    )

    config = env["data"]["configuration"]
    assert config["loadFiles"] == [config["executable"]]


def test_a_non_cortex_debug_preview_carries_no_load_files_key(tmp_path):
    env = envelope(
        run_cli(
            tmp_path, "--target-kind", YOCTO_USERSPACE, "--server", GDBSERVER,
            "--preview", "--format", "json",
        )
    )

    assert "loadFiles" not in env["data"]["configuration"]


def test_a_real_build_resolution_updates_load_files_alongside_executable(tmp_path):
    """`loadFiles` must never drift from `executable` once a real build
    resolves it -- both name the same artefact, or a consumer reading
    `programsDevice: true` alongside a stale `loadFiles` would be told the
    wrong file gets flashed."""
    pytest.importorskip("yaml")
    root = str(tmp_path).replace("\\", "/")
    build_dir = f"{root}/build/m55_hp-zephyr/build"
    write_manifest(
        tmp_path,
        "schema_version: 1\nslices:\n- core_id: m55_hp\n  os: zephyr\n"
        f"  board: alp_x\n  build_dir: {build_dir}\n"
        f"  output_artefact: {build_dir}/zephyr/zephyr.elf\n",
    )

    env = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--preview", "--format", "json")
    )

    config = env["data"]["configuration"]
    assert config["executable"] == "${workspaceFolder}/build/m55_hp-zephyr/build/zephyr/zephyr.elf"
    assert config["loadFiles"] == [config["executable"]]


def test_a_write_persists_load_files_alongside_the_executable_on_disk(tmp_path):
    env = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json")
    )

    assert env["exitCode"] == 0, env
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    written = on_disk["configurations"][0]
    assert written["loadFiles"] == [written["executable"]]
    assert env["data"]["configuration"]["loadFiles"] == written["loadFiles"]


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


def test_a_pre_subcommand_format_reaches_the_command_not_a_root_refusal(tmp_path):
    """INVERTED by tan-cli#378, deliberately. This asserted the opposite --
    `tan --format json validate --offline` must answer `cli.parse-error` -- on
    the premise that a command not reading the root `--format` would otherwise
    run in silent text mode (exit 0, nothing on stdout, an envelope-less
    `--format json` run). The premise was sound; #378 removed it, by relocating
    the flag past the subcommand name onto the command's OWN always-read
    `--format` instead of hand-listing which commands were allowed to precede
    it. Refusing is now the defect, not the guard: the refusal named
    `command: "cli"` for argv the oracle runs, for the 20 of 32 commands the
    allowlist never reached.

    Anchored on the oracle, not on the port's own new answer -- measured,
    `target/debug/tan.exe --format json validate --offline` in an empty
    directory is exit 2 with `command: "validate"` and
    `validate.board-yaml-missing`. That is what this now pins, so a
    re-introduced root-level refusal (a `cli.parse-error`, whatever its
    message) fails here again."""
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
    envelope_ = json.loads(proc.stdout)
    assert envelope_["command"] == "validate", (
        "the pre-subcommand `--format json` must reach `validate` and be reported "
        f"as ITS envelope, not the CLI's: {envelope_}"
    )
    assert envelope_["issues"][0]["code"] == "validate.board-yaml-missing", envelope_


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
#: the same SoM preset + SoC walk as
#: `contract/envelopes/debug-config-preview-zephyr-mcu-sdk-identity`'s
#: fixture, reused here for the write-path/no-core cases that fixture doesn't
#: reach (a hermetic conformance golden never writes to a customer's file).
#:
#: tan-cli#477 major 2 adds the `cores` array, which that frozen fixture (a
#: contract golden, uneditable) omits. It is not decoration: `cores[].id` is
#: what the pre-build `--core` guard validates against, and the values here
#: are alp-sdk `metadata/socs/alif/ensemble/e8.json`'s own, verbatim --
#: `a32_cluster`, `m55_hp`, `m55_he`. Note `a32_cluster` is a REAL core with
#: no `jlink_device` entry, which is the shape that keeps
#: `debug-config.sdk-identity-core-unresolved`'s core-mismatch arm reachable
#: now that a core outside this list is refused outright.
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
            "cores": [{"id": "a32_cluster"}, {"id": "m55_hp"}, {"id": "m55_he"}],
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


#: Deliberately narrower than the real `som-preset-v1.schema.json` -- the same
#: narrowing rationale `test_presets_command.py`'s own `_SOM_SCHEMA` states:
#: enough to exercise the gate (`silicon:` typed), not a byte-for-byte mirror
#: of a schema this file's coverage must not depend on never changing shape.
_DEBUG_CONFIG_SOM_SCHEMA = json.dumps({
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "sku", "silicon"],
    "properties": {
        "schema_version": {"const": 1},
        "sku": {"type": "string"},
        "silicon": {"type": "string"},
    },
})


def write_sdk_fixture_with_schema_invalid_som_preset(root):
    """Same shape as `write_sdk_fixture`, plus `metadata/schemas/som-preset-v1
    .schema.json` and a `silicon:` field the schema forbids (a number, not a
    string) -- tan-cli#964 review (major 5): `debug-config` reads this exact
    SoM preset through TWO walks (`_sdk_published_cores`,
    `_fill_debug_probe_identity_from_sdk`), both via the shared
    `read_sdk_som_and_soc`, and before the fix neither passed `warnings` at
    all."""
    sdk = Path(root, "sdk")
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    schema_dir = sdk / "metadata" / "schemas"
    schema_dir.mkdir(parents=True)
    (schema_dir / "som-preset-v1.schema.json").write_text(
        _DEBUG_CONFIG_SOM_SCHEMA, encoding="utf-8"
    )
    som_dir = sdk / "metadata" / "e1m_modules"
    som_dir.mkdir(parents=True)
    (som_dir / "E1M-AEN801.yaml").write_text(
        "schema_version: 1\nsku: E1M-AEN801\nsilicon: 7\n"
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
            "cores": [{"id": "a32_cluster"}, {"id": "m55_hp"}, {"id": "m55_he"}],
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


def test_a_schema_invalid_som_preset_warns_on_debug_config(tmp_path):
    """tan-cli#964 review (major 5): `debug-config` is one of the ten
    read-path commands the decided rule names, but its own two walks
    (`_sdk_published_cores`/`_fill_debug_probe_identity_from_sdk`) passed no
    `warnings` to `read_sdk_som_and_soc` at all -- despite the PR body's own
    claim that it inherited the WARN half "transitively". This is the
    `--preview` regression test for the fix: the command still resolves
    exactly as before (silicon degrades, no refusal, exit 0), but now ALSO
    reports one `debug-config.metadata-schema-invalid` issue naming the
    file, the JSON pointer, and what was found.

    Mutation-proven: reverting the `warnings=schema_warnings` threading added
    to `_sdk_published_cores`/`_fill_debug_probe_identity_from_sdk`'s call
    sites (byte copy restored after, never `git checkout`) turns this test's
    `codes`/`message` assertions red; restoring turns them green.
    """
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    write_sdk_fixture_with_schema_invalid_som_preset(tmp_path)

    env = envelope(
        run_cli(
            tmp_path,
            "--target-kind", ZEPHYR_MCU, "--server", JLINK,
            "--sdk-root", "./sdk", "--preview", "--format", "json",
        )
    )
    assert env["exitCode"] == 0
    codes = [i["code"] for i in env["issues"]]
    assert codes.count("debug-config.metadata-schema-invalid") == 1, env["issues"]
    issue = next(
        i for i in env["issues"] if i["code"] == "debug-config.metadata-schema-invalid"
    )
    assert issue["severity"] == "warning"
    assert "silicon: 7 is not of type 'string'" in issue["message"]


def test_jlink_device_stays_the_placeholder_with_no_core_and_no_build(tmp_path):
    """alp-sdk#1026 review finding #3: `jlink_device` is keyed BY core id, so
    on a project that has never been built AND passes no `--core`,
    `identity_core` is `None` and `device` must stay the placeholder -- there
    is no core to index the map with, and no "only entry" guess.

    tan-cli#489 (4): the issue reported for WHY it stayed a placeholder must
    not misattribute this to "the SDK publishes no `device` value" --
    `write_sdk_fixture`'s own `e8.json` publishes one for every core it has
    (`m55_hp`/`m55_he`); the only missing input is which core to look up, and
    the message must say so and name the working remedy (`--core`)."""
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
    codes = [i["code"] for i in env["issues"]]
    assert "debug-config.sdk-identity-key-absent" not in codes, (
        "misattributes 'no core to look up with' as 'the SDK publishes no "
        f"value at all': {env['issues']!r}"
    )
    issue = next(
        (i for i in env["issues"] if i["code"] == "debug-config.sdk-identity-core-unresolved"),
        None,
    )
    assert issue is not None, env["issues"]
    assert "--core" in issue["message"]


def test_jlink_device_names_the_known_cores_for_a_core_the_map_has_no_entry_for(tmp_path):
    """tan-cli#489 (4) refinement: an EXPLICIT `--core` the SDK's published map
    has no entry for is the same underlying cause (no usable lookup key) as
    the no-core case above, sharing the same code -- but the message can, and
    must, name the cores the SDK DOES publish, since a core WAS given here.

    tan-cli#477 major 2 changed the core this drives with, not the assertion:
    it used to pass `m55_typo`, which is now REFUSED outright (exit 2,
    `debug-config.core-unknown`) because it names no core the SoM publishes.
    `a32_cluster` is the honest shape for this arm and the one real metadata
    actually has -- a core the SoC genuinely HAS (`cores[].id` in alp-sdk's
    own `e8.json`) for which `variants[].debug.jlink_device` publishes no
    entry. The lookup still has no usable key, so the code and the "name the
    cores the SDK does publish" requirement are unchanged."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    write_sdk_fixture(tmp_path)

    env = envelope(
        run_cli(
            tmp_path,
            "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--core", "a32_cluster",
            "--sdk-root", "./sdk", "--preview", "--format", "json",
        )
    )
    assert env["exitCode"] == 0
    assert env["data"]["configuration"]["device"] == "<resolved-device>"
    codes = [i["code"] for i in env["issues"]]
    assert "debug-config.sdk-identity-key-absent" not in codes, env["issues"]
    issue = next(
        (i for i in env["issues"] if i["code"] == "debug-config.sdk-identity-core-unresolved"),
        None,
    )
    assert issue is not None, env["issues"]
    assert "a32_cluster" in issue["message"]
    assert "m55_hp" in issue["message"] and "m55_he" in issue["message"]


def write_sdk_fixture_with_no_jlink_device(root):
    """tan-cli#489 review round: today's REAL Alif shape -- `variants[].debug`
    publishes `openocd_config` but declares NO `jlink_device` key at all (the
    registry's own note for `sdk-identity-key-absent` names this exact case).
    `known_jlink_cores` is then the empty set for EVERY `--core`, valid or
    not -- distinct from `write_sdk_fixture`, whose `jlink_device` map makes
    the empty-set case unreachable."""
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
                        "openocd_config": "board/alif_e8.cfg"
                    }
                }
            ]
        }""",
        encoding="utf-8",
    )


def test_a_valid_core_with_no_published_jlink_device_map_keeps_the_key_absent_code(tmp_path):
    """tan-cli#489 review round, finding 1: `sdk-identity-core-unresolved`
    must not steal `sdk-identity-key-absent`'s own correct case. A SoM that
    publishes NO `jlink_device` map at all makes `known_jlink_cores` the empty
    set regardless of `--core` -- `bool(known_jlink_cores)` gates the
    core-mismatch branch so a VALID `--core m55_hp` is not told the map has
    no entry for it (self-contradictory: 'its published cores are: none ...
    pass --core with one of the cores above' when a core WAS already given).
    FAILS against the pre-fix code, which emitted
    `sdk-identity-core-unresolved` unconditionally whenever `known_jlink_cores`
    was empty."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    write_sdk_fixture_with_no_jlink_device(tmp_path)

    env = envelope(
        run_cli(
            tmp_path,
            "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--core", "m55_hp",
            "--sdk-root", "./sdk", "--preview", "--format", "json",
        )
    )
    assert env["exitCode"] == 0
    codes = [i["code"] for i in env["issues"]]
    assert "debug-config.sdk-identity-core-unresolved" not in codes, env["issues"]
    issue = next(
        (i for i in env["issues"] if i["code"] == "debug-config.sdk-identity-key-absent"), None
    )
    assert issue is not None, env["issues"]
    assert "device" in issue["message"]


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


def test_sdk_identity_overwrite_message_stays_true_for_config_files(tmp_path):
    """tan-cli#489 review round: `sdk-identity-overwrite`'s message says the
    write REPLACED the existing value with the incoming one and tells the
    customer to restore the old one by hand if that was deliberate. The
    identity-only merge (this round's own regression) no longer replaced
    `configFiles` at all -- it kept BOTH -- so a customer following that
    advice would hand-add a THIRD copy. The position-anchored merge fixes
    the underlying behaviour the message describes; this proves the message
    and the on-disk result agree again for the one-element SDK-filled case
    `sdk_identity_overwrites` is scoped to.

    tan-cli#518: `board/OLD.cfg` must now be PROVEN tan's own prior output
    before the merge (and this disclosure) will touch it, so this test
    primes `.alp/debug-launch-provenance.json` with exactly the record a
    real EARLIER `tan debug-config` run would have left behind for it --
    the realistic story this scenario always told (a stale resolved value
    from a run against an older SDK fixture), not a customer's own typed
    value. `test_an_sdk_filled_config_files_value_with_no_provenance_is_
    disclosed_as_appended_not_replaced` below covers the un-primed case."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "configurations": [
                    {
                        "name": "Alp: Zephyr Debug (OpenOCD)",
                        "type": "cortex-debug",
                        "request": "launch",
                        "servertype": "openocd",
                        "configFiles": ["board/OLD.cfg"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_sdk_fixture_with_no_jlink_device(tmp_path)
    provenance_sidecar(tmp_path).parent.mkdir()
    provenance_sidecar(tmp_path).write_text(
        launch_provenance.render(
            launch_provenance.empty().updated(
                "Alp: Zephyr Debug (OpenOCD)", {"configFiles": ["board/OLD.cfg"]}
            )
        ),
        encoding="utf-8",
    )

    env = envelope(
        run_cli(
            tmp_path,
            "--target-kind", ZEPHYR_MCU, "--server", "openocd",
            "--sdk-root", "./sdk", "--format", "json",
        )
    )
    assert env["exitCode"] == 0, env

    # The message says "replaced" -- so the on-disk result must actually BE a
    # replacement (one element, the NEW value), not both old and new kept.
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["configurations"][0]["configFiles"] == ["board/alif_e8.cfg"], on_disk

    overwrite_issue = next(
        (i for i in env["issues"] if i["code"] == "debug-config.sdk-identity-overwrite"), None
    )
    assert overwrite_issue is not None, env["issues"]
    assert "board/OLD.cfg" in overwrite_issue["message"]
    assert "board/alif_e8.cfg" in overwrite_issue["message"]

    # tan-cli#982 review finding #2's sibling code must NOT fire here -- this
    # run genuinely replaced the value, nothing was appended beside it.
    appended_issue = next(
        (i for i in env["issues"] if i["code"] == "debug-config.sdk-identity-appended"), None
    )
    assert appended_issue is None, env["issues"]


def test_an_sdk_filled_config_files_value_with_no_provenance_is_disclosed_as_appended_not_replaced(
    tmp_path,
):
    """tan-cli#518's own core scenario, reached through the SDK-identity
    path specifically: the exact fixture of the test above, but with no
    `.alp/` sidecar at all -- `board/OLD.cfg` could be a customer's own
    hand-typed value. Nothing here can tell it apart from tan's own stale
    output, so the merge must not gamble: `board/alif_e8.cfg` is APPENDED,
    `board/OLD.cfg` survives untouched, and -- because nothing was actually
    replaced -- `sdk_identity_overwrites` must not raise a "replaced" alarm
    over a value that is still sitting right there in the file. A disclosure
    here would be worse than silence: it would send the customer hunting for
    a value to restore that was never touched.

    tan-cli#982 review finding #2: staying silent about the OVERWRITE is
    right, but this run still made a decision worth telling the customer
    about -- it left `board/OLD.cfg` in the file and appended a second
    `configFiles` entry beside it, rather than reconciling to one. Two board
    `.cfg`s sourced on the same TAP is the same failure class
    `test_a_resolved_replacement_overwrites_the_previous_one_instead_of_
    accumulating` names, and `issues: []` here told the customer nothing.
    `debug-config.sdk-identity-appended` is the disclosure for exactly this
    shape."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "configurations": [
                    {
                        "name": "Alp: Zephyr Debug (OpenOCD)",
                        "type": "cortex-debug",
                        "request": "launch",
                        "servertype": "openocd",
                        "configFiles": ["board/OLD.cfg"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_sdk_fixture_with_no_jlink_device(tmp_path)
    assert not provenance_sidecar(tmp_path).exists()

    env = envelope(
        run_cli(
            tmp_path,
            "--target-kind", ZEPHYR_MCU, "--server", "openocd",
            "--sdk-root", "./sdk", "--format", "json",
        )
    )
    assert env["exitCode"] == 0, env

    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["configurations"][0]["configFiles"] == [
        "board/OLD.cfg",
        "board/alif_e8.cfg",
    ], on_disk

    overwrite_issue = next(
        (i for i in env["issues"] if i["code"] == "debug-config.sdk-identity-overwrite"), None
    )
    assert overwrite_issue is None, env["issues"]

    # tan-cli#982 review finding #2: the append that DID happen is disclosed
    # instead -- naming both the stranded existing value and what landed
    # beside it, so the customer can tell there are now two.
    appended_issue = next(
        (i for i in env["issues"] if i["code"] == "debug-config.sdk-identity-appended"), None
    )
    assert appended_issue is not None, env["issues"]
    assert appended_issue["severity"] == "info"
    assert "board/OLD.cfg" in appended_issue["message"]
    assert "board/alif_e8.cfg" in appended_issue["message"]


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


def test_a_hand_added_second_config_file_survives_a_resolved_one_element_draft():
    """tan-cli#489 (3): the guard above only covers an ALL-placeholder
    incoming list. Here the incoming `configFiles` is a single RESOLVED
    entry (a real build's `runners.yaml` registered only one `--config`),
    which cannot take that guard's branch -- the per-index merge below it
    used to silently truncate the customer's second `.cfg`, attaching to
    half a target."""
    draft = create_launch_draft(ZEPHYR_MCU, "openocd", None)
    draft["configFiles"] = ["board/renesas_rzv2n.cfg"]
    existing = json.dumps(
        {
            "version": "0.2.0",
            "configurations": [
                {
                    "name": "Alp: Zephyr Debug (OpenOCD)",
                    "configFiles": ["board/renesas_rzv2n.cfg", "interface/jlink.cfg"],
                }
            ],
        }
    )

    plan = create_launch_json_write_plan(existing, draft)

    assert plan.written_configuration["configFiles"] == [
        "board/renesas_rzv2n.cfg",
        "interface/jlink.cfg",
    ]


def test_a_reordered_config_file_is_not_destroyed_or_duplicated():
    """tan-cli#489 review round, finding 2: the SAME scenario as the test
    above, but in the conventional OpenOCD INTERFACE-FIRST order. A per-INDEX
    merge (the test above's own fix, before this round) paired the
    customer's `interface/jlink.cfg` (position 0) with the draft's own
    resolved `board/renesas_rzv2n.cfg` -- overwriting the interface entry --
    and then found `board/renesas_rzv2n.cfg` a SECOND time via the
    tail-preservation of `existing[1:]`, which still held it: OpenOCD ends up
    loading the board config twice and the interface config never. Matching
    by identity instead of position finds `board/renesas_rzv2n.cfg`'s real
    counterpart wherever it sits, so nothing is duplicated and nothing is
    lost regardless of order. FAILS against the pre-fix (positional) merge,
    which produced `["board/renesas_rzv2n.cfg", "board/renesas_rzv2n.cfg"]`."""
    draft = create_launch_draft(ZEPHYR_MCU, "openocd", None)
    draft["configFiles"] = ["board/renesas_rzv2n.cfg"]
    existing = json.dumps(
        {
            "version": "0.2.0",
            "configurations": [
                {
                    "name": "Alp: Zephyr Debug (OpenOCD)",
                    "configFiles": ["interface/jlink.cfg", "board/renesas_rzv2n.cfg"],
                }
            ],
        }
    )

    plan = create_launch_json_write_plan(existing, draft)

    config_files = plan.written_configuration["configFiles"]
    assert config_files.count("board/renesas_rzv2n.cfg") == 1, config_files
    assert "interface/jlink.cfg" in config_files, config_files
    # tan-cli#489 review round (second pass): order matters to OpenOCD (the
    # interface driver must come before the board/target config that uses
    # it), so this pins the EXACT order, not just membership + no
    # duplication -- the merge now emits in `existing`'s OWN order (matched
    # entries updated in place), never the draft's.
    assert config_files == ["interface/jlink.cfg", "board/renesas_rzv2n.cfg"], config_files


def test_a_resolved_replacement_overwrites_the_previous_one_instead_of_accumulating():
    """tan-cli#489 review round (second pass): the BLOCKER. Identity-only
    matching (no positional fallback) treated a NEW resolved value that
    matches nothing already in the file as an ADDITION every time, never a
    REPLACEMENT of what tan itself wrote last run -- three consecutive
    `tan debug-config` runs (rebuilding only `runners.yaml`'s `--config=`
    between them) left `configFiles` holding all three values in turn,
    and OpenOCD sources every `-f`, so three board configs on one TAP fail
    the session outright. Position is restored as the fallback signal for
    exactly this one-to-one case. FAILS against the review round's own
    identity-only merge, which accumulated all three.

    tan-cli#518 threads each call's own `.provenance` into the next, exactly
    as `tan debug-config` itself does by reading back the `.alp/` sidecar it
    just wrote -- the real CLI's idempotency (proven end-to-end by
    `test_three_real_cli_runs_replace_configfiles_each_time_not_accumulate`
    below) depends on that persistence, not on calling this pure function
    with no memory of what a prior call wrote. Passing no `provenance` at all
    is covered on its own by
    `test_an_unrecorded_positional_slot_is_never_overwritten_by_a_bare_merge`."""
    draft_a = create_launch_draft(ZEPHYR_MCU, "openocd", None)
    draft_a["configFiles"] = ["board/alp_rev_a.cfg"]
    existing = json.dumps(
        {"version": "0.2.0", "configurations": []}
    )
    plan_a = create_launch_json_write_plan(existing, draft_a)
    assert plan_a.written_configuration["configFiles"] == ["board/alp_rev_a.cfg"]

    draft_b = create_launch_draft(ZEPHYR_MCU, "openocd", None)
    draft_b["configFiles"] = ["board/alp_rev_b.cfg"]
    plan_b = create_launch_json_write_plan(plan_a.content, draft_b, provenance=plan_a.provenance)
    assert plan_b.written_configuration["configFiles"] == ["board/alp_rev_b.cfg"], (
        plan_b.written_configuration["configFiles"]
    )

    draft_c = create_launch_draft(ZEPHYR_MCU, "openocd", None)
    draft_c["configFiles"] = ["board/alp_rev_c.cfg"]
    plan_c = create_launch_json_write_plan(plan_b.content, draft_c, provenance=plan_b.provenance)
    assert plan_c.written_configuration["configFiles"] == ["board/alp_rev_c.cfg"], (
        plan_c.written_configuration["configFiles"]
    )


def test_a_resolved_replacement_still_keeps_a_hand_added_entry_in_place():
    """tan-cli#489 review round (second pass): the pairing case for the test
    above -- a customer's own hand-added SECOND entry (never matched by
    anything the draft resolves) must survive a positional replacement of
    the FIRST entry, in its OWN place, not reordered to the front. This is
    the exact case #489's own defect 3 is about.

    tan-cli#518: `board/old.cfg` is what makes the positional replacement
    legal at all now -- it must be PROVEN tan's own prior output (a recorded
    content hash), not just occupy the one free slot. The `provenance` built
    here is exactly what `create_launch_json_write_plan` itself would have
    produced from an earlier run that wrote `board/old.cfg` as this same
    field's only entry -- i.e. this test now covers "tan's own prior value
    gets replaced", and its sibling below,
    `test_an_unrecorded_positional_slot_is_never_overwritten_by_a_bare_merge`,
    covers the NEW case this issue is actually about: the identical file with
    NO provenance for `board/old.cfg` must NOT be overwritten."""
    draft = create_launch_draft(ZEPHYR_MCU, "openocd", None)
    draft["configFiles"] = ["board/new.cfg"]
    existing = json.dumps(
        {
            "version": "0.2.0",
            "configurations": [
                {
                    "name": "Alp: Zephyr Debug (OpenOCD)",
                    "configFiles": ["board/old.cfg", "interface/jlink.cfg"],
                }
            ],
        }
    )
    provenance = launch_provenance.empty().updated(
        "Alp: Zephyr Debug (OpenOCD)", {"configFiles": ["board/old.cfg"]}
    )

    plan = create_launch_json_write_plan(existing, draft, provenance=provenance)

    assert plan.written_configuration["configFiles"] == [
        "board/new.cfg",
        "interface/jlink.cfg",
    ], plan.written_configuration["configFiles"]


def test_an_unrecorded_positional_slot_is_never_overwritten_by_a_bare_merge():
    """tan-cli#518, the gap tan-cli#489 itself named as a "Known, accepted
    limitation": the EXACT scenario above, but with no provenance at all --
    `board/old.cfg` might be the customer's own hand-typed value, or tan's
    own output from a run that predates this sidecar, or simply a sidecar
    that got deleted. Either way, nothing here can PROVE it is tan's, so the
    merge must not gamble: `board/new.cfg` is appended instead of replacing
    it, and `board/old.cfg` survives untouched, in its own place, exactly
    like the customer's `interface/jlink.cfg` beside it always has.

    FAILS against the pre-#518 position-heuristic merge, which overwrote
    `board/old.cfg` unconditionally the moment nothing else claimed that
    slot -- the same test this file's own history shows previously asserted
    the overwrite."""
    draft = create_launch_draft(ZEPHYR_MCU, "openocd", None)
    draft["configFiles"] = ["board/new.cfg"]
    existing = json.dumps(
        {
            "version": "0.2.0",
            "configurations": [
                {
                    "name": "Alp: Zephyr Debug (OpenOCD)",
                    "configFiles": ["board/old.cfg", "interface/jlink.cfg"],
                }
            ],
        }
    )

    plan = create_launch_json_write_plan(existing, draft)

    assert plan.written_configuration["configFiles"] == [
        "board/old.cfg",
        "interface/jlink.cfg",
        "board/new.cfg",
    ], plan.written_configuration["configFiles"]


def test_a_hand_authored_load_files_survives_a_rerun():
    """tan-cli#1020 review BLOCKER: `loadFiles` is a list field, so before
    this fix it silently routed through `configFiles`/`setupCommands`'s
    OWN identity-plus-positional-append merge -- which APPENDS an unmatched
    incoming value beside an existing one it cannot prove is tan's own,
    rather than protecting the existing value the way every OTHER hand-
    filled field in this module does. That is the right call for
    `configFiles` (independently-owned entries a customer and tan can both
    legitimately contribute one of); it is wrong for `loadFiles`, which
    names ONE deliberate artefact list -- appending means cortex-debug
    programs the customer's file AND tan's resolved one. Measured, at this
    review's head, against a customer's own `["${workspaceFolder}/custom/
    app.hex"]`: the pre-fix merge produced `["${workspaceFolder}/custom/
    app.hex", "${workspaceFolder}/build/app/zephyr/zephyr.elf"]`. FAILS
    against that merge; this fix leaves the customer's single entry alone."""
    draft = create_launch_draft(ZEPHYR_MCU, "jlink", None)
    existing = json.dumps(
        {
            "version": "0.2.0",
            "configurations": [
                {
                    "name": "Alp: Zephyr Debug (J-Link)",
                    "loadFiles": ["${workspaceFolder}/custom/app.hex"],
                }
            ],
        }
    )

    plan = create_launch_json_write_plan(existing, draft)

    assert plan.written_configuration["loadFiles"] == [
        "${workspaceFolder}/custom/app.hex"
    ], plan.written_configuration["loadFiles"]


def test_an_explicit_empty_load_files_survives_a_rerun_as_attach_only():
    """tan-cli#1020 review BLOCKER, the SAFETY-critical row: an explicit
    `"loadFiles": []` is `marus25.cortex-debug`'s own documented spelling for
    "program nothing, attach only" -- exactly the fact this issue exists to
    let a customer express. `configFiles`'s own merge rule treats an EMPTY
    existing list as "nothing to protect" (there is no concept of a
    deliberate empty `configFiles`), and reusing that rule for `loadFiles`
    silently turned a customer's attach-only session back into one that
    programs silicon: measured, at this review's head, `[]` -> `["${
    workspaceFolder}/build/app/zephyr/zephyr.elf"]`, at exit 0 with
    `issues: []`. FAILS against that merge; this fix leaves `[]` exactly as
    the customer wrote it."""
    draft = create_launch_draft(ZEPHYR_MCU, "jlink", None)
    existing = json.dumps(
        {
            "version": "0.2.0",
            "configurations": [
                {"name": "Alp: Zephyr Debug (J-Link)", "loadFiles": []}
            ],
        }
    )

    plan = create_launch_json_write_plan(existing, draft)

    assert plan.written_configuration["loadFiles"] == [], (
        plan.written_configuration["loadFiles"]
    )


def test_a_tan_owned_load_files_is_synced_to_a_new_build_resolution():
    """The pairing case for the two tests above: a `loadFiles` this run CAN
    prove -- via `.alp/` provenance -- is tan's OWN prior output must still
    track a fresh resolution, the same "an updated build makes a stale
    value updateable again" rule `configFiles` already gets. Protecting
    every existing value unconditionally would be just as wrong as the
    blocker this test's siblings cover: a rebuild that resolves a new
    per-core ELF must still reach `loadFiles`, or `programsDevice: true`
    would sit beside a stale artefact path."""
    draft = create_launch_draft(ZEPHYR_MCU, "jlink", None)
    draft["loadFiles"] = ["${workspaceFolder}/build/app/zephyr/zephyr_rev_b.elf"]
    existing = json.dumps(
        {
            "version": "0.2.0",
            "configurations": [
                {
                    "name": "Alp: Zephyr Debug (J-Link)",
                    "loadFiles": ["${workspaceFolder}/build/app/zephyr/zephyr_rev_a.elf"],
                }
            ],
        }
    )
    provenance = launch_provenance.empty().updated(
        "Alp: Zephyr Debug (J-Link)",
        {"loadFiles": ["${workspaceFolder}/build/app/zephyr/zephyr_rev_a.elf"]},
    )

    plan = create_launch_json_write_plan(existing, draft, provenance=provenance)

    assert plan.written_configuration["loadFiles"] == [
        "${workspaceFolder}/build/app/zephyr/zephyr_rev_b.elf"
    ], plan.written_configuration["loadFiles"]


def test_a_load_files_key_absent_from_an_existing_entry_is_recorded_as_tan_owned():
    """tan-cli#1020 re-review MAJOR: a `loadFiles` key genuinely ABSENT from an
    existing entry (a pre-#945 `tan` wrote the configuration before this field
    existed, or the `.alp/` sidecar was never shared) is not a customer's value
    to protect -- there is nothing there to have hand-authored. Before this fix
    `_merge_configuration` only populated `owned_entries` on its `(list, list)`
    branch, so this write's own fresh `loadFiles` landed on disk but the
    returned `provenance` recorded NOTHING for it. FAILS pre-fix:
    `hashes_for` returns the empty set even though `loadFiles` is right there
    in `written_configuration`, freshly written by this very run."""
    draft = create_launch_draft(ZEPHYR_MCU, "jlink", None)
    existing = json.dumps(
        {
            "version": "0.2.0",
            "configurations": [
                {"name": "Alp: Zephyr Debug (J-Link)", "servertype": "jlink"}
            ],
        }
    )

    plan = create_launch_json_write_plan(existing, draft)

    assert plan.written_configuration["loadFiles"] == draft["loadFiles"]
    recorded = plan.provenance.hashes_for("Alp: Zephyr Debug (J-Link)", "loadFiles")
    assert recorded == frozenset(
        launch_provenance.content_hash(v) for v in draft["loadFiles"]
    ), recorded


def test_an_explicit_json_null_load_files_is_not_conflated_with_a_missing_key():
    """tan-cli#1020 review round 4 nit: `existing.get(key)` returns `None`
    for BOTH "the key is absent" and "the key is present holding JSON
    `null`" -- an `is None` check cannot tell them apart. `loadFiles: null`
    is a concrete value some tool or hand-edit put in the file, not the
    same fact as the key never having existed; conflating them let a
    pre-fix build silently overwrite it AND record it as tan-owned, as if
    it were the brand-new-entry case. This write must still land the fresh
    value (there is nothing sensible to merge `null` against), but must NOT
    claim ownership of it -- a later run that resolves something different
    again must still be free to protect whatever a customer put there in
    the meantime, the same as any other unprovable existing value."""
    draft = create_launch_draft(ZEPHYR_MCU, "jlink", None)
    existing = json.dumps(
        {
            "version": "0.2.0",
            "configurations": [
                {
                    "name": "Alp: Zephyr Debug (J-Link)",
                    "servertype": "jlink",
                    "loadFiles": None,
                }
            ],
        }
    )

    plan = create_launch_json_write_plan(existing, draft)

    assert plan.written_configuration["loadFiles"] == draft["loadFiles"]
    recorded = plan.provenance.hashes_for("Alp: Zephyr Debug (J-Link)", "loadFiles")
    assert recorded == frozenset(), recorded


def test_the_default_upgrade_path_heals_load_files_within_one_run():
    """The pairing test for the one above, proving the fix actually closes the
    loop rather than merely recording something that goes nowhere: feed run
    1's own returned `provenance` into run 2, the same way the real CLI
    persists it to `.alp/debug-launch-provenance.json` between invocations. A
    rebuild that resolves a NEW per-core ELF between the two runs must still
    reach `loadFiles` on run 2. FAILS pre-fix -- run 2's `loadFiles` stays
    pinned to run 1's own value forever, the review's measured "never heals"
    repro, because run 1 never recorded what it wrote."""
    pre_945_existing = json.dumps(
        {
            "version": "0.2.0",
            "configurations": [
                {"name": "Alp: Zephyr Debug (J-Link)", "servertype": "jlink"}
            ],
        }
    )
    draft_1 = create_launch_draft(ZEPHYR_MCU, "jlink", None)
    run_1 = create_launch_json_write_plan(pre_945_existing, draft_1)
    assert run_1.written_configuration["loadFiles"] == draft_1["loadFiles"]

    draft_2 = create_launch_draft(ZEPHYR_MCU, "jlink", None)
    draft_2["loadFiles"] = ["${workspaceFolder}/build/app/zephyr/zephyr_rev_b.elf"]
    run_2 = create_launch_json_write_plan(run_1.content, draft_2, provenance=run_1.provenance)

    assert run_2.written_configuration["loadFiles"] == [
        "${workspaceFolder}/build/app/zephyr/zephyr_rev_b.elf"
    ], run_2.written_configuration["loadFiles"]


def test_a_hand_authored_load_files_survives_a_real_cli_rerun_and_is_disclosed(tmp_path):
    """The end-to-end counterpart of the two pure-merge blocker tests above,
    through the real CLI and a real `.vscode/launch.json` -- and the
    tan-cli#1020 review's disclosure ask: a write that protects a
    hand-authored `loadFiles` must say so, the same way `configFiles`'s own
    protected-append case gets `debug-config.sdk-identity-appended`."""
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "launch.json").write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "configurations": [
                    {
                        "name": "Alp: Zephyr Debug (J-Link)",
                        "type": "cortex-debug",
                        "request": "launch",
                        "executable": "${workspaceFolder}/build/app/zephyr/zephyr.elf",
                        "loadFiles": [],
                        "servertype": "jlink",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    env = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json")
    )

    assert env["exitCode"] == 0, env
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["configurations"][0]["loadFiles"] == [], on_disk["configurations"][0]
    assert env["data"]["configuration"]["loadFiles"] == []
    preserved_issue = next(
        (i for i in env["issues"] if i["code"] == "debug-config.load-files-preserved"), None
    )
    assert preserved_issue is not None, env["issues"]
    assert preserved_issue["severity"] == "info"
    assert "loadFiles" in preserved_issue["message"]


def test_the_default_upgrade_path_heals_load_files_within_one_run_through_the_real_cli(tmp_path):
    """tan-cli#1020 re-review MAJOR, through the real CLI and a real `.alp/`
    sidecar (not the pure-merge tests above, which the re-review noted "hand-
    feed `launch_provenance.empty().updated(...)`, so nothing exercises
    whether a real write ever *records* `loadFiles`"). Mirrors the
    re-review's own measured repro: a pre-#945-shaped entry (`executable`, no
    `loadFiles` key, no sidecar) -- run 1 writes a fresh `loadFiles` -- then a
    rebuild resolves a NEW per-core ELF -- run 2 must track it, not stay
    pinned to run 1's own value. FAILS pre-fix: run 2's `loadFiles` on disk is
    still run 1's stale ELF path, `executable` and `loadFiles` name different
    files, and `.alp/debug-launch-provenance.json` still records nothing for
    `loadFiles` after two writes."""
    pytest.importorskip("yaml")
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "launch.json").write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "configurations": [
                    {
                        "name": "Alp: Zephyr Debug (J-Link)",
                        "type": "cortex-debug",
                        "request": "launch",
                        "executable": "${workspaceFolder}/build/app/zephyr/zephyr.elf",
                        "servertype": "jlink",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert not provenance_sidecar(tmp_path).exists()

    run_1 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json")
    )
    assert run_1["exitCode"] == 0, run_1
    after_run_1 = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    load_files_after_run_1 = after_run_1["configurations"][0]["loadFiles"]
    assert load_files_after_run_1 == [after_run_1["configurations"][0]["executable"]]
    sidecar_after_run_1 = json.loads(provenance_sidecar(tmp_path).read_text(encoding="utf-8"))
    assert sidecar_after_run_1["configurations"]["Alp: Zephyr Debug (J-Link)"]["loadFiles"], (
        "run 1's own fresh loadFiles must be recorded, or run 2 can never prove it its own"
    )

    root = str(tmp_path).replace("\\", "/")
    build_dir = f"{root}/build/m55_hp-zephyr/build"
    write_manifest(
        tmp_path,
        "schema_version: 1\nslices:\n- core_id: m55_hp\n  os: zephyr\n"
        f"  board: alp_x\n  build_dir: {build_dir}\n"
        f"  output_artefact: {build_dir}/zephyr/zephyr_rev_b.elf\n",
    )

    run_2 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json")
    )

    assert run_2["exitCode"] == 0, run_2
    after_run_2 = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    entry = after_run_2["configurations"][0]
    assert entry["executable"].endswith("zephyr_rev_b.elf"), entry
    assert entry["loadFiles"] == [entry["executable"]], entry
    assert not any(i["code"] == "debug-config.load-files-preserved" for i in run_2["issues"]), (
        "a provably tan-owned loadFiles must sync, not be reported as preserved"
    )


def test_the_load_files_preserved_disclosure_reaches_text_mode(tmp_path):
    """tan-cli#1020 review round 4: `debug-config.load-files-preserved` was
    only ever rendered under `--format json` -- `launch_provenance.py`'s own
    "disclosed, every run" claim held for a JSON consumer and NOT for a
    customer at a terminal, the DEFAULT output mode. Measured pre-fix: the
    residual case (a `loadFiles` this run cannot prove is tan's own, e.g. a
    sidecar lost while the key was already present) printed three routine
    `note:` lines and exited 0 with no hint that `executable` and the
    actually-programmed `loadFiles` had just diverged -- exactly the silent
    divergence this `flash-path`/`safety`-labelled issue exists to prevent.
    FAILS pre-fix: no `note:` line mentions `loadFiles` at all. Shown even
    under `--quiet`, the same as `debug-config.comments-dropped`."""
    pytest.importorskip("yaml")
    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "launch.json").write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "configurations": [
                    {
                        "name": "Alp: Zephyr Debug (J-Link)",
                        "type": "cortex-debug",
                        "request": "launch",
                        "executable": "${workspaceFolder}/build/app/zephyr/zephyr_rev_a.elf",
                        "servertype": "jlink",
                        "loadFiles": ["${workspaceFolder}/build/app/zephyr/zephyr_rev_a.elf"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    root = str(tmp_path).replace("\\", "/")
    build_dir = f"{root}/build/m55_hp-zephyr/build"
    write_manifest(
        tmp_path,
        "schema_version: 1\nslices:\n- core_id: m55_hp\n  os: zephyr\n"
        f"  board: alp_x\n  build_dir: {build_dir}\n"
        f"  output_artefact: {build_dir}/zephyr/zephyr_rev_b.elf\n",
    )

    # tan-cli#182: stdout is the envelope channel in BOTH modes; the human
    # text preview is stderr (see `test_text_mode_writes_nothing_to_stdout`).
    proc = run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == ""
    load_files_notes = [
        line for line in proc.stderr.splitlines() if line.startswith("note:") and "loadFiles" in line
    ]
    assert load_files_notes, proc.stderr
    assert "zephyr_rev_a.elf" in load_files_notes[0]
    assert "zephyr_rev_b.elf" in load_files_notes[0]

    proc_quiet = run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--quiet")
    assert proc_quiet.returncode == 0, proc_quiet.stdout + proc_quiet.stderr
    assert any("loadFiles" in line for line in proc_quiet.stderr.splitlines()), proc_quiet.stderr


def test_three_real_cli_runs_replace_configfiles_each_time_not_accumulate(tmp_path):
    """tan-cli#489 review round (second pass): the reviewer's own end-to-end
    repro, through the real CLI and a real (rewritten between runs)
    `runners.yaml` -- not just the unit-level `create_launch_json_write_plan`
    calls above. FAILS against the identity-only merge, which left all three
    revisions in `configFiles` after the third run."""
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
    runners_yaml = zephyr_dir / "runners.yaml"

    for rev in "a", "b", "c":
        runners_yaml.write_text(
            "runners:\n- openocd\n"
            f"args:\n  openocd:\n  - --config=board/alp_rev_{rev}.cfg\n",
            encoding="utf-8",
        )
        env = envelope(
            run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
        )
        assert env["exitCode"] == 0, env
        assert env["data"]["configuration"]["configFiles"] == [f"board/alp_rev_{rev}.cfg"], (
            rev,
            env["data"]["configuration"]["configFiles"],
        )
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["configurations"][0]["configFiles"] == ["board/alp_rev_c.cfg"], on_disk


def test_provenance_survives_the_targeted_splice_write_path(tmp_path):
    """tan-cli#518's own explicit callout: `jsonc_splice` preserves byte
    spans -- everything OUTSIDE the one entry being rewritten is copied
    through unconditionally (tan-cli#182) -- so this proves the sidecar's own
    identity is unaffected by which write path (`jsonc_splice.apply_edit` vs
    the whole-document fallback `jsonc_splice.pretty_json`) actually produced
    a given `launch.json` on disk. The comment above `configurations` forces
    `_write_content` down the TARGETED SPLICE path (the same one
    `test_a_write_into_the_stock_template_keeps_every_byte_outside_the_entry`
    pins) on every write here, never the fallback -- if content-hash
    provenance were somehow keyed to raw file bytes instead of the PARSED
    value, splicing (which never re-serialises the untouched comment, but
    DOES re-serialise the one entry it rewrites) would be exactly the kind of
    thing that could desync it."""
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
    runners_yaml = zephyr_dir / "runners.yaml"
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(STOCK_TEMPLATE, encoding="utf-8")

    for rev in "a", "b", "c":
        runners_yaml.write_text(
            "runners:\n- openocd\n"
            f"args:\n  openocd:\n  - --config=board/splice_rev_{rev}.cfg\n",
            encoding="utf-8",
        )
        env = envelope(
            run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
        )
        assert env["exitCode"] == 0, env
        # The comment survives every single write -- proof the targeted
        # splice path ran, not the whole-document fallback (which would have
        # destroyed it on the very first write).
        assert "// Use IntelliSense" in launch_json(tmp_path).read_text(encoding="utf-8")

    on_disk = json.loads(strip_jsonc(launch_json(tmp_path).read_text(encoding="utf-8")))
    # Not accumulated: if provenance had desynced from the real (spliced)
    # file, rev "b" and "c" would each have found no recorded hash for rev
    # "a"'s entry and appended instead, same failure shape as tan-cli#489.
    assert on_disk["configurations"][0]["configFiles"] == ["board/splice_rev_c.cfg"], on_disk


def test_a_customer_edit_of_a_tans_own_entry_orphans_it_instead_of_overwriting_it(tmp_path):
    """tan-cli#518's central scenario, end to end through the real CLI and a
    real (rewritten between runs) `runners.yaml`: tan writes a resolved
    `configFiles` value on run 1 (recorded in `.alp/` as tan's own), the
    CUSTOMER then hand-edits that exact entry in `launch.json` -- the desync
    the whole sidecar exists to survive -- and a SECOND real build resolves
    a yet-DIFFERENT value on run 2. The edited entry's current content no
    longer hashes to what run 1 recorded, so it reads as "not tan's any
    more": run 2 must APPEND its own new value beside the customer's edit,
    never silently overwrite what they just typed. FAILS against the
    pre-#518 position-heuristic merge, which would have overwritten the
    customer's edit unconditionally (nothing but position identified that
    slot)."""
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
    runners_yaml = zephyr_dir / "runners.yaml"
    runners_yaml.write_text(
        "runners:\n- openocd\nargs:\n  openocd:\n  - --config=board/rev_1.cfg\n",
        encoding="utf-8",
    )
    env_1 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
    )
    assert env_1["exitCode"] == 0, env_1
    assert env_1["data"]["configuration"]["configFiles"] == ["board/rev_1.cfg"]

    # The customer opens launch.json and edits the value tan just wrote --
    # the sidecar still names the OLD content, `board/rev_1.cfg`, as tan's;
    # what's on disk now is something else entirely.
    on_disk_1 = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    on_disk_1["configurations"][0]["configFiles"] = ["my/own/handpicked.cfg"]
    launch_json(tmp_path).write_text(json.dumps(on_disk_1), encoding="utf-8")

    runners_yaml.write_text(
        "runners:\n- openocd\nargs:\n  openocd:\n  - --config=board/rev_2.cfg\n",
        encoding="utf-8",
    )
    env_2 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
    )
    assert env_2["exitCode"] == 0, env_2

    on_disk_2 = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk_2["configurations"][0]["configFiles"] == [
        "my/own/handpicked.cfg",
        "board/rev_2.cfg",
    ], on_disk_2


def test_a_customer_edit_stays_orphaned_through_a_third_run_never_reclaimed(tmp_path):
    """tan-cli#982 review finding #1 (1): `_merge_list_by_identity`'s own
    docstring promises pass-3 (appended) entries are NEVER recorded as
    tan-owned -- only pass 1's identity matches and pass 2's positional
    placements are. The test above
    (`test_a_customer_edit_of_a_tans_own_entry_orphans_it_instead_of_
    overwriting_it`) only runs TWO builds, which cannot catch a violation of
    that promise: nothing would wrongly treat the customer's still-orphaned
    edit as tan's own until something LOOKS UP what run 2 recorded, and
    nothing does that until a THIRD run. Reproduces the reviewer's own probe
    end to end: run 1 resolves and records `board/rev_1.cfg`; the customer
    hand-edits that exact entry to `my/own/handpicked.cfg`; run 2 cannot
    match it any more (the content hash disagrees) so it APPENDS
    `board/rev_2.cfg` beside it -- and, per the docstring's promise, must
    NOT record the still-unmatched `my/own/handpicked.cfg` as tan's own even
    though it now sits in the merged result; run 3 must therefore still find
    nothing it can prove is tan's at slot 0 and append again, never
    overwrite the customer's edit. FAILS against `owned_entries =
    list(result)` (claiming every merged entry as owned, including the ones
    pass 1/2 never matched or placed) -- there, run 2 wrongly records
    `my/own/handpicked.cfg` as tan's own, and run 3 overwrites it outright,
    deleting it: `['board/rev_3.cfg', 'board/rev_2.cfg']` instead of this
    test's own assertion below."""
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
    runners_yaml = zephyr_dir / "runners.yaml"
    runners_yaml.write_text(
        "runners:\n- openocd\nargs:\n  openocd:\n  - --config=board/rev_1.cfg\n",
        encoding="utf-8",
    )
    env_1 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
    )
    assert env_1["exitCode"] == 0, env_1
    assert env_1["data"]["configuration"]["configFiles"] == ["board/rev_1.cfg"]

    on_disk_1 = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    on_disk_1["configurations"][0]["configFiles"] = ["my/own/handpicked.cfg"]
    launch_json(tmp_path).write_text(json.dumps(on_disk_1), encoding="utf-8")

    runners_yaml.write_text(
        "runners:\n- openocd\nargs:\n  openocd:\n  - --config=board/rev_2.cfg\n",
        encoding="utf-8",
    )
    env_2 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
    )
    assert env_2["exitCode"] == 0, env_2
    on_disk_2 = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk_2["configurations"][0]["configFiles"] == [
        "my/own/handpicked.cfg",
        "board/rev_2.cfg",
    ], on_disk_2

    runners_yaml.write_text(
        "runners:\n- openocd\nargs:\n  openocd:\n  - --config=board/rev_3.cfg\n",
        encoding="utf-8",
    )
    env_3 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
    )
    assert env_3["exitCode"] == 0, env_3
    on_disk_3 = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk_3["configurations"][0]["configFiles"] == [
        "my/own/handpicked.cfg",
        "board/rev_3.cfg",
    ], on_disk_3


def test_a_run_that_resolves_nothing_for_a_field_leaves_its_provenance_record_untouched(
    tmp_path,
):
    """tan-cli#982 review finding #1 (2): `_merge_list_field`'s all-placeholder
    guard returns `owned_entries=[]` for a run that resolved NOTHING for a
    list field, and `_merge_configuration`'s own docstring promises that
    means the field's `.alp/` provenance record is left EXACTLY as it
    already was, never wiped -- gated by `if owned_entries_out is not None
    and owned:`. Reproduces the reviewer's own probe end to end: run 1
    resolves and records `board/rev_1.cfg` as tan's own; run 2's
    `runners.yaml` is REMOVED entirely (a build that has not been re-run
    against this server, or registers no openocd runner at all), so
    `configFiles` resolves to nothing but tan's own placeholder and the
    all-placeholder guard fires -- this run touches NOTHING for the field;
    run 3's `runners.yaml` comes back with a fresh value. If run 2 had wiped
    the field's provenance record instead of leaving it alone, run 3 has
    nothing left to match `board/rev_1.cfg` against and must APPEND rather
    than replace it -- tan-cli#489's own accumulation blocker regressing,
    silently. FAILS against dropping the `and owned` half of that guard's
    condition (`debug_launch.py`'s `_merge_configuration`): there, run 3
    yields `['board/rev_1.cfg', 'board/rev_3.cfg']` instead of this test's
    own assertion below."""
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
    runners_yaml = zephyr_dir / "runners.yaml"
    runners_yaml.write_text(
        "runners:\n- openocd\nargs:\n  openocd:\n  - --config=board/rev_1.cfg\n",
        encoding="utf-8",
    )
    env_1 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
    )
    assert env_1["exitCode"] == 0, env_1
    assert env_1["data"]["configuration"]["configFiles"] == ["board/rev_1.cfg"]

    # A run with no `runners.yaml` at all: `configFiles` resolves to nothing
    # but tan's own placeholder, so the all-placeholder guard in
    # `_merge_list_field` fires and this run touches NOTHING for the field.
    runners_yaml.unlink()
    env_2 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
    )
    assert env_2["exitCode"] == 0, env_2
    on_disk_2 = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk_2["configurations"][0]["configFiles"] == ["board/rev_1.cfg"], on_disk_2

    runners_yaml.write_text(
        "runners:\n- openocd\nargs:\n  openocd:\n  - --config=board/rev_3.cfg\n",
        encoding="utf-8",
    )
    env_3 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
    )
    assert env_3["exitCode"] == 0, env_3
    on_disk_3 = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk_3["configurations"][0]["configFiles"] == ["board/rev_3.cfg"], on_disk_3


def test_a_multi_element_draft_with_a_customer_prepended_entry_still_replaces(tmp_path):
    """tan-cli#489 review round (THIRD pass): the BLOCKER the reviewer chose
    the algorithm for. The test above only ever resolves ONE `--config`, so
    the positional fallback's own index (draft position 0) always lined up
    with `existing`'s first free slot by coincidence -- a multi-`--config`
    board (ordinary: an interface driver plus a target config is the normal
    OpenOCD shape) with a customer-prepended entry shifts the two index
    spaces out of alignment, and the SECOND-round fix's index-relative
    fallback (`existing[i]` for the unmatched draft item's own index `i`)
    never lands on a free slot: it is always either consumed by the
    `interface` match, or past `len(existing)`, so it falls to the `append`
    branch on every single run. `openocd_search`/`searchDir` (a second,
    independently-merged list field) is exercised the same way, since it is
    reachable through the exact same code path. FAILS against the
    THIRD-round algorithm's own predecessor (`e19ef39`'s index-relative
    fallback): three runs there leave `configFiles` with FOUR target
    revisions (one per run plus the customer's), not one."""
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
    runners_yaml = zephyr_dir / "runners.yaml"

    # rev_a establishes tan's own entries FIRST, through an ordinary run --
    # this is what gives `interface/cmsis-dap.cfg` (and the stable
    # `openocd_search` entry) their own ANCHOR for every later run to match
    # against. The customer then hand-PREPENDS their own entries, between
    # rev_a and rev_b -- the exact sequence the issue's own evidence
    # describes ("a hand-added second .cfg" added to an already-tan-managed
    # file), not a customer authoring the file from nothing.
    runners_yaml.write_text(
        "runners:\n- openocd\n"
        "config:\n  openocd_search:\n  - /opt/zephyr-sdk/scripts\n"
        f"  - {root}/build/a/zephyr\n"
        "args:\n  openocd:\n  - --config=interface/cmsis-dap.cfg\n"
        "  - --config=target/rev_a.cfg\n",
        encoding="utf-8",
    )
    env_a = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
    )
    assert env_a["exitCode"] == 0, env_a

    on_disk_a = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    on_disk_a["configurations"][0]["configFiles"].insert(0, "custom/pre.cfg")
    on_disk_a["configurations"][0]["searchDir"].insert(0, "/home/me/my-scripts")
    launch_json(tmp_path).write_text(json.dumps(on_disk_a), encoding="utf-8")

    for rev in "b", "c", "d":
        runners_yaml.write_text(
            "runners:\n- openocd\n"
            "config:\n  openocd_search:\n  - /opt/zephyr-sdk/scripts\n"
            f"  - {root}/build/{rev}/zephyr\n"
            "args:\n  openocd:\n  - --config=interface/cmsis-dap.cfg\n"
            f"  - --config=target/rev_{rev}.cfg\n",
            encoding="utf-8",
        )
        env = envelope(
            run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "openocd", "--format", "json")
        )
        assert env["exitCode"] == 0, env
        config = env["data"]["configuration"]
        assert config["configFiles"] == [
            "custom/pre.cfg",
            "interface/cmsis-dap.cfg",
            f"target/rev_{rev}.cfg",
        ], (rev, config["configFiles"])
        assert config["searchDir"] == [
            "/home/me/my-scripts",
            "/opt/zephyr-sdk/scripts",
            f"{root}/build/{rev}/zephyr",
        ], (rev, config["searchDir"])

    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["configurations"][0]["configFiles"] == [
        "custom/pre.cfg",
        "interface/cmsis-dap.cfg",
        "target/rev_d.cfg",
    ], on_disk


def test_a_hand_written_setup_commands_list_survives_a_one_element_draft(tmp_path):
    """tan-cli#489 (3): `setupCommands` is the case the all-placeholder guard
    can NEVER reach -- its elements are dicts, never `<...>` strings, so
    `all(_is_unresolved(v) for v in next_value)` is always False. Reproduces
    the issue's own measured scenario: a real remote-gdb `setupCommands` list
    (pretty-printing + a sysroot + a solib-search-path) reduced to the
    draft's single hardcoded element, silently, at exit 0."""
    launch_json(tmp_path).parent.mkdir()
    launch_json(tmp_path).write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "configurations": [
                    {
                        "name": "Alp: Yocto Remote Debug",
                        "type": "cppdbg",
                        "setupCommands": [
                            {"text": "-enable-pretty-printing", "ignoreFailures": True},
                            {
                                "text": "-gdb-set sysroot "
                                "/opt/poky/4.0/sysroots/cortexa55-poky-linux"
                            },
                            {
                                "text": "set solib-search-path "
                                "/opt/poky/4.0/sysroots/cortexa55-poky-linux/usr/lib"
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    env = envelope(
        run_cli(
            tmp_path, "--target-kind", YOCTO_USERSPACE, "--server", GDBSERVER,
            "--gdbserver-address", "192.168.1.42:2345", "--format", "json",
        )
    )

    assert env["exitCode"] == 0
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    setup_commands = on_disk["configurations"][0]["setupCommands"]
    assert setup_commands == [
        # The customer's own `ignoreFailures` key survives -- our draft never
        # writes that key, so a wholesale per-index replace would have
        # dropped it even on the one element that DID match.
        {"text": "-enable-pretty-printing", "ignoreFailures": True},
        {"text": "-gdb-set sysroot /opt/poky/4.0/sysroots/cortexa55-poky-linux"},
        {
            "text": "set solib-search-path "
            "/opt/poky/4.0/sysroots/cortexa55-poky-linux/usr/lib"
        },
    ], setup_commands


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


def test_pre_launch_task_empty_string_opt_out_removes_it_on_a_real_write(tmp_path):
    """tan-cli#489 (6): the write-path counterpart of the preview test above,
    which never reaches the merge at all (`--preview` returns before the
    customer's file is even read). Sequence from the issue's own repro: run
    once (writes the restored v0.3.1 default), then again with
    `--pre-launch-task ''` -- the key must be GONE from the file, not merely
    absent from a fresh preview draft. FAILS against the pre-fix code, which
    left `\"preLaunchTask\": \"alp: build active target\"` in place after the
    second run (`create_launch_draft` deletes the key from its OWN draft, but
    `_merge_configuration` only visits the draft's keys, so a key the draft
    doesn't carry is never removed from what already exists)."""
    env1 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--format", "json")
    )
    assert env1["exitCode"] == 0
    assert (
        json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))["configurations"][0][
            "preLaunchTask"
        ]
        == "alp: build active target"
    )

    env2 = envelope(
        run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                "--pre-launch-task", "", "--format", "json")
    )
    assert env2["exitCode"] == 0
    assert "preLaunchTask" not in env2["data"]["configuration"]
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert "preLaunchTask" not in on_disk["configurations"][0], on_disk


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
    assert env["exitCode"] == 2
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


# ---------------------------------------------------------------------------
# tan-cli#456: an omitted --target-kind must not silently default to
# native-host on a project whose build can never produce that binary.
# ---------------------------------------------------------------------------

#: The exact shape from the tan-cli#456 report: a real hardware SoM
#: (`som.sku`) whose only built slices are Zephyr on `m55_hp` / `m55_he` --
#: `native_sim` was never built. Before the fix, the omitted --target-kind
#: still resolved `native-host`/`none` and wrote a launch.json `program`
#: pointing at `build/native_sim/zephyr/zephyr.exe`, a binary this project
#: never produces.
MANIFEST_HARDWARE_ONLY_NO_NATIVE_SIM = """\
schema_version: 1
hw_info:
  sku: E1M-AEN801
slices:
- core_id: m55_hp
  os: zephyr
  board: alp_e1m_aen801_m55_hp
  status: ok
  build_dir: {root}/build/m55_hp-zephyr/build
  output_artefact: {root}/build/m55_hp-zephyr/build/zephyr/zephyr.elf
- core_id: m55_he
  os: zephyr
  board: alp_e1m_aen801_m55_he
  status: ok
  build_dir: {root}/build/m55_he-zephyr/build
  output_artefact: {root}/build/m55_he-zephyr/build/zephyr/zephyr.elf
ipc: []
helper_mcus: []
boot_order: []
"""

MANIFEST_MIXED_ZEPHYR_AND_YOCTO = """\
schema_version: 1
slices:
- core_id: m33_sm
  os: zephyr
  board: alp_e1m_v2n101_m33_sm
  output_artefact: {root}/build/m33_sm-zephyr/build/zephyr/zephyr.elf
- core_id: a55_cluster
  os: yocto
  image: core-image-minimal
ipc: []
helper_mcus: []
boot_order: []
"""


def test_an_omitted_target_kind_infers_zephyr_mcu_from_the_built_manifest(tmp_path):
    """tan-cli#456 reproduced verbatim: `som.sku: E1M-AEN801` + `m55_hp`/
    `m55_he` Zephyr slices, no `native_sim` slice anywhere. Before the fix
    this wrote `target=native-host server=none` and a `program` pointing at
    `build/native_sim/zephyr/zephyr.exe`, a binary this project never
    produces -- FAILS against the pre-fix code, which hardcodes
    `parse_target_kind(None) == NATIVE_HOST` with no project inspection at
    all."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n"
        "  m55_he:\n    app: ./src_he\n",
        encoding="utf-8",
    )
    root = str(tmp_path).replace("\\", "/")
    write_manifest(tmp_path, MANIFEST_HARDWARE_ONLY_NO_NATIVE_SIM.format(root=root))

    env = envelope(run_cli(tmp_path, "--format", "json"))

    assert env["exitCode"] == 0, env
    assert env["data"]["targetKind"] == ZEPHYR_MCU
    assert env["data"]["server"] == JLINK
    assert env["data"]["configuration"]["type"] == "cortex-debug"
    executable = env["data"]["configuration"]["executable"]
    assert "native_sim" not in executable, (
        f"must not point at a binary this project never builds: {executable}"
    )
    assert executable == "${workspaceFolder}/build/m55_hp-zephyr/build/zephyr/zephyr.elf"
    on_disk = json.loads(launch_json(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["configurations"][0]["executable"] == executable
    # tan-cli#456 review (minor): the inference is otherwise silent -- say so,
    # the same "never a silent no-op" floor the --svd/--gdbserver-address
    # notes already hold to.
    assert any(
        "inferred 'zephyr-mcu'" in n and "build/system-manifest.yaml" in n
        for n in env["data"]["notes"]
    ), env["data"]["notes"]


def test_a_project_that_does_not_exist_is_refused_not_created(tmp_path):
    """tan-cli#476: `--project` names a project that EXISTS.

    A typo'd path used to be MATERIALISED -- the writer calls
    `mkdir(parents=True)`, so `debug-config --project <ghost>` created the
    directory, wrote a `native_sim` launch.json into it, and reported exit 0
    with `issues: []`. Nothing downstream could tell that apart from writing
    into a real project.

    A deliberate divergence from the Rust oracle, which does the same thing
    (measured: `target/release/tan debug-config --project <ghost>` exits 0 and
    leaves `<ghost>/.vscode/launch.json`). No parity CASE pins it -- every
    frozen `debug-config` argv runs in an existing work_dir -- so no frozen
    comparison changes.
    """
    ghost = tmp_path / "no-such-project"

    env = envelope(
        run_cli(tmp_path, "--project", str(ghost), "--preview", "--format", "json")
    )

    assert env["exitCode"] == 2, env
    assert env["ok"] is False, env
    assert "debug-config.project-not-found" in [i["code"] for i in env["issues"]]
    assert not ghost.exists(), "refused, but the directory was created anyway"


def test_a_refused_project_is_not_created_even_on_the_writing_path(tmp_path):
    """The guard runs BEFORE anything can write, so it holds without
    `--preview` too -- which is the invocation that actually creates files.

    Review round: the writing path used to pin only the exit code, so a
    future change routing it to a DIFFERENT exit-2 code would still pass.
    Pin the issue code here too, same as the `--preview` sibling above."""
    ghost = tmp_path / "no-such-project-write"

    env = envelope(run_cli(tmp_path, "--project", str(ghost), "--format", "json"))

    assert env["exitCode"] == 2, env
    assert env["ok"] is False, env
    assert "debug-config.project-not-found" in [i["code"] for i in env["issues"]]
    assert not ghost.exists(), "refused, but the directory was created anyway"


def test_a_project_arg_that_is_an_existing_file_is_refused_as_not_a_directory(tmp_path):
    """Review round: `os.path.isdir` alone cannot distinguish "missing" from
    "exists but is a file" -- a `--project` pointing at, say, `board.yaml`
    genuinely exists, so telling the caller it "does not exist" is false.
    Same code and exit as the missing-path case; the message names the real
    reason."""
    a_file = tmp_path / "board.yaml"
    a_file.write_text("som:\n  sku: E1M-AEN801\n")

    env = envelope(
        run_cli(tmp_path, "--project", str(a_file), "--preview", "--format", "json")
    )

    assert env["exitCode"] == 2, env
    assert env["ok"] is False, env
    assert "debug-config.project-not-found" in [i["code"] for i in env["issues"]]
    message = env["issues"][0]["message"]
    assert "is not a directory" in message, message
    assert "does not exist" not in message, message


@pytest.mark.parametrize(
    "argv,what",
    [
        (("--target-kind", "bogus-kind"), "--target-kind"),
        (("--target-kind", "zephyr-mcu", "--server", "bogus-srv"), "--server"),
        # A legal server for the wrong target class.
        (("--target-kind", "zephyr-mcu", "--server", GDBSERVER), "target+server pairing"),
        (
            ("--target-kind", "zephyr-mcu", "--server", JLINK, "--svd", "no/such/file.svd"),
            "--svd",
        ),
        (
            (
                "--target-kind", YOCTO_USERSPACE, "--server", GDBSERVER,
                "--gdbserver-address", "",
            ),
            "--gdbserver-address",
        ),
    ],
    ids=["target-kind", "server", "pairing", "svd", "gdbserver-address"],
)
def test_a_bad_flag_value_is_the_callers_input_not_a_tan_crash(tmp_path, argv, what):
    """tan-cli#477: exit 2, never 5.

    Every one of these already produced a complete, actionable message --

        Unsupported --target-kind 'bogus-kind'. Allowed values: zephyr-mcu,
        baremetal-mcu, yocto-userspace, native-host.

    -- and reported it as `debug-config.internal-failure` at exit 5, which
    tells the user tan crashed and tells CI to treat a typo as a tool defect.
    tan-cli#462 made that argument for the four PRECONDITIONS; this is the
    argument-validation half it left behind.

    EVERY CASE HERE MUST REACH ITS OWN REFUSAL, which is what an earlier
    revision of this test got wrong (caught in review of #508). `--server`
    defaults to `SERVER_NONE`, and `_SERVER_CHOICES[ZEPHYR_MCU]` does not
    contain it, so `create_launch_draft` raises on the SERVER before a later
    flag is consulted at all: a `--core`/`--svd`/`--gdbserver-address` case
    that omits `--server` passes on the server refusal and would pass
    identically with its own flag deleted. Each case below therefore supplies
    a server its target accepts, except the two whose subject IS the server.

    `--gdbserver-address` is deliberately the EMPTY string: that flag is not
    shape-validated by design (`_resolve_gdbserver_address`'s own docstring --
    "there is no single `host:port` shape narrow enough to validate without
    rejecting a real one"), so `not-an-address` exits 0 and only `""` refuses.

    `--core` is absent from this list on purpose: its refusal is
    `debug-config.core-unknown`, not this code, and it needs a built manifest
    to reach -- see `test_an_unknown_core_with_an_explicit_target_kind_refuses`.
    """
    env = envelope(run_cli(tmp_path, "--preview", "--format", "json", *argv))

    assert env["exitCode"] == 2, f"{what}: {env}"
    codes = [i["code"] for i in env["issues"]]
    assert "debug-config.invalid-argument" in codes, f"{what}: {codes}"
    assert "debug-config.internal-failure" not in codes, (
        f"{what}: still reported as a tan crash"
    )


@pytest.mark.parametrize(
    "argv,want_target,want_server",
    [
        (("--target-kind", "bogus-kind"), ZEPHYR_MCU, "none"),
        (
            ("--target-kind", NATIVE_HOST, "--server", JLINK),
            NATIVE_HOST,
            JLINK,
        ),
        (
            ("--target-kind", YOCTO_USERSPACE, "--server", GDBSERVER,
             "--gdbserver-address", ""),
            YOCTO_USERSPACE,
            GDBSERVER,
        ),
    ],
    ids=["unparsed", "pairing", "parsed"],
)
def test_a_refusal_reports_the_target_and_server_it_actually_knows(
    tmp_path, argv, want_target, want_server
):
    """tan-cli#477 review: the placeholder is correct only where nothing has
    been parsed yet.

    A refusal raised while PARSING `--target-kind`/`--server` genuinely does
    not know them, so `zephyr-mcu`/`none` is the honest answer -- that is the
    `unparsed` case. A refusal raised AFTER -- an unsupported target+server
    PAIRING (`create_launch_draft`, both values individually valid but not
    together -- `native-host`/`jlink` here, matching the exact customer
    report), `--svd`, or `--gdbserver-address` -- knows both, and reporting
    the placeholder there misdescribes what the caller asked for. That excuse
    held while this was a crash report; it does not hold for a validation
    verdict the extension may render.
    """
    env = envelope(run_cli(tmp_path, "--preview", "--format", "json", *argv))

    assert env["exitCode"] == 2, env
    assert env["data"]["targetKind"] == want_target, env["data"]
    assert env["data"]["server"] == want_server, env["data"]


def test_an_unknown_core_with_an_explicit_target_kind_refuses(tmp_path):
    """The `--core` half, which needs a built manifest to reach at all.

    With `--target-kind` supplied, `infer_target_kind`'s own `core-unknown`
    guard never runs -- that path is the OMITTED-`--target-kind` one. tan-cli
    #489 added `_explicit_core_unknown_failure` for this one; pinned here
    because #477's original report mis-attributed an exit 5 to `--core` (it
    was the server refusal, with `--server` omitted), and the correction only
    holds if something measures the real path.
    """
    build = tmp_path / "build"
    build.mkdir()
    (build / "system-manifest.yaml").write_text(
        "schema_version: 1\n"
        "hw_info: {sku: E1M-AEN801}\n"
        "slices:\n"
        "  - {core_id: m33_sm, os: zephyr, board: alp_e1m_aen801_m33, build_dir: build/app}\n",
        encoding="utf-8",
    )

    bad = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                           "--core", "no_such_core", "--preview", "--format", "json"))
    assert bad["exitCode"] == 2, bad
    assert "debug-config.core-unknown" in [i["code"] for i in bad["issues"]]

    good = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                            "--core", "m33_sm", "--preview", "--format", "json"))
    assert good["exitCode"] == 0, good


def test_an_unknown_core_with_no_manifest_is_refused_against_the_sdks_core_list(tmp_path):
    """tan-cli#477 major 2: INVERTS the assertion this test used to carry.

    It used to be `test_an_unknown_core_with_no_manifest_at_all_is_a_known_
    open_gap`, pinning `exitCode == 0` / `issues == []` and citing #508's
    "deliberately left" note. The reason recorded there was that a guard
    keyed on "does a build manifest exist" cannot tell a typo apart from
    `--core`'s SECOND, legitimate pre-build job -- selecting which core's
    SDK-published debug-probe identity to resolve (alp-sdk#1026) -- "without
    also consulting the SDK's own published core list". That is precisely
    what the fix does: the SoC JSON's own `cores[].id` (union the
    `variants[].debug.jlink_device` keys) IS a published core list, and it is
    already resolvable at this call site from the same `--sdk-root` the
    identity fallback reads.

    Measured against the real metadata this validates against
    (alp-sdk `metadata/socs/alif/ensemble/e8.json`): `cores` is
    `a32_cluster`, `m55_hp`, `m55_he`, and `jlink_device` keys `m55_hp`/
    `m55_he` -- so the union is the SoC's whole core vocabulary and
    `jlink_device` never widens it. The same ids are what a
    `build/system-manifest.yaml` slice spells as `core_id`.

    Without the refusal this writes `"device": "<resolved-device>"` into
    `.vscode/launch.json` at exit 0: a file that looks valid, that the
    debugger then fails on, with nothing connecting the failure back to this
    command."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801\n", encoding="utf-8"
    )
    write_sdk_fixture(tmp_path)

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                           "--core", "no_such_core", "--sdk-root", "./sdk",
                           "--preview", "--format", "json"))

    assert env["exitCode"] == 2, env
    assert "debug-config.core-unknown" in [i["code"] for i in env["issues"]], env
    message = next(i["message"] for i in env["issues"]
                   if i["code"] == "debug-config.core-unknown")
    # Names the typo AND the cores it could have meant -- the same standard
    # `explicit_core_unknown_message` holds for the with-manifest half.
    assert "no_such_core" in message, message
    for core in ("a32_cluster", "m55_hp", "m55_he"):
        assert core in message, message


@pytest.mark.parametrize("server", [JLINK, "openocd", "pyocd"])
def test_an_unknown_core_is_refused_for_every_server_not_just_jlink(tmp_path, server):
    """tan-cli#477 major 2, the rest of the surface. The `device` placeholder
    that made the jlink case visible at all is a J-Link field; measured
    before the fix, `--server openocd` reported only
    `debug-config.sdk-identity-key-absent` (which says nothing about the
    core) and `--server pyocd` reported `issues: []` -- strictly more silent
    than the case the issue quotes. `--core` is not a per-server flag, so a
    core the SoM does not have is refused the same way whatever server was
    asked for."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    write_sdk_fixture(tmp_path)

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", server,
                           "--core", "no_such_core", "--sdk-root", "./sdk",
                           "--preview", "--format", "json"))

    assert env["exitCode"] == 2, env
    assert "debug-config.core-unknown" in [i["code"] for i in env["issues"]], env


def test_an_unknown_core_refusal_reports_the_resolved_target_and_server(tmp_path):
    """The same standard `test_a_refusal_reports_the_target_and_server_it_
    actually_knows` holds for the sibling refusals: both are already parsed
    by the time this guard runs, so the `zephyr-mcu`/`none` placeholder pair
    would misdescribe what the caller asked for."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    write_sdk_fixture(tmp_path)

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", "pyocd",
                           "--core", "no_such_core", "--sdk-root", "./sdk",
                           "--preview", "--format", "json"))

    assert env["exitCode"] == 2, env
    assert env["data"]["targetKind"] == ZEPHYR_MCU, env["data"]
    assert env["data"]["server"] == "pyocd", env["data"]


def test_an_unknown_core_with_no_manifest_is_refused_before_the_write(tmp_path):
    """The write half of the same defect -- and the one the issue's own
    wording is about: a typo'd `--core` "writes a launch.json pointed at the
    wrong ELF and reports success". Pre-build there is no ELF yet, so what
    lands on disk is `"device": "<resolved-device>"`. Either way the file
    must not be created."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    write_sdk_fixture(tmp_path)

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                           "--core", "no_such_core", "--sdk-root", "./sdk", "--format", "json"))

    assert env["exitCode"] == 2, env
    assert not launch_json(tmp_path).exists(), "wrote a launch.json it had just refused"


def test_only_an_sdk_this_project_is_entitled_to_may_refuse_a_core():
    """tan-cli#477 major 2, REVIEW round: WHICH checkout gets to refuse.

    The refusal above is decided by an alp-sdk checkout, and with no
    `--sdk-root` and no project pin that can be the machine-global default --
    which `tan bootstrap` may have last pointed at an unrelated project.
    Measured on a project directory naming no SDK at all:

        resolve_project_context('.', None, None).sdk
        -> SdkInfo(root='.../sdk-triage', source_tier='globalDefault',
                   foreign_global_default_for='.../t477/p')

    and the verdict flips purely on who answers -- measured, one project, one
    `--core m55_hp`, two checkouts differing only in `e8.json`'s `cores[]`:
    `--sdk-root A` exit 0, `--sdk-root B` exit 2 `debug-config.core-unknown`.
    `debug-config` publishes no `sdk` envelope block and does not emit the
    `sdk.global-default-foreign-project` warning `size`/`image` emit, so that
    verdict would arrive unattributable.

    A checkout another project pinned cannot PROVE anything about this
    project's SoM, so it declines and the run stays on the "cannot be asked"
    floor. Every other tier still refuses."""
    assert _sdk_core_refusal_authority("/sdk", "sdkRootFlag", None) == "/sdk"
    assert _sdk_core_refusal_authority("/sdk", "projectPin", None) == "/sdk"
    # This project's OWN bootstrap set the global default -- still entitled.
    assert _sdk_core_refusal_authority("/sdk", "globalDefault", None) == "/sdk"
    # Another project's bootstrap set it. Declines.
    assert _sdk_core_refusal_authority("/sdk", "globalDefault", "/elsewhere") is None
    # Nothing resolved at all.
    assert _sdk_core_refusal_authority(None, "none", None) is None


def test_the_sdk_core_refusal_names_the_checkout_that_decided_it(tmp_path):
    """tan-cli#477 major 2, REVIEW round. The with-manifest arm needs no such
    line -- the manifest is inside the project the user pointed at -- but this
    arm's authority may be a checkout the argv never mentions, and the envelope
    carries no `sdk` block to look it up in. So the message names the path and
    the tier that chose it, which is what makes `--sdk-root <the right one>`
    the obvious next move rather than a support ticket."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    write_sdk_fixture(tmp_path)

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                           "--core", "no_such_core", "--sdk-root", "./sdk",
                           "--preview", "--format", "json"))

    assert env["exitCode"] == 2, env
    message = next(i["message"] for i in env["issues"]
                   if i["code"] == "debug-config.core-unknown")
    assert "sdk" in message, message
    assert "--sdk-root" in message, message
    assert "sdkRootFlag" in message, message


def test_a_core_the_sdk_cannot_be_asked_about_is_still_not_refused(tmp_path):
    """The floor under the refusal above: it fires only where tan can PROVE
    the core is unknown. With no resolvable SDK checkout there is no
    published core list and no build manifest either, so nothing can tell a
    typo from a real core id -- staying silent there is the same standard the
    with-manifest guard already holds (`if all_slices and not any(...)`), and
    a refusal on an unprovable case would be a false negative on a legitimate
    `--core`.

    `--sdk-root` names a directory that is not an SDK checkout (no
    `scripts/alp_project.py`), which is how this test guarantees no ambient
    checkout on the developer's box resolves instead."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    Path(tmp_path, "not-an-sdk").mkdir()

    env = envelope(run_cli(tmp_path, "--target-kind", ZEPHYR_MCU, "--server", JLINK,
                           "--core", "no_such_core", "--sdk-root", "./not-an-sdk",
                           "--preview", "--format", "json"))

    assert env["exitCode"] == 0, env
    assert env["data"]["configuration"]["device"] == "<resolved-device>", env["data"]


def test_a_malformed_existing_launch_json_stays_an_internal_failure(tmp_path):
    """The other side of tan-cli#477's line, pinned so the reclassification
    cannot creep. Exit 5 is still correct for state no flag value can produce
    -- `_build_manifest_missing_failure`'s docstring reserves it for exactly
    the `except Exception` backstop and an unreadable/malformed EXISTING
    launch.json. This test covers only the LATTER (a malformed *existing*
    launch.json); the `except Exception` backstop itself has no dedicated
    case here."""
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    (vscode / "launch.json").write_text("{ this is not json", encoding="utf-8")

    env = envelope(run_cli(tmp_path, "--target-kind", "native-host", "--format", "json"))

    assert env["exitCode"] == 5, env
    assert "debug-config.internal-failure" in [i["code"] for i in env["issues"]]


def test_an_omitted_target_kind_with_no_project_signal_refuses_rather_than_guessing(tmp_path):
    """tan-cli#476 half (b): INVERTS the assertion this test used to carry.

    It used to read "the historical native-host default must survive
    untouched" and pin `exitCode == 0` / `targetKind == native-host` on an
    empty scratch directory. That default IS the defect #476 half (b)
    reports: a directory carrying no evidence at all -- no
    `build/system-manifest.yaml`, no `board.yaml` `som.sku` -- got a
    `native_sim` launch configuration written into it and a clean envelope
    saying so. Half (a) (a `--project` that does not exist) was fixed by
    tan-cli#508; #508's own body records half (b) as "deliberately left".

    `native-host` is still perfectly reachable -- it just has to be ASKED
    for now, which is exactly what the issue requests ("instead of refusing
    or requiring an explicit `--target-kind`")."""
    env = envelope(run_cli(tmp_path, "--preview", "--format", "json"))

    assert env["exitCode"] == 2, env
    assert "debug-config.target-kind-unresolved" in [i["code"] for i in env["issues"]], env

    # …and an explicit --target-kind still produces the native-host draft.
    ok = envelope(
        run_cli(tmp_path, "--target-kind", NATIVE_HOST, "--preview", "--format", "json")
    )
    assert ok["exitCode"] == 0, ok
    assert ok["data"]["targetKind"] == NATIVE_HOST
    assert ok["data"]["server"] == "none"


def test_a_no_signal_project_is_refused_before_a_launch_json_is_written(tmp_path):
    """tan-cli#476 half (b), the WRITE half -- the part a customer actually
    trips over. The refusal above is worth nothing if the file is created
    anyway: `.vscode/launch.json` must not exist afterwards, because the
    whole complaint is a stray `native_sim` launch configuration
    materialising in a directory that is not a project.

    Distinct from `test_a_project_that_does_not_exist_is_refused_not_created`
    (half (a), tan-cli#508): this directory DOES exist -- it is the "ran it
    from the wrong cwd" case the issue names in its own words."""
    env = envelope(run_cli(tmp_path, "--format", "json"))

    assert env["exitCode"] == 2, env
    assert "debug-config.target-kind-unresolved" in [i["code"] for i in env["issues"]], env
    assert not launch_json(tmp_path).exists(), "wrote a launch.json it had just refused"


def test_an_omitted_target_kind_refuses_rather_than_guess_pre_build(tmp_path):
    """A real hardware project (`som.sku` set) with no build yet cannot say
    which of zephyr/baremetal/yocto its core defaults to without shelling the
    SDK -- tan-cli#456's own floor: refuse with a coded issue rather than
    write a launch.json that cannot work.

    tan-cli#462: this is the CALLER's own precondition (run `tan build`
    first, or pass `--target-kind` explicitly), not a tan-side crash --
    `VALIDATION_FAILURE` (2), not `INTERNAL_FAILURE` (5), matching the
    distinction tan-cli#262 settled for `tan validate`. FAILS against the
    pre-#462 code, which reported this exact refusal at exit 5 with issue
    code `debug-config.internal-failure`."""
    Path(tmp_path, "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n",
        encoding="utf-8",
    )

    env = envelope(run_cli(tmp_path, "--format", "json"))

    assert env["exitCode"] == 2, env
    assert env["issues"][0]["code"] == "debug-config.build-manifest-missing"
    assert "som.sku: E1M-AEN801" in env["issues"][0]["message"]
    assert not launch_json(tmp_path).exists()


def test_an_omitted_target_kind_refuses_on_a_mixed_manifest(tmp_path):
    """A board with slices in more than one target class (Cortex-A yocto +
    Cortex-M zephyr) is genuinely ambiguous with no `--core` to pick one --
    refuse rather than silently pick a winner.

    Review round on tan-cli#456: asserts the mapped `--target-kind` SPELLINGS
    (`yocto-userspace`/`zephyr-mcu`), not just the substrings `"yocto"`/
    `"zephyr"` -- those substrings also match the OLD, wrong message (which
    printed the raw manifest `os` values `yocto`/`zephyr`, not a value
    `--target-kind` actually accepts), so the old assertion never would have
    caught that bug.

    tan-cli#462 review round: this hits a fully built, CORRECT project (a
    mixed-core board with no `--core` to narrow it) and never stops -- worse
    than the pre-build refusal above, since it fires on every run, not just
    one command early. `VALIDATION_FAILURE` (2), not `INTERNAL_FAILURE` (5),
    same reasoning. FAILS against the pre-fix code, which reported this exact
    refusal at exit 5 with issue code `debug-config.internal-failure`."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-V2N101\n", encoding="utf-8")
    root = str(tmp_path).replace("\\", "/")
    write_manifest(tmp_path, MANIFEST_MIXED_ZEPHYR_AND_YOCTO.format(root=root))

    env = envelope(run_cli(tmp_path, "--format", "json"))

    assert env["exitCode"] == 2, env
    assert env["issues"][0]["code"] == "debug-config.target-kind-ambiguous"
    message = env["issues"][0]["message"]
    assert "yocto-userspace" in message and "zephyr-mcu" in message, message
    assert not launch_json(tmp_path).exists()


def test_an_omitted_target_kind_refuses_with_its_own_message_for_a_single_unmapped_os(tmp_path):
    """Review round on tan-cli#456: a lone slice whose `os` maps to no
    `--target-kind` at all (e.g. `linux`) is NOT "more than one target
    class" -- the shared ambiguous-manifest message said so anyway, which is
    false with only one slice in play. This gets its own, honest message.

    tan-cli#462 review round: this is a knowledge/version skew between tan
    and the SDK, not a crash -- no invariant was violated and the command
    still produces a coherent verdict with a working remedy (`--target-kind`
    explicit). `VALIDATION_FAILURE` (2), not `INTERNAL_FAILURE` (5). FAILS
    against the pre-fix code, which reported this exact refusal at exit 5
    with issue code `debug-config.internal-failure`."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-UNKNOWN\n", encoding="utf-8")
    write_manifest(
        tmp_path,
        "schema_version: 1\nslices:\n- core_id: a55_cluster\n  os: linux\n"
        "ipc: []\nhelper_mcus: []\nboot_order: []\n",
    )

    env = envelope(run_cli(tmp_path, "--format", "json"))

    assert env["exitCode"] == 2, env
    assert env["issues"][0]["code"] == "debug-config.no-debuggable-target-class"
    message = env["issues"][0]["message"]
    assert "no debuggable target class for os: linux" in message, message
    assert "more than one" not in message, message


def test_an_omitted_target_kind_with_core_disambiguates_a_mixed_manifest(tmp_path):
    """`--core` alone, with no `--target-kind`, is enough to resolve the same
    mixed manifest the previous test refuses on."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text("som:\n  sku: E1M-V2N101\n", encoding="utf-8")
    root = str(tmp_path).replace("\\", "/")
    write_manifest(tmp_path, MANIFEST_MIXED_ZEPHYR_AND_YOCTO.format(root=root))

    env = envelope(run_cli(tmp_path, "--core", "m33_sm", "--preview", "--format", "json"))

    assert env["exitCode"] == 0
    assert env["data"]["targetKind"] == ZEPHYR_MCU
    assert env["data"]["server"] == JLINK


def test_an_omitted_target_kind_with_a_core_matching_nothing_refuses(tmp_path):
    """Review round on tan-cli#456 blocker: `--core` naming NOTHING in the
    manifest must refuse outright, never silently fall back to voting across
    every OTHER core's class (the exact bug the next test pins the fix for).
    Also pins tan-cli#456 review finding on `launchJsonPath`: this refusal
    fires AFTER `launch_json_path` is resolved (unlike the parse_target_kind
    catch-all below it), so it must report the PROJECT's own path, not one
    built from wherever the shell happened to be.

    tan-cli#462: a bad `--core` value is the caller's own typo, not a tan-side
    crash -- `VALIDATION_FAILURE` (2), not `INTERNAL_FAILURE` (5), and the
    message now NAMES the cores this build actually produced (`m55_hp`,
    `m55_he`, from `MANIFEST_HARDWARE_ONLY_NO_NATIVE_SIM`) rather than only
    saying `bogus-core` does not match. FAILS against the pre-#462 code, which
    reported this refusal at exit 5 with issue code
    `debug-config.internal-failure` and no core listing in the message."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n",
        encoding="utf-8",
    )
    root = str(tmp_path).replace("\\", "/")
    write_manifest(tmp_path, MANIFEST_HARDWARE_ONLY_NO_NATIVE_SIM.format(root=root))

    # `cwd` is the SESSION temp root, deliberately NOT `tmp_path` (the
    # project) -- a cwd-based `launchJsonPath` and the project's own would
    # otherwise be indistinguishable strings.
    env = envelope(run_cli(tmp_path.parent, "--core", "bogus-core", "--project", str(tmp_path), "--format", "json"))

    assert env["exitCode"] == 2, env
    assert env["issues"][0]["code"] == "debug-config.core-unknown"
    message = env["issues"][0]["message"]
    assert "does not match any slice" in message, message
    assert "its cores: m55_hp, m55_he" in message, message
    assert env["data"]["launchJsonPath"] == str(Path(tmp_path, ".vscode", "launch.json"))
    assert not launch_json(tmp_path).exists()


def test_an_explicit_target_kind_with_a_core_matching_nothing_also_refuses(tmp_path):
    """tan-cli#489 (5): the `--target-kind`-EXPLICIT counterpart of the test
    above. `infer_target_kind` (and ITS OWN `--core`-vs-manifest guard) only
    runs when `--target-kind` is OMITTED -- passing it explicitly used to
    bypass that guard entirely, so a mistyped `--core` on a real, BUILT
    two-core project sailed through in silence: exit 0, `device` still the
    placeholder, and `executable` pointing at
    `${workspaceFolder}/build/app/zephyr/zephyr.elf`, a path this project's
    build never produced (measured in the issue). FAILS against the pre-fix
    code, which reported `exitCode: 0`, `ok: true`, `issues: []` here."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n",
        encoding="utf-8",
    )
    root = str(tmp_path).replace("\\", "/")
    write_manifest(tmp_path, MANIFEST_HARDWARE_ONLY_NO_NATIVE_SIM.format(root=root))

    env = envelope(
        run_cli(
            tmp_path,
            "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--core", "m55_typo",
            "--format", "json",
        )
    )

    assert env["exitCode"] == 2, env
    assert env["issues"][0]["code"] == "debug-config.core-unknown"
    message = env["issues"][0]["message"]
    assert "does not match any slice" in message, message
    assert "its cores: m55_hp, m55_he" in message, message
    # Unlike the omitted-`--target-kind` refusal above (which fires before
    # target/server are known and so reports the `zephyr-mcu`/`none`
    # placeholder pair), this one already HAS a real target/server resolved
    # -- report them, not the placeholder.
    assert env["data"]["targetKind"] == ZEPHYR_MCU and env["data"]["server"] == JLINK
    assert not launch_json(tmp_path).exists()


def test_an_explicit_core_matching_a_real_slice_still_resolves(tmp_path):
    """The regression-safety pairing for the fix above: a `--core` that DOES
    name a real slice must keep working exactly as before -- this is a
    validation floor, not a new requirement to name every slice explicitly."""
    pytest.importorskip("yaml")
    root = str(tmp_path).replace("\\", "/")
    write_manifest(tmp_path, MANIFEST_HARDWARE_ONLY_NO_NATIVE_SIM.format(root=root))

    env = envelope(
        run_cli(
            tmp_path,
            "--target-kind", ZEPHYR_MCU, "--server", JLINK, "--core", "m55_he",
            "--preview", "--format", "json",
        )
    )

    assert env["exitCode"] == 0, env
    assert env["data"]["configuration"]["executable"] == (
        "${workspaceFolder}/build/m55_he-zephyr/build/zephyr/zephyr.elf"
    )


def test_an_omitted_target_kind_with_core_naming_the_native_sim_slice_infers_native_host(tmp_path):
    """The exact wrong-config bug this review round caught: the original
    tan-cli#456 fix filtered only the HARDWARE slices by `--core`, and fell
    back to the UNFILTERED hardware list when `--core` matched none of THEM
    -- so on a mixed native_sim+hardware manifest, `--core <the native_sim
    slice's own core id>` silently voted on the co-built hardware slice
    instead: inferred `zephyr-mcu`/`jlink` and wrote a J-Link session whose
    `executable` pointed at the native_sim binary a J-Link probe can never
    attach to. `--core` naming the native_sim slice must infer `native-host`,
    the same target `_resolve_from_build`'s own NATIVE_HOST arm resolves it
    against (`test_native_host_resolves_the_native_sim_slice_not_the_first_zephyr_one`,
    above)."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text(
        "som:\n  sku: E1M-AEN701\ncores:\n  m55_hp:\n    app: ./src\n",
        encoding="utf-8",
    )
    root = str(tmp_path).replace("\\", "/")
    write_manifest(tmp_path, MANIFEST_MCU_THEN_NATIVE_SIM.format(root=root))

    env = envelope(run_cli(tmp_path, "--core", "native_sim", "--preview", "--format", "json"))

    assert env["exitCode"] == 0, env
    assert env["data"]["targetKind"] == NATIVE_HOST
    assert env["data"]["server"] == "none"
    assert env["data"]["configuration"]["type"] == "lldb"
    program = env["data"]["configuration"]["program"]
    assert program == "${workspaceFolder}/build/native_sim-zephyr/build/zephyr/zephyr.exe", program


def test_an_omitted_target_kind_and_core_infers_hardware_over_a_co_built_native_sim_slice(tmp_path):
    """tan-cli#456 review finding (minor): hardware outvotes a co-built
    native_sim slice when `--core` is NOT given either -- the #83
    `_select_slice` NATIVE_HOST arm is reachable only via an explicit
    `--target-kind native-host` (or `--core` naming the native_sim slice, the
    previous test) on a board that also built a real MCU image, never by the
    plain no-flag default. Pinned so it cannot silently flip."""
    pytest.importorskip("yaml")
    Path(tmp_path, "board.yaml").write_text(
        "som:\n  sku: E1M-AEN701\ncores:\n  m55_hp:\n    app: ./src\n",
        encoding="utf-8",
    )
    root = str(tmp_path).replace("\\", "/")
    write_manifest(tmp_path, MANIFEST_MCU_THEN_NATIVE_SIM.format(root=root))

    env = envelope(run_cli(tmp_path, "--preview", "--format", "json"))

    assert env["exitCode"] == 0, env
    assert env["data"]["targetKind"] == ZEPHYR_MCU
    assert env["data"]["server"] == JLINK
    executable = env["data"]["configuration"]["executable"]
    assert executable == "${workspaceFolder}/build/m55_hp-zephyr/build/zephyr/zephyr.elf", executable


def test_inferred_target_kind_stays_native_host_for_a_pure_native_sim_manifest():
    """A project whose build produced ONLY a `native_sim` slice really is a
    host-simulation project -- the one case where `native-host` remains the
    right default, not a guess. Pure -- `infer_target_kind` takes the
    already-parsed manifest, no file IO."""
    manifest = {
        "schema_version": 1,
        "slices": [{"core_id": "native_sim", "os": "zephyr", "board": "native_sim/native/64"}],
    }

    target, code, ambiguous = infer_target_kind(manifest, None, None)

    assert target == NATIVE_HOST and code is None and ambiguous is None
