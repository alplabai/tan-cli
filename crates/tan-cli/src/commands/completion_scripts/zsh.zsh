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

  _arguments -C     '1:command:->command'     '*::arg:->args'

  case $state in
    command)
      _describe 'command' commands
      ;;
    args)
      case $words[2] in
        completion)
          _arguments '--shell[Shell type]:shell:(bash zsh fish)' '--format[Output format]:format:(text json)' '--help[Show help]'
          ;;
        generate)
          _arguments '--target[Generation target]' '--all[Generate all targets]' '--force[Overwrite existing files]' '--format[Output format]:format:(text json)' '--project[Project root]:path:_files -/' '--board-yaml[board.yaml path]:path:_files' '--sdk-root[SDK root]:path:_files -/'
          ;;
        explain)
          _arguments '--template[Template id]' '--format[Output format]:format:(text json)'
          ;;
        init)
          _arguments '--template[Template id]' '--from-example[Example source dir]' '--name[Name value]' '--destination[Output directory]:path:_files -/' '--som[SoM SKU]' '--cores[Cores list]' '--preview[Preview only]' '--force[Overwrite existing files]' '--format[Output format]:format:(text json)'
          ;;
        scaffold)
          _arguments '--template[Template id]' '--name[Name value]' '--destination[Output directory]:path:_files -/' '--preview[Preview only]' '--force[Overwrite existing files]' '--format[Output format]:format:(text json)'
          ;;
        pinmux)
          _arguments '--sku[SoM SKU]' '--family[Pinmux family]' '--format[Output format]:format:(text json)'
          ;;
        doctor)
          _arguments '--target-kind[Debug target]:target:(zephyr-mcu baremetal-mcu yocto-userspace native-host)' '--server[Debug server]:server:(jlink openocd pyocd gdbserver none)' '--build[Build readiness preflight]' '--fix[Auto-repair a fixable blocker]' '--format[Output format]:format:(text json)'
          ;;
        inspect)
          _arguments '--path[Field path]' '--show-origin[Include source metadata]' '--format[Output format]:format:(text json)'
          ;;
        trace)
          _arguments '--path[Field path]' '--format[Output format]:format:(text json)'
          ;;
        debug-config)
          _arguments '--target-kind[Debug target]:target:(zephyr-mcu baremetal-mcu yocto-userspace native-host)' '--server[Debug server]:server:(jlink openocd pyocd gdbserver none)' '--preview[Preview only]' '--format[Output format]:format:(text json)'
          ;;
        support-bundle)
          _arguments '--destination[Output directory]:path:_files -/' '--target-kind[Debug target]:target:(zephyr-mcu baremetal-mcu yocto-userspace native-host)' '--server[Debug server]:server:(jlink openocd pyocd gdbserver none)' '--path[Field path]' '--format[Output format]:format:(text json)'
          ;;
        sdk)
          _arguments '1:subcommand:(list install current switch)' '--destination[Cache root]:path:_files -/'
          ;;
        bootstrap)
          _arguments '--no-pip[Skip pip install]' '--no-west[Skip west init/update]' '--print-env[Print environment lines only]' '--format[Output format]:format:(text json)'
          ;;
        build)
          _arguments '--plan[Show the build plan]' '--plan-from[Read build plan from file]:path:_files' '--materialise[Materialise plan files]' '--native[Build natively]' '--manifest[Show the system manifest]' '--manifest-from[Read manifest from file]:path:_files' '--format[Output format]:format:(text json)'
          ;;
        kconfig)
          _arguments '--core[Core id to scope the menu to]' '--format[Output format]:format:(text json)'
          ;;
        image)
          _arguments '--build-root[Override build root]:path:_files -/' '--format[Output format]:format:(text json)'
          ;;
        flash)
          _arguments '--build-root[Override build root]:path:_files -/' '--dry-run[Print planned commands only]' '--core[Flash only this core]' '--helper[Flash only this helper MCU]' '--skip-missing-tools[Skip entries with no tool on PATH]' '--format[Output format]:format:(text json)'
          ;;
        run)
          _arguments '--flash[Flash the board after building]' '--core[Flash only this core]' '--format[Output format]:format:(text json)'
          ;;
        clean)
          _arguments '--build-root[Override build root]:path:_files -/' '--dry-run[List targets without removing]' '--format[Output format]:format:(text json)'
          ;;
        renode)
          _arguments '--build-root[Override build root]:path:_files -/' '--board[Override SoM SKU]' '--image-bundle[Pre-built artefacts dir]:path:_files -/' '--log[Console log file]:path:_files' '--timeout[Wall-clock cap in seconds]' '--expect[Stop early on this substring]' '--sim-mode[Studio hardware-simulator mode]' '--format[Output format]:format:(text json)'
          ;;
        size)
          _arguments '--build-root[Override build root]:path:_files -/' '--board[Override SoM SKU]' '--fail-over-budget[Exit non-zero over budget]' '--format[Output format]:format:(text json)'
          ;;
        *)
          _arguments '--project[Project root]:path:_files -/' '--board-yaml[board.yaml path]:path:_files' '--sdk-root[SDK root]:path:_files -/' '--format[Output format]:format:(text json)' '--help[Show help]'
          ;;
      esac
      ;;
  esac
}

compdef _tan tan
