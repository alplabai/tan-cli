# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1267: `tan flash` / `tan run` and Flow A's whole-ATOC guard.

A `zephyr_west_flash` slice whose west runner is `alif_flash` burns over the
SE-UART, replacing the whole ATOC. alp-sdk's runner (alp-sdk#2262) reads the
resident ATOC first and refuses, leaving a v1 verdict at
`<build_dir>/alif_flash/atoc-guard.json`. These cases drive the real
`flash_cmd._run` with `_spawn` faked -- nothing here reaches a device -- and
the fake writes verdict JSON the way the runner does.
"""
import inspect
import json
import os
import subprocess
import sys

import pytest
import typer
from typer.testing import CliRunner

from tan.commands import flash_cmd, run_cmd
from tan.core.run import RunAction
from tan.exit_codes import ExitCode

GUARDED_RUNNER = (
    "parser.add_argument(\n    '--replace-atoc', dest='replace_atoc',\n"
    "    action='store_true')\n"
    "verdict = {'schema': 'alp-sdk.alif-flash-atoc-guard.v1'}\n"
)
OLD_RUNNER = "class AlifFlashBinaryRunner:\n    pass\n"


def _verdict(status, foreign=(), query_status="ok", allowed=("ALP-HE",)):
    return {
        "schema": "alp-sdk.alif-flash-atoc-guard.v1",
        "status": status,
        "foreign": list(foreign),
        "transcript": "atoc-before.txt",
        "allowed": list(allowed),
        "query_status": query_status,
    }


def _image(build_dir, flash_runner="alif_flash", modules_txt=None):
    """One Zephyr image dir: `zephyr/zephyr.bin`, `zephyr/runners.yaml`, and
    optionally `zephyr_modules.txt`."""
    (build_dir / "zephyr").mkdir(parents=True)
    (build_dir / "zephyr" / "zephyr.bin").write_bytes(b"\x00")
    if flash_runner is not None:
        (build_dir / "zephyr" / "runners.yaml").write_text(
            f"runners:\n- {flash_runner}\n\nflash-runner: {flash_runner}\n", encoding="utf-8", newline="\n"
        )
    if modules_txt is not None:
        (build_dir / "zephyr_modules.txt").write_text(modules_txt, encoding="utf-8", newline="\n")


def _module(root, source):
    """An alp-sdk module checkout carrying `source` as its alif_flash runner."""
    runners = root / "scripts" / "west_commands" / "runners"
    runners.mkdir(parents=True)
    (runners / "alif_flash.py").write_text(source, encoding="utf-8", newline="\n")
    return f'"alp-sdk":"{root.as_posix()}":"{root.as_posix()}/zephyr"\n'


def _manifest(tmp_path, slices):
    """`slices`: `(core_id, output_artefact, flash_args)` triples."""
    rows = "".join(
        f"- {{core_id: {core}, os: zephyr, output_artefact: {artefact}, status: ok,\n"
        f"   flash_method: zephyr_west_flash, flash_args: {args}}}\n"
        for core, artefact, args in slices
    )
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        f"schema_version: 1\nhw_info: {{sku: S}}\nslices:\n{rows}"
        "helper_mcus: []\nboot_order: []\n",
        encoding="utf-8",
        newline="",
    )


def _project(tmp_path, *, runner_source=GUARDED_RUNNER, flash_runner="alif_flash",
             flash_method="zephyr_west_flash", flash_args="{}"):
    """A built project: one slice whose artefact sits in `build/app/zephyr/`,
    so `west flash --build-dir` is `build/app` and the verdict lands in
    `build/app/alif_flash/`."""
    build_dir = tmp_path / "build" / "app"
    (build_dir / "zephyr").mkdir(parents=True)
    (build_dir / "zephyr" / "zephyr.bin").write_bytes(b"\x00")
    if flash_runner is not None:
        (build_dir / "zephyr" / "runners.yaml").write_text(
            f"runners:\n- {flash_runner}\n\nflash-runner: {flash_runner}\n", encoding="utf-8", newline="\n"
        )
    sdk = tmp_path / "sdk"
    (sdk / "scripts" / "west_commands" / "runners").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8", newline="\n")
    if runner_source is not None:
        (sdk / "scripts" / "west_commands" / "runners" / "alif_flash.py").write_text(
            runner_source, encoding="utf-8", newline="\n"
        )
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        "schema_version: 1\nhw_info: {sku: S}\nslices:\n"
        "- {core_id: m55_he, os: zephyr, output_artefact: app/zephyr/zephyr.bin, "
        f"status: ok,\n   flash_method: {flash_method}, flash_args: {flash_args}}}\n"
        "helper_mcus: []\nboot_order: []\n",
        encoding="utf-8",
        newline="",
    )
    return build_dir


def _flash(tmp_path, monkeypatch, *, rc=0, verdict=None, raw=None, dry_run=False,
           replace_atoc=False, atoc_unqueryable=False, spawned=None, steps=None,
           core=None, workspace=None, cwd=None, stderr=None):
    """`flash_cmd._run` with `west` on a fake PATH and `_spawn` faked. The fake
    writes `verdict` (a dict) or `raw` (text) where the runner would -- under
    the `--build-dir` it was given. `steps`, when set, is one `(rc, verdict)`
    per spawn, in order, for a multi-entry run. `workspace` fakes the west
    topdir `west flash` is spawned in. `stderr` replaces the failing spawn's
    one-line stderr."""
    tools = tmp_path / "faketools"
    tools.mkdir(exist_ok=True)
    for name in ("west", "JLinkExe"):
        tool = tools / (f"{name}.exe" if os.name == "nt" else name)
        tool.write_text("", encoding="utf-8", newline="\n")
        if os.name != "nt":
            os.chmod(tool, 0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.delenv("ALP_FLASH_REQUIRE_DPIDR", raising=False)
    monkeypatch.setattr(flash_cmd, "venv_bin_dir", lambda *_a, **_k: None)
    monkeypatch.setattr(
        flash_cmd, "west_workspace_dir",
        lambda *_a, **_k: None if workspace is None else os.fspath(workspace),
    )
    if cwd is not None:
        monkeypatch.chdir(cwd)
    calls = spawned if spawned is not None else []
    plan = list(steps) if steps is not None else None

    def fake_spawn(argv, *_a, **_k):
        calls.append(list(argv))
        step_rc, step_verdict, step_raw = rc, verdict, raw
        if plan is not None:
            step_rc, step_verdict = plan.pop(0)
            step_raw = None
        build_dir = argv[argv.index("--build-dir") + 1]
        if step_verdict is not None or step_raw is not None:
            verdict_file = os.path.join(build_dir, "alif_flash", "atoc-guard.json")
            os.makedirs(os.path.dirname(verdict_file), exist_ok=True)
            text = step_raw if step_raw is not None else json.dumps(step_verdict, indent=2) + "\n"
            with open(verdict_file, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        return flash_cmd._Outcome(
            success=step_rc == 0, returncode=step_rc, captured=True,
            stderr="" if step_rc == 0 else (stderr or "FATAL ERROR: command exited with status 1"),
        )

    monkeypatch.setattr(flash_cmd, "_spawn", fake_spawn)
    return flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core=core, helper=None, dry_run=dry_run,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
        atoc_unqueryable=atoc_unqueryable, replace_atoc=replace_atoc,
    )


def _run_app() -> typer.Typer:
    """`run` as a subcommand; the unused second command stops Typer
    collapsing a one-command app into a bare CLI."""
    app = typer.Typer(add_completion=False)
    app.command("run")(run_cmd.run)
    app.command("_unused")(lambda: None)
    return app


def _codes(issues):
    return [i.code for i in issues]


# ── the argv ─────────────────────────────────────────────────────────────────


def test_replace_atoc_reaches_west_flash_on_a_guarded_alif_flash_slice(tmp_path, monkeypatch):
    _project(tmp_path)
    spawned = []
    exit_code, _data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, replace_atoc=True, spawned=spawned,
        verdict=_verdict("replaced", ["A32_APP"]),
    )
    assert exit_code == 0, issues
    assert len(spawned) == 1
    assert spawned[0][:2] == ["west", "flash"]
    assert spawned[0][-1] == "--replace-atoc", spawned
    assert "--atoc-unqueryable" not in spawned[0]
    assert _codes(issues) == []


def test_without_the_flag_west_flash_gets_no_override(tmp_path, monkeypatch):
    _project(tmp_path)
    spawned = []
    _flash(tmp_path, monkeypatch, spawned=spawned, verdict=_verdict("clear"))
    assert "--replace-atoc" not in spawned[0], spawned
    assert "--atoc-unqueryable" not in spawned[0], spawned


def test_atoc_unqueryable_is_never_read_as_replace_atoc(tmp_path, monkeypatch):
    """The Flow D acknowledgement must not open Flow A's guard."""
    _project(tmp_path)
    spawned = []
    _flash(tmp_path, monkeypatch, spawned=spawned, atoc_unqueryable=True,
           verdict=_verdict("clear"))
    assert "--replace-atoc" not in spawned[0], spawned


def test_dry_run_shows_the_flag_in_the_planned_argv(tmp_path, monkeypatch):
    _project(tmp_path)
    spawned = []
    exit_code, data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, dry_run=True, replace_atoc=True, spawned=spawned
    )
    assert exit_code == 0, issues
    assert spawned == []
    message = data["entries"][0]["message"]
    assert message.startswith("would run west flash --build-dir "), message
    assert message.endswith(" --replace-atoc"), message


def test_a_successful_write_reports_what_the_override_replaced(tmp_path, monkeypatch):
    _project(tmp_path)
    _exit, data, _issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, replace_atoc=True, verdict=_verdict("replaced", ["A32_APP"]),
    )
    message = data["entries"][0]["message"]
    assert message.endswith(
        "; ATOC guard: --replace-atoc overrode resident entries, now delisted: A32_APP"
    ), message


# ── refusals ─────────────────────────────────────────────────────────────────


def test_a_refused_foreign_verdict_is_its_own_code_naming_every_entry(tmp_path, monkeypatch):
    _project(tmp_path)
    exit_code, data, issues, text, _sdk = _flash(
        tmp_path, monkeypatch, rc=1,
        verdict=_verdict("refused-foreign", ["A32_APP", "BOOTLOAD"]),
    )
    assert exit_code != 0
    assert _codes(issues) == ["flash.atoc-guard-refused"], issues
    message = issues[0].message
    assert message.startswith(
        "zephyr_west_flash[m55_he]: the alif_flash runner's pre-burn ATOC guard refused "
        "this write before anything was burned (status: refused-foreign; transcript: "
        "atoc-before.txt)."
    ), message
    assert "the board also carries: A32_APP, BOOTLOAD." in message
    assert "re-run with --replace-atoc" in message
    assert message.endswith(
        "West reported: zephyr_west_flash[m55_he]: FATAL ERROR: command exited with status 1"
    ), message
    assert data["entries"][0]["status"] == "failed"
    assert any(line.startswith("  FAIL: zephyr_west_flash[m55_he]: the alif_flash") for line in text)


#: What the E1M-AEN803 bench printed around a real refusal (tan-cli#1426):
#: two runner-loading warnings that say nothing about the ATOC guard.
_RUNNER_NOISE_LINES = (
    'The module for runner "rtsflash" could not be imported (No module named \'usb\')',
    "WARNING: runners.alif_flash: the 'fdt' Python package (needed by app-gen-toc) was not found",
)
_NOISY_STDERR = "\n".join(
    (_RUNNER_NOISE_LINES[0], "-- west flash: using runner alif_flash", _RUNNER_NOISE_LINES[1],
     "ERROR: ATOC guard refused the burn", "FATAL ERROR: command exited with status 1")
) + "\n"


def test_a_refusal_keeps_runner_setup_warnings_out_of_its_message(tmp_path, monkeypatch):
    _project(tmp_path)
    _exit, _data, issues, text, _sdk = _flash(
        tmp_path, monkeypatch, rc=1, stderr=_NOISY_STDERR,
        verdict=_verdict("refused-foreign", ["A32_APP"]),
    )
    assert _codes(issues) == ["flash.atoc-guard-refused", "flash.runner-setup-warnings"], issues
    refusal, note = issues[0].message, issues[1].message
    assert refusal.endswith(
        "West reported: zephyr_west_flash[m55_he]: -- west flash: using runner alif_flash | "
        "ERROR: ATOC guard refused the burn | FATAL ERROR: command exited with status 1"
    ), refusal
    assert "rtsflash" not in refusal and "fdt" not in refusal, refusal
    assert issues[1].severity == "warning"
    assert note.endswith(" | ".join(_RUNNER_NOISE_LINES)), note
    assert note in text


def test_a_refusal_with_no_runner_noise_adds_no_warning(tmp_path, monkeypatch):
    _project(tmp_path)
    _exit, _data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, rc=1, verdict=_verdict("refused-foreign", ["A32_APP"]),
    )
    assert _codes(issues) == ["flash.atoc-guard-refused"], issues


def test_a_failure_the_guard_did_not_refuse_keeps_the_runner_warnings(tmp_path, monkeypatch):
    """Past a `clear` verdict the warnings may be the cause: a runner that
    could not be imported is exactly why a burn fails. They stay put."""
    _project(tmp_path)
    _exit, _data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, rc=1, stderr=_NOISY_STDERR, verdict=_verdict("clear"),
    )
    assert _codes(issues) == ["flash.entry-failed"], issues
    assert "WARNING: runners.alif_flash: the 'fdt' Python package" in issues[0].message


def test_a_refused_unverified_verdict_says_the_read_could_not_be_verified(tmp_path, monkeypatch):
    _project(tmp_path)
    _exit, _data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, rc=1, verdict=_verdict("refused-unverified", query_status="unverified"),
    )
    assert _codes(issues) == ["flash.atoc-guard-refused"], issues
    message = issues[0].message
    assert "(status: refused-unverified;" in message
    assert "could not verify what is resident in the ATOC over the SE-UART" in message
    assert "the board also carries" not in message


def test_a_failure_after_the_guard_passed_is_an_ordinary_failure(tmp_path, monkeypatch):
    _project(tmp_path)
    _exit, _data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, rc=1, verdict=_verdict("clear"),
    )
    assert _codes(issues) == ["flash.entry-failed"], issues
    assert issues[0].message.endswith("(ATOC guard: clear; the failure came after it)")


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (None, "was written for this attempt"),
        ("{truncated", "is not valid JSON"),
        (json.dumps({**_verdict("refused-foreign", ["X"]),
                     "schema": "alp-sdk.alif-flash-atoc-guard.v2"}), "its schema is"),
        (json.dumps(_verdict("refused-sideways")), "its status 'refused-sideways'"),
    ],
)
def test_an_absent_or_unreadable_verdict_is_never_turned_into_one(tmp_path, monkeypatch, raw, reason):
    _project(tmp_path)
    _exit, _data, issues, _text, _sdk = _flash(tmp_path, monkeypatch, rc=1, raw=raw)
    assert _codes(issues) == ["flash.entry-failed"], issues
    message = issues[0].message
    assert message.startswith("zephyr_west_flash[m55_he]: FATAL ERROR"), message
    assert "The alif_flash ATOC guard verdict could not be read (" in message
    assert reason in message, message
    assert "so tan cannot say what the guard decided for this write" in message


def test_a_stale_verdict_from_an_earlier_run_is_not_this_runs_verdict(tmp_path, monkeypatch):
    """A `west flash` that dies before the runner's `do_run` (bad argument,
    runner import failure) never reaches the runner's own unlink. tan removes
    the old verdict first, so an earlier refusal is not reported again."""
    build_dir = _project(tmp_path)
    old = build_dir / "alif_flash" / "atoc-guard.json"
    old.parent.mkdir()
    old.write_text(json.dumps(_verdict("refused-foreign", ["OLD_ENTRY"])), encoding="utf-8", newline="\n")
    _exit, _data, issues, _text, _sdk = _flash(tmp_path, monkeypatch, rc=1)
    assert _codes(issues) == ["flash.entry-failed"], issues
    assert "OLD_ENTRY" not in issues[0].message
    assert "was written for this attempt" in issues[0].message
    assert not old.exists()


# ── slices the flag does not apply to ────────────────────────────────────────


def test_replace_atoc_on_a_non_alif_flash_runner_warns_and_is_not_passed(tmp_path, monkeypatch):
    _project(tmp_path, flash_runner="jlink")
    spawned = []
    exit_code, _data, issues, text, _sdk = _flash(
        tmp_path, monkeypatch, replace_atoc=True, spawned=spawned
    )
    assert exit_code == 0
    assert "--replace-atoc" not in spawned[0]
    assert _codes(issues) == ["flash.replace-atoc-not-applicable"], issues
    assert "its west runner is jlink, not alif_flash" in issues[0].message
    assert issues[0].severity == "warning"
    assert issues[0].message in text


def test_replace_atoc_on_a_flow_d_slice_points_at_the_flow_d_flag(tmp_path, monkeypatch):
    """Flow D refuses (unacknowledged) before anything spawns; the warning
    still says `--replace-atoc` is not its acknowledgement."""
    _project(tmp_path, flash_args='{jlink_flash_device: PART, atoc: a.bin, '
             'atoc_address: "0x8057F5B0", confirm: true}')
    exit_code, _data, issues, _text, _sdk = _flash(tmp_path, monkeypatch, replace_atoc=True)
    assert exit_code != 0
    assert _codes(issues) == [
        "flash.atoc-replacement-unacknowledged", "flash.replace-atoc-not-applicable",
    ], issues
    assert "Flow D's whole-ATOC acknowledgement is --atoc-unqueryable" in issues[1].message


def test_an_sdk_runner_without_the_guard_is_disclosed_and_never_passed_the_flag(tmp_path, monkeypatch):
    _project(tmp_path, runner_source=OLD_RUNNER)
    spawned = []
    exit_code, _data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, replace_atoc=True, spawned=spawned
    )
    assert exit_code == 0
    assert "--replace-atoc" not in spawned[0], spawned
    assert _codes(issues) == ["flash.atoc-guard-unavailable"], issues
    message = issues[0].message
    assert "has no pre-burn ATOC guard (alp-sdk#2262)" in message
    assert "--replace-atoc was NOT passed" in message


def test_an_unguarded_alif_flash_write_is_disclosed_without_the_flag_too(tmp_path, monkeypatch):
    _project(tmp_path, runner_source=None)
    _exit, _data, issues, _text, _sdk = _flash(tmp_path, monkeypatch)
    assert _codes(issues) == ["flash.atoc-guard-unavailable"], issues
    assert "--replace-atoc was NOT passed" not in issues[0].message


def test_a_plain_west_slice_without_the_flag_says_nothing_new(tmp_path, monkeypatch):
    _project(tmp_path, flash_runner=None)
    _exit, _data, issues, _text, _sdk = _flash(tmp_path, monkeypatch)
    assert _codes(issues) == [], issues


# ── the CLI surfaces ─────────────────────────────────────────────────────────


def test_tan_flash_registers_replace_atoc_as_its_own_option():
    option = inspect.signature(flash_cmd.flash).parameters["replace_atoc"].default
    assert option.param_decls == ("--replace-atoc",)
    assert "--atoc-unqueryable" not in option.param_decls
    assert "[" not in option.help  # rich markup would eat it


def test_tan_run_forwards_replace_atoc_to_the_flash_path(tmp_path, monkeypatch):
    calls = []

    def fake_internal_run(**kwargs):
        calls.append(kwargs)
        return (ExitCode.SUCCESS, None, [], ["run: ok."])

    monkeypatch.setattr(run_cmd, "_run", fake_internal_run)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(_run_app(), ["run", "--flash", "--confirm", "--replace-atoc"])
    assert "No such option" not in result.output, result.output
    assert calls[-1]["replace_atoc"] is True, calls[-1]
    assert calls[-1]["atoc_unqueryable"] is False, calls[-1]

    runner.invoke(_run_app(), ["run", "--flash", "--confirm"])
    assert calls[-1]["replace_atoc"] is False, calls[-1]


def test_run_internal_hands_replace_atoc_to_flash_run(tmp_path, monkeypatch):
    seen = {}

    def fake_flash_run(**kwargs):
        seen.update(kwargs)
        return (ExitCode.SUCCESS, {}, [], [], None)

    monkeypatch.setattr(run_cmd, "_build", lambda **_k: (ExitCode.SUCCESS, {}, []))
    monkeypatch.setattr(run_cmd, "decide_run_action", lambda *a, **k: RunAction.FLASH)
    monkeypatch.setattr(flash_cmd, "_run", fake_flash_run)
    run_cmd._run(
        build_root=str(tmp_path), sdk_root=None, sdk_root_for_stamp=None, board_yaml=None,
        flash=True, core=None, json_mode=True, replace_atoc=True,
    )
    assert seen["replace_atoc"] is True
    assert seen["atoc_unqueryable"] is False


# ── JSON-mode hygiene, through a real process ────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="the fake west below is a POSIX shell script")
def test_a_refusal_in_json_mode_is_one_envelope_and_a_silent_stderr(tmp_path):
    """The refusal path end to end: a real `python -m tan flash --format json`
    spawning a fake `west` that writes the verdict where the runner would and
    exits 1. stdout must be exactly one envelope and stderr empty."""
    _project(tmp_path)
    tools = tmp_path / "faketools"
    tools.mkdir()
    west = tools / "west"
    verdict = json.dumps(_verdict("refused-foreign", ["A32_APP"]))
    # argv: flash --build-dir <dir>
    west.write_text(
        "#!/bin/sh\n"
        'mkdir -p "$3/alif_flash"\n'
        f"printf '%s' '{verdict}' > \"$3/alif_flash/atoc-guard.json\"\n"
        "echo 'FATAL ERROR: refusing to burn' 1>&2\n"
        "exit 1\n",
        encoding="utf-8", newline="\n",
    )
    os.chmod(west, 0o755)
    package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": package_root,
    }
    env.pop("ALP_FLASH_FORCE", None)
    # No --replace-atoc: with it the real runner writes `replaced`, never a
    # refusal, so the fake must not be driven into a state the runner cannot reach.
    proc = subprocess.run(
        [sys.executable, "-m", "tan", "flash", "--sdk-root", "./sdk", "--format", "json", "."],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", env=env, timeout=180,
    )
    assert proc.stderr == "", proc.stderr
    payload = json.loads(proc.stdout)  # a second document would raise here
    assert payload["exitCode"] != 0 and payload["ok"] is False
    assert [i["code"] for i in payload["issues"]] == ["flash.atoc-guard-refused"], payload
    message = payload["issues"][0]["message"]
    assert "the board also carries: A32_APP." in message, message
    assert message.endswith("FATAL ERROR: refusing to burn"), message


# ── review round 1: one flag, many writes (item 1) ───────────────────────────


def _dual_core(tmp_path):
    """Two hand-written Flow A slices on alif_flash, as the review's scenario."""
    _project(tmp_path)  # sdk + m55_he image; manifest rewritten below
    _image(tmp_path / "build" / "hp")
    _manifest(tmp_path, [
        ("m55_he", "app/zephyr/zephyr.bin", "{}"),
        ("m55_hp", "hp/zephyr/zephyr.bin", "{}"),
    ])


def test_one_replace_atoc_never_reaches_two_alif_flash_writes(tmp_path, monkeypatch):
    _dual_core(tmp_path)
    spawned = []
    exit_code, data, issues, text, _sdk = _flash(
        tmp_path, monkeypatch, replace_atoc=True, spawned=spawned
    )
    assert exit_code != 0
    assert spawned == [], "a refusal must land before anything is written"
    assert data["entries"] == []
    assert _codes(issues) == ["flash.replace-atoc-ambiguous"], issues
    assert issues[0].message == (
        "flash: --replace-atoc would reach 2 alif_flash writes in this run (m55_he, m55_hp). "
        "Each one REPLACES the whole ATOC with only its own entry, so the flag given to "
        "accept losing what is on the board now would also override the next write's "
        "guard into delisting what the previous write in this same run had just put "
        "there. Nothing was flashed. Narrow the run to one entry with --core CORE_ID (or "
        "--helper NAME) and pass --replace-atoc only there. Flashing several cores this "
        "way leaves only the last one listed; to put both M55 cores in the ATOC use "
        "alp-sdk's scripts/bench/aen/flash-run-dualcore.sh <hp-build-dir> <he-build-dir>, "
        "which burns one ATOC carrying both cores' entries (docs/aen-bench-bringup.md, "
        "\"Flow A -- Dual-core deferred-TOC boot\"; it emits the HP-master shape, and an "
        "HE-master ATOC is not scripted) (tan-cli#1267)."
    )
    assert issues[0].message in text


def test_the_ambiguity_refusal_covers_a_preview_too(tmp_path, monkeypatch):
    _dual_core(tmp_path)
    _exit, _data, issues, _text, _sdk = _flash(tmp_path, monkeypatch, replace_atoc=True,
                                               dry_run=True)
    assert _codes(issues) == ["flash.replace-atoc-ambiguous"], issues


def test_narrowing_to_one_core_lets_the_flag_through(tmp_path, monkeypatch):
    _dual_core(tmp_path)
    spawned = []
    exit_code, _data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, replace_atoc=True, core="m55_hp", spawned=spawned,
        verdict=_verdict("replaced", ["A32_APP"], allowed=("ALP-HP",)),
    )
    assert exit_code == 0, issues
    assert len(spawned) == 1 and spawned[0][-1] == "--replace-atoc", spawned
    assert spawned[0][3].endswith(os.path.join("build", "hp")), spawned


def test_a_later_core_refused_over_an_entry_this_run_wrote_is_told_why(tmp_path, monkeypatch):
    """No flag: HE burns {ALP-HE}; HP's guard then sees ALP-HE as foreign."""
    _dual_core(tmp_path)
    _exit, _data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch,
        steps=[
            (0, _verdict("clear", allowed=("ALP-HE",))),
            (1, _verdict("refused-foreign", ["ALP-HE"], allowed=("ALP-HP",))),
        ],
    )
    assert _codes(issues) == ["flash.atoc-guard-refused"], issues
    message = issues[0].message
    assert message.startswith("zephyr_west_flash[m55_hp]: the alif_flash runner's pre-burn")
    assert (
        "The resident entry it would delist is ALP-HE (written by m55_he), written earlier "
        "in this same run. This manifest cannot be Flow A-flashed one core after another"
    ) in message
    assert "--replace-atoc is NOT the fix here" in message
    assert "re-run with --replace-atoc" not in message


# ── review round 1: which runner west really loads (item 2) ──────────────────


def _runner_path(root):
    return os.path.join(str(root), "scripts", "west_commands", "runners", "alif_flash.py")


def test_the_runner_checked_is_the_one_the_build_used_not_the_bound_sdk(tmp_path, monkeypatch):
    """Bound SDK has the guard; the build's alp-sdk module does not. tan must
    not mark it guarded, must not pass the flag, and must name the file."""
    build_dir = _project(tmp_path)  # bound sdk: GUARDED_RUNNER
    modules = _module(tmp_path / "other-sdk", OLD_RUNNER)
    (build_dir / "zephyr_modules.txt").write_text(modules, encoding="utf-8", newline="\n")
    spawned = []
    exit_code, _data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, replace_atoc=True, spawned=spawned
    )
    assert exit_code == 0
    assert "--replace-atoc" not in spawned[0], spawned
    assert _codes(issues) == ["flash.atoc-guard-unavailable"], issues
    message = issues[0].message
    other = (tmp_path / "other-sdk").as_posix()
    checked = os.path.join(other, "scripts", "west_commands", "runners", "alif_flash.py")
    assert f"the runner tan checked -- {checked} (the alp-sdk module this build used, per " \
        in message, message


def test_a_guarded_build_module_is_honoured_when_the_bound_sdk_is_older(tmp_path, monkeypatch):
    build_dir = _project(tmp_path, runner_source=OLD_RUNNER)
    modules = _module(tmp_path / "new-sdk", GUARDED_RUNNER)
    (build_dir / "zephyr_modules.txt").write_text(modules, encoding="utf-8", newline="\n")
    spawned = []
    exit_code, _data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, replace_atoc=True, spawned=spawned,
        verdict=_verdict("replaced", ["A32_APP"]),
    )
    assert exit_code == 0, issues
    assert spawned[0][-1] == "--replace-atoc", spawned
    assert _codes(issues) == [], issues


def test_a_build_listing_no_alp_sdk_module_is_unguarded(tmp_path, monkeypatch):
    build_dir = _project(tmp_path)
    (build_dir / "zephyr_modules.txt").write_text('"hal_alif":"/h":"/h/zephyr"\n',
                                                  encoding="utf-8", newline="\n")
    _exit, _data, issues, _text, _sdk = _flash(tmp_path, monkeypatch)
    assert _codes(issues) == ["flash.atoc-guard-unavailable"], issues
    assert "that build lists no alp-sdk module" in issues[0].message


def test_a_build_with_no_module_list_falls_back_to_the_bound_sdk_and_says_so(tmp_path, monkeypatch):
    _project(tmp_path, runner_source=OLD_RUNNER)
    _exit, _data, issues, _text, _sdk = _flash(tmp_path, monkeypatch)
    assert _codes(issues) == ["flash.atoc-guard-unavailable"], issues
    assert f"the runner tan checked -- {_runner_path(tmp_path / 'sdk')} (the bound alp-sdk; " \
        "the build has no readable " in issues[0].message, issues[0].message


def test_a_guarded_write_that_succeeds_with_no_verdict_is_disclosed(tmp_path, monkeypatch):
    """The fail-open shape: tan thought the runner had the guard, west wrote
    happily, and no verdict appeared -- the guard did not run."""
    _project(tmp_path)
    exit_code, data, issues, text, _sdk = _flash(tmp_path, monkeypatch)
    assert exit_code == 0
    assert data["entries"][0]["status"] == "ok"
    assert _codes(issues) == ["flash.atoc-guard-unavailable"], issues
    message = issues[0].message
    assert message.startswith(
        "zephyr_west_flash[m55_he]: west flash succeeded, but the alif_flash runner's ATOC "
        "guard verdict is unusable (no "
    ), message
    assert "Treat this write as UNGUARDED: it replaced the whole ATOC" in message
    assert message in text


# ── review round 1: relative build_dir (item 3) ──────────────────────────────


def test_a_relative_build_dir_names_the_tree_west_writes_to(tmp_path, monkeypatch):
    """`west flash` runs in the west topdir; tan's own cwd is elsewhere. The
    argv, runners.yaml, the stale unlink and the verdict must all name
    `<west topdir>/out/he`, never `<tan cwd>/out/he`."""
    _project(tmp_path)
    west_top = tmp_path / "westtop"
    _image(west_top / "out" / "he")
    elsewhere = tmp_path / "elsewhere"
    _image(elsewhere / "out" / "he", flash_runner="jlink")  # the wrong tree
    _manifest(tmp_path, [("m55_he", "app/zephyr/zephyr.bin", "{build_dir: out/he}")])
    stale = west_top / "out" / "he" / "alif_flash" / "atoc-guard.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps(_verdict("refused-foreign", ["OLD"])), encoding="utf-8", newline="\n")
    spawned = []
    _exit, _data, issues, _text, _sdk = _flash(
        tmp_path, monkeypatch, rc=1, replace_atoc=True, spawned=spawned,
        workspace=west_top, cwd=elsewhere,
        verdict=_verdict("replaced", ["A32_APP"]),
    )
    assert os.path.normpath(spawned[0][3]) == os.path.join(str(west_top), "out", "he"), spawned
    assert spawned[0][-1] == "--replace-atoc", spawned
    assert _codes(issues) == ["flash.entry-failed"], issues
    assert issues[0].message.endswith(
        "(ATOC guard: --replace-atoc overrode A32_APP; the write then failed, so whether "
        "A32_APP is still listed is unknown; the failure came after it)"
    ), issues[0].message


def test_an_unguarded_entry_keeps_its_relative_build_dir_on_the_argv(tmp_path, monkeypatch):
    """The absolutised `build_dir` exists so the guard's files and the argv name
    one tree; an entry the guard does not cover (another runner) keeps the
    manifest's own spelling on its `west flash` argv."""
    _project(tmp_path, flash_runner="jlink")
    west_top = tmp_path / "westtop"
    _image(west_top / "out" / "he", flash_runner="jlink")
    _manifest(tmp_path, [("m55_he", "app/zephyr/zephyr.bin", "{build_dir: out/he}")])
    spawned = []
    _flash(tmp_path, monkeypatch, spawned=spawned, workspace=west_top)
    assert spawned[0][spawned[0].index("--build-dir") + 1] == "out/he", spawned


def test_replace_atoc_with_ram_warns_that_it_had_no_effect(tmp_path, monkeypatch):
    from tan.commands import flash_ram

    _project(tmp_path)
    monkeypatch.setattr(
        flash_ram, "run_ram_entry",
        lambda target, ctx: (0, flash_cmd._Entry("slice", target.id, "ram", "ok", 0, "ran"), []),
    )
    spawned = []
    monkeypatch.setattr(flash_cmd, "_spawn", lambda argv, *_a, **_k: spawned.append(argv))
    _exit, _data, issues, text, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core="m55_he", helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
        replace_atoc=True, ram=True, confirm_flag=True,
    )
    ignored = [i for i in issues if i.code == "flash.replace-atoc-ignored"]
    assert len(ignored) == 1 and ignored[0].severity == "warning", issues
    assert ignored[0].message in text
    assert "flash.replace-atoc-ambiguous" not in _codes(issues)
    assert spawned == []


def test_ram_without_replace_atoc_stays_quiet(tmp_path, monkeypatch):
    from tan.commands import flash_ram

    _project(tmp_path)
    monkeypatch.setattr(
        flash_ram, "run_ram_entry",
        lambda target, ctx: (0, flash_cmd._Entry("slice", target.id, "ram", "ok", 0, "ran"), []),
    )
    _exit, _data, issues, _text, _sdk = flash_cmd._run(
        app_path=".", build_root_arg=None, sdk_root_arg=str(tmp_path / "sdk"),
        board_yaml=None, core="m55_he", helper=None, dry_run=False,
        skip_missing_tools=False, capture=True, cwd=str(tmp_path),
        ram=True, confirm_flag=True,
    )
    assert "flash.replace-atoc-ignored" not in _codes(issues)


# ── review round 1: sysbuild (item 6) ────────────────────────────────────────


def _sysbuild(tmp_path, domains):
    _project(tmp_path)
    sb = tmp_path / "build" / "sb"
    sb.mkdir(parents=True)
    rows = "".join(f"- name: {d}\n  build_dir: {(sb / d).as_posix()}\n" for d in domains)
    (sb / "domains.yaml").write_text(
        f"default: {domains[-1]}\nbuild_dir: {sb.as_posix()}\ndomains:\n{rows}", encoding="utf-8", newline="\n"
    )
    for d in domains:
        _image(sb / d)
    (sb / "merged.hex").write_bytes(b"\x00")
    _manifest(tmp_path, [("m55_he", "sb/merged.hex", f"{{build_dir: '{sb.as_posix()}'}}")])
    return sb


def test_a_single_domain_sysbuild_is_guarded_in_its_domain_dir(tmp_path, monkeypatch):
    sb = _sysbuild(tmp_path, ["app"])
    _exit, _data, issues, _text, _sdk = _flash(tmp_path, monkeypatch, rc=1)
    # The fake writes nothing; the guard looked in the DOMAIN dir.
    assert _codes(issues) == ["flash.entry-failed"], issues
    expected = os.path.join((sb / "app").as_posix(), "alif_flash", "atoc-guard.json")
    assert f"no {expected} was written for this attempt" in issues[0].message, issues[0].message


def test_a_multi_domain_sysbuild_says_why_the_flag_cannot_apply(tmp_path, monkeypatch):
    _sysbuild(tmp_path, ["mcuboot", "app"])
    spawned = []
    _exit, _data, issues, _text, _sdk = _flash(tmp_path, monkeypatch, replace_atoc=True,
                                               spawned=spawned)
    assert "--replace-atoc" not in spawned[0]
    assert _codes(issues) == ["flash.replace-atoc-not-applicable"], issues
    assert "is a sysbuild tree with 2 domains" in issues[0].message
    assert (
        "refuses a multi-domain sysbuild before its guard runs and leaves no verdict "
        "(alp-sdk#2274)"
    ) in issues[0].message


# ── review round 1: stale verdict that could not be removed (item 8) ─────────


def test_a_success_after_a_failed_stale_removal_says_the_verdict_is_unknown(tmp_path):
    from tan.commands import flash_atoc_guard

    def refuse(_path):
        raise PermissionError("denied")

    build_dir = tmp_path / "b"
    (build_dir / "alif_flash").mkdir(parents=True)
    (build_dir / "alif_flash" / "atoc-guard.json").write_text("{}", encoding="utf-8", newline="\n")
    stale = flash_atoc_guard.clear_stale_verdict(str(build_dir), remove=refuse)
    assert stale is not None and "could not be removed first (denied)" in stale
    message, warning, sections = flash_atoc_guard.guarded_success(
        "ok-msg", "m55_he", str(build_dir), stale, "src"
    )
    assert message.startswith("ok-msg The alif_flash ATOC guard verdict could not be read (")
    assert warning is None and sections == ()


# ── review round 1: `tan run` without --flash (item 7) ───────────────────────


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        ({"replace_atoc": True}, "--replace-atoc"),
        ({"atoc_unqueryable": True}, "--atoc-unqueryable"),
        ({"replace_atoc": True, "atoc_unqueryable": True}, "--replace-atoc and --atoc-unqueryable"),
    ],
)
def test_run_without_a_flash_says_the_atoc_flags_did_nothing(tmp_path, monkeypatch, flags, named):
    monkeypatch.setattr(run_cmd, "_build", lambda **_k: (ExitCode.SUCCESS, {}, []))
    monkeypatch.setattr(run_cmd, "decide_run_action", lambda *a, **k: RunAction.BUILD_ONLY)
    _exit, _data, issues, text = run_cmd._run(
        build_root=str(tmp_path), sdk_root=None, sdk_root_for_stamp=None, board_yaml=None,
        flash=False, core=None, json_mode=True, **flags,
    )
    expected = (
        f"run: {named} had no effect -- this run did not flash anything (they only act on "
        "a write, which needs --flash on a hardware target whose build succeeded)."
    )
    assert [(i.code, i.severity, i.message) for i in issues] == [
        ("run.flash-flags-ignored", "warning", expected)
    ]
    assert text[-1] == expected


def test_run_without_the_atoc_flags_adds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(run_cmd, "_build", lambda **_k: (ExitCode.SUCCESS, {}, []))
    monkeypatch.setattr(run_cmd, "decide_run_action", lambda *a, **k: RunAction.BUILD_ONLY)
    _exit, _data, issues, _text = run_cmd._run(
        build_root=str(tmp_path), sdk_root=None, sdk_root_for_stamp=None, board_yaml=None,
        flash=False, core=None, json_mode=True,
    )
    assert issues == []
