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


def main() -> None:
    """Run the real CLI, and turn a closed stdout into a quiet exit.

    `tan generate --help | grep -q x` (or `| head`, any reader that stops
    early) closes tan's stdout mid-write. On POSIX that raises
    `BrokenPipeError` (`errno.EPIPE`); on Windows the same condition surfaces
    as a plain `OSError` with `errno.EINVAL` instead -- CPython's Windows
    layer maps the underlying `ERROR_NO_DATA` to EINVAL, not EPIPE, so
    `except BrokenPipeError` alone (the POSIX-only idiom from the `signal`
    docs' "Note on SIGPIPE") does not catch it there. Left unhandled, Typer's
    installed pretty-exception hook renders the whole stack as a boxed
    traceback and the process exits non-zero -- confirmed against the Rust
    oracle (`tan.exe generate --help | head -n1`), which exits 0 with empty
    stderr in the same situation; this matches that.

    EINVAL alone is not pipe-specific on Windows -- a plain filesystem error
    (e.g. `open("bad<n.txt")`) raises the same errno. That OSError carries a
    `filename`; the broken-pipe OSError does not (verified against CPython's
    Windows console-write path), so `filename is None` discriminates a closed
    stdout from a real filesystem error without weakening the pipe guard.

    Redirecting stdout to the null device before returning matters too:
    without it, the interpreter's own shutdown-time flush of the now-broken
    stdout raises the same OSError again, straight past any exception
    handler, and prints "Exception ignored in: ..." on stderr regardless.
    """
    try:
        _cli_main()
    except OSError as exc:
        is_broken_pipe = exc.errno == errno.EPIPE or (
            sys.platform == "win32"
            and exc.errno == errno.EINVAL
            and exc.filename is None
        )
        if not is_broken_pipe:
            raise
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)


if __name__ == "__main__":
    main()
