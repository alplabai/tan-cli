<!-- SPDX-License-Identifier: Apache-2.0 -->
# `tan` UX polish sweep — design

**Date:** 2026-08-05 · **Status:** shipped (2d402fd, #480) · **Baseline measured:** `main` @ `0277b4c`
(v0.5.0), Python surface, `py -3.12 -m tan`, typer 0.27.0 / click 8.4.2.

Five low-risk changes to the `tan` command surface, each measured against the
running CLI rather than read off the source. None of them changes what `tan`
does; all of them change how a first-time user finds it.

## Goal

A user who has just installed `tan` should be able to (a) see the 31 commands
organised rather than alphabetised, (b) read `--help` without hitting the port's
own changelog, (c) be told what to type next when a command refuses, and (d) get
a useful answer out of `presets` and `examples` by default.

Four items ship. A fifth was investigated and withdrawn — see item 2.

## Non-goals

Two larger items were considered and deliberately deferred:

- **`Issue.fix`.** `Issue` is `{code, severity, message}` (`tan/envelope.py:55-61`)
  with 108 construction sites. `doctor`'s own `Check` *does* carry `fix`
  (`tan/commands/doctor_cmd.py:254`) and renders it, but the fix is dropped when
  a check becomes an `Issue` — so `issues[]`, the channel alp-sdk-vscode reads,
  carries no remediation anywhere in the CLI. Adding the field is the single
  highest-value change available, and it is an envelope-contract change needing
  a declared oracle divergence plus a golden regeneration. Own sub-project.
- **The onboarding cliff.** `tan sdk install` and `tan sdk switch` both answer
  `not available in this build of tan.` (`tan/commands/sdk_cmd.py:969`). `tan`
  is inert without an alp-sdk checkout, and the one command a new user would
  type to get one refuses. Real feature work; own sub-project.

## The parity constraint

Everything below is bounded by what `python/tests/parity/` freezes. Measured,
not assumed:

- **Help text and stderr are never compared.** `tests/parity/oracle.py`'s module
  docstring: *"stderr is deliberately NOT compared -- it is clap's help renderer
  versus Typer/rich's, human text with no contract over it."*
- **The frozen `CASES` table is 9 argv shapes** (`tests/parity/test_oracle_parity.py:67-103`).
  The two usage-error cases — `["bogus-command"]` and `[]` — are **text mode**
  and assert stdout stays *empty*. Verified: bare `tan` writes 0 bytes to
  stdout, 612 to stderr, exits 2.
- **`presets` is frozen only at `["presets", "--format", "json"]`.** Its text
  renderer is not in the table.
- **`examples` is frozen only at its no-SDK refusal**
  (`tests/parity/test_command_surface_oracle_parity.py:366`, `data == {"schemaVersion": "1", "examples": []}`).
  The full-answer path is recorded as uncovered in `oracle_fixtures/PARITY-COVERAGE.txt`.
- **`completion` verbs are frozen as an ORDERED list**
  (`tests/parity/test_command_surface_oracle_parity.py:104-130`:
  `assert _completion_verbs(p_out["__raw__"], shell) == oracle_verbs`). This is
  the one real constraint in the sweep — see item 2.

One hazard that is *not* a parity failure but must be understood: under
`--format json`, `cli.py:824` folds tee'd stderr verbatim into the usage-error
envelope's `data.message` and `issues[0].message`. Any stderr wording change on
a usage-error path is therefore visible in the JSON channel too. No frozen case
covers a bare `--format json` invocation, so this is a review note, not a
blocker.

## Item 1 — group `tan --help` into panels

**Problem.** `tan --help` lists 32 commands flat and alphabetical. There is no
signal about which four a new user needs.

**Change.** Pass `rich_help_panel=` on each `app.command()` call in
`tan/cli.py:79-110`. Supported by typer 0.27.0. No logic, no parity exposure.

| Panel | Commands |
|---|---|
| Setup | `doctor` `bootstrap` `sdk` `completion` |
| Start a project | `init` `scaffold` `examples` `presets` `explain` |
| Configure | `validate` `generate` `migrate` `kconfig` `model` `lock` `quality` |
| Build & run | `build` `run` `clean` `size` `image` |
| Hardware | `flash` `monitor` `debug-config` `faultdecode` |
| Inspect & author | `inspect` `diff` `trace` `support-bundle` `pinmux` `new-som` |

31 total; every registered command appears exactly once. (Was 32 when this
shipped; `renode` was removed from the surface afterwards.)

**Test impact.** `_SUBCOMMAND_NAMES` (`tan/cli.py:128-131`) derives from
`app.registered_commands` and is unaffected by a panel kwarg. Any test asserting
on rendered `--help` text needs its expectation updated.

## Item 2 — WITHDRAWN: completion-script drift is already guarded

**The proposal was to** de-duplicate the completion command list, a hardcoded
literal in three places — `tan/commands/completion_cmd.py:115` (bash), `:227`
(zsh), `:387` (fish) — on the premise that a command registered later would be
silently uncompletable.

**The premise is false.** `test_embedded_scripts_list_every_registered_subcommand`
(`tests/commands/test_completion_command.py:105`) already reads
`tan.cli._SUBCOMMAND_NAMES` and word-boundary-checks every registered verb
against all three scripts. Verified green on this baseline:

```
$ py -3.12 -m pytest tests/commands/test_completion_command.py::test_embedded_scripts_list_every_registered_subcommand -q
1 passed in 0.37s
```

A command added without touching the scripts turns that test **red**. The
duplication is real but loud, and it has a user-facing benefit of exactly zero:
completion already cannot fall behind the command table. Refactoring it would be
churn against a live guard.

Recorded rather than deleted so the same idea does not get re-proposed. Two
facts worth keeping if it ever is revisited: the parity extractor compares an
**ordered** list, in the oracle's declaration order, not alphabetical
(`test_command_surface_oracle_parity.py:104-130`); and the three shells need
different text — bash and fish take a space-separated name list, zsh takes
`'name:description'` rows matching
`_ZSH_COMMAND_ROW_RE = r"^\s*'([a-z][a-z0-9-]*):[^']*'\s*$"`. A single flat
`@COMMANDS@` mark cannot serve all three.

## Item 3 — remove the port's archaeology from user-facing help

**Problem.** `tan build --help` reads as a changelog. Verbatim from
`tan/commands/build_cmd.py:1225`: *"ADDED BY THIS PORT, not a v0.4.1 flag: there
--plan-from implies --plan and outranks --native, so a file-supplied plan cannot
be dispatched at all. Deliberate, not a parity gap."* `--native` at `:1216`
likewise opens *"Like v0.4.1, this does NOT override…"*. A user has never heard
of v0.4.1.

**Change.**

- Move the v0.4.1 rationale on `--native` (`:1216`) and `--execute` (`:1225`)
  into the surrounding docstring, where the same reasoning is already recorded.
  Leave the user-facing sentence describing what the flag does.
- Reword `_DEFERRED_HELP` (`tan/commands/build_cmd.py:172`), currently
  `"Deferred, not implemented in this build (tan-cli#427)."` — "deferred" and
  "this build" are the port's vocabulary, not a user's. Proposed:
  `"Accepted by other commands; not implemented for \`build\` yet (tan-cli#427)."`

**Explicitly rejected: hiding the deferred options.** `_DEFERRED_HELP` is
attached to **12** `build` options (`:1252-1265` — the count its own comment at
`:166-167` already states), including `--verbose`,
`--quiet`, `--no-color`, `--non-interactive` and `--ci` — flags every other
command implements and a user will reasonably type. Hiding them from `--help`
and then refusing them at exit 1 via `deferred_cmd.py`'s `cli.command-deferred`
is strictly worse than listing them honestly. They stay visible; only the
wording changes.

**Test impact.** Help-text assertions only. No envelope, no parity.

## Item 4 — a next step on each dead-end refusal

**Problem.** Four refusals leave the user with nowhere to go. All measured:

| Invocation | Current output |
|---|---|
| `tan build` (no SDK) | `no alp-sdk checkout found -- pass \`--sdk-root <PATH>\` or run from a project beside one. Planning reads the SDK's \`metadata/**\`.` |
| `tan build` (no project) | `no board.yaml found -- pass \`--board-yaml <PATH>\` or run from a project.` |
| `tan init` (no args) | `init: One or more files would be overwritten. Use --force to allow updates.` |
| `tan sdk install 0.15.0` | `sdk install: not available in this build of tan.` + `Use \`--sdk-root <path>\` to point a command at a checkout directly.` |
| `tan` (bare) | `a command is required` |

**Change.** One added line each:

- `tan/commands/build_cmd.py:678` — extend the `build.plan-unavailable`
  board.yaml refusal to name `tan init` as the way to create a project. Its
  sibling at `:670` (the no-SDK refusal, same issue code) is the more common
  first-run failure and gets the same treatment.
- `tan init` — name *which* files would be overwritten, and point at `--preview`
  / `--destination` / `--name` rather than jumping straight to `--force`.
- `tan/commands/sdk_cmd.py:963,969` — the `--sdk-root` hint does not say how to
  *obtain* a checkout. Add that.
- Bare `tan` — a new-user line after `a command is required`
  (`doctor` → `init` → `build`). Safe: this path writes to stderr only, and the
  frozen `([], ENVELOPE, None)` case asserts stdout is empty, which it stays.

**Test impact.** `tests/test_cli_skeleton.py::test_bare_invocation_exits_2_with_help_on_stderr`
and any test matching these strings. `tests/commands/test_cli_global_flags.py:422`
asserts `"a command is required" not in leading.stderr` for a *different* argv —
appending a line keeps that substring assertion valid either way, but re-run it.

## Item 5 — make `presets` and `examples` answer the question

**`presets`.** `render_presets_text` (`tan/commands/presets_cmd.py:544-552`)
prints a count line — measured: `presets: skus=11 libraries=8 boardLibraries=32`
— and, under `--verbose`, one bare `sku: <id>` line each. So `--verbose` does
already answer "which SKUs exist"; the gap is narrower than a first read
suggests:

- the **default** is three integers, which answers nothing;
- even `--verbose` prints bare ids, while the JSON carries `soms[]` with
  `displayName`, `family` and `cores[].id` / `cores[].os` per SKU.

Change: make the default list the SKUs with their `displayName`, and let
`--verbose` add `family` and the core/OS pairs. **JSON output untouched** — it
is the one parity-frozen `presets` surface.

**`examples`.** Already has `--filter` (substring on id/title,
`example_matches_filter` at `tan/commands/examples_cmd.py:271`) and `--verbose`
(appends the description). The gap is that the *default* dumps 100 lines with no
hint that a taxonomy exists.

Change: add `--category`, and print the category list on an unfiltered run. The
12 categories are the `id` prefix and need no new data: `aen`, `ai`, `audio`,
`bringup`, `camera-vision`, `connectivity`, `display`, `multicore`,
`peripheral-io`, `power-timing`, `testing`, `v2n`.

**`--som` is dropped as infeasible.** An example entry carries exactly four
fields — `id`, `sourceDir`, `title`, `description` (measured against the live
`--format json`). There is no SoM field to filter on, and inventing one is
alp-sdk metadata work, not a CLI change.

Also not in scope: the example titles are mostly the slug repeated
(`aen/aen-cc3501e-gpio    aen-cc3501e-gpio`). Those come from alp-sdk, not this
repo.

## Sequencing

Four PRs into `dev`:

1. Item 1 (`tan/cli.py`)
2. Item 3 (`tan/commands/build_cmd.py`)
3. Item 4 (`tan/commands/build_cmd.py`, `sdk_cmd.py`, `init_cmd.py`, `cli.py`) —
   shares `build_cmd.py` with item 3 and `cli.py` with item 1, so it lands after
   both
4. Item 5 (`tan/commands/presets_cmd.py`, `examples_cmd.py`) — file-disjoint
   from every other item; can land at any point

One ordering is forced: item 4 after items 1 and 3. Items 1, 3 and 5 are free to
run concurrently.

## Gates

Per PR, from `python/`:

- `py -3.12 -m pytest tests -q` — **zero failures**, not a fixed count
  (`ci.yml:82-86` states this explicitly)
- `py -3.12 scripts/version_check.py --selftest --self`

`ALP_SDK_ROOT` must point at the pinned commit or at `main`/`dev`. Pointing it
at an arbitrary feature branch with uncommitted metadata turns roughly 400
`test_planner_emit_parity.py` cases red — a measured failure mode, not a
hypothetical.

## Risks

- **No shipping item can break a parity test.** Every remaining change lands in
  help text, stderr, or a text-mode renderer; the frozen `CASES` table touches
  none of those. The one item that would have had a parity constraint was
  withdrawn.
- **Item 4 leaks into the JSON channel** through `cli.py:824`'s stderr fold. No
  frozen case covers it, but review the resulting `data.message` on a bare
  `--format json` run before merging.
- **Items 1 and 3 will break help-text assertions.** Expected and cheap; they
  are the port's own tests, not a contract.
