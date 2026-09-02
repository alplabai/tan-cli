# SPDX-License-Identifier: Apache-2.0
"""Pure decision logic for `tan bootstrap` -- no IO, no subprocesses.

Mirrors `crates/tan-core/src/bootstrap/` (its `manifest`/`prerequisites`/
`runtime`/`blocks`/`workspace_guard` split, collapsed into one module because
Python needs no visibility ceremony to keep them apart). The spawning half lives
in `tan.commands.bootstrap_cmd`.

The FACTS every step acts on -- tool lists, argv, pip specs, pins, env map,
hints -- come from `<sdkRoot>/metadata/bootstrap.json`, never from literals
here. That file is a live consumer contract (invariant **I-64**: *"tan (Rust,
cross-platform) has read the same facts since tan-cli PR #55 ... not merely an
INTENDED future consumer"*), and its own drift gate
(`scripts/check_bootstrap_manifest.py`) inspects only `bootstrap.sh` and
`bootstrap.ps1` -- so a hand-ported constant here desyncs silently. The
`fallback_facts` constants below are therefore stale-by-default and exist only
for an SDK predating the manifest.

**tan does not shell the SDK's bootstrap scripts.** Invariant **I-32** and
anti-pattern **22** of `docs/superpowers/specs/2026-07-29-tan-port-invariants.md`
record that giving a command an alp-sdk-script dependency it deliberately does
not have is a regression the parity gates cannot see; the Rust oracle's own
module doc says the same ("No `bash` anywhere -- native Windows is a first-class
host (#49), so the two scripts are the parity oracle for CONTROL FLOW and
message strings, not a runtime dependency"). The scripts are read as an oracle
for wording and step ORDER, and re-implemented.

Message strings and step order come from those two oracles. Their whitespace is
load-bearing twice over: a human reads the lines, and the envelope's issue
message is `" ".join(lines)`.
"""
from __future__ import annotations

from tan.core.os_class import infer_runtime_for_core_id

import json
import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from tan.core import artifact_provenance
from tan.core.artifact_provenance import ArtifactProvenance
from tan.core.timestamp import generated_at_iso

# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

#: The four hosts the flow distinguishes. Plain strings, not an enum: these ARE
#: the manifest's own `prerequisites.install` keys for three of the four, so a
#: separate enum would only need translating back.
LINUX = "linux"
MACOS = "macos"
#: `install.linux`'s package-manager keys (alp-sdk#1464 / tan-cli#760) -- see
#: `detect_linux_pm` for the detection order and why `pacman` has no constant
#: here at all.
LINUX_PM_APT = "apt"
LINUX_PM_DNF = "dnf"
WINDOWS = "windows"
OTHER = "other"


def detect_host_os(platform: str) -> str:
    """Classify a `sys.platform` value. A PARAMETER, not read from `sys` here,
    so both branches stay testable from either host (`HostOs::detect`)."""
    if platform.startswith("linux"):
        return LINUX
    if platform == "darwin":
        return MACOS
    if platform in ("win32", "cygwin"):
        return WINDOWS
    return OTHER


def os_label(host: str) -> str:
    """The POSIX script's `OS_LABEL`. `windows-bash` (git-bash/MSYS) has no
    counterpart: on Windows `tan bootstrap` runs the native flow, which prints
    the Python version instead of an OS label."""
    return "unknown" if host == OTHER else host


# ---------------------------------------------------------------------------
# Constants (the documented fallbacks -- stale by default; see the module doc)
# ---------------------------------------------------------------------------

#: FALLBACK Zephyr pin, used only when the SDK has no `metadata/bootstrap.json`.
ZEPHYR_VERSION = "v4.4.1"

#: FALLBACK west requirement -- a FLOOR, not a pin. Mirrors `west.pipSpec`.
WEST_REQUIREMENT = "west>=0.14.0"

#: Manifest path relative to the SDK checkout root.
BOOTSTRAP_MANIFEST_REL_PATH = "metadata/bootstrap.json"

#: The only `schemaVersion` this consumer understands
#: (`metadata/schemas/bootstrap-v1.schema.json` pins it `const: 1`).
BOOTSTRAP_MANIFEST_SCHEMA_VERSION = 1

#: `${SDK_ROOT}` / `${WORKSPACE_DIR}` substitution tokens.
TOKEN_SDK_ROOT = "${SDK_ROOT}"
TOKEN_WORKSPACE_DIR = "${WORKSPACE_DIR}"

#: The dedicated subdirectory the workspace-parent guard offers to relocate the
#: checkout into. NOT a detection heuristic -- the guard never keys off a
#: directory NAME (see `parent_needs_workspace_guard`); this is only the name tan
#: chooses for the new home it builds.
DEFAULT_WORKSPACE_DIR_NAME = "alp-workspace"

#: `tan doctor`'s wording, reused verbatim so the two agree
#: (`tan_core::build_readiness::YOCTO_HOST_DETAIL`).
YOCTO_HOST_DETAIL = "Yocto builds are Linux-only; use WSL2 or a Linux host/container."

#: The per-core `os:` value that takes a core OUT of play entirely.
OS_OFF = "off"


# ---------------------------------------------------------------------------
# Venv layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VenvLayout:
    """Where a venv keeps its executables and what they are called. The
    DIRECTORY names are manifest facts (`venv.posixBinDir`/`windowsBinDir`); the
    executable names are not in the manifest and live here."""

    bin_dir: str
    python: str
    west: str


def venv_layout(is_windows: bool) -> VenvLayout:
    if is_windows:
        return VenvLayout("Scripts", "python.exe", "west.exe")
    return VenvLayout("bin", "python", "west")


def venv_exe_names(bin_dir: str, facts: BootstrapFacts) -> VenvLayout:
    """The venv executable names for whichever bin dir actually WON. Both
    scripts pick the bin dir by which one exists, so a `Scripts/` venv created
    under git-bash keeps working on a POSIX host -- the names follow that
    choice, not the host."""
    return venv_layout(bin_dir == facts.venv_windows_bin_dir)


def python_candidates(is_windows: bool) -> list[list[str]]:
    """Host-interpreter candidates to probe, best first.

    Windows leads with the `py` launcher because a machine can have a perfectly
    good 3.12 with NO bare `python` on PATH, and the bare `python.exe` there is
    very often the Microsoft Store alias -- on PATH, prints nothing.
    """
    if is_windows:
        return [["py", "-3"], ["python"], ["python3"]]
    return [["python3"], ["python"]]


# ---------------------------------------------------------------------------
# Version parsing (tan_core::preflight)
# ---------------------------------------------------------------------------


def parse_version_tag(revision: str) -> str | None:
    """`"v4.4.1"` / `"4.4"` / `"v4.4.0-rc1"` -> `"4.4.1"` / `"4.4.0"` /
    `"4.4.0"`. `None` for a branch/SHA with no leading `MAJOR.MINOR`.

    Normalises the two shapes that would defeat the comparison: a missing PATCH
    reads as `0`, and a pre-release suffix is dropped from the patch component
    rather than failing the whole parse.
    """
    stripped = revision.strip()
    if stripped.startswith("v"):
        stripped = stripped[1:]
    parts = stripped.split(".")
    if len(parts) < 2:
        return None
    try:
        major = int(parts[0])
        minor = int(parts[1])
    except ValueError:
        return None
    patch = 0
    if len(parts) > 2:
        digits = re.match(r"\d+", parts[2])
        if digits is not None:
            patch = int(digits.group(0))
    return f"{major}.{minor}.{patch}"


def parse_zephyr_version_file(body: str) -> str | None:
    """`<ZEPHYR_BASE>/VERSION` -> `MAJOR.MINOR.PATCH`. `None` when MAJOR or
    MINOR is missing; PATCHLEVEL defaults to `0`."""
    major: int | None = None
    minor: int | None = None
    patch = 0
    for line in body.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        raw = value.strip()
        if key == "VERSION_MAJOR":
            major = _int_or_none(raw)
        elif key == "VERSION_MINOR":
            minor = _int_or_none(raw)
        elif key == "PATCHLEVEL":
            patch = _int_or_none(raw) or 0
    if major is None or minor is None:
        return None
    return f"{major}.{minor}.{patch}"


def _int_or_none(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def parse_west_zephyr_pin(body: str) -> str | None:
    """The Zephyr pin as `MAJOR.MINOR.PATCH` from a `west.yml` body: the
    `manifest.projects[]` entry named `zephyr`, whose `revision` is a tag.

    PyYAML when importable, else a two-key scan. tan ships no YAML dependency
    and the frozen binary is built without one, so the fallback is THE path on
    the shipped artifact -- the same bargain `presets_cmd._load_som_yaml` and
    `generate_cmd._board_sku` strike.
    """
    revision = _west_zephyr_revision(body)
    return parse_version_tag(revision) if revision else None


def _west_zephyr_revision(body: str) -> str | None:
    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError:
        return _scan_west_zephyr_revision(body)
    try:
        doc = yaml.safe_load(body)
    except Exception:  # noqa: BLE001 -- yaml.YAMLError and anything a loader raises
        return None
    if not isinstance(doc, dict):
        return None
    manifest = doc.get("manifest")
    if not isinstance(manifest, dict):
        return None
    projects = manifest.get("projects")
    if not isinstance(projects, list):
        return None
    for project in projects:
        if isinstance(project, dict) and project.get("name") == "zephyr":
            revision = project.get("revision")
            return revision if isinstance(revision, str) else None
    return None


def _scan_west_zephyr_revision(body: str) -> str | None:
    """The no-PyYAML reader: `revision:` inside the `- name: zephyr` list item.

    Answers one question, in either key order (`revision:` may precede
    `name:`), and stops at the next `- ` item so a later project's revision is
    never attributed to zephyr.
    """
    in_item = False
    is_zephyr = False
    revision: str | None = None
    for raw in body.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if is_zephyr and revision is not None:
                return revision
            in_item = True
            is_zephyr = False
            revision = None
            stripped = stripped[2:].strip()
        if not in_item:
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        cleaned = value.strip().strip("'\"")
        if key.strip() == "name" and cleaned == "zephyr":
            is_zephyr = True
        elif key.strip() == "revision":
            revision = cleaned
    return revision if is_zephyr else None


def resolve_zephyr_pin(west_yml: str | None, facts_version: str) -> str:
    """The ONE Zephyr pin the workspace-reuse test compares against.

    `west.yml` leads because `build`'s preflight `zephyrVersion` check reads
    exactly that file, and the operator re-runs `tan bootstrap` ON its warning
    (`tan build` does not do it for them -- tan-cli#427 settled that this port
    has no implicit bootstrap and is not getting one). With
    two pin sources an SDK bump made bootstrap ADOPT a workspace preflight
    simultaneously called stale -- a loop that never converges. Full
    `MAJOR.MINOR.PATCH`, never a `MAJOR.MINOR` truncation: that truncation is
    what let a `v4.4.0` tree satisfy a `v4.4.1` pin, silently.
    """
    if west_yml is not None:
        pinned = parse_west_zephyr_pin(west_yml)
        if pinned is not None:
            return pinned
    return parse_version_tag(facts_version) or ""


# ---------------------------------------------------------------------------
# `metadata/bootstrap.json`
# ---------------------------------------------------------------------------


class BootstrapManifestError(Exception):
    """A manifest that is present and unusable. NEVER a silent fallback: the
    absent-file case is the legacy path and falls back, but degrading here would
    re-introduce hand-ported behaviour against an SDK that explicitly declared
    something else."""


@dataclass(frozen=True)
class NativeLibHint:
    """A per-OS optional-native-libs hint. `note` is an ARRAY of lines, not one
    paragraph (the schema's `minItems: 1`): both scripts print one line per
    element, so an aligned `package -> API` mapping survives instead of
    collapsing into a ~380-char unwrapped line."""

    note: tuple[str, ...]
    command: str | None


@dataclass(frozen=True)
class Tokens:
    """`${SDK_ROOT}` / `${WORKSPACE_DIR}` substitution values.

    Applied at RENDER time, not baked in at load time, because workspace
    selection can repoint `workspace_dir` afterwards (adopting a compatible
    `$ZEPHYR_BASE` tree). `bootstrap.sh` re-substitutes on every
    `print_env_lines` call for exactly this reason; `bootstrap.ps1` binds once
    BEFORE selection and prints the pre-reuse path -- we follow bash.
    """

    sdk_root: str
    workspace_dir: str

    def apply(self, value: str) -> str:
        """One blind substitution pass (`tok()` / `Resolve-BootstrapToken`)."""
        return value.replace(TOKEN_SDK_ROOT, self.sdk_root).replace(
            TOKEN_WORKSPACE_DIR, self.workspace_dir
        )


@dataclass(frozen=True)
class BootstrapFacts:
    """The workspace-assembly facts, however obtained: parsed from the manifest,
    or reconstructed from the fallback constants for an SDK that predates it.

    ONE shape for both sources so no step branches on provenance -- only
    `from_manifest` records which it was, for the envelope's
    `factsFromManifest`.
    """

    zephyr_version: str
    zephyr_requirements_path: str
    venv_dir_name: str
    venv_posix_bin_dir: str
    venv_windows_bin_dir: str
    prerequisites_posix: tuple[str, ...]
    #: `prerequisites.macos`, or EMPTY when the manifest declares none -- which
    #: means "read `posix`", the behaviour of every SDK before v0.14.0. See
    #: `prerequisites` for why that fallback is load-bearing rather than tidy.
    prerequisites_macos: tuple[str, ...]
    prerequisites_windows: tuple[str, ...]
    python_min_version: tuple[int, int]
    #: `zephyr.pythonMinVersion` -- tan-cli#606: alp-sdk#1078 (Option A, the
    #: chosen path) added this Zephyr-SCOPED floor alongside the pre-existing
    #: host-universal `prerequisites.pythonMinVersion` above; the two are
    #: deliberately different numbers ("3.12" vs "3.10" as of this writing),
    #: not a duplicate. `None` when the manifest predates the key (every SDK
    #: before alp-sdk's zephyr-scoped-floor change) -- callers fall back to
    #: `doctor_cmd.ZEPHYR_PYTHON_FLOOR` in that case, same as before this field
    #: existed. Not `_require`d: unlike `prerequisites.pythonMinVersion`, an
    #: absent key here is a normal, expected shape, not a malformed manifest.
    zephyr_python_min_version: tuple[int, int] | None
    #: `prerequisites.install`, keyed `linux`/`macos`/`windows`. `macos`/
    #: `windows` map tool -> command directly. `linux` is one level deeper
    #: (alp-sdk#1464 / tan-cli#760): PACKAGE MANAGER -> tool -> command --
    #: `{"apt": {...}, "dnf": {...}}`, `dnf` a SUBSET of `apt`'s tools (see
    #: `normalize_linux_install`/`_fallback_install_commands`). Use
    #: `install_for_host`, never this field directly.
    install: dict[str, dict[str, str] | dict[str, dict[str, str]]]
    #: `artifactProvenance`, tool -> tier/licence/upstream page/size, already
    #: normalised (tan-cli#1066, alp-sdk#1574). TOP-LEVEL in the manifest, not
    #: under `prerequisites`, even though its keys are the prerequisite tool
    #: vocabulary. EMPTY for an SDK predating alp-sdk v0.16.0, and empty for a
    #: malformed block -- never an error: this is advisory metadata for a
    #: consumer's consent screen, and refusing to bootstrap over it would trade
    #: a working host for a display fact. See `tan.core.artifact_provenance`.
    artifact_provenance: dict[str, ArtifactProvenance]
    west_pip_spec: str
    west_init_args: tuple[str, ...]
    west_update_args: tuple[str, ...]
    west_export_args: tuple[str, ...]
    west_extension_guard: str
    pip_bootstrap_upgrade: tuple[str, ...]
    pip_sdk_extras: tuple[str, ...]
    pip_editable_install: str
    #: `env`, ordered, still tokened. A list of pairs because ORDER is what
    #: makes the rendered `export`/`$env:` lines come out in the manifest's
    #: declared order (serde's `preserve_order`; `json.loads` gives it free).
    env: tuple[tuple[str, str], ...]
    hint_linux: NativeLibHint
    hint_macos: NativeLibHint
    hint_windows: NativeLibHint
    manual_install_windows: tuple[str, ...]
    #: `manualInstallHints.posix.note` -- EMPTY when the manifest declares no
    #: `posix` key at all (tan-cli#495 defect 6). Optional on the wire, unlike
    #: `windows`: every SDK before alp-sdk v0.14.0 declares `windows` alone,
    #: and requiring it here would turn each of those into a hard
    #: `BootstrapManifestError` for everyone who runs `tan bootstrap` against
    #: an older SDK -- the same trap `prerequisites.install` and
    #: `prerequisites.windows` above already document. Empty renders nothing,
    #: which is exactly what those SDKs did before the field existed.
    manual_install_posix: tuple[str, ...]
    from_manifest: bool

    def venv_bin_dir(self, is_windows: bool) -> str:
        return self.venv_windows_bin_dir if is_windows else self.venv_posix_bin_dir

    def prerequisites(self, host: str) -> tuple[str, ...]:
        """The tool list for this host. The lists genuinely differ (`python` vs
        `python3`) and the manifest records that faithfully rather than
        unifying them -- so does this.

        Takes the HOST, not `is_windows`, since alp-sdk v0.14.0: that release
        added `xz` and `wget` to `prerequisites.posix` AND a separate
        `prerequisites.macos` that omits them. Keying off a bool hands macOS the
        POSIX list and refuses a stock macOS host -- which ships neither `wget`
        nor a standalone `xz` -- over tools the SDK does not ask macOS for.

        An EMPTY `prerequisites_macos` means the manifest declared none (every
        SDK before v0.14.0), and macOS then reads `posix` exactly as it always
        did. The fallback is the old behaviour, not a guess.
        """
        if host == WINDOWS:
            return self.prerequisites_windows
        if host == MACOS and self.prerequisites_macos:
            return self.prerequisites_macos
        return self.prerequisites_posix

    def install_for_host(self, host: str, linux_pm: str | None = None) -> dict[str, str]:
        """THE one place the manifest's `linux`/`macos`/`windows` install keying
        is reconciled with `prerequisites`' `posix`/`windows` tool-list keying.
        Callers resolve once, by host, and hand the resolved map down -- so no
        caller can look a tool up in the wrong OS's table (a POSIX refusal on
        macOS getting Linux's `apt-get` lines).

        `OTHER` (a POSIX host that is neither Linux nor macOS) has no manifest
        entry and is not going to grow one: every tool there reports
        `command: null`. The alternatives are both worse than the `null` -- a
        throw, or handing a BSD user a `brew install` line.

        `linux_pm` (alp-sdk#1464 / tan-cli#760) is the extra hop `host ==
        LINUX` needs: `self.install[LINUX]` is package-manager -> tool ->
        command, so a caller must say WHICH sub-map it wants. `None` (the
        default) is an EMPTY map, never a guess -- a caller with no confirmed
        package manager (`detect_linux_pm`) gets `command: null` for every
        tool, not one PM's data served for another's.
        """
        if host == LINUX:
            if linux_pm is None:
                return {}
            sub = self.install.get(LINUX, {})
            node = sub.get(linux_pm) if isinstance(sub, dict) else None
            return dict(node) if isinstance(node, dict) else {}
        return self.install.get(host, {})

    def native_lib_hint(self, host: str) -> NativeLibHint | None:
        """`None` for `OTHER` -- `bootstrap.sh`'s `*)` arm prints no hint, just
        the not-detected line."""
        return {
            LINUX: self.hint_linux,
            MACOS: self.hint_macos,
            WINDOWS: self.hint_windows,
        }.get(host)


def _str_list(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: `{what}` is not a list of strings"
        )
    return tuple(value)


def _require(doc: Any, key: str, kind: type, what: str) -> Any:
    if not isinstance(doc, dict):
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: `{what}` is not an object"
        )
    value = doc.get(key)
    if not isinstance(value, kind):
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: missing or mistyped "
            f"`{what}.{key}`"
        )
    return value


def _hint(doc: Any, key: str) -> NativeLibHint:
    node = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(node, dict):
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: missing or mistyped "
            f"`nativeLibHints.{key}`"
        )
    command = node.get("command")
    return NativeLibHint(
        note=_str_list(node.get("note"), f"nativeLibHints.{key}.note"),
        command=command if isinstance(command, str) else None,
    )


def parse_min_version(raw: str) -> tuple[int, int] | None:
    """`"3.10"` -> `(3, 10)`."""
    major, sep, minor = raw.strip().partition(".")
    if not sep:
        return None
    try:
        return int(major.strip()), int(minor.strip())
    except ValueError:
        return None


def is_plain_relative(raw: str) -> bool:
    """A relative path with no `..`, no root and no drive letter -- the shape a
    manifest-supplied directory name must have before it is joined onto the
    workspace (`tan_core::path_guard::is_plain_relative`)."""
    if not raw or raw != raw.strip():
        return False
    if os.path.isabs(raw) or ntpath_isabs(raw):
        return False
    parts = re.split(r"[\\/]", raw)
    return all(part not in ("", ".", "..") for part in parts)


def ntpath_isabs(raw: str) -> bool:
    """Windows-shaped absoluteness (`C:\\x`, `\\\\server\\share`, `\\x`),
    checked on EVERY host: the manifest is authored once and consumed on all
    three, so a POSIX `os.path.isabs` alone would wave `C:\\Windows` through."""
    import ntpath  # noqa: PLC0415 -- one call site

    return ntpath.isabs(raw) or bool(re.match(r"^[A-Za-z]:", raw))


def parse_bootstrap_manifest(text: str) -> BootstrapFacts:
    """Parse `metadata/bootstrap.json`. Pure -- the caller reads the file and
    decides what an absent file means (see `fallback_facts`).

    `schemaVersion` is read on its own FIRST: a future manifest may legitimately
    reshape fields this consumer would otherwise fail on, and the user deserves
    "unsupported version N", not "missing field `foo`".
    """
    try:
        doc = json.loads(text)
    except ValueError as err:
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: {err}"
        ) from err
    if not isinstance(doc, dict):
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: not a JSON object"
        )
    version = doc.get("schemaVersion")
    # `bool` excluded explicitly: `True == 1` in Python, so `schemaVersion: true`
    # would pass an `== 1` test that serde's `as_u64()` rejects.
    if not isinstance(version, int) or isinstance(version, bool):
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: missing `schemaVersion`"
        )
    if version != BOOTSTRAP_MANIFEST_SCHEMA_VERSION:
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} declares schemaVersion {version}, but this "
            f"`tan` supports only {BOOTSTRAP_MANIFEST_SCHEMA_VERSION}. Update `tan`, or "
            f"pin an SDK whose bootstrap manifest this version understands."
        )

    zephyr = doc.get("zephyr")
    venv = doc.get("venv")
    prerequisites = doc.get("prerequisites")
    west = doc.get("west")
    pip = doc.get("pip")
    env = doc.get("env")
    hints = doc.get("nativeLibHints")
    manual = doc.get("manualInstallHints")

    min_raw = _require(prerequisites, "pythonMinVersion", str, "prerequisites")
    python_min_version = parse_min_version(min_raw)
    if python_min_version is None:
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: "
            f"prerequisites.pythonMinVersion `{min_raw}` is not MAJOR.MINOR"
        )

    # OPTIONAL, unlike `prerequisites.pythonMinVersion` above: absent is the
    # normal shape for any manifest predating alp-sdk#1078's zephyr-scoped
    # floor, not a malformed one -- so a present-but-unparseable value still
    # hard-fails (it was deliberately authored), but a missing key does not.
    zephyr_min_raw = zephyr.get("pythonMinVersion") if isinstance(zephyr, dict) else None
    zephyr_python_min_version: tuple[int, int] | None = None
    if zephyr_min_raw is not None:
        if not isinstance(zephyr_min_raw, str):
            raise BootstrapManifestError(
                f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: "
                f"zephyr.pythonMinVersion is not a string"
            )
        zephyr_python_min_version = parse_min_version(zephyr_min_raw)
        if zephyr_python_min_version is None:
            raise BootstrapManifestError(
                f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: "
                f"zephyr.pythonMinVersion `{zephyr_min_raw}` is not MAJOR.MINOR"
            )

    dir_name = _require(venv, "dirName", str, "venv")
    # `venv.dirName` joins straight onto `workspace_dir` and the join's result is
    # later handed to `rmtree` when a stale venv is recreated -- an unvalidated
    # `..`-bearing or absolute value would let the manifest name an arbitrary
    # removal target outside the workspace. Rejected at this one seam, which
    # every consumer of the name reads through.
    if not is_plain_relative(dir_name):
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: venv.dirName "
            f"`{dir_name}` is not a plain relative path"
        )

    if not isinstance(env, dict):
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: missing or mistyped `env`"
        )
    manual_node = manual.get("windows") if isinstance(manual, dict) else None
    posix_node = manual.get("posix") if isinstance(manual, dict) else None
    if not isinstance(manual_node, dict):
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: missing or mistyped "
            f"`manualInstallHints.windows`"
        )

    return BootstrapFacts(
        zephyr_version=_require(zephyr, "version", str, "zephyr"),
        zephyr_requirements_path=_require(zephyr, "requirementsPath", str, "zephyr"),
        venv_dir_name=dir_name,
        venv_posix_bin_dir=_require(venv, "posixBinDir", str, "venv"),
        venv_windows_bin_dir=_require(venv, "windowsBinDir", str, "venv"),
        prerequisites_posix=_str_list(prerequisites.get("posix"), "prerequisites.posix"),
        # OPTIONAL on the wire: absent means "use `posix`", which is every SDK
        # before v0.14.0. Required here, it would turn each of those into a hard
        # ValidationFailure for every `tan bootstrap` run against such an SDK.
        prerequisites_macos=_str_list(prerequisites.get("macos", []), "prerequisites.macos"),
        prerequisites_windows=_str_list(
            prerequisites.get("windows"), "prerequisites.windows"
        ),
        python_min_version=python_min_version,
        zephyr_python_min_version=zephyr_python_min_version,
        install=_resolve_install_commands(prerequisites.get("install")),
        # Read off `doc`, not `prerequisites`: alp-sdk#1574 put the block at
        # the manifest's TOP level. Never `_require`d -- see the field comment.
        artifact_provenance=artifact_provenance.parse_table(
            doc.get(artifact_provenance.BLOCK_KEY)
        ),
        west_pip_spec=_require(west, "pipSpec", str, "west"),
        west_init_args=_str_list(_require(west, "initArgs", list, "west"), "west.initArgs"),
        west_update_args=_str_list(
            _require(west, "updateArgs", list, "west"), "west.updateArgs"
        ),
        west_export_args=_str_list(
            _require(west, "exportArgs", list, "west"), "west.exportArgs"
        ),
        west_extension_guard=_require(west, "extensionGuardCommand", str, "west"),
        pip_bootstrap_upgrade=_str_list(
            _require(pip, "bootstrapUpgrade", list, "pip"), "pip.bootstrapUpgrade"
        ),
        pip_sdk_extras=_str_list(_require(pip, "sdkExtras", list, "pip"), "pip.sdkExtras"),
        pip_editable_install=_require(pip, "editableInstall", str, "pip"),
        # A non-string value degrades to `""` rather than failing the manifest,
        # matching serde's `v.as_str().unwrap_or_default()`.
        env=tuple((k, v if isinstance(v, str) else "") for k, v in env.items()),
        hint_linux=_hint(hints, LINUX),
        hint_macos=_hint(hints, MACOS),
        hint_windows=_hint(hints, WINDOWS),
        manual_install_windows=_str_list(
            manual_node.get("note"), "manualInstallHints.windows.note"
        ),
        # OPTIONAL on the wire, unlike `windows` just above: a missing or
        # non-dict `posix` is an empty list, never an error (see the field's
        # own comment on `BootstrapFacts`).
        manual_install_posix=(
            _str_list(posix_node.get("note"), "manualInstallHints.posix.note")
            if isinstance(posix_node, dict)
            else ()
        ),
        from_manifest=True,
    )


def _fallback_install_commands() -> dict[str, dict[str, str] | dict[str, dict[str, str]]]:
    """The install one-liners as `metadata/bootstrap.json` carries them.

    Two callers: the whole-manifest fallback, and `_resolve_install_commands`'s
    gap-fill for a manifest predating alp-sdk#959 (which carried no `install`
    key at all). Note `ninja`'s PACKAGE name differs from the binary name --
    which is the whole argument for carrying these as data rather than guessing.

    `LINUX` is re-pinned to alp-sdk `dev` @ `7a419865` (tan-cli#760's second
    half / alp-sdk#1464+#1471): package-manager-keyed now, matching
    `BootstrapFacts.install`'s own nested shape, not the flat tool -> command
    map this table carried before. `dnf` has no `ninja` entry and there is no
    `pacman` key at all -- both deliberate DATA (see `detect_linux_pm`), not
    gaps to fill in later.
    """
    return {
        LINUX: {
            LINUX_PM_APT: {
                "git": "sudo apt-get install -y git",
                "cmake": "sudo apt-get install -y cmake",
                "python3": "sudo apt-get install -y python3",
                "ninja": "sudo apt-get install -y ninja-build",
                # `xz`/`wget` joined `prerequisites.posix` at alp-sdk v0.14.0.
                # Same package-name-differs-from-binary-name point as `ninja`:
                # the binary is `xz`, the package is `xz-utils`.
                "xz": "sudo apt-get install -y xz-utils",
                "wget": "sudo apt-get install -y wget",
            },
            LINUX_PM_DNF: {
                "git": "sudo dnf install -y git",
                "cmake": "sudo dnf install -y cmake",
                "python3": "sudo dnf install -y python3",
                # No `ninja` -- see this function's own docstring.
                "xz": "sudo dnf install -y xz",
                "wget": "sudo dnf install -y wget",
            },
        },
        MACOS: {
            "git": "brew install git",
            "cmake": "brew install cmake",
            "python3": "brew install python3",
            "ninja": "brew install ninja",
            # Present even though `prerequisites.macos` does NOT list `xz`/`wget`
            # -- the manifest declares these commands for macOS regardless, and
            # this table is byte-pinned to it. A user who needs them (an SDK
            # predating `prerequisites.macos`, so macOS reads the POSIX list)
            # gets the `brew` line rather than Linux's `apt-get`.
            "xz": "brew install xz",
            "wget": "brew install wget",
        },
        WINDOWS: {
            "git": "winget install -e --id Git.Git",
            "cmake": "winget install -e --id Kitware.CMake",
            "python": "winget install -e --id Python.Python.3.12",
            "ninja": "winget install -e --id Ninja-build.Ninja",
            # NOT in `prerequisites.windows` -- the manifest declares an install
            # command for a tool it does not require. `7z` is what unpacks the
            # Zephyr SDK's native-Windows hosttools archive, so a Windows user
            # who takes that optional step needs the line; bootstrap.ps1 itself
            # does not. Carried because this table is byte-pinned to the
            # manifest's `prerequisites.install.windows`, not to its tool LIST.
            "7zip": "winget install -e --id 7zip.7zip",
        },
    }


def normalize_linux_install(raw: Any) -> dict[str, dict[str, str]]:
    """`prerequisites.install.linux`, canonicalised to PACKAGE MANAGER -> tool
    -> command, whichever of the two shapes alp-sdk declared it in.

    **New shape** (alp-sdk#1464, `dev` @ `7a419865` on): `{"apt": {tool:
    command}, "dnf": {tool: command}}` -- detected structurally, by every
    top-level value being a dict, not by key name, so a package manager this
    parser has never heard of is still readable rather than silently dropped.

    **Legacy shape** (every manifest before alp-sdk#1471): a FLAT tool ->
    command map, unconditionally Debian's -- that key had no other content
    until #1464 gave it a package-manager dimension at all. Read here AS
    `apt`'s sub-map (design decision (4)): a NEW tan against an OLD
    `--sdk-root` still works on a real apt host exactly as it always did, and
    `select_linux_install` never hands the same data out to a dnf/pacman/other
    caller -- read what was always there, under the key it was implicitly
    for, and let `detect_linux_pm` decide who may see it.

    Empty/absent/malformed input returns `{}` -- callers fall back to
    `_fallback_install_commands()[LINUX]`, matching every other per-OS gap in
    `_resolve_install_commands`.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    if all(isinstance(v, dict) for v in raw.values()):
        out: dict[str, dict[str, str]] = {}
        for pm, tools in raw.items():
            if not isinstance(pm, str) or not isinstance(tools, dict):
                continue
            clean = {k: v for k, v in tools.items() if isinstance(k, str) and isinstance(v, str)}
            if clean:
                out[pm] = clean
        return out
    # Legacy flat shape -- read as `apt`'s sub-map (see the docstring above).
    clean = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    return {LINUX_PM_APT: clean} if clean else {}


def select_linux_install(normalized: dict[str, dict[str, str]], pm: str | None) -> dict[str, str]:
    """The tool -> command map for ONE package manager, out of
    `normalize_linux_install`'s output. `pm=None` -- no package manager
    confirmed on this host (`detect_linux_pm` found neither `apt-get` nor
    `dnf`, or found a third one this manifest ships no sub-map for, e.g.
    Arch's `pacman`) -- is `{}`, never a different PM's data: the whole point
    of keying `install.linux` by package manager is that one PM's shape must
    never stand in for another's."""
    if pm is None:
        return {}
    sub = normalized.get(pm)
    return dict(sub) if isinstance(sub, dict) else {}


def detect_linux_pm(available: Callable[[str], bool]) -> str | None:
    """Which package-manager sub-map to read under `install.linux`
    (alp-sdk#1464 / tan-cli#760) -- `apt` checked before `dnf`, `pacman`
    never probed.

    **Order and fallthrough MUST agree with alp-sdk's own two detectors** --
    `scripts/bootstrap.sh`'s `LINUX_PM` block and `scripts/alp_cli/doctor.py`'s
    `_prereq_linux_pm()` -- both probe `apt-get` before `dnf`, neither probes
    `pacman`, both fall through to a host-neutral degrade when neither
    resolves. A third detector that disagrees with those two is a future
    drift bug: keep this order in lockstep with those two by hand; there is
    no shared gate across the two repos that would.

    `pacman` is deliberately never probed: `install.linux` ships no `pacman`
    sub-map at all (an unattended `pacman -Sy` risks a partial upgrade) --
    detecting it would only ever resolve to the same `None` below.

    Pure: `available` is injected (`on_path(binary) is not None`), matching
    every other PATH check in this module -- see `_confirmed`.
    """
    if available("apt-get"):
        return LINUX_PM_APT
    if available("dnf"):
        return LINUX_PM_DNF
    return None


def _resolve_install_commands(
    declared: Any,
) -> dict[str, dict[str, str] | dict[str, dict[str, str]]]:
    """`prerequisites.install` as parsed, with each EMPTY per-OS map replaced by
    the fallback's.

    PER OS, not whole-subtree: `install: {}` -- or one carrying `windows` alone
    -- is indistinguishable from an absent key after parsing, and filling only
    the whole subtree would hand the absent OSes empty maps. On Windows that is
    the real pre-#959 loss: all four `winget` lines vanish. Emptiness is the
    signal because a SERVED OS map is never legitimately empty (the producer's
    schema requires its keys to equal `prerequisites.<os>`).

    Degrade, do not refuse: every shape handled here is out of contract today,
    and a `ValidationFailure` on a manifest field fails the whole
    `tan bootstrap` run -- which is the step a customer has to get through
    before `tan build` or `tan run` can work at all, since neither of those
    bootstraps implicitly (tan-cli#427).

    `LINUX` is handled separately (tan-cli#760's second half):
    `normalize_linux_install` reconciles both shapes `install.linux` has ever
    had into one PM-keyed map, so `BootstrapFacts.install[LINUX]` is ALWAYS
    `{pm: {tool: command}}`.
    """
    fallback = _fallback_install_commands()
    if not isinstance(declared, dict):
        return fallback
    out: dict[str, dict[str, str] | dict[str, dict[str, str]]] = {}
    for host in (MACOS, WINDOWS):
        node = declared.get(host)
        clean = (
            {k: v for k, v in node.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(node, dict)
            else {}
        )
        out[host] = clean or fallback[host]
    out[LINUX] = normalize_linux_install(declared.get(LINUX)) or fallback[LINUX]
    return out


def fallback_facts(min_python: tuple[int, int]) -> BootstrapFacts:
    """The hand-ported facts, for an SDK with no `metadata/bootstrap.json`.

    LAST-KNOWN values transcribed from the pre-#917 scripts. The manifest wins
    outright when present, so an SDK-side pin bump reaches tan without a tan
    release; `check_bootstrap_manifest.py` does not scan this file, so treat
    every literal below as stale-by-default.
    """
    return BootstrapFacts(
        zephyr_version=ZEPHYR_VERSION,
        zephyr_requirements_path="zephyr/scripts/requirements.txt",
        venv_dir_name=".venv",
        venv_posix_bin_dir="bin",
        venv_windows_bin_dir="Scripts",
        # `ninja` is POSIX too, not Windows-only: Zephyr picks Ninja as its
        # default CMake generator on every host, so a POSIX box without it fails
        # `west build` with a CMake error naming nothing useful. `xz`/`wget`
        # joined the list at alp-sdk v0.14.0, which also split `macos` out
        # WITHOUT them -- a stock macOS host has neither.
        prerequisites_posix=("git", "cmake", "python3", "ninja", "xz", "wget"),
        prerequisites_macos=("git", "cmake", "python3", "ninja"),
        prerequisites_windows=("git", "cmake", "python", "ninja"),
        python_min_version=min_python,
        # Transcribed from `contract/fixtures/bootstrap/manifest.json`'s
        # `zephyr.pythonMinVersion`, same as `zephyr_version` above -- held to
        # the vendored fixture by
        # `test_the_fallback_constants_match_the_real_manifest_field_for_field`,
        # NOT derived from `min_python` (the caller's `prerequisites.
        # pythonMinVersion`): the two keys are independently-declared numbers
        # in the real manifest ("3.12" vs "3.10" as of this writing) and must
        # not be conflated here either.
        zephyr_python_min_version=(3, 12),
        install=_fallback_install_commands(),
        # No fallback provenance, deliberately: an SDK with no manifest at all
        # publishes no licensing claim, and transcribing one here would be tan
        # ASSERTING a licence nobody handed it -- the one thing the `null`
        # spelling exists to avoid (tan-cli#1066).
        artifact_provenance={},
        west_pip_spec=WEST_REQUIREMENT,
        west_init_args=("init", "-l"),
        west_update_args=("update", "--narrow", "-o=--depth=1"),
        west_export_args=("zephyr-export",),
        west_extension_guard="alp-migrate",
        pip_bootstrap_upgrade=("pip", "wheel"),
        pip_sdk_extras=("jsonschema", "imgtool"),
        pip_editable_install=TOKEN_SDK_ROOT,
        env=(
            ("ZEPHYR_BASE", f"{TOKEN_WORKSPACE_DIR}/zephyr"),
            ("ZEPHYR_TOOLCHAIN_VARIANT", "zephyr"),
        ),
        # The note arrays are transcribed VERBATIM, intra-line padding included:
        # the manifest carries the `->` column alignment, and re-wrapping here
        # would make the fallback print differently from the manifest path.
        hint_linux=NativeLibHint(
            note=(
                "libmosquitto-dev  -> alp_mqtt_* (cleartext + TLS)",
                "libasound2-dev    -> alp_audio_*",
                "libssl-dev        -> alp_hash_* / alp_aead_* / alp_random_bytes",
            ),
            command=(
                "sudo apt-get install -y libmosquitto-dev libasound2-dev libssl-dev "
                "pkg-config"
            ),
        ),
        hint_macos=NativeLibHint(
            note=(
                "Equivalents via Homebrew:",
                "mosquitto  -> alp_mqtt_* (cleartext + TLS)",
                "macOS uses CoreAudio rather than ALSA, so the Yocto audio backend "
                "doesn't apply on macOS hosts.",
                "OpenSSL ships with macOS.",
            ),
            command="brew install mosquitto pkg-config",
        ),
        hint_windows=NativeLibHint(
            note=(
                "Under Git Bash / MSYS2 the Yocto-side backends aren't intended to run "
                "-- the canonical use is WSL2 + Ubuntu with the linux command above; "
                "skip this step on native Windows.",
            ),
            command=None,
        ),
        manual_install_windows=(
            "The Zephyr SDK (`west sdk install`) is a separate, manual, one-time "
            "install on native Windows -- not auto-installed by bootstrap.ps1. It is "
            "the one every Zephyr-on-M customer needs: it provides the "
            "`arm-zephyr-eabi` cross toolchain the real-silicon build (`west build` / "
            "`west flash`) actually uses. Run it from your west workspace's top-level "
            "directory -- the alp-sdk checkout's parent directory -- after this script "
            "completes.",
            "7-Zip must already be on PATH before running `west sdk install` on native "
            "Windows: west delegates .7z extraction to patoolib, which shells out to "
            "an external 7z/7za/7zr/7zz/7zzs/unar binary and has no pure-Python "
            "fallback.",
            "The Zephyr SDK's native-Windows hosttools bundle ships neither `dtc` nor "
            "`gperf` (verified: `hosttools_windows-x86_64.7z`, sdk-ng v1.0.1, "
            "sha256-checked against upstream's own sha256.sum -- 1486 entries via "
            "`7z l`, zero dtc/gperf/device-tree matches -- while the equivalent Linux "
            "hosttools archive does ship `dtc`). Both are separate, manual installs on "
            "native Windows if you need them (see docs/cross-platform-setup.md); "
            "WARN-only in `alp doctor` (`_check_dtc` / `_check_gperf`) -- not required "
            "by bootstrap.ps1.",
            "The Arm GNU Toolchain (`arm-none-eabi-gcc`) is a SEPARATE manual install, "
            "needed by three opt-in paths -- rebuilding the GD32 bridge firmware "
            "(custom-carrier bring-up or bridge recovery), building the CC3501E bridge "
            "firmware's silicon-free stub target (its production image builds with TI "
            "ticlang, not this toolchain), or hand-writing bare-metal firmware for a "
            "real M-class core -- most customers never touch any of them, since the "
            "GD32G553 ships pre-flashed by Alp Lab (rebuilding it is optional and "
            "fully open, see docs/gd32-bridge.md). Installer EXE: "
            "https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads (tick "
            "'Add path to environment variable' during install).",
            "native_sim / Yocto need WSL2 (docs/cross-platform-setup.md section 5).",
        ),
        # Transcribed from `contract/fixtures/bootstrap/manifest.json`, the
        # vendored producer output every fallback field is held against by
        # `test_the_fallback_constants_match_the_real_manifest_field_for_field`
        # -- this one included, since tan-cli#585 re-vendored the fixture. It
        # used to carry ONE deliberate departure: the stale fixture ended this
        # first note on `tan sdk switch`, which is in
        # `sdk_cmd.NOT_PORTED_SDK_SUBCOMMANDS` and REFUSES in this build, so
        # transcribing it verbatim failed `test_sdk_onboarding_dead_end.py`.
        # The refresh removed the conflict at its source; nothing here is
        # exempt from the field-for-field check any more.
        manual_install_posix=(
            "The Zephyr SDK (`west sdk install`) is a separate, manual, one-time install on "
            "Linux/macOS -- not auto-installed by bootstrap.sh. It is the one every Zephyr-on-M "
            "customer on Linux or Apple Silicon macOS needs (Intel Mac excepted -- see below): it "
            "provides the `arm-zephyr-eabi` cross toolchain the real-silicon build (`west build` / "
            "`west flash`) actually uses. Run it from your west workspace's "
            "top-level directory -- the alp-sdk checkout's parent directory -- after this script "
            "completes, e.g. `west sdk install --gnu-toolchains arm-zephyr-eabi --no-hosttools "
            "--install-dir \"$PWD/zephyr-sdk\"`; see docs/getting-started.md for the full one-liner "
            "and select the checkout with `tan build --sdk-root <path>` or `.alp/sdk-path`. On an "
            "Intel Mac this command has no target to install: the pinned SDK ships `macos-aarch64` "
            "only (no `macos-x86_64`), so real-silicon Zephyr builds need a Linux host instead -- "
            "`native_sim` and this bootstrap step are unaffected.",
            "The Arm GNU Toolchain (`arm-none-eabi-gcc`) is a SEPARATE manual install, needed by "
            "three opt-in paths -- rebuilding the GD32 bridge firmware (custom-carrier bring-up or"
            " bridge recovery), building the CC3501E bridge firmware's silicon-free stub target "
            "(its production image builds with TI ticlang, not this toolchain), or hand-writing "
            "bare-metal firmware for a real M-class core -- most customers never touch any of "
            "them, since the GD32G553 ships pre-flashed by Alp Lab (rebuilding it is optional and "
            "fully open, see docs/gd32-bridge.md). See docs/cross-platform-setup.md section 2.3 "
            "(Linux) / 3.4 (macOS) for the apt/brew/curl install.",
            "`west sdk install` may print \"could not find a 'file' executable, falling back to "
            "guess mime type by file extension\" -- patool's extension-based fallback works fine "
            "without it; this is WARN-only, not a bootstrap.sh prerequisite. Install `file` "
            "(`apt-get install -y file` / it ships by default on macOS) only to silence the "
            "message.",
        ),
        from_manifest=False,
    )


# ---------------------------------------------------------------------------
# The prerequisite gate's PURE half: what a refusal says
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissingPrerequisite:
    """One missing host prerequisite, in the form a consumer can act on.

    `command` is `None` -- never prose -- for a tool the manifest lists no
    command for: a consumer renders this field as something it can RUN, and
    prose in a runnable-command field is a button that fails. The generic advice
    belongs in the printed line (`hint_line`) only.

    `provenance` (tan-cli#1066) is the same manifest's `artifactProvenance`
    entry for this tool: the tier, licence, upstream page and size alp-sdk
    v0.16.0 publishes (alplabai/alp-sdk#1574). It rides on the ENTRY rather
    than as a sibling map on `data` because the join is by `tool`, an identity
    the entry already carries -- a consumer must not have to re-join two
    arrays, and a consumer that resolved its own SDK checkout must not join
    tan's commands against a DIFFERENT checkout's provenance (a real risk:
    `tan bootstrap` relocates the alp-sdk checkout to
    `<parent>/alp-workspace/<name>`). Defaults to `UNKNOWN` -- all four fields
    `null` -- for every caller with no table to hand, which is exactly what a
    tool with no entry reports; see `tan.core.artifact_provenance`.
    """

    tool: str
    command: str | None
    provenance: ArtifactProvenance = artifact_provenance.UNKNOWN

    def as_dict(self) -> dict[str, str | int | None]:
        """`{tool, command}` plus provenance's four keys, ALWAYS all six: the
        provenance keys are never omitted and never defaulted, so
        `contract/doctor-data-keys.json` can declare one item shape for every
        SDK (an older one simply reports `null`s)."""
        return {
            "tool": self.tool,
            "command": self.command,
            **self.provenance.as_dict(),
        }


@dataclass(frozen=True)
class PrereqFailure:
    """A refused prerequisite gate: the `bootstrap.<code>` suffix, the message
    lines verbatim, and the structured per-tool form of them.

    The structured half exists because the envelope's issue message is
    `" ".join(lines)` and an install command contains the same spaces the join
    used -- the split is not recoverable, so a consumer that wants "which tool,
    which command" must be HANDED it (alp-sdk-vscode#347 proved that parse dead
    and deleted it).

    The code is per-refusal rather than one blanket `prerequisites-missing`
    because the Python-floor refusals have no missing TOOL at all -- a
    `{tool, command}` pair cannot represent "the Python you have is 3.10".
    """

    code: str
    lines: tuple[str, ...]
    missing: tuple[MissingPrerequisite, ...] = ()


#: Program names this guard refuses to accept as "the confirmed installer",
#: even when `available` says yes -- tan-cli#760 review, MINOR 1. Every one
#: of these is a universally-present WRAPPER that can front an absent real
#: installer without this guard ever seeing it: `env FOO=bar apt-get ...` and
#: `sh -c '...'` both resolve their OWN leading token to one of these, so
#: `available("env")` (true on nearly every host) would confirm a command
#: whose real dependency was never checked at all. Harmless against today's
#: manifest (none of the six shipped commands use a wrapper); load-bearing
#: the moment landing (b) adds a real per-distro command table.
_OPAQUE_WRAPPER_BINARIES = frozenset(
    {"env", "sh", "bash", "dash", "zsh", "ksh", "eval", "exec"}
)

#: Shell composition this guard cannot see past -- `apt-get update && apt-get
#: install -y cmake` would confirm only the FIRST command, then hand out the
#: whole two-command line as if the second had been checked too. Refuse
#: outright on any of these rather than half-confirm (tan-cli#760 review,
#: MINOR 1). `"&"` alone (a single backgrounding operator, e.g. `apt-get
#: install -y cmake & rm -rf /`) already contains `"&&"` as a substring, so
#: listing both would be redundant; `"|"` likewise already covers `"||"`.
#: `"\n"` closes the same class of gap a real shell would hit at a bare
#: newline (`"apt-get update\napt-get install -y cmake"`) -- tan-cli#760
#: review round 3, NIT: both were measured to slip past the pre-round-3 list.
#: None of today's six manifest commands use any of these.
_SHELL_METACHARACTERS = ("&", ";", "|", "`", "$(", "\n")


def leading_binary(command: str) -> str:
    """The program name a shell would actually invoke for one of the
    manifest's install one-liners -- stripping a literal leading `sudo `
    exactly as `--fix` (`doctor_cmd.run_fix`) does before it ever spawns
    anything, and parsing the rest with `shlex.split`, the SAME parser
    `run_fix` itself calls on the (sudo-stripped) command before resolving
    `argv[0]` -- so whatever this returns is provably the same token
    `run_fix` would try to resolve, quoting included, not merely a similar
    one (tan-cli#760 review, MINOR 2: `str.split` disagreed with `run_fix`'s
    `shlex.split` on a quoted path). `""` for a blank command, or one
    `shlex` cannot parse at all (an unterminated quote) -- nothing to
    confirm either way, and callers treat an empty name as "not available"
    rather than guessing at a half-token."""
    stripped = command.strip()
    if stripped.startswith("sudo "):
        stripped = stripped[len("sudo ") :].lstrip()
    if not stripped:
        return ""
    try:
        parts = shlex.split(stripped)
    except ValueError:
        return ""
    return parts[0] if parts else ""


def _confirmed(command: str, available: Callable[[str], bool]) -> bool:
    """Whether `command` -- one manifest install one-liner -- is safe to hand
    a consumer as a real, runnable command on THIS host. The tan-cli#760
    guard's one decision point, shared by `confirmed_install_commands`
    (dict shape) and `confirm_missing` (`MissingPrerequisite` tuple shape)
    below -- there is exactly one place that decides this, even though there
    are now two entry points for the two shapes a caller needs it in.

    Refuses outright, never half-confirms, on a shell metacharacter
    (`_SHELL_METACHARACTERS`) or an opaque wrapper leading binary
    (`_OPAQUE_WRAPPER_BINARIES`) -- tan-cli#760 review MINOR 1.

    When the command is `sudo`-prefixed, `sudo` itself is confirmed too, not
    only the program after it: a stock `debian:12` image has `apt-get` but
    does NOT ship `sudo` by default, so confirming `apt-get` alone would
    still hand out a command that fails with `sudo: not found`
    (tan-cli#760 review, MINOR 3).

    This still is not an exhaustive runnability proof -- a PATH-shadowing
    binary, a permissions error, an environment quirk are all beyond what a
    PATH lookup can promise. What it DOES guarantee: every program name this
    command would need to spawn is confirmed present, or the command is
    refused rather than handed out as if it were checked."""
    if any(meta in command for meta in _SHELL_METACHARACTERS):
        return False
    stripped = command.strip()
    needs_sudo = stripped.startswith("sudo ")
    binary = leading_binary(command)
    if not binary or binary in _OPAQUE_WRAPPER_BINARIES:
        return False
    if needs_sudo and not available("sudo"):
        return False
    return available(binary)


def confirmed_install_commands(
    install: dict[str, str], available: Callable[[str], bool]
) -> dict[str, str]:
    """`install`, keeping only the entries `_confirmed` (above) accepts on
    THIS host -- the tan-cli#760 guard's dict-shaped entry point.

    Every `MissingPrerequisite.command` this port emits is built by looking a
    tool up in a dict shaped exactly like `install` (`_structured_missing`,
    `doctor_cmd.prerequisites_check`) or, for a `PrereqFailure` already built
    (`posix_venv_unusable()`), via `confirm_missing`'s tuple-shaped twin
    below -- `_confirmed` is the one decision both apply. Measured on
    `fedora:42`/`archlinux:latest`/`rockylinux:9`: alp-sdk's Linux table is
    six `sudo apt-get install -y ...` lines and none of those hosts has
    `apt-get` on PATH, so every entry is dropped here and the tool downgrades
    to `command: null` everywhere that dict then feeds the CUSTOMER-FACING
    surfaces -- `data.missingPrerequisites[].command`, which a human reads
    and `alp-sdk-vscode`'s Fix button sends verbatim to a terminal. (`tan
    doctor --fix` itself does its OWN PATH resolution and must NOT read a
    guarded dict -- see `doctor_cmd.prerequisites_check`'s `fix_missing`.)

    Pure: `available` is injected rather than a real PATH probe, keeping this
    module's "no IO" contract (see the module docstring). Callers pass the
    real check -- `on_path(binary) is not None` -- from `bootstrap_cmd`/
    `doctor_cmd`, both of which already own a hardened PATH walk."""
    return {
        tool: command for tool, command in install.items() if _confirmed(command, available)
    }


def confirm_missing(
    missing: tuple[MissingPrerequisite, ...], available: Callable[[str], bool]
) -> tuple[MissingPrerequisite, ...]:
    """`missing` -- an already-built `PrereqFailure.missing`/`{tool, command}`
    sequence, such as `posix_venv_unusable()` returns -- with any entry
    `_confirmed` (above) rejects degraded to `command: None`. The tuple-
    shaped twin of `confirmed_install_commands`, for a caller that already
    has `MissingPrerequisite` objects rather than the `install` dict they
    were built from (tan-cli#760 review MAJOR 2 / tan-cli#765:
    `posix_venv_unusable()`'s hardcoded `sudo apt-get install -y
    python3-venv` reached the envelope completely unguarded before this
    existed -- the confirmation guard was never actually "the ONE place"
    that decided what got handed out; this closes that gap without changing
    `posix_venv_unusable`'s own signature).

    An entry whose `command` is already `None` passes through unchanged --
    nothing to confirm, and re-checking it would be a wasted PATH probe."""
    out: list[MissingPrerequisite] = []
    for m in missing:
        if m.command is not None and not _confirmed(m.command, available):
            # `replace`, not `MissingPrerequisite(m.tool, None)`: this guard
            # nulls the COMMAND and nothing else. Rebuilding the entry from two
            # of its fields silently dropped the third the moment `provenance`
            # existed (tan-cli#1066) -- so an unconfirmable installer would
            # have cost the customer the licence/tier the consent screen needs
            # to render, on exactly the hosts (a Fedora box reading alp-sdk's
            # apt table) where this guard fires.
            out.append(replace(m, command=None))
        else:
            out.append(m)
    return tuple(out)


def _structured_missing(
    missing: list[str],
    install: dict[str, str],
    provenance: dict[str, ArtifactProvenance] | None = None,
) -> tuple[MissingPrerequisite, ...]:
    """The `{tool, command, <provenance>}` entries for a refusal.

    `provenance` defaults to `None` -- "no table", indistinguishable
    downstream from "no entry for this tool" (`artifact_provenance.for_tool`)
    -- so a caller that has not resolved a manifest (and every pre-tan-cli#1066
    unit test of the refusal builders) keeps reporting the same `null`s an SDK
    predating `artifactProvenance` yields."""
    return tuple(
        MissingPrerequisite(
            tool, install.get(tool), artifact_provenance.for_tool(provenance, tool)
        )
        for tool in missing
    )


def hint_line(tool: str, install: dict[str, str]) -> str:
    """The printed report line for one missing Windows prerequisite. A tool the
    manifest lists no command for gets generic ADVICE rather than being dropped
    -- which is why this is separate from `_structured_missing` and not an
    `or` over the same lookup. The rendering (two-space indent, `  ->  ` with
    two spaces each side) is `bootstrap.ps1`'s and must stay byte-identical."""
    command = install.get(tool)
    if command is not None:
        return f"  {tool}  ->  {command}"
    return f"  {tool}  ->  install `{tool}` and put it on PATH"


#: tan-cli#355, added as a SECOND line on the refusals below -- the oracle's own
#: first line is left byte-identical. See `posix_refusal` for why.
#:
#: tan-cli#650: both hints below now carry the SAME parenthetical --
#: `--fix` mutates the host, so it goes through the identical `can_prompt`
#: consent gate (`tan.core.consent`) every other host mutation in this CLI
#: does, and that gate refuses outright with no TTY on either `stdin` or
#: `stderr` -- piped, redirected, or CI, exactly the shape a Dockerfile `RUN`
#: or an onboarding script has. Measured in a clean `ubuntu:24.04` container
#: (tan-cli#650): `tan doctor --build --fix` with no TTY attached exits 4 and
#: changes nothing. This hint is often the FIRST thing a customer reads about
#: `--fix`, before they have tried it -- recommending it with no caveat there
#: reads as a working remedy, which for a scripted/CI caller it is not.
_DOCTOR_FIX_HINT = (
    "Or run `tan doctor --build --fix` (needs a real, interactive terminal) "
    "to install them from the SDK's manifest."
)

#: tan-cli#370. The line above is TRUE only where the manifest's own install
#: commands need no elevation OR the caller is already root. alp-sdk's
#: `prerequisites.install.linux` is six `sudo apt-get install -y ...` entries;
#: `doctor --build --fix` never spawns the `sudo` PROGRAM itself
#: (`doctor_cmd.fix_needs_sudo_check`, `doctor.fix-needs-sudo`) -- under
#: `--format json` this process's stdio is captured end to end and a password
#: prompt would hang forever rather than fail loudly -- but tan-cli#650 made
#: it root-aware: for a caller whose effective UID is already 0 (a root
#: container, most CI base images, a fresh cloud VM) there is no elevation
#: left to acquire, so `--fix` strips the manifest's literal `sudo ` word and
#: runs the rest of the line directly. A non-root Linux caller still gets
#: NOTHING installed there, only the exact command printed per tool.
#:
#: Keyed on the COMMANDS, not on the platform: macOS is POSIX and its `brew
#: install ...` needs no elevation, so it earns the plain wording, and Windows
#: `winget` (user-scope) does too. Reading the commands is also what keeps this
#: correct against a manifest nobody here has seen.
_DOCTOR_FIX_HINT_NEEDS_ELEVATION = (
    "Or run `tan doctor --build --fix` (needs a real, interactive terminal): "
    "it prints the exact command for each tool from the SDK's manifest, runs "
    "the ones needing no elevation, and -- when already running as root -- "
    "runs the `sudo`-prefixed ones too, without ever spawning `sudo` itself."
)


def _doctor_fix_hint(missing: list[str], install: dict[str, str]) -> str:
    """Which of the three hints is true for THESE tools on THIS host.

    Only the commands reachable for the MISSING tools are considered: a `sudo`
    entry belonging to some tool that is already present says nothing about
    what `--fix` will do in this run. A tool the manifest has no command for
    cannot need elevation either -- it contributes generic advice, not a spawn.

    tan-cli#760 adds the third shape. `install` here has already been through
    `confirmed_install_commands`, so by the time this runs "no entry for tool
    X" means "no command this host can actually run" -- which, when it is
    true of EVERY missing tool, means `--fix` cannot INSTALL anything for
    them here (it may still report a diagnostic, e.g. `tan doctor --fix`'s
    own `doctor.fix-installer-not-found` -- this hint is about the remedy,
    not that separate command's full behaviour). The two hints above both
    promise an install ("it prints the exact command for each tool from the
    SDK's manifest"), which is exactly the unrunnable-remedy defect this
    guard exists to close, just moved from the structured field into this
    prose line instead. Name the tools that are missing; a guessed package
    NAME here would be the identical defect against a different OS/distro
    (see `zephyr_requirements_hint` for the precedent)."""
    confirmed = [tool for tool in missing if install.get(tool) is not None]
    if not confirmed:
        return (
            "`tan doctor --build --fix` has no confirmed install command for "
            f"this host: {', '.join(missing)}.  Install them with your OS's "
            "package manager and put them on PATH, then re-run."
        )
    return (
        _DOCTOR_FIX_HINT_NEEDS_ELEVATION
        if any(install.get(tool, "").split(maxsplit=1)[:1] == ["sudo"] for tool in missing)
        else _DOCTOR_FIX_HINT
    )


def windows_refusal(
    missing: list[str],
    install: dict[str, str],
    provenance: dict[str, ArtifactProvenance] | None = None,
) -> PrereqFailure:
    """`bootstrap.ps1`'s `$Prereqs` loop: header, one `hint_line` each, the
    reopen-PowerShell tail.

    `provenance` reaches only the STRUCTURED half (`missingPrerequisites[]`),
    never the printed lines -- tan-cli#1066 carries a licensing fact for a
    consumer's consent screen; it does not re-word a refusal the oracle's
    wording is pinned against."""
    lines = ["Missing required tools:"]
    lines.extend(hint_line(tool, install) for tool in missing)
    lines.append("Install the tools above (then reopen PowerShell) and re-run.")
    # tan-cli#355: same gap as the POSIX refusal -- name the installer tan ships.
    # The Windows wording is tan's own (it already carries per-tool hints the
    # POSIX one may not), so this is an addition, not a divergence.
    # tan-cli#370: which of the two hints is true depends on the commands, not
    # on the platform -- Windows `winget` is user-scope today, but the manifest
    # is alp-sdk's to change and this reads what it actually says.
    lines.append(_doctor_fix_hint(missing, install))
    return PrereqFailure(
        "prerequisites-missing",
        tuple(lines),
        _structured_missing(missing, install, provenance),
    )


def posix_refusal(
    missing: list[str],
    install: dict[str, str],
    provenance: dict[str, ArtifactProvenance] | None = None,
) -> PrereqFailure:
    """`bootstrap.sh`'s one line: the tool names and nothing else -- TWO spaces
    before "Install". The oracle prints no per-tool commands and neither may
    this; alp-sdk#959 changed what the STRUCTURED half carries, not what a POSIX
    user reads.

    **tan-cli#355 adds a SECOND line, and only a second line.** The oracle's
    first line is still emitted byte for byte, two spaces and all, and a parity
    test pins it so the match stays provable. What is added is the sentence
    naming `tan doctor --build --fix`.

    A DELIBERATE divergence from the oracle, recorded here so nobody restores
    the silence. "The oracle prints no per-tool commands and neither may this"
    was right when tan had no installer of its own; tan-cli#91 changed that
    fact, and `doctor --build --fix` now runs exactly the manifest-owned
    install commands these missing tools need. Measured in a pristine
    `ubuntu:24.04`, a first-time customer got

        Missing required tools: cmake ninja xz wget.  Install them and re-run.

    and nothing else, while the command that would install them sat one
    subcommand away, unmentioned. Withholding a remedy tan HAS, to match an
    oracle that never had one, is parity serving nobody.

    The per-tool commands themselves still stay OUT of the prose -- that half of
    the original constraint holds, and they remain where alp-sdk#959 put them,
    in the structured payload's `{tool, command}` pairs -- which is also where
    tan-cli#1066's `provenance` goes, and only there (see `windows_refusal`)."""
    return PrereqFailure(
        "prerequisites-missing",
        (
            f"Missing required tools: {' '.join(missing)}.  Install them and re-run.",
            _doctor_fix_hint(missing, install),
        ),
        _structured_missing(missing, install, provenance),
    )


def windows_python_not_runnable(install: dict[str, str]) -> PrereqFailure:
    """Windows: `python` is on PATH but did not run -- the Microsoft Store alias
    prints nothing (`bootstrap.ps1`'s `$PyVer` check).

    Its own code, not `prerequisites-missing`: there is no missing tool here and
    no `{tool, command}` pair that could carry the fix, so the install command
    reaches the user through the PROSE -- which is exactly why the package ID in
    it comes from `prerequisites.install.windows` like every other one. A
    hardcoded `Python.Python.3.12` here would be a second copy of a manifest
    fact sitting beside a correct read of it.
    """
    command = install.get("python")
    if command is not None:
        line = (
            f"python did not run (Windows Store alias?).  Install real Python: "
            f"{command}, reopen PowerShell, re-run."
        )
    else:
        # Only reachable for an out-of-contract manifest: the schema requires
        # `install.windows`' keys to equal `prerequisites.windows`, which lists
        # `python`. Degrade the sentence rather than inventing a package ID.
        line = (
            "python did not run (Windows Store alias?).  Install a real Python 3, "
            "reopen PowerShell, re-run."
        )
    return PrereqFailure("python-not-runnable", (line,))


def posix_python_not_runnable() -> PrereqFailure:
    """POSIX: `python3` is on PATH but did not run -- the only failure this port
    adds over `bootstrap.sh`, which would have hit it one step later at
    `python3 -m venv`."""
    return PrereqFailure(
        "python-not-runnable",
        ("python3 is on PATH but did not run.  Install a working Python 3 and re-run.",),
    )


def python_too_old(
    found: tuple[int, int],
    floor: tuple[int, int],
    install: dict[str, str],
    *,
    floor_source: str,
    manifest_floor: tuple[int, int] | None = None,
) -> PrereqFailure:
    """A working interpreter below the EFFECTIVE floor.

    **This is the customer-facing fix, not a port.** The oracle refuses here on
    Windows only and against the MANIFEST's floor
    (`crates/tan-cli/src/commands/bootstrap/steps.rs`, whose POSIX branch states
    outright *"this branch cannot fail on version"*). Three facts compose into a
    silent failure: `metadata/bootstrap.json:16` declares
    `"pythonMinVersion": "3.10"`; Zephyr's `cmake/modules/python.cmake:14` sets
    `set(PYTHON_MINIMUM_REQUIRED 3.12)`; Ubuntu 22.04 ships `python3` = 3.10. So
    today `tan bootstrap` succeeds, and the customer's FIRST build dies inside
    Zephyr's CMake configure with an error naming Zephyr rather than us. The
    floor enforced here is therefore the EFFECTIVE one -- the higher of the two
    -- on BOTH platforms, the same floor `tan doctor` already reports
    (`tan.commands.doctor_cmd.python_check`, via the same
    `zephyr_python_floor`).

    Tool-less, so the install command travels in the prose. `floor_source` names
    WHERE the number came from, and `manifest_floor` (when it is lower) names
    the skew -- otherwise a customer refused at 3.11 greps the manifest, reads
    `3.10`, and concludes tan is broken.

    The manifest's install command is SUPPRESSED in the skew case, deliberately.
    That command is scoped to the manifest's OWN floor, so it cannot be trusted
    to deliver a higher one: on the host this whole fix exists for -- Ubuntu
    22.04 -- `sudo apt-get install -y python3` installs 3.10, which is exactly
    the version being refused. Printing it would send the customer round a loop.
    """
    skewed = manifest_floor is not None and manifest_floor < floor
    verdict = (
        f"Python {found[0]}.{found[1]} found; the SDK tooling needs "
        f">= {floor[0]}.{floor[1]}"
    )
    command = None if skewed else (install.get("python") or install.get("python3"))
    line = f"{verdict} ({command})." if command is not None else f"{verdict}."
    line = f"{line}  That floor comes from {floor_source}."
    if skewed and manifest_floor is not None:
        line = (
            f"{line}  alp-sdk's {BOOTSTRAP_MANIFEST_REL_PATH} declares only "
            f"{manifest_floor[0]}.{manifest_floor[1]}, so its own install command is "
            f"not enough here -- install a Python "
            f"{floor[0]}.{floor[1]}+ and put it ahead of "
            f"{found[0]}.{found[1]} on PATH, then re-run so the workspace venv is "
            f"built with it."
        )
    return PrereqFailure("python-too-old", (line,))


def python_floor_skew_warning(
    manifest_floor: tuple[int, int],
    effective_floor: tuple[int, int],
    source: str,
    from_manifest: bool = True,
) -> tuple[str, str] | None:
    """`(code suffix, message)` when the two declared floors disagree, else
    `None`.

    Reported rather than silently reconciled, and worded to match
    `tan.commands.doctor_cmd.python_floor_skew_check` -- doctor raises the same
    verdict as `doctor.pythonFloor`, and two commands describing one manifest
    defect differently is the drift this port keeps hitting. Fires on a
    SUCCESSFUL run too: the host is fine and the two declared floors disagree.

    It does NOT follow that the fix belongs in `metadata/bootstrap.json` -- this
    docstring used to say so, and the remedy below used to act on it. Raising
    `prerequisites.pythonMinVersion` was tried and REVERTED (alp-sdk#1078): the
    key is host-universal while this floor is Zephyr's, so raising it refuses a
    3.10/3.11 host for a Yocto-only or metadata-only project that builds today.
    The skew is deliberate; the message says so and points the customer at the
    only thing that actually helps them (tan-cli#300).

    `from_manifest=False` (pass `facts.from_manifest`) means `manifest_floor`
    never actually came from a read `metadata/bootstrap.json` -- this SDK
    predates it (`load_facts`'s `_manifest_absent_floor` branch) -- and is
    instead tan's own frozen fallback constant standing in. Claiming alp-sdk's
    manifest "declares" that number, and telling the customer to edit it, would
    send them to a file bootstrap never read.
    """
    if manifest_floor >= effective_floor:
        return None
    if from_manifest:
        claim = (
            f"alp-sdk's {BOOTSTRAP_MANIFEST_REL_PATH} declares pythonMinVersion "
            f"{manifest_floor[0]}.{manifest_floor[1]}"
        )
        # NOT "raise pythonMinVersion in the manifest". That was tried and
        # REVERTED (alp-sdk#1078): the key is host-universal, and
        # `build_readiness.rs:401` checks Python BEFORE any `os_set` branch, so
        # raising it refuses a 3.10/3.11 host for a Yocto-only or metadata-only
        # project that builds today -- and 3.12 is unreachable via the remedy
        # the manifest itself offers (`sudo apt-get install -y python3`) on the
        # Ubuntu 22.04 hosts the docs recommend. This warning fires while
        # bootstrap is REFUSING, so it is the last line a blocked user reads and
        # the likeliest thing they act on; it has to name something that helps
        # them, not an SDK edit that would make things worse (tan-cli#300).
        fix = (
            f" The skew is known and deliberately unresolved (alp-sdk#1078): the "
            f"manifest key is host-universal while this floor is Zephyr's. Nothing "
            f"to change in alp-sdk -- put a Python "
            f"{effective_floor[0]}.{effective_floor[1]} or newer on the build path."
        )
    else:
        claim = (
            f"this SDK checkout has no {BOOTSTRAP_MANIFEST_REL_PATH} to declare a floor, "
            f"so tan's own built-in floor {manifest_floor[0]}.{manifest_floor[1]} is "
            f"standing in"
        )
        fix = " Update this SDK checkout to a version that ships that manifest."
    return (
        "python-floor-skew",
        f"{claim}, but the build's effective floor is "
        f"{effective_floor[0]}.{effective_floor[1]} (from {source}). bootstrap enforces "
        f"the higher, effective floor, so a host this manifest would have accepted is "
        f"refused here rather than failing later inside Zephyr's CMake configure."
        f"{fix}",
    )


# ---------------------------------------------------------------------------
# The Python CEILING (tan-cli#285): a floor alone caught "too old"; it cannot
# catch "too new for the ecosystem".
# ---------------------------------------------------------------------------

#: The highest CPython minor tan has actually seen a full venv build clean
#: against. STALE BY DEFAULT, exactly like `ZEPHYR_VERSION` above -- there is
#: no `pythonMaxVersion` in `metadata/bootstrap.json` yet (it carries only the
#: FLOOR, `pythonMinVersion`), so this is tan's own placeholder until that
#: manifest can carry a real ceiling. Bump it only against a real run that
#: built a complete venv on the newer minor -- not by inference.
#:
#: A design choice, not a mechanical value, and worth stating explicitly: this
#: used to read `(3, 13)`, on the reasoning "3.14 broke, so one minor below it
#: is probably fine" -- a COMPUTED guess asserted as "a MEASUREMENT, not a
#: computed bound", which it never was (nothing in CI, `getting-started.yml`
#: or a first-blink run has ever bootstrapped on 3.13). `(3, 12)` is what is
#: actually measured good (every CI Python job pins it) against `(3, 14)`
#: measured bad (the `hidapi` failure this whole mechanism exists to warn
#: about). Tightening the number to what is true is NOT the same change as
#: tightening the gate: this stays a WARN at 3.12 exactly as it was at 3.13 --
#: see `python_ceiling_warning`'s own docstring for why a hard refusal here
#: would be its own defect, symmetric to the floor bug this port already
#: fixed. A working 3.13 host still bootstraps clean either way; it now also
#: gets told, correctly, that this port has not verified that combination.
PYTHON_CEILING_KNOWN_GOOD = (3, 12)


def python_ceiling_warning(found: tuple[int, int], venv_dir: str) -> tuple[str, str] | None:
    """`(code suffix, message)` when `found` is newer than any Python tan has
    verified a complete venv against, else `None`. `venv_dir` is the
    already-rendered (`_native`) workspace venv path, named in the remedy.

    **Deliberately a WARN, never a refusal.** The floor check above refuses,
    because a too-OLD interpreter is a GUARANTEED failure -- Zephyr's own CMake
    configure enforces its floor unconditionally. A too-NEW interpreter is not
    guaranteed to fail at all: most projects never touch the specific optional
    dependency (`hidapi`, in the one case measured so far) that lacks a
    prebuilt wheel for it, and most hosts will bootstrap a perfectly complete
    venv anyway. Refusing a host that would have built cleanly is the same
    defect the floor fix above exists to close, mirrored onto the other edge --
    a hard ceiling that blocks a WORKING host is its own bug, not a safety
    rail. This warning exists only to give the customer the "why" up front,
    before they spend time chasing a build failure back to their interpreter
    choice; the venv-completeness check (tan-cli#285's other half) is what
    actually catches it when it happens.
    """
    if found <= PYTHON_CEILING_KNOWN_GOOD:
        return None
    return (
        "python-newer-than-verified",
        f"Python {found[0]}.{found[1]} is newer than the highest tan has verified a "
        f"complete venv against ({PYTHON_CEILING_KNOWN_GOOD[0]}."
        f"{PYTHON_CEILING_KNOWN_GOOD[1]}). Not refused -- most hosts and most projects "
        f"bootstrap cleanly on a newer Python anyway -- but a dependency with no "
        f"prebuilt wheel yet for this interpreter (hidapi is the one seen so far) can "
        f"still fall back to a source build and fail. If a later warning reports the "
        f"venv incomplete: delete {venv_dir} (there is no --recreate-venv) and re-run "
        f"`tan bootstrap` -- a REUSED venv keeps the interpreter that created it, so "
        f"installing another Python 3 alongside this one does nothing by itself. On "
        f"Windows, put that older interpreter first on PATH before re-running (or create "
        f"the venv yourself, e.g. `py -3.12 -m venv {venv_dir}`), since tan's own default "
        f"candidate is `py -3`, which resolves to the newest install.",
    )


# ---------------------------------------------------------------------------
# The pip phase's remediation hints (tan-cli#285): gated on the REAL host, not
# assumed Linux.
# ---------------------------------------------------------------------------


def zephyr_requirements_hint(host: str) -> str:
    """The OS-gated remedy appended to the `zephyr-requirements` warning.

    Only LINUX and WINDOWS get a named package/command below: those are the
    two hosts a real failure has actually been measured and diagnosed on (a
    stock ubuntu-24.04 CI runner; Python 3.14 on Windows, `LINK : fatal error
    LNK1104`). Printing the Linux line unconditionally used to send a Windows
    customer to run `sudo apt-get` on a host with no `apt-get` at all, and to
    misdiagnose an MSVC linker failure as a missing header. macOS/other get a
    host-neutral line rather than a GUESSED command -- printing an unverified
    package name would repeat the exact defect this fixes, just against a
    different OS.

    None of the three text blames "the output above"/"the output" as if a
    reader can already see it: `--format json` has no terminal output at all
    -- the caller (`pip_phase`) appends the actual captured pip tail to the
    SAME message when one was captured, so "the captured pip output" here
    always names something that is either right there in the message or
    genuinely was not captured (text mode, where the child's own log already
    streamed live).
    """
    if host == WINDOWS:
        return (
            "On Windows this is usually `hidapi` with no prebuilt wheel yet for this "
            "Python, falling back to a source build that needs the MSVC linker (look "
            "for `LINK : fatal error LNK1104` in the captured pip output -- this is NOT "
            "a missing native header): install the \"Desktop development with C++\" "
            "workload from the Visual Studio Build Tools "
            "(https://visualstudio.microsoft.com/visual-cpp-build-tools/), which "
            "supplies both the linker and the Windows SDK libraries hidapi links "
            "against, then re-run `tan bootstrap`."
        )
    if host == LINUX:
        return (
            "On Linux this is usually `hidapi` needing native headers: `sudo apt-get "
            "install -y pkg-config libusb-1.0-0-dev libudev-dev`, then re-run `tan "
            "bootstrap`."
        )
    return (
        "Check the captured pip output for the real cause (often a native "
        "dependency with no prebuilt wheel for this host), then re-run `tan bootstrap`."
    )


def posix_venv_unusable() -> PrereqFailure:
    """Linux: `python3` runs and clears every check above, but its `venv` module
    cannot create a usable environment because `ensurepip` is missing --
    Debian/Ubuntu split `python3-venv` out of the base `python3` package.

    A SECOND check, deliberately not folded into the manifest's
    `prerequisites.posix` list: that list is an alp-sdk fact and `python3-venv`
    is not in it upstream. Its own code, like the Python-floor refusals -- and
    unlike them it HAS a real `{tool, command}` pair, which a Fix button needs.

    `python3-venv`, not the version-specific `python3.NN-venv` Python's own
    error names: apt resolves the unversioned meta-package to the matching
    versioned one, and this message cannot know which minor is running.
    """
    return PrereqFailure(
        "venv-unusable",
        (
            "python3 found, but its venv module cannot create a usable virtual "
            "environment (ensurepip is missing).  On Debian/Ubuntu: sudo apt-get "
            "install -y python3-venv, then re-run.",
        ),
        (MissingPrerequisite("python3-venv", "sudo apt-get install -y python3-venv"),),
    )


def reported_missing(
    missing: tuple[MissingPrerequisite, ...],
) -> list[dict[str, str | int | None]] | None:
    """The envelope form: `None` when the refusal names no tool.

    `[]` is NEVER a value here. The Python-floor refusals reach this empty, and
    `[]` on the wire would spell "checked, nothing missing" -- which is what a
    run that found the list clean reports, as `None`. One fact, one spelling.
    """
    return [m.as_dict() for m in missing] if missing else None


# ---------------------------------------------------------------------------
# The Yocto host gate
# ---------------------------------------------------------------------------

#: Verdicts of `yocto_gate`.
GATE_CLEAR = "clear"
GATE_WARN = "warn"
GATE_REFUSE = "refuse"


def in_play_runtimes(
    board_cores: dict[str, str | None] | None,
    board_os: str | None,
    topology: dict[str, str],
) -> list[str]:
    """The distinct runtimes a project puts in play, sorted.

    A `cores:` block IS the project's core selection: each entry resolves
    through its explicit `os:` override (`"off"` removes the core), else the
    matching topology entry, else the core-id heuristic. With no `cores:` block
    a v1 top-level `os:` wins, and failing that the whole SoM topology is in
    play.

    `topology` empty means the SoM metadata could not be read; an empty RESULT
    means "unresolvable", which every caller must treat as "proceed".
    """
    def from_topology(core_id: str) -> str:
        return topology.get(core_id) or infer_runtime_for_core_id(core_id)

    def declared(value: str | None) -> str | None:
        cleaned = (value or "").strip()
        return cleaned or None

    out: set[str] = set()
    if board_cores:
        for core_id, raw in board_cores.items():
            os_value = declared(raw)
            if os_value == OS_OFF:
                continue
            out.add(os_value or from_topology(core_id))
    else:
        top_level = declared(board_os)
        if top_level is not None and top_level != OS_OFF:
            out.add(top_level)
        else:
            out.update(topology.values())
    return sorted(out)


def yocto_gate(runtimes: list[str], host: str) -> str:
    """Refusal is deliberately narrow -- only a project that is *entirely* Yocto
    on a non-Linux host. Erring toward running is harmless (bootstrap is
    idempotent); erring toward refusing bricks the command.

    The test is "every runtime in play is `yocto`" rather than "none is
    `zephyr`/`baremetal`": an unrecognised `os:` string is an unresolvable core,
    and unresolvable means proceed.
    """
    if host == LINUX or not runtimes:
        return GATE_CLEAR
    if all(r == "yocto" for r in runtimes):
        return GATE_REFUSE
    if any(r == "yocto" for r in runtimes):
        return GATE_WARN
    return GATE_CLEAR


def yocto_only_refusal() -> str:
    return (
        f"every core in this project targets Yocto. {YOCTO_HOST_DETAIL} Re-run "
        f"`tan bootstrap` inside WSL2 or on a Linux host."
    )


def yocto_mixed_warning() -> str:
    return (
        f"a Yocto core is in play. {YOCTO_HOST_DETAIL} The Zephyr/baremetal cores "
        f"bootstrap normally here."
    )


# ---------------------------------------------------------------------------
# `$ZEPHYR_BASE` workspace selection
# ---------------------------------------------------------------------------

#: Outcomes of `decide_workspace_reuse`.
REUSE = "reuse"
STALE = "stale"
MANIFEST_MISMATCH = "manifest-mismatch"
INCOMPATIBLE = "incompatible"


def decide_workspace_reuse(
    version_file: str,
    top_is_west_workspace: bool,
    manifest_is_sdk: bool,
    pin: str,
) -> tuple[str, str]:
    """`(choice, that tree's Zephyr version)` from already-gathered facts.

    Untouched reuse needs ALL THREE of a `.west/` topdir, a manifest resolving
    to the SDK root, and an EXACT `MAJOR.MINOR.PATCH` match. A tree clearing the
    first two but not the third is `STALE` -- it is this SDK's own workspace, so
    `west update` against this SDK's own `west.yml` is precisely what brings it
    back to the pins, and adopting it is cheaper and less surprising than
    cloning a second Zephyr elsewhere.

    STILL NOT COVERED: only `zephyr`'s pin is compared. A bump touching only a
    non-`zephyr` `west.yml` project (`hal_alif`, `cmsis`, `mcuboot`) leaves the
    version identical, so this still returns `REUSE`.
    """
    version = parse_zephyr_version_file(version_file)
    if version is None or not top_is_west_workspace:
        # No readable VERSION -- nothing to judge, so it cannot be adopted.
        return INCOMPATIBLE, version or ""
    if not manifest_is_sdk:
        # #769 stays version-gated: a foreign tree on some unrelated Zephyr is
        # simply not this workspace, and gets the plain "ignoring it" message.
        return (MANIFEST_MISMATCH if version == pin else INCOMPATIBLE), version
    return (REUSE if version == pin else STALE), version


def parent_needs_workspace_guard(
    entries: list[str],
    checkout_name: str,
    venv_dir_name: str,
    dot_west_is_workspace: bool,
) -> bool:
    """Whether the checkout's parent needs the workspace-parent guard.

    `west init -l <alp-sdk>` forces the west topdir to be the checkout's own
    PARENT, so a customer who clones into `~/Downloads` gets
    zephyr/modules/.west/venv sprayed there, unannounced, outside the checkout
    where no `.gitignore` can reach it. Proceed silently when the parent holds
    NOTHING BUT the checkout, bootstrap's OWN venv, and/or an existing west
    workspace; otherwise guard.

    `dot_west_is_workspace` is a TYPED fact the caller computes with a
    filesystem check, never inferred from `entries` containing the literal
    `".west"`: a plain FILE named `.west` is not a workspace, and letting the
    NAME answer that was a false PROCEED. When it is true, every other entry is
    that workspace's own content.

    Otherwise the parent is judged purely on COUNT, dotfiles included.
    Deliberately NOT a directory-NAME check (no `Downloads`/`Desktop` list): a
    name list is locale-dependent and incomplete by construction.
    """
    if dot_west_is_workspace:
        return False
    venv_top = re.split(r"[\\/]", venv_dir_name)[0] if venv_dir_name else None
    return any(entry != checkout_name and entry != venv_top for entry in entries)


def resolve_workspace_target(raw: str, cwd: str) -> str:
    """Validate + absolutise `--workspace <path>`. Raises `ValueError`.

    This relocates a customer's checkout, so an empty value (`--workspace ""`,
    the classic unset-`$WS` shell accident) or an ambiguous drive-relative one
    (an MSYS-style `/e/foo/ws` on Windows) must never resolve to a guess. Pure
    validation -- no IO.
    """
    trimmed = raw.strip()
    if not trimmed:
        raise ValueError("--workspace requires a non-empty path")
    if _is_drive_relative(trimmed):
        raise ValueError(_drive_relative(trimmed))
    if os.path.isabs(trimmed) or ntpath_isabs(trimmed):
        # `\x` on Windows has a root but no drive: rooted-but-driveless is
        # rejected just below, so only a fully absolute path passes here.
        if os.name == "nt" and not re.match(r"^([A-Za-z]:|[\\/]{2})", trimmed):
            raise ValueError(_rooted_no_drive(trimmed))
        return os.path.normpath(trimmed)
    if trimmed.startswith(("/", "\\")):
        raise ValueError(_rooted_no_drive(trimmed))
    return os.path.normpath(os.path.join(cwd, trimmed))


def _is_drive_relative(raw: str) -> bool:
    """`C:ws` -- a drive letter with NO separator after it (tan-cli#495 defect 8).

    Windows keeps a separate current directory PER DRIVE, so `C:ws` means "`ws`
    under whatever the process's current directory on C: happens to be" -- the
    same class of ambiguity `--workspace /e/foo` is already refused for, and
    the caller is about to RELOCATE a customer's checkout into it.

    Fixed here at the call site rather than in `ntpath_isabs`, which has two
    callers and wants the opposite answers from them. `ntpath.isabs("C:ws")`
    is False while `ntpath_isabs`'s own `^[A-Za-z]:` regex is True, and that
    disagreement is CORRECT for the other caller: `is_plain_relative` must
    reject `C:ws` as a manifest-supplied directory name, which is exactly what
    the regex buys it. Narrowing the predicate would have let `C:ws` through
    there as a "plain relative" name to be joined onto the workspace.
    """
    return bool(re.match(r"^[A-Za-z]:(?![\\/])", raw))


def _drive_relative(trimmed: str) -> str:
    return (
        f"--workspace '{trimmed}' is drive-relative, which is ambiguous (it would "
        f"resolve against the current directory on that drive, not the drive root); "
        f"pass a full absolute path such as '{trimmed[:2]}\\{trimmed[2:]}' instead"
    )


def _rooted_no_drive(trimmed: str) -> str:
    return (
        f"--workspace '{trimmed}' has a root but no drive, which is ambiguous on this "
        f"host (it would resolve against whichever drive the process happens to be "
        f"running from); pass a full absolute path instead"
    )


# ---------------------------------------------------------------------------
# `.west/config` (an ini file, read/written by hand -- west is not installed yet)
# ---------------------------------------------------------------------------


def _section_header(line: str) -> str | None:
    trimmed = line.strip()
    if trimmed.startswith("[") and trimmed.endswith("]"):
        return trimmed[1:-1].strip()
    return None


def _key_value(line: str) -> tuple[str, str] | None:
    trimmed = line.lstrip()
    if not trimmed or trimmed[0] in "#;":
        return None
    key, sep, value = line.partition("=")
    if not sep or not key.strip():
        return None
    return key.strip(), value.strip()


def get_manifest_path(config: str) -> str | None:
    """The `[manifest]` section's `path = ` value. Section-scoped: a `path =`
    line under a different section is never returned."""
    section = ""
    for line in config.splitlines():
        header = _section_header(line)
        if header is not None:
            section = header
            continue
        if section != "manifest":
            continue
        pair = _key_value(line)
        if pair is not None and pair[0].lower() == "path":
            return pair[1]
    return None


def same_directory(a: Path, b: Path) -> bool:
    """True when `a` and `b` name the same directory. `realpath` when both exist
    (the reliable answer); a lexical `normpath`+`normcase` comparison when either
    does not -- e.g. a stale config's target SDK version since pruned.

    Moved down from `bootstrap_cmd._same_directory` with `manifest_points_at`
    (tan-cli#495): it is pure path logic with no IO policy of its own, and
    leaving it in the command module was half of what forced the deferred
    imports described on `manifest_points_at`.
    """
    try:
        if a.exists() and b.exists():
            return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(
        os.path.normpath(str(b))
    )


def manifest_points_at(topdir: Path, repo_root: Path) -> bool:
    """Whether `<topdir>/.west/config`'s `[manifest] path` resolves to
    `repo_root`. west and the venv are not necessarily set up when this is
    asked, so the config is read directly rather than shelling
    `west config manifest.path`.

    **Lives in `tan.core` because `tan.core.venv` is its heaviest caller**
    (tan-cli#495). It was defined in `tan.commands.bootstrap_cmd`, so
    `venv.py` -- a `tan.core` module -- reached UP into the command layer four
    separate times, each behind a deferred `# noqa: PLC0415` import whose only
    job was to dodge the resulting import cycle. That inverts this repo's
    layering (pure logic in `tan/core` or `tan/planner`, never the command/IO
    file), and a cycle-dodging import is a symptom to fix rather than
    annotate. `bootstrap_cmd` now imports it from here like every other
    consumer.

    Reads leniently (`errors="replace"`), matching the reader this used to be
    handed: a `.west/config` with a mojibake byte is a config whose `path` key
    should still be found, not an exception out of a predicate.
    """
    try:
        config = (topdir / ".west" / "config").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return False
    rel = get_manifest_path(config)
    if rel is None:
        return False
    return same_directory(topdir / rel.strip(), repo_root)


def set_manifest_path(config: str, new_rel: str) -> str | None:
    """`config` with the `[manifest]` section's `path` rewritten, every other
    line byte-identical -- each line's own terminator (`\\r\\n`, `\\n`, or none
    for a final newline-less line) survives, so a CRLF `.west/config` stays
    CRLF. `None` when there is no line to replace."""
    section = ""
    out: list[str] = []
    rewrote = False
    for segment in config.splitlines(keepends=True):
        content = segment.rstrip("\r\n")
        terminator = segment[len(content) :]
        header = _section_header(content)
        if header is not None:
            section = header
        elif not rewrote and section == "manifest":
            pair = _key_value(content)
            if pair is not None and pair[0].lower() == "path":
                out.append(f"path = {new_rel}{terminator}")
                rewrote = True
                continue
        out.append(segment)
    return "".join(out) if rewrote else None


# ---------------------------------------------------------------------------
# The `<topdir>/.west/tan-workspace-sdk` record (tan-cli#292). Written by
# `tan.commands.bootstrap_cmd.record_workspace_sdk` after a `west update` that
# actually ran; read back by `tan.commands.doctor_cmd`'s `venvProvenance`
# check. A record-less workspace (bootstrapped by alp-sdk's own
# `bootstrap.sh`, `crates/tan-cli/src/venv.rs:25-27`) is NOT an error here --
# `parse_workspace_sdk_record` only ever returns "usable" or `None`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceSdkRecord:
    """A parsed `<topdir>/.west/tan-workspace-sdk`. `sdk_path` is the only
    field every record (even one written before tan-cli#292) carries; the
    venv provenance fields are `None` on an older record, or one written by a
    caller that could not compute them -- ABSENCE, never a claim, so a
    consumer never reads a `None` as "confirmed empty"."""

    sdk_path: str
    #: The venv directory, relative to `topdir` (e.g. `.venv`) -- so a moved
    #: workspace, or one whose `metadata/bootstrap.json` names a non-default
    #: `venv.dirName`, still resolves without re-deriving it.
    venv_dir_name: str | None = None
    #: The bin-dir layout actually created (`bin` / `Scripts`, tan-cli#291) --
    #: which directory `venv_dir_name` holds the executables under.
    venv_layout: str | None = None
    #: Lowercase-hex SHA-256 of the `zephyr.requirementsPath` file that
    #: populated the venv's Python packages (`bootstrap_cmd.pip_phase`) --
    #: the provenance stamp: the venv can be re-verified against a LATER
    #: read of the same file without re-running pip.
    requirements_digest: str | None = None


def workspace_sdk_record_json(
    sdk_path: str,
    venv_dir_name: str | None = None,
    venv_layout: str | None = None,
    requirements_digest: str | None = None,
) -> str:
    """The `<topdir>/.west/tan-workspace-sdk` record's contents: which SDK a
    `west update` last synced this topdir's trees to, plus (tan-cli#292) which
    venv it populated and a content-hash provenance stamp for the Zephyr
    requirements that filled it. `updatedAt` is `generated_at_iso()`, matching
    `sdk_pointer_json`'s own self-contained timestamp -- `SOURCE_DATE_EPOCH`
    wins over the clock, so a captured record is reproducible, and that helper
    NEVER raises (an out-of-range epoch used to kill `tan init` here).

    Deliberately its OWN function, not a `tan.core.scaffold.sdk_pointer_json`
    extension: that function is the `.alp/sdk-path` PROJECT pin and
    `~/.alp/sdk-default` GLOBAL pin -- a different record with different
    readers (`tan init`'s scaffold, `sdk_cmd`'s resolution ladder) -- growing
    ITS shape for this record's needs would silently add fields those readers
    never asked for and never validate.

    `venv_dir_name`/`venv_layout`/`requirements_digest` are omitted from the
    JSON (not written as `null`) when the caller has nothing to report --
    mirroring `Check.as_dict`'s optional fields -- so a record predating
    tan-cli#292 and one written by a caller that could not compute a hash are
    indistinguishable on the wire, and `parse_workspace_sdk_record` reads both
    as "nothing to compare against" rather than a false claim.
    """
    payload: dict[str, str] = {"sdkPath": sdk_path, "updatedAt": generated_at_iso()}
    if venv_dir_name is not None:
        payload["venvDir"] = venv_dir_name
    if venv_layout is not None:
        payload["venvLayout"] = venv_layout
    if requirements_digest is not None:
        payload["requirementsDigest"] = requirements_digest
    return json.dumps(payload, indent=2) + "\n"


def parse_workspace_sdk_record(text: str) -> WorkspaceSdkRecord | None:
    """Parse a `<topdir>/.west/tan-workspace-sdk` record's text. `None` on
    anything that is not a usable record -- not JSON, not an object, or no
    usable `sdkPath` -- so a record `doctor` cannot read is "nothing to
    compare against", the SAME as no record at all, never a mismatch WARNING
    against a checkout `tan` cannot even name.
    """
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    sdk_path = doc.get("sdkPath")
    if not isinstance(sdk_path, str) or not sdk_path:
        return None

    def _opt(key: str) -> str | None:
        value = doc.get(key)
        return value if isinstance(value, str) and value else None

    return WorkspaceSdkRecord(
        sdk_path=sdk_path,
        venv_dir_name=_opt("venvDir"),
        venv_layout=_opt("venvLayout"),
        requirements_digest=_opt("requirementsDigest"),
    )


# ---------------------------------------------------------------------------
# The printed blocks. Copy-pasteable shell snippets, so they carry NO
# `bootstrap: ` prefix (unlike the progress lines) and their whitespace is
# load-bearing.
# ---------------------------------------------------------------------------


def render_env_lines(
    env: tuple[tuple[str, str], ...], tokens: Tokens, prefix: str, is_windows: bool
) -> list[str]:
    """The manifest's `env` map as shell-ready lines.

    POSIX (`print_env_lines`) quotes the value only when it looks like a path --
    contains `/` -- which keeps `export ZEPHYR_TOOLCHAIN_VARIANT=zephyr`
    unquoted while `ZEPHYR_BASE` is quoted. Windows (`Write-EnvLines`) always
    quotes.

    One deliberate divergence from `bootstrap.ps1`: a token-substituted value is
    separator-normalised, so Windows emits `C:\\dev\\ws\\zephyr` rather than the
    script's mixed `C:\\dev\\ws/zephyr`. Both work; only one is copy-pasteable
    without a double-take. A value with no token in it is passed through
    untouched.
    """
    lines = []
    for key, raw in env:
        value = tokens.apply(raw)
        substituted = value != raw
        if is_windows:
            if substituted:
                value = value.replace("/", "\\")
            lines.append(f'{prefix}$env:{key} = "{value}"')
        elif "/" in value:
            lines.append(f'{prefix}export {key}="{value}"')
        else:
            lines.append(f"{prefix}export {key}={value}")
    return lines


def print_env_block(
    facts: BootstrapFacts, tokens: Tokens, venv_bin_dir: str, is_windows: bool
) -> list[str]:
    """`--print-env`: the venv-activation comment header plus the rendered `env`
    map. Both scripts print exactly this and exit 0."""
    venv = facts.venv_dir_name
    if is_windows:
        # The workspace token is forward-slash on every OS (the resolved project
        # path), so it is normalised here or this line comes out mixed
        # (`C:/Users/dev\.venv\Scripts\Activate.ps1`).
        workspace = tokens.workspace_dir.replace("/", "\\")
        lines = [
            "# Add to your PowerShell profile (or run before invoking the SDK):",
            "# Activate the workspace venv (west + Zephyr/SDK Python deps live here):",
            f'#   & "{workspace}\\{venv}\\{venv_bin_dir}\\Activate.ps1"',
        ]
    else:
        lines = [
            "# Add to your shell profile (or run before invoking the SDK):",
            "# Activate the workspace venv (west + Zephyr/SDK Python deps live here):",
            f'#   source "{tokens.workspace_dir}/{venv}/{venv_bin_dir}/activate"',
        ]
    lines.extend(render_env_lines(facts.env, tokens, "", is_windows))
    return lines


def optional_libs_block(facts: BootstrapFacts, host: str) -> list[str]:
    """The trailing manual-install hint.

    POSIX prints the manifest's per-OS optional-native-libs note (plus its
    install command, when the OS has one); native Windows prints
    `manualInstallHints.windows.note`, one two-space-indented line per element
    under its own heading -- the SDK-sourced fact, not a hand-typed copy that
    would desync silently.

    The Windows arm must NOT also read `nativeLibHints.windows.note`: appending
    both printed the Arm/Zephyr-SDK sentence twice. That field is parsed for
    round-trip fidelity but rendered by NOTHING here -- host detection reads the
    real platform, so a Windows host always takes this branch, git-bash or not,
    and the `bootstrap.sh` arm below is unreachable there.

    NO blank line between the Windows heading and the first note element: the
    oracle has nothing in between. The POSIX arm below still emits its blank
    because `bootstrap.sh` genuinely echoes one.
    """
    if host == WINDOWS:
        lines = ["", "bootstrap: NOT auto-installed (manual, one-time):"]
        lines.extend(f"  {line}" for line in facts.manual_install_windows)
        return lines

    lines = ["", "bootstrap: Optional native libraries unlock the Yocto-side backends:"]
    hint = facts.native_lib_hint(host)
    if hint is None:
        # Structured as the oracle's `match` is (`blocks.rs:191-202`): this arm
        # pushes its line and FALLS THROUGH to the manual-install block below
        # rather than returning. Equivalent today -- `native_lib_hint` is None
        # only for `OTHER`, which that block already excludes -- but the shape
        # is kept identical so that adding an OS, or making a per-OS hint
        # optional at parse, cannot silently re-drop the POSIX note the way
        # tan-cli#495 defect 6 did.
        lines.append("  (OS not auto-detected; see docs/testing.md)")
    else:
        lines.append("")
        lines.extend(f"  {line}" for line in hint.note)
        if hint.command:
            lines.append("")
            lines.append(f"  {hint.command}")
    # tan-cli#495 defect 6: `manualInstallHints.posix.note`, which the port
    # dropped at parse, at render AND in the fallback -- so Linux/macOS
    # customers were never told that `west sdk install` is a separate manual
    # step, the one every Zephyr-on-M customer needs. Lands AFTER the
    # optional-native-libs section because the oracle prints it after
    # (`blocks.rs:229-245`, `bootstrap.sh:594` vs `:638`), under the same
    # heading the Windows arm uses.
    #
    # `LINUX`/`MACOS` ONLY, matching the oracle's `matches!(host, Linux |
    # MacOs)` and the shell `case` behind it -- never `OTHER`, which must not
    # start rendering a POSIX-specific fact by accident. The Windows arm
    # returned early above, so this is unreachable there regardless.
    if host in (LINUX, MACOS) and facts.manual_install_posix:
        lines.append("")
        lines.append("bootstrap: NOT auto-installed (manual, one-time):")
        lines.extend(f"  {line}" for line in facts.manual_install_posix)
    return lines


def next_steps_block(
    facts: BootstrapFacts,
    tokens: Tokens,
    venv_dir: str,
    venv_bin_dir: str,
    is_windows: bool,
) -> list[str]:
    """The closing "Next steps:" block: activate the venv, export the `env`
    map, run `tan doctor`, and one ready-to-paste build command."""
    lines = ["", "Next steps:"]
    if is_windows:
        lines.append(
            "  # Activate the workspace venv (west + Zephyr/SDK deps + tan's Python "
            "backend):"
        )
        lines.append(f'  & "{venv_dir}\\{venv_bin_dir}\\Activate.ps1"')
    else:
        lines.append("  # Activate the workspace venv (west + Zephyr/SDK deps live here):")
        lines.append(f'  source "{venv_dir}/{venv_bin_dir}/activate"')
    lines.append("")
    lines.append("  # Make Zephyr reachable for builds:")
    lines.extend(render_env_lines(facts.env, tokens, "  ", is_windows))
    # The pinned install.sh/install.ps1 one-liner, NOT `cargo install --git`
    # (that built unpinned HEAD). `tan doctor`, not `--build`: plain doctor
    # already folds in the build-readiness preflight.
    if is_windows:
        install_line = (
            "  # for: irm https://raw.githubusercontent.com/alplabai/tan-cli/main/"
            "install.ps1 | iex):"
        )
    else:
        install_line = (
            "  # for: curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/"
            "main/install.sh | sh):"
        )
    lines.extend(
        [
            "",
            "  # Sanity-check the host environment (needs tan on PATH -- see README.md",
            install_line,
            "  tan doctor",
            "",
        ]
    )
    if is_windows:
        # `bootstrap.ps1` interpolates a native backslash path here and spells
        # the example as `examples\...`, so a raw forward-slash `${SDK_ROOT}`
        # would print mixed.
        repo_root = tokens.sdk_root.replace("/", "\\")
        lines.extend(
            [
                "  # Or jump straight into building an example for real silicon",
                "  # (needs the Zephyr SDK toolchain -- bootstrap just tried to acquire it",
                "  #  automatically (ADR 0021 Lane 1 P1); `tan doctor` above confirms "
                "whether it",
                "  #  landed, and names the exact install command if it did not):",
                "  west build -b alp_e1m_aen801_m55_he/ae822fa0e5597ls0/rtss_he `",
                f"      examples\\peripheral-io\\uart-echo -- "
                f"-DEXTRA_ZEPHYR_MODULES={repo_root}",
                "",
                "References:",
                "  - docs\\cross-platform-setup.md  -- the full per-OS setup guide",
                "  - docs\\cli.md                   -- the tan CLI verb reference",
            ]
        )
    else:
        # Routed through `tan build`, not a raw `west build`: the printed
        # success message otherwise routes the customer around tan's own claim
        # to be "the single executor and the user command surface".
        # `--sdk-root`/`--project` are ABSOLUTE because the workspace-parent
        # guard can have just moved the checkout to a sibling
        # `alp-workspace/alp-sdk`, so `$PWD` silently builds from the wrong tree.
        lines.extend(
            [
                "  # Run the local test suite:",
                "  bash scripts/test-all.sh",
                "",
                "  # Or jump straight into building an example for real silicon",
                "  # (needs the Zephyr SDK toolchain -- bootstrap just tried to acquire it",
                "  #  automatically (ADR 0021 Lane 1 P1); `tan doctor` above confirms "
                "whether it",
                "  #  landed, and names the exact install command if it did not):",
                f'  tan build --sdk-root "{tokens.sdk_root}" \\',
                f'      --project "{tokens.sdk_root}/examples/peripheral-io/uart-echo"',
                "",
                "References:",
                "  - docs/testing.md          -- full test-coverage map + how to run "
                "from scratch",
                "  - docs/test-plan.md        -- per-feature verification ledger "
                "(\u23f3 / \U0001f7e1 / \u2705)",
            ]
        )
    return lines


def completion_verdict(blocking: list[str], allow_partial: bool) -> tuple[list[str], bool]:
    """The closing text line(s), and whether the run counts as a SUCCESS,
    given which install phases left the workspace unable to do what it was
    bootstrapped for (tan-cli#220 / tan-cli#285).

    Ported from the Rust oracle's `verdict()`
    (`crates/tan-cli/src/commands/bootstrap/mod.rs`), not re-derived: the
    wording, the named failures and the `--allow-partial` escape hatch are the
    ALREADY-SHIPPED, ALREADY TAGGED (`CHANGELOG.md` `[0.5.0-rc1]`) contract
    tan-cli#220 defined. A second, independently-worded rule for the same
    decision is exactly how this port's closing line, its escape hatch and its
    severity drift from the one alp-sdk-vscode and every other consumer
    already integrated against.

    `blocking` is `Log.blocking()`'s output, in the order the warnings were
    raised: the subset of recorded warning codes after which the workspace
    cannot do what it was bootstrapped for (`WORKSPACE_BLOCKING`). Empty (the
    normal case) reports success, unchanged from before tan-cli#220.

    Printing `bootstrap: complete.` and exiting 0 after a step already warned
    the venv is incomplete is the original defect: both read as an unqualified
    green light, and nothing about the exit code or the closing line told a
    consumer -- human or the extension -- to go look back at a warning that
    may have scrolled off screen minutes earlier (`hidapi`'s wheel build is
    minutes into a cold `west update`). `--allow-partial` is the informed
    escape: it still reports success, but the line still NAMES what did not
    install, so accepting the gap is a choice rather than a silent default.
    """
    if not blocking:
        return ["bootstrap: complete."], True
    named = ", ".join(blocking)
    if allow_partial:
        return (
            [
                "bootstrap: complete.",
                f"  (--allow-partial: {named} did not install; commands that need "
                f"them will fail.)",
            ],
            True,
        )
    return (
        [
            f"bootstrap: INCOMPLETE -- {named} did not install, so this workspace "
            f"cannot build yet.",
            "  The messages above name the remedy for each. Fix them and re-run `tan "
            "bootstrap`, or pass --allow-partial to accept this workspace as-is (the "
            "west workspace and venv are already on disk, and a build that needs none "
            "of the missing packages will still work).",
        ],
        False,
    )


def capture_tail(stdout: bytes | str, stderr: bytes | str, lines: int = 4) -> str:
    """The last few non-empty lines of a failed step's captured output. Prefers
    stderr, falling back to stdout when stderr is empty; `""` when there is
    nothing usable.

    Without this the JSON envelope carried no failure reason at all -- a pip
    traceback, a "no such file" -- because only the exit status was read.

    `lines` defaults to 4 -- unchanged for every existing caller. `west sdk
    install`'s failure path (`bootstrap_cmd._acquire_toolchain`) passes a
    wider window (tan-cli#990 review): a real CI run's `tar --xz` extraction
    failure produced a message naming NO cause at all, because the actual
    subprocess error line sat above the closing frames of `west`'s own
    Python traceback and the 4-line default discarded it along with
    everything else -- a diagnostic gap that cannot be recovered after the
    fact, since the untruncated child output is never written anywhere else.
    """
    text = _as_text(stderr)
    if not text.strip():
        text = _as_text(stdout)
    tail = [line for line in text.splitlines() if line.strip()][-lines:]
    return " | ".join(tail)


def _as_text(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def die(base: str, detail: str) -> str:
    """A fatal message: the script's own `die` text plus whatever detail the
    runner recovered. Text mode usually has none (the child's log already
    streamed), so the bare message is what the user sees there -- no dangling
    colon."""
    return f"{base}: {detail}" if detail.strip() else base
