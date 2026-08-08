# tan surface walk

Runs **every** `tan` command, in dependency order, against a **real project** —
the way you would type them by hand. Built so that checking a build of `tan`
does not require anyone to reconstruct the command list from memory.

`tan flash` is never run and there is no flag to enable it.

## Run it

```sh
# Fastest useful check: no project scaffolding, no build. ~20 seconds.
scripts/tan-surface/run.sh --sdk-root ~/src/alp-sdk --phase discovery

# The full walk against a scaffolded throwaway project.
scripts/tan-surface/run.sh --sdk-root ~/src/alp-sdk

# Against YOUR project, read-only (nothing is written into it).
scripts/tan-surface/run.sh --project ~/src/my-app --sdk-root ~/src/alp-sdk

# Against YOUR project, including the steps that write generated files,
# a real build, `run`, `lock`, and a support bundle.
scripts/tan-surface/run.sh --project ~/src/my-app --sdk-root ~/src/alp-sdk --allow-mutate

# Drive a specific binary rather than whatever `tan` is on PATH.
scripts/tan-surface/run.sh --tan ./dist/tan/tan --sdk-root ~/src/alp-sdk

# Fail the run if anything was SKIPped, not only if something failed --
# "green" then means "the whole surface ran", not just "nothing that ran failed".
scripts/tan-surface/run.sh --sdk-root ~/src/alp-sdk --allow-bootstrap --strict
```

Exit status: **0** everything met expectation · **1** something failed *or* a
known-broken step started passing (or, under `--strict`, something was
skipped) · **2** the harness could not start.

**"Read-only" is a real, enforced promise.** Every step that WRITES into
`--project` — `build --materialise`, a real `build`, `run`, `lock`, and
`support-bundle` — is gated on `--allow-mutate` exactly like `generate` and
`clean` are, not only the steps whose phase name says "generate". Without
`--allow-mutate`, a `--project` run touches nothing but reads and the
in-sandbox scratch fixtures under `--work`.

**Presence on PATH is not proof `--tan PATH` (or a bare `tan`) IS tan.** The
harness runs `$TAN --version` and requires it to print `tan <version>` before
doing anything else, and aborts naming what it actually got otherwise. If
you're invoking this script itself as `bash scripts/tan-surface/run.sh` on
Windows, the same caution applies one level up: a `bash.exe` on `PATH` there
is often the WSL launcher stub (installed the moment "Windows Subsystem for
Linux" is enabled, even with zero distributions registered) — a real,
executable, `PATH`-resolvable binary that is not bash. Run `bash -c 'echo ok'`
and confirm you get `ok`, not a UTF-16LE "no distributions installed" banner,
before trusting anything this script goes on to report from inside it.

## The five result buckets

| Bucket | Means |
|---|---|
| `PASS`  | met its expectation |
| `FAIL`  | did not — a regression, or an expectation that needs correcting |
| `XFAIL` | a known defect, still broken. Pinned to an open issue. Run stays green |
| `XPASS` | **a known defect now passes.** Retire the entry from `cases.sh` |
| `SKIP`  | a precondition was absent (no bootstrapped workspace, no `renode`, …) |

`XPASS` fails the run on purpose. A harness that stays green while its own
expectations rot is worse than no harness.

`SKIP` does NOT fail the run by default — a `--phase` run, a host with no
`renode`, and a not-yet-bootstrapped workspace are all legitimate reasons the
whole surface did not walk, and none of them are a defect. The summary always
says how many were skipped and offers `--strict` (see above) for the case
where you specifically want "0 skip" enforced too.

## Build and workspace phases

`build`, and the `quality`/`migrate`/`lock`/`kconfig` half of `workspace`, need a
bootstrapped west workspace. The harness **detects one automatically**: if the
checkout you passed as `--sdk-root` sits inside a directory holding
`.west/config` and `.venv`, those phases run. Otherwise they `SKIP` — loudly,
never silently.

To have the harness build one for you, pass `--allow-bootstrap`. Read this first:

> `tan bootstrap` **moves** the checkout into its workspace (tan-cli#185) and
> writes a machine-global default at `~/.alp/sdk-default`.
>
> So `--allow-bootstrap` **copies your SDK into the sandbox and bootstraps the
> copy** — your checkout is never moved. The global default it writes is saved
> beforehand and restored on exit (removed if it did not exist). Expect several
> GB of disk and 10+ minutes.

## Adding a command

Everything lives in `cases.sh`, grouped by phase. The primitives:

```sh
step   "<label>" <exit|any>            -- <args...>   # assert the exit code
step_out "<label>" '<regex>'           -- <args...>   # assert the output
step_out_rc "<label>" <exit|any> '<regex>' -- <args...> # assert BOTH, one invocation
envelope "<label>"                     -- <args...>   # --format json contract
xstep  <issue> "<label>" <exit-broken> -- <args...>   # known defect, by exit code
xstep_out <issue> "<label>" '<regex-while-broken>' -- <args...>
skip   "<label>" "<reason>"
```

`step`, `xstep`, `step_out`, `step_out_rc`, and `xstep_out` all take an
optional `--timeout N` before the `--`.

Prefer `step_out_rc` over a separate `step` + `step_out` pair on the SAME
command: `step_out` alone never checks the exit code, so a command that
prints the expected line while exiting wrong (or crashing) still passes —
measured on `run`, where a stub that printed the built-message while exiting
5 scored a clean PASS on "run does not flash the board". A separate `step` +
`step_out` pair closes that but re-runs the command twice, which is real cost
on a multi-minute `build`/`run`/`renode`; `step_out_rc` asserts both from ONE
invocation. `xstep` and `xstep_out` also refuse to score a **timeout** (RC
124) as XPASS — "not the broken exit code" is not evidence a defect is fixed,
and a hang is the single likeliest failure shape for the remaining `renode`
`xstep` (#448).

`envelope` checks `command`/`ok`/`exitCode`/`data` are present, that
`ok == (exitCode == 0)`, and — load-bearing in a different way — that the
envelope's own `exitCode` matches the REAL process exit code `_invoke`
captured (#327: an envelope can be internally consistent and still lie about
what actually happened). Missing `project`/`issues`/`sdk` is reported as a
note, not a failure, because not every command carries all three
(`faultdecode` has no `sdk`).

`check_command_surface` (called once, at the top of `phase_discovery`) is not
a case for a single command; it proves the `KNOWN_COMMANDS` list above it
still matches `tan --help`'s own output, in both directions. It exists
because this file's own command list is otherwise exactly the kind of
hand-kept surface `tan/cli.py`'s `_SUBCOMMAND_NAMES` derivation was written
to stop being (a command landing here uncovered is a silent gap, not a loud
one) — this is the shell-side equivalent, run against the binary rather than
the source.

Ordering in `cases.sh` is a dependency chain, not taste: `size` needs a build,
`build` needs a workspace, and `teardown` destroys what the earlier phases made,
so it runs last.

## Why this is not `scripts/e2e-full.sh`

`e2e-full.sh` is a **release regression** harness: it hijacks `$HOME`, wipes its
tree every run, and drives seven commands deeply (`--version`, `bootstrap`,
`init`, `doctor`, `build`, `generate`, `examples`) to prove a specific set of
already-fixed bugs stays fixed. Every assertion in it was first validated
against a known-bad asset.

This one is the other axis — the whole surface, shallowly, on your actual
machine, repeatably. Neither replaces the other, and merging them would make
both worse: the regression suite needs a hermetic home to mean anything, and
this one needs your real environment to mean anything.

## Known-defect ledger

Expectations target **tan 0.5.2** (re-derived against `dev`, 2026-08-08 — see
"Re-derived against dev" below for what moved since 0.5.1). One `xstep`
remains:

| Issue | What is asserted as broken |
|---|---|
| [#448](https://github.com/alplabai/tan-cli/issues/448) | `renode` never reaches the app console (command is slated for removal) |

### What this harness has already caught

Written against 0.5.0, first run against 0.5.1 — it reported **8 XPASS** and
exited non-zero, naming every entry to retire. Those are now positive
assertions in `cases.sh`, so the fixed behaviour is what gets defended:

| Issue | Fixed in 0.5.1 | Now asserted as |
|---|---|---|
| [#453](https://github.com/alplabai/tan-cli/issues/453) | `kconfig` resolves the bootstrapped workspace | `kconfig` exits 0 |
| [#454](https://github.com/alplabai/tan-cli/issues/454) | `quality`/`migrate` gained their required flags | `--profile quick` / `--check` exit 0; omitting them exits 2 |
| [#455](https://github.com/alplabai/tan-cli/issues/455) | `diff` schema-validates | exit 2 on a board `validate` rejects |
| [#456](https://github.com/alplabai/tan-cli/issues/456) | `debug-config` stopped guessing | refuses pre-build (exit 2); infers `zephyr-mcu` post-build |
| [#457](https://github.com/alplabai/tan-cli/issues/457) | `generate --all` is re-runnable | second run exits 0 |
| [#458](https://github.com/alplabai/tan-cli/issues/458) | `pinmux` prints its issues | the error line must appear |
| [#469](https://github.com/alplabai/tan-cli/issues/469) | no stringified `None`; bootstraps in place | `--dry-run` exits 0 |
| [#470](https://github.com/alplabai/tan-cli/issues/470) | `renode` honours `--project` | `--project` used throughout |

#469 and #470 were found *by writing this harness*, not by the manual pass that
produced the rest.

### Two lessons the harness learned about itself

**Assert the message, not just the exit code.** Under #470's bug, `renode`'s
"there is no build yet" and "pick a core" cases both collapsed into the same
CWD-lookup failure — an exit-code-only check passed while measuring nothing.

**An `xstep_out` pattern must not survive the fix.** The #458 entry matched
`^pinmux: family=v2n pads=0$`, and 0.5.1 fixed the bug by *adding* an error line
below that one. `grep` is line-oriented, so the pattern still matched and the
harness reported XFAIL on an already-fixed defect — a false "still broken",
which is the same silent staleness the XPASS mechanism exists to prevent.

Two more findings have no `xstep` because they are not expressible as a single
command's exit code or output:
[#459](https://github.com/alplabai/tan-cli/issues/459) (`--print-env` before the
first bootstrap) needs an un-bootstrapped checkout, which the harness will not
manufacture; and
[alp-sdk#1224](https://github.com/alplabai/alp-sdk/issues/1224) (an unknown chip
token reaching `alp.conf`) is an SDK-side schema gap.

### Re-derived against `dev` (2026-08-08)

16 commits landed on `dev` since this file was last checked against a real
binary. Every assertion below was re-measured against the real `tan` built
from `dev` and a real alp-sdk `dev` checkout, not carried over from 0.5.1 on
the assumption it still held — two of them did NOT:

- **`pinmux --family <unknown>` exits 0, not 2.** `pinmux_cmd.py`'s
  file-not-found branch appends a `warning`-severity issue and leaves the
  exit code at its initial `SUCCESS`, by design (its own comment calls this
  the same "I don't know" shape every unreadable table gets) — a DIFFERENT
  code path from `v2n`'s "parsed with zero pads" case right above it, which
  DOES exit 2 because the table parsed successfully and was found empty.
  Measured directly; this file now asserts exit 0 for the unknown-family
  case and does not "fix" it to 2.
- **`renode` without a build, and `renode` without `--core` on a
  multi-slice manifest, both exit 1 (`RUNTIME_FAILURE`)**, not merely "not
  0" — every `RenodeError` on these paths falls through `fail_sdk()`'s
  default exit code. Verified against `renode_plan.py` and, for the
  no-build case, against the real binary.
- **`run` without `--flash`, once built, exits 0** and prints the literal
  line `run: built; pass --flash to program the board.` — `run_cmd.py`'s
  `BUILD_ONLY` action returns the build's own exit code, `SUCCESS` for a
  build that actually succeeded. The old `'pass --flash|built'` alternation
  is now the exact line, so a bare "built" substring elsewhere in the
  output can no longer satisfy it.
- **`debug-config --preview` post-build exits 0** for the target-inference
  case (`_success_text`'s preview branch always returns `ExitCode.SUCCESS`).

None of `swd_probe`'s new `openocd_usb_location`/`pyocd_uid` flags, or
`tan build`'s new refusal on a `cores.<id>.app` that resolves to a
nonexistent directory (`build.app-dir-missing`, #523), are reachable from
this harness today: `flash`/`swd_probe` are out of scope by design, and every
scaffolded/example `app:` this harness exercises resolves to a real
directory. Noted here so the next re-derivation does not have to rediscover
that they were checked and found not to apply, rather than skipped.

Full-surface phases needing a real bootstrapped workspace (`quality`,
`migrate`, `lock`, `kconfig`, the real `build`/`run`/`renode`) were **not**
re-run end-to-end this pass — `--allow-bootstrap` costs several GB of disk
and 10+ minutes and needs a Zephyr toolchain this environment did not carry.
Their expectations were re-derived from source (`renode_plan.py`,
`run_cmd.py`, `debug_config_cmd.py`, `exit_codes.py`) and, where a lighter
proxy exists, measured directly against the real binary and a real alp-sdk
`dev` checkout: `build --materialise` (needs no workspace) and the pre-build
refusals for `size`/`image`/`renode` all passed as asserted. The real
`build`/`run`/`renode` steps carry the same source-derived confidence as the
rest of this section but were not run end-to-end here — run
`--allow-bootstrap` yourself to confirm them on a host that can afford the
disk and time.
