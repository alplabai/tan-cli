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
