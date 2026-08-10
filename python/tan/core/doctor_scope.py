# SPDX-License-Identifier: Apache-2.0
"""The `scope` every `tan doctor` check carries on the wire (tan-cli#549).

A consumer rendering the doctor report has to split HOST rows from PROJECT
rows -- the dependency panel in `alp-sdk-vscode` shows the host half with no
folder open. Before this field the only handle was `checks[].name`, so the
split was a hand-written list of another program's identifiers. That list went
stale across two pin bumps and shipped `zephyrSdkHost`, an id this CLI stopped
emitting when the check was renamed `zephyrSdkAvailableForHost`; nothing
failed, the row was simply never admitted, and a missing row reads to a user
as "not a problem" rather than "not asked" (alp-sdk-vscode#472, patched
downstream in alp-sdk-vscode#487 with the caveat that a re-derived hand-list
rots again).

## The two values, and the rule that decides between them

The question is **what the check's verdict is ABOUT** -- its subject -- not
which inputs it happened to read on the way to that verdict.

`host`
    The subject is THIS MACHINE: a tool on PATH, an OS setting, the host
    interpreter, the home directory, whether the pinned Zephyr SDK publishes a
    build for this OS/arch. A project or SDK fact may refine the threshold or
    the wording (`hostPython`'s floor is the higher of the SDK manifest's and
    the workspace Zephyr's), but the thing being verdicted is the host, and
    the row is worth rendering with no folder open.

`project`
    The subject is the SELECTED PROJECT and the alp-sdk checkout / Zephyr
    workspace resolved for it: whether a `board.yaml` is there, whether a
    workspace resolved, whether that workspace's Zephyr matches the SDK's own
    pin, where this venv or SDK came from. With nothing selected these still
    report -- honestly, against "nothing selected" -- but they answer a
    question the user has not asked yet.

The one judgement call worth naming: `sdk` and `pythonFloor` are `project`.
`sdk`'s top resolution tier is the machine-global `~/.alp/sdk-default`, which
argues for `host`; its subject is still "which checkout will THIS project
build against", and its lower tiers (`.alp/sdk-path`, the positional walk) are
directory-dependent. `pythonFloor` reads as a host fact from its name, but
both its arms verdict a DECLARATION -- the SDK manifest's `pythonMinVersion`
against the workspace Zephyr's, or the absence of a manifest to read -- and
its remedy in the no-SDK arm is "resolve an alp-sdk checkout". The host-side
half of that pair is `hostPython`, which is `host`.

## Why the vocabulary lives here rather than beside `Check`

Nothing in this module imports typer or touches IO, so the gate that walks
every `Check(...)` call site (`python/tests/gates/test_doctor_check_scope.py`)
and `tan/commands/support_bundle_cmd.py`, which builds a doctor-shaped report
of its own, can both read the vocabulary without importing a command module.
"""
from __future__ import annotations

#: The check verdicts a fact about this machine. See the module docstring.
HOST = "host"

#: The check verdicts a fact about the selected project/SDK/workspace.
PROJECT = "project"

#: The complete wire vocabulary, in the order the contract documents it. A
#: value outside this tuple is refused at construction (`Check.__post_init__`)
#: rather than shipped: a consumer filtering on `scope` cannot tell an
#: unrecognised value from a row it should hide, which is the same fail-open
#: silence the name-matching seam had.
CHECK_SCOPES: tuple[str, ...] = (HOST, PROJECT)
