# SPDX-License-Identifier: Apache-2.0
"""`tan clean` -- remove this project's build root, the orchestrator's state
cache, and any out-of-tree slice build dir the system manifest names.

Port of `crates/tan-cli/src/commands/clean.rs` plus the pure planning half it
delegates to (`tan_core::clean` + `tan_core::path_guard::is_unsafe_removal_target`).
Ported into ONE file deliberately: the pure part is ~50 lines with exactly one
consumer, and a separate `tan/core/clean.py` holding a four-line guard would be
an abstraction with a single call site. Every function names the Rust item it
mirrors.

**This command DELETES, so the safety rules are stricter than anywhere else in
the port:**

* Every removal candidate is screened by [`is_unsafe_removal_target`] -- the
  build root included -- BEFORE any filesystem call. A candidate that IS the
  project root, an ancestor of it, or a bare filesystem/drive/UNC root is
  REFUSED and reported, never silently dropped and never removed. That covers
  the `rm -rf $UNSET_VAR` shape: `--build-root ""`, `.` and `..` all resolve to
  the project root or above, as does a manifest `build_dir: ""`.
* The screen is NOT "must stay under the build root", and must not become that.
  `confine_to_build_root` -- the hardened containment guard this module DOES
  reuse, see [`_subsumed_by_build_root`] -- answers a different question, and
  two of the three target classes the oracle removes are legitimately OUTSIDE
  the build root: the app-root `.alp-build-state.json`, and an out-of-tree slice
  `build_dir` such as a Yocto tmp dir. Verified against the Rust binary:
  `tan clean --build-root ../outside` removes `../outside` and exits 0.
  Applying containment to every target would refuse two supported cases and
  diverge from the oracle on a destructive command. The rule is "not
  catastrophic", not "not outside" (`path_guard.rs:100-103`).
* A symlink or junction is never followed OUT of the tree. `shutil.rmtree`
  refuses a link outright, and [`_remove_dir`] removes the LINK itself instead
  -- so a `build/` junctioned at another directory unlinks the junction and
  leaves its target intact (verified against the Rust binary, whose
  `remove_dir_all` does the same on Windows).
* `--dry-run` reaches no removal call at all: [`_classify`] returns a
  `would-remove` disposition and the removal arms are never entered.
* The build root is never guessed. An unresolvable SDK is exit 1
  (`clean.sdk-root-not-found`) and an unsafe build root is exit 1
  (`clean.unsafe-build-root`), rather than a best-effort removal of something
  nearby.

**Nothing here learns a hardware fact and nothing shells the SDK.** The
checkout is probed for its loader marker (`scripts/alp_project.py`, I-31) and
otherwise untouched: removing a build directory needs no SDK, and invoking one
would give `clean` a dependency it deliberately does not have (I-32, port-spec
anti-pattern #22). The only project input beyond the arguments is
`<build_root>/system-manifest.yaml`, which this project's own build wrote.

Every failure path emits a coded envelope. An escaping traceback puts nothing
parseable on stdout and the extension then renders an empty panel with no
error, so [`clean`]'s outer guard converts any unexpected exception into
`clean.internal-failure` at exit 5. Its recovery path builds the envelope from
constants only -- never a helper that can itself throw -- because a helper
called from the recovery path is how a single fault became a DOUBLE fault
elsewhere in this port.

**KNOWN GAP, for whoever owns packaging.** The manifest sweep needs a YAML
parser and tan declares none; `scripts/build_binary.sh` documents the frozen
binary's build environment as `pip install typer rich pyinstaller`, so the
SHIPPED `tan clean` takes the no-PyYAML arm and emits a
`clean.manifest-unreadable` warning on every project that has ever been built
-- where the Rust oracle emits nothing. The behaviour is correct (see
[`parse_manifest_slices`]: reported, never swallowed, never fatal) but noisy.
Closing it is a packaging call -- add PyYAML to the frozen build, weighed against
the artefact-size budget `build_binary.sh` records -- and deliberately NOT a
hand-rolled fallback scanner here: a mis-parse would name a PATH handed to a
recursive removal.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer

from tan.commands.build.materialise import MaterialiseError, confine_to_build_root
from tan.commands.build_cmd import resolve_sdk_root_ladder
from tan.commands.presets_cmd import resolve_project_paths, resolve_sdk
from tan.commands.sdk_cmd import SDK_MARKER, global_default_foreign_project_issue, project_pin_issue
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

#: The orchestrator state cache removed alongside the build root -- verbatim
#: `alp_clean.py`'s `targets[1]`. The orchestrator actually writes its cache at
#: `<build_root>/.alp-build-state.json`, already subsumed by the recursive
#: build-root removal; this app-root path is the faithful, usually-absent target
#: the Python cleaner kept. Preserved, not "fixed" (`tan_core::clean` docs).
STATE_FILE = ".alp-build-state.json"

#: The manifest this command sweeps for out-of-tree slice build dirs, relative
#: to the resolved build root.
MANIFEST_NAME = "system-manifest.yaml"

#: The system-manifest schema major consumed here. A different value is a
#: warning and the manifest is IGNORED -- never read as if it were v1
#: (`SYSTEM_MANIFEST_SCHEMA_VERSION`).
MANIFEST_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Pure path shape -- `tan_core::path_guard`
# ---------------------------------------------------------------------------


def _rust_join(base: str, rel: str) -> str:
    """`PathBuf::push` semantics, which `os.path.join` does not have on Windows.

    Rust replaces the base OUTRIGHT when the right-hand side carries its own
    prefix -- a drive (`C:foo`), a UNC share (`\\\\server\\share\\x`) or the
    device namespace (`\\\\?\\C:\\x`). `ntpath.join` agrees for a DIFFERENT
    drive but treats a drive-relative path on the SAME drive as relative to the
    accumulated path, so `join("C:/proj", "C:foo")` yields `C:/proj\\foo` where
    Rust yields `C:foo`. `C:foo` is one of the shapes a `--build-root` guard has
    to get right, so the divergence is closed here rather than tolerated.

    Everything else defers to `os.path.join`, which already matches Rust for the
    rooted-but-prefixless case (`\\rooted` keeps the base's drive) and for the
    empty and `..` cases (verified against the Rust binary). On POSIX
    `splitdrive` always reports no drive, so this is `os.path.join` verbatim.
    """
    if os.path.splitdrive(rel)[0]:
        return rel
    return os.path.join(base, rel)


def _normalize(path: str) -> str:
    """Lexically collapse `.`/`..` without touching the filesystem --
    `path_guard::normalize`.

    `os.path.normpath`, so no symlink is resolved and a path that does not exist
    still normalizes. Used for COMPARISON only; a reported path keeps whatever
    spelling the oracle would report.

    One known divergence, unreachable here: Rust's `normalize` pops past the
    start, so a relative `..` collapses to the empty path where `normpath`
    keeps `..`. Every input below is already absolute (the project root is
    cwd-anchored, and the build root and slice dirs are joined onto it), so the
    difference cannot be reached.
    """
    return os.path.normpath(path)


def _has_normal_component(normalized: str) -> bool:
    """Whether the path names anything at all beyond a root/prefix -- Rust's
    `components().any(Component::Normal)`.

    False for `/`, `C:\\`, `C:` and a bare UNC share `\\\\server\\share`: each is
    a root whose recursive removal takes out far more than a build tree.
    """
    return os.path.splitdrive(normalized)[1].strip("\\/") != ""


def _is_under(base: str, path: str) -> bool:
    """Component-wise containment on normalized paths -- Rust's
    `Path::starts_with`, NOT a string prefix test.

    `/p/build` contains `/p/build/x` and itself, but NOT the sibling
    `/p/build2` -- which a plain `str.startswith` would wrongly accept, and
    which decides whether a slice dir is treated as a separate removal target.
    Case-sensitive, matching Rust (which folds case on the Windows drive prefix
    only, and both sides here come from the same join).
    """
    base_n, path_n = _normalize(base), _normalize(path)
    if base_n == path_n:
        return True
    return path_n.startswith(base_n.rstrip("\\/") + os.sep)


def is_unsafe_removal_target(project_root: str, target: str) -> bool:
    """True when recursively removing `target` would take out far more than a
    build tree, and the caller must refuse -- `path_guard::is_unsafe_removal_target`.

    Rejects a filesystem/drive/UNC root (no `Normal` component at all), and the
    project root itself or any ancestor of it. Deliberately does NOT require
    containment under the project root: an out-of-tree slice build dir is a
    supported clean target (see the module docstring).

    tan-cli#499: the LEXICAL test (`_normalize` = `os.path.normpath`, never
    resolving a symlink) is exact oracle parity, but on its own it is
    defeated when `target` reaches the project root -- or an ancestor of it
    -- through a SYMLINKED PARENT component (a `current ->` release link, a
    WSL/`/mnt` alias, any checkout reached through a linked parent
    directory): the two strings compare unequal even though they name the
    same directory, the candidate is judged safe, and `shutil.rmtree`
    recursively deletes the user's sources. The `os.path.realpath` arm below
    is ADDITIVE, never a replacement -- it can only ever refuse MORE targets
    than the lexical test alone, never remove more, and it changes nothing
    about what a caller REPORTS: `data.buildRoot` and every rejection message
    still carry the lexical spelling the user typed
    (`test_normalize_is_lexical_and_never_resolves_a_symlink` pins that; this
    function's own return value is a bare bool, so there is nothing here for
    a resolved path to leak into). A target that IS a symlink itself (not
    reached THROUGH one) stays safe to remove, matching the module's own
    documented rule that a link is unlinked, never followed.
    """
    target_n = _normalize(target)
    if not _has_normal_component(target_n):
        return True
    # True when the target IS the project root or an ancestor of it -- both
    # would delete the user's sources.
    if _is_under(target_n, project_root):
        return True
    return _is_under(os.path.realpath(target), os.path.realpath(project_root))


# ---------------------------------------------------------------------------
# Pure removal planning -- `tan_core::clean`
# ---------------------------------------------------------------------------

#: Filesystem-kind + disposition pairs, keyed by what a probe found.
_DIR_ACTIONS = {False: ("dir", "removed"), True: ("dir", "would-remove")}
_FILE_ACTIONS = {False: ("file", "removed"), True: ("file", "would-remove")}


@dataclass(frozen=True)
class _Rejected:
    """A candidate refused as too dangerous to remove, with where it came from
    so the message can name the culprit -- `clean::RejectedTarget`."""

    path: str
    #: `build-root` | `slice` -- the state file is never screened (see
    #: [`plan_clean_targets`]), so it has no rejection message.
    origin: str
    core_id: str = ""
    raw: str = ""

    def reason(self) -> str:
        """One-line explanation naming the source, verbatim from
        `RejectedTarget::reason`. The em dash is the oracle's own character."""
        if self.origin == "slice":
            return (
                f"refusing to remove slice '{self.core_id}' build_dir "
                f'"{self.raw}" (resolves to {self.path}) \u2014 it is the '
                "project root, an ancestor of it, or a filesystem root; fix "
                "build/system-manifest.yaml"
            )
        return (
            f"refusing to remove build root {self.path} \u2014 it is the "
            "project root, an ancestor of it, or a filesystem root"
        )


@dataclass
class _Plan:
    """`clean::CleanPlan`: paths cleared for removal (build root first), plus
    everything refused. A non-empty `rejected` means the command must report and
    fail -- never quietly clean less than asked."""

    targets: list[str] = field(default_factory=list)
    rejected: list[_Rejected] = field(default_factory=list)


def _subsumed_by_build_root(build_root: str, resolved: str) -> bool:
    """Whether a slice `build_dir` is already covered by the recursive build-root
    removal, so it contributes no extra target.

    Two tests, and EITHER answering "inside" is enough:

    * the oracle's lexical `clean::is_under`, which is what parity is measured
      against; and
    * [`confine_to_build_root`], the port's hardened containment guard, reused
      here rather than re-derived -- this is the one question in `clean` whose
      semantics really are "is this path confined under the build root". It
      resolves both sides, so it also catches a junction or symlink inside
      `build/` that points out of the tree, and the Windows shapes
      (`C:foo`, `\\x`, UNC, `\\\\?\\`) a lexical test misses.

    OR, not AND, deliberately: a disagreement can then only make the port treat
    a path as ALREADY COVERED, i.e. remove strictly less than the oracle -- and
    the disagreement only arises when the path genuinely does live inside
    `build_root`, which the recursive removal handles anyway, so the resulting
    disk state is identical. Requiring both to agree would let a resolved-inside
    path become a SEPARATE `shutil.rmtree` call, which is the one direction a
    destructive command must not drift in.
    """
    if _is_under(build_root, resolved):
        return True
    if not os.path.isabs(resolved):
        # A DRIVE-RELATIVE leftover (`C:rel`, the one shape `_rust_join` cannot
        # make absolute because Rust does not either). It cannot be containment-
        # tested against an unrelated base without inventing a meaning for it:
        # `Path("C:/proj/build") / "C:rel"` re-reads it as relative to the base
        # and answers "inside", while Rust reports it as its own target resolved
        # against drive C:'s current directory. Measured -- a manifest
        # `build_dir: "C:rel"` made the oracle list an `absent` target the port
        # silently dropped. The lexical answer above IS the oracle's answer here,
        # and the candidate is still screened by `is_unsafe_removal_target`.
        return False
    try:
        confine_to_build_root(Path(build_root), resolved)
    except (MaterialiseError, OSError, ValueError):
        # `MaterialiseError` is the escape verdict; `OSError`/`ValueError` come
        # from `Path.resolve()` on a shape the host rejects outright (a device-
        # namespace path, an over-long name). Either way: not proven inside.
        return False
    return True


def plan_clean_targets(
    project_root: str, build_root: str, slices: list[dict[str, Any]]
) -> _Plan:
    """Ordered, de-duplicated removal targets -- `clean::clean_targets`.

    1. `build_root`, recursively.
    2. `<project_root>/.alp-build-state.json`.
    3. each slice `build_dir` that lies OUTSIDE `build_root` (see
       [`_subsumed_by_build_root`]); a relative value resolves against
       `project_root`, an absolute one is taken as-is.

    Every candidate except the state file is then screened by
    [`is_unsafe_removal_target`]. The manifest is unvalidated file content and
    `build_dir: ""`, `.`, `/` or `../..` each resolve to the project root or
    above; a rejected candidate goes to `rejected` so the caller can surface it
    and fail, never silently dropped. The state file is exempt because it is a
    single unlink of one fixed name under the project root, never a recursive
    removal -- matching the oracle's own exemption.
    """
    candidates: list[tuple[str, _Rejected | None]] = [
        (build_root, _Rejected(build_root, "build-root")),
        (_rust_join(project_root, STATE_FILE), None),
    ]
    for entry in slices:
        raw = entry.get("build_dir")
        if not isinstance(raw, str):
            continue
        resolved = _rust_join(project_root, raw)
        if not _subsumed_by_build_root(build_root, resolved):
            core_id = entry.get("core_id", "")
            candidates.append(
                # `str()`: a plain-scalar `core_id: 7` is a valid String to
                # serde_yaml, so it can reach the rejection message as an int.
                (resolved, _Rejected(resolved, "slice", str(core_id), raw))
            )

    plan = _Plan()
    seen: list[str] = []
    for path, rejection in candidates:
        key = _normalize(path)
        if key in seen:
            continue
        seen.append(key)
        if rejection is None or not is_unsafe_removal_target(project_root, path):
            plan.targets.append(path)
        else:
            plan.rejected.append(rejection)
    return plan


def _classify(is_dir: bool, is_file: bool, dry_run: bool) -> tuple[str, str]:
    """`(kind, action)` from a probed filesystem type + the dry-run flag --
    `clean::classify`. A path that is neither a dir nor a file is `absent`:
    skipped entirely, not counted, no removal attempted."""
    if is_dir:
        return _DIR_ACTIONS[dry_run]
    if is_file:
        return _FILE_ACTIONS[dry_run]
    return ("absent", "absent")


# ---------------------------------------------------------------------------
# The system-manifest sweep
# ---------------------------------------------------------------------------


#: `serde` renders an unexpected value as `<kind>` for containers and
#: <code>&lt;kind&gt; `value`</code> for scalars. Harvested from the Rust binary
#: across 25 malformed-manifest shapes, so the port's warning text matches
#: rather than approximates.
_SERDE_KIND = {
    type(None): "unit value",
    bool: "boolean",
    int: "integer",
    float: "floating point",
    str: "string",
    list: "sequence",
    dict: "map",
}


def _serde_value(value: Any) -> str:
    """How serde names an unexpected value in `invalid type: ...`.

    `sequence`/`map`/`unit value` carry no payload; a bool renders lowercase
    (`true`), a string in double quotes, a number in backticks.
    """
    kind = _SERDE_KIND.get(type(value), type(value).__name__)
    if value is None or isinstance(value, (list, dict)):
        return kind
    if isinstance(value, bool):
        return f"{kind} `{'true' if value else 'false'}`"
    if isinstance(value, str):
        return f'{kind} "{value}"'
    return f"{kind} `{value}`"


def _is_yaml_scalar(value: Any) -> bool:
    """Whether serde_yaml would accept this value for a `String` field.

    YAML plain scalars are untyped, and serde_yaml 0.9 hands one to whichever
    visitor the target field asks for -- so `core_id: 7` deserializes into
    `String` as `"7"` with no error. Verified against the Rust binary
    (`core_id: 7` parses clean and the run exits 0 with no issue). A port that
    demanded `isinstance(str)` here would emit a `clean.manifest-unreadable`
    warning the oracle does not.
    """
    return isinstance(value, (str, int, float, bool))


def parse_manifest_slices(text: str) -> tuple[list[dict[str, Any]], str | None]:
    """`(slices, error)` for a `system-manifest.yaml` document --
    `parse_system_manifest`, narrowed to the one field this command consumes.

    FAIL-CLOSED, matching serde: a document that is not a well-formed v1
    manifest yields NO slices and an error string, so a half-read manifest can
    never hand a garbage `build_dir` to a recursive removal. Tolerant of
    additive v1 fields (`deny_unknown_fields` is deliberately off upstream).

    Message text was matched against the Rust binary shape by shape; two
    divergences remain and are deliberate:

    * the trailing ` at line N column M` serde_yaml appends is absent --
      `yaml.safe_load` discards node marks, and recovering them would mean a
      custom composer for a warning string;
    * a raw YAML SYNTAX error (a stray tab, an unclosed flow node) carries
      PyYAML's wording after the shared `system-manifest is not valid YAML: `
      prefix.

    `build_dir` REQUIRES a real string, where serde_yaml would coerce a plain
    scalar (`build_dir: 7` becomes `"7"` and, verified against the Rust binary,
    a THIRD removal target at `<project>/7`). Diverging here is deliberate: this
    is the only manifest field that becomes a path handed to a recursive
    removal, and deriving a delete target from a number in a malformed manifest
    is not a behaviour worth reproducing. The port removes strictly less, the
    SDK emits strings, and [`test_numeric_build_dir_is_not_turned_into_a_delete_target`]
    pins it so the choice cannot drift silently.

    PyYAML is optional -- tan declares no YAML dependency -- and its absence is
    REPORTED rather than swallowed: a project whose slices build out of tree
    would otherwise have them left behind with no indication why. Still only a
    warning; `clean` never fails over a manifest.
    """
    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError:  # pragma: no cover -- present in every real workspace
        return [], (
            "no YAML parser available (PyYAML is not installed), so out-of-tree "
            "slice build dirs were not swept"
        )
    try:
        doc = yaml.safe_load(text)
    except Exception as err:  # noqa: BLE001 -- any parser failure, incl. the C ext
        return [], f"system-manifest is not valid YAML: {err}"

    prefix = "system-manifest is not valid YAML: "
    if doc is None:
        # `yaml.safe_load` collapses two cases serde keeps apart: a file with no
        # document at all (empty, whitespace-only, comments-only, or a bare
        # `---`) has no fields, so serde reports the first REQUIRED one; an
        # explicit `null`/`~` node is a real value of the wrong TYPE. Both were
        # measured against the Rust binary. Told apart by whether the source
        # carries any node text of its own.
        body = "\n".join(
            line
            for line in text.splitlines()
            if line.strip() not in ("", "---", "...") and not line.lstrip().startswith("#")
        )
        if not body.strip():
            return [], f"{prefix}missing field `schema_version`"
        return [], f"{prefix}invalid type: unit value, expected struct SystemManifest"
    if not isinstance(doc, dict):
        return [], f"{prefix}invalid type: {_serde_value(doc)}, expected struct SystemManifest"

    if "schema_version" not in doc:
        return [], f"{prefix}missing field `schema_version`"
    version = doc["schema_version"]
    # `bool` is an `int` in Python but never a serde `u32`, so it is a type
    # error, not a version. Checked before the int test for that reason.
    if isinstance(version, bool) or not isinstance(version, int):
        return [], (
            f"{prefix}schema_version: invalid type: {_serde_value(version)}, expected u32"
        )
    if version != MANIFEST_SCHEMA_VERSION:
        return [], (
            f"unsupported system-manifest schema_version {version} (this CLI "
            f"consumes v{MANIFEST_SCHEMA_VERSION}); upgrade the CLI or the SDK "
            "so the versions match"
        )

    raw = doc.get("slices", [])
    if not isinstance(raw, list):
        return [], f"{prefix}slices: invalid type: {_serde_value(raw)}, expected a sequence"
    for index, entry in enumerate(raw):
        # `core_id`/`os` are non-Option in the Rust `Slice`, so serde fails the
        # WHOLE document when either is missing; `build_dir` is
        # `Option<String>`, so `null`/absent is fine and a sequence is not. A
        # partial read here would act on a manifest the oracle rejects outright.
        if not isinstance(entry, dict):
            return [], (
                f"{prefix}slices[{index}]: invalid type: {_serde_value(entry)}, "
                "expected struct Slice"
            )
        for required in ("core_id", "os"):
            if not _is_yaml_scalar(entry.get(required)):
                missing = required not in entry or entry.get(required) is None
                return [], (
                    f"{prefix}slices[{index}]: missing field `{required}`"
                    if missing
                    else f"{prefix}slices[{index}].{required}: invalid type: "
                    f"{_serde_value(entry[required])}, expected a string"
                )
        build_dir = entry.get("build_dir")
        if build_dir is not None and not _is_yaml_scalar(build_dir):
            return [], (
                f"{prefix}slices[{index}].build_dir: invalid type: "
                f"{_serde_value(build_dir)}, expected a string"
            )
    return list(raw), None


def _read_manifest(build_root: str) -> tuple[list[dict[str, Any]], str | None]:
    """The manifest's slices, or `([], error)`.

    A READ failure -- absent, a directory in its place, a denied ACL, non-UTF-8
    bytes -- is SILENT (`([], None)`), matching the oracle's `Err(_) => None`
    arm: no issue, no text, exit unchanged. Verified against the Rust binary for
    both the directory and the non-UTF-8 cases. Only a document that WAS read
    and could not be understood is a warning.
    """
    try:
        text = Path(build_root, MANIFEST_NAME).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return [], None
    return parse_manifest_slices(text)


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


def is_link(path: str) -> bool:
    """Whether `path` is a link that must not be followed -- a POSIX symlink, a
    Windows directory symlink, OR a Windows JUNCTION.

    **`os.path.islink` is not this test.** On Windows `ntpath.islink` returns
    True only for `IO_REPARSE_TAG_SYMLINK`; a junction is
    `IO_REPARSE_TAG_MOUNT_POINT`, and `stat.S_ISLNK` is False for it as well.
    Measured on this host: for `build/` junctioned at an out-of-tree directory,
    `os.path.islink` and `S_ISLNK` both report False while
    `st_reparse_tag == IO_REPARSE_TAG_MOUNT_POINT`. A guard written on
    `os.path.islink` therefore lets a junction reach `shutil.rmtree` -- which
    has its OWN, correct check (`shutil._rmtree_islink`, mirrored here) and
    refuses, so nothing outside the tree is destroyed, but the junction is then
    never cleaned and the run reports a spurious `remove-failed`. This was a
    live defect in the first cut of this port, caught only by diffing against
    the Rust binary.
    """
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        and getattr(st, "st_reparse_tag", 0)
        == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)
    )


def os_error_text(err: BaseException) -> str:
    """An `OSError` rendered the way Rust's `io::Error` Display renders it:
    `<system message> (os error <code>)`.

    Python's own `str(OSError)` is `[WinError 32] <message>: '<path>'`, which
    both differs from the oracle and repeats a path the message already names.
    The Windows error code (`winerror`) is preferred over the translated
    `errno`, matching Rust, which reports the raw OS code.

    One character still differs on Windows: `FormatMessageW` ends its sentences
    with a period and Rust keeps it, while Python's `strerror` strips it. Not
    synthesized here -- guessing at punctuation inside a system message is worse
    than a documented one-character divergence in a warning string.
    """
    if not isinstance(err, OSError):
        return str(err)
    code = getattr(err, "winerror", None) or err.errno
    if err.strerror is None or code is None:
        return str(err)
    return f"{err.strerror} (os error {code})"


#: `func` values this hook can safely retry as a bare `func(path)` call --
#: tan-cli#499. `shutil.rmtree`'s POSIX walk (`_rmtree_safe_fd`) calls this
#: hook with several OTHER raw stdlib functions on the fd-based path --
#: `os.open` (needs a required `flags` argument shutil's own call supplies
#: but a bare `func(path)` retry does not), `os.close` (needs a file
#: descriptor, not a path string -- and CPython's own `shutil.py` source
#: carries the comment "close() should not be retried after an error"),
#: `os.scandir` and `os.lstat` (retrying either on the SAME path answers a
#: different question than the one that failed: `os.scandir(topfd)`'s
#: retry-by-path is not the same call), and `os.path.islink` (not a real
#: failure at all -- a `raise OSError(...)` this module manufactures for "a
#: directory turned into a symlink mid-walk", which no chmod can fix). A
#: read-only bit on a directory or file is exactly what `os.unlink`/
#: `os.rmdir` hit, which is the ONLY case this hook exists to retry (see the
#: docstring below) -- every other `func` re-raises instead.
_RETRYABLE_RMTREE_FUNCS = (os.unlink, os.rmdir)


def _retry_after_clearing_readonly(func, path, _exc=None) -> None:
    """`shutil.rmtree` error hook: clear the read-only bit and retry once.

    Rust's `remove_dir_all` deletes a read-only file on Windows outright (it
    passes `FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE`), where `shutil.rmtree`
    fails the WHOLE tree with `[WinError 5] Access is denied`. Measured against
    the Rust binary: one read-only file inside `build/` had Rust remove the
    build dir and exit 0 while the port left every artefact in place and warned.
    Read-only build outputs are ordinary -- some toolchains mark generated files
    that way -- so this is the primary path, not an exotic one.

    `st_mode | S_IWUSR` rather than a bare `S_IWRITE`: on POSIX the latter would
    replace the whole mode with `0o200` and strip the owner's read/execute bits
    from a directory mid-walk. A failure here propagates out of `rmtree` and is
    reported by the caller as `clean.remove-failed`.

    tan-cli#499: a bare `func(path)` retry used to be unconditional. On
    POSIX, `shutil.rmtree` runs the fd-based `_rmtree_safe_fd` walk, which
    hands this hook `os.open`/`os.close` for a NESTED directory it could not
    open/close -- neither is callable with one positional argument, and
    `os.open(path)` raising `TypeError: open() missing required argument
    'flags'` is neither `OSError` nor `ValueError`, so it sailed past this
    module's own `except (OSError, ValueError)` guard, escaped the whole
    target loop, and hit `clean`'s outer catch-all (`clean.internal-failure`,
    exit 5) -- abandoning every OTHER target still queued (the state file,
    any out-of-tree slice dir) with no attempt made. The real trigger is not
    a root-owned tree (`os.chmod` fails there first with `EPERM`, an
    `OSError`, handled correctly) but a directory the invoking user OWNS and
    cannot OPEN -- no r/x bits, or any non-permission `os.open` failure such
    as fd exhaustion.

    Only [`_RETRYABLE_RMTREE_FUNCS`] are retried; every other `func` clears
    the read-only bit (harmless even when it cannot help) and then RE-RAISES
    the original exception via a bare `raise` -- re-raising the exception
    this hook is being called synchronously from inside the handling of,
    exactly like the retried arm already relies on `func(path)` raising to
    propagate a retry failure. Re-raising, not silently returning, matters:
    `shutil.rmtree` treats an `onexc` hook that returns without raising as
    "handled, keep going", which here would report `removed` for a subtree
    this process never actually removed.
    """
    os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
    if func in _RETRYABLE_RMTREE_FUNCS:
        func(path)
        return
    raise


#: `shutil.rmtree`'s error-hook keyword. `onerror` is deprecated from 3.12 and
#: scheduled for removal; `onexc` does not exist before it. Selected once here so
#: the call site stays a single expression on either interpreter -- the handler
#: signature is compatible because it ignores its third argument, which is the
#: only thing the two hooks disagree about (`exc_info` tuple vs exception).
_RMTREE_HOOK = "onexc" if sys.version_info >= (3, 12) else "onerror"


def _remove_dir(path: str) -> None:
    """Remove a directory target recursively, never following a link out of the
    tree.

    A link ([`is_link`]) is unlinked ITSELF, exactly as the oracle's
    `remove_dir_all` does on Windows: verified against the Rust binary with
    `build/` junctioned at an out-of-tree directory -- the junction goes, the
    target's contents stay. `shutil.rmtree` handles the ordinary case and never
    recurses through a link INSIDE the tree, so both arms are contained.

    `os.rmdir` before `os.unlink`: on Windows a junction or directory symlink is
    removed by `RemoveDirectory`, and `unlink` fails on it; on POSIX `rmdir`
    fails on a symlink and `unlink` is what removes it.
    """
    if is_link(path):
        try:
            os.rmdir(path)
        except OSError:
            os.unlink(path)
        return
    shutil.rmtree(path, **{_RMTREE_HOOK: _retry_after_clearing_readonly})


# ---------------------------------------------------------------------------
# SDK resolution
# ---------------------------------------------------------------------------


def _cli_workspace_root(project_arg: str | None) -> Path:
    """`util::cli_workspace_root`: `--project` joined to the cwd, UNNORMALIZED
    (the oracle normalizes only for the reported `project.root`), or the cwd
    itself when the flag is absent. Feeds the SDK guard's discovery walk, whose
    sibling/ancestor probes are lexical -- so the unnormalized spelling is what
    keeps the two implementations probing the same directories."""
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = "."  # `current_dir().unwrap_or_else(|_| PathBuf::from("."))`
    return Path(cwd if project_arg is None else _rust_join(cwd, project_arg))


def sdk_root_resolves(sdk_root: str | None, workspace_root: Path) -> bool:
    """Whether `build_cmd.resolve_sdk_root_ladder` would resolve a checkout --
    the guard behind `clean.sdk-root-not-found`.

    `--sdk-root` is TERMINAL (I-31): an explicit path without the loader marker
    fails here rather than falling through to a lower tier and cleaning against
    a checkout the caller never named -- checked explicitly below because the
    ladder itself returns a `--sdk-root` value unvalidated (matching the
    oracle's `resolve_sdk_tiered`, terminal for REPORTING); this gate matches
    `util::resolve_sdk_root`, terminal AND validated. The project-pin and
    global-default tiers are best-effort, so a stale pointer falls through
    instead of locking the user out.

    The ladder's LAST tier is the wide positional walk (root, child `alp-sdk`,
    sibling `alp-sdk`, sibling `alp-sdk-upstream`, then ancestors; first match
    wins), but it is reached only when the narrower `resolve_sdk_tiered`
    discovery tier AHEAD of it answers `None` -- a narrow hit short-circuits.
    So in a workspace holding BOTH a child `alp-sdk` and a lateral one, what
    gates this command is the lateral checkout, not the child, and the wide
    walk never runs (measured against the oracle: `tan clean` there resolves
    `../alp-sdk` too, tan-cli#263).

    That ordering does not move this boolean: every candidate the narrow tier
    probes is also one the wide walk probes, so a narrow hit implies a wide
    hit. What the wide tail still buys is the case the narrow tier cannot
    answer -- a `tan bootstrap` workspace whose checkout is a CHILD of the cwd,
    where narrow returns `None` and, without the tail, `tan clean` would refuse
    to run (tan-cli#218; measured: the oracle resolves `<ws>/alp-sdk` there).

    Note this gate and the REPORTED `sdk` key are still two different
    resolutions: [`resolve_sdk`][tan.commands.presets_cmd.resolve_sdk] (below)
    reports through `resolve_sdk_tiered` alone, so a bootstrap-child workspace
    gates open here while reporting no `sdk` at all.
    """
    resolution = resolve_sdk_root_ladder(sdk_root, workspace_root)
    if resolution.path is None:
        return False
    if resolution.tier == "sdkRootFlag":
        return resolution.path.joinpath(*SDK_MARKER).exists()
    return True


# ---------------------------------------------------------------------------
# Envelope assembly
# ---------------------------------------------------------------------------


@dataclass
class _Outcome:
    """What one run produced. Built and returned, never emitted in place, so the
    exception guard in [`clean`] can wrap the whole computation without also
    catching `typer.Exit` (a `RuntimeError` subclass, not a `SystemExit`, which a
    bare `except Exception` would otherwise swallow)."""

    exit_code: ExitCode
    data: dict[str, Any]
    project: Project
    sdk: SdkInfo | None
    issues: list[Issue]
    text: list[str]


def _report(build_root: str, dry_run: bool, targets: list[dict[str, str]], removed: int):
    return {
        "buildRoot": build_root,
        "dryRun": dry_run,
        "targets": targets,
        "removed": removed,
    }


def _run(
    *,
    app_path: str,
    build_root_arg: str | None,
    dry_run: bool,
    project_arg: str | None,
    board_yaml_arg: str | None,
    sdk_root_arg: str | None,
    quiet: bool,
) -> _Outcome:
    """The whole command as a computation returning one outcome. Nothing here
    emits or exits; [`clean`] does both exactly once."""
    workspace_root, board_yaml = resolve_project_paths(project_arg, board_yaml_arg)
    # tan-cli#236: `boardYaml` reported only when the file really exists.
    project = Project.resolved(workspace_root, board_yaml)
    resolved_sdk = resolve_sdk(sdk_root_arg, workspace_root)
    sdk = SdkInfo(resolved_sdk.path, resolved_sdk.tier) if resolved_sdk else None
    pin_issue = (
        project_pin_issue(resolved_sdk.broken_project_pin, resolved_sdk.tier)
        if resolved_sdk
        else None
    )
    foreign_issue = (
        global_default_foreign_project_issue(resolved_sdk.foreign_global_default_for)
        if resolved_sdk
        else None
    )

    # App base: a non-`.` positional roots the removal at that app dir,
    # overriding `--project`; `.` falls back to the resolved workspace.
    if app_path == ".":
        project_root = workspace_root
    else:
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = "."
        project_root = _rust_join(cwd, app_path)

    # SDK-root guard -- faithful to `alp_clean.py`'s `log.die('Cannot locate
    # alp-sdk root.')`. Arguably YAGNI (removing a build dir needs no SDK), but
    # the oracle keeps it, so the port keeps it.
    if not sdk_root_resolves(sdk_root_arg, _cli_workspace_root(project_arg)):
        message = "Cannot locate alp-sdk root."
        return _Outcome(
            exit_code=ExitCode.RUNTIME_FAILURE,
            data=_report("", dry_run, [], 0),
            project=project,
            sdk=sdk,
            issues=[Issue("clean.sdk-root-not-found", "error", message)],
            text=[f"clean: {message}"],
        )

    # `--build-root`: absolute as-is, relative against the project root,
    # default `<project_root>/build`. The default is deliberately NOT
    # normalized -- the oracle normalizes only the flag branch, and
    # `data.buildRoot` is a compared field.
    if build_root_arg is not None:
        build_root = _normalize(_rust_join(project_root, build_root_arg))
    else:
        build_root = _rust_join(project_root, "build")

    # Fail fast, BEFORE the manifest is read: `--build-root ""` / `.` / `..`
    # each resolve to the project root or above. Refusing here is what stops
    # the `rm -rf $UNSET_VAR` shape reaching a recursive removal at exit 0.
    if is_unsafe_removal_target(project_root, build_root):
        why = (
            f"refusing to remove `{build_root}`: a build root may not be the "
            "project root, an ancestor of it, or a filesystem root"
        )
        return _Outcome(
            exit_code=ExitCode.RUNTIME_FAILURE,
            data=_report(build_root, dry_run, [], 0),
            project=project,
            sdk=sdk,
            issues=[Issue("clean.unsafe-build-root", "error", why)],
            text=[f"clean: {why}"],
        )

    text: list[str] = []
    issues: list[Issue] = []
    if pin_issue is not None:
        # tan-cli#263 review: `clean` reached the SDK guard above (something
        # DID resolve), so the pin's silent fallthrough belongs in the same
        # place every other non-fatal notice here lands.
        issues.append(pin_issue)
    if foreign_issue is not None:
        # tan-cli#464: same reasoning, for a `globalDefault` answer a
        # DIFFERENT project's bootstrap relocation actually decided.
        issues.append(foreign_issue)

    # Best-effort, manifest-aware sweep. Absence (or an unreadable file) is
    # silent; a parse/version error is a warning, NEVER fatal -- clean must not
    # fail over a manifest it only consults for an optimisation.
    slices, manifest_error = _read_manifest(build_root)
    if manifest_error is not None:
        detail = f"ignoring unreadable system-manifest.yaml: {manifest_error}"
        if not quiet:
            text.append(f"clean: {detail}")
        issues.append(Issue("clean.manifest-unreadable", "warning", detail))

    plan = plan_clean_targets(project_root, build_root, slices)

    records: list[dict[str, str]] = []
    removed = 0
    exit_code = ExitCode.SUCCESS

    # A manifest naming a catastrophic build_dir is a broken manifest, not a
    # reason to quietly clean less than asked. Report every refusal and fail.
    for refused in plan.rejected:
        why = refused.reason()
        text.append(f"clean: {why}")
        issues.append(Issue("clean.unsafe-target", "error", why))
        records.append(
            {"path": refused.path, "kind": "dir", "action": "refused-unsafe"}
        )
        exit_code = ExitCode.RUNTIME_FAILURE

    for target in plan.targets:
        probe = Path(target)
        try:
            is_dir, is_file = probe.is_dir(), probe.is_file()
        except OSError:
            # `Path.is_dir()` swallows its own OSError, but a shape the host
            # rejects outright (an over-long name, an illegal character) can
            # still raise ValueError/OSError on some hosts. Treat as absent --
            # a path that cannot be probed is certainly not removed.
            is_dir = is_file = False
        except ValueError:
            is_dir = is_file = False
        kind, action = _classify(is_dir, is_file, dry_run)

        if action == "would-remove":
            verb = "rmtree" if kind == "dir" else "unlink"
            text.append(f"[DRY] would {verb} {target}")
            records.append({"path": target, "kind": kind, "action": action})
            continue
        if action == "absent":
            records.append({"path": target, "kind": kind, "action": action})
            continue

        text.append(f"clean: removing {target}")
        if kind == "dir":
            # Best-effort, matching `rmtree(ignore_errors=True)`: a failure does
            # NOT fail the command, but it IS reported -- as a warning issue and
            # the `remove-failed` action -- and is not counted `removed`. The
            # envelope must never claim a directory was removed when it was not.
            try:
                _remove_dir(target)
            except (OSError, ValueError) as err:
                detail = f"could not fully remove {target}: {os_error_text(err)}"
                text.append(f"clean: warning: {detail}")
                issues.append(Issue("clean.remove-failed", "warning", detail))
                records.append({"path": target, "kind": "dir", "action": "remove-failed"})
            else:
                removed += 1
                records.append({"path": target, "kind": "dir", "action": "removed"})
        else:
            # The state-file unlink is NOT ignore_errors in the Python source --
            # a failure propagates to exit 1.
            try:
                os.remove(target)
            except (OSError, ValueError) as err:
                detail = f"failed to remove {target}: {os_error_text(err)}"
                issues.append(Issue("clean.remove-failed", "error", detail))
                text.append(f"clean: error: {detail}")
                exit_code = ExitCode.RUNTIME_FAILURE
                records.append({"path": target, "kind": "file", "action": "remove-failed"})
            else:
                removed += 1
                records.append({"path": target, "kind": "file", "action": "removed"})

    # Faithful trailing line -- suppressed under `--dry-run` (the Python source
    # guards it with `and not args.dry_run`) and when a hard removal error
    # already fired.
    if removed == 0 and not dry_run and exit_code == ExitCode.SUCCESS:
        text.append("clean: nothing to remove")

    return _Outcome(
        exit_code=exit_code,
        data=_report(build_root, dry_run, records, removed),
        project=project,
        sdk=sdk,
        issues=issues,
        text=text,
    )


def clean(
    app_path: str = typer.Argument(
        ".",
        metavar="APP_PATH",
        help=(
            "Application source directory (default: '.'). The build root "
            "defaults to <APP_PATH>/build; a non-'.' value overrides --project."
        ),
    ),
    build_root: str = typer.Option(
        None,
        "--build-root",
        metavar="PATH",
        help="Override the build root to remove (default: <APP_PATH>/build).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List the paths that would be removed; delete nothing."
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
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress the non-essential manifest notice."
    ),
    verbose: bool = typer.Option(False, "--verbose", hidden=True),
    no_color: bool = typer.Option(False, "--no-color", hidden=True),
    non_interactive: bool = typer.Option(False, "--non-interactive", hidden=True),
    ci: bool = typer.Option(False, "--ci", hidden=True),
    target: str = typer.Option(None, "--target", hidden=True),
    all_cores: bool = typer.Option(False, "--all", hidden=True),
) -> None:
    """Remove this project's build directory and build-state cache."""
    # The six options above are `clap`'s `GlobalArgs` members that `clean.rs`
    # accepts and never reads. Declared here purely so the argv SURFACE matches:
    # `tan clean --no-color` exits 0 on the oracle and, without these, exited 2 as
    # a Click usage error -- so a customer's `tan clean --ci` in a CI script
    # cleaned nothing. Hidden from `--help` because they do nothing.
    #
    # This gap is PORT-WIDE, not `clean`'s alone -- measured against the Rust
    # binary, `presets --no-color`, `doctor --no-color` and `sdk current --ci`
    # each still exit 2 where the oracle exits 0/4/0. Closed here rather than
    # left because `clean` is the command whose refusal means work that should
    # have been removed was not; the shared fix (one decorator for every command)
    # belongs with whoever owns the global-flag surface.
    del verbose, no_color, non_interactive, ci, target, all_cores
    json_mode = output_format == "json"

    try:
        outcome = _run(
            app_path=app_path,
            build_root_arg=build_root,
            dry_run=dry_run,
            project_arg=project,
            board_yaml_arg=board_yaml,
            sdk_root_arg=sdk_root,
            quiet=quiet,
        )
    except Exception as err:  # noqa: BLE001
        # The recurring break this guard exists for: an escaping traceback puts
        # nothing parseable on stdout and the extension renders an empty panel
        # with no error at all. CONSTANTS ONLY below -- no call to a helper that
        # could itself throw, which is how a single fault became a double fault
        # elsewhere in this port. In particular `resolve_project_paths` is NOT
        # re-run here: it reads the cwd, and a deleted cwd is one of the ways
        # `_run` can fail in the first place.
        outcome = _Outcome(
            exit_code=ExitCode.INTERNAL_FAILURE,
            data=_report("", dry_run, [], 0),
            project=Project(root=None, board_yaml=None),
            sdk=None,
            issues=[
                Issue(
                    "clean.internal-failure",
                    "error",
                    f"clean failed unexpectedly: {err}",
                )
            ],
            text=["clean: internal failure"],
        )

    if json_mode:
        emit(
            Envelope(
                "clean",
                outcome.project,
                outcome.data,
                outcome.issues,
                outcome.exit_code,
                sdk=outcome.sdk,
            )
        )
    else:
        # stdout is the envelope channel and carries nothing else, in either
        # mode; stderr carries no contract of its own.
        for line in outcome.text:
            print(line, file=sys.stderr)
    raise typer.Exit(int(outcome.exit_code))
