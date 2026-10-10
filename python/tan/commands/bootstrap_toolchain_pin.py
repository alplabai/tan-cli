# SPDX-License-Identifier: Apache-2.0
"""IO half of tan-cli#1496: fetch the release `sha256.sum` through the same
proxy decision + OS-trust-store TLS context `tan sdk list --online` uses, then
hand the text to `tan.core.toolchain_pin`. Only this small file is downloaded."""
from __future__ import annotations

from tan.core.proxy import select_https_proxy

#: Cap on the sum file. The real one is ~100 KiB; anything bigger is not it.
MAX_SUM_BYTES = 4 * 1024 * 1024


def fetch_sum_text(url: str) -> tuple[str | None, str | None]:
    """`(text, None)` or `(None, why)`. Never raises: every transport failure is a
    message, so the caller always refuses visibly instead of passing silently."""
    from tan.commands.sdk_cmd import (  # noqa: PLC0415
        NETWORK_TIMEOUT_SECONDS,
        _releases_opener,
        _unroutable_proxy_refusal,
    )

    proxy = select_https_proxy(url)
    refusal = _unroutable_proxy_refusal(proxy, url)
    if refusal is not None:
        return None, refusal
    import urllib.request  # noqa: PLC0415

    request = urllib.request.Request(url, headers={"User-Agent": "tan-cli/0"})  # noqa: S310
    try:
        with _releases_opener(proxy).open(request, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_SUM_BYTES + 1)
    except Exception as err:  # noqa: BLE001 -- see docstring
        return None, f"{type(err).__name__}: {err}"
    if len(raw) > MAX_SUM_BYTES:
        return None, f"response exceeds {MAX_SUM_BYTES} bytes"
    return raw.decode("utf-8", errors="replace"), None
