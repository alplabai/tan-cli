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
- 2026-08-16 -- tan-cli#760 second half (alp-sdk#1464/#1471): install.linux is now package-manager-keyed; bootstrap.py gains normalize_linux_install/select_linux_install/detect_linux_pm + wider BootstrapFacts.install typing, bootstrap_cmd.py/doctor_cmd.py wire package-manager detection into check_prerequisites/_collect
    - tan/commands/bootstrap_cmd.py: 3277 -> 3286
    - tan/commands/doctor_cmd.py: 4114 -> 4131
    - tan/core/bootstrap.py: 2275 -> 2400
    - function_count_budget: 259 -> 260
- 2026-08-16 -- tan-cli#795: relocate the expect_dpidr width guard (_validate_expect_dpidr_width) from flash_cmd.py's real-write-time preflight into flash_plan.py's plan-time validate_flow_d_preflight_args, beside its validate_address(expect_dpidr) calls, so a truncated expect_dpidr surfaces under --dry-run too; the pure banner-matching helpers it does not need (FlashPlanError) moved out to a new tan/core/dp_id.py instead, but this one function must stay beside the exception type it raises
    - tan/core/flash_plan.py: 3164 -> 3199
- 2026-08-16 -- tan-cli#798/#801: build_root anchoring for the split-layout configure-cache guard, plus coupling the missing-tool message to a shared plan_exec constant, need a few lines of call-site docstring in execute.py and build_cmd.py that don't belong split elsewhere
    - tan/commands/build/execute.py: 1643 -> 1662
    - tan/commands/build_cmd.py: 2161 -> 2172
- 2026-08-16 -- tan-cli#804: consume _teardown_sim's grace-loop poll (which already waits up to _QUIT_GRACE_S) as the sim-exited-early source at --timeout 0, discriminated via a surfaced quit() write-failure or a nonzero exit code so a healthy quit-driven shutdown is never misreported
    - tan/commands/renode_cmd.py: 1533 -> 1587
    - function_count_budget: 260 -> 261
- 2026-08-16 -- tan-cli#799: build the Envelope once, unconditionally, in size_cmd/clean_cmd/validate_cmd (and image_cmd/run_cmd, both still under the cap) so the sdk.discovery-divergent seam warning reaches text mode too, not only --format json
    - tan/commands/clean_cmd.py: 1110 -> 1119
    - tan/commands/size_cmd.py: 831 -> 842
    - tan/commands/validate_cmd.py: 1571 -> 1587
- 2026-08-16 -- tan-cli#799 review: scope validate_cmd's Envelope construction to TEXT/JSON only (nit fix)
    - tan/commands/validate_cmd.py: 1587 -> 1591
- 2026-08-16 -- tan-cli#799: validate's sdk.* seam-issue text now carries a severity prefix, matching clean/size/image/run
    - tan/commands/validate_cmd.py: 1591 -> 1613
- 2026-08-18 -- tan-cli#448: Renode is retired repo-wide; the `tan renode` verb and the three modules behind it (`tan/commands/renode_cmd.py`, `tan/core/renode_plan.py`, `tan/core/renode_sim.py`) are deleted, so `regen_module_size_budget.py` was re-run -- shrink-only, no `--reason` needed
    - tan/cli.py: 1008 -> 1006
    - tan/commands/renode_cmd.py: 1587 -> dropped (module deleted)
    - function_count_budget: 261 -> 252
- 2026-08-18 -- tan-cli#826: nine oracle-parity citations swept off the test modules tan-cli#269 deleted. Four of them said a deliberate divergence was PINNED by one of those modules; the correct replacement is not "unpinned" (an adversarial re-read of the first draft measured live tan-side tests holding all four) but the NAME of the test that took over, and cli.py's stderr rule had to be narrowed from "nothing compares stderr" to "Click's rendering is unpinned" for the same reason -- ~500 assertions under python/tests/ take stderr as their subject. Naming the real pin costs more words than either wrong version did. No code changed; every line here is a comment or docstring. scaffold.py's `_vendored_files` was kept under the 50-line function cap by tightening its wording rather than by raising the cap, so function_count_budget stays at 252.
    - tan/cli.py: 1006 -> 1015
    - tan/commands/debug_config_cmd.py: 1949 -> 1954
    - tan/commands/size_cmd.py: 842 -> 849
    - tan/core/flash_plan.py: 3199 -> 3205
    - tan/core/scaffold.py: 1510 -> 1512
- 2026-08-18 -- tan-cli#846: port alp-sdk#1535's _tag_resolves guard into _docs_ref (tan/planner/template.py), a verbatim relocation of scripts/alp_template.py where the same guard already lives upstream
    - tan/planner/template.py: 1436 -> 1470
- 2026-08-19 -- tan-cli#856: corrected the stale --fix sudo help text (3 lines longer)
    - tan/commands/doctor_cmd.py: 4035 -> 4038
- 2026-08-19 -- tan-cli#815: the shapes.py dedup finished. Six private helper definitions deleted (_is_file x4, _is_dir x2) plus sdk_cmd's duplicate SDK_MARKER and rejected_sdk_root_message, so seven modules shrink in the tree; the five tracked in this file are below. clean_cmd.py grows by exactly 1: it had no `from tan.core` import at all and now needs one line for SDK_MARKER, which it previously took from sdk_cmd's second spelling of the same literal.
    - tan/commands/clean_cmd.py: 1119 -> 1120
- 2026-08-20 -- tan-cli#868: the alp-sdk 94378a05..ac38a069 planner re-sync. kconfig.py +4 (the metadata-root argument threaded through _emit_subsystems / _per_core_library_kconfig / the six library-layer calls, alp-sdk#1485) and loader.py +22 (the same threading through _validate_topology_cores, plus the corrected _resolve_slot0_load_address docstring alp-sdk#1445 rewrote). Both are MIRROR modules of scripts/alp_orchestrate/: extracting here would put tan's copy out of shape with the upstream file every re-sync 3-way-merges against, which is the drift this repo pays a whole gate to prevent. partition.py grew by 260 lines on the same port and stayed inside its existing budget.
    - tan/planner/kconfig.py: 2088 -> 2092
    - tan/planner/loader.py: 1313 -> 1335
    - function_count_budget: 252 -> 254
- 2026-08-20 -- tan-cli#810: four single-use imports moved into their call sites, so a bare `tan --version` stops loading click.testing, jsonschema, PyYAML and urllib.request (427 -> 279 modules, measured). The lines are the deferred imports plus the comment at each site saying why it is not at module scope; sdk_cmd also gains a TYPE_CHECKING block and the paragraph explaining why `_releases_opener`'s return annotation had to be respelled. REGENERATED on the merge with dev rather than resolving the ratchet conflict by side-picking: #858/#862/#851 moved tan/cli.py too, so BOTH sides' numbers were wrong for the merged tree -- ours said 1012, dev's said 1015, the merged file is 1021.
    - tan/cli.py: 1015 -> 1021
    - tan/commands/new_som_cmd.py: 1361 -> 1381
    - tan/commands/sdk_cmd.py: 1415 -> 1441
- 2026-08-21 -- merge-resync (growth already reasoned on the merged branches)
    - tan/cli.py: 1015 -> 1021
    - tan/commands/new_som_cmd.py: 1360 -> 1380
    - tan/commands/sdk_cmd.py: 1392 -> 1416
- 2026-08-21 -- tan-cli#825: correcting the cmake/alp.cmake present-tense claim added 12 comment lines to tan/commands/generate_cmd.py (1348 -> 1360) and 2 to tan/planner/template.py (1470 -> 1472). Both are prose in place of a false statement -- alp.cmake is absent at alp-sdk v0.16.0-rc1, dev and main -- so there is no code to extract; the alternative to the raise is leaving the documentation wrong.
    - tan/commands/generate_cmd.py: 1348 -> 1360
    - tan/planner/template.py: 1470 -> 1472
- 2026-08-23 -- merge-resync (growth already reasoned on the merged branches)
    - function_count_budget: 252 -> 254
- 2026-08-25 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/generate_cmd.py: 1348 -> 1360
    - tan/planner/template.py: 1470 -> 1472
