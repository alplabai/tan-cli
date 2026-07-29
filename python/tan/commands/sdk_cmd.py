# SPDX-License-Identifier: Apache-2.0
"""`tan sdk <list|install|current|switch>` -- which alp-sdk checkout is active.

Mirrors `crates/tan-cli/src/commands/sdk.rs`. A subcommand GROUP, but not a
Click/clap subcommand group: the Rust takes `subcommand` and `arg` as two plain
positionals (`cli.rs`'s `SdkArgs`), which is precisely why an unknown verb
reaches the command's own dispatch and gets a real envelope instead of a
parser-level usage error. Reproduced here for the same reason -- promoting
`sdk` to a `typer.Typer()` sub-app would hand `tan sdk bogus` to Click, which
exits 2 with usage text on stderr and NOTHING on stdout. The
`sdk-unknown-subcommand` golden pins exit **1** and a full envelope, so the
group has to stay flat.

Two committed goldens drive this file:

* `sdk-current-no-sdk` -- `sdk current` in a workspace with no SDK anywhere:
  `sdkPath: null`, `readiness: null`, `sourceTier: "none"`, exit 0. It is a
  SUCCESS: "nothing is selected" is an answer, not a failure.
* `sdk-unknown-subcommand` -- `sdk bogus`, exit 1. Note the asymmetry, which is
  the contract and not a bug to tidy: the failure envelope carries the
  **`list`-shaped** payload (`{"subcommand": "list", "releases": []}`), because
  the Rust builds its unknown-verb failure out of `ListData`. `data.releases` is
  what the extension reads with a `?? []` fallback, so the shape is deliberate.

**It cannot hang.** `current` is pure filesystem probing -- four tier lookups,
a readiness stat, and an optional `~/.alp/sdk-cache` listing. No subprocess, no
network, no prompt. That is a property of the no-checkout golden, not an
accident of it.

**Network is opt-in.** `sdk list` is the one verb that talks to GitHub, and it
only does so behind an explicit `--online`; without the flag it refuses with a
coded issue rather than reaching out. The fetch itself carries an explicit
timeout, because a `urlopen` with no timeout inherits the socket default of
"forever" and a CI job driving `--format json` would hang until the runner
killed it (I-23's failure mode, arrived at through a socket instead of a
prompt).

**No SDK is ever shelled.** Nothing here runs `python -m alp_cli` or
`alp_project.py`: readiness is a stat of `scripts/alp_project.py`,
`metadata/`, `VERSION` and `metadata/sdk_version.yaml`. I-32 and the
port-invariants anti-pattern list (#22, "Ship `tan init` shelling the SDK
instead of reading the vendored scaffold tree") forbid giving a command an
alp-sdk-checkout dependency it does not have; `sdk current`'s whole job is to
report on a checkout that may not exist at all, so shelling one would be
circular as well as forbidden.

**tan learns no hardware fact** (I-26). This file knows one path literal --
`scripts/alp_project.py`, the SDK-root marker (I-31) -- and no SKU, address,
pin name or vendor branch.

NOT PORTED, and loud about it: `install` and `switch`. Both refuse with
`sdk.not-ported` (exit 5) rather than half-working. `switch` in particular must
not land partially: the Rust writes the active-SDK pointer AND reconciles
`<topdir>/.west/config`'s own `manifest.path` (tan-cli #62/#31), and a version
that writes the pointer while silently skipping the reconciliation reports
success while `west` keeps resolving the manifest from the stale pointer --
which is the bug #62 reported, re-introduced invisibly. A loud refusal is
strictly safer than that.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode

#: GitHub Releases API endpoint for `alplabai/alp-sdk`, from
#: `tan_core::sdk::GITHUB_RELEASES_URL`.
GITHUB_RELEASES_URL = "https://api.github.com/repos/alplabai/alp-sdk/releases"

#: `scripts/alp_project.py` is THE marker for an alp-sdk checkout (I-31) -- the
#: same literal `tan_core::project::contains_loader_script` hardcodes. Spelled
#: once here; `build_cmd.SDK_MARKER` is the same fact for the build path.
SDK_MARKER = ("scripts", "alp_project.py")

#: Wall-clock ceiling on the ONE network call in this file. Every subprocess and
#: socket probe in the port carries a timeout; `urlopen` without one blocks on
#: the socket default (no timeout at all), which turns an unreachable proxy into
#: a hung `--format json` consumer rather than an error it can render.
NETWORK_TIMEOUT_SECONDS = 20.0

#: The verbs `run` dispatches, for the unknown-subcommand text line. Verbatim
#: from `sdk.rs`'s failure text.
AVAILABLE_SUBCOMMANDS = (
    "Available subcommands: list, install <version>, current, switch <version>"
)

#: `sdk list`'s proxy/CA hint, verbatim from `tan_core::sdk::describe_network_error`.
#: Names only the variables that actually steer an `https://` request -- neither
#: tan nor git applies `HTTP_PROXY` to one, so naming it sent users to edit a
#: variable that changes nothing.
_PROXY_HINT = (
    "Check ALL_PROXY/HTTPS_PROXY/NO_PROXY — the configured proxy refused or "
    "could not complete the connection."
)

_TLS_HINT = (
    "This is usually a TLS-intercepting proxy or a corporate CA that this "
    "machine does not trust, not a broken connection."
)


# ── filesystem primitives (every failure is a value, never an exception) ─────


def _read_file(path: Path) -> str | None:
    """`tan_core`'s injected `read_file`: contents, or `None` on ANY read
    failure -- missing, a directory, permission-denied, non-UTF-8 bytes.

    `encoding="utf-8"` explicitly (I-27): a bare `read_text()` decodes with the
    host locale, so a pointer file or `sdk_version.yaml` carrying one non-ASCII
    byte raises `UnicodeDecodeError` on a cp1252 Windows host and passes on
    ubuntu CI. Swallowing it to `None` is what the Rust does and is what keeps
    an unreadable file a reported fact instead of a traceback.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _has_loader_script(root: Path) -> bool:
    """True when `root/scripts/alp_project.py` exists -- `util.rs`'s
    `has_loader_script`. `Path.exists()` swallows its own `OSError`/`ValueError`
    (a too-long path, an illegal name), so a pathological pointer value reads as
    "not an SDK" rather than raising out of a tier lookup."""
    return root.joinpath(*SDK_MARKER).exists()


def _to_posix(path: Path) -> str:
    """`tan_core::project::to_posix`. The discovery tier's result is a reported
    path, and every golden-pinned path field is platform-identical forward
    slashes; `Path` renders `\\` on Windows."""
    return str(path).replace("\\", "/")


def _home_alp_dir() -> Path:
    """`~/.alp` -- `USERPROFILE` on Windows else `HOME`, falling back to `.`
    when neither is set (`util.rs`'s `home_alp_dir`). Home of the global default
    pointer and the install cache, and the reason the conformance harness
    overrides BOTH variables: a developer's real `~/.alp/sdk-default` would
    otherwise decide what `sdk current` reports."""
    home = os.environ.get("USERPROFILE" if os.name == "nt" else "HOME")
    return Path(home or ".") / ".alp"


def _pointer_target(pointer: Path) -> str | None:
    """The `sdkPath` out of a `{"sdkPath": ..., "updatedAt": ...}` pointer file.

    One function for both pointers -- `tan_core`'s `resolve_active_sdk`
    (`<workspace>/.alp/sdk-path`) and `resolve_global_default_sdk`
    (`~/.alp/sdk-default`) are the same read of the same shape at two paths.
    Every failure is `None`, matching the Rust's `.ok()?` chain: a hand-edited
    pointer holding invalid JSON, a list, or no `sdkPath` at all must fall
    through to the next tier, not abort the command.
    """
    if not pointer.exists():
        return None
    raw = _read_file(pointer)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("sdkPath")
    return value if isinstance(value, str) else None


# ── readiness (tan_core::sdk::check_sdk_readiness) ──────────────────────────


def parse_sdk_version_yaml(text: str) -> str | None:
    """`metadata/sdk_version.yaml`'s version: a `version:` line if present, else
    the first bare (colon-free) scalar. A hand-rolled scan, deliberately, exactly
    as the Rust is: the file is comment-heavy prose around a single scalar, and
    tan ships no YAML dependency the offline paths can rely on.
    """
    bare: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("version:"):
            value = line[len("version:") :].strip().strip('"').strip("'")
            if value:
                return value
        elif bare is None and ":" not in line:
            bare = line.strip('"').strip("'")
    return bare


def check_sdk_readiness(sdk_path: str) -> dict[str, Any]:
    """Inspect a local SDK path. Never raises; a problem is an `issues[]` string.

    `version` reads `VERSION` FIRST and falls back to
    `metadata/sdk_version.yaml` (tan-cli #162): no released alp-sdk ships a
    top-level `VERSION` file, so before the fallback every `sdk current`
    reported `version (unknown)` while `tan doctor` resolved a real one from the
    other file. `VERSION` stays first so an extracted release archive that DOES
    carry one is never regressed.

    `state` is `missing` on a absent loader script (not an SDK root at all),
    `partial` when the marker is there but something else is not, else `ready`.
    Note the asymmetry that follows: a missing `metadata/` adds an issue but
    keeps `loaderScriptPresent` true, so the verdict is `partial`, never
    `missing`.
    """
    root = Path(sdk_path)
    issues: list[str] = []

    loader_present = _has_loader_script(root)
    if not loader_present:
        issues.append(
            f'scripts/alp_project.py not found — "{sdk_path}" is not a valid '
            "Alp SDK root."
        )

    metadata_present = (root / "metadata").exists()
    if not metadata_present:
        issues.append("metadata/ directory is missing.")

    version: str | None = None
    version_file = root / "VERSION"
    if version_file.exists():
        contents = _read_file(version_file)
        if contents is None:
            issues.append("VERSION file could not be read.")
        else:
            # An empty/whitespace VERSION is "no version", not the empty string
            # -- `format_readiness_block` renders `None` as `(unknown)`.
            version = contents.strip() or None
    if version is None:
        sdk_version_yaml = root / "metadata" / "sdk_version.yaml"
        if sdk_version_yaml.exists():
            contents = _read_file(sdk_version_yaml)
            if contents is not None:
                version = parse_sdk_version_yaml(contents)

    if not loader_present:
        state = "missing"
    elif issues:
        state = "partial"
    else:
        state = "ready"

    return {
        "sdkPath": sdk_path,
        "version": version,
        "loaderScriptPresent": loader_present,
        "metadataPresent": metadata_present,
        "state": state,
        "issues": issues,
    }


def empty_readiness(sdk_path: str) -> dict[str, Any]:
    """The placeholder `missing` report a failure envelope carries when no real
    check ran (`sdk.rs`'s `empty_readiness`) -- so `data.readiness` keeps its
    full key set on every path instead of appearing and disappearing."""
    return {
        "sdkPath": sdk_path,
        "version": None,
        "loaderScriptPresent": False,
        "metadataPresent": False,
        "state": "missing",
        "issues": [],
    }


# ── the four-tier precedence chain ──────────────────────────────────────────


@dataclass(frozen=True)
class ActiveSdk:
    """What `sdk current` reports: the active path (or `None`) and the tier that
    produced it. `tier` is the wire string, camelCase, from `SdkSourceTier`."""

    path: str | None
    tier: str


def _nearest_ancestor_sdk(start: Path) -> str | None:
    """The nearest ENCLOSING checkout, walking `start`'s parents upward.
    `start` itself is deliberately not probed -- every caller checks it first
    (`tan_core::project::nearest_ancestor_sdk`).

    This is what makes the documented Quickstart resolve: `tan --project
    examples/<cat>/<name>` puts the workspace root levels BELOW the checkout it
    was invoked from (tan-cli #101). The walk yields at most ONE path, so it can
    never turn an otherwise-unambiguous resolution into an ambiguous `None`.
    """
    for ancestor in start.parents:
        if _has_loader_script(ancestor):
            return _to_posix(ancestor)
    return None


def discover_workspace_sdk(workspace_root: Path) -> str | None:
    """Auto-discovery for the `discovery` tier: the workspace root itself or its
    SIBLING `../alp-sdk`, else the nearest enclosing checkout. Two or more
    candidates is ambiguous, which is `None` -- not a choice.

    **Deliberately NOT `build_cmd.discover_sdk_root`**, which is a different
    Rust function (`util.rs`'s `discover_sdk_root`) with a WIDER candidate set:
    it also probes the child `<ws>/alp-sdk` and `../alp-sdk-upstream`, and takes
    the first match rather than requiring uniqueness. `sdk current` must mirror
    `tan_core::discover_workspace_sdk` instead, per `resolve_sdk_tiered`'s own
    doc comment: the tier it reports has to be what build/validate/doctor would
    actually resolve here, or `sourceTier: "discovery"` names a path no other
    command agrees with. Reusing the build-side helper would be the tempting
    de-duplication and it would make the report lie.
    """
    candidates: list[str] = []
    lateral_hit = False

    if _has_loader_script(workspace_root):
        # Normalised BEFORE the dedup below: on Windows a root spelled with
        # backslashes and its `parent/alp-sdk` sibling can name the same
        # directory yet compare unequal as strings, which counted one SDK twice
        # and reported "ambiguous".
        candidates.append(_to_posix(workspace_root))
        lateral_hit = True

    # `parent != self` is pathlib's spelling of Rust's `Path::parent()` returning
    # `None`: at a filesystem/drive root `Path("C:/").parent` is `Path("C:/")`
    # itself, so an unguarded probe would invent a `C:/alp-sdk` candidate the
    # Rust never considers -- flipping `sourceTier` from `none` to `discovery`
    # for anyone whose cwd is a drive root.
    parent = workspace_root.parent
    if parent != workspace_root:
        sibling = _to_posix(parent / "alp-sdk")
        if _has_loader_script(Path(sibling)):
            if sibling not in candidates:
                candidates.append(sibling)
            lateral_hit = True

    # A strict fallback, gated on whether THIS folder's lateral probes answered
    # -- never on the candidate count, which stays unchanged for a folder that
    # resolved perfectly well but deduped against an earlier one.
    if not lateral_hit:
        ancestor = _nearest_ancestor_sdk(workspace_root)
        if ancestor is not None:
            candidates.append(ancestor)

    return candidates[0] if len(candidates) == 1 else None


def resolve_sdk_tiered(sdk_root: str | None, workspace_root: Path) -> ActiveSdk:
    """`--sdk-root` > project pin > global default > discovery > nothing
    (`util.rs`'s `resolve_sdk_tiered` + `tan_core`'s `resolve_sdk_source_tier`).

    `--sdk-root` is TERMINAL and returned as-is **even when it is not a
    checkout** (I-31). That is not an oversight to tidy: a bad `--sdk-root`
    surfaces as a `missing` readiness naming the path the user typed, where the
    Pythonic `if not valid: continue` would silently fall through to a lower
    tier and report a DIFFERENT SDK than the one they asked for.

    Both pointer tiers are best-effort by contrast -- each is used only while it
    still points at a real checkout -- so a stale pointer falls through instead
    of locking the user out of every command.
    """
    flag = (sdk_root or "").strip()
    if flag:
        return ActiveSdk(flag, "sdkRootFlag")

    pin = _pointer_target(workspace_root / ".alp" / "sdk-path")
    if pin is not None and _has_loader_script(Path(pin)):
        return ActiveSdk(pin, "projectPin")

    default = _pointer_target(_home_alp_dir() / "sdk-default")
    if default is not None and _has_loader_script(Path(default)):
        return ActiveSdk(default, "globalDefault")

    discovered = discover_workspace_sdk(workspace_root)
    if discovered is not None:
        return ActiveSdk(discovered, "discovery")

    return ActiveSdk(None, "none")


def _default_cache_root() -> Path:
    """`~/.alp/sdk-cache` -- where `tan sdk install` clones to."""
    return _home_alp_dir() / "sdk-cache"


def cached_sdk_versions(cache_root: Path | None = None) -> list[str]:
    """Directory names directly under the cache root that hold a real checkout,
    sorted. An unreadable or absent cache root is an empty list, never an error.

    This is what lets `current`'s "nothing selected" message say `switch`
    instead of sending the user back to `install` -- tan-cli #162, a loop with
    no exit. Deliberately does not guess WHICH version to switch to; it lists
    what exists and lets the user choose.
    """
    root = _default_cache_root() if cache_root is None else cache_root
    try:
        return sorted(entry.name for entry in root.iterdir() if _has_loader_script(entry))
    except OSError:
        return []


# ── human text (stderr only; stdout is the envelope channel) ─────────────────


def no_active_sdk_text(cached: list[str]) -> list[str]:
    if not cached:
        return [
            "No active SDK configured for this workspace.",
            "Run 'tan sdk install <version>' to get started.",
        ]
    return [
        "No active SDK configured for this workspace.",
        f"Already installed: {', '.join(cached)}",
        "Run 'tan sdk switch <version>' to select one.",
    ]


def format_readiness_block(header: str, report: dict[str, Any]) -> list[str]:
    lines = [
        header,
        f"  path    {report['sdkPath']}",
        f"  version {report['version'] or '(unknown)'}",
        f"  state   {report['state']}",
    ]
    if report["issues"]:
        lines.append("  issues:")
        lines.extend(f"    - {issue}" for issue in report["issues"])
    return lines


def format_release_table(releases: list[dict[str, Any]]) -> list[str]:
    """Tag, publish date, truncated notes, then the draft/prerelease flags.

    Flags come AFTER the notes (tan-cli #122): placed between the fixed-width
    date and the notes they would shift the notes column by their own width.
    """
    if not releases:
        return ["No SDK releases found."]
    lines = [f"Alp SDK releases ({len(releases)})"]
    for release in releases:
        date = release["publishedAt"][:10]
        summary = release["releaseNotesSummary"]
        # Char-aware truncation to 60, no ellipsis -- Python slicing is by
        # character, matching the Rust's `chars().take(60)` rather than bytes.
        notes = f"   {summary[:60]}" if summary else ""
        flags = {
            (True, True): " [draft, prerelease]",
            (True, False): " [draft]",
            (False, True): " [prerelease]",
            (False, False): "",
        }[(bool(release["draft"]), bool(release["prerelease"]))]
        lines.append(f"  {release['tag']:<12} {date}{notes}{flags}")
    return lines


# ── sdk list: the one network path, opt-in and timed out ─────────────────────


def _proxy_configured() -> bool:
    """Whether a proxy steers an `https://` request -- the one bit the transport
    error string cannot carry. An unreachable or refused proxy fails as a plain
    connect error that never contains the word "proxy", and it is the single
    most common misconfiguration behind a corporate network."""
    return any(
        os.environ.get(name)
        for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy")
    )


def describe_network_error(error: str) -> str:
    """Append the likely environmental cause to a raw transport error.

    A middlebox-signed certificate and an unreachable proxy both read to a user
    as "the network is down", so they go hunting in the wrong place. Ordering is
    the whole design and matches `tan_core::sdk::describe_network_error`:
    named-proxy first (a proxy failure that also mentions TLS is still a proxy
    problem), then certificate/TLS even when a proxy IS set (the middlebox is
    reachable; it is its CA that is untrusted, and the proxy vars are the wrong
    knob there), and only then a bare connect failure -- and only when there is
    a proxy to blame.
    """
    lower = error.lower()
    if "proxy" in lower:
        hint = _PROXY_HINT
    elif "certificate" in lower or "tls" in lower:
        hint = _TLS_HINT
    elif _proxy_configured() and "connect" in lower:
        hint = _PROXY_HINT
    else:
        return error
    return f"{error} {hint}"


def _str_field(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    return value if isinstance(value, str) else ""


def _first_paragraph(body: str) -> str:
    trimmed = body.strip()
    if not trimmed:
        return ""
    head, separator, _ = trimmed.partition("\n\n")
    return head.strip() if separator else trimmed


def parse_remote_sdk_releases(payload: Any) -> list[dict[str, Any]] | None:
    """Map the GitHub Releases payload onto the wire shape, or `None` when the
    response is not an array at all.

    Entries without a string `tag_name` are dropped (there is nothing to name
    them). `draft`/`prerelease` default to `False` when absent or non-boolean:
    absent means "not flagged", never a reason to drop the release (tan-cli
    #122).
    """
    if not isinstance(payload, list):
        return None
    releases: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("tag_name"), str):
            continue
        body = _str_field(item, "body")
        releases.append(
            {
                "tag": _str_field(item, "tag_name"),
                "publishedAt": _str_field(item, "published_at"),
                "tarballUrl": _str_field(item, "tarball_url"),
                "releaseNotesSummary": _first_paragraph(body),
                "releaseNotes": body.strip(),
                "draft": item.get("draft") is True,
                "prerelease": item.get("prerelease") is True,
            }
        )
    return releases


def _fetch_releases(url: str = GITHUB_RELEASES_URL) -> tuple[list[dict[str, Any]], str | None]:
    """GET the releases API and parse it. Returns `(releases, error_message)`;
    exactly one side is meaningful.

    Every failure -- DNS, TLS, a proxy, a 404, a truncated body, HTML where JSON
    was promised -- comes back as a MESSAGE, so the caller always has an
    envelope to emit. A bare `except Exception` is the point: this is the one
    path in the file that touches a network stack, and an unanticipated
    transport error escaping here is the port's recurring bug class (a traceback
    on stderr, nothing on stdout, and an extension that renders nothing).
    """
    request = urllib.request.Request(  # noqa: S310 -- constant https:// endpoint
        url,
        headers={
            "User-Agent": "tan-cli/0",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 -- as above
            request, timeout=NETWORK_TIMEOUT_SECONDS
        ) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except Exception as err:  # noqa: BLE001 -- see the docstring
        detail = str(err) or type(err).__name__
        return [], describe_network_error(detail)

    try:
        payload = json.loads(raw)
    except ValueError as err:
        return [], f"Alp SDK: could not parse the GitHub Releases response: {err}"

    releases = parse_remote_sdk_releases(payload)
    if releases is None:
        return [], "Alp SDK: unexpected response shape from GitHub Releases API."
    return releases, None


# ── envelope plumbing ───────────────────────────────────────────────────────


def _list_data(releases: list[dict[str, Any]]) -> dict[str, Any]:
    """`sdk list`'s payload -- and the payload the unknown-subcommand FAILURE
    carries too, matching the Rust, which builds that refusal out of `ListData`.
    `data.releases` is the key the extension reads with `?? []`."""
    return {"subcommand": "list", "releases": releases}


def _emit(
    *,
    json_mode: bool,
    data: dict[str, Any],
    issues: list[Issue],
    exit_code: ExitCode,
    text_lines: list[str],
    sdk: SdkInfo | None = None,
) -> None:
    """The single exit point: one envelope on stdout in JSON mode, human lines on
    stderr otherwise, then `typer.Exit`.

    `project` is always null (`sdk.rs`'s `null_project`) -- `sdk` verbs manage a
    checkout, not a board. Text goes to stderr in BOTH modes so stdout can never
    carry a stray byte beside the envelope; the conformance harness asserts
    stderr is empty under `--format json`, which is why nothing is written there
    in that mode at all.
    """
    if json_mode:
        emit(
            Envelope(
                "sdk",
                Project(root=None, board_yaml=None),
                data,
                issues,
                exit_code,
                sdk=sdk,
            )
        )
    else:
        stream = typer.get_text_stream("stderr")
        for line in text_lines:
            stream.write(f"{line}\n")
    raise typer.Exit(int(exit_code))


def _fail(
    *,
    json_mode: bool,
    data: dict[str, Any],
    code: str,
    message: str,
    text_lines: list[str],
    exit_code: ExitCode = ExitCode.RUNTIME_FAILURE,
) -> None:
    """A refusal: the given payload plus exactly one `sdk.<code>` error issue.
    `exit_code` reaches the envelope as well as the process, so `ok` and
    `exitCode` can never disagree."""
    _emit(
        json_mode=json_mode,
        data=data,
        issues=[Issue(f"sdk.{code}", "error", message)],
        exit_code=exit_code,
        text_lines=text_lines,
    )


# ── subcommands ─────────────────────────────────────────────────────────────


def _run_current(*, json_mode: bool, sdk_root: str | None, workspace_root: Path) -> None:
    """`tan sdk current` -- the active SDK, its readiness, and the winning tier.

    Always exit 0. "Nothing is configured" is a successful answer to the
    question asked, and the `sdk-current-no-sdk` golden pins it.
    """
    active = resolve_sdk_tiered(sdk_root, workspace_root)
    readiness = check_sdk_readiness(active.path) if active.path is not None else None
    text = (
        format_readiness_block("Active SDK", readiness)
        if readiness is not None
        else no_active_sdk_text(cached_sdk_versions())
    )
    _emit(
        json_mode=json_mode,
        data={
            "subcommand": "current",
            "sdkPath": active.path,
            "readiness": readiness,
            "sourceTier": active.tier,
        },
        issues=[],
        exit_code=ExitCode.SUCCESS,
        text_lines=text,
        # The envelope's optional `sdk` key mirrors the Rust's
        # `sdk_report::record` at this exact call site: `run_current` is the one
        # place whose resolution IS "the active SDK". Recorded even for an
        # invalid `--sdk-root`, matching the `if let Some(root)` there -- and
        # absent entirely (never `null`) when nothing resolved.
        sdk=SdkInfo(active.path, active.tier) if active.path is not None else None,
    )


def _run_list(*, json_mode: bool, online: bool) -> None:
    """`tan sdk list` -- the published alp-sdk releases.

    `--online` is required. The Rust reaches the network unconditionally here;
    this port gates it because a command that silently opens a socket cannot be
    driven from a hermetic test, an air-gapped host, or a fixture. The refusal
    is a normal coded envelope, so a consumer sees a reason rather than a hang.
    """
    if not online:
        _fail(
            json_mode=json_mode,
            data=_list_data([]),
            code="network-required",
            message=(
                "`sdk list` queries the GitHub releases API. Re-run with "
                "`--online` to allow the network request."
            ),
            text_lines=[
                "sdk list: this command needs network access.",
                "Re-run as `tan sdk list --online`.",
            ],
        )
        return

    releases, error = _fetch_releases()
    if error is not None:
        _fail(
            json_mode=json_mode,
            data=_list_data([]),
            code="fetch-failed",
            message=error,
            text_lines=["sdk list: failed to fetch releases.", error],
        )
        return

    _emit(
        json_mode=json_mode,
        data=_list_data(releases),
        issues=[],
        exit_code=ExitCode.SUCCESS,
        text_lines=format_release_table(releases),
    )


def _run_not_ported(*, json_mode: bool, subcommand: str, data: dict[str, Any]) -> None:
    """`install` / `switch` -- refused outright rather than half-implemented.

    Exit 5 (`InternalFailure`), following `validate_cmd`'s precedent for its own
    unported spawn path: this is a gap in tan, not a mistake the user made, and
    2 would be rendered as a warning by a consumer that should be seeing a hard
    stop. The payload keeps each verb's real key set so a consumer reading
    `data.sdkPath`/`data.version` still finds them.
    """
    _fail(
        json_mode=json_mode,
        data=data,
        code="not-ported",
        message=(
            f"`sdk {subcommand}` is not available in this build of tan. It writes "
            "the active-SDK pointer and reconciles the west workspace manifest, "
            "and a partial implementation would report success while `west` kept "
            "resolving the old SDK."
        ),
        text_lines=[
            f"sdk {subcommand}: not available in this build of tan.",
            "Use `--sdk-root <path>` to point a command at a checkout directly.",
        ],
        exit_code=ExitCode.INTERNAL_FAILURE,
    )


def _run_unknown(*, json_mode: bool, subcommand: str | None) -> None:
    """Anything else. Exit 1 with the `list`-shaped payload -- both pinned by
    the `sdk-unknown-subcommand` golden. `(none)` stands in for a bare `tan
    sdk`, matching the Rust's `other.unwrap_or("(none)")`; an explicitly EMPTY
    argument is reported as the empty string it was, same as there.
    """
    sub = "(none)" if subcommand is None else subcommand
    _fail(
        json_mode=json_mode,
        data=_list_data([]),
        code="unknown-subcommand",
        message=f"Unknown sdk subcommand: {sub}",
        text_lines=[f"sdk: unknown subcommand '{sub}'.", AVAILABLE_SUBCOMMANDS],
    )


# ── the command ─────────────────────────────────────────────────────────────


def sdk(
    subcommand: str = typer.Argument(
        None, metavar="SUBCOMMAND", help="list, install, current, or switch."
    ),
    arg: str = typer.Argument(
        None, metavar="ARG", help="Version for install, version|path for switch."
    ),
    # Accepted and unused: `--destination` only steers `install`/`switch` cache-root
    # resolution, neither of which is ported. Kept in the surface because dropping
    # it would turn an argv the shipped Rust binary accepts into a usage error.
    destination: str = typer.Option(
        None, "--destination", metavar="PATH", help="SDK cache root (default: ~/.alp/sdk-cache)."
    ),
    global_: bool = typer.Option(
        False, "--global", help="With switch: pin the machine-global default."
    ),
    online: bool = typer.Option(
        False, "--online", help="Allow `list` to query the GitHub releases API."
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="Explicit alp-sdk checkout to report."
    ),
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """Manage local Alp SDK installs."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    try:
        # `cwd.join(project)` unnormalised, matching `util.rs`'s
        # `cli_workspace_root`: every other reader of `.alp/sdk-path` resolves it
        # under this same root, so `tan --project ./firmware sdk current` must
        # report the pointer build/flash consult for THAT project, not the
        # repo-root one.
        workspace_root = Path.cwd() / project if project else Path.cwd()

        if subcommand == "current":
            _run_current(
                json_mode=json_mode, sdk_root=sdk_root, workspace_root=workspace_root
            )
        elif subcommand == "list":
            _run_list(json_mode=json_mode, online=online)
        elif subcommand == "install":
            _run_not_ported(
                json_mode=json_mode,
                subcommand="install",
                data={
                    "subcommand": "install",
                    "version": arg or "",
                    "sdkPath": "",
                    "readiness": empty_readiness(""),
                    "selected": False,
                },
            )
        elif subcommand == "switch":
            _run_not_ported(
                json_mode=json_mode,
                subcommand="switch",
                data={
                    "subcommand": "switch",
                    "sdkPath": "",
                    "version": None,
                    "scope": "global" if global_ else "project",
                },
            )
        else:
            _run_unknown(json_mode=json_mode, subcommand=subcommand)
    except typer.Exit:
        # `_emit`'s own signal. `click.exceptions.Exit` subclasses RuntimeError,
        # so the catch-all below would otherwise swallow every successful run and
        # re-report it as an internal failure.
        raise
    except Exception as err:  # noqa: BLE001 -- the envelope IS the error contract
        # The backstop for the failure nobody enumerated. Without it an unhandled
        # exception replaces the envelope with a traceback on stderr while stdout
        # stays empty, and the extension renders nothing at all -- no error, no
        # log, no warning. Five Criticals in this port were exactly that.
        _fail(
            json_mode=json_mode,
            data={"subcommand": subcommand or "(none)", "releases": []},
            code="internal-failure",
            message=f"sdk failed unexpectedly: {err}",
            text_lines=[f"sdk: unexpected failure: {err}"],
            exit_code=ExitCode.INTERNAL_FAILURE,
        )
