# SPDX-License-Identifier: Apache-2.0
"""tan-cli#491: the envelope invariant, not the one character class that
exposed it.

`#491` defect 1 is stated as an invariant, and it is the invariant the
`# noqa: BLE001 -- no payload may ever crash stdout` comment in `_serialise`
has always claimed: *no payload, however malformed, may prevent the single
envelope from being written*. The lone-surrogate case that filed the issue is
closed (`test_envelope_surrogate.py`) -- a surrogate is scrubbed to U+FFFD on
the finished document -- but the invariant itself was still false at two sites
after that fix, both measured on `dev`@`4dcfdf5`:

  1. `_serialise`'s fallback arm re-serialises `self.project` (and `self.sdk`)
     VERBATIM. A value there that `json.dumps` cannot encode therefore breaks
     the fallback that exists to keep stdout alive, and `_serialise` RAISES:

         >>> Envelope("x", Project(root=Unserialisable(), board_yaml=None),
         ...          {"ok": 1}, [], 0).to_json()
         TypeError: Object of type Unserialisable is not JSON serializable

     which is zero bytes on stdout through `emit()`, and a raw traceback
     through `tan.cli._usage_error_envelope`/`_interrupted_envelope`, which
     both call `to_json()`.

  2. `emit()`'s `print(text)` sat outside every guard -- the placement error
     `#491` names in so many words. `_serialise()` returns a `str`; the ENCODE
     is `print`'s, so a stdout whose codec cannot represent a character in that
     `str` raises AFTER `_serialise` has reported success. Stated at the
     strength it was measured: this is defence at the boundary, NOT a live CLI
     reproduction. `tan.cli._reconfigure_stdio` pins the real process's stdout
     to utf-8 and the surrogate scrub removes the only character class utf-8
     itself refuses, so no argv found in this lane reaches it through the
     shipped CLI -- the guard is here because the invariant is the contract,
     and both of those are single points of failure protecting it.

Both sites are closed by ONE ASCII-only document
(`envelope._last_resort_document`). These cases pin it.
"""
import contextlib
import io
import json
import sys

import tan.envelope as envelope_module
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode


def call(fn):
    """`(result, escaped_exception)` for `fn()` -- never re-raises.

    Every case below asserts a CONTRACT ("nothing escapes, and stdout carries
    one envelope"), and a test that simply calls `emit()` would, with either
    guard removed, die on the incidental `UnicodeEncodeError`/`TypeError`
    itself. That is a red bar for the wrong reason: it proves the payload is
    unserialisable, not that this file noticed. Routing the call through here
    turns "it raised" into a value the assertions below can name, so removing a
    guard fails on `assert escaped is None`, in this file, with the escaping
    type in the message.
    """
    try:
        return fn(), None
    except Exception as err:  # noqa: BLE001 -- "nothing escapes" is the assertion
        return None, err


class Unserialisable:
    """A perfectly ordinary object that `json.dumps` has no encoder for.

    Deliberately NOT a `set`/tuple-key payload: those break the FIRST
    `json.dumps` only, which the pre-existing `envelope.serialize-failed`
    fallback already handles (`test_envelope.py`). To reach the fallback's own
    unguarded arm the offending value has to sit in `project`/`sdk`, the two
    members that arm copies through verbatim.
    """

    def __repr__(self) -> str:  # pragma: no cover -- readability of a failure only
        return "<Unserialisable>"


@contextlib.contextmanager
def ascii_stdout():
    """`sys.stdout` as a strict-ASCII text stream over an in-memory buffer, for
    the duration of the `with` block.

    The realistic shape of a stdout that refuses a string tan already
    serialised: a Windows console/redirect left on a legacy code page, or a
    `PYTHONIOENCODING` a caller pinned. `write_through=True` so the assertion
    reads bytes without depending on flush ordering, and `newline="\\n"` so
    `print`'s terminator is not translated -- both mirror what
    `tan.cli._reconfigure_stdio` asks of the real streams.

    A CONTEXT MANAGER used inside the test body, deliberately not a fixture
    that installs the stream during setup. pytest's capture plugin re-asserts
    its own `sys.stdout` when it resumes global capture for the `call` phase,
    which silently undoes a setup-time `monkeypatch.setattr(sys, "stdout", ...)`
    -- measured while writing this file: the first draft's `print` went to
    pytest's captured stdout instead, the wrapper was then garbage-collected,
    and the assertion died with `ValueError: I/O operation on closed file`
    rather than on anything about the envelope. Installed here, in the call
    phase, nothing re-asserts over it.

    `emit()` is process-global by design (one envelope per run); both halves of
    that state are saved and restored so a case neither inherits nor leaks it.
    """
    stream = io.TextIOWrapper(
        io.BytesIO(), encoding="ascii", errors="strict", newline="\n", write_through=True
    )
    real_stdout = sys.stdout
    emitted = envelope_module._emitted
    emitted_code = envelope_module._emitted_exit_code
    envelope_module._emitted = False
    envelope_module._emitted_exit_code = None
    sys.stdout = stream
    try:
        yield stream
    finally:
        sys.stdout = real_stdout
        envelope_module._emitted = emitted
        envelope_module._emitted_exit_code = emitted_code


def test_a_write_stdout_cannot_encode_still_leaves_one_parseable_envelope():
    """Site 2. The payload serialises perfectly -- `Sensör` is a valid `str`
    and valid JSON -- and the failure is purely the write. Pre-fix this raised
    `UnicodeEncodeError` out of `emit()` with the buffer left empty; the
    contract says stdout carries exactly one envelope, so it must carry one
    here too."""
    envelope = Envelope("inspect", Project("/w", None), {"note": "Sensör"}, [], 0)

    with ascii_stdout() as stream:
        code, escaped = call(lambda: emit(envelope))
        raw = stream.buffer.getvalue()

    assert escaped is None, f"emit() let {type(escaped).__name__} escape: {escaped}"
    assert raw != b"", "emit() wrote ZERO bytes to stdout -- the #491 shape exactly"
    # Pure ASCII, and exactly ONE document -- not the failed one plus this one.
    raw.decode("ascii")
    assert raw.count(b"\n") == 1, raw
    document = json.loads(raw)
    assert document["command"] == "inspect"
    assert document["ok"] is False
    assert document["exitCode"] == int(ExitCode.INTERNAL_FAILURE)
    assert document["project"] == {"root": None, "boardYaml": None}
    assert document["data"] is None
    assert document["issues"] == [
        {
            "code": "envelope.serialize-failed",
            "severity": "error",
            # Verbatim, so the REASON survives into the envelope rather than
            # being flattened to "something went wrong". `position 105` is
            # where `ö` sits in `_serialise`'s own output for this envelope;
            # a change to the envelope's field order or key names moves it,
            # and that is a wire change worth re-reading this line for.
            "message": (
                "failed to write command output: UnicodeEncodeError: 'ascii' codec "
                "can't encode character '\\xf6' in position 105: ordinal not in range(128)"
            ),
        }
    ]
    # The wire invariant (tan-cli#327): what `emit` reports back is what the
    # JSON on stdout says, so `tan.cli.main` exits 5 rather than the command's
    # own 0.
    assert code == int(ExitCode.INTERNAL_FAILURE)


def test_a_project_the_fallback_cannot_serialise_still_answers_one_envelope():
    """Site 1, through `to_json()` -- the entry point `tan.cli`'s two envelope
    printers use, and the one a raise here reaches as a traceback."""
    envelope = Envelope(
        "clean",
        Project(root=Unserialisable(), board_yaml=None),
        {"ok": 1},
        [Issue("clean.nothing-to-do", "warning", "m")],
        0,
    )

    text, escaped = call(envelope.to_json)

    assert escaped is None, f"to_json() let {type(escaped).__name__} escape: {escaped}"
    text.encode("ascii")
    assert json.loads(text) == {
        "command": "clean",
        "ok": False,
        "exitCode": int(ExitCode.INTERNAL_FAILURE),
        "project": {"root": None, "boardYaml": None},
        "data": None,
        "issues": [
            {
                "code": "envelope.serialize-failed",
                "severity": "error",
                "message": (
                    "failed to write command output: TypeError: Object of type "
                    "Unserialisable is not JSON serializable"
                ),
            }
        ],
    }


def test_the_same_project_reaches_stdout_through_emit():
    """The two sites compose: `emit()` on the site-1 envelope must still put a
    document on stdout AND report 5, not merely avoid raising."""
    envelope = Envelope("clean", Project(root=Unserialisable(), board_yaml=None), {}, [], 0)

    with ascii_stdout() as stream:
        code, escaped = call(lambda: emit(envelope))
        raw = stream.buffer.getvalue()

    assert escaped is None, f"emit() let {type(escaped).__name__} escape: {escaped}"
    assert raw != b"", "emit() wrote ZERO bytes to stdout -- the #491 shape exactly"
    document = json.loads(raw)
    assert document["issues"][0]["code"] == "envelope.serialize-failed"
    assert document["exitCode"] == int(ExitCode.INTERNAL_FAILURE)
    assert code == int(ExitCode.INTERNAL_FAILURE)


def test_a_surrogate_in_the_command_name_stays_ascii_on_the_last_resort_path():
    """The last-resort document is the one that must survive a stdout which has
    ALREADY refused a string, so its own escaping is not free to be
    `ensure_ascii=False` like `_serialise`'s. A surrogate in the only payload
    field it carries verbatim -- `command` -- has to come out as the ASCII
    escape `\\udcff`, not as a raw code point that would fail the same write a
    second time."""
    envelope = Envelope(
        "we\udcffird", Project(root=Unserialisable(), board_yaml=None), {}, [], 0
    )

    with ascii_stdout() as stream:
        _, escaped = call(lambda: emit(envelope))
        raw = stream.buffer.getvalue()

    assert escaped is None, f"emit() let {type(escaped).__name__} escape: {escaped}"
    assert b'"command":"we\\udcffird"' in raw, raw
    raw.decode("ascii")
    assert json.loads(raw)["command"] == "we\udcffird"
