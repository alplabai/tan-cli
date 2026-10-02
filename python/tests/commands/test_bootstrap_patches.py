# SPDX-License-Identifier: Apache-2.0
"""`tan bootstrap`'s `zephyr/patches.yml` phase (tan-cli#1296).

Three layers, none of which spawns west or the real verifier:

* the phase itself, with `Runner.run_status` / `Runner.run` replaced by a
  scripted fake that RECORDS argv and cwd;
* the real `Runner.run_status`, driven with `sys.executable -c` children;
* the real command, `tan bootstrap --dry-run`, asserting where the verify
  argv lands in `plannedCommands` for each flag combination.

Paths come from `tmp_path` and `VenvBin` (never a literal `/` or `bin/`), so
every case holds on Windows.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tan.commands import bootstrap_cmd, bootstrap_patches
from tan.commands.bootstrap_patches import patches_phase
from tan.core import west_patches
from tan.core.bootstrap import completion_verdict, fallback_facts
from tests.commands.test_bootstrap_command import PRESENT_TOOL, envelope, make_sdk, run_tan


class Spawns:
    """Scripted stand-in for the Runner's two spawn seams. `verifies` is the
    queue of `(rc, stdout, stderr)` answers for non-`--list-unapplied` verifier
    calls, `listing` the answer to `--list-unapplied`. Every call is recorded as
    `(kind, argv, cwd)`."""

    def __init__(self, verifies, listing=(0, "", ""), apply_failure=None):
        self.verifies = list(verifies)
        self.listing = listing
        self.apply_failure = apply_failure
        self.calls: list[tuple[str, list[str], Path | None]] = []

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _, _ in self.calls]

    def run_status(self, runner, argv, cwd=None):
        runner.planned.append(list(argv))
        if runner.dry_run:
            self.calls.append(("verify", list(argv), cwd))
            return None, "", ""
        if "--list-unapplied" in argv:
            self.calls.append(("list", list(argv), cwd))
            return self.listing
        self.calls.append(("verify", list(argv), cwd))
        return self.verifies.pop(0)

    def run(self, argv, cwd=None):
        self.calls.append(("apply", list(argv), cwd))
        return self.apply_failure


def _setup(tmp_path: Path, monkeypatch, spawns: Spawns, *, with_verifier=True, dry_run=False):
    sdk = tmp_path / "ws" / "alp-sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "zephyr").mkdir()
    if with_verifier:
        (sdk / "scripts" / "verify_west_patches.py").write_text("", encoding="utf-8")
        (sdk / "zephyr" / "patches.yml").write_text("patches: []\n", encoding="utf-8")
    ws = bootstrap_cmd.Workspace(
        is_windows=False,
        facts=fallback_facts((3, 12)),
        repo_root=sdk,
        workspace_dir=tmp_path / "ws",
        venv_dir=tmp_path / "ws" / ".venv",
    )
    venv = bootstrap_cmd.VenvBin(tmp_path / "venv" / "python", tmp_path / "venv" / "west", "bin")
    runner = bootstrap_cmd.Runner(json=True, dry_run=dry_run)
    monkeypatch.setattr(
        bootstrap_cmd.Runner, "run_status",
        lambda self, argv, cwd=None: spawns.run_status(self, argv, cwd),
    )
    monkeypatch.setattr(
        bootstrap_cmd.Runner, "run",
        lambda self, argv, cwd=None, extra_env=None, tail_lines=4: spawns.run(argv, cwd),
    )
    log = bootstrap_cmd.Log(json_mode=True)
    return ws, venv, runner, log, sdk


def _issues(log):
    return [(i.code, i.severity, i.message) for i in log.take_issues(escalate_blocking=True)]


def test_an_already_patched_workspace_applies_nothing(tmp_path, monkeypatch):
    spawns = Spawns([(0, "", "")])
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns)

    patches_phase(ws, venv, log, runner, str(sdk))

    assert spawns.kinds == ["verify"]
    assert _issues(log) == []


def test_processes_run_where_they_must_and_the_verifier_gets_topdir_and_west(
    tmp_path, monkeypatch
):
    spawns = Spawns([(1, "", ""), (0, "", "")], listing=(0, "zephyr\n", ""))
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns)

    patches_phase(ws, venv, log, runner, str(sdk))

    for kind, argv, cwd in spawns.calls:
        if kind in ("verify", "list"):
            assert cwd == sdk  # the verifier resolves its own patches.yml from here
            assert argv[:2] == [str(venv.python), str(sdk / "scripts" / "verify_west_patches.py")]
            assert argv[argv.index("--topdir") + 1] == str(ws.workspace_dir)
            assert argv[argv.index("--west") + 1] == str(venv.west)
        else:
            assert kind == "apply"
            assert cwd == ws.workspace_dir  # `west patch` finds its topdir from cwd


def test_each_unapplied_module_gets_its_own_apply_with_dst_module_before_apply(
    tmp_path, monkeypatch
):
    spawns = Spawns([(1, "", "ABSENT"), (0, "", "")], listing=(0, "zephyr\nmcuboot\n", ""))
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns)

    patches_phase(ws, venv, log, runner, str(sdk))

    assert spawns.kinds == ["verify", "list", "apply", "apply", "verify"]
    applies = [argv for kind, argv, _ in spawns.calls if kind == "apply"]
    assert applies == [
        [str(venv.west), "patch", "--dst-module", "zephyr", "apply"],
        [str(venv.west), "patch", "--dst-module", "mcuboot", "apply"],
    ]
    assert _issues(log) == []


def test_an_apply_failure_names_the_module_the_output_the_clean_remedy_and_the_opt_out(
    tmp_path, monkeypatch
):
    spawns = Spawns(
        [(1, "", "")], listing=(0, "zephyr\n", ""), apply_failure="error: patch does not apply"
    )
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns)

    patches_phase(ws, venv, log, runner, str(sdk))

    [(code, severity, message)] = _issues(log)
    assert (code, severity) == ("bootstrap.west-patches-failed", "error")
    assert "--dst-module zephyr apply" in message
    assert "patch does not apply" in message
    assert "west patch --dst-module zephyr clean" in message
    assert "--no-patches" in message
    assert spawns.kinds == ["verify", "list", "apply"]  # stops: no re-verify over a failure


def test_a_first_verify_exit_3_is_the_unchecked_warning_not_a_false_failure(
    tmp_path, monkeypatch
):
    """Exit 3 = everything inspectable is patched, a named module just is not in
    this workspace. Run 1 used to warn and run 2 used to FAIL on it, because
    only exit 0 short-circuited and `--list-unapplied` is empty for exit 3."""
    spawns = Spawns([(3, "", "module hal_x not checked out")])
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns)

    patches_phase(ws, venv, log, runner, str(sdk))

    assert log.blocking() == []
    [(code, severity, message)] = _issues(log)
    assert (code, severity) == ("bootstrap.west-patches-unchecked", "warning")
    assert "hal_x not checked out" in message
    assert spawns.kinds == ["verify"]  # no listing, no apply


@pytest.mark.parametrize("first", [(2, "", "no workspace here"), (None, "", "failed to run python")])
def test_a_verifier_that_could_not_run_is_not_reported_as_not_applied(
    tmp_path, monkeypatch, first
):
    spawns = Spawns([first])
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns)

    patches_phase(ws, venv, log, runner, str(sdk))

    [(code, severity, message)] = _issues(log)
    assert (code, severity) == ("bootstrap.west-patches-failed", "error")
    assert "could not run or inspect" in message
    assert "patch state is unknown" in message
    assert "is not applied" not in message
    assert first[2] in message
    assert spawns.kinds == ["verify"]


def test_reverify_exit_3_is_a_non_blocking_warning(tmp_path, monkeypatch):
    spawns = Spawns(
        [(1, "", ""), (3, "", "module hal_x not checked out")], listing=(0, "zephyr\n", "")
    )
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns)

    patches_phase(ws, venv, log, runner, str(sdk))

    # Asserted BEFORE `_issues` drains the log: `blocking()` reads the same list.
    assert log.blocking() == []
    [(code, severity, message)] = _issues(log)
    assert (code, severity) == ("bootstrap.west-patches-unchecked", "warning")
    assert "hal_x not checked out" in message


def test_reverify_exit_1_is_a_blocking_error_carrying_the_verifier_output(tmp_path, monkeypatch):
    spawns = Spawns(
        [(1, "", ""), (1, "", "ABSENT zephyr/0002-ipm.patch")], listing=(0, "zephyr", "")
    )
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns)

    patches_phase(ws, venv, log, runner, str(sdk))

    assert log.blocking() == ["west-patches-failed"]
    [(code, severity, message)] = _issues(log)
    assert (code, severity) == ("bootstrap.west-patches-failed", "error")
    assert "still not applied" in message
    assert "ABSENT zephyr/0002-ipm.patch" in message
    assert "--no-patches" in message


def test_reverify_exit_2_says_unknown_not_unapplied(tmp_path, monkeypatch):
    spawns = Spawns([(1, "", ""), (2, "", "workspace vanished")], listing=(0, "zephyr", ""))
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns)

    patches_phase(ws, venv, log, runner, str(sdk))

    [(_code, _severity, message)] = _issues(log)
    assert "could not run or inspect" in message
    assert "workspace vanished" in message


def test_an_empty_unapplied_list_is_an_error_not_a_silent_pass(tmp_path, monkeypatch):
    spawns = Spawns([(1, "", "ABSENT x")], listing=(0, "\n", ""))
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns)

    patches_phase(ws, venv, log, runner, str(sdk))

    [(code, _severity, message)] = _issues(log)
    assert code == "bootstrap.west-patches-failed"
    assert "named no module" in message
    assert "ABSENT x" in message
    assert "apply" not in spawns.kinds


def test_an_sdk_without_the_verifier_skips_quietly(tmp_path, monkeypatch):
    spawns = Spawns([])
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns, with_verifier=False)

    patches_phase(ws, venv, log, runner, str(sdk))

    assert spawns.calls == []
    assert _issues(log) == []


def test_a_dry_run_records_the_verify_and_executes_nothing_else(tmp_path, monkeypatch):
    spawns = Spawns([])
    ws, venv, runner, log, sdk = _setup(tmp_path, monkeypatch, spawns, dry_run=True)

    patches_phase(ws, venv, log, runner, str(sdk))

    assert spawns.kinds == ["verify"]  # planned only; run_status spawns nothing under dry-run
    assert _issues(log) == []


def test_the_failure_code_is_workspace_blocking_and_the_verdict_wording_fits():
    assert bootstrap_patches.FAILED in bootstrap_cmd.WORKSPACE_BLOCKING
    assert bootstrap_patches.UNCHECKED not in bootstrap_cmd.WORKSPACE_BLOCKING

    lines, ok = completion_verdict([bootstrap_patches.FAILED], False)
    assert ok is False
    assert "zephyr/patches.yml was not applied" in lines[0]
    assert "did not install" not in lines[0]

    mixed, _ = completion_verdict(["sdk-extras", bootstrap_patches.FAILED], True)
    assert "sdk-extras did not install; zephyr/patches.yml was not applied" in mixed[1]


def test_pure_helpers():
    assert west_patches.parse_unapplied("zephyr\n\n mcuboot \nzephyr\n") == ["zephyr", "mcuboot"]
    assert [west_patches.classify_verify(c) for c in (0, 3, 1, 2, None)] == [
        "applied", "unchecked", "unapplied", "unrunnable", "unrunnable",
    ]
    assert west_patches.output_tail("\n".join(str(i) for i in range(100))).endswith("99")


# ---------------------------------------------------------------------------
# The real `Runner.run_status`
# ---------------------------------------------------------------------------


def test_run_status_returns_the_exit_code_and_both_streams():
    script = "import sys; print('to-out'); print('to-err', file=sys.stderr); sys.exit(3)"
    runner = bootstrap_cmd.Runner(json=True)

    code, out, err = runner.run_status([sys.executable, "-c", script])

    assert code == 3  # not collapsed to "failed": the verifier's 3 is its own verdict
    assert "to-out" in out and "to-err" in err
    assert runner.planned == [[sys.executable, "-c", script]]


def test_run_status_honours_cwd(tmp_path):
    runner = bootstrap_cmd.Runner(json=True)

    code, out, _err = runner.run_status(
        [sys.executable, "-c", "import os; print(os.getcwd())"], cwd=tmp_path
    )

    assert code == 0
    assert Path(out.strip()).resolve() == tmp_path.resolve()


def test_run_status_pins_utf8_for_the_child():
    runner = bootstrap_cmd.Runner(json=True)

    _code, out, _err = runner.run_status(
        [sys.executable, "-c", "import os; print(os.environ['PYTHONIOENCODING'])"]
    )

    assert out.strip() == "utf-8"


def test_run_status_of_a_missing_binary_is_none_with_the_reason(tmp_path):
    runner = bootstrap_cmd.Runner(json=True)

    code, out, err = runner.run_status([str(tmp_path / "no-such-binary")])

    assert code is None and out == ""
    assert "failed to run" in err


def test_run_status_under_dry_run_spawns_nothing_but_records_the_argv(tmp_path):
    marker = tmp_path / "ran"
    argv = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    runner = bootstrap_cmd.Runner(json=True, dry_run=True)

    assert runner.run_status(argv) == (None, "", "")

    assert not marker.exists()
    assert runner.planned == [argv]


# ---------------------------------------------------------------------------
# The real command: gating and ordering of the phase in `tan bootstrap`
# ---------------------------------------------------------------------------


def _sdk_with_verifier(tmp_path: Path) -> Path:
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    (sdk / "scripts" / "verify_west_patches.py").write_text("", encoding="utf-8")
    (sdk / "zephyr").mkdir()
    (sdk / "zephyr" / "patches.yml").write_text("patches: []\n", encoding="utf-8")
    return sdk


def _planned(tmp_path: Path, *flags: str) -> tuple[list[str], dict]:
    sdk = _sdk_with_verifier(tmp_path)
    env = envelope(
        run_tan(
            "bootstrap", "--dry-run", "--no-toolchain", *flags, "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
        )
    )
    assert env["exitCode"] == 0, env["issues"]
    return env["data"]["plannedCommands"], env["data"]


def _verify_index(planned: list[str]) -> list[int]:
    return [i for i, cmd in enumerate(planned) if "verify_west_patches.py" in cmd]


def test_the_verify_runs_after_the_pip_installs_by_default(tmp_path):
    planned, data = _planned(tmp_path)

    [at] = _verify_index(planned)
    assert at == len(planned) - 1  # last: after zephyr reqs, SDK extras AND the editable install
    assert "-e " in planned[at - 1]
    assert data["noPatches"] is False


def test_the_verify_still_runs_under_no_pip(tmp_path):
    planned, _ = _planned(tmp_path, "--no-pip")

    [at] = _verify_index(planned)
    assert planned[at - 1].endswith("zephyr-export")  # right after the west phase
    assert not any("pip install -q" in cmd for cmd in planned)


def test_no_west_skips_the_verify(tmp_path):
    planned, _ = _planned(tmp_path, "--no-west")

    assert _verify_index(planned) == []


def test_no_patches_skips_the_verify_and_is_reported(tmp_path):
    planned, data = _planned(tmp_path, "--no-patches")

    assert _verify_index(planned) == []
    assert data["noPatches"] is True
    assert any("-e " in cmd for cmd in planned)  # the rest of the run is unaffected


def test_no_patches_logs_that_the_patches_are_not_applied(tmp_path):
    sdk = _sdk_with_verifier(tmp_path)

    proc = run_tan(
        "bootstrap", "--dry-run", "--no-toolchain", "--no-patches",
        "--sdk-root", str(sdk), cwd=sdk.parent,
    )

    assert "--no-patches" in proc.stderr and "NOT applied" in proc.stderr


def test_the_flag_is_documented_in_help(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    proc = run_tan("bootstrap", "--help", cwd=cwd)

    assert "--no-patches" in proc.stdout
