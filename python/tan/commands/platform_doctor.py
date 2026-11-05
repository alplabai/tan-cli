# SPDX-License-Identifier: Apache-2.0
"""The IO half of the platform report: read the workspace pin and the
component versions tan can actually know (tan-cli#1571)."""
from __future__ import annotations

from tan.core.platform_manifest import pin_path_for_sdk, platform_report


def installed_versions(sdk_root: str) -> dict[str, str | None]:
    from tan.commands.sdk_cmd import check_sdk_readiness  # noqa: PLC0415
    from tan.version import TAN_VERSION  # noqa: PLC0415

    return {"alp-sdk": check_sdk_readiness(sdk_root).get("version"), "tan-cli": TAN_VERSION}


def pin_report(sdk_root: str | None) -> tuple[dict | None, str, str]:
    """`(data.platform, status, detail)` for the workspace pin (`None` data and
    `pass` when there is none, or no SDK to anchor the lookup on). An
    unreadable pin is a `fail` carrying the real OS error."""
    if sdk_root is None:
        return platform_report(None, {})
    try:
        pin_bytes: bytes | None = pin_path_for_sdk(sdk_root).read_bytes()
    except FileNotFoundError:
        pin_bytes = None
    except OSError as err:
        return None, "fail", f"the workspace platform pin cannot be read: {err}"
    return platform_report(pin_bytes, installed_versions(sdk_root))
