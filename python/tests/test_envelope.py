# SPDX-License-Identifier: Apache-2.0
import json
import sys

from tan.envelope import Envelope, Issue, Project, SdkInfo
from tan.exit_codes import ExitCode


def test_sdk_key_is_absent_when_none_not_null():
    """Absent, never null -- this is what keeps the contract goldens byte-identical."""
    env = Envelope("test", Project(root="/p", board_yaml=None), 1, [], ExitCode.SUCCESS)
    parsed = json.loads(env.to_json())
    assert "sdk" not in parsed, f"sdk must be absent, not null: {parsed}"


def test_sdk_key_serialises_camel_case_member_set():
    env = Envelope(
        "test", Project(root="/p", board_yaml=None), 1, [], ExitCode.SUCCESS,
        sdk=SdkInfo(root="/resolved/sdk", source_tier="discovery"),
    )
    parsed = json.loads(env.to_json())
    assert parsed["sdk"] == {"root": "/resolved/sdk", "sourceTier": "discovery"}


def test_ok_is_derived_from_exit_code_and_keys_are_camel_case():
    env = Envelope(
        "test", Project(root=None, board_yaml="/p/board.yaml"), 42,
        [Issue("x.y", "error", "m")], ExitCode.VALIDATION_FAILURE,
    )
    parsed = json.loads(env.to_json())
    assert parsed["ok"] is False
    assert parsed["exitCode"] == 2
    assert parsed["project"] == {"root": None, "boardYaml": "/p/board.yaml"}
    assert parsed["data"] == 42
    assert parsed["issues"] == [{"code": "x.y", "severity": "error", "message": "m"}]


def test_to_json_never_raises_on_unserialisable_payload():
    """Rust contract: a payload that cannot serialise must still emit ONE parseable
    envelope with ok:false and an envelope.serialize-failed issue -- never a crash
    with zero bytes on stdout."""
    env = Envelope("test", Project(None, None), {(1, 2): 3}, [], ExitCode.SUCCESS)
    parsed = json.loads(env.to_json())
    assert parsed["ok"] is False
    assert parsed["exitCode"] == 5
    assert parsed["issues"][0]["code"] == "envelope.serialize-failed"


def test_to_json_never_raises_on_recursion_error():
    """json.dumps raises RecursionError (not TypeError/ValueError) on a deeply nested
    payload -- to_json() must still catch it and emit the same fallback envelope."""
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        nested: list = []
        cur = nested
        for _ in range(1000):
            cur.append([])
            cur = cur[0]
        env = Envelope("test", Project(None, None), nested, [], ExitCode.SUCCESS)
        parsed = json.loads(env.to_json())
    finally:
        sys.setrecursionlimit(old_limit)
    assert parsed["ok"] is False
    assert parsed["exitCode"] == 5
    assert parsed["issues"][0]["code"] == "envelope.serialize-failed"
