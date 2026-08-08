# SPDX-License-Identifier: Apache-2.0
"""tan-cli#478: the foreign-global-default warning must reach EVERY command
that resolves through it, not the five that happened to be wired.

tan-cli#464 gave `~/.alp/sdk-default` a `writtenFor` field and taught
`doctor`/`generate`/`build`/`presets`/`examples` to warn when the pointer was
written for a DIFFERENT project. Measured against the shipped v0.5.1 binary,
five more commands resolved the same foreign checkout and said nothing:

    command          sdk.sourceTier   warned
    inspect          globalDefault    no
    trace            globalDefault    no
    validate         globalDefault    no
    diff             globalDefault    no
    support-bundle   globalDefault    no

The silence is not passive. `trace` PRINTS the `alp_project.py` a build would
run -- from the other project's checkout. `validate` SPAWNS that checkout's
`scripts/validate_board_yaml.py`, so another project's schemas, at whatever
revision it sits on, decide "clean" or "violation" for this board.yaml.
`diff` normalises through it. And `support-bundle` -- the one artefact a user
sends to someone else to explain a broken machine -- carried the fact
nowhere, not in `issues[]` and not in the reduced doctor set it embeds
(tan-cli#441 keeps host checks only).

The setup is the real two-project sequence, not a hand-written pointer file:
`test_build_command.py` proves it for `build`, and this replays it once and
asks all five. Writing the pointer by hand would prove only that the reader
works; bootstrapping twice proves the state a user actually reaches.

Queried from a SUBdirectory of project A, for the reason that test records:
a subdirectory carries no `.alp/sdk-path` pin, so it is the one location that
resolves through `globalDefault` -- the collision this warning exists for.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.commands.test_bootstrap_command import PRESENT_TOOL, codes, make_sdk
from tests.commands.test_bootstrap_command import run_tan as run_tan_with_env

#: The five tan-cli#478 names, each as the argv it is driven with. None takes
#: `--offline` on purpose: the whole point is the SDK-resolving path.
FOREIGN_DEFAULT_COMMANDS = (
    ("inspect", ("inspect",)),
    ("trace", ("trace",)),
    ("validate", ("validate",)),
    ("diff", ("diff",)),
    ("support-bundle", ("support-bundle",)),
)


def _envelope(proc):
    """The last stdout line, parsed. Matches `test_build_command.envelope_of`
    -- a command may print progress before its envelope."""
    line = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1]
    return json.loads(line)


@pytest.fixture(scope="module")
def two_projects(tmp_path_factory):
    """Project A bootstraps and relocates, then project B does -- same
    machine-global pointer, last writer wins. Module-scoped because the
    sequence runs two real `tan bootstrap` invocations and every case below
    asks the identical question of the identical state.

    `HOME`/`USERPROFILE` are redirected into the tmp tree: this test writes a
    `~/.alp/sdk-default`, and doing that to the developer's real home would
    repoint whatever they were working on -- the very defect tan-cli#464 is
    about, inflicted by its own test.
    """
    tmp_path = tmp_path_factory.mktemp("foreign-default")
    home = tmp_path / "shared-home"
    env_extra = {"HOME": str(home), "USERPROFILE": str(home)}

    sdk_a = make_sdk(tmp_path / "projA", tools=[PRESENT_TOOL])
    sdk_b = make_sdk(tmp_path / "projB", tools=[PRESENT_TOOL])
    proj_a, proj_b = sdk_a.parent, sdk_b.parent
    # tan-cli#302's trigger: a sibling file beside the clone is what makes the
    # dirty-parent guard auto-relocate each checkout.
    (proj_a / "unrelated.txt").write_text("x", encoding="utf-8")
    (proj_b / "unrelated.txt").write_text("x", encoding="utf-8")

    for sdk, cwd, who in ((sdk_a, proj_a, "A"), (sdk_b, proj_b, "B")):
        env = _envelope(
            run_tan_with_env(
                "bootstrap", "--no-west", "--no-pip", "--format", "json",
                "--sdk-root", str(sdk), cwd=cwd, env_extra=env_extra,
            )
        )
        assert env["exitCode"] == 0
        assert "bootstrap.workspace-relocated" in codes(env), (
            f"precondition unmet: {who}'s bootstrap must actually relocate"
        )

    pointer = json.loads((home / ".alp" / "sdk-default").read_text(encoding="utf-8"))
    # Posix on BOTH sides. tan writes `writtenFor` posix-normalised on every
    # platform; `str(proj_b)` is native, so on Windows this compared
    # `C:/Users/.../projB` against `C:\\Users\\...\\projB` and failed a
    # precondition that was actually met. The assertions below already
    # normalised -- this one was the omission, and it is exactly the
    # windows-only shape this repo keeps getting bitten by.
    assert pointer["writtenFor"].replace("\\", "/") == str(proj_b).replace("\\", "/"), (
        "precondition unmet: the shared pointer must name B, the last writer"
    )

    sub_a = proj_a / "sub"
    sub_a.mkdir()
    return sub_a, proj_b, proj_b / "alp-workspace" / sdk_b.name, env_extra


@pytest.mark.parametrize(
    "name,argv", FOREIGN_DEFAULT_COMMANDS, ids=[n for n, _ in FOREIGN_DEFAULT_COMMANDS]
)
def test_every_command_discloses_a_foreign_global_default(name, argv, two_projects):
    """Asked from inside project A, each command must name project B.

    The exit code is deliberately unasserted: from a bare subdirectory with no
    board.yaml, some of these succeed and some refuse, and #478 is about what
    the envelope DISCLOSES either way -- a refusal that hides which SDK it
    refused against is no better than a success that does.
    """
    sub_a, proj_b, new_sdk_b, env_extra = two_projects

    env = _envelope(run_tan_with_env(*argv, "--format", "json", cwd=sub_a, env_extra=env_extra))

    assert env["sdk"] is not None, f"precondition unmet: {name} reported no sdk block"
    assert env["sdk"]["sourceTier"] == "globalDefault", (
        f"precondition unmet: {name} did not resolve through the shared pointer"
    )
    assert env["sdk"]["root"] == str(new_sdk_b).replace("\\", "/"), (
        f"precondition unmet: {name} did not resolve B's relocated checkout"
    )
    assert "sdk.global-default-foreign-project" in codes(env), (
        f"DEFECT (tan-cli#478): {name} resolved another project's SDK silently"
    )
    message = next(
        i["message"] for i in env["issues"]
        if i["code"] == "sdk.global-default-foreign-project"
    )
    assert str(proj_b).replace("\\", "/") in message, (
        f"{name} warned, but did not name the project the pointer was written for"
    )


@pytest.mark.parametrize(
    "name,argv", FOREIGN_DEFAULT_COMMANDS, ids=[n for n, _ in FOREIGN_DEFAULT_COMMANDS]
)
def test_every_command_discloses_it_in_default_text_mode_too(name, argv, two_projects):
    """tan-cli#478 review finding 6: the JSON envelope carrying the pair is
    not enough on its own -- a diagnostic the customer cannot see is not a
    diagnostic, and the DEFAULT invocation (no `--format json`) is what a
    human actually runs. Same precondition as the parametrized JSON case
    above; asserts on `proc.stderr` instead of the envelope, with no
    `--format` flag at all.
    """
    sub_a, proj_b, _new_sdk_b, env_extra = two_projects

    proc = run_tan_with_env(*argv, cwd=sub_a, env_extra=env_extra)

    assert "machine-global default SDK" in proc.stderr, (
        f"DEFECT (tan-cli#478): {name}'s default text output did not disclose "
        f"the foreign global default:\n{proc.stderr}"
    )
    assert str(proj_b).replace("\\", "/") in proc.stderr, (
        f"{name}'s text output warned, but did not name the project the "
        f"pointer was written for:\n{proc.stderr}"
    )


def test_the_support_bundle_file_itself_records_it(two_projects):
    """The envelope is not enough for this one, and an earlier revision of this
    test only checked the envelope -- which was false assurance over a real
    miss (caught in review of #504).

    `support-bundle`'s whole purpose is the FILE. That file is what a user
    attaches to a bug report, so the fact explaining "my build used the wrong
    SDK" has to survive INTO it. The previous revision resolved
    `bundle_path`, asserted `is_file()`, and then asserted on `codes(env)` --
    the envelope, which the parametrized case above already covers -- so
    `bundle_path` was loaded and never opened. It would have passed with the
    payload change absent, which is exactly what happened.

    Measured on v0.5.1 and on the first revision of this PR: the string
    `global-default-foreign-project` appeared NOWHERE in the bundle.

    Reads the file. The bundle redacts the home directory (see
    support_bundle_cmd's REDACTION POLICY), which is why this asserts on the
    CODE -- redaction never touches it -- rather than on the project path,
    which may legitimately come back as `<home>/...`.
    """
    sub_a, _proj_b, _new_sdk_b, env_extra = two_projects

    env = _envelope(
        run_tan_with_env("support-bundle", "--format", "json", cwd=sub_a, env_extra=env_extra)
    )
    bundle_path = Path(env["data"]["outputPath"])
    assert bundle_path.is_file(), "support-bundle reported a path it did not write"

    written = json.loads(bundle_path.read_text(encoding="utf-8"))
    codes_in_file = [entry["code"] for entry in written.get("sdkResolution", [])]
    assert "sdk.global-default-foreign-project" in codes_in_file, (
        "DEFECT (tan-cli#478): the bundle FILE does not record which project's "
        f"SDK answered -- sdkResolution={written.get('sdkResolution')!r}"
    )
    # And the raw-string form of #478's own repro, so a future refactor that
    # renames the key without dropping the fact still has to keep it findable.
    assert "global-default-foreign-project" in bundle_path.read_text(encoding="utf-8")


def test_the_sdk_advisory_never_leaks_into_validates_board_documents(two_projects):
    """tan-cli#478 review: the pair belongs in the ENVELOPE only.

    `--format diagnostic-v1` and `--format sarif` are ported alp-sdk documents
    in which every entry is anchored at `board.yaml`, region 1:1. A CI job
    uploading the SARIF to code scanning would annotate line 1 of the
    customer's board.yaml with a fact about the HOST -- "the machine-global
    default SDK was last set by a bootstrap relocation in <other project>".

    `data.issueCount` is the same confusion in the JSON: it reads as "how many
    findings does this board have", and counting a host fact made a CLEAN
    board report `outcome: "clean"`, `exitCode: 0`, `issueCount: 1`.
    """
    sub_a, _proj_b, _new_sdk_b, env_extra = two_projects
    (sub_a / "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n", encoding="utf-8"
    )

    env = _envelope(run_tan_with_env("validate", "--format", "json", cwd=sub_a, env_extra=env_extra))
    assert "sdk.global-default-foreign-project" in codes(env), (
        "precondition unmet: this run must resolve the foreign pointer"
    )
    assert env["data"]["issueCount"] == sum(
        1 for i in env["issues"] if not i["code"].startswith("sdk.")
    ), "issueCount counted a host advisory as a board finding"

    for fmt in ("sarif", "diagnostic-v1"):
        proc = run_tan_with_env("validate", "--format", fmt, cwd=sub_a, env_extra=env_extra)
        assert "global-default-foreign-project" not in proc.stdout, (
            f"--format {fmt} anchored a host fact at the customer's board.yaml"
        )


def test_support_bundles_early_return_paths_still_disclose_it(two_projects):
    """PR #504 review MAJOR 1: `support-bundle`'s early-return failure paths
    (`_internal_failure`/`_server_incompatible`) built their `_Outcome` from a
    bare `issues=[Issue(...)]` list, dropping the SDK-resolution pair on
    exactly the runs a customer hits when something has already gone wrong --
    a `--target-kind`/`--server` parse refusal or an unsupported target/server
    pairing. Drives both shapes from inside the same foreign-global-default
    state the parametrized case above proves for the success path.
    """
    sub_a, proj_b, _new_sdk_b, env_extra = two_projects

    parse_failure = _envelope(
        run_tan_with_env(
            "support-bundle", "--target-kind", "bogus", "--format", "json",
            cwd=sub_a, env_extra=env_extra,
        )
    )
    assert parse_failure["exitCode"] == 5
    assert "support-bundle.internal-failure" in codes(parse_failure)
    assert "sdk.global-default-foreign-project" in codes(parse_failure), (
        "DEFECT: the --target-kind parse-refusal path dropped the SDK-"
        "resolution pair"
    )

    server_incompatible = _envelope(
        run_tan_with_env(
            "support-bundle", "--target-kind", "yocto-userspace", "--server", "jlink",
            "--format", "json", cwd=sub_a, env_extra=env_extra,
        )
    )
    assert server_incompatible["exitCode"] == 4
    assert "support-bundle.server-compatibility" in codes(server_incompatible)
    assert "sdk.global-default-foreign-project" in codes(server_incompatible), (
        "DEFECT: the server-incompatibility refusal path dropped the SDK-"
        "resolution pair"
    )
    for env in (parse_failure, server_incompatible):
        message = next(
            i["message"] for i in env["issues"]
            if i["code"] == "sdk.global-default-foreign-project"
        )
        assert str(proj_b).replace("\\", "/") in message


def test_a_missing_board_yaml_keeps_its_own_verdict_wording(two_projects):
    """tan-cli#350's wording, which the first revision of this PR regressed.

    That branch keyed off `len(issues) == 1`, so prepending the SDK advisory
    pushed it to 2 and control fell through to the generic line. The result:
    `validate: validation failure` -- "a VERDICT that implies something was
    checked and found wrong. Nothing was validated." -- printed for an empty
    directory, exactly what #350 removed.
    """
    sub_a, _proj_b, _new_sdk_b, env_extra = two_projects
    empty = sub_a.parent / "no-board-here"
    empty.mkdir(exist_ok=True)

    proc = run_tan_with_env("validate", cwd=empty, env_extra=env_extra)

    assert "no board.yaml to validate" in proc.stderr, proc.stderr
    assert "validation failure" not in proc.stderr, (
        "tan-cli#350's wording regressed: nothing was checked, so nothing was "
        "found wrong"
    )


#: The two `west`-forwarding verbs whose child declares a REQUIRED flag
#: (tan-cli#454), as the bare argv a customer actually types. Both refuse
#: BEFORE `west` is ever spawned, so neither needs a real toolchain here --
#: which is exactly why the refusal is the path that must still disclose.
REQUIRED_FLAG_REFUSALS = (
    ("quality", ("quality",)),
    ("migrate", ("migrate",)),
)


@pytest.mark.parametrize(
    "name,argv", REQUIRED_FLAG_REFUSALS, ids=[n for n, _ in REQUIRED_FLAG_REFUSALS]
)
def test_a_required_flag_refusal_discloses_it_in_default_text_mode_too(
    name, argv, two_projects
):
    """PR #504 review: `_refuse_required`'s text branch printed
    `f"{subcommand}: {message}"` and NOTHING ELSE, while its `--format json`
    branch disclosed the pair through `Envelope.__init__`'s seam.

    This is not an exotic path. `--profile` (`quality`) and one-of
    `--check`/`--preview`/`--apply` (`migrate`) are REQUIRED by the child's
    own argparse, so a bare `tan quality` / `tan migrate` -- the first thing
    anyone types -- lands here, and `_plan` would have run `west` out of the
    OTHER project's checkout. Measured against this same two-project fixture
    before the fix:

        quality: `--profile` is required (`west alp-quality --profile ...`).
        # ... and nothing else on stderr, while --format json carried
        # ['quality.profile-required', 'sdk.global-default-foreign-project']

    Both directions are asserted here: the text path must say it, and the
    JSON path must keep saying it, so a future "just print it in text" fix
    that drops the envelope copy fails too.
    """
    sub_a, proj_b, _new_sdk_b, env_extra = two_projects

    proc = run_tan_with_env(*argv, cwd=sub_a, env_extra=env_extra)

    assert proc.returncode == 2, proc.stderr
    assert "machine-global default SDK" in proc.stderr, (
        f"DEFECT (tan-cli#478): `tan {name}`'s required-flag refusal ran in "
        f"DEFAULT text mode and never disclosed the foreign global default "
        f"it resolved:\n{proc.stderr}"
    )
    assert str(proj_b).replace("\\", "/") in proc.stderr, (
        f"`tan {name}` warned, but did not name the project the pointer was "
        f"written for:\n{proc.stderr}"
    )

    env = _envelope(run_tan_with_env(*argv, "--format", "json", cwd=sub_a, env_extra=env_extra))
    assert env["sdk"]["sourceTier"] == "globalDefault", (
        "precondition unmet: the refusal did not resolve through the shared pointer"
    )
    assert "sdk.global-default-foreign-project" in codes(env), (
        f"`tan {name}`'s JSON refusal stopped disclosing it"
    )


#: Commands that cannot be driven bare in this fixture: they need an argument,
#: a built project, hardware, or they mutate the very pointer under test.
#: Everything else is enumerated from the CLI itself, so command 33 is covered
#: the day it is registered.
NOT_DRIVABLE_BARE = frozenset(
    {
        "bootstrap",     # rewrites ~/.alp/sdk-default -- the state under test
        "init",          # writes the project pin, same reason
        "new-som",       # would scaffold into the SDK checkout
        "flash",         # programs hardware
        "monitor",       # opens a serial port
        "completion",    # emits a shell script, no envelope
        "faultdecode",   # no project, no SDK
        "west",          # forwards argv to a real west
    }
)


def test_no_command_reports_a_foreign_global_default_without_saying_so(two_projects):
    """The invariant, enumerated from `cli.py` rather than a hand-written list.

    tan-cli#478 asks for the warning "from one place, so a new command cannot
    forget it". A hardcoded tuple of command names cannot express that: it is
    exactly what the 33rd command would not be added to. This walks every
    registered subcommand instead, so a new one is covered the day it lands.

    The assertion is deliberately one-directional and cheap: no envelope may
    report `sdk.sourceTier == "globalDefault"` without the warning in
    `issues[]`. A REFUSAL must carry it too -- knowing which checkout answered
    matters most when the answer was no -- so no command needs a working build
    fixture to be meaningful here.

    Commands that cannot be driven bare are named in `NOT_DRIVABLE_BARE` with
    the reason, never skipped silently.
    """
    import tan.cli as cli_module

    sub_a, _proj_b, _new_sdk_b, env_extra = two_projects
    registered = sorted(
        {
            info.name or getattr(info.callback, "__name__", "").replace("_", "-")
            for info in cli_module.app.registered_commands
        }
    )
    assert registered, "could not enumerate the CLI's registered commands"

    offenders = []
    checked = []
    for name in registered:
        if name in NOT_DRIVABLE_BARE:
            continue
        proc = run_tan_with_env(name, "--format", "json", cwd=sub_a, env_extra=env_extra)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if not lines:
            continue  # no envelope on stdout (usage error) -- nothing to assert
        try:
            env = json.loads(lines[-1])
        except json.JSONDecodeError:
            continue
        sdk = env.get("sdk")
        if not sdk or sdk.get("sourceTier") != "globalDefault":
            continue
        checked.append(name)
        if "sdk.global-default-foreign-project" not in codes(env):
            offenders.append(f"{name} (exit {env.get('exitCode')})")

    # A vacuous pass is the failure mode this whole file exists to prevent: if
    # no command reached `globalDefault`, the loop above asserts nothing while
    # reporting green. Measured on the fixture -- keep this floor honest rather
    # than trusting the loop ran.
    assert len(checked) >= 5, (
        f"only {len(checked)} command(s) reached globalDefault: {checked}. "
        "The invariant asserted almost nothing -- fix the fixture, not this bound."
    )
    assert offenders == [], (
        "these commands resolved ANOTHER project's SDK through the machine-global "
        "pointer and said nothing about it:\n  " + "\n  ".join(offenders)
    )
