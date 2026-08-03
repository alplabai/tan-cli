

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
    """tan-cli#370. `doctor --build --fix` REFUSES to spawn any command whose
    first word is `sudo` (`doctor_cmd.fix_needs_sudo_check`) -- under
    `--format json` this process's stdio is captured end to end, so a password
    prompt would hang forever rather than fail loudly. Every one of alp-sdk's
    `prerequisites.install.linux` commands starts with `sudo`, so on Linux
    `--fix` installs nothing and prints instead.

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


def test_the_remedy_still_promises_an_install_where_none_needs_elevation():
    """The other half of tan-cli#370: macOS is POSIX, and `brew install ...`
    needs no elevation, so `--fix` really does install there. Keying the wording
    on the PLATFORM rather than on the commands would have made this case wrong
    in the opposite direction."""
    from tan.core.bootstrap import posix_refusal

    lines = posix_refusal(["cmake", "ninja"], _MACOS_INSTALL).lines

    assert lines[1] == (
        "Or run `tan doctor --build --fix` to install them from the SDK's manifest."
    )


def test_a_tool_the_manifest_has_no_command_for_does_not_imply_elevation():
    """A missing tool the manifest cannot install contributes generic advice,
    never a spawn -- so it must not tip the wording toward the elevation
    variant. Guards the `install.get(tool, "")` default."""
    from tan.core.bootstrap import posix_refusal

    lines = posix_refusal(["cmake", "dtc"], {"cmake": "brew install cmake"}).lines

    assert "to install them from the SDK's manifest" in lines[1]
