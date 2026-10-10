# SPDX-License-Identifier: Apache-2.0
"""tan-cli#887: keep ``contract/sdk-list-data-keys.json`` -- the ``sdk-list``
family the release workflow folds into ``envelope-contract.json`` -- in
lockstep with what ``tan sdk list --online --format json`` actually emits.

``contract/README.md`` listed ``data.releases`` as **NOT COVERED** ("Hits the
GitHub releases API") from the day that table was written. The fields the
extension needs were already on the wire -- measured on the pinned
v0.6.0-rc1, ``parse_remote_sdk_releases`` has carried ``draft`` and
``prerelease`` per entry since ``tan sdk`` was first added -- but nothing
pinned them, so a rename would have been silent on both sides while the
published contract said the field was uncovered. This is the producer-side
half of closing that, exactly as ``test_doctor_contract_key_set.py`` is for
``doctor``.

**What this test does and does not prove, stated rather than implied.** The
release list's VALUES are whatever alp-sdk has published at the moment of the
call, so like ``doctor`` this pins the KEY SET and never a value. It replaces
**only the socket** -- ``urllib.request.OpenerDirector.open`` -- and then runs
the real command: the real ``_fetch_releases`` (headers, proxy selection, JSON
decode), the real ``parse_remote_sdk_releases``, the real ``_list_data``, and
the real ``emit()``. What it therefore does NOT prove is that GitHub's own
payload still carries the fields tan reads out of it; it proves that the
mapping from that payload to tan's wire shape is the declared one. Nothing
short of a network call can prove the former, and a gate that dials
api.github.com is a gate that goes red on an airgapped laptop and on GitHub's
next outage -- which is precisely why the row said NOT COVERED instead of
being covered badly.

Every key set asserted here is read FROM ``contract/sdk-list-data-keys.json``,
never hardcoded: a published file corrupted in either direction (a declared
key deleted, an unemitted key added) has to fail here, and it cannot if this
file carries its own second copy of the answer.

The declared-but-never-emitted direction needs its own run, for the same
reason ``doctor``'s does. A GitHub payload that carries every field would
exercise all seven keys whether or not tan defaults any of them, so
``test_a_payload_missing_every_optional_github_field_still_emits_all_seven``
feeds an entry carrying nothing but ``tag_name`` -- the case
``parse_remote_sdk_releases`` documents (tan-cli#122: absent means "not
flagged", never a reason to drop the release) -- and asserts the key set is
identical and ``draft``/``prerelease`` are real ``False`` booleans rather
than missing keys a consumer would have to treat as unknown.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tan.cli import app

CONTRACT = Path(__file__).resolve().parents[3] / "contract" / "sdk-list-data-keys.json"
DECLARED: dict[str, Any] = json.loads(CONTRACT.read_text(encoding="utf-8"))
DECLARED_DATA_KEYS: dict[str, Any] = DECLARED["dataKeys"]
DECLARED_RELEASE_KEYS: dict[str, str] = DECLARED_DATA_KEYS["releases"]["requiredKeys"]

#: The machine tokens `dataKeys` uses, mapped to what a consumer would
#: structurally validate against. Same vocabulary as
#: `contract/doctor-data-keys.json`, plus `bool` -- which that file has no
#: occasion to use and this one does, twice.
_TOKEN_TYPES: dict[str, type | tuple[type, ...]] = {"string": str, "bool": bool}

runner = CliRunner()

#: One release carrying every field GitHub's own payload has for the seven
#: keys tan maps out of it. `prerelease: True` deliberately -- it is the field
#: tan-cli#887 was filed about, and a fixture where every flag is `False`
#: cannot tell "read from the payload" from "hardcoded to False".
_FULL_PAYLOAD = [
    {
        "tag_name": "v0.16.0-rc.1",
        "published_at": "2026-08-01T00:00:00Z",
        "tarball_url": "https://api.github.com/repos/alplabai/alp-sdk/tarball/v0.16.0-rc.1",
        "body": "First paragraph.\n\nSecond paragraph.",
        "draft": False,
        "prerelease": True,
    },
    {
        "tag_name": "v0.15.0",
        "published_at": "2026-07-01T00:00:00Z",
        "tarball_url": "https://api.github.com/repos/alplabai/alp-sdk/tarball/v0.15.0",
        "body": "Stable.",
        "draft": False,
        "prerelease": False,
    },
]


class _FakeResponse:
    """Just enough of `http.client.HTTPResponse` for `_fetch_releases`'s
    `with opener.open(...) as response: response.read()`."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _run_list(monkeypatch: pytest.MonkeyPatch, payload: list[dict[str, Any]]) -> dict[str, Any]:
    """The declared `args`, with only the socket replaced. Returns the parsed
    envelope."""
    for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    body = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open", lambda *_a, **_k: _FakeResponse(body)
    )
    result = runner.invoke(app, DECLARED["args"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_the_declared_args_are_the_invocation_this_contract_describes():
    """A stale `args` would publish a key set enumerated against a command
    line nobody runs."""
    assert DECLARED["args"] == ["sdk", "list", "--online", "--format", "json"]


def test_emitted_data_keys_equal_the_declared_set_in_both_directions(monkeypatch):
    envelope = _run_list(monkeypatch, _FULL_PAYLOAD)
    data = envelope["data"]
    assert set(data) == set(DECLARED_DATA_KEYS), (
        "`sdk list`'s `data` key set and contract/sdk-list-data-keys.json "
        f"disagree.\n  emitted but undeclared: {sorted(set(data) - set(DECLARED_DATA_KEYS))}\n"
        f"  declared but unemitted: {sorted(set(DECLARED_DATA_KEYS) - set(data))}"
    )
    assert isinstance(data["releases"], list) and data["releases"], (
        "expected a non-empty `releases` array from the stubbed payload; the "
        "key-set assertions below are vacuous without one"
    )
    for entry in data["releases"]:
        assert set(entry) == set(DECLARED_RELEASE_KEYS), (
            "a `releases[]` entry's key set and the declared one disagree.\n"
            f"  emitted but undeclared: {sorted(set(entry) - set(DECLARED_RELEASE_KEYS))}\n"
            f"  declared but unemitted: {sorted(set(DECLARED_RELEASE_KEYS) - set(entry))}"
        )


@pytest.mark.parametrize(("key", "token"), sorted(DECLARED_RELEASE_KEYS.items()))
def test_every_release_field_carries_the_declared_machine_type(monkeypatch, key, token):
    """A key set alone would let `prerelease` become the STRING `"true"` with
    this contract still green -- and a consumer's `!r.prerelease` would then
    be false for every release, silently."""
    envelope = _run_list(monkeypatch, _FULL_PAYLOAD)
    for entry in envelope["data"]["releases"]:
        assert isinstance(entry[key], _TOKEN_TYPES[token]), (
            f"`releases[].{key}` is {type(entry[key]).__name__}, declared {token}"
        )


def test_the_flags_are_read_from_the_payload_not_defaulted(monkeypatch):
    """The fixture's first entry is a prerelease and its second is not. A tan
    that hardcoded either flag would pass every key-set assertion above."""
    releases = _run_list(monkeypatch, _FULL_PAYLOAD)["data"]["releases"]
    assert [(r["tag"], r["draft"], r["prerelease"]) for r in releases] == [
        ("v0.16.0-rc.1", False, True),
        ("v0.15.0", False, False),
    ]


def test_a_payload_missing_every_optional_github_field_still_emits_all_seven(monkeypatch):
    """The declared-but-never-emitted direction. An entry carrying nothing but
    `tag_name` still emits all seven keys, with `""` for the strings and real
    `False` booleans for the flags -- so a consumer never has to tell absent
    from false (tan-cli#122, `parse_remote_sdk_releases`, `_str_field`)."""
    releases = _run_list(monkeypatch, [{"tag_name": "v0.1.0"}])["data"]["releases"]
    assert len(releases) == 1
    entry = releases[0]
    assert set(entry) == set(DECLARED_RELEASE_KEYS)
    assert entry["draft"] is False and entry["prerelease"] is False
    assert entry["publishedAt"] == "" and entry["tarballUrl"] == ""
    assert entry["releaseNotes"] == "" and entry["releaseNotesSummary"] == ""


def test_the_offline_answer_carries_the_same_two_keys(monkeypatch):
    """`releases` is `[]`, never absent, on the path that emits this shape
    without a fetch (tan-cli#351: a bare `sdk list` answers OFFLINE at exit 0).
    A consumer reading `data.releases` with `?? []` must not be the only thing
    standing between it and a missing key."""
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open",
        lambda *_a, **_k: pytest.fail("a bare `sdk list` must not touch the network"),
    )
    result = runner.invoke(app, ["sdk", "list", "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert set(data) == set(DECLARED_DATA_KEYS)
    assert data == {"subcommand": "list", "releases": []}
