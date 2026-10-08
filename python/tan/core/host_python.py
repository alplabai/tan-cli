# SPDX-License-Identifier: Apache-2.0
"""ONE host-interpreter resolver, shared by `tan doctor`'s `hostPython` check
and `tan build`'s `-DPython3_EXECUTABLE`.

tan-cli#1317. Doctor probed `python3`/`python` off PATH and passed on a 3.14
interpreter while `tan build` (no workspace venv) baked the BARE name
`python3` into the Zephyr slice, so CMake ran its own `FindPython3` search
and picked a shadowing `~/.local/bin/python3.10`. Two resolvers meant the two
commands could disagree about which Python the build uses. This module is the
single answer; both call it.

`resolve_tool` finds each candidate, the candidate is RUN, and the absolute
`sys.executable` it reports is what a build bakes in -- never a bare name.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from tan.core.probe import probe
from tan.core.tool_lookup import resolve_tool

# Security (code execution via module hijack): `-c` puts the CWD on `sys.path`,
# so `core.probe` runs every `python -c` probe from a fresh EMPTY directory, and
# the code below also strips '' / the CWD from `sys.path` before LOCATING
# `west` (`find_spec('west.version')` imports the parent `west` package from
# the interpreter's own site dirs, never from the CWD). `-I` is deliberately
# NOT used: it would hide a legitimately installed west in the user site
# (`~/.local/lib/pythonX.Y/site-packages`, where `pip install --user west`
# lands). `-P` (3.11+) is not used either: the probe must run under 3.10
# candidates too, and the explicit strip does the same job there.
_PROBE_CODE = (
    "import sys,os;sys.path[:]=[p for p in sys.path if p not in ('',os.getcwd(),os.curdir)];"
    "import importlib.util as u\n"
    "def f(n):\n"
    " try:\n"
    "  s=u.find_spec(n)\n"
    "  return s is not None and s.origin is not None\n"
    " except Exception:\n"
    "  return False\n"
    "print('%d.%d' % sys.version_info[:2]);print(sys.executable);"
    "print('west=%d' % (f('west') and f('west.version')))"
)


@dataclass(frozen=True)
class HostPython:
    display: str  #: how the candidate is spelled (`python3`, `py -3`)
    version: tuple[int, int]
    resolved: str  #: PATH-resolved path of the candidate's first argv word
    interpreter: str  #: absolute `sys.executable` the interpreter reported
    has_west: bool = False  #: `import west.version` succeeds under it


def python_candidates() -> list[list[str]]:
    """Windows leads with the `py` launcher (a machine can have a good 3.12
    with no bare `python`, and bare `python.exe` is often the Store alias)."""
    if os.name == "nt":
        return [["py", "-3"], ["python"], ["python3"]]
    return [["python3"], ["python"]]


def _posix_path_pythons(environ) -> list[str]:
    """Every `python3`/`python`/`python3.N` file along PATH, in PATH order (a
    venv's `bin/` first on PATH must not hide a better interpreter behind it).
    An unreadable PATH entry is skipped, never raised."""
    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    for d in (environ.get("PATH") or "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        minors = sorted(
            (n for n in names if re.fullmatch(r"python3\.\d+", n)),
            key=lambda n: -int(n.split(".")[1]),
        )
        for name in ["python3", "python", *minors]:
            full = os.path.join(d, name)
            if not (os.path.isfile(full) and os.access(full, os.X_OK)):
                continue
            # Dedupe on (realpath(dir), realpath(file)): folds `/bin` vs
            # `/usr/bin` under usrmerge, but keeps a venv's `bin/python3`
            # (a symlink to the system python, in a DIFFERENT dir and a
            # different environment) separate.
            key = (os.path.realpath(d), os.path.realpath(full))
            if key in seen:
                continue
            seen.add(key)
            out.append(full)
    return out


def _parse(raw: str) -> tuple[tuple[int, int], str | None, bool] | None:
    """Version from the LAST version-shaped line (noise may precede it); the
    next lines are `sys.executable` and `west=0|1`."""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    for i in range(len(lines) - 1, -1, -1):
        m = re.fullmatch(r"(\d+)\.(\d+)", lines[i])
        if m is not None:
            exe = lines[i + 1] if i + 1 < len(lines) else None
            west = i + 2 < len(lines) and lines[i + 2] == "west=1"
            return (int(m.group(1)), int(m.group(2))), exe, west
    if lines:
        m = re.search(r"(\d+)\.(\d+)", lines[-1])
        if m is not None:
            return (int(m.group(1)), int(m.group(2))), None, False
    return None


def probe_all_host_pythons(env=None) -> list[HostPython]:
    """Every distinct interpreter that RUNS, in discovery order."""
    environ = os.environ if env is None else env
    if os.name == "nt":
        cands = [(c, resolve_tool(c[0], environ).resolved) for c in python_candidates()]
    else:
        # argv[0] is the ABSOLUTE path: a bare name makes Python re-search PATH for
        # `sys.executable`, misreporting a venv's python as whatever is later on PATH.
        cands = [([p], p) for p in _posix_path_pythons(environ)]
    seen: set[str] = set()
    found: list[HostPython] = []
    for candidate, resolved in cands:
        if resolved is None:
            continue
        out = probe([*candidate, "-c", _PROBE_CODE], executable=resolved)
        parsed = _parse(out) if out is not None else None
        if parsed is None:
            continue
        version, exe, west = parsed
        interpreter = exe if exe and os.path.isabs(exe) else resolved
        if interpreter in seen:
            continue
        seen.add(interpreter)
        found.append(HostPython(" ".join(candidate) if os.name == "nt" else os.path.basename(candidate[0]), version, resolved, interpreter, west))
    return found


def select_host_python(
    found: list[HostPython], floor: tuple[int, int], *, need_west: bool = False
) -> HostPython | None:
    """Best of `found`: >=floor AND west-capable, then >=floor (unless
    `need_west`), then the first that merely ran (so a too-old message can name
    a real version)."""
    for pred in (
        lambda h: h.version >= floor and h.has_west,
        lambda h: h.version >= floor and not need_west,
        lambda h: True,
    ):
        for h in found:
            if pred(h):
                return h
    return None


def probe_host_python(
    floor: tuple[int, int], env=None, *, need_west: bool = False
) -> HostPython | None:
    return select_host_python(probe_all_host_pythons(env), floor, need_west=need_west)


def describe_unsuitable(found: list[HostPython], floor: tuple[int, int]) -> str:
    """Name every candidate and why it does not qualify."""
    want = f"{floor[0]}.{floor[1]}"
    if not found:
        return (
            "no runnable Python interpreter was found on PATH; "
            f"Zephyr needs Python {want}+ with `west` importable."
        )
    parts = []
    for h in found:
        why = []
        if h.version < floor:
            why.append(f"too old (below {want})")
        if not h.has_west:
            why.append("no `west` module")
        parts.append(f"`{h.interpreter}` {h.version[0]}.{h.version[1]}: " + ", ".join(why or ["ok"]))
    return (
        f"no host Python is usable for the Zephyr build -- Zephyr needs Python {want}+ "
        "that can import `west`. Found: " + "; ".join(parts) + "."
    )


def build_interpreter(
    venv_python: str | None,
    fallback: str,
    plan_uses_python_token: bool,
    floor: tuple[int, int],
    *,
    probe_all=probe_all_host_pythons,
) -> tuple[str, str | None]:
    """The `${PYTHON}` a build bakes in, as `(interpreter, refusal)`.

    A workspace venv wins unchanged. Otherwise, when the plan uses `${PYTHON}`
    (every Zephyr slice's `-DPython3_EXECUTABLE`), the interpreter is the
    absolute one the shared resolver picks: >= `floor` (the doctor's EFFECTIVE
    floor) and able to import `west` -- never the bare `python3` (tan-cli#1317).
    Forward-slashed, because a Windows backslash in a CMake `-D` is an escape
    (alp-sdk#849, `tan.core.venv.venv_python`). `refusal` names each candidate
    when none qualifies.
    """
    if venv_python is not None or not plan_uses_python_token:
        return (venv_python or fallback), None
    found = probe_all()
    best = select_host_python(found, floor, need_west=True)
    if best is not None and best.version >= floor and best.has_west:
        return best.interpreter.replace("\\", "/"), None
    return fallback, describe_unsuitable(found, floor)
