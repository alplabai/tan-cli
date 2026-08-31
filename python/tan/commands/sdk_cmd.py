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
only does so behind an explicit `--online`. tan-cli#351: without the flag it
answers OFFLINE, at exit 0 -- not a refusal. The oracle has no `--online` flag
at all and reaches the network unconditionally on every `sdk list` call
(measured: `--help` lists no such option; a live run with network reachable
succeeds at exit 0 with no flag given), so gating the call is this port's OWN
addition, for hermeticity (I-23) -- a command that silently opens a socket
cannot be driven from a hermetic test, an air-gapped host, or a fixture. That
gate is not a verdict on anything the caller did wrong, so it must not exit
non-zero: the bare answer says plainly that the releases it reports are
UPSTREAM and that `--online` is the switch that fetches them, the same way
`sdk current` answers "nothing configured" at exit 0 instead of failing (see
`_run_list`'s own docstring for the full reasoning). The fetch itself carries
an explicit timeout, because a `urlopen` with no timeout inherits the socket
default of "forever" and a CI job driving `--format json` would hang until the
runner killed it (I-23's failure mode, arrived at through a socket instead of
a prompt).

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
`sdk.not-ported` (exit 1, see `_run_not_ported`'s own docstring for why not 5)
rather than half-working. `switch` in particular must not land partially: the
Rust writes the active-SDK pointer AND reconciles `<topdir>/.west/config`'s own
`manifest.path` (tan-cli #62/#31), and a version that writes the pointer while
silently skipping the reconciliation reports success while `west` keeps
resolving the manifest from the stale pointer -- which is the bug #62
reported, re-introduced invisibly. A loud refusal is strictly safer than that.

**tan-cli#305: that refusal must not be a dead end.** `doctor_cmd`,
`bootstrap_cmd`, and this module's own `sdk current` used to independently
recommend `sdk install`/`sdk switch` as the fix for "no SDK resolved" -- three
copies of advice naming a subcommand this build refuses, which on a clean
host (no checkout, no cache, no pointer anywhere) left `tan bootstrap`
unreachable by any documented path. `NO_SDK_NEXT_STEPS` below is the one
surviving mechanism (`--sdk-root`, plus how to get a checkout at all) all
three now read instead.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    # Type-checker only. `urllib.request` is deferred into the two functions
    # that use it (tan-cli#810), so this name exists for annotations and
    # costs nothing at runtime -- `TYPE_CHECKING` is False in a real run.
    from urllib.request import OpenerDirector

import typer

from tan.core.atomic_write import atomic_write_text
from tan.core.global_flags import accept_global_flags
from tan.core.proxy import (
    HTTPS_PROXY_ENV_VARS,
    host_of,
    select_https_proxy,
    unsupported_proxy_scheme,
)
# `_has_loader_script`/`_home_alp_dir`/`_read_file` moved to
# `tan.core.sdk_discovery` alongside `resolve_sdk_tiered` (tan-cli#408 review
# follow-up) -- `check_sdk_readiness`/`cached_sdk_versions`/
# `_default_cache_root` below still need them for readiness reporting, which
# stayed here because it is not part of the discovery/resolution cluster.
# `_abs_posix`/`_pointer_target` are the same kind of shared filesystem
# primitive, pulled in for `sdk remove` (tan-cli#790): `_abs_posix` is the
# cwd-anchored/lexical path form every removal-target comparison below uses,
# `_pointer_target` is the direct (not `resolve_sdk_tiered`-mediated) read of
# `~/.alp/sdk-default`'s own `sdkPath`, needed because that pointer can name
# a checkout THIS workspace does not resolve through at all while still being
# exactly what removing it would orphan for some OTHER project on the host.
from tan.core.sdk_discovery import (
    ActiveSdk,
    _abs_posix,
    _has_loader_script,
    _home_alp_dir,
    _pointer_target,
    _read_file,
    global_default_foreign_project_issue,
    project_pin_issue,
    resolve_sdk_root_ladder,
    resolve_sdk_tiered,
    sdk_ladder_divergence_issue,
)
from tan.core.sdk_default_registry import (
    load_raw,
    normalized_sdk_path,
    parse_registry,
    prune_entries_by_sdk_path,
    registry_path,
    registry_text,
)
from tan.core.sdk_removal import (
    RemovalOutcome,
    is_cache_root_itself,
    is_outside_cache_root,
    remove_sdk_tree,
    resolve_removal_target,
)
from tan.core.text_layout import wrap_lines
from tan.env import wrap_width
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.net import default_ssl_context
from tan.output_format import FORMAT_HELP, OutputFormat

#: GitHub Releases API endpoint for `alplabai/alp-sdk`, from
#: `tan_core::sdk::GITHUB_RELEASES_URL`.
GITHUB_RELEASES_URL = "https://api.github.com/repos/alplabai/alp-sdk/releases"


#: Wall-clock ceiling on the ONE network call in this file. Every subprocess and
#: socket probe in the port carries a timeout; `urlopen` without one blocks on
#: the socket default (no timeout at all), which turns an unreachable proxy into
#: a hung `--format json` consumer rather than an error it can render.
NETWORK_TIMEOUT_SECONDS = 20.0

#: The verbs `run` dispatches, for the unknown-subcommand text line. Was
#: verbatim from `sdk.rs`'s failure text until tan-cli#305: the oracle's
#: `install`/`switch` really work, so its wording is honest there; this port's
#: do not (`_run_not_ported` below), so repeating it unqualified here was one
#: more site recommending a subcommand this build refuses.
AVAILABLE_SUBCOMMANDS = (
    "Available subcommands: list, current, install <version> (refuses -- not "
    "yet ported), switch <version> (refuses -- not yet ported), "
    "remove <version|path>"
)

#: `install`/`switch` refuse outright in this build (`_run_not_ported` below)
#: -- the ONE fact `doctor_cmd.sdk_check` and `bootstrap_cmd`'s SDK-unresolved
#: refusal must read instead of each independently hardcoding "run `tan sdk
#: install <ver>`" (tan-cli#305). Three copies of that recommendation, none
#: checking whether the build it shipped in actually has the subcommand, is
#: how a clean host was left with no way to reach `tan bootstrap` at all.
NOT_PORTED_SDK_SUBCOMMANDS = frozenset({"install", "switch"})

#: The one accurate way to point tan at an SDK checkout in THIS build. Neither
#: `sdk install` nor `sdk switch` works (tan-cli#305), so `--sdk-root` on the
#: invocation itself is the only surviving mechanism, and a host with no
#: checkout anywhere needs telling how to get one at all -- an accurate manual
#: instruction beats naming a subcommand that exits 1. Shared by
#: `doctor_cmd.sdk_check`, `bootstrap_cmd`'s SDK-unresolved refusal, and
#: `_run_current` below so the three cannot drift into three different,
#: independently-worded dead ends again.
NO_SDK_NEXT_STEPS = (
    "get an alp-sdk checkout (`git clone https://github.com/alplabai/alp-sdk`), "
    "then point tan at it with `--sdk-root <path>`"
)

#: `sdk list`'s proxy/CA hint, verbatim from `tan_core::sdk::describe_network_error`.
#: Names only the variables that actually steer an `https://` request -- neither
#: tan nor git applies `HTTP_PROXY` to one, so naming it sent users to edit a
#: variable that changes nothing. That claim was FALSE for `ALL_PROXY` until
#: tan-cli#497: `urllib` mapped it to a key its `https` dispatch never consulted,
#: so this hint named a variable that really did change nothing. `tan/core/proxy.py`
#: is what makes all three names accurate.
_PROXY_HINT = (
    "Check ALL_PROXY/HTTPS_PROXY/NO_PROXY — the configured proxy refused or "
    "could not complete the connection."
)

#: #304: this used to open "This is USUALLY a TLS-intercepting proxy or a
#: corporate CA that this machine does not trust" -- a confident diagnosis with
#: no evidence behind it. The v0.5.0-rc2 asset hit this exact error with no
#: proxy and no corporate CA anywhere in sight; the real cause was the frozen
#: binary shipping with no CA bundle at all (fixed in `tan/net.py`, but a
#: future build that regresses that fix must not be misdiagnosed the same way
#: again). A message naming a probable cause needs evidence for that cause or
#: has to name the alternative -- this names both and gives the one comparison
#: (`curl`/a browser against the same host) that tells them apart, mirroring
#: how #304 itself was actually diagnosed.
_TLS_HINT = (
    "Not necessarily a broken connection -- this can mean a TLS-intercepting "
    "proxy or a corporate CA this machine does not trust, or that this tan "
    "build's own CA trust failed to load. If curl or a browser reach this host "
    "fine from here, the trust store is the likelier cause; if they fail too, "
    "the proxy/CA is."
)


def global_default_pointer_fix_hint(native_path: str, native_registry_path: str) -> str:
    """How to fix -- or safely clear -- an already-written `~/.alp/sdk-
    default` pointer (`native_path`) AND its origin-keyed sibling
    `~/.alp/sdk-defaults.json` (`native_registry_path`, tan-cli#466) by hand.
    Both are the caller's to compute -- `_home_alp_dir() / "sdk-default"` and
    `sdk_default_registry.registry_path(_home_alp_dir())`, each rendered
    through whatever this-platform-separator helper it already has
    (`bootstrap_cmd._native` for its callers).

    Names BOTH files, unconditionally, because tan-cli#466's registry is
    consulted FIRST and a caller cannot tell from the fix-hint's call site
    alone whether the answer it just got came from the registry or fell
    through to the legacy pointer -- so a hint naming only one of them could
    send a reader to edit the file that was not actually the one that
    answered.

    `_pointer_target` above degrades every read failure on the legacy file
    (missing, invalid JSON, list-shaped, no `sdkPath`) to `None`, and
    `sdk_default_registry.parse_registry` degrades every read/parse failure
    on the registry the identical way, to `{}` -- both resolvers
    (`resolve_sdk_tiered`) then fall through to the next tier on that, so
    DELETING either or both files is always a safe recovery, never a step
    backwards; hand-editing a `"sdkPath"` field is the targeted fix when the
    caller knows what it should say instead.

    Shared so a caller describing this by hand cannot drift from what
    `_pointer_target`/`parse_registry` actually read. `bootstrap_cmd`'s
    workspace-relocation and rollback-failure messages are why this exists
    (tan-cli#305 follow-up): they used to send a user -- sometimes one
    already in a broken, checkout-moved-but-rollback-incomplete state -- to
    `tan sdk switch --global`, which refuses outright in this build. Naming
    the pointer files directly, not the command, is also honest about the
    mechanism `switch` itself would use once ported, so this will not go
    stale the moment that disposition changes.
    """
    return (
        f"delete {native_path} and/or {native_registry_path} (tan falls "
        f'through to the next SDK it can resolve), or edit the "sdkPath" '
        f"field(s) by hand"
    )


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


# ── the install cache ────────────────────────────────────────────────────────
# `ActiveSdk`/`resolve_sdk_tiered`/the project-pin and global-default `Issue`
# builders moved to `tan.core.sdk_discovery` (tan-cli#408 review follow-up,
# see the module docstring and the top-of-file import) -- `_run_current`
# below imports them back from there.


def _default_cache_root() -> Path:
    """`~/.alp/sdk-cache` -- where `tan sdk install` clones to."""
    return _home_alp_dir() / "sdk-cache"


def cached_sdk_versions(cache_root: Path | None = None) -> list[str]:
    """Directory names directly under the cache root that hold a real checkout,
    sorted. An unreadable or absent cache root is an empty list, never an error.

    This is what lets `current`'s "nothing selected" message name the
    checkouts already sitting in the cache instead of claiming none exist --
    tan-cli #162, a loop with no exit. Deliberately does not guess WHICH
    version to point at; it lists what exists and lets the user choose (via
    `--sdk-root`, since `sdk switch` itself refuses -- tan-cli#305).
    """
    root = _default_cache_root() if cache_root is None else cache_root
    try:
        return sorted(entry.name for entry in root.iterdir() if _has_loader_script(entry))
    except OSError:
        return []


# ── human text (stderr only; stdout is the envelope channel) ─────────────────


def no_active_sdk_text(cached: list[str]) -> list[str]:
    """`sdk current`'s "nothing is configured" guidance. Used to send an empty
    cache to `sdk install` and a populated one to `sdk switch` -- neither
    works in this build (tan-cli#305), so both branches now point at
    `NO_SDK_NEXT_STEPS`, the one mechanism that does."""
    if not cached:
        return [
            "No active SDK configured for this workspace.",
            f"To get started, {NO_SDK_NEXT_STEPS}.",
        ]
    return [
        "No active SDK configured for this workspace.",
        f"Already installed under {_default_cache_root()}: {', '.join(cached)}",
        "Point tan at one with `--sdk-root <path>`.",
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
        # Collapse ALL whitespace before truncating. `releaseNotesSummary` is
        # the release body verbatim, and a body whose first line is short
        # (`## Highlights`) used to put a literal newline INSIDE a table row:
        # the row spilled onto a second, unindented line and the `{flags}`
        # suffix landed after the spilled text rather than on the row it
        # describes (#316). Collapsing also spends the 60-char budget on
        # visible text rather than on layout this table then discards.
        summary = " ".join((release["releaseNotesSummary"] or "").split())
        # Char-aware truncation to 60, no ellipsis -- Python slicing is by
        # character, matching the Rust's `chars().take(60)` rather than bytes.
        # The Rust does NOT collapse first, so this is a deliberate divergence
        # on the text surface: measured, no committed fixture and no parity
        # case covers this table, and the oracle renders the same broken row.
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


def _proxy_variable_name() -> str:
    """The env var [`select_https_proxy`] actually read the winning proxy from,
    for a message that sends the reader to the right line of their shell
    profile. `ALL_PROXY` by default -- the head of the precedence list -- when
    none is set, which only happens if a caller asks outside a proxied run."""
    for name in HTTPS_PROXY_ENV_VARS:
        value = os.environ.get(name)
        if value and value.strip():
            return name
    return HTTPS_PROXY_ENV_VARS[0]


def describe_network_error(error: str, *, proxy_used: bool) -> str:
    """Append the likely environmental cause to a raw transport error.

    A middlebox-signed certificate and an unreachable proxy both read to a user
    as "the network is down", so they go hunting in the wrong place. Ordering is
    the whole design and matches `tan_core::sdk::describe_network_error`:
    named-proxy first (a proxy failure that also mentions TLS is still a proxy
    problem), then certificate/TLS even when a proxy IS set (the middlebox is
    reachable; it is its CA that is untrusted, and the proxy vars are the wrong
    knob there), and only then a bare connect failure -- and only when there is
    a proxy to blame.

    `proxy_used` is the caller's own POST-`NO_PROXY` answer -- whether the
    request that just failed actually went through a proxy -- not a bare env
    scan. The bare scan was a second wrong-blame case (tan-cli#497 defect 8):
    `NO_PROXY=api.github.com` beside a `HTTPS_PROXY` correctly sends this
    request DIRECT (measured: rc 0, 13 releases), yet a failure on that direct
    connection was still hinted at the proxy, which had no part in it. The Rust
    this ports (`http.rs:115`, `proxy_configured() -> env_proxy().is_some()`)
    is post-bypass too.
    """
    lower = error.lower()
    if "proxy" in lower:
        hint = _PROXY_HINT
    elif "certificate" in lower or "tls" in lower:
        hint = _TLS_HINT
    elif proxy_used and "connect" in lower:
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


def _unroutable_proxy_refusal(proxy: str | None, url: str) -> str | None:
    """Why this request must not be attempted at all, or `None`.

    REFUSES rather than falling back to a direct connection when the selected
    proxy uses a scheme urllib cannot carry (`unsupported_proxy_scheme`). A host
    that exports a SOCKS proxy is telling tan it has no other egress, or that
    direct egress is not permitted; quietly ignoring it is exactly the
    silent-bypass defect this path was fixed for (tan-cli#497 defect 8). The
    oracle CAN dial SOCKS -- `crates/tan-cli/src/http.rs` compiles ureq's
    `socks-proxy` feature, measured -- so this names the limitation instead of
    hiding it.

    **The remediation names the variable that actually WON, and offers unsetting
    it first.** The first cut said "Set `HTTPS_PROXY` to an http:// or https://
    proxy", which cannot take effect: `HTTPS_PROXY_ENV_VARS` puts `ALL_PROXY`
    ahead of `HTTPS_PROXY`, so the selection never reaches the variable the
    reader was told to set. Measured --
    `ALL_PROXY=socks5://127.0.0.1:45997 HTTPS_PROXY=http://127.0.0.1:45996` gave
    the identical refusal at rc 1 with NEITHER socket touched. That is the same
    self-defeating-remediation class as tan-cli#497 defect 7, which this branch
    fixed two commands away: an instruction the user can follow exactly and be
    no better off.
    """
    if proxy is None:
        return None
    scheme = unsupported_proxy_scheme(proxy)
    if scheme is None:
        return None
    variable = _proxy_variable_name()
    return (
        f"Alp SDK: {variable} names a {scheme}:// proxy, which this "
        f"build of tan cannot route through (its HTTP client has no {scheme} "
        f"transport). Unset {variable}, or point it at an http:// or https:// "
        f"proxy, or add {host_of(url)} to NO_PROXY to allow a direct connection."
    )


def _releases_opener(proxy: str | None) -> OpenerDirector:
    """An opener carrying OUR proxy decision, never urllib's own.

    `urlopen` derives its handler from `getproxies_environment()`, which maps
    `ALL_PROXY` to the key `all` -- and `OpenerDirector._open` dispatches on
    `req.type` (`"https"`), so the `all_open` method that installs is never
    called and the proxy is silently bypassed (tan-cli#497 defect 8). Both keys
    carry the same value because a proxy chosen for an `https://` request is
    what `ALL_PROXY` means, and an EMPTY mapping means DIRECT -- not "fall back
    to the environment", which would re-open the `NO_PROXY` half of the same
    defect.

    tan-cli#810: `urllib.request` is imported HERE rather than at module scope.
    `cli.py` static-imports every command module, so this file's module-scope
    `import urllib.request` -- which drags `email`, `http` and `ssl` behind it
    -- was paid by `tan --version`, for a stack only the two network functions
    in this file ever touch.

    The return annotation is spelled `OpenerDirector`, bound by the
    `TYPE_CHECKING` block at the top rather than by a runtime import, and that
    is deliberate: an annotation binds in the MODULE namespace, not the
    function's, so `-> urllib.request.OpenerDirector` would have named
    something no longer there. `from __future__ import annotations` keeps it a
    string, so nothing breaks at def or call time -- but
    `typing.get_type_hints()` on this function would raise `NameError`, and
    that API is live in this very file (`accept_global_flags` calls it,
    `core/global_flags.py`). It is aimed at the `sdk` COMMAND callback, never
    at this private helper, so the two do not meet; do not add a wrapper that
    resolves hints here without re-reading this paragraph.
    """
    import urllib.request  # noqa: PLC0415

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({} if proxy is None else {"http": proxy, "https": proxy}),
        urllib.request.HTTPSHandler(context=default_ssl_context()),
    )


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
    import urllib.request  # noqa: PLC0415 -- deferred, see `_releases_opener`

    request = urllib.request.Request(  # noqa: S310 -- constant https:// endpoint
        url,
        headers={
            "User-Agent": "tan-cli/0",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    proxy = select_https_proxy(url)
    refusal = _unroutable_proxy_refusal(proxy, url)
    if refusal is not None:
        return [], refusal
    opener = _releases_opener(proxy)
    try:
        with opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except Exception as err:  # noqa: BLE001 -- see the docstring
        detail = str(err) or type(err).__name__
        return [], describe_network_error(detail, proxy_used=proxy is not None)

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
    if active.path is None:
        # tan-cli#497 defect 1. `resolve_sdk_tiered` alone has NO candidate for
        # a CHILD `<ws>/alp-sdk` -- the README Quickstart cwd, straight after
        # `git clone https://github.com/alplabai/alp-sdk`. Measured there: this
        # command answered `sdkPath: null`, `sourceTier: "none"`, `issues: []`
        # and printed "get an alp-sdk checkout (`git clone ...`)", while
        # `doctor`, `build`, `validate`, `inspect` and `trace` in that SAME cwd
        # all reported `<ws>/alp-sdk` at `sourceTier: "discovery"` through
        # `tan.core.sdk_discovery.resolve_sdk_root_ladder` -- whose own docstring names `sdk
        # current` as one of its thirteen callers, and whose wide-walk TAIL is
        # the tier missing here. `discover_workspace_sdk`'s docstring states the
        # invariant this broke: "the tier it reports has to be what
        # build/validate/doctor would actually resolve here."
        #
        # An intentional divergence from the oracle, which has the identical
        # split (`sdk.rs::run_current` also calls the narrow `resolve_sdk_tiered`
        # while `util::resolve_sdk_root` falls through to `discover_sdk_root`).
        # `sdk current` is the one command whose entire job is answering "which
        # SDK am I on?", so answering it differently from every command that
        # ACTS on that SDK is the defect, not the parity.
        #
        # Consulted ONLY when the narrow ladder resolved nothing, and the ladder
        # itself is reused rather than its tail re-implemented: this can add an
        # answer where there was none, never change one that already existed
        # (which would move the SDK root under a live workspace), and there is
        # no third copy of the tier rule to keep true. `resolve_sdk_root_ladder`
        # is imported at module level, from `tan.core.sdk_discovery`, alongside
        # `resolve_sdk_tiered` and `ActiveSdk` (tan-cli#408 review follow-up) --
        # no cycle between this module and that one exists any more, in either
        # direction.
        ladder = resolve_sdk_root_ladder(sdk_root, workspace_root)
        if ladder.path is not None:
            active = ActiveSdk(
                str(ladder.path),
                ladder.tier,
                ladder.broken_project_pin,
                ladder.foreign_global_default_for,
            )
    readiness = check_sdk_readiness(active.path) if active.path is not None else None
    text = (
        format_readiness_block("Active SDK", readiness)
        if readiness is not None
        else no_active_sdk_text(cached_sdk_versions())
    )
    issues: list[Issue] = []
    pin_issue = project_pin_issue(active.broken_project_pin, active.tier)
    if pin_issue is not None:
        # tan-cli#263: without this, a workspace whose `.alp/sdk-path` names an
        # unreachable checkout reports `ok: true`, `issues: []`, `sourceTier`
        # silently one tier lower -- identical to a workspace that was never
        # pinned at all. `warning`, not `error`: the command still answers the
        # question asked (SOME SDK, or none, is active); it is the SILENCE
        # about the rejected pin that was the defect, not the fallthrough
        # itself.
        text = [
            f'Project pin .alp/sdk-path names "{active.broken_project_pin}", which '
            f"does not resolve to an alp-sdk checkout from here.",
            *text,
        ]
        issues.append(pin_issue)
    foreign_issue = global_default_foreign_project_issue(active.foreign_global_default_for)
    if foreign_issue is not None:
        # tan-cli#464: the shared, last-writer-wins `~/.alp/sdk-default` used
        # to answer here with no signal that a DIFFERENT project's bootstrap
        # relocation was what actually decided it -- `ok: true`, `issues: []`,
        # identical to the correct case. `warning`, matching `pin_issue`
        # above: the tier still answers the question asked; it is the silence
        # about WHOSE answer this is that was the defect.
        text = [
            f'The global default SDK was last set for '
            f'"{active.foreign_global_default_for}", not this workspace.',
            *text,
        ]
        issues.append(foreign_issue)
    # tan-cli#407 names THIS command in particular: `sdk current` is what a
    # user runs to ask "which SDK am I on?", and in a workspace holding both a
    # child `<ws>/alp-sdk` and a lateral `../alp-sdk` it answers with the
    # narrow one only -- reporting the readiness and VERSION of the checkout
    # `tan generate` did not use. `sdk_ladder_divergence_issue` is imported at
    # module level, from `tan.core.sdk_discovery` (tan-cli#408).
    divergence = sdk_ladder_divergence_issue(sdk_root, workspace_root, wide=False)
    if divergence is not None:
        text = [*text, divergence.message]
        issues.append(divergence)
    if not json_mode:
        # Only in text mode: `wrap_width()` probes stderr, which is the
        # right thing to skip entirely for `--format json`, where `text` is
        # built but never printed (`_emit`'s json branch reads `data`/
        # `issues`, not `text_lines`).
        text = wrap_lines(text, wrap_width())
    _emit(
        json_mode=json_mode,
        data={
            "subcommand": "current",
            "sdkPath": active.path,
            "readiness": readiness,
            "sourceTier": active.tier,
        },
        issues=issues,
        exit_code=ExitCode.SUCCESS,
        text_lines=text,
        # The envelope's optional `sdk` key mirrors the Rust's
        # `sdk_report::record` at this exact call site: `run_current` is the one
        # place whose resolution IS "the active SDK". Recorded even for an
        # invalid `--sdk-root`, matching the `if let Some(root)` there -- and
        # absent entirely (never `null`) when nothing resolved.
        sdk=SdkInfo.from_resolution(active.path, active) if active.path is not None else None,
    )


def _run_list(*, json_mode: bool, online: bool) -> None:
    """`tan sdk list` -- the published alp-sdk releases.

    tan-cli#351: bare `sdk list` (no `--online`) answers OFFLINE, at exit 0.
    Measured first, since this file already carries the reasoning for why the
    port diverges from the oracle here: the oracle (`target/debug/tan.exe`,
    tan 0.4.1) has no `--online` flag at all -- `sdk list --help` lists none --
    and reaches the network unconditionally on every `sdk list` call (a live
    run against a reachable network succeeds at exit 0 with no flag given).
    Gating the fetch behind `--online` is this PORT's own addition, for
    hermeticity (I-23): a command that silently opens a socket cannot be
    driven from a hermetic test, an air-gapped host, or a fixture. That gate
    is not, itself, a verdict on anything the caller did wrong -- there is no
    "failure" here to report, only a question (`sdk list` answers what alp-sdk
    has published upstream) that needs an explicit flag to actually reach the
    network for. Exiting non-zero for it (as this used to) treated a normal,
    everyday invocation the same as a real error, which is exactly the
    asymmetry `sdk current` never had: "nothing configured" is exit 0 there,
    and "list needs `--online`" now is here too. The message says plainly
    what `sdk list` reports (UPSTREAM releases) and that `--online` is the
    switch that fetches them, rather than reporting the missing flag as a
    network requirement failure.
    """
    if not online:
        _emit(
            json_mode=json_mode,
            data=_list_data([]),
            issues=[
                Issue(
                    "sdk.network-required",
                    "warning",
                    "`sdk list` reports the Alp SDK releases published upstream "
                    "on GitHub -- there is no local/offline copy to answer from. "
                    "Add --online to fetch them.",
                )
            ],
            exit_code=ExitCode.SUCCESS,
            text_lines=[
                "sdk list: reports Alp SDK releases published upstream on GitHub.",
                "Add --online to fetch them: `tan sdk list --online`.",
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

    Exit 1 (`RuntimeFailure`) -- the same code every other refusal in this
    module already uses (`sdk list` without `--online`, a bare `tan sdk`) and
    the same one the deferred-verb stubs in `deferred_cmd` settled on.

    This was exit 5 (`InternalFailure`) until #262, on a docstring that
    justified it as "following `validate_cmd`'s precedent for its own unported
    spawn path". That citation was backwards: `validate_cmd` uses exit 1, and
    its module docstring says exit 5 would wrongly tell CI/the extension this
    is a tan crash. The comment argued for the opposite of what the code did.

    Measured, not inferred -- the oracle's own `sdk switch` failure is exit 1:
    `tan sdk switch 0.0.0-nonexistent --format json` -> rc=1,
    `sdk.path-not-found`. Nothing under `contract/` pins a 5 here.

    5 stays reserved for a genuine tan crash; 2 would be rendered as a warning
    by a consumer that should be seeing a hard stop. The payload keeps each
    verb's real key set so a consumer reading `data.sdkPath`/`data.version`
    still finds them.
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
            f"To get started, {NO_SDK_NEXT_STEPS}.",
            "`tan doctor` reports which checkout tan currently resolves.",
        ],
        exit_code=ExitCode.RUNTIME_FAILURE,
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


# ── sdk remove (tan-cli#790) ─────────────────────────────────────────────────
#
# The one destructive verb in this file, so it earns its own section rather
# than living beside `install`/`switch`'s refusal stubs. Designed for
# long-term customer experience the way the maintainer's own standing
# direction for this class of question asks, spelled out here once rather
# than re-derived at every call site below:
#
#   * an install that is currently load-bearing -- the ACTIVE resolution for
#     this workspace, the machine-global default, or a project's own
#     registered pin -- refuses to be removed without `--force`
#     ([`_load_bearing_reasons`]); silently orphaning any of the three is a
#     worse failure than a refusal that names exactly what would break;
#   * removal is IDEMPOTENT: a target that is already absent succeeds at
#     `data.removed: false`, so a rotation script never has to pre-check;
#   * a target outside the cache root refuses without `--force` too -- the
#     footgun guard `is_outside_cache_root` exists for -- but ONLY once it is
#     confirmed to exist, so an idempotent no-op never trips it;
#   * the cache ROOT ITSELF -- every install at once, not one of them --
#     refuses without `--force` too (`is_cache_root_itself`): the outside-root
#     guard above deliberately does NOT catch this (`target == destination` is
#     not "outside"), and no version is individually load-bearing for its own
#     root, so this was the one target `--force`-less `remove` could wipe the
#     whole cache with, found live during this review;
#   * every failure names WHAT blocked it (`sdk.remove-active`,
#     `sdk.remove-outside-root`, `sdk.remove-is-cache-root`,
#     `sdk.remove-in-use`, `sdk.remove-permission` -- flat, ONE dot, not the
#     nested `sdk.remove.active` shape the issue's own prose sketches:
#     `contract/issue-codes.json`'s `sdk.remove-missing-argument` entry
#     explains why nested is not available on this wire) and what to do
#     instead (`--force`, for the first three; close the holder, for the
#     fourth; fix the permissions/attributes by hand, for the fifth) -- the
#     issue's own bar: "the refusal messages must name what blocked it and
#     what to do instead".
#
# `sdk list`'s proposed `managed`/`active` columns (tan-cli#790's own "related
# gap" aside) are deliberately OUT of this change: `sdk list` today reports
# UPSTREAM GitHub releases, not local installs, so a per-release
# managed/active flag has no local install to describe until `sdk
# install`/`sdk switch` are themselves ported (tan-cli#305) -- tracked
# separately, not bundled into a single-verb change.


def _sdk_default_pointer_target() -> str | None:
    """`~/.alp/sdk-default`'s own `sdkPath`, read DIRECTLY rather than through
    `resolve_sdk_tiered`. That ladder only ever reports the ONE tier that
    wins for the CALLER's workspace, and the plain machine-global pointer can
    name a checkout this workspace's own project pin or registry entry
    outranks -- so it would never surface as `active.path` here -- while
    still being exactly what removing it would orphan for every OTHER,
    unregistered project on the host that falls through to it. `None` on any
    read/parse failure, the same degrade `_pointer_target` already applies to
    a missing or malformed pointer.
    """
    return _pointer_target(_home_alp_dir() / "sdk-default")


def _registered_origins_for(target_posix: str) -> list[str]:
    """Every `~/.alp/sdk-defaults.json` origin whose `sdkPath` names
    `target_posix` (posix-normalised, matching how the registry itself stores
    it -- `bootstrap_cmd._write_global_sdk_registry`'s own `_to_posix` write).
    Sorted for a deterministic message; `[]` on any read/parse failure,
    matching `parse_registry`'s own best-effort contract.
    """
    raw = _read_file(registry_path(_home_alp_dir()))
    registry = parse_registry(raw)
    return sorted(
        origin
        for origin, sdk_path in registry.items()
        # Separator-folded, not a raw `==`: a hand-edited registry on Windows
        # spells the same directory with backslashes, and missing the match
        # here means this removal does NOT refuse and silently orphans that
        # project -- see `normalized_sdk_path`'s own docstring.
        if normalized_sdk_path(sdk_path) == target_posix
    )


def _load_bearing_reasons(target_posix: str, active: ActiveSdk) -> list[str]:
    """Every reason removing `target_posix` right now would orphan something
    live -- the tan-cli#790 design bar itself: "silently orphaning either [the
    active install or a pinned one] is a worse failure than refusing". `[]`
    means safe to remove without `--force`. More than one reason can apply at
    once (the active install for THIS workspace can also be another
    project's registered default), and the caller reports all of them rather
    than only the first.
    """
    reasons: list[str] = []
    if active.path is not None and _abs_posix(active.path) == target_posix:
        reasons.append(f'the active alp-sdk for this workspace (sourceTier "{active.tier}")')
    default_target = _sdk_default_pointer_target()
    if default_target is not None and _abs_posix(default_target) == target_posix:
        reasons.append("the machine-global default SDK (~/.alp/sdk-default)")
    for origin in _registered_origins_for(target_posix):
        reasons.append(f'the registered global default for project "{origin}"')
    return reasons


def _prune_registry_entries_for(target_posix: str) -> None:
    """Best-effort: drop every `~/.alp/sdk-defaults.json` entry naming the
    just-removed `target_posix` -- keeps tan-cli#905's registry honest about
    what still resolves (`sdk_default_registry.prune_entries_by_sdk_path`),
    the same read-modify-write shape `bootstrap_cmd._write_global_sdk_registry`
    already uses for the ORIGIN-pruning half of the identical file. Silent on
    any failure, matching every other best-effort registry write in this
    codebase: the removal itself already succeeded by the time this runs, and
    a registry entry this call could not prune degrades no worse than it
    already would have before this function existed (`deepest_covering_entry`
    skips a covering entry whose `sdkPath` fails `_has_loader_script`, so a
    dead entry left behind answers nobody incorrectly -- it is merely not yet
    tidied).
    """
    try:
        path = registry_path(_home_alp_dir())
        raw = path.read_text(encoding="utf-8") if path.is_file() else None
        pruned = prune_entries_by_sdk_path(load_raw(raw), sdk_path=target_posix)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(str(path), registry_text(pruned))
    except Exception:  # noqa: BLE001 -- best-effort, matching every other registry write
        pass


def _resolves_to_after(workspace_root: Path) -> dict[str, Any]:
    """The narrow `resolve_sdk_tiered` answer for `workspace_root` right now
    -- tan-cli#1028's answer to "what resolves after this removal". This is
    NOT necessarily what `tan sdk current` would report for the same
    workspace at the same instant: it reuses `resolve_sdk_tiered`, the SAME
    narrow ladder `_load_bearing_reasons`'/`active` above already call
    (`resolve_sdk_tiered(None, workspace_root)`, `sdk_root` never threaded
    through from the `--sdk-root` flag either), so `remove`'s own load-bearing
    refusal logic and its `resolvesToAfter` answer are always asking the same
    question of the same ladder -- but `_run_current` falls through to the
    WIDE `resolve_sdk_root_ladder` tail when the narrow ladder finds nothing
    (tan-cli#497 defect 1) and honours an explicit `--sdk-root`. Neither
    fallback runs here, so this can report a tier -- or `"none"` -- one below
    what `sdk current` would say for the identical workspace right after this
    call returns. Consistency with `remove`'s own refusal ladder wins over
    parity with `sdk current`'s wider answer; a caller that needs the wider
    answer still makes the separate `sdk current` call.

    Called at EVERY `_remove_data` call site below, not only on a load-bearing
    removal (tan-cli#1028's own design question, answered: an always-present
    field is easier for a caller to code against than one that appears only
    sometimes). Computed FRESH at each call site -- after `remove_sdk_tree` on
    the branches that actually delete something, unchanged (because nothing on
    disk changed) on every refusal/idempotent/failed branch -- so it is
    truthful on both kinds of branch without a second "did this call mutate
    the tree" flag to keep in sync with the first.

    Mirrors `sdk current`'s own `data` SHAPE (`sdkPath`, `readiness`,
    `sourceTier`) rather than inventing a fourth shape for the same question
    -- a caller that already knows how to read `sdk current`'s answer does
    not have to learn a second shape -- and the no-SDK-resolves case -- the
    one a caller most needs told about after a load-bearing removal -- comes
    back exactly as `sdk-current-no-sdk` pins it: `sdkPath: null`, `readiness:
    null`, `sourceTier: "none"`. That `"none"` reading also covers a
    force-removed PROJECT PIN whose `.alp/sdk-path` file this command does
    not clear (tan-cli#1051): the pin is left dangling, not cleared, and this
    field cannot tell that apart from a workspace that was never pinned --
    `sdk current` afterward still carries the `sdk.project-pin-unresolved`
    warning naming the dangling pointer; `sdk remove`'s own envelope does not
    yet surface that warning.
    """
    resolved = resolve_sdk_tiered(None, workspace_root)
    return {
        "sdkPath": resolved.path,
        "readiness": check_sdk_readiness(resolved.path) if resolved.path is not None else None,
        "sourceTier": resolved.tier,
    }


def _remove_data(
    *,
    removed: bool,
    path: str | None,
    version: str | None,
    was_active: bool,
    freed_bytes: int,
    resolves_to_after: dict[str, Any],
) -> dict[str, Any]:
    """`sdk remove`'s payload shape, built at every call site through this one
    function so a field can never drift between the idempotent, refused and
    successful arms below."""
    return {
        "subcommand": "remove",
        "removed": removed,
        "path": path,
        "version": version,
        "wasActive": was_active,
        "freedBytes": freed_bytes,
        "resolvesToAfter": resolves_to_after,
    }


def _run_remove(
    *,
    json_mode: bool,
    arg: str | None,
    destination_arg: str | None,
    force: bool,
    workspace_root: Path,
) -> None:
    """`tan sdk remove <version|path>` (tan-cli#790) -- see the section banner
    above this function for the design this implements. Every refusal fires
    BEFORE any filesystem write, so the three possible outcomes -- refused,
    idempotently already-absent, or removed -- are the only ones a caller
    ever has to handle; nothing here can leave a target partially gone in a
    way a rerun cannot cleanly finish or recover from.

    `active`/`was_active` are resolved ONCE, right after the target itself,
    and threaded through every branch below (including every refusal) rather
    than recomputed per-branch -- so `data.wasActive` is truthful on the
    outside-root refusal too, not just on the one branch that refuses BECAUSE
    of it.
    """
    raw_arg = (arg or "").strip()
    if not raw_arg:
        _fail(
            json_mode=json_mode,
            data=_remove_data(
                removed=False,
                path=None,
                version=None,
                was_active=False,
                freed_bytes=0,
                resolves_to_after=_resolves_to_after(workspace_root),
            ),
            code="remove-missing-argument",
            message=(
                "`sdk remove` needs a version name (looked up under --destination) "
                "or an explicit path naming the install to remove."
            ),
            text_lines=[
                "sdk remove: needs a version or path argument, e.g. `tan sdk remove v0.15.0`."
            ],
        )
        return

    destination = Path(destination_arg).expanduser() if destination_arg else _default_cache_root()
    resolution = resolve_removal_target(raw_arg, destination)
    target = resolution.target
    target_posix = _abs_posix(str(target))
    version = raw_arg if resolution.is_named_version else None

    active = resolve_sdk_tiered(None, workspace_root)
    was_active = active.path is not None and _abs_posix(active.path) == target_posix

    # `os.path.lexists`, NOT `target.exists()`: the latter FOLLOWS a link, so a
    # BROKEN symlink or a junction whose target is gone -- an ordinary leftover in
    # a cache that has had an install removed out from under a `current ->` style
    # pointer -- reports False while the link itself is still very much on disk.
    # Answering "already absent" there breaks the idempotence this branch exists to
    # provide (tan-cli#790's own point 3): the rotation script that trusted the
    # success then fails on the NEXT install with a path that already exists.
    # Everything downstream of this gate already handles the link case correctly --
    # `compute_tree_bytes` charges the link's OWN lstat size, and
    # `dir_removal.remove_dir` unlinks the link itself rather than following it --
    # so this predicate was the single place the link was invisible.
    if not os.path.lexists(target):
        _emit(
            json_mode=json_mode,
            data=_remove_data(
                removed=False,
                path=target_posix,
                version=version,
                was_active=was_active,
                freed_bytes=0,
                resolves_to_after=_resolves_to_after(workspace_root),
            ),
            issues=[],
            exit_code=ExitCode.SUCCESS,
            text_lines=[f"sdk remove: nothing at {target_posix} -- already absent."],
        )
        return

    # Checked BEFORE the outside-root guard, and separately from it:
    # `is_outside_cache_root` deliberately answers False for `target ==
    # destination` (a caller CAN name the root on purpose), which left the
    # single most destructive target -- the whole cache, every version at
    # once -- completely unguarded: `_load_bearing_reasons` only ever names a
    # specific version subdirectory, never the root that holds them, so
    # nothing else in this function would have refused it either. Found live
    # (`tan sdk remove .` from inside an otherwise-empty cache root, `ok:
    # true`, no `--force`) -- see `sdk_removal.is_cache_root_itself`.
    if is_cache_root_itself(target, destination) and not force:
        _fail(
            json_mode=json_mode,
            data=_remove_data(
                removed=False,
                path=target_posix,
                version=version,
                was_active=was_active,
                freed_bytes=0,
                resolves_to_after=_resolves_to_after(workspace_root),
            ),
            code="remove-is-cache-root",
            message=(
                f'"{target_posix}" IS the SDK cache root itself; removing it '
                "would delete every install under it at once, not a single "
                "one. Pass --force to remove the entire cache root, or name "
                "a specific version or path to remove one install."
            ),
            text_lines=[
                f"sdk remove: {target_posix} is the cache root; refusing without --force."
            ],
        )
        return

    if is_outside_cache_root(target, destination) and not force:
        _fail(
            json_mode=json_mode,
            data=_remove_data(
                removed=False,
                path=target_posix,
                version=version,
                was_active=was_active,
                freed_bytes=0,
                resolves_to_after=_resolves_to_after(workspace_root),
            ),
            code="remove-outside-root",
            message=(
                f'"{target_posix}" is outside the SDK cache root '
                f'"{_abs_posix(str(destination))}". Pass --force to remove an '
                "explicit path outside the managed cache."
            ),
            text_lines=[
                f"sdk remove: {target_posix} is outside the cache root; refusing without --force."
            ],
        )
        return

    if not resolution.is_named_version:
        version = check_sdk_readiness(str(target)).get("version")

    reasons = _load_bearing_reasons(target_posix, active)
    if reasons and not force:
        _fail(
            json_mode=json_mode,
            data=_remove_data(
                removed=False,
                path=target_posix,
                version=version,
                was_active=was_active,
                freed_bytes=0,
                resolves_to_after=_resolves_to_after(workspace_root),
            ),
            code="remove-active",
            message=(
                f'"{target_posix}" is currently load-bearing: it is '
                + "; and it is ".join(reasons)
                + ". Pass --force to remove it anyway."
            ),
            text_lines=[
                f"sdk remove: {target_posix} is still in use; refusing without --force."
            ],
        )
        return

    outcome: RemovalOutcome = remove_sdk_tree(target)
    if not outcome.ok:
        failing = (outcome.failing_path or target_posix).replace("\\", "/")
        failure_data = _remove_data(
            removed=False,
            path=target_posix,
            version=version,
            was_active=was_active,
            freed_bytes=outcome.freed_bytes,
            # Recomputed AFTER the attempt, not carried from the pre-attempt
            # `active` above: `remove_sdk_tree` can fail partway through a
            # multi-entry tree (`outcome.freed_bytes` above is already
            # partial-attempt-aware for the identical reason), so the
            # resolution has to be re-read from the filesystem it just
            # touched rather than assumed unchanged.
            resolves_to_after=_resolves_to_after(workspace_root),
        )
        # TWO literal `code=` call sites, deliberately not one dynamic
        # `f"remove-{outcome.kind}"`: `test_every_issue_code_is_registered.py`
        # can only resolve a `code=` keyword argument to a registered wire
        # string when it is a literal at the call site, exactly the same
        # non-vacuity discipline that gate applies to every OTHER command in
        # this codebase.
        if outcome.kind == "in-use":
            _fail(
                json_mode=json_mode,
                data=failure_data,
                code="remove-in-use",
                message=(
                    f"could not remove {failing}: {outcome.detail} -- another "
                    "process still holds this open; close whatever has it open "
                    "(a shell, a build, an editor, an indexer) and retry."
                ),
                text_lines=[f"sdk remove: failed removing {target_posix} (in-use)."],
            )
        else:
            _fail(
                json_mode=json_mode,
                data=failure_data,
                code="remove-permission",
                message=(
                    f"could not remove {failing}: {outcome.detail} -- tan could "
                    "not clear the permissions/attributes blocking this; check "
                    "ownership/ACLs on the path above, or remove it by hand with "
                    "elevated privileges."
                ),
                text_lines=[f"sdk remove: failed removing {target_posix} (permission)."],
            )
        return

    _prune_registry_entries_for(target_posix)
    _emit(
        json_mode=json_mode,
        data=_remove_data(
            removed=True,
            path=target_posix,
            version=version,
            was_active=was_active,
            freed_bytes=outcome.freed_bytes,
            resolves_to_after=_resolves_to_after(workspace_root),
        ),
        issues=[],
        exit_code=ExitCode.SUCCESS,
        text_lines=[f"sdk remove: removed {target_posix} ({outcome.freed_bytes} bytes freed)."],
    )


# ── the command ─────────────────────────────────────────────────────────────


def sdk(
    subcommand: str = typer.Argument(
        None,
        metavar="SUBCOMMAND",
        help=(
            "list, current, install, switch, or remove. install/switch are not "
            "yet ported and refuse in this build -- use --sdk-root instead "
            "(tan-cli#305)."
        ),
    ),
    arg: str = typer.Argument(
        None,
        metavar="ARG",
        help="Version for install, version|path for switch/remove.",
    ),
    # `--destination` steers `install`/`switch` cache-root resolution (neither
    # ported, tan-cli#305) AND `remove`'s (tan-cli#790, live): a bare version
    # name is looked up under this root, and it is the root `remove`'s
    # outside-root footgun guard is measured against.
    destination: str = typer.Option(
        None, "--destination", metavar="PATH", help="SDK cache root (default: ~/.alp/sdk-cache)."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "With remove: also remove the active/pinned install, or a path "
            "outside the cache root."
        ),
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
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
) -> None:
    """Manage local Alp SDK installs."""
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
        elif subcommand == "remove":
            _run_remove(
                json_mode=json_mode,
                arg=arg,
                destination_arg=destination,
                force=force,
                workspace_root=workspace_root,
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


# tan-cli#261: adds the eight oracle `GlobalArgs` flags this command was
# missing entirely (`--all`/`--board-yaml`/`--ci`/`--no-color`/
# `--non-interactive`/`--quiet`/`--target`/`--verbose`); see
# `tan.core.global_flags`. All inert here: every envelope `sdk` emits reports
# `Project(root=None, board_yaml=None)`, an SDK-wide fact with no project of
# its own to anchor a `--board-yaml`/`--target` on.
sdk = accept_global_flags(sdk)
