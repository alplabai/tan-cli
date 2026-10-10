# SPDX-License-Identifier: Apache-2.0
"""`validate_board_yaml.py`, in-process: the default engine behind `tan validate`.

PORTED from alp-sdk `scripts/validate_board_yaml.py` (tan-cli#270), pinned by
`tests/gates/test_planner_relocation_freshness.py::HAND_PORT_HASHES`.
The function returns what that script's process would have produced -- an exit
status, a stdout and a stderr -- so `tan.commands.validate_cmd` feeds it through
the SAME `analyze_validator_output` it always fed a spawned script's output
through, and the exit-code map, the issue codes and the envelope shape do not
move. `tests/parity/test_board_validator_parity.py` runs both engines over a
fixture corpus and asserts the three values are identical.

Two stages, as in the script:

1. the rich diagnostic validator (`board_validator`), whose findings are
   rendered to stderr; any error ends the run at status 1;
2. the orchestrator's `load_board_yaml` -- here `tan.planner.loader`, already
   in-process for `tan build` -- for the hard cross-field checks and the three
   hw_rev refusals, which keep the script's own exit statuses 3, 4 and 5.

`metadata/**` is read from the bound SDK checkout (ADR-0017); the script's
`--metadata-root` / `--no-presets` / `--no-color` flags are not ported because
`tan validate` never passed them (the SDK-side script keeps serving
west wrappers and MCP).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tan.core.board_diagnostic import render
from tan.core.board_validator import PlannerFacts, validate_board_yaml

#: The script's own exit statuses (`validate_board_yaml.py`), unchanged.
EXIT_SDK_REVISION_UNSUPPORTED = 3
EXIT_SDK_REVISION_UNKNOWN = 4
EXIT_SDK_REVISION_NOT_BUILDABLE = 5


@dataclass(frozen=True)
class ValidatorRun:
    """What the script's process would have left behind."""

    status: int
    stdout: str
    stderr: str


def _traceback_stderr(exc: BaseException) -> str:
    """An uncaught exception, in the one shape `analyze_validator_output`
    recognises as a crash (`Traceback ...` header, `Type: message` last line),
    so a tan-side fault is never read as a verdict about the customer's board."""
    return (
        "Traceback (most recent call last):\n"
        f"{type(exc).__name__}: {exc}\n"
    )


def run_board_validator(board_path: str, sdk_root: Path) -> ValidatorRun:
    """Validate *board_path* against the metadata of the SDK at *sdk_root*."""
    try:
        return _run(board_path, Path(sdk_root))
    except Exception as exc:  # the script's uncaught-exception exit 1
        return ValidatorRun(1, "", _traceback_stderr(exc))


def _run(board_path: str, sdk_root: Path) -> ValidatorRun:
    path = Path(board_path)
    if not path.is_file():
        return ValidatorRun(1, "", f"FAIL {path}: file not found\n")

    # Read first, as the script does: an undecodable board is its uncaught
    # exception, and must not bind the planner to a root on the way.
    source_text = path.read_text(encoding="utf-8")

    # `tan.planner` freezes its metadata paths at import, so the SDK root is
    # bound BEFORE anything below imports it (see `tan.planner_root`).
    from tan.planner_root import bind_sdk_root

    bind_sdk_root(sdk_root)
    from tan.planner.camera_owner import plan_cameras, resolve_cores
    from tan.planner.slugs import _BLOCK_SLUGS

    collector = validate_board_yaml(
        path,
        metadata_root=sdk_root / "metadata",
        facts=PlannerFacts(
            repo_root=sdk_root,
            block_slugs=_BLOCK_SLUGS,
            plan_cameras=plan_cameras,
            resolve_cores=resolve_cores,
        ),
    )
    stderr = "".join(
        render(diag, source_text=source_text, color=False) + "\n"
        for diag in collector
    )
    if collector.has_errors():
        return ValidatorRun(1, "", stderr)

    from tan.planner import (
        OrchestratorError,
        SdkRevisionNotBuildable,
        SdkRevisionUnknown,
        SdkRevisionUnsupported,
        load_board_yaml,
    )

    try:
        load_board_yaml(path)
    except SdkRevisionUnknown as exc:
        return ValidatorRun(
            EXIT_SDK_REVISION_UNKNOWN, "", f"{stderr}FAIL sdk-compat: {exc}\n"
        )
    except SdkRevisionNotBuildable as exc:
        return ValidatorRun(
            EXIT_SDK_REVISION_NOT_BUILDABLE, "", f"{stderr}FAIL sdk-compat: {exc}\n"
        )
    except SdkRevisionUnsupported as exc:
        return ValidatorRun(
            EXIT_SDK_REVISION_UNSUPPORTED, "", f"{stderr}FAIL sdk-compat: {exc}\n"
        )
    except OrchestratorError as exc:
        return ValidatorRun(1, "", f"{stderr}FAIL consistency: {exc}\n")

    warnings = sum(1 for diag in collector if diag.severity == "warning")
    verdict = f"{path}: clean ({warnings} warning(s))" if warnings else f"{path}: clean"
    return ValidatorRun(0, verdict + "\n", stderr)
