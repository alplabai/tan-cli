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
    assert pointer["writtenFor"] == str(proj_b), (
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
    """The envelope alone is not enough for this one. `support-bundle`'s whole
    purpose is the FILE, and that file is what gets attached to a bug report
    -- so the fact that explains "my build used the wrong SDK" has to survive
    into the run that produces it.

    Measured on v0.5.1 before the fix: the string
    `global-default-foreign-project` appeared nowhere in the bundle at all.
    """
    sub_a, _proj_b, _new_sdk_b, env_extra = two_projects

    env = _envelope(
        run_tan_with_env("support-bundle", "--format", "json", cwd=sub_a, env_extra=env_extra)
    )
    bundle_path = Path(env["data"]["outputPath"])
    assert bundle_path.is_file(), "support-bundle reported a path it did not write"
    # Assert on the CODE, not the project path: the bundle redacts the home
    # directory (support_bundle_cmd's REDACTION POLICY), so a legitimate path
    # can come back as `<home>/...` while the code never changes.
    assert "sdk.global-default-foreign-project" in codes(env)
