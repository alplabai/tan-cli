# SPDX-License-Identifier: Apache-2.0
"""The outage probe must claim an outage ONLY on the signature it observed.

tan-cli#840. `west sdk install` resolves `--version` against
`GET /repos/zephyrproject-rtos/sdk-ng/releases`, and it reports an EMPTY list
with the same words it uses for a genuinely absent version:

    FATAL ERROR: Unavailable SDK version: 1.0.1.Please select from the list below:

The blank line after that colon is the entire diagnosis, and it is the only
tell. The probe exists to make that tell machine-readable so the step can say
"upstream's list came back empty" instead of letting west accuse a pin that
`scripts/check_toolchain_lock.py` re-derives upstream.

WHY THE DANGEROUS DIRECTION IS THE ONE UNDER TEST
--------------------------------------------------

A false "outage" is far worse than a missed one. On outage the step skips the
install AND `getting-started.yml` skips its two SDK-backed build steps -- the
real ARM build is the reason the job exists. A probe that over-claims turns
that build gate off silently, on every PR, and the job still reports green.

So three of the four rows below are PROCEED, and they are the load-bearing
ones: an unmeasurable list proceeds, an empty list beside an unreachable
`latest` proceeds, a populated list proceeds. Only the exact conjunction the
issue measured -- list empty WHILE the release itself is alive -- is allowed to
skip anything.

    list_len   latest_tag   verdict
    ---------  -----------  -----------------------------------------
    None       any          proceed   (never claim what we did not see)
    > 0        any          proceed   (the list answers; west decides)
    0          None         proceed   (broader outage, not this one)
    0          "v1.0.1"     upstream-list-empty

The fetchers are injected rather than mocked-in-place for the reason
`test_glibc_floor_scan.py` records: a default argument binds once at import, so
a `monkeypatch.setattr` on the module would leave the real network call in
place while the test believed it had swapped it, and the test would pass for
the wrong reason.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sdk_release_list_probe.py"
_spec = importlib.util.spec_from_file_location("sdk_release_list_probe", _SCRIPT)
assert _spec and _spec.loader
probe_mod = importlib.util.module_from_spec(_spec)
sys.modules["sdk_release_list_probe"] = probe_mod
_spec.loader.exec_module(probe_mod)

PINNED = "1.0.1"


def _fetchers(list_len, latest_tag):
    """Injected fetchers that also record that they were consulted."""
    calls = []

    def fetch_list_len():
        calls.append("list")
        return list_len

    def fetch_latest_tag():
        calls.append("latest")
        return latest_tag

    return fetch_list_len, fetch_latest_tag, calls


# --------------------------------------------------------------------------
# The truth table. One test per row, named after the row.
# --------------------------------------------------------------------------


def test_an_empty_list_beside_a_live_latest_is_the_outage_signature():
    # Arrange -- the exact measurement in tan-cli#840's body: length 0 from
    # the list endpoint while `releases/latest` still resolves to v1.0.1.
    fl, flt, calls = _fetchers(0, "v1.0.1")

    # Act
    result = probe_mod.probe(PINNED, fetch_list_len=fl, fetch_latest_tag=flt)

    # Assert
    assert result.verdict == probe_mod.VERDICT_UPSTREAM_LIST_EMPTY
    assert result.is_outage is True
    assert calls == ["list", "latest"], (
        "both endpoints must be consulted -- the conjunction IS the diagnosis, "
        f"and this run consulted {calls}"
    )


def test_an_unmeasurable_list_proceeds():
    """`gh api` itself failed. We measured nothing, so we claim nothing --
    west runs, and if it fails the job goes red with west's own message."""
    fl, flt, _ = _fetchers(None, "v1.0.1")

    result = probe_mod.probe(PINNED, fetch_list_len=fl, fetch_latest_tag=flt)

    assert result.verdict == probe_mod.VERDICT_PROCEED
    assert result.is_outage is False


def test_an_empty_list_with_an_unreachable_latest_proceeds():
    """Both endpoints down is a broader outage (or a token/network fault on
    this runner), not the tan-cli#840 signature. Skipping the build gate on
    it would mean any total loss of api.github.com silently disables the ARM
    build this job exists for."""
    fl, flt, _ = _fetchers(0, None)

    result = probe_mod.probe(PINNED, fetch_list_len=fl, fetch_latest_tag=flt)

    assert result.verdict == probe_mod.VERDICT_PROCEED
    assert result.is_outage is False


def test_a_populated_list_proceeds():
    """The recovery check the issue ends with: non-zero means the failure mode
    is gone and west decides on its own."""
    fl, flt, calls = _fetchers(100, "v1.0.1")

    result = probe_mod.probe(PINNED, fetch_list_len=fl, fetch_latest_tag=flt)

    assert result.verdict == probe_mod.VERDICT_PROCEED
    assert result.is_outage is False
    assert calls == ["list"], (
        "a populated list settles it -- the second call is wasted quota "
        f"against an API this repo has already rate-limited once; got {calls}"
    )


# --------------------------------------------------------------------------
# The message. A verdict nobody can read costs the same investigation twice.
# --------------------------------------------------------------------------


def test_the_outage_message_clears_the_pin_by_name():
    """The whole point of tan-cli#840: the reader must not go and edit a pin
    that is correct. So the message names the pinned version AND says it is
    not the cause."""
    fl, flt, _ = _fetchers(0, "v1.0.1")

    result = probe_mod.probe(PINNED, fetch_list_len=fl, fetch_latest_tag=flt)

    assert PINNED in result.message
    assert "v1.0.1" in result.message, "the live latest tag is the evidence"
    assert probe_mod.SDK_NG_REPO in result.message
    lowered = result.message.lower()
    assert "empty" in lowered
    assert "not the pin" in lowered, (
        "a reader who skims this line must come away knowing the pin is "
        f"exonerated, not merely unmentioned: {result.message!r}"
    )


def test_the_proceed_message_does_not_announce_an_outage():
    """Anti-false-alarm: the ordinary path must not print outage words that a
    log reader would then chase."""
    fl, flt, _ = _fetchers(100, "v1.0.1")

    result = probe_mod.probe(PINNED, fetch_list_len=fl, fetch_latest_tag=flt)

    assert "::warning" not in result.message
    assert "empty" not in result.message.lower()


# --------------------------------------------------------------------------
# main(): the seam between this script and the workflow step.
# --------------------------------------------------------------------------


def test_main_writes_the_verdict_where_the_step_reads_it(tmp_path, capsys):
    """The key name is a contract with `getting-started.yml`. A rename on
    either side alone leaves the workflow reading an unset output, which
    evaluates as "no outage" and runs everything -- safe, but silently
    unable to ever report the outage this script exists to report."""
    out = tmp_path / "gh-output"
    fl, flt, _ = _fetchers(0, "v1.0.1")

    rc = probe_mod.main(
        ["--version", PINNED],
        github_output=out,
        fetch_list_len=fl,
        fetch_latest_tag=flt,
    )

    assert rc == 0, "the probe is a measurement, not a gate -- it never reds"
    written = out.read_text(encoding="utf-8")
    assert f"{probe_mod.OUTPUT_KEY}=true" in written, written
    assert "::warning::" in capsys.readouterr().out


def test_main_writes_false_on_the_ordinary_path(tmp_path):
    out = tmp_path / "gh-output"
    fl, flt, _ = _fetchers(100, "v1.0.1")

    rc = probe_mod.main(
        ["--version", PINNED],
        github_output=out,
        fetch_list_len=fl,
        fetch_latest_tag=flt,
    )

    assert rc == 0
    assert f"{probe_mod.OUTPUT_KEY}=false" in out.read_text(encoding="utf-8")


def test_main_without_a_github_output_still_runs(capsys):
    """Run by hand on a workstation, reproducing the issue, there is no
    `$GITHUB_OUTPUT`. That must print rather than crash."""
    fl, flt, _ = _fetchers(0, "v1.0.1")

    rc = probe_mod.main(
        ["--version", PINNED],
        github_output=None,
        fetch_list_len=fl,
        fetch_latest_tag=flt,
    )

    assert rc == 0
    assert "::warning::" in capsys.readouterr().out


def test_the_real_fetchers_are_resolved_at_call_time():
    """The tan-cli#450 trap, guarded here rather than rediscovered. If the
    module bound its real fetchers as default arguments, they would be
    captured once at import and this monkeypatch-shaped swap would be
    silently ignored -- the test would exercise the real `gh api` call while
    believing it had replaced it."""
    saved = (probe_mod._gh_release_list_len, probe_mod._gh_latest_release_tag)
    try:
        probe_mod._gh_release_list_len = lambda: 0
        probe_mod._gh_latest_release_tag = lambda: "v1.0.1"
        result = probe_mod.probe(PINNED)
        assert result.verdict == probe_mod.VERDICT_UPSTREAM_LIST_EMPTY
    finally:
        # BOTH restored: leaving either swapped would make every later test
        # in this session depend on collection order.
        probe_mod._gh_release_list_len, probe_mod._gh_latest_release_tag = saved
