# SPDX-License-Identifier: Apache-2.0
"""`tan.core.bootstrap`: prerequisite-refusal wording, the manual-install
hint blocks, and `--workspace` path validation."""
import json
import os

import pytest




def test_the_oracle_first_line_stays_byte_identical_and_the_remedy_is_a_second_line():
    """tan-cli#355 is a DELIBERATE divergence, and this pins its exact shape so
    it cannot drift into an accidental one.

    `bootstrap.sh` prints one line and nothing else -- note the TWO spaces
    before "Install", which a reflow would silently eat. tan keeps that line
    byte for byte and adds a SECOND naming `tan doctor --build --fix`, which is
    the installer tan-cli#91 gave tan and which the original wording predates.

    Fails if someone restores the oracle's silence (the remedy line vanishes),
    and equally if someone "tidies" the first line and breaks the parity it is
    the whole point of preserving."""
    from tan.core.bootstrap import posix_refusal

    failure = posix_refusal(["cmake", "ninja", "xz", "wget"], {})
    lines = failure.lines if hasattr(failure, "lines") else failure[1]

    assert len(lines) == 2, lines
    # Byte-identical to the oracle, TWO spaces included.
    assert lines[0] == "Missing required tools: cmake ninja xz wget.  Install them and re-run."
    assert "  Install them" in lines[0], "the oracle's double space was reflowed away"
    # The remedy tan actually ships.
    assert "tan doctor --build --fix" in lines[1]


#: alp-sdk `dev`'s real `prerequisites.install.linux`, transcribed. Every entry
#: needs elevation, which is the whole point of tan-cli#370.
_LINUX_INSTALL = {
    "git": "sudo apt-get install -y git",
    "cmake": "sudo apt-get install -y cmake",
    "python3": "sudo apt-get install -y python3",
    "ninja": "sudo apt-get install -y ninja-build",
    "xz": "sudo apt-get install -y xz-utils",
    "wget": "sudo apt-get install -y wget",
}

#: alp-sdk `dev`'s real `prerequisites.install.macos`. No elevation anywhere.
_MACOS_INSTALL = {
    "cmake": "brew install cmake",
    "ninja": "brew install ninja",
}


def test_the_remedy_does_not_promise_an_install_it_cannot_perform():
    """tan-cli#370. `doctor --build --fix` never spawns the `sudo` PROGRAM
    (`doctor_cmd.fix_needs_sudo_check`) -- under `--format json` this
    process's stdio is captured end to end, so a password prompt would hang
    forever rather than fail loudly. Every one of alp-sdk's
    `prerequisites.install.linux` commands starts with `sudo`, so on Linux
    `--fix` installs nothing for a NON-root caller and prints instead
    (tan-cli#650 made the root case install directly -- see
    `test_run_fix_runs_a_sudo_command_directly_when_already_root` in
    `tests/commands/test_doctor_command.py` -- but this wording is not
    conditioned on the caller's UID, only on the commands, so it stays true
    either way).

    tan-cli#355's original second line said "to install them from the SDK's
    manifest", which is therefore false on the host most customers are on --
    replacing one wrong expectation with another, which is exactly what #355
    set out to stop.

    Fails against the pre-#370 wording, which was a single constant."""
    from tan.core.bootstrap import posix_refusal

    lines = posix_refusal(["cmake", "ninja", "xz", "wget"], _LINUX_INSTALL).lines

    # The oracle's line is untouched by any of this.
    assert lines[0] == "Missing required tools: cmake ninja xz wget.  Install them and re-run."
    assert "tan doctor --build --fix" in lines[1]
    assert "prints the exact command" in lines[1]
    assert "sudo" in lines[1], "the reason it cannot install has to be the reason given"
    assert "to install them from the SDK's manifest" not in lines[1]
    # tan-cli#650: the hint that recommends `--fix` also discloses, in the
    # same breath, that it needs a real interactive terminal -- a Dockerfile
    # `RUN` or a CI step reading this exact line would otherwise have no way
    # to know the remedy it was just pointed at is inert for them.
    assert "interactive terminal" in lines[1]


def test_the_remedy_still_promises_an_install_where_none_needs_elevation():
    """The other half of tan-cli#370: macOS is POSIX, and `brew install ...`
    needs no elevation, so `--fix` really does install there. Keying the wording
    on the PLATFORM rather than on the commands would have made this case wrong
    in the opposite direction."""
    from tan.core.bootstrap import posix_refusal

    lines = posix_refusal(["cmake", "ninja"], _MACOS_INSTALL).lines

    assert lines[1] == (
        "Or run `tan doctor --build --fix` (needs a real, interactive "
        "terminal) to install them from the SDK's manifest."
    )


def test_a_tool_the_manifest_has_no_command_for_does_not_imply_elevation():
    """A missing tool the manifest cannot install contributes generic advice,
    never a spawn -- so it must not tip the wording toward the elevation
    variant. Guards the `install.get(tool, "")` default."""
    from tan.core.bootstrap import posix_refusal

    lines = posix_refusal(["cmake", "dtc"], {"cmake": "brew install cmake"}).lines

    assert "to install them from the SDK's manifest" in lines[1]


# ---------------------------------------------------------------------------
# tan-cli#760: `data.missingPrerequisites[].command` must never hand back a
# line that cannot run. Measured on fedora:42/archlinux:latest/rockylinux:9:
# alp-sdk's `prerequisites.install.linux` is six `sudo apt-get install -y
# ...` lines and none of those hosts has `apt-get` on PATH -- byte-identical
# to the `debian:12` output before this guard existed.
# ---------------------------------------------------------------------------


def test_leading_binary_strips_a_literal_sudo_prefix():
    """The parsing half of the guard, isolated: it has to agree with
    `doctor_cmd.run_fix`'s own `sudo ` strip (tan-cli#650) about which binary
    a command actually spawns, or the guard and the spawn could disagree
    about what "confirmed" means."""
    from tan.core.bootstrap import leading_binary

    assert leading_binary("sudo apt-get install -y cmake") == "apt-get"
    assert leading_binary("brew install cmake") == "brew"
    assert leading_binary("winget install -e --id Kitware.CMake") == "winget"
    assert leading_binary("   ") == ""


def test_confirmed_install_commands_drops_a_command_whose_binary_is_absent():
    """The core of the guard: on a host with no `apt-get` (Fedora/Arch/Rocky),
    every `sudo apt-get ...` entry the manifest carries must be dropped from
    the dict -- which downstream readers (`install.get(tool)`, in
    `_structured_missing`/`hint_line`/`doctor_cmd.prerequisites_check`) then
    see as `None`, never a string. A test that never saw `command: null`
    escape this function would not have caught tan-cli#760."""
    from tan.core.bootstrap import confirmed_install_commands

    install = {
        "cmake": "sudo apt-get install -y cmake",
        "ninja": "sudo apt-get install -y ninja-build",
    }
    confirmed = confirmed_install_commands(install, lambda binary: binary == "dnf")
    assert confirmed == {}


def test_confirmed_install_commands_keeps_a_command_whose_binary_is_present():
    """The other half: a real Debian/Ubuntu host DOES have `apt-get`, so the
    guard must not drop a command just because it CAN be wrong on some other
    host -- only when it actually is, on THIS one."""
    from tan.core.bootstrap import confirmed_install_commands

    install = {
        "cmake": "sudo apt-get install -y cmake",
        "ninja": "sudo apt-get install -y ninja-build",
    }
    confirmed = confirmed_install_commands(install, lambda binary: binary == "apt-get")
    assert confirmed == install


def test_posix_refusal_gives_a_host_neutral_hint_when_no_command_is_confirmed():
    """Item 2 of tan-cli#760. `install` here is already in the POST-guard
    shape (every entry dropped, exactly what `confirmed_install_commands`
    leaves on a host it could not confirm `apt-get` on) -- so the `--fix`
    hint must stop promising an install it cannot perform, and must name the
    MISSING TOOLS rather than guess a package name (a guessed name here would
    be the identical defect this issue fixes, just moved to a different OS).
    """
    from tan.core.bootstrap import posix_refusal

    lines = posix_refusal(["cmake", "ninja"], {}).lines
    assert "cmake" in lines[1] and "ninja" in lines[1]
    assert "package manager" in lines[1]
    assert "to install them from the SDK's manifest" not in lines[1]
    assert "prints the exact command" not in lines[1]


def test_posix_refusal_keeps_the_confirmed_wording_when_a_command_survives():
    """No regression on the ordinary, confirmed-host case: at least one
    missing tool with a real command still gets the existing (non-host-
    neutral) hints, unchanged."""
    from tan.core.bootstrap import posix_refusal

    lines = posix_refusal(["cmake", "ninja"], {"cmake": "sudo apt-get install -y cmake"}).lines
    assert "prints the exact command" in lines[1]
    assert "has no confirmed install command" not in lines[1]


# ---------------------------------------------------------------------------
# tan-cli#495 defect 6: `manualInstallHints.posix.note` was dropped at parse,
# at render, AND in the fallback -- three places, so no single one of them
# looked like a gap.
# ---------------------------------------------------------------------------


#: A minimal manifest that PARSES -- every required key, nothing more, so the
#: only thing these tests vary is `manualInstallHints`. Deliberately synthetic
#: rather than read from a live alp-sdk checkout: this suite must run in CI,
#: where no SDK is bound.
_MANIFEST_WITH_POSIX_NOTE = json.dumps(
    {
        "schemaVersion": 1,
        "west": {
            "pipSpec": "west>=0.14.0",
            "initArgs": ["init", "-l"],
            "updateArgs": ["update"],
            "exportArgs": ["zephyr-export"],
            "extensionGuardCommand": "alp-migrate",
        },
        "pip": {
            "bootstrapUpgrade": ["pip", "wheel"],
            "sdkExtras": ["jsonschema", "imgtool"],
            "editableInstall": "${SDK_ROOT}",
        },
        "verdict": {
            # Templates are placeholders: nothing in this suite renders a
            # verdict, they only have to be present and well-typed to parse.
            "blockingPhases": ["zephyr-requirements"],
            "partialNoteTemplate": "({{PHASES}} did not install.)",
            "incompleteMessageTemplate": "INCOMPLETE -- {{PHASES}}.",
            "incompleteRemedy": "Fix them and re-run.",
        },
        "zephyr": {
            "version": "v4.4.1",
            "requirementsPath": "zephyr/scripts/requirements.txt",
            "pythonMinVersion": "3.12",
        },
        "prerequisites": {
            "pythonMinVersion": "3.10",
            "posix": ["git", "cmake"],
            "windows": ["git", "cmake"],
        },
        "venv": {"dirName": ".venv", "posixBinDir": "bin", "windowsBinDir": "Scripts"},
        "env": {},
        "nativeLibHints": {
            "linux": {"note": ["linux lib note"], "command": "apt-get install -y libfoo"},
            "macos": {"note": ["macos lib note"], "command": "brew install foo"},
            "windows": {"note": ["windows lib note"]},
        },
        "manualInstallHints": {
            "windows": {"note": ["windows note"]},
            "posix": {"note": ["run `west sdk install`", "install arm-none-eabi-gcc"]},
        },
    }
)


def test_a_posix_manual_install_note_is_parsed_and_rendered_on_linux_and_macos():
    """**tan-cli#495 defect 6.** alp-sdk v0.14.0 added the POSIX twin of the
    Windows manual-install key. The port never read it, never stored it and
    never rendered it, so no Linux or macOS customer was told that the Zephyr
    SDK is a separate `west sdk install` -- the one manual step every
    Zephyr-on-M customer needs. The data was in the manifest and inert.

    Heading, indent and position are the oracle's (`blocks.rs:229-245`, itself
    transcribing `bootstrap.sh`'s `case linux|macos)` arm): a blank line, the
    same heading the Windows arm uses, then one two-space-indented line per
    element, AFTER the optional-native-libs section.
    """
    from tan.core.bootstrap import LINUX, MACOS, optional_libs_block, parse_bootstrap_manifest

    facts = parse_bootstrap_manifest(_MANIFEST_WITH_POSIX_NOTE)
    assert facts.manual_install_posix == (
        "run `west sdk install`",
        "install arm-none-eabi-gcc",
    )

    for host in (LINUX, MACOS):
        lines = optional_libs_block(facts, host)
        heading = lines.index("bootstrap: NOT auto-installed (manual, one-time):")
        assert lines[heading - 1] == ""
        assert lines[heading + 1 :] == [
            "  run `west sdk install`",
            "  install arm-none-eabi-gcc",
        ]
        # AFTER the optional-native-libs section, never before it.
        assert heading > lines.index(
            "bootstrap: Optional native libraries unlock the Yocto-side backends:"
        )


def test_a_posix_manual_install_note_is_not_rendered_on_an_unrecognised_host():
    """`LINUX`/`MACOS` only, matching the oracle's `matches!(host, Linux |
    MacOs)` exactly. `OTHER` is the host tan could not identify -- rendering a
    POSIX-specific `apt-get`/`brew` instruction there is a guess, and the
    oracle's own comment says the arm is written out rather than assumed so
    this cannot happen by accident."""
    from tan.core.bootstrap import OTHER, optional_libs_block, parse_bootstrap_manifest

    lines = optional_libs_block(parse_bootstrap_manifest(_MANIFEST_WITH_POSIX_NOTE), OTHER)

    assert "bootstrap: NOT auto-installed (manual, one-time):" not in lines
    assert "  run `west sdk install`" not in lines


def test_a_manifest_with_no_posix_key_still_parses_and_renders_nothing():
    """The compatibility half, and the reason the field is optional where
    `windows` is required: every SDK before alp-sdk v0.14.0 declares `windows`
    alone. Requiring `posix` would turn each of those into a hard
    `BootstrapManifestError` that `tan build` inherits through auto-bootstrap
    -- trading a missing hint for a dead command."""
    from tan.core.bootstrap import LINUX, optional_libs_block, parse_bootstrap_manifest

    doc = json.loads(_MANIFEST_WITH_POSIX_NOTE)
    del doc["manualInstallHints"]["posix"]

    facts = parse_bootstrap_manifest(json.dumps(doc))

    assert facts.manual_install_posix == ()
    assert "bootstrap: NOT auto-installed (manual, one-time):" not in optional_libs_block(
        facts, LINUX
    )


def test_the_no_manifest_fallback_carries_the_posix_note_too():
    """The third place defect 6 dropped it. `fallback_facts` is what tan
    believes when it cannot read a manifest at all, so a fix at parse+render
    alone still left every no-manifest run silent."""
    from tan.core.bootstrap import LINUX, fallback_facts, optional_libs_block

    facts = fallback_facts((3, 12))

    assert len(facts.manual_install_posix) == 3
    assert any("west sdk install" in note for note in facts.manual_install_posix)
    assert "bootstrap: NOT auto-installed (manual, one-time):" in optional_libs_block(
        facts, LINUX
    )


# ---------------------------------------------------------------------------
# tan-cli#495 defect 8: a drive-relative `--workspace C:ws`
# ---------------------------------------------------------------------------


def test_a_drive_relative_workspace_is_refused_rather_than_returned_unjoined():
    """**tan-cli#495 defect 8.** `ntpath.isabs("C:ws")` is False, but
    `ntpath_isabs`'s own `^[A-Za-z]:` regex says True, so the "already
    absolute" branch returned `C:ws` UNJOINED -- a relative path out of a
    function whose entire contract is to absolutise one, with `cwd` ignored.

    `C:ws` is drive-relative: Windows keeps a separate current directory per
    drive, so it means "`ws` under whatever the process's cwd on C: happens to
    be". That is the same ambiguity `--workspace /e/foo` is already refused
    for, and this call is about to RELOCATE a customer's checkout into it.
    """
    from tan.core.bootstrap import resolve_workspace_target

    with pytest.raises(ValueError) as excinfo:
        resolve_workspace_target("C:ws", os.path.join(os.sep, "home", "u"))

    message = str(excinfo.value)
    assert "drive-relative" in message
    # Names the unambiguous form the customer should have passed.
    assert "C:\\ws" in message


@pytest.mark.parametrize("raw", ["C:\\ws", "C:/ws"])
def test_a_genuinely_absolute_windows_workspace_still_resolves(raw):
    """The narrowing must not catch a real drive-absolute path: only a drive
    letter with NO separator after it is ambiguous."""
    from tan.core.bootstrap import resolve_workspace_target

    assert resolve_workspace_target(raw, os.path.join(os.sep, "home", "u"))


def test_the_drive_relative_guard_did_not_change_is_plain_relative():
    """The fix is at the call site because `ntpath_isabs`'s two callers want
    OPPOSITE answers for `C:ws`. `is_plain_relative` must keep rejecting it --
    that value is a manifest-supplied directory name about to be joined onto
    the workspace, and the `^[A-Za-z]:` regex is exactly what stops it.
    Narrowing the predicate instead would have let `C:ws` through here."""
    from tan.core.bootstrap import is_plain_relative, ntpath_isabs

    assert ntpath_isabs("C:ws") is True
    assert is_plain_relative("C:ws") is False
    assert is_plain_relative("ws") is True
