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
from tan.envelope import Envelope, Issue, Project, _last_resort_document, emit
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


#: How many bytes reach the wire before the failure. One `BufferedWriter`
#: block, so the truncation is a realistic device/pipe boundary rather than an
#: arbitrary offset.
_ACCEPTED_BYTES = 4096


class FailsAfterOneBlock(io.RawIOBase):
    """A raw stream that accepts one block and then fails.

    The partial-write shape a `BufferedWriter` produces against a real device
    that fills up or a pipe that goes away mid-document: bytes are already on
    the wire when the failure arrives, so nothing can un-write them.

    The first call is a SHORT write (`_ACCEPTED_BYTES` of whatever it was
    handed), not a full one -- measured, that is what it takes to get two raw
    writes out of one `print`. `BufferedWriter` passes a write larger than its
    buffer straight through in a single call, so a raw that accepts the whole
    thing and only fails on a hypothetical SECOND call never fails at all: the
    first draft of this case reported `escaped is None` for exactly that
    reason, which would have made it a case that cannot fail.
    """

    def __init__(self) -> None:
        self.written = bytearray()
        self._calls = 0

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self._calls += 1
        if self._calls > 1:
            raise OSError(28, "No space left on device")
        accepted = min(len(b), _ACCEPTED_BYTES)
        self.written += bytes(b[:accepted])
        return accepted


def test_an_io_failure_mid_document_propagates_instead_of_claiming_a_clean_fallback():
    """The guard is narrowed to the ENCODE class, and this is why.

    An encode failure happens before any byte leaves the stream, so recovering
    from it is safe. An I/O failure does not: a `BufferedWriter` that accepted
    4096 bytes and then failed has left a TRUNCATED document on the wire, and
    appending the last-resort document to that would report a clean
    `envelope.serialize-failed` over stdout no consumer can parse -- a
    regression in HONESTY, since the pre-guard code left the same truncated
    bytes but at least raised and claimed nothing.

    So: the `OSError` must come back out, stdout must still hold only the
    truncated first document, and `emit` must not have marked an envelope as
    emitted.
    """
    raw = FailsAfterOneBlock()
    stream = io.TextIOWrapper(
        io.BufferedWriter(raw, buffer_size=4096), encoding="utf-8", newline="\n"
    )
    # >4096 bytes, so the buffer flushes at least twice while writing ONE
    # document and the second flush is the one that fails.
    envelope = Envelope("build", Project("/w", None), {"log": "x" * 20000}, [], 0)

    real_stdout = sys.stdout
    emitted = envelope_module._emitted
    emitted_code = envelope_module._emitted_exit_code
    envelope_module._emitted = False
    envelope_module._emitted_exit_code = None
    sys.stdout = stream
    try:
        _, escaped = call(lambda: emit(envelope))
        was_emitted = envelope_module._emitted
    finally:
        sys.stdout = real_stdout
        envelope_module._emitted = emitted
        envelope_module._emitted_exit_code = emitted_code

    assert isinstance(escaped, OSError), (
        "an I/O failure must propagate, not be converted into a clean "
        f"envelope.serialize-failed fallback (got {escaped!r})"
    )
    assert len(raw.written) == _ACCEPTED_BYTES, (
        "the premise of this case is that bytes DID reach the wire before the "
        f"failure (got {len(raw.written)})"
    )
    assert b"envelope.serialize-failed" not in bytes(raw.written), (
        "the last-resort document was appended to a truncated first document -- "
        "unparseable stdout, reported as a clean fallback"
    )
    assert was_emitted is False, "nothing was successfully emitted, so the flag must stay False"


def test_a_command_that_is_not_a_string_still_produces_the_last_resort_document():
    """`Envelope.command` is typed `str` and nothing enforces it at runtime.
    It is the ONE field the last-resort document carries from the caller, so a
    non-`str` there used to make the document that exists to survive everything
    raise `TypeError` itself -- and `emit`'s own arm calls this function with
    the same argument, so both `to_json()` and `emit()` went down with it.

    Not reachable through the shipped CLI (every call site passes a literal),
    which is exactly why the docstring's totality claim has to be true rather
    than merely usually true."""
    text, escaped = call(lambda: _last_resort_document(Unserialisable(), ValueError("v")))

    assert escaped is None, f"_last_resort_document let {type(escaped).__name__} escape: {escaped}"
    assert json.loads(text)["command"] == "<Unserialisable>"


def test_the_same_non_string_command_reaches_stdout_through_emit():
    """End to end: `_serialise`'s fallback and `emit`'s arm both hand this
    `command` on, so a raise in the coercion is a raise out of the process."""
    envelope = Envelope(
        Unserialisable(), Project(root=Unserialisable(), board_yaml=None), {}, [], 0
    )

    with ascii_stdout() as stream:
        code, escaped = call(lambda: emit(envelope))
        raw = stream.buffer.getvalue()

    assert escaped is None, f"emit() let {type(escaped).__name__} escape: {escaped}"
    assert raw != b"", "emit() wrote ZERO bytes to stdout -- the #491 shape exactly"
    document = json.loads(raw)
    assert document["command"] == "<Unserialisable>"
    assert document["issues"][0]["code"] == "envelope.serialize-failed"
    assert code == int(ExitCode.INTERNAL_FAILURE)


def test_the_last_resort_document_has_the_same_key_set_as_a_normal_envelope():
    """The last-resort document HAND-BUILDS `project` and `issues[]` instead of
    calling `Project.as_dict()`/`Issue.as_dict()`, and that is deliberate: it
    must not call code that can raise, which is the whole point of it. The cost
    is that a field added to `Project` or `Issue` would silently give this one
    envelope a different shape from every other envelope on the same contract,
    with nothing to notice. This is what notices -- a parity assertion, not a
    refactor back onto the raising helpers.

    `sdk` is absent from both sides here, which pins the other half: the
    last-resort document omits it exactly as an `Envelope` with no `sdk` does,
    rather than emitting `null` (`test_sdk_key_is_absent_when_none_not_null`).
    """
    normal = json.loads(
        Envelope(
            "inspect", Project("/w", "/w/board.yaml"), {"a": 1}, [Issue("x.y", "error", "m")], 0
        ).to_json()
    )
    last_resort = json.loads(_last_resort_document("inspect", ValueError("v")))

    assert set(last_resort) == set(normal)
    assert set(last_resort["project"]) == set(normal["project"])
    assert set(last_resort["issues"][0]) == set(normal["issues"][0])


def test_an_sdk_block_that_raises_while_the_fallback_is_BUILT_still_answers_one_envelope():
    """The fallback arm's guard has to cover the arm's CONSTRUCTION, not just
    its `json.dumps`.

    Building the fallback runs payload code in three places -- `SdkInfo.
    as_dict()` (`self.root.replace(...)`, an `AttributeError` for a non-`str`
    root), the `f"...{err}"` reason line (`err.__str__`), and only then the
    encode. With just the encode wrapped, the first two escaped `_serialise`
    outright. That mattered more once `emit`'s own guard was narrowed to the
    encode class for the truncated-write reason above: `AttributeError` is
    neither `ValueError` nor `TypeError`, so nothing downstream would have
    caught it either.

    `data` is a `set` so the PRIMARY encode fails and the fallback arm is the
    code under test; `sdk` is what then detonates the arm itself.
    """
    envelope = Envelope(
        "doctor",
        Project("/w", None),
        {"bad"},
        [],
        0,
        sdk=envelope_module.SdkInfo(root=Unserialisable(), source_tier="sdkRootFlag"),
    )

    with ascii_stdout() as stream:
        code, escaped = call(lambda: emit(envelope))
        raw = stream.buffer.getvalue()

    assert escaped is None, f"emit() let {type(escaped).__name__} escape: {escaped}"
    assert raw != b"", "emit() wrote ZERO bytes to stdout -- the #491 shape exactly"
    document = json.loads(raw)
    assert document["command"] == "doctor"
    assert document["issues"][0]["code"] == "envelope.serialize-failed"
    assert document["issues"][0]["message"].startswith(
        "failed to write command output: AttributeError: "
    )
    assert code == int(ExitCode.INTERNAL_FAILURE)
