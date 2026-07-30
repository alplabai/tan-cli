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

import json
import os
import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

#: The four hosts the flow distinguishes. Plain strings, not an enum: these ARE
#: the manifest's own `prerequisites.install` keys for three of the four, so a
#: separate enum would only need translating back.
LINUX = "linux"
MACOS = "macos"
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
    exactly that file, and `build`'s auto-bootstrap fires ON its warning. With
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
    prerequisites_windows: tuple[str, ...]
    python_min_version: tuple[int, int]
    #: `prerequisites.install`, keyed `linux`/`macos`/`windows` -> tool ->
    #: command. NOT the `posix`/`windows` split the tool LISTS use: an
    #: apt-shaped command and a brew-shaped one cannot share one `posix` key.
    install: dict[str, dict[str, str]]
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
    from_manifest: bool

    def venv_bin_dir(self, is_windows: bool) -> str:
        return self.venv_windows_bin_dir if is_windows else self.venv_posix_bin_dir

    def prerequisites(self, is_windows: bool) -> tuple[str, ...]:
        """The tool list for this host. The two genuinely differ (`python` vs
        `python3`) and the manifest records that faithfully rather than
        unifying them -- so does this."""
        return self.prerequisites_windows if is_windows else self.prerequisites_posix

    def install_for_host(self, host: str) -> dict[str, str]:
        """THE one place the manifest's `linux`/`macos`/`windows` install keying
        is reconciled with `prerequisites`' `posix`/`windows` tool-list keying.
        Callers resolve once, by host, and hand the resolved map down -- so no
        caller can look a tool up in the wrong OS's table (a POSIX refusal on
        macOS getting Linux's `apt-get` lines).

        `OTHER` (a POSIX host that is neither Linux nor macOS) has no manifest
        entry and is not going to grow one: every tool there reports
        `command: null`. The alternatives are both worse than the `null` -- a
        throw, or handing a BSD user a `brew install` line.
        """
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
        prerequisites_windows=_str_list(
            prerequisites.get("windows"), "prerequisites.windows"
        ),
        python_min_version=python_min_version,
        install=_resolve_install_commands(prerequisites.get("install")),
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
        from_manifest=True,
    )


def _fallback_install_commands() -> dict[str, dict[str, str]]:
    """The install one-liners as `metadata/bootstrap.json` carries them.

    Two callers: the whole-manifest fallback, and `_resolve_install_commands`'s
    gap-fill for a manifest predating alp-sdk#959 (which carried no `install`
    key at all). Note `ninja`'s PACKAGE name differs from the binary name --
    which is the whole argument for carrying these as data rather than guessing.
    """
    return {
        LINUX: {
            "git": "sudo apt-get install -y git",
            "cmake": "sudo apt-get install -y cmake",
            "python3": "sudo apt-get install -y python3",
            "ninja": "sudo apt-get install -y ninja-build",
        },
        MACOS: {
            "git": "brew install git",
            "cmake": "brew install cmake",
            "python3": "brew install python3",
            "ninja": "brew install ninja",
        },
        WINDOWS: {
            "git": "winget install -e --id Git.Git",
            "cmake": "winget install -e --id Kitware.CMake",
            "python": "winget install -e --id Python.Python.3.12",
            "ninja": "winget install -e --id Ninja-build.Ninja",
        },
    }


def _resolve_install_commands(declared: Any) -> dict[str, dict[str, str]]:
    """`prerequisites.install` as parsed, with each EMPTY per-OS map replaced by
    the fallback's.

    PER OS, not whole-subtree: `install: {}` -- or one carrying `windows` alone
    -- is indistinguishable from an absent key after parsing, and filling only
    the whole subtree would hand the absent OSes empty maps. On Windows that is
    the real pre-#959 loss: all four `winget` lines vanish. Emptiness is the
    signal because a SERVED OS map is never legitimately empty (the producer's
    schema requires its keys to equal `prerequisites.<os>`).

    Degrade, do not refuse: every shape handled here is out of contract today,
    and a `ValidationFailure` on a manifest field reaches `tan build` and
    `tan run` through auto-bootstrap.
    """
    fallback = _fallback_install_commands()
    if not isinstance(declared, dict):
        return fallback
    out: dict[str, dict[str, str]] = {}
    for host in (LINUX, MACOS, WINDOWS):
        node = declared.get(host)
        clean = (
            {k: v for k, v in node.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(node, dict)
            else {}
        )
        out[host] = clean or fallback[host]
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
        # `west build` with a CMake error naming nothing useful.
        prerequisites_posix=("git", "cmake", "python3", "ninja"),
        prerequisites_windows=("git", "cmake", "python", "ninja"),
        python_min_version=min_python,
        install=_fallback_install_commands(),
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
    """

    tool: str
    command: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {"tool": self.tool, "command": self.command}


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


def _structured_missing(
    missing: list[str], install: dict[str, str]
) -> tuple[MissingPrerequisite, ...]:
    return tuple(MissingPrerequisite(tool, install.get(tool)) for tool in missing)


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


def windows_refusal(missing: list[str], install: dict[str, str]) -> PrereqFailure:
    """`bootstrap.ps1`'s `$Prereqs` loop: header, one `hint_line` each, the
    reopen-PowerShell tail."""
    lines = ["Missing required tools:"]
    lines.extend(hint_line(tool, install) for tool in missing)
    lines.append("Install the tools above (then reopen PowerShell) and re-run.")
    return PrereqFailure(
        "prerequisites-missing", tuple(lines), _structured_missing(missing, install)
    )


def posix_refusal(missing: list[str], install: dict[str, str]) -> PrereqFailure:
    """`bootstrap.sh`'s one line: the tool names and nothing else -- TWO spaces
    before "Install". The oracle prints no per-tool commands and neither may
    this; alp-sdk#959 changed what the STRUCTURED half carries, not what a POSIX
    user reads."""
    return PrereqFailure(
        "prerequisites-missing",
        (f"Missing required tools: {' '.join(missing)}.  Install them and re-run.",),
        _structured_missing(missing, install),
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
    manifest_floor: tuple[int, int], effective_floor: tuple[int, int], source: str
) -> tuple[str, str] | None:
    """`(code suffix, message)` when the two declared floors disagree, else
    `None`.

    Reported rather than silently reconciled, and worded to match
    `tan.commands.doctor_cmd.python_floor_skew_check` -- doctor raises the same
    verdict as `doctor.pythonFloor`, and two commands describing one manifest
    defect differently is the drift this port keeps hitting. Fires on a
    SUCCESSFUL run too: the host is fine, the manifest is not, and the fix
    belongs in `metadata/bootstrap.json` rather than on the customer's machine.
    """
    if manifest_floor >= effective_floor:
        return None
    return (
        "python-floor-skew",
        f"alp-sdk's {BOOTSTRAP_MANIFEST_REL_PATH} declares pythonMinVersion "
        f"{manifest_floor[0]}.{manifest_floor[1]}, but the build's effective floor is "
        f"{effective_floor[0]}.{effective_floor[1]} (from {source}). bootstrap enforces "
        f"the higher, effective floor, so a host this manifest would have accepted is "
        f"refused here rather than failing later inside Zephyr's CMake configure. Raise "
        f"`prerequisites.pythonMinVersion` to "
        f"{effective_floor[0]}.{effective_floor[1]} in alp-sdk's "
        f"{BOOTSTRAP_MANIFEST_REL_PATH} (and re-run its "
        f"scripts/check_bootstrap_manifest.py drift gate).",
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
) -> list[dict[str, str | None]] | None:
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
    from tan.commands.presets_cmd import infer_runtime_for_core_id  # noqa: PLC0415

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
    if os.path.isabs(trimmed) or ntpath_isabs(trimmed):
        # `\x` on Windows has a root but no drive: rooted-but-driveless is
        # rejected just below, so only a fully absolute path passes here.
        if os.name == "nt" and not re.match(r"^([A-Za-z]:|[\\/]{2})", trimmed):
            raise ValueError(_rooted_no_drive(trimmed))
        return os.path.normpath(trimmed)
    if trimmed.startswith(("/", "\\")):
        raise ValueError(_rooted_no_drive(trimmed))
    return os.path.normpath(os.path.join(cwd, trimmed))


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
        lines.append("  (OS not auto-detected; see docs/testing.md)")
        return lines
    lines.append("")
    lines.extend(f"  {line}" for line in hint.note)
    if hint.command:
        lines.append("")
        lines.append(f"  {hint.command}")
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
                "  # (needs the Zephyr SDK toolchain, which bootstrap does NOT install --",
                "  #  the `tan doctor` above reports it, and names the exact install "
                "command):",
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
                "  # (needs the Zephyr SDK toolchain, which bootstrap does NOT install --",
                "  #  the `tan doctor` above reports it, and names the exact install "
                "command):",
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


def capture_tail(stdout: bytes | str, stderr: bytes | str) -> str:
    """The last few non-empty lines of a failed step's captured output. Prefers
    stderr, falling back to stdout when stderr is empty; `""` when there is
    nothing usable.

    Without this the JSON envelope carried no failure reason at all -- a pip
    traceback, a "no such file" -- because only the exit status was read.
    """
    text = _as_text(stderr)
    if not text.strip():
        text = _as_text(stdout)
    tail = [line for line in text.splitlines() if line.strip()][-4:]
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
