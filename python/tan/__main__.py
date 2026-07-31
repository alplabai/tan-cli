# SPDX-License-Identifier: Apache-2.0
"""The `tan` entrypoint. The command surface itself lives in `tan.cli`.

This is the PROCESS BOUNDARY -- the one place both real entrypoints resolve
to: `pyproject.toml`'s `[project.scripts]` names `tan.__main__:main` (so a
`pip install`ed console-script calls this module's `main` attribute directly,
never running the `if __name__ == "__main__"` guard below), and
`scripts/build_binary.sh` points PyInstaller at this same file. A broken-pipe
guard sprinkled into each command instead would miss both.
"""
import errno
import os
import sys

from tan.cli import main as _cli_main


def _is_broken_pipe(exc: BaseException) -> bool:
    """Is this the OSError a reader closing tan's stdout raises?

    EPIPE on POSIX. On Windows the same condition surfaces as a plain
    `OSError` with `errno.EINVAL` instead -- CPython's Windows layer maps the
    underlying `ERROR_NO_DATA` to EINVAL, not EPIPE, so `except
    BrokenPipeError` alone (the POSIX-only idiom from the `signal` docs' "Note
    on SIGPIPE") does not catch it there.

    EINVAL alone is not pipe-specific on Windows -- a plain filesystem error
    (e.g. `open("bad<n.txt")`) raises the same errno. That OSError carries a
    `filename`; the broken-pipe OSError does not (verified against CPython's
    Windows console-write path), so `filename is None` discriminates a closed
    stdout from a real filesystem error without weakening the pipe guard.
    """
    return isinstance(exc, OSError) and (
        exc.errno == errno.EPIPE
        or (sys.platform == "win32" and exc.errno == errno.EINVAL and exc.filename is None)
    )


def _caused_by_broken_pipe(exc: BaseException) -> bool:
    """Was this `SystemExit` raised WHILE a closed stdout was being handled?

    Nothing reaches this module's `except OSError` arm on POSIX, because two
    layers below it already convert the `BrokenPipeError` into an exit of
    their own and neither is reachable through a public hook:

    * Rich -- `Console._check_buffer` catches `BrokenPipeError` and calls
      `Console.on_broken_pipe`, which sets `quiet` and raises `SystemExit(1)`.
      This is the one that fires for `tan --help`, since Typer renders every
      `--help` through Rich.
    * Typer/Click -- `typer.core._main` (Click's `BaseCommand.main` shape)
      has `except OSError: if e.errno == errno.EPIPE: ... sys.exit(1)`.

    Both raise their exit from INSIDE the `except BrokenPipeError`/`except
    OSError` block that caught the pipe, so CPython's implicit chaining leaves
    the original error on `SystemExit.__context__`. Following that chain is
    what identifies the case: `SystemExit(1)` on its own is far too broad to
    remap -- every failed `tan build` raises it -- and the alternatives are
    all private detail of somebody else's library (Click's
    `PacifyFlushWrapper` swap, Rich's `console.quiet`), which an entrypoint
    should not be importing.
    """
    seen: set[int] = set()
    cause: BaseException | None = exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen:
        if _is_broken_pipe(cause):
            return True
        seen.add(id(cause))
        cause = cause.__cause__ or cause.__context__
    return False


def _exit_quietly() -> None:
    """Exit 0 with a stdout that can no longer raise.

    Redirecting stdout to the null device before returning matters: without
    it, the interpreter's own shutdown-time flush of the now-broken stdout
    raises the same OSError again, straight past any exception handler, and
    prints "Exception ignored in: ..." on stderr regardless.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except OSError:
        pass
    sys.exit(0)


def main() -> None:
    """Run the real CLI, and turn a closed stdout into a quiet exit.

    `tan generate --help | grep -q x` (or `| head`, any reader that stops
    early) closes tan's stdout mid-write. The Rust oracle exits 0 with empty
    stderr in that situation on BOTH hosts -- measured: `tan.exe generate
    --help | head -n1` on Windows, and tan 0.4.1 linux-musl writing a
    4327-byte `--help` into a never-read 4 KiB pipe, which is a guaranteed
    EPIPE. This pins the port to the same observable behaviour, and it takes
    two arms because the pipe reaches this frame in two different shapes:

    * POSIX -- as a `SystemExit(1)` some library layer already raised for it
      (see `_caused_by_broken_pipe`). Left alone, `tan --help | head -n1`
      exited 1 here where the oracle exits 0.
    * Windows -- as the raw `OSError`, because the errno is EINVAL and none of
      those layers recognises it (see `_is_broken_pipe`). Left unhandled,
      Typer's installed pretty-exception hook renders the whole stack as a
      boxed traceback and the process exits non-zero.
    """
    try:
        _cli_main()
    except SystemExit as exc:
        if not _caused_by_broken_pipe(exc):
            raise
        _exit_quietly()
    except OSError as exc:
        if not _is_broken_pipe(exc):
            raise
        _exit_quietly()


if __name__ == "__main__":
    main()
