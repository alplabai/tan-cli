# SPDX-License-Identifier: Apache-2.0
"""`tan validate` -- is this `board.yaml` structurally sound?

Two paths, mirroring `crates/tan-cli/src/commands/validate.rs`:

* ``--offline`` runs only the structural checks that ship inside tan. No SDK
  checkout, no subprocess, no network. This is the path the two committed
  conformance fixtures exercise, which is why their ``data.commandLine`` is
  ``""``.
* without ``--offline`` the real validator is the SDK's own
  ``scripts/validate_board_yaml.py``, spawned as a subprocess. tan does not
  reimplement alp-sdk's schema: the SDK owns ``metadata/schemas/`` and
  ADR-0017's doctrine is to consume what exists. **PORTED (tan-cli#376)** --
  see "the spawn path" below. Until #376 this branch refused with
  ``validate.spawn-not-implemented`` at exit 2, which made the DEFAULT
  invocation the root quickstart documents (``tan validate``) incapable of
  validating anything, and -- because exit 2 is also the genuine
  validation-failure class -- indistinguishable from a bad board to any
  caller that does not read the issue code.

  **tan-cli#262 (v0.6.0, TAKEN): a missing verdict is the VALIDATOR's
  problem, not a tan crash -- exit 2, not exit 1.** An earlier revision of
  this docstring left that an open question ("the genuine v0.6.0 decision,
  tracked in tan-cli#262"); the maintainer has now decided it. Measured
  directly, running ``target/debug/tan.exe`` (tan 0.4.1-dev) with
  ``--format json``:

  - empty directory -> exit **2**, ``validate.board-yaml-missing``
  - ``board.yaml`` present, no SDK root -> exit **2**,
    ``validate.sdk-root-unresolved``

  Exit 1 is reachable in the oracle only AFTER a validator actually spawns and
  returns an exit status the resolver cannot map to a named outcome
  (``Outcome::Failed``, see the table below) -- the oracle's own
  ``validation_outcome_exit_code`` (`crates/tan-cli/src/commands/validate.rs:
  54-62`) sends that ONE case to ``RuntimeFailure`` (1) while every other
  non-clean outcome already goes to ``ValidationFailure`` (2). alp-sdk-vscode
  renders exit 2 as severity "warning" and exit 1 as "error", so that one
  oracle case alone painted a genuinely-failing project red in the IDE --
  indistinguishable from `tan` itself crashing.

  This port deliberately does NOT mirror that one oracle case. "The validator
  could not produce a verdict" is still the validator's verdict, in every
  shape it takes here -- GENERALLY, and now that #376 has landed the spawn
  path, that generality is what carries the decision: every reachable
  ``OUTCOME_FAILED`` (an exit status outside the 0-3 range the resolver
  names, a validator that crashed with a traceback, a validator that ran past
  [`VALIDATOR_TIMEOUT_S`]) emits ``ExitCode.VALIDATION_FAILURE`` (2), not
  ``RuntimeFailure`` (1) -- do not "fix" that back to oracle parity; that
  parity is the bug #262 fixes.
  A genuine `tan`-side crash is unaffected by this decision and keeps its own
  exit code: a file that could not be read, or an unexpected exception that
  escaped tan's own code, stays ``ExitCode.INTERNAL_FAILURE`` (5, the two
  cases already implemented below); the spawn-LAUNCH I/O error specifically
  (the subprocess could not even be started -- no interpreter on PATH, the
  script unreadable) is ``validate.spawn-failed`` at
  ``ExitCode.RUNTIME_FAILURE`` (1) -- the "generic runtime failure (e.g. I/O
  or subprocess error)" `crates/tan-cli/src/exit.rs` itself names that code
  for, and the ONE case #262's own text carved out for it. Only the
  validator's-verdict exit code moves; tan's own crash exit codes do not.
  A timeout is deliberately on the other side of that line: the child DID
  start, so it is a verdict that never arrived (2), not a launch failure (1).

  Exit 5 is wrong on every one of these paths regardless: reporting a
  validator that could not answer as ``InternalFailure`` would tell CI/the
  extension this is a tan crash, the same mistake the Rust comment on the
  offline path (below) was written to stop repeating.

  Re-measure before changing any of this. Do not infer the oracle's behaviour
  from a report, a docstring, or ``crates/`` -- run the binary.

  **The validator-exit-status -> outcome map**, measured on the oracle
  (``target/debug/tan.exe``) with a contrived SDK (``scripts/alp_project.py``
  marker + ``scripts/validate_board_yaml.py`` exiting a chosen code),
  ``board.yaml`` present, ``--sdk-root`` pointed at it, no ``--offline``:

  =============== =============================== ===
  validator exit   outcome                          rc
  =============== =============================== ===
  0                clean                            0
  1                schema-violation                 2
  2                missing-preset                    2
  3                hardware-revision                2
  5, 77            failed                            1
  =============== =============================== ===

  So ``validate.failed`` is specifically the "anything outside the 0-3 range
  the resolver maps by number" case -- NOT every non-clean status, and in
  particular not exit 2 or 3, which have their own named outcomes. A reader
  must not infer "any nonzero -> failed" from this docstring. The ``rc``
  column above is the ORACLE's, measured, and stays 1 for the ``failed`` row
  -- that is a fact about ``target/debug/tan.exe``, not a decision, and must
  not be edited to "fix" the table. This port's OWN rc for that row is 2, per
  tan-cli#262 above -- a divergence recorded in prose here rather than in the
  table because the table is a record of what was measured, not of this
  port's choices.

  ``validate.spawn-not-implemented`` is GONE as of #376 (the branch that
  emitted it is), and its ``contract/issue-codes.json`` entry deleted rather
  than marked ``retired``: it was ``reserved``/``consumer: none``, which the
  registry's own header defines as the state where "renaming or dropping it
  costs nothing on the wire", and it never existed in the Rust CLI at all, so
  no pinned v0.4.1 binary can have taught a consumer to expect it.

**The spawn path, as ported.** Three guards, then the subprocess, then the
child's stderr mapped into the envelope:

* ``board.yaml`` must exist -- ``validate.board-yaml-missing``, checked
  FIRST and on both paths (see the guard's own comment).
* the alp-sdk checkout must resolve -- ``validate.sdk-root-unresolved``,
  through [`resolve_sdk_root_ladder`], the NARROW ladder the oracle resolves
  ``validate`` with (that helper's own docstring names `validate` in the
  thirteen). This is why ``--sdk-root`` is now READ rather than merely
  accepted: an earlier revision of the option's help said "declared, not
  consumed ... an arbitrary ``--sdk-root`` value changes nothing in the
  envelope", which was true only because the oracle re-validates the flag
  against the loader marker and an ARBITRARY path resolves to nothing.
  ``resolve_sdk_root_ladder`` does NOT re-validate it -- it returns an
  explicit flag verbatim (I-31, terminal-for-REPORTING) -- so this command
  applies tan-cli#257/#258's own guard at the call site, exactly as
  `build_cmd` and `run_cmd` do. Without it a ``--sdk-root`` pointing at an
  empty directory reached the SPAWN, whose child died with ``python.exe:
  can't open file '...\\scripts\\validate_board_yaml.py'``, exit 2 -- which
  this command's status map reads as ``missing-preset``. MEASURED before the
  fix: tan answered ``validate.missing-preset`` and named the customer's
  BOARD for the operator's bad flag; the oracle answers
  ``validate.sdk-root-unresolved`` on that same input, with no ``sdk`` block.
* the interpreter must be new enough to run the SDK scripts --
  ``validate.python-too-old``, the oracle's own guard 3
  (`crates/tan-cli/src/commands/validate.rs:124-129`, message body at
  `crates/tan-cli/src/util.rs:192-202`). The scripts use
  ``@dataclass(slots=True)``, so an older interpreter dies with a cryptic
  ``dataclass() got an unexpected keyword argument 'slots'`` from inside
  alp-sdk instead of an actionable refusal. TWO deliberate divergences, both
  the port's established shape for this same guard elsewhere: the floor is
  the resolved SDK's OWN declared ``metadata/bootstrap.json``
  ``pythonMinVersion`` (`doctor_cmd.resolve_manifest_python_floor`, I-24)
  rather than the oracle's compiled-in ``MIN_PYTHON = (3, 10)`` that can
  drift from it, and the interpreter probed is [`_planner_python`]'s -- the
  one that will actually be spawned two lines later -- not the bare PATH
  ``python``/``python3`` the oracle probes and then also spawns. Both are
  `generate_cmd`/`model_cmd`'s decisions, and this reuses
  `generate_cmd._python_too_old` rather than becoming a fourth copy of it.

The argv is the oracle's, verbatim --
``<python> <sdk>/scripts/validate_board_yaml.py --input <board>`` -- and
``data.commandLine`` reports exactly that string (the offline path, and every
one of the three guards, keep reporting ``""``, which the two committed
conformance fixtures pin).

**The ``sdk`` block.** ``sdk: {root, sourceTier}`` is emitted on the spawn
path whenever a checkout actually resolved, and OMITTED (absent, never null)
otherwise. Measured across every reachable shape: present with
``"sourceTier":"sdkRootFlag"`` for a valid ``--sdk-root`` and with
``"discovery"`` for a sibling checkout -- including on the
``board-yaml-missing`` refusal, where the SDK resolved and the board did not
-- and absent for ``--offline`` (with or without a resolvable checkout, and
with or without ``--sdk-root``), for the unresolved-SDK guard, and for a
``--sdk-root`` that failed the marker check. So the rule is not "the spawn
path reports an SDK" but "the envelope reports whatever SDK this run
resolved", and ``--offline`` resolves none by construction -- it has no
subprocess to point anywhere.

The interpreter is [`_planner_python`], NOT the bare ``python3``/``python``
the oracle hardcodes: the child imports ``jsonschema``/``PyYAML``/
``colorama``, which live in the workspace venv `tan bootstrap` creates, and
a bare PATH interpreter without them exits 1 with a traceback -- the exact
collision `is_interpreter_crash` exists to disambiguate. Preferring the venv
does not remove that guard (the venv can still be broken), it just stops the
common case from taking it.

Two hardenings the oracle does not have, both load-bearing:

* **A timeout.** ``Command::output()`` has none, so an SDK validator that
  wedges wedges `tan` -- and under ``--format json`` a consumer then gets no
  envelope at all, which is indistinguishable from a slow project. Bounded by
  [`VALIDATOR_TIMEOUT_S`]; the timeout is a ``failed`` outcome (exit 2), see
  the #262 paragraph above.
* **ANSI stripping** (`_ANSI_RE`) -- defence, NOT the load-bearing repair an
  earlier revision of this docstring claimed. That revision stated as
  MEASURED fact that the child writes ``\x1b[31merror[ALP-B005]\x1b[0m: ...``
  to stderr and that "the oracle therefore drops every rich diagnostic it
  spawned the validator to collect". Re-measured, per this module's own rule,
  and the two halves came apart:

  - The PARSER half is true. Driven through the oracle with a stand-in
    validator writing one ANSI-wrapped and one plain ``error[ALP-B*]`` header
    on the same run, only the plain one reached ``issues[]``:
    ``parse_rich_header`` anchors on the line START, so an escape in front of
    ``error`` costs the whole block.
  - The PREMISE is false. ``validate_board_yaml.py:43`` does render with
    ``color=not args.no_color`` and ``alp_cli.diagnostic._use_color(True)``
    does return True unconditionally -- but ``alp_cli.diagnostic`` calls
    ``colorama.init()`` at import, which replaces ``sys.stderr`` with an
    ``AnsiToWin32`` wrapper that STRIPS SGR escapes when the stream is not a
    tty. Every spawn -- the oracle's and this one -- gives the child a pipe.
    Captured bytes from a real ``render(..., color=True)`` through one:
    ``b'error[ALP-B005]: msg\r\n  --> board.yaml:1:1\r\n   |\r\n...'``, no
    ``\x1b`` anywhere. The colorama-ABSENT branch paints through a ``_Stub``
    whose ``__getattr__`` returns ``""``, so it emits no escapes either.
    There is no third branch, so the oracle does not in fact drop the real
    SDK's rich diagnostics.

  Kept anyway, at one regex per line: it covers exactly the conditions this
  reasoning rests on ceasing to hold -- a renderer that stops going through
  colorama, a ``FORCE_COLOR``-style override, a caller that hands the child a
  pty. Still NOT "fixed" by passing ``--no-color``, for the unchanged reason:
  an older SDK without that flag would argparse-error to exit 2, which this
  command's own status map reads as ``missing-preset`` -- a wrong verdict is
  worse than an escape sequence.

``Severity::Suggestion`` (the oracle's severity for a standalone ``hint:``
line) is emitted here as ``note``. The oracle has no ``--format
diagnostic-v1``/``sarif``; this port does, and both vocabularies are
``error|warning|note`` (``diagnostic-v1.schema.json``'s severity enum,
`_SARIF_LEVEL`). A literal ``"suggestion"`` would emit a document that fails
alp-sdk's own schema and raise ``KeyError`` on the SARIF path.

``tan_core::validate::is_summary_line`` is deliberately NOT ported: in the
oracle its branch (``i += 1; continue``) is byte-identical to the loop's own
fall-through (``i += 1``), so it classifies nothing -- an unmatched line is
skipped either way. Porting it would be porting a no-op.

**A wrong-shaped board.yaml is the USER's problem, not a tan crash.** The Rust
carries a comment earned the hard way: routing a malformed file through
``InternalFailure`` (exit 5) "told CI/the extension this was a tan crash" and
disagreed with the spawn path, which reports the identical file as exit 2
``schema-violation``. So a file that parses as YAML but does not fit the model
is a *validation* failure -- exit 2 -- and the one place in this port where
``ValidationFailure`` is genuinely the right code. (Contrast `tan build`, where a
malformed *plan* is exit 1: the consumer renders 2 as a warning and 1 as an
error, and an unbuildable plan is not a warning.)

The structural checks are deliberately about the CONTRACT's shape -- is there a
top-level ``os:``, is there a ``cores:`` block -- never about hardware. No SKU
list, no addresses, no pin names live here: the OS is derived from each core's
Cortex class by the SDK planner and is never selectable, so tan has no business
knowing which SKUs exist.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.commands.build_cmd import _is_sdk_root, _planner_python, resolve_sdk_root_ladder
from tan.commands.doctor_cmd import resolve_manifest_python_floor

# `_python_too_old` is IMPORTED, not re-spelled: `generate_cmd` and `model_cmd`
# already carry this same guard for this same reason (a spawned SDK script that
# needs `@dataclass(slots=True)`), and the whole point of guard 3 is that a
# floor and a message duplicated per command drift apart per command.
# `model_cmd`'s copy is that drift already begun -- its message dropped the
# oracle's own "(VS Code users can instead set alpSdk.pythonPath)" sentence. A
# third copy here would make it the pattern.
from tan.commands.generate_cmd import _python_too_old
from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS
from tan.core.global_flags import accept_global_flags
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, ValidateOutputFormat
from tan.version import TAN_VERSION

#: `data.schemaVersion` for this command's payload -- the envelope payload's own
#: version, unrelated to `board.yaml`'s `schemaVersion:`.
DATA_SCHEMA_VERSION = "1"

#: Seconds the SDK validator may run before it is killed. Generous for the same
#: reason `generate_cmd.EMIT_TIMEOUT_S` is (a cold run imports the whole
#: orchestrator package and reads every metadata file for the SKU), and bounded
#: for the reason the oracle's unbounded `Command::output()` is not: a wedged
#: child under `--format json` produces no envelope at all, which a consumer
#: cannot tell from a slow project. Read through the module at call time, so a
#: test can shorten it.
VALIDATOR_TIMEOUT_S = 300

#: `<sdk>/scripts/validate_board_yaml.py` -- the script the oracle spawns
#: (`crates/tan-cli/src/commands/validate.rs::run_spawn`). NOT
#: `python -m alp_cli.main validate`: that needs `<sdk>/scripts` on PYTHONPATH,
#: while a script path puts its own directory on `sys.path[0]` for free.
VALIDATOR_SCRIPT = ("scripts", "validate_board_yaml.py")

# The `--format` values this command accepts are declared ONCE, as
# `tan.output_format.ValidateOutputFormat` (tan-cli#403) -- not as a literal
# tuple here plus a hand-written membership check in [`validate`]'s body,
# which is how `--help`, `cli.py`'s root callback and all three
# shell-completion scripts came to name only `text` and `json` while this
# command really accepted four: nothing connected them to the list, so the two
# IDE-oriented formats stayed reachable but undiscoverable.
#
# `diagnostic-v1` and `sarif` are NOT part of the shared
# `Format::Text`/`Format::Json` enum every other command mirrors from
# `crates/tan-cli/src/cli.rs` -- there is no Rust precedent for them yet. They
# exist so alp-sdk's quality gate (`scripts/check_diagnostic_schema.py`) can
# point at `tan` instead of spawning
# `python -m alp_cli.main validate --format json`.
#
# WARNING -- `--format json` does NOT mean the same thing in the two CLIs.
# In alp-sdk (`scripts/alp_cli/validate.py:20,34`), `--format json` IS the
# diagnostic-v1 document. In tan, `--format json` is the envelope
# (`{command,ok,exitCode,project,data,issues}`; see `_emit` below) and the
# diagnostic-v1 document moved to its own `--format diagnostic-v1`. The
# obvious find-and-replace swap -- pointing the gate's argv at `tan` while
# keeping `"--format", "json"` -- validates the envelope against
# `diagnostic-v1.schema.json` (whose root is `additionalProperties: false`)
# and fails with something like "Additional properties are not allowed
# ('command', 'ok', 'exitCode', ...)", which names nothing about the real
# cause. The gate-side edit `check_diagnostic_schema.py` needs is therefore
# NOT a repoint of the same argv -- it must spawn
# `tan validate --offline --format diagnostic-v1`, not
# `tan validate --offline --format json`. `--format sarif` keeps its name
# unchanged in both CLIs, so only the `json` case silently diverges.

#: `diagnostic-v1.schema.json`'s own `schemaVersion` -- an integer `const: 1`,
#: unrelated to `DATA_SCHEMA_VERSION` above (a different document, a different
#: field, a different type).
_DIAGNOSTIC_SCHEMA_VERSION = 1

_SARIF_SCHEMA_URI = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/"
    "sarif-schema-2.1.0.json"
)

#: Outcome strings, verbatim from `tan_core::validate::Outcome::as_str`. The
#: issue code is `validate.<outcome>`, so these strings are wire contract.
OUTCOME_CLEAN = "clean"
OUTCOME_SCHEMA_VIOLATION = "schema-violation"
OUTCOME_MISSING_PRESET = "missing-preset"
OUTCOME_HARDWARE_REVISION = "hardware-revision"
OUTCOME_FAILED = "failed"

#: Validator exit status -> outcome, verbatim from
#: `tan_core::validate::classify_validation_outcome`. Anything else -- and a
#: `None` status (killed, never started) -- is `OUTCOME_FAILED`. Today's
#: `validate_board_yaml.py` only ever exits 0 or 1; 2 and 3 are kept because
#: they are the shared vocabulary the TS/extension side classifies by, not
#: because this SDK reaches them.
_STATUS_OUTCOME = {
    0: OUTCOME_CLEAN,
    1: OUTCOME_SCHEMA_VIOLATION,
    2: OUTCOME_MISSING_PRESET,
    3: OUTCOME_HARDWARE_REVISION,
}

#: `Issue.severity` -> SARIF `level`, mirroring
#: `scripts/alp_cli/diagnostic_format.py:_SARIF_LEVEL`. A deliberate explicit
#: table, not a passthrough: an `Issue.severity` outside this set must raise
#: (KeyError) rather than silently emit a SARIF log whose `level` is not one
#: of the spec's three values. The spawn path now feeds it `warning` and
#: `note` as well as `error` -- see the module docstring on why the oracle's
#: fourth severity, `suggestion`, is emitted as `note` here.
_SARIF_LEVEL = {"error": "error", "warning": "warning", "note": "note"}

#: SGR escapes only -- everything `alp_cli.diagnostic.render` can emit through
#: colorama's `Fore`/`Style`. See the module docstring: the oracle's own spawn
#: leaves these in and then fails to parse its own child's output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

#: `^(error|warning|note)\[(ALP-[A-Z]\d+)\]:\s*(.+)` -- the header of a rich
#: diagnostic block (`tan_core::validate::parse_rich_header`). `(\S.*)` rather
#: than `(.+)` so a header whose message is whitespace-only does not match,
#: matching the Rust `if !message.is_empty()` after its `trim_start`.
_RICH_HEADER_RE = re.compile(r"^(error|warning|note)\[ALP-[A-Z][0-9]+\]:\s*(\S.*)$")

#: `^\s+-->\s+\S+:(\d+):(\d+)` -- the location arrow. The trailing
#: `(?:\s|$)` reproduces `is_arrow_line`'s `rsplitn(3, ':')`: `a:1:2:xyz` is
#: NOT an arrow there (its last field is not numeric), and without the anchor
#: this regex would match its `a:1:2` prefix and disagree.
_ARROW_RE = re.compile(r"^\s+-->\s+\S+:[0-9]+:[0-9]+(?:\s|$)")

#: `^\s+[|0-9^= ]` -- an indented source/underline/hint continuation line.
_BLOCK_CONTINUATION_RE = re.compile(r"^\s[\s|^=0-9]")

#: `^(FAIL|WARN)\s+(.+)` -- the legacy line shape `validate_board_yaml.py`
#: still emits for its consistency failures (`FAIL consistency: ...`).
_FAIL_WARN_RE = re.compile(r"^(FAIL|WARN)\s+(\S.*)$")

#: `^\s{2,}\S` -- an indented continuation of a FAIL/WARN line.
_FAIL_CONTINUATION_RE = re.compile(r"^\s{2,}\S")

#: `^\s*(hint|suggestion|suggest):` (case-insensitive).
_HINT_RE = re.compile(r"^\s*(?:hint|suggestion|suggest):", re.IGNORECASE)

#: The header of an unhandled interpreter/environment crash. The validator
#: exits 1 both for a genuine schema violation AND when it dies importing
#: `jsonschema`, so this is the only thing that tells a broken validator
#: environment from a real "board.yaml is invalid" verdict (issue #38).
_TRACEBACK_HEADER = "Traceback (most recent call last):"

#: Severity of a rich block, by its own header keyword. `note` is the oracle's
#: `Severity::Suggestion` arm.
_HEADER_SEVERITY = {"error": "error", "warning": "warning", "note": "note"}


@dataclass(frozen=True)
class _Result:
    outcome: str
    #: `(severity, message)` per finding, in the order they must reach
    #: `issues[]`. The offline checks only ever produce `error`; the spawn
    #: path carries the validator's own severities (see `_HEADER_SEVERITY`
    #: and `_severity_for_outcome`).
    findings: tuple[tuple[str, str], ...]


class BoardShapeError(Exception):
    """`board.yaml` parsed as YAML but does not fit the board model."""


def _load_yaml(text: str) -> Any:
    """Parse YAML using PyYAML when present, else a minimal top-level reader.

    tan ships no YAML dependency of its own (`typer` + `rich` only), and the
    offline path must work with nothing installed. PyYAML is used when it
    happens to be importable -- it usually is, since a Zephyr workspace needs
    it -- and otherwise we fall back to reading only what the structural checks
    actually consult: which top-level keys exist and whether each is a scalar
    or a block. That is enough to distinguish `som: <scalar>` from
    `som:` + an indented mapping, which is exactly what the checks below ask.
    """
    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError:
        return _top_level_shape(text)
    try:
        return yaml.safe_load(text)
    except Exception as err:  # yaml.YAMLError and anything a loader raises
        raise BoardShapeError(f"could not be parsed as YAML: {err}") from err


def _top_level_shape(text: str) -> dict[str, Any]:
    """Map each unindented `key:` to a scalar string or a nested-block marker.

    Deliberately not a YAML parser. It answers one question -- is this key a
    scalar or does it open a block -- because that is all the structural checks
    need when PyYAML is absent.
    """
    shape: dict[str, Any] = {}
    pending: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1].isspace() or raw.lstrip().startswith("-"):
            if pending is not None:
                shape[pending] = {}  # the key opened a block
                pending = None
            continue
        pending = None
        key, sep, rest = raw.partition(":")
        if not sep:
            continue
        name = key.strip()
        value = rest.strip()
        if value:
            shape[name] = value
        else:
            shape[name] = None
            pending = name
    return shape


def _effective_schema_version(doc: dict[str, Any]) -> int:
    raw = doc.get("schemaVersion")
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return 1


def validate_board_text(text: str) -> _Result:
    """The offline structural validator. Pure: text in, outcome out."""
    doc = _load_yaml(text)
    if doc is None:
        raise BoardShapeError("the document is empty")
    if not isinstance(doc, dict):
        raise BoardShapeError(
            f"the top level must be a mapping of keys, got {type(doc).__name__}"
        )

    som = doc.get("som")
    if som is not None and not isinstance(som, dict):
        raise BoardShapeError(
            "`som:` must be a mapping carrying a `sku:` key, but a scalar was "
            f"given ({som!r}). Write it as:\n  som:\n    sku: <SKU>"
        )

    messages: list[str] = []
    if _effective_schema_version(doc) >= 2:
        # I-02: the OS is derived from each core's Cortex class and is never
        # selectable, so a top-level `os:` is rejected outright.
        if doc.get("os") is not None:
            messages.append(
                "board.yaml v2: top-level 'os:' is not valid; move it into a 'cores:' block"
            )
        cores = doc.get("cores")
        if not isinstance(cores, dict) or not cores:
            messages.append(
                "board.yaml v2: 'cores:' block is required and must have at least one entry"
            )

    outcome = OUTCOME_CLEAN if not messages else OUTCOME_SCHEMA_VIOLATION
    return _Result(outcome, tuple(("error", message) for message in messages))


# ───────────────────────── full (spawn) validation ─────────────────────────
#
# Port of `tan_core::validate`'s spawn half + `validate.rs::run_spawn`'s
# subprocess. See the module docstring for the two deliberate divergences
# (timeout, ANSI stripping) and the one deliberate omission (`is_summary_line`,
# a no-op in the oracle).


def classify_validator_status(status: int | None) -> str:
    """Validator exit status -> outcome (`classify_validation_outcome`). A
    `None` status -- the process was killed, or never started -- lands on
    `failed` through the same default, exactly as the oracle's `_ =>` arm
    does; on POSIX a signal death arrives here as a NEGATIVE returncode
    instead, which is equally unmapped and equally `failed`."""
    return _STATUS_OUTCOME.get(status, OUTCOME_FAILED)


def _severity_for_outcome(outcome: str) -> str:
    """`severity_for_outcome`: a missing preset is a warning, everything else
    an error. Applies to legacy `FAIL` lines only -- a rich block and a `WARN`
    line each carry their own severity."""
    return "warning" if outcome == OUTCOME_MISSING_PRESET else "error"


def parse_validator_stderr(stderr: str, severity: str) -> tuple[tuple[str, str], ...]:
    """The validator's stderr as `(severity, message)` findings -- port of
    `tan_core::validate::parse_validation_issues`.

    Three shapes are recognised, and anything else is dropped: a rich
    `error[ALP-B*]` block (whose `-->` arrow and indented source/hint
    continuation lines are consumed with it, so one block is one finding), a
    legacy `FAIL`/`WARN` line (folding its own indented continuations in,
    joined by TWO spaces exactly as the oracle does), and a standalone
    `hint:`/`suggestion:` line.

    ANSI escapes are stripped per line before any of that -- see the module
    docstring; without it the rich-header match fails on every real run.
    """
    lines = [_ANSI_RE.sub("", raw) for raw in stderr.replace("\r\n", "\n").split("\n")]
    findings: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        rich = _RICH_HEADER_RE.match(line)
        if rich:
            findings.append((_HEADER_SEVERITY[rich.group(1)], rich.group(2).strip()))
            i += 1
            # Only an arrow line opens a continuation run: a header on its own
            # is a complete finding, and the next line may be another header.
            if i < len(lines) and _ARROW_RE.match(lines[i]):
                i += 1
                while i < len(lines) and _BLOCK_CONTINUATION_RE.match(lines[i]):
                    i += 1
            continue

        legacy = _FAIL_WARN_RE.match(line)
        if legacy:
            parts = [legacy.group(2).strip()]
            while i + 1 < len(lines) and _FAIL_CONTINUATION_RE.match(lines[i + 1]):
                i += 1
                parts.append(lines[i].strip())
            # WARN is a warning whatever the outcome; FAIL follows the outcome.
            findings.append(
                ("warning" if legacy.group(1) == "WARN" else severity, "  ".join(parts))
            )
            i += 1
            continue

        if _HINT_RE.match(line):
            findings.append(("note", line.strip()))
            i += 1
            continue

        i += 1
    return tuple(findings)


def _is_interpreter_crash(stderr: str) -> bool:
    return any(line.lstrip().startswith(_TRACEBACK_HEADER) for line in stderr.splitlines())


def analyze_validator_output(status: int | None, stderr: str) -> _Result:
    """`(outcome, findings)` for one validator execution -- port of
    `analyze_validation_result`, crash reclassification included: a traceback
    on exit 1 is a broken validator ENVIRONMENT (`failed`), never a verdict
    about the board (`schema-violation`)."""
    outcome = classify_validator_status(status)
    if outcome == OUTCOME_SCHEMA_VIOLATION and _is_interpreter_crash(stderr):
        outcome = OUTCOME_FAILED
    return _Result(outcome, parse_validator_stderr(stderr, _severity_for_outcome(outcome)))


def _synthesised_finding(outcome: str, stderr: str) -> tuple[str, str]:
    """The one finding a non-clean run with NOTHING parseable still owes the
    wire -- `to_cli_issues`' own synthesis, plus the last non-empty stderr
    line. The oracle stops at "Validation ended with outcome 'failed'.", which
    on the case that reaches this most often (a validator whose environment
    lacks `jsonschema`, so its whole stderr is a traceback the parser drops)
    tells the user nothing at all about what went wrong."""
    message = f"Validation ended with outcome '{outcome}'."
    tail = [line.strip() for line in _ANSI_RE.sub("", stderr).splitlines() if line.strip()]
    if tail:
        message = f"{message} Last line of validator output: {tail[-1]}"
    return "error", message


def _resolve_board_path(project: str | None, board_yaml: str | None) -> tuple[str, str]:
    """Return `(project_root, board_yaml_path)`, both as the CLI reports them.

    Mirrors `resolve_offline_board_path`: the root defaults to the literal `"."`
    and the board path stays RELATIVE, which the conformance fixtures pin
    (`project.root == "."`, `boardYamlPath == "./board.yaml"`).
    """
    root = project if project else "."
    if board_yaml and os.path.isabs(board_yaml):
        return root, board_yaml
    leaf = board_yaml or "board.yaml"
    # Joined as STRINGS, not via pathlib: `Path(".") / "board.yaml"` normalises
    # to `board.yaml`, but Rust's `Path::new(".").join("board.yaml")` keeps the
    # `./`, and the conformance fixtures pin `"./board.yaml"`.
    sep = "" if root.endswith(("/", "\\")) else "/"
    return root, f"{root}{sep}{leaf}"


#: A single point: every issue this command emits is document-level rather
#: than tied to a source line/column. The offline checks have none to give
#: (`board.yaml` parses fine; the finding is about the whole document), and
#: the spawn path deliberately drops the SDK's own `Diagnostic.line`/`.col` --
#: `parse_validator_stderr` consumes the `-->` arrow only to skip the block,
#: exactly as the oracle does ("block code/line/col are parsed for correct
#: line skipping but not retained", `parse_validation_issues`), because the
#: envelope's `Issue` has nowhere to carry them. Zero
#: satisfies `diagnostic-v1.schema.json`'s `required: [start, end]` honestly --
#: it is not a claim about *where* on the line, only that no better position
#: is known.
_ZERO_POSITION = {"line": 0, "character": 0}


def _issue_to_diagnostic(issue: Issue, board_path: str) -> dict[str, Any]:
    """One `issues[].code` (e.g. `validate.schema-violation`) as one
    `$defs/diagnostic` entry. The dot is stripped because `diagnostic-v1`'s
    `code` pattern (`^[A-Za-z][A-Za-z0-9_-]*$`) forbids it -- this is a
    different wire, not a rename of the envelope code.

    Deliberate omission: the oracle (`diagnostic_format.py:_diagnostic_to_
    json`) sets `documentationUri` unconditionally via `_doc_url`, which
    builds `docs/diagnostics/<code>.md`. tan ships no such landing pages, so
    there is no honest URL to put there; the field is left out rather than
    invented. It is optional in `diagnostic-v1.schema.json`, so the document
    still validates."""
    return {
        "uri": board_path,
        "range": {"start": _ZERO_POSITION, "end": _ZERO_POSITION},
        "severity": issue.severity,
        "code": issue.code.replace(".", "-"),
        "message": issue.message,
    }


def _diagnostic_v1_document(issues: list[Issue], board_path: str) -> dict[str, Any]:
    """`metadata/schemas/diagnostic-v1.schema.json`, mirroring
    `scripts/alp_cli/diagnostic_format.py:to_machine_json`'s shape."""
    return {
        "schemaVersion": _DIAGNOSTIC_SCHEMA_VERSION,
        "tool": {"name": "tan", "version": TAN_VERSION},
        "diagnostics": [_issue_to_diagnostic(i, board_path) for i in issues],
    }


def _sarif_document(issues: list[Issue], board_path: str) -> dict[str, Any]:
    """SARIF 2.1.0 (`runs[].results[]`), mirroring
    `scripts/alp_cli/diagnostic_format.py:to_sarif`. A separate artefact from
    `diagnostic-v1.schema.json` -- SARIF `region` is one-based by spec, so it
    does not reuse `_ZERO_POSITION`.

    Deliberate omission: the oracle's `_sarif_rules` gives every rule a
    `helpUri` via the same `_doc_url`-backed `documentationUri` as above. Same
    reasoning as `_issue_to_diagnostic`: tan has no `docs/diagnostics/<code>.md`
    to point at, so `helpUri` is left off each rule rather than faked.
    `helpUri` is optional in the SARIF 2.1.0 schema."""
    rules: dict[str, dict[str, str]] = {}
    results = []
    for issue in issues:
        code = issue.code.replace(".", "-")
        rules.setdefault(code, {"id": code})
        results.append(
            {
                "ruleId": code,
                "level": _SARIF_LEVEL[issue.severity],
                "message": {"text": issue.message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": board_path},
                            "region": {
                                "startLine": 1,
                                "startColumn": 1,
                                "endLine": 1,
                                "endColumn": 1,
                            },
                        }
                    }
                ],
            }
        )
    return {
        "$schema": _SARIF_SCHEMA_URI,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "tan",
                        "informationUri": "https://github.com/alplabai/tan-cli",
                        "version": TAN_VERSION,
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def _emit(
    *,
    output_format: ValidateOutputFormat,
    root: str,
    board_path: str,
    outcome: str,
    issues: list[Issue],
    exit_code: ExitCode,
    command_line: str = "",
    sdk: SdkInfo | None = None,
) -> None:
    if output_format == ValidateOutputFormat.JSON:
        data = {
            "schemaVersion": DATA_SCHEMA_VERSION,
            "outcome": outcome,
            "issueCount": len(issues),
            # The validator command line that actually ran, or `""` -- which
            # every guard and the whole offline path keep, and which the two
            # committed conformance fixtures pin.
            "commandLine": command_line,
            # `data.boardYamlPath` is UNTOUCHED by tan-cli#236 -- it names where
            # tan looked even on the missing-board refusal below; only
            # `project.boardYaml` (Rust's starkest instance of the bug: the
            # refusal one line below says the file does not exist, in the same
            # envelope that used to still name it) is existence-filtered.
            "boardYamlPath": board_path,
        }
        emit(
            Envelope(
                "validate",
                Project.resolved(root, board_path),
                data,
                issues,
                exit_code,
                # Absent, not null, when nothing resolved -- `Envelope` omits
                # the key on `None`. See the module docstring's `sdk` block
                # paragraph for the measured presence/absence matrix.
                sdk=sdk,
            )
        )
    elif output_format == ValidateOutputFormat.DIAGNOSTIC_V1:
        # indent=2, matching scripts/alp_cli/validate.py:34's
        # `json.dumps(to_machine_json(collector), indent=2)` -- these two
        # formats are ported documents, not the envelope (which is
        # deliberately compact; see `tan.envelope.emit`'s `separators`).
        typer.echo(json.dumps(_diagnostic_v1_document(issues, board_path), indent=2))
    elif output_format == ValidateOutputFormat.SARIF:
        # indent=2, matching scripts/alp_cli/validate.py:36.
        typer.echo(json.dumps(_sarif_document(issues, board_path), indent=2))
    else:
        stream = typer.get_text_stream("stderr")
        if len(issues) == 1 and issues[0].code == "validate.board-yaml-missing":
            # tan-cli#350: this is not a VALIDATION failure -- there is no
            # board.yaml to validate, so nothing was checked and found
            # wrong. Every other non-clean outcome below still says
            # "validate: validation failure"; only this one issue code gets
            # its own verdict wording. `issues[0].message` (shared with
            # `--format json`'s `issues[].message`) already names where tan
            # looked and the remedy -- see the guard above.
            stream.write("validate: no board.yaml to validate\n")
            stream.write(f"{issues[0].message}\n")
        elif outcome != OUTCOME_CLEAN:
            stream.write("validate: validation failure\n")
            for issue in issues:
                stream.write(f"{issue.message}\n")
        else:
            # Keyed off the OUTCOME, not off `issues` being non-empty: a
            # SPAWNED validator that exits 0 having printed warnings
            # (`validate_board_yaml.py` renders every diagnostic and only
            # RETURNS 1 for `collector.has_errors()`) is a clean board that
            # still has something to say. Reading `issues` instead -- which is
            # what this branch did while only the offline path could reach it,
            # where a clean result has no messages by construction -- printed
            # "validate: validation failure" over exit 0. The oracle's own
            # `spawn_text` keys off the outcome for exactly this reason.
            stream.write(f"validate: {board_path} is clean\n")
            for issue in issues:
                stream.write(f"{issue.message}\n")
    raise typer.Exit(int(exit_code))


def validate(
    offline: bool = typer.Option(
        False, "--offline", help="Run only the structural checks that ship in tan."
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: ValidateOutputFormat = typer.Option(
        ValidateOutputFormat.TEXT, "--format", help=FORMAT_HELP
    ),
) -> None:
    """Validate a board.yaml.

    `--sdk-root` must stay declared HERE, as a same-named local option, even
    though clap makes it `global = true` in Rust
    (`crates/tan-cli/src/cli.rs`): without it `cli._reorder_global_flags`
    relocates the flag to right after `validate` and Click rejects it there as
    unrecognised -- and the pre-subcommand position (`tan --sdk-root X
    validate`) is the one the extension actually uses
    (`alpCli/vscodeAdapter.ts`'s `withSdkRoot`). Since tan-cli#376 it is also
    READ -- and VALIDATED against the loader marker (tan-cli#257/#258) -- by
    the spawn path's own SDK guard; `--offline` still ignores it entirely,
    having no subprocess to point anywhere, and reports no `sdk` block.
    """
    root, board_path = _resolve_board_path(project, board_yaml)

    # Resolved BEFORE the board.yaml guard, though it is guard 2, because the
    # `sdk` block is not a guard result -- it reports what this RUN resolved,
    # and the oracle emits it on the board-yaml-missing refusal too (measured:
    # a valid `--sdk-root` + an empty directory answers
    # `validate.board-yaml-missing` WITH `sdk.sourceTier: "sdkRootFlag"`).
    # Guard ORDER is unchanged -- board.yaml still refuses first.
    #
    # `os.path.abspath`, not `Path(root)`: the ladder WALKS ANCESTORS, and
    # `Path(".").parents` is EMPTY -- the default `--project` would never
    # discover the sibling `../alp-sdk` every workspace layout puts there.
    # Lexical (not `.resolve()`) for the same reason `build_output.normalise`
    # is: a project reached through a symlink keeps the name the user typed.
    #
    # Skipped entirely under `--offline`: no subprocess, so no interpreter and
    # no checkout to report, and the oracle emits no `sdk` there even when one
    # would resolve (measured with a sibling checkout AND with an explicit
    # `--sdk-root`).
    resolved_sdk: Path | None = None
    sdk_info: SdkInfo | None = None
    if not offline:
        resolved_sdk, sdk_tier, _broken_pin = resolve_sdk_root_ladder(
            sdk_root, Path(os.path.abspath(root))
        )
        # tan-cli#257/#258, at this call site because `resolve_sdk_root_ladder`
        # deliberately does not do it: an explicit `--sdk-root` comes back
        # verbatim and unvalidated, which is right for a caller that only
        # REPORTS the tier and wrong for this one, which spawns out of it. An
        # unresolvable explicit root is no root at all -- guard 2 below then
        # gives the refusal the oracle gives, instead of the SDK's own
        # `can't open file ...validate_board_yaml.py` reaching the status map
        # as a verdict about the customer's board.
        if sdk_tier == "sdkRootFlag" and not _is_sdk_root(resolved_sdk):
            resolved_sdk = None
        if resolved_sdk is not None:
            sdk_info = SdkInfo(str(resolved_sdk), sdk_tier)

    def fail(code: str, message: str, exit_code: ExitCode) -> None:
        _emit(
            output_format=output_format,
            root=root,
            board_path=board_path,
            outcome=OUTCOME_FAILED,
            issues=[Issue(f"validate.{code}", "error", message)],
            exit_code=exit_code,
            sdk=sdk_info,
        )

    if not Path(board_path).exists():
        # Ordered BEFORE the SDK guard and the spawn, deliberately -- the same
        # order `run_spawn` puts its own three guards in. (The SDK is RESOLVED
        # above this, which is not the same thing: resolving fills the `sdk`
        # block the oracle reports on this very refusal; refusing is what stays
        # ordered, and no guard fires before this one.) MEASURED against the
        # oracle (`target/debug/tan.exe`, tan 0.4.1-dev) in an empty directory:
        # exit 2, `validate.board-yaml-missing`, `outcome: "failed"` -- and the
        # oracle reaches this guard on both paths, `--offline` or not. Anything
        # that short-circuits above this check answers a DIFFERENT question in
        # the one case a brand-new user hits first: before #376 a "not ported
        # yet" stub did, and an SDK guard hoisted above it would answer "no
        # alp-sdk checkout" to a project that has no board.yaml either way.
        #
        # tan-cli#350 (DELIBERATE divergence -- the oracle is byte-identical
        # here, down to exit code and message): the oracle's own wording,
        # "board.yaml path could not be resolved or the file does not
        # exist.", names no remedy and, worse, is fronted in text mode by
        # "validate: validation failure" -- a VERDICT that implies something
        # was checked and found wrong. Nothing was validated; there is no
        # board.yaml to validate. This is the state every user is in before
        # `tan init`, and the old wording sent them looking for a defect in a
        # file that does not exist. The message below names WHERE tan looked
        # and the two remedies every sibling guard names for its own missing
        # input (`build` names `--sdk-root`, `doctor` names `tan init` /
        # `--board-yaml <path>` for this exact guard -- see
        # `doctor_cmd.py`'s `_board_yaml_check`). The exit code (2) and issue
        # CODE (`validate.board-yaml-missing`) are UNCHANGED: a
        # found-but-invalid board.yaml still exits 2 as
        # `validate.schema-violation` and still prints "validate: validation
        # failure" below -- the issue code is how a machine consumer (or a
        # human reading `--format json`) tells the two apart, since the exit
        # code alone does not.
        fail(
            "board-yaml-missing",
            f"no board.yaml found at {board_path} -- run `tan init` to create "
            "one, or pass --board-yaml <path> to point at an existing file.",
            ExitCode.VALIDATION_FAILURE,
        )
        return

    #: The validator argv, for `data.commandLine`. Stays `""` on the offline
    #: path and on every guard -- nothing ran.
    command_line = ""

    if offline:
        try:
            text = Path(board_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            # Not a tan bug: an unreadable or non-UTF-8 file is the user's to fix.
            fail(
                "internal-failure",
                f"could not read board.yaml: {err}",
                ExitCode.INTERNAL_FAILURE,
            )
            return

        try:
            result = validate_board_text(text)
        except BoardShapeError as err:
            fail(
                "schema-violation",
                f"board.yaml is not valid: {err}",
                ExitCode.VALIDATION_FAILURE,
            )
            return
        except Exception as err:  # never a bare traceback; the envelope is the contract
            fail(
                "internal-failure",
                f"validator failed unexpectedly: {err}",
                ExitCode.INTERNAL_FAILURE,
            )
            return
    else:
        if resolved_sdk is None:
            # The oracle's guard 2, message included in substance (its own
            # wording names only `--sdk-root`; the two other tiers this port's
            # ladder resolves are named here for the same reason
            # `trace_cmd.py`'s identical guard names them).
            #
            # tan-cli#381, FIFTH site: this line hardcoded "pin one with `tan
            # sdk switch <version|path>`" -- a subcommand that refuses outright
            # in this build (`sdk_cmd._run_not_ported`, `sdk.not-ported`). It
            # was not missed by the #305 sweep or the #381 sweep; it did not
            # EXIST when either ran. #376 wrote it here, from the oracle's own
            # (honest, there) wording, while #381 was cleaning the other four.
            # That is the whole mechanism, so the fix is not just this string:
            # `test_sdk_onboarding_dead_end.py`'s AST sweep now walks every
            # string literal under `python/tan/` and fails on the phrase, which
            # is what catches the SIXTH site before a human has to.
            fail(
                "sdk-root-unresolved",
                "alp-sdk root is unresolved. Use --sdk-root, place the project "
                f"near an alp-sdk checkout, or {NO_SDK_NEXT_STEPS}. "
                "`tan validate --offline` runs the structural checks that need "
                "no SDK.",
                ExitCode.VALIDATION_FAILURE,
            )
            return

        script = os.path.join(str(resolved_sdk), *VALIDATOR_SCRIPT)
        python_binary = _planner_python(os.path.abspath(root), str(resolved_sdk))

        # The oracle's guard 3 (`validate.rs:124-129`), the one #376 left out.
        # AFTER the SDK guard because both of its inputs come from the resolved
        # checkout: the floor is that checkout's own declared
        # `pythonMinVersion`, and `_planner_python` prefers its workspace venv.
        # BEFORE the spawn because the whole point is to replace alp-sdk's
        # `dataclass() got an unexpected keyword argument 'slots'` traceback --
        # which arrives as validator exit 1 WITH a traceback, i.e. `failed`
        # with the traceback's last line quoted at the user -- with a message
        # naming the actual defect. `command_line` is still `""` here: nothing
        # ran, exactly as on guards 1 and 2 and as the oracle reports.
        floor, _floor_source = resolve_manifest_python_floor(str(resolved_sdk))
        if (too_old := _python_too_old(python_binary, floor)) is not None:
            fail("python-too-old", too_old, ExitCode.VALIDATION_FAILURE)
            return

        # Verbatim from `run_spawn`'s own `format!` -- this string is reported,
        # never re-parsed, so it is built beside the argv rather than from it.
        command_line = f"{python_binary} {script} --input {board_path}"
        try:
            out = subprocess.run(
                [python_binary, script, "--input", board_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                # The validator never reads stdin; without this a child that
                # somehow prompts would block forever behind the timeout.
                stdin=subprocess.DEVNULL,
                timeout=VALIDATOR_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # The child STARTED, so this is a verdict that never arrived, not a
            # launch failure: `failed` at exit 2, per tan-cli#262.
            result = _Result(
                OUTCOME_FAILED,
                (
                    (
                        "error",
                        f"the SDK validator did not finish within "
                        f"{VALIDATOR_TIMEOUT_S}s and was killed: {command_line}",
                    ),
                ),
            )
        except (OSError, ValueError, subprocess.SubprocessError) as err:
            # The one RUNTIME_FAILURE (1) case #262 carved out: the subprocess
            # could not even be started (no interpreter on PATH, the script
            # unreadable). Nothing validated anything, so this is not a verdict.
            fail(
                "spawn-failed",
                f"could not run the SDK validator ({command_line}): {err}",
                ExitCode.RUNTIME_FAILURE,
            )
            return
        else:
            result = analyze_validator_output(out.returncode, out.stderr)
            if result.outcome != OUTCOME_CLEAN and not result.findings:
                # `to_cli_issues`' synthesis: a non-clean run must never reach a
                # consumer as "exit 2, zero issues", which reads as no problem.
                result = _Result(
                    result.outcome, (_synthesised_finding(result.outcome, out.stderr),)
                )

    issues = [
        Issue(f"validate.{result.outcome}", severity, message)
        for severity, message in result.findings
    ]
    exit_code = (
        ExitCode.SUCCESS
        if result.outcome == OUTCOME_CLEAN
        else ExitCode.VALIDATION_FAILURE
    )
    _emit(
        output_format=output_format,
        root=root,
        board_path=board_path,
        outcome=result.outcome,
        issues=issues,
        exit_code=exit_code,
        command_line=command_line,
        # `None` on the offline path (never resolved) -- the two committed
        # conformance fixtures are offline runs and stay `sdk`-less.
        sdk=sdk_info,
    )


# tan-cli#261: adds the seven oracle `GlobalArgs` flags this command was
# still missing (`--all`/`--ci`/`--no-color`/`--non-interactive`/`--quiet`/
# `--target`/`--verbose`) on top of `--board-yaml`, already declared and read
# above; see `tan.core.global_flags`.
validate = accept_global_flags(validate)
