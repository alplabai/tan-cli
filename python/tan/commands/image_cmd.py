# SPDX-License-Identifier: Apache-2.0
"""`tan image` -- assemble a flashable-image bundle from
`build/system-manifest.yaml`.

Port of `crates/tan-cli/src/commands/image.rs`: the IO half. tar+gzip each built
slice's `build_dir` into `image-bundle/slices/<core>-<os>.tar.gz`, copy each
helper-MCU firmware into `image-bundle/helper-mcus/`, and write
`image-bundle/bundle-manifest.json`. Every non-IO decision -- bundle shape, key
order, forward-slash artefact paths, the ok-slice gate, the path guard -- is pure
in `tan.core.image_bundle`.

**Nothing here shells the SDK, and nothing here needs one.** The main input is a
file this project's own build wrote (I-32 / anti-pattern #22); the one exception
is a helper's `firmware_path` (below), which reads a real file straight out of
an already-resolved SDK checkout when the build tree does not have it. No `sdk`
key is emitted unless the shared project resolver happened to resolve a
checkout, which is exactly what the oracle reports.

Deliberate divergences from the retired `west alp-image`, carried over from the
Rust and re-verified against the binary:
  - the helper entry carries `chip`, not the perpetually-`null` `role`;
  - artefact paths are forward-slash, always (the Python `\\` on Windows was a
    latent cross-platform bug in the MACHINE contract);
  - an IO failure is WriteFailure(3), not a traceback at rc 1;
  - a helper's DECLARED, concrete `firmware_path` that does not resolve to a file
    is a HARD error, not a silent skip: a consumer keying on `ok`/exit code must
    not see a "complete" bundle missing a promised artefact. The `TBD` pending
    sentinel and an absent `firmware_path` stay non-fatal skips.

A relative `firmware_path` resolves against **build_root, then sdk_root**
(`tan.core.image_bundle.helper_firmware_candidates`) -- alp-sdk#330: the SDK's
`som-preset-v1.schema.json` defines it repository-relative, i.e. relative to the
SDK checkout, not to this project's `build/`, so build-root-only resolution
(the pre-#330 behaviour) rejected every helper an SDK actually ships. build_root
still goes first, matching the precedence `flash_plan.resolve_artefact_path`
already established for the same two-roots ambiguity (#301, #322): a helper
artefact THIS build produced under `build/` wins over a same-named file the SDK
happens to ship. When neither root has the file, the `image.helper-missing`
error names every root tried and the absolute path each one produced, not just
the raw manifest string -- the old message left a reporter to go find the file
by hand to prove it existed.

The one divergence from the ORACLE is the archive bytes: Python's `tarfile` +
`gzip` cannot produce the Rust `tar`+`flate2` stream byte-for-byte (member order,
header fields, gzip metadata), so `slices[].sha256`/`size` differ. The contract
those fields have to satisfy is self-consistency -- each hash matches the bytes
this run actually wrote at that artefact path -- and
`tests/commands/test_image_command.py` asserts exactly that. Helper firmwares are
plain copies and DO hash identically to the oracle.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
from dataclasses import dataclass
from typing import Any

import typer

from tan.commands.build_output import (
    ManifestInvalid,
    ManifestUnavailable,
    ProjectContext,
    load_manifest,
    resolve_app_base,
    resolve_build_root,
    resolve_project_context,
)
from tan.commands.sdk_cmd import sdk_resolution_issues
from tan.core.global_flags import accept_global_flags
from tan.core.image_bundle import (
    BUNDLE_DIR,
    BUNDLE_MANIFEST,
    HELPERS_DIR,
    PENDING_SENTINEL,
    SLICES_DIR,
    assemble_bundle_manifest,
    helper_artefact_rel,
    helper_entry,
    helper_firmware_candidates,
    slice_archive_name,
    slice_artefact_rel,
    slice_entry,
    slice_should_bundle,
)
from tan.core.system_manifest import (
    SystemManifest,
    raw_passthrough,
    slice_build_dir,
)
from tan.envelope import Envelope, Issue, Project, SdkDisclosure, SdkInfo, emit, json_safe_floats
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

#: Streaming read size for the SHA-256, matching the oracle's 65536-byte buffer.
_HASH_CHUNK = 65536


class BundleWriteError(Exception):
    """A bundle-assembly IO failure -- `image.bundle-write-failed` at exit 3."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class _Notice:
    """A slice or helper item left OUT of the bundle. Carries its own issue code
    so `--format json` reports it as an `Issue`, not only as a text line: before
    that, an incomplete bundle was indistinguishable from a complete one in the
    mode the extension always uses.

    `severity` is `warning` for a legitimate, already-accounted-for gap (no
    build_dir yet, an unsafe core_id/os, a `TBD` helper). The one `error` case is
    a helper's declared, concrete `firmware_path` that is not a file -- and that
    flips the exit/`ok`, even though assembly itself completed.
    """

    code: str
    severity: str
    message: str


@dataclass
class _Outcome:
    exit_code: ExitCode
    data: dict[str, Any]
    project: Project
    issues: list[Issue]
    text: list[str]
    sdk: SdkInfo | None = None


def _sha256_and_size(path: str) -> tuple[str, int]:
    """Streaming lowercase-hex SHA-256 + byte length. Raises
    [`BundleWriteError`] rather than an OSError, so no read failure can escape
    the error contract."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        size = os.stat(path).st_size
    except OSError as err:
        raise BundleWriteError(f"hash {path}: {err}") from err
    return digest.hexdigest(), size


def _basename(path: str) -> str:
    """`Path::file_name()`: the last component, with any trailing separator
    ignored (`os.path.basename` returns `""` there, Rust's `file_name` does not)
    and `/` honoured as a separator on Windows too."""
    return os.path.basename(path.replace("\\", "/").rstrip("/"))


def _tar_gzip_dir(src_dir: str, dest: str) -> None:
    """Recursively tar+gzip `src_dir` into `dest`, tar root arcname =
    basename(src_dir). Any existing archive is unlinked first, matching the
    oracle (and so a half-written archive from a previous run can never be
    appended to)."""
    try:
        if os.path.lexists(dest):
            os.remove(dest)
    except OSError as err:
        raise BundleWriteError(f"unlink {dest}: {err}") from err
    arcname = _basename(src_dir) or "build"
    try:
        with tarfile.open(dest, "w:gz") as archive:
            archive.add(src_dir, arcname=arcname)
    except (OSError, tarfile.TarError) as err:
        raise BundleWriteError(f"tar {src_dir}: {err}") from err


def _bundle_slice(
    slice_: dict, build_root: str, slices_dir: str
) -> dict[str, Any] | _Notice:
    """Tar+gzip one ok slice, or a notice saying why it was left out."""
    core_id = str(slice_.get("core_id"))
    os_name = str(slice_.get("os"))
    build_dir = slice_build_dir(slice_, build_root)
    if build_dir is None or not _is_dir(build_dir):
        return _Notice(
            "image.slice-skipped",
            "warning",
            f"image: skipping {core_id} (build_dir missing)",
        )
    # The shared seam that rejects a `core_id`/`os` shaped like `../../x` or
    # `C:/x`: without it, joining onto `slices_dir` escaped the bundle dir and the
    # unlink above could remove an arbitrary `*.tar.gz` outside the build tree.
    archive_name = slice_archive_name(core_id, os_name)
    if archive_name is None:
        return _Notice(
            "image.slice-unsafe-name",
            "warning",
            f"image: skipping {core_id} (core_id/os is not a safe archive name)",
        )
    archive = os.path.join(slices_dir, archive_name)
    _tar_gzip_dir(build_dir, archive)
    sha256, size = _sha256_and_size(archive)
    artefact = slice_artefact_rel(core_id, os_name)
    assert artefact is not None  # already validated by slice_archive_name above
    return slice_entry(core_id, os_name, artefact, sha256, size)


def _unresolved_sdk_clause(sdk_root_arg: str | None) -> str:
    """The trailing "why was there no sdk_root" clause of an
    `image.helper-missing` message.

    tan-cli#497 defect 6: the message hardcoded "no --sdk-root and no
    discoverable checkout" whenever nothing resolved -- including when
    `--sdk-root` WAS supplied and failed the loader-marker check, because
    `resolve_project_context` returns `None` for a supplied-but-invalid flag
    exactly as it does for an absent one. So `tan image --sdk-root
    /definitely/not/a/checkout` told the reporter to pass the flag they had
    just typed, on a hard-error path (exit 1) that refuses to produce the
    bundle -- sending them to look for a missing flag instead of at the typo
    in the path they gave.

    Entirely inside the `(tried ...)` clause that `tests/parity/
    test_image_size_oracle.py` stripped from BOTH modes as a declared
    alp-sdk#330 divergence (the frozen oracle has no `sdk_root` fallback and
    so no such clause at all). That module went with the oracle axis in
    tan-cli#269, so no parity CASE diffs it any more -- but BOTH branches of
    the string below are asserted verbatim by
    `tests/commands/test_image_command.py`'s
    `test_a_rejected_sdk_root_flag_is_named_not_reported_as_absent` and
    `test_an_absent_sdk_root_flag_still_says_so`, so a reword lands there too.
    """
    if sdk_root_arg is None:
        return "sdk root not resolved (no --sdk-root and no discoverable checkout)"
    return (
        f'sdk root not resolved (--sdk-root "{sdk_root_arg}" is not an alp-sdk '
        f"checkout)"
    )


def _bundle_helper(
    helper: dict,
    build_root: str,
    sdk_root: str | None,
    helpers_dir: str,
    used_names: set[str],
    sdk_root_arg: str | None = None,
) -> dict[str, Any] | _Notice | None:
    """Copy one helper's firmware, or a notice, or `None` for an absent one.

    `sdk_root_arg` is the RAW `--sdk-root` the caller typed, carried only so
    the unresolved-sdk clause can tell "you passed nothing" apart from "what
    you passed is not a checkout" (tan-cli#497 defect 6); `sdk_root` remains
    the RESOLVED root, and is the only one any path operation uses.

    `used_names` tracks every destination basename already claimed this run:
    Zephyr's default layout puts EVERY helper's firmware at the same basename
    (`zephyr.bin`), flattened from different `firmware_path`s. Without it, the
    second helper's copy silently overwrote the first's file and the first's
    already-recorded `sha256` pointed at bytes that were no longer there.
    """
    raw = helper.get("firmware_path")
    if not isinstance(raw, str) or raw == "":
        return None
    if raw.strip() == PENDING_SENTINEL:
        # The documented not-yet-built placeholder: a legitimate state, so a
        # warning, never the error a genuinely missing concrete path gets.
        return _Notice(
            "image.helper-skipped",
            "warning",
            f"image: helper-mcu firmware not found at {raw}; skipping",
        )
    # Two roots, tried in order -- see `helper_firmware_candidates` for why
    # build_root goes first and sdk_root is the fallback that resolves a
    # genuinely SDK-shipped, repository-relative firmware_path (alp-sdk#330).
    candidates = helper_firmware_candidates(raw, build_root, sdk_root)
    firmware = next((path for _, path in candidates if _is_file(path)), None)
    if firmware is None:
        tried = "; ".join(f"{label} {path}" for label, path in candidates)
        if sdk_root is None and not os.path.isabs(raw):
            tried += f"; {_unresolved_sdk_clause(sdk_root_arg)}"
        return _Notice(
            "image.helper-missing",
            "error",
            f"image: helper-mcu firmware not found at {raw} (tried {tried}); "
            "refusing to produce an incomplete bundle",
        )
    basename = _basename(firmware) or "firmware.bin"
    dest_name = basename
    counter = 1
    while dest_name in used_names:
        counter += 1
        dest_name = f"{counter}-{basename}"
    used_names.add(dest_name)

    destination = os.path.join(helpers_dir, dest_name)
    try:
        _copy_file(firmware, destination)
    except OSError as err:
        raise BundleWriteError(f"copy {firmware} -> {destination}: {err}") from err
    sha256, size = _sha256_and_size(destination)
    name = helper.get("name")
    chip = helper.get("chip")
    return helper_entry(
        name if isinstance(name, str) else None,
        chip if isinstance(chip, str) else None,
        helper_artefact_rel(dest_name),
        sha256,
        size,
    )


def _copy_file(src: str, dst: str) -> None:
    """Byte copy, streamed. `shutil.copy` is avoided deliberately: it also copies
    permission bits, and on a read-only source firmware that leaves the bundle
    copy unwritable, so the NEXT `tan image` run fails to overwrite it."""
    with open(src, "rb") as reader, open(dst, "wb") as writer:
        while True:
            chunk = reader.read(_HASH_CHUNK)
            if not chunk:
                break
            writer.write(chunk)


def _is_dir(path: str) -> bool:
    try:
        return os.path.isdir(path)
    except (OSError, ValueError):
        return False


def _is_file(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except (OSError, ValueError):
        # A path the OS refuses to stat at all (a too-long name, an embedded NUL
        # from a hand-edited manifest) is "not a file", never an exception that
        # escapes the envelope.
        return False


def _assemble_bundle(
    build_root: str,
    sdk_root: str | None,
    manifest: SystemManifest,
    yaml_text: str,
    sdk_root_arg: str | None = None,
) -> tuple[list[_Notice], dict[str, Any], str]:
    """Do the filesystem work: mkdir the bundle tree, tar each ok slice, copy each
    present helper firmware, write `bundle-manifest.json`."""
    bundle_dir = os.path.join(build_root, BUNDLE_DIR)
    slices_dir = os.path.join(bundle_dir, SLICES_DIR)
    helpers_dir = os.path.join(bundle_dir, HELPERS_DIR)
    for directory in (bundle_dir, slices_dir, helpers_dir):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as err:
            raise BundleWriteError(f"mkdir {directory}: {err}") from err

    notices: list[_Notice] = []
    slice_entries: list[dict[str, Any]] = []
    for slice_ in manifest.slices:
        status = slice_.get("status")
        if not slice_should_bundle(status):
            # tan-cli#499 defect 1: a bare `continue` here -- the ONE exclusion
            # in this function that reported nothing, while `build_dir`-missing,
            # unsafe-name and the `TBD` helper all emit a notice. Reachable
            # from a GREEN `tan build`: a slice skipped under executionPolicy
            # (no `west`/`bitbake`) is written as `status: skipped` and
            # `build_cmd` keeps a PARTIAL build at ok:true/exit 0, so `tan
            # image` answered `ok:true`, `exitCode:0`, `issues:[]` and wrote a
            # `bundle-manifest.json` whose `boot_order` still named a core
            # `slices[]` carried no artefact for -- a release/OTA consumer
            # keying on `ok`/`issues[]` ships a bundle missing a core's
            # firmware.
            #
            # A DELIBERATE oracle divergence, measured before diverging:
            #
            #   $ target/debug/tan image --format json --build-root build  # tan 0.4.1
            #   ...,"slices":[{"core_id":"m55_hp",...}],
            #      "boot_order":[{"core":"m55_hp"},{"core":"a55_0"}]},"issues":[]}
            #
            # i.e. the frozen oracle drops it silently too (`image.rs`'s bare
            # `continue`). Warning severity, not an exit-code flip: this is
            # the same "already-accounted-for gap" class `_Notice` documents,
            # and a `failed` status has already exited non-zero at `tan build`.
            #
            # Only a DECLARED status reports -- the same narrowing `size`'s
            # twin guard applies, for the same reason: a manifest that omits
            # `status` says nothing about whether the slice built, and this
            # module's reader is "deliberately tolerant, not a validator". It
            # also confines the divergence to the shape the defect actually
            # has (`tan build` always writes a `status`) instead of every
            # hand-written manifest in the parity corpus.
            if isinstance(status, str):
                notices.append(
                    _Notice(
                        "image.slice-skipped",
                        "warning",
                        f"image: skipping {slice_.get('core_id')} (status: {status})",
                    )
                )
            continue
        result = _bundle_slice(slice_, build_root, slices_dir)
        if isinstance(result, _Notice):
            notices.append(result)
        else:
            slice_entries.append(result)

    helper_entries: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for helper in manifest.helper_mcus:
        result = _bundle_helper(
            helper, build_root, sdk_root, helpers_dir, used_names, sdk_root_arg
        )
        if result is None:
            continue
        if isinstance(result, _Notice):
            notices.append(result)
        else:
            helper_entries.append(result)

    hw_info, boot_order = raw_passthrough(yaml_text)
    bundle = assemble_bundle_manifest(hw_info, boot_order, slice_entries, helper_entries)

    try:
        # `indent=2` + serde_json's `to_string_pretty` separators, and exactly one
        # trailing newline written with `newline=""` so a Windows host cannot
        # silently rewrite the file to CRLF (I-27).
        #
        # `json_safe_floats` for the same reason the envelope applies it, and it
        # matters MORE here (tan-cli#387): this is a persisted artefact that
        # downstream flashing/OTA tooling parses long after the run that wrote
        # it. A `.inf` in `hw_info` used to leave `Infinity` on disk inside a
        # bundle tan had just reported as successfully assembled, so every
        # strict consumer of that file failed with nothing in the envelope
        # saying why. It cannot be shared with the envelope's own call -- this
        # document is written before `data` is built, and the two go through
        # different `json.dumps` calls.
        document = json.dumps(json_safe_floats(bundle), indent=2) + "\n"
    except (TypeError, ValueError) as err:
        # `hw_info` is verbatim YAML: a mapping key JSON cannot express reaches
        # here. The oracle hits the same wall in `Envelope::to_json` and reports
        # it as an issue rather than aborting; this reports it as a write failure,
        # which is where the failure actually is.
        raise BundleWriteError(f"serialize {BUNDLE_MANIFEST}: {err}") from err
    manifest_out = os.path.join(bundle_dir, BUNDLE_MANIFEST)
    try:
        with open(manifest_out, "w", encoding="utf-8", newline="") as handle:
            handle.write(document)
    except OSError as err:
        raise BundleWriteError(f"write {manifest_out}: {err}") from err

    return notices, bundle, bundle_dir


def _empty_bundle() -> dict[str, Any]:
    """An empty bundle manifest, so the envelope's `data` shape stays stable on
    every failure path."""
    return assemble_bundle_manifest({}, [], [], [])


def _sdk_warning_lines(issues: list[Issue]) -> list[str]:
    """The text-mode rendering of the `sdk_resolution_issues` pair
    (tan-cli#497 defect 5). `{severity}: {message}` -- the shape `tan build`
    and `tan run` already print a resolution warning with, so the same
    workspace reads the same way whichever command a developer runs.

    Nothing pins the previous silence: `test_text_mode_writes_nothing_to_
    stdout_and_always_names_the_bundle` asserts only containment, and the
    oracle text-parity cases redirect HOME and set no `.alp/sdk-path`, so
    neither warning can fire there."""
    return [f"{issue.severity}: {issue.message}" for issue in issues]


def _error_outcome(
    project: Project,
    context: ProjectContext,
    exit_code: ExitCode,
    code: str,
    message: str,
) -> _Outcome:
    """tan-cli#464 review: took a bare `sdk: SdkInfo | None` and reported only
    the `code`/`message` issue -- so the manifest gate (the dominant refusal
    path, reached before `_run`'s own `sdk_resolution_issues` call further
    down) reported neither `sdk.project-pin-unresolved` nor
    `sdk.global-default-foreign-project`, even with `sdk.sourceTier` naming a
    tier that should have triggered one. Takes the whole `context` so every
    caller -- including a bundle-write failure, past the manifest gate -- gets
    the same pair from the one shared `sdk_resolution_issues`.

    tan-cli#497 defect 5: that fix reached `issues` and stopped there -- `text`
    was built without the pair, so the DEFAULT mode dropped both warnings
    while `--format json` reported them. Composed from the SAME list now, so
    the two channels cannot disagree."""
    issues = sdk_resolution_issues(
        context.broken_project_pin, context.sdk_source_tier, context.foreign_global_default_for
    )
    text = _sdk_warning_lines(issues)
    issues.append(Issue(code, "error", message))
    return _Outcome(
        exit_code,
        _empty_bundle(),
        project,
        issues,
        [*text, f"image: {message}"],
        context.sdk,
    )


def _run(
    *,
    app_path: str | None,
    build_root_arg: str | None,
    project_arg: str | None,
    board_yaml_arg: str | None,
    sdk_root_arg: str | None,
    disclosure: SdkDisclosure,
) -> _Outcome:
    """`disclosure` is the caller's, by reference -- the resolution facts are
    computed HERE and `image`'s `image.internal-failure` catch-all needs a name
    to read them from once this function has already raised. See
    `SdkDisclosure`."""
    context: ProjectContext = resolve_project_context(
        project_arg, board_yaml_arg, sdk_root_arg
    )
    project = context.project()
    # Recorded the instant the ladder answers, ahead of every other step this
    # function performs -- all of which can raise something unenumerated.
    disclosure.record(
        context.sdk,
        sdk_resolution_issues(
            context.broken_project_pin,
            context.sdk_source_tier,
            context.foreign_global_default_for,
        ),
    )
    app_base = resolve_app_base(app_path, context.workspace_root)
    build_root = resolve_build_root(build_root_arg, app_base)

    try:
        yaml_text, manifest = load_manifest(build_root)
    except ManifestUnavailable as err:
        # Note the asymmetry with `size`, which is faithful: `image` does NOT put
        # the OS error in its message, and a non-UTF-8 manifest is "not found"
        # here too (the read fails before the parser ever sees it).
        return _error_outcome(
            project,
            context,
            ExitCode.RUNTIME_FAILURE,
            "image.manifest-unavailable",
            f"system-manifest.yaml not found at {err.path}; run `tan build` first.",
        )
    except ManifestInvalid as err:
        return _error_outcome(
            project,
            context,
            ExitCode.RUNTIME_FAILURE,
            "image.manifest-invalid",
            f"{err.path}: {err.detail}",
        )

    sdk_root = context.sdk.root if context.sdk is not None else None
    try:
        notices, bundle, bundle_dir = _assemble_bundle(
            build_root, sdk_root, manifest, yaml_text, sdk_root_arg
        )
    except BundleWriteError as err:
        return _error_outcome(
            project,
            context,
            ExitCode.WRITE_FAILURE,
            "image.bundle-write-failed",
            err.message,
        )

    # Not always a green exit: an `error`-severity notice (a helper's concrete,
    # declared firmware_path that is not a file) flips exit/`ok` even though
    # assembly completed -- mkdir/tar/write all succeeded, so the manifest and
    # every OTHER artefact are still on disk and inspectable.
    exit_code = (
        ExitCode.RUNTIME_FAILURE
        if any(n.severity == "error" for n in notices)
        else ExitCode.SUCCESS
    )
    issues = [Issue(n.code, n.severity, n.message) for n in notices]
    # The same pair `_error_outcome` above computes for a manifest/bundle-write
    # refusal, from the same shared `sdk_resolution_issues` -- read back off
    # the disclosure rather than computed a second time, so the happy path, the
    # refusals and the catch-all cannot disagree.
    sdk_issues = list(disclosure.issues)
    issues.extend(sdk_issues)
    return _Outcome(
        exit_code,
        bundle,
        project,
        issues,
        # tan-cli#497 defect 5, happy-path half: a bundle whose helper
        # firmwares came out of a FALLBACK checkout used to be reported as a
        # plain `image: bundle ready at ...` with no mention that the project
        # pin had been ignored.
        _sdk_warning_lines(sdk_issues)
        + [n.message for n in notices]
        + [f"image: bundle ready at {bundle_dir}"],
        context.sdk,
    )


def image(
    ctx: typer.Context,
    app_path: str = typer.Argument(
        None,
        metavar="APP_PATH",
        help="Application source directory (default: the resolved --project "
        "workspace). build_root defaults to <APP_PATH>/build.",
    ),
    build_root: str = typer.Option(
        None,
        "--build-root",
        metavar="PATH",
        help="Override the build root holding system-manifest.yaml "
        "(default: <APP_PATH>/build).",
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
) -> None:
    """Assemble a flashable-image bundle from build/system-manifest.yaml."""
    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    json_mode = resolved_format == "json"

    # tan-cli#497 defect 5, the site the first pass missed. `_error_outcome` and
    # the happy path both report the SDK-resolution pair; this handler -- which
    # runs strictly after `resolve_project_context` has already answered --
    # reported only the crash, so a `.alp/sdk-path` the run ignored stayed
    # silent on exactly the path where a reader most needs to know which
    # checkout was in play. Recorded rather than recomputed here: the resolver
    # is itself one of the things that can raise, and this handler must not.
    disclosure = SdkDisclosure()
    try:
        outcome = _run(
            app_path=app_path,
            build_root_arg=build_root,
            project_arg=project,
            board_yaml_arg=board_yaml,
            sdk_root_arg=sdk_root,
            disclosure=disclosure,
        )
    except Exception as err:  # noqa: BLE001
        # The port's most-repeated defect class: a traceback puts nothing
        # parseable on stdout and the extension renders an empty panel with no
        # error. Anything reaching here is a tan bug, reported as one. Nothing in
        # this handler can itself throw -- `_empty_bundle()` and
        # `Project(None, None)` are both total, `_sdk_warning_lines` only
        # formats, and no path helper is called.
        outcome = _Outcome(
            ExitCode.INTERNAL_FAILURE,
            _empty_bundle(),
            Project(root=None, board_yaml=None),
            [
                *disclosure.issues,
                Issue(
                    "image.internal-failure",
                    "error",
                    f"image failed unexpectedly: {type(err).__name__}: {err}",
                ),
            ],
            [*_sdk_warning_lines(disclosure.issues), "image: internal failure"],
            disclosure.sdk,
        )

    # Built ONCE, for both formats: `Envelope.__init__` appends the tan-cli#407
    # `sdk.discovery-divergent` warning at the shared seam (`_with_sdk_
    # divergence`), and `outcome.text` was assembled strictly before any
    # `Envelope` existed -- so a seam-appended issue reached `--format json`
    # and was silent on the default text channel (tan-cli#799). Diffed
    # against `outcome.issues` (by value: `Issue` is a frozen dataclass) so
    # only what the seam ADDED is rendered, never a duplicate of a warning
    # `outcome.text` already carries via `_sdk_warning_lines` above.
    envelope = Envelope(
        "image",
        outcome.project,
        outcome.data,
        outcome.issues,
        outcome.exit_code,
        sdk=outcome.sdk,
    )
    if json_mode:
        emit(envelope)
    else:
        seam_extra = [issue for issue in envelope.issues if issue not in outcome.issues]
        stream = typer.get_text_stream("stderr")
        for issue in seam_extra:
            stream.write(f"{issue.severity}: {issue.message}\n")
        for line in outcome.text:
            stream.write(f"{line}\n")
    raise typer.Exit(int(outcome.exit_code))


# tan-cli#261: adds the seven oracle `GlobalArgs` flags this command was
# still missing (`--all`/`--ci`/`--no-color`/`--non-interactive`/`--quiet`/
# `--target`/`--verbose`) on top of `--board-yaml`, already declared and read
# above; see `tan.core.global_flags`.
image = accept_global_flags(image)
