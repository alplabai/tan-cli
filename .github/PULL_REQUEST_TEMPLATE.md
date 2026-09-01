<!--
SPDX-License-Identifier: Apache-2.0

Replace each placeholder below and delete the guidance comments before
submitting. Sections that genuinely do not apply get "N/A" and one line
saying why -- an empty section reads as "not considered", not as "not
applicable".
-->

## Summary

<!-- One paragraph: what this changes and why. If it fixes a defect, say
     what the defect actually did, not just that it existed. -->

## Issue

<!-- `Closes #N.` when this fully resolves it; `Refs #N.` when related.

     GitHub honours the keyword PER ISSUE: `Closes #1, #2, #3` closes ONLY
     #1. Write one sentence each -- `Closes #1. Closes #2. Closes #3.`

     `dev` is the default branch, so `Closes` auto-closes on a normal
     dev-targeted PR. A PR targeting `main` does NOT auto-close; close
     those by hand. -->

## Base branch

<!-- `dev` for essentially everything; `main` only receives `dev` through a
     release PR. Before opening, confirm the diff is only your change:

       git fetch origin dev
       git log --oneline origin/dev..HEAD
       git diff origin/dev...HEAD --stat

     Unexpected commits or files mean the branch was cut from somewhere
     else -- target that branch, not `main`. -->

## Scope

- [ ] Command surface (`python/tan/commands/**`)
- [ ] Pure logic (`python/tan/core/**`, `python/tan/planner/**`)
- [ ] Envelope contract (`contract/**`, issue codes) — consumed by alp-sdk-vscode
- [ ] Planner port from alp-sdk (`python/tan/planner/**` mirroring `scripts/**`)
- [ ] Templates / scaffold (`python/tan/templates/**`)
- [ ] CI, gates, workflows (`.github/**`, `python/tests/gates/**`)
- [ ] Docs (`README.md`, `docs/**`, comments)

## Envelope impact

<!-- The `{command, ok, exitCode, project, data, issues}` contract is what
     alp-sdk-vscode consumes. A changed field, a new issue code, or a moved
     exit code is a contract change even when no test fails here. -->

- [ ] No envelope change.
- [ ] Additive (new field / new issue code) — registered in `contract/issue-codes.json`.
- [ ] Changed or removed field, or a moved exit code — name the extension code that reads it.

## Test plan

<!-- Behaviour is established by RUNNING the CLI, never by reading source.
     Paste the real invocation and its real output for anything you claim.
     `crates/` was deleted in #269 -- there is no Rust oracle to diff
     against any more; `python/tan/` IS the shipped artifact. -->

- [ ] `python -m pytest tests -q` from `python/` — **zero failures is the bar, not a count.**
      Pinning a number turns every landed port into a red build.
      Paste the real summary line:

      ```
      <paste: N passed, N skipped, ... in Ns>
      ```

- [ ] Parity suites exercised, or explicitly not needed.
      Without `ALP_SDK_ROOT` these SKIP loudly — that is correct, not a pass.
      Bind it at the pinned commit (or a matching ref), never an arbitrary
      working branch: a mismatched checkout turns skips into hundreds of
      failures.

- [ ] New behaviour has a test that FAILS without the change.
      Mutation-prove it: revert the fix, watch the new test fail, restore.
      A test that passes either way pins nothing.

<!-- Environment note, in case you hit it: several command suites build their
     own `env={**os.environ, "PYTHONPATH": ..., "HOME": tmp_path, ...}`, which
     hides user-site packages from the spawned child and yields spurious
     `ModuleNotFoundError` failures. Run the suite from a clean venv
     (`pip install -e ./python pytest`) rather than working around it. -->

## Gates this touches

<!-- Tick what your diff makes relevant and confirm each is green LOCALLY.
     The five required contexts on dev and main are:
       seam1 -- plan-shape parity
       python -- pytest across python/ (ubuntu-latest / windows-latest / macos-latest)
       zizmor · workflow security -->

- [ ] `.github/workflows/**` changed → `zizmor` clean, and every new job carries `timeout-minutes`.
- [ ] A module grew → `python/tests/gates/test_module_size_budget.py`. Regenerate with
      `python/scripts/regen_module_size_budget.py --reason ...`; never hand-edit a record under
      `python/tests/gates/module_size_budget.d/` or the log.
      A ratchet bump needs a reason in the PR, not just in the file.
- [ ] Planner port → `test_planner_relocation_freshness.py`. Do NOT move
      `PINNED_SDK_COMMIT` / `HAND_PORT_PINNED_SDK_COMMIT` without a measured diff:
      moving a pin re-freezes every OTHER un-ported source in the same range.
      Porting one source ahead of the pin is fine — record it with a `#:` note.
- [ ] New issue code → registered, no duplicates, frozen-set gate green.
- [ ] Host paths / conflict markers → `test_no_leaked_host_paths.py`,
      `test_no_conflict_markers.py`.

## Changelog

- [ ] `changelog.d/<issue>.<category>.md` added — category is one of
      `added` / `changed` / `fixed` / `removed`.
      Never edit `CHANGELOG.md` directly; every open PR conflicts on it by
      construction (see `changelog.d/README.md`).
      The file contains the bullet exactly as it should ship — the assembler
      does not reformat, rewrap or summarise.

## Checklist

- [ ] Verbatim technical strings kept exact — issue codes, exit codes, flags,
      paths, SKUs, register and pin names. A rounded number or a dropped digit
      is a defect, not a style choice.
- [ ] No local host paths (`/home/...`, `C:\Users\...`) in any committed file.
- [ ] No AI attribution — no `Co-Authored-By: Claude`, no `claude.ai` session
      URLs, no "generated with" banners. This repo is PUBLIC and a PR body
      becomes permanent public history. Attribute to the human author.
- [ ] Comments and docstrings still describe what the code does. If this PR
      falsified one, fix it in the same commit — including a docstring that
      states WHY something happened, which is the part that rots silently.
