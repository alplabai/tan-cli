# SPDX-License-Identifier: Apache-2.0
"""A 3000-line module and a 679-line function must not arrive unnoticed
(tan-cli#408).

The house guideline is 800 lines per module and 50 per function. Nothing
enforced either: there is no `[tool.ruff]`, flake8 or pylint section in
`python/pyproject.toml` and no Python lint job in `.github/workflows/`. The
only acknowledgement anywhere under `python/tan/` is one `# noqa: PLR0911,
PLR0912, PLR0915` on `bootstrap_cmd._run`, which no configured linter would
ever act on.

The measurement that makes this a gate rather than a preference: between
tan-cli#408 being filed and this file being written, with nobody objecting
and nothing failing, every one of the six modules it named GREW.

    doctor_cmd.py      3019 -> 3114
    bootstrap_cmd.py   2658 -> 2781
    core/flash_plan.py 1721 -> 1808
    flash_cmd.py       1652 -> 1783
    _run                630 -> 679 lines

So this is a RATCHET, not a cap. It records what is true today and fails on
growth. It deliberately does NOT fail the 23 modules and 199 functions that
are already over -- a gate that is red on the day it lands gets disabled, and
then it guards nothing at all.

## Why a pytest gate and not a ruff job

`python -- pytest across python/` is ALREADY a required context on `main` and
`dev`. A new CI job would have to be added to the required list to matter,
and adding a required context blocks every open PR until it has run on each
of them. This runs inside a gate that is already required, so it starts
enforcing on the next PR with no protection change. `pyproject.toml` gaining
a `[tool.ruff]` section is still worth doing for editor integration; it is
not what makes a rule enforced here.

## How to change these numbers

Lower them, freely, whenever a split lands -- that is the point. Raising one
means a module grew, and needs a reason in the diff that raises it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: `python/tan`, found from this file rather than from a cwd, so the gate is
#: identical however pytest was started (tan-cli#423's lesson).
_PACKAGE = Path(__file__).resolve().parents[2] / "tan"

#: The house guideline. Any module NOT in `_MODULE_BUDGET` must be under it --
#: that is what stops a 24th oversized module joining the list silently.
_MODULE_CAP = 800

#: The guideline for a function body, same role for the function ratchet.
_FUNCTION_CAP = 50

#: Every module over `_MODULE_CAP` as of 2026-08-04, with its measured line
#: count as its ceiling.
#:
#: Four entries were re-measured when this branch caught up with `dev`: the
#: ratchet was calibrated before tan-cli#426 merged, and that PR grew exactly
#: these four. Each still pins the file's EXACT current size, so the next
#: unnoticed growth still fails -- the baseline moved, the gate did not
#: loosen. `build/execute.py` 902 -> 941 (#419's Zephyr-SDK message),
#: `generate_cmd.py` 1101 -> 1147 (#420's refused-emit cleanup),
#: `build_cmd.py` 1555 -> 1559 (#407's ladder-divergence helper),
#: `validate_cmd.py` 1092 -> 1093 (a corrected #262 docstring). Recorded per file rather than as a single "worst
#: module" number so a split that shrinks one file cannot be spent widening
#: another.
#:
#: The UX polish sweep's own raises are recorded per entry below, against the
#: `dev` numbers this branch rebased onto -- `cli.py`, `build_cmd.py`,
#: `doctor_cmd.py`, `init_cmd.py`, `sdk_cmd.py`. Each was re-measured after
#: the rebase rather than carried over from the pre-rebase branch, since four
#: of the five had moved on `dev` in between.
_MODULE_BUDGET: dict[str, int] = {
    # 3127, not 3114: the 27-camelCase-issue-code fix added `kebab_check_name`
    # (+ its `_CAMEL_BOUNDARY` regex), the one shared place a `Check.name`
    # becomes a kebab issue-code suffix, used by both `checks_to_issues()` here
    # and `support_bundle_cmd._doctor_issues()`.
    # 3135, not 3127, as of the tan-cli#464 rework: `doctor` now threads
    # `resolve_sdk_root_ladder`'s `SdkRootResolution` through named fields
    # instead of a tuple unpack, and appends `sdk.global-default-foreign-
    # project` alongside `sdk.project-pin-unresolved` -- the same warning
    # `sdk current`/`tan build` already disclose, wired into the "0 issues"
    # report doctor exists to be honest about.
    # 3192, not 3135, as of the UX polish sweep's doctor work: 116 insertions
    # against 59 deletions, net +57, and the deletions are the point -- the
    # whole text renderer moved OUT to `tan/core/doctor_render.py` (the
    # wrapping, colouring and footer are pure formatting and belong beside
    # `text_layout`, not in the command). What stayed and grew is the
    # streaming path: `_collect` gained an `on_check` callback and this module
    # gained `_print_check`, so each check's block prints the moment it is
    # produced instead of the whole report printing after the last probe --
    # a wedged probe is now named by the last line on screen rather than
    # leaving a blank terminal. Plus the `--fix`/`--no-color` plumbing and the
    # `TEXT_WRAP_MIN_WIDTH` floor the renderer is called with.
    #
    # 3289, not 3192, as of the five-finding review pass on that same doctor
    # work, PLUS the correction pass on the review's own written rationale
    # (measured, not inferred: a legacy-codepage stderr never reaches this
    # stream -- `_reconfigure_stdio()` already forces utf-8/strict before any
    # command runs -- see `_print_stream_lines`'s docstring for the real
    # mechanism): `run_fix` gained its own `on_check` (mirroring `_collect`'s
    # `_add` closure) so `--fix`'s checks stream per-tool instead of only
    # after all four tools finish -- previously the single largest blank-
    # terminal window in the command. BOTH stderr-print call sites
    # (`_print_check`'s per-check block and the footer, which used to sit
    # outside the `try` entirely) now route through the ONE guarded
    # `_print_stream_lines`, catching `UnicodeError`/`OSError`/`ValueError`
    # (a lone-surrogate filesystem path, a closed pipe, a stream closed
    # outright) instead of discarding an already-completed diagnosis or, for
    # the footer, escaping `doctor()` as a raw traceback; the standalone
    # `_note_print_failure` helper (one call site) is gone, inlined into that
    # same guard. `width`/`color` moved out of the `if stream:` guard
    # `_print_check` closed over, which let the docstrings this module and
    # `_collect` carry state their invariants honestly again instead of via
    # an 11-line comment. None of this is the width-wrapping fix itself --
    # that grew `doctor_render.py` (well under its own 800-line cap), not
    # this file.
    #
    # Still 3289, unchanged, as of `explain`/`sdk current` adopting the same
    # wrap seam: `_DOCTOR_TEXT_MIN_WIDTH` moved out to
    # `tan.env.TEXT_WRAP_MIN_WIDTH` so the two new callers and this module
    # share one floor instead of drifting into two, which cost a handful of
    # comment lines recording WHY this module's own width resolution stays
    # unconditional (piped or not) even though the new seam is not (see the
    # comment at the `width = max(...)` call site) -- but the doc-comment
    # block for the constant this module no longer defines (only imports)
    # is gone, not merely moved, so the net measures out to exactly the
    # pre-existing count.
    # 3531, not 3289, as of tan-cli#488 (the eight wrong-verdict defects):
    # `probe_status` (spawn-vs-parse split, `west_resolved_check`'s new `ran`
    # arm), `_is_own_git_checkout` (the nested-git-repo guard for
    # `_git_short_commit`/`_git_behind_upstream`), `_module_importable` (asks
    # the workspace venv's interpreter, not tan's own, for `fdt`), the
    # `schemaVersion`-first read and the host-keyed (not `windows`-bool-keyed)
    # tool list in `_load_manifest`/`_collect`, `zephyr_python_floor`'s
    # three-way fallback split, and the bare-PATH-re-probe fix for `west` --
    # each earns its own paragraph of "why", which is most of the growth; the
    # `doctor()` prologue also grew by one `try:` level (the whole
    # `cwd = Path.cwd()`-onward body, not just `_collect`, now reports
    # `doctor.internal-failure` instead of a raw traceback on a deleted
    # working directory) plus five pre-try defaults for the names its
    # exception handler and final `emit()` read.
    # 3575, not 3531, as of tan-cli#488 ROUND 2 (the six remaining findings a
    # first pass reported fixed but left half-done): `west_check` gained a
    # `resolved_ran` parameter and a new branch (a resolved-but-unspawnable
    # west can no longer report `pass`, closing the one branch defect 1's own
    # `ran` fix did not touch), the `schemaVersion`-mismatch message dropped
    # its trailing period (it is always read back through a string that
    # supplies its own), and `sys.stdin.isatty()` gained an `is not None`
    # guard alongside the existing `sys.stderr` one.
    # 3588, not 3575, as of tan-cli#488 ROUND 3: round 2's `is not None` guard
    # landed only inside `fix_suppressed_issue`'s own local duplicate of the
    # tty check, one call AFTER `doctor()`'s `fix_allowed = fix and
    # can_prompt(...)` had already crashed on the same `None` `sys.stdin` --
    # the real fix moved into `can_prompt` itself (`tan.core.consent`, not
    # counted here), and `fix_suppressed_issue`'s docstring grew a correction
    # explaining why its own copy of the guard stays (per-condition reporting)
    # even though the crash it originally cited is now closed one call site
    # earlier.
    # 3600, not 3588, as of tan-cli#488 ROUND 5: rounds 3/4 guarded
    # `sys.stdin` in `fix_suppressed_issue`'s local tty check but left the
    # NEXT operand of the same `and` chain, `sys.stderr`, unguarded -- a live
    # non-tty stdin with a detached stderr still raised the identical
    # `AttributeError`. `fix_suppressed_issue` gained the matching
    # `sys.stderr is not None` guard plus a docstring paragraph explaining the
    # recurrence.
    # 3612, not 3600, as of tan-cli#488 ROUND 6: rounds 3-5's `is not None`
    # guards stopped a detached (`None`) `sys.stdin`/`sys.stderr` from
    # crashing `fix_suppressed_issue`'s local tty check, but not a handle
    # that EXISTS and simply has no `.isatty()` -- exactly what `sys.stderr`
    # is under `--format json` (`tan.cli.main`'s `_TeeStderr`). The local
    # `sys.stdin is not None and sys.stdin.isatty() and sys.stderr is not
    # None and sys.stderr.isatty()` check is now `stdin_is_tty() and
    # stderr_is_tty()` (imported from `tan.env`, the shared probe tan-cli#288
    # already built for this exact class), plus a docstring paragraph
    # recording the recurrence and why this copy was never itself observed
    # to crash (its own `not json_mode` guard in front) even though the
    # sibling copy in `build_cmd._dispatch` did.
    "tan/commands/doctor_cmd.py": 3612,
    # 2833, not 2781, as of tan-cli#459: `--print-env` used to disagree with
    # `--dry-run` about which workspace a real run would build, on both the
    # workspace-parent-relocation branch AND a `$ZEPHYR_BASE` adoption branch
    # -- fixing both moved real logic into a new module-level
    # `_print_env_outcome` (extracted specifically to keep `_run` AT, not
    # over, its own 679-line ceiling below -- extraction cannot shrink the
    # MODULE total, only move lines off the worst function), plus the
    # one-line `target is not None` gate on the tan-cli#389 orphan refusal
    # (was refusing every in-place re-run of an already-bootstrapped
    # workspace, naming its own non-existent relocation target "None").
    #
    # 2886, not 2953, as of the tan-cli#464 REWORK (measured majors, then an
    # independent design review, against the 2953-line shape immediately
    # above): the directory-scoped `.alp/sdk-path` project pin a relocation
    # used to also write (`_write_project_sdk_pointer`/
    # `_read_project_sdk_pointer`, `RelocationUndo.previous_project_pointer`,
    # `_undo_relocation`'s matching rollback branch) is REMOVED outright --
    # that directory is bootstrap's cwd, the workspace PARENT in the
    # quickstart, not a project, and a bootstrap run from `$HOME` pinned
    # inside tan's own machine-global config dir, silencing the layer-2
    # warning below for essentially every project the user owns. Only layer 2
    # (`writtenFor` + `sdk.global-default-foreign-project`) ships; the
    # removal gives back more lines than the small amount `_print_env_outcome`
    # threading a `foreign_issue` alongside `pin_issue` adds back.
    # 2917, not 2886, as of tan-cli#469: the inline `workspace-orphan-refused`
    # message (three string-literal list items, spliced together by `_refusal`)
    # is now `workspace_orphan_refusal`, a standalone function alongside its two
    # siblings (`workspace_guard_target_occupied_refusal`,
    # `enclosing_west_workspace_refusal`) that already had that shape -- it was
    # the one of the three still inlined. Branches on `target is None` instead
    # of interpolating it unconditionally: unreachable today (`target is not
    # None` already gates the call site, tan-cli#389/#390), but the inline
    # f-string was the exact shape that printed a stringified `None` and then
    # advised dropping a `--workspace` the invocation never carried, and
    # nothing stopped that gate from being loosened again.
    # 2919, not 2917, as of tan-cli#516: `reconcile_west_manifest_path`'s
    # write moved from a bare `Path.write_text` + `os.replace` (no `fsync` at
    # all) to the new shared `atomic_write_text` (`tan/core/atomic_write.py`),
    # plus a comment explaining the durability gap the old shape had. Net
    # growth is small -- the call site itself SHRANK (the caller's own
    # temp-cleanup `except` is gone, `atomic_write_text` does its own) -- the
    # import line and the expanded rationale comment account for the +2.
    # 3034, not 2919, as of tan-cli#495: five defect fixes plus their
    # rationale. The largest single share is prose, not logic -- defect 2's
    # `_zephyr_base_will_adopt` gate, defect 3's `_relocation_target_occupied`
    # (which records the two `.west`-exemption cuts that were tried and
    # reverted), defect 4's stdout/stderr split, defect 5's
    # `_append_git_config_override`, and defect 7's `_existing_venv_bin_dir`.
    # Two functions LEFT this file in the same change (`_manifest_points_at`,
    # `_same_directory`, both moved down to `tan/core/bootstrap.py`), so the
    # figure is net of that removal.
    "tan/commands/bootstrap_cmd.py": 3034,
    # 2042, not 1890, as of tan-cli#495: defect 6's `manual_install_posix`
    # field, its parse arm, its render arm and the three-element fallback
    # tuple transcribed from the oracle (`manifest.rs:712-718`) -- the
    # fallback prose alone is ~30 lines; plus defect 8's `_is_drive_relative`
    # and its message; plus `manifest_points_at`/`same_directory` MOVED DOWN
    # here from `tan/commands/bootstrap_cmd.py` so `tan/core/venv.py` stops
    # reaching up into the command layer through four deferred imports.
    # 2049 after review: the fallback's POSIX notes are re-transcribed from
    # `contract/fixtures/bootstrap/manifest.json` (the source the field-for-
    # field gate holds them against) rather than the oracle's older copy, and
    # carry the comment recording the one clause that must diverge because
    # `test_sdk_onboarding_dead_end.py` refuses a shipped `tan sdk switch`.
    "tan/core/bootstrap.py": 2049,
    # 1987, not 1808, as of tan-cli#486 and its review round: two guard
    # functions (`validate_commander_path`, closing the J-Link Commander
    # newline/`"`-injection hole on the artefact/atoc/serial interpolations,
    # and `validate_openocd_word`, closing the OpenOCD `-c` Jim Tcl `[...]`
    # command-substitution hole on the artefact) plus their call sites and
    # docstrings across `jlink_commander_script`, `plan_swd_probe`,
    # `plan_alif_mram_jlink` and `flow_d_preflight_script`; then the review
    # round added `openocd_program_word` (braces the OpenOCD artefact word
    # against Jim Tcl's own word-splitting/backslash substitution),
    # `validate_identifier`'s `destination` override (so `jlink_serial`'s
    # refusal names the J-Link Commander script it actually reaches, not
    # OpenOCD), `validate_commander_path`'s `"` rejection, and hoisting
    # `jlink_serial` validation into `validate_flow_d_shape` so a hostile
    # value refuses at `--dry-run` time instead of only deep inside
    # `plan_alif_mram_jlink`/`flow_d_preflight_script`.
    # 2107, not 1987, as of tan-cli#487: `_resolve_dev_root` (a PURE lexical
    # `/dev/` traversal-collapse check, replacing the bare `startswith`
    # `plan_yocto_wic` used to trust) plus `YOCTO_WIC_METHODS`/
    # `_KNOWN_UNSUPPORTED_COMPRESSION_SUFFIXES` and the `compress` vocabulary
    # refusal it feeds -- a still-compressed stream must never reach `dd` raw
    # -- plus the `swd_probe` success messages now withholding `@ {base}` for
    # a non-`.bin` artefact on both the J-Link and openocd/pyocd arms.
    # 2123, not 2107, as of the tan-cli#487 REVIEW round (finding 2): the
    # `compress` vocabulary refusals moved from ahead of tool selection into
    # the `elif dd:` arm (bmaptool decompresses natively and never reads
    # `compress`, so those refusals must not fire on that arm at all), which
    # nets a few lines of comment explaining why, over and above the code
    # move itself.
    # 2161, not 2123, as of tan-cli#511: `openocd_program_word` braces every
    # artefact unconditionally instead of only when it carries whitespace or
    # a backslash -- the conditional predicate never actually preserved the
    # oracle parity its docstring claimed (both frozen fixtures are captured
    # on `CAPTURE_PLATFORM = "win32"`, so their `<ORACLE-ROOT-0>` scratch
    # root always carries a backslash; the predicate could never once
    # observe "no backslash" on the platform it was pinned to). Net growth
    # is almost entirely docstring: the rationale for why unconditional
    # bracing was measured-safe, the Fable-advisory finding that made it so,
    # and the one Tcl brace-counting glass jaw (an odd trailing backslash
    # count) it deliberately still leaves fail-safe.
    # 2199, not 2161, as of tan-cli#513: `jlink_commander_script` gains an
    # optional `serial` parameter that emits a leading `SelectEmuBySN` line
    # (matching Flow D's own shape) and `plan_swd_probe`'s J-Link arm now
    # resolves/validates `flash_args.jlink_serial` the same way Flow D
    # already does, instead of silently dropping it -- net growth is mostly
    # docstring explaining why this mirrors Flow D's guard verbatim.
    # 2263, not 2199, as of the tan-cli#513 REVIEW round: (1) the J-Link arm's
    # argv now also carries `-SelectEmuBySN {serial}` -- not only the
    # Commander script's leading line -- because that arm's own `-AutoConnect
    # 1` (Flow D's argv has none) means JLinkExe can start connecting before
    # the script is ever read, so the script line alone did not provably
    # precede the connect; and (2) `jlink_serial` is now resolved/validated
    # BEFORE the J-Link-vs-openocd/pyocd arm split, with an explicit refusal
    # when it is set but the resolved arm is openocd/pyocd (neither has a
    # probe-serial selector of its own), closing both the host-dependent
    # refusal (a hostile value was refused only under `--dry-run`, which
    # always forces the J-Link arm) and the same accept-and-ignore shape #513
    # fixed for the J-Link arm, previously still open one branch over. Net
    # growth is comments explaining both, plus one new refusal message.
    # 2313, not 2263, as of tan-cli#520: `plan_swd_probe`'s J-Link arm calls
    # `validate_flow_d_preflight_args(fa, method="swd_probe")` at plan-build
    # time (so a malformed `expect_dpidr`/`jlink_device` pairing surfaces
    # under `--dry-run` too, mirroring Flow D's own plan-time call) and its
    # openocd/pyocd arm gained the same accept-and-ignore refusal #513 already
    # gave `jlink_serial` on that arm, one field over: `expect_dpidr`/
    # `jlink_device` are JLinkExe-only concepts, so landing on openocd/pyocd
    # with either set must refuse rather than silently drop the wrong-board
    # guard. `validate_flow_d_preflight_args`/`flow_d_preflight_script`
    # themselves gained an optional `method` parameter (defaulting to
    # `FLOW_D_METHOD`, so every Flow D call site is untouched) so the refusal
    # text names the right backend -- reused, not copied, into a second
    # checker. Net growth is almost entirely docstring/comment explaining the
    # reuse and the two new refusal shapes; the two new guard bodies are a
    # handful of lines each.
    # 2390, not 2313, after the fixture-parity finding on the same issue:
    # `jlink_device` on `swd_probe` ALREADY meant the write's own `-device`
    # profile (`_resolve_jlink_device`), oracle-pinned with no `expect_dpidr`
    # anywhere near it (`tests/parity/…jlink-bin-artefact-uses-loadbin`) --
    # pairing it with `expect_dpidr` the way Flow D's DIFFERENT `jlink_device`
    # is paired moved that frozen fixture's answer (measured, then reverted).
    # `validate_flow_d_preflight_args` gained a keyword-only `require_device_
    # key` (default `True`, Flow D's shape unchanged) and `flow_d_preflight_
    # script` a keyword-only `read_device` override so `expect_dpidr` alone
    # arms `swd_probe`'s preflight, reusing the ALREADY-RESOLVED write device
    # instead of a second `flash_args` key -- `FlashPlan` gained one new
    # optional field, `preflight_device`, to carry it from `plan_swd_probe` to
    # the caller without a second resolve. Net growth is mostly the docstring
    # explaining why the naive pairing was wrong and what replaced it.
    # 2410, not 2390, as of the tan-cli#520 REVIEW round (BLOCKER 1):
    # `flow_d_preflight_script`'s caller-supplied `read_device` (`swd_probe`'s
    # `FlashPlan.preflight_device`, i.e. `_resolve_jlink_device`'s unvalidated
    # return value) reached a Commander script LINE with no charset guard at
    # all -- safe while it only ever reached argv, not once it reached a
    # line-based script an embedded newline could splice extra commands into,
    # ahead of the wrong-board abort. Now `validate_identifier`-checked,
    # unconditionally, regardless of which of the two sources (Flow D's own
    # paired `jlink_device`, already checked inside `validate_flow_d_
    # preflight_args`, or `swd_probe`'s caller-supplied override) it came
    # from -- net growth is almost entirely the docstring/comment explaining
    # the hole and why the fix applies to both sources rather than one.
    # 2438, not 2410, as of the tan-cli#520 REVIEW round 2 MAJOR fix:
    # `_resolve_jlink_device` now validates `flash_args.jlink_device` at PLAN
    # time (`validate_identifier`, the same guard `jlink_serial` already gets
    # a few lines below in `plan_swd_probe`) instead of relying solely on
    # `flow_d_preflight_script`'s own write-time-only defensive check --
    # closing a `--dry-run`-vs-real-run disagreement on the identical
    # manifest, plus a second hole where the unvalidated value still reached
    # `data.entries[].message` verbatim on an UNARMED (no `expect_dpidr`)
    # write. Also adds the `SWD_PROBE_METHOD` constant (nit: a bare
    # `"swd_probe"` literal at the one code call site that still had it,
    # where the sibling backend already has `FLOW_D_METHOD`). Net growth is
    # mostly the docstring explaining why plan time, not just write time.
    # 2453, not 2438, as of the tan-cli#520 REVIEW round 3, finding 1:
    # round 2's `validate_identifier` call lived INSIDE `_resolve_jlink_
    # device`, which only `plan_swd_probe`'s J-Link arm ever calls -- so the
    # openocd/pyocd arm accepted an unvalidated `jlink_device` outright, and
    # `--dry-run` (which always forces the J-Link arm) still disagreed with a
    # real run on an openocd-only host for the identical manifest. `plan_
    # swd_probe` now validates `jlink_device`'s charset itself, unconditionally,
    # before the arm split -- mirroring the `jlink_serial` guard a few lines
    # above it -- and `_resolve_jlink_device`'s own call is kept as the
    # documented defensive repeat rather than deleted. Net growth is the new
    # guard plus both functions' docstrings explaining why the earlier
    # choke point was insufficient and why the later one is now redundant
    # but kept.
    # 2458, not 2453, as of PR #528 review nits: the `device_precheck`
    # guard's own comment now says "charset AND type" instead of
    # "charset-only" -- `fa_str_checked` already refused a non-string
    # `jlink_device` (`true`/`-8`/a list/a map) before this line was ever
    # added, the earlier wording just undersold what the hoist actually
    # unified across both arms; a separate stale-drift correction to a
    # related comment in `flash_cmd.py` is its own entry below. Comment-only;
    # net growth is the corrected explanation.
    # 2568, not 2458, as of tan-cli#519: `plan_swd_probe` gained the two new
    # probe-selection fields the OpenOCD/pyOCD arms never had at all --
    # `flash_args.openocd_usb_location` (an `adapter usb location` `-c` word,
    # charset-guarded by `validate_openocd_word`) and `flash_args.pyocd_uid`
    # (a `--uid` argv pair, guarded by `validate_identifier`) -- plus FOUR new
    # wrong-arm refusals mirroring `jlink_serial`'s own #513 shape (each field
    # refused on the OTHER two arms, not just accepted-and-ignored), all
    # hoisted ahead of the `interface`/`target` requirement the same way
    # `jlink_serial`/`expect_dpidr` already are. Net growth is mostly the
    # per-field docstring/comment explaining why the shape is two distinct
    # fields rather than one neutral one (the tools genuinely select probes
    # differently) plus the four refusal messages themselves.
    # 2662, not 2568, as of the tan-cli#519/#522 review round (BLOCKER +
    # MAJOR 2 + two minors): the OpenOCD/pyOCD arm split now resolves the
    # taken arm ONCE (`chosen`, mirroring the `if openocd: ... elif pyocd:
    # ...` argv-build precedence) instead of letting each of the two new
    # wrong-arm refusals ask its own tool-AVAILABILITY question in isolation
    # -- the original #519 shape stayed silent on a host with BOTH openocd
    # and pyocd present, since OpenOCD always wins the arm there but a
    # `pyocd_uid`-only refusal keyed off pyOCD merely being available never
    # fired; `--dry-run`'s J-Link default is now bypassed (real `which()`
    # consulted) specifically when `openocd_usb_location`/`pyocd_uid` is
    # named, so a preview and a real run on the same host agree, instead of
    # `--dry-run` unconditionally assuming J-Link and refusing every preview
    # naming either field; the OpenOCD `-c` word for `openocd_usb_location`
    # is now braced (`openocd_program_word`, reused verbatim) the same way
    # the flash artefact's own `-c` word already is, closing the one
    # remaining unbraced `-c` interpolation this module had; and
    # `validate_pyocd_uid` widens the plain `validate_identifier` charset
    # guard by exactly one shape, pyOCD's own documented `<plugin>:<uid>`
    # probe selector, which the plain guard's `:` refusal previously
    # over-rejected. Net growth is almost entirely docstring/comment
    # explaining each of the four fixes and why the narrower (not the
    # broadest possible) version of each was chosen.
    # 2699, not 2662, as of the tan-cli#519/#522 review round 3 (a second
    # review pass on the round above, not a new issue): the round-above
    # MAJOR-2 fix only scoped the J-Link side of the arm split's `--dry-run`
    # bypass -- the openocd/pyocd side kept `(inp.dry_run or which(...))`
    # unconditionally, so a manifest naming `openocd_usb_location`/
    # `pyocd_uid` still hit an unscoped "assume the tool is present" bypass
    # on THAT side, and `--dry-run` disagreed with a real run in both
    # directions on a single-tool host (measured: a pyocd-only host preview
    # planned a full `openocd` command line for a tool not installed there;
    # the same host refused a `pyocd_uid` preview a real run would accept).
    # Scoped the same way the J-Link side already was: the unconditional
    # bypass survives only when NEITHER new field is named. Also a MINOR:
    # `openocd_usb_location: "  "` (whitespace-only) passed the Tcl-metachar
    # charset guard untouched and reached OpenOCD as `adapter usb location
    # {  }`, an empty selector the tool would only reject at runtime; now
    # refused at plan time. Net growth is the re-scoped bypass's own
    # docstring plus the whitespace-only guard and its explanation.
    # 2751, not 2699, as of the tan-cli#519/#522 close-out (a NIT, not a new
    # issue): a bare host (no J-Link, no OpenOCD, no pyOCD) that names
    # `flash_args.openocd_usb_location`/`pyocd_uid` used to get two DIFFERENT
    # misleading refusals depending on mode -- `--dry-run` said "taking the
    # J-Link path" (its own bare-host preview default), a real run said "not
    # taking the OpenOCD/pyOCD path" (naming a tool that also is not there).
    # A shared `_SWD_PROBE_NO_TOOL_FOUND` constant plus a `jlink_is_bare_host_
    # fallback` flag (threaded through the J-Link branch's own two wrong-arm
    # checks) and a `chosen is None and new_probe_selector_named` guard (ahead
    # of the openocd/pyocd arm's own two wrong-arm checks) now both raise the
    # SAME "no flash tool found" diagnosis the bottom-of-function fallback
    # already gives this exact host shape when neither field is named. Net
    # growth is almost entirely the two guards' own docstrings explaining why
    # a field-specific "not taking the OTHER tool's path" message is the
    # wrong diagnosis when NO tool resolved at all.
    "tan/core/flash_plan.py": 2751,
    # 1996, not 1829, as of tan-cli#487: `_yocto_wic_block_device_refusal`
    # (the write-time `stat.S_ISBLK` gate `_resolve_dev_root` above cannot
    # perform -- it is pure), `_timeout_stderr` (folds a killed child's
    # partial captured output into the timeout report instead of discarding
    # it), the `_execute_message` gate fix so a text-mode run surfaces a
    # tan-authored diagnosis instead of a bare `flash command failed`,
    # absolutising `ctx.sdk_root` so an artefact anchored on a relative
    # `--sdk-root` resolves against the SAME base the flasher spawns from,
    # and gating Flow D's SETOOLS auto-sign on the SAME confirm check the
    # MRAM write it feeds already requires.
    # 2075, not 1996, as of the tan-cli#487 REVIEW round: finding 1 narrows
    # `_yocto_wic_block_device_refusal`'s ENOENT fail-open to "target's
    # parent is `/dev` itself" instead of any target, with a new refusal
    # message and its rationale; finding 4 absolutises `resolved_sdk` ONCE,
    # before `venv_bin_dir`/`west_workspace_dir` as well as `ctx.sdk_root`
    # (the original fix only covered the latter); finding 5 threads a
    # `captured` flag through `_half_lines`/`_pipeline_stderr`/
    # `_timed_out_stderr` so an uncaptured (text-mode) pipeline failure does
    # not emit a body-less header; and nit 1 adds a TOCTOU-boundedness
    # comment beside the write-time gate.
    # 2098, not 2075, as of tan-cli#487 defect 7: the aggregate "did anything
    # flash" check after `_flash_entry`'s loop split into a THIRD branch --
    # `plan.targets` non-empty but every one of them was skipped INSIDE
    # `_flash_entry` (an unresolved `TBD` flash_arg, no flash_method, or a
    # missing tool under `--skip-missing-tools`) -- so it stops reporting
    # `flash.nothing-matched` on a run that carried no `--core`/`--helper`
    # filter and genuinely matched a target. New code `flash.entries-skipped`,
    # `ok`/exit code unchanged (still SUCCESS). The five oracle-parity cases
    # this used to share a wrong answer with moved to
    # `tests/commands/test_flash_command.py` (see their own docstring for the
    # divergence rationale).
    # 2112, not 2094, as of tan-cli#511: `_flash_entry` gained a keyword-only
    # `yocto_wic_stat` (default `os.stat`, threaded into
    # `_yocto_wic_block_device_refusal`'s own `stat_fn`) plus its docstring
    # addition, so a test can reach the write-time block-device gate's real
    # call site with an injected mode -- portably, on every CI platform,
    # instead of needing a real regular file under a literal `/dev/`-rooted
    # path (Linux-only tmpfs `/dev/shm`; neither macOS nor Windows have an
    # equivalent).
    # 2170, not 2112, as of tan-cli#512: the read-only DPIDR preflight moved
    # ahead of the SETOOLS auto-sign inside `_flash_entry` (a wrong-board
    # refusal must not have already mutated the customer's SETOOLS install),
    # plus its own docstring explaining the ordering invariant and its old
    # call site's replacement comment; and the wrong-DP-ID message now names
    # the ACTUAL SW-DP ID the probe reported, not only the expected one
    # (`_dp_id_value`, `_DP_ID_RE` gained a capture group). A #514 change
    # (resolving `swd_probe`'s `jlink_device` from SDK metadata ahead of
    # `flash_plan`'s `_DEFAULT_JLINK_DEVICE` fallback) landed and was
    # REVERTED in the same round: #514's premise was wrong -- `swd_probe`
    # only ever flashes the GD32 supervisor MCU on E1M-X V2N/V2N-M1
    # (`helper_firmware:` in alp-sdk metadata, never a SoC-core slice), so
    # `GD32G553MEY7TR` is the CORRECT device, not a foreign default, and
    # substituting the SoC's generic attach profile there would have been a
    # raw memory write misreported as a successful flash.
    # 2207, not 2170, as of tan-cli#520: `_flash_entry` gains a `swd_probe`
    # call site for the read-only DPIDR preflight, placed immediately before
    # the one spawn (`_execute`) that can write for this entry -- there is no
    # earlier mutating step to hoist ahead of on this path (unlike Flow D's
    # SETOOLS auto-sign, #512), so this position already satisfies "before
    # anything that writes or mutates" without needing a hoist of its own.
    # `_flow_d_preflight` (the shared runner both backends now call) gained an
    # optional `method` keyword (default `FLOW_D_METHOD`) threaded into every
    # one of its refusal messages, replacing the hardcoded MRAM wording with
    # backend-neutral "write"/"write to" phrasing so a `swd_probe` refusal
    # does not claim to be aborting an MRAM write it was never going to make.
    # Net growth is mostly comment explaining the ordering invariant and the
    # call-site's own no-op-when-unarmed safety argument.
    # 2217, not 2207, same round: `_flow_d_preflight`/its `flow_d_preflight_
    # script` call site gained a keyword-only `read_device` passthrough
    # (`plan.preflight_device`, `swd_probe`'s call site) so the preflight can
    # use the already-resolved write device instead of a second `flash_args`
    # key -- see the `flash_plan.py` entry above for why that key was already
    # taken.
    # 2290, not 2217, as of the tan-cli#520 REVIEW round: BLOCKER 2 re-gates
    # the swd_probe preflight call site on `plan.preflight_device is not
    # None` instead of `method == "swd_probe"` alone, so an openocd/pyocd-arm
    # entry no longer derives Flow D's PAIRED `require_device_key` shape by
    # accident (a working manifest used to hard-fail at write time only,
    # disagreeing with a green `--dry-run`); minor 3 branches `_flow_d_
    # preflight`'s refusal wording on `method` so Flow D's original "write
    # MRAM" phrasing survives byte-for-byte instead of silently becoming
    # backend-neutral prose nothing pins; and the review's own design point
    # adds a NEW non-fatal `flash.dpidr-preflight-unarmed` warning `Issue`
    # (`_Entry.preflight_unarmed`, read by `_run`'s own entry loop) for a
    # confirmed swd_probe J-Link write that ran with no `expect_dpidr` armed,
    # so that silent gap now has a signal without making the field mandatory.
    # 2298, not 2290, same round: the duplicated `method == "swd_probe" and
    # plan.preflight_device is not None` condition (nit: two copies that had
    # to stay in lockstep) is now one local, `swd_probe_took_jlink_arm`, read
    # by both the preflight call site and the unarmed-warning signal; the
    # bare `"swd_probe"` literal there is now the imported `SWD_PROBE_
    # METHOD` constant.
    # 2312, not 2298, as of the tan-cli#520 REVIEW round 3, finding 2: the
    # `flash.dpidr-preflight-unarmed` warning built above was appended to
    # `issues` only -- `_run`'s caller prints only `text_lines` in the
    # DEFAULT, non-JSON mode, so a plain `tan flash` gave no signal at all
    # that the wrong-board guard never armed. The warning message is now
    # built once and appended to both `text_lines` and `issues`, matching
    # `flash.entries-skipped`'s own shape a few lines below. Net growth is
    # the new `text_lines.append` call plus the comment explaining why the
    # warning was JSON-only and why that mattered on the bench.
    # 2317, not 2312, as of PR #528 review nits: the BLOCKER 2 preflight
    # comment's "the plan-time guard just below `plan_swd_probe`'s arm split
    # only ever checks `expect_dpidr`, never `jlink_device`, on that arm"
    # claim went stale once `flash_plan.py`'s round-four hoist
    # (`device_precheck`, `flash_plan.py:1225`) started checking
    # `jlink_device`'s charset/type ahead of that same split, on both arms --
    # corrected to say so while keeping the underlying conclusion (no
    # preflight-only meaning for `jlink_device` on the openocd/pyocd arm)
    # unchanged. Comment-only; net growth is the correction.
    # 2371, not 2317, as of tan-cli#522: Flow D's static, plan-time
    # `ok_message` used to claim `verified and PIN-reset` even when the
    # transcript showed `Failed to halt CPU` -- an intent, not an observed
    # outcome (the same class #487 defect 6 already fixed for `swd_probe`'s
    # asserted address). `_flow_d_reset_qualified_message` reads the ALREADY-
    # CAPTURED `outcome.stdout`/`.stderr` for the one tail
    # `plan_alif_mram_jlink` always appends and swaps it for an honest
    # sentence when the transcript names a halt failure -- a targeted
    # substring swap, not a general transcript-scraping layer, and (as first
    # shipped) a no-op in text mode, since nothing was captured there --
    # corrected below, this was MAJOR 1 of the next review round. Net growth
    # is the new helper plus its docstring and the one call site in the
    # ok-outcome branch.
    # 2486, not 2371, as of the tan-cli#519/#522 review round, MAJOR 1: text
    # mode -- the DEFAULT, standalone invocation, not only `--format json` --
    # left `outcome.stdout`/`.stderr` empty in every branch of `_spawn`, so
    # the qualification above never reached an operator reading the console,
    # which is exactly the sentence #522 was filed against. The live-console
    # branch now TEES a written child's combined output (still streamed to
    # the console live, via the new `_Tee` helper on a background thread,
    # instead of only streaming it) -- the wrapped-console branch (no
    # OS-level stderr handle -- a pytest/embedded capture object) now threads
    # its ALREADY-captured `proc.stdout`/`.stderr` through instead of only
    # `print()`-replaying and discarding them; and a `Popen.wait`-timeout on
    # the live-console branch now builds its own `TimeoutExpired` with the
    # tees' partial output (`Popen.wait`'s own carries none) so
    # `_timeout_stderr` sees the same shape on all three spawn variants. Net
    # growth is `_Tee` itself (a `_Drain`-shaped background-thread reader,
    # teeing instead of only draining) plus the docstring explaining why each
    # of the three branches needed a different fix.
    # 2551, not 2486, as of the tan-cli#519/#522 review round 3 (a second
    # review pass on the same change, not a new issue): the first `_Tee` read
    # a TEXT-mode stream in fixed 4096-*character* chunks, which is NOT live
    # -- `TextIOWrapper.read(n)` blocks until `n` characters are decoded or
    # EOF, so a slowly-dribbling child produced no console output for over a
    # second at a time (measured), the opposite of the class's own purpose.
    # `_Tee` now reads the raw BINARY pipe with `read1` (bytes ready, not
    # characters decoded) and decodes them itself, incrementally. Separately,
    # `_Tee.join()`'s timeout defaulted to `None` -- unbounded -- so a killed
    # child's own orphaned grandchild holding the pipe open (a backgrounded
    # `sleep &`, measured) could hang `tan flash` indefinitely past its own
    # `_FLASH_TIMEOUT_S`; `join` now bounds on the same `_DRAIN_JOIN_S` the
    # pipeline's stderr drain already uses. Also a MINOR: the sink-write
    # `except (OSError, ValueError): pass` silently swallowed
    # `UnicodeEncodeError` (a `ValueError` subclass) too, discarding a whole
    # chunk -- including any clean lines it shared -- when the sink's own
    # encoding could not represent one decoded character; it now retries with
    # a lossy re-encode instead of dropping the chunk. Net growth is the
    # rewritten `_Tee` docstring/body (binary read + incremental decode + the
    # bounded join + the narrower exception handling) plus one `flash_plan.py`
    # MAJOR-2 companion fix's own `flash_cmd.py`-side commentary.
    # 2629, not 2551, as of the tan-cli#519/#522 close-out review (three
    # findings, no new issue): MAJOR 1's `_execute_message`/`_half_lines`
    # docstrings were corrected -- an ORDINARY text-mode failure no longer
    # unconditionally leaves `outcome.stdout`/`.stderr` empty now that
    # `_spawn`'s single-tool branches capture the child's transcript in every
    # mode, and the old test asserting that as "unaffected" was split into a
    # narrower still-true case (a genuinely silent child) plus a NEW test
    # guarding the changed case against a real `_spawn` outcome, not only a
    # hand-built `_Outcome`; MAJOR 2's `_Tee` docstring dropped its overstated
    # "nothing is silently withheld" claim and documents the tty-vs-pipe
    # consequence measured on a real pty (tracked as tan-cli#541); MINOR 1
    # documents that `_spawn` joins TWO `_Tee`s, so the real overrun past
    # `_FLASH_TIMEOUT_S` is up to `2 * _DRAIN_JOIN_S`, not one, at both
    # constants' own definitions; and the NIT on `_flow_d_reset_qualified_
    # message` names the one residual risk a bounded transcript join leaves:
    # a truncated transcript falls through to the OPTIMISTIC "verified and
    # PIN-reset" claim, not a refusal. Net growth is docstring/comment plus
    # one new decoder test (`test_tee_decodes_a_multibyte_utf8_sequence_
    # split_across_a_read1_boundary`, MINOR 2) proving the incremental
    # decoder against a naive per-chunk one.
    # +2, tan-cli#478: `SdkInfo.from_resolution` -- the seam that lets
    # `Envelope.__init__` disclose a foreign global default without this
    # command knowing the issue exists -- does not fit on one line here
    # (101 chars against the 100 `[tool.ruff] line-length`), so the ternary
    # wraps. `doctor_cmd.py`/`clean_cmd.py`/`inspect_cmd.py` took the same
    # conversion at 97/92/93 chars and kept their one-liners, which is why
    # only this one moves a number.
    "tan/commands/flash_cmd.py": 2631,
    # 1643, not 1639, as of tan-cli#485: `_emit_cross_core_shmem_cache`'s
    # docstring/body grew to cover `kind: rpmsg` too (alp-sdk #1088's
    # companion half -- `needs_dcache_off` now checks
    # `entry.kind in ("raw_shmem", "rpmsg")` instead of `raw_shmem` alone).
    # This branch carried a stale 1639 from before #485 landed on dev; the
    # rebase re-derived it on the merged tree rather than keeping either
    # side's number by ownership.
    # 1679, not 1643, as of tan-cli#543: the alp-sdk#1241 port adds
    # `_chip_has_driver` and the `som_chips` filter that stopped tan emitting
    # `CONFIG_ALP_SDK_CHIP_DP83825=y`, a symbol declared nowhere -- the live
    # red on eight consecutive `parity` dispatch runs (+41, upstream's own
    # delta), less the 7 lines alp-sdk#1267 MOVED out of this file when
    # `_BLOCK_SLUGS` relocated to `slugs.py`, plus the 2-line docstring
    # correction from alp-sdk#1228. Raised rather than extracted: this file
    # mirrors an upstream module line for line, so a split here would make the
    # next port a hand-merge instead of a diff. Measured, not computed.
    "tan/planner/kconfig.py": 1679,

    # 1607, not 1559, as of the tan-cli#464 rework: `resolve_sdk_root_ladder`/
    # `resolve_sdk_root_wide` return a named `SdkRootResolution` instead of a
    # tuple that would need a fourth positional slot for
    # `foreign_global_default_for` -- the exact silent-drop shape #464 exists
    # to close -- and `build` appends `sdk.global-default-foreign-project`
    # beside `sdk.project-pin-unresolved`.
    # 1612, not 1607, as of the UX polish sweep, two tasks again. Task 2
    # (+3 net): the `--execute`/`--native` port-vs-oracle rationale moved OUT
    # of `--help` and into the module docstring (+5) while the `--execute`
    # help string shrank once that rationale left it (-2); the
    # `_DEFERRED_HELP` and `--native` rewrites are line-count-neutral.
    # Task 3 (+2): both `build.plan-unavailable` refusals (no-SDK, no-
    # board.yaml) gained a next-step sentence naming `tan doctor`/`tan init`/
    # `tan examples`, each appended as its own wrapped string literal rather
    # than lengthening an existing line.
    #
    # 1695, not 1612, as of tan-cli#483: `_missing_app_dirs` (a new
    # module-level function, with docstring) checks every zephyr/baremetal
    # slice's resolved `cores.<id>.app` for existence, and `_dispatch` grew
    # a second held-back-outcome path beside the existing `${TOOLCHAIN_ROOT}`
    # demotion one -- same shape, not a new convention -- to fail just the
    # bad slice rather than the whole plan.
    #
    # 1703, not 1695, as of the tan-cli#483 review round: `_missing_app_dirs`
    # took a `build_root` parameter and now anchors a relative `appDir` on
    # it (not the tan process's own CWD, which broke the moment `tan build`
    # ran from anywhere but the project itself) and reports the anchored
    # absolute path; gained a `command is not None` guard mirroring the
    # `${TOOLCHAIN_ROOT}` demotion filter beside it (a slice the planner
    # already refused a command for must not have its unread `app:` reported
    # as the failure reason instead); and split "does not exist" from "is
    # not a directory" so an existing file/broken symlink is named honestly.
    # Its own docstring was trimmed back to keep the function AT the
    # function-length ratchet's 50-line cap (49 lines) rather than paying
    # that ratchet too, matching this file's own established precedent
    # (`doctor_render.py:render_doctor_footer`, above).
    # 1706, not 1703, as of tan-cli#510: `_missing_tool_issues`'s match
    # dropped its `endswith("` not found")` half (the message now carries a
    # `-- searched ...` tail) and gained its own two-line explanation of why.
    # 1732, not 1706, as of the tan-cli#510 REVIEW round: `_slice_result`
    # gained the new, always-present `resolvedTool` field plus the docstring
    # explaining why it is the one exception to "omitted when absent", and
    # `_text_recap` gained the resolved-tool note it now composes for a
    # failed/cancelled slice (never folded into `reason` -- see both
    # functions' own docstrings).
    "tan/commands/build_cmd.py": 1732,
    # 1720, not 1703, as of tan-cli#488 defect 8: `build()`'s resolution
    # prologue (`Path.cwd()` through `Project.resolved(...)`) moved inside a
    # `try`, with pre-try safe defaults for every name the exception handler
    # and the final `emit()` read -- mirroring `doctor_cmd.doctor`'s identical
    # fix for the same defect class -- so a raise anywhere in it produces the
    # `build.internal-failure` envelope instead of a raw traceback.
    # 1732, not 1720, as of tan-cli#488 round 5 class sweep: `_dispatch`'s
    # `_Heartbeat(enabled=...)` read a bare `sys.stderr.isatty()`, the exact
    # unguarded shape `tan.core.consent.can_prompt` was fixed for -- a
    # detached-stdio `tan build`/`tan run` raised the same `AttributeError`
    # arming the heartbeat, before a single slice ever dispatched. Gained a
    # `sys.stderr is not None` guard plus a docstring paragraph.
    # 1754, not 1732, as of tan-cli#488 ROUND 6: round 5's `sys.stderr is not
    # None` guard stopped a detached (`None`) stderr from crashing this line,
    # but not a stderr that EXISTS and simply has no `.isatty()` -- exactly
    # what `sys.stderr` is under `--format json` (`tan.cli.main`'s
    # `_TeeStderr`). Measured against the real binary: `tan run --format
    # json --sdk-root <sdk>` from a real project crashed with `AttributeError:
    # '_TeeStderr' object has no attribute 'isatty'` (exit 5,
    # `run.internal-failure`) before a single slice dispatched, on every run
    # -- `run_cmd._run` calls `_build` with no `json_mode` of its own, so
    # `_dispatch` always saw the `json_mode=False` default and never
    # short-circuited past this line. `enabled=not json_mode and sys.stderr
    # is not None and sys.stderr.isatty()` is now `enabled=not json_mode and
    # stderr_is_tty()`, importing the shared probe `tan.env` already built
    # for this exact class (tan-cli#288) instead of a fourth hand-rolled
    # copy, which also collapses back onto one line under the 100-column
    # limit. Also corrects six stale `run_cmd._build_then_run` docstring/
    # comment references (a name that was never real in `run_cmd.py`, which
    # has only ever defined `_run`) to the actual name while editing the
    # same paragraphs, and two `run_cmd.py:258` line citations that pointed
    # at `_run`'s docstring line rather than its actual `_build(` call site
    # (`run_cmd.py:267`). Net growth is almost entirely the docstring
    # recording the round-6 recurrence and the residual `json_mode`-
    # threading gap this round deliberately still leaves open.
    # 1783 on the merged tree, MEASURED with `wc -l`: both sides grew this file
    # -- tan-cli#530's resolver on `dev` and #488's doctor/consent guards here --
    # and the auto-merge kept only one side's number.
    # 1908, not 1783, MEASURED on this tree, from the two issues in this PR.
    # tan-cli#547 adds `_toolchain_for_plan` (the LAZY call site: the host
    # toolchain scan runs only for a plan that actually names
    # `${TOOLCHAIN_ROOT}`, which is the property the issue asks the port to
    # preserve rather than merely claim) and threads the resolver's own
    # `toolchain_advice` through the substitution call, replacing the
    # hardcoded `toolchain_root=None` stub. The resolver ITSELF is a new
    # module (`tan/commands/build/toolchain.py`, 219 lines, well under the
    # 800 cap and therefore not budgeted) rather than more of this file --
    # extraction cannot shrink a module total, but it can stop one growing.
    # tan-cli#517 adds `_substituted_app_dirs` plus its
    # `_APP_DIR_SUBSTITUTING_BACKENDS` constant and the `_dispatch` wiring;
    # most of that is the docstring recording WHY the parent fallback is
    # announced and not refused (96 of 105 of alp-sdk's own example core
    # entries take it), why the severity is `info` and not `warning`, and why
    # the probe mirrors `_zephyr_app_dir`'s condition instead of reading the
    # substituted path back out of `command.args`.
    "tan/commands/build_cmd.py": 1908,

    # 1476, not 1440, as of the tan-cli#464 rework: `_resolve_sdk_root_and_tier`
    # returns a named `_SdkRootAndTier` instead of a tuple, and both `renode`
    # entry points (`_run`, `--sim-mode`) append
    # `sdk.global-default-foreign-project` beside `sdk.project-pin-unresolved`.
    #
    # 1501, not 1476, as of tan-cli#470: `--project PATH` used to be accepted
    # and silently dropped for the default build root (resolved from the CWD
    # instead), matching every OTHER command's `--project` handling except
    # this one. `app_path` is now resolved through the same
    # `build_output.resolve_app_base` ladder `size`/`image` already call,
    # fed a native-separator workspace dir and renormalised after (this
    # command does not POSIX-normalise its paths, unlike those two) -- the
    # extra lines are the routing plus the docstring bullet documenting the
    # divergence this fix removes.
    "tan/commands/renode_cmd.py": 1501,
    # 1402, not 1296, as of tan-cli#462 (both rounds): four new `_failure`
    # callers (`_build_manifest_missing_failure`/`_core_unknown_failure`,
    # then the review round's `_target_kind_ambiguous_failure`/
    # `_no_debuggable_target_class_failure`) split `--target-kind`
    # inference's four user-fixable refusals off the old blanket
    # `internal-failure` verdict into their own `VALIDATION_FAILURE` (2)
    # issue codes, plus the call-site branches that pick between them and the
    # pre-#462 `internal-failure` fallback. Raised rather than collapsed into
    # one shared `_validation_failure(generated_at, code, ...)` helper:
    # `test_every_issue_code_is_registered.py` resolves this file's `_failure`
    # calls by requiring a LITERAL `code=` keyword at every call site (it
    # reconstructs `debug-config.<code>` by parsing the AST, not by running
    # the code), so collapsing the four wrappers into one parameterised
    # helper reddens that gate with "N code-position argument(s) could not be
    # resolved to a literal ... a 'code=' keyword argument is not a
    # resolvable code literal" -- each wrapper's own literal is the only
    # shape the gate can verify against the registry.
    # 1402 -> 1535: tan-cli#489 fixed three data-loss/misattribution defects
    # in one bounded change: (1+2) the launch.json write moved from a
    # truncating `open(path, "w")` to a temp-sibling + `os.replace` (matching
    # `bootstrap_cmd.reconcile_west_manifest_path`'s own pattern); (4) split
    # `sdk-identity-key-absent` off a new `_sdk_identity_core_unresolved_issue`
    # for the "no core to index the SDK's per-core identity with" case, which
    # it used to misattribute; (5) `_explicit_core_unknown_failure` closes the
    # silent `--core`-vs-manifest gap `infer_target_kind`'s own guard cannot
    # reach once `--target-kind` is given explicitly.
    # 1535 -> 1643, review round on the same issue: (1) `bool(known_jlink_cores)`
    # guard plus its own explaining comment, so a SoM with no `jlink_device`
    # map at all does not steal `sdk-identity-key-absent`'s correct case;
    # (4/5, symlink+fsync) `_atomic_write_launch_json` replaced the bare
    # temp-plus-`os.replace` with a symlink-resolving, `fsync`'d,
    # mode-preserving write plus a stale-temp sweep -- a real function with a
    # real docstring, not a few extra lines at the call site; (6)
    # `explicit_omissions` threaded through `create_launch_json_write_plan`
    # so `--pre-launch-task ''` actually removes an existing key on a write,
    # not just a fresh draft.
    # 1643 -> 1656, SECOND review round on the same issue: the stale-temp
    # sweep from the round above was DELETED outright (it could delete a
    # concurrent process's own in-flight temp, including one outside the
    # project through a symlinked `launch.json`) rather than shrinking this
    # file; the mode-preservation logic grew instead -- reading the existing
    # file's mode BEFORE the write and applying it AFTER a successful
    # replace (never to the temp itself, so a failed replace's cleanup
    # `unlink` is never blocked by a read-only-mirrored temp), plus the
    # umask-respecting default for a brand first-ever write, plus a guard
    # against `os.fdopen` leaking `mkstemp`'s own fd.
    # 1656 -> 1661, THIRD review round: doc-only corrections (the stale
    # `shutil.copymode` present-tense claim after `shutil` was dropped from
    # the imports; `.gitignore`'s comment split into a `debug_config_cmd.py`
    # entry) plus a single-threaded-CLI caveat on the `os.umask` set-restore
    # -- the list-merge algorithm change that dominates this round's diff
    # lives in `debug_launch.py` below, not here.
    # 1661 -> 1542, tan-cli#516 review round: `_atomic_write_launch_json`
    # (the 119-line function this file grew across the three rounds recorded
    # immediately above) is GONE, not grown again -- the fsync/symlink/mode
    # shape it carried moved into the new shared `tan/core/atomic_write.py:
    # atomic_write_text`, which `bootstrap_cmd.reconcile_west_manifest_path`
    # now also calls, so the write plan's own docstring comment ("`_atomic_
    # write_launch_json`'s own docstring covers the rest") stopped being true
    # the moment #516 gave `reconcile_west_manifest_path` its own unsynchronised
    # copy of the same durability gap this module had already closed once.
    # The call site now reads `atomic_write_text(launch_json_path, plan.content)`
    # directly; nothing here duplicates the durability sequence any more.
    # MERGE (tan-cli#476 + tan-cli#477): dev's 1542 above is #516's number.
    # #477 adds `_invalid_argument_failure` (1542 -> 1596, including its own
    # review round threading `target`/`server` through the two POST-parse
    # refusals). #476 independently adds `_project_not_found_failure` and
    # the guard that reaches it (1542 -> 1597, including its own review
    # round distinguishing "missing" from "exists but is a file"). Neither
    # branch's own figure is the resolved one -- measured after the merge
    # rather than copied from either side, which is the mistake a two-way
    # conflict invites here.
    # 1656 -> 1689, #477 SECOND review round: Major 1 splits the one `try`
    # covering `parse_target_kind`/`parse_server_kind`/`create_launch_draft`
    # into two, so the target+server PAIRING refusal (from `create_launch_
    # draft`) reports the pair it actually has instead of the placeholder --
    # +13 lines. Major 2 does NOT touch behaviour (the `--core`-with-no-
    # manifest gap it names stays open on purpose -- see the inline comment
    # and CHANGELOG) but documents why closing it is not a simple mirror of
    # `infer_target_kind`'s own guard -- +20 lines of comment, 0 of code.
    # Measured after both, not computed from the deltas above.
    "tan/commands/debug_config_cmd.py": 1689,
    # 1329, not 1199, as of tan-cli#544's hand-port audit: three alp-sdk
    # commits land here at once -- `_safe_join` + `PathEscapeError`
    # (alp-sdk#1126's resolve-then-contain path-traversal guard, applied at
    # both `_rendered_bytes` read sites and `render_to_envelope`'s
    # `example_dir`), `_cmake_core_map` (alp-sdk#1287: each Zephyr core's own
    # `--core` rename against its own CMakeLists.txt), and `_scaffold_readme`'s
    # qualified-then-short board-target rewrite plus the `_m33_sm`
    # `west flash --host <board-ip>` rule (alp-sdk#1266). Same mirrored-module
    # reasoning as `kconfig.py` above: raised, not extracted.
    "tan/planner/template.py": 1329,
    # 1341, not 1106, as of tan-cli#494: defects 1-4 -- `_example_source_files`
    # (the `os.walk` with build-output pruning, `onerror`, and the symlink
    # skip), `retarget_board_yaml_cores` with its two span helpers,
    # `_require_complete_tree` with its derived expectation, and the
    # file-naming `OSError`. Each carries the docstring recording what it was
    # measured against, which is most of the growth.
    "tan/core/scaffold.py": 1341,
    # 1150, not 1147, as of tan-cli#457's review round: the overlay guard's
    # `--all` re-run fix had to become content-aware -- reading the existing
    # overlay and comparing it against the banner every tan-emitted one
    # carries -- to tell tan's own prior output from a real hand edit, which
    # the previous 1-line `destination.exists()` check could not. Raised
    # rather than extracted: `_overlay_would_overwrite` is five body lines
    # including its own read-error handling; there is nothing left to split.
    #
    # 1186, not 1150, as of the tan-cli#464 rework: `_resolve_sdk_root` returns
    # a named `_ResolvedSdkRoot` instead of a tuple, and `generate` appends
    # `sdk.global-default-foreign-project` beside `sdk.project-pin-unresolved`
    # -- this command WRITES `build/generated/alp.conf` and the DTS overlays
    # out of whichever checkout resolved.
    # 1303, not 1186, as of tan-cli#498 defects 4 and 5. Defect 4:
    # `_missing_emit_output` treated a zero-byte artefact as proof of failure,
    # which is wrong -- `alp_project.py`'s unscoped per-core emits legitimately
    # write 0 bytes and exit 0 when no core matches the mode's OS class, so the
    # subprocess engine reported `generate.emit-failed`/exit 3 for emits that
    # succeeded (and, under `--output`, then UNLINKED the artefact). The check
    # is existence-based now, which took the probe cleanup into
    # `_ensure_writable` (it unlinks the file it created, closing the ambiguity
    # at its source) and added `_output_stamp` so an untouched pre-existing
    # destination is still refused -- tan-cli#397's guarantee, kept. Defect 5:
    # `resolve_targets` gained the `--all` + explicit `--target` refusal that
    # closes the silent target discard AND the `--core` hole it opened. Most of
    # the growth is the measured rationale each carries, per this file's own
    # "needs a reason in the diff" rule.
    "tan/commands/generate_cmd.py": 1303,
    # 1215, not 1096, as of tan-cli#464 (measured majors, then an independent
    # design review): the `globalDefault` tier gained a `writtenFor`-vs-caller
    # check (`_workspace_under`, `global_default_foreign_project_issue`) and a
    # shared `_read_pointer_json` (`_pointer_target`/`_pointer_written_for`
    # now both read through it, rather than duplicating the same
    # parse-and-degrade logic a second time). `_pointer_written_for` grew
    # again in the same rework's review round: a non-absolute `writtenFor`
    # (measured: an empty string resolved `Path("")` to the process's OWN
    # cwd) is now rejected at the source rather than reaching
    # `_workspace_under` at all.
    #
    # 1265, not 1228, as of the tan-cli#464 stage-2 review round:
    # `_pointer_written_for`'s `Path(value).is_absolute()` was platform-native
    # (`PureWindowsPath` needs a drive, `PurePosixPath` needs a leading `/`),
    # so a legitimate absolute `writtenFor` written by the OTHER platform's tan
    # degraded to "no opinion" -- now accepted when EITHER `PurePosixPath` or
    # `PureWindowsPath` calls it absolute. Also adds `sdk_resolution_issues`,
    # the one shared point `flash`/`size`/`image` now call (from `_error`/
    # `_error_outcome` themselves) instead of each hand-copying the
    # pin-issue/foreign-issue pair only on their happy path.
    # 1266, not 1265, as of the UX polish sweep task 3: the `not-ported`
    # refusal's `text_lines` gained its next step (the `git clone` remedy and
    # the `tan doctor` pointer), +1 net.
    #
    # 1274, not 1266, as of `sdk current` adopting the shared wrap seam:
    # `_run_current` now calls `tan.core.text_layout.wrap_lines` (via
    # `tan.env.wrap_width`) on its assembled text report, +8 net -- an
    # import and a one-line call site, nothing command-local. A first pass
    # here carried a command-local `_wrap_current_text` plus
    # `_RECORD_LINE_PREFIXES`/`_ISSUE_LINE_PREFIX` tables classifying which
    # lines to exempt from wrapping; deleted (not merely simplified) because
    # the case they existed for cannot occur -- `wrap_width()` returns a
    # width only when stderr IS a tty, and any pipe/grep that could read a
    # "record-shaped" line makes stderr not a tty, which already returns
    # `None` and skips wrapping wholesale.
    # 800 -> 812, tan-cli#478 review finding 6: the JSON envelope discloses a
    # foreign global default via the `Envelope` seam, but `diff`'s text mode
    # never touches `Envelope` -- `_emit_failure`'s `else` branch and the
    # success branch both write straight to stderr. `sdk_context_issues` is
    # now computed for real (was a permanently-empty placeholder left by the
    # #478 review round that deleted the hand-call, reasoning the JSON seam
    # alone was enough) and printed in both text branches. A brand-new
    # module in this ledger, not a raised number: this is the first time
    # `diff_cmd.py` crossed the 800-line cap.
    # 812 -> 822, tan-cli#498 defect 2 reaching this module: `diff` reuses
    # `validate_cmd`'s validator parser wholesale, so when findings became a
    # `_Finding` record instead of a `(severity, message)` tuple, the import,
    # the `_spawn_validator` return annotation and the one-sentence fold in
    # `_reject_if_sdk_validator_disagrees` all followed the shape. MEASURED on
    # the rebased tree, not 812 + this branch's pre-rebase delta.
    "tan/commands/diff_cmd.py": 822,
    "tan/commands/sdk_cmd.py": 1274,
    # 1093 -> 1108: tan-cli#478 wired `sdk_resolution_issues` through the
    # spawn path, so a `globalDefault` written for ANOTHER project stops
    # being silent here. Raised rather than absorbed by cutting the
    # comments that explain why: extracting from a 1100-line module is
    # tan-cli#408's job, and holding a defect fix hostage to it would
    # leave the silence in place for the sake of a number.
    # 1108 -> 1127, tan-cli#478 review round: `_emit` now splits this
    # command's own findings from the SDK-resolution advisories, so
    # `data.issueCount`, `--format sarif`, `--format diagnostic-v1` and the
    # tan-cli#350 text verdict stop treating a HOST fact as a board finding.
    # 1127 -> 1157, dev-merge with tan-cli#488 defect 8: `validate()`'s whole
    # body (from the SDK-root ladder through the final `_emit(...)`) moved
    # inside a `try`/`except typer.Exit: raise`/`except Exception`, so the
    # identical unguarded-prologue shape `build_cmd.build` already had
    # (`os.path.abspath` calling `os.getcwd()` on a deleted cwd) now also
    # produces a `validate.internal-failure` envelope instead of a raw
    # traceback -- combined with the #478 seam above, which stays inside the
    # new `try`.
    # 1157 -> 1166, tan-cli#478 review finding 6: the "no board.yaml to
    # validate" text branch printed `findings[0]` alone (deliberately, per
    # tan-cli#350's narrow verdict wording) but dropped the `sdk.*`
    # advisories `findings` filters OUT of `issueCount`/sarif/diagnostic-v1
    # -- the other two text branches already show them because they loop
    # over the unfiltered `issues`.
    #
    # (The #488-defect-8 `try`/`except` wrap is recorded above as the
    # 1127 -> 1157 step; it reached `dev` before this branch rebased onto it.)
    #
    # 1166 -> RE-MEASURED BELOW, as of tan-cli#498 defects 1, 2 and 3 -- three behaviour
    # changes in one command, each with its own measured argument for diverging
    # from the frozen oracle, which is where the bulk of the 295 lines went.
    # Defect 1: the two v2 structural checks are UNGATED (`schemaVersion >= 2`
    # was unreachable -- alp-sdk pins `LATEST = 1` -- while `board.schema.json`
    # conditions nothing on the key), so `--offline` stops calling boards clean
    # that the SDK rejects. Defect 2: findings became a `_Finding` record
    # instead of a `(severity, message)` tuple, so a rich `error[ALP-Bxxx]`
    # block's code, `= hint:`, `= see:` page and `line:col`+caret span survive
    # into `issues[].message` and into the diagnostic-v1/SARIF documents
    # (`_lsp_range`, `_sarif_region`, `_diagnostic_code`, `_issue_message` are
    # the new helpers). Defect 3: an unreadable board.yaml is
    # `validate.board-yaml-unreadable` at exit 2, not `internal-failure` at 5.
    #
    # The number below accounts for BOTH #478's `_emit` split (already on
    # `dev`) and #498's three defects, and is a `wc -l` of the rebased file --
    # NOT 1166 + (1417 - 1122) arithmetic across the two branches. The two
    # overlap inside `_emit`: #478's `sdk.*` filter and #498's `_Finding`
    # pairing collapse into one `reportable`/`reported` pair rather than
    # stacking, so the union is smaller than either side's sum implies.
    "tan/commands/validate_cmd.py": 1478,
    # 1057, not 1047, as of the tan-cli#464 rework: `new-som` appends
    # `sdk.global-default-foreign-project` beside `sdk.project-pin-unresolved`
    # -- this command writes metadata skeletons into whichever checkout
    # resolved, the same cost `project_pin_issue` above already justified.
    "tan/commands/new_som_cmd.py": 1341,

    # 1013, not 1000, as of the tan-cli#464 rework: `resolve_sdk` (shared with
    # `presets`) now returns the shared `ActiveSdk` instead of its own tuple,
    # and `clean` appends `sdk.global-default-foreign-project` beside
    # `sdk.project-pin-unresolved`.
    "tan/commands/clean_cmd.py": 1013,
    # 1015, not 996, as of tan-cli#485: `_load_yaml`/`_load_json` route
    # through the new `strict_loaders.strict_yaml_load`/`strict_json_loads`
    # (alp-sdk #1127, a duplicate-mapping-key refusal), and the IPC-entry
    # loop gained the alp-sdk #1088 refusal of `cacheable: true` on a
    # `kind: rpmsg` entry.
    # 1016, not 1015, as of tan-cli#543: alp-sdk#1223 DROPS the legacy
    # `raw: true` -> `fs: raw` normalisation (the schema now rejects the key
    # outright) and replaces it with the five-line comment recording that
    # measurement -- a net +1. Despite the `docs(accuracy)` subject line, this
    # is a behavioural change, which is why it was ported rather than skipped.
    "tan/planner/loader.py": 1016,
    # 1009, not 974, as of the tan-cli#464 review round: `_resolve_sdk_root`
    # carries `foreign_global_default_for` through into `_Sdk`, and `init`
    # surfaces `sdk.global-default-foreign-project` BEFORE `_pin_sdk` writes
    # -- this is the command a foreign `globalDefault` hurts most, since the
    # pin it is about to write is PERMANENT, unlike one build silently using
    # the wrong SDK once.
    # 1023, not 1009, as of the UX polish sweep task 3: the new module-level
    # `overwrite_refusal_message` helper (its docstring, the `update`-kind
    # filter, and the returned `--preview`/`--force` message), +14 lines,
    # single cause -- the `init.would-overwrite` call site itself stayed the
    # same length, swapping a literal string for a call.
    # 1042, not 1023, as of tan-cli#494 defect 10: `_cwd_or_dot` and the
    # docstring recording that the frozen oracle exits 0 on the identical
    # removed cwd where this port raised `init.internal-failure` / exit 5.
    "tan/commands/init_cmd.py": 1042,
    # 1060, not 923, as of the tan-cli#456 review round: `_select_slice`'s
    # `os`-vocabulary map, its `native_sim` board discriminator, its manifest
    # slice reader, and the `--target-kind` inference decision itself
    # (`infer_target_kind`, its message-building split into four small
    # helpers to keep it under the FUNCTION ratchet too) all moved here from
    # `debug_config_cmd.py`, which was over ITS OWN budget after the same
    # review's bugfix -- "move the decision, don't just extract a helper" was
    # the review's own suggested fix, since `support_bundle_cmd.py` needs the
    # identical decision and both commands already import this module.
    # Raised rather than split further: the alternative was leaving the
    # shared decision duplicated per command, the exact drift this move
    # exists to prevent.
    # 1068, not 1060, as of tan-cli#462: `infer_target_kind` now returns a
    # `(target, code, message)` 3-tuple instead of `(target, message)`, so its
    # two user-fixable refusals (a pre-build hardware project, a `--core`
    # matching nothing) carry a bare reason code the caller maps to a new
    # issue code -- and `_core_not_in_manifest_message` grew a `slices`
    # parameter to name the cores the build actually produced.
    # 1109, not 1068, as of tan-cli#489: (3) `_merge_value`'s list branch grew
    # a no-truncation tail-append plus a recursive dict branch (a per-index
    # merge used to silently delete a customer's extra `setupCommands`/
    # `configFiles` entries and their hand-added keys); (5)
    # `explicit_core_unknown_message` is the `--target-kind`-explicit
    # counterpart of `_core_not_in_manifest_message` above, kept in `tan.core`
    # rather than duplicated in the command module per this file's own
    # pure-logic convention.
    # 1201, not 1109, review round on the same issue: (2/3) the per-index
    # list merge was replaced with `_list_item_identity` +
    # `_merge_list_by_identity` (a reordered `configFiles`/`setupCommands`
    # was pairing the wrong entries, both destroying one and duplicating
    # another) -- a real function plus its own docstring recording the
    # remaining, deliberately-accepted "cannot shrink a list tan itself
    # wrote" limitation, not a few extra lines; (6) `explicit_omissions`
    # threaded through `create_launch_json_write_plan` so
    # `--pre-launch-task ''` removes an existing key on a write, not only a
    # fresh draft.
    # 1221, not 1201, SECOND review round on the same issue: the identity-only
    # merge above was NON-IDEMPOTENT (measured: three consecutive runs
    # accumulated three revisions of `configFiles` instead of holding only
    # the latest) and emitted in the DRAFT's own order, not `existing`'s --
    # both fatal to OpenOCD/gdb sessions that depend on entry order.
    # `_merge_list_by_identity` now keeps position as a weaker fallback
    # signal for a draft item matching nothing already present, restoring
    # idempotence and order, with its own docstring rewritten to describe
    # the residual limitation this ACTUALLY leaves (not the "cannot shrink"
    # framing the previous round's docstring used, which described a
    # symptom of the accumulation bug, not a real property).
    # 1275, not 1221, THIRD review round on the same issue: the SECOND
    # round's fallback used the unmatched draft item's own INDEX against
    # `existing` at that same index -- correct only while nothing earlier in
    # the draft had also identity-matched, which any customer-prepended
    # entry ahead of tan's own resolved values immediately breaks (measured:
    # accumulation persisted for any board with more than one `--config`).
    # `_merge_list_by_identity` now does ANCHOR-relative placement (a free
    # `existing` slot bracketed by the nearest identity matches before/after
    # the unmatched item in the draft, not its own raw index) -- more state
    # to track per merge (`anchor_of_draft_index`, the two-pass placement
    # loop) and a docstring long enough to record why the simpler two-pass
    # ("assign every leftover draft item to every leftover existing slot in
    # order") was rejected, not just what shipped.
    "tan/core/debug_launch.py": 1275,
    # 1003, not 941, as of tan-cli#510: `_command_on_path`/`_tool_is_
    # available` (a bool-only availability check, and a spawn that repeated
    # a SEPARATE, unhardened PATH lookup) were replaced by one
    # `_resolve_tool` returning the resolved absolute path AND what it
    # searched -- the spawn now runs that path, never the bare identity, and
    # the missingTool refusal names the search. The per-slice resolved-tool
    # note appended ahead of `output_artefact` resolution accounts for the
    # rest.
    # 1075, not 1003, as of the tan-cli#510 REVIEW round: that per-slice
    # resolved-tool note (appended to `message`) is GONE -- `resolved_tool`
    # is now a dedicated `SliceOutcome` field, computed once per slice and
    # threaded through every outcome constructor -- but the review's other
    # three findings cost more lines than that removal saved: `_resolve_tool`
    # takes an `env` parameter and resolves against it (MAJOR 2, moving the
    # whole env-assembly block earlier in the loop plus its own explanatory
    # comment), the env-assembly-before-resolution reordering itself carries
    # a paragraph explaining why (MAJOR 2), `SliceOutcome.resolved_tool` and
    # a second `execute.py` module-docstring divergence paragraph (mirroring
    # the existing tan-cli#307 one) are both new, and the absolute-path-miss
    # message split into two non-circular returns (the review's minor
    # finding) plus the `os.get_exec_path(env)` POSIX comment (MAJOR 2).
    # 1119, not 1075, as of the tan-cli#510 REVIEW ROUND 3: the missing-tool
    # refusal's searched-`PATH` text was reaching the persisted
    # `system-manifest.yaml` `reason` (a support-ticket-forwarded artefact),
    # not just the transient message/envelope -- `SliceOutcome` gained a
    # `manifest_message` field (and its own docstring) plus a short-form
    # value threaded through the missing-tool call site and
    # `_write_manifest_after_dispatch`'s `reason=` line; `SliceOutcome.
    # resolved_tool`'s docstring also grew a paragraph documenting a
    # deliberate ambiguity the review flagged as a NIT (an already-absolute
    # `tool` that fails to launch reports `resolvedTool: null`,
    # indistinguishable from "never resolved" without a separate field this
    # port does not add).
    "tan/commands/build/execute.py": 1119,
    # 970, not 848, as of tan-cli#432: the alp-sdk#1069 port added the
    # disjoint per-core slot0 partition map (+168, matching alp-sdk's own
    # delta in scripts/gen_zephyr_board.py line for line). Raised rather
    # than extracted because this file mirrors an upstream generator --
    # splitting it here would make the next port a hand-merge instead of
    # a diff.
    # 975, not 970, as of tan-cli#485: `_resolve_variant` grew a
    # case/whitespace-insensitive `is_tbd`-shaped TBD check (alp-sdk #1048,
    # matching `som_metadata.py::_resolve_silicon_variant`'s own copy).
    # 1027, not 975, as of tan-cli#544: the alp-sdk#1289 port reserves the App
    # MRAM top for the SE-owned ATOC -- `_AEN_ATOC_KIB = 32` with its measured
    # sizing evidence, an `atoc` region appended after `storage` in BOTH
    # `_aen_flash_partitions` branches, and the four label/nodelabel maps.
    # The evidence comment is kept VERBATIM from alp-sdk rather than
    # summarised: it carries the bench-observed addresses (ATOC magic `ckBS`
    # / `0x53426B63` at `0x8057EA50`, window top `0x80580000`) that say why
    # 32 KiB and not less, and a paraphrase of those is worth nothing on a
    # bench. Raised, not extracted -- same mirrored-generator reasoning as the
    # tan-cli#432 entry above.
    "tan/planner/zephyr_board.py": 1027,
    # 834 -> 842: tan-cli#478, same reason as `validate_cmd.py` above --
    # the bundle is the one artefact that never carried the foreign-default
    # warning, and its embedded doctor set could not have supplied it.
    # 842 -> 859, tan-cli#478 review round: the `sdkResolution` block now
    # goes into the bundle PAYLOAD, before `_write_bundle` -- the earlier
    # revision computed it after the write, so the file a user attaches
    # still answered False to the issue's own repro.
    # 859 -> 876, tan-cli#478 review finding 6: the JSON envelope and the
    # bundle file both carry the foreign-default pair, but `outcome.text`
    # (the default, non-JSON path) never did. Filters `outcome.issues` for
    # its `sdk.*` entries at the print site -- NOT recomputed from
    # `outcome.sdk`, which is `resolve_debug_project_context`'s legacy bare
    # `SdkInfo(sdk_root, sdk_tier)` and carries neither
    # `foreign_global_default_for` nor `broken_project_pin` (measured: a
    # first revision that read `outcome.sdk` printed nothing, the exact
    # silent-drop shape this whole issue is about).
    # 876 -> 935, PR #504 review MAJOR 1: the three early-return failure
    # paths (`_internal_failure` x2, `_server_incompatible`) built their
    # `_Outcome` from a bare `issues=[Issue(...)]` list, dropping the
    # SDK-resolution pair on exactly the paths a customer hits when
    # something has already gone wrong -- a parse refusal, an unsupported
    # target/server pairing, or a bundle-write `OSError`. Both helpers now
    # take the same three `broken_project_pin`/`sdk_tier`/
    # `foreign_global_default_for` keyword-only fields `_run`'s success path
    # already threads, defaulted so the outer exception guard (which never
    # resolved a project context) keeps its prior empty-list behaviour.
    "tan/commands/support_bundle_cmd.py": 932,
    # 842, not 831, as of tan-cli#433: `_reorder_global_flags` now consults
    # `_every_declared_format()` -- the same single source `_format_callback`
    # reads -- instead of a second, driftable tuple, and the docstring
    # records why (the old rule silently DROPPED the subcommand for any
    # leading `--format` outside `text`/`json`). Raised rather than
    # extracted: the growth is the explanation of a shipped regression,
    # which is the last thing to move out of the file it explains.
    # 856, not 842, as of the UX polish sweep, from two separate tasks.
    # Task 1 (+9): all 32 command registrations now carry `rich_help_panel=`
    # keywords grouping them into six titled panels on `--help` -- +6 lines
    # from wrapping the three `context_settings=FORWARD_CONTEXT_SETTINGS`
    # arguments (lock, migrate, quality) from single lines to three-line
    # blocks, and +3 from the registration-table header comment growing from
    # 1 line to 4 to document the panel-keyword pattern.
    # Task 3 (+5): the bare-`tan` `ctx.fail("a command is required")` became a
    # three-sentence message naming `tan doctor`/`tan init`/`tan build` and
    # `tan --help`, since exiting 2 at a user who typed the binary's own name
    # with nowhere to go next is the most-hit dead end in the CLI.
    # 1008, not 856, MEASURED with `wc -l`, as of tan-cli#546 (= #491 defect 3)
    # plus #491 defect 4. Three additions, and the growth is overwhelmingly the
    # RATIONALE, which is the point -- two earlier attempts at #546 each closed
    # the named defect and reopened it a different way, and a third textual
    # shape was proposed that the oracle also refutes, so the measurements
    # ruling all three out live beside the code that replaced them:
    #  * `_DispatchedCommand` (+ the `_dispatched_json_mode` module state and
    #    the loop that assigns `cls` across the registration table): records
    #    whether a subcommand BODY ever ran and what its own `--format`
    #    resolved to. That is the half of Rust's `match Cli::try_parse()` the
    #    port never had -- `_wants_json`'s own BEHAVIOUR is unchanged (it is
    #    Rust's `Err`-arm scan); only its docstring grew, to record the
    #    narrowed role and the measurement that rules out a third rewrite.
    #  * `_SIGINT_EXIT_CODE`/`_INTERRUPTED_MESSAGE`/`_interrupted_envelope`:
    #    the `cli.interrupted` arm, so Ctrl-C stops being reported as
    #    `cli.parse-error` at an out-of-contract exit 130.
    #  * `main()`'s handler grew the `ran_in_text_mode` gate both arms read.
    # Raised rather than extracted: every one of these is about what `main()`
    # does at the process boundary, and the two constants and the one class
    # are read only there.
    "tan/cli.py": 1008,
}

#: Some of these are `tan/planner/**`, which is a hash-audited MIRROR of
#: alp-sdk's `scripts/alp_orchestrate/**` (`test_planner_relocation_
#: freshness.py`). Splitting one here would make the mirror diverge in SHAPE
#: from upstream and is the wrong repository for the fix -- tan-cli#408's
#: acceptance names `kconfig.py` and a `_library_alias_table` dedup across
#: `kconfig.py`/`libraries.py`/`loader.py`, and all of those are mirror
#: files. That part of #408 belongs upstream, not here.
_MIRRORED = ("tan/planner/",)

#: Functions over `_FUNCTION_CAP` as of 2026-08-04: 199 of them, which is far
#: too many to enumerate readably. Two numbers ratchet them instead -- the
#: COUNT (a new long function pushes it up) and the WORST (an existing one
#: growing pushes it up). Neither can move without this file moving.
# 200, not 199, for the same reason as the four module entries above: both
# functions that crossed 50 lines came from the tan-cli#407 fixes that landed
# after this ratchet was calibrated -- `tan/commands/sdk_cmd.py:_run_current`
# and `tan/envelope.py:_with_sdk_divergence`, each of which now emits the
# shared `sdk.discovery-divergent` warning. Measured: 198 over 50 lines at
# f3208e1, 200 now.
# 201 as of tan-cli#432: `tan/planner/zephyr_board.py:_aen_flash_partitions`
# crossed 50 lines carrying the alp-sdk#1069 disjoint-slot0 branch. It is a
# line-for-line port of alp-sdk's own function, which is the same size --
# extracting here would make the next port a hand-merge instead of a diff.
# 202, not 203, as of the tan-cli#464 REWORK: `_undo_relocation` dropped back
# under 50 lines once its project-pin rollback branch (added, then reverted
# on review, by the same issue) came back out -- `resolve_sdk_tiered` (61
# lines, `sdk_cmd.py`, the `writtenFor`-vs-caller check) stays over.
#
# 707, not 700, same rework: `bootstrap_cmd._run` gave back the removed
# project-pin write/read/rollback lines but took on more than that back in
# disclosure plumbing -- `resolve_sdk_root_ladder`'s named `SdkRootResolution`
# unpacked into local variables (rather than a tuple, per review) and a
# `foreign_issue` threaded alongside `pin_issue` into both the `--print-env`
# short-circuit and the final issues list. Not extracted further: `_run` is
# already the one long linear refusal ladder tan-cli#408's own `# noqa:
# PLR0911, PLR0912, PLR0915` stands in front of, and splitting the
# resolution-and-disclosure lines out would not shrink the MODULE total below,
# only move them off this ratchet onto that one.
#
# 203, not 202, as of the UX polish sweep task 4:
# `tan/commands/examples_cmd.py:render_examples_text` crossed 50 lines (47 ->
# 61) adding `--category`, from three separate additions, not one: the
# signature grew from 3 to 7 lines (+4) once `category: str | None` forced
# one-parameter-per-line wrapping; a new `if category is not None:` empty-
# result branch added 5 lines; and the trailing "categories: ..." /
# "narrow with --category ..." hint appended after the entry loop on an
# unfiltered run added another 5 lines. It stacks on top of the #464 count
# above rather than replacing it -- the two crossings are different
# functions.
#
# Still 203, not 204, after the five-finding review pass on the doctor work:
# the wrapping fix for that pass's MAJOR finding (a `--fix`-suppressed
# warning measured at 262 columns, unwrapped, inside the very report this
# task wraps) pushed `tan/core/doctor_render.py:render_doctor_footer` to 62
# lines (39 -> 62), of which the BODY grew by only 1 line (18 -> 19) --
# the DOCSTRING grew by 22 (18 -> 40), and `_long_functions` spans
# `lineno`..`end_lineno`, so the docstring is what crossed the cap, not the
# wrapping code itself. Trimmed back to a 26-line docstring (48 lines total)
# instead of paying the ratchet for prose. `_print_stream_lines`
# (`doctor_cmd.py`, the guarded print path both `_print_check` and the
# footer route through as of the same pass) measures exactly 50 lines --
# AT the cap, not over it -- for the same reason: trimmed on purpose rather
# than left to bump this budget a second time. `run_fix` (`doctor_cmd.py`)
# also grew this pass, but it was already over 50 lines before it, so it
# never moved the COUNT either way.
#
# 205, not 203, as of tan-cli#489: two more functions crossed 50 lines --
# `debug_config_cmd.py:_fill_debug_probe_identity_from_sdk` (52 -> 57), which
# now also returns the `known_jlink_cores` set item (4)'s
# `sdk-identity-core-unresolved` message needs, and
# `debug_launch.py:_merge_value` (44 -> 53), whose list branch gained the
# no-truncation tail-append plus a recursive dict branch (item (3): a
# per-index merge used to silently delete a customer's own `setupCommands`/
# `configFiles` entries). `_run` (`debug_config_cmd.py`) also grew, 320 -> 382,
# but was already over the cap, so it does not move this count.
#
# 206, not 205, review round on the same issue: `_merge_value`'s old 53-line
# list branch was REPLACED by `_merge_list_by_identity` (52 lines, a wash --
# still one function over the cap, not two), but `_atomic_write_launch_json`
# (`debug_config_cmd.py`, findings 4+5: symlink-resolving, `fsync`'d,
# mode-preserving) is a genuinely NEW one at 81 lines, so the count moves by
# exactly the net +1 that accounts for.
#
# 207, not 206, merging in tan-cli#485's review round (independent of the
# #489 work above -- both landed on top of the same 203 baseline):
# `project_loader.py:_hwrev_pad_route_overrides` (trimmed to exactly 50
# lines, at the cap not over it, in #485's first pass) needed its dropped
# constraint note back -- "same error TYPE and MESSAGE SHAPE as
# loader.load_board_yaml's SoM-side refusals" is load-bearing: the function
# INLINES `loader._status_repr` rather than importing it, and that inlining
# is invisible without the note telling a future editor the two must be kept
# in sync by hand. +6 lines (50 -> 56), all docstring, so the function
# crosses the cap again -- the same choice `render_doctor_footer` made the
# OTHER way, above, when the growth was prose that added nothing a reader
# couldn't infer; this growth names a real constraint the code doesn't
# otherwise state anywhere.
# 204, not 203, as of tan-cli#485's review round: `project_loader.py:
# _hwrev_pad_route_overrides` (trimmed to exactly 50 lines, at the cap not
# over it, in #485's first pass) needed its dropped constraint note back --
# "same error TYPE and MESSAGE SHAPE as loader.load_board_yaml's SoM-side
# refusals" is load-bearing: the function INLINES `loader._status_repr`
# rather than importing it, and that inlining is invisible without the note
# telling a future editor the two must be kept in sync by hand. +6 lines
# (50 -> 56), all docstring, this budget raised rather than re-trimmed --
# the same choice `render_doctor_footer` made the OTHER way, above, when the
# growth was prose that added nothing a reader couldn't infer; this growth
# names a real constraint the code doesn't otherwise state anywhere. This
# step landed on `dev` via tan-cli#521, independently of the two steps below.
#
# 205, not 204, as of the tan-cli#487 REVIEW round (finding 1):
# `flash_cmd.py:_yocto_wic_block_device_refusal` crossed 50 lines (41 -> 66)
# once its docstring grew to explain the narrowed ENOENT fail-open (the
# review's own point -- a blanket fail-open was the bug, and the function
# needs to say precisely which shape still fails open and why, or the next
# reader re-widens it by "simplifying" the condition). Body growth is small
# (one `if`/`return` pair); the docstring is the reason. `_FUNCTION_WORST_
# BUDGET` is untouched -- 66 lines is nowhere near it.
#
# 206, not 205, as of tan-cli#511: `flash_plan.py:openocd_program_word`
# crossed 50 lines (36 -> 74). The body is a single line, unchanged
# (`return f"{{{text}}}"`, now shorter than before -- the predicate it used
# to branch on is gone); the docstring is the entire growth, recording why
# the conditional it replaces never actually preserved the parity fixture
# it claimed to (the CAPTURE_PLATFORM finding), and the one Tcl brace-
# counting edge case (an odd trailing backslash count) unconditional
# bracing deliberately still leaves fail-safe rather than special-cased
# away. `_FUNCTION_WORST_BUDGET` is untouched -- 74 lines is nowhere near it.
# These last two steps landed on tan-cli#487/#511's own branch, disjoint from
# tan-cli#521's `project_loader.py` step above -- merging #521's dev tip into
# #511 (tan-cli#511's PR merge) lands all three functions in the same tree at
# once, so the budget here is 203 + 1 (#521) + 2 (#487/#511) = 206, not
# either side's pre-merge number on its own. Re-measured against the merged
# tree with the gate's own `ast`-walk, not summed from the two branches'
# comments on faith.
#
# 209, not 206 and not 207, merging tan-cli#489's branch with the `dev` tip
# that now carries BOTH tan-cli#521 (#485's `project_loader.py` step) and
# tan-cli#511 (#487's `_yocto_wic_block_device_refusal` and #511's
# `openocd_program_word`). The two comment histories above each describe a
# DIFFERENT subset of the same 203 baseline and overlap on #485's single
# step, so neither side's total survives the merge and neither does the
# naive union of them: 207 + 206 - 203 = 210, which is WRONG. The real
# figure is 203 + 1 (#485, counted once) + 3 (#489) + 2 (#487/#511) = 209,
# and it was re-measured against the merged tree with the gate's own
# `ast`-walk rather than summed from the two branches' comments on faith --
# the arithmetic and the measurement disagree by one, and the measurement
# wins.
#
# 206, not 205, as of tan-cli#520: `flash_plan.py:flow_d_preflight_script`
# crossed 50 lines (44 -> 51) once it gained a `read_device` keyword-only
# parameter, doubled its docstring to explain the two ways it can now be
# armed (Flow D's paired `jlink_device` vs `swd_probe`'s caller-supplied
# device), and picked up one extra local (`paired_device`) to distinguish the
# two. `_FUNCTION_WORST_BUDGET` is untouched -- 51 lines is nowhere near it.
#
# 207, not 206, as of the tan-cli#520 REVIEW round 2 MAJOR fix:
# `flash_plan.py:_resolve_jlink_device` crossed 50 lines (49 -> 69) once it
# gained the plan-time `validate_identifier` call plus the docstring
# explaining why write-time-only validation (round 1's fix) let `--dry-run`
# and a real run disagree on an identical hostile manifest.
# `_FUNCTION_WORST_BUDGET` is untouched -- 69 lines is nowhere near it.
#
# MEASURED, not summed: the two histories above are disjoint subsets of the
# same 203 baseline -- the `ours` side counts #485/#489/#487/#511, the
# `theirs` side counts tan-cli#520's own two crossings -- so neither total
# survives rebasing #520's work onto the `dev` tip that now carries all of
# the former. Re-walked with the gate's own `ast` logic (span > 50 over all
# of `tan/`, planner included) against this exact tree.
#
# 214, not 211, as of tan-cli#488: three NEW functions crossed 50 lines --
# `doctor_cmd.py:zephyr_python_floor` (28 -> 59, the three-way fallback
# split), `doctor_cmd.py:_load_manifest` (48 -> 80, the `schemaVersion`-first
# read), and `doctor_cmd.py:west_resolved_check` (50 -> 81, the `ran` arm).
# `doctor` (269 -> 286) and `_collect` (343 -> 380) both grew too, but were
# already over the cap, so neither moves this count. `_FUNCTION_WORST_BUDGET`
# is untouched -- 380 lines is nowhere near 707.
#
# 215, not 214, as of tan-cli#488 ROUND 2: `doctor_cmd.py:fix_suppressed_issue`
# crossed 50 lines with its `sys.stdin is not None` guard and the docstring
# paragraph explaining it (defect 6). `west_check` (already over the cap)
# grew further with the `resolved_ran` branch (defect 3) but does not move
# this count. `_FUNCTION_WORST_BUDGET` is untouched -- nothing here is close
# to 707.
#
# 216, not 215, as of tan-cli#488 ROUND 6: `tan/core/consent.py:can_prompt`
# crossed 50 lines (45 -> 60) with the docstring paragraph explaining why
# `is not None` was never the whole guard -- a stream that EXISTS but lacks
# `.isatty()` (`_TeeStderr` under `--format json`) still crashes it, the
# same class `build_cmd._dispatch` and `doctor_cmd.fix_suppressed_issue`
# were fixed for in this same round. `fix_suppressed_issue` (76 -> 88) and
# `_dispatch` (186 -> 209) both grew too, but were already over the cap, so
# neither moves this count. `_FUNCTION_WORST_BUDGET` is untouched --
# measured (AST walk, all of `tan/` including `tan/planner/`) at 701 lines
# worst (`bootstrap_cmd.py:_run`), under the 707 ceiling.
# 212, not 211, as of tan-cli#516 (first pass): the new shared `tan/core/
# atomic_write.py:atomic_write_text` (54 lines, docstring included) was a
# genuinely NEW function over the cap -- `reconcile_west_manifest_path`'s own
# body did not cross it either before or after this change, so that was a
# pure +1, not a replacement.
#
# 211, not 212, as of the tan-cli#516 REVIEW round: `debug_config_cmd.py:
# _atomic_write_launch_json` -- the 81-line function that was ALREADY over
# the cap before this issue, one of the 199 baseline never enumerated here --
# is gone outright. `_write` now calls the shared `atomic_write_text`
# directly instead of keeping a second, hand-synchronised copy of the same
# durability sequence beside it (the drift #516 itself was filed to close);
# `atomic_write_text` grew to 95 lines picking up that copy's mode-
# preservation and its broadened exception handling, but it was already
# counted once in the 212 above and staying one function does not add a
# second count. Net: +1 (atomic_write_text, counted in the prior step) - 1
# (`_atomic_write_launch_json`, deleted) against the pre-#516 211 baseline
# nets back to 211, not a coincidence -- re-measured with the gate's own
# `ast` walk against this exact tree, not summed on faith.
# `_FUNCTION_WORST_BUDGET` is untouched: re-measuring the CURRENT worst
# (`bootstrap_cmd.py:_run`) with this gate's own `ast` walk finds 701 lines,
# not 707 -- unrelated to this change (that function is untouched here) and
# still comfortably under the recorded ceiling, so the ceiling is left as
# recorded rather than tightened on faith.
# 212, not 211, as of tan-cli#496's remaining-defects pass:
# `new_som_cmd.py:_rollback_write_failure` is a genuinely NEW function (61
# lines) extracted from the write-failure `except OSError:` block so the
# restore-vs-delete logic (defect 2) and the exists()-gated cleanup
# reporting (the finding against this file's own earlier fix) are
# unit-testable on their own; every other function this pass touched
# (`_interactive`, `_render_preset`, `new_som`, `scaffold_cmd.py:scaffold`)
# was already over 50 lines before it, so growing them moves nothing here.
# `_FUNCTION_WORST_BUDGET` is untouched -- `_rollback_write_failure` is 61
# lines and `new_som` (grown to 506) is still well under `bootstrap_cmd.
# _run`'s 701, itself under the recorded 707.

# 213, not 211, as of the tan-cli#510 REVIEW round: two functions crossed 50
# lines, both docstring/parameter growth, not new branching:
# `build/execute.py:_resolve_tool` (47 -> 71) gained an `env` parameter
# (MAJOR 2: resolve against the slice's OWN assembled env, not
# `os.environ`) plus the docstring explaining why, and split its absolute-
# path miss into its own non-circular message (the review's own minor
# finding); `build_cmd.py:_text_recap` (48 -> 61) gained the resolved-tool
# note it prints for a failed/cancelled slice (MAJOR 1: carried in the new
# `resolvedTool` field, never folded into `reason`, so `_text_recap` is
# where the note is actually composed for default text). Neither is
# anywhere near `_FUNCTION_WORST_BUDGET`. Re-walked with the gate's own
# `ast` logic against this exact tree, not computed from the diff alone.
#
# 214 on the merged tree, MEASURED by AST walk -- neither side's number: #496
# contributed one crossing (212) and tan-cli#530's resolver two (213), and the
# union is 214, not either. Taking either side here fails the gate.
# 213 on the merged tree, measured by AST walk: #516 added no long function, and
# tan-cli#530 (#510's resolver) added two -- so dev's figure carries, not #516's
# pre-merge 211.
#
# 218 on the merged tree, MEASURED by AST walk over all of `tan/` including
# `tan/planner/` -- neither side's figure; #488's doctor/consent work and dev's
# tan-cli#530 resolver both add crossings.
# 214 on the merged tree, MEASURED by AST walk over all of `tan/` including
# `tan/planner/` -- #496 contributes one crossing that dev's 213 does not.
#
# 219 on the merged tree, MEASURED by AST walk over all of `tan/` including
# `tan/planner/` -- neither #488's 218 nor dev's 214: the two branches' crossings
# are disjoint and `new_som_cmd.py`'s gate resolution adds none of its own.
#
# 220 as of tan-cli#543/#544/#545, MEASURED by AST walk over all of `tan/`
# INCLUDING `tan/planner/` (49 of the 220 crossings live there), never
# computed from the diff. Exactly ONE function crosses: `tan/planner/
# partition.py:_reserved_spans` at 70 lines, new with the alp-sdk#1331 port.
# It is a line-for-line port of alp-sdk's own function at the same size, and
# it is one indivisible decision -- derive the device origin, VERIFY it
# (`origin + capacity == highest region top`), or refuse and say why. Splitting
# the verify away from the derive is precisely how it would come back guessing
# a base. The other five functions this change introduces stay under the cap
# (`_chip_has_driver` 23, `_first_free` 21, `_safe_join` 17,
# `_curated_library_names` 26, `_cmake_core_map` 36), and every function it
# GROWS -- `_emit_chips`, `_aen_flash_partitions`, `_scaffold_readme`,
# `render_to_envelope`, `_emit_hw_info_h` -- was already over 50 before it.
# `_FUNCTION_WORST_BUDGET` is untouched: the worst is still
# `bootstrap_cmd.py:_run` at 701, which this change does not go near.
#
# 220, tan-cli#478 review finding 6 (default text disclosure): `diff_cmd.py`'s
# `_emit_failure` crossed from 44 to 52 lines printing `sdk_context_issues`'
# messages on the text path -- the one new crossing; `diff`/`inspect`/`trace`/
# `support_bundle`/`_run_text` all grew too but were already over the cap.
# Re-measured by the gate's own AST walk over all of `tan/`, not inferred.
#
# STILL 220 after the dev merge that brings tan-cli#507/#508 in -- re-walked on
# the MERGED tree, not carried over: `debug_config_cmd.py`'s two new refusals
# add no function over 50 lines, and this branch's own `west_forward_cmd.
# _refuse_required` lands at exactly 50 (the cap is `> 50`), with the SDK-
# resolution echo extracted into an 18-line `_echo_sdk_resolution` rather than
# open-coded. Of the 220, `tan/planner/` contributes 48 -- it is NOT excluded
# from the walk, which is the arithmetic mistake to avoid when two branches'
# numbers are compared instead of the merged tree being measured.
#
# 221 ON THE MERGED TREE, and NEITHER side's 220 -- the two branches reached
# the same number by disjoint routes (dev's `diff_cmd.py:_emit_failure`
# crossing at 52, this branch's `tan/planner/partition.py:_reserved_spans`
# crossing at 70), so the union is one higher than either. Re-walked with the
# gate's own AST walk over all of `tan/` INCLUDING `tan/planner/` (49 of the
# 221 crossings live there) after the merge, exactly as the paragraph above
# says to: taking either side's figure by ownership is the arithmetic mistake,
# and here it would have shipped a budget the tree already exceeds.
#
# RE-MEASURED BELOW on the tree rebased onto tan-cli#582's dev, by the gate's
# own `ast.walk` span over ALL of `tan/` INCLUDING `tan/planner/` -- NOT
# computed from the diff, and NOT dev's 221 carried over. Exactly ONE new
# crossing from this branch, set-differenced against dev:
# `build_cmd._substituted_app_dirs` at 75 lines (tan-cli#517), all but nine
# of which is the docstring recording why the parent fallback is announced
# rather than refused, why the severity is `info`, and why the probe mirrors
# `_zephyr_app_dir` instead of reading `command.args`. tan-cli#547's resolver
# adds NONE: `resolve_toolchain_root` is 29 lines, `_candidates` 38,
# `_scan_roots` 22, `build_cmd._toolchain_for_plan` 26 -- the reasoning that
# would have pushed them over lives in the module docstring instead.
# 225 as of tan-cli#498, RE-WALKED with the gate's own AST walk over all of
# `tan/` INCLUDING `tan/planner/` (49 of the 225 crossings live there) after
# this branch was rebased onto dev's 221 -- never carried over from the
# pre-rebase figure and never 221 + 4 arithmetic. Set-differenced against dev
# the four new crossings are all in this change and all in `generate_cmd.py`/
# `validate_cmd.py`, each because its RATIONALE grew, not its branching:
# `validate_board_text` 89 (the argument for ungating two dead structural
# checks, and for the one conformance case that costs), `resolve_targets` 75
# (why `--all` + an explicit `--target` is refused rather than silently
# resolved one way), `_missing_emit_output` 75 (why a zero-byte artefact is a
# real emit, and how tan-cli#397's guarantee survives the widening) and
# `_ensure_writable` 60 (why the writability probe now removes the file it
# created). Nothing dropped below the cap. The worst function is untouched at
# `bootstrap_cmd.py:_run` 701 <= 707.
# 226, not 221, as of tan-cli#494/#495, RE-WALKED after the rebase onto
# tan-cli#582's dev with the gate's own AST walk over all of `tan/` INCLUDING
# `tan/planner/` (49 of the 226 crossings live there) -- never 221 + 5
# arithmetic, and never the pre-rebase 224 carried over. FIVE functions
# crossed 50 lines; the newly-over set was diffed by name against dev, and
# nothing dropped BELOW the cap to mask a crossing:
#   * `core/scaffold.py:retarget_board_yaml_cores` -- NEW (82), #494 defect 2's
#     `cores:` re-derivation. The block walk is the size it is because it must
#     find the entries, identify the single `app:` one, and leave an
#     unrecognised shape verbatim rather than half-rewritten.
#   * `core/module_template.py:_wiring` -- NEW (67), #494 defect 5's README
#     section. 40 of the 67 are the docstring recording the measurement
#     against the frozen scaffold trees and why the oracle's bytes are not the
#     fixed point here; the returned string is most of the rest.
#   * `core/venv.py:find_workspace_venv` (44 -> 61) -- #495 defect 1's
#     `_not_a_foreign_topdir` guard on the upward walk, plus the docstring
#     paragraph on why it is conditional on `.west` rather than a naive mirror
#     of tan-cli#307's `manifest_ok`.
#   * `core/bootstrap.py:optional_libs_block` (46 -> 58) -- #495 defect 6's
#     POSIX manual-install arm and the comment transcribing the oracle's
#     `case linux|macos)` shape it is keyed to.
#   * `commands/bootstrap_cmd.py:_print_env_outcome` (50 -> 51) -- #495 defect
#     7 by ONE line, after `_existing_venv_bin_dir` was already extracted to
#     hold it down. Not trimmed further: the remaining line is the call, not
#     commentary, and shaving a comment to dodge a ratchet is how the ratchet
#     stops meaning anything.
# `_FUNCTION_WORST_BUDGET` is untouched and NOT raised: `bootstrap_cmd.py:_run`
# measured 704 here (701 at `origin/dev` -- +3 for defect 2's
# `_zephyr_base_will_adopt` gate and defect 3's occupied-target call), still
# under the 707 ceiling.
# MERGED VALUE, tan-cli#547/#517 on top of dev carrying #494/#495 (#583):
# re-walked with the gate's own AST walk on the MERGED tree, 227 -- the two
# comment blocks above describe disjoint crossing sets, so neither side's
# number is the answer and only a walk of the union is.
# MERGED VALUE, tan-cli#498 on top of dev carrying #494/#495 (#583): re-walked
# with the gate's own AST walk on the MERGED tree, 230 -- not 226 + 4 and not
# 225 + 5. The two comment blocks above each describe their own side's
# crossings; the sets are disjoint, which is why neither side's number is the
# answer and only a walk of the union is. `_FUNCTION_WORST_BUDGET` stays 707:
# worst measured 704 here (`bootstrap_cmd.py:_run`, +3 from #583), not 701.
# MERGED VALUE on dev carrying #494/#495 (#583) and #498 (#576): re-walked
# on the merged tree, 231. Disjoint crossing sets, so no side's number applies.
_FUNCTION_COUNT_BUDGET = 231
_FUNCTION_WORST_BUDGET = 707


def _modules() -> list[Path]:
    return sorted(_PACKAGE.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(_PACKAGE.parent).as_posix()


def _long_functions(tree: ast.AST) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = (node.end_lineno or node.lineno) - node.lineno + 1
            if span > _FUNCTION_CAP:
                out.append((span, node.name))
    return out


def test_no_module_grows_past_its_recorded_budget():
    """The ratchet. A budgeted module may shrink freely; growing past its
    recorded size fails and must be answered in the diff that causes it."""
    grew = []
    for path in _modules():
        rel = _rel(path)
        lines = len(path.read_text(encoding="utf-8").splitlines())
        ceiling = _MODULE_BUDGET.get(rel, _MODULE_CAP)
        if lines > ceiling:
            grew.append(f"{rel}: {lines} lines, budget {ceiling}")

    assert grew == [], (
        "these modules are over budget:\n  "
        + "\n  ".join(grew)
        + f"\n\nA module not in _MODULE_BUDGET is capped at {_MODULE_CAP}. Either "
        "extract from it, or raise its entry with a reason -- the entries "
        "record what was true on 2026-08-04, and every one of them grew "
        "silently before this gate existed."
    )


def test_the_module_budget_has_not_gone_stale():
    """The other direction: a budget entry for a file that has SHRUNK well
    under its ceiling is a ratchet that stopped ratcheting. Lower it, so the
    next growth is caught at the new level rather than at the old one.

    The slack allowed is deliberately generous (50 lines). This gate exists
    to catch a module doubling, not to make every ordinary edit renegotiate a
    number."""
    slack = []
    for rel, ceiling in sorted(_MODULE_BUDGET.items()):
        path = _PACKAGE.parent / rel
        if not path.exists():
            slack.append(f"{rel}: budgeted but no longer exists -- drop the entry")
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines <= _MODULE_CAP:
            slack.append(f"{rel}: {lines} lines, now under {_MODULE_CAP} -- drop the entry")
        elif ceiling - lines > 50:
            slack.append(f"{rel}: {lines} lines, budget {ceiling} -- lower it")

    assert slack == [], "the module budget no longer describes the tree:\n  " + "\n  ".join(slack)


def test_no_new_long_function_and_none_of_them_grows():
    """199 functions are already over 50 lines, so enumerating them would be
    a 199-line table nobody reads. The COUNT and the WORST are ratcheted
    instead: a new long function moves the count, and an existing one growing
    moves the worst. `bootstrap_cmd._run` is the worst at 679 lines, which is
    what tan-cli#408's `# noqa: PLR0911, PLR0912, PLR0915` is standing in
    front of."""
    found: list[tuple[int, str]] = []
    for path in _modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as err:  # pragma: no cover -- a broken tree fails elsewhere first
            pytest.fail(f"{_rel(path)} does not parse: {err}")
        found.extend((span, f"{_rel(path)}:{name}") for span, name in _long_functions(tree))

    worst = max(found, default=(0, "<none>"))
    assert len(found) <= _FUNCTION_COUNT_BUDGET, (
        f"{len(found)} functions are over {_FUNCTION_CAP} lines, budget "
        f"{_FUNCTION_COUNT_BUDGET}. Extract from the one you just grew, or "
        f"raise the budget with a reason. Longest: {worst[1]} at {worst[0]} lines."
    )
    assert worst[0] <= _FUNCTION_WORST_BUDGET, (
        f"{worst[1]} is {worst[0]} lines, past the recorded worst "
        f"({_FUNCTION_WORST_BUDGET}). The longest function in the package "
        f"getting longer is the exact drift tan-cli#408 reports."
    )


def test_the_mirrored_planner_is_named_as_out_of_scope():
    """`tan/planner/**` is a hash-audited relocation of alp-sdk's
    `scripts/alp_orchestrate/**`. Splitting a mirror file here would make it
    diverge in SHAPE from upstream, which
    `test_planner_relocation_freshness.py` exists to prevent -- so any
    oversized module under it is upstream's to fix, and this records that
    rather than leaving the next reader to rediscover it from a failing hash.

    Asserted, not commented, because the fact is load-bearing: tan-cli#408's
    acceptance asks for `kconfig.py` to be split and for
    `_library_alias_table` to be deduplicated across `kconfig.py`,
    `libraries.py` and `loader.py`. All of those are mirror files. That part
    of the issue cannot be done in this repository."""
    mirrored = [rel for rel in _MODULE_BUDGET if rel.startswith(_MIRRORED)]
    assert mirrored, "no mirrored planner module is budgeted -- has the mirror moved?"
    for rel in mirrored:
        assert (_PACKAGE.parent / rel).exists(), f"{rel} is budgeted but missing"
