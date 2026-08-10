# Frozen Rust-oracle captures — HISTORY, not assertions about what ships

**Nothing in this directory describes what `tan` does today.** Every `.json`
here was recorded from an actual run of the **Rust oracle, `tan 0.4.1`**
(`crates/` at `204c3a0b8dcb165e10ac3d14f3e9bfa699e45b59`), and `crates/` was
deleted in tan-cli#269 along with the ~13 parity modules that replayed these
files and the workflow that re-captured them. A capture whose bytes disagree
with the shipping CLI is **correct and expected**: it is the old answer, which
is the entire reason a capture exists.

Read `PROVENANCE.txt` for how each answer was obtained, and
`PARITY-COVERAGE.txt` for what the retired parity suite did and did not cover.
Both are closed records. **Do not hand-edit a recorded value** — nothing in
this repository can produce a new one, so a doctored byte is indistinguishable
from a real one and unrecoverable. That failure mode has already happened once
(tan-cli#511) and `tests/gates/test_oracle_fixture_capture_platform_convention.py`
exists because of it.

## Which files are still read by code, and which are history only

Measured by walking `python/tests/**/*.py` for uses of
`tests.oracle_captures.CAPTURES_DIR`;
`tests/gates/test_oracle_capture_store_is_labelled.py` re-measures it on every
run and fails if this table drifts from the tree.

| File | Live reader |
| --- | --- |
| `test_flash_oracle_parity.json` | `tests/gates/test_oracle_fixture_capture_platform_convention.py` — the separator-convention gate |
| `test_run_oracle_parity.json` | `tests/commands/test_output_format.py` — the frozen `--help` `--format` choice list |
| `test_build_sdk_root_oracle_parity.json` | none — history only |
| `test_clean_parity.json` | none — history only |
| `test_command_surface_oracle_parity.json` | none — history only (**superseded**, see below) |
| `test_image_size_oracle.json` | none — history only |
| `test_oracle_parity.json` | none — history only |
| `platform_bound.json` | none — history only |
| `scaffold_trees.json` | none — history only |
| `PARITY-COVERAGE.txt` | none — closed record |
| `PROVENANCE.txt` | none — closed record |

"History only" is not "dead weight". `docs/ROADMAP.md`,
`docs/ux-polish-sweep-plan.md` ("Where a behaviour question needs an oracle
answer, cite `python/tests/fixtures/oracle_captures/` — do not assert one from
memory"), `README.md`, and source comments in `tan/core/flash_plan.py`,
`tan/core/module_template.py`, `tan/core/scaffold.py` and `tan/output_format.py`
all cite this store as the authoritative record of what the oracle did. It is
the only such record left, and it cannot be regenerated.

## Known supersessions — captures the shipping CLI has since moved past

Recorded here so a reader who greps this store for a behaviour lands on the
explanation instead of on a byte-for-byte copy of the opposite of what ships.
`tests/gates/test_oracle_capture_store_is_labelled.py` holds each row to being
simultaneously still true of the capture and still false of the shipped source
— so neither a laundered capture nor a stale claim survives.

### `test_command_surface_oracle_parity.json` — completion scripts, superseded by tan-cli#614

The `test_completion_*` keys freeze the oracle's `tan completion <shell>`
output. tan-cli#614 rewrote that script to find the subcommand past a leading
global flag and to stop offering a root-only `--version`. These substrings are
in the capture and are **no longer** in `tan/commands/completion_cmd.py`:

- `case "${COMP_WORDS[1]}"` — the bash dispatch that assumed word 1 is the verb
- `--ci --help --version` — the root option list
- `complete -c tan -l version` — the fish root `--version` completion

tan-cli#617 also lists `cword -eq 1` as superseded. **Measured, it is not**:
that substring is still present in the shipped `completion_cmd.py`, so it is
not a divergence and is deliberately absent from the gate's table.
