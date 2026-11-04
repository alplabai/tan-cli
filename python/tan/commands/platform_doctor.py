# SPDX-License-Identifier: Apache-2.0
"""The IO half of the platform report: read the workspace pin and the
component versions tan can actually know (tan-cli#1571)."""
from __future__ import annotations

from tan.core.platform_manifest import pin_path_for_sdk


def installed_versions(sdk_root: str) -> dict[str, str | None]:
    from tan.commands.sdk_cmd import check_sdk_readiness  # noqa: PLC0415
    from tan.version import TAN_VERSION  # noqa: PLC0415

    return {"alp-sdk": check_sdk_readiness(sdk_root).get("version"), "tan-cli": TAN_VERSION}


def pin_and_installed(sdk_root: str | None) -> tuple[bytes | None, dict[str, str | None]]:
    """The pin's bytes (`None`: no pin, or no SDK to anchor the lookup on) and
    the installed versions. An unreadable pin comes back as bytes that fail
    validation, so it reports as an invalid pin rather than "no pin"."""
    if sdk_root is None:
        return None, {}
    try:
        pin_bytes: bytes | None = pin_path_for_sdk(sdk_root).read_bytes()
    except FileNotFoundError:
        pin_bytes = None
    except OSError as err:
        pin_bytes = f"unreadable: {err}".encode()
    return pin_bytes, installed_versions(sdk_root)
