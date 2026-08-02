# SPDX-License-Identifier: Apache-2.0
import sys

import pytest

from tan.net import default_ssl_context

truststore = pytest.importorskip("truststore")


def test_prefers_truststore_when_importable():
    assert isinstance(default_ssl_context(), truststore.SSLContext)


def test_falls_back_to_certifi_when_truststore_cannot_be_imported(monkeypatch):
    # `sys.modules[name] = None` is the documented CPython way to make
    # `import name` raise ImportError without truststore actually being
    # uninstalled from the test environment.
    monkeypatch.setitem(sys.modules, "truststore", None)
    ctx = default_ssl_context()
    assert type(ctx) is __import__("ssl").SSLContext
    assert not isinstance(ctx, truststore.SSLContext)


def test_falls_back_to_certifi_when_truststore_construction_raises(monkeypatch):
    class _BoomSSLContext:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("platform verifier unavailable")

    monkeypatch.setattr(truststore, "SSLContext", _BoomSSLContext)
    ctx = default_ssl_context()
    assert not isinstance(ctx, _BoomSSLContext)


def test_the_certifi_floor_actually_carries_trust_anchors(monkeypatch):
    """The #304 defect in one assertion: a context with zero loaded CAs passes
    every check that only looks at ITS TYPE. `cert_store_stats()` is what
    `verify_binary.sh`'s frozen-binary proof also reads, kept in sync here."""
    monkeypatch.setitem(sys.modules, "truststore", None)
    stats = default_ssl_context().cert_store_stats()
    assert stats["x509_ca"] > 0, stats
