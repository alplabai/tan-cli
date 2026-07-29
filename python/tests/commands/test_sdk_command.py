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

from tan.commands.sdk_cmd import (
    _fetch_releases,
    check_sdk_readiness,
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


def write_pointer(path: Path, sdk_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sdkPath": str(sdk_path), "updatedAt": "1970-01-01T00:00:00Z"}),
        encoding="utf-8",
        newline="",
    )


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
    not win, or one deleted directory breaks every command."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    write_pointer(workspace / ".alp" / "sdk-path", tmp_path / "deleted-checkout")
    active = resolve_sdk_tiered(None, workspace)
    assert (active.tier, active.path) == ("none", None)


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

    monkeypatch.setattr("tan.commands.sdk_cmd.urllib.request.urlopen", boom)
    releases, error = _fetch_releases()
    assert releases == []
    assert error is not None and expect_in_message in error


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
    assert env["sdk"] == {"root": str(root), "sourceTier": "sdkRootFlag"}
    assert env["project"] == {"root": None, "boardYaml": None}


def test_current_with_no_sdk_omits_the_sdk_key(tmp_path, isolated_home):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = envelope(run_tan("sdk", "current", "--format", "json", cwd=workspace))
    assert "sdk" not in env, "absent, never null"
    assert env["data"]["sdkPath"] is None
    assert env["data"]["sourceTier"] == "none"


def test_list_refuses_without_online_and_touches_no_network(tmp_path, isolated_home):
    proc = run_tan("sdk", "list", "--format", "json", cwd=tmp_path)
    assert proc.returncode == 1
    env = envelope(proc)
    assert env["issues"][0]["code"] == "sdk.network-required"
    # The `list`-shaped payload survives the refusal: the extension reads
    # `data.releases` with a `?? []` fallback.
    assert env["data"] == {"subcommand": "list", "releases": []}


@pytest.mark.parametrize("verb", ["install", "switch"])
def test_install_and_switch_refuse_loudly_rather_than_half_working(tmp_path, isolated_home, verb):
    """A partial `switch` writes the active-SDK pointer but skips the
    `.west/config` reconciliation, reporting success while `west` keeps
    resolving the old SDK -- tan-cli #62, re-introduced invisibly."""
    proc = run_tan("sdk", verb, "v0.14.0", "--format", "json", cwd=tmp_path)
    assert proc.returncode == 5
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
