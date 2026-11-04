# SPDX-License-Identifier: Apache-2.0
"""`tan platform <use|show|clear|list>` -- pin the workspace to an Alp Platform
release (tan-cli#1571).

A flat `subcommand` + `arg` pair like `tan sdk`, so an unknown verb gets a real
envelope instead of a Click usage error. Pure logic lives in
`tan.core.platform_manifest`; the network in `platform_fetch`.

Never flashes firmware: bridge firmware versions are reported, nothing more.
tan has no SDK checkout-switching mechanism (`tan sdk install`/`switch` refuse,
tan-cli#305), so `use` writes the pin and names the `alp-sdk` version to check
out as a next step; tan's own version mismatch is reported, never self-updated.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from tan.commands.platform_doctor import installed_versions, pin_report
from tan.core.atomic_write import atomic_write_bytes
from tan.core.global_flags import accept_global_flags
from tan.core.platform_manifest import (
    PIN_REL,
    PlatformError,
    PlatformVersion,
    latest_ga_for_line,
    load_manifest_text,
    parse_line,
    parse_version,
    pin_dir_for_sdk,
    pin_path_for_sdk,
    platform_report,
    release_asset_url,
    releases_listing,
)
from tan.core.sdk_discovery import resolve_sdk_root_ladder
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

VERBS = ("use", "show", "clear", "list")

_Fetched = tuple[bytes | None, Issue | None, ExitCode, PlatformVersion | None]


def _path(p: Path) -> str:
    return str(p).replace("\\", "/")


def _done(
    json_mode: bool,
    data: dict[str, Any],
    issues: list[Issue],
    code: ExitCode,
    lines: list[str],
) -> None:
    if json_mode:
        emit(Envelope("platform", Project(root=None, board_yaml=None), data, issues, code))
    else:
        stream = typer.get_text_stream("stderr")
        for line in [*lines, *(f"{i.severity}: {i.message}" for i in issues)]:
            stream.write(f"{line}\n")
    raise typer.Exit(int(code))


def _err(issue: Issue, exit_code: ExitCode) -> _Fetched:
    return None, issue, exit_code, None


def _resolve_version(arg: str) -> tuple[PlatformVersion | None, _Fetched | None]:
    """`arg` (a line or a full version) as the one version to fetch, or the
    refusal envelope parts."""
    from tan.commands.platform_fetch import list_release_tags  # noqa: PLC0415

    try:
        if parse_line(arg) is None:
            return parse_version(arg), None
    except PlatformError as err:
        return None, _err(
            Issue("platform.version-invalid", "error", str(err)), ExitCode.VALIDATION_FAILURE
        )
    tags, err = list_release_tags()
    if tags is None:
        return None, _err(
            Issue("platform.fetch-failed", "error", str(err)), ExitCode.RUNTIME_FAILURE
        )
    version = latest_ga_for_line(arg, tags)
    if version is None:
        message = f"no GA release of platform line {arg} found (rc versions must be named in full)."
        return None, _err(
            Issue("platform.line-unresolved", "error", message), ExitCode.RUNTIME_FAILURE
        )
    return version, None


def _get_manifest_bytes(arg: str | None, manifest: str | None) -> _Fetched:
    """The manifest to pin -- a local file, or the release asset for `arg` --
    plus the version that asset must name (`None` for a local file)."""
    if manifest is not None:
        try:
            return Path(manifest).read_bytes(), None, ExitCode.SUCCESS, None
        except OSError as err:
            issue = Issue("platform.manifest-unreadable", "error", f"cannot read {manifest}: {err}")
            return _err(issue, ExitCode.RUNTIME_FAILURE)
    if arg is None:
        message = "give a platform version (27.2.0) or line (27.2), or --manifest FILE."
        return _err(
            Issue("platform.version-required", "error", message), ExitCode.VALIDATION_FAILURE
        )
    from tan.commands.platform_fetch import fetch_manifest  # noqa: PLC0415

    version, refusal = _resolve_version(arg)
    if version is None:
        return refusal  # type: ignore[return-value]
    raw, err = fetch_manifest(release_asset_url(version))
    if raw is None:
        message = f"fetching platform {version}: {err}"
        return _err(Issue("platform.fetch-failed", "error", message), ExitCode.RUNTIME_FAILURE)
    return raw, None, ExitCode.SUCCESS, version


def _off_manifest(report: dict | None, want_sdk: str, sdk: str) -> tuple[list[Issue], list[str]]:
    issues: list[Issue] = []
    next_steps: list[str] = []
    for row in (report or {}).get("components", []):
        if row["status"] != "mismatch":
            continue
        if row["name"] == "alp-sdk":
            step = (
                f"check out alp-sdk v{want_sdk} (`git -C {sdk} checkout v{want_sdk}`), "
                "then run `tan bootstrap`"
            )
            next_steps.append(step)
            issues.append(Issue(
                "platform.sdk-off-manifest", "warning",
                f"alp-sdk is {row['installed']}, the platform pins {want_sdk}: {step}.",
            ))
        elif row["name"] == "tan-cli":
            issues.append(Issue(
                "platform.tan-off-manifest", "warning",
                f"tan is {row['installed']}, the platform pins {row['pinned']}; "
                "tan never updates itself -- install that release.",
            ))
    return issues, next_steps


def _run_use(
    json_mode: bool, arg: str | None, manifest: str | None, sdk_root: str | None, workspace: Path
) -> None:
    data: dict[str, Any] = {"subcommand": "use", "platform": None, "pinPath": None}
    resolved = resolve_sdk_root_ladder(sdk_root, workspace).path
    if resolved is None:
        from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS  # noqa: PLC0415

        message = f"no alp-sdk checkout resolved: {NO_SDK_NEXT_STEPS}"
        issue = Issue("platform.no-sdk", "error", message)
        return _done(json_mode, data, [issue], ExitCode.RUNTIME_FAILURE, [])
    sdk = str(resolved)
    pin_dir = pin_dir_for_sdk(sdk)
    if pin_dir is None:
        issue = Issue(
            "platform.no-workspace", "error",
            f"no west workspace (.west/) found above {sdk}; run `tan bootstrap` first.",
        )
        return _done(json_mode, data, [issue], ExitCode.RUNTIME_FAILURE, [])

    raw, issue, code, expected = _get_manifest_bytes(arg, manifest)
    if raw is None:
        return _done(json_mode, data, [issue], code, [])
    try:
        parsed = load_manifest_text(raw.decode("utf-8", errors="strict"))
        if expected is not None and parsed.version != expected:
            raise PlatformError(
                f"the manifest names platform {parsed.version}, not the requested {expected}."
            )
    except (PlatformError, UnicodeDecodeError) as err:
        issue = Issue("platform.manifest-invalid", "error", f"{err}")
        return _done(json_mode, data, [issue], ExitCode.VALIDATION_FAILURE, [])

    pin = pin_dir / PIN_REL[1]
    try:
        pin.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(str(pin), raw)
    except OSError as err:
        issue = Issue("platform.write-failed", "error", f"cannot write {pin}: {err}")
        return _done(json_mode, data, [issue], ExitCode.WRITE_FAILURE, [])

    report = platform_report(raw, installed_versions(sdk))[0]
    issues, next_steps = _off_manifest(report, parsed.pinned("alp-sdk"), sdk)
    data.update({"platform": report, "pinPath": _path(pin), "nextSteps": next_steps})
    lines = [
        f"Alp Platform {parsed.version} pinned in {pin}.",
        "No firmware was written; bridge firmware versions are reported only.",
    ]
    _done(json_mode, data, issues, ExitCode.SUCCESS, lines)


def _run_show(json_mode: bool, sdk_root: str | None, workspace: Path) -> None:
    resolved = resolve_sdk_root_ladder(sdk_root, workspace).path
    data: dict[str, Any] = {"subcommand": "show", "platform": None, "pinPath": None}
    if resolved is None:
        lines = ["no platform pinned (no alp-sdk checkout resolved)."]
        return _done(json_mode, data, [], ExitCode.SUCCESS, lines)
    pin = pin_path_for_sdk(resolved)
    data["pinPath"] = _path(pin)
    report, status, detail = pin_report(str(resolved))
    if status == "fail":
        issue = Issue("platform.pin-invalid", "error", detail)
        return _done(json_mode, data, [issue], ExitCode.VALIDATION_FAILURE, [])
    data["platform"] = report
    if report is None:
        return _done(json_mode, data, [], ExitCode.SUCCESS, ["no platform pinned."])
    lines = [detail, *(
        f"  {r['name']}: pinned {r['pinned']}, "
        f"installed {r['installed'] or '(unknown)'} [{r['status']}]"
        for r in report["components"]
    )]
    _done(json_mode, data, [], ExitCode.SUCCESS, lines)


def _run_clear(json_mode: bool, sdk_root: str | None, workspace: Path) -> None:
    resolved = resolve_sdk_root_ladder(sdk_root, workspace).path
    data: dict[str, Any] = {"subcommand": "clear", "cleared": False, "pinPath": None}
    pin = pin_path_for_sdk(resolved) if resolved is not None else None
    if pin is None or not pin.is_file():
        lines = ["no platform pinned; nothing to clear."]
        return _done(json_mode, data, [], ExitCode.SUCCESS, lines)
    data["pinPath"] = _path(pin)
    try:
        pin.unlink()
    except OSError as err:
        issue = Issue("platform.write-failed", "error", f"cannot remove {pin}: {err}")
        return _done(json_mode, data, [issue], ExitCode.WRITE_FAILURE, [])
    data["cleared"] = True
    _done(json_mode, data, [], ExitCode.SUCCESS, [f"removed {pin}."])


def _run_list(json_mode: bool, include_rc: bool, sdk_root: str | None, workspace: Path) -> None:
    from tan.commands.platform_fetch import list_releases  # noqa: PLC0415

    resolved = resolve_sdk_root_ladder(sdk_root, workspace).path
    pinned = (pin_report(str(resolved) if resolved is not None else None)[0] or {}).get("version")
    data: dict[str, Any] = {"releases": [], "pinned": pinned}
    releases, err = list_releases()
    if releases is None:
        issue = Issue("platform.list-failed", "error", f"listing platform releases: {err}")
        return _done(json_mode, data, [issue], ExitCode.RUNTIME_FAILURE, [])
    data["releases"] = releases_listing(releases, include_rc)
    lines = [
        f"{r['version']}{' (rc)' if r['prerelease'] else ''}  {r['published'] or ''}"
        f"{'  <- pinned' if r['version'] == pinned else ''}"
        for r in data["releases"]
    ] or ["no published platform releases."]
    _done(json_mode, data, [], ExitCode.SUCCESS, lines)


def platform(
    subcommand: str = typer.Argument(None, metavar="SUBCOMMAND", help="use, show, clear, or list."),
    arg: str = typer.Argument(
        None, metavar="ARG", help="With use: a platform version (27.2.0) or line (27.2, latest GA)."
    ),
    manifest: str = typer.Option(
        None,
        "--manifest",
        metavar="FILE",
        help="With use: pin this local platform.yaml instead of fetching.",
    ),
    include_rc: bool = typer.Option(
        False, "--include-rc", help="With list: also show -rcN pre-releases."
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    sdk_root: str = typer.Option(None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
) -> None:
    """Pin the workspace to an Alp Platform release (never flashes firmware)."""
    json_mode = output_format == "json"
    workspace = Path.cwd() / project if project else Path.cwd()
    if subcommand == "use":
        _run_use(json_mode, arg, manifest, sdk_root, workspace)
    elif subcommand == "show":
        _run_show(json_mode, sdk_root, workspace)
    elif subcommand == "list":
        _run_list(json_mode, include_rc, sdk_root, workspace)
    elif subcommand == "clear":
        _run_clear(json_mode, sdk_root, workspace)
    else:
        issue = Issue(
            "platform.unknown-subcommand", "error",
            f"unknown subcommand {subcommand!r}; available: {', '.join(VERBS)}.",
        )
        data = {"subcommand": subcommand, "platform": None, "pinPath": None}
        _done(json_mode, data, [issue], ExitCode.RUNTIME_FAILURE, [])


platform = accept_global_flags(platform)
