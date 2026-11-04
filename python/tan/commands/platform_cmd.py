# SPDX-License-Identifier: Apache-2.0
"""`tan platform <use|show|clear>` -- pin the workspace to an Alp Platform
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

from tan.commands.platform_doctor import installed_versions
from tan.core.atomic_write import atomic_write_bytes
from tan.core.global_flags import accept_global_flags
from tan.core.platform_manifest import (
    PIN_REL,
    PlatformError,
    latest_ga_for_line,
    load_manifest_text,
    parse_line,
    parse_version,
    pin_dir_for_sdk,
    pin_path_for_sdk,
    platform_report,
    release_asset_url,
    same_version,
)
from tan.core.sdk_discovery import resolve_sdk_root_ladder
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

VERBS = ("use", "show", "clear")


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


def _get_manifest_bytes(arg: str | None, manifest: str | None) -> tuple[bytes | None, Issue | None, ExitCode]:
    """The manifest to pin: a local file, or the release asset for `arg`."""
    if manifest is not None:
        try:
            return Path(manifest).read_bytes(), None, ExitCode.SUCCESS
        except OSError as err:
            return None, Issue("platform.manifest-unreadable", "error", f"cannot read {manifest}: {err}"), ExitCode.RUNTIME_FAILURE
    if arg is None:
        return None, Issue("platform.version-required", "error", "give a platform version (27.2.0) or line (27.2), or --manifest FILE."), ExitCode.VALIDATION_FAILURE
    from tan.commands.platform_fetch import fetch_manifest, list_release_tags  # noqa: PLC0415

    try:
        if parse_line(arg) is not None:
            tags, err = list_release_tags()
            if tags is None:
                return None, Issue("platform.fetch-failed", "error", str(err)), ExitCode.RUNTIME_FAILURE
            version = latest_ga_for_line(arg, tags)
            if version is None:
                return None, Issue("platform.line-unresolved", "error", f"no GA release of platform line {arg} found (rc versions must be named in full)."), ExitCode.RUNTIME_FAILURE
        else:
            version = parse_version(arg)
    except PlatformError as err:
        return None, Issue("platform.version-invalid", "error", str(err)), ExitCode.VALIDATION_FAILURE
    raw, err = fetch_manifest(release_asset_url(version))
    if raw is None:
        return None, Issue("platform.fetch-failed", "error", f"fetching platform {version}: {err}"), ExitCode.RUNTIME_FAILURE
    return raw, None, ExitCode.SUCCESS


def _run_use(json_mode: bool, arg: str | None, manifest: str | None, sdk_root: str | None, workspace: Path) -> None:
    data: dict[str, Any] = {"subcommand": "use", "platform": None, "pinPath": None}
    resolved = resolve_sdk_root_ladder(sdk_root, workspace).path
    if resolved is None:
        from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS  # noqa: PLC0415

        issue = Issue("platform.no-sdk", "error", f"no alp-sdk checkout resolved: {NO_SDK_NEXT_STEPS}")
        return _done(json_mode, data, [issue], ExitCode.RUNTIME_FAILURE, [])
    sdk = str(resolved)
    pin_dir = pin_dir_for_sdk(sdk)
    if pin_dir is None:
        issue = Issue("platform.no-workspace", "error", f"no west workspace (.west/) found above {sdk}; run `tan bootstrap` first.")
        return _done(json_mode, data, [issue], ExitCode.RUNTIME_FAILURE, [])

    raw, issue, code = _get_manifest_bytes(arg, manifest)
    if raw is None:
        return _done(json_mode, data, [issue], code, [])
    try:
        parsed = load_manifest_text(raw.decode("utf-8", errors="strict"))
        if arg is not None and parse_line(arg) is None and manifest is None and str(parsed.version) != arg:
            raise PlatformError(f"the manifest names platform {parsed.version}, not the requested {arg}.")
    except (PlatformError, UnicodeDecodeError) as err:
        return _done(json_mode, data, [Issue("platform.manifest-invalid", "error", f"{err}")], ExitCode.VALIDATION_FAILURE, [])

    pin = pin_dir / PIN_REL[1]
    try:
        pin.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(str(pin), raw)
    except OSError as err:
        return _done(json_mode, data, [Issue("platform.write-failed", "error", f"cannot write {pin}: {err}")], ExitCode.WRITE_FAILURE, [])

    report, _status, _detail = platform_report(raw, installed_versions(sdk))
    want_sdk = parsed.pinned("alp-sdk")
    issues: list[Issue] = []
    next_steps: list[str] = []
    for row in (report or {}).get("components", []):
        if row["status"] == "mismatch" and row["name"] == "alp-sdk":
            step = f"check out alp-sdk v{want_sdk} (`git -C {sdk} checkout v{want_sdk}`), then run `tan bootstrap`"
            next_steps.append(step)
            issues.append(Issue("platform.sdk-off-manifest", "warning", f"alp-sdk is {row['installed']}, the platform pins {want_sdk}: {step}."))
        elif row["status"] == "mismatch" and row["name"] == "tan-cli":
            issues.append(Issue("platform.tan-off-manifest", "warning", f"tan is {row['installed']}, the platform pins {row['pinned']}; tan never updates itself -- install that release."))
    data.update({"platform": report, "pinPath": str(pin).replace("\\", "/"), "nextSteps": next_steps})
    lines = [f"Alp Platform {parsed.version} pinned in {pin}.", "No firmware was written; bridge firmware versions are reported only."]
    _done(json_mode, data, issues, ExitCode.SUCCESS, lines)


def _run_show(json_mode: bool, sdk_root: str | None, workspace: Path) -> None:
    resolved = resolve_sdk_root_ladder(sdk_root, workspace).path
    data: dict[str, Any] = {"subcommand": "show", "platform": None, "pinPath": None}
    if resolved is None:
        return _done(json_mode, data, [], ExitCode.SUCCESS, ["no platform pinned (no alp-sdk checkout resolved)."])
    pin = pin_path_for_sdk(resolved)
    data["pinPath"] = str(pin).replace("\\", "/")
    if not pin.is_file():
        return _done(json_mode, data, [], ExitCode.SUCCESS, ["no platform pinned."])
    report, status, detail = platform_report(pin.read_bytes(), installed_versions(str(resolved)))
    data["platform"] = report
    if status == "fail":
        return _done(json_mode, data, [Issue("platform.pin-invalid", "error", detail)], ExitCode.VALIDATION_FAILURE, [])
    lines = [detail, *(f"  {r['name']}: pinned {r['pinned']}, installed {r['installed'] or '(unknown)'} [{r['status']}]" for r in report["components"])]
    _done(json_mode, data, [], ExitCode.SUCCESS, lines)


def _run_clear(json_mode: bool, sdk_root: str | None, workspace: Path) -> None:
    resolved = resolve_sdk_root_ladder(sdk_root, workspace).path
    data: dict[str, Any] = {"subcommand": "clear", "cleared": False, "pinPath": None}
    pin = pin_path_for_sdk(resolved) if resolved is not None else None
    if pin is None or not pin.is_file():
        return _done(json_mode, data, [], ExitCode.SUCCESS, ["no platform pinned; nothing to clear."])
    data["pinPath"] = str(pin).replace("\\", "/")
    try:
        pin.unlink()
    except OSError as err:
        return _done(json_mode, data, [Issue("platform.write-failed", "error", f"cannot remove {pin}: {err}")], ExitCode.WRITE_FAILURE, [])
    data["cleared"] = True
    _done(json_mode, data, [], ExitCode.SUCCESS, [f"removed {pin}."])


def platform(
    subcommand: str = typer.Argument(None, metavar="SUBCOMMAND", help="use, show, or clear."),
    arg: str = typer.Argument(None, metavar="ARG", help="With use: a platform version (27.2.0) or line (27.2, latest GA)."),
    manifest: str = typer.Option(None, "--manifest", metavar="FILE", help="With use: pin this local platform.yaml instead of fetching."),
    project: str = typer.Option(None, "--project", metavar="PATH", help="Project root (defaults to '.')."),
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
    elif subcommand == "clear":
        _run_clear(json_mode, sdk_root, workspace)
    else:
        issue = Issue("platform.unknown-subcommand", "error", f"unknown subcommand {subcommand!r}; available: {', '.join(VERBS)}.")
        _done(json_mode, {"subcommand": subcommand, "platform": None, "pinPath": None}, [issue], ExitCode.RUNTIME_FAILURE, [])


platform = accept_global_flags(platform)
