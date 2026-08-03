

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
