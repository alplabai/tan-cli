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
    "tan/commands/doctor_cmd.py": 3289,
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
    "tan/commands/bootstrap_cmd.py": 2917,
    "tan/core/bootstrap.py": 1890,
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
    "tan/core/flash_plan.py": 2161,
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
    "tan/commands/flash_cmd.py": 2170,
    # 1643, not 1639, as of tan-cli#485: `_emit_cross_core_shmem_cache`'s
    # docstring/body grew to cover `kind: rpmsg` too (alp-sdk #1088's
    # companion half -- `needs_dcache_off` now checks
    # `entry.kind in ("raw_shmem", "rpmsg")` instead of `raw_shmem` alone).
    # This branch carried a stale 1639 from before #485 landed on dev; the
    # rebase re-derived it on the merged tree rather than keeping either
    # side's number by ownership.
    "tan/planner/kconfig.py": 1643,
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
    "tan/commands/build_cmd.py": 1703,
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
    "tan/commands/debug_config_cmd.py": 1661,
    "tan/planner/template.py": 1199,
    "tan/core/scaffold.py": 1106,
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
    "tan/commands/generate_cmd.py": 1186,
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
    "tan/commands/sdk_cmd.py": 1274,
    "tan/commands/validate_cmd.py": 1093,
    # 1057, not 1047, as of the tan-cli#464 rework: `new-som` appends
    # `sdk.global-default-foreign-project` beside `sdk.project-pin-unresolved`
    # -- this command writes metadata skeletons into whichever checkout
    # resolved, the same cost `project_pin_issue` above already justified.
    "tan/commands/new_som_cmd.py": 1057,
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
    "tan/planner/loader.py": 1015,
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
    "tan/commands/init_cmd.py": 1023,
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
    "tan/commands/build/execute.py": 941,
    # 970, not 848, as of tan-cli#432: the alp-sdk#1069 port added the
    # disjoint per-core slot0 partition map (+168, matching alp-sdk's own
    # delta in scripts/gen_zephyr_board.py line for line). Raised rather
    # than extracted because this file mirrors an upstream generator --
    # splitting it here would make the next port a hand-merge instead of
    # a diff.
    # 975, not 970, as of tan-cli#485: `_resolve_variant` grew a
    # case/whitespace-insensitive `is_tbd`-shaped TBD check (alp-sdk #1048,
    # matching `som_metadata.py::_resolve_silicon_variant`'s own copy).
    "tan/planner/zephyr_board.py": 975,
    "tan/commands/support_bundle_cmd.py": 834,
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
    "tan/cli.py": 856,
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
_FUNCTION_COUNT_BUDGET = 209
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
