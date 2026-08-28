# SPDX-License-Identifier: Apache-2.0
"""`tan support-bundle` -- port of `crates/tan-cli/src/commands/support_bundle.rs`.

Envelope/exit-code shapes below were measured against a freshly-built oracle
(`cargo build -p alp-tan-cli --bin tan` from THIS worktree's `crates/`).

**The bundled doctor section is the oracle's DEBUG-focused report**
(tan-cli#357): `workspaceRoot`/`sdkRoot`/`boardYaml`, the per-target-kind
extension + tool pair, then the host checks. Before #357 this module
substituted `doctor_cmd._collect`'s whole build/flash-readiness checklist and
declared that a deliberate divergence; a bundle attached to a debug failure
therefore carried no debugger state at all. The five host checks are still
`doctor_cmd`'s own -- harvested by name from `doctor_cmd.host_environment_checks`
(tan-cli#441; formerly `_collect`, which paid for the whole discarded
checklist to yield the same five) -- so they are monkeypatched here with a
deterministic list, keeping these tests independent of this host's
Zephyr/tool state.

**Exit follows the oracle's rule: `summary.fail > 0` -> `DOCTOR_FAILURE` (4),
`ok: false`.** A warn does NOT flip it, and the bundle file is written on the
failing path either way. The live cross-check against the real oracle lives in
`tests/parity/test_support_bundle_oracle_parity.py`; these cases pin the same
rule without needing a built Rust binary.

`support-bundle` IS registered in `tan.cli.app` (`tan/cli.py:34,105` -- and
`tests/parity/test_support_bundle_oracle_parity.py` depends on that
registration to spawn it). tan-cli#374 finding 8: an earlier version of this
note claimed otherwise. These tests still build a throwaway local Typer app
around the ported command function directly, the same lighter-weight harness
`test_trace_command.py`/`test_inspect_command.py` use for their own
already-registered commands -- not a workaround for missing registration.
"""
from __future__ import annotations

import json
import os

import pytest
import typer
from typer.testing import CliRunner

from tan.commands import doctor_cmd
from tan.commands.support_bundle_cmd import (
    REDACTION_SKIPPED_CODE,
    _home_variants,
    _redact,
    home_redaction_refusal,
    support_bundle,
)


def _local_app():
    local = typer.Typer()

    @local.callback(invoke_without_command=True)
    def root(ctx: typer.Context, output_format: str = typer.Option(None, "--format")) -> None:
        ctx.obj = {"format": output_format}

    local.command("support-bundle")(support_bundle)
    return local


app = _local_app()
runner = CliRunner()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def sdk_at(root):
    write(root / "scripts" / "alp_project.py", "# stub")


def _clean_checks(*, fail=False, warn=False):
    """A deterministic `doctor_cmd.host_environment_checks` return, standing
    in for whatever this real host's tools/Zephyr workspace happen to report
    -- keeps the end-to-end tests below independent of CI/dev-machine state.

    The `sdk` entry is deliberately one the bundle must DROP: only the five
    names in `_HOST_CHECK_ORDER` are harvested, and `sdk` is the build
    checklist's own SDK verdict (`host_environment_checks` itself never
    returns one -- this stub adds it purely to prove
    `_host_checks_from_doctor`'s own by-name filter still holds), superseded
    here by the debug report's `sdkRoot`."""
    status = "fail" if fail else ("warn" if warn else "pass")
    checks = [doctor_cmd.Check("sdk", "pass", "alp-sdk at /sdk", scope="project")]
    if fail or warn:
        checks.append(
            doctor_cmd.Check(
                "hostPrerequisites",
                status,
                "missing from PATH: ninja." if fail else "west is old.",
                fix="Install the missing prerequisites, then run `tan bootstrap`.",
                scope="host",
            )
        )
    else:
        checks.append(doctor_cmd.Check("hostPrerequisites", "pass", "git, cmake present", scope="host"))
    return checks


# ---------------------------------------------------------------------------
# Redaction -- the critical property this command has to get right.
# ---------------------------------------------------------------------------


def test_redact_replaces_every_occurrence_recursively():
    payload = {
        "a": "prefix C:\\Users\\alice\\proj suffix",
        "b": ["C:\\Users\\alice\\one", "unrelated"],
        "c": {"d": "C:/Users/alice/posix/path"},
        "e": True,
        "f": None,
        "g": 3,
    }
    redacted = _redact(payload, ("C:\\Users\\alice", "C:/Users/alice"))
    assert redacted["a"] == "prefix <home>\\proj suffix"
    assert redacted["b"] == ["<home>\\one", "unrelated"]
    assert redacted["c"]["d"] == "<home>/posix/path"
    # Non-strings pass through unchanged, not stringified.
    assert redacted["e"] is True
    assert redacted["f"] is None
    assert redacted["g"] == 3


def test_redact_is_a_noop_with_no_home_variants():
    payload = {"a": "C:\\Users\\alice\\proj"}
    assert _redact(payload, ()) == payload


def test_home_variants_covers_native_and_posix_spelling(monkeypatch):
    env_key = "USERPROFILE" if os.name == "nt" else "HOME"
    monkeypatch.setenv(env_key, "C:\\Users\\alice" if os.name == "nt" else "/home/alice")
    variants = _home_variants()
    assert len(variants) >= 1
    if os.name == "nt":
        assert "C:\\Users\\alice" in variants
        assert "C:/Users/alice" in variants


def test_home_variants_empty_when_unset(monkeypatch):
    env_key = "USERPROFILE" if os.name == "nt" else "HOME"
    monkeypatch.delenv(env_key, raising=False)
    assert _home_variants() == ()


def test_written_bundle_never_contains_the_raw_home_directory(tmp_path, monkeypatch):
    """The end-to-end property: a project living UNDER the resolved home
    directory must not leak that home path anywhere in the WRITTEN file --
    only the stdout envelope (never attached wholesale to a public issue the
    way the file is) may still carry it."""
    env_key = "USERPROFILE" if os.name == "nt" else "HOME"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(env_key, str(home))
    project = home / "proj"
    write(project / "board.yaml", "x")
    monkeypatch.chdir(project)
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks())

    result = runner.invoke(app, ["support-bundle", "--format", "json"])
    doc = json.loads(result.stdout)
    output_path = doc["data"]["outputPath"]
    # The stdout envelope is NOT redacted -- it must stay a real, followable
    # path for whatever just asked for it.
    assert str(home) in output_path or str(home).replace("\\", "/") in output_path

    bundle_text = open(output_path, encoding="utf-8").read()
    home_native = str(home)
    home_posix = home_native.replace("\\", "/")
    assert home_native not in bundle_text
    assert home_posix not in bundle_text
    assert "<home>" in bundle_text
    # The project's own sub-path under home survives, minus the home prefix.
    assert "proj" in bundle_text


def test_a_workspace_outside_home_is_left_legible_in_the_bundle(tmp_path, monkeypatch):
    """Redaction is narrow: a project OUTSIDE the home directory is not
    touched at all -- a maintainer reading the file needs the real layout."""
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks())

    result = runner.invoke(app, ["support-bundle", "--format", "json"])
    doc = json.loads(result.stdout)
    bundle_text = open(doc["data"]["outputPath"], encoding="utf-8").read()
    posix_root = str(tmp_path).replace("\\", "/")
    assert posix_root in bundle_text


# ---------------------------------------------------------------------------
# Target/server validation
# ---------------------------------------------------------------------------


def test_verbose_hint_never_appears_on_a_failure_path(tmp_path, monkeypatch):
    """Measured against the oracle: `--verbose` together with a server-
    incompatible refusal prints only the one incompatibility line -- the
    "include --format json" hint is exclusive to the bundle-written success
    text, not a blanket verbose flag."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "support-bundle",
            "--target-kind",
            "yocto-userspace",
            "--server",
            "jlink",
            "--verbose",
        ],
    )
    assert result.exit_code == 4
    assert "include --format json" not in result.stderr
    assert "not supported for target" in result.stderr


def test_server_incompatible_with_target_is_doctor_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "support-bundle",
            "--target-kind",
            "yocto-userspace",
            "--server",
            "jlink",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 4
    doc = json.loads(result.stdout)
    assert doc["data"]["outputPath"] == ""
    assert doc["issues"] == [
        {
            "code": "support-bundle.server-compatibility",
            "severity": "error",
            "message": "Server 'jlink' is not supported for target 'yocto-userspace'.",
        }
    ]
    # No file written on this path.
    assert not (tmp_path / ".alp-support").exists()


def test_invalid_target_kind_is_an_internal_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["support-bundle", "--target-kind", "bogus", "--format", "json"]
    )
    assert result.exit_code == 5
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["code"] == "support-bundle.internal-failure"
    assert "bogus" in doc["issues"][0]["message"]
    # Measured against the oracle: the raw invalid value is never echoed back
    # into data.targetKind/data.server -- both report the defaults.
    assert doc["data"]["targetKind"] == "native-host"
    assert doc["data"]["server"] == "none"


def test_invalid_server_is_an_internal_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["support-bundle", "--server", "bogus", "--format", "json"])
    assert result.exit_code == 5
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["code"] == "support-bundle.internal-failure"
    # Measured against the oracle: --server bogus alone still reports the
    # DEFAULT server ("none"), not the raw invalid "bogus" value.
    assert doc["data"]["targetKind"] == "native-host"
    assert doc["data"]["server"] == "none"


def test_a_valid_target_kind_with_an_invalid_server_still_reports_defaults_for_both(
    tmp_path, monkeypatch
):
    """Measured against the oracle: `--target-kind zephyr-mcu --server bogus`
    -> rc=5 targetKind="native-host" server="none" -- a partial parse failure
    resets BOTH fields to their defaults, not just the one that failed."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "support-bundle",
            "--target-kind",
            "zephyr-mcu",
            "--server",
            "bogus",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 5
    doc = json.loads(result.stdout)
    assert doc["data"]["targetKind"] == "native-host"
    assert doc["data"]["server"] == "none"


# ---------------------------------------------------------------------------
# Trace section leniency -- checks Option-presence, never file existence
# ---------------------------------------------------------------------------


def test_trace_section_still_plans_all_four_targets_without_a_real_board_yaml(
    tmp_path, monkeypatch
):
    """Measured against the oracle: unlike bare `tan trace`, a resolved SDK
    with a MISSING board.yaml still gets four Planned decisions here."""
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks())

    result = runner.invoke(app, ["support-bundle", "--sdk-root", str(sdk), "--format", "json"])
    doc = json.loads(result.stdout)
    assert doc["data"]["decisionCount"] == 4


def test_trace_section_falls_back_to_one_failed_decision_with_no_sdk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks(fail=True))

    result = runner.invoke(app, ["support-bundle", "--format", "json"])
    doc = json.loads(result.stdout)
    assert doc["data"]["decisionCount"] == 1


def test_path_focus_adds_one_more_decision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks())

    result = runner.invoke(
        app, ["support-bundle", "--sdk-root", str(sdk), "--path", "som.sku", "--format", "json"]
    )
    doc = json.loads(result.stdout)
    assert doc["data"]["decisionCount"] == 5


def test_unknown_generation_target_is_an_internal_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    result = runner.invoke(
        app, ["support-bundle", "--sdk-root", str(sdk), "--target", "bogus", "--format", "json"]
    )
    assert result.exit_code == 5
    doc = json.loads(result.stdout)
    assert doc["issues"][0]["code"] == "support-bundle.internal-failure"
    assert "bogus" in doc["issues"][0]["message"]


# ---------------------------------------------------------------------------
# Doctor -> issues / exit code wiring
# ---------------------------------------------------------------------------


def _healthy(tmp_path, monkeypatch, **kwargs):
    """A project whose DEBUG report's own three base checks all pass: a
    board.yaml on disk and a resolvable SDK. Without the SDK, `sdkRoot` fails
    and the exit code is 4 for a reason that has nothing to do with the
    harvested host checks under test."""
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks(**kwargs))
    return ["support-bundle", "--sdk-root", str(sdk), "--format", "json"]


def test_clean_doctor_checks_mean_success_and_no_issues(tmp_path, monkeypatch):
    argv = _healthy(tmp_path, monkeypatch)
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["ok"] is True
    assert doc["issues"] == []


def test_a_failing_check_is_a_doctor_failure_not_a_silent_success(tmp_path, monkeypatch):
    """tan-cli#357, the regression this file exists to hold. The oracle's rule
    is `if doctor.summary.fail > 0 { ExitCode::DoctorFailure }`; this port
    hardcoded `SUCCESS`, so automation read `ok: true` and exit 0 out of the
    same envelope that carried an error-severity issue. Measured against the
    oracle on the identical failing host: rc=4, ok=false, exitCode=4.

    The three assertions are one invariant, not three: process exit code ==
    `envelope.exitCode` == `not ok`. Fixing any one of them alone leaves a
    consumer trusting whichever it happens to read."""
    argv = _healthy(tmp_path, monkeypatch, fail=True)
    result = runner.invoke(app, argv)
    assert result.exit_code == 4
    doc = json.loads(result.stdout)
    assert doc["exitCode"] == 4
    assert doc["ok"] is False
    issue = next(i for i in doc["issues"] if i["code"] == "support-bundle.host-prerequisites")
    assert issue["severity"] == "error"
    assert issue["message"] == "missing from PATH: ninja."
    # The bundle file is still written on a doctor failure -- it is precisely
    # what the user attaches, and the failure is why they are attaching it.
    assert doc["data"]["outputPath"] != ""
    assert os.path.isfile(doc["data"]["outputPath"])


def test_a_warning_check_becomes_a_warning_issue_and_stays_exit_zero(tmp_path, monkeypatch):
    """`summary.fail > 0`, never `warn > 0`: the oracle counts warns and
    ignores them for the exit code, so a bundle from a merely-degraded host
    still exports cleanly."""
    argv = _healthy(tmp_path, monkeypatch, warn=True)
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["ok"] is True
    issue = next(i for i in doc["issues"] if i["code"] == "support-bundle.host-prerequisites")
    assert issue["severity"] == "warning"


def test_an_unresolved_sdk_root_fails_the_bundle(tmp_path, monkeypatch):
    """The oracle's `sdkRoot` check is `status_pass_fail(has_sdk)` -- a hard
    fail, so a bundle taken with no alp-sdk checkout exits 4. Measured against
    the oracle from a project with no resolvable SDK: rc=4, issues include
    `support-bundle.sdk-root` at error severity."""
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks())

    result = runner.invoke(app, ["support-bundle", "--format", "json"])
    assert result.exit_code == 4
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    issue = next(i for i in doc["issues"] if i["code"] == "support-bundle.sdk-root")
    assert issue["severity"] == "error"
    assert issue["message"] == "No alp-sdk checkout resolved."


# ---------------------------------------------------------------------------
# The DEBUG-focused doctor report (tan-cli#357)
# ---------------------------------------------------------------------------


def _bundle(result):
    return json.loads(open(json.loads(result.stdout)["data"]["outputPath"], encoding="utf-8").read())


def test_the_bundle_carries_the_debug_report_not_the_build_checklist(tmp_path, monkeypatch):
    """The other half of #357: the check LIST. `_collect`'s build/flash-
    readiness names (`sdk`, `setools`, `jlink`, `west`, ...) are not what a
    debug bundle is for, and substituting them dropped `codeLLDBExtension`/
    `lldb` -- the debugger facts this command exists to collect."""
    argv = _healthy(tmp_path, monkeypatch)
    names = [c["name"] for c in _bundle(runner.invoke(app, argv))["doctor"]["checks"]]
    assert names == [
        "workspaceRoot",
        "sdkRoot",
        "boardYaml",
        "codeLLDBExtension",
        "lldb",
        "hostPrerequisites",
    ]
    # `sdk` is in the monkeypatched `host_environment_checks` list and must
    # NOT ride along -- only the five harvested host names do.
    assert "sdk" not in names


def test_the_real_host_environment_checks_produce_the_oracle_shaped_check_list(
    tmp_path, monkeypatch
):
    """tan-cli#374 finding 7: every OTHER test in this module monkeypatches
    `doctor_cmd.host_environment_checks` with the 2-check `_clean_checks()`
    stub, so none of them would notice a check entering or leaving the
    bundle -- exactly how findings 1 (`longPaths`'s undue `fail` arm) and 5
    (the undeclared `bootstrapManifest` divergence) reached production with
    no unit test failing.

    This one runs the REAL `host_environment_checks`, against a resolvable
    SDK carrying a real, readable `metadata/bootstrap.json` (so
    `bootstrapManifest` -- a documented, separately-tracked port-only
    divergence, finding 5 -- does not fire and inflate the count), and pins
    the check-NAME list only: the oracle's own shape (`longPaths` is
    Windows-only, hence the platform branch), regardless of what THIS host's
    real tools/registry answer each one with -- unlike `_healthy`'s
    deterministic stub, a real status here would be host-dependent and not
    this test's job to pin.

    tan-cli#441: before this fix, the seam this test measures WAS
    `doctor_cmd._collect` -- the exact same expected NAME list below was
    already the assertion (renamed only, never re-derived), which is itself
    the equivalence proof the fix owes: the harvested five read identically
    whether they were picked out of `_collect`'s much longer list or built
    directly by `host_environment_checks`.
    """
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    write(
        sdk / "metadata" / "bootstrap.json",
        json.dumps(
            {
                "schemaVersion": 1,
                "prerequisites": {
                    "posix": [],
                    "windows": [],
                    "pythonMinVersion": "3.10",
                    "install": {},
                },
            }
        ),
    )

    result = runner.invoke(app, ["support-bundle", "--sdk-root", str(sdk), "--format", "json"])
    names = [c["name"] for c in _bundle(result)["doctor"]["checks"]]
    expected = [
        "workspaceRoot",
        "sdkRoot",
        "boardYaml",
        "codeLLDBExtension",
        "lldb",
        "hostPrerequisites",
        "zephyrSdkAvailableForHost",
    ]
    if os.name == "nt":
        expected.append("longPaths")
    expected.append("homePath")
    assert names == expected
    assert "bootstrapManifest" not in names


#: Every check-builder `_collect` calls that `host_environment_checks` must
#: NOT -- `_collect`'s own build/flash-readiness preflight
#: (`sdk_check`..`zephyr_workspace_check`), the discarded host-adjacent
#: checks (`west_check`, `zephyr_sdk_check`, `seven_zip_check`), and the
#: three probes tan-cli#441's own issue names by name (`setools_check`,
#: `jlink_check`/`jlink_banner`/`jlink_flash_device` for J-Link, plus
#: `sdk_provenance_check`'s git shell-out). `west_resolved_check` is listed
#: separately from `west_check` -- `_collect` builds both from two
#: independent `west --version` spawns.
_DISCARDED_CHECKLIST_BUILDERS = (
    "sdk_check",
    "board_yaml_preflight_check",
    "libraries_check",
    "workspace_preflight_check",
    "west_resolved_check",
    "venv_provenance_check",
    "zephyr_version_preflight_check",
    "zephyr_workspace_check",
    "west_check",
    "zephyr_sdk_check",
    "seven_zip_check",
    "setools_check",
    "jlink_check",
    "jlink_banner",
    "jlink_flash_device",
    "sdk_provenance_check",
)


def test_host_environment_checks_never_calls_the_discarded_checklist_builders(
    tmp_path, monkeypatch
):
    """tan-cli#441 acceptance criterion: prove `support-bundle` (via
    `doctor_cmd.host_environment_checks`) does not invoke `west`, J-Link,
    SETOOLS, or any of `_collect`'s other build/flash-readiness probes --
    the ones a bundle's own doctor section never reported even before this
    fix, but used to pay for anyway.

    Poisons every check-builder `_collect` calls that is NOT one of the five
    `host_environment_checks` keeps, with a spy that fails the test the
    instant ANY of them runs, then ALSO wraps `doctor_cmd.probe`/
    `probe_status` themselves so a raw west/JLink/SETOOLS spawn made without
    going through any of those builders (`_collect`'s own `west --version`
    probes are exactly that shape) is caught too -- builder-granular alone is
    not spawn-granular (tan-cli#980 review). Then calls the REAL seam against
    a real (if minimal) SDK checkout. `_collect` itself (`tan doctor`'s own
    full checklist -- see `test_doctor_command.py`) keeps calling every one
    of these; only `host_environment_checks` must not.
    """
    for name in _DISCARDED_CHECKLIST_BUILDERS:
        monkeypatch.setattr(
            doctor_cmd,
            name,
            lambda *a, _name=name, **k: pytest.fail(
                f"host_environment_checks must not call {_name}()"
            ),
        )

    # tan-cli#980 review finding 2: the builder-name poison above is
    # builder-GRANULAR -- it catches a re-added *check* (a call to one of the
    # sixteen names above), not a re-added *spawn*. `_collect` itself proves
    # the gap exists: its `west --version` probes (`doctor_cmd.py`, the
    # `probe_status([west_resolved_exe, "--version"])` / `probe([west_exe,
    # "--version"])` calls) are raw spawns made directly in `_collect`'s own
    # body, not routed through `west_check`/`west_resolved_check` (those two
    # are pure formatters over an already-probed version) -- so a
    # `host_environment_checks` that grew the identical raw call would sail
    # straight past every poison above. Wrap, rather than replace outright,
    # `doctor_cmd.probe`/`probe_status`: a west/JLink/SETOOLS-shaped argv
    # fails the test the instant it is attempted; anything else -- the macOS
    # `sysctl` Rosetta probe `zephyrSdkAvailableForHost` needs, the
    # python-floor probe `hostPrerequisites` needs -- still runs for real, so
    # this stays a spawn guard and not a second `_clean_checks`-shaped stub
    # that would make the five real checks untestable here.
    _forbidden_argv_tokens = ("west", "jlink", "setools")
    _real_probe = doctor_cmd.probe
    _real_probe_status = doctor_cmd.probe_status

    def _argv_names_a_discarded_tool(argv) -> bool:
        return any(
            token in str(part).lower() for part in argv for token in _forbidden_argv_tokens
        )

    def _guarded_probe(argv, *a, **k):
        if _argv_names_a_discarded_tool(argv):
            pytest.fail(f"host_environment_checks must not spawn {argv!r}")
        return _real_probe(argv, *a, **k)

    def _guarded_probe_status(argv, *a, **k):
        if _argv_names_a_discarded_tool(argv):
            pytest.fail(f"host_environment_checks must not spawn {argv!r}")
        return _real_probe_status(argv, *a, **k)

    monkeypatch.setattr(doctor_cmd, "probe", _guarded_probe)
    monkeypatch.setattr(doctor_cmd, "probe_status", _guarded_probe_status)

    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    write(
        sdk / "metadata" / "bootstrap.json",
        json.dumps(
            {
                "schemaVersion": 1,
                "prerequisites": {
                    "posix": [],
                    "windows": [],
                    "pythonMinVersion": "3.10",
                    "install": {},
                },
            }
        ),
    )

    checks = doctor_cmd.host_environment_checks(str(sdk), workspace_root=str(tmp_path))
    names = {c.name for c in checks}
    assert "hostPrerequisites" in names
    assert "zephyrSdkAvailableForHost" in names
    assert "homePath" in names


def test_support_bundle_never_calls_the_whole_doctor_checklist(tmp_path, monkeypatch):
    """The regression this fix exists to prevent, pinned directly: if
    `_host_checks_from_doctor` (or anything else in this module) ever again
    reaches for `doctor_cmd._collect` -- reintroducing the whole discarded
    build/flash-readiness checklist tan-cli#441 removed -- this test fails
    the instant `support-bundle` runs, regardless of what `_collect` would
    have returned. A poisoned `_collect` that is genuinely never called is
    the "cannot quietly return" assertion tan-cli#441's own acceptance
    criteria ask for; `test_host_environment_checks_never_calls_the_
    discarded_checklist_builders` above covers the finer-grained "which
    individual probes" half of the same requirement.
    """
    monkeypatch.setattr(
        doctor_cmd,
        "_collect",
        lambda *a, **k: pytest.fail("support-bundle must not call doctor_cmd._collect"),
    )
    argv = _healthy(tmp_path, monkeypatch)
    result = runner.invoke(app, argv)
    assert result.exit_code == 0


def test_the_extension_checks_are_unknown_and_count_toward_nothing(tmp_path, monkeypatch):
    """#102: the standalone binary cannot enumerate VS Code's extensions, so
    `codeLLDBExtension` must not claim an install state nobody probed, must
    not join the pass total, and must raise no issue -- otherwise it could
    move an exit code off a question that was never asked."""
    argv = _healthy(tmp_path, monkeypatch)
    result = runner.invoke(app, argv)
    doctor = _bundle(result)["doctor"]
    check = next(c for c in doctor["checks"] if c["name"] == "codeLLDBExtension")
    assert check["status"] == "unknown"
    assert "is installed" not in check["detail"]
    assert "fix" not in check
    assert sum(doctor["summary"].values()) < len(doctor["checks"])
    assert not [i for i in json.loads(result.stdout)["issues"] if "Extension" in i["code"]]


def test_lldb_passes_even_with_none_on_path(tmp_path, monkeypatch):
    """#131: `vadimcn.vscode-lldb` ships its own LLDB and never reads PATH, so
    a bare-PATH miss must not warn or offer an install remedy that fixes
    nothing."""
    argv = _healthy(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor_cmd, "on_path", lambda name: None)
    check = next(
        c for c in _bundle(runner.invoke(app, argv))["doctor"]["checks"] if c["name"] == "lldb"
    )
    assert check["status"] == "pass"
    assert "fix" not in check
    assert "ships its own LLDB" in check["detail"]


def test_a_zephyr_target_swaps_in_the_cortex_and_backend_checks(tmp_path, monkeypatch):
    """`--target-kind`/`--server` genuinely change the report now -- before
    #357 the bundled checklist never branched on either."""
    argv = _healthy(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor_cmd, "on_path", lambda name: None)
    result = runner.invoke(
        app, [*argv, "--target-kind", "zephyr-mcu", "--server", "jlink"]
    )
    checks = {c["name"]: c for c in _bundle(result)["doctor"]["checks"]}
    assert "cortexDebugExtension" in checks
    assert checks["jlinkBackend"]["status"] == "warn"
    assert checks["jlinkBackend"]["detail"] == "No jlink executable was found on PATH."
    # A warn, so the bundle still exports cleanly.
    assert result.exit_code == 0


def test_a_yocto_target_swaps_in_the_cpptools_and_gdb_checks(tmp_path, monkeypatch):
    argv = _healthy(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor_cmd, "on_path", lambda name: name if name == "gdb" else None)
    # `gdbserver` explicitly: `none` is not a supported server for this target,
    # and the pairing guard would refuse before any report is built.
    result = runner.invoke(
        app, [*argv, "--target-kind", "yocto-userspace", "--server", "gdbserver"]
    )
    checks = {c["name"]: c for c in _bundle(result)["doctor"]["checks"]}
    assert "cppToolsExtension" in checks
    assert checks["gdb"]["status"] == "pass"
    assert checks["gdb"]["detail"] == "gdb"


def test_the_context_carries_project_selected_and_debugger_extensions(tmp_path, monkeypatch):
    """Both were omitted before #357 as "IDE-extension-host concepts with no
    standalone reader". `projectSelected` is derived from this invocation's
    own flags, and `debuggerExtensions` carries `observable: false`, which
    says in the file itself that nothing probed the three flags."""
    argv = _healthy(tmp_path, monkeypatch)
    context = _bundle(runner.invoke(app, argv))["inspect"]["context"]
    assert context["projectSelected"] is False
    assert context["debuggerExtensions"] == {
        "cortexDebug": True,
        "cppTools": True,
        "codeLLDB": True,
        "observable": False,
    }

    selected = _bundle(runner.invoke(app, [*argv, "--project", str(tmp_path)]))
    assert selected["inspect"]["context"]["projectSelected"] is True


def test_a_missing_board_yaml_warns_until_a_project_is_selected(tmp_path, monkeypatch):
    """#100: `tan bootstrap` sends every new customer to run tan from the SDK
    checkout root, which has no board.yaml and needs none -- so a missing one
    is a hard failure only once `--project`/`--board-yaml` NAMED a project.
    The exit code follows: warn keeps the bundle at 0, fail takes it to 4."""
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks())
    argv = ["support-bundle", "--sdk-root", str(sdk), "--format", "json"]

    unselected = runner.invoke(app, argv)
    check = next(
        c for c in _bundle(unselected)["doctor"]["checks"] if c["name"] == "boardYaml"
    )
    assert check["status"] == "warn"
    assert "no project selected" in check["detail"]
    assert unselected.exit_code == 0

    selected = runner.invoke(app, [*argv, "--project", str(tmp_path)])
    check = next(c for c in _bundle(selected)["doctor"]["checks"] if c["name"] == "boardYaml")
    assert check["status"] == "fail"
    assert selected.exit_code == 4


# ---------------------------------------------------------------------------
# --destination
# ---------------------------------------------------------------------------


def test_explicit_destination_is_used_verbatim(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    dest = tmp_path / "custom-dest"
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks())

    result = runner.invoke(
        app, ["support-bundle", "--destination", str(dest), "--format", "json"]
    )
    doc = json.loads(result.stdout)
    output_path = doc["data"]["outputPath"]
    assert os.path.dirname(output_path) == str(dest)
    assert dest.is_dir()


def test_default_destination_is_dot_alp_support_under_the_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks())

    result = runner.invoke(app, ["support-bundle", "--format", "json"])
    doc = json.loads(result.stdout)
    assert (tmp_path / ".alp-support").is_dir()
    assert os.path.basename(os.path.dirname(doc["data"]["outputPath"])) == ".alp-support"


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_verbose_text_mode_adds_the_json_hint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks())

    quiet = runner.invoke(app, ["support-bundle"])
    verbose = runner.invoke(app, ["support-bundle", "--verbose"])
    assert "include --format json" not in quiet.stderr
    assert "include --format json" in verbose.stderr


def test_a_bad_format_is_a_usage_error_not_a_traceback():
    result = runner.invoke(app, ["support-bundle", "--format", "yaml"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# tan-cli#499 defect 5 -- redaction must be path-anchored, and never silent
# ---------------------------------------------------------------------------


def test_redaction_only_replaces_a_whole_path_token():
    """tan-cli#499 defect 5, the prefix half. `_redact` did an unanchored
    `str.replace`, so any path the home merely PREFIXES was rewritten into a
    silently WRONG one. Both shapes below were reproduced end to end with
    `HOME=/home/<user>`: `/home/<user>-two/alp-sdk` was written as
    `<home>-two/alp-sdk`, and `/srv/home/<user>-ops/proj` as
    `/srv<home>-ops/proj` -- register-grade path data corrupted at `ok: true`,
    in the one artifact that exists to carry it verbatim.

    The third case is the one a trailing-boundary rule alone would still get
    wrong: `/srv/home/<user>/proj` ends on a separator but is a DIFFERENT
    directory, so a leading boundary is required too."""
    home = ("/home/<user>",)
    assert _redact("/home/<user>-two/alp-sdk", home) == "/home/<user>-two/alp-sdk"
    assert _redact("/srv/home/<user>-ops/proj", home) == "/srv/home/<user>-ops/proj"
    assert _redact("/srv/home/<user>/proj", home) == "/srv/home/<user>/proj"
    # What must STILL be redacted: the whole token, a prefix ending on a
    # separator, and either of those embedded in a command line.
    assert _redact("/home/<user>", home) == "<home>"
    assert _redact("/home/<user>/proj", home) == "<home>/proj"
    assert _redact("run /home/<user>/x --in /home/<user>", home) == "run <home>/x --in <home>"
    assert _redact('quoted "/home/<user>/x"', home) == 'quoted "<home>/x"'


def test_a_root_home_is_refused_rather_than_shredding_every_separator():
    """The `HOME=/` half. Docker/OpenShift hand a uid with no /etc/passwd entry
    `HOME=/`; `_home_variants()` then returned `('/',)` and every `/` in every
    string of the bundle became `<home>`. MEASURED: exit 0, no warning, and 88
    `<home>` substitutions in a single bundle. The shape
    `workspaceRoot: "<home>srv<home>ci<home>work<home>..."` is ILLUSTRATIVE --
    the run's real paths are elided, not transcribed. A root home has no
    account name to protect, so the answer is to refuse, not to escape it."""
    for root in ("/", "//", "C:\\", "C:/"):
        assert home_redaction_refusal(root) is not None
    assert home_redaction_refusal("/home/dev") is None
    assert home_redaction_refusal("") is None


def test_a_root_home_writes_an_unredacted_bundle_and_says_so(tmp_path, monkeypatch):
    """The end-to-end shape, in BOTH output modes. A refusal the user never
    sees is the same defect one layer down: they are about to attach this file
    believing the account name was scrubbed."""
    env_key = "USERPROFILE" if os.name == "nt" else "HOME"
    monkeypatch.setenv(env_key, "C:\\" if os.name == "nt" else "/")
    write(tmp_path / "board.yaml", "x")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks())

    doc = json.loads(runner.invoke(app, ["support-bundle", "--format", "json"]).stdout)
    warning = [i for i in doc["issues"] if i["code"] == REDACTION_SKIPPED_CODE]
    assert len(warning) == 1
    assert warning[0]["severity"] == "warning"
    assert "filesystem root" in warning[0]["message"]

    bundle_text = open(doc["data"]["outputPath"], encoding="utf-8").read()
    # Nothing shredded: the paths a maintainer needs are readable.
    assert "<home>" not in bundle_text
    assert str(tmp_path).replace("\\", "/") in bundle_text

    text_result = runner.invoke(app, ["support-bundle"])
    assert "filesystem root" in text_result.stderr


def test_an_ordinary_home_emits_no_redaction_warning(tmp_path, monkeypatch):
    """The warning must fire ONLY where redaction was actually skipped --
    otherwise it is noise on every healthy run and stops being read."""
    env_key = "USERPROFILE" if os.name == "nt" else "HOME"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(env_key, str(home))
    write(tmp_path / "board.yaml", "x")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor_cmd, "host_environment_checks", lambda *a, **k: _clean_checks())

    doc = json.loads(runner.invoke(app, ["support-bundle", "--format", "json"]).stdout)
    assert [i for i in doc["issues"] if i["code"] == REDACTION_SKIPPED_CODE] == []


def test_a_drive_anchored_home_is_redacted_behind_a_separator():
    """Review round on #620. `_TOKEN_BREAK` excludes `/` and `\\` from the
    LEADING boundary -- correct for the POSIX case (`/srv/home/dev` is a
    different directory), but `%USERPROFILE%` starts with a DRIVE LETTER, so
    Windows' extended-length prefix put a separator in front of it and the
    account name survived. Measured before the fix:
    `\\\\?\\C:\\Users\\alice\\proj` came back unredacted -- on the one platform
    whose home directory IS the account name, which is the whole thing this
    redaction exists to remove.

    A drive letter re-anchors the path absolutely, so accepting a separator
    before it cannot admit the POSIX confusion (asserted directly below)."""
    win = ("C:\\Users\\runner", "C:/Users/runner")
    assert _redact(r"\\?\C:\Users\runner\proj", win) == r"\\?\<home>\proj"
    assert _redact("//?/C:/Users/runner/proj", win) == "//?/<home>/proj"
    assert _redact(r"cmd \\?\C:\Users\runner\build\zephyr.elf", win) == (
        r"cmd \\?\<home>\build\zephyr.elf"
    )
    # Unchanged shapes: no prefix at all, and the bare token.
    assert _redact(r"C:\Users\runner\proj", win) == r"<home>\proj"
    assert _redact("C:/Users/runner", win) == "<home>"


def test_the_drive_anchored_relaxation_does_not_over_redact():
    """The relaxation must stay boundary-respecting on BOTH sides: a longer
    account name that merely starts with the home's, and the same user on a
    different drive, are different directories."""
    # `runner` / `runner~1` is the real Windows 8.3-alias pair, and the longer
    # name genuinely starts with the shorter -- exactly the confusion the
    # trailing boundary has to refuse.
    win = ("C:\\Users\\runner", "C:/Users/runner")
    assert _redact(r"C:\Users\runner~1\proj", win) == r"C:\Users\runner~1\proj"
    assert _redact(r"D:\Users\runner\proj", win) == r"D:\Users\runner\proj"
    # And the POSIX leading-separator exclusion is untouched. These two are the
    # shapes that rule actually DECIDES -- a home directly preceded by a
    # separator. `/srv/home/<user>/proj` is NOT one of them: it survives on the
    # `v` before the match whether separators lead or not (measured), so
    # asserting it here would not hold this relaxation in place at all.
    posix = ("/home/<user>",)
    assert _redact("/srv//home/<user>/proj", posix) == "/srv//home/<user>/proj"
    assert _redact("file:///home/<user>/proj", posix) == "file:///home/<user>/proj"
