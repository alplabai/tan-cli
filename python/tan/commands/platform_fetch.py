# SPDX-License-Identifier: Apache-2.0
"""The network half of `tan platform use` (tan-cli#1571): fetch a platform
manifest release asset, or list `alplabai/alp-platform`'s releases to resolve
a bare `YY.M` line. Reuses `tan platform`'s sibling `sdk list` plumbing (proxy
selection, TLS context, error wording) rather than adding an HTTP helper.

Both functions return `(value, error_message)`; they never raise. A
`GITHUB_TOKEN` in the environment is sent as a bearer token (rate limits,
private mirrors)."""
from __future__ import annotations

import json
import os

from tan.core.platform_manifest import REPO

RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=100"


def _get(url: str, accept: str) -> tuple[bytes | None, str | None]:
    import urllib.request  # noqa: PLC0415

    from tan.commands.sdk_cmd import (  # noqa: PLC0415
        NETWORK_TIMEOUT_SECONDS,
        _releases_opener,
        _unroutable_proxy_refusal,
        describe_network_error,
    )
    from tan.core.proxy import select_https_proxy  # noqa: PLC0415

    headers = {"User-Agent": "tan-cli/0", "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 -- constant https://
    proxy = select_https_proxy(url)
    refusal = _unroutable_proxy_refusal(proxy, url)
    if refusal is not None:
        return None, refusal
    try:
        with _releases_opener(proxy).open(request, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            return resp.read(), None
    except Exception as err:  # noqa: BLE001 -- every transport failure becomes a message
        return None, describe_network_error(str(err) or type(err).__name__, proxy_used=proxy is not None)


def fetch_manifest(url: str) -> tuple[bytes | None, str | None]:
    return _get(url, "application/octet-stream")


def list_release_tags() -> tuple[list[str] | None, str | None]:
    body, err = _get(RELEASES_URL, "application/vnd.github+json")
    if body is None:
        return None, err
    try:
        payload = json.loads(body)
        return [str(r["tag_name"]) for r in payload if isinstance(r, dict) and "tag_name" in r], None
    except (ValueError, TypeError) as exc:
        return None, f"could not parse the {REPO} releases response: {exc}"
