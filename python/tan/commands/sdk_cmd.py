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
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import typer

from tan.core.global_flags import accept_global_flags
from tan.core.text_layout import wrap_lines
from tan.env import wrap_width
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.net import default_ssl_context
from tan.output_format import FORMAT_HELP, OutputFormat

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

#: The verbs `run` dispatches, for the unknown-subcommand text line. Was
#: verbatim from `sdk.rs`'s failure text until tan-cli#305: the oracle's
#: `install`/`switch` really work, so its wording is honest there; this port's
#: do not (`_run_not_ported` below), so repeating it unqualified here was one
#: more site recommending a subcommand this build refuses.
AVAILABLE_SUBCOMMANDS = (
    "Available subcommands: list, current, install <version> (refuses -- not "
    "yet ported), switch <version> (refuses -- not yet ported)"
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
#: variable that changes nothing.
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


def _read_pointer_json(pointer: Path) -> dict[str, Any] | None:
    """Parse a pointer file (`.alp/sdk-path`, `~/.alp/sdk-default`) into its
    dict, or `None` on ANY failure -- missing, unreadable, invalid JSON, or a
    non-dict shape (a list). Shared by every field reader over this shape so
    `_pointer_target` and `_pointer_written_for` can never disagree about what
    counts as an unreadable pointer.
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
    return parsed if isinstance(parsed, dict) else None


def _pointer_target(pointer: Path) -> str | None:
    """The `sdkPath` out of a `{"sdkPath": ..., "updatedAt": ...}` pointer file.

    One function for both pointers -- `tan_core`'s `resolve_active_sdk`
    (`<workspace>/.alp/sdk-path`) and `resolve_global_default_sdk`
    (`~/.alp/sdk-default`) are the same read of the same shape at two paths.
    Every failure is `None`, matching the Rust's `.ok()?` chain: a hand-edited
    pointer holding invalid JSON, a list, or no `sdkPath` at all must fall
    through to the next tier, not abort the command.
    """
    parsed = _read_pointer_json(pointer)
    value = parsed.get("sdkPath") if parsed is not None else None
    return value if isinstance(value, str) else None


def _pointer_written_for(pointer: Path) -> str | None:
    """The optional `writtenFor` field alongside `sdkPath`/`updatedAt` in
    `~/.alp/sdk-default` -- which project's bootstrap relocation last wrote
    this machine-global pointer (tan-cli#464).

    `None` covers both "no opinion" cases the same way, on purpose: a pointer
    written by an older tan that predates this field, and one this tan wrote
    for a run that never relocated a checkout FOR a project at all. Neither is
    a claim that no other project wrote the pointer -- it is the absence of
    evidence, and `resolve_sdk_tiered` must never manufacture a warning out of
    it.

    A non-`str`, empty, or non-absolute value is ALSO `None` (measured
    tan-cli#464 review regression): `writtenFor: ""` used to pass the bare
    `isinstance(value, str)` check here and reach `_workspace_under(ws, "")`,
    which resolves `Path("")` to the process's cwd -- so whether the foreign
    warning fired depended on where the caller happened to be standing, not
    on anything the pointer actually recorded. Every OTHER project root this
    field can legitimately hold is written by `bootstrap_cmd._run` as an
    already-absolute `root` (`Project.resolved`'s own contract), so rejecting
    a relative or blank one here is not narrowing real coverage ON THE
    WRITER'S OWN PLATFORM -- but `~/.alp/sdk-default` is one pointer shared by
    every tan on the host, and a bare `Path(value).is_absolute()` is answered
    by whichever pathlib flavour the READER's OS picked: `PureWindowsPath`
    needs a drive letter, so a legitimate `"/home/u/projB"` written by a
    Linux/macOS tan degraded to "no opinion" the moment a Windows tan read it
    back, and symmetrically `"C:/projB"` from a Windows writer is non-absolute
    to `PurePosixPath` on the other two. Accepted here when EITHER
    `PurePosixPath` or `PureWindowsPath` calls it absolute, so a value either
    platform's tan legitimately wrote still counts, while `""`/`"."`/a bare
    relative segment (`"projB/ws"`)/a drive-relative `"C:projB"` stay `None`
    under both -- the safe degradation is unchanged, only which absolute
    shapes clear it.
    """
    parsed = _read_pointer_json(pointer)
    value = parsed.get("writtenFor") if parsed is not None else None
    if not isinstance(value, str) or not value:
        return None
    if not PurePosixPath(value).is_absolute() and not PureWindowsPath(value).is_absolute():
        return None
    return value


def _workspace_under(workspace_root: Path, root: str) -> bool:
    """Whether `workspace_root` IS `root`, or sits somewhere below it --
    resolved on both sides so a `..`, a symlink, or Windows' case-folding
    cannot spoof a match (mirrors `tan.core.fs_confine.resolve_confined`'s own
    reasoning for the same comparison). Any resolution failure (a path shape
    the host rejects outright) reads as "not under it" -- the caller treats
    that as grounds for a WARNING, never a hard failure.
    """
    try:
        return workspace_root.resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def global_default_pointer_fix_hint(native_path: str) -> str:
    """How to fix -- or safely clear -- an already-written `~/.alp/sdk-
    default` pointer by hand, given its OS-native absolute path (the caller's
    to compute: `_home_alp_dir() / "sdk-default"`, rendered through whatever
    this-platform-separator helper it already has -- `bootstrap_cmd._native`
    for its callers).

    `_pointer_target` above degrades every read failure on this exact file
    (missing, invalid JSON, list-shaped, no `sdkPath`) to `None`, and every
    tier resolver (`resolve_sdk_tiered`) then falls through to the next tier
    on that -- so DELETING the file is always a safe recovery, never a step
    backwards; hand-editing its `"sdkPath"` field is the targeted fix when
    the caller knows what it should say instead.

    Shared so a caller describing this by hand cannot drift from what
    `_pointer_target` actually reads. `bootstrap_cmd`'s workspace-relocation
    and rollback-failure messages are why this exists (tan-cli#305 follow-
    up): they used to send a user -- sometimes one already in a broken,
    checkout-moved-but-rollback-incomplete state -- to `tan sdk switch
    --global`, which refuses outright in this build. Naming the pointer file
    directly, not the command, is also honest about the mechanism `switch`
    itself would use once ported, so this will not go stale the moment that
    disposition changes.
    """
    return (
        f'delete {native_path} (tan falls through to the next SDK it can '
        f'resolve), or edit its `"sdkPath"` field by hand'
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


# ── the four-tier precedence chain ──────────────────────────────────────────


@dataclass(frozen=True)
class ActiveSdk:
    """What `sdk current` reports: the active path (or `None`) and the tier that
    produced it. `tier` is the wire string, camelCase, from `SdkSourceTier`.

    `broken_project_pin` (tan-cli#263) is the raw `sdkPath` a workspace
    `.alp/sdk-path` pointer held when that file existed but its target failed
    the loader-script check -- `None` on every other path, including "no
    pointer file at all". Set once, in the one tier that can discover it,
    then carried through every LOWER tier this call falls through to: a caller
    that reports the tier which finally answered must still be able to say a
    pin existed and did not, rather than silently looking as deliberate as a
    workspace that was never pinned in the first place. Distinct from a
    SIXTH `sourceTier` value on purpose -- the tier that actually supplied
    `path` stays accurate; this is a supplementary fact about a REJECTED
    candidate, reported by the caller via `issues[]` instead.

    `foreign_global_default_for` (tan-cli#464) is the SAME shape of fact for a
    different silence: `~/.alp/sdk-default` is machine-global and
    last-writer-wins across every project that ever relocated a checkout on
    this host, so a caller resolving through the `globalDefault` tier from a
    workspace that is neither the project the pointer was last written for
    nor under the SDK it names is reading an answer left behind by SOMEONE
    ELSE'S bootstrap -- silently, before this. Set only on the `globalDefault`
    tier (the only one this ambiguity can apply to); `None` covers both "the
    pointer was written for (or covers) this workspace" and "the pointer
    predates `writtenFor` and carries no opinion at all" -- deliberately the
    same as `broken_project_pin`'s "no pointer" case, since neither is
    evidence of a mismatch."""

    path: str | None
    tier: str
    broken_project_pin: str | None = None
    foreign_global_default_for: str | None = None


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
    of locking the user out of every command. The project pin's own fallthrough
    is not silent, though (tan-cli#263): its raw target survives on the
    returned `ActiveSdk.broken_project_pin` no matter which lower tier ends up
    answering, so a caller can report "this workspace IS pinned, and the pin
    does not resolve" instead of looking indistinguishable from a workspace
    that was never pinned at all -- exactly what let a `tan init` run under a
    since-moved project silently re-resolve a DIFFERENT alp-sdk checkout with
    `ok: true`, `issues: []`.

    The `globalDefault` tier carries the SAME kind of silence one tier down
    (tan-cli#464): it is one pointer shared by every project on the host, so a
    caller resolving through it from a workspace the pointer was not written
    for -- and that is not even under the SDK it names -- gets no signal that
    a DIFFERENT project's bootstrap relocation is what actually decided this
    answer. `foreign_global_default_for` names that project when this run hit
    exactly that case; `None` when the pointer covers this caller, or predates
    `writtenFor` and has no opinion. Resolution is unaffected either way --
    the same root that would have been returned before this field existed
    still is.
    """
    flag = (sdk_root or "").strip()
    if flag:
        return ActiveSdk(flag, "sdkRootFlag")

    broken_project_pin: str | None = None
    pin = _pointer_target(workspace_root / ".alp" / "sdk-path")
    if pin is not None:
        if _has_loader_script(Path(pin)):
            return ActiveSdk(pin, "projectPin")
        broken_project_pin = pin

    default_pointer = _home_alp_dir() / "sdk-default"
    default = _pointer_target(default_pointer)
    if default is not None and _has_loader_script(Path(default)):
        written_for = _pointer_written_for(default_pointer)
        foreign = (
            written_for
            if written_for is not None
            and not _workspace_under(workspace_root, written_for)
            and not _workspace_under(workspace_root, default)
            else None
        )
        return ActiveSdk(default, "globalDefault", broken_project_pin, foreign)

    discovered = discover_workspace_sdk(workspace_root)
    if discovered is not None:
        return ActiveSdk(discovered, "discovery", broken_project_pin)

    return ActiveSdk(None, "none", broken_project_pin)


def project_pin_issue(broken_project_pin: str | None, tier: str) -> Issue | None:
    """The tan-cli#263 warning for an unresolvable `.alp/sdk-path` project pin
    -- shared by EVERY caller of `resolve_sdk_tiered` (directly, or through
    `build_cmd.resolve_sdk_root_ladder`/`resolve_sdk_root_wide`), not just `sdk
    current`. `None` when nothing was rejected, so every call site can do
    `issue = project_pin_issue(broken, tier); if issue: issues.append(issue)`
    unconditionally.

    `tan build` is the caller this matters most for: a workspace whose pin
    silently misses still gets a real build, against whichever SDK the ladder
    fell through to, with no signal it was not the one `.alp/sdk-path` names
    -- `sdk current` alone only helps someone already suspicious enough to run
    it."""
    if broken_project_pin is None:
        return None
    return Issue(
        "sdk.project-pin-unresolved",
        "warning",
        f'.alp/sdk-path names "{broken_project_pin}", which does not resolve '
        f"to an alp-sdk checkout from the current directory -- falling "
        f"through to the {tier} tier instead.",
    )


def global_default_foreign_project_issue(foreign_global_default_for: str | None) -> Issue | None:
    """The tan-cli#464 warning for a `globalDefault` answer that a DIFFERENT
    project's bootstrap relocation actually decided: `~/.alp/sdk-default` is
    one pointer, shared and last-writer-wins across every project that ever
    relocates a checkout on this host, so the earlier of two projects can
    silently start resolving the later one's SDK the moment the later one
    bootstraps -- `ok: true`, `issues: []`, same as a caller that was never
    pinned at all (the maintainer's own #464 repro).

    `None` when `resolve_sdk_tiered` found nothing to warn about -- the
    pointer covers this caller, or it predates `writtenFor` and carries no
    opinion -- so every call site can do `issue =
    global_default_foreign_project_issue(active.foreign_global_default_for);
    if issue: issues.append(issue)` unconditionally, exactly like
    `project_pin_issue`. Resolution itself is unchanged by this warning: the
    root `sdk current` reports is the same root it would have reported before
    this fix existed."""
    if foreign_global_default_for is None:
        return None
    return Issue(
        "sdk.global-default-foreign-project",
        "warning",
        f'The machine-global default SDK (~/.alp/sdk-default) was last set by '
        f'a bootstrap relocation in "{foreign_global_default_for}", not by one '
        f"here -- this workspace is falling through to that project's SDK, "
        f"which may not be the checkout you expect. Pin this workspace "
        f"explicitly with `--sdk-root <path>`, or bootstrap here, to stop "
        f"relying on the shared default.",
    )


def sdk_resolution_issues(
    broken_project_pin: str | None, tier: str, foreign_global_default_for: str | None
) -> list[Issue]:
    """`project_pin_issue` + `global_default_foreign_project_issue`, together,
    in the order `flash`/`size`/`image` -- the three callers this actually
    has -- append them: each used to compute this pair only on the happy
    path and skip it on a manifest-gate early return.

    **Not the only copy.** Twelve other modules still hand-copy the pair
    directly rather than through here -- most fold in a third, caller-
    specific issue or use a different order, not the mechanical swap it
    looks like; left as-is (tan-cli#464 review).

    `[]` when neither fires -- a caller can `issues.extend(...)` unconditionally."""
    issues: list[Issue] = []
    pin_issue = project_pin_issue(broken_project_pin, tier)
    if pin_issue is not None:
        issues.append(pin_issue)
    foreign_issue = global_default_foreign_project_issue(foreign_global_default_for)
    if foreign_issue is not None:
        issues.append(foreign_issue)
    return issues


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
            request, timeout=NETWORK_TIMEOUT_SECONDS, context=default_ssl_context()
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
    # `tan generate` did not use. Function-level import because `build_cmd`
    # imports THIS module at line 97; the same one-way dependency
    # `build/manifest.py` works around the same way.
    from tan.commands.build_cmd import sdk_ladder_divergence_issue

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


# ── the command ─────────────────────────────────────────────────────────────


def sdk(
    subcommand: str = typer.Argument(
        None,
        metavar="SUBCOMMAND",
        help=(
            "list, current, install, or switch. install/switch are not yet "
            "ported and refuse in this build -- use --sdk-root instead "
            "(tan-cli#305)."
        ),
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
