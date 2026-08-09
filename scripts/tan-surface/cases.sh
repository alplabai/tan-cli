#!/usr/bin/env bash
# The ordered command surface. THIS is the file you edit when tan grows a
# command or an issue below gets fixed.
#
# Ordering is a dependency chain, not taste: `size` needs a build, `build`
# needs a bootstrapped workspace, the workspace needs an SDK, and `clean`
# destroys what the earlier phases produced -- so it runs last. Run a single
# phase with `--phase <name>` when you only care about one link.
#
# Expectations target tan 0.5.2 (re-derived against `dev` -- run every
# assertion below against the real binary/SDK before trusting an OLDER
# recorded expectation; see the "--version" and command-surface-drift steps
# right below, which exist to catch a case list that fell out of sync). Every
# `xstep`/`xstep_out` entry is a defect with an open issue: while the bug
# stands the run is green, and the day it is fixed the harness reports XPASS,
# exits non-zero, and names the entry to delete. Do not convert an XPASS back
# into an xstep to quiet it.
#
# WRITING AN xstep_out PATTERN. Make it something the FIXED output cannot
# match. `grep` is line-oriented, so a pattern matching the summary line still
# matches after a fix that ADDS an error line below it -- that produced a false
# XFAIL here against 0.5.1, which is the exact silent-staleness this file
# exists to prevent. Prefer asserting the fixed behaviour positively with
# `step`/`step_out` and keep `xstep_out` for output that genuinely disappears.
#
# `tan flash` is absent by design -- see lib.sh.

# The command surface `tan --help` is expected to list, one per line, group
# headers excluded. `flash` is real and deliberately absent from this
# harness (see lib.sh); everything else tan registers must be here. This is
# still a hand-kept list -- there is no non-Python way to ask the binary for
# its own registration table -- but check_command_surface() below proves it
# against the binary's OWN --help output every run, so command 33 landing
# unlisted here is a loud FAIL, not silent drift (tan/cli.py's own
# `_SUBCOMMAND_NAMES` derivation exists for exactly this reason on the Python
# side; this is the shell-side equivalent).
KNOWN_COMMANDS='bootstrap
build
clean
completion
debug-config
diff
doctor
examples
explain
faultdecode
flash
generate
image
init
inspect
kconfig
lock
migrate
model
monitor
new-som
pinmux
presets
quality
renode
run
scaffold
sdk
size
support-bundle
trace
validate'

check_command_surface() {
  local seen unknown missing
  seen=$("$TAN" --help 2>/dev/null | grep -oE '^│ [a-z][a-z-]*' | awk '{print $2}' | sort -u)
  unknown=$(comm -23 <(printf '%s\n' "$seen") <(printf '%s\n' "$KNOWN_COMMANDS" | sort -u))
  missing=$(comm -13 <(printf '%s\n' "$seen") <(printf '%s\n' "$KNOWN_COMMANDS" | sort -u))
  if [ -n "$unknown" ] || [ -n "$missing" ]; then
    FAIL=$((FAIL+1)); FAILED_LABELS+=("command-surface drift")
    _result FAIL "command-surface drift" "see notes below"
    [ -n "$unknown" ] && note "tan --help lists a command this file does not cover: $(printf '%s' "$unknown" | tr '\n' ' ')"
    [ -n "$missing" ] && note "this file expects a command tan --help no longer lists: $(printf '%s' "$missing" | tr '\n' ' ')"
    _ledger FAIL "command-surface drift" KNOWN_COMMANDS "1" "" --help
  else
    PASS=$((PASS+1)); _result PASS "command-surface drift" "tan --help matches KNOWN_COMMANDS (flash excluded by design)"
    _ledger PASS "command-surface drift" KNOWN_COMMANDS "0" "" --help
  fi
}

# --------------------------------------------------------------------------
# 1. discovery -- everything that needs no project of its own
# --------------------------------------------------------------------------
phase_discovery() {
  hdr "discovery (no project required)"

  check_command_surface

  step "--version"                     0 -- --version
  # Expectations below are pinned to a specific tan version's behaviour (see
  # the file header). Prove that is what is actually running, rather than
  # silently scoring 0.5.0's eight retired xsteps as regressions against a
  # binary this file was never written for.
  step_out "--version prints a tan version" '^tan [0-9]+\.[0-9]+\.' -- --version
  step "--help lists the surface"      0 -- --help

  step "presets"                       0 -- presets --sdk-root "$SDK"
  envelope "presets envelope"            -- presets --sdk-root "$SDK"
  step "examples"                      0 -- examples --sdk-root "$SDK"
  envelope "examples envelope"           -- examples --sdk-root "$SDK"

  step "explain a target"              0 -- explain --target zephyr-conf --sdk-root "$SDK"

  # Every template the --help text advertises must actually explain. A template
  # that ships in the id list but has no catalog entry is a real break.
  local t
  for t in minimal-app zephyr-app sensor-starter iot-starter edge-ai-starter board-diagnostics; do
    step "explain template $t"         0 -- explain --template "$t" --sdk-root "$SDK"
  done

  step "pinmux aen"                    0 -- pinmux --sdk-root "$SDK" --family aen

  # Until 0.5.1 (#458) text mode printed ONLY the summary line, so an exit-2
  # error was invisible and a typo'd family looked identical to a real table.
  # The exit codes never moved, which is why these assert the MESSAGE.
  step "pinmux v2n exits 2"            2 -- pinmux --sdk-root "$SDK" --family v2n
  step_out "pinmux v2n prints its error" 'parsed with zero pads' \
      -- pinmux --sdk-root "$SDK" --family v2n
  # Re-derived against dev, NOT a guess: an unknown family is a MISSING table,
  # a different code path from v2n's PARSED-but-empty one above --
  # pinmux_cmd.py's own file-not-found branch appends a `warning`-severity
  # issue and leaves exit_code at its initial ExitCode.SUCCESS, by design (its
  # comment calls this the same "I don't know" shape every unreadable table
  # gets). Measured: exit 0. Do not "fix" this to exit 2 -- that would be
  # reverting a deliberate choice, not closing a gap.
  step_out_rc "pinmux names an unknown family" 0 'No pinmux capability table' \
      -- pinmux --sdk-root "$SDK" --family totally-bogus-xyz

  # sdk install/switch are unported in this build and must refuse rather than
  # half-act. Verified they touch no global state.
  step "sdk current"                   0 -- sdk current --sdk-root "$SDK"
  step "sdk list (offline)"            0 -- sdk list
  step "sdk install refuses"           1 -- sdk install 0.14.0
  step "sdk switch refuses"            1 -- sdk switch 0.14.0

  local sh
  for sh in bash zsh fish; do
    step "completion --shell $sh"      0 -- completion --shell "$sh"
  done

  # A decode with no ELF and no project -- pure register math, so it is the one
  # command that can be asserted on content cheaply.
  #
  # TWO assertions, not one `A|B` alternation: `'PRECISERR|BFARVALID'` also
  # matches faultdecode's own "(BFARVALID not set -- address may be stale)"
  # note on a DIFFERENT CFSR value, so a decode that LOST BFARVALID would
  # still pass a step labelled PRECISERR+BFARVALID. Each pattern below is
  # anchored to the "[BFSR] <NAME> (bit N)" SET-flag line format, which the
  # "not set" prose never matches, and `step_out` has no AND -- two assertions
  # is how "both flags are SET" gets checked instead of "either substring
  # appears somewhere".
  step_out "faultdecode reports PRECISERR set" '\[BFSR\] PRECISERR \(bit 9\)' \
      -- faultdecode --cfsr 0x00008200 --hfsr 0x40000000 --bfar 0xDEADBEEF
  step_out "faultdecode reports BFARVALID set" '\[BFSR\] BFARVALID \(bit 15\)' \
      -- faultdecode --cfsr 0x00008200 --hfsr 0x40000000 --bfar 0xDEADBEEF
  envelope "faultdecode envelope"        -- faultdecode --cfsr 0x00008200

  # doctor's verdict is host-dependent (no J-Link, no Zephyr SDK, ...), so the
  # exit code is deliberately unasserted. The envelope shape is not negotiable.
  step "doctor runs"                 any -- doctor --sdk-root "$SDK"
  envelope "doctor envelope"             -- doctor --sdk-root "$SDK"
}

# --------------------------------------------------------------------------
# 2. project -- scaffold, then everything that only reads board.yaml
# --------------------------------------------------------------------------
phase_project() {
  hdr "project (read-only against board.yaml)"

  if [ "$MODE" = sandbox ]; then
    step "init --preview writes nothing" 0 -- init --template "$TEMPLATE" --som "$SOM" \
        --name "$PROJ_NAME" --destination "$WORK" --sdk-root "$SDK" --preview
    step "init creates the project"      0 -- init --template "$TEMPLATE" --som "$SOM" \
        --name "$PROJ_NAME" --destination "$WORK" --sdk-root "$SDK" --force
    step "init --from-example"           0 -- init --from-example peripheral-io/gpio-button-led \
        --name "$PROJ_NAME-example" --destination "$WORK" --sdk-root "$SDK" --force
  else
    skip "init" "--project given; not scaffolding over a real tree"
  fi

  if [ ! -f "$PROJ/board.yaml" ]; then
    # A bare `warn` here used to record neither SKIP nor FAIL: an `init` that
    # exited 0 while writing no board.yaml silently dropped every
    # project-phase assertion from the counts, so the run looked no smaller
    # than a full one. `skip` at least puts the drop where the summary can
    # see it.
    skip "project phase (validate/diff/inspect/trace/doctor)" "no board.yaml at $PROJ"
    return
  fi

  step "validate a clean board.yaml"   0 -- validate --project "$PROJ" --sdk-root "$SDK"
  envelope "validate envelope"           -- validate --project "$PROJ" --sdk-root "$SDK"
  step "diff"                          0 -- diff --project "$PROJ" --sdk-root "$SDK"
  step "inspect"                       0 -- inspect --project "$PROJ" --sdk-root "$SDK"
  envelope "inspect envelope"            -- inspect --project "$PROJ" --sdk-root "$SDK"
  step "trace"                         0 -- trace --project "$PROJ" --sdk-root "$SDK"
  step "doctor in-project"           any -- doctor --project "$PROJ" --sdk-root "$SDK"

  # The negative half: a board.yaml the SDK cannot build. Written to a scratch
  # directory, never to $PROJ, so this is safe even in --project mode.
  local bad="$WORK/invalid-board"
  mkdir -p "$bad"
  cat >"$bad/board.yaml" <<'YAML'
som:
  sku: E1M-NOSUCHSKU
preset: not-a-real-preset
cores:
  m55_hp:
    app: ./src
    peripherals: [i2c, notaperipheral]
YAML
  step "validate rejects a bad board"  2 -- validate --project "$bad" --sdk-root "$SDK"
  # Until 0.5.1 (#455) diff parsed the YAML, skipped the schema, and reported
  # "no differences" -- exit 0 -- on the file validate rejects with four errors.
  step "diff rejects a bad board"      2 -- diff --project "$bad" --sdk-root "$SDK"

  printf 'this is not: [a, valid\n  board yaml\n' >"$bad/board.yaml"
  step "diff catches malformed YAML"   2 -- diff --project "$bad" --sdk-root "$SDK"
}

# --------------------------------------------------------------------------
# 3. generate -- everything that WRITES into the project tree
# --------------------------------------------------------------------------
phase_generate() {
  hdr "generate + scaffold (writes into the project)"

  if ! can_mutate; then
    skip "generate/scaffold/debug-config/new-som" "real project without --allow-mutate"
    return
  fi

  step "generate one target"           0 -- generate --project "$PROJ" --target zephyr-conf --sdk-root "$SDK"
  step "generate is idempotent"        0 -- generate --project "$PROJ" --target zephyr-conf --sdk-root "$SDK"
  step "generate --all (first run)"    0 -- generate --project "$PROJ" --all --force --sdk-root "$SDK"
  # Until 0.5.1 (#457) the second run exited 3: eight targets rewrote freely and
  # the ninth (boards/*.overlay, outside build/) refused, aborting the whole run.
  step "generate --all re-runs"        0 -- generate --project "$PROJ" --all --sdk-root "$SDK"
  step "generate --all --force"        0 -- generate --project "$PROJ" --all --force --sdk-root "$SDK"

  step "scaffold --preview"            0 -- scaffold --project "$PROJ" --template sensor-driver \
      --name surfaceprobe --preview --sdk-root "$SDK"
  step "scaffold a module"             0 -- scaffold --project "$PROJ" --template sensor-driver \
      --name surfaceprobe --force --sdk-root "$SDK"

  # Until 0.5.1 (#456) this defaulted to native-host on a hardware project and
  # wrote a program path the build never produces. It now refuses to guess while
  # no manifest exists, which is the behaviour worth pinning -- a default that
  # guesses wrong is worse than one that asks.
  # The old `'system-manifest.yaml|--target-kind'` alternation would also
  # match `debug-config --help`'s own options table (which lists
  # --target-kind), so an unrelated regression that dumped help text instead
  # of refusing could still pass this step. Anchored to the actual refusal
  # sentence instead, and paired with its exit code in one invocation.
  step_out_rc "debug-config refuses to guess pre-build" 2 \
      'no build/system-manifest\.yaml exists yet' \
      -- debug-config --project "$PROJ" --preview --sdk-root "$SDK"
  step "debug-config explicit zephyr-mcu" 0 -- debug-config --project "$PROJ" \
      --target-kind zephyr-mcu --server jlink --core "$CORE" --preview --sdk-root "$SDK"

  # new-som defaults --output-root to the SDK CHECKOUT. Always pass an explicit
  # scratch root; without it a surface walk silently edits the SDK.
  step "new-som --dry-run"             0 -- new-som --sku E1M-TST101 --soc-ref nxp:imx9:imx95 \
      --family nxp-imx9 --output-root "$WORK/new-som" --sdk-root "$SDK" --dry-run
  step "new-som writes skeletons"      0 -- new-som --sku E1M-TST101 --soc-ref nxp:imx9:imx95 \
      --family nxp-imx9 --output-root "$WORK/new-som" --sdk-root "$SDK" --force
}

# --------------------------------------------------------------------------
# 4. workspace -- bootstrap, then the west-backed commands
# --------------------------------------------------------------------------
phase_workspace() {
  hdr "workspace (bootstrap + west-backed commands)"

  # Exit 0 in both states: a fresh checkout plans the move, and one already
  # inside its workspace plans an in-place bootstrap. 0.5.0 refused the second
  # case outright (#469) -- and its refusal named a stringified `None`.
  step "bootstrap --dry-run"           0 -- bootstrap --project "$PROJ" --sdk-root "$SDK" --dry-run
  envelope "bootstrap --dry-run envelope" -- bootstrap --project "$PROJ" --sdk-root "$SDK" --dry-run
  step "print-env + --workspace conflicts" 2 -- bootstrap --project "$PROJ" --sdk-root "$SDK" \
      --print-env --workspace "$WORK/ws"

  if [ "$BOOTSTRAPPED" != 1 ]; then
    skip "quality, migrate, lock, kconfig" "no bootstrapped workspace (see --allow-bootstrap)"
    return
  fi

  step "print-env after bootstrap"     0 -- bootstrap --project "$PROJ" --sdk-root "$SDK" --print-env

  # Until 0.5.1 (#454) neither command exposed the argument its west extension
  # declares required, so both were unreachable under every input. Assert both
  # halves: the working invocation, and the refusal when the argument is absent.
  #
  # The two working invocations are gated on can_mutate even though NEITHER
  # writes. What they report is a verdict on the PROJECT'S OWN content, not on
  # tan: `tan migrate --help` documents `--check` verbatim as "Report version
  # drift; nonzero on drift", and `quality --profile quick` grades the board
  # against the SDK's rules. Hard-asserting exit 0 against an operator's real
  # --project therefore scores THEIR drift or THEIR quality findings as a tan
  # regression -- the same "reports a verdict it did not earn" shape the
  # `model build` gate in phase_diag closes, for the same reason. Against the
  # scaffolded sandbox project the assertion is real (it is generated from the
  # current templates on the spot, so it cannot be drifted), so it keeps its
  # exit 0 there and asks for the operator's explicit --allow-mutate signal
  # before running against a real tree.
  #
  # The two REFUSAL halves stay ungated: they are argument validation, decided
  # before either command looks at the project's content at all, so they say
  # the same thing about tan whatever the board.yaml holds.
  if can_mutate; then
    step "quality --profile quick"     0 -- quality --project "$PROJ" --profile quick --sdk-root "$SDK"
    step "migrate --check"             0 -- migrate --project "$PROJ" --check --sdk-root "$SDK"
  else
    skip "quality --profile quick, migrate --check" \
        "real project without --allow-mutate (their own drift/findings are not a tan regression)"
  fi
  step "quality demands --profile"     2 -- quality --project "$PROJ" --sdk-root "$SDK"
  step "migrate demands a mode"        2 -- migrate --project "$PROJ" --sdk-root "$SDK"

  # `lock` WRITES a lock file into the project -- gate it like the other
  # writing steps below, not only the ones in a phase named "generate".
  if can_mutate; then
    step "lock"                        0 -- lock --project "$PROJ" --sdk-root "$SDK"
  else
    skip "lock" "real project without --allow-mutate"
  fi

  # Until 0.5.1 (#453) kconfig looked up a bare `west` and inherited the ambient
  # ZEPHYR_BASE instead of resolving the workspace tan itself built -- so it
  # failed against the exact environment the quickstart produces.
  step "kconfig emits the symbol menu" 0 --timeout 900 \
      -- kconfig --project "$PROJ" --core "$CORE" --sdk-root "$SDK"
}

# --------------------------------------------------------------------------
# 5. build -- the real compile, then everything that consumes its output
# --------------------------------------------------------------------------
phase_build() {
  hdr "build + post-build"

  # Ordered on purpose: assert the pre-build refusals BEFORE building, because
  # after a build they cannot be observed again without a clean. All three are
  # pure reads (no build/ exists yet to write over), so these run regardless
  # of --allow-mutate.
  if [ ! -f "$PROJ/build/system-manifest.yaml" ]; then
    step "size refuses without a build"   1 -- size   --project "$PROJ" --sdk-root "$SDK"
    step "image refuses without a build"  1 -- image  --project "$PROJ" --sdk-root "$SDK"
    # renode_plan.py's ManifestUnavailable path falls through to fail_sdk()'s
    # default exit code (RUNTIME_FAILURE=1), same as size/image above --
    # verified directly, not assumed carried over from a prior version.
    step_out_rc "renode refuses without a build" 1 'no system-manifest.yaml' \
        -- renode --project "$PROJ" --sdk-root "$SDK"
  else
    skip "pre-build refusals" "build/ already present -- run --phase teardown first"
  fi

  # `--plan` only previews; deferred (#427) and refuses without touching disk.
  step "build --plan is deferred"      1 -- build --project "$PROJ" --plan --sdk-root "$SDK"

  # Everything past this point WRITES into $PROJ (build --materialise first,
  # then a real `build`/`run`/`renode`) -- gate it exactly like the other
  # writing phases, not only the ones named "generate"/"teardown". This is
  # the fix for the false "read-only against a real project" claim: before
  # it, `build --materialise`, the full `build`, and `run` all ran
  # unconditionally against a real --project with no --allow-mutate.
  if ! can_mutate; then
    skip "build --materialise, build, size, image, run, renode, debug-config (post-build)" \
        "real project without --allow-mutate"
    return
  fi

  step "build --materialise"           0 --timeout 600 -- build --project "$PROJ" --materialise --sdk-root "$SDK"

  if [ "$BOOTSTRAPPED" != 1 ]; then
    skip "build, run, size, image, renode" "no bootstrapped workspace (see --allow-bootstrap)"
    return
  fi

  step "build"                         0 --timeout 1800 -- build --project "$PROJ" --sdk-root "$SDK"
  envelope "build envelope"              --timeout 1800 -- build --project "$PROJ" --sdk-root "$SDK"

  step "size"                          0 -- size  --project "$PROJ" --sdk-root "$SDK"
  envelope "size envelope"               -- size  --project "$PROJ" --sdk-root "$SDK"
  step "image"                         0 -- image --project "$PROJ" --sdk-root "$SDK"

  # The other half of #456: with a manifest to read, the target class is
  # inferred rather than guessed, and it is a hardware one. `--preview` keeps
  # this read-only even though it now runs inside the mutate gate above.
  step_out_rc "debug-config infers the MCU target post-build" 0 'target=zephyr-mcu' \
      -- debug-config --project "$PROJ" --core "$CORE" --preview --sdk-root "$SDK"

  # No --flash: on a hardware project `run` must build, report, and stop --
  # exit 0 (run_cmd.py's BUILD_ONLY action returns the build's own exit code,
  # and a successful build's is SUCCESS), with the exact line `run_cmd.py`
  # prints for that action, not the looser 'pass --flash|built' alternation
  # (which a bare "built" substring anywhere would also satisfy).
  step_out_rc "run stops short of flashing" 0 \
      'run: built; pass --flash to program the board\.' \
      --timeout 1800 -- run --project "$PROJ" --sdk-root "$SDK"

  if command -v renode >/dev/null 2>&1; then
    # Until 0.5.1 (#470) renode accepted --project and resolved the build root
    # from the CWD anyway. These assert the MESSAGE, not only exit 1, because
    # under the old behaviour "there is no build" and "pick a core" collapsed
    # into the same CWD-lookup failure and an exit-code check measured
    # nothing. Both this SoM's zephyr slices (m55_hp, m55_he) resolving into
    # the manifest is what makes this ambiguous without --core in the first
    # place; renode_plan.py's multi-slice RenodeError falls through to the
    # same default exit code (RUNTIME_FAILURE=1) as the no-build case above.
    step_out_rc "renode demands --core on a multi-slice manifest" 1 \
        'zephyr slices|Pick one with' -- renode --project "$PROJ" --sdk-root "$SDK"
    step "renode boots one slice"      0 --timeout 300 -- renode --project "$PROJ" \
        --core "$CORE" --sdk-root "$SDK"
    # tan renode's exit code says nothing about whether the firmware reached
    # main(); --expect is the only assertion it offers. On the pinned Renode
    # v1.16.1 an MRAM-linked AEN image cannot complete a boot (documented in the
    # SDK's own alif_ensemble_e8.resc).
    #
    # #448 is a support PAUSE, not a removal -- the maintainer reframed it on
    # 2026-08-04 ("renode is PAUSED, not removed"; the issue title now reads
    # "emit a support-paused warning; retain the command, modules, fixtures and
    # CI models"). So this entry is not waiting for the command to disappear:
    # the command and its models are retained, and this xstep stays until the
    # boot itself is fixed. Do not delete the case on the assumption `renode`
    # is going away.
    xstep 448 "renode reaches the app console" 1 --timeout 300 \
        -- renode --project "$PROJ" --core "$CORE" --expect "$EXPECT_MARKER" --sdk-root "$SDK"
  else
    skip "renode" "no renode binary on PATH"
  fi
}

# --------------------------------------------------------------------------
# 6. diagnostics -- host-facing commands that need no board
# --------------------------------------------------------------------------
phase_diag() {
  hdr "diagnostics"

  # Both are failures: no port given, and a port that does not exist. Exit 1
  # each -- a monitor that could not open anything must not report success.
  step "monitor without --port"        1 -- monitor
  step "monitor rejects a bad port"    1 -- monitor --port /dev/tan-surface-no-such-port
  envelope "monitor envelope"            -- monitor --port /dev/tan-surface-no-such-port

  step "model without a subcommand"    1 -- model --project "$PROJ" --sdk-root "$SDK"
  # `--out` is pinned to an ABSOLUTE path inside $WORK: model_cmd.py resolves a
  # relative `--out` against the *project* root (model_cmd.py:352-354), not the
  # harness's scratch dir, so an unpinned `--out` against a project whose
  # board.yaml carries a `models:` block would land `build/models` inside
  # "$PROJ" itself. The absolute path makes the write land in $WORK regardless
  # of mode or the board's `models:` block.
  #
  # Gated on can_mutate like every other writing step, even though the pinned
  # --out keeps the write itself inside $WORK: a real --project's `models:`
  # block is board-owned content this harness cannot predict -- unlike the
  # scaffolded sandbox project (which never declares one, so this is always the
  # `models: []` no-op success path there), a real project's models may name
  # sources the harness's SDK/toolchain cannot compile, and hard-asserting exit
  # 0 against that unknown shape is the same "reports a verdict it did not
  # earn" failure mode the envelope/xstep fixes above closed. Requiring
  # --allow-mutate here matches the operator's own signal that running real
  # commands against their project is what they asked for.
  if can_mutate; then
    step "model build (models: optional)" 0 \
        -- model build --project "$PROJ" --sdk-root "$SDK" --out "$WORK/model-build-out"
  else
    skip "model build" "real project without --allow-mutate"
  fi

  # support-bundle WRITES $PROJ/.alp-support/*.json (proven right below by
  # reading it back) -- gate it like the project phase's other writing steps.
  if can_mutate; then
    step "support-bundle"              0 -- support-bundle --project "$PROJ" --sdk-root "$SDK"
    # The bundle snapshots the machine; a raw home-directory path inside it is the
    # leak the scrubber exists to stop. Assert the scrubber ran.
    local bundle
    bundle=$(ls -t "$PROJ/.alp-support/"*.json 2>/dev/null | head -1)
    if [ -n "$bundle" ] && grep -q "$HOME" "$bundle" 2>/dev/null; then
      FAIL=$((FAIL+1)); FAILED_LABELS+=("support-bundle scrubs the home path")
      _result FAIL "support-bundle scrubs the home path" "raw home path found in the bundle"
    elif [ -n "$bundle" ]; then
      PASS=$((PASS+1)); _result PASS "support-bundle scrubs the home path" "no raw home path"
    else
      skip "support-bundle scrubs the home path" "no bundle produced"
    fi
  else
    skip "support-bundle" "real project without --allow-mutate"
  fi
}

# --------------------------------------------------------------------------
# 7. teardown -- destroys the build output, so it runs last
# --------------------------------------------------------------------------
phase_teardown() {
  hdr "teardown"

  if ! can_mutate; then
    skip "clean" "real project without --allow-mutate"
    return
  fi
  step "clean removes the build dir"   0 -- clean --project "$PROJ" --sdk-root "$SDK"
  step "clean is idempotent"           0 -- clean --project "$PROJ" --sdk-root "$SDK"
}
