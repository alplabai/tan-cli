# SPDX-License-Identifier: Apache-2.0
"""Host toolchain-root resolution for build-plan `${TOOLCHAIN_ROOT}`
substitution -- the Python port of `crate::toolchain::resolve_toolchain_root`
(tan-cli#547).

`${TOOLCHAIN_ROOT}` was a RECOGNISED token on the Python line with no
resolver behind it: `build_cmd.py` passed `toolchain_root=None`
unconditionally, so every slice naming the token demoted on EVERY host --
including hosts that have a perfectly good toolchain -- rather than only
where the host genuinely has none. That is what this module closes.

## The contract, MEASURED against the oracle, never read out of `crates/`

Established by running `target/debug/tan` (`tan 0.4.1`) with a
`planPathMode: tokened` plan whose one `configArtefacts[].contents` is
`TOOLCHAIN_ROOT=${TOOLCHAIN_ROOT}`, under `--materialise`, and reading back
the file it wrote (or the `issues[]` entry when it wrote nothing):

  1. `ZEPHYR_SDK_INSTALL_DIR`, when set, non-empty, and naming a path that
     EXISTS, wins outright and is returned VERBATIM. Measured: a trailing
     slash survives into the substituted value (`/home/x/zephyr-sdk-1.0.1/`),
     and a directory that exists but contains no toolchain at all is still
     accepted -- the oracle probes existence, not contents.
  2. Set but naming a path that does NOT exist -- and the empty string --
     fall THROUGH to the scan rather than failing. Measured: with a real
     install under `$HOME` and `ZEPHYR_SDK_INSTALL_DIR` pointing at a
     nonexistent directory, the oracle resolved the scanned install.
  3. Otherwise every entry named `zephyr-sdk*` DIRECTLY under a scan root is
     a candidate. Prefix match, not a `zephyr-sdk-<version>` shape:
     `zephyr-sdkXYZ` was accepted. Direct children only: an install one
     level deeper (`$HOME/tools/zephyr-sdk-1.0.1`) was NOT found.
  4. Exactly one candidate resolves. ZERO is unresolved with
     `NO_TOOLCHAIN_ADVICE`. TWO OR MORE is ALSO unresolved -- with a
     different, sharper reason (`_several_installs_advice`) that names every
     candidate, SORTED (measured: created in `zzz, mmm, aaa` readdir order,
     reported `aaa, mmm, zzz`). That second reason had no Python equivalent
     at all before this module; the port collapsed both cases onto the
     "no toolchain" wording.

## The two DELIBERATE divergences from the oracle, and why

  * **A candidate must be a DIRECTORY.** The oracle accepts a plain FILE
    named `zephyr-sdk-*` as a toolchain root (measured: a `touch`ed
    `$HOME/zephyr-sdk-1.0.1` resolved). That is not a wart with no
    consequence: `"zephyr-sdk-0.16.5_linux-x86_64.tar.xz".startswith(
    "zephyr-sdk")` is true, so the downloaded ARCHIVE sitting in `$HOME`
    beside the install it was extracted from makes the host look like it has
    TWO installs -- and rule 4 above then demotes every slice on a host that
    has exactly one working toolchain. Requiring a directory can only ever
    REMOVE a false candidate, never add one, so it cannot cause the
    demotion this issue is about.
  * **The scan roots come from `HOME`, `USERPROFILE` and `Path.home()`, not
    `HOME` alone.** Measured, the oracle reads `$HOME` only: with `HOME`
    pointed at an SDK-less directory and `USERPROFILE` at the real one, it
    resolved nothing. Reproducing that exactly would re-open the defect
    `doctor_cmd._zephyr_sdk_scan_roots` was written for -- under Git
    Bash/MSYS on Windows `HOME` is a POSIX-translated path (`/c/Users/dev`)
    while the SDK sits under the native `%USERPROFILE%`, and a real host was
    measured reporting "no SDK" with the SDK installed. On any ordinary
    POSIX host the two sets are identical, so the measured Linux parity
    above still holds byte-for-byte.

`doctor_cmd`'s scan is DUPLICATED here rather than imported: `doctor_cmd`
imports from `build_cmd`, and `build_cmd` imports this module, so the import
would be circular. (`token_substitution._is_own_git_checkout` is duplicated
from the same module for its own reason -- this is the second instance of
the same shape, not a new one.)

`doctor_cmd._zephyr_sdk_root_valid`'s stricter probe -- "does
`gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc` exist" -- is deliberately NOT
reused. Doctor answers a yes/no health question where a false PASS is the
harm (tan-cli#286). Here the harm runs the other way: a Zephyr SDK installed
with a non-ARM toolchain subset is a real, supported install, and refusing
it would demote every slice on that host -- which is precisely the defect
tan-cli#547 reports.
"""
import os
from dataclasses import dataclass
from pathlib import Path

#: What every candidate directory's name must start with. A PREFIX, not a
#: `zephyr-sdk-<version>` pattern -- measured against the oracle, which
#: accepted a directory named `zephyr-sdkXYZ`.
ZEPHYR_SDK_DIR_PREFIX = "zephyr-sdk"

#: The env var that overrides the scan outright, and the one every reason
#: string below tells the caller to set.
ZEPHYR_SDK_INSTALL_DIR = "ZEPHYR_SDK_INSTALL_DIR"

#: The reason a `${TOOLCHAIN_ROOT}` demotion (or the plan-fatal
#: `boardYaml`/`sharedArtefacts` refusal) carries when this host has NO
#: toolchain at all. Verbatim the oracle's wording, in this port's ASCII
#: dash convention. Lives here rather than in `token_substitution.py` so the
#: resolver owns both of its own reasons; that module imports it.
NO_TOOLCHAIN_ADVICE = (
    "no toolchain install is detectable on this host -- install the Zephyr SDK "
    "(`west sdk install`) or set `ZEPHYR_SDK_INSTALL_DIR` to an existing install"
)


def _several_installs_advice(candidates: list[str]) -> str:
    """The reason for the AMBIGUOUS host -- several installs and nothing
    picking between them. Sharper than `NO_TOOLCHAIN_ADVICE` because the fix
    is different: nothing needs installing, one of these needs choosing.
    Names every candidate so the caller can paste one straight into
    `ZEPHYR_SDK_INSTALL_DIR`."""
    return (
        f"this host has several toolchain installs and no `{ZEPHYR_SDK_INSTALL_DIR}` to "
        f"choose between them ({', '.join(candidates)}) -- set `{ZEPHYR_SDK_INSTALL_DIR}` "
        f"to the one this build should use"
    )


@dataclass(frozen=True)
class ToolchainResolution:
    """`root` is the host toolchain root, or `None` when this host has no
    single unambiguous one. `advice` is the reason for that `None`, threaded
    into the demotion message so the two unresolved cases -- none installed
    vs several installed -- read differently. `advice` is still populated
    when `root` resolved (nothing reads it then); it is not a nullable
    second signal for the same fact."""

    root: str | None
    advice: str = NO_TOOLCHAIN_ADVICE


def _scan_roots() -> list[Path]:
    """Every directory scanned for a `zephyr-sdk*` install: `/opt`, plus
    `$HOME`, `%USERPROFILE%` AND `Path.home()` -- ALL of them, never
    `HOME or USERPROFILE`. See this module's docstring for the measured Git
    Bash/MSYS host that makes the union load-bearing rather than defensive.

    Deduplicated on the raw string only; identical directories reached by
    different spellings are collapsed later, on the CANDIDATES, where a
    duplicate would otherwise fake an ambiguous host."""
    roots = [Path("/opt")]
    seen: set[str] = set()
    for raw in (os.environ.get("HOME"), os.environ.get("USERPROFILE")):
        if raw and raw not in seen:
            seen.add(raw)
            roots.append(Path(raw))
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        home = None
    if home is not None and str(home) not in seen:
        roots.append(home)
    return roots


def _candidates() -> list[str]:
    """Every distinct `zephyr-sdk*` install directory found under
    `_scan_roots()`, as POSIX-separator strings, SORTED.

    Deduplicated on `Path.resolve()`, keeping the FIRST literal spelling
    seen. This is the guard that stops `_scan_roots()`'s deliberate overlap
    from breaking the caller: `$HOME` and `Path.home()` routinely name the
    same directory (and a symlinked or trailing-slash `$HOME` slips past
    `_scan_roots`'s own string-level dedup), which would list one install
    twice and demote every slice on a host with exactly one toolchain --
    the very failure tan-cli#547 is about.

    `as_posix()`, not `str()`: identical on POSIX (so the measured oracle
    parity holds byte-for-byte) and the separator convention
    `token_substitution.py` already normalises `${PROJECT_ROOT}` to, so a
    substituted `${TOOLCHAIN_ROOT}` does not arrive at CMake in the one
    spelling `${PROJECT_ROOT}` never uses.

    Never raises: an unreadable or missing scan root is "nothing found
    there", not a failed build.
    """
    found: dict[str, str] = {}
    for root in _scan_roots():
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith(ZEPHYR_SDK_DIR_PREFIX):
                continue
            try:
                if not entry.is_dir():
                    continue
                key = str(entry.resolve())
            except OSError:
                continue
            found.setdefault(key, entry.as_posix())
    return sorted(found.values())


def resolve_toolchain_root() -> ToolchainResolution:
    """This host's `${TOOLCHAIN_ROOT}`, or the reason there is not exactly
    one. See the module docstring for the measured oracle contract each
    branch below reproduces.

    Called LAZILY by `build_cmd` -- only for a plan that actually names the
    token -- so the filesystem scan never runs for the plans every SDK emits
    today, which name none. The laziness is a property of the CALL SITE, not
    of this function: it does the work it is asked for, every time.
    """
    env_dir = os.environ.get(ZEPHYR_SDK_INSTALL_DIR)
    if env_dir:
        try:
            exists = Path(env_dir).exists()
        except OSError:
            exists = False
        if exists:
            # VERBATIM, not normalised: measured, the oracle substitutes the
            # env value exactly as given, trailing slash and all. An operator
            # who pinned this variable pinned a literal path, and rewriting
            # it would make the substituted value disagree with what they set.
            return ToolchainResolution(env_dir)

    candidates = _candidates()
    if len(candidates) == 1:
        return ToolchainResolution(candidates[0])
    if not candidates:
        return ToolchainResolution(None, NO_TOOLCHAIN_ADVICE)
    return ToolchainResolution(None, _several_installs_advice(candidates))
