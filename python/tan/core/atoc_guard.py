# SPDX-License-Identifier: Apache-2.0
"""Flow A's half of the whole-ATOC guard (tan-cli#1267). Pure -- no IO.

A `zephyr_west_flash` slice that stays on Flow A runs `west flash`, which on
an Alif Ensemble board picks the board.cmake default runner `alif_flash` and
burns over the SE-UART. That write REPLACES the whole ATOC, the same as Flow
D's -- but unlike Flow D, the runner can ask the part what is resident first.
alp-sdk#2262 (PR alp-sdk#2275, in tan's pin since #1301) made it do exactly
that: before burning, `alif_flash` reads the resident ATOC back over the
SE-UART and refuses when a foreign entry would be delisted or the read could
not be verified, unless `--replace-atoc` is passed. Every attempt leaves a
machine-readable verdict, `<build_dir>/alif_flash/atoc-guard.json`, whose
shape alp-sdk freezes as `alp-sdk.alif-flash-atoc-guard.v1`
(`docs/aen-provisioning.md` section 0.6). tan's half, all decided here:

* pass `--replace-atoc` through to `west flash` -- and only to a slice whose
  runner is `alif_flash`, from a module whose runner has the guard
  ([`decide_replace_atoc`]), and only to ONE such slice per run
  ([`ambiguous_replace_message`]);
* read the verdict when `west flash` exits ([`parse_verdict`]);
* turn a refusal into its own message ([`refusal_message`]), which the CLI
  reports under its own issue code. The message texts live in
  `tan.core.atoc_guard_messages`.

`--replace-atoc` is NOT `--atoc-unqueryable` and the two are never merged or
aliased (alp-sdk#2025's header, repeated by the runner's own help). Flow D's
flag acknowledges a write that cannot be checked; this one overrides a check
that ran. An operator on a no-SE-UART bench passes the Flow D flag on every
run, so a flag that answered both would silence the one guard that can look
first.

**No manifest spelling, unlike Flow D's `flash_args.atoc_unqueryable`.** That
key exists because Flow D can never query, so a manifest that knows its own
boot layout can record the acknowledgement once. Flow A's guard does query:
a persisted override would turn off a working check for every later run,
including the one where the board carries something new. The override is a
statement about what is resident on the board right now, so it is re-stated
per invocation. `tan run --flash` regenerates the manifest anyway, so a key
would not survive there either.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from tan.core.atoc_guard_messages import (
    FLOW_D_ACK_FLAG,
    REPLACE_ATOC_FLAG,
    not_applicable_message,
    unguarded_message,
)

#: The west runner that carries the guard. A runner NAME, not a part number:
#: tan decides applicability from the build's own `runners.yaml` (or an
#: explicit `flash_args.runner`), never from silicon knowledge (ADR-0017).
ALIF_FLASH_RUNNER = "alif_flash"

#: The Zephyr module name alp-sdk declares in its `zephyr/module.yml`
#: (`name: alp-sdk`) -- the key `zephyr_modules.txt` lists it under.
SDK_MODULE_NAME = "alp-sdk"

VERDICT_SCHEMA = "alp-sdk.alif-flash-atoc-guard.v1"
VERDICT_DIR = "alif_flash"
VERDICT_FILE = "atoc-guard.json"

REFUSED_FOREIGN = "refused-foreign"
REFUSED_UNVERIFIED = "refused-unverified"
REPLACED = "replaced"
#: Every `status` the v1 contract defines. Anything else is a verdict tan
#: cannot read, never a guess.
VERDICT_STATUSES = ("clear", "empty", REFUSED_FOREIGN, REFUSED_UNVERIFIED, REPLACED)
QUERY_STATUSES = ("unverified", "empty", "ok")

#: The module-relative path of the runner whose source tan reads to learn
#: whether it has the guard -- the `runners: - file:` alp-sdk's
#: `zephyr/module.yml` declares. Path segments, joined by the caller.
RUNNER_SOURCE_PARTS = ("scripts", "west_commands", "runners", "alif_flash.py")

_FLASH_RUNNER_RE = re.compile(r"^flash-runner:[ \t]*['\"]?([^'\"\s#]+)", re.MULTILINE)
_MODULE_LINE_RE = re.compile(r'^"([^"]*)":"([^"]*)":')


def parse_flash_runner(runners_yaml: str) -> str | None:
    """The build's default flash runner out of Zephyr's
    `<build_dir>/zephyr/runners.yaml` -- the top-level `flash-runner:` line
    `west flash` itself falls back to when no `--runner` is given. `None`
    when absent."""
    match = _FLASH_RUNNER_RE.search(runners_yaml)
    return match.group(1) if match else None


def parse_module_dir(zephyr_modules_txt: str, name: str = SDK_MODULE_NAME) -> str | None:
    """The module root `name` was built from, out of Zephyr's
    `<build_dir>/zephyr_modules.txt` (one `"<name>":"<path>":"<cmake>"` line
    per module, written by `zephyr_module.py --cmake-out` at configure time).
    This is the checkout `west flash` imports the module's runners from,
    which need not be the SDK tan is bound to. `None` when not listed."""
    for line in zephyr_modules_txt.splitlines():
        match = _MODULE_LINE_RE.match(line.strip())
        if match and match.group(1) == name:
            return match.group(2)
    return None


def parse_domain_build_dirs(domains_yaml: str) -> list[str] | None:
    """Every domain's build dir out of a sysbuild's top-level `domains.yaml`
    (Zephyr's `domains:` list of `{name, build_dir}`), in file order. `None`
    when the text is not that shape. `west flash` with no `--domain` runs
    the board's runner once per domain, each with that domain's own
    `build_dir` -- which is where the verdict lands. PyYAML is imported
    here, not at module scope: `import tan.cli` must not pay for it
    (tan-cli#810, `tests/gates/test_cli_import_is_lean.py`)."""
    import yaml  # noqa: PLC0415

    try:
        doc = yaml.safe_load(domains_yaml)
    except yaml.YAMLError:
        return None
    domains = doc.get("domains") if isinstance(doc, dict) else None
    if not isinstance(domains, list) or not domains:
        return None
    dirs = [d.get("build_dir") for d in domains if isinstance(d, dict)]
    if len(dirs) != len(domains) or not all(isinstance(d, str) and d for d in dirs):
        return None
    return dirs


def runner_has_guard(runner_source: str) -> bool:
    """Does this `alif_flash.py` source carry the #2262 guard? Both halves
    are required: the `--replace-atoc` argument (west rejects an unknown one,
    so tan must never pass it blind) and the v1 verdict schema (what tan reads
    back). They landed together in alp-sdk PR #2275."""
    return f"'{REPLACE_ATOC_FLAG}'" in runner_source and VERDICT_SCHEMA in runner_source


# ── which slices the flag applies to ─────────────────────────────────────────

#: `ReplaceAtocDecision.warning_kind` values. The CLI maps each to its own
#: issue code.
NOT_APPLICABLE = "not-applicable"
UNGUARDED = "unguarded"


@dataclass(frozen=True)
class RunnerFacts:
    """What tan learnt about the runner one west entry will reach.

    `runner`: the effective west runner, `None` when unknown, with
    `unknown_reason` saying why. `source`: the `alif_flash.py` tan checked,
    `origin` where that path came from (the build's module list, or the
    bound SDK as a fallback). `has_guard`: whether that source has the guard
    (`None` when not checked). `verdict_dir`: the dir the runner's verdict
    lands under (the single sysbuild domain's, or `--build-dir` itself)."""

    runner: str | None = None
    unknown_reason: str | None = None
    source: str = ""
    origin: str = ""
    has_guard: bool | None = None
    verdict_dir: str = ""


@dataclass(frozen=True)
class ReplaceAtocDecision:
    """What tan does about the guard for one entry.

    `append`: put `--replace-atoc` on the `west flash` argv. `guarded`: this
    entry runs a runner that writes a verdict, so the outcome is checked
    against it. `warning`/`warning_kind`: a disclosure, or `None`.
    `source`: the runner file checked, for later messages."""

    append: bool = False
    guarded: bool = False
    warning: str | None = None
    warning_kind: str | None = None
    source: str = ""


def decide_replace_atoc(
    method: str, entry_id: str, facts: RunnerFacts, replace_atoc: bool
) -> ReplaceAtocDecision:
    """Pure. `method` is the DISPATCHED method (Flow D already selected)."""
    if method != "zephyr_west_flash" or facts.runner != ALIF_FLASH_RUNNER:
        if not replace_atoc:
            return ReplaceAtocDecision()
        return ReplaceAtocDecision(
            warning=not_applicable_message(
                method, entry_id, facts.runner, facts.unknown_reason, ALIF_FLASH_RUNNER
            ),
            warning_kind=NOT_APPLICABLE,
        )
    if not facts.has_guard:
        return ReplaceAtocDecision(
            warning=unguarded_message(entry_id, facts.source, facts.origin, replace_atoc),
            warning_kind=UNGUARDED,
            source=facts.source,
        )
    return ReplaceAtocDecision(append=replace_atoc, guarded=True, source=facts.source)


# ── reading the verdict ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class GuardVerdict:
    """A verdict that matched the v1 contract."""

    status: str
    foreign: tuple[str, ...]
    transcript: str
    query_status: str
    allowed: tuple[str, ...] = ()

    @property
    def refused(self) -> bool:
        return self.status in (REFUSED_FOREIGN, REFUSED_UNVERIFIED)


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(n, str) for n in value)


def parse_verdict(text: str) -> GuardVerdict | str:
    """The verdict, or a one-line reason it cannot be read. Strict: an
    unknown schema, status or query_status, a field of the wrong type, or a
    combination the runner cannot produce ([`_inconsistency`]) is unreadable
    -- tan never reports a refusal the runner did not state."""
    try:
        raw = json.loads(text)
    except ValueError as err:
        return f"it is not valid JSON ({err})"
    if not isinstance(raw, dict):
        return "it is not a JSON object"
    if raw.get("schema") != VERDICT_SCHEMA:
        return f"its schema is {raw.get('schema')!r}, not {VERDICT_SCHEMA!r}"
    status, query = raw.get("status"), raw.get("query_status")
    if status not in VERDICT_STATUSES:
        return f"its status {status!r} is not one {VERDICT_SCHEMA} defines"
    if query not in QUERY_STATUSES:
        return f"its query_status {query!r} is not one {VERDICT_SCHEMA} defines"
    for field in ("foreign", "allowed"):
        if not _str_list(raw.get(field)):
            return f"its {field} field is not a list of strings"
    if not isinstance(raw.get("transcript"), str):
        return "its transcript field is not a string"
    verdict = GuardVerdict(
        status, tuple(raw["foreign"]), raw["transcript"], query, tuple(raw["allowed"])
    )
    problem = _inconsistency(verdict)
    return verdict if problem is None else problem


def _inconsistency(v: GuardVerdict) -> str | None:
    """A status/query_status/foreign combination alp-sdk's `decide_atoc_guard`
    (`scripts/aen_atoc.py`, which the runner writes the verdict from) cannot
    produce. Its rules, and the contract's own reading of `replaced`
    (`query_status == "unverified"` with `foreign == []`, or any other
    query_status with a non-empty `foreign`): an unverified query is decided
    before the foreign check, so its `foreign` is always `[]`; `empty` means
    no ATOC was found, so nothing can be foreign."""
    expected = {
        REFUSED_FOREIGN: v.query_status == "ok" and bool(v.foreign),
        REFUSED_UNVERIFIED: v.query_status == "unverified" and not v.foreign,
        REPLACED: (v.query_status == "unverified" and not v.foreign)
        or (v.query_status == "ok" and bool(v.foreign)),
        "clear": v.query_status == "ok" and not v.foreign,
        "empty": v.query_status == "empty" and not v.foreign,
    }[v.status]
    if expected:
        return None
    return (
        f"its status {v.status!r} with query_status {v.query_status!r} and foreign "
        f"{list(v.foreign)!r} is a combination {VERDICT_SCHEMA} does not produce"
    )
