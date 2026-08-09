# SPDX-License-Identifier: Apache-2.0
"""Machine-readable result envelope. JSON mode writes exactly one to stdout."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tan.exit_codes import ExitCode


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


#: The four commands resolving the SDK through `resolve_sdk_root_wide` rather
#: than `resolve_sdk_root_ladder` (tan-cli#407). Only used to label WHICH side
#: of a divergence the reader is holding; getting it wrong would swap two
#: labels in a warning, never change which root a command uses.
WIDE_LADDER_COMMANDS = frozenset({"init", "generate", "examples", "renode"})


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
            return (
                json.dumps(
                    json_safe_floats(self._as_dict()),
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                self.exit_code,
            )
        except Exception as err:  # noqa: BLE001 -- no payload may ever crash stdout
            fallback_code = int(ExitCode.INTERNAL_FAILURE)
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
            return (
                json.dumps(fallback, separators=(",", ":"), ensure_ascii=False),
                fallback_code,
            )


#: Whether this process has already written its one envelope to stdout.
#: Process-global because the thing it guards is process-global: stdout.
_emitted = False

#: The exit code the LAST-emitted envelope's own JSON actually reports --
#: `None` until `emit()` runs once. See `envelope_emitted_exit_code()`.
_emitted_exit_code: int | None = None


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
    """
    global _emitted, _emitted_exit_code
    text, exit_code = envelope._serialise()
    print(text)
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
