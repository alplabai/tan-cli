# SPDX-License-Identifier: Apache-2.0
"""Up-front validation of a `--port` that is a pyserial URL (tan-cli#1448).

`rfc2217://100.64.0.1:` (an empty port number) used to reach pyserial, which
failed deep inside with `'<=' not supported between instances of 'int' and
'NoneType'`, reported as `monitor.capture-open-failed`. This names the problem
instead. A plain device path (no `://`) is not a URL and is never judged here.
"""
from __future__ import annotations

from urllib.parse import urlsplit

#: pyserial's `serial.urlhandler` schemes (3.5). A scheme outside this set is
#: refused rather than handed to pyserial to fail obscurely.
KNOWN_SCHEMES = frozenset({"alt", "cp2110", "hwgrep", "loop", "rfc2217", "socket", "spy"})
#: The schemes that name a TCP endpoint and so need `host:port`.
NETWORK_SCHEMES = frozenset({"rfc2217", "socket"})


def port_url_problem(port: str) -> str | None:
    """What is wrong with `port` as a serial URL, or `None` if it is fine or not a URL."""
    if "://" not in port:
        return None
    scheme = port.split("://", 1)[0].lower()
    if scheme not in KNOWN_SCHEMES:
        return (
            f"unknown serial URL scheme '{scheme}://' in '{port}' "
            f"(known: {', '.join(sorted(s + '://' for s in KNOWN_SCHEMES))})"
        )
    if scheme not in NETWORK_SCHEMES:
        return None
    try:
        parts = urlsplit(port)
        host, number = parts.hostname, parts.port
    except ValueError as err:
        return f"'{port}' is not a valid {scheme}:// URL: {err}"
    if not host:
        return f"'{port}' has no host; use {scheme}://HOST:PORT"
    if number is None:
        netloc = parts.netloc
        why = "the port number is empty" if netloc.endswith(":") else "the port number is missing"
        return f"'{port}': {why}; use {scheme}://{host}:PORT (1-65535)"
    if not 1 <= number <= 65535:
        return f"'{port}': port {number} is outside 1-65535"
    return None
