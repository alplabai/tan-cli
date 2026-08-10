# SPDX-License-Identifier: Apache-2.0
"""`tan.core.proxy` -- which proxy an `https://` request goes through.

Port of `crates/tan-core/src/proxy.rs`'s own test module, plus the two cases
this port has that the Rust does not (`unsupported_proxy_scheme`, because
urllib carries no SOCKS transport where ureq does).

Every precedence claim below was MEASURED on the frozen oracle before it was
written down, not read off `proxy.rs`: two listening sockets, one
`target/debug/tan sdk list`, and the connection observed landing on one of
them. See the module docstring of `tan/core/proxy.py` for the numbers.

The lookup is INJECTED in every case, so nothing here mutates the process
environment -- these are the rules, not the environment read.
"""
from __future__ import annotations

import pytest

from tan.core.proxy import (
    host_of,
    no_proxy_covers,
    select_https_proxy,
    unsupported_proxy_scheme,
)

#: The one URL this repo's HTTP path ever requests, so every case is stated
#: against the host a real user's `NO_PROXY` would be judged on.
TARGET = "https://api.github.com/repos/alplabai/alp-sdk/releases"


def env(pairs):
    """A lookup over a fixed table -- each case states exactly the environment
    it means, and nothing leaks between tests."""
    return lambda key: dict(pairs).get(key)


def test_all_proxy_outranks_https_proxy():
    """MEASURED on the oracle: with `ALL_PROXY` on port 45001 and `HTTPS_PROXY`
    on 45002, the oracle's connection landed on 45001. This is also the whole
    reason tan-cli#497 defect 8 existed -- `urllib.getproxies_environment()`
    maps `ALL_PROXY` to a key (`all`) that its https dispatch never reads, so
    the winner was silently no proxy at all."""
    chosen = select_https_proxy(
        TARGET,
        env([("HTTPS_PROXY", "http://corp:8080"), ("ALL_PROXY", "socks5://127.0.0.1:1080")]),
    )
    assert chosen == "socks5://127.0.0.1:1080"


def test_uppercase_outranks_its_lowercase_alias():
    chosen = select_https_proxy(
        TARGET,
        env([("https_proxy", "http://lower:8080"), ("HTTPS_PROXY", "http://upper:8080")]),
    )
    assert chosen == "http://upper:8080"


def test_http_proxy_alone_leaves_an_https_request_direct():
    """The regression the Rust module exists to prevent, carried over verbatim:
    a plain-HTTP-only proxy that refuses CONNECT would break a corporate host
    that worked fine going direct. curl, git and urllib all agree."""
    assert (
        select_https_proxy(
            TARGET,
            env([("HTTP_PROXY", "http://corp:8080"), ("http_proxy", "http://corp:8080")]),
        )
        is None
    )


def test_an_empty_value_means_unset_and_falls_through():
    """`HTTPS_PROXY=` is how a shell disables an inherited proxy: it must not
    shadow a real `ALL_PROXY`, and on its own it is not a proxy URL."""
    assert select_https_proxy(TARGET, env([("HTTPS_PROXY", "   ")])) is None
    assert (
        select_https_proxy(TARGET, env([("ALL_PROXY", ""), ("HTTPS_PROXY", "http://corp:8080")]))
        == "http://corp:8080"
    )


@pytest.mark.parametrize(
    "bypass",
    ["api.github.com", ".github.com", "github.com", "*", "a, ,api.github.com,", "API.GitHub.COM",
     "api.github.com:443"],
)
def test_no_proxy_entries_that_cover_the_target(bypass):
    """MEASURED on the oracle: `ALL_PROXY` at a listening socket plus
    `NO_PROXY=api.github.com` went DIRECT (rc 0, the release list) and the
    socket was never touched."""
    assert select_https_proxy(TARGET, env([("HTTPS_PROXY", "http://corp:8080"),
                                           ("NO_PROXY", bypass)])) is None


@pytest.mark.parametrize("bypass", ["hub.com", "notgithub.com", "", " , ", "githubXcom"])
def test_no_proxy_entries_that_do_not_cover_the_target(bypass):
    """A bare `endswith` is the classic bug here: `hub.com` must NOT disable the
    proxy for `api.github.com`. Matching is on a label boundary."""
    assert select_https_proxy(TARGET, env([("HTTPS_PROXY", "http://corp:8080"),
                                           ("NO_PROXY", bypass)])) == "http://corp:8080"


def test_no_proxy_matches_on_a_label_boundary_not_a_string_suffix():
    assert no_proxy_covers("github.com", "api.github.com") is True
    assert no_proxy_covers("hub.com", "api.github.com") is False
    assert no_proxy_covers("hub.com", "github.com") is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.github.com/repos/x", "api.github.com"),
        ("https://API.GitHub.com./repos/x", "api.github.com"),
        ("https://user:pw@api.github.com:443/x?q=1#f", "api.github.com"),
        ("api.github.com/x", "api.github.com"),
    ],
)
def test_host_of_strips_everything_that_is_not_the_host(url, expected):
    assert host_of(url) == expected


@pytest.mark.parametrize("proxy", ["socks5://127.0.0.1:1080", "SOCKS5H://h:1", "socks4://h:1"])
def test_a_socks_proxy_is_reported_unsupported(proxy):
    """The one place this port CANNOT follow the oracle. `crates/tan-cli`
    compiles ureq's `socks-proxy` feature and the oracle really does dial a
    `socks5://` `ALL_PROXY` (measured: the listener was hit); urllib has no
    SOCKS transport at all. The caller must refuse rather than fall back to a
    direct connection -- silently bypassing a mandated proxy is the defect,
    not the fix."""
    assert unsupported_proxy_scheme(proxy) == proxy.split("://")[0].lower()


@pytest.mark.parametrize("proxy", ["http://h:1", "https://h:1", "HTTP://h:1", "127.0.0.1:8080"])
def test_a_routable_proxy_is_not_reported_unsupported(proxy):
    """A bare `host:port` with no scheme is supported: urllib's `_parse_proxy`
    treats it as the request's own scheme, which is the http(s) CONNECT path."""
    assert unsupported_proxy_scheme(proxy) is None
