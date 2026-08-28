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

tan-cli#907: this file carries `merge=union` in the repo's `.gitattributes`,
so as of that change a real two-branch conflict on THIS file resolves itself
(both sides' appended entries kept, no conflict markers) -- that is an
interim mitigation for the git-mechanics problem, not a change to the
append-only contract above. The union keeps ours-then-theirs order regardless
of date (measured: a 2026-08-27 entry landed above a 2026-08-25 one), so don't
read entry order as chronological order -- each entry carries its own date for
that reason. The sibling `module_size_budget.generated.json`
in this same directory is deliberately NOT unioned (union-merging two JSON
documents that both add a key can leave two entries with no comma between
them, i.e. invalid JSON) and will still conflict normally. If you land here
resolving that conflict: do not hand-merge the JSON hunks -- take either
side, then rerun `python python/scripts/regen_module_size_budget.py --merge-resync`
from the repo root (that is where you land mid-conflict) and let it re-measure
and write both files from the real merged tree.

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
    - tan/core/flash_plan.py: 3164 -> 3201
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
- 2026-08-16 -- alp-sdk #1470 Tasks 4-5: re-source the vela footprint refusal's evidence from metadata, and wire the optional vendor .ini. ethos_u.py 742 -> 886 crosses the 800 cap the previous entry predicted it would ('58 lines of headroom ... and nothing in this ratchet will warn on the way there') -- the growth is measured-fact documentation, not logic: the three ethos-u-vela 5.1.0 exit codes that make --config/--system-config/--memory-mode a TRIPLE rather than a pair (--config alone and --config+--system-config are both rc=1, verbatim, and --config REPLACES Arm's vela.ini rather than merging), plus the two clauses now sourced per part (vendor_config_filename, external_memory_interfaces). Deliberately NOT split this time: the refusal/caveat text is one narrative with the module docstring that explains vela's memory model, and moving it out would sever the evidence from the code it justifies. The extraction to make when this file next grows is that whole diagnostics half (VelaFootprintRefused, _profile_clause, _refusal_remedy, _no_dram_marker, _refuse_zero_sram_footprint, _default_profile_caveats -- ~350 lines of pure string logic with no subprocess in it). model_cmd.py 938 -> 1011 for doctor's optional-prerequisite row (_vela_vendor_config_status + the data.optional[] shaping and its text line); the probe stays beside _deepx_dxm1_status/_drpai_status, which is where doctor's own narrower host probes live by convention. function_count 266 -> 268: VelaAdapter.compile and _vela_profile each cross 50 lines on that same measured-fact comment growth.
    - tan/commands/model_cmd.py: 938 -> 1011
    - tan/model/adapters/ethos_u.py: new entry at 886
    - function_count_budget: 266 -> 268
- 2026-08-16 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/model_cmd.py: new entry at 1011
    - tan/model/adapters/ethos_u.py: new entry at 886
    - function_count_budget: 260 -> 269
- 2026-08-16 -- tan/commands/model_cmd.py 1011 -> 1029: `_declared_hw_rev` + plumbing board.yaml's `som.hw_rev` into `check_model_backends`. A bench-measured perf point's identity includes the module revision it ran on (alp-sdk f724d3e4), so tier-2 resolution cannot serve an r2 measurement to a customer holding an r1 module; the value has to reach the engine from the only place that states it.
    - tan/commands/model_cmd.py: 1011 -> 1029
- 2026-08-17 -- tan-cli#791 review MINOR 5: _declared_hw_rev now fails CLOSED (ModelError) on a present-but-unusable som.hw_rev (e.g. an unquoted YAML int) instead of silently falling through to the SKU preset's default_hw_rev; function_count_budget's 269->270 (pre-drift) is a pre-existing gap measured before any edit this session (git stash showed 270 over 50 lines against a committed 269 budget), fixed here alongside the real growth
    - tan/commands/model_cmd.py: 1029 -> 1059
    - function_count_budget: 269 -> 272
- 2026-08-16 -- tan-cli#795 (as measured on the origin/dev side before this merge): relocate the expect_dpidr width guard, same change as above -- two branches' independent regens disagreed by 2 lines on flash_plan.py's total because of what else was in each tree at the time; kept as its own line rather than overwritten, per this log's own "two branches ... produce two lines" design
    - tan/core/flash_plan.py: 3164 -> 3199
- 2026-08-16 -- tan-cli#798/#801: build_root anchoring for the split-layout configure-cache guard, plus coupling the missing-tool message to a shared plan_exec constant, need a few lines of call-site docstring in execute.py and build_cmd.py that don't belong split elsewhere
    - tan/commands/build/execute.py: 1643 -> 1662
    - tan/commands/build_cmd.py: 2161 -> 2172
- 2026-08-16 -- tan-cli#804: consume _teardown_sim's grace-loop poll (which already waits up to _QUIT_GRACE_S) as the sim-exited-early source at --timeout 0, discriminated via a surfaced quit() write-failure or a nonzero exit code so a healthy quit-driven shutdown is never misreported
    - tan/commands/renode_cmd.py: 1533 -> 1587
    - function_count_budget: 260 -> 261
- 2026-08-17 -- merge-resync (growth already reasoned on the merged branches)
    - function_count_budget: 272 -> 273
- 2026-08-17 -- tan-cli#791 round-2 review: apply_perf_point's docstring grew to explain the item-2 refusal-note plumbing through every return path (_with_refusal_note); no logic was extracted, the growth is documentation
    - function_count_budget: 273 -> 274
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
- 2026-08-25 -- PR #878 fix round: re-verify #824/#792/#825/#814 prose against measured facts (98 real alp_project.py callers not 126, cold-chain-monitor converted at v0.16.0, template.py hedging) grew generate_cmd.py and template.py docstrings/comments
    - tan/commands/generate_cmd.py: 1360 -> 1361
    - tan/planner/template.py: 1472 -> 1487
- 2026-08-25 -- tan-cli#468: resolve_sdk now always returns an ActiveSdk (carrying broken_project_pin/foreign_global_default_for even when unresolved) instead of a bare None -- clean_cmd.py and diff_cmd.py grew threading that through their guards and envelope construction.
    - tan/commands/clean_cmd.py: 1120 -> 1129
    - tan/commands/diff_cmd.py: 882 -> 889
- 2026-08-25 -- tan-cli#466: origin-keyed global SDK default registry (~/.alp/sdk-defaults.json) -- resolve_sdk_tiered's globalDefault tier, its bootstrap-side writer/rollback in bootstrap_cmd.py, and the two-file global_default_pointer_fix_hint
    - tan/commands/bootstrap_cmd.py: 3272 -> 3364
    - tan/commands/doctor_cmd.py: 4038 -> 4040
    - tan/commands/sdk_cmd.py: 1416 -> 1472
    - function_count_budget: 254 -> 255
    - function_worst_budget: 747 -> 757
- 2026-08-25 -- tan-cli#466 follow-up: clarify global_default_foreign_project_issue's docstring now that a registry hit never sets foreign_global_default_for
    - tan/commands/sdk_cmd.py: 1472 -> 1480
- 2026-08-25 -- tan-cli#904 review round: resolved-origin ranking + RuntimeError/ELOOP handling + atomic registry write in sdk_cmd.py/bootstrap_cmd.py
    - tan/commands/bootstrap_cmd.py: 3364 -> 3378
    - tan/commands/sdk_cmd.py: 1480 -> 1514
    - function_count_budget: 255 -> 256
- 2026-08-25 -- PR #878 fix round (3rd pass): _scaffold_cmakelists's docstring trimmed to defer to _HARDCODED_ALP_PROJECT_PY_RE's own comment instead of restating the cold-chain-monitor/alp-sdk#1400 story -- a shrink, no --reason needed, but recorded here so the shipped ceiling (1481) has a traceable entry
    - tan/planner/template.py: 1487 -> 1481
- 2026-08-25 -- tan-cli#896: zephyr_board.py's _aen_flash_partitions docstring re-synced (comment-only) against alp-sdk 522ea3204's stale-prose fix
    - tan/planner/zephyr_board.py: 1486 -> 1494
- 2026-08-25 -- merge-resync (growth already reasoned on the merged branches)
    - tan/planner/zephyr_board.py: 1486 -> 1494
- 2026-08-25 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/bootstrap_cmd.py: 3272 -> 3382
    - tan/commands/doctor_cmd.py: 4038 -> 4040
    - tan/commands/sdk_cmd.py: 1416 -> 1533
    - function_count_budget: 254 -> 256
    - function_worst_budget: 747 -> 757
- 2026-08-25 -- tan-cli#904 third round: wall_clock_iso split off generated_at_iso, atomic_write_bytes added, registry rollback wired through it, docstring corrections (base-depth nit, changelog overclaim)
    - tan/commands/bootstrap_cmd.py: 3382 -> 3402
    - function_count_budget: 256 -> 257
- 2026-08-25 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/generate_cmd.py: 1348 -> 1361
    - tan/planner/template.py: 1470 -> 1481
- 2026-08-25 -- tan-cli#904 final round: wall_clock_iso (timestamp.py) grows past 50 lines defending an out-of-range wall clock (item 2) and enumerating all eight generated_at_iso call sites (nit); deepest_covering_entry's docstring (sdk_default_registry.py) grows documenting the updated_at precision-normalisation fix (nit) and the 21x1x20 lstat factorization correction (item 3) -- all four are review-requested prose/behaviour fixes on tan-cli#904's final round, not unreviewed growth.
    - function_count_budget: 257 -> 258
- 2026-08-26 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/model_cmd.py: new entry at 1059
    - tan/model/adapters/ethos_u.py: new entry at 886
    - function_count_budget: 258 -> 270
- 2026-08-26 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/model_cmd.py: new entry at 1059
    - tan/model/adapters/ethos_u.py: new entry at 886
    - function_count_budget: 258 -> 270
- 2026-08-26 -- tan-cli#900: examples_cmd.py/generate_cmd.py routed their unresolved-SDK refusal through the shared broken-project-pin/foreign-global-default disclosure (project_pin_issue/global_default_foreign_project_issue), matching resolve_sdk's tan-cli#468 fix; generate_cmd.py grew threading GenerateError.extra_issues through the refusal path
    - tan/commands/generate_cmd.py: 1361 -> 1400
    - function_count_budget: 258 -> 259
- 2026-08-26 -- tan-cli#922: init_cmd._sdk_block moves its Optional-collapse guard to the call site (matching the resolution-wrapper gate's own no-bare-None contract), growing the module past its recorded budget
    - tan/commands/init_cmd.py: 1258 -> 1276
- 2026-08-26 -- tan-cli#870: presets_cmd.py gains cores[].type/allowedOs -- SomCore/Som dataclasses, parse_som_preset's core-type/allowed-os enrichment, and the new _soc_lookups planner-binding helper (reuses tan.planner.topology._allowed_os_for_core rather than re-deriving the cortex-a/cortex-m rule)
    - tan/commands/presets_cmd.py: new entry at 844
    - function_count_budget: 258 -> 260
- 2026-08-26 -- tan-cli#870 follow-up: _soc_lookups reworked to stop importing tan.planner (its process-global SDK-root bind poisoned 292 unrelated parity tests when exercised from presets_cmd's many per-test synthetic checkouts) -- now reads tan.core.os_class + inlines the board-schema-enum/SoC-JSON reads directly
    - tan/commands/presets_cmd.py: 844 -> 887
- 2026-08-26 -- tan-cli#914 fix round: allowed_os_lookup guards the unresolved-core-type sentinel (Major -- degrade to [] instead of a plausible cross-class subset) with a mutation-proof test, module-docstring corrections attributing the duplicated-truth motivation to alp-sdk-vscode's coreRuntime.ts regex rather than alp-sdk-vscode#538 (Minor 1), and a tan-cli#917 follow-up pointer on _resolve_soc_path's known duplication (Minor 4)
    - tan/commands/presets_cmd.py: 887 -> 912
- 2026-08-26 -- correction, no number changed: the 2026-08-26 tan-cli#870 entry above ("new entry at 844") describes the design `_soc_lookups` was rewritten OUT of one entry later that same day ("follow-up") -- it names the abandoned `tan.planner.topology._allowed_os_for_core` reuse, not the shipped `tan.core.os_class` one. Append-only, so this stands as a correction rather than an edit to that line.
- 2026-08-26 -- review round: mutation-proof the a:b:c:d fixture (test_presets_command.py), narrow the presets/build-time-gate agreement claim to the cross-class exclusion (presets_cmd.py)
    - tan/commands/presets_cmd.py: 905 -> 915
- 2026-08-26 -- correction, no number changed: the 2026-08-26 tan-cli#914 fix round entry above ("887 -> 912") is the log's last entry before this round but is stale as a description of HEAD -- a later commit on the same PR (b35ad259, "share the unresolved-core-type degrade with the build-time gate") shrank `tan/commands/presets_cmd.py` from 912 to 905 (measured: `git show 62c2894a:python/tan/commands/presets_cmd.py | wc -l` -> 912, `git show b35ad259:...` -> 905), and that shrink was never logged. `module_size_budget.generated.json` already recorded the correct 905 (regen is measurement-driven, not log-driven, so it did not go stale); only this append-only log's prose fell behind. This round's own entry above measures growth from the true 905, not the stale 912. Append-only, so this stands as a correction rather than an edit to that line.
- 2026-08-26 -- review round: state the type+allowedOs degrade-disambiguation rule in presets_cmd.py's contract prose (Minor 4)
    - tan/commands/presets_cmd.py: 915 -> 929
- 2026-08-26 -- tan-cli#925: guard the ipc: append on the board not already declaring one. PyYAML accepts a duplicate top-level key and keeps the LAST, so the unconditional append silently discarded the project's own channel -- measured on alp-sdk's multicore-mailbox scaffold, where alp_shmem0 (referenced by SHMEM_REGION_NAME in both src/main.c and peer/main.c) was replaced by tan's stub. A correctness fix that cannot be written in zero lines; contrast #921, where a ratchet was DECLINED because the growth was a comment.
    - tan/core/scaffold.py: 1512 -> 1519
- 2026-08-26 -- tan-cli#864: register multicore-mailbox in TEMPLATE_IDS/_VENDORED_TEMPLATE_DIR and replace the one-off IOT_STARTER_SUPPORTED_SKU with the TEMPLATE_SUPPORTED_SKUS table, which two measured failures showed the single hard-coded if could not cover (a silent AEN301 render, and an init.template-unreadable that blamed the installation for a wrong --som).
    - tan/commands/explain_cmd.py: 1020 -> 1043
    - tan/commands/init_cmd.py: 1258 -> 1264
    - tan/core/scaffold.py: 1519 -> 1538
    - function_count_budget: 258 -> 259
- 2026-08-26 -- tan-cli#890: --from-example now consults the SDK scaffold catalog's supported.som_skus and warns when --som is outside it. The +24 in init_cmd.py is the guard and its Issue construction, not prose -- the reasoning lives in the new tan/core/example_catalog.py, and the in-place comment was cut to five lines pointing there. Contrast #921, where a ratchet was DECLINED because the growth was purely a comment.
    - tan/commands/init_cmd.py: 1264 -> 1288
- 2026-08-26 -- tan-cli#886: every inert option's help is rendered by tan.core.inert.inert_help, which costs build_cmd.py 3 lines and doctor_cmd.py 5 (one import each, plus the call's own wrapping).
    - tan/commands/build_cmd.py: 2175 -> 2178
    - tan/commands/doctor_cmd.py: 4040 -> 4045
- 2026-08-27 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/build_cmd.py: 2175 -> 2178
    - tan/commands/doctor_cmd.py: 4040 -> 4045
    - tan/commands/explain_cmd.py: 1020 -> 1043
    - tan/commands/init_cmd.py: 1258 -> 1288
    - tan/core/scaffold.py: 1512 -> 1538
    - function_count_budget: 270 -> 271
    - tan/commands/init_cmd.py: 1276 -> 1306
    - tan/core/scaffold.py: 1512 -> 1538
    - function_count_budget: 259 -> 260
- 2026-08-27 -- merge-resync (growth already reasoned on the merged branches)
    - function_count_budget: 260 -> 261
- 2026-08-27 -- merge-resync (growth already reasoned on the merged branches)
    - function_count_budget: 261 -> 262
- 2026-08-27 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/generate_cmd.py: 1361 -> 1400
    - tan/commands/init_cmd.py: 1288 -> 1306
    - tan/commands/presets_cmd.py: new entry at 929
    - function_count_budget: 271 -> 274
- 2026-08-27 -- tan-cli#950: bind_sdk/_fail carry ExplainError.extra_issues (pin/foreign-default disclosure) + two regression tests
    - tan/commands/explain_cmd.py: 1043 -> 1087
    - function_count_budget: 262 -> 263
- 2026-08-27 -- tan-cli#926: restored the deleted #263-review/#464 provenance comments (grew bootstrap_cmd.py + new_som_cmd.py and bootstrap_cmd.py:_run past their recorded ceilings) and fixed bootstrap's sdk-root-unresolved refusal to render its pin/foreign-default warnings on the text channel too (tan-cli#677 recurrence), not just --format json
    - tan/commands/bootstrap_cmd.py: 3402 -> 3421
    - tan/commands/new_som_cmd.py: 1380 -> 1385
    - function_worst_budget: 757 -> 770
- 2026-08-27 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/bootstrap_cmd.py: 3402 -> 3421
    - tan/commands/new_som_cmd.py: 1380 -> 1385
    - function_worst_budget: 757 -> 770
- 2026-08-28 -- merge-resync (growth already reasoned on the merged branches)
    - tan/commands/explain_cmd.py: 1043 -> 1087
    - function_count_budget: 274 -> 275
- 2026-08-28 -- tan-cli#959: explain_cmd.py grows a shared _print_sdk_resolution_warnings() helper wired into the success path and _fail's text branch, closing the text-mode SDK-advisory disclosure gap
    - tan/commands/explain_cmd.py: 1087 -> 1135
