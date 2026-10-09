# SPDX-License-Identifier: Apache-2.0
"""Is this path trustworthy as an interlock binary? (tan-cli#1452)

The same rule PR #1454 (`flash_raw.py`, tan-cli#1457) applies to the
reservation-enforcing J-Link wrapper: an ABSOLUTE path that resolves (symlinks
followed) to an executable regular file outside the cwd, where BOTH chains --
the path as written and the resolved target, each with its parents -- are owned
by root or the current user and are not writable by others or by a group anyone
else belongs to (a sticky directory is allowed).

TODO(#1461/#1457): once #1454 merges, `flash_raw.py` and this module should
share ONE implementation (and `tan reset` should also require the session
lease nonce through the same helper). The check is copied here, not the lease
code, so this PR does not depend on #1454.
"""
from __future__ import annotations

import os
import pwd
import stat

WRAPPER_ENV = "TAN_JLINK_WRAPPER"


def _current_user() -> str:
    """The account name of the real uid (never `USER`, which the caller controls)."""
    return pwd.getpwuid(os.getuid()).pw_name


def _private_group(gid: int) -> bool:
    """Whether `gid` is a group only the current user belongs to."""
    import grp  # noqa: PLC0415 (POSIX only)

    me = _current_user()
    try:
        members = set(grp.getgrgid(gid).gr_mem)
    except KeyError:
        return False
    return members <= {me} and all(u.pw_name == me for u in pwd.getpwall() if u.pw_gid == gid)


def _node_problem(node: str, st: os.stat_result) -> str | None:
    mode = st.st_mode
    sticky_dir = stat.S_ISDIR(mode) and bool(mode & stat.S_ISVTX)
    if st.st_uid not in (0, os.getuid()):
        return f"{node} is owned by uid {st.st_uid}, not root or you"
    if stat.S_ISLNK(mode):
        return None  # a symlink's own mode bits are always 0777 and mean nothing
    if mode & stat.S_IWOTH and not sticky_dir:
        return f"{node} is world-writable"
    if mode & stat.S_IWGRP and not sticky_dir and not (
        st.st_gid == os.getgid() and _private_group(st.st_gid)
    ):
        return f"{node} is group-writable by a group others belong to"
    return None


def _chain_problem(start: str) -> str | None:
    node = start
    while True:
        try:
            st = os.lstat(node)
        except OSError:
            return f"{node} cannot be inspected"
        problem = _node_problem(node, st)
        if problem:
            return problem
        parent = os.path.dirname(node)
        if parent == node:
            return None
        node = parent


def unsafe(path: str) -> str | None:
    """Why `path` cannot be trusted as an interlock binary, or `None`."""
    if not os.path.isabs(path):
        return "is not an absolute path"
    real = os.path.realpath(path)
    cwd = os.path.realpath(os.getcwd())
    if real == cwd or real.startswith(cwd + os.sep):
        return "lives under the current directory"
    if not (os.path.isfile(real) and os.access(real, os.X_OK)):
        return "is not an executable file"
    return _chain_problem(os.path.abspath(path)) or _chain_problem(real)


def is_configured_wrapper(exe: str | None) -> tuple[bool, str]:
    """`(True, '')` only when `exe` IS the wrapper named by `TAN_JLINK_WRAPPER`
    (absolute, trustworthy, same realpath). Otherwise `(False, why)`: a marker
    string, a PATH name or a look-alike proves nothing."""
    configured = os.environ.get(WRAPPER_ENV, "").strip()
    if not configured:
        return False, f"{WRAPPER_ENV} is not set"
    why = unsafe(configured)
    if why is not None:
        return False, f"{WRAPPER_ENV}={configured} {why}"
    if not exe or not os.path.isabs(exe) or os.path.realpath(exe) != os.path.realpath(configured):
        return False, f"the J-Link program ({exe}) is not the configured wrapper ({configured})"
    return True, ""
