# SPDX-License-Identifier: Apache-2.0
import json

from tan.envelope import Envelope, Issue, Project, SdkInfo
from tan.exit_codes import ExitCode


def test_project_resolved_nulls_a_board_yaml_that_does_not_exist(tmp_path):
    """tan-cli#236: `Project.resolved` must report `null`, not the joined path,
    when nothing is really at it -- the shared seam every command with a real
    (resolver-derived) `board_yaml` should route through."""
    project = Project.resolved(str(tmp_path), str(tmp_path / "board.yaml"))
    assert project.root == str(tmp_path)
    assert project.board_yaml is None


def test_project_resolved_keeps_a_board_yaml_that_exists(tmp_path):
    board = tmp_path / "board.yaml"
    board.write_text("", encoding="utf-8")
    project = Project.resolved(str(tmp_path), str(board))
    assert project.board_yaml == str(board)


def test_project_resolved_passes_root_through_untouched():
    """tan-cli#236 rules a directory-is-not-a-project question explicitly out
    of scope -- only `board_yaml` is existence-filtered."""
    project = Project.resolved("/does/not/exist", None)
    assert project.root == "/does/not/exist"
    assert project.board_yaml is None


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


def test_non_finite_floats_serialise_as_null_not_infinity_or_nan():
    """tan-cli#387: `json.dumps` defaults to `allow_nan=True` and writes the
    JavaScript literals `Infinity` / `-Infinity` / `NaN`, which RFC 8259 has
    no production for -- `JSON.parse` throws `SyntaxError: Unexpected token
    'I'` on the exact byte stream tan put on stdout, and the same bytes reach
    the persisted `<build-root>/image-bundle/bundle-manifest.json`. They are
    reachable from user data: `.inf` / `-.inf` / `.nan` / an overflowing
    `1e400` in a hand-edited `build/system-manifest.yaml`'s `hw_info:` flows
    verbatim into `image`'s envelope `data`.

    `serde_json` writes `null` for a non-finite `f64`, so the oracle hands the
    same consumer a parseable envelope; this asserts the port now does too,
    AT THE SAME EXIT CODE (0 here). The exit code is half the fix: routing
    through `allow_nan=False` alone would raise into the
    `envelope.serialize-failed` / exit-5 fallback, trading invalid JSON for an
    exit-code divergence.

    `parse_constant` is what makes this a real check: Python's own
    `json.loads` ACCEPTS the three literals by default, so a plain round-trip
    passes on the broken output. Raising there is the failure condition, the
    strict-parser stand-in for `JSON.parse`."""
    payload = {"a": float("inf"), "b": float("-inf"), "c": float("nan"), "d": [float("nan")]}
    env = Envelope("test", Project(None, None), payload, [], ExitCode.SUCCESS)
    text = env.to_json()

    assert "Infinity" not in text and "NaN" not in text, text

    def _strict(literal):
        raise AssertionError(f"non-JSON literal {literal!r} reached the wire: {text}")

    parsed = json.loads(text, parse_constant=_strict)
    assert parsed["data"] == {"a": None, "b": None, "c": None, "d": [None]}
    assert parsed["exitCode"] == 0 and parsed["ok"] is True


def test_a_non_finite_float_does_not_become_a_serialize_failure():
    """The half of tan-cli#387 that a `null`-only assertion would miss: the
    envelope must keep the command's OWN exit code, not the fallback's 5.
    Pinned separately because the fallback is the obvious wrong fix and it
    still produces valid JSON."""
    env = Envelope("test", Project(None, None), {"x": float("nan")}, [], ExitCode.VALIDATION_FAILURE)
    parsed = json.loads(env.to_json())
    assert parsed["exitCode"] == 2
    assert parsed["issues"] == []


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
