# SPDX-License-Identifier: Apache-2.0
"""Machine-readable result envelope. JSON mode writes exactly one to stdout."""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tan.exit_codes import ExitCode

#: Every code point in the UTF-16 surrogate range. A Python `str` may hold one
#: UNPAIRED -- `os.fsdecode`/`surrogateescape` turns each byte of an
#: un-decodable filesystem name into exactly one of these (`proj\xffx` ->
#: `proj\udcffx`) and carries it verbatim into `project.root`, an argv token, a
#: scanned path in `data`. It is a perfectly ordinary character to `json.dumps`,
#: which writes it straight through under `ensure_ascii=False` -- but it is NOT
#: valid UTF-8, so the failure used to land one call later, at `emit()`'s
#: `print(text)`, as an uncaught `UnicodeEncodeError` AFTER `_serialise()` had
#: already reported success (tan-cli#491). See `_scrub_lone_surrogates`.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

#: What each one becomes: U+FFFD REPLACEMENT CHARACTER, which is precisely what
#: the frozen v0.4.1 oracle puts on the wire for the same directory. Measured,
#: not assumed -- `target/debug/tan inspect --format json` run from a directory
#: created as `os.fsdecode(b"proj\xffx")` answers
#: `"root":"...\/proj\xef\xbf\xbdx"` at exit 0 (`\xef\xbf\xbd` is U+FFFD's UTF-8
#: encoding), because every path Rust puts in the envelope goes through
#: `Path::to_string_lossy`, whose lossy step is this same substitution, one
#: replacement character per un-decodable byte. `surrogateescape` is also one
#: surrogate per un-decodable byte, so the two agree character for character.
_REPLACEMENT_CHARACTER = "\ufffd"


def _scrub_lone_surrogates(text: str) -> str:
    """`text` with every surrogate code point replaced by U+FFFD.

    Applied to the SERIALISED JSON rather than walked over the payload: a
    surrogate can arrive in a `data` value, in a `project.root`, in an issue
    MESSAGE, or in a dict KEY, and one pass over the finished document catches
    all four for the cost of one regex scan instead of a second full recursive
    walk beside `json_safe_floats`. Nothing else in the document can be a
    surrogate -- an astral character is a single non-surrogate code point in a
    Python `str`, and `json.dumps` escapes the only characters it rewrites
    (`"`, `\\`, controls) into ASCII -- so every match came from the payload.

    Deliberately NOT `ensure_ascii=True` as a fallback re-serialisation (the
    first shape tried for tan-cli#491, and rejected against the oracle):
    that escapes the surrogate as `\\udcff`, which is parseable JSON but is not
    what the oracle emits, and it also escapes every OTHER non-ASCII character
    in the same document -- so one bad byte in a path would have turned a
    perfectly good `Sensör Ölçüm` elsewhere in `data` into `Sens\\u00f6r`,
    re-opening the byte-for-byte divergence `ensure_ascii=False` exists to
    close. This substitution touches only the offending characters.
    """
    return _LONE_SURROGATE.sub(_REPLACEMENT_CHARACTER, text)


#: OPEN QUESTION, deliberately left open (tan-cli#491). This substitution is
#: SILENT: nothing in the envelope tells a consumer that a value it is reading
#: was rewritten, so a path that differs from the real one only in the bytes
#: that were replaced reads as authoritative. Announcing it -- a `warning`
#: issue beside the payload -- was considered and NOT done, because the
#: substitution was chosen for byte parity with the frozen v0.4.1 oracle
#: (`Path::to_string_lossy` makes the identical replacement and emits no issue
#: of its own), so adding one changes the wire bytes of a case that currently
#: matches by construction. `--format json` is a machine contract; that is a
#: divergence to decide deliberately, not a side effect of a bug fix. Left for
#: the maintainer with the trade-off recorded rather than settled here.


#: tan-cli#491's invariant is that NO payload, however malformed, may stop the
#: single envelope from being written -- the same claim `_serialise`'s own
#: `# noqa: BLE001 -- no payload may ever crash stdout` has always made. After
#: the surrogate scrub above, TWO sites still broke it, both measured on
#: `dev`@`4dcfdf5`, not theorised:
#:
#:   * `_serialise`'s `except` arm re-serialises `self.project` (and
#:     `self.sdk`) VERBATIM, so a value there that `json.dumps` cannot encode
#:     detonates the very fallback that exists to keep stdout alive:
#:     `Envelope("x", Project(root=<non-str>, board_yaml=None), {"ok": 1}, [],
#:     0).to_json()` raised `TypeError: Object of type ... is not JSON
#:     serializable` straight out of the arm -- zero bytes on stdout through
#:     `emit()`, a raw traceback through `tan.cli`'s two `to_json()` callers.
#:   * `emit()`'s `print(text)` sat outside every guard. That is the exact
#:     placement error #491 names: `_serialise()` returns a `str` and reports
#:     success, and the ENCODE happens one call later, at the write.
#:
#: `_last_resort_document` is what both sites fall back to. It is deliberately
#: `ensure_ascii=True` -- the opposite of `_serialise`, which needs
#: `ensure_ascii=False` for byte parity on the normal path. Here the document's
#: job is to be writable to a stdout that has ALREADY refused one string, so
#: pure ASCII (encodable under utf-8, ascii, latin-1, every Windows code page)
#: is worth more than parity. It is also why no surrogate scrub is needed on
#: this path: `ensure_ascii=True` renders one as the escape `\udcff`, ASCII on
#: the wire.
#:
#: TWO of its fields are not literals, and neither is trusted. `command` is
#: typed `str` but nothing enforces that at runtime, and a non-`str` there made
#: the document itself raise (`TypeError: Object of type ... is not JSON
#: serializable`) -- taking `to_json()` AND `emit()` down with it, since
#: `emit`'s own arm calls this with the same argument. The reason text runs
#: `err.__str__`, which is payload code too. Both go through `str()` inside a
#: guard, so the only way out of the function is a document. `"cli"` is the
#: fallback command name because `tan.cli`'s own envelopes already use it for a
#: run with no resolved subcommand -- a value a consumer already handles.
#:
#: Both guards catch `Exception`, deliberately NOT `BaseException`: a `__str__`
#: that raises `KeyboardInterrupt` escapes intact, and that is correct.
#: Swallowing an interrupt is the defect #491's own Ctrl-C arm exists to fix.
#:
#: `envelope.serialize-failed` and `ExitCode.INTERNAL_FAILURE` (5) reuse the
#: existing fallback's code and exit code rather than introducing new ones: the
#: consumer effect is identical (tan could not represent this run's output),
#: and `contract/issue-codes.json` already registers it.
def _last_resort_document(command: str, err: Exception) -> str:
    """One parseable envelope built from ASCII literals and two COERCED
    strings -- what stdout gets when neither the real envelope nor
    `_serialise`'s own fallback could be produced or written. See the notes
    above for the two sites, the two coercions, and the reused issue code.
    """
    try:
        reason = f"{type(err).__name__}: {err}"
    except Exception:  # noqa: BLE001 -- an exception's own __str__ may raise
        reason = type(err).__name__
    try:
        name = str(command)
    except Exception:  # noqa: BLE001 -- so may an object standing in for a command name
        name = "cli"
    return json.dumps(
        {
            "command": name,
            "ok": False,
            "exitCode": int(ExitCode.INTERNAL_FAILURE),
            # Never the real `project`/`sdk`/`data`: carrying them through is
            # precisely what broke the arm that sent us here. Hand-built rather
            # than `Project.as_dict()`/`Issue.as_dict()` for the same reason --
            # this document may not call code that can raise. The cost is that
            # a field added to either type would silently give this envelope a
            # different shape from every other one on the contract, which
            # `test_the_last_resort_document_has_the_same_key_set_as_a_normal_envelope`
            # is what notices.
            "project": {"root": None, "boardYaml": None},
            "data": None,
            "issues": [
                {
                    "code": "envelope.serialize-failed",
                    "severity": "error",
                    "message": f"failed to write command output: {reason}",
                }
            ],
        },
        separators=(",", ":"),
        ensure_ascii=True,
    )


def json_safe_floats(value: Any) -> Any:
    """`value` with every NON-FINITE float replaced by `None`, recursively --
    `serde_json`'s own answer for an `f64` RFC 8259 cannot express (tan-cli#387).

    Python's `json.dumps` defaults to `allow_nan=True` and writes the
    non-standard literals `Infinity`, `-Infinity` and `NaN`. Those are not JSON.
    `JSON.parse` throws on them, and the alp-sdk-vscode extension's only channel
    is this envelope -- so a `build/system-manifest.yaml` carrying `.inf` in
    `hw_info` (verbatim into `data` via `raw_passthrough`) used to hand the
    consumer a parse throw at exit code 0 with `issues:[]`: no coded signal to
    fall back on, and nothing to distinguish it from tan crashing. The oracle on
    the identical input emits `null` and the consumer gets a field it can
    inspect.

    NOT solved with `allow_nan=False` at the `json.dumps` below. That raises,
    which lands in `_serialise`'s `envelope.serialize-failed` / exit-5 fallback
    where the oracle exits 0 -- one divergence traded for another. This projects
    instead, so the exit code and `issues` stay exactly what the command decided.

    Applied at SERIALISE time, mirroring where `serde_json` makes the same
    substitution, so it holds for every command's `data` rather than for the one
    payload the defect was found in.

    Map KEYS are deliberately left alone: `json.dumps` renders a non-finite
    float key as the *string* `"Infinity"`, which is already valid JSON, and
    rewriting it would change a key a consumer may be matching on.

    A new object always -- the caller's payload is never mutated, so a command
    that inspects its own `data` after `emit()` still sees its own floats.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: json_safe_floats(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        # A tuple becomes a list: `json.dumps` writes both as a JSON array, so
        # the wire bytes are unchanged.
        return [json_safe_floats(v) for v in value]
    return value


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass(frozen=True)
class Project:
    root: str | None
    board_yaml: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"root": self.root, "boardYaml": self.board_yaml}

    @staticmethod
    def resolved(root: str | None, board_yaml: str | None) -> "Project":
        """The `project` block for a RESOLVED project -- the one seam every
        command that reports a real (as opposed to hardcoded-None) `board_yaml`
        should build it through (tan-cli#236, #170; mirrors the Rust CLI's
        `Project::from_context` in `crates/tan-cli/src/envelope.rs`).

        `board_yaml` is reported only when a file is really there. The doc has
        always read "if found"; before this fix, every call site cloned a
        resolver's `<root>/board.yaml` straight through regardless of whether
        anything was there, so a consumer that opened it got ENOENT. `null` is
        not a new value here -- it is what the field already carries wherever
        resolution finds nothing.

        `root` passes through untouched -- tan-cli#236 rules a
        directory-is-not-a-project question explicitly out of scope.

        Deliberately NOT pushed into the resolvers that compute `board_yaml`
        (`resolve_project_paths`, `resolve_project_context`, ...): several
        commands need that unfiltered path precisely when the file is absent --
        a "no board.yaml at `<path>`" refusal message, in particular -- so
        nulling it at the resolver would strip the path out of the very message
        that names it. Reporting is the seam; the resolver is not.
        """
        exists = board_yaml is not None and os.path.exists(board_yaml)
        return Project(root=root, board_yaml=board_yaml if exists else None)


@dataclass(frozen=True)
class SdkInfo:
    root: str
    source_tier: str
    #: tan-cli#478. CARRIED from the resolution, never re-derived at emit time,
    #: and deliberately NOT part of `as_dict` -- the wire shape stays
    #: `{root, sourceTier}`, which is the extension handshake.
    #:
    #: Carried rather than recomputed because `~/.alp/sdk-default` is MUTABLE
    #: mid-run: `bootstrap` rewrites it with its own `writtenFor` before its
    #: envelope goes out, and `init` writes the project pin after resolving.
    #: `test_bootstrap_command.py` pins the resolution-time semantic ("`init`
    #: surfaces `sdk.global-default-foreign-project` BEFORE `_pin_sdk`
    #: writes"), so an emit-time re-read would report the POST-mutation state
    #: and the warning would vanish on exactly the two commands that change
    #: it. That is the difference from `_with_sdk_divergence` below, which may
    #: re-derive safely: #407 is a stateless property of the filesystem,
    #: #464 is a property of the resolution THIS run acted on.
    foreign_global_default_for: str | None = None
    #: The unreadable/unresolvable `.alp/sdk-path` pin, same carry-through --
    #: `sdk_resolution_issues` emits the two together, in this order.
    broken_project_pin: str | None = None

    @classmethod
    def from_resolution(cls, root: str, resolution: Any) -> SdkInfo:
        """The ONE blessed constructor. `resolution` is any object carrying
        `tier`/`foreign_global_default_for`/`broken_project_pin` -- every
        ladder already answers with one (`SdkRootResolution`, `ActiveSdk`).

        A raw `SdkInfo(root, tier)` silently drops both facts, which is how 16
        of ~32 commands ended up disclosing a foreign global default and the
        rest staying quiet; `tests/gates/test_sdk_info_is_built_from_a_resolution.py`
        refuses new raw constructions outside the seams that predate this.
        """
        return cls(
            root,
            resolution.tier,
            getattr(resolution, "foreign_global_default_for", None),
            getattr(resolution, "broken_project_pin", None),
        )

    def as_dict(self) -> dict[str, str]:
        # Forward slashes ALWAYS, mirroring `crates/tan-cli/src/sdk_report.rs`'s
        # `root.replace('\\', "/")` and the guarantee its own doc comment makes:
        # "`sdk.root` never diverges by separator style depending on which
        # resolver happened to record it." This is part of the extension
        # handshake, so it must be platform-identical.
        #
        # Normalised HERE, at the one shared seam, rather than in each command:
        # `build`, `doctor` and `sdk` all populate this field and all three had
        # the same defect. No conformance fixture can catch it -- `sdk` is absent
        # from every committed golden, because none of them resolves a checkout.
        # `data.sdkPath` stays raw/native on both sides; only this key is posix.
        return {"root": self.root.replace("\\", "/"), "sourceTier": self.source_tier}


@dataclass
class SdkDisclosure:
    """What a command already knew about its SDK resolution at the moment it
    blew up, so its OUTER `<command>.internal-failure` catch-all can report it
    (tan-cli#497 defect 2, the site the first pass missed).

    Every command here splits into a `_run`-style inner function that resolves
    the SDK and an outer wrapper that guards the whole thing with a bare
    `except Exception`. The facts -- which checkout answered, and the
    `sdk.project-pin-unresolved` / `sdk.global-default-foreign-project` pair
    that says the pin was ignored -- are computed INSIDE the inner function, so
    the handler in the outer one had no name to read them from and reported the
    crash alone. `model`/`run` avoided that by resolving in the outer function;
    the three that could not (their resolution needs paths the inner function
    resolves) pass one of these down and `record()` into it the instant the
    pair is known.

    MUTABLE and shared by reference on purpose: the handler must see what the
    inner call recorded before it raised. Reading an unrecorded disclosure is
    the honest "nothing was resolved yet" -- `None` and `[]`, exactly what a
    crash before the ladder ran should report.
    """

    sdk: SdkInfo | None = None
    issues: list[Issue] = field(default_factory=list)

    def record(self, sdk: SdkInfo | None, issues: list[Issue]) -> None:
        """Copies `issues` rather than aliasing it: the caller goes on to
        `append` command-specific issues onto its own list, and those are not
        resolution facts -- a crash after them must not report them twice."""
        self.sdk = sdk
        self.issues = list(issues)


#: The three commands resolving the SDK through `resolve_sdk_root_wide` rather
#: than `resolve_sdk_root_ladder` (tan-cli#407). Only used to label WHICH side
#: of a divergence the reader is holding; getting it wrong would swap two
#: labels in a warning, never change which root a command uses.
WIDE_LADDER_COMMANDS = frozenset({"init", "generate", "examples"})


class Envelope:
    def __init__(self, command, project, data, issues, exit_code, sdk=None):
        self.command = command
        self.project = project
        self.data = data
        issues = self._with_sdk_divergence(command, project, issues, sdk)
        self.issues = self._with_sdk_resolution_advisories(issues, sdk)
        self.exit_code = int(exit_code)
        self.sdk = sdk

    @staticmethod
    def _with_sdk_resolution_advisories(issues, sdk):
        """Append tan-cli#464's pair -- `sdk.project-pin-unresolved` and
        `sdk.global-default-foreign-project` -- from what the resolution
        already recorded on `sdk`.

        Same argument as `_with_sdk_divergence` below, applied to a second
        fact: doing it at the one seam every envelope passes through, instead
        of at each command, is what stops the 33rd command from forgetting.
        Before this, 16 of ~32 commands hand-called `sdk_resolution_issues`
        and the rest silently resolved another project's checkout -- `validate`
        spawning ITS schema validator for this project's board.yaml, `trace`
        printing ITS orchestrator path -- at `ok: true, issues: []`.

        DEDUPED BY CODE, and that is load-bearing rather than defensive: the
        hand-call sites still exist, and several fold these two into a
        command-specific ORDER this must not disturb. A command that already
        emitted the pair keeps its own copy and position; one that never did
        gets it here.

        Appends to a NEW list -- several commands keep rendering their own
        text from the list they passed in.
        """
        if sdk is None:
            return issues
        from tan.commands.sdk_cmd import sdk_resolution_issues

        advisories = sdk_resolution_issues(
            sdk.broken_project_pin, sdk.source_tier, sdk.foreign_global_default_for
        )
        if not advisories:
            return issues
        seen = {issue.code for issue in issues}
        extra = [issue for issue in advisories if issue.code not in seen]
        return [*issues, *extra] if extra else issues

    @staticmethod
    def _with_sdk_divergence(command, project, issues, sdk):
        """Append the tan-cli#407 warning when the two SDK ladders would answer
        DIFFERENT checkouts from this project root.

        Done at the ONE seam every command's envelope passes through, not at
        each of the 20 `SdkInfo(...)` construction sites: #407's whole
        complaint is that the two ladders report the same `sourceTier`
        ("discovery") for two roots, and a fix present on `build` but missing
        on the other sixteen commands would leave exactly the silence the
        issue is about -- the vscode extension branches on `sourceTier` from
        `generate`/`examples` (wide) AND `build`/`doctor`/`sdk current`
        (narrow), so partial coverage still gives it two roots it cannot tell
        apart.

        Gated on `sourceTier == "discovery"` before anything touches the disk,
        which makes this free in every normal layout: any HIGHER tier
        (`--sdk-root`, the project pin, the global default) is shared verbatim
        by both ladders and cannot be the pair that differs, so there is
        nothing to compare. That gate is also why passing `sdk_root_arg=None`
        below is exact rather than approximate -- a `--sdk-root` run reports
        `sdkRootFlag`, never `discovery`, so it never reaches here.

        Appends to a NEW list. Mutating the caller's would be a side effect on
        an argument, and several commands keep rendering their own text from
        the list they passed in.

        Import is local: `build_cmd` imports this module at module level, so a
        top-level import here would be circular.
        """
        if sdk is None or getattr(sdk, "source_tier", None) != "discovery":
            return issues
        # `project.root` is None for the commands that are not project-scoped
        # -- `examples` and `sdk current` both report `{"root": null}` -- and
        # those are exactly two of the commands #407 measured as divergent, so
        # bailing on a null root would have left the reported collision
        # unreported on the wide side. They resolved the SDK from the cwd, so
        # that is the workspace root to ask about.
        root = getattr(project, "root", None)
        try:
            # RESOLVED, never the raw `project.root`. Several commands report
            # it as the relative `"."` (`validate` does), and `Path(".").parent`
            # is `"."` -- so the ladders' lateral `../alp-sdk` candidate
            # collapses onto the child `./alp-sdk` and a real divergence reads
            # as agreement. Measured: from a cwd where `doctor` warned,
            # `validate` did not, purely because of that one character.
            start = Path(root).resolve() if root else Path.cwd()

            from tan.commands.build_cmd import sdk_ladder_divergence_issue

            divergence = sdk_ladder_divergence_issue(
                None, start, wide=command in WIDE_LADDER_COMMANDS
            )
        except Exception:  # noqa: BLE001
            # A warning about ambiguity must never be the reason a command
            # cannot report its actual result. Whatever the run was doing is
            # more important than this advisory.
            return issues
        if divergence is None:
            return issues
        if any(getattr(i, "code", None) == divergence.code for i in issues):
            return issues
        return [*issues, divergence]

    def _as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "command": self.command,
            "ok": self.exit_code == 0,
            "exitCode": self.exit_code,
            "project": self.project.as_dict(),
        }
        # Absent, not null -- see test_sdk_key_is_absent_when_none_not_null.
        if self.sdk is not None:
            out["sdk"] = self.sdk.as_dict()
        out["data"] = self.data
        out["issues"] = [i.as_dict() for i in self.issues]
        return out

    def to_json(self) -> str:
        return self._serialise()[0]

    def _serialise(self) -> tuple[str, int]:
        """The JSON text plus the exit code THAT TEXT actually reports.

        The two differ only when serialisation itself fails: the fallback
        below substitutes `ExitCode.INTERNAL_FAILURE` (5) for whatever
        `self.exit_code` was, so a caller that wants the wire invariant
        `process exit code == envelope.exitCode` to hold (tan-cli#327) must
        read the code from HERE, not from `self.exit_code` -- `emit()` does
        exactly that. `to_json()` stays a plain `str` return -- every existing
        caller (`test_envelope.py`'s `json.loads(env.to_json())` assertions)
        depends on that shape.
        """
        try:
            # `ensure_ascii=False`: the default (True) escapes every non-ASCII
            # codepoint as `\uXXXX`, which is valid JSON but not what the
            # oracle emits -- `serde_json::to_string` writes raw UTF-8 bytes
            # verbatim (measured: `scaffold --name "Sensör Ölçüm"` on the
            # Rust CLI puts the literal `Sensör Ölçüm` on the wire, not
            # `Sensör...`). A consumer that byte-compares tan's envelope
            # against the oracle's, or that greps stdout for a raw non-ASCII
            # string, saw a divergence stdout never had a reason to carry.
            #
            # `json_safe_floats`: `Infinity`/`-Infinity`/`NaN` are Python's
            # non-standard extension literals, not JSON (tan-cli#387). See that
            # function for why this is a projection and not `allow_nan=False`.
            #
            # `_scrub_lone_surrogates`: `ensure_ascii=False` writes a lone
            # surrogate straight through into the `str` this returns, and
            # nothing downstream could recover from that -- the encode fails at
            # `emit()`'s `print(text)`, one call AFTER this function has already
            # reported success, so the `except` below (whose own comment says
            # "no payload may ever crash stdout") never sees it and the process
            # dies with ZERO bytes on stdout (tan-cli#491). Scrubbed HERE, on
            # the finished document, so `to_json()` -- and therefore
            # `tan.cli._usage_error_envelope`'s own `print` -- is covered by the
            # same call.
            return (
                _scrub_lone_surrogates(
                    json.dumps(
                        json_safe_floats(self._as_dict()),
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                ),
                self.exit_code,
            )
        except Exception as err:  # noqa: BLE001 -- no payload may ever crash stdout
            fallback_code = int(ExitCode.INTERNAL_FAILURE)
            # The WHOLE fallback, construction included, sits inside the guard
            # below -- not just its `json.dumps`. Building it runs payload code
            # in three places, and every one of them can raise: `SdkInfo.
            # as_dict()` calls `self.root.replace(...)` (an `AttributeError` for
            # a non-`str` root), the f-string below calls `err.__str__`, and
            # `json.dumps` then re-encodes `project`/`sdk` verbatim. With only
            # the `dumps` wrapped, the first two escaped `_serialise` entirely
            # -- past `emit()`, past `to_json()`, out to a traceback and zero
            # bytes on stdout, which is the exact shape tan-cli#491 is about.
            try:
                fallback = {
                    "command": self.command,
                    "ok": False,
                    "exitCode": fallback_code,
                    "project": self.project.as_dict(),
                }
                if self.sdk is not None:
                    fallback["sdk"] = self.sdk.as_dict()
                fallback["data"] = None
                fallback["issues"] = [
                    Issue(
                        "envelope.serialize-failed",
                        "error",
                        f"failed to serialize command output: {err}",
                    ).as_dict()
                ]
                # Scrubbed on this arm too: `self.project.as_dict()` is carried
                # into the fallback verbatim, and `err` itself stringifies
                # whatever the failed payload held -- either can carry the same
                # lone surrogate, so the fallback that exists to keep stdout
                # alive must not be the thing that kills it.
                return (
                    _scrub_lone_surrogates(
                        json.dumps(fallback, separators=(",", ":"), ensure_ascii=False)
                    ),
                    fallback_code,
                )
            except Exception as fallback_err:  # noqa: BLE001 -- nor may THIS arm crash stdout
                # Keeping this here (rather than only at `emit`) is what makes
                # `to_json()` total as well -- and `to_json()` is what
                # `tan.cli`'s `_usage_error_envelope`/`_interrupted_envelope`
                # print. See `_last_resort_document`.
                return _last_resort_document(self.command, fallback_err), fallback_code


#: Whether this process has already written its one envelope to stdout.
#: Process-global because the thing it guards is process-global: stdout.
_emitted = False

#: The exit code the LAST-emitted envelope's own JSON actually reports --
#: `None` until `emit()` runs once. See `envelope_emitted_exit_code()`.
_emitted_exit_code: int | None = None

#: Why `emit()`'s guard below catches `(ValueError, TypeError)` and not
#: `Exception` (tan-cli#491). Those are the ENCODE class: `UnicodeEncodeError`
#: is a `UnicodeError` is a `ValueError`, and a codec handed something it
#: cannot accept raises `TypeError`. They fire BEFORE any byte leaves the
#: stream, because `TextIOWrapper.write` encodes the whole string before
#: writing any of it -- measured on an ascii-encoded stream, the buffer is
#: empty after the raise and the ASCII document then lands as the only thing on
#: it. Recovering there is safe: stdout is still empty.
#:
#: An `OSError` is deliberately NOT caught. Measured on a
#: `TextIOWrapper`->`BufferedWriter(4096)`->raw stream that accepts one block
#: and then fails, given a 20 KB envelope: 4096 bytes of a TRUNCATED first
#: document are already on the wire. Appending the last-resort document to that
#: would report a clean `envelope.serialize-failed` over stdout no consumer can
#: parse -- worse than the raise, which at least claims nothing. So an I/O
#: failure propagates exactly as it did before this guard existed, and
#: `_emitted` stays `False`.


def emit(envelope: Envelope) -> int:
    """Write THE envelope to stdout, record that it went out, and return the
    exit code that JSON just reported.

    A command signals failure by exiting non-zero, and `tan.cli.main` wraps
    the whole dispatch to add a `cli.parse-error` envelope when a `--format
    json` run exits non-zero with nothing on stdout -- the Click-usage-error
    path, which never reaches a command at all. Without this flag that
    fallback also fires after a command that ALREADY printed its own
    envelope, putting two JSON documents on stdout: valid JSON lines, and a
    consumer that parses stdout whole gets neither.

    The returned code (also stashed in `_emitted_exit_code`, read back by
    `envelope_emitted_exit_code()`) is `envelope.exit_code` UNLESS
    serialisation itself fell back to `envelope.serialize-failed` -- in which
    case it is the fallback's `ExitCode.INTERNAL_FAILURE` (5), not the
    command's original code. tan-cli#327: a caller that already committed to
    its own `typer.Exit(<original code>)` before calling this needs a way to
    learn the fallback happened; `tan.cli.main`'s process boundary is that
    caller, so the wire invariant `process exit code == envelope.exitCode`
    holds even when serialisation fails.

    Same for the WRITE failing (tan-cli#491): the last-resort document below
    reports 5 too, and this returns 5, so a command that had already committed
    to `typer.Exit(0)` still exits 5 to match the JSON stdout actually carries.
    """
    global _emitted, _emitted_exit_code
    try:
        text, exit_code = envelope._serialise()
        print(text)
    except (ValueError, TypeError) as err:  # the ENCODE class only -- see above
        exit_code = int(ExitCode.INTERNAL_FAILURE)
        print(_last_resort_document(envelope.command, err))
    _emitted = True
    _emitted_exit_code = exit_code
    return exit_code


def envelope_emitted() -> bool:
    return _emitted


def envelope_emitted_exit_code() -> int | None:
    """The exit code the one emitted envelope's JSON reports, or `None` if
    nothing has been emitted yet this process. `tan.cli.main` reads this at
    the process boundary to catch a serialize-failure fallback that changed
    the reported code out from under a command's own `typer.Exit` (tan-cli#327)
    -- carried directly from `emit()`, never re-derived by parsing stdout back."""
    return _emitted_exit_code
