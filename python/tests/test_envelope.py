# SPDX-License-Identifier: Apache-2.0
import json

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


class _RecursionBomb(dict):
    """A payload that makes `json.dumps` raise RecursionError -- deterministically,
    on every interpreter.

    A dict SUBCLASS on purpose: `_json.c`'s `encoder_listencode_dict` walks an
    exact dict with `PyDict_Next` and only calls `items()` for a subclass, so
    this is the seam that raises from inside the encoder rather than from the
    test. Non-empty because the encoder short-circuits an empty dict to `{}`
    before it ever reaches `items()`.

    It replaces the obvious provocation -- 1000-deep nesting under
    `setrecursionlimit(200)` -- which stopped provoking anything in CPython
    3.12: the C encoder's stack guard there is no longer driven by
    `sys.getrecursionlimit()`, so `json.dumps` simply SUCCEEDS and the old test
    asserted its way to a failure on a claim it had quietly stopped making.
    """

    def items(self):
        raise RecursionError("maximum recursion depth exceeded while encoding a JSON object")


def test_to_json_never_raises_on_recursion_error():
    """RecursionError is neither TypeError nor ValueError, so `to_json`'s catch has
    to be broad enough to hold it too -- otherwise a payload the encoder chokes on
    puts ZERO bytes on stdout instead of one fallback envelope."""
    env = Envelope("test", Project(None, None), _RecursionBomb(a=1), [], ExitCode.SUCCESS)
    parsed = json.loads(env.to_json())
    assert parsed["ok"] is False
    assert parsed["exitCode"] == 5
    assert parsed["issues"][0]["code"] == "envelope.serialize-failed"


def test_sdk_root_is_always_posix_separated():
    """`sdk.root` must never diverge by separator style.

    Rust normalises in `crates/tan-cli/src/sdk_report.rs`
    (`root.replace('\', "/")`) and its doc comment makes it a guarantee. The
    field is part of the extension handshake, so a backslash on Windows and a
    forward slash on POSIX for the same checkout is a contract break.

    No conformance fixture can catch this: `sdk` is absent from every committed
    golden, because none of them resolves an SDK checkout. `build`, `doctor` and
    `sdk` all populate the field, so it is normalised once in `SdkInfo`.
    """
    env = Envelope(
        "test",
        Project(root=None, board_yaml=None),
        1,
        [],
        ExitCode.SUCCESS,
        sdk=SdkInfo(root=r"C:\Users\dev\alp-sdk", source_tier="sdkRootFlag"),
    )
    parsed = json.loads(env.to_json())
    assert parsed["sdk"]["root"] == "C:/Users/dev/alp-sdk"
    assert "\\" not in parsed["sdk"]["root"]
