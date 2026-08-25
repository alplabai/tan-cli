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
`scripts/glibc_floor_scan.py:44-51` records: a default argument binds once at
import, so a `monkeypatch.setattr` on the module would leave the real network
call in place while the test believed it had swapped it, and the test would
pass for the wrong reason.

Injection alone would leave `_gh` and its two callers -- the whole real
network half -- exercised by nothing, which is its own hazard: a typo in an
endpoint string makes `gh api` fail, `probe` reports `proceed`, and the fix is
inert forever while every test stays green. The module's own comment says a
wrong repository value would "report `proceed` forever". `TestTheRealGhPath`
below therefore fakes `subprocess.run` and pins the argv, so the strings are
checked without a network.
"""

from __future__ import annotations

import importlib.util
import subprocess
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


def test_the_outage_message_says_west_is_no_evidence_about_the_pin():
    """The whole point of tan-cli#840: the reader must not go and edit a pin
    on the strength of west's message. The message must say so, and name the
    evidence it actually has."""
    fl, flt, _ = _fetchers(0, "v1.0.1")

    result = probe_mod.probe(PINNED, fetch_list_len=fl, fetch_latest_tag=flt)

    assert PINNED in result.message
    assert "v1.0.1" in result.message, "the live latest tag is the evidence"
    assert probe_mod.SDK_NG_REPO in result.message
    lowered = result.message.lower()
    assert "empty" in lowered
    assert "not evidence about the pin" in lowered, (
        "a reader who skims this line must come away knowing west's message "
        f"proves nothing about the pin: {result.message!r}"
    )


def test_the_outage_message_does_not_claim_the_pin_is_correct():
    """The probe measures two ENDPOINT states. It never reads the pin, so it
    must not vouch for it. An earlier draft asserted the pin `is correct`
    because `check_toolchain_lock.py` re-derives it -- a script that does not
    exist in this repository at all (it is alp-sdk's), applied to a pinned SDK
    revision this job does not re-verify. If a list outage ever coincided with
    a genuinely bad pin, that sentence would have been a flat falsehood
    printed at the exact moment the evidence was being skipped."""
    fl, flt, _ = _fetchers(0, "v1.0.1")

    result = probe_mod.probe(PINNED, fetch_list_len=fl, fetch_latest_tag=flt)

    lowered = result.message.lower()
    for overclaim in ("is correct", "check_toolchain_lock"):
        assert overclaim not in lowered, (
            f"the outage message claims {overclaim!r}, which this probe never "
            f"measured: {result.message!r}"
        )


def test_the_proceed_message_does_not_announce_an_outage():
    """Anti-false-alarm: the ordinary path must not raise an annotation a log
    reader would then chase."""
    fl, flt, _ = _fetchers(100, "v1.0.1")

    result = probe_mod.probe(PINNED, fetch_list_len=fl, fetch_latest_tag=flt)

    assert "::warning" not in result.message
    assert probe_mod.VERDICT_UPSTREAM_LIST_EMPTY not in result.message


def test_the_populated_list_message_does_not_vouch_for_the_pin():
    """The case this probe does NOT detect: a non-empty but stale or truncated
    list that omits the pin. west then prints the same `Unavailable SDK
    version` message, and a proceed line reading "upstream lists 100 releases"
    invites exactly the wrong conclusion -- that upstream is healthy, so the
    pin must really be wrong. The line must claim only that the list is not
    empty, hand the membership question to west, and say the short-list case
    is undetected."""
    fl, flt, _ = _fetchers(100, "v1.0.1")

    result = probe_mod.probe(PINNED, fetch_list_len=fl, fetch_latest_tag=flt)

    lowered = result.message.lower()
    assert "west decides" in lowered, result.message
    assert "not detected" in lowered, (
        "the populated-list line must name the partial-list case it cannot "
        f"see, or it reads as an all-clear it has not earned: {result.message!r}"
    )


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


# --------------------------------------------------------------------------
# The real network half. Nothing above reaches `_gh`, so a typo in an endpoint
# string or a jq program would make every `gh api` call fail -- which this
# module deliberately treats as "not a measurement" and turns into `proceed`.
# The fix would then be inert forever with the whole suite green. These pin the
# argv without a network.
# --------------------------------------------------------------------------


class _FakeRun:
    """Records the argv it was handed and replays a scripted result."""

    def __init__(self, returncode=0, stdout="", raises=None):
        self.calls = []
        self._returncode = returncode
        self._stdout = stdout
        self._raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(
            argv, self._returncode, stdout=self._stdout, stderr=""
        )


class TestTheRealGhPath:
    def test_the_list_call_asks_the_release_list_endpoint_for_its_length(
        self, monkeypatch
    ):
        fake = _FakeRun(stdout="100\n")
        monkeypatch.setattr(probe_mod.subprocess, "run", fake)

        assert probe_mod._gh_release_list_len() == 100

        (argv, kwargs), = fake.calls
        assert argv == [
            "gh",
            "api",
            "repos/zephyrproject-rtos/sdk-ng/releases?per_page=100",
            "--jq",
            "length",
        ], argv
        assert kwargs["timeout"] == probe_mod._GH_TIMEOUT_S
        assert kwargs["check"] is False, (
            "check=True would raise instead of returning a non-zero result, "
            "and the caller's None-means-unmeasured contract would never fire"
        )

    def test_the_latest_call_asks_the_latest_endpoint_for_its_tag(self, monkeypatch):
        fake = _FakeRun(stdout="v1.0.1\n")
        monkeypatch.setattr(probe_mod.subprocess, "run", fake)

        assert probe_mod._gh_latest_release_tag() == "v1.0.1"

        (argv, _), = fake.calls
        assert argv == [
            "gh",
            "api",
            "repos/zephyrproject-rtos/sdk-ng/releases/latest",
            "--jq",
            ".tag_name",
        ], argv

    def test_the_repo_both_calls_name_is_the_one_west_resolves_against(self):
        """The module's own comment says a wrong value here would 'silently
        probe some other repository and report proceed forever'. Pinned."""
        assert probe_mod.SDK_NG_REPO == "zephyrproject-rtos/sdk-ng"

    def test_a_non_zero_gh_exit_is_not_a_measurement(self, monkeypatch):
        monkeypatch.setattr(
            probe_mod.subprocess, "run", _FakeRun(returncode=1, stdout="")
        )
        assert probe_mod._gh_release_list_len() is None
        assert probe_mod._gh_latest_release_tag() is None

    def test_a_missing_gh_binary_is_not_a_measurement(self, monkeypatch):
        monkeypatch.setattr(
            probe_mod.subprocess,
            "run",
            _FakeRun(raises=FileNotFoundError("gh")),
        )
        assert probe_mod._gh_release_list_len() is None

    def test_a_timeout_is_not_a_measurement(self, monkeypatch):
        monkeypatch.setattr(
            probe_mod.subprocess,
            "run",
            _FakeRun(raises=subprocess.TimeoutExpired(["gh"], 30)),
        )
        assert probe_mod._gh_release_list_len() is None

    def test_an_unparseable_length_is_not_a_measurement(self, monkeypatch):
        """A body that is not an integer -- an error object, an HTML error
        page -- must not be coerced into a count."""
        monkeypatch.setattr(probe_mod.subprocess, "run", _FakeRun(stdout="banana"))
        assert probe_mod._gh_release_list_len() is None

    def test_a_blank_latest_tag_is_not_a_measurement(self, monkeypatch):
        """`--jq .tag_name` on a body without that key prints an empty line at
        exit 0. Empty is not a tag."""
        monkeypatch.setattr(probe_mod.subprocess, "run", _FakeRun(stdout="\n"))
        assert probe_mod._gh_latest_release_tag() is None

    def test_the_outage_verdict_is_reachable_through_the_real_fetchers(
        self, monkeypatch
    ):
        """End to end with no injection at all: only `subprocess.run` is faked,
        so `probe` resolves both real fetchers and the outage row is reached
        through the same code the runner executes."""

        def fake_run(argv, **kwargs):
            body = "0\n" if "releases?per_page=100" in argv[2] else "v1.0.1\n"
            return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")

        monkeypatch.setattr(probe_mod.subprocess, "run", fake_run)

        result = probe_mod.probe(PINNED)

        assert result.verdict == probe_mod.VERDICT_UPSTREAM_LIST_EMPTY
        assert result.latest_tag == "v1.0.1"


# --------------------------------------------------------------------------
# The step summary: on the outage path the job goes GREEN having skipped the
# real ARM build, and the commit-status surface cannot tell that run from one
# that built.
# --------------------------------------------------------------------------


def test_main_records_the_skipped_build_in_the_step_summary(tmp_path):
    summary = tmp_path / "summary.md"
    fl, flt, _ = _fetchers(0, "v1.0.1")

    probe_mod.main(
        ["--version", PINNED],
        github_output=tmp_path / "out",
        step_summary=summary,
        fetch_list_len=fl,
        fetch_latest_tag=flt,
    )

    written = summary.read_text(encoding="utf-8")
    assert "did NOT perform the ARM build" in written, written
    assert "v1.0.1" in written
    assert probe_mod.SDK_NG_REPO in written


def test_main_writes_no_step_summary_on_the_ordinary_path(tmp_path):
    """A summary section on every green run is noise, and noise is how the
    outage section stops being read."""
    summary = tmp_path / "summary.md"
    fl, flt, _ = _fetchers(100, "v1.0.1")

    probe_mod.main(
        ["--version", PINNED],
        github_output=tmp_path / "out",
        step_summary=summary,
        fetch_list_len=fl,
        fetch_latest_tag=flt,
    )

    assert not summary.exists()
