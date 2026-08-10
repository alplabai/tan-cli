# SPDX-License-Identifier: Apache-2.0
"""``tan sdk`` -- the paths the two committed goldens do not reach.

``contract/envelopes/sdk-current-no-sdk`` and ``sdk-unknown-subcommand`` pin the
no-checkout answer and the unknown-verb refusal. Everything else about this
command is unguarded by them, and three groups of it are worth real assertions:

* **the four-tier precedence chain**, whose whole point is which SDK a later
  ``build``/``flash`` will use. No golden resolves an SDK at all (the harness
  isolates ``HOME`` precisely so none can), so every tier above ``none`` is
  covered only here.
* **``--sdk-root`` being terminal even when invalid** (I-31). The Pythonic
  ``if not valid: continue`` inverts it and reports a *different* checkout than
  the user named -- silently, with exit 0.
* **no path emits a traceback**. The recurring bug class in this port is an
  uncaught exception replacing the envelope: stdout empty, a traceback on
  stderr, and an extension that renders nothing with no error anywhere.

Envelope-shape cases run as a real subprocess, matching
``test_init_command.py``: exit code, ONE JSON document on stdout, and stdout
staying clean in text mode are all properties of the process, not of a function.
The pure helpers are called in-process -- there is nothing a subprocess would add.

Nothing here touches the network. ``sdk list`` is asserted to REFUSE without
``--online``, which is the only assertion about it that can be made hermetically.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer

from tan.commands.sdk_cmd import (
    _fetch_releases,
    check_sdk_readiness,
    describe_network_error,
    discover_workspace_sdk,
    parse_remote_sdk_releases,
    parse_sdk_version_yaml,
    resolve_sdk_tiered,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def run_tan(*argv, cwd, env_extra=None):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
        **(env_extra or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
    )


def envelope(proc):
    """The one JSON document on stdout. Zero or two are the same break for a
    consumer that parses stdout whole."""
    assert "Traceback" not in proc.stderr, f"an exception escaped the contract:\n{proc.stderr}"
    assert proc.stdout.strip(), f"no envelope on stdout; stderr:\n{proc.stderr}"
    return json.loads(proc.stdout)


def make_sdk_root(path: Path, *, metadata: bool = True, version: str | None = None) -> Path:
    """Turn `path` into something `_has_loader_script` accepts."""
    (path / "scripts").mkdir(parents=True, exist_ok=True)
    (path / "scripts" / "alp_project.py").write_text("", encoding="utf-8", newline="")
    if metadata:
        (path / "metadata").mkdir(exist_ok=True)
    if version is not None:
        (path / "VERSION").write_text(f"{version}\n", encoding="utf-8", newline="")
    return path


def write_pointer(path: Path, sdk_path: Path, *, written_for: Path | str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"sdkPath": str(sdk_path), "updatedAt": "1970-01-01T00:00:00Z"}
    if written_for is not None:
        doc["writtenFor"] = str(written_for)
    path.write_text(json.dumps(doc), encoding="utf-8", newline="")


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """A `~/.alp` nothing else can populate -- the machine-global-default tier
    and the install-cache listing both read it, so a developer's real one would
    otherwise decide what these tests observe."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


# ── the precedence chain ─────────────────────────────────────────────────────


def test_sdk_root_flag_is_terminal_even_when_it_is_not_a_checkout(tmp_path, isolated_home):
    """I-31: `--sdk-root` wins and is returned AS-IS, so a bad path fails loudly
    at the readiness report. Falling through to a lower tier here would report a
    different SDK than the one the user named."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # A perfectly good lower tier exists and must NOT win.
    pin_target = make_sdk_root(tmp_path / "pinned")
    write_pointer(workspace / ".alp" / "sdk-path", pin_target)

    active = resolve_sdk_tiered("  not/an/sdk  ", workspace)
    assert active.tier == "sdkRootFlag"
    assert active.path == "not/an/sdk", "the flag is trimmed but otherwise verbatim"

    readiness = check_sdk_readiness(active.path)
    assert readiness["state"] == "missing"
    assert readiness["loaderScriptPresent"] is False


def test_tier_order_is_flag_then_pin_then_global_then_discovery(tmp_path, isolated_home):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pin_target = make_sdk_root(tmp_path / "pinned")
    global_target = make_sdk_root(tmp_path / "globally-default")

    write_pointer(isolated_home / ".alp" / "sdk-default", global_target)
    assert resolve_sdk_tiered(None, workspace).tier == "globalDefault"

    write_pointer(workspace / ".alp" / "sdk-path", pin_target)
    active = resolve_sdk_tiered(None, workspace)
    assert (active.tier, active.path) == ("projectPin", str(pin_target))

    assert resolve_sdk_tiered(str(global_target), workspace).tier == "sdkRootFlag"


def test_a_stale_pointer_falls_through_instead_of_locking_the_user_out(tmp_path, isolated_home):
    """Both pointer tiers are best-effort: a pointer whose target is gone must
    not win, or one deleted directory breaks every command. tan-cli#263: the
    fallthrough must not be SILENT about it, though -- the rejected pin's own
    target survives on `broken_project_pin` no matter which lower tier answers,
    so a caller can still say a pin existed and did not resolve."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    write_pointer(workspace / ".alp" / "sdk-path", tmp_path / "deleted-checkout")
    active = resolve_sdk_tiered(None, workspace)
    assert (active.tier, active.path) == ("none", None)
    assert active.broken_project_pin == str(tmp_path / "deleted-checkout")


@pytest.mark.parametrize(
    "contents",
    ["not json at all", "[]", '{"noSuchKey": 1}', '{"sdkPath": 42}'],
    ids=["invalid-json", "array", "missing-key", "wrong-type"],
)
def test_a_malformed_pointer_is_a_fallthrough_not_a_crash(
    tmp_path, isolated_home, contents
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pointer = workspace / ".alp" / "sdk-path"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(contents, encoding="utf-8", newline="")
    assert resolve_sdk_tiered(None, workspace).tier == "none"


# ── tan-cli#464: the shared, last-writer-wins global default pointer ────────


def test_global_default_written_for_is_absent_on_an_older_pointer(tmp_path, isolated_home):
    """A pointer written by a tan that predates `writtenFor` carries no
    opinion -- absent must read as UNKNOWN, never as "foreign", or every
    upgrade would start warning on a pointer nobody wrote wrong."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    global_target = make_sdk_root(tmp_path / "globally-default")
    write_pointer(isolated_home / ".alp" / "sdk-default", global_target)  # no writtenFor

    active = resolve_sdk_tiered(None, workspace)
    assert active.tier == "globalDefault"
    assert active.foreign_global_default_for is None


def test_global_default_names_the_project_it_was_written_for(tmp_path, isolated_home):
    """The maintainer's #464 repro shape: a caller resolving through
    `globalDefault` from a workspace that is neither the project the pointer
    was written for nor under the SDK it names gets the fact on `ActiveSdk`,
    without the resolved root changing at all."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    other_project = tmp_path / "other-project"
    other_project.mkdir()
    global_target = make_sdk_root(tmp_path / "globally-default")
    write_pointer(
        isolated_home / ".alp" / "sdk-default", global_target, written_for=other_project
    )

    active = resolve_sdk_tiered(None, workspace)
    assert active.tier == "globalDefault"
    assert active.path == str(global_target)
    assert active.foreign_global_default_for == str(other_project)


def test_global_default_does_not_warn_for_the_project_it_was_written_for(tmp_path, isolated_home):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    global_target = make_sdk_root(tmp_path / "globally-default")
    write_pointer(
        isolated_home / ".alp" / "sdk-default", global_target, written_for=workspace
    )
    assert resolve_sdk_tiered(None, workspace).foreign_global_default_for is None


def test_global_default_does_not_warn_for_a_subdirectory_of_the_written_for_project(
    tmp_path, isolated_home
):
    """Closing the SUBdirectory silence #464 names explicitly: a caller
    beneath the project the pointer was written for is still that project,
    not a foreign one."""
    project = tmp_path / "proj"
    sub = project / "firmware" / "app"
    sub.mkdir(parents=True)
    global_target = make_sdk_root(tmp_path / "globally-default")
    write_pointer(isolated_home / ".alp" / "sdk-default", global_target, written_for=project)

    assert resolve_sdk_tiered(None, sub).foreign_global_default_for is None


def test_global_default_does_not_warn_from_inside_the_sdk_checkout_it_names(
    tmp_path, isolated_home
):
    """A caller working FROM the SDK checkout the pointer resolves to is not
    "foreign", even when `writtenFor` names a different project entirely --
    resolution behaviour (returning that checkout) is unchanged by this fix,
    so the warning must not contradict it."""
    global_target = make_sdk_root(tmp_path / "globally-default")
    write_pointer(
        isolated_home / ".alp" / "sdk-default",
        global_target,
        written_for=tmp_path / "unrelated-project",
    )
    assert resolve_sdk_tiered(None, global_target).foreign_global_default_for is None


def test_global_default_written_for_empty_string_reads_as_unknown(tmp_path, isolated_home):
    """tan-cli#464 review regression: `writtenFor: ""` used to pass the bare
    `isinstance(value, str)` check in `_pointer_written_for` and reach
    `_workspace_under(ws, "")`, which resolves `Path("")` to the process's OWN
    cwd -- so whether the foreign warning fired depended on where the caller
    happened to be standing, not on anything the pointer actually recorded.
    `workspace` is a `tmp_path` descendant, never the test runner's cwd, so
    the pre-fix bug reports "foreign" here."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    global_target = make_sdk_root(tmp_path / "globally-default")
    write_pointer(isolated_home / ".alp" / "sdk-default", global_target, written_for="")
    assert resolve_sdk_tiered(None, workspace).foreign_global_default_for is None


def test_global_default_written_for_relative_path_reads_as_unknown(tmp_path, isolated_home):
    """The `is_absolute()` clause itself had zero coverage: every existing
    case above (`""`, a non-`str` shape) is already caught by the bare
    `isinstance(value, str) and value` check, so a mutant that dropped the
    absolute check entirely still passed every one of them. A bare relative
    segment is a shape no real writer ever produces (`bootstrap_cmd._run`
    only ever writes an already-absolute `Project.resolved` root), but must
    degrade the same safe way, not warn from wherever `_workspace_under`
    happens to resolve it relative to."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    global_target = make_sdk_root(tmp_path / "globally-default")
    write_pointer(
        isolated_home / ".alp" / "sdk-default", global_target, written_for="projB/ws"
    )
    assert resolve_sdk_tiered(None, workspace).foreign_global_default_for is None


def test_global_default_written_for_cross_platform_absolute_path_is_recognised(
    tmp_path, isolated_home
):
    """tan-cli#464 stage-2 review: `Path(value).is_absolute()` is answered by
    whichever pathlib flavour the READER's OS picked -- a POSIX-absolute
    `writtenFor` a Linux/macOS tan legitimately wrote (`/home/u/projB`) is NOT
    absolute under `PureWindowsPath` (no drive letter), so it used to degrade
    to "no opinion" here on a Windows reader, exactly the silent drop
    tan-cli#464 exists to close -- just triggered by which OS is reading the
    shared pointer rather than by which project wrote it. Accepted now
    because `PurePosixPath("/home/u/projB").is_absolute()` is `True`
    regardless of host OS."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    global_target = make_sdk_root(tmp_path / "globally-default")
    foreign_root = "/home/u/projB"
    write_pointer(
        isolated_home / ".alp" / "sdk-default", global_target, written_for=foreign_root
    )
    assert resolve_sdk_tiered(None, workspace).foreign_global_default_for == foreign_root


def test_global_default_written_for_cross_platform_windows_path_is_recognised(
    tmp_path, isolated_home
):
    """The symmetric direction from the test above, and the one that actually
    kills the single-platform mutant: `/home/u/projB` above is ALREADY
    absolute under a bare `Path(value).is_absolute()` on whichever POSIX
    runner this suite happens to run on (`ubuntu-latest`/`macos-latest`), so
    that test alone passes against the pre-fix, reader-OS-native check too --
    it only proves something on a Windows runner. A Windows-written
    `writtenFor` (`C:/projB`) is NOT absolute under a bare `Path(...)` on
    POSIX (no leading `/`), so THIS direction is the one newly accepted on
    Linux/macOS by checking `PureWindowsPath` too, and had no coverage at
    all before this fix."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    global_target = make_sdk_root(tmp_path / "globally-default")
    foreign_root = "C:/projB"
    write_pointer(
        isolated_home / ".alp" / "sdk-default", global_target, written_for=foreign_root
    )
    assert resolve_sdk_tiered(None, workspace).foreign_global_default_for == foreign_root


@pytest.mark.parametrize(
    "written_for", [123, ["a"], {"x": 1}, None], ids=["int", "list", "dict", "null"]
)
def test_global_default_written_for_wrong_type_reads_as_unknown(
    tmp_path, isolated_home, written_for
):
    """Every non-`str` shape already fell through to `None` safely before the
    tan-cli#464 review closed the empty-string gap above; this pins that it
    still does -- a malformed `writtenFor` must degrade to "no opinion", the
    same as a pointer that predates the field entirely, never a crash."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    global_target = make_sdk_root(tmp_path / "globally-default")
    pointer = isolated_home / ".alp" / "sdk-default"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps({"sdkPath": str(global_target), "writtenFor": written_for}),
        encoding="utf-8",
        newline="",
    )
    assert resolve_sdk_tiered(None, workspace).foreign_global_default_for is None


def test_discovery_prefers_the_workspace_itself_and_refuses_ambiguity(tmp_path):
    """`discover_workspace_sdk` is exactly-one-or-none, unlike the build path's
    wider first-match-wins `discover_sdk_root`. Two candidates is ambiguous, so
    `sourceTier` never claims `discovery` for a path nothing else resolves."""
    lonely = tmp_path / "solo" / "ws"
    lonely.mkdir(parents=True)
    make_sdk_root(lonely.parent / "alp-sdk")
    assert discover_workspace_sdk(lonely) == str(lonely.parent / "alp-sdk").replace("\\", "/")

    both = tmp_path / "both" / "ws"
    make_sdk_root(both)
    make_sdk_root(both.parent / "alp-sdk")
    assert discover_workspace_sdk(both) is None, "two candidates is ambiguous, not a choice"


def test_discovery_finds_the_enclosing_checkout_only_when_nothing_lateral_does(tmp_path):
    checkout = make_sdk_root(tmp_path / "alp-sdk")
    nested = checkout / "examples" / "peripheral-io" / "gpio"
    nested.mkdir(parents=True)
    assert discover_workspace_sdk(nested) == str(checkout).replace("\\", "/")


# ── readiness ────────────────────────────────────────────────────────────────


def test_readiness_states(tmp_path):
    ready = make_sdk_root(tmp_path / "ready", version="9.9.9")
    assert check_sdk_readiness(str(ready))["state"] == "ready"
    assert check_sdk_readiness(str(ready))["version"] == "9.9.9"

    # Marker present, metadata/ absent -> partial, NOT missing.
    partial = make_sdk_root(tmp_path / "partial", metadata=False)
    report = check_sdk_readiness(str(partial))
    assert report["state"] == "partial"
    assert report["loaderScriptPresent"] is True
    assert report["issues"] == ["metadata/ directory is missing."]

    missing = check_sdk_readiness(str(tmp_path / "nowhere"))
    assert missing["state"] == "missing"
    assert missing["version"] is None


def test_version_falls_back_to_sdk_version_yaml(tmp_path):
    """tan-cli #162: no released alp-sdk ships a top-level `VERSION`, so without
    this fallback every `sdk current` reported `version (unknown)` while
    `tan doctor` resolved a real one from the other file."""
    root = make_sdk_root(tmp_path / "sdk")
    (root / "metadata" / "sdk_version.yaml").write_text(
        "# single source of truth\nversion: 0.13.0\n", encoding="utf-8", newline=""
    )
    assert check_sdk_readiness(str(root))["version"] == "0.13.0"

    # A present-but-empty VERSION is "no version", so the fallback still runs.
    (root / "VERSION").write_text("   \n", encoding="utf-8", newline="")
    assert check_sdk_readiness(str(root))["version"] == "0.13.0"

    # A real VERSION wins -- an extracted release archive must not regress.
    (root / "VERSION").write_text("1.2.3\n", encoding="utf-8", newline="")
    assert check_sdk_readiness(str(root))["version"] == "1.2.3"


def test_readiness_survives_a_non_utf8_version_file(tmp_path):
    """I-27's read side: a bare `read_text()` decodes with the host locale, so
    one stray byte raises on a cp1252 Windows host and passes on ubuntu CI."""
    root = make_sdk_root(tmp_path / "sdk")
    (root / "VERSION").write_bytes(b"\xff\xfe not utf-8")
    report = check_sdk_readiness(str(root))
    assert report["issues"] == ["VERSION file could not be read."]
    assert report["state"] == "partial"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("version: 0.13.0", "0.13.0"),
        ('version: "0.13.0"', "0.13.0"),
        ("# comment\n\n0.14.0\n", "0.14.0"),
        ("version:\nother: x\n", None),
        ("", None),
    ],
)
def test_parse_sdk_version_yaml(text, expected):
    assert parse_sdk_version_yaml(text) == expected


# ── release parsing ──────────────────────────────────────────────────────────


def test_release_parsing_drops_untagged_and_defaults_the_flags():
    releases = parse_remote_sdk_releases(
        [
            {
                "tag_name": "v1.5.0",
                "published_at": "2024-01-02T00:00:00Z",
                "tarball_url": "u",
                "body": "First line.\n\nrest",
            },
            {"published_at": "x"},
            {"tag_name": "v2.0.0-rc1", "body": "b", "draft": True, "prerelease": True},
        ]
    )
    assert [r["tag"] for r in releases] == ["v1.5.0", "v2.0.0-rc1"]
    assert releases[0]["releaseNotesSummary"] == "First line."
    assert releases[0]["releaseNotes"] == "First line.\n\nrest"
    # #122: absent flags are "not flagged", never a reason to drop the release.
    assert (releases[0]["draft"], releases[0]["prerelease"]) == (False, False)
    assert (releases[1]["draft"], releases[1]["prerelease"]) == (True, True)
    # Absent strings become "", matching serde's `unwrap_or("")`.
    assert releases[1]["tarballUrl"] == ""


def test_a_non_array_payload_is_rejected():
    assert parse_remote_sdk_releases({"message": "Not Found"}) is None


@pytest.mark.parametrize(
    ("raised", "expect_in_message"),
    [
        (OSError("certificate verify failed"), "corporate CA"),
        (OSError("proxy tunnel refused"), "ALL_PROXY/HTTPS_PROXY/NO_PROXY"),
        (TimeoutError(), "TimeoutError"),
        (RuntimeError("something nobody anticipated"), "something nobody anticipated"),
    ],
    ids=["tls", "proxy", "timeout-no-message", "unanticipated"],
)
def test_a_dead_network_returns_a_message_never_an_exception(
    monkeypatch, raised, expect_in_message
):
    """The one path in this file that touches a socket. Every transport failure
    has to come back as a MESSAGE so the caller still has an envelope to emit --
    an exception escaping here is the port's recurring bug class. `TimeoutError`
    carries no text at all, which is why the fallback names the class."""

    def boom(*_args, **_kwargs):
        raise raised

    # `OpenerDirector.open`, not `urlopen`: tan-cli#497 builds an EXPLICIT
    # opener (its own `ProxyHandler`, because urllib's env-derived one ignores
    # `ALL_PROXY` on an https request), so the module-level `urlopen` is no
    # longer the call this path makes.
    monkeypatch.setattr("tan.commands.sdk_cmd.urllib.request.OpenerDirector.open", boom)
    releases, error = _fetch_releases()
    assert releases == []
    assert error is not None and expect_in_message in error


def test_the_tls_hint_does_not_assert_a_proxy_with_no_evidence_for_one():
    """#304: the frozen v0.5.0-rc2 macOS asset hit `CERTIFICATE_VERIFY_FAILED`
    with no proxy and no corporate CA anywhere on the host -- the real cause
    was the freeze shipping with zero CA trust anchors (`tan/net.py`). The old
    text opened "This is USUALLY a TLS-intercepting proxy or a corporate CA",
    asserting one specific cause with nothing to back it; on a locked-down
    network that also hides a REAL proxy misconfiguration by sending the one
    population likely to have one down the wrong path too. This message must
    name the alternative instead of asserting a single cause."""
    message = describe_network_error("certificate verify failed", proxy_used=False)
    assert "usually" not in message.lower()
    assert "corporate CA" in message
    assert "trust" in message.lower() and ("load" in message.lower() or "ca" in message.lower())


# ── envelopes, driven as a real process ──────────────────────────────────────


def test_current_reports_a_resolved_sdk_with_the_optional_sdk_key(tmp_path, isolated_home):
    """`sdk` is absent from every golden (none resolves a checkout), so the key
    that appears once one does is covered only here -- and it must be ABSENT
    rather than null when nothing resolves."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = make_sdk_root(tmp_path / "the-sdk", version="0.14.0")

    proc = run_tan(
        "sdk", "current", "--sdk-root", str(root), "--format", "json", cwd=workspace
    )
    assert proc.returncode == 0
    env = envelope(proc)
    assert env["ok"] is True
    assert env["data"]["sourceTier"] == "sdkRootFlag"
    assert env["data"]["readiness"]["state"] == "ready"
    assert env["data"]["readiness"]["version"] == "0.14.0"
    # POSIX separators, not the host's: Rust normalises in `sdk_report.rs`
    # (`root.replace('\\', "/")`) and guarantees `sdk.root` never diverges by
    # separator style. Asserting `str(root)` here would pin the platform-native
    # form and bake the bug in -- see `SdkInfo.as_dict`.
    assert env["sdk"] == {
        "root": str(root).replace("\\", "/"),
        "sourceTier": "sdkRootFlag",
    }
    assert env["project"] == {"root": None, "boardYaml": None}


def test_current_with_no_sdk_omits_the_sdk_key(tmp_path, isolated_home):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = envelope(run_tan("sdk", "current", "--format", "json", cwd=workspace))
    assert "sdk" not in env, "absent, never null"
    assert env["data"]["sdkPath"] is None
    assert env["data"]["sourceTier"] == "none"


def test_current_names_an_unresolvable_project_pin_instead_of_reporting_it_silently(
    tmp_path, isolated_home
):
    """tan-cli#263: the maintainer's exact repro was a workspace whose
    `.alp/sdk-path` named an unreachable checkout resolving `ok: true`,
    `issues: []`, with `sourceTier` silently one tier lower -- indistinguishable
    from a workspace that was never pinned. A discovered fallback still
    resolves SOMETHING here; the point is that resolving something must not
    erase the fact that the pin itself was rejected."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    broken_target = tmp_path / "does-not-exist"
    write_pointer(workspace / ".alp" / "sdk-path", broken_target)
    make_sdk_root(workspace.parent / "alp-sdk")

    env = envelope(run_tan("sdk", "current", "--format", "json", cwd=workspace))
    assert env["ok"] is True
    assert env["data"]["sourceTier"] == "discovery"
    assert env["issues"] == [
        {
            "code": "sdk.project-pin-unresolved",
            "severity": "warning",
            "message": (
                f'.alp/sdk-path names "{broken_target}", which does not resolve '
                f"to an alp-sdk checkout from the current directory -- falling "
                f"through to the discovery tier instead."
            ),
        }
    ]


def test_list_without_online_answers_offline_and_touches_no_network(tmp_path, isolated_home):
    """tan-cli#351: bare `sdk list` is a NORMAL state, matching `sdk current`'s
    "nothing configured" -- exit 0, `ok: true` -- not a failure. The oracle has
    no `--online` flag and always reaches the network for `sdk list`; gating it
    is this port's own hermeticity addition (I-23), so the gate itself must not
    read as an error. `sdk.network-required` survives as a `warning`-severity
    issue (still present, still the code a consumer can key on), not an
    `error` on a passing envelope."""
    proc = run_tan("sdk", "list", "--format", "json", cwd=tmp_path)
    assert proc.returncode == 0
    env = envelope(proc)
    assert env["ok"] is True
    assert env["issues"][0]["code"] == "sdk.network-required"
    assert env["issues"][0]["severity"] == "warning"
    assert "upstream" in env["issues"][0]["message"].lower()
    assert "--online" in env["issues"][0]["message"]
    # The `list`-shaped payload survives: the extension reads `data.releases`
    # with a `?? []` fallback.
    assert env["data"] == {"subcommand": "list", "releases": []}


def test_list_without_online_text_mode_names_upstream_and_the_flag(tmp_path, isolated_home):
    """Text mode gets the same "this is normal, here's the switch" framing as
    JSON, not the old "this command needs network access" wording that read
    like a broken host rather than a plain missing flag."""
    proc = run_tan("sdk", "list", cwd=tmp_path)
    assert proc.returncode == 0
    assert "upstream" in proc.stderr.lower()
    assert "--online" in proc.stderr


@pytest.mark.parametrize("verb", ["install", "switch"])
def test_install_and_switch_refuse_loudly_rather_than_half_working(tmp_path, isolated_home, verb):
    """A partial `switch` writes the active-SDK pointer but skips the
    `.west/config` reconciliation, reporting success while `west` keeps
    resolving the old SDK -- tan-cli #62, re-introduced invisibly.

    Exit 1, not 5. This asserted 5 until #262, matching a `_run_not_ported`
    docstring whose stated precedent (`validate_cmd`) actually uses 1. Exit 1
    is what every sibling refusal in `sdk_cmd` uses, what `deferred_cmd`'s
    stubs use, and what the oracle itself returns for a `sdk switch` that
    cannot resolve (`sdk.path-not-found`, measured). 5 means "tan crashed"."""
    proc = run_tan("sdk", verb, "v0.14.0", "--format", "json", cwd=tmp_path)
    assert proc.returncode == 1
    env = envelope(proc)
    assert env["ok"] is False
    assert env["issues"][0]["code"] == "sdk.not-ported"
    assert env["data"]["subcommand"] == verb


def test_a_bare_sdk_reports_the_none_placeholder(tmp_path, isolated_home):
    proc = run_tan("sdk", "--format", "json", cwd=tmp_path)
    assert proc.returncode == 1
    env = envelope(proc)
    assert env["issues"][0]["message"] == "Unknown sdk subcommand: (none)"


def test_text_mode_writes_nothing_to_stdout(tmp_path, isolated_home):
    """stdout is the envelope channel in BOTH modes; human lines go to stderr,
    so a consumer that starts parsing stdout never has to special-case text."""
    proc = run_tan("sdk", "current", cwd=tmp_path)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "No active SDK configured for this workspace." in proc.stderr


def test_a_bad_format_value_is_a_usage_error_not_a_traceback(tmp_path, isolated_home):
    proc = run_tan("sdk", "current", "--format", "yaml", cwd=tmp_path)
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr


# --- format_release_table (#316) ---------------------------------------------
#
# This table had NO test at all before #316 -- which is how a release body's
# newline came to be printed straight into the middle of a row and shipped in
# v0.5.0-rc1 through -rc3.


def _release(tag, summary, *, draft=False, prerelease=False):
    return {
        "tag": tag,
        "publishedAt": "2026-07-06T00:00:00Z",
        "releaseNotesSummary": summary,
        "draft": draft,
        "prerelease": prerelease,
    }


def test_a_multi_line_release_body_stays_on_one_row():
    """The #316 defect verbatim: a body whose FIRST line is short (`##
    Highlights`) used to emit a literal newline mid-row, so the row spilled
    onto a second, unindented line."""
    from tan.commands.sdk_cmd import format_release_table  # noqa: PLC0415

    body = "## Highlights\n- **`alp` CLI as the single front door** - `bu`\n- more"
    lines = format_release_table([_release("v0.9.0", body)])

    assert len(lines) == 2, f"header + exactly one row expected, got {lines!r}"
    assert "\n" not in lines[1]
    assert lines[1].startswith("  v0.9.0")
    # The collapsed text is what the 60-char budget is spent on, so content
    # from BEYOND the first line now reaches the cell.
    assert "## Highlights - **`alp` CLI" in lines[1]


def test_the_flags_suffix_stays_attached_to_its_own_row():
    """The consequence that made this more than cosmetic: with the row split,
    `[prerelease]` landed after the spilled text, describing nothing."""
    from tan.commands.sdk_cmd import format_release_table  # noqa: PLC0415

    lines = format_release_table(
        [_release("v0.9.0", "## Highlights\n- something", prerelease=True)]
    )

    assert len(lines) == 2
    assert lines[1].endswith(" [prerelease]")


@pytest.mark.parametrize(
    "summary, expected_present",
    [
        ("plain one-liner", "plain one-liner"),
        ("tabs\tand\nnewlines   collapse", "tabs and newlines collapse"),
        ("   leading and trailing   ", "leading and trailing"),
    ],
    ids=["single-line-untouched", "all-whitespace-collapses", "edges-stripped"],
)
def test_whitespace_is_collapsed_not_just_newlines(summary, expected_present):
    from tan.commands.sdk_cmd import format_release_table  # noqa: PLC0415

    lines = format_release_table([_release("v1.0.0", summary)])
    assert expected_present in lines[1]


@pytest.mark.parametrize(
    "summary", [None, ""], ids=["missing-summary", "empty-summary"]
)
def test_a_release_with_no_notes_renders_without_them(summary):
    """`releaseNotesSummary` is absent or empty for a release with no body --
    the collapse must not turn that into a crash or a stray column."""
    from tan.commands.sdk_cmd import format_release_table  # noqa: PLC0415

    lines = format_release_table([_release("v1.0.0", summary)])
    assert lines[1].rstrip() == "  v1.0.0       2026-07-06"


def test_the_notes_cell_is_still_truncated_to_sixty_characters():
    """Collapsing must not become a licence to print the whole body."""
    from tan.commands.sdk_cmd import format_release_table  # noqa: PLC0415

    lines = format_release_table([_release("v1.0.0", "x" * 200)])
    assert "x" * 60 in lines[1]
    assert "x" * 61 not in lines[1]


# --------------------------------------------------------------------------
# The shared wrap seam (`tan.core.text_layout.wrap_lines` via
# `tan.env.wrap_width`), adopted here after `doctor` (PR #480) and `explain`.
# `wrap_lines` is pure logic and lives (and is unit-tested generically) in
# `tan.core.text_layout` -- `test_text_layout.py` covers the mechanism.
# What is worth asserting here is composition: `sdk current`'s own
# assembled report going through it correctly, on a real terminal and a
# piped one.
#
# There is no per-line exemption any more (an earlier version of this seam
# kept `  path`/`  version`/`  state`/issue rows unwrapped via a
# `_RECORD_LINE_PREFIXES` classification table, on the theory that a piped
# reader might grep them) -- that reader can never observe a wrapped line in
# the first place: `wrap_width()` returns a width only when stderr IS a
# tty, and any pipe that could grep these lines makes stderr not a tty,
# which already returns `None` and disables wrapping wholesale (see
# `test_current_text_mode_does_not_wrap_off_a_terminal` below).
# --------------------------------------------------------------------------


def test_wrap_lines_wraps_the_no_sdk_next_steps_line():
    """The measured baseline this task's brief names verbatim: bare `tan sdk
    current`'s "To get started, ..." line is 137 columns unwrapped."""
    from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS  # noqa: PLC0415
    from tan.core.text_layout import wrap_lines  # noqa: PLC0415

    lines = ["No active SDK configured for this workspace.", f"To get started, {NO_SDK_NEXT_STEPS}."]
    assert len(lines[1]) == 137

    wrapped = wrap_lines(lines, 100)
    assert not any(len(line) > 100 for line in wrapped)
    # Reassembled, the wrapped text is still the exact original sentence.
    # `.strip()` peels off the two-space hanging indent `wrap_lines` puts on
    # a continuation line (item 5's fix) before rejoining with a single
    # space -- not a blanket `.replace("  ", " ")`, which would also (and
    # wrongly) collapse a legitimate double space inside the body itself,
    # were there one (there is not, in `NO_SDK_NEXT_STEPS` -- verified).
    assert " ".join(line.strip() for line in wrapped) == " ".join(lines)


def test_wrap_lines_now_wraps_the_fixed_fields_too():
    """Contract change from this task: `  path`/`  version`/`  state` are no
    longer exempt from wrapping (see the section comment above) -- on a
    real terminal narrower than the line, this now wraps like any other,
    with `break_long_words=False` still keeping the path token itself
    intact on its own line rather than mangled mid-character."""
    from tan.commands.sdk_cmd import format_readiness_block  # noqa: PLC0415
    from tan.core.text_layout import wrap_lines  # noqa: PLC0415

    report = {
        "sdkPath": "/very/long/nested/checkout/path/" + "segment/" * 10 + "alp-sdk",
        "version": "0.14.0",
        "state": "ready",
        "issues": [],
    }
    lines = format_readiness_block("Active SDK", report)
    wrapped = wrap_lines(lines, 60)
    assert wrapped != lines, "the long path line must actually wrap now"
    assert any(report["sdkPath"] in line for line in wrapped), (
        "the path token itself must survive whole"
    )


def test_wrap_lines_wraps_an_issue_sentence_but_keeps_the_quoted_path_whole():
    from tan.commands.sdk_cmd import format_readiness_block  # noqa: PLC0415
    from tan.core.text_layout import wrap_lines  # noqa: PLC0415

    long_path = "/nested/" + "segment/" * 8 + "checkout"
    report = {
        "sdkPath": long_path,
        "version": None,
        "state": "missing",
        "issues": [f'scripts/alp_project.py not found — "{long_path}" is not a valid Alp SDK root.'],
    }
    lines = format_readiness_block("Active SDK", report)
    wrapped = wrap_lines(lines, 60)
    assert not any(len(line) > 60 for line in wrapped if long_path not in line)
    assert any(long_path in line for line in wrapped), "the path token itself must survive whole"


def test_current_text_mode_wraps_on_a_real_terminal(monkeypatch, capsys, tmp_path, isolated_home):
    """End to end through `_run_current` itself (not just the pure helper):
    forces `sys.stderr.isatty()` True and a fixed terminal width, same
    technique `test_size_command.py`'s own `NO_COLOR` test uses."""
    import shutil

    from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS, _run_current  # noqa: PLC0415

    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda **_: os.terminal_size((100, 24)))

    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(typer.Exit):
        _run_current(json_mode=False, sdk_root=None, workspace_root=workspace)

    err = capsys.readouterr().err
    lines = err.splitlines()
    assert not any(len(line) > 100 for line in lines)
    # The advice sentence survived, reassembled across its wrapped lines.
    assert NO_SDK_NEXT_STEPS.split(",")[0] in " ".join(lines)


def test_current_text_mode_does_not_wrap_off_a_terminal(tmp_path, isolated_home):
    """`isolated_home`'s `run_tan` subprocess pipes stderr, which is never a
    tty -- the 137-column line from the task's own measured baseline must
    come back exactly as before."""
    proc = run_tan("sdk", "current", cwd=tmp_path)
    assert proc.returncode == 0
    long_line = (
        "To get started, get an alp-sdk checkout "
        "(`git clone https://github.com/alplabai/alp-sdk`), then point tan at "
        "it with `--sdk-root <path>`."
    )
    assert long_line in proc.stderr.splitlines()
    assert len(long_line) == 137


# ── tan-cli#497 defect 1: the quickstart child checkout ──────────────────────


def test_current_sees_the_child_checkout_the_acting_commands_resolve(tmp_path, isolated_home):
    """tan-cli#497 defect 1. `resolve_sdk_tiered` alone has NO candidate for a
    CHILD `<ws>/alp-sdk` -- the README Quickstart cwd, straight after
    `git clone https://github.com/alplabai/alp-sdk`.

    Measured there before the fix: `sdkPath: null`, `sourceTier: "none"`, no
    `sdk` key, `issues: []`, and the text telling someone standing beside a
    checkout to go clone one -- while `tan doctor` in the SAME cwd reported that
    checkout at `sourceTier: "discovery"`. `sdk current` is the one command
    whose entire job is "which SDK am I on?", so it must answer what
    `build`/`doctor`/`validate` would act on. Asserted AGAINST
    `resolve_sdk_root_ladder` itself rather than a hardcoded tier, so the two
    cannot drift apart again."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    child = make_sdk_root(workspace / "alp-sdk", version="0.14.0")

    from tan.commands.build_cmd import resolve_sdk_root_ladder

    expected = resolve_sdk_root_ladder(None, workspace)
    assert expected.path is not None and Path(expected.path) == child

    doc = envelope(run_tan("sdk", "current", "--format", "json", cwd=workspace))
    assert doc["data"]["sdkPath"] == str(child)
    assert doc["data"]["sourceTier"] == expected.tier == "discovery"
    assert doc["sdk"] == {"root": str(child), "sourceTier": "discovery"}
    assert doc["data"]["readiness"]["version"] == "0.14.0"
    # The "go clone one" text is exactly what must NOT be printed here.
    text = run_tan("sdk", "current", cwd=workspace)
    assert "git clone" not in text.stderr + text.stdout


def test_current_still_answers_none_when_there_really_is_no_checkout(tmp_path, isolated_home):
    """The other half: the fall-through tail may only ADD an answer where there
    was none. An empty workspace must still report `sourceTier: "none"` with no
    `sdk` key -- the shape the `sdk-current-no-sdk` golden pins."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    doc = envelope(run_tan("sdk", "current", "--format", "json", cwd=workspace))
    assert doc["data"]["sdkPath"] is None
    assert doc["data"]["sourceTier"] == "none"
    assert "sdk" not in doc


# ── tan-cli#497 defect 8: ALL_PROXY must actually steer the request ──────────


def test_all_proxy_is_installed_on_the_opener_urllib_would_have_ignored(monkeypatch):
    """tan-cli#497 defect 8. `urlopen` derives its proxies from
    `getproxies_environment()`, which maps `ALL_PROXY` to the key `all`;
    `ProxyHandler` then installs an `all_open` method that `OpenerDirector._open`
    -- dispatching on `req.type`, i.e. `"https"` -- never calls.

    Measured live against the GitHub Releases API before the fix:
    `HTTPS_PROXY` at a closed port failed as it should (rc 1,
    `[Errno 111] Connection refused`), while `ALL_PROXY` at the SAME closed port
    connected DIRECTLY and returned rc 0 with 13 releases -- the mandated proxy
    silently bypassed. This asserts the handler tan builds itself carries the
    value under a key an `https://` request actually dispatches on."""
    import urllib.request

    for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:45999")

    seen = {}
    real_build_opener = urllib.request.build_opener

    def capture(*handlers):
        for handler in handlers:
            if isinstance(handler, urllib.request.ProxyHandler):
                seen["proxies"] = dict(handler.proxies)
        return real_build_opener(*handlers)

    monkeypatch.setattr("tan.commands.sdk_cmd.urllib.request.build_opener", capture)
    monkeypatch.setattr(
        "tan.commands.sdk_cmd.urllib.request.OpenerDirector.open",
        lambda *a, **k: (_ for _ in ()).throw(OSError("connect: refused")),
    )

    releases, error = _fetch_releases()
    assert releases == []
    assert seen["proxies"] == {"http": "http://127.0.0.1:45999",
                               "https": "http://127.0.0.1:45999"}
    # A proxy WAS used, so the connect-failure hint is earned here.
    assert error is not None and "ALL_PROXY/HTTPS_PROXY/NO_PROXY" in error


def test_no_proxy_sends_the_request_direct_and_stops_blaming_the_proxy(monkeypatch):
    """The second wrong-blame case. `_proxy_configured()` was a bare env scan,
    so a `NO_PROXY` that correctly exempts the target still had the failure of
    the resulting DIRECT connection hinted at a proxy that had no part in it.
    Measured: `HTTPS_PROXY` at a closed port with `NO_PROXY=api.github.com`
    returns rc 0 and the release list, so the proxy really is out of the
    picture."""
    import urllib.request

    for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:45999")
    monkeypatch.setenv("NO_PROXY", "api.github.com")

    seen = {}
    real_build_opener = urllib.request.build_opener

    def capture(*handlers):
        for handler in handlers:
            if isinstance(handler, urllib.request.ProxyHandler):
                seen["proxies"] = dict(handler.proxies)
        return real_build_opener(*handlers)

    monkeypatch.setattr("tan.commands.sdk_cmd.urllib.request.build_opener", capture)
    monkeypatch.setattr(
        "tan.commands.sdk_cmd.urllib.request.OpenerDirector.open",
        lambda *a, **k: (_ for _ in ()).throw(OSError("connect: refused")),
    )

    releases, error = _fetch_releases()
    assert releases == []
    # DIRECT: an EMPTY handler, not urllib's env-derived fallback.
    assert seen["proxies"] == {}
    assert error is not None and "ALL_PROXY/HTTPS_PROXY/NO_PROXY" not in error


def test_a_socks_proxy_is_refused_rather_than_silently_bypassed(monkeypatch):
    """urllib carries no SOCKS transport where the oracle's ureq does (measured:
    the oracle dialled a `socks5://` `ALL_PROXY`). Falling back to a direct
    connection would silently circumvent the only egress a locked-down host
    has -- so this refuses, naming the variable, the scheme and the two ways
    out. No network call is made at all, which is what the un-patched opener
    here proves."""
    for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")

    releases, error = _fetch_releases()
    assert releases == []
    assert error == (
        "Alp SDK: ALL_PROXY names a socks5:// proxy, which this build of tan "
        "cannot route through (its HTTP client has no socks5 transport). Unset "
        "ALL_PROXY, or point it at an http:// or https:// proxy, or add "
        "api.github.com to NO_PROXY to allow a direct connection."
    )


def test_the_socks_refusal_names_the_variable_that_actually_won(monkeypatch):
    """Review round on #620. The first cut said "Set `HTTPS_PROXY` to an http://
    or https:// proxy" -- which CANNOT take effect, because
    `HTTPS_PROXY_ENV_VARS` puts `ALL_PROXY` ahead of `HTTPS_PROXY` and the
    selection never reaches it. Measured:
    `ALL_PROXY=socks5://127.0.0.1:45997 HTTPS_PROXY=http://127.0.0.1:45996
    tan sdk list --online` gave the identical refusal at rc 1 with NEITHER
    socket touched.

    That is the same self-defeating-remediation class as tan-cli#497 defect 7,
    which this branch fixes two commands away -- so the remediation must name
    the variable the selection ACTUALLY read, and offer unsetting it."""
    for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:45997")
    # The variable the reader would otherwise be sent to set, still losing.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:45996")

    _, error = _fetch_releases()
    assert error is not None
    assert "Unset ALL_PROXY, or point it at an http:// or https:// proxy" in error
    # Naming the LOSING variable as the fix is what made the old text unusable.
    assert "Set HTTPS_PROXY" not in error


def test_the_socks_refusal_names_https_proxy_when_that_is_what_won(monkeypatch):
    """The other half: the variable is not hardcoded. With only `https_proxy`
    set, IT is what the selection read, so it is what the remediation must
    name -- telling this reader to unset `ALL_PROXY` would be the same
    unusable instruction pointed the other way."""
    for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("https_proxy", "socks5h://127.0.0.1:45997")

    _, error = _fetch_releases()
    assert error is not None
    assert error.startswith("Alp SDK: https_proxy names a socks5h:// proxy")
    assert "Unset https_proxy, or point it at an http:// or https:// proxy" in error
