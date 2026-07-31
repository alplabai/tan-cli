# SPDX-License-Identifier: Apache-2.0
"""`tan.__main__.main` is the process boundary (see its own docstring for why
it, not `tan.cli`, owns this): a closed stdout must exit quietly, not crash.
"""
import os
import subprocess
import sys
import threading
from pathlib import Path

#: `python/` -- `python -m tan` resolves the package off `os.getcwd()`, not the
#: test file's own location, so a scratch cwd needs this pinned onto the
#: child's `PYTHONPATH` (mirrors `test_cli_skeleton.py`'s `PACKAGE_ROOT`).
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def test_closed_stdout_exits_quietly_instead_of_crashing():
    """Reproduces `tan generate --help | grep -q x`: the reader closes its end
    of the pipe partway through tan's `--help` output.

    Pre-fix this dumped a boxed traceback ending in `OSError: [Errno 22]
    Invalid argument` -- the Windows shape a broken pipe takes (CPython's
    Windows layer maps the underlying `ERROR_NO_DATA` to `errno.EINVAL`, not
    `errno.EPIPE`, so it is not a `BrokenPipeError` and the POSIX-only
    "Note on SIGPIPE" idiom from the `signal` docs does not catch it) -- and
    exited non-zero. The Rust oracle exits 0 with empty stderr in the same
    situation (verified: `tan.exe generate --help | head -n1`); this pins the
    Python port to the same observable behaviour.

    It stayed red on POSIX long after Windows went green, and NOT because the
    expectation is Windows-specific: the oracle exits 0 there too (measured
    against tan 0.4.1 linux-musl, whose 4327-byte `--help` was written into a
    never-read 4 KiB pipe -- a guaranteed EPIPE). The port exited 1 because a
    `BrokenPipeError` never reaches `tan.__main__`'s `except OSError` on
    POSIX: Rich's `Console.on_broken_pipe` turns it into `SystemExit(1)`
    first (and Typer's `_main` would otherwise do the same). Both are
    handled now -- see `_caused_by_broken_pipe`. So a failure here is a
    REGRESSION in that guard, not a platform expectation to relax.

    Drains stderr on a background thread rather than `Popen.communicate()`:
    `communicate()` also tries to read the now-manually-closed stdout pipe,
    which raises `ValueError: read of closed file` on its own reader thread.
    """
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    with subprocess.Popen(
        [sys.executable, "-m", "tan", "generate", "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    ) as proc:
        stderr_chunks: list[bytes] = []
        drain = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()))
        drain.start()

        proc.stdout.readline()  # read exactly one line, like `head -n1` / `grep -q`
        proc.stdout.close()  # then close the read end early -- the broken pipe

        proc.wait(timeout=10)
        drain.join(timeout=10)
        stderr = b"".join(stderr_chunks)

        assert not drain.is_alive(), "stderr drain thread did not finish -- read is incomplete"
        assert proc.returncode == 0, stderr.decode(errors="replace")
        assert stderr == b"", stderr.decode(errors="replace")
