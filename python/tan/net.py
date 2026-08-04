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
    import certifi

    try:
        import truststore

        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # tan-cli#354: the floor has to be UNDER truststore, not an
        # ALTERNATIVE to it. `truststore.SSLContext(...)` constructs perfectly
        # well on a host with an EMPTY OS trust store -- it defers to the
        # platform verifier, and that verifier simply has no anchors. The
        # failure then happens at VERIFY time, inside `urlopen`, which no
        # `except` around construction can ever observe. So on any minimal
        # container (`ubuntu:24.04`, `debian:*-slim`, and most CI base images
        # ship no `ca-certificates`) every HTTPS call died
        # `CERTIFICATE_VERIFY_FAILED` while tan's own `certifi` sat unused
        # inside the freeze. Measured in a pristine `ubuntu:24.04`, and
        # reproduced identically on the published `v0.5.0-rc4` asset -- so it
        # predates the `--onedir` change and had shipped in every RC.
        #
        # Loading certifi into the SAME context WIDENS the anchor set instead
        # of replacing it: a populated OS store -- the corporate-CA case #304
        # chose truststore for -- keeps working, and a host with no OS store
        # can still verify public CAs. That is the "merge, never narrow"
        # intent this module's docstring takes from
        # `crates/tan-cli/src/http.rs`, which the original fall-back shape
        # could not actually express.
        context.load_verify_locations(cafile=certifi.where())
        return context
    except Exception:
        # `ImportError` if truststore is absent; anything else is truststore
        # failing outright (an unsupported OS, a store it cannot open) or
        # refusing the extra anchors. Either way certifi alone is a trust
        # anchor set that does not depend on the platform at all.
        return ssl.create_default_context(cafile=certifi.where())
