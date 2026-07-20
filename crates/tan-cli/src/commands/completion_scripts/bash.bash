# tan CLI bash completion
_tan_complete() {
  local cur prev words cword

  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  cword=${COMP_CWORD}

  local commands="validate generate init scaffold examples doctor completion diff presets pinmux explain inspect trace debug-config support-bundle sdk bootstrap build image flash run clean renode size migrate lock quality model monitor new-som faultdecode"
  local global_flags="--project --board-yaml --sdk-root --format --verbose --quiet --no-color --non-interactive --ci --help"

  if [[ "$prev" == "--format" ]]; then
    COMPREPLY=( $(compgen -W "text json" -- "$cur") )
    return
  fi

  if [[ "$prev" == "--shell" ]]; then
    COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") )
    return
  fi

  if [[ $cword -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
    return
  fi

  case "${COMP_WORDS[1]}" in
    generate)
      COMPREPLY=( $(compgen -W "$global_flags --target --all --force" -- "$cur") )
      ;;
    explain)
      COMPREPLY=( $(compgen -W "$global_flags --template" -- "$cur") )
      ;;
    init)
      COMPREPLY=( $(compgen -W "$global_flags --template --from-example --name --destination --som --cores --preview --force" -- "$cur") )
      ;;
    scaffold)
      COMPREPLY=( $(compgen -W "$global_flags --template --name --destination --preview --force" -- "$cur") )
      ;;
    diff|presets|examples)
      COMPREPLY=( $(compgen -W "$global_flags" -- "$cur") )
      ;;
    completion)
      COMPREPLY=( $(compgen -W "--shell --format --help" -- "$cur") )
      ;;
    pinmux)
      COMPREPLY=( $(compgen -W "$global_flags --sku --family" -- "$cur") )
      ;;
    doctor)
      COMPREPLY=( $(compgen -W "$global_flags --target-kind --server --build --fix" -- "$cur") )
      ;;
    inspect)
      COMPREPLY=( $(compgen -W "$global_flags --path --show-origin" -- "$cur") )
      ;;
    trace)
      COMPREPLY=( $(compgen -W "$global_flags --path" -- "$cur") )
      ;;
    debug-config)
      COMPREPLY=( $(compgen -W "$global_flags --target-kind --server --preview" -- "$cur") )
      ;;
    support-bundle)
      COMPREPLY=( $(compgen -W "$global_flags --destination --target-kind --server --path" -- "$cur") )
      ;;
    sdk)
      COMPREPLY=( $(compgen -W "list install current switch --destination" -- "$cur") )
      ;;
    bootstrap)
      COMPREPLY=( $(compgen -W "$global_flags --no-pip --no-west --print-env" -- "$cur") )
      ;;
    build)
      COMPREPLY=( $(compgen -W "$global_flags --plan --plan-from --materialise --native --manifest --manifest-from" -- "$cur") )
      ;;
    image)
      COMPREPLY=( $(compgen -W "$global_flags --build-root" -- "$cur") )
      ;;
    flash)
      COMPREPLY=( $(compgen -W "$global_flags --build-root --dry-run --core --helper --skip-missing-tools" -- "$cur") )
      ;;
    run)
      COMPREPLY=( $(compgen -W "$global_flags --flash --core" -- "$cur") )
      ;;
    clean)
      COMPREPLY=( $(compgen -W "$global_flags --build-root --dry-run" -- "$cur") )
      ;;
    renode)
      COMPREPLY=( $(compgen -W "$global_flags --build-root --board --image-bundle --log --timeout --expect --sim-mode" -- "$cur") )
      ;;
    size)
      COMPREPLY=( $(compgen -W "$global_flags --build-root --board --fail-over-budget" -- "$cur") )
      ;;
    *)
      COMPREPLY=( $(compgen -W "$global_flags" -- "$cur") )
      ;;
  esac
}

complete -F _tan_complete tan
