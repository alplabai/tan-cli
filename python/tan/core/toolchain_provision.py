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
import re
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
#: variable of its own; `grep environ` across that file finds only
#: `ZEPHYR_BASE` and `ZEPHYR_SDK_INSTALL_DIR`), but its GitHub call goes
#: through `requests`, and `requests` applies netrc credentials whenever the
#: request carries no `Authorization` header -- which is exactly west's
#: `req_headers = {}` branch. West's own rate-limit message names the
#: mechanism: "Try executing install script with --personal-access-token
#: argument **or use a .netrc file**".
#:
#: **This is STRICTLY NARROWER than the flag it replaces, and that is a
#: feature, not a consolation.** `--personal-access-token` puts an
#: `Authorization: Bearer` header on west's `requests` session, and
#: `requests` carries a session header across a redirect to a different host
#: unless the caller strips it; a netrc credential is matched PER MACHINE, and
#: [`netrc_text`] names only [`GITHUB_API_HOST`]. So the token is offered to
#: the rate-limited `fetch_releases` call and to nothing else -- not to the
#: release CDN the multi-hundred-megabyte archive is actually fetched from,
#: not to anything else the child talks to.
#:
#: **It REPLACES rather than augments the caller's own netrc**, for the
#: duration of that one child. `get_netrc_auth` sets
#: `netrc_locations = (netrc_file,)` when `$NETRC` is set -- there is no
#: `~/.netrc` fallback behind it -- so a caller whose own `~/.netrc`
#: authenticates some other host loses it inside `west sdk install`.
#: Accepted rather than worked around: merging their file into ours would
#: mean copying THEIR secrets into a file tan wrote, which is a worse trade
#: than one child process not seeing an unrelated credential, and the child
#: in question talks to GitHub and its CDN and nothing else. Only ever set
#: when tan actually has a token; an unauthenticated run passes no env at all.
#:
#: **Staging touches disk.** The token is written to a real file (see
#: `bootstrap_cmd._stage_sdk_credential`), not held in memory -- mode 0600
#: inside a 0700 directory under the toolchain root, deleted in a `finally`
#: and swept on the next run if a crash skipped that.
#:
#: **RESIDUAL RISK, recorded because tan pins neither west nor requests --
#: and CHECKED AT RUNTIME since tan-cli#1154.** Two external behaviours make
#: this work and neither is under a version constraint tan controls: (1)
#: `requests.utils.get_netrc_auth` reading `$NETRC` (measured on 2.34.2, which
#: is also the current release on PyPI; present since 2.15), and (2) west's
#: no-token branch leaving `req_headers` EMPTY (measured on Zephyr v4.4.1
#: `sdk.py:478`, unchanged at v4.4.2 `:478` and on `zephyrproject/main`
#: @8a4e180b93e `:479`).
#:
#: **(2)'s MECHANISM was stated wrongly here and the correction is recorded
#: rather than quietly swapped in.** An earlier revision said `requests`
#: "skips netrc entirely for a request that already carries an
#: `Authorization` header". Measured against 2.34.2 -- a real `http.server`, a
#: real netrc, the header read back off the wire -- it does not.
#: `Session.prepare_request` gates netrc on `self.trust_env and not auth and
#: not self.auth` -- the `auth=` ARGUMENT and the session's `trust_env`, never
#: the headers; `PreparedRequest.prepare` runs `prepare_auth` AFTER
#: `prepare_headers`; so a netrc match OVERWRITES an `Authorization` header
#: passed through `headers=`, which is exactly how west passes `req_headers`.
#: A future west that sets a header on that branch therefore does not make
#: TAN's credential inert -- it makes WEST'S inert, under tan's, silently.
#: Either way the user is authenticated as something other than what they were
#: told, and either way the answer is the same: stop claiming authentication
#: and say so.
#:
#: The corollary matters more than the correction, because it says what a
#: SUPPRESSED netrc actually looks like: the only two shapes that stop tan's
#: credential reaching the wire are the `auth=` argument (netrc skipped,
#: west's own credential goes out instead) and `trust_env=False` (measured:
#: `Authorization: None`, nothing goes out at all). Headers are not one of
#: them. Anything downstream that tells a reader otherwise is wrong -- see
#: [`_AUTHENTICATED_QUOTA_NOTE`], which said exactly that until tan-cli#1170,
#: two rounds after this note was corrected: a docstring fix that leaves the
#: customer-facing copy of the same claim standing has fixed nothing.
#:
#: There is no CI gate for this: `requests` is not a tan dependency
#: (`pyproject.toml` -- `import requests` is a `ModuleNotFoundError` in a
#: CI-shaped venv) and `ci.yml` checks out alp-sdk but never zephyr, so a
#: CI-time gate would SKIP everywhere, which is a dead gate rather than a real
#: one. The RUNTIME check is [`west_sdk_netrc_assumptions_hold`], driven by
#: `bootstrap_cmd._west_sdk_netrc_drift` against the `zephyr_base` the
#: bootstrap has already resolved: it runs where the artifact exists and
#: nowhere else, and downgrades the "Authenticating ..." line to a
#: `bootstrap.sdk-credential-unverified` warning when it trips. It watches for
#: BOTH suppressing shapes named above and for the flag the token arrives on
#: -- read its own docstring for the bound on that, which is real: a west that
#: stops using `requests` at all satisfies every fact it checks.
#: [`rate_limit_note`]'s authenticated branch remains the second half of this
#: -- it is the moment a suppressed transport becomes observable, when a
#: credentialled download is rate-limited anyway. Re-verify with
#: `grep -n "req_headers" <zephyr>/scripts/west_commands/sdk.py` and
#: `python -c "import inspect,requests as r; print(inspect.getsource(
#: r.sessions.Session.prepare_request))"` -- the gate itself, not just
#: `get_netrc_auth`, since the gate is where `auth=` and `trust_env` bite.
NETRC_ENV_VAR = "NETRC"

#: The source line `west sdk install`'s NO-TOKEN branch leaves behind -- the
#: `else:` arm of `if args.personal_access_token:`. See
#: [`west_sdk_netrc_assumptions_hold`].
WEST_SDK_NO_TOKEN_HEADERS = "req_headers = {}"

#: The attribute that `else:` is the arm of. Measured present in the real file
#: at Zephyr v4.4.1 `sdk.py:473`. Two jobs: it is what makes
#: [`WEST_SDK_NO_TOKEN_HEADERS`] mean anything (a `req_headers = {}` that is no
#: longer that branch proves nothing about the no-token path), and its
#: DISAPPEARANCE is the "west grew a credential of its own" drift -- a west
#: that reads a token from a config file rather than this flag can leave every
#: other fact below satisfied while tan's netrc is no longer what authenticates
#: the download.
WEST_SDK_TOKEN_FLAG_ATTR = "args.personal_access_token"

#: Every environment variable `scripts/west_commands/sdk.py` is known to read
#: (`sdk.py:231` and `:573` at Zephyr v4.4.1). No credential among them, which
#: is the entire reason tan's token has to travel by netrc rather than by an
#: environment variable west would read directly.
WEST_SDK_KNOWN_ENV_READS = frozenset({"ZEPHYR_BASE", "ZEPHYR_SDK_INSTALL_DIR"})

#: What [`west_sdk_netrc_assumptions_hold`]'s environment-allowlist fact
#: actually sees, and what it does not. It matches TEXT, not reads: measured
#: against a copy of the real v4.4.1 file it TRIPS on a bare COMMENT
#: mentioning `os.environ.get("ZEPHYR_TOOLCHAIN_VARIANT")`, and it stays
#: SILENT on `from os import environ; environ.get("WEST_GITHUB_TOKEN")` and on
#: `os.environ.get(_TOKEN_VAR)` -- an indirect name it cannot resolve. The
#: substring facts have the same two-sided character: `req_headers = dict()`
#: and `req_headers: dict = {}` both TRIP [`WEST_SDK_NO_TOKEN_HEADERS`]
#: despite changing nothing that matters. Widening the regex to close the
#: silences would cost more false trips, which is the trade this file has
#: already made in the other direction -- but a reader must not take silence
#: for proof.
_WEST_SDK_ENV_READ = re.compile(
    r"""os\.(?:environ(?:\.get)?|getenv)\s*[(\[]\s*["']([A-Za-z_][A-Za-z0-9_]*)"""
)

#: An `auth=` keyword argument anywhere in the file. THE shape that makes
#: `requests` skip netrc outright -- `Session.prepare_request` gates the netrc
#: lookup on `self.trust_env and not auth and not self.auth`, re-measured on
#: 2.34.2 -- so a west that grows one takes tan's credential out of the request
#: entirely and puts its own in. Word-bounded, so `oauth=` is not a match;
#: measured ABSENT from the real v4.4.1 file, whose three `requests.get` calls
#: pass only `url`, `headers=`, `params=` and `stream=` (`sdk.py:266`, `:332`,
#: `:340`).
#:
#: Two-sided in the same way [`_WEST_SDK_ENV_READ`] is, and recorded here for
#: the same reason: it matches TEXT, so a comment, a help string or an
#: unrelated helper's `auth=None` default TRIPS it, while a call that reaches
#: the same place indirectly (`**kwargs`, a wrapper that adds the argument
#: itself) is SILENT. Fail-toward-telling is the chosen direction, but the
#: silence is real.
_WEST_SDK_REQUESTS_AUTH_ARG = re.compile(r"\bauth\s*=")

#: The other half of that gate. A `Session(trust_env=False)` was measured to
#: send `Authorization: None` -- no netrc read at all, and unlike the `auth=`
#: case nothing of west's replacing it, so the download simply goes out on the
#: anonymous quota. Matched as a bare substring rather than a call shape, so
#: `s.trust_env = False` on a session built elsewhere trips it too. Measured
#: absent from the real v4.4.1 file, which constructs no `Session` and calls
#: the module-level `requests.get`.
WEST_SDK_TRUST_ENV = "trust_env"


def west_sdk_netrc_assumptions_hold(source: str) -> bool:
    """Whether `source` -- the text of `<zephyr_base>/scripts/west_commands/
    sdk.py` -- still matches what [`NETRC_ENV_VAR`]'s residual-risk note was
    measured against.

    **A TRIPWIRE, not a parser** (tan-cli#1154). It answers one question --
    "is this the shape the netrc route was reasoned about?" -- and deliberately
    does not model west's control flow. Four substring-level facts:

    1. [`WEST_SDK_TOKEN_FLAG_ATTR`] is still present -- west's own token still
       arrives on `--personal-access-token`, not from a config file.
    2. [`WEST_SDK_NO_TOKEN_HEADERS`] is still present -- that flag's `else:`
       arm still sends an empty header dict.
    3. Nothing passes `requests` an [`_WEST_SDK_REQUESTS_AUTH_ARG`] and nothing
       mentions [`WEST_SDK_TRUST_ENV`] -- the only two shapes MEASURED to stop
       tan's netrc reaching the wire.
    4. Every environment variable the file reads is in
       [`WEST_SDK_KNOWN_ENV_READS`]. (Nothing here checks that the flag still
       feeds a header; fact 1 is the whole of what stands behind it.)

    **Fact 3 is why 1, 2 and 4 are not enough.** An `Authorization` header on
    the no-token branch does NOT make tan's credential inert -- the netrc match
    OVERWRITES it (see [`NETRC_ENV_VAR`]) -- so a check watching only for that
    would guard none of the vectors this predicate exists for.

    **It fails toward telling the user, but only for shapes its substrings and
    its regex see, and both directions are real** -- the measured trips and the
    measured silences are recorded on [`_WEST_SDK_ENV_READ`]. The false trip is
    the direction to prefer: it costs one warning and an issue report, while a
    missed one costs a user who is told they are authenticated and is not.

    **STILL UNCOVERED, said plainly rather than left to be inferred.** Two
    drifts satisfy all four facts: a west that stops using `requests`
    altogether (`urllib`, `httpx`), or moves the GitHub call into a module this
    file merely imports -- `$NETRC` then means nothing and no single-file
    substring check can see it -- and a PARTIAL move off
    `--personal-access-token`, since fact 1 is a substring and a rewrite
    leaving `args.personal_access_token` in a deprecation shim or a help string
    still passes it (measured: replacing BOTH of `sdk.py:473`/`:475` trips it,
    replacing only `:473` does not). The absence of a
    `bootstrap.sdk-credential-unverified` warning is not evidence that neither
    happened.
    """
    if WEST_SDK_TOKEN_FLAG_ATTR not in source:
        return False
    if WEST_SDK_NO_TOKEN_HEADERS not in source:
        return False
    if WEST_SDK_TRUST_ENV in source or _WEST_SDK_REQUESTS_AUTH_ARG.search(source):
        return False
    return not (set(_WEST_SDK_ENV_READ.findall(source)) - WEST_SDK_KNOWN_ENV_READS)


def west_sdk_netrc_drift_message(var: str, version: str) -> str:
    """The `bootstrap.sdk-credential-unverified` message.

    **Names what the reader should DO, never what west does internally**
    (tan-cli#1154 acceptance). `req_headers`, `os.environ` and
    `prepare_request` are facts about somebody else's file; a customer whose
    SDK download is about to go out on the anonymous quota needs the two
    commands that get them unstuck and the URL that gets tan fixed.
    """
    return (
        "this workspace's Zephyr handles `west sdk install` credentials "
        "differently from the version tan was measured against, so tan cannot "
        f"confirm the token in ${var} reaches the download -- treat this run as "
        "unauthenticated. If it fails on a GitHub rate limit, retry from a "
        "network with its own egress address, or run `west sdk install "
        f"--version {version} -t {TOOLCHAIN_COMPONENT}` by hand from your west "
        "workspace's top-level directory with a `~/.netrc` entry for "
        f"{GITHUB_API_HOST}. Please report this at "
        "https://github.com/alplabai/tan-cli/issues so tan can be updated."
    )


#: The host `west sdk install`'s rate-limited call goes to (`fetch_releases`
#: against the GitHub REST API). The staged netrc names ONLY this machine, so
#: the credential is never offered to the release CDN the archive itself is
#: fetched from, nor to anything else the child happens to talk to.
GITHUB_API_HOST = "api.github.com"

#: The prefix of the private directory the staged netrc lives in, created
#: under the TOOLCHAIN ROOT rather than under `$TMPDIR` -- see
#: [`netrc_scratch_glob_pattern`] for why that placement is what makes the
#: crash-residue sweep safe. Leading dot, so it can never collide with a
#: [`store_dir_name`] result or with a [`TMP_SUFFIX_PREFIX`] sibling.
NETRC_SCRATCH_PREFIX = ".alp-netrc-"


def netrc_scratch_glob_pattern() -> str:
    """The glob that reclaims a PRIOR run's staged credential -- the
    crash-residue half of the cleanup, since a `finally` does not run on
    SIGKILL, an OOM kill or power loss, and a secret accumulating one copy
    per crash with nothing sweeping it is the wrong steady state.

    The reason this pattern is rooted at the toolchain root and not at
    `$TMPDIR` is that the sweep's only proof of provenance is the NAME, and a
    name is only proof inside a namespace tan owns. `$TMPDIR` is shared by
    every process on the box, so a `.alp-netrc-*` found there is not
    necessarily one tan wrote; the toolchain root (or the directory
    `$ALP_TOOLCHAIN_ROOT` deliberately named) is the same ownership argument
    [`wreckage_glob_pattern`] already relies on, so this sweep is exactly as
    well-founded as the one running beside it.

    **Not, as an earlier revision claimed, because sweeping `/tmp` would be
    exploitable** -- corrected on measurement (tan-cli#1148 round 2): `/tmp`
    is `0o1777`, but `shutil.rmtree.avoids_symlink_attacks` is `True` here,
    so no escalation was ever demonstrated and this should not have asserted
    one. What the move measurably DID buy is a swept namespace that is
    NARROWER than `$TMPDIR`, not one that is unconditionally private, and the
    difference is the caller's `umask`. Measured under `umask 022`: root
    `0o755`, scratch `0o700`, netrc `0o600` -- no other user can create a
    `.alp-netrc-*` here. Measured under `umask 002` (this repo's own CI and
    developer default): root `0o775`, so the root is GROUP-WRITABLE and a
    member of that group can. The credential itself is unreadable either way
    (`mkdtemp` forces `0o700` and the netrc `0o600` regardless of umask); the
    part that varies is who can put a directory into the namespace the sweep
    globs.

    Two residuals follow from that and are recorded rather than claimed away:
    a group-writable root under `umask 002`, and an `$ALP_TOOLCHAIN_ROOT`
    that the operator deliberately pointed at a SHARED directory. In both,
    the sweep can be handed a `.alp-netrc-*` tan did not write. What that
    buys an attacker is bounded by what the sweep does -- `rmtree` of a
    directory, with `avoids_symlink_attacks` true -- so it is a nuisance
    (deleting their own planted directory) rather than an escalation; but
    "narrower than `$TMPDIR`, and private at `umask 022`" is the honest
    statement, not "not creatable at all".
    """
    return f"{NETRC_SCRATCH_PREFIX}*"


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


def rejected_sdk_token_vars(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The [`SDK_TOKEN_ENV_VARS`] that are SET to a non-blank value this
    module will not use -- [`usable_sdk_token`] refused the characters.

    Exists so the refusal is not silent. The realistic way to land here is a
    `.env` line or a shell export that kept its literal quotes
    (`GH_TOKEN="ghp_..."` read by something that does not strip them), and
    the symptom without this is a download that is anonymous for a reason
    nothing on screen names, followed by a remedy telling the reader to set
    a variable they can see is already set.

    A variable that is unset or blank is NOT reported: `GITHUB_TOKEN=` is
    what an unset workflow secret expands to, and warning about it on every
    CI run would be noise, not signal.
    """
    return tuple(
        name
        for name in SDK_TOKEN_ENV_VARS
        if (environ.get(name) or "").strip() and usable_sdk_token(environ.get(name)) is None
    )


def shadowed_sdk_token_vars(
    environ: Mapping[str, str], winner: SdkToken | None
) -> tuple[str, ...]:
    """The [`rejected_sdk_token_vars`] that would have WON precedence over
    `winner` -- all of them when `winner` is `None`.

    `winner` is a REQUIRED argument rather than something this re-derives:
    the one caller has already called [`resolve_sdk_token`], and resolving
    the same three variables twice invites the two answers to disagree the
    day anything about the environment is not stable between them
    (tan-cli#1148 round 3).

    This, not the raw rejected set, is what is worth telling a customer
    about. A variable BEHIND the one that won was never going to be consulted
    (`resolve_sdk_token` takes the first usable and stops), so reporting it
    is noise about a value that changed nothing; a variable AHEAD of it lost
    a race it would otherwise have won, which is a real surprise worth
    naming.
    """
    order = SDK_TOKEN_ENV_VARS
    limit = order.index(winner.source) if winner is not None else len(order)
    return tuple(name for name in rejected_sdk_token_vars(environ) if order.index(name) < limit)


#: What a customer is told about a variable [`usable_sdk_token`] refused.
#: `{name}` is the variable, never the value -- a rejected token is as much a
#: secret as an accepted one.
_UNUSABLE_TOKEN_PREFIX = (
    "${name} is set but is not a value tan can use as a GitHub token (letters, "
    "digits, `_`, `-` and `.` only -- a quoted `.env` value keeps its quotes)"
)

#: The two possible ENDINGS, chosen from what actually happened rather than
#: from what was about to be attempted. tan-cli#1148 review round 2: this
#: warning used to be raised before the resolve, so with
#: `TAN_GITHUB_TOKEN='"quoted"'` AND a good `GH_TOKEN` the envelope carried a
#: registered issue code asserting the download "will go out unauthenticated"
#: while it went out authenticated. A wire surface stating the opposite of
#: what happened is worse than the silence this warning replaced.
_UNUSABLE_TOKEN_FELL_BACK = "; tan authenticated the download with ${fallback} instead."
_UNUSABLE_TOKEN_UNAUTHENTICATED = "; the Zephyr SDK download will go out unauthenticated."


def unusable_token_message(name: str, *, authenticated_as: str | None) -> str:
    """The `bootstrap.sdk-credential-unstaged` message for one refused
    variable. `authenticated_as` is the variable whose token really reached
    the download by the time this is rendered -- `None` if none did."""
    text = _UNUSABLE_TOKEN_PREFIX.format(name=name)
    if authenticated_as is not None:
        return text + _UNUSABLE_TOKEN_FELL_BACK.format(fallback=authenticated_as)
    return text + _UNUSABLE_TOKEN_UNAUTHENTICATED


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


#: The AUTHENTICATED-quota branch of [`rate_limit_note`]. `{source}` is the
#: variable name, never the value. The closing sentence is deliberate: an
#: authenticated download that is rate-limited anyway is the only live symptom
#: a SUPPRESSED netrc would ever produce (see [`NETRC_ENV_VAR`]), and this is
#: exactly the moment it would show.
#:
#: **It named the wrong shape until tan-cli#1170, and this is the only
#: USER-FACING string that carried the claim.** It used to close with "a
#: `west` that sets its own Authorization header would silently bypass it".
#: Measured on `requests` 2.34.2, a header does not bypass anything -- the
#: netrc match OVERWRITES it, so tan's credential is the one that goes out. A
#: customer following that sentence would have filed a west bug about a header
#: that demonstrably suppresses nothing, and gone on believing the netrc route
#: was the suspect. The two shapes that really do leave it unread are the
#: `auth=` argument and `trust_env=False`, so those are what it names now, and
#: what [`west_sdk_netrc_assumptions_hold`] watches the file for.
_AUTHENTICATED_QUOTA_NOTE = (
    "That is GitHub's AUTHENTICATED quota, not the anonymous per-IP one: tan "
    "already handed this download the credential in ${source}. Check that the token "
    "is valid, unexpired and not already exhausted by other traffic, or wait for the "
    "quota window to reset and re-run `tan bootstrap`. If you are confident the token "
    "is good and this persists, the credential may not be reaching `west sdk install` "
    "at all -- tan hands it over through a netrc, which a `west` that passes its HTTP "
    "client its own credential argument, or that builds a session with the "
    "environment switched off, would leave unread; please report it with your "
    "`west --version` (tan-cli#1143)."
)

#: The branch for "tan staged and passed a credential, but this run had
#: already withdrawn the claim that it reaches the download" -- the state
#: `bootstrap.sdk-credential-unverified` puts the run into. `{source}` is the
#: variable name, never the value.
#:
#: It exists so the two surfaces cannot contradict each other inside one run
#: (tan-cli#1170). The thing it must NOT do is what
#: [`_AUTHENTICATED_QUOTA_NOTE`] does -- open with "that is GitHub's
#: AUTHENTICATED quota". That sentence is an INFERENCE from "tan handed over a
#: credential", and the `sdk-credential-unverified` warning is precisely tan
#: saying it can no longer draw it. So this branch names both possibilities and
#: hands the reader back to the remedies already printed above, rather than
#: repeating them or picking a quota it cannot see.
_UNVERIFIED_QUOTA_NOTE = (
    "tan cannot tell you which of GitHub's two quotas that is. It staged the "
    "credential in ${source} and passed it to `west sdk install`, but it also warned "
    "above (`sdk-credential-unverified`) that this workspace's `west` no longer "
    "matches the shape that route was measured against. So this is either the "
    "AUTHENTICATED quota -- check the token is valid, unexpired and not already "
    "exhausted by other traffic, then wait for the window to reset and re-run "
    "`tan bootstrap` -- or the anonymous per-IP one, shared with everyone behind the "
    "same egress address. The two remedies in that warning cover both cases."
)

#: The branch for "the environment HELD a credential and this download went
#: out anonymous anyway". Points at the `bootstrap.sdk-credential-unstaged`
#: warning that says WHY, instead of repeating advice the reader has already
#: followed.
_UNUSED_CREDENTIAL_NOTE = (
    "That is GitHub's anonymous per-IP API quota, and this download went out "
    "anonymous even though ${source} is set -- tan could not use it, and said so in "
    "the warning above rather than here. Fix that first: re-running with a usable "
    "credential is what lifts this limit."
)

#: The plain anonymous branch -- the one a customer behind a shared egress
#: address actually hits, and the only one that names a variable to set.
_ANONYMOUS_QUOTA_NOTE = (
    "That is GitHub's anonymous per-IP API quota -- shared with everyone behind the "
    "same egress address, so it can be exhausted by traffic that is not yours. "
    "Authenticate the download by setting ${primary} (or {alternatives}, read in that "
    "order) to a GitHub token with no scopes -- listing public releases needs none -- "
    "and re-run `tan bootstrap`. Ignore the `--personal-access-token` flag named "
    "above: that is `west`'s own flag, and `tan bootstrap` does not accept it. tan "
    "passes the token to `west sdk install` out of band, so it never appears in an "
    "argv, a log or a `--dry-run` plan."
)


def rate_limit_note(
    detail: str,
    *,
    authenticated_as: str | None,
    credential_seen: str | None = None,
    verified: bool = True,
) -> str | None:
    """The tan-side remedy to append to a rate-limited `west sdk install`
    failure, or `None` when `detail` says nothing about a quota.

    The same shape as [`low_disk_note`] and [`augment_acquisition_failure`]:
    the child's own message is kept verbatim and a note is appended, because
    that message is actively MISLEADING here -- west tells the reader to
    "try executing install script with --personal-access-token argument",
    which is `west`'s flag on a command the customer never typed and which
    `tan bootstrap` does not accept. Naming tan's own surface is the whole
    point of the note.

    FOUR states, because there are four genuinely different situations and
    collapsing any two of them hands somebody advice that cannot help:
    a credential that reached the download
    ([`_AUTHENTICATED_QUOTA_NOTE`]), one that was staged and passed but whose
    transport tan could not vouch for ([`_UNVERIFIED_QUOTA_NOTE`]), one that
    was present but unusable ([`_UNUSED_CREDENTIAL_NOTE`]), and none at all
    ([`_ANONYMOUS_QUOTA_NOTE`]).

    `verified` is the fourth, added in tan-cli#1170 because without it this
    function contradicted a warning printed minutes earlier in the same run:
    `bootstrap.sdk-credential-unverified` says "treat this run as
    unauthenticated", and the authenticated branch then said "tan already
    handed this download the credential in ${source}". Two wire surfaces
    asserting opposite facts about one download is the shape
    `bootstrap_cmd._sdk_credential`'s own docstring calls worse than the
    silence the warning replaced.
    """
    if _RATE_LIMIT_MARKER not in detail.lower():
        return None
    if authenticated_as is not None:
        if not verified:
            return _UNVERIFIED_QUOTA_NOTE.format(source=authenticated_as)
        return _AUTHENTICATED_QUOTA_NOTE.format(source=authenticated_as)
    if credential_seen is not None:
        return _UNUSED_CREDENTIAL_NOTE.format(source=credential_seen)
    primary, *fallbacks = SDK_TOKEN_ENV_VARS
    return _ANONYMOUS_QUOTA_NOTE.format(
        primary=primary, alternatives="/".join(f"${name}" for name in fallbacks)
    )
