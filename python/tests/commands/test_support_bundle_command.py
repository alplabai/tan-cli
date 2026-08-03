# SPDX-License-Identifier: Apache-2.0
"""`tan support-bundle` -- port of `crates/tan-cli/src/commands/support_bundle.rs`.

Envelope/exit-code shapes below were measured against a freshly-built oracle
(`cargo build -p alp-tan-cli --bin tan` from THIS worktree's `crates/`). The
DOCTOR SECTION's check names/content are a deliberate, documented divergence
from the oracle (this port reuses `doctor_cmd`'s own build/flash-readiness
checks rather than re-implementing the oracle's separate debug-flavoured
doctor -- see `support_bundle_cmd`'s module docstring) -- tests here assert
`doctor_cmd`'s own checks are wired in correctly (via a monkeypatched,
deterministic check list, so these tests do not depend on this host's own
Zephyr/tool state), not that they match the oracle's check names.

**Exit code matches the oracle, decoupled from the reused doctor checklist.**
Measured on a normal project with a resolved SDK: oracle rc=0 issues=[]. A
bundle that WRITES successfully exits 0 regardless of what its embedded
doctor section found -- the doctor checks still surface as
`support-bundle.<name>` issues, they just no longer flip the exit code (see
`test_a_failing_check_becomes_a_support_bundle_coded_issue_but_stays_exit_zero`).
The one exception is the target/server incompatibility precondition, which is
this command's OWN failure, not the reused checklist's, and still exits
`DOCTOR_FAILURE` (4) (see `test_server_incompatible_with_target_is_doctor_failure`).

`support-bundle` is not yet registered in `tan.cli.app` (the orchestrator's to
wire), so these tests build a throwaway local Typer app around the ported
command function directly.
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
    """A deterministic doctor check list, standing in for whatever this real
    host's tools/Zephyr workspace happen to report -- keeps the end-to-end
    tests below independent of CI/dev-machine state."""
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


def test_clean_doctor_checks_mean_success_and_no_issues(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks())

    result = runner.invoke(app, ["support-bundle", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["ok"] is True
    assert doc["issues"] == []


def test_a_failing_check_becomes_a_support_bundle_coded_issue_but_stays_exit_zero(
    tmp_path, monkeypatch
):
    """Matches the oracle: the bundle EXPORT succeeded, so exit stays 0 even
    though the reused doctor checklist found a `fail`-status check -- the
    doctor section is DATA inside the bundle, not this command's verdict (see
    `support_bundle_cmd`'s module docstring). The failing check still surfaces
    as an error-severity issue for a human reading the envelope; only the
    exit code is decoupled from it."""
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks(fail=True))

    result = runner.invoke(app, ["support-bundle", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["ok"] is True
    issue = next(i for i in doc["issues"] if i["code"] == "support-bundle.hostPrerequisites")
    assert issue["severity"] == "error"
    assert issue["message"] == "missing from PATH: ninja."
    # The bundle file is still written on a doctor failure.
    assert doc["data"]["outputPath"] != ""


def test_a_warning_check_becomes_a_warning_issue_but_stays_exit_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: _clean_checks(warn=True))

    result = runner.invoke(app, ["support-bundle", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    issue = next(i for i in doc["issues"] if i["code"] == "support-bundle.hostPrerequisites")
    assert issue["severity"] == "warning"


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
