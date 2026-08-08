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
