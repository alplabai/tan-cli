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
  * **The ADR 0021 artifact-keyed store (`tan.core.toolchain_provision.
    resolve_toolchain_root`, `~/.alp/toolchains` or `$ALP_TOOLCHAIN_ROOT`) is
    ALSO scanned, one level in, for its own `zephyr-sdk*`-prefixed leaf
    directories.** The v0.4.1 oracle predates issue #474 entirely and has no
    opinion on this store -- there is nothing to diverge FROM here, only a
    gap `tan-cli#990` review closed: `tan bootstrap`'s own toolchain phase
    installs into `<store-root>/zephyr-sdk-<version>-arm-zephyr-eabi/`, two
    directory levels below every root this function used to look at, so a
    customer who ran nothing but `tan bootstrap` (the whole point of issue
    #474) still could not get `${TOOLCHAIN_ROOT}` to resolve to what it just
    downloaded without ALSO hand-exporting `ZEPHYR_SDK_INSTALL_DIR` --
    keeping the very manual step ADR 0021 Lane 1 P1 exists to remove. Per
    alp-sdk#1286's own closing decision ("never a location; resolving it to
    a concrete path is the executor's job, not the plan's"), tan is that
    executor, and this is that resolution. A `.tmp-<pid>` wreckage sibling
    (`toolchain_provision.TMP_SUFFIX_PREFIX`) from an interrupted attempt is
    excluded by name in `_candidates()` below -- it also starts with
    `zephyr-sdk`, and would otherwise fake a "several installs, ambiguous"
    host next to the one real, verified store.

`doctor_cmd`'s scan is DUPLICATED here rather than imported: `doctor_cmd`
imports from `build_cmd`, and `build_cmd` imports this module, so the import
would be circular. (`token_substitution._is_own_git_checkout` is duplicated
from the same module for its own reason -- this is the second instance of
the same shape, not a new one.)

That circularity is about `_scan_roots`/`_candidates`/`resolve_toolchain_root`
specifically, not about every name in this module: `doctor_cmd` DOES import
[`_is_toolchain_wreckage`] and [`_toolchain_store_scan_root`] directly from
here (tan-cli#1186), since neither of those two touches `build_cmd` and the
cycle above never applies to them.

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

from tan.core import toolchain_provision as _tp

#: What every candidate directory's name must start with. A PREFIX, not a
#: `zephyr-sdk-<version>` pattern -- measured against the oracle, which
#: accepted a directory named `zephyr-sdkXYZ`. Matches BOTH a manual
#: `zephyr-sdk-1.0.1` install AND `tan.core.toolchain_provision.
#: store_dir_name`'s `zephyr-sdk-<version>-arm-zephyr-eabi` leaf -- no
#: change needed here for the artifact-keyed store below, only to WHERE it
#: is looked for.
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


def _toolchain_store_scan_root() -> Path:
    """`~/.alp/toolchains` (or `$ALP_TOOLCHAIN_ROOT`) -- the ADR 0021
    artifact-keyed store `tan bootstrap`'s own toolchain phase installs
    into. Delegates to `toolchain_provision.resolve_toolchain_root`, the
    SAME function `bootstrap_cmd._toolchain_root_and_leaf` calls, so this
    can never compute a different root than the one bootstrap actually used
    -- the two-levels-deep discovery gap tan-cli#990 review found was a
    location bug, not a fact this module should ever re-derive its own
    opinion of.

    `home_alp_dir` is duplicated inline (`~/.alp`, `USERPROFILE` on Windows
    else `HOME`) rather than importing `tan.core.sdk_discovery._home_alp_dir`
    -- this module already duplicates `doctor_cmd`'s scan for the identical
    reason (see the module docstring): one two-line duplication is cheaper
    than auditing this module's own import graph for a cycle every time the
    canonical helper's home changes.
    """
    home = os.environ.get("USERPROFILE" if os.name == "nt" else "HOME")
    home_alp_dir = str(Path(home or ".") / ".alp")
    root = _tp.resolve_toolchain_root(os.environ.get("ALP_TOOLCHAIN_ROOT"), home_alp_dir)
    return Path(root.path_str)


def _read_text_or_none(path: Path) -> str | None:
    """`None` on ANY read failure -- `OSError` (missing/unreadable/a
    directory) AND `UnicodeDecodeError` (a subclass of `ValueError`) alike.

    tan-cli#1209 review MAJOR: `verified_store_dir`'s two callers of this
    used to write `path.read_text(encoding="utf-8")` under a bare
    `except OSError`, so one non-UTF-8 byte in either
    `<sdk_root>/metadata/toolchains.json` or the store's own
    `.alp-toolchain-stamp.json` raised `UnicodeDecodeError` straight past
    this function's own "never raises" contract and out of `execute_slices`
    before any slice dispatched -- with no envelope at all under
    `--format json`. `errors="replace"` plus `except (OSError, ValueError)`
    mirrors `doctor_cmd._read_text` exactly (same two files, read from the
    same two call sites in that module), so `tan build` and `tan doctor`
    can no longer disagree on a corrupt manifest or stamp: neither raises,
    and a mangled byte fails the JSON/shape parse that follows rather than
    the read itself."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _read_manifest(sdk_root: str | None) -> _tp.ToolchainManifest | None:
    """@sdk_root's `metadata/toolchains.json`, parsed -- `None` on no
    `sdk_root`, an unreadable file, or a malformed manifest. Shared by
    [`verified_store_dir`] and [`host_scan_has_toolchain`] so the two can
    never independently disagree on what THIS checkout's pin says; never
    raises, matching every other reader in this module."""
    if sdk_root is None:
        return None
    manifest_path = Path(sdk_root) / "metadata" / "toolchains.json"
    manifest_text = _read_text_or_none(manifest_path)
    if manifest_text is None:
        return None
    try:
        return _tp.parse_toolchain_manifest(manifest_text)
    except _tp.ToolchainManifestError:
        return None


def _toolchain_store_dir(manifest: _tp.ToolchainManifest) -> Path:
    """`<store-root>/<per-version leaf>` for @manifest -- the ONE directory
    tan's own installs use for this exact pin. Mirrors
    `doctor_cmd._toolchain_store_dir` byte-for-byte (same formula: `_
    toolchain_store_scan_root() / store_dir_name(manifest.version)`) so the
    two modules can never independently compute a different answer for
    "which directory is tan's own install for this pin".

    [`verified_store_dir`] and [`host_scan_has_toolchain`] both key their
    store exclusion off THIS leaf, never the whole `_toolchain_store_
    scan_root()` -- tan-cli#1209 review BLOCKER. See `doctor_cmd.
    _host_toolchain_matching_pin`'s docstring for why the narrower leaf,
    not the whole configured root, is the correct exclusion under the ADR
    0021 `$ALP_TOOLCHAIN_ROOT`-pointed-at-an-ancestor escape hatch: a
    stamped leaf for a DIFFERENT, no-longer-pinned version can sit right
    next to the current one inside the same store, and is a real,
    independent host toolchain that must not be hidden by excluding the
    whole root."""
    return _toolchain_store_scan_root() / _tp.store_dir_name(manifest.version)


def verified_store_dir(sdk_root: str | None) -> Path | None:
    """tan's own ADR 0021 store for the checkout's PINNED cross-toolchain
    version, iff `.alp-toolchain-stamp.json` there matches that pin -- the
    identical pin+stamp predicate `bootstrap_cmd.toolchain_phase`
    (skip-if-already-installed) and `doctor_cmd.toolchain_check` (the
    `toolchain` check) already apply via `stamp_matches_pin`, reused here
    rather than re-derived a third time (tan-cli#1209: `ZEPHYR_SDK_INSTALL_DIR`
    handoff into a spawned build's env).

    `None` on ANY of: no `sdk_root`, an unreadable/malformed
    `metadata/toolchains.json`, no stamp file, or a stamp that does not
    match -- never raises, matching every other reader in this module.

    Deliberately NOT [`resolve_toolchain_root`] (this module's
    `${TOOLCHAIN_ROOT}` resolver): that scans every `zephyr-sdk*` under
    `_scan_roots()` PLUS this store and refuses outright on ambiguity
    ("several installs"). A caller wiring `ZEPHYR_SDK_INSTALL_DIR` for a
    spawned child wants tan's own stamped, digest-verified store for
    EXACTLY this pin -- never a scan that can be defeated by an unrelated
    hand-installed SDK sitting elsewhere on the same host.
    """
    manifest = _read_manifest(sdk_root)
    if manifest is None:
        return None
    store_dir = _toolchain_store_dir(manifest)
    stamp_text = _read_text_or_none(store_dir / _tp.STAMP_FILENAME)
    if stamp_text is None:
        return None
    stamp = _tp.parse_stamp(stamp_text)
    if not _tp.stamp_matches_pin(stamp, manifest):
        return None
    return store_dir


def _is_toolchain_wreckage(name: str) -> bool:
    """`True` for a `.tmp-<pid>` sibling of an interrupted acquisition
    (`toolchain_provision.TMP_SUFFIX_PREFIX`) -- it starts with
    `zephyr-sdk` too (it is `<leaf><TMP_SUFFIX_PREFIX><pid>` and `leaf`
    itself starts with `zephyr-sdk`), so without this it would fake a
    SECOND, ambiguous candidate next to the one real, verified store entry
    every time `tan bootstrap` was interrupted mid-acquisition and its own
    startup reclaim (`bootstrap_cmd._reclaim_toolchain_wreckage`) has not
    yet run again."""
    return _tp.TMP_SUFFIX_PREFIX in name


def _candidates(roots: list[Path] | None = None) -> list[str]:
    """Every distinct `zephyr-sdk*` install directory found under @roots, as
    POSIX-separator strings, SORTED. @roots defaults to `_scan_roots()` PLUS
    `_toolchain_store_scan_root()` -- the full set `resolve_toolchain_root`
    needs. [`host_scan_has_toolchain`] passes `_scan_roots()` alone, to ask
    the narrower "does the HOST carry one, never mind tan's own store"
    question.

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
    if roots is None:
        roots = [*_scan_roots(), _toolchain_store_scan_root()]
    found: dict[str, str] = {}
    for root in roots:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith(ZEPHYR_SDK_DIR_PREFIX):
                continue
            if _is_toolchain_wreckage(entry.name):
                continue
            try:
                if not entry.is_dir():
                    continue
                key = str(entry.resolve())
            except OSError:
                continue
            found.setdefault(key, entry.as_posix())
    return sorted(found.values())


#: Duplicated from `doctor_cmd.ZEPHYR_SDK_TOOLCHAIN_DIR` -- see this
#: module's own docstring on why `doctor_cmd` is not imported here.
_ZEPHYR_SDK_TOOLCHAIN_DIR = ("gnu", "arm-zephyr-eabi", "bin")


def _host_toolchain_is_usable(root: Path) -> bool:
    """`True` when @root actually contains the `arm-zephyr-eabi` cross
    compiler, not merely a directory named right. Duplicated from
    `doctor_cmd._zephyr_sdk_root_valid` (import cycle, see module
    docstring) for [`host_scan_has_toolchain`] ALONE -- never for
    `_candidates`/`resolve_toolchain_root` above, which deliberately accept
    a name-only or non-ARM directory as a real, substitutable toolchain
    (see this module's docstring on the two deliberate divergences).

    `host_scan_has_toolchain` asks a different question -- "does the host
    already have ITS OWN working toolchain, independent of tan's store" --
    and a directory that merely STARTS WITH `zephyr-sdk`, with no compiler
    inside it, answers "no", not "yes" (tan-cli#1209 review MAJOR: an empty
    `~/zephyr-sdk-leftover/` -- a stale download, an interrupted manual
    extraction -- was silently disabling tan's own verified store on any
    host carrying one, because `_candidates` applies no validity probe by
    design)."""
    exe = "arm-zephyr-eabi-gcc.exe" if os.name == "nt" else "arm-zephyr-eabi-gcc"
    try:
        return root.joinpath(*_ZEPHYR_SDK_TOOLCHAIN_DIR, exe).is_file()
    except OSError:
        return False


def host_scan_has_toolchain(sdk_root: str | None) -> bool:
    """`True` when a USABLE `zephyr-sdk*` directory is visible under
    `_scan_roots()` -- CMake's own `FindZephyr-sdk.cmake` prefix-scan
    territory -- that is NOT itself tan's own ADR 0021 store leaf for
    @sdk_root's pinned version.

    tan-cli#1209 review MINOR: `verified_store_dir`'s caller
    (`zephyr_env_overrides`, wired from `execute.py`) used to fill
    `ZEPHYR_SDK_INSTALL_DIR` from tan's own store whenever it verified,
    full stop -- outranking CMake's own prefix scan and the CMake user
    package registry for EVERY slice, on EVERY host. `doctor_cmd.
    _zephyr_sdk_scan_roots` never does that: it lists `_toolchain_store_
    scan_root()` LAST among its scan roots, so a full, pre-existing host
    SDK always wins there. tan's own store is installed `-t
    arm-zephyr-eabi` ONLY, so forcing it into a spawned child's env ahead
    of a fuller host SDK can fail a non-ARM slice (e.g.
    `aarch64-zephyr-elf`) that configured fine without any
    `ZEPHYR_SDK_INSTALL_DIR` override at all, and makes `tan build` use a
    different toolchain than `tan doctor` reports. `execute_slices` calls
    this before deciding whether to pass `verified_store_dir`'s result
    through at all, so tan's own store fills the gap only when the host
    scan finds nothing else -- the same "last resort" precedence doctor
    already gives it.

    The store exclusion is keyed on `_toolchain_store_dir(manifest)` -- the
    one PER-VERSION leaf tan's own install for THIS pin uses -- never the
    whole `_toolchain_store_scan_root()` (tan-cli#1209 review BLOCKER: this
    used to key on the whole root, the exact form `doctor_cmd.
    _host_toolchain_matching_pin`'s own docstring documents as wrong, and the
    same mistake tan-cli#1186 already shipped and had to fix once).
    Excluding the whole root hides every OTHER leaf living in the same
    store too -- e.g. a stamped leaf for a version this checkout no longer
    pins, still a real, independent, usable toolchain -- as well as
    misreading path containment under the ADR 0021
    `$ALP_TOOLCHAIN_ROOT`-pointed-at-an-ancestor escape hatch (`$HOME` or
    `/opt`, ADR 0021's own documented bench/CI case), where the whole store
    root coincides with a `_scan_roots()` root and would otherwise
    swallow a genuinely independent, adjacent hand-install along with tan's
    own leaf. `sdk_root=None` (no checkout resolved, no pin to protect)
    excludes nothing -- matching `verified_store_dir(None)`, which returns
    `None` regardless of this function's answer in that case.

    Never raises: an unresolvable path reads as itself, matching
    `_candidates`'s own `except OSError` fallbacks."""
    manifest = _read_manifest(sdk_root)
    store_leaf: Path | None = None
    if manifest is not None:
        try:
            store_leaf = _toolchain_store_dir(manifest).resolve()
        except OSError:
            store_leaf = _toolchain_store_dir(manifest)
    for candidate in _candidates(_scan_roots()):
        entry = Path(candidate)
        try:
            resolved = entry.resolve()
        except OSError:
            resolved = entry
        if store_leaf is not None and resolved.is_relative_to(store_leaf):
            continue
        if _host_toolchain_is_usable(resolved):
            return True
    return False


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
