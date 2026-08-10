# SPDX-License-Identifier: Apache-2.0
"""Which proxy environment variable applies to an `https://` request, whether
`NO_PROXY` exempts the target from it, and whether this interpreter can actually
route through the proxy that wins.

Port of `crates/tan-core/src/proxy.rs`, which is the frozen oracle's rule and
therefore the contract. Pure on purpose, for the same reason the Rust is:
reading the environment is IO and belongs at the call site (`sdk_cmd`), while
the *precedence rule* is a decision, and a decision with a scheme trap in it
deserves tests that never mutate the process environment.

**Why this module exists at all.** The port's one network call site used a bare
`urllib.request.urlopen`, which builds its proxy handler from
`getproxies_environment()`. That maps `ALL_PROXY`/`all_proxy` to the key `all`,
and `ProxyHandler` then installs an `all_open` method that `OpenerDirector._open`
-- which dispatches on `req.type`, i.e. `"https"` -- never calls. Measured
against the live GitHub Releases API: with `HTTPS_PROXY` pointing at a closed
port `tan sdk list --online` failed as it should (rc 1, `sdk.fetch-failed`,
`[Errno 111] Connection refused`); with `ALL_PROXY` pointing at that SAME closed
port and nothing else set, tan connected DIRECTLY and returned rc 0 with 13
releases. So a host whose only egress is `ALL_PROXY` -- the spelling curl, git
and the shipped Rust `tan` all honour -- was hard-broken, and a host with direct
egress silently circumvented the proxy it was told to use (tan-cli#497 defect 8).

**The precedence, MEASURED on the oracle rather than read off `proxy.rs`.** Two
listening sockets, `ALL_PROXY=http://127.0.0.1:45001` and
`HTTPS_PROXY=http://127.0.0.1:45002`, one `target/debug/tan sdk list`: the
connection landed on **45001**. `ALL_PROXY` outranks `HTTPS_PROXY`, and the
uppercase spelling outranks its lowercase alias. `HTTP_PROXY`/`http_proxy` are
deliberately ABSENT: curl, git and urllib all treat those as http-only, and
pushing an `https://` request through a plain-HTTP proxy that may refuse
`CONNECT` would break a corporate host that worked fine going direct.

**`NO_PROXY` is the other half of the same rule**, also measured: with
`ALL_PROXY` at a listening socket and `NO_PROXY=api.github.com`, the oracle went
direct and returned the release list (rc 0), and nothing ever reached the
socket. Honouring the proxy without the bypass list would proxy a host that was
deliberately exempted.

**SOCKS is where this port cannot follow the oracle.** `crates/tan-cli/src/http.rs`
compiles ureq's `socks-proxy` feature, and the oracle really does dial a
`socks5://` `ALL_PROXY` (measured: the listener on port 45012 was hit).
`urllib` has no SOCKS transport at all -- `ProxyHandler` would hand the request
back to `OpenerDirector` under the type `socks5` and produce `unknown url type`.
[`unsupported_proxy_scheme`] names that case so the call site can REFUSE with a
message that says what happened, rather than falling back to a direct
connection: silently bypassing a proxy the host mandated is the failure mode
this whole module exists to remove, and doing it as the *fix* would be worse
than the bug.
"""

from __future__ import annotations

import os
from collections.abc import Callable

#: Proxy env vars that apply to an `https://` URL, in the oracle's own
#: precedence order (`ALL_PROXY` first, each lowercase alias immediately after
#: its uppercase form). `HTTP_PROXY`/`http_proxy` are deliberately absent -- see
#: the module docstring.
HTTPS_PROXY_ENV_VARS = ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy")

#: Bypass-list env vars, uppercase first for the same reason. curl and git read
#: exactly these two.
NO_PROXY_ENV_VARS = ("NO_PROXY", "no_proxy")

#: URL schemes `urllib` can actually carry a proxied `https://` request over.
#: Anything else (`socks5`, `socks5h`, `socks4`, `socks4a`) needs a transport
#: this interpreter does not ship -- see [`unsupported_proxy_scheme`].
SUPPORTED_PROXY_SCHEMES = ("http", "https")


def _first_set(keys: tuple[str, ...], lookup: Callable[[str], str | None]) -> str | None:
    """The first key with a non-blank value, or `None`.

    An empty value counts as UNSET: exporting `HTTPS_PROXY=` is the conventional
    way to disable an inherited proxy, and treating it as a proxy URL would make
    every request fail.
    """
    for key in keys:
        value = lookup(key)
        if value is not None and value.strip():
            return value
    return None


def host_of(url: str) -> str:
    """The host of an absolute URL -- no scheme, userinfo, port, path or
    trailing root dot -- lowercased for comparison.

    Deliberately not a URL parser: the one URL this path ever requests is one we
    wrote ourselves (`sdk_cmd.GITHUB_RELEASES_URL`), so a parser would be more
    moving parts than the two splits it replaces.
    """
    after_scheme = url.split("://", 1)[-1] if "://" in url else url
    authority = after_scheme
    for delimiter in ("/", "?", "#"):
        authority = authority.split(delimiter, 1)[0]
    host = authority.rsplit("@", 1)[-1]
    return host.split(":", 1)[0].rstrip(".").lower()


def no_proxy_covers(list_: str, host: str) -> bool:
    """Does a `NO_PROXY` list cover `host` (already lowercased by [`host_of`])?

    The rules are curl's, git's and Python's, because those are what the user's
    existing `NO_PROXY` was written against:

    * `*` as an entry bypasses the proxy for everything.
    * Comma-separated; surrounding whitespace and empty entries are ignored, so
      `"a, ,b"` is two entries and a trailing comma is harmless.
    * Case-insensitive on both sides -- DNS is.
    * An entry matches the host exactly, or as a suffix **on a label boundary**:
      `github.com` and `.github.com` both cover `api.github.com`, while
      `hub.com` covers neither. A bare `endswith` is the classic bug here -- it
      would let `hub.com` silently disable the proxy for `github.com`.
    * A `:port` on an entry is ignored rather than honoured: every request on
      this path is `https://` on 443, so a port-qualified entry can only mean
      443 anyway.
    """
    for raw in list_.split(","):
        entry = raw.strip()
        if entry == "*":
            return True
        entry = entry.split(":", 1)[0].strip(".").lower()
        if not entry:
            continue
        if host == entry:
            return True
        if host.endswith(entry) and host[: -len(entry)].endswith("."):
            return True
    return False


def select_https_proxy(
    target_url: str, lookup: Callable[[str], str | None] | None = None
) -> str | None:
    """The proxy URL an `https://` request to `target_url` should go through, or
    `None` for direct.

    `lookup` is the injected environment read, defaulting to `os.environ.get`.
    Takes the whole URL rather than a pre-extracted host so that deriving the
    host is covered by this module's tests too.
    """
    read = os.environ.get if lookup is None else lookup
    proxy = _first_set(HTTPS_PROXY_ENV_VARS, read)
    if proxy is None:
        return None
    bypass = _first_set(NO_PROXY_ENV_VARS, read)
    if bypass is not None and no_proxy_covers(bypass, host_of(target_url)):
        return None
    return proxy.strip()


def unsupported_proxy_scheme(proxy_url: str) -> str | None:
    """The scheme of `proxy_url` when `urllib` cannot route through it, else
    `None`.

    A bare `host:port` with no scheme is supported: `urllib._parse_proxy` treats
    it as the request's own scheme, which is exactly the http(s) CONNECT path.
    """
    scheme, separator, _ = proxy_url.partition("://")
    if not separator:
        return None
    scheme = scheme.lower()
    if scheme in SUPPORTED_PROXY_SCHEMES:
        return None
    return scheme
