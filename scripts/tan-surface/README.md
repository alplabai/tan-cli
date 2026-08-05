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

# Against YOUR project, including the steps that write generated files.
scripts/tan-surface/run.sh --project ~/src/my-app --sdk-root ~/src/alp-sdk --allow-mutate

# Drive a specific binary rather than whatever `tan` is on PATH.
scripts/tan-surface/run.sh --tan ./dist/tan/tan --sdk-root ~/src/alp-sdk
```

Exit status: **0** everything met expectation · **1** something failed *or* a
known-broken step started passing · **2** the harness could not start.

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
envelope "<label>"                     -- <args...>   # --format json contract
xstep  <issue> "<label>" <exit-broken> -- <args...>   # known defect, by exit code
xstep_out <issue> "<label>" '<regex-while-broken>' -- <args...>
skip   "<label>" "<reason>"
```

Both `step` and `xstep` take an optional `--timeout N` before the `--`.

`envelope` checks `command`/`ok`/`exitCode`/`data` are present and — the
load-bearing part — that `ok == (exitCode == 0)`. Missing `project`/`issues`/`sdk`
is reported as a note, not a failure, because not every command carries all
three (`faultdecode` has no `sdk`).

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

Expectations target **tan 0.5.1**. One `xstep` remains:

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
