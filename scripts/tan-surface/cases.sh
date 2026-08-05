#!/usr/bin/env bash
# The ordered command surface. THIS is the file you edit when tan grows a
# command or an issue below gets fixed.
#
# Ordering is a dependency chain, not taste: `size` needs a build, `build`
# needs a bootstrapped workspace, the workspace needs an SDK, and `clean`
# destroys what the earlier phases produced -- so it runs last. Run a single
# phase with `--phase <name>` when you only care about one link.
#
# Expectations target tan 0.5.1. Every `xstep`/`xstep_out` entry is a defect
# with an open issue: while the bug stands the run is green, and the day it is
# fixed the harness reports XPASS, exits non-zero, and names the entry to
# delete. Do not convert an XPASS back into an xstep to quiet it.
#
# WRITING AN xstep_out PATTERN. Make it something the FIXED output cannot
# match. `grep` is line-oriented, so a pattern matching the summary line still
# matches after a fix that ADDS an error line below it -- that produced a false
# XFAIL here against 0.5.1, which is the exact silent-staleness this file
# exists to prevent. Prefer asserting the fixed behaviour positively with
# `step`/`step_out` and keep `xstep_out` for output that genuinely disappears.
#
# `tan flash` is absent by design -- see lib.sh.

# --------------------------------------------------------------------------
# 1. discovery -- everything that needs no project of its own
# --------------------------------------------------------------------------
phase_discovery() {
  hdr "discovery (no project required)"

  step "--version"                     0 -- --version
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
  step_out "pinmux names an unknown family" 'No pinmux capability table' \
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
  step_out "faultdecode PRECISERR+BFARVALID" 'PRECISERR|BFARVALID' \
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

  [ -f "$PROJ/board.yaml" ] || { warn "no board.yaml at $PROJ -- skipping the rest"; return; }

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

  if [ "$MODE" != sandbox ] && [ "$ALLOW_MUTATE" != 1 ]; then
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
  step "debug-config refuses to guess pre-build" 2 -- debug-config --project "$PROJ" \
      --preview --sdk-root "$SDK"
  step_out "and says a build is needed first" 'system-manifest.yaml|--target-kind' \
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
  step "quality --profile quick"       0 -- quality --project "$PROJ" --profile quick --sdk-root "$SDK"
  step "quality demands --profile"     2 -- quality --project "$PROJ" --sdk-root "$SDK"
  step "migrate --check"               0 -- migrate --project "$PROJ" --check --sdk-root "$SDK"
  step "migrate demands a mode"        2 -- migrate --project "$PROJ" --sdk-root "$SDK"
  step "lock"                          0 -- lock --project "$PROJ" --sdk-root "$SDK"

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
  # after a build they cannot be observed again without a clean.
  if [ ! -f "$PROJ/build/system-manifest.yaml" ]; then
    step "size refuses without a build"   1 -- size   --project "$PROJ" --sdk-root "$SDK"
    step "image refuses without a build"  1 -- image  --project "$PROJ" --sdk-root "$SDK"
    step_out "renode refuses without a build" 'no system-manifest.yaml' \
        -- renode --project "$PROJ" --sdk-root "$SDK"
  else
    skip "pre-build refusals" "build/ already present -- run --phase teardown first"
  fi

  step "build --plan is deferred"      1 -- build --project "$PROJ" --plan --sdk-root "$SDK"
  step "build --materialise"           0 --timeout 600 -- build --project "$PROJ" --materialise --sdk-root "$SDK"

  if [ "$BOOTSTRAPPED" != 1 ]; then
    skip "build, run, size, image, renode" "no bootstrapped workspace (see --allow-bootstrap)"
    return
  fi

  step "build"                         0 --timeout 1800 -- build --project "$PROJ" --sdk-root "$SDK"
  envelope "build envelope"              -- build --project "$PROJ" --sdk-root "$SDK"

  step "size"                          0 -- size  --project "$PROJ" --sdk-root "$SDK"
  envelope "size envelope"               -- size  --project "$PROJ" --sdk-root "$SDK"
  step "image"                         0 -- image --project "$PROJ" --sdk-root "$SDK"

  # The other half of #456: with a manifest to read, the target class is
  # inferred rather than guessed, and it is a hardware one.
  step_out "debug-config infers the MCU target post-build" 'target=zephyr-mcu' \
      -- debug-config --project "$PROJ" --core "$CORE" --preview --sdk-root "$SDK"

  # No --flash: on a hardware project `run` must build, report, and stop.
  step_out "run stops short of flashing" 'pass --flash|built' \
      --timeout 1800 -- run --project "$PROJ" --sdk-root "$SDK"

  if command -v renode >/dev/null 2>&1; then
    # Until 0.5.1 (#470) renode accepted --project and resolved the build root
    # from the CWD anyway. These assert the MESSAGE rather than exit 1, because
    # under the old behaviour "there is no build" and "pick a core" collapsed
    # into the same CWD-lookup failure and an exit-code check measured nothing.
    step_out "renode demands --core on a multi-slice manifest" \
        'zephyr slices|Pick one with' -- renode --project "$PROJ" --sdk-root "$SDK"
    step "renode boots one slice"      0 --timeout 300 -- renode --project "$PROJ" \
        --core "$CORE" --sdk-root "$SDK"
    # tan renode's exit code says nothing about whether the firmware reached
    # main(); --expect is the only assertion it offers. On the pinned Renode
    # v1.16.1 an MRAM-linked AEN image cannot complete a boot (documented in the
    # SDK's own alif_ensemble_e8.resc), and the command is slated for removal.
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
  step "model build with no models:"   0 -- model build --project "$PROJ" --sdk-root "$SDK"

  step "support-bundle"                0 -- support-bundle --project "$PROJ" --sdk-root "$SDK"
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
}

# --------------------------------------------------------------------------
# 7. teardown -- destroys the build output, so it runs last
# --------------------------------------------------------------------------
phase_teardown() {
  hdr "teardown"

  if [ "$MODE" != sandbox ] && [ "$ALLOW_MUTATE" != 1 ]; then
    skip "clean" "real project without --allow-mutate"
    return
  fi
  step "clean removes the build dir"   0 -- clean --project "$PROJ" --sdk-root "$SDK"
  step "clean is idempotent"           0 -- clean --project "$PROJ" --sdk-root "$SDK"
}
