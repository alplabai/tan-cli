# SPDX-License-Identifier: Apache-2.0
"""The IO half of Flow A's whole-ATOC guard (tan-cli#1267): the file reads
and the one unlink `tan flash` needs around a `west flash` spawn. Every
decision lives in `tan.core.atoc_guard` and every sentence in
`tan.core.atoc_guard_messages`; this module only fetches their inputs.

What is read, none of it tan's own, all from the build west will flash:

* a sysbuild's top-level `domains.yaml`, for the one domain whose
  `build_dir` the runner actually runs in (and writes its verdict under). A
  multi-domain sysbuild is refused by the runner itself before its guard
  runs (alp-sdk#2274), so tan says so instead of guessing a domain;
* that build's `zephyr/runners.yaml`, for the runner `west flash` falls back
  to when the manifest names none;
* that build's `zephyr_modules.txt`, for the alp-sdk module checkout the
  build used -- the one `west flash` imports `alif_flash` from, which need
  not be the SDK tan is bound to. The bound SDK is the fallback only when
  the build has no readable module list, and every message names the file;
* that module's `scripts/west_commands/runners/alif_flash.py`, for whether
  the runner has the guard (`runner_has_guard`: the `--replace-atoc`
  argument AND the v1 verdict schema);
* the runner's verdict, `<domain build dir>/alif_flash/atoc-guard.json`.

The verdict is removed BEFORE the spawn. The runner does the same itself at
the top of `do_run`, so that "no file" means "the guard never reached a
verdict for this attempt" (alp-sdk docs/aen-provisioning.md section 0.6). But
`do_run` is only reached once west has parsed its arguments and loaded the
runner; a `west flash` that fails before that leaves the LAST run's verdict
in place, and reading it would report an old `clear` (or an old refusal) as
this attempt's result. Removing it first closes that gap without trusting
timestamps.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Callable, Iterable

from tan.core.atoc_guard import (
    ALIF_FLASH_RUNNER,
    RUNNER_SOURCE_PARTS,
    VERDICT_DIR,
    VERDICT_FILE,
    GuardVerdict,
    ReplaceAtocDecision,
    RunnerFacts,
    decide_replace_atoc,
    parse_domain_build_dirs,
    parse_flash_runner,
    parse_module_dir,
    parse_verdict,
    runner_has_guard,
)
from tan.core.atoc_guard_messages import (
    no_verdict_after_success,
    passed_note,
    refusal_message,
    unreadable_note,
)
from tan.core.flash_plan import fa_str, zephyr_west_build_dir

WEST_FLASH = "zephyr_west_flash"


def _read_text(path: str) -> str | None:
    """`None` for a missing, unreadable or non-UTF-8 file -- every input here
    is best-effort, and the pure layer turns `None` into "could not tell"."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def absolutise_build_dir(flash_args: Any, base: str) -> Any:
    """`flash_args` with a relative `build_dir` anchored on `base` -- the cwd
    `west flash` is spawned in (the west topdir, else tan's own cwd). West
    resolves `--build-dir` against ITS cwd, so tan must read the guard files
    under the same directory, and passes the same absolute path on the argv
    so the two can never name different trees. A new mapping; the manifest's
    own is not touched. Anything else is returned unchanged."""
    if not isinstance(flash_args, Mapping):
        return flash_args
    build_dir = fa_str(flash_args, "build_dir")
    if build_dir is None or os.path.isabs(build_dir):
        return flash_args
    return {**flash_args, "build_dir": os.path.join(base, build_dir)}


def _image_dir(build_dir: str) -> tuple[str | None, str | None]:
    """`(the build dir the runner runs in, None)`, or `(None, why not)`."""
    domains_path = os.path.join(build_dir, "domains.yaml")
    text = _read_text(domains_path)
    if text is None:
        return build_dir, None
    dirs = parse_domain_build_dirs(text)
    if dirs is None:
        return None, f"{build_dir} is a sysbuild tree whose {domains_path} could not be read"
    if len(dirs) > 1:
        return None, (
            f"{build_dir} is a sysbuild tree with {len(dirs)} domains ({domains_path}); "
            "the alif_flash runner refuses a multi-domain sysbuild before its guard runs "
            "and leaves no verdict (alp-sdk#2274)"
        )
    only = dirs[0]
    return (only if os.path.isabs(only) else os.path.join(build_dir, only)), None


def _runner_source(image_dir: str, sdk_root: str) -> tuple[str, str]:
    """`(the alif_flash.py to check, where that path came from)`."""
    modules_path = os.path.join(image_dir, "zephyr_modules.txt")
    modules = _read_text(modules_path)
    if modules is None:
        return (
            os.path.join(sdk_root, *RUNNER_SOURCE_PARTS),
            f"the bound alp-sdk; the build has no readable {modules_path}",
        )
    module_dir = parse_module_dir(modules)
    if module_dir is None:
        return modules_path, "that build lists no alp-sdk module, so its runner is not checkable"
    return (
        os.path.join(module_dir, *RUNNER_SOURCE_PARTS),
        f"the alp-sdk module this build used, per {modules_path}",
    )


def runner_facts(flash_args: Any, build_dir: str, sdk_root: str) -> RunnerFacts:
    """Everything `decide_replace_atoc` needs about one west entry."""
    image_dir, why_not = _image_dir(build_dir)
    if image_dir is None:
        return RunnerFacts(unknown_reason=why_not, verdict_dir=build_dir)
    runner = fa_str(flash_args, "runner")
    runners_path = os.path.join(image_dir, "zephyr", "runners.yaml")
    if runner is None:
        text = _read_text(runners_path)
        runner = parse_flash_runner(text) if text is not None else None
    if runner != ALIF_FLASH_RUNNER:
        reason = (
            f"flash_args.runner is unset and {runners_path} is missing or names no "
            "flash-runner. Build first, or set flash_args.runner"
        )
        return RunnerFacts(runner=runner, unknown_reason=reason, verdict_dir=image_dir)
    source, origin = _runner_source(image_dir, sdk_root)
    text = _read_text(source) if source.endswith(".py") else None
    return RunnerFacts(
        runner=runner, source=source, origin=origin,
        has_guard=text is not None and runner_has_guard(text), verdict_dir=image_dir,
    )


def plan_atoc_guard(
    method: str, entry_id: str, flash_args: Any, artefact_path: str, sdk_root: str,
    replace_atoc: bool,
) -> tuple[ReplaceAtocDecision, str]:
    """The guard decision for one entry, plus the dir its verdict lands under
    (`""` for a non-west entry). `flash_args` must already be absolutised."""
    if method != WEST_FLASH:
        return decide_replace_atoc(method, entry_id, RunnerFacts(), replace_atoc), ""
    facts = runner_facts(flash_args, zephyr_west_build_dir(flash_args, artefact_path), sdk_root)
    return decide_replace_atoc(method, entry_id, facts, replace_atoc), facts.verdict_dir


def entries_taking_the_flag(
    candidates: Iterable[tuple[str, str, Any, str]], sdk_root: str
) -> list[str]:
    """The ids of every `(method, entry_id, flash_args, artefact_path)` that
    `--replace-atoc` would actually be appended to."""
    return [
        entry_id
        for method, entry_id, flash_args, artefact_path in candidates
        if plan_atoc_guard(method, entry_id, flash_args, artefact_path, sdk_root, True)[0].append
    ]


def verdict_path(verdict_dir: str) -> str:
    return os.path.join(verdict_dir, VERDICT_DIR, VERDICT_FILE)


def clear_stale_verdict(verdict_dir: str, remove: Callable[[str], None] = os.remove) -> str | None:
    """Remove a previous attempt's verdict before spawning (see the module
    docstring). Returns why it could not be removed, else `None`; the caller
    then distrusts whatever file is there afterwards."""
    path = verdict_path(verdict_dir)
    try:
        remove(path)
    except FileNotFoundError:
        return None
    except OSError as err:
        return f"a verdict left by an earlier run at {path} could not be removed first ({err})"
    return None


def read_verdict(verdict_dir: str) -> GuardVerdict | str:
    """This attempt's verdict, or why there is none to use."""
    path = verdict_path(verdict_dir)
    if not os.path.exists(path):
        return (
            f"no {path} was written for this attempt -- the runner stopped before its "
            "guard ran, or is not one that writes a verdict"
        )
    text = _read_text(path)
    if text is None:
        return f"{path} could not be read"
    result = parse_verdict(text)
    return result if isinstance(result, GuardVerdict) else f"{path}: {result}"


def guarded_failure_message(
    west_message: str, entry_id: str, verdict_dir: str, stale: str | None,
    earlier: Mapping[str, str], *, refused_west_message: str | None = None,
) -> tuple[str, bool]:
    """The message for a failed `west flash` on a guarded runner, and whether
    the guard refused it. `earlier`: ATOC section -> the entry of this run
    that wrote it. `refused_west_message`, when given, stands in for
    `west_message` on a refusal only (tan-cli#1426: west's tail without its
    runner-loading warnings)."""
    verdict = stale if stale is not None else read_verdict(verdict_dir)
    if isinstance(verdict, str):
        return west_message + unreadable_note(verdict), False
    if verdict.refused:
        reported = west_message if refused_west_message is None else refused_west_message
        return f"{refusal_message(entry_id, verdict, earlier)} West reported: {reported}", True
    return f"{west_message} ({passed_note(verdict, failed=True)}; the failure came after it)", False


def guarded_success(
    ok_message: str, entry_id: str, verdict_dir: str, stale: str | None, source: str
) -> tuple[str, str | None, tuple[str, ...]]:
    """`(message, unguarded warning or None, ATOC sections this write put
    there)` for a `west flash` that succeeded on a guarded runner. No usable
    verdict means the guard did not run for this write -- never silence."""
    if stale is not None:
        return ok_message + unreadable_note(stale), None, ()
    verdict = read_verdict(verdict_dir)
    if isinstance(verdict, GuardVerdict) and verdict.refused:
        verdict = f"it says {verdict.status!r}, yet west flash succeeded"
    if isinstance(verdict, str):
        return ok_message, no_verdict_after_success(entry_id, verdict, source), ()
    return f"{ok_message}; {passed_note(verdict)}", None, verdict.allowed
