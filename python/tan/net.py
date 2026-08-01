# SPDX-License-Identifier: Apache-2.0
"""The one TLS trust context every network call in `tan` uses (tan-cli#304).

A PyInstaller `--onefile` freeze bundles its own Python and its own `ssl`, but
NOT a CA bundle, and `ssl.create_default_context()`'s `set_default_verify_paths`
falls back to whatever cert path OpenSSL was compiled with -- a path on the
BUILD machine, not the one the frozen binary later runs on. The result is a
frozen `tan` with zero trust anchors: every `urllib` HTTPS call fails
`CERTIFICATE_VERIFY_FAILED`, even though the same host's `curl`/browser/system
`python3` all verify the same endpoint fine (#304's single-variable proof:
`SSL_CERT_FILE=/etc/ssl/cert.pem` alone fixed a bare invocation).

Two mechanisms, layered:

* `truststore` (preferred) -- `SSLContext` that verifies through the OS's own
  trust store (Security.framework / CryptoAPI / OpenSSL-with-system-paths), so
  a genuine corporate CA installed in the machine's keychain keeps working.
  `certifi` alone cannot do that: it is a fixed, hermetic public-CA list, so a
  host behind a real TLS-intercepting proxy would trade one failure for
  another under `certifi`-only.
* `certifi`'s bundled `cacert.pem` (the floor) -- used only if `truststore`
  itself is unavailable or fails to construct a context, so a run never has
  zero trust anchors just because the preferred mechanism came up short.
  Hermetic: it verifies public CAs even on a host with no usable OS store.

Mirrors `crates/tan-cli/src/http.rs`'s "merge, never narrow" intent (widen the
webpki roots with the OS store rather than replacing them) with the tool the
Python side actually has: there is no single Python API that merges a bundled
list with the OS store in one context, so this falls back instead of merging.
"""
from __future__ import annotations

import ssl


def default_ssl_context() -> ssl.SSLContext:
    """An `ssl.SSLContext` that actually has trust anchors, in a frozen build
    or not. Pass as `urllib.request.urlopen(..., context=default_ssl_context())`.
    """
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        # ImportError if truststore is somehow absent; anything else is
        # truststore failing to reach the platform verifier (unsupported OS,
        # a broken OS store). Either way, certifi's bundled list is a trust
        # anchor set that does not depend on the platform at all.
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
