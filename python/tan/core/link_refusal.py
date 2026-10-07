# SPDX-License-Identifier: Apache-2.0
"""`diagnostics.link: itcm` (tan-cli#1350) -- the pieces every planner consumer
and the flash path share without importing the planner.

The planner raises `LinkTargetError` (`tan/planner/link_target.py`), which
carries a `build.link-itcm-*` issue code. The planner needs a bound SDK root to
import, so a consumer that catches a planner failure recognises the refusal by
that code (duck-typed), never by class: `refusal_code(err)`.
"""

from __future__ import annotations

#: Both refusal codes start with this; see `contract/issue-codes.json`.
CODE_PREFIX = "build.link-itcm-"

#: The `flash_method` the manifest carries for an ITCM-linked slice. NOT a
#: registered backend on purpose: the image is linked at 0x0 and must never be
#: signed or written to MRAM; `tan flash` refuses it and points at
#: `tan flash --ram`.
RAM_RUN_ONLY_METHOD = "ram_run_only"


def refusal_code(err: BaseException) -> str | None:
    """The `build.link-itcm-*` code `err` carries, else None."""
    code = getattr(err, "code", None)
    if isinstance(code, str) and code.startswith(CODE_PREFIX):
        return code
    return None


def split_coded_message(message: str) -> tuple[str | None, str]:
    """Inverse of the `"<code>: <text>"` form a string-returning consumer uses."""
    if message.startswith(CODE_PREFIX):
        code, _, text = message.partition(": ")
        return code, text
    return None, message


RAM_ONLY_PROJECT_CODE = "flash.ram-run-only-project"


def ram_run_only_project_refusal(
    slices: "list[tuple[str, str | None]]",
    core: str | None,
    helper: str | None,
    *,
    ram: bool = False,
) -> str | None:
    """Fail-closed rule for a project marked RAM-only (`diagnostics.link: itcm`).

    `slices` is `(core_id, flash_method)` for every manifest slice. A plain
    `tan flash` (not `--ram`) on a manifest holding any `ram_run_only` slice is
    refused as a WHOLE run -- the project's other slices (e.g. the default
    `m55_hp` stock shim, still planned for an MRAM write) must not be flashed
    in a partial run -- UNLESS the operator explicitly selected only non-ram
    slices (`--core <non-ram core>`) or only a helper (`--helper`). Returns the
    refusal message, else None. `--ram` is never affected.
    """
    if ram:
        return None
    ram_cores = sorted(c for c, m in slices if m == RAM_RUN_ONLY_METHOD)
    if not ram_cores:
        return None
    if helper is not None:
        return None
    if core is not None and core not in ram_cores:
        return None
    first = ram_cores[0]
    return (
        f"flash: this project is marked RAM-only (board.yaml `diagnostics.link: "
        f"itcm`; slice {', '.join(ram_cores)} is `ram_run_only`) -- refusing the "
        f"whole run before any write, so a RAM-only project is never partly "
        f"written to MRAM. RAM-run it with `tan flash --ram --core {first}`; to "
        f"flash only another slice, name it explicitly with `--core <id>`; or "
        f"remove `diagnostics.link` and rebuild."
    )
