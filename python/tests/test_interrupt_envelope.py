# SPDX-License-Identifier: Apache-2.0
"""tan-cli#491 defect 4: Ctrl-C must not be reported as an invalid command line.

`KeyboardInterrupt` is a `BaseException` sibling of `Exception`, so every
command's own catch-all (`flash_cmd.py`'s `except Exception`, `build_cmd`'s
dispatch) is blind to it -- and it does NOT reach `tan.cli.main` raw either:
Typer catches it wherever in the call stack it was raised and re-raises
`Exit(130)`, which Click turns into a plain `sys.exit(130)`. So an interrupted
run used to be indistinguishable from any other non-zero exit that emitted no
envelope, and fell into the SAME `_usage_error_envelope` branch every genuine
Click usage error does.

Measured on `dev`, SIGINT 4s into a real `tan build --plan-from p.json
--execute --format json` whose one slice spawns `sleep 30`:

    EXIT= 130
    STDOUT= {"command":"cli","ok":false,"exitCode":130,...,"data":{"message":
             "invalid command line invocation"},"issues":[{"code":
             "cli.parse-error",...}]}
    STDERR= (empty)

-- an envelope asserting the COMMAND LINE was invalid for a run that was
already spawning, at an `exitCode` of 130, outside the contract's fixed 0-5
set. `flash` is the worst-consequence instance (an interrupted MRAM/eMMC write
loses its whole `data.entries[]`), but the defect is not flash-specific: every
command lands in the same handler, which is why the fix is there and not in any
one command.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tan.exit_codes import ExitCode

# `tan.cli`'s interrupt helpers are imported INSIDE the unit test below, not at
# module scope: the two subprocess cases assert observable behaviour that a
# pre-fix `tan/cli.py` gets wrong, and a module-level import of a name that
# does not exist there turns that into a collection ERROR -- which fails, but
# proves nothing about what the CLI did. Kept local so all three cases stay
# drivable against either version of the source.

#: `python/` -- see `test_wants_help_positions.py`'s copy of this note.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

#: How long the slice's child runs. It must outlive the interrupt below (so the
#: SIGINT lands with tan already inside `subprocess.run` -- the shape an
#: operator's Ctrl-C 20 s into a real flash/build actually takes) and it also
#: bounds each case's wall time: `build/execute.py` spawns its slice in its OWN
#: session, so the child does NOT receive the process-group SIGINT and keeps
#: tan's inherited stdout/stderr pipes open until it exits on its own, which
#: `communicate()` then waits out. Measured: at `30` each case took 30.2 s.
_CHILD_SECONDS = "10"

#: A plan whose single slice spawns that child. `appDir` must exist or the
#: slice fails before it ever spawns.
_PLAN = {
    "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml",
    "sku": "S", "buildRoot": "build", "sharedArtefacts": [], "warnings": [],
    "slices": [{
        "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1", "appDir": "app",
        "configArtefacts": [], "toolchain": None, "artifacts": {}, "debug": {},
        "command": {"tool": "sleep", "args": [_CHILD_SECONDS], "cwd": None},
        "env": {}, "envAppendPath": {},
    }],
}


def test_the_interrupt_envelope_is_coded_and_in_contract_range():
    """Portable half (the subprocess cases below are POSIX-only): the envelope
    itself. `cli.interrupted`, not `cli.parse-error`; `exitCode` inside
    `ExitCode`'s 0-5, not the raw 130 -- the wire invariant is `process exit
    code == envelope.exitCode` (tan-cli#327), so a 130 in the envelope would
    have to be a 130 out of the process too."""
    from tan.cli import _INTERRUPTED_MESSAGE, _interrupted_envelope

    envelope = json.loads(_interrupted_envelope())

    assert envelope["command"] == "cli", envelope
    assert envelope["ok"] is False, envelope
    assert envelope["exitCode"] == int(ExitCode.RUNTIME_FAILURE), envelope
    assert [i["code"] for i in envelope["issues"]] == ["cli.interrupted"], envelope
    assert envelope["data"]["message"] == _INTERRUPTED_MESSAGE, envelope
    assert envelope["exitCode"] in {int(c) for c in ExitCode}, envelope


def _interrupt(tmp_path, *argv):
    """Spawn a real `tan` in its own process group, let it get as far as
    spawning its slice, then SIGINT the whole group -- what a terminal's Ctrl-C
    does. In-process is not an option: Typer's interrupt handling and `main()`'s
    process boundary are exactly what is under test."""
    (tmp_path / "app").mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_PLAN), encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "tan", "build", "--plan-from", str(plan), "--execute", *argv],
        cwd=tmp_path, env=env, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(4.0)
    os.killpg(proc.pid, signal.SIGINT)
    stdout, stderr = proc.communicate(timeout=60)
    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX process groups + SIGINT")
def test_an_interrupted_json_run_answers_cli_interrupted(tmp_path):
    """The end-to-end case, at the real process boundary."""
    code, stdout, stderr = _interrupt(tmp_path, "--format", "json")

    envelope = json.loads(stdout)
    assert envelope["issues"][0]["code"] == "cli.interrupted", envelope
    assert "invalid command line invocation" not in stdout
    # The wire invariant, checked on the wire.
    assert code == envelope["exitCode"] == int(ExitCode.RUNTIME_FAILURE), (code, envelope)


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX process groups + SIGINT")
def test_an_interrupted_text_run_is_untouched(tmp_path):
    """TEXT mode keeps Click's own machinery, so an operator's shell still sees
    128+SIGINT and stdout stays empty -- the envelope remapping above applies
    only to the channel that promised an envelope."""
    code, stdout, stderr = _interrupt(tmp_path)

    assert code == 130, (code, stdout, stderr)
    assert stdout == "", stdout
