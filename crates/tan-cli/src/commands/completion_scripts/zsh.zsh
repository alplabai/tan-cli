#compdef tan

_tan() {
  local -a commands
  commands=(
    'validate:Validate board.yaml config'
    'generate:Generate derived artifacts'
    'init:Initialize a starter project'
    'scaffold:Scaffold module files'
    'examples:List SDK example projects'
    'doctor:Run debug and environment checks'
    'completion:Generate shell completion script'
    'diff:Show board normalization diff'
    'presets:List SDK presets'
    'pinmux:Show pinmux capability table'
    'explain:Explain templates and targets'
    'inspect:Inspect effective resolved values'
    'trace:Trace generation decisions'
    'debug-config:Generate a launch.json debug configuration'
    'support-bundle:Export support bundle payload'
    'sdk:Manage local SDK installs'
    'bootstrap:Set up the SDK build environment'
    'build:Build the project natively'
    'kconfig:Show the board-scoped Kconfig symbol menu'
    'image:Assemble a flashable image bundle'
    'flash:Flash slices and helper MCUs onto the device'
    'run:Build then run the project'
    'clean:Remove the build dir and state cache'
    'renode:Boot the built manifest in headless Renode'
    'size:Report per-slice firmware footprint'
    'migrate:Migrate board.yaml to the current schema'
    'lock:Pin/lock library dependencies'
    'quality:Run board.yaml quality checks'
    'model:Compile and package board.yaml models'
    'monitor:Open a serial console to the board'
    'new-som:Scaffold a new SoM metadata skeleton'
    'faultdecode:Decode an ARM Cortex-M fault dump'
  )

  # Every flag `GlobalArgs` marks `global = true` (cli.rs) is accepted by
  # clap on EVERY subcommand — AND on the root command itself, before any
  # subcommand word is even typed — so every arm below splices this in, and
  # so does the root `_arguments -C` call a few lines down. Unlike bash's
  # single `$global_flags` string var, zsh's per-arm `_arguments` has no
  # inheritance of its own (issue #92 MAJOR 2) — a flag left out of an arm
  # here is simply not completable for that subcommand (or, left out of the
  # root call, not completable at `tan --<TAB>` before a subcommand: issue
  # #92 round-3 FINDING 1).
  local -a global_args
  global_args=(
    '--project[Project root]:path:_files -/'
    '--board-yaml[board.yaml path]:path:_files'
    '--sdk-root[SDK root]:path:_files -/'
    '--target[Generation target]'
    '--all[Generate all targets]'
    '--format[Output format]:format:(text json)'
    '--verbose[Verbose output]'
    '--quiet[Quiet output]'
    '--no-color[Disable color output]'
    '--non-interactive[Disable prompts]'
    '--ci[CI mode]'
    '--help[Show help]'
    '--version[Show version]'
  )

  _arguments -C     '1:command:->command'     '*::arg:->args'     "${global_args[@]}"

  case $state in
    command)
      _describe 'command' commands
      ;;
    args)
      case $words[2] in
        validate)
          _arguments '--offline[Offline structural validation only]' "${global_args[@]}"
          ;;
        completion)
          _arguments '--shell[Shell type]:shell:(bash zsh fish)' "${global_args[@]}"
          ;;
        generate)
          _arguments '--force[Overwrite existing files]' '--core[Core id (zephyr-board target)]' "${global_args[@]}"
          ;;
        explain)
          _arguments '--template[Template id]' "${global_args[@]}"
          ;;
        examples)
          _arguments '--filter[Substring match on id/title]' "${global_args[@]}"
          ;;
        init)
          _arguments '--template[Template id]' '--from-example[Example source dir]' '--name[Name value]' '--destination[Output directory]:path:_files -/' '--som[SoM SKU]' '--cores[Cores list]' '--preview[Preview only]' '--force[Overwrite existing files]' "${global_args[@]}"
          ;;
        scaffold)
          _arguments '--template[Template id]' '--name[Name value]' '--destination[Output directory]:path:_files -/' '--preview[Preview only]' '--force[Overwrite existing files]' "${global_args[@]}"
          ;;
        pinmux)
          _arguments '--sku[SoM SKU]' '--family[Pinmux family]' "${global_args[@]}"
          ;;
        doctor)
          _arguments '--target-kind[Debug target]:target:(zephyr-mcu baremetal-mcu yocto-userspace native-host)' '--server[Debug server]:server:(jlink openocd pyocd gdbserver none)' '--build[Build readiness preflight]' '--fix[Auto-repair a fixable blocker]' "${global_args[@]}"
          ;;
        inspect)
          _arguments '--path[Field path]' '--show-origin[Include source metadata]' "${global_args[@]}"
          ;;
        trace)
          _arguments '--path[Field path]' "${global_args[@]}"
          ;;
        debug-config)
          _arguments '--target-kind[Debug target]:target:(zephyr-mcu baremetal-mcu yocto-userspace native-host)' '--server[Debug server]:server:(jlink openocd pyocd gdbserver none)' '--core[Build slice core id]' '--pre-launch-task[VS Code task to run before launching]' '--preview[Preview only]' "${global_args[@]}"
          ;;
        support-bundle)
          _arguments '--destination[Output directory]:path:_files -/' '--target-kind[Debug target]:target:(zephyr-mcu baremetal-mcu yocto-userspace native-host)' '--server[Debug server]:server:(jlink openocd pyocd gdbserver none)' '--path[Field path]' "${global_args[@]}"
          ;;
        sdk)
          _arguments '1:subcommand:(list install current switch)' '--destination[Cache root]:path:_files -/' '--global[Pin the machine-global default]' "${global_args[@]}"
          ;;
        bootstrap)
          _arguments '--no-pip[Skip pip install]' '--no-west[Skip west init/update]' '--print-env[Print environment lines only]' "${global_args[@]}"
          ;;
        build)
          _arguments '--plan[Show the build plan]' '--plan-from[Read build plan from file]:path:_files' '--materialise[Materialise plan files]' '--native[Build natively]' '--manifest[Show the system manifest]' '--manifest-from[Read manifest from file]:path:_files' '--no-auto-bootstrap[Never bootstrap implicitly]' '--pristine[Force-wipe build dirs before dispatch]' "${global_args[@]}"
          ;;
        kconfig)
          _arguments '--core[Core id to scope the menu to]' "${global_args[@]}"
          ;;
        image)
          _arguments '--build-root[Override build root]:path:_files -/' "${global_args[@]}"
          ;;
        flash)
          _arguments '--build-root[Override build root]:path:_files -/' '--dry-run[Print planned commands only]' '--core[Flash only this core]' '--helper[Flash only this helper MCU]' '--skip-missing-tools[Skip entries with no tool on PATH]' "${global_args[@]}"
          ;;
        run)
          _arguments '--flash[Flash the board after building]' '--core[Flash only this core]' "${global_args[@]}"
          ;;
        clean)
          _arguments '--build-root[Override build root]:path:_files -/' '--dry-run[List targets without removing]' "${global_args[@]}"
          ;;
        renode)
          _arguments '--build-root[Override build root]:path:_files -/' '--board[Override SoM SKU]' '--core[Zephyr slice core id]' '--image-bundle[Pre-built artefacts dir]:path:_files -/' '--log[Console log file]:path:_files' '--timeout[Wall-clock cap in seconds]' '--expect[Stop early on this substring]' '--sim-mode[Studio hardware-simulator mode]' "${global_args[@]}"
          ;;
        size)
          _arguments '--build-root[Override build root]:path:_files -/' '--board[Override SoM SKU]' '--fail-over-budget[Exit non-zero over budget]' "${global_args[@]}"
          ;;
        *)
          _arguments "${global_args[@]}"
          ;;
      esac
      ;;
  esac
}

compdef _tan tan
