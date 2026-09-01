# SPDX-License-Identifier: Apache-2.0
"""EXECUTE `release-combination.yml`'s consumer-pin resolution and dedup --
tan-cli#1050.

`release-combination.yml` is `schedule` + `workflow_dispatch` only, so nothing
in this suite can exercise it end to end and no PR run ever will. That is
exactly how tan-cli#1050 survived review and a green CI: #767's
`resolve-consumer-pin` read alp-sdk-vscode's `SUPPORTED_CLI_VERSION` off its
`dev` branch alone, and `dev` is structurally the one branch whose pin cannot
stay diverged from tan's own `latest` (it moves to the newest pin as soon as
one is cut). So `skip=true` fired on EVERY scheduled run, the leg #767 added
executed on no day at all, and the pin alp-sdk-vscode actually SHIPS -- on
`main`, `0.5.1` on 2026-08-31, proven RED against alp-sdk `v0.16.0` by PR
#1047's own dispatched run 33397989209 -- was read by nothing.

This module is the only proof available for the fix. It does two things:

1. STATIC: `yaml.safe_load`s the workflow and asserts the job graph is what
   the header claims -- the four jobs, their `needs:` edges, the outputs
   `build-matrix` consumes, that `resolve-consumer-pin` fetches BOTH
   `/dev/` and `/main/` copies of `src/alpCli/service.ts`, and that `journey`
   is driven off `fromJSON(needs.build-matrix.outputs.matrix)`.

2. DYNAMIC: extracts the `run:` bodies of `resolve-consumer-pin`'s and
   `build-matrix`'s steps VERBATIM out of the YAML (the mechanism
   `test_planner_resync_pr_step_executes.py` uses, so this cannot drift from
   the workflow) and runs them under a real `bash` with `curl` and `gh`
   stubbed on `PATH` and a real `$GITHUB_OUTPUT` file. Every dedup case the
   header enumerates is asserted on the ACTUAL emitted
   `consumer_matrix`/`skip`, not on a re-implementation of the logic:

   * all three versions equal        -> `skip=true`, `consumer_matrix=[]`
   * `dev` == `main` != `latest`     -> ONE leg, `consumer-pin-dev+main`
   * all three differ                -> TWO legs, `consumer-pin-dev` and
                                        `consumer-pin-main`
   * `dev` == `latest` != `main`     -> just `consumer-pin-main`
   * `main` == `latest` != `dev`     -> just `consumer-pin-dev`

   ...plus the failure verdicts, which are the half that must not degrade
   quietly: a fetch that 404s on EITHER branch, a renamed constant on either
   branch, and a `service.ts` carrying two `SUPPORTED_CLI_VERSION = "..."`
   matches all exit non-zero with an `::error::` that names the branch.
   Continuing on a partial pin set would recreate #1050 exactly -- a leg that
   does not run, hiding a shipped combination.

Both `run:` bodies are pure `env:` indirection (zizmor's template-injection
rule), so neither contains a `${{ }}` expression the runner would have
resolved before bash saw it -- there is nothing to substitute here, unlike
`test_planner_resync_pr_step_executes.py`. That is asserted below rather than
assumed, so a future `${{ }}` added to either body cannot silently run as
literal text.
"""

from __future__ import annotations

import functools
import json
import pathlib
import shutil
import subprocess

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-combination.yml"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash to run the step body with")

_RESOLVE_JOB = "resolve-consumer-pin"
_MATRIX_JOB = "build-matrix"

#: The three SKUs `build-matrix` fans every combination across. Named here so
#: a SKU silently vanishing from the catalogue reds this gate too, rather than
#: shrinking the matrix invisibly.
_SKUS = ("E1M-AEN801", "E1M-V2N101", "E1M-NX9101")


@functools.cache
def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _only_step_run(job: str) -> str:
    steps = _workflow()["jobs"][job]["steps"]
    runs = [s["run"] for s in steps if "run" in s]
    assert len(runs) == 1, f"expected exactly one `run:` step in `{job}`, found {len(runs)}"
    body = runs[0]
    assert "${{" not in body, (
        f"`{job}`'s `run:` body now contains a `${{{{ }}}}` expression. The GitHub "
        "Actions runner resolves those before bash ever sees the script; this gate "
        "runs the body under a plain bash, where it would execute as literal text "
        "and prove nothing. Move it to `env:` (which is also what zizmor's "
        "template-injection audit wants), or teach this gate to substitute it."
    )
    return body


# --------------------------------------------------------------------------
# 1. STATIC: the job graph is what the header claims
# --------------------------------------------------------------------------


def test_the_four_jobs_and_their_needs_edges():
    jobs = _workflow()["jobs"]
    assert set(jobs) == {"resolve-refs", _RESOLVE_JOB, _MATRIX_JOB, "journey"}
    assert jobs[_MATRIX_JOB]["needs"] == ["resolve-refs", _RESOLVE_JOB]
    assert jobs["journey"]["needs"] == _MATRIX_JOB
    assert _RESOLVE_JOB not in (jobs["resolve-refs"].get("needs") or []), (
        "resolve-refs must stay independent of the consumer-pin resolution"
    )
    for name, job in jobs.items():
        assert "timeout-minutes" in job, f"{name} is unbounded"


def test_resolve_consumer_pin_publishes_the_matrix_build_matrix_consumes():
    jobs = _workflow()["jobs"]
    outputs = jobs[_RESOLVE_JOB]["outputs"]
    assert set(outputs) == {"consumer_matrix", "skip"}, (
        "tan-cli#1050 replaced the single `consumer_tan_version` output with a JSON "
        f"array of deduped combinations; got {sorted(outputs)}"
    )
    env = jobs[_MATRIX_JOB]["steps"][0]["env"]
    assert env["CONSUMER_MATRIX"] == f"${{{{ needs.{_RESOLVE_JOB}.outputs.consumer_matrix }}}}"
    assert env["CONSUMER_SKIP"] == f"${{{{ needs.{_RESOLVE_JOB}.outputs.skip }}}}"
    assert jobs["journey"]["strategy"]["matrix"] == (
        f"${{{{ fromJSON(needs.{_MATRIX_JOB}.outputs.matrix) }}}}"
    )


@pytest.mark.parametrize("branch", ["dev", "main"])
def test_both_alp_sdk_vscode_branches_are_fetched(branch: str):
    """THE tan-cli#1050 invariant, stated statically.

    #767 fetched `/dev/` and nothing else. If a future edit drops either
    branch from the resolution, the version alp-sdk-vscode ships (`main`) or
    the version it is about to ship (`dev`) stops being tested and the skip
    notice goes back to being unconditionally true.
    """
    body = _only_step_run(_RESOLVE_JOB)
    url = f"https://raw.githubusercontent.com/alplabai/alp-sdk-vscode/${{branch}}/src/alpCli/service.ts"
    assert f"resolve_pin {branch}" in body, (
        "`resolve-consumer-pin` no longer resolves BOTH alp-sdk-vscode branches "
        f"(looking for `resolve_pin {branch}`) -- that is tan-cli#1050 exactly: "
        "`dev`'s pin tracks tan's own `latest` by construction, so reading it "
        "alone makes the consumer-pin leg skip on every scheduled run while "
        "`main` carries the pin actually shipped to users."
    )
    assert url in body, "the per-branch raw.githubusercontent.com fetch was rewritten"


def test_the_workflow_stays_schedule_and_dispatch_only():
    """The scope decision tan-cli#639/#767/#1050 all restate in prose, pinned.

    This gate depends on release tags in two other repos and a pin in a third;
    it is deliberately NOT a required PR check, and the header says so three
    times. Nothing asserted it. Adding `pull_request:` here would put a
    ~60-minute-per-leg journey (three to nine legs) on every PR and make a
    third repo's pin move able to block an unrelated merge.
    """
    # YAML 1.1 parses a bare `on:` key as the boolean True; both spellings are
    # accepted so this cannot rot on a PyYAML/loader change.
    raw = _workflow()
    triggers = raw.get("on", raw.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}, (
        f"release-combination.yml's triggers are now {sorted(triggers)}. It is "
        "`schedule` + `workflow_dispatch` ONLY by design -- see the file "
        "header's NOT-a-required-PR-check paragraph."
    )
    assert set(triggers["workflow_dispatch"]["inputs"]) == {
        "tan_version",
        "consumer_tan_version",
        "alp_sdk_ref",
    }


# --------------------------------------------------------------------------
# 2. DYNAMIC: run the bodies
# --------------------------------------------------------------------------

_SERVICE_TS = """\
// unrelated preamble
export const SOMETHING_ELSE = "1.2.3";
export const SUPPORTED_CLI_VERSION = "{version}";
export const RENESAS_BUILD_CLI_VERSION = "0.6.0-rc1";
"""


#: The ONE GitHub API question this job is allowed to ask. The dedup baseline
#: is "the version the `latest` leg will actually install", which comes from
#: **tan-cli's** own latest release -- not alp-sdk's, not alp-sdk-vscode's.
#: Review of PR #1088 measured what an argv-blind `gh` stub costs: repointing
#: this endpoint to `repos/alplabai/alp-sdk/releases/latest` (an alp-sdk tag,
#: which no consumer pin can ever equal) left the gate at 22/22 GREEN while
#: making `skip=true` unreachable and adding six extra ~60-minute journey legs
#: to every scheduled run, forever -- the exact inverse of tan-cli#1050. A
#: stub that answers every question the same way cannot prove which question
#: was asked, so this one refuses anything else.
_EXPECTED_GH_ARGV = "api repos/alplabai/tan-cli/releases/latest -q .tag_name"

#: Same reasoning for `curl`: the stub serves a branch fixture, so it must
#: first prove the URL it was handed is the per-branch `service.ts` on
#: alp-sdk-vscode and nothing else.
_EXPECTED_CURL_URL_RE = (
    "^https://raw[.]githubusercontent[.]com/alplabai/alp-sdk-vscode/"
    "(dev|main)/src/alpCli/service[.]ts$"
)


def _stub_bin(tmp_path: pathlib.Path, *, latest_tan: str, dev: str | None, main: str | None) -> pathlib.Path:
    """A `PATH` dir with a `curl` and a `gh` that ASSERT their argv, then
    answer from fixtures.

    `dev`/`main` are the raw `service.ts` bodies to serve for that branch;
    `None` means "this branch 404s", which the real
    `curl -fsSL --retry 3 --retry-connrefused` reports as a non-zero exit and
    an empty body (a 404 is not a transient, so curl does not retry it).

    Every invocation is appended to `<tmp_path>/calls.log` as `<tool> <argv>`
    so a test can assert not just the answer but the flags the caller passed.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    served = tmp_path / "served"
    served.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    # Idempotent: a test may drive the step body twice under one `tmp_path`
    # (e.g. transient-vs-404). Each set-up starts from a clean fixture set and
    # an empty call log, so `_calls()` only ever describes the latest run.
    calls.unlink(missing_ok=True)
    for stale in served.iterdir():
        stale.unlink()
    for branch, content in (("dev", dev), ("main", main)):
        if content is not None:
            (served / branch).write_text(content, encoding="utf-8")

    (bindir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "curl %s\\n" "$*" >> "{calls}"\n'
        'url="${@: -1}"\n'
        f'if ! printf "%s" "$url" | grep -qE \'{_EXPECTED_CURL_URL_RE}\'; then\n'
        '  echo "STUB-REFUSED: curl was asked for an unexpected URL: $url" >&2\n'
        "  exit 99\n"
        "fi\n"
        f'branch="$(printf "%s" "$url" | sed -E "s#.*/alp-sdk-vscode/([^/]+)/.*#\\1#")"\n'
        f'f="{served}/$branch"\n'
        'if [ ! -f "$f" ]; then exit 22; fi\n'
        'cat "$f"\n',
        encoding="utf-8",
    )
    (bindir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "gh %s\\n" "$*" >> "{calls}"\n'
        f'if [ "$*" != "{_EXPECTED_GH_ARGV}" ]; then\n'
        '  echo "STUB-REFUSED: the dedup baseline must come from '
        f'\'{_EXPECTED_GH_ARGV}\', got: $*" >&2\n'
        "  exit 98\n"
        "fi\n"
        f"printf '%s\\n' '{latest_tan}'\n",
        encoding="utf-8",
    )
    for f in bindir.iterdir():
        f.chmod(0o755)
    return bindir


def _run_resolve(
    tmp_path: pathlib.Path,
    *,
    latest_tan: str = "v0.6.0",
    dev: str | None = "0.6.0",
    main: str | None = "0.6.0",
    dev_raw: str | None = None,
    main_raw: str | None = None,
    override_version: str = "",
    tan_version_override: str = "",
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    dev_body = dev_raw if dev_raw is not None else (None if dev is None else _SERVICE_TS.format(version=dev))
    main_body = main_raw if main_raw is not None else (None if main is None else _SERVICE_TS.format(version=main))
    bindir = _stub_bin(tmp_path, latest_tan=latest_tan, dev=dev_body, main=main_body)
    gh_output = tmp_path / "github_output"
    gh_output.touch()
    script = tmp_path / "resolve.sh"
    script.write_text(_only_step_run(_RESOLVE_JOB), encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GITHUB_OUTPUT": str(gh_output),
            "GH_TOKEN": "stub",
            "OVERRIDE_VERSION": override_version,
            "TAN_VERSION_OVERRIDE": tan_version_override,
        },
    )
    parsed: dict[str, str] = {}
    for line in gh_output.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key] = value
    return proc, parsed


def _combinations(parsed: dict[str, str]) -> list[tuple[str, str, str]]:
    """`(combination, tan_version, label)` per resolved leg.

    The LABEL is in here deliberately (PR #1088 review, hole 2). It is not
    cosmetic: `journey`'s `name:` is
    `"${{ matrix.sku }} (${{ matrix.combination_label }}) -- ..."`, so two legs
    sharing a label are two runs a human cannot tell apart in the Actions tab
    -- and the six-case dedup table this module pins distinguishes its cases
    by exactly that string. Asserting only the version count leaves the table
    unpinned; a mutant that gives both legs the label
    `alp-sdk-vscode's pinned tan` passed the first version of this gate.
    """
    return [
        (e["combination"], e["tan_version"], e["label"])
        for e in json.loads(parsed["consumer_matrix"])
    ]


def _calls(tmp_path: pathlib.Path, tool: str) -> list[str]:
    """Every `curl`/`gh` invocation the step body made, argv included."""
    log = tmp_path / "calls.log"
    if not log.exists():
        return []
    return [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.startswith(f"{tool} ")]


def test_all_three_equal_skips_loudly_with_no_extra_leg(tmp_path):
    proc, parsed = _run_resolve(tmp_path, latest_tan="v0.6.0", dev="0.6.0", main="0.6.0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert parsed["skip"] == "true"
    assert _combinations(parsed) == []
    assert "::notice::" in proc.stdout, "the skip must be LOUD -- a silent skip is tan-cli#1050"
    assert "nothing extra to test today" in proc.stdout


def test_both_branches_agree_but_differ_from_latest_gives_one_credited_leg(tmp_path):
    proc, parsed = _run_resolve(tmp_path, latest_tan="v0.7.0", dev="0.6.0", main="0.6.0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert parsed["skip"] == "false"
    assert _combinations(parsed) == [
        ("consumer-pin-dev+main", "v0.6.0", "alp-sdk-vscode@dev+main's pinned tan")
    ], (
        "one tan binary must produce ONE journey, credited to both branches -- not "
        "two identical ~250-step journeys"
    )


def test_all_three_differ_gives_two_legs(tmp_path):
    """The 2026-08-31 shape from tan-cli#1050: `main` on the shipped pin,
    `dev` already moved to the next one, `latest` a third value."""
    proc, parsed = _run_resolve(tmp_path, latest_tan="v0.7.0", dev="0.6.0", main="0.5.1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert parsed["skip"] == "false"
    assert _combinations(parsed) == [
        ("consumer-pin-dev", "v0.6.0", "alp-sdk-vscode@dev's pinned tan"),
        ("consumer-pin-main", "v0.5.1", "alp-sdk-vscode@main's pinned tan"),
    ]


def test_dev_equals_latest_still_runs_mains_shipped_pin(tmp_path):
    """The exact configuration tan-cli#1050 measured on 2026-08-31, and the
    one #767's single-branch read produced NO leg at all for."""
    proc, parsed = _run_resolve(tmp_path, latest_tan="v0.6.0", dev="0.6.0", main="0.5.1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert parsed["skip"] == "false"
    assert _combinations(parsed) == [
        ("consumer-pin-main", "v0.5.1", "alp-sdk-vscode@main's pinned tan")
    ], (
        "with `dev` == `latest`, #767's resolution skipped the whole leg; `main`'s "
        "shipped pin (RED against alp-sdk v0.16.0, run 33397989209) must still run"
    )


def test_main_equals_latest_still_runs_devs_next_pin(tmp_path):
    proc, parsed = _run_resolve(tmp_path, latest_tan="v0.5.1", dev="0.6.0", main="0.5.1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _combinations(parsed) == [
        ("consumer-pin-dev", "v0.6.0", "alp-sdk-vscode@dev's pinned tan")
    ]


def test_a_prerelease_suffix_survives_resolution(tmp_path):
    """tan-cli#767's own trap: a narrower regex truncated `0.5.0-rc1` to
    `0.5.0` and installed a different tan than the pin names."""
    proc, parsed = _run_resolve(tmp_path, latest_tan="v0.6.0", dev="0.6.0-rc1", main="0.6.0-rc1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _combinations(parsed) == [
        ("consumer-pin-dev+main", "v0.6.0-rc1", "alp-sdk-vscode@dev+main's pinned tan")
    ]


def test_the_tan_version_dispatch_override_is_what_the_pins_dedup_against(tmp_path):
    """`latest`'s leg installs the override when set, so the dedup must
    compare against that, not against install.sh's own latest."""
    proc, parsed = _run_resolve(
        tmp_path, latest_tan="v0.6.0", dev="0.6.0", main="0.6.0", tan_version_override="v0.5.1"
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _combinations(parsed) == [
        ("consumer-pin-dev+main", "v0.6.0", "alp-sdk-vscode@dev+main's pinned tan")
    ]


def test_the_consumer_override_fetches_neither_branch(tmp_path):
    proc, parsed = _run_resolve(
        tmp_path, latest_tan="v0.6.0", dev=None, main=None, override_version="v0.5.1"
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert parsed["skip"] == "false"
    assert _combinations(parsed) == [("consumer-pin-override", "v0.5.1", "overridden consumer pin")]


@pytest.mark.parametrize("missing", ["dev", "main"])
def test_a_fetch_failure_on_either_branch_is_fatal(tmp_path, missing: str):
    kwargs = {"dev": "0.6.0", "main": "0.6.0"}
    kwargs[missing] = None
    proc, parsed = _run_resolve(tmp_path, latest_tan="v0.6.0", **kwargs)
    assert proc.returncode != 0, (
        f"a 404 on alp-sdk-vscode@{missing} exited 0 -- a silently-skipped branch is "
        "the tan-cli#1050 defect, not a degraded-but-acceptable run"
    )
    assert f"::error::could not fetch src/alpCli/service.ts from alp-sdk-vscode@{missing}" in proc.stdout
    assert "consumer_matrix" not in parsed


def test_the_dedup_baseline_is_tan_clis_own_latest_release(tmp_path):
    """PR #1088 review, hole 1 -- pin WHICH release the dedup compares against.

    The baseline must be tan-cli's own latest release, because that is what
    `install.sh` will install on the `latest` leg. Repointed at alp-sdk's
    latest release tag instead, every comparison would be pin-vs-an-alp-sdk-tag
    -- never equal -- so `skip=true` becomes unreachable and every scheduled
    run grows six extra ~60-minute legs, forever. That is tan-cli#1050 inverted
    (a leg that always runs rather than never), and an argv-blind stub is green
    on it.
    """
    proc, parsed = _run_resolve(tmp_path, latest_tan="v0.6.0", dev="0.6.0", main="0.6.0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _calls(tmp_path, "gh") == [f"gh {_EXPECTED_GH_ARGV}"], (
        "the dedup baseline is no longer read from tan-cli's own latest release"
    )
    assert parsed["skip"] == "true"


def test_a_partial_pin_set_is_refused_even_when_the_other_branch_diverged(tmp_path):
    """The fatal verdict must hold in the case where continuing is TEMPTING.

    `dev` resolves cleanly to a version that genuinely differs from `latest`,
    so there is a real leg that could be run -- and `main`, the branch carrying
    the SHIPPED pin, is unreachable. Running dev's leg alone here is precisely
    #767's behaviour and precisely tan-cli#1050's defect: a green-looking run
    that never tested what users have installed. Refuse the whole job instead.
    """
    proc, parsed = _run_resolve(tmp_path, latest_tan="v0.7.0", dev="0.6.0", main=None)
    assert proc.returncode != 0
    assert "Refusing to continue with a partial pin set" in proc.stdout
    assert parsed.get("consumer_matrix") is None and parsed.get("skip") is None, (
        f"a partial resolution still published outputs ({parsed}) -- build-matrix "
        "would have fanned out a set that silently omits main's shipped pin"
    )


def test_both_fetches_carry_the_retry_flags_and_a_404_is_still_fatal(tmp_path):
    """PR #1088 review, the retry judgement call.

    Fatal-on-failure is right for a 404 or a renamed constant (real signal),
    but this verdict fails the WHOLE job -- including tan-cli#639's `latest`
    axis, which has nothing to do with the consumer pin -- and #1050 doubled
    the number of fetches that can trip it. `--retry 3 --retry-connrefused`
    splits those cases the way curl already splits them: transient failures
    (connection errors, 408/429/5xx) are retried, a 404 is not.

    NAMED FOR WHAT IT CHECKS. This test asserts the flags reach both fetches
    and that the 404 path stays fatal; it does NOT serve a transient, because
    retry-on-5xx-but-not-on-404 is curl's contract, not this workflow's, and
    stubbing it here would only re-assert the stub. That contract was measured
    directly during the #1088 review against a request-counting local server,
    on curl 8.5.0 -- the ubuntu-24.04 runner's version -- with these exact
    flags:

        HTTP 404: exit=22  attempts=1  elapsed=0.0s   <- NOT retried
        HTTP 503: exit=22  attempts=4  elapsed=3.0s
        HTTP 429: exit=22  attempts=4  elapsed=3.0s
        HTTP 408: exit=22  attempts=4  elapsed=3.0s
        HTTP 200: exit= 0  attempts=1  elapsed=0.0s

    An earlier name promised the transient behaviour this body does not
    exercise; renamed rather than left overclaiming.
    """
    proc, _ = _run_resolve(tmp_path, latest_tan="v0.6.0", dev="0.6.0", main="0.6.0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    curl_calls = _calls(tmp_path, "curl")
    assert len(curl_calls) == 2, f"expected one fetch per branch, got {curl_calls}"
    for call in curl_calls:
        assert "--retry 3" in call and "--retry-connrefused" in call, (
            f"a fetch whose failure reds the whole daily run does not retry: {call}"
        )
    # ...and the 404 path is unaffected: still exactly one attempt from the
    # step's point of view, still fatal.
    proc, _ = _run_resolve(tmp_path, latest_tan="v0.6.0", dev="0.6.0", main=None)
    assert proc.returncode != 0
    assert len(_calls(tmp_path, "curl")) == 2


@pytest.mark.parametrize("renamed", ["dev", "main"])
def test_a_renamed_constant_on_either_branch_is_fatal(tmp_path, renamed: str):
    kwargs: dict[str, object] = {"dev": "0.6.0", "main": "0.6.0"}
    kwargs[f"{renamed}_raw"] = 'export const CLI_PIN = "0.6.0";\n'
    proc, _ = _run_resolve(tmp_path, latest_tan="v0.6.0", **kwargs)  # type: ignore[arg-type]
    assert proc.returncode != 0
    assert f"on its {renamed} branch" in proc.stdout
    assert "Refusing to fall back to 'latest' silently" in proc.stdout


def test_two_matching_constants_are_fatal_rather_than_a_multiline_output(tmp_path):
    """A second `SUPPORTED_CLI_VERSION = "..."`-shaped line (a comment quoting
    the pin verbatim) would otherwise write a multi-line value to
    `$GITHUB_OUTPUT` and blow the runner's file command up with "Invalid
    format" instead of a deliberate `::error::`."""
    two = 'export const SUPPORTED_CLI_VERSION = "0.6.0";\n// SUPPORTED_CLI_VERSION = "0.5.1"\n'
    proc, _ = _run_resolve(tmp_path, latest_tan="v0.6.0", dev="0.6.0", main_raw=two)
    assert proc.returncode != 0
    assert "resolved 2 candidate SUPPORTED_CLI_VERSION values" in proc.stdout
    assert "on its main branch" in proc.stdout


# --------------------------------------------------------------------------
# 3. DYNAMIC: build-matrix fans the surviving set across the SKUs
# --------------------------------------------------------------------------


def _run_build_matrix(
    tmp_path: pathlib.Path, *, consumer_matrix: list[dict], skip: bool, tan_override: str = ""
) -> tuple[subprocess.CompletedProcess[str], dict]:
    gh_output = tmp_path / "github_output"
    gh_output.touch()
    script = tmp_path / "matrix.sh"
    script.write_text(_only_step_run(_MATRIX_JOB), encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GITHUB_OUTPUT": str(gh_output),
            "ALP_SDK_REF": "v0.16.0",
            "TAN_VERSION_OVERRIDE": tan_override,
            "CONSUMER_MATRIX": json.dumps(consumer_matrix),
            "CONSUMER_SKIP": "true" if skip else "false",
        },
    )
    text = gh_output.read_text(encoding="utf-8")
    matrix = {}
    for line in text.splitlines():
        if line.startswith("matrix="):
            matrix = json.loads(line[len("matrix=") :])
    return proc, matrix


def test_no_survivors_leaves_only_the_latest_combination(tmp_path):
    proc, matrix = _run_build_matrix(tmp_path, consumer_matrix=[], skip=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [e["sku"] for e in matrix["include"]] == list(_SKUS)
    assert {e["combination"] for e in matrix["include"]} == {"latest"}
    assert {e["combination_label"] for e in matrix["include"]} == {"released tan"}


@pytest.mark.parametrize("n", [1, 2])
def test_each_surviving_version_gets_its_own_full_sku_sweep(tmp_path, n: int):
    entries = [
        {
            "combination": "consumer-pin-dev",
            "tan_version": "v0.6.0",
            "label": "alp-sdk-vscode@dev's pinned tan",
            "branches": "dev",
        },
        {
            "combination": "consumer-pin-main",
            "tan_version": "v0.5.1",
            "label": "alp-sdk-vscode@main's pinned tan",
            "branches": "main",
        },
    ][:n]
    proc, matrix = _run_build_matrix(tmp_path, consumer_matrix=entries, skip=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    include = matrix["include"]
    assert len(include) == len(_SKUS) * (1 + n)
    combos = [e["combination"] for e in include]
    assert combos.count("latest") == len(_SKUS)
    for entry in entries:
        legs = [e for e in include if e["combination"] == entry["combination"]]
        assert [e["sku"] for e in legs] == list(_SKUS)
        assert {e["tan_version"] for e in legs} == {entry["tan_version"]}
        assert {e["alp_sdk_ref"] for e in legs} == {"v0.16.0"}
        # PR #1088 review, hole 2: the resolved label must survive the fan-out
        # UNCHANGED. `build-matrix` copies it into `combination_label`, which
        # is the only part of `journey`'s `name:` that distinguishes one
        # combination from another.
        assert {e["combination_label"] for e in legs} == {entry["label"]}, (
            f"{entry['combination']}'s legs lost or rewrote their label -- "
            "`journey`'s job name is built from `matrix.combination_label`, so "
            "this is what a human reads in the Actions tab"
        )
    # `matrix.combination` is the upload-artifact name suffix; a duplicate
    # would collide two legs' logs into one artifact.
    assert len(set(combos)) == 1 + n
    # ...and a duplicate LABEL is the same defect one layer up: two legs
    # rendering an identical `journey` job name. Distinct per combination,
    # constant within one.
    labels = {e["combination"]: e["combination_label"] for e in include}
    assert len(set(labels.values())) == len(labels), (
        f"two combinations share a `combination_label` ({labels}) -- their "
        "`journey` job names are indistinguishable in the Actions tab, and the "
        "six-case dedup table this module pins is keyed on that string"
    )


def test_build_matrix_refuses_a_skip_that_disagrees_with_the_resolved_set(tmp_path):
    """`skip` and `consumer_matrix` come from the same step; if they ever
    disagree, one branch's pin was dropped between the two jobs and the run
    would quietly test fewer versions than it resolved."""
    entries = [
        {
            "combination": "consumer-pin-main",
            "tan_version": "v0.5.1",
            "label": "alp-sdk-vscode@main's pinned tan",
            "branches": "main",
        }
    ]
    proc, _ = _run_build_matrix(tmp_path, consumer_matrix=entries, skip=True)
    assert proc.returncode != 0
    assert "resolve-consumer-pin disagreed with itself" in proc.stdout + proc.stderr
