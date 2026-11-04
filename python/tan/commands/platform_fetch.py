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
from urllib.parse import urlparse

from tan.core.platform_manifest import MAX_MANIFEST_BYTES, REPO

RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=100"


#: Hosts that may receive `GITHUB_TOKEN`. The release asset URL redirects to
#: `*.githubusercontent.com`, which must never see it.
_TOKEN_HOSTS = frozenset({"api.github.com", "github.com"})
#: Ceiling for the releases-API listing (a manifest has `MAX_MANIFEST_BYTES`).
MAX_LISTING_BYTES = 4 * 1024 * 1024


def _get(url: str, accept: str, limit: int) -> tuple[bytes | None, str | None]:
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
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 -- constant https://
    if token and urlparse(url).hostname in _TOKEN_HOSTS:
        # Unredirected: urllib copies `headers` onto a redirect's new request
        # but not these, so the token reaches only the host named in `url`.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    proxy = select_https_proxy(url)
    refusal = _unroutable_proxy_refusal(proxy, url)
    if refusal is not None:
        return None, refusal
    try:
        with _releases_opener(proxy).open(request, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            body = resp.read(limit + 1)
        if len(body) > limit:
            return None, f"the response from {urlparse(url).hostname} is larger than {limit} bytes."
        return body, None
    except Exception as err:  # noqa: BLE001 -- every transport failure becomes a message
        message = describe_network_error(str(err) or type(err).__name__, proxy_used=proxy is not None)
        if getattr(err, "code", None) in (403, 404):
            message += (
                f" If {REPO} is private, a GITHUB_TOKEN with read access to it is needed "
                "(export GITHUB_TOKEN=<token>)."
            )
        return None, message


def fetch_manifest(url: str) -> tuple[bytes | None, str | None]:
    return _get(url, "application/octet-stream", MAX_MANIFEST_BYTES)


def list_releases() -> tuple[list[dict] | None, str | None]:
    """Every release as `{tag_name, prerelease, published_at}` dicts."""
    body, err = _get(RELEASES_URL, "application/vnd.github+json", MAX_LISTING_BYTES)
    if body is None:
        return None, err
    try:
        payload = json.loads(body)
        return [r for r in payload if isinstance(r, dict) and "tag_name" in r], None
    except (ValueError, TypeError) as exc:
        return None, f"could not parse the {REPO} releases response: {exc}"


def list_release_tags() -> tuple[list[str] | None, str | None]:
    releases, err = list_releases()
    if releases is None:
        return None, err
    return [str(r["tag_name"]) for r in releases], None
