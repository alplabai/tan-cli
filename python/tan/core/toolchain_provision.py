# SPDX-License-Identifier: Apache-2.0
"""Pure decision logic for ADR 0021 Lane 1 P1: `tan bootstrap` acquiring the
`arm-zephyr-eabi` cross toolchain via `west sdk install --install-dir`, into
the artifact-keyed store `~/.alp/toolchains/<artifact>-<version>/` (or
`$ALP_TOOLCHAIN_ROOT`, ADR 0021's own escape hatch).

**No IO here.** Every function below is a pure transform over already-read
bytes (a manifest's text, a stamp's text, a `shutil.disk_usage` reading, a
platform pair) -- the SAME split `tan.core.bootstrap` already draws for the
venv/west phases. `tan.commands.bootstrap_cmd.toolchain_phase` is the IO
layer: it reads `<sdkRoot>/metadata/toolchains.json`, probes the filesystem
and spawns `west sdk install`, then hands the results through the functions
here to decide what happened and what to do next.

**Why a manifest DIGEST, not just a version compare.** ADR 0021's own words:
"a stamped 1.0.1 store against a moved pin is a Fail with a fix, not 'a
toolchain exists'". A version bump already changes [`store_dir_name`]'s own
directory, so two DIFFERENT versions can never collide on disk -- the digest
exists for the narrower case a version number alone cannot catch: an
artifact's `sha256`/`baseUrl` rotating under an UNCHANGED version string (a
corrected pin, a re-signed release). [`ToolchainManifest.digest`] hashes the
manifest's own raw bytes, so that case invalidates a stamp too, at the one
cost of also invalidating it on a comment-only edit -- the safe direction to
err in, matching `zephyr_python_floor`'s own "prefer the fact over the cached
memory of it" reasoning doctor already applies one layer up.

**Directory-exists is never the success predicate anywhere in this module.**
[`stamp_matches_pin`] is the ONLY verdict function, and it takes a *parsed*
stamp plus a *parsed* manifest -- never a boolean "does the path exist".
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

#: The one cross-toolchain ADR 0021 Lane 1 acquires. `west sdk install`'s own
#: `--gnu-toolchains` vocabulary spells it exactly this way.
TOOLCHAIN_COMPONENT = "arm-zephyr-eabi"

#: The stamp file's name, inside the store directory it describes. Written
#: LAST (`tan.commands.bootstrap_cmd.toolchain_phase`), only once the compiler
#: probe below has actually run and the installed `sdk_version` file has been
#: read back and compared -- never on `west sdk install` merely exiting 0.
STAMP_FILENAME = ".alp-toolchain-stamp.json"

#: Sibling-directory naming for an in-progress/interrupted acquisition.
#: Nothing else under a toolchain root ever produces this suffix, so a
#: directory matching [`wreckage_glob_pattern`] found at the START of a new
#: `tan bootstrap` run is unambiguously wreckage tan itself left behind on a
#: prior interrupted attempt -- reclaimable the same way `ensure_venv`
#: reclaims a corpse venv `_probe_venv_pip` proved absent, never merely
#: unstamped. Never applied to `$ALP_TOOLCHAIN_ROOT` (see [`ToolchainRoot`]):
#: an adopted root may contain directories tan never created, and this
#: pattern existing is not evidence tan created THIS ONE on THIS root.
TMP_SUFFIX_PREFIX = ".tmp-"

#: Safety margin above the manifest's own MEASURED extracted footprint
#: (`measuredFootprint.extractedBytes.wholeSdk`) -- covers filesystem block
#: overhead and the transient extra copy `west sdk install` itself needs
#: mid-extraction (the downloaded archive and the extracted tree briefly
#: coexist inside its own `tempfile.TemporaryDirectory` before the archive is
#: discarded). Not a guess: 15% of ~1.89 GiB is comfortably inside "a rounding
#: error", while still catching a host that is only a few hundred MiB short.
DISK_MARGIN_RATIO = 0.15

#: `<store>/sdk_version` -- the file `west sdk install` itself writes at the
#: SDK's own top level (verified against a real extracted 1.0.1 tree: exact
#: byte content `"1.0.1\n"`), read back and string-compared against the pin
#: AFTER install, never assumed from the version tan asked `west` to install.
#: A `west` that silently resolved a different version (a stale local cache,
#: a future `west` defaulting when `--version` is dropped by a caller error)
#: is caught here rather than trusted.
SDK_VERSION_FILE_RELPATH = "sdk_version"


def gcc_binary_relpath(*, is_windows: bool) -> tuple[str, ...]:
    """`<store>` -> the `arm-zephyr-eabi-gcc` binary's path components.
    Verified against a real extracted `zephyr-sdk-1.0.1` tree:
    `gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc` (SDK 1.0.0+ places GNU
    toolchains under `gnu/`, per `west`'s own `scripts/west_commands/sdk.py`
    comment beside its `gnu_toolchains` walk)."""
    name = "arm-zephyr-eabi-gcc.exe" if is_windows else "arm-zephyr-eabi-gcc"
    return ("gnu", TOOLCHAIN_COMPONENT, "bin", name)


# ---------------------------------------------------------------------------
# Host identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnsupportedHost:
    """This host cannot get the pinned toolchain -- named honestly rather than
    guessed at or silently downgraded to a build that will not run.
    `host_key` is the best key this function could compute (even an invented
    one, for the detail message); `reason` is customer-facing prose."""

    host_key: str
    reason: str


def toolchain_host_key(sys_platform: str, machine: str) -> str | UnsupportedHost:
    """Map `(sys.platform, platform.machine())` onto the
    `metadata/toolchains.json` / `west sdk install` host vocabulary
    (`linux-x86_64`, `linux-aarch64`, `windows-x86_64`, `macos-aarch64`,
    `macos-x86_64`).

    `windows-arm64` is refused BY NAME with the WSL2 redirect ADR 0021 itself
    specifies ("no official Zephyr SDK host build ... must be routed to
    WSL2-aarch64"), rather than silently resolving to `windows-x86_64`
    (nothing on that host can execute those binaries) or falling through to
    the generic "no artifact for this host" message a manifest-driven check
    would otherwise give it, which does not name the WSL2 remedy.
    """
    m = machine.strip().lower()
    if sys_platform.startswith("linux"):
        if m in ("x86_64", "amd64"):
            return "linux-x86_64"
        if m in ("aarch64", "arm64"):
            return "linux-aarch64"
        return UnsupportedHost(f"linux-{m or 'unknown'}", f"no Zephyr SDK host build for linux-{m}")
    if sys_platform == "darwin":
        if m in ("arm64", "aarch64"):
            return "macos-aarch64"
        if m in ("x86_64", "amd64"):
            return "macos-x86_64"
        return UnsupportedHost(f"macos-{m or 'unknown'}", f"no Zephyr SDK host build for macos-{m}")
    if sys_platform in ("win32", "cygwin"):
        if m in ("amd64", "x86_64"):
            return "windows-x86_64"
        if m in ("arm64", "aarch64"):
            return UnsupportedHost(
                "windows-arm64",
                "the pinned Zephyr SDK publishes no windows-arm64 host build (ADR 0021) -- "
                # tan-cli#990 review MINOR: NOT "linux-x86_64 or linux-aarch64" --
                # measured against both the vendored fixture and a live alp-sdk
                # checkout, the pinned manifest publishes linux-x86_64 only (no
                # linux-aarch64 row at all), so a WSL2-aarch64 reader following
                # the old wording landed on this SAME host's own unsupported-host
                # skip, one level down. Name the ONE host this pin can actually
                # serve; widen this sentence again only once the manifest does.
                "run `tan bootstrap` from a WSL2 distro (linux-x86_64) instead; see "
                "docs/cross-platform-setup.md.",
            )
        return UnsupportedHost(f"windows-{m or 'unknown'}", f"no Zephyr SDK host build for windows-{m}")
    return UnsupportedHost(sys_platform or "unknown", f"unrecognised host platform {sys_platform!r}")


# ---------------------------------------------------------------------------
# The manifest -- `<sdkRoot>/metadata/toolchains.json`, read fresh every run
# ---------------------------------------------------------------------------


class ToolchainManifestError(Exception):
    """`<sdkRoot>/metadata/toolchains.json` does not have the shape this
    reader needs. Caught at the one call site (`toolchain_phase`) and reported
    as a non-fatal, `WORKSPACE_BLOCKING`-eligible warning naming the file --
    mirrors `BootstrapManifestError`'s own role for `bootstrap.json`."""


@dataclass(frozen=True)
class ToolchainArtifact:
    host: str
    component: str
    filename: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ToolchainManifest:
    version: str
    base_url: str
    artifacts: tuple[ToolchainArtifact, ...]
    #: `measuredFootprint.extractedBytes.wholeSdk`, when present. `None` on an
    #: older manifest shape that predates it -- [`required_bytes`] treats that
    #: as "cannot preflight", not as "zero bytes needed".
    extracted_bytes_whole_sdk: int | None
    #: The exact bytes read from disk -- what [`digest`] hashes. Kept
    #: verbatim (not re-serialised) so the digest is of what alp-sdk actually
    #: shipped, not of this reader's own re-encoding of it.
    raw_text: str

    def digest(self) -> str:
        """A stable identity for "the pin this manifest declares" -- sha256 of
        the file's own raw bytes. See the module docstring for why the WHOLE
        file, not just the artifact rows."""
        return hashlib.sha256(self.raw_text.encode("utf-8")).hexdigest()

    def artifacts_for_host(self, host_key: str) -> tuple[ToolchainArtifact, ...]:
        return tuple(a for a in self.artifacts if a.host == host_key)


def parse_toolchain_manifest(text: str) -> ToolchainManifest:
    """`<sdkRoot>/metadata/toolchains.json`'s text -> a [`ToolchainManifest`],
    or [`ToolchainManifestError`] naming exactly which required field is
    missing or malformed -- never a bare `KeyError`/`TypeError` escaping to
    the caller, matching `parse_bootstrap_manifest`'s own contract.
    """
    try:
        doc = json.loads(text)
    except ValueError as err:
        raise ToolchainManifestError(f"not valid JSON: {err}") from err
    if not isinstance(doc, dict):
        raise ToolchainManifestError("top level is not a JSON object")
    zephyr_sdk = doc.get("zephyrSdk")
    if not isinstance(zephyr_sdk, dict):
        raise ToolchainManifestError("missing object `zephyrSdk`")
    version = zephyr_sdk.get("version")
    base_url = zephyr_sdk.get("baseUrl")
    if not isinstance(version, str) or not version:
        raise ToolchainManifestError("missing non-empty string `zephyrSdk.version`")
    if not isinstance(base_url, str) or not base_url:
        raise ToolchainManifestError("missing non-empty string `zephyrSdk.baseUrl`")
    raw_artifacts = zephyr_sdk.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise ToolchainManifestError("missing array `zephyrSdk.artifacts`")
    artifacts = tuple(_parse_artifact(i, item) for i, item in enumerate(raw_artifacts))
    extracted = _extracted_whole_sdk(doc)
    return ToolchainManifest(version, base_url, artifacts, extracted, text)


def _parse_artifact(index: int, item: object) -> ToolchainArtifact:
    if not isinstance(item, dict):
        raise ToolchainManifestError(f"artifacts[{index}] is not an object")
    try:
        return ToolchainArtifact(
            host=str(item["host"]),
            component=str(item["component"]),
            filename=str(item["filename"]),
            size_bytes=int(item["sizeBytes"]),
            sha256=str(item["sha256"]),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise ToolchainManifestError(f"artifacts[{index}] malformed: {err}") from err


def _extracted_whole_sdk(doc: dict) -> int | None:
    footprint = doc.get("measuredFootprint")
    if not isinstance(footprint, dict):
        return None
    extracted = footprint.get("extractedBytes")
    if not isinstance(extracted, dict):
        return None
    whole = extracted.get("wholeSdk")
    return whole if isinstance(whole, int) and not isinstance(whole, bool) else None


def artifacts_missing_for_host(manifest: ToolchainManifest, host_key: str) -> bool:
    """True when the pinned manifest publishes NO artifact row for
    `host_key` -- the general, manifest-driven form of the ADR's named Intel
    Mac case (`macos-x86_64` simply has no row at the pinned version): a
    coded, honest skip that needs no per-platform special-casing, and that
    stays correct if alp-sdk adds/drops a host row later.
    """
    return len(manifest.artifacts_for_host(host_key)) == 0


# ---------------------------------------------------------------------------
# The artifact-keyed store
# ---------------------------------------------------------------------------


def store_dir_name(version: str) -> str:
    """`~/.alp/toolchains/<artifact>-<version>/`'s leaf name -- ADR 0021's own
    worked example (`zephyr-sdk-0.17.0-arm-zephyr-eabi`). Keyed by ARTIFACT
    (Zephyr SDK version + component), never by an alp-sdk checkout or SKU, so
    two projects pinning the same Zephyr SDK version share one on-disk copy
    ("Key the toolchain store by SDK version" -- rejected in the ADR's own
    Alternatives for duplicating ~1-17 GB per release)."""
    return f"zephyr-sdk-{version}-{TOOLCHAIN_COMPONENT}"


def wreckage_glob_pattern(leaf_name: str) -> str:
    """The glob a caller runs INSIDE the toolchain root to find a prior
    interrupted attempt at `leaf_name` worth reclaiming before starting a new
    one. See [`TMP_SUFFIX_PREFIX`]."""
    return f"{leaf_name}{TMP_SUFFIX_PREFIX}*"


@dataclass(frozen=True)
class ToolchainRoot:
    #: Where `~/.alp/toolchains` (or the override) actually is.
    path_str: str
    #: True when `$ALP_TOOLCHAIN_ROOT` was set -- an ADOPTED root tan did not
    #: necessarily create. Mirrors the venv-recreate rule's own exception:
    #: wreckage reclamation ([`TMP_SUFFIX_PREFIX`]) still applies (that naming
    #: pattern is tan's own and nothing else produces it), but a bare,
    #: UNSTAMPED store directory under an adopted root is never assumed to be
    #: tan's to delete and start over -- only a genuinely wreckage-suffixed
    #: sibling is.
    adopted: bool


def resolve_toolchain_root(env_override: str | None, home_alp_dir_str: str) -> ToolchainRoot:
    """`$ALP_TOOLCHAIN_ROOT` when set and non-blank (ADR 0021's escape hatch
    "for bench machines and CI"), else `<home_alp_dir>/toolchains`."""
    if env_override and env_override.strip():
        return ToolchainRoot(env_override.strip(), True)
    return ToolchainRoot(f"{home_alp_dir_str.rstrip('/').rstrip(chr(92))}/toolchains", False)


# ---------------------------------------------------------------------------
# The stamp -- written LAST, only after a real compiler probe succeeds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolchainStamp:
    version: str
    manifest_digest: str
    #: The triple `arm-zephyr-eabi-gcc -dumpmachine` (or the fixed
    #: `TOOLCHAIN_COMPONENT` string, when the probe output was not parsed)
    #: reported -- carried through to the doctor detail line so a customer
    #: reading `tan doctor`'s output sees which compiler was actually proven
    #: to run, not just a version number.
    target_triple: str


def render_stamp(stamp: ToolchainStamp) -> str:
    """The stamp file's exact bytes. `schemaVersion` for the same forward-
    compatibility reason every other tan/alp-sdk fact file carries one."""
    return (
        json.dumps(
            {
                "schemaVersion": 1,
                "version": stamp.version,
                "manifestDigest": stamp.manifest_digest,
                "targetTriple": stamp.target_triple,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def parse_stamp(text: str) -> ToolchainStamp | None:
    """`None` on ANY parse problem (missing file, invalid JSON, wrong shape)
    -- an unreadable stamp is treated exactly like "no stamp", never a crash
    and never a stale-but-trusted value."""
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    version = doc.get("version")
    digest = doc.get("manifestDigest")
    triple = doc.get("targetTriple")
    if not (isinstance(version, str) and isinstance(digest, str) and isinstance(triple, str)):
        return None
    return ToolchainStamp(version, digest, triple)


def stamp_matches_pin(stamp: ToolchainStamp | None, manifest: ToolchainManifest) -> bool:
    """The ONE verdict function -- `False` for "no stamp" (`None`) and `False`
    for a stamp naming a different version OR a manifest whose bytes rotated
    under an unchanged version. Never asked "does the directory exist";
    callers that only have a path must read+parse the stamp file first.

    This is ADR 0021's "a stamped 1.0.1 store against a moved pin is a Fail
    with a fix, not 'a toolchain exists'" made literal: `doctor`'s `toolchain`
    check and `bootstrap`'s skip-if-already-installed step both call this,
    and only this, so they cannot independently drift on what "still valid"
    means.
    """
    if stamp is None:
        return False
    return stamp.version == manifest.version and stamp.manifest_digest == manifest.digest()


# ---------------------------------------------------------------------------
# Disk preflight
# ---------------------------------------------------------------------------


def required_bytes(manifest: ToolchainManifest) -> int | None:
    """Bytes tan should refuse to proceed without -- the manifest's own
    measured extracted footprint plus [`DISK_MARGIN_RATIO`]. `None` when the
    manifest predates `measuredFootprint` (an older alp-sdk pin): there is
    nothing to preflight against, so the caller skips the check rather than
    inventing a number -- an unmeasured guess would be exactly the ~17 GB
    `pr-twister.yml` estimate ADR 0021 itself found wrong in both directions.
    """
    if manifest.extracted_bytes_whole_sdk is None:
        return None
    return int(manifest.extracted_bytes_whole_sdk * (1 + DISK_MARGIN_RATIO))


def _gib(num_bytes: int) -> str:
    return f"{num_bytes / (1 << 30):.2f} GiB"


def disk_preflight_refusal(free_bytes: int, needed_bytes: int) -> str | None:
    """`None` when there is enough room; otherwise a message naming BOTH
    numbers -- "Refuse with a number" (ADR 0021's own phrase), never a bare
    "not enough disk space"."""
    if free_bytes >= needed_bytes:
        return None
    return (
        f"only {_gib(free_bytes)} free, but the Zephyr SDK + {TOOLCHAIN_COMPONENT} "
        f"toolchain need about {_gib(needed_bytes)} on disk (the pinned manifest's "
        f"own measured footprint plus a {int(DISK_MARGIN_RATIO * 100)}% margin) -- "
        "free up space and re-run `tan bootstrap`, or pass `--no-toolchain` to skip "
        "cross-toolchain acquisition for now (native_sim and workspace setup are "
        "unaffected)."
    )


#: Below this, a `west sdk install` failure gets a disk-exhaustion note even
#: though the PREFLIGHT passed -- the preflight checks free space once,
#: before anything downloads; the download+extract itself is what actually
#: consumes it, and a host that barely cleared the preflight bar can still
#: run out mid-extraction (the archive and the extracted tree briefly
#: coexist, exactly the margin [`DISK_MARGIN_RATIO`] exists for, but a margin
#: is a cushion, not a guarantee on an already-tight host).
LOW_DISK_AFTER_FAILURE_FLOOR_BYTES = 512 * (1 << 20)


def low_disk_note(free_bytes: int) -> str | None:
    """`None` when `free_bytes` is comfortably above
    [`LOW_DISK_AFTER_FAILURE_FLOOR_BYTES`]; otherwise a note worth appending
    to a `west sdk install` failure message -- disk exhaustion DURING
    extraction reads identically to a dozen other causes, and is worth
    naming as a candidate even now that `west sdk install`'s own capture
    uses a wider tail (`bootstrap_cmd.TOOLCHAIN_INSTALL_TAIL_LINES`, tan-cli
    #990 review) than `tan.core.bootstrap.capture_tail`'s 4-line default:
    the free-space reading here is taken AFTER `west`'s own
    `tempfile.TemporaryDirectory` cleanup already ran, so it can still miss
    a failure that was genuinely disk-caused DURING extraction and only
    looks fine again once the failed attempt's own partial state was
    deleted.
    """
    if free_bytes >= LOW_DISK_AFTER_FAILURE_FLOOR_BYTES:
        return None
    return (
        f"only {_gib(free_bytes)} free on this volume right now -- if the failure "
        "above does not obviously name a cause, low disk space during extraction "
        "is a likely one even though the preflight passed (the preflight checks "
        "space ONCE, before anything is written; a host that barely cleared it can "
        "still run out mid-extraction)."
    )


# ---------------------------------------------------------------------------
# `west sdk install` argv + failure classification
# ---------------------------------------------------------------------------


def west_sdk_install_argv(west: str, *, version: str, install_dir: str) -> list[str]:
    """The one place this argv is assembled -- `--gnu-toolchains`, not the
    deprecated `--toolchains` alias (`scripts/west_commands/sdk.py`: the
    deprecated spelling only warns-and-aliases today, but a customer reading
    `data.plannedCommands` under `--dry-run` should see the command tan will
    actually run, not one the ADR's own text used loosely)."""
    return [
        west,
        "sdk",
        "install",
        "--version",
        version,
        "--gnu-toolchains",
        TOOLCHAIN_COMPONENT,
        "--install-dir",
        install_dir,
    ]


def augment_acquisition_failure(detail: str) -> str:
    """Append the proxy/CA-interference hint (#304's `_TLS_HINT` lesson,
    applied to THIS download) when `detail` -- the captured `west sdk
    install` failure text -- names a checksum mismatch. `west` itself
    already sha256-verifies every archive it downloads against the release's
    own published `sha256.sum` (`scripts/west_commands/sdk.py`,
    `download_and_extract`) and raises `sha256 mismatched: <want>:<got>` on a
    disagreement -- which reads exactly like "the upstream archive is
    corrupt" unless a reader is told a TLS-terminating middlebox rewriting
    the byte stream produces the identical symptom.
    """
    lower = detail.lower()
    if "sha256" in lower or "checksum" in lower:
        return (
            f"{detail} This can mean a TLS-intercepting proxy or corporate CA "
            "rewrote the download in flight rather than the upstream archive "
            "being corrupt -- check ALL_PROXY/HTTPS_PROXY/NO_PROXY, or retry "
            "from a network path that does not intercept TLS."
        )
    return detail


# ---------------------------------------------------------------------------
# Authenticating the SDK download (tan-cli#1143)
# ---------------------------------------------------------------------------

#: The environment variables `tan bootstrap` reads a GitHub credential from,
#: in precedence order, for the `west sdk install` download ONLY.
#:
#: **An environment variable, not a CLI flag, and not a manifest field.** A
#: token is a secret, and the three candidate surfaces are not equally safe
#: for one:
#:
#: - a CLI flag lands the value in shell history, in the host process table
#:   for the whole (multi-minute) run, and in any CI log that echoes the
#:   command -- and `tan`'s own argv is what a customer pastes into a bug
#:   report;
#: - a manifest field puts it in a file people commit;
#: - an environment variable is the one surface that survives none of those,
#:   and is already how every workflow in this repo carries `GH_TOKEN`.
#:
#: The precedence deliberately DEFERS to the existing credential sources
#: rather than inventing a tan-only concept: `GH_TOKEN` then `GITHUB_TOKEN`
#: is `gh`'s own order, so a developer who has run `gh auth login`, and a
#: GitHub Actions job that already exports the workflow token, both get an
#: authenticated download with nothing new to set. `TAN_GITHUB_TOKEN` leads
#: so a host with an ambient `GH_TOKEN` bound to some other account can
#: override it for tan alone.
SDK_TOKEN_ENV_VARS: tuple[str, ...] = ("TAN_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

#: The env var `requests` reads a netrc path from -- `requests.utils.
#: get_netrc_auth` consults `os.environ["NETRC"]` FIRST and falls back to
#: `~/.netrc` only when it is unset. This is the whole reason the token can
#: travel to `west` out of band: `west sdk install` reads a token from
#: `--personal-access-token` and from NOTHING else (measured against Zephyr
#: v4.4.1 `scripts/west_commands/sdk.py:473` -- it consults no environment
#: variable of its own), but its GitHub call goes through `requests`, and
#: `requests` applies netrc credentials whenever the request carries no
#: `Authorization` header -- which is exactly west's `req_headers = {}`
#: branch. West's own rate-limit message names the mechanism: "Try executing
#: install script with --personal-access-token argument **or use a .netrc
#: file**".
NETRC_ENV_VAR = "NETRC"

#: The host `west sdk install`'s rate-limited call goes to (`fetch_releases`
#: against the GitHub REST API). The staged netrc names ONLY this machine, so
#: the credential is never offered to the release CDN the archive itself is
#: fetched from, nor to anything else the child happens to talk to.
GITHUB_API_HOST = "api.github.com"

#: netrc's `login` for a GitHub token. GitHub ignores the username on a
#: token-as-password Basic credential; `x-access-token` is the spelling
#: GitHub's own docs use for it.
NETRC_LOGIN = "x-access-token"

#: A token this module is willing to write into a netrc file. Deliberately a
#: WHITELIST, not a blacklist of separators: `netrc`'s parser is
#: whitespace-and-quote-sensitive, so a value carrying a space, a newline or
#: a `#` does not merely fail to authenticate -- it can make the parser
#: mis-split the file, and `netrc.NetrcParseError` quotes the offending token
#: back in its message, which is precisely a secret arriving in a child's
#: stderr and from there in `capture_tail`. Every GitHub credential format
#: (`ghp_`/`gho_`/`ghs_`/`github_pat_`, and a bare 40-hex classic token) is
#: inside this class, so the guard costs nothing real and closes the
#: injection path completely.
_SDK_TOKEN_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


@dataclass(frozen=True)
class SdkToken:
    """A usable GitHub credential and the NAME of the variable it came from.

    `source` is the only half of this that is ever printed, logged or put in
    an envelope: it is a variable name the customer chose, never the secret.
    """

    #: The secret. Never logged, never in an argv, never in an envelope.
    value: str
    #: The environment variable it was read from -- safe to print.
    source: str


def usable_sdk_token(raw: str | None) -> str | None:
    """`raw` stripped, or `None` when it is empty or carries a character
    [`_SDK_TOKEN_ALLOWED`] refuses. Surrounding whitespace is forgiven (a
    `GH_TOKEN` set from a file read is routinely `"...\\n"`); interior
    whitespace is not, because a value that cannot be written into a netrc
    line unambiguously must not be written into one at all."""
    if raw is None:
        return None
    token = raw.strip()
    if not token:
        return None
    if any(ch not in _SDK_TOKEN_ALLOWED for ch in token):
        return None
    return token


def resolve_sdk_token(environ: Mapping[str, str]) -> SdkToken | None:
    """The first usable credential among [`SDK_TOKEN_ENV_VARS`], in order, or
    `None` when the environment names none -- in which case the download
    stays exactly as unauthenticated as it has always been. A variable that
    is set but empty, or set to something [`usable_sdk_token`] refuses, is
    SKIPPED rather than ending the search: `GITHUB_TOKEN=` is what an unset
    workflow secret expands to, and letting that shadow a perfectly good
    `GH_TOKEN` behind it would be the surprise."""
    for name in SDK_TOKEN_ENV_VARS:
        token = usable_sdk_token(environ.get(name))
        if token is not None:
            return SdkToken(token, name)
    return None


def netrc_text(token: str) -> str:
    """The netrc document staged for the `west sdk install` child -- one
    machine, [`GITHUB_API_HOST`], and nothing else. Trailing newline: Python's
    `netrc` parser is line-oriented and a file whose last line is unterminated
    is a needless edge case."""
    return f"machine {GITHUB_API_HOST}\n  login {NETRC_LOGIN}\n  password {token}\n"


#: The one narrowly-matched marker that says a `west sdk install` failure was
#: GitHub's API quota rather than anything about the pin, the network or the
#: host. `clean-host.yml`'s own rate-limit exclusion already settled the
#: shape of this test in this repo -- match the literal `rate limit`, not a
#: bare `403`, so a CA/permission 403 (the #304 shape) cannot be mistaken for
#: a quota one and handed a remedy that would not fix it.
_RATE_LIMIT_MARKER = "rate limit"


def rate_limit_note(detail: str, *, token_source: str | None) -> str | None:
    """The tan-side remedy to append to a rate-limited `west sdk install`
    failure, or `None` when `detail` says nothing about a quota.

    The same shape as [`low_disk_note`] and [`augment_acquisition_failure`]:
    the child's own message is kept verbatim and a note is appended, because
    that message is actively MISLEADING here -- west tells the reader to
    "try executing install script with --personal-access-token argument",
    which is `west`'s flag on a command the customer never typed and which
    `tan bootstrap` does not accept. Naming tan's own surface is the whole
    point of the note.

    `token_source` splits the two genuinely different situations. With no
    token in play the limit hit is GitHub's anonymous PER-IP quota -- shared
    with everyone behind the same NAT (an office egress, a corporate VPN, a
    hosted runner pool), which is why this reaches a customer long before it
    reaches a lone developer -- and the fix is to set one. With a token
    already supplied the per-IP quota was never the binding limit, so
    repeating "set a token" would send the reader to a lever they have
    already pulled.
    """
    if _RATE_LIMIT_MARKER not in detail.lower():
        return None
    if token_source is not None:
        return (
            f"That is GitHub's AUTHENTICATED quota, not the anonymous per-IP one: "
            f"tan already handed this download the credential in ${token_source}. "
            "Check that the token is valid, unexpired and not already exhausted by "
            "other traffic, or wait for the quota window to reset and re-run "
            "`tan bootstrap`."
        )
    primary, *fallbacks = SDK_TOKEN_ENV_VARS
    alternatives = "/".join(f"${name}" for name in fallbacks)
    return (
        "That is GitHub's anonymous per-IP API quota -- shared with everyone behind "
        "the same egress address, so it can be exhausted by traffic that is not "
        f"yours. Authenticate the download by setting ${primary} (or {alternatives}, "
        "read in that order) to a GitHub token with no scopes -- listing public "
        "releases needs none -- and re-run `tan bootstrap`. Ignore the "
        "`--personal-access-token` flag named above: that is `west`'s own flag, and "
        "`tan bootstrap` does not accept it. tan passes the token to `west sdk "
        "install` out of band, so it never appears in an argv, a log or a "
        "`--dry-run` plan."
    )
