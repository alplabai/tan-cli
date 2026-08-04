# SPDX-License-Identifier: Apache-2.0
"""The shared spelling of "tan knows this, and it is not here YET".

**All seven verbs this module used to stub are now ported** (tan-cli#260:
`scaffold`, `completion`, `diff`, `pinmux`, `inspect`, `trace`,
`support-bundle`), so the stub factory and its `DEFERRED_VERBS` tuple are
gone. What remains is the two constants `build_cmd.py` still needs for the
deferred *flags* it declares -- `--plan`, `--target`, and friends, which are
real, working flags of the v0.4.1 oracle that this port does not implement yet
and refuses explicitly rather than as a typo.

**Why a declared refusal beats an absent one, for a flag exactly as for a
verb.** A name Typer has never heard of is a Click `UsageError`: exit 2,
`cli.parse-error` on the wire, and a message that reads exactly like a typo --
indistinguishable from `tan bulid`. That is a strictly worse signal than the
truth. A caller (or the extension) that greps for the issue code below, or the
issue URL in the message, can special-case "deferred" from "typo"
without a hardcoded list of its own.

**Exit code: `RUNTIME_FAILURE` (1), not `VALIDATION_FAILURE` (2), chosen
deliberately.** `VALIDATION_FAILURE` is what Click's `UsageError` already
returns for a truly unknown command/flag -- reusing it here would put the
"known but deferred" case back at the exact same exit code as the "typo" case
this module exists to distinguish it from, silently defeating the point.

**Issue code: one shared `cli.command-deferred`.** Every deferral reports
literally the same fact, so a caller that wants to special-case the situation
needs exactly one code to match, not one per site.
"""
from __future__ import annotations

#: Shared by every deferral -- see the module docstring's "Issue code" section.
DEFERRED_ISSUE_CODE = "cli.command-deferred"

#: The tan-cli issue tracking the deferred surface. Named in every message.
#:
#: #427, NOT #260. #260 tracked the seven verbs, all of which shipped in 0.5.0,
#: so it is closed -- and a refusal that says "see <closed issue about seven
#: commands that all work>" contradicts itself in front of the user. #427 is
#: the flags that are actually still deferred.
DEFERRED_ISSUE_URL = "https://github.com/alplabai/tan-cli/issues/427"

#: Accept (and silently discard) any positional/flag argv, so a caller's
#: existing arguments never turn into a SEPARATE parse-error ahead of the
#: deferral message.
DEFERRED_CONTEXT_SETTINGS = {"ignore_unknown_options": True, "allow_extra_args": True}
