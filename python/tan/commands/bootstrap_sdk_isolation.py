# SPDX-License-Identifier: Apache-2.0
"""Keep `west sdk install` from adopting an SDK it did not install (tan-cli#1552).

Zephyr's `west sdk install` ignores `--install-dir` when its own discovery
(`FindZephyr-sdk.cmake`) already lists the requested version, and then runs
`setup.sh` on THAT directory. Discovery reads the CMake user package registry
and the fixed prefixes `/usr /usr/local /opt $HOME $HOME/.local
$HOME/.local/opt $HOME/bin`. tan then finds its scratch directory without
`sdk_version`, and the old "remove that directory and re-run" remedy looped
forever. Separately, when west DID honour `--install-dir`, its `setup.sh -c`
registered the scratch path in the user's real `~/.cmake/packages`, and tan
then moved the directory away, leaving a dangling entry. The scratch HOME fixes
that on POSIX only: on Windows CMake's registry is in HKCU, which no environment
variable redirects, so the dangling entry for the moved scratch path remains.

Two halves:

* [`isolated_env`] -- run west with a scratch `HOME`/`USERPROFILE`, which hides
  the `$HOME` prefixes and the file-based registry and redirects registration
  into a directory tan deletes afterwards.
* [`reuse_warning`] -- what is left (`/opt`, `/usr/local`, `/usr`, and the
  Windows HKCU registry, which no env var can hide) is detected after the fact
  and named, with a remedy that actually breaks the loop.

`tan build` is unaffected by the registry: it exports `ZEPHYR_SDK_INSTALL_DIR`
from tan's verified store itself (`tan.core.zephyr_env`), so nothing is
registered for the final store path.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from tan.core import toolchain_provision
from tan.core.subprocess_env import spawn_env

#: Where a scratch home lives, under the toolchain root. Matches
#: `toolchain_provision.wreckage_glob_pattern(leaf)` (`<leaf>.tmp-*`), so a
#: crash that skips the `finally` is swept by the next run's wreckage reclaim.
SCRATCH_HOME_INFIX = "home-"

#: The prefixes `FindZephyr-sdk.cmake` scans, relative to HOME or absolute.
_FIXED_PREFIXES = ("/usr", "/usr/local", "/opt")
_HOME_PREFIXES = ("", ".local", ".local/opt", "bin")


def scratch_home_path(root: Path, leaf: str, pid: int) -> Path:
    return root / f"{leaf}{toolchain_provision.TMP_SUFFIX_PREFIX}{SCRATCH_HOME_INFIX}{pid}"


#: Variables CMake/west read to locate an SDK directly. Blanked (empty is
#: falsy for west's `os.environ.get` and ignored by `find_package`) rather than
#: removed, because `Runner` only overlays.
_SDK_POINTER_VARS = ("ZEPHYR_SDK_INSTALL_DIR", "Zephyr-sdk_DIR", "Zephyr-sdk_ROOT")

#: Tools `west sdk install` shells out to. A PATH entry that is the only home of
#: one of these is never dropped (see [`_cleaned_path`]).
_NEEDED_TOOLS = ("west", "cmake", "7z", "7za", "7zz", "tar", "git")


def _strip_bin(entry: Path) -> Path:
    """CMake turns a PATH entry ending in `bin`/`sbin` (any case) into its
    parent prefix; any other entry is used as-is."""
    return entry.parent if entry.name.lower() in ("bin", "sbin") else entry


def _sdk_like_dirs(prefix: Path) -> list[Path]:
    """`prefix` and its case-insensitive `zephyr-sdk*` children."""
    out = [prefix]
    try:
        out += [c for c in prefix.iterdir() if c.name.lower().startswith("zephyr-sdk") and c.is_dir()]
    except OSError:
        pass
    return out


def _sdk_dirs_at(prefix: Path) -> list[Path]:
    """Zephyr SDK directories CMake's config search takes from `prefix`: those
    of [`_sdk_like_dirs`] holding `Zephyr-sdkConfig.cmake` directly or under
    `cmake/` or `CMake/`."""
    found = []
    for cand in _sdk_like_dirs(prefix):
        try:
            if any((cand / sub / "Zephyr-sdkConfig.cmake").is_file() for sub in ("cmake", "CMake", "")):
                found.append(cand)
        except OSError:
            continue
    return found


def _prefixes_of(value: str, *, is_path: bool) -> list[tuple[str, Path]]:
    """(original entry, prefix CMake searches) for each ABSOLUTE entry. A
    relative entry is relative to west's cwd, not tan's, so it is not judged."""
    out = []
    for entry in value.split(os.pathsep):
        if entry and os.path.isabs(entry):
            out.append((entry, _strip_bin(Path(entry)) if is_path else Path(entry)))
    return out


def _tool_only_here(entry: str, value: str, without: str) -> bool:
    here = Path(entry)
    for tool in _NEEDED_TOOLS:
        found = shutil.which(tool, path=value)
        if found and Path(found).parent == here and shutil.which(tool, path=without) is None:
            return True
    return False


def _cleaned_path(value: str, *, is_path: bool) -> str:
    """`value` minus the entries through which CMake would discover a Zephyr SDK.

    Decision (tan-cli#1552 review): CMake offers no environment variable that
    disables PATH/CMAKE_PREFIX_PATH-derived search, and listsdk's cmake run
    cannot be given one, so the entry itself must go. For PATH that is risky
    for a general prefix such as `~/bin` or `~/.local/bin` (user-installed
    `west`, `cmake`, `7z`), so such an entry is kept when a needed tool
    resolves ONLY through it; the post-hoc reuse warning is then the backstop.
    An entry that is itself an SDK (`<sdk>/bin`) is always dropped.
    """
    hits = [
        (entry, prefix)
        for entry, prefix in _prefixes_of(value, is_path=is_path)
        if _sdk_dirs_at(prefix)
    ]
    if not hits:
        return value
    drop = {entry for entry, _ in hits}
    if is_path:
        without = os.pathsep.join(e for e in value.split(os.pathsep) if e not in drop)
        for entry, prefix in hits:
            if prefix not in _sdk_dirs_at(prefix) and _tool_only_here(entry, value, without):
                drop.discard(entry)
    return os.pathsep.join(e for e in value.split(os.pathsep) if e not in drop)


@contextmanager
def scratch_home(
    root: Path, leaf: str, pid: int, *, enabled: bool, on_degraded: Callable[[str], None] | None = None
) -> Iterator[Path | None]:
    """Create the scratch home and delete it on every exit. Yields `None` when
    disabled (`--dry-run`: nothing is spawned) or when it cannot be created --
    `on_degraded` is then called once, and west still runs with the pointer and
    PATH scrubbing of [`isolated_env`] but without a private HOME."""
    home = scratch_home_path(root, leaf, pid)
    created = False
    if enabled:
        try:
            home.mkdir(parents=True, exist_ok=True)
            created = True
        except OSError as err:
            if on_degraded is not None:
                on_degraded(
                    f"cannot create {home} ({err}); running west sdk install without a "
                    f"private HOME, so an SDK under your home may be reused"
                )
    try:
        yield home if created else None
    finally:
        if created:
            shutil.rmtree(home, ignore_errors=True)


def _real_file(real_home: str | None, *names: str) -> str | None:
    if not real_home:
        return None
    for name in names:
        candidate = Path(real_home) / name
        if candidate.is_file():
            return str(candidate)
    return None


def isolated_env(
    home: Path | None,
    base_extra: dict[str, str] | None,
    *,
    real_home: str | None,
    environ: Mapping[str, str],
    user_base: str | None = None,
) -> dict[str, str] | None:
    """`extra_env` for the west call: the caller's own overlay (the staged
    `NETRC`), SDK-pointer scrubbing, and -- when `home` is given -- a scratch
    home. `None` when there is nothing to overlay.

    `HOME` is what CMake's `$ENV{HOME}` reads; `USERPROFILE` is what Python's
    `expanduser` reads on Windows, where `HOME` is usually unset. Both are set
    on every platform -- harmless where unused.

    What the scratch home hides that west still needs is handed back
    explicitly: the staged credential keeps working (`requests` reads
    `$NETRC`); with none staged the user's own `~/.netrc` is pointed at; a
    `west` that is not the workspace venv's (`pip install --user`) still
    imports because `PYTHONUSERBASE` is that interpreter's REAL user base
    (`user_base`, from [`user_base_for`]; never tan's own, which differs by
    Python version on macOS framework builds); and the user's
    `~/.westconfig` is kept through `WEST_CONFIG_GLOBAL`.

    SDK scrubbing (pointer variables, PATH / CMAKE_PREFIX_PATH entries that
    lead CMake to an SDK) applies with or without a scratch home: measured
    with the real `listsdk.cmake`, each of those finds an SDK on its own.
    """
    env = dict(base_extra or {})
    for name in _SDK_POINTER_VARS:
        if environ.get(name):
            env[name] = ""
    for name in ("PATH", "CMAKE_PREFIX_PATH"):
        current = environ.get(name)
        if current:
            cleaned = _cleaned_path(current, is_path=name == "PATH")
            if cleaned != current:
                env[name] = cleaned
    if home is not None:
        env["HOME"] = env["USERPROFILE"] = str(home)
        if user_base and not environ.get("PYTHONUSERBASE"):
            env["PYTHONUSERBASE"] = user_base
        if "NETRC" not in env and not environ.get("NETRC"):
            netrc = _real_file(real_home, ".netrc", "_netrc")
            if netrc is not None:
                env["NETRC"] = netrc
        if not environ.get("WEST_CONFIG_GLOBAL"):
            west_config = _real_file(real_home, ".westconfig")
            if west_config is not None:
                env["WEST_CONFIG_GLOBAL"] = west_config
    return env or None


def _launcher_interpreter(west: str, path: str | None) -> str | None:
    """The interpreter a `west` launcher script runs under, from its shebang
    (`#!/py` or `#!/usr/bin/env py`); `None` for a binary launcher or anything
    unreadable."""
    launcher = shutil.which(west, path=path)
    if launcher is None:
        return None
    try:
        with open(launcher, "rb") as handle:
            first = handle.readline(512).decode("utf-8", "replace").strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    parts = first[2:].split()
    if parts and os.path.basename(parts[0]) == "env":
        parts = [p for p in parts[1:] if not p.startswith("-")]
        return shutil.which(parts[0], path=path) if parts else None
    return parts[0] if parts else None


def user_base_for(
    west: str, environ: Mapping[str, str], say: Callable[[str], None], *, enabled: bool
) -> str | None:
    """`PYTHONUSERBASE` for a west that is NOT the workspace venv's.

    The scratch HOME hides `~/.local` (or `~/Library/Python/<ver>`), where a
    `pip install --user` west keeps its packages. That location depends on the
    interpreter that runs west, so it is asked of west's OWN interpreter (its
    launcher's shebang) with the real environment -- tan's `site.getuserbase()`
    would be the wrong answer whenever the two Pythons differ. A workspace venv
    west (an absolute path) never reads the user site, so nothing is set. When
    the interpreter cannot be determined nothing is set and `say` explains.
    """
    if not enabled or os.path.isabs(west):
        return None
    python = _launcher_interpreter(west, environ.get("PATH"))
    if python is not None:
        try:
            out = subprocess.run(
                [python, "-c", "import site;print(site.getuserbase())"],
                capture_output=True, text=True, timeout=30, check=False, env=spawn_env(base=environ),
            )
            base = out.stdout.strip()
            if out.returncode == 0 and base:
                return base
        except (OSError, subprocess.SubprocessError):
            pass
    say(
        f"could not tell which Python runs {west}, so PYTHONUSERBASE is left unset; a "
        f"`pip install --user` west may fail to import under the scratch home"
    )
    return None


def reused_sdk_candidates(version: str, real_home: str | None, environ: Mapping[str, str]) -> list[str]:
    """Existing SDK directories of `version` in every place CMake searches:
    the fixed prefixes, the home prefixes, and the PATH / CMAKE_PREFIX_PATH
    derived ones. Registry-only reuse cannot be located from here."""
    prefixes = [Path(p) for p in _FIXED_PREFIXES]
    if real_home:
        prefixes += [Path(real_home) / rel if rel else Path(real_home) for rel in _HOME_PREFIXES]
    for name in ("PATH", "CMAKE_PREFIX_PATH"):
        prefixes += [p for _, p in _prefixes_of(environ.get(name, ""), is_path=name == "PATH")]
    found: list[str] = []
    for prefix in prefixes:
        for sdk in _sdk_like_dirs(prefix):
            try:
                text = (sdk / toolchain_provision.SDK_VERSION_FILE_RELPATH).read_text(encoding="utf-8")
            except OSError:
                continue
            if text.strip() == version and str(sdk) not in found:
                found.append(str(sdk))
    return found


def reuse_warning(
    version: str, tmp_dir: Path, real_home: str | None, environ: Mapping[str, str]
) -> str:
    """The `toolchain-install` text for "west exited 0 but installed nothing
    into `tmp_dir`" -- west very likely reused an SDK it already knew about."""
    found = reused_sdk_candidates(version, real_home, environ)
    where = (
        ", ".join(sorted({str(Path(f).parent) for f in found}))
        if found
        else "/usr, /usr/local, /opt, $HOME, $HOME/.local, $HOME/.local/opt and $HOME/bin"
    )
    named = (
        "; the likely one is " + ", ".join(found)
        if found
        else " (no zephyr-sdk-" + version + " directory was found in the scanned "
        "prefixes, so it is probably known only through the CMake package registry)"
    )
    return (
        f"west sdk install exited 0 but wrote no sdk_version into {tmp_dir}: it most "
        f"likely printed \"Zephyr SDK version {version} is already installed at <path>. "
        f"Using it.\" and ignored --install-dir, because another Zephyr SDK {version} "
        f"is visible to CMake's package discovery{named}. Removing the scratch "
        f"directory will not help. Move or rename that SDK out of {where}, or delete its "
        f"entry from the CMake user package registry (~/.cmake/packages/Zephyr-sdk/, "
        f"or HKEY_CURRENT_USER\\Software\\Kitware\\CMake\\Packages\\Zephyr-sdk on "
        f"Windows), then re-run `tan bootstrap`."
    )
