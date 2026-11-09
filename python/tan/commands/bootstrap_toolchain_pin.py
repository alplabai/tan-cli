# SPDX-License-Identifier: Apache-2.0
"""IO half of tan-cli#1496/#1559: fetch the release `sha256.sum` through the
same proxy decision + OS-trust-store TLS context `tan sdk list --online` uses,
then hand the text to `tan.core.toolchain_pin`. Since tan-cli#1559 this runs
only AFTER an archive failed its pin hash, to explain the refusal -- never to
decide one -- so it is bounded tightly: a refusal that is already decided must
not wait on it."""
from __future__ import annotations

import time

from tan.core.proxy import select_https_proxy

#: Cap on the sum file. The real one is ~100 KiB; anything bigger is not it.
MAX_SUM_BYTES = 4 * 1024 * 1024

#: Whole-fetch deadline. The per-read socket timeout alone lets a slow-drip
#: server hold up an already-decided refusal for as long as it likes.
SUM_FETCH_DEADLINE_S = 30.0
_CHUNK = 64 * 1024

#: Seam, so a test can drive the deadline without patching global `time`.
_now = time.monotonic


def fetch_sum_text(url: str) -> tuple[str | None, str | None]:
    """`(text, None)` or `(None, why)`. Never raises: every transport failure,
    an oversized body, or a fetch past `SUM_FETCH_DEADLINE_S` is a message, and
    the caller's refusal stands undiagnosed."""
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
    deadline = _now() + SUM_FETCH_DEADLINE_S
    chunks: list[bytes] = []
    total = 0
    try:
        with _releases_opener(proxy).open(request, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            while True:
                if _now() > deadline:
                    return None, f"did not finish within {SUM_FETCH_DEADLINE_S:g}s"
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SUM_BYTES:
                    return None, f"response exceeds {MAX_SUM_BYTES} bytes"
                chunks.append(chunk)
    except Exception as err:  # noqa: BLE001 -- see docstring
        return None, f"{type(err).__name__}: {err}"
    return b"".join(chunks).decode("utf-8", errors="replace"), None
