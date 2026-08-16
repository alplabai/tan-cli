# Module/function size ratchet log

Append-only. `scripts/regen_module_size_budget.py` writes one entry here every
time it raises a ceiling in `module_size_budget.generated.json` with
`--reason`. A `--merge-resync` re-derivation (re-measuring after a merge
conflict, where the growth was already reasoned on the branches being
combined) also gets a line, so a stale reader can tell "somebody made a call
here" from "a merge conflict was resolved mechanically" without diffing two
commits.

Because every entry only ever APPENDS at the end of the file, two branches
that both grow a (different, or even the same) module produce two lines in
a non-overlapping region -- exactly the "keep both sides" merge shape that is
correct for `CHANGELOG.md` and was wrong for the old `_MODULE_BUDGET` dict
(tan-cli#668), because nothing here encodes ABSOLUTE state; each line is a
self-contained, past-tense fact.

Before tan-cli#668, the ratchet's numbers lived in `test_module_size_budget.py`
itself, as a hand-maintained dict with a paragraph of prose per entry
recording exactly why each module grew. That history is not reproduced here
-- it is still fully readable via `git log -p -- python/tests/gates/test_module_size_budget.py`
up to the commit that migrated this gate to a generated file. This log starts
a new, coarser-grained (one line per regen, not one paragraph per PR) record
from that point on; the tradeoff is deliberate, in exchange for a file whose
conflicts resolve by re-running a command instead of by hand-merging prose.

## Entries

- 2026-08-11 -- migrated the ratchet from a hand-maintained dict in
  `test_module_size_budget.py` to a generated file (tan-cli#668). No module or
  function budget moved in this change; `module_size_budget.generated.json`
  was produced by running the new `regen_module_size_budget.py` against the
  same `dev` tree the old dict already described, and matched it exactly. The
  25 modules, the function count (249) and the worst function (728 lines) are
  unchanged from the values `dev`@`1e929c1` already carried; only their
  storage and the review-time record for future changes.
- 2026-08-12 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/doctor_cmd.py: 3849 -> 3920
    - tan/core/bootstrap.py: 2094 -> 2111
    - tan/planner/loader.py: 1016 -> 1251
    - function_count_budget: 249 -> 251
- 2026-08-12 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/init_cmd.py: 1121 -> 1247
    - tan/core/scaffold.py: 1500 -> 1510
- 2026-08-12 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/bootstrap_cmd.py: 3236 -> 3255
    - tan/core/flash_plan.py: 3071 -> 3079
    - function_worst_budget: 728 -> 747
- 2026-08-12 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/clean_cmd.py: 1098 -> 1110
    - tan/commands/renode_cmd.py: 1501 -> 1533
    - tan/commands/diff_cmd.py: 839 -> 882
- 2026-08-12 -- tan-cli#573: load_board_yaml's metadata_root override now reaches stages 4-5 (two new parameters) and travels on BoardProject to the downstream resolvers (one new keyword argument); +3 lines in loader.py, none elsewhere.
    - tan/planner/loader.py: 1251 -> 1254
- 2026-08-12 -- tan-cli#564: build_cmd/doctor_cmd gained the comments explaining why the width probes measure stderr, not sys.__stdout__
    - tan/commands/build_cmd.py: 2074 -> 2082
    - tan/commands/doctor_cmd.py: 3920 -> 3925
- 2026-08-13 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/build_cmd.py: 2074 -> 2082
    - tan/commands/doctor_cmd.py: 3920 -> 3925
- 2026-08-13 -- tan-cli#699: flash_plan.py gained an os:"off" refused_skipped branch (+ docstring detail) so an off core no longer hard-fails tan flash; flash_cmd.py's matching divergence-note docstring grew by one line
    - tan/commands/flash_cmd.py: 3922 -> 3923
    - tan/core/flash_plan.py: 3079 -> 3112
- 2026-08-13 -- tan-cli#699 followup: correct false operator-facing off-core claims + stale present-tense oracle references in flash_plan.py/flash_cmd.py comments
    - tan/commands/flash_cmd.py: 3923 -> 3927
    - tan/core/flash_plan.py: 3112 -> 3127
- 2026-08-13 -- fix(flash): correct select_flash_method's Flow D provenance docstring + FLOW_D_KEYS comment (tan-cli#700) -- net growth from re-attributing jlink_flash_device/expect_dpidr/jlink_device (SoC debug: block) vs slot0_load_address (SoM memory_map:, alp-sdk#1069) correctly, not a new feature
    - tan/core/flash_plan.py: 3079 -> 3093
- 2026-08-13 -- tan-cli#560: buildplan.py/orchestrator.py west build/ level port, zephyr_board.py AEN LOG_MODE_MINIMAL port (tan-cli#690), template.py pin-doc collision guard + scaffold stale-comment rewrite port (alp-sdk#1394/#1399/#1400) -- all four SDK pins moved to d00dbdc1
    - tan/planner/template.py: 1329 -> 1436
    - tan/planner/zephyr_board.py: 1372 -> 1433
    - function_count_budget: 251 -> 254
- 2026-08-13 -- tan-cli#560 review: correct the flash_plan.py resolve_artefact_path docstring's now-stale claim about the plan's artifacts block (Major 5)
    - tan/core/flash_plan.py: 3079 -> 3083
- 2026-08-13 -- tan-cli#697: cross-drive project/workspace refusal (execute.py's _cross_drive_source_refusal + build_cmd.py's _cross_drive_issues promotion)
    - tan/commands/build/execute.py: 1598 -> 1687
    - tan/commands/build_cmd.py: 2082 -> 2107
- 2026-08-13 -- tan-cli#501: generate_cmd.py's native-sim-overlay guard now distinguishes an explicit --target ask from an implicit --all/bare inclusion (drop-and-report, not a whole-run refusal or a silent --force clobber of a vendored overlay)
    - tan/commands/generate_cmd.py: 1312 -> 1342
- 2026-08-13 -- tan-cli#501 review round 5: closed findings 1-3 (false PREPEND rationale reverted, overlay-not-owned warning no longer recommends the destructive --force clobber of a vendored overlay, data.engine kept in step with a dropped target) -- the explanatory comments this needed grew the module past its prior ratchet
    - tan/commands/generate_cmd.py: 1342 -> 1348
- 2026-08-13 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/build_cmd.py: 2074 -> 2082
    - tan/commands/clean_cmd.py: 1098 -> 1110
    - tan/commands/doctor_cmd.py: 3920 -> 3925
    - tan/commands/generate_cmd.py: 1312 -> 1348
- 2026-08-13 -- tan-cli#719: flash_cmd.py gained the run-level unconfirmed-flash verdict and the --confirm flag; flash_plan.py gained CONFIRM_REMEDY/confirm_gate_note, the single source for the confirm-gate remedy
    - tan/commands/flash_cmd.py: 3922 -> 3966
    - tan/core/flash_plan.py: 3079 -> 3102
- 2026-08-13 -- tan-cli#720: accept --som/--sku as aliases on init, pinmux and new-som
    - tan/commands/init_cmd.py: 1247 -> 1258
    - tan/commands/new_som_cmd.py: 1353 -> 1361
- 2026-08-13 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/build/execute.py: 1598 -> 1643
    - tan/commands/build_cmd.py: 2082 -> 2106
    - tan/planner/loader.py: 1251 -> 1254
- 2026-08-13 -- merge-resync (growth already reasoned on the merged branches)
    - tan/core/flash_plan.py: 3102 -> 3106
    - tan/core/flash_plan.py: 3127 -> 3131
    - tan/core/flash_plan.py: 3089 -> 3093
    - tan/planner/template.py: 1329 -> 1436
    - tan/planner/zephyr_board.py: 1372 -> 1433
    - function_count_budget: 251 -> 254
- 2026-08-13 -- merge-resync (growth already reasoned on the merged branches)
    - tan/core/flash_plan.py: 3106 -> 3116
    - tan/core/flash_plan.py: 3131 -> 3141
- 2026-08-14 -- tan-cli#727: sdk_check grew the dangling --sdk-root arm, plus its call-site guard and the sdkProvenance skip
    - tan/commands/doctor_cmd.py: 3925 -> 3970
- 2026-08-14 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/flash_cmd.py: 3966 -> 3971
    - tan/core/flash_plan.py: 3116 -> 3164
- 2026-08-13 -- tan-cli#728: planner refuses an undeclared CONFIG_ALP_SDK_CHIP_ symbol instead of emitting it
    - tan/planner/kconfig.py: 2012 -> 2088
- 2026-08-14 -- tan-cli#736: sevenZip runs unconditionally on Windows; docstring premise corrected
    - tan/commands/doctor_cmd.py: 3970 -> 3984
- 2026-08-14 -- tan-cli#696: port alp-sdk#1413 -- _boot_target_is_single_slot plus the swap-mode refusal in secure.py
    - function_count_budget: 254 -> 255
- 2026-08-14 -- tan-cli#734: Slice.jlink_flash_device_declared plus loader._jlink_flash_device_declared, so a declared-null survives to flash_args
    - tan/planner/loader.py: 1254 -> 1278
- 2026-08-14 -- tan-cli#739: doctor_cmd.py gains the measured-evidence docstring explaining why the J-Link V13 firmware requirement was removed (a probe on V11.00 firmware programmed MRAM and byte-verified), plus the reworked setools message separating the Linux SE-UART path from Windows Flow D signing
    - tan/commands/doctor_cmd.py: 3970 -> 3989
- 2026-08-14 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/doctor_cmd.py: 3989 -> 4003
- 2026-08-14 -- tan-cli#744: port alp-sdk#1445's os==zephyr scoping onto the slot0 collision guard
    - tan/planner/loader.py: 1278 -> 1293
    - function_count_budget: 255 -> 256
- 2026-08-14 -- tan-cli#744: slot0 gate reverted to value-based, with the emit-vs-slot0 distinction spelled out
    - tan/planner/loader.py: 1293 -> 1301
- 2026-08-14 -- tan-cli#747 adds slot0_bytes_for_core + the memory_map arm of resolve_budget (core/size.py); tan-cli#746 adds _print_text_issues/_text_issues (commands/build_cmd.py); size_cmd.py gains _read_som_memory_map to feed the per-core window
    - tan/commands/build_cmd.py: 2106 -> 2161
    - tan/commands/size_cmd.py: new entry at 831
    - function_count_budget: 255 -> 256
- 2026-08-14 -- rebase onto #745: recompute after tan-cli#744's planner growth and tan-cli#747/#746 landed in the same budget
    - function_count_budget: 256 -> 257
- 2026-08-15 -- tan-cli#760: PATH-confirmation guard for install commands (bootstrap.py leading_binary/confirmed_install_commands, wired through bootstrap_cmd.check_prerequisites and doctor_cmd._collect)
    - tan/commands/bootstrap_cmd.py: 3255 -> 3263
    - tan/commands/doctor_cmd.py: 4003 -> 4014
    - tan/core/bootstrap.py: 2111 -> 2172
    - function_count_budget: 257 -> 258
- 2026-08-15 -- tan-cli#760 review fixes: MAJOR 1 (Check.fix_missing split, prerequisites_check's available param), MAJOR 2/Closes #765 (confirm_missing guards posix_venv_unusable at both call sites), MINOR 1/2/3 (wrapper+metachar refusal, shlex parity, sudo confirmation), MINOR 4 (doctor's own host-neutral fix hint), MINOR 5 (bootstrap_cmd guards windows_python_not_runnable/python_too_old too)
    - tan/commands/bootstrap_cmd.py: 3263 -> 3277
    - tan/commands/doctor_cmd.py: 4014 -> 4087
    - tan/core/bootstrap.py: 2172 -> 2269
- 2026-08-15 -- tan-cli#760 review round 3: mutation-provable --fix wiring test (Check.fix_missing fallback in __post_init__, corrected fix_installer_not_found_check/run_fix docstrings, hardened shell-metacharacter denylist)
    - tan/commands/doctor_cmd.py: 4087 -> 4114
    - tan/core/bootstrap.py: 2269 -> 2275
- 2026-08-15 -- tan-cli#756: hand-port alp-sdk#1446's _aen_require_disjoint_slot0 into tan/planner/zephyr_board.py -- a dual-M55 AEN SoM with no per-role <role>_slot0 region is now refused at emit time instead of silently sharing one MRAM slot0 address
    - tan/planner/loader.py: 1301 -> 1313
    - tan/planner/zephyr_board.py: 1433 -> 1486
    - function_count_budget: raised on this branch from 257 to 258; the final count after merging tan-cli#760 is recorded by the merge-resync entry below
- 2026-08-15 -- merge-resync (growth already reasoned on the merged branches)
    - function_count_budget: 258 -> 259
- 2026-08-15 -- ADR-0028 Task 2: relocate the alp-sdk model engine (13 modules, 1029 lines) into tan.model verbatim; two of its already-existing over-50-line functions move with it
    - function_count_budget: 258 -> 260
- 2026-08-15 -- tan-cli#782: model_cmd.py grows past 800 wiring `tan model check` (a third subcommand's board.yaml/SDK resolution, per-model dispatch and envelope shaping, matching build/doctor's own shape); function_count_budget's 260->261 is a PRE-EXISTING drift from the model-doctor merge (25443c4), not from this change -- measured before any edit in this session, fixed here since it blocks a green gate.
    - tan/commands/model_cmd.py: new entry at 893
    - function_count_budget: 260 -> 261
- 2026-08-16 -- tan-cli#789 review (f): model_cmd.py 893 -> 938 for _shipped_caveat_issues -- tan model build now reads the package it just wrote back (package.read_manifest_file) and reports each shipped blob's compiler caveat as a model.target-caveat warning, plus the model.caveat-readback-failed fallback and the exit-code split that keys on error-severity issues only. Kept in the command module deliberately: it is artifact IO plus envelope Issue construction, both of which tan/core is IO- and Issue-free by convention (see tan/core/model_check.py's own 'No IO' docstring), and the message construction it would leave behind is four lines.
    - tan/commands/model_cmd.py: 893 -> 938
- 2026-08-16 -- alp-sdk #1470 threads each SoM's vela memory profile from SoC metadata to the vela command line: VelaAdapter.compile, check._maybe_exact_ethos_u and ethos_u._footprint each cross 50 lines on measured-fact DOCSTRING and comment growth, not on logic. compile/_maybe_exact_ethos_u gained the two vela_* kwargs plus the paragraph saying they DO change the artifact (unlike silicon_ref, which reaches diagnostics only); _footprint gained the record of a known open gap -- under --memory-mode Sram_Only it reports the arena alone, because vela files the const region under on_chip_flash as a bookkeeping rename that is still SRAM0-resident on an Alif part (measured: 72.0 reported vs 72.0 + 235.265625 KiB for person_detect_int8.tflite at ethos-u85-256). Splitting a docstring out of the function it documents is not the extraction this ratchet asks for.
    - function_count_budget: 261 -> 264
- 2026-08-16 -- tan-cli#789 review BLOCKER: _refuse_zero_sram_footprint 42 -> 59 for the comment recording which test actually enforces its selector-clause wording -- the two tests it used to name enforce nothing on this template, and the one that does only started doing so now that it renders its note by CALLING this function instead of raising a hand-copied literal of it. The 17 lines are kept INSIDE the function deliberately, because A FLAT RATCHET DOES NOT IMPLY A FLAT FILE (tan-cli#789 review MINOR 2): core.long_functions measures end_lineno - lineno per def, so prose that moves from a docstring into a comment block ABOVE the def leaves every number here unchanged while the module grows. Measured across 88ef2f1 -> 08314b0: tan/model/adapters/ethos_u.py went 591 -> 725 lines while _refusal_remedy's span SHRANK 50 -> 42 and this file's over-50 count held at 2; only compile moved (59 -> 75). Moving that prose out of the docstrings was the right call and is NOT being undone -- but do not read a flat function_count as a flat module. As of this entry ethos_u.py measures 742 lines against MODULE_CAP 800, i.e. 58 lines of headroom before it joins the tracked modules map and starts failing outright, and nothing in this ratchet will warn on the way there.
    - function_count_budget: 264 -> 265
- 2026-08-16 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/model_cmd.py: new entry at 938
    - function_count_budget: 259 -> 266
