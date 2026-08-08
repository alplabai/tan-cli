# SPDX-License-Identifier: Apache-2.0
"""`tan support-bundle` -- port of `crates/tan-cli/src/commands/support_bundle.rs`.

Envelope/exit-code shapes below were measured against a freshly-built oracle
(`cargo build -p alp-tan-cli --bin tan` from THIS worktree's `crates/`).

**The bundled doctor section is the oracle's DEBUG-focused report**
(tan-cli#357): `workspaceRoot`/`sdkRoot`/`boardYaml`, the per-target-kind
extension + tool pair, then the host checks. Before #357 this module
substituted `doctor_cmd._collect`'s whole build/flash-readiness checklist and
declared that a deliberate divergence; a bundle attached to a debug failure
therefore carried no debugger state at all. The four host checks are still
`doctor_cmd`'s own -- harvested by name from `_collect` -- so they are
monkeypatched here with a deterministic list, keeping these tests independent
of this host's Zephyr/tool state.

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

import typer
from typer.testing import CliRunner

from tan.commands import doctor_cmd
from tan.commands.support_bundle_cmd import _home_variants, _redact, support_bundle


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
    """A deterministic `doctor_cmd._collect` return, standing in for whatever
    this real host's tools/Zephyr workspace happen to report -- keeps the
    end-to-end tests below independent of CI/dev-machine state.

    The `sdk` entry is deliberately one the bundle must DROP: only the five
    names in `_HOST_CHECK_ORDER` are harvested, and `sdk` is the build
    checklist's own SDK verdict, superseded here by the debug report's
    `sdkRoot`."""
    status = "fail" if fail else ("warn" if warn else "pass")
    checks = [doctor_cmd.Check("sdk", "pass", "alp-sdk at /sdk")]
    if fail or warn:
        checks.append(
            doctor_cmd.Check(
                "hostPrerequisites",
                status,
                "missing from PATH: ninja." if fail else "west is old.",
                fix="Install the missing prerequisites, then run `tan bootstrap`.",
            )
        )
    else:
        checks.append(doctor_cmd.Check("hostPrerequisites", "pass", "git, cmake present"))
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
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks())

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
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks())

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
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks())

    result = runner.invoke(app, ["support-bundle", "--sdk-root", str(sdk), "--format", "json"])
    doc = json.loads(result.stdout)
    assert doc["data"]["decisionCount"] == 4


def test_trace_section_falls_back_to_one_failed_decision_with_no_sdk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks(fail=True))

    result = runner.invoke(app, ["support-bundle", "--format", "json"])
    doc = json.loads(result.stdout)
    assert doc["data"]["decisionCount"] == 1


def test_path_focus_adds_one_more_decision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks())

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
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks(**kwargs))
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
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks())

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
    # `sdk` is in the monkeypatched `_collect` list and must NOT ride along --
    # only the five harvested host names do.
    assert "sdk" not in names


def test_the_real_collect_produces_the_oracle_shaped_check_list(tmp_path, monkeypatch):
    """tan-cli#374 finding 7: every OTHER test in this module monkeypatches
    `doctor_cmd._collect` with the 2-check `_clean_checks()` stub, so none of
    them would notice a check entering or leaving the bundle -- exactly how
    findings 1 (`longPaths`'s undue `fail` arm) and 5 (the undeclared
    `bootstrapManifest` divergence) reached production with no unit test
    failing.

    This one runs the REAL `_collect`, against a resolvable SDK carrying a
    real, readable `metadata/bootstrap.json` (so `bootstrapManifest` -- a
    documented, separately-tracked port-only divergence, finding 5 -- does
    not fire and inflate the count), and pins the check-NAME list only: the
    oracle's own shape (`longPaths` is Windows-only, hence the platform
    branch), regardless of what THIS host's real tools/registry answer each
    one with -- unlike `_healthy`'s deterministic stub, a real status here
    would be host-dependent and not this test's job to pin.
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
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks())
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
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks())

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
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks())

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
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks())

    quiet = runner.invoke(app, ["support-bundle"])
    verbose = runner.invoke(app, ["support-bundle", "--verbose"])
    assert "include --format json" not in quiet.stderr
    assert "include --format json" in verbose.stderr


def test_a_bad_format_is_a_usage_error_not_a_traceback():
    result = runner.invoke(app, ["support-bundle", "--format", "yaml"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
