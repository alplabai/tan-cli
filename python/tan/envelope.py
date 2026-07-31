# SPDX-License-Identifier: Apache-2.0
"""Machine-readable result envelope. JSON mode writes exactly one to stdout."""
import json
import os
from dataclasses import dataclass
from typing import Any

from tan.exit_codes import ExitCode


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


class Envelope:
    def __init__(self, command, project, data, issues, exit_code, sdk=None):
        self.command = command
        self.project = project
        self.data = data
        self.issues = issues
        self.exit_code = int(exit_code)
        self.sdk = sdk

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
        try:
            return json.dumps(self._as_dict(), separators=(",", ":"))
        except Exception as err:  # noqa: BLE001 -- no payload may ever crash stdout
            fallback = {
                "command": self.command,
                "ok": False,
                "exitCode": int(ExitCode.INTERNAL_FAILURE),
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
            return json.dumps(fallback, separators=(",", ":"))


#: Whether this process has already written its one envelope to stdout.
#: Process-global because the thing it guards is process-global: stdout.
_emitted = False


def emit(envelope: Envelope) -> None:
    """Write THE envelope to stdout, and record that it went out.

    A command signals failure by exiting non-zero, and `tan.cli.main` wraps
    the whole dispatch to add a `cli.parse-error` envelope when a `--format
    json` run exits non-zero with nothing on stdout -- the Click-usage-error
    path, which never reaches a command at all. Without this flag that
    fallback also fires after a command that ALREADY printed its own
    envelope, putting two JSON documents on stdout: valid JSON lines, and a
    consumer that parses stdout whole gets neither.
    """
    global _emitted
    print(envelope.to_json())
    _emitted = True


def envelope_emitted() -> bool:
    return _emitted
