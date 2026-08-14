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
