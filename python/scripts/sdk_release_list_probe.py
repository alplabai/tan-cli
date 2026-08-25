#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tell an EMPTY upstream sdk-ng release list apart from a bad Zephyr SDK pin.

tan-cli#840. `west sdk install --version <v>` resolves `<v>` against
`GET /repos/zephyrproject-rtos/sdk-ng/releases`, and when that endpoint answers
`[]` it reports the empty list in the same words it uses for a version that
genuinely does not exist:

    FATAL ERROR: Unavailable SDK version: 1.0.1.Please select from the list below:

The blank line after that colon is the whole diagnosis, and it is the only
tell. `getting-started.yml` runs that command because it is the remedy `tan
doctor` PRINTS, so when the list goes empty the gate reds with a message
accusing a pin that `scripts/check_toolchain_lock.py` re-derives from the
Zephyr revision at every run. The measured incident cost one full
investigation to reach "not ours"; this script exists so the next one costs a
line of log.

It is a PROBE, not a gate. It never exits non-zero -- it measures, writes a
verdict to `$GITHUB_OUTPUT`, and lets the step decide.

WHY OVER-CLAIMING IS THE DANGEROUS DIRECTION
---------------------------------------------

On `outage=true` the step skips `west sdk install`, and `getting-started.yml`
skips the two steps behind it that need a toolchain -- the real ARM build of
the scaffolded project, which is the reason the job exists. A probe that
over-claims therefore turns off the build gate silently, on every PR, while
the job still reports green. A probe that under-claims merely costs one red
run with west's own message, which is where we were before this file.

So the verdict is deliberately lopsided. Only the exact conjunction the issue
measured -- the LIST empty WHILE the release itself is alive -- skips
anything. Everything else proceeds:

    list_len   latest_tag   verdict              reasoning
    ---------  -----------  -------------------  ------------------------------
    None       any          proceed              the call failed; we measured
                                                 nothing, so we claim nothing
    > 0        any          proceed              the list answers; west decides
    0          None         proceed              both endpoints down is a
                                                 broader outage, not this one
    0          "v1.0.1"     upstream-list-empty  the tan-cli#840 signature

The third row is the one worth defending. `api.github.com` being wholly
unreachable would otherwise satisfy "the list is empty" and disable the ARM
build on every PR for the duration.

WHAT THIS DOES NOT DETECT, ON PURPOSE
--------------------------------------

A PARTIAL list -- non-empty, but stale or truncated so that it omits the
pinned version -- lands in row 2 and proceeds, and west then prints the same
`Unavailable SDK version` message this file exists to explain. That case is
NOT covered, and it cannot be covered by counting: "the list is short and the
pin is absent" and "the pin is genuinely wrong" are the same observation from
here, and only the second one should ever be reported as a pin problem.
Telling them apart needs a source of truth for the pin, which is
`metadata/toolchains.json` in the PINNED alp-sdk -- a different measurement
than this probe makes, and the reason row 2's message below is careful to
claim only that the list is not empty, and to hand the "is the pin among
them" question to west rather than implying an answer.

WHY THE FETCHERS ARE INJECTED
------------------------------

`fetch_list_len` / `fetch_latest_tag` are parameters of `probe`, resolved at
CALL time from the module globals -- not bound as default arguments, which are
evaluated once at import and would make a `monkeypatch.setattr` on this module
silently ineffective. `scripts/glibc_floor_scan.py:44-51` records that same
trap costing a green test that exercised the real reader while believing it
had swapped it. Injection is also what lets the truth table above be driven
on all three required legs rather than only where `gh` is installed and
authenticated.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

#: The upstream that publishes the Zephyr SDK releases `west sdk install`
#: lists. Verbatim rather than derived: it is the subject of the measurement,
#: and a wrong value here would silently probe some other repository and
#: report `proceed` forever.
SDK_NG_REPO = "zephyrproject-rtos/sdk-ng"

#: The two verdicts. Strings rather than a bool so the log and the
#: `$GITHUB_OUTPUT` line both name what was concluded.
VERDICT_PROCEED = "proceed"
VERDICT_UPSTREAM_LIST_EMPTY = "upstream-list-empty"

#: The `$GITHUB_OUTPUT` key `getting-started.yml` reads. A rename on one side
#: alone leaves the workflow reading an unset output, which evaluates as "no
#: outage" -- safe, but permanently unable to report the outage this script is
#: for. `test_main_writes_the_verdict_where_the_step_reads_it` and the
#: workflow-shape gate hold the two sides together.
OUTPUT_KEY = "outage"

#: Seconds per `gh api` call. A hung probe must not become a hung job: the
#: whole point is to spend less than the 50 s the three-attempt retry loop
#: already burns before failing.
_GH_TIMEOUT_S = 30


@dataclass(frozen=True)
class ProbeResult:
    """What was measured and what it means. Frozen: callers report it, never
    edit it."""

    verdict: str
    list_len: int | None
    latest_tag: str | None
    message: str

    @property
    def is_outage(self) -> bool:
        return self.verdict == VERDICT_UPSTREAM_LIST_EMPTY


def _gh(args: list[str]) -> str | None:
    """One `gh api` call. Returns its stdout, or None if the call itself
    failed -- a failed CALL is not evidence about the endpoint's contents,
    and the caller must not treat it as any."""
    try:
        proc = subprocess.run(
            ["gh", "api", *args],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _gh_release_list_len() -> int | None:
    """How many releases the LIST endpoint reports. None if unmeasurable."""
    out = _gh([f"repos/{SDK_NG_REPO}/releases?per_page=100", "--jq", "length"])
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        # An answer we cannot parse is not a measurement either.
        return None


def _gh_latest_release_tag() -> str | None:
    """The tag `releases/latest` resolves to. None if unreachable or blank."""
    out = _gh([f"repos/{SDK_NG_REPO}/releases/latest", "--jq", ".tag_name"])
    return out or None


def probe(
    pinned_version: str,
    fetch_list_len: Callable[[], int | None] | None = None,
    fetch_latest_tag: Callable[[], str | None] | None = None,
) -> ProbeResult:
    """Measure the two endpoints and classify the result.

    `fetch_latest_tag` is consulted ONLY when the list came back empty. A
    populated list already settles the question, and this repository has
    already been rate-limited against this API once -- see the `GH_TOKEN`
    comment on `getting-started.yml`'s "install the Zephyr SDK" step. Cited by
    step name rather than by line: an earlier draft of this file cited
    `getting-started.yml:490-501`, and the change that added the probe step
    pushed those lines down by 57, so the citation was stale in the same
    commit that wrote it (tan-cli#899).
    """
    # Resolved here, at call time -- see the module docstring.
    if fetch_list_len is None:
        fetch_list_len = _gh_release_list_len
    if fetch_latest_tag is None:
        fetch_latest_tag = _gh_latest_release_tag

    list_len = fetch_list_len()

    if list_len is None:
        return ProbeResult(
            VERDICT_PROCEED,
            None,
            None,
            f"could not measure the release list for {SDK_NG_REPO} -- the "
            f"gh api call did not answer. Proceeding: anything that fails "
            f"below is west's own to report.",
        )

    if list_len > 0:
        return ProbeResult(
            VERDICT_PROCEED,
            list_len,
            None,
            f"{SDK_NG_REPO} lists {list_len} releases, so the list endpoint "
            f"is not empty and this is not the tan-cli#840 signature. "
            f"Proceeding: west decides whether {pinned_version} is among "
            f"them. A short or stale list that omits it is NOT detected here "
            f"-- see the module docstring.",
        )

    latest_tag = fetch_latest_tag()

    if latest_tag is None:
        return ProbeResult(
            VERDICT_PROCEED,
            0,
            None,
            f"{SDK_NG_REPO} answered 0 releases AND releases/latest did not "
            f"resolve either. That is a broader upstream or network fault, "
            f"not the narrow list outage this probe is for. Proceeding, so "
            f"the job still reds.",
        )

    return ProbeResult(
        VERDICT_UPSTREAM_LIST_EMPTY,
        0,
        latest_tag,
        f"::warning::{SDK_NG_REPO} returned an EMPTY release list (0 "
        f"releases) while releases/latest still resolves to {latest_tag}. "
        f"west sdk install reports that empty list as "
        f'"Unavailable SDK version: {pinned_version}", so that message is '
        f"not evidence about the pin: an empty list produces it whatever the "
        f"pin says. This probe did not measure the pin and does not claim it "
        f"is right. This is tan-cli#840. Skipping the install and the "
        f"SDK-backed build steps behind it.",
    )


def _resolve_env_path(explicit: Path | None, var: str) -> Path | None:
    """An explicit path wins; otherwise read `var` from the environment at
    CALL time. Never a default argument -- that would freeze one process
    environment at import."""
    if explicit is not None:
        return explicit
    value = os.environ.get(var)
    return Path(value) if value else None


def main(
    argv: list[str] | None = None,
    github_output: Path | None = None,
    step_summary: Path | None = None,
    fetch_list_len: Callable[[], int | None] | None = None,
    fetch_latest_tag: Callable[[], str | None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        required=True,
        help="the pinned Zephyr SDK version, so the message can exonerate it "
        "by name (read from alp-sdk metadata/toolchains.json by the caller)",
    )
    args = parser.parse_args(argv)

    result = probe(
        args.version,
        fetch_list_len=fetch_list_len,
        fetch_latest_tag=fetch_latest_tag,
    )
    print(result.message)

    github_output = _resolve_env_path(github_output, "GITHUB_OUTPUT")
    if github_output is not None:
        with github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"{OUTPUT_KEY}={'true' if result.is_outage else 'false'}\n")

    # On the outage path the job goes GREEN having skipped the real ARM build.
    # A `::warning::` annotation is the only other trace, and the commit-status
    # surface -- the one anything downstream actually reads -- cannot tell that
    # run apart from one that built. The step summary is where a human landing
    # on the run page sees it without knowing to look for annotations. It does
    # not change the conclusion; nothing here should red a PR over somebody
    # else's outage.
    step_summary = _resolve_env_path(step_summary, "GITHUB_STEP_SUMMARY")
    if result.is_outage and step_summary is not None:
        with step_summary.open("a", encoding="utf-8") as handle:
            handle.write(
                "### Zephyr SDK install skipped -- upstream release list "
                "outage (tan-cli#840)\n\n"
                f"`{SDK_NG_REPO}` returned an empty release list while "
                f"`releases/latest` still resolved to `{result.latest_tag}`.\n\n"
                "**This run did NOT perform the ARM build.** `west sdk "
                "install`, `tan build`, the ARM-ELF assertion and the two "
                "SDK-dependent dirty-host steps were all skipped. A green "
                "conclusion here does not mean the toolchain path was "
                "exercised -- re-run the job once upstream recovers.\n"
            )

    # Always 0. This is a measurement; the step decides what to do with it,
    # and a probe that could red the job would be a second way for an
    # upstream hiccup to fail a PR about something else.
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
