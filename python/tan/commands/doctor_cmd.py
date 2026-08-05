# SPDX-License-Identifier: Apache-2.0
"""`tan doctor` -- is this host actually able to build and flash?

Every check here answers a question some customer already lost an afternoon to.
Two of them exist because the answer used to be a confident, wrong "Pass".

**The Python floor is not what the manifest says it is.**
`metadata/bootstrap.json` declares `prerequisites.pythonMinVersion` (read live
below, currently `"3.10"` on alp-sdk's `dev`), while separately
Zephyr's `cmake/modules/python.cmake` sets `PYTHON_MINIMUM_REQUIRED 3.12`. And
the Rust oracle's POSIX bootstrap branch was explicit that it "cannot fail on
version" (`crates/tan-cli/src/commands/bootstrap/steps.rs:230-234`). Ubuntu 22.04
ships `python3` = 3.10. Compose the three and a fresh customer got: `tan
bootstrap` succeeds, `tan doctor` reports Pass, and the FIRST build dies inside
Zephyr's CMake configure with an error naming Zephyr, not us. So the floor this
command enforces is the EFFECTIVE one -- the higher of the manifest's and
Zephyr's -- and where the two disagree that disagreement is itself reported
(`pythonFloor`), naming which is which, so the fix lands in the manifest instead
of in the customer.

**"Zephyr's" used to mean whatever `$ZEPHYR_BASE` pointed at, not the
workspace the report was actually about (tan-cli#301).** `zephyrWorkspace`
(tan-cli#290) reads the RESOLVED west topdir (`west_workspace_dir`); until now
`hostPython`/`pythonFloor` independently re-read `$ZEPHYR_BASE`, which is
extremely commonly stale -- Zephyr's own docs, and this command's own
`tan bootstrap` next-steps block, both tell a customer to export it. One
report could then name two different Zephyrs: `zephyrWorkspace` passing
against the real workspace while `hostPython`'s floor, and the interpreter it
demanded, came from an unrelated tree the customer was not building against.
`_collect` now feeds `zephyr_python_floor` the SAME resolved `workspace_path`
`zephyrWorkspace` reports, falling back to a literal `$ZEPHYR_BASE` read only
when no workspace resolves at all, and to `ZEPHYR_PYTHON_FLOOR` when neither
does -- see `zephyr_python_floor`'s docstring for the three-way split.

`tan bootstrap` now enforces the same effective floor on BOTH platforms, by
calling `zephyr_python_floor` below rather than re-deriving it -- see
`tan.commands.bootstrap_cmd.resolve_python_floor`. Keep that the ONE reader: a
second floor rule is how the two commands come to disagree about the same host,
which is worse than either verdict alone.

**SETOOLS was never mentioned by any doctor.** Neither `alp doctor`
(`scripts/alp_cli/doctor.py` -- it has `_check_python`, `_check_west`,
`_check_jlink`, and nothing for this) nor the shipped `tan doctor` says a word
about `SETOOLS_DIR`, `SE_UART`, or the `fdt` pip package. A customer therefore
gets a clean bill of health and then meets a bare `RuntimeError` out of
`scripts/west_commands/runners/alif_flash.py` at the moment they try to flash an
AEN part. The `setools` check names all three, plus the Alif developer download
(`app-release-exec-linux-SE_FW_x.y.z`) it cannot redistribute.

**Nothing that probes may throw.** Four Criticals in this port were uncaught
exceptions escaping the error contract: a raw traceback instead of an envelope,
so the VS Code extension renders nothing at all and neither side reports an
error. `doctor` interrogates a hostile environment BY DEFINITION -- a missing
binary, an unreadable directory, a tool that waits for a probe that is not
plugged in, a subprocess that answers in bytes that are not UTF-8. Every one of
those becomes a structured issue here; `probe()` is the single choke point and
it has a timeout on every call.

**Exit 4, never 0, when unhealthy.** A doctor that exits 0 on a broken
environment is worse than no doctor: it converts a fixable setup problem into a
mystery inside somebody else's build system.

Deliberately NOT ported from `crates/tan-cli/src/commands/doctor.rs`: the debug
half (`--target-kind`/`--server`, the cortex-debug/CodeLLDB extension set).
That needs context this port has no command to produce yet, and half a debug
verdict is worse than none. The envelope keys that survive --
`data.summary.{pass,warn,fail}` and `data.checks[]` -- are the ones
`alp-sdk-vscode` actually reads (`src/debug.ts`, `src/toolchain.ts`).

**`--build` is accepted, real, and now (tan-cli#290) a no-op vs. plain `tan
doctor` -- not the Rust oracle's second, disjoint check vocabulary.**
Measured against a real `tan.exe`, plain `tan doctor` and `tan doctor
--build` run two almost entirely different check lists (debug-readiness vs.
zephyr/yocto/baremetal build-readiness -- compare `tan doctor`'s
`workspaceRoot`/`codeLLDBExtension`/`lldb` against `tan doctor --build`'s
`git`/`cmake`/`ninja`/`dtc`/`gperf`/`vendorToolchain`/...). Byte-parity with
BOTH of those lists is a second command's worth of new checks, not a flag
gap -- and this port's own check list -- `hostPython`/`hostPrerequisites`/
`west`/`zephyrSdk`/`setools`/`jlink`, plus (tan-cli#294) `sdk`/`boardYaml`/
`workspace`/`zephyrVersion`/`zephyrSdkAvailableForHost`/`longPaths`/
`homePath`/`sdkProvenance`, plus (tan-cli#290) `westResolved`/
`zephyrWorkspace` -- is ALREADY build/flash-oriented by design (see above),
unlike the Rust oracle's PLAIN `doctor`. `zephyrWorkspace` -- whether the
RESOLVED workspace's Zephyr matches alp-sdk's `west.yml` pin -- used to be
the ONE check this flag gated; ADR 0021 Lane 1 P0a runs PLAIN `tan doctor`
as the very first command a customer types, before `--build` is ever named,
so gating it there left the exact alp-sdk#855 v4.4.0->v4.4.1 drift invisible
on that first run. It is unconditional now, alongside every other
tan-cli#294/#290 fact -- `--build` therefore changes nothing about this
port's check-name set any more. The flag stays accepted rather than
removed: both `alp-sdk-vscode` call sites (`["doctor", "--build"]`,
`["doctor", "--build", "--fix"]`) still pass it, and a flag a caller already
relies on does not need to keep doing something to still be worth accepting
without error.

`--fix` is a separate, NOT-yet-ported flag gap (it is not part of this one):
the oracle's `--build --fix` auto-repairs a missing Zephyr workspace by
running `tan bootstrap`, and nothing here does that yet.
"""
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import typer

from tan.commands.build_cmd import (
    SDK_DISCOVERY_DIVERGENT,
    _abs_posix,
    discover_sdk_root,
    resolve_sdk_root_ladder,
    resolve_sdk_root_wide,
)
from tan.commands.sdk_cmd import (
    NO_SDK_NEXT_STEPS,
    _has_loader_script,
    _home_alp_dir,
    _pointer_target,
    global_default_foreign_project_issue,
    global_default_pointer_fix_hint,
    parse_sdk_version_yaml,
    project_pin_issue,
)
from tan.core.bootstrap import (
    MissingPrerequisite,
    PrereqFailure,
    WorkspaceSdkRecord,
    parse_west_zephyr_pin,
    parse_workspace_sdk_record,
    parse_zephyr_version_file,
    posix_venv_unusable,
    reported_missing,
)
from tan.core.consent import can_prompt
from tan.core.doctor_render import render_check_lines, render_doctor_footer
from tan.core.global_flags import accept_global_flags
from tan.core.timestamp import generated_at_iso
from tan.core.venv import find_workspace_venv, west_program, west_workspace_dir
from tan.env import use_color
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

#: Zephyr's own floor, from `<zephyr>/cmake/modules/python.cmake`'s
#: `set(PYTHON_MINIMUM_REQUIRED 3.12)`. Only the FALLBACK -- `zephyr_python_floor`
#: reads the real file when a workspace resolves, so a Zephyr bump raises this
#: floor on the customer's machine without waiting for a tan release.
ZEPHYR_PYTHON_FLOOR = (3, 12)

#: The floor `metadata/bootstrap.json` is assumed to declare when no manifest
#: resolves at all -- used ONLY as the `manifest_floor` input to `max()` below,
#: never as a verdict by itself. It mirrors `crate::util::MIN_PYTHON`
#: (`crates/tan-cli/src/util.rs`), which is frozen at 3.10 and does NOT track
#: `metadata/bootstrap.json` -- that Rust constant and the manifest's declared
#: `pythonMinVersion` are two independently-edited numbers, not one fact, and
#: they can and do drift apart (the manifest is mid-raise to 3.12 as of this
#: writing; the oracle constant is not). The manifest is the authority: when it
#: resolves AND declares `pythonMinVersion`, that number is read live and this
#: constant is not consulted for the verdict -- but a manifest that resolves
#: while omitting the key still falls back to this same constant (see
#: `resolve_manifest_python_floor`/`_collect` below), so this is not a
#: no-manifest-only fallback. `ZEPHYR_PYTHON_FLOOR` above still composes with
#: it via `max()` either way, so a resolvable SDK checkout with the key present
#: never depends on this value being current.
FALLBACK_PYTHON_FLOOR = (3, 10)

#: Seconds any single probe may take before it is killed. Generous enough for a
#: cold `west --version` (it imports the whole west package), short enough that
#: a J-Link binary waiting on a probe that is not plugged in cannot wedge the
#: command.
PROBE_TIMEOUT_S = 15

#: The SETOOLS executables `alif_flash.py` looks for inside `$SETOOLS_DIR`
#: (its `--app-gen-toc` / `--app-write-mram` defaults).
SETOOLS_EXECUTABLES = ("app-gen-toc", "app-write-mram")

#: The Alif developer-portal bundle `$SETOOLS_DIR` must point INTO. The `-linux`
#: is not incidental: `alif_flash.py` hard-codes `app-release-exec-linux` in the
#: refusal it raises, so this path is Linux-only in this tree.
SETOOLS_BUNDLE = "app-release-exec-linux-SE_FW_x.y.z"

#: The J-Link DLL that first shipped Alif's built-in MRAM flash loader. Below
#: this, Flow D has nothing to program MRAM with.
JLINK_MIN_DLL = (9, 46)

#: The device profile that UNLOCKS that loader. The generic `Cortex-M55` profile
#: connects fine for read/attach/RAM-run and has no MRAM loader at all, so a
#: Flow D burn against it silently is not one.
JLINK_AEN_DEVICE = "AE822FA0E5597LS0_M55_HE"

#: The Zephyr SDK release `west sdk install --version` pins. Mirrors
#: `tan_core::host_env::ZEPHYR_SDK_INSTALL_VERSION` byte-for-byte, so the
#: `zephyrSdk` check's fix hint below and the Rust oracle's own can never name
#: two different versions.
#:
#: A NEW consumer of the pin `contract/fixtures/toolchains/toolchains.json`
#: owns -- that fixture's own `_comment` states the rule verbatim: "A NEW
#: consumer of this pin needs its own parity assertion; widening this scan
#: will not reach it." `test_zephyr_sdk_install_version_matches_the_real_
#: toolchain_lock` (test_doctor_command.py) is that assertion, mirroring
#: `crates/tan-core/src/host_env.rs`'s test of the same name (tan-cli#172) --
#: without it, an alp-sdk version bump makes Rust fail loudly and this
#: constant go silently stale.
ZEPHYR_SDK_INSTALL_VERSION = "1.0.1"

#: PATH names west's `.7z` toolchain extraction (via patoolib, which shells
#: out to an external binary with no pure-Python fallback) will accept --
#: mirrors `crate::build_readiness::SEVEN_ZIP_PROGRAMS` byte-for-byte. Any ONE
#: is enough; probing only `7z` would false-negative a host that has `7zz` or
#: `unar` instead.
SEVEN_ZIP_PROGRAMS = ("7z", "7za", "7zr", "7zz", "7zzs", "unar")

#: Verified resolvable (`winget show 7zip.7zip` -> `Found 7-Zip [7zip.7zip]`,
#: publisher Igor Pavlov) -- mirrors `crate::build_readiness::
#: SEVEN_ZIP_INSTALL_COMMAND` byte-for-byte.
SEVEN_ZIP_INSTALL_COMMAND = "winget install -e --id 7zip.7zip"

#: The host platforms the pinned Zephyr SDK (`ZEPHYR_SDK_INSTALL_VERSION`
#: above) actually publishes a build for -- mirrors
#: `tan_core::host_env::ZEPHYR_SDK_HOSTS` byte-for-byte (tan-cli#294 finding
#: 1, reintroducing tan-cli#70). `windows-arm64` was never published at any
#: release; `macos-x86_64` was dropped in the 1.0.0 line the pinned SDK is
#: past. Spelled in the SDK's own release-asset tokens (`x86_64`, not `x64`).
ZEPHYR_SDK_HOSTS = ("linux-aarch64", "linux-x86_64", "macos-aarch64", "windows-x86_64")

#: Text-mode report width floor (`render_check_lines`/`render_doctor_footer`,
#: resolved once in `doctor()` before the first check streams) -- a real
#: terminal narrower than this (or a redirected/piped run whose
#: `shutil.get_terminal_size` fallback would otherwise apply) must not be
#: left to wrap one word per line.
_DOCTOR_TEXT_MIN_WIDTH = 60


@dataclass(frozen=True)
class Check:
    """One verdict. `status` is the Rust `DoctorStatus` vocabulary verbatim:
    `pass` / `warn` / `fail` / `unknown`, where `unknown` means the question was
    not askable on this host -- counted in NO summary bucket and raising no
    issue, so an unverifiable assumption is never rendered as observed fact.

    `code` overrides the default `doctor.<name>` issue code. It exists for the
    three FROZEN `bootstrap.*` codes (`contract/issue-codes.json`), which
    `alp-sdk-vscode`'s `PREREQ_CODES` matches with `Set.has()` -- an unrecognised
    code there is indistinguishable from "no problem", so the spelling is load-
    bearing and must not be re-derived from the check name.

    `missing` carries the structured per-tool form of a `hostPrerequisites`
    refusal (tan-cli#294 finding 4: `data.missingPrerequisites`) -- NOT
    serialized by `as_dict()` below, unlike every other field: it does not
    ride on the per-check JSON at all (mirroring Rust's `DoctorCheck`, which
    has no such field either), only on the report-level
    `data.missingPrerequisites` `doctor()` builds from it.
    """

    name: str
    status: str
    detail: str
    fix: str | None = None
    code: str | None = None
    missing: list[dict[str, str | None]] | None = None

    def as_dict(self) -> dict:
        out = {"name": self.name, "status": self.status, "detail": self.detail}
        # Omitted when absent, not null -- Rust's `skip_serializing_if`.
        if self.fix is not None:
            out["fix"] = self.fix
        return out


# ---------------------------------------------------------------------------
# Probing. Every subprocess and every filesystem read in this module goes
# through one of these two, and neither can raise.
# ---------------------------------------------------------------------------


def probe(argv: list[str], timeout: int = PROBE_TIMEOUT_S) -> str | None:
    """Run `argv` and return its stdout, or `None` for every way that can fail.

    `None` means "no answer", never "the answer is bad" -- callers must not read
    it as a verdict. The failure modes this swallows are all real on a fresh
    host: the binary is absent (`FileNotFoundError`), it is a directory or not
    executable (`OSError`/`PermissionError`), it waits forever on a probe that is
    not plugged in (`TimeoutExpired`), or it exits non-zero.

    `stdin` is closed, not inherited: a tool that decides to prompt then reads
    EOF and dies instead of blocking until the timeout. `errors="replace"` is
    the same reason `tests/conformance` uses it -- a tool answering in the
    platform code page must not turn into a `UnicodeDecodeError` crash that
    masquerades as a host problem.
    """
    try:
        out = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        # SubprocessError covers TimeoutExpired (the child is already killed by
        # `run`); ValueError catches an empty/garbage argv rather than letting
        # it escape as a traceback.
        return None
    return out.stdout if out.returncode == 0 else None


def on_path(command: str) -> str | None:
    """Resolve `command` against `$PATH` ONLY, returning its full path.

    NOT `shutil.which`: on Windows that inserts `os.curdir` ahead of PATH
    (documented Windows search order), so a project checked out with its own
    `west.exe`/`openocd.exe` at its root would be reported as this host's
    tooling -- and a later flow would spawn exactly that project-controlled
    binary. `crate::util::command_on_path` walks PATH by hand for this reason;
    so does this.
    """
    raw = os.environ.get("PATH") or ""
    if os.name == "nt":
        exts = [""] + [
            e
            for e in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
            if e
        ]
    else:
        exts = [""]
    for directory in raw.split(os.pathsep):
        if not directory:
            continue
        for ext in exts:
            candidate = Path(directory) / (command + ext)
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
            except OSError:
                # A PATH entry on a dead network share, a name too long for the
                # filesystem: skip the entry, never fail the command.
                continue
    return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Version floors
# ---------------------------------------------------------------------------


def _parse_two(raw: str) -> tuple[int, int] | None:
    """`"3.12"`, `"v1.2.0"`, `"West version: v1.2.0"` -> `(major, minor)`."""
    match = re.search(r"(\d+)\.(\d+)", raw)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def zephyr_python_floor(zephyr_base: str | None) -> tuple[tuple[int, int], str]:
    """The floor Zephyr's CMake will actually enforce, and where it came from.

    Read from `<zephyr_base>/cmake/modules/python.cmake` when that resolves,
    because THAT is the file whose `PYTHON_MINIMUM_REQUIRED` aborts the build --
    a constant compiled into tan goes stale the moment Zephyr bumps it, and a
    stale floor here reintroduces exactly the silent gap this command exists to
    close. `ZEPHYR_PYTHON_FLOOR` is the fallback for a host with no workspace
    yet, which is every host at `tan bootstrap` time.

    `zephyr_base` is a plain path in, not necessarily `$ZEPHYR_BASE` itself --
    THIS function has no opinion on where it came from, only `_collect` (this
    module's `hostPython`/`pythonFloor` caller) does. As of tan-cli#301,
    `_collect` passes the resolved workspace's `zephyr/` subtree -- the SAME
    `tan.core.venv.west_workspace_dir` result `zephyrWorkspace` reports -- when
    one resolved, a literal `$ZEPHYR_BASE` read only when no workspace resolved
    at all, and `None` (landing on `ZEPHYR_PYTHON_FLOOR` below) when neither
    does; that is the three-way split the resulting `source` string names. The
    OTHER caller, `tan.commands.bootstrap_cmd.resolve_python_floor`, still
    passes a literal `$ZEPHYR_BASE` read directly -- `tan bootstrap` runs before
    any workspace can have resolved, so there is nothing else for it to prefer.
    """
    if zephyr_base:
        path = Path(zephyr_base) / "cmake" / "modules" / "python.cmake"
        text = _read_text(path)
        if text is not None:
            match = re.search(r"PYTHON_MINIMUM_REQUIRED\s+(\d+)\.(\d+)", text)
            if match is not None:
                return (int(match.group(1)), int(match.group(2))), str(path)
    return ZEPHYR_PYTHON_FLOOR, (
        f"Zephyr's PYTHON_MINIMUM_REQUIRED, from tan's built-in pin "
        f"{ZEPHYR_PYTHON_FLOOR[0]}.{ZEPHYR_PYTHON_FLOOR[1]} -- no $ZEPHYR_BASE "
        f"workspace on this host to read `cmake/modules/python.cmake` from"
    )


def jlink_flash_device(sdk_root: str | None) -> tuple[str, str]:
    """The Flow-D part-number J-Link device profile, and where it came from.

    Read from `<sdk>/metadata/socs/alif/ensemble/e8.json`
    `variants[].debug.jlink_flash_device` -- the ONE variant carrying that key
    is the one with an MRAM loader profile at all; the other AE822 package
    variant's `debug` has a `jlink_device` (attach) entry but no
    `jlink_flash_device`, because it has no Flow D loader to unlock.

    `JLINK_AEN_DEVICE` is the fallback for THREE distinct causes, and the
    returned source string names WHICH one fired (tan-cli#310) -- they used
    to collapse into one sentence that only ever matched the first, so a host
    with a perfectly good SDK checkout got told "no alp-sdk checkout
    resolved" in the same envelope that reported resolving one:

    1. no `sdk_root` at all -- the honest "nothing to read from" case;
    2. `sdk_root` resolved but `e8.json` is missing, unreadable, or does not
       parse as a JSON object -- named with the exact path that was tried;
    3. `sdk_root` resolved and `e8.json` parsed fine, but no variant carries
       `debug.jlink_flash_device` -- the real state of a checkout predating
       alp-sdk#1057, which publishes this fact into a per-board `flash_args`
       value instead; doctor has no board selected to read one from, so the
       built-in constant is the honest answer, not a resolution failure.

    Every variant is checked, not just the first hit: if a future package
    variant declares a DIFFERENT `jlink_flash_device`, picking whichever
    serialises first would silently advise the wrong part with nothing to
    catch it. More than one DISTINCT value is ambiguous, not resolved -- it
    falls back to `JLINK_AEN_DEVICE` with a source that says so, rather than
    guessing.

    Never raises: a missing SDK, an unreadable or malformed `e8.json`, or no
    variant carrying the key all fall back the same way -- doctor's whole job
    is to run on a host where things are wrong.
    """
    if not sdk_root:
        return JLINK_AEN_DEVICE, (
            f"tan's built-in fallback {JLINK_AEN_DEVICE} -- no alp-sdk checkout "
            "resolved to read metadata/socs/alif/ensemble/e8.json "
            "variants[].debug.jlink_flash_device from"
        )

    path = Path(sdk_root) / "metadata" / "socs" / "alif" / "ensemble" / "e8.json"
    text = _read_text(path)
    doc = None
    if text is not None:
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
    if not isinstance(doc, dict):
        return JLINK_AEN_DEVICE, (
            f"tan's built-in fallback {JLINK_AEN_DEVICE} -- {path} is missing, "
            "unreadable, or did not parse as a JSON object, so its "
            "variants[].debug.jlink_flash_device could not be read"
        )

    found: set[str] = set()
    for variant in doc.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        debug = variant.get("debug")
        device = debug.get("jlink_flash_device") if isinstance(debug, dict) else None
        if isinstance(device, str) and device:
            found.add(device)
    if len(found) == 1:
        return next(iter(found)), str(path)
    if len(found) > 1:
        return JLINK_AEN_DEVICE, (
            f"tan's built-in fallback {JLINK_AEN_DEVICE} -- {path} "
            f"variants[].debug.jlink_flash_device carries {len(found)} "
            "DIFFERENT values across variants (ambiguous), refusing to "
            "pick one arbitrarily"
        )
    return JLINK_AEN_DEVICE, (
        f"tan's built-in fallback {JLINK_AEN_DEVICE} -- {path} parsed but no "
        "variant carries debug.jlink_flash_device; alp-sdk#1057 publishes this "
        "profile into a per-board flash_args value instead, and doctor has no "
        "board selected to read one from"
    )


def _fmt(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


# ---------------------------------------------------------------------------
# The checks. Pure: probed facts in, a verdict out.
# ---------------------------------------------------------------------------


def python_check(
    found: tuple[str, tuple[int, int]] | None, floor: tuple[int, int], floor_source: str
) -> Check:
    """`hostPython` -- is there an interpreter, and does it clear the EFFECTIVE
    floor?

    `found` is `(how it is spelled, (major, minor))` for the best candidate that
    actually RAN. `None` is not "too old", it is "nothing runs": the Microsoft
    Store `python.exe` alias satisfies any presence check and prints nothing,
    which is why the probe insists on parseable output rather than existence.
    """
    if found is None:
        return Check(
            "hostPython",
            "fail",
            "no runnable Python interpreter found -- none of `python3`/`python`"
            + (" / `py -3`" if os.name == "nt" else "")
            + " ran and reported a version.",
            "Install Python "
            + _fmt(floor)
            + "+ and put it on PATH."
            + (
                " On Windows, a `python.exe` that opens the Microsoft Store is the"
                " Store ALIAS, not an interpreter: disable it under Settings > Apps >"
                " App execution aliases, or install from python.org."
                if os.name == "nt"
                else ""
            ),
            # FROZEN (contract/issue-codes.json). Spelled, never derived.
            code="bootstrap.python-not-runnable",
        )
    binary, version = found
    if version < floor:
        return Check(
            "hostPython",
            "fail",
            f"Python {_fmt(version)} (`{binary}`) is below the effective floor "
            f"{_fmt(floor)}, which comes from {floor_source}. The build does not "
            f"fail here -- it fails later, inside Zephyr's own CMake configure, "
            f"with an error that names Zephyr rather than your Python.",
            f"Install Python {_fmt(floor)}+ and put it ahead of {_fmt(version)} on PATH, "
            f"then re-run `tan bootstrap` so the workspace venv is built with it."
            + (
                # Named because it is THE case: the distro `python3` on 22.04 is
                # 3.10, which clears the manifest floor and dies at Zephyr's
                # configure -- the exact host this check exists for.
                f" Ubuntu 22.04's distro `python3` is 3.10, so this needs a newer one: "
                f"`sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt-get install -y "
                f"python{_fmt(floor)} python{_fmt(floor)}-venv`."
                if sys.platform.startswith("linux")
                else ""
            ),
            # FROZEN (contract/issue-codes.json).
            code="bootstrap.python-too-old",
        )
    return Check(
        "hostPython",
        "pass",
        f"Python {_fmt(version)} (`{binary}`) meets the effective floor "
        f"{_fmt(floor)} ({floor_source}).",
    )


def python_floor_skew_check(
    manifest_floor: tuple[int, int],
    effective_floor: tuple[int, int],
    effective_source: str,
    manifest_is_real: bool = True,
) -> Check | None:
    """`pythonFloor` -- the two declared floors disagree.

    Reported rather than silently reconciled. A host that satisfies the higher
    floor is fine TODAY, but the manifest is the number a customer will read and
    trust, so while the skew stands the two sources disagree about which hosts
    are supported. Saying which number came from which file is the whole value.

    **Not fixed by raising the manifest (tan-cli#300).** That was tried and
    reverted -- alp-sdk#1078: `crates/tan-core/src/build_readiness.rs:401`
    pushes the Python check BEFORE any `os_set` branch ("EVERY backend's
    build-plan emission runs `alp_project.py` ... not just Zephyr's"), so
    raising the shared `pythonMinVersion` key would refuse a Yocto-only or
    metadata-only project, on a host that builds it fine today, over a floor
    that project never needs -- and the raised floor is unreachable via the
    manifest's own remedy (`sudo apt-get install -y python3`) on the Ubuntu
    22.04 hosts the docs recommend. The skew is real and known, and scoped to
    Zephyr; the fix for a Zephyr build on a below-floor host is a newer
    interpreter on THAT host (see `hostPython` above), not a manifest edit.

    `manifest_is_real` is `False` when `manifest_floor` never actually came from
    a read `metadata/bootstrap.json` -- no SDK resolved, or this SDK predates
    the manifest -- and is instead tan's own `FALLBACK_PYTHON_FLOOR` standing
    in. Callers pass `_load_manifest`'s own `ManifestLoad.is_real` verdict
    straight through -- never re-derived from `ManifestLoad.source`'s prose, so
    a future rewording of that message cannot silently flip which branch below
    fires. Misreporting that number as "alp-sdk's metadata/bootstrap.json
    declares" sends the customer to edit a file that was never consulted, so
    the wording and the fix both change for this case.

    `tan bootstrap` enforces the SAME effective floor this reports -- it calls
    `zephyr_python_floor` below with the same argument (see
    `tan.commands.bootstrap_cmd.resolve_python_floor`) and raises
    `bootstrap.python-floor-skew` with the same two numbers. Before that, the
    Rust oracle's POSIX branch enforced only the manifest's, which is how a
    3.10 host passed both commands and then died inside Zephyr's CMake
    configure.
    """
    if manifest_floor >= effective_floor:
        return None
    if manifest_is_real:
        claim = f"alp-sdk's metadata/bootstrap.json declares pythonMinVersion {_fmt(manifest_floor)}"
        fix = (
            f"Known, Zephyr-scoped skew (alp-sdk#1078) -- raising "
            f"`prerequisites.pythonMinVersion` to {_fmt(effective_floor)} was tried "
            f"and reverted, because that key also gates Yocto-only and "
            f"metadata-only projects, which do not need it. Building for Zephyr on "
            f"a host below {_fmt(effective_floor)} needs a newer interpreter -- see "
            f"the `hostPython` check above."
        )
    else:
        claim = (
            f"no alp-sdk metadata/bootstrap.json was read (no SDK checkout resolved, "
            f"or this SDK predates it), so tan's own built-in floor {_fmt(manifest_floor)} "
            f"is standing in"
        )
        fix = (
            # `tan sdk switch` refuses in this build (tan-cli#305) -- point at
            # the mechanism that actually resolves one instead.
            f"Resolve an alp-sdk checkout: {NO_SDK_NEXT_STEPS}. That checkout's "
            "own metadata/bootstrap.json pythonMinVersion is then read instead "
            "of tan's built-in floor."
        )
    return Check(
        "pythonFloor",
        "warn",
        f"{claim}, but the build's effective floor is "
        f"{_fmt(effective_floor)} (from {effective_source}). Both `tan doctor` and "
        f"`tan bootstrap` enforce the higher, effective floor, so a host this "
        f"manifest would have accepted is refused up front rather than failing "
        f"later at Zephyr's CMake configure.",
        fix,
    )


def prerequisites_check(
    checked: list[str],
    missing: list[str],
    install: dict[str, str],
    source: str,
    venv_refusal: PrereqFailure | None = None,
) -> Check:
    """`hostPrerequisites` -- the manifest's own tool list, on PATH, PLUS
    (Linux only) whether the interpreter's `venv` module can actually create
    a usable environment (tan-cli#294 finding 3, reintroducing tan-cli#161).

    Mirrors `tan_core::bootstrap::doctor_prerequisite_check`, including that
    the per-tool install commands come from the manifest rather than being
    spelled here: they are per-platform facts alp-sdk owns.

    `venv_refusal` is `posix_venv_unusable()` when `python3` is on PATH and
    ran, but its `venv` module cannot create a usable environment because
    `ensurepip` is missing -- the Debian/Ubuntu `python3-venv` package split.
    Before this, `tan doctor` probed bare PATH presence and never
    `ensurepip`, so it passed on a host that then died at `tan bootstrap`
    time. `venv_refusal.missing` (`{tool: "python3-venv", command: ...}`)
    folds into this check's own `missing` field alongside any tool-presence
    entries, so one `data.missingPrerequisites` list (finding 4) carries
    both failure shapes -- never two.
    """
    entries = tuple(MissingPrerequisite(tool, install.get(tool)) for tool in missing)
    if venv_refusal is not None:
        entries = entries + venv_refusal.missing
    missing_data = reported_missing(entries)

    if missing:
        commands = [install[tool] for tool in missing if tool in install]
        return Check(
            "hostPrerequisites",
            "fail",
            f"missing from PATH: {', '.join(missing)} ({source}).",
            (
                "Install the missing prerequisites, then run `tan bootstrap`."
                + ("  " + "; ".join(commands) if commands else "")
            ),
            # FROZEN (contract/issue-codes.json).
            code="bootstrap.prerequisites-missing",
            missing=missing_data,
        )
    if venv_refusal is not None:
        return Check(
            "hostPrerequisites",
            "fail",
            f"{' '.join(venv_refusal.lines)} ({source}).",
            "Install the missing prerequisites, then run `tan bootstrap`.",
            code=f"bootstrap.{venv_refusal.code}",
            missing=missing_data,
        )
    return Check(
        "hostPrerequisites", "pass", f"{', '.join(checked)} present ({source})."
    )


def _posix_venv_capable(argv: list[str]) -> bool:
    """Whether `argv`'s Python can create a USABLE virtual environment
    (tan-cli#161). `python -m venv --help` cannot tell -- argparse answers
    before `ensurepip` is ever touched -- so this probes the real
    dependency: `import ensurepip`, which fails fast on the Debian/Ubuntu
    split where `python3-venv` is a separate, unmet package.

    Fails OPEN, not closed (tan-cli#294 review): `True` both when the probe
    ran and exited 0, AND when it could not be launched at all (bogus argv,
    spawn failure, signal death) -- mirrors `crate::util::
    python_venv_capable`'s `.output().map(|out| out.status.success())
    .unwrap_or(true)` verdict, not only its probed command; the real `python
    -m venv` a moment later surfaces its own error if something is genuinely
    wrong. Only a probe that actually RAN and exited non-zero refuses the
    host.

    NOT built on this file's own `probe()`: `probe()` collapses "ran and
    exited non-zero" and "could not run at all" to the same `None`, and
    those two outcomes need OPPOSITE verdicts here.
    """
    try:
        result = subprocess.run(
            [*argv, "-c", "import ensurepip"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return True
    return result.returncode == 0


def west_check(
    found: str | None,
    version: tuple[int, int] | None,
    floor: tuple[int, int] | None,
    resolved: str | None = None,
) -> Check:
    """`west` -- present on BARE PATH, or resolvable through the SAME
    resolver `westResolved` uses (`tan.core.venv.west_program`).

    **Now consults the resolver (tan-cli#299 second half).** This docstring
    used to argue the opposite:

        Does NOT assert that the venv resolved one: this check cannot see
        what `westResolved` found... Name the authority instead of
        predicting its answer.

    That was deliberate at the time: `found` (bare PATH) was this check's
    ONLY signal, so a hard `fail` here on the default post-bootstrap state --
    `tan bootstrap` deliberately does NOT put `west` on PATH; its own
    next-steps text tells the user to activate the venv afterwards -- was a
    false, exit-4 refusal of a host that provably builds. Measured on the
    published v0.5.0-rc2 binary: `tan build` produced real ELFs through the
    resolved venv `west` while this check alone reported the host broken.
    Downgrading that `fail` to `warn` (this file's other, earlier change on
    this branch) fixed the exit code, but the warning still fires on every
    correctly-bootstrapped host's very first `tan doctor` -- and a warning
    that fires on every correct install trains users to ignore warnings,
    which is the same defect as the false `fail`, one severity down
    (hkngln, tan-cli#299).

    So this now takes `resolved`: the SAME absolute venv path `westResolved`
    already computed via `tan.core.venv.west_program` -- never a second,
    independent probe of its own (`tool_in_venv` already confirmed that file
    exists before `westResolved` ever saw it) -- and reports `pass`, naming
    it, when bare PATH lacks `west` but the resolver found one. A bare-PATH
    probe that cannot see the venv was never a second opinion; it was a
    worse one. It now defers to the real one instead of contradicting it.

    **Still never the FAIL owner.** When `resolved` is ALSO `None` -- west
    absent from PATH and unresolvable anywhere -- this stays `warn`, not
    `fail`. That severity belongs to `westResolved` alone (below), by
    tan-cli#123's one-version-per-check contract applied to severity: making
    both checks fatal on the same absent-everywhere fact is the two-owners
    bug tan-cli#123 closed, and reintroducing it is exactly what let west
    absent everywhere exit 0 the one time this branch made BOTH checks
    non-fatal at once, before `west_resolved_check` was raised back to
    `fail`. Keeping `west` a `warn` even in that state is what lets
    `westResolved` be the sole, unambiguous reason `tan doctor` exits 4 on a
    genuinely unbuildable host.

    Only WARN on an old or unreadable version too -- west is forward-
    compatible in practice and refusing a host on a version string we could
    not parse is a worse failure than letting the real invocation report its
    own.
    """
    if found is None:
        if resolved is not None:
            return Check(
                "west",
                "pass",
                f"`west` is not on bare PATH, but resolves through the workspace "
                f"venv: {resolved} -- the same binary `westResolved` above "
                f"reports, and the one a real build actually spawns. This is the "
                f"default state right after `tan bootstrap`, which deliberately "
                f"does not put `west` on PATH; activating the venv (its "
                f"`bin`/`Scripts` directory holds the `west` launcher) would "
                f"additionally put it on bare PATH, for tools that spawn it "
                f"directly rather than through tan.",
            )
        return Check(
            "west",
            "warn",
            # Does NOT assert that the venv resolved one: `resolved` above
            # already covers that case with a `pass`, so reaching here means
            # it is genuinely `None` too -- PATH absence on its own, with
            # nothing for the resolver to find either. Name the authority
            # instead of predicting its answer.
            "`west` is not on bare PATH. `westResolved` above is the check that "
            "answers whether a build slice can run -- it reports the binary one "
            "would actually execute. PATH absence on its own is the normal state "
            "before the workspace venv is activated in this shell.",
            "If `westResolved` above also could not resolve one, run `tan "
            "bootstrap`; otherwise activate the workspace venv (its `bin`/`Scripts` "
            "directory holds the `west` launcher) so tools invoked directly find it "
            "too.",
        )
    if version is None:
        return Check(
            "west",
            "warn",
            f"`west` found at {found} but `west --version` produced nothing this "
            f"command could parse.",
            "Run `west --version` by hand; a west that cannot report its version "
            "usually cannot run either.",
        )
    if floor is not None and version < floor:
        return Check(
            "west",
            "warn",
            f"west {_fmt(version)} ({found}) is older than the {_fmt(floor)} floor "
            f"alp-sdk's metadata/bootstrap.json pins.",
            "Upgrade inside the workspace venv: `pip install --upgrade west`.",
        )
    return Check("west", "pass", f"west {_fmt(version)} ({found}).")


def west_resolved_check(found: str | None, version: tuple[int, int] | None) -> Check:
    """`westResolved` -- is `west` resolved through the WORKSPACE VENV
    (`tan.core.venv.west_program`), not bare PATH (tan-cli#123/#290)?

    Distinct from `west` above, which probes `on_path("west")` ONLY: on a
    host where the workspace venv holds `west` but PATH does not -- the
    normal GUI-launched-editor state, `tan.core.venv`'s own module
    docstring -- `west` reports failing while a real build succeeds through
    the venv binary. `westResolved` verifies the SAME binary a build would
    actually run, and `version` (when probed) MUST come from that identical
    resolution -- never a second, bare-PATH re-probe. Mirrors
    `tan_core::preflight::build_preflight_checks`'s `westResolved`
    (`west_available`) check, unconditional in BOTH doctor modes exactly like
    `sdk`/`workspace` beside it (`crates/tan-cli/src/commands/doctor.rs:1828`
    asserts all three together in the plain fold).

    **FAIL when west resolves nowhere.** This used to be a Warn, justified by
    "`west` above already fails outright on a totally-absent west, so this is
    the narrower, additive fact". tan-cli#299 removed that Fail -- correctly,
    because bare PATH is the wrong question -- and thereby falsified the
    premise this severity rested on. Measured on a real host with `west.exe`
    renamed out of the venv and absent from PATH:

        westResolved  warn   west not resolved through the workspace venv or PATH
        west          warn   ... every build slice actually resolves it through the venv
        12 passed, 4 warning(s), 0 failed.        EXIT=0

    Exit 0 on a host where nothing can execute a single slice, and `west`'s
    text asserting the venv resolves it while THIS check says it does not. A
    false refusal was traded for a false pass, which is the worse of the two.

    So the pair now splits cleanly: `west` answers "is it on bare PATH" and is
    never fatal (an unactivated venv is the normal post-bootstrap state);
    `westResolved` answers "can a build slice run at all" and is fatal when the
    answer is no. Exactly one of them owns the exit code, which is tan-cli#123's
    one-version-per-check contract applied to severity.
    """
    if found is None:
        return Check(
            "westResolved",
            "fail",
            "west resolved neither through the workspace venv nor PATH -- no build "
            "slice can be executed. Run `tan bootstrap` to create the workspace venv.",
            "tan bootstrap",
        )
    if version is None:
        return Check("westResolved", "pass", f"west resolved: {found}.")
    return Check("westResolved", "pass", f"west {_fmt(version)} resolved: {found}.")


def zephyr_sdk_install_command() -> str:
    """The exact `west sdk install` invocation the `zephyrSdk` check's `fix`
    names -- the ONE place it is assembled, mirroring
    `tan_core::zephyr_sdk_install_command` verbatim. `tan bootstrap`'s own
    "Next steps" text (`tan.core.bootstrap`) already promises "the `tan
    doctor` above reports it, and names the exact install command"; this is
    what makes that promise true rather than a second, independently-worded
    copy able to drift from it.
    """
    return f"west sdk install --version {ZEPHYR_SDK_INSTALL_VERSION} -t arm-zephyr-eabi"


def zephyr_sdk_check(detected: bool, env_dir: str | None = None) -> Check:
    """`zephyrSdk` -- is the Zephyr SDK cross toolchain (`arm-zephyr-eabi`)
    actually installed on this host? Ports `tan_core::zephyr_sdk_toolchain_check`
    / `append_zephyr_sdk_toolchain` (tan-cli#160), closing tan-cli#286: this
    port had NO such check at all, so on a host with no Zephyr SDK `tan
    doctor` reported "3 passed, 2 warning(s), 0 failed" and never used the
    word "toolchain" -- the exact alp-sdk#855 fresh-host gap #160 closed in
    the Rust oracle, reintroduced here.

    UNCONDITIONAL -- called from `_collect` regardless of `--build`, a
    `board.yaml`, or an SDK checkout resolving. This is a HOST fact (an env
    var / a scanned install dir), and ADR 0021 Lane 1 P0a runs `tan doctor`
    as the very first command a customer runs, before anything project-shaped
    exists. A Yocto-only project still gets a real `fail` here -- that host
    genuinely has no Zephyr SDK -- not a skip for lacking a Zephyr core.

    `env_dir` is the raw `ZEPHYR_SDK_INSTALL_DIR` value (or `None`), carried
    only to word the Fail detail correctly: "ZEPHYR_SDK_INSTALL_DIR unset" is
    true only when the variable really is unset. It used to be hardcoded even
    when the variable WAS set and simply named a directory with no working
    toolchain in it -- the exact stale-var case `_zephyr_sdk_detected` guards
    against -- so a customer who greps their own environment and finds it set
    disbelieved a diagnostic that was actually correct.

    Paired with `seven_zip_check` on Windows (`_collect`, gated `os.name ==
    "nt" and not detected` -- mirroring `crate::build_readiness`'s exact
    `probe.is_windows && !probe.zephyr_sdk` gate, tan-cli#204): the `west sdk
    install` this Fail's fix names cannot complete on native Windows without
    7-Zip on PATH (`tan.core.bootstrap`'s `manual_install_windows` prose), so
    this Fail's advice is only actionable together with that check.
    """
    if detected:
        return Check("zephyrSdk", "pass", "Zephyr SDK toolchain detected.")
    where = (
        f"ZEPHYR_SDK_INSTALL_DIR=`{env_dir}` does not contain a working toolchain"
        if env_dir
        else "ZEPHYR_SDK_INSTALL_DIR unset"
    )
    # tan-cli#424: on POSIX that command needs `file(1)`, which NOTHING else
    # tells the customer about -- the SDK's own host-tools step dies with
    # "ERROR: Host tools installation failed" / "FATAL ERROR: command
    # `<sdk>/setup.sh -t arm-zephyr-eabi -h` failed", naming neither the tool
    # nor a remedy. Isolated to that single variable in a pristine
    # ubuntu:24.04: identical bootstrapped workspace, identical HOME, `file`
    # the only difference -- WITH it `west sdk install` exits 0 ("All done"),
    # WITHOUT it exits 1 with those two lines.
    #
    # Named HERE rather than added to the manifest's `prerequisites`, because
    # `tan bootstrap` genuinely does not need it and succeeds without it --
    # promoting it there would refuse hosts over a tool the bootstrap never
    # uses. This is the message immediately preceding the failure, which is
    # where it is actionable. Same shape as `seven_zip_check` above: a
    # host-tool prerequisite of `west sdk install` itself, surfaced beside the
    # command that needs it rather than folded into the bootstrap gate.
    host_tool_note = (
        ""
        if os.name == "nt"
        else " On Linux/macOS the SDK's host-tools step also needs `file` on PATH"
        " (Debian/Ubuntu: `sudo apt-get install -y file`); without it the install"
        ' fails with "Host tools installation failed" and names nothing.'
    )
    return Check(
        "zephyrSdk",
        "fail",
        f"Zephyr SDK toolchain not detected ({where}) -- from "
        f"an initialised west workspace, run `{zephyr_sdk_install_command()}`.",
        f"Install the Zephyr SDK toolchain (arm-zephyr-eabi, version "
        f"{ZEPHYR_SDK_INSTALL_VERSION}): from an initialised west workspace, run "
        f"`{zephyr_sdk_install_command()}`.{host_tool_note} Details: "
        "https://docs.zephyrproject.org/latest/develop/toolchains/zephyr_sdk.html",
    )


def seven_zip_check(found: bool) -> Check:
    """`sevenZip` -- Windows-only, and only while `zephyrSdk` is failing (see
    `_collect`'s gate). Ports the Rust oracle's sibling check (`crate::
    build_readiness`, tan-cli#204): `west sdk install`, the remedy
    `zephyr_sdk_check` names, extracts the `.7z` toolchain archive by
    delegating to `patoolib`, which shells out to one of `SEVEN_ZIP_PROGRAMS`
    and has no pure-Python fallback -- documented in this repo's own
    `tan.core.bootstrap` (`manual_install_windows` prose) but, until this
    check, reaching no JSON consumer, so `alp-sdk-vscode` had no way to
    surface it and a customer who followed the `zephyrSdk` fix hint alone hit
    a patoolib error naming no Alp surface and no mention of 7-Zip.

    `Warn`, not `Fail`, mirroring the oracle: a host that already has the SDK
    never reaches this (the gate), and among hosts that do not, missing
    7-Zip blocks the REMEDY, not the build itself -- `zephyrSdk` is the
    `Fail` that stops things.
    """
    if found:
        return Check(
            "sevenZip",
            "pass",
            "7-Zip is available -- `west sdk install` can extract the toolchain.",
        )
    programs = ", ".join(SEVEN_ZIP_PROGRAMS)
    return Check(
        "sevenZip",
        "warn",
        f"No 7-Zip on PATH (looked for {programs}) -- `west sdk install` extracts "
        "the toolchain with patoolib, which shells out to one of these and has no "
        "pure-Python fallback, so it will fail on native Windows. Install it with "
        f"`{SEVEN_ZIP_INSTALL_COMMAND}`.",
        f"Install 7-Zip before running `west sdk install`: `{SEVEN_ZIP_INSTALL_COMMAND}`.",
    )


def zephyr_workspace_check(workspace_dir: str, version_text: str | None) -> Check:
    """`zephyrWorkspace` -- unconditional now, not `--build`-only
    (tan-cli#290): does the RESOLVED workspace's `zephyr/` subtree actually
    look like a Zephyr checkout at all?

    `workspace_dir`/`version_text` are the SAME
    `tan.core.venv.west_workspace_dir`-resolved facts `workspace`/
    `zephyrVersion` above already compute -- not a second, independent
    `$ZEPHYR_BASE` env-var read, which was tan-cli#294's own complaint about
    this check ("probes an env var, not the resolved topdir"). Callers only
    reach this once a workspace has actually resolved: `workspace` above
    already fails outright on a totally-absent one, and re-warning that same
    absence here under a second name would be exactly the one-fact-twice
    duplication this file's `boardYaml` handling (mirroring the Rust oracle)
    already refuses to do -- so there is no "unresolved" branch here at all.

    **No Fail branch (tan-cli#295 review, reversing tan-cli#290's own
    addition of one).** A version-mismatch Fail was added to mirror Rust's
    `crates/tan-core/src/preflight.rs:118-145` (tan-cli#98/#159, the
    alp-sdk#855 v4.4.0->v4.4.1 incident, where a drifted checkout reported
    `11 passed, 6 warnings, 0 failed` and the very next build broke) -- but
    `zephyr_version_preflight_check` above already reports that identical
    fact, from these identical two inputs (`workspace_version`/`sdk_pin`), at
    Fail severity. This check's would-be Fail condition was a strict SUBSET
    of that one, so it could never fire without `zephyrVersion` having
    already reported it under a different code: measured on a drifted host,
    `summary.fail` came out 5 instead of 4, both `doctor.zephyrVersion` and
    `doctor.zephyrWorkspace` present, and two `nextSteps` strings for the one
    `tan bootstrap` remedy. Removed rather than kept "in step" with it --
    Rust's own `crates/tan-cli/src/commands/doctor.rs` drops its comparable
    `boardYaml` duplicate for the identical reason ("emitting both would
    report one fact twice"), and `grep -rn "zephyrWorkspace" crates/` is
    empty: there is no Rust oracle row here for a version-mismatch Fail to
    stay parallel with.

    An unreadable `zephyr/VERSION` stays `Warn`: neither the Rust oracle nor
    `zephyr_version_preflight_check` above (which silently SKIPS rather than
    fails when the version is unknown -- "don't nag when this cannot
    actually be verified") treats this as more than that, and a resolved
    `.west` workspace mid-`west update` -- `zephyr/` not yet cloned -- is a
    legitimate, working-in-progress host state, not a proven blocker. This is
    the one fact `zephyrVersion` cannot see at all (it skips outright), so it
    is this check's whole remaining reason to exist.
    """
    if version_text is None:
        return Check(
            "zephyrWorkspace",
            "warn",
            f"workspace at `{workspace_dir}` does not look like a Zephyr checkout "
            f"(no readable zephyr/VERSION file).",
            "Run `tan bootstrap`, or point the workspace at a real Zephyr checkout.",
        )
    return Check(
        "zephyrWorkspace", "pass", f"Zephyr {version_text} at `{workspace_dir}`."
    )


def setools_check(
    setools_dir: str | None, se_uart: str | None, has_fdt: bool, is_linux: bool
) -> Check:
    """`setools` -- can this host flash an Alif AEN part's MRAM at all?

    Nothing else in either doctor asks. `scripts/west_commands/runners/
    alif_flash.py` raises a bare `RuntimeError` for each of these the moment a
    customer runs `west flash`, so the first time they learn is at the bench.

    WARN, not FAIL: this is one flow, on one SoM family. A customer building for
    a V2N or native_sim never touches it, and a `fail` here would exit 4 on a
    perfectly healthy host. `unknown` off Linux -- `alif_flash.py` hard-codes
    `app-release-exec-linux`, so there is no verdict to give a native
    Windows/macOS host, and `unknown` is counted in no summary bucket.
    """
    if not is_linux and not setools_dir and not se_uart:
        return Check(
            "setools",
            "unknown",
            "AEN MRAM flashing over the SE-UART is Linux-only in this tree: the "
            f"Alif Security Toolkit bundle is `{SETOOLS_BUNDLE}` and "
            "scripts/west_commands/runners/alif_flash.py hard-codes "
            "`app-release-exec-linux`. Nothing to check on this host -- run the "
            "flash from WSL2/Linux (Windows hosts pass the SE-UART through with "
            "usbipd), or use the J-Link Flow D path below.",
        )

    problems: list[str] = []
    if not setools_dir:
        problems.append(
            "$SETOOLS_DIR is unset (the Alif Security Toolkit is license-gated and "
            "NOT redistributed by alp-sdk)"
        )
    else:
        root = Path(setools_dir)
        absent = []
        for exe in SETOOLS_EXECUTABLES:
            try:
                if not (root / exe).is_file():
                    absent.append(exe)
            except OSError:
                absent.append(exe)
        if absent:
            problems.append(
                f"$SETOOLS_DIR=`{setools_dir}` does not look like an "
                f"app-release-exec-linux directory (no {', '.join(absent)})"
            )
    if not se_uart:
        problems.append(
            "$SE_UART is unset (the SE-UART device: Linux /dev/ttyUSB*, macOS "
            "/dev/cu.usbserial-*, a passed-through COM under WSL)"
        )
    if not has_fdt:
        problems.append(
            "the `fdt` Python package is not importable (app-gen-toc needs it; it "
            "is not a Zephyr requirement, so bootstrap never installs it)"
        )

    if not problems:
        return Check(
            "setools",
            "pass",
            f"SETOOLS ready: $SETOOLS_DIR=`{setools_dir}` has "
            f"{'/'.join(SETOOLS_EXECUTABLES)}, $SE_UART=`{se_uart}`, `fdt` importable.",
        )
    return Check(
        "setools",
        "warn",
        "AEN MRAM flashing (`west flash`, the alif_flash runner) will fail: "
        + "; ".join(problems)
        + ".",
        f"Download the Alif Security Toolkit (`{SETOOLS_BUNDLE}`) from the Alif "
        f"developer portal -- it is license-gated and alp-sdk does not "
        f"redistribute it -- then `export SETOOLS_DIR=<...>/app-release-exec-linux`, "
        f"`export SE_UART=/dev/ttyUSB0` (your SE-UART device), and `pip install fdt` "
        f"into the workspace venv. See docs/aen-bench-bringup.md.",
    )


def jlink_check(
    found: str | None,
    version: tuple[int, int] | None,
    device: str = JLINK_AEN_DEVICE,
    device_source: str | None = None,
) -> Check:
    """`jlink` -- Flow D, the day-to-day burn path (J-Link direct MRAM flash over
    SWD, ~0.16 s, no SE-UART).

    Three facts a presence check alone would hide, so all three travel in the
    message even when the binary is there: the loader is built into the J-Link
    DLL from V9.46 (nothing separate to install, and nothing at all below it),
    it is unlocked ONLY by the part-number device profile -- the generic
    `Cortex-M55` connects fine and has no MRAM loader, so a burn against it
    silently is not one -- and the probe needs matched V13 firmware or the
    part-number device will not connect. The last two are not host-probeable,
    which is exactly why they must be said.

    `device` defaults to `JLINK_AEN_DEVICE` so every existing call site keeps
    working; `_collect` passes the metadata-resolved value from
    `jlink_flash_device` instead, when an SDK checkout resolved one.

    `device_source` (also from `jlink_flash_device`) is surfaced into the
    detail text when given, so the same `device` string is not byte-identical
    whether it came from a resolved SDK checkout or tan's built-in fallback --
    otherwise a user on a host where the SDK did not resolve has no way to
    tell which one they are looking at.
    """
    requirements = (
        f"Flow D needs the `{device}` part-number device profile (NOT the "
        f"generic `Cortex-M55`, which has no MRAM loader), a J-Link DLL "
        f"V{_fmt(JLINK_MIN_DLL)}+, and a probe on matched J-Link V13 firmware."
    )
    if device_source is not None:
        requirements += f" Device profile resolved from: {device_source}."
    if found is None:
        return Check(
            "jlink",
            "warn",
            "SEGGER J-Link tools are not on PATH (optional -- needed for Flow D "
            "MRAM flash and SWD debug, not for native_sim or SE-UART flashing). "
            + requirements,
            "Install the SEGGER J-Link Software & Documentation Pack "
            f"(V{_fmt(JLINK_MIN_DLL)} or newer) and update the probe to V13 firmware.",
        )
    if version is None:
        return Check(
            "jlink",
            "warn",
            f"J-Link tools found at {found} but their version could not be read, so "
            f"the Flow D MRAM loader could not be confirmed. " + requirements,
            "Run `JLinkExe -?` by hand and confirm the banner reports "
            f"V{_fmt(JLINK_MIN_DLL)} or newer.",
        )
    if version < JLINK_MIN_DLL:
        return Check(
            "jlink",
            "warn",
            f"J-Link V{_fmt(version)} ({found}) predates V{_fmt(JLINK_MIN_DLL)}, which "
            f"is where Alif's MRAM flash loader became built in -- Flow D has nothing "
            f"to program MRAM with on this DLL. " + requirements,
            f"Upgrade the SEGGER J-Link pack to V{_fmt(JLINK_MIN_DLL)}+ and put the "
            f"probe on matched V13 firmware.",
        )
    return Check(
        "jlink", "pass", f"J-Link V{_fmt(version)} ({found}). " + requirements
    )


# ---------------------------------------------------------------------------
# Host-environment checks (tan-cli#294 finding 1, reintroducing tan-cli#70).
#
# `zephyr_sdk_check` above only answers "is a Zephyr SDK installed HERE" --
# never "CAN one be installed on this machine at all". A Windows-on-ARM or
# Intel-Mac host is served by neither a native Zephyr SDK build nor (on
# macOS) a WSL2 fallback, and `zephyrSdkAvailableForHost` below is the ONLY
# check that says so; `zephyrSdk`'s Fail just points at a `west sdk install`
# that can never complete there. Unconditional, like `zephyr_sdk_check`: a
# HOST fact needing no board.yaml/workspace/SDK, so it runs on plain
# `tan doctor` (ADR 0021 Lane 1 P0a runs that BEFORE anything project-shaped
# exists).
# ---------------------------------------------------------------------------


def zephyr_sdk_host_check(host_os: str, arch: str) -> Check:
    """`zephyrSdkAvailableForHost` -- mirrors
    `tan_core::host_env::zephyr_sdk_host_check` byte-for-byte, including the
    two DIFFERENT remedies for the two unserved hosts: a Windows-on-ARM host
    has a first-class route (WSL2, which reports as the served
    `linux-aarch64`), a macOS host does not (Rosetta translates x86_64 FOR
    Apple silicon, not the reverse, and there is no WSL2 equivalent) --
    collapsing the two into one message would send an Intel Mac owner
    chasing a `wsl --install` that does not exist on their OS.

    `Fail`, not `Warn`: this is the one check in the trio that means "the
    toolchain cannot run here at all", the same category as a missing
    `ninja` (`hostPrerequisites`'s own `Fail`) -- there is no artifact for
    `west sdk install` to fetch, and no amount of PATH or workspace fixing
    changes that.
    """
    tag = f"{host_os}-{arch}"
    if tag in ZEPHYR_SDK_HOSTS:
        return Check(
            "zephyrSdkAvailableForHost",
            "pass",
            f"The Zephyr SDK publishes a host build for {tag}.",
        )
    served = ", ".join(ZEPHYR_SDK_HOSTS)
    if tag == "windows-aarch64":
        detail = (
            f"Windows on ARM ({tag}, `windows-arm64` in Zephyr's own naming): the Zephyr "
            f"SDK has never published a host build for it. Served hosts are {served}. A "
            "native Windows build cannot be provisioned on this machine."
        )
        fix = (
            "Build inside WSL2 instead: install a WSL2 Linux distribution "
            "(`wsl --install`), then run `tan bootstrap` and `tan build` from inside it -- "
            "a WSL2 distro on this hardware is linux-aarch64, which the Zephyr SDK does "
            "publish."
        )
    elif tag == "macos-x86_64":
        detail = (
            f"Intel Mac ({tag}): the Zephyr SDK published this host through 0.17.4 and "
            f"dropped it in 1.0.0; the pinned SDK serves {served} only. macos-aarch64 is "
            "not a substitute -- Rosetta translates x86_64 for Apple silicon, not the "
            "reverse -- and macOS has no WSL2 equivalent to fall back to."
        )
        fix = (
            "Build on a Linux host: a linux-x86_64 VM or container on this Mac, or a "
            "remote Linux builder. Pinning an older Zephyr SDK is not an option -- the "
            f"pinned Zephyr requires {ZEPHYR_SDK_INSTALL_VERSION}, which is past the "
            "release that dropped macos-x86_64."
        )
    else:
        detail = f"The Zephyr SDK publishes no host build for {tag}. Served hosts are {served}."
        fix = f"Build on one of {served} -- natively, or in a VM/container on this machine."
    return Check("zephyrSdkAvailableForHost", "fail", detail, fix)


def _enable_long_paths_fix(key: str) -> str:
    """The elevated one-liner that sets `LongPathsEnabled` -- shared by every
    `long_paths_check` arm that names it, so the command cannot drift between
    them."""
    return (
        "Enable long paths in an ELEVATED PowerShell, then reopen your shell and VS "
        f"Code so new processes pick it up: New-ItemProperty -Path '{key}' -Name "
        "LongPathsEnabled -Value 1 -PropertyType DWORD -Force"
    )


#: Fix #3 in tan-cli#306: the remedy must name this EXACT command, verbatim
#: and runnable, no elevation needed (unlike `_enable_long_paths_fix`, which
#: touches `HKLM`) -- the cheaper fix, and the one that unblocks the actual
#: reported failure (`west update`'s own `git` calls).
_GIT_LONG_PATHS_FIX = "Enable it in git: git config --global core.longpaths true"


def long_paths_check(registry_enabled: bool | None, git_core_longpaths: bool | None) -> Check:
    """`longPaths` -- Windows only. Mirrors
    `tan_core::host_env::long_paths_check`.

    Two independent axes, and conflating them into one is exactly the defect
    tan-cli#306 reports. `LongPathsEnabled` (the registry) governs manifested
    Win32 API calls (CMake, Ninja, a plain file open); it does nothing for
    git, which refuses any path past its own limit unless ITS OWN
    `core.longpaths` is set, regardless of the registry. `west update`
    clones/checks out every Zephyr module with `git`, so on a fresh `HOME`
    (no global `.gitconfig` -- a first-run customer's exact state) the
    registry read alone reported `pass` while `west update` died on
    `hal_nxp`'s `tf-psa-crypto` vendor tree with "Filename too long".

    **`Fail`, not `Warn`, exactly when the registry reads enabled and git's
    does not.** That combination is not a probability the way a bare
    disabled registry flag is: `west update` runs `git`, `git` is the first
    thing in the whole toolchain to touch a long path, and its own setting
    says no -- the break is certain. Anything softer here would repeat the
    exact defect this check exists to fix.

    **`Warn`, not `Fail` or `Pass`, when git is set but the registry is
    not.** Git manages long paths on its own once `core.longpaths=true` (it
    prefixes paths with `\\\\?\\` internally and never consults the
    registry), so the specific failure this check exists to catch will not
    reproduce -- but `LongPathsEnabled` still governs every OTHER manifested
    tool in the chain, so real residual risk remains.

    **`Warn` when neither is set** -- the original, pre-#306 severity for a
    bare disabled registry flag: workspace-root-depth-dependent, not
    certain.
    """
    key = r"HKLM\SYSTEM\CurrentControlSet\Control\FileSystem"
    registry_on = registry_enabled is True
    git_on = git_core_longpaths is True

    if registry_enabled is True:
        registry_detail = f"{key}\\LongPathsEnabled = 1"
    elif registry_enabled is False:
        registry_detail = f"{key}\\LongPathsEnabled is 0 or unset"
    else:
        registry_detail = f"{key}\\LongPathsEnabled could not be read"

    if git_core_longpaths is True:
        git_detail = "git core.longpaths is true"
    elif git_core_longpaths is False:
        git_detail = "git core.longpaths is unset or false"
    else:
        git_detail = "git core.longpaths could not be determined"

    if registry_on and git_on:
        status = "pass"
        headline = "Windows long paths are enabled at both the OS level and in git."
        fix = None
    elif registry_on and not git_on:
        status = "fail"
        headline = (
            "Windows reports long paths enabled, but git does not honour that flag: git "
            "has its own core.longpaths and refuses paths past its limit without it, "
            "regardless of the registry. west update runs git, so bootstrap WILL fail on "
            "a long Zephyr module path (e.g. hal_nxp's tf-psa-crypto vendor tree) even "
            "though this host looks fine."
        )
        fix = _GIT_LONG_PATHS_FIX
    elif git_on:
        status = "warn"
        headline = (
            "git's own core.longpaths is set, so west update's git operations are safe. "
            "Windows' LongPathsEnabled is not, though, and every OTHER tool in the build "
            "chain (CMake, Ninja, plain Win32 file APIs) relies on it -- a sufficiently "
            "deep workspace can still cross MAX_PATH outside of git."
        )
        fix = _enable_long_paths_fix(key)
    else:
        status = "warn"
        headline = (
            "Neither Windows' LongPathsEnabled nor git's core.longpaths is set. A Zephyr "
            "build/ tree nests deep enough to cross the 260-character MAX_PATH limit, and "
            'it surfaces as a git "Filename too long" error during west update, or a '
            "CMake/compiler error about a file that exists."
        )
        fix = f"{_GIT_LONG_PATHS_FIX}\n{_enable_long_paths_fix(key)}"

    return Check("longPaths", status, f"{headline} ({registry_detail}; {git_detail}).", fix)


def home_path_check(home: str | None) -> Check:
    """`homePath` -- does the home directory contain a space? Mirrors
    `tan_core::host_env::home_path_check`.

    `Warn`, not `Fail`: a space in `C:\\Users\\Jane Doe` is a real historical
    Zephyr breakage (unquoted paths through CMake/west/Kconfig), but most of
    the chain quotes correctly now and plenty of hosts with a space build
    fine -- degraded-but-usable, not a host the toolchain cannot run on at
    all. `Fail` here would exit 4 for every user whose Windows account name
    is two words.

    All platforms, not Windows-only: a POSIX `/home/jane doe` breaks the same
    way -- Windows is merely where `%USERPROFILE%` is derived from a display
    name the user never chose.
    """
    if home is None:
        return Check(
            "homePath",
            "warn",
            "Could not resolve the home directory (neither USERPROFILE nor HOME is set).",
            "Set HOME (or USERPROFILE on Windows) -- tan resolves ~/.alp for the SDK cache "
            "and the global default-SDK pointer from it.",
        )
    if " " in home:
        return Check(
            "homePath",
            "warn",
            f"Home directory contains a space: {home}. Zephyr's CMake/west/Kconfig chain "
            "has historically broken on unquoted paths, and a workspace created under it "
            "inherits the space.",
            "Create the workspace at a space-free path (e.g. C:\\alp or /opt/alp) and run "
            "tan from there with --project, rather than under the home directory.",
        )
    return Check("homePath", "pass", f"Home directory has no spaces: {home}")


# ---------------------------------------------------------------------------
# Build-environment preflight (tan-cli#294 finding 2, reintroducing
# tan-cli#100, #98, #159): does a build even have a shot at starting?
#
# Folded into PLAIN `tan doctor`, mirroring
# `tan_core::preflight::build_preflight_checks` -- #100's own words for the
# gap this closes: "probed nothing about the build environment and printed
# byte-identical output across four materially different host states."
#
# `westResolved` (the venv-resolved `west` binary's own presence, tan-cli#123
# reintroduced) and `zephyrWorkspace`'s severity/gating are now IN scope here
# too (tan-cli#290) -- see `west_resolved_check`/`zephyr_workspace_check`'s
# own docstrings. `workspace`/`zephyrVersion`/`zephyrWorkspace` below are all
# sourced from the SHARED `tan.core.venv.west_workspace_dir` (tan-cli#294
# review) -- ALL THREE of its steps, including the `$ZEPHYR_BASE`-derived,
# manifest-verified fallback. A fourth, partial copy of the same search
# (this module's own retired `_resolve_west_workspace_dir`) previously
# covered only the project-tree walk and the SDK-derived layout, so a host
# relying SOLELY on a manually exported `$ZEPHYR_BASE` outside both a
# project tree and `<sdk-parent>` reported a false `workspace` Fail -- "no
# Zephyr workspace -- run `tan bootstrap`" -- that would have the customer
# bootstrap a SECOND workspace. Importing the one shared resolver closed
# that gap and retired the fourth copy one commit before this one; see
# `tan.core.venv.west_workspace_dir`'s own docstring for why the search
# lives there and not here.
# ---------------------------------------------------------------------------


def _broken_global_default() -> str | None:
    """The raw `sdkPath` `~/.alp/sdk-default` names, ONLY when that pointer
    file exists but its target is NOT a valid alp-sdk checkout (tan-cli#344).
    `None` when the pointer is absent, unreadable/malformed, or DOES resolve
    -- every one of those is indistinguishable from "nothing configured" and
    stays that way; this exists to name the one case that is not.

    Reads the exact file `sdk_cmd.resolve_sdk_tiered` already reads
    (`_pointer_target(_home_alp_dir() / "sdk-default")` + `_has_loader_script`)
    the SAME way, purely for this one extra fact -- it changes no resolution
    outcome (`resolve_sdk_root_ladder`/`resolve_sdk_tiered` are untouched by
    this function; it is called separately, only to feed `sdk_check`'s
    report). `resolve_sdk_tiered` itself already tracks an analogous broken
    POINTER for the project-pin tier (`ActiveSdk.broken_project_pin`) and
    surfaces it via `project_pin_issue` regardless of which lower tier
    answers -- this is the same idea one tier up, for the one tier that had
    no such memory at all: a dangling global default fell through silently,
    with nothing left to report it had ever existed.
    """
    target = _pointer_target(_home_alp_dir() / "sdk-default")
    if target is None or _has_loader_script(Path(target)):
        return None
    return target


def sdk_check(
    sdk_root: str | None,
    project_scope: str | None,
    tier: str | None = None,
    unselected_candidate: str | None = None,
    broken_global_default: str | None = None,
    divergent_candidate: str | None = None,
) -> Check:
    """`sdk` -- is an alp-sdk checkout resolved at all? Mirrors
    `tan_core::preflight::build_preflight_checks`'s `sdk` check.

    `project_scope` (the `--project` value, unjoined) used to name a SCOPED
    `tan sdk switch <path>` fix (tan-cli#101: the `.alp/sdk-path` pointer
    `sdk switch` writes is scoped to `--project`, so a bare `tan sdk switch
    <path>` from a `tan --project <p> doctor` run would have reported success
    while changing nothing about THIS invocation). That fix is moot now that
    `sdk switch` refuses outright in every build of tan on this branch
    (tan-cli#305, `sdk_cmd._run_not_ported`) -- recommending it, scoped or
    not, was the actual dead end #305 reported, since the ONLY thing left
    that resolves an SDK at all is `--sdk-root`, which needs no scoping. The
    parameter stays (worded into the fail detail below) because `--project`
    is still a fact worth naming, just no longer the reason for a different
    remedy.

    `tier`/`unselected_candidate` (tan-cli#301) -- a reported host named THREE
    different roots in one report (a leftover `globalDefault`, a stale
    `$ZEPHYR_BASE` workspace, and the checkout the user was actually standing
    in, which appeared nowhere), and `tan doctor`/`tan bootstrap` disagreed
    about which SDK a bare invocation meant. `GlobalDefault` outranking
    `Discovery` is deliberate (tan-cli#263 made pins absolute on purpose) --
    NO behaviour change here, only visibility: `tier` is the `SdkSourceTier`
    wire spelling (`sdkRootFlag`/`projectPin`/`globalDefault`/`discovery`)
    that answered, reported alongside the root the same way `tan sdk
    current`'s envelope already pairs `sdkPath` with `sourceTier`.
    `unselected_candidate` is a DIFFERENT checkout discoverable from cwd that
    a higher tier outranked (`None` when the winning tier already IS
    discovery, or nothing else resolves there) -- named explicitly, with how
    to select it, so a plausible checkout sitting right there does not read
    as unconsidered.

    `broken_global_default` (tan-cli#344, `_broken_global_default` above) is
    the raw `sdkPath` a machine-global `~/.alp/sdk-default` pointer held when
    that file exists but its target is no longer a valid checkout -- only
    meaningful in the `sdk_root is None` branch (a global default that DID
    resolve never reaches this function with `sdk_root is None` at all).
    Before this, "I have nothing configured" and "what I configured is
    broken and tan silently fell through past it" printed the identical
    sentence: `NO_SDK_NEXT_STEPS`, which tells the user to clone a checkout
    and pass `--sdk-root`, with no hint the thing they already configured is
    dangling. Falling through stays correct (unchanged here) and exit 4
    stays correct (unchanged here) -- only which sentence explains it
    changes. `bootstrap_cmd`'s own broken-pointer messages
    (`global_default_pointer_fix_hint`) are the shape this matches: name the
    pointer file directly, never `tan sdk switch`, which refuses outright in
    this build (tan-cli#305) -- recommending it here would be the exact
    dead end #305 already fixed for the project-pin case.
    """
    if sdk_root is not None:
        detail = f"alp-sdk at {sdk_root}"
        if tier is not None:
            detail += f" ({tier}"
            if unselected_candidate is not None:
                detail += (
                    f"; a checkout at {unselected_candidate} was not selected -- "
                    f"pass --sdk-root {unselected_candidate} to use it"
                )
            detail += ")"
        if divergent_candidate is not None:
            # BOTH roots re-rendered POSIX here, unlike the `pass` branch's
            # `alp-sdk at {sdk_root}` above, which keeps this host's native
            # separators and is left byte-identical. The reason is the CODE:
            # this branch's detail becomes an `issues[].message` under
            # `sdk.discovery-divergent`, which five other commands also emit
            # -- and all five build it from `_abs_posix`. A consumer
            # correlating two envelopes would otherwise match a code across a
            # message spelled `C:/...` on one side and `C:\...` on the other.
            # The envelope is the contract; normalise the filesystem side to
            # meet it, never the other way round (caught by
            # windows-latest on tan-cli#428, green everywhere else).
            posix_root = f"alp-sdk at {_abs_posix(sdk_root)}"
            if tier is not None:
                posix_root += f" ({tier})"
            return Check(
                "sdk",
                "warn",
                f"{posix_root}; `tan init`, `tan generate`, `tan examples` "
                f"and `tan renode` resolve a DIFFERENT checkout from this "
                f"directory ({_abs_posix(divergent_candidate)}) and report "
                f'the same sourceTier "discovery", so generated files and '
                f"the build plan can come from different SDK versions.",
                f"--sdk-root <path> to pin the one you mean for a single run, "
                f"or `tan init --sdk-root <path>` to write it into "
                f".alp/sdk-path, which outranks both discovery tiers.",
                code=SDK_DISCOVERY_DIVERGENT,
            )
        return Check("sdk", "pass", detail)
    scope_note = f" for --project {project_scope}" if project_scope is not None else ""
    if broken_global_default is not None:
        pointer = str(_home_alp_dir() / "sdk-default")
        return Check(
            "sdk",
            "fail",
            f"no SDK selected{scope_note} -- the machine-global default "
            f'({pointer}) names "{broken_global_default}", which is not a '
            f"valid alp-sdk checkout, so tan fell through past it and found "
            f"nothing else either.",
            f"{global_default_pointer_fix_hint(pointer)}, or pass "
            f"--sdk-root <path> directly.",
        )
    return Check(
        "sdk",
        "fail",
        f"no SDK selected{scope_note} -- {NO_SDK_NEXT_STEPS}",
        "--sdk-root <path>",
    )


def board_yaml_preflight_check(present: bool, project_selected: bool) -> Check:
    """`boardYaml` -- mirrors `build_preflight_checks`'s check of the same
    name, PLUS the project-selection awareness the Rust oracle's debug
    report has and this port's copy used to lack (tan-cli#294 review,
    reintroducing #100(b)): `tan bootstrap` prints `tan doctor` as the very
    next command, run from the SDK checkout root it just set up -- which has
    no `board.yaml` and needs none. Failing there made the first command a
    new customer types report `1 failed` and exit 4 for a non-problem.

    `project_selected` is True only when `--project` or `--board-yaml` was
    actually given (mirrors `crates/tan-cli/src/commands/doctor.rs::
    project_selected` -- with neither flag the resolved path is a guess at
    the cwd, not a request) and is only read when `present` is False.

    NOT a duplicate of a debug-report `boardYaml` check (this port has not
    built the debug half -- see the module docstring), so this is the only
    `boardYaml` check in this file and it is never dropped.
    """
    if present:
        return Check("boardYaml", "pass", "board.yaml found")
    if project_selected:
        return Check(
            "boardYaml",
            "fail",
            "board.yaml not found -- run `tan init` or pass `--board-yaml <path>`",
            "tan init",
        )
    return Check(
        "boardYaml",
        "warn",
        "no project selected -- no board.yaml found",
        "Select a project with `--project <dir>` (or `--board-yaml <path>`) to check one.",
    )


def workspace_preflight_check(workspace_dir: str | None) -> Check:
    """`workspace` -- is a Zephyr WORKSPACE (a directory holding `.west/`)
    resolved at all? Mirrors `build_preflight_checks`'s check of the same
    name. Distinct from `hostPrerequisites`/`west` above, which only confirm
    the TOOLS needed to build are on PATH -- neither confirms a Zephyr tree
    exists to build against.
    """
    if workspace_dir is not None:
        return Check("workspace", "pass", f"Zephyr workspace at {workspace_dir}")
    return Check(
        "workspace",
        "fail",
        "no Zephyr workspace -- run `tan bootstrap` (reuses a compatible Zephyr, else "
        "bootstraps one)",
        "tan bootstrap",
    )


def zephyr_version_preflight_check(
    workspace_version: str | None, sdk_pin: str | None
) -> Check | None:
    """`zephyrVersion` -- does a REUSED workspace's Zephyr match the active
    SDK's `west.yml` pin? Mirrors `build_preflight_checks`'s check
    (tan-cli#98/#159): compared at full `MAJOR.MINOR.PATCH`, because a
    truncated `MAJOR.MINOR` comparison let a patch-level pin bump
    (`v4.4.0` -> `v4.4.1`) read as a match -- the drifted-checkout shape of
    the alp-sdk#855 incident.

    `None` (no check emitted) when either side is unknown, matching Rust's
    own skip: don't nag when this cannot actually be verified.

    **`Fail`, not `Warn`** (#159): a reused workspace on the wrong Zephyr
    does not "maybe" break the build -- it compiles against a different
    Zephyr than the plan was emitted for, and a Warn here is indistinguishable
    from a check that can never fail.
    """
    if workspace_version is None or sdk_pin is None:
        return None
    if workspace_version == sdk_pin:
        return Check(
            "zephyrVersion", "pass", f"Zephyr v{workspace_version} matches the SDK pin"
        )
    return Check(
        "zephyrVersion",
        "fail",
        f"reused Zephyr v{workspace_version} != SDK pin v{sdk_pin} -- run `tan bootstrap` "
        "to refresh the workspace",
        "tan bootstrap",
    )


# ---------------------------------------------------------------------------
# Venv provenance (tan-cli#292 consequences 1 and 3).
# ---------------------------------------------------------------------------


def venv_provenance_check(record: WorkspaceSdkRecord | None, sdk_root: str | None) -> Check | None:
    """`venvProvenance` -- does the RESOLVED workspace venv's tan-written
    record (`<topdir>/.west/tan-workspace-sdk`, tan-cli#292) name the SAME SDK
    this report resolved against? Catches two of #292's three consequences:
    `tan sdk switch` leaving the venv behind (consequence 3 -- the record
    still names the SDK that last populated it), and a neighbouring project's
    venv winning `find_workspace_venv`'s upward walk when that venv is ITSELF
    tan-bootstrapped, just for a different SDK (consequence 1 -- its own
    record then names a project this report was never asked about). Both
    otherwise surface only later, as a Zephyr build failing on a
    wrong-version package that names the SYMPTOM, not the cause.

    **A WARNING, not a re-resolution (tan-cli#292 rc3 scope).** The record is
    not yet the resolver's primary source -- `tan build` still uses whatever
    `find_workspace_venv`'s search resolved; this only tells the customer
    that venv's packages may not match BEFORE a build fails on it. Consequence
    1's upward-walk case is caught only when the neighbouring venv carries its
    OWN record; one populated by a bare `west update` with no tan involvement
    anywhere still resolves silently -- the same gap `west_workspace_dir`'s
    `$ZEPHYR_BASE` manifest guard cannot close for an unrelated tree with no
    alp-sdk manifest to check against either. The full record-primary
    resolver the issue also proposes is out of scope for this fix; see the
    issue for the follow-up.

    `None` (no check emitted, matching `zephyr_version_preflight_check`'s own
    skip) when there is nothing to compare: no venv resolved, it carries no
    record at all -- a workspace bootstrapped by alp-sdk's own `bootstrap.sh`
    writes none (`crates/tan-cli/src/venv.rs:25-27`), and neither does a tan
    predating tan-cli#292 -- or no `sdk_root` resolved to compare against.
    """
    if record is None or sdk_root is None:
        return None
    if os.path.normcase(_abs_posix(record.sdk_path)) == os.path.normcase(_abs_posix(sdk_root)):
        return Check(
            "venvProvenance", "pass", f"workspace venv populated for the active SDK ({record.sdk_path})"
        )
    return Check(
        "venvProvenance",
        "warn",
        f"workspace venv was populated for a different SDK ({record.sdk_path}) than the "
        f"one currently selected ({sdk_root}) -- Zephyr packages installed into it may not "
        "match; run `tan bootstrap` to resync the venv",
        "tan bootstrap",
    )


# ---------------------------------------------------------------------------
# SDK provenance (tan-cli#294 finding 5; no numbered GH issue -- the Rust
# doc comment cites "conformance Issue 4 + 6").
# ---------------------------------------------------------------------------


def sdk_provenance_check(sdk_root: str) -> Check:
    """`sdkProvenance` -- records the SDK checkout's git short-commit and
    `metadata/sdk_version.yaml` version, so a build plan can be traced back
    to the planner that produced it, and warns when the checkout is behind
    its upstream tracking ref. Mirrors
    `crates/tan-cli/src/commands/doctor.rs`'s `append_sdk_provenance`.

    Advisory only: `git_behind_upstream` reads the local remote-tracking ref
    and performs no network fetch, so it only reflects the checkout's state
    as of the last `git fetch` -- never blocks a build over it.
    """
    commit = _git_short_commit(sdk_root)
    version = _read_sdk_version(sdk_root)
    if version and commit:
        detail = f"alp-sdk {version} @ {commit}"
    elif commit:
        detail = f"alp-sdk @ {commit}"
    elif version:
        detail = f"alp-sdk {version}"
    else:
        detail = f"alp-sdk at {sdk_root} (no git checkout / metadata/sdk_version.yaml)"

    behind = _git_behind_upstream(sdk_root)
    if behind is not None and behind > 0:
        return Check(
            "sdkProvenance",
            "warn",
            f"{detail} -- {behind} commit(s) behind upstream",
            f"Update the SDK checkout: git -C {sdk_root} pull",
        )
    return Check("sdkProvenance", "pass", detail)


def _git_short_commit(root: str) -> str | None:
    """`git -C <root> rev-parse --short HEAD`, or `None` when `root` is not a
    git checkout (e.g. an extracted SDK release archive)."""
    out = probe(["git", "-C", root, "rev-parse", "--short", "HEAD"])
    if out is None:
        return None
    commit = out.strip()
    return commit or None


def _git_behind_upstream(root: str) -> int | None:
    """Commit count `HEAD` is behind its upstream tracking ref, without
    fetching. `None` when there is no upstream or `root` is not a git
    checkout."""
    out = probe(["git", "-C", root, "rev-list", "--count", "HEAD..@{upstream}"])
    if out is None:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _read_sdk_version(root: str) -> str | None:
    """Read a version from `<root>/metadata/sdk_version.yaml`. Shares
    `sdk_cmd.parse_sdk_version_yaml` with `check_sdk_readiness`
    (tan-cli#162), so `tan sdk install`/`current`/`switch` and this check
    read the SAME version out of the SAME file rather than two copies of the
    scan able to disagree."""
    text = _read_text(Path(root) / "metadata" / "sdk_version.yaml")
    if text is None:
        return None
    return parse_sdk_version_yaml(text)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def summarise(checks: list[Check]) -> dict[str, int]:
    """`pass`/`warn`/`fail` counts. `unknown` lands in NONE of them, so
    `sum(summary.values())` can be smaller than `len(checks)` -- deliberate, and
    the same shape the Rust `DoctorSummary` has."""
    return {
        "pass": sum(1 for c in checks if c.status == "pass"),
        "warn": sum(1 for c in checks if c.status == "warn"),
        "fail": sum(1 for c in checks if c.status == "fail"),
    }


def next_steps(checks: list[Check]) -> list[str]:
    """Deduplicated fixes for non-passing checks. `unknown` contributes none:
    a check nobody could run has nothing to remediate."""
    steps: list[str] = []
    for check in checks:
        if check.status in ("pass", "unknown") or check.fix is None:
            continue
        if check.fix not in steps:
            steps.append(check.fix)
    return steps


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def kebab_check_name(name: str) -> str:
    """`Check.name` (camelCase, also a JSON data key/display string -- left
    alone) -> the kebab-case slug an issue *code* uses, e.g. `'sevenZip'` ->
    `'seven-zip'`. `contract/issue-codes.json`'s registry gate
    (`^[a-z][a-z0-9-]*\\.[a-z0-9-]+$`) rejects camelCase; 27 issue codes
    derived straight from `check.name` shipped camelCase in v0.5.0 before this
    existed."""
    return _CAMEL_BOUNDARY.sub("-", name).lower()


def checks_to_issues(checks: list[Check]) -> list[Issue]:
    """Warn/fail checks become issues; `unknown` raises none (it is not a
    problem, the question was simply not askable). The code is the check's own
    when it has one -- the frozen `bootstrap.*` spellings -- else Rust's
    `doctor.<checkName>` convention, kebab-cased (`kebab_check_name`)."""
    return [
        Issue(
            check.code or f"doctor.{kebab_check_name(check.name)}",
            "error" if check.status == "fail" else "warning",
            check.detail,
        )
        for check in checks
        if check.status in ("warn", "fail")
    ]


def exit_code_for(checks: list[Check]) -> ExitCode:
    """Exit 4 on any failure. Never 0 on an unhealthy host: a green doctor over a
    broken environment converts a fixable setup problem into a mystery inside
    somebody else's build system."""
    return (
        ExitCode.DOCTOR_FAILURE
        if any(c.status == "fail" for c in checks)
        else ExitCode.SUCCESS
    )


# ---------------------------------------------------------------------------
# The IO layer: probe the host, then hand facts to the pure checks above
# ---------------------------------------------------------------------------


def _python_candidates() -> list[list[str]]:
    """Verbatim `tan_core::bootstrap::python_candidates`. Windows leads with the
    `py` launcher because a machine can have a perfectly good 3.12 with no bare
    `python` on PATH, and the bare `python.exe` there is very often the Store
    alias."""
    if os.name == "nt":
        return [["py", "-3"], ["python"], ["python3"]]
    return [["python3"], ["python"]]


#: `platform.machine()` -> the Zephyr-SDK-release arch token
#: (`tan_core::host_env::ZEPHYR_SDK_HOSTS`'s spelling). Values seen in
#: practice: Windows `AMD64`/`ARM64`, macOS `x86_64`/`arm64`, Linux
#: `x86_64`/`aarch64`. An unrecognised value is passed through unchanged, so
#: `zephyr_sdk_host_check` reports it as a real, unserved tag rather than
#: silently mapping it onto a served one.
_ARCH_TAGS = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}


def _macos_rosetta_translated() -> bool:
    """`True` when THIS process's Python interpreter is an x86_64 binary
    running under Rosetta on Apple silicon -- `sysctl -n
    sysctl.proc_translated` == 1. Mirrors
    `tan_core::host_env::arch_for_proc_translated`'s macOS probe
    (`crates/tan-cli/src/commands/doctor.rs:601-611`) via the `sysctl` CLI
    rather than a `ctypes` binding to the same `sysctlbyname` FFI -- this
    module's probes are all subprocess-based, and the sysctl is a stable
    macOS command-line surface. `probe()` (and so this) returns `False` on a
    pre-Big-Sur host where the sysctl does not exist -- the compiled arch is
    already correct there, matching Rust's `rc == 0 && translated == 1`.
    """
    return (probe(["sysctl", "-n", "sysctl.proc_translated"]) or "").strip() == "1"


def _host_os_arch_tags() -> tuple[str, str]:
    """`(os, arch)` in `tan_core::host_env::ZEPHYR_SDK_HOSTS`'s tokens, read
    from `platform.system()`/`platform.machine()`, corrected for Rosetta.

    Unlike the Rust oracle, this does NOT detect Windows-on-ARM x64 emulation
    (`IsWow64Process2`): tan's Python port runs under whatever interpreter is
    already installed rather than a separately-compiled per-arch binary, so
    `platform.machine()` reflects the INTERPRETER's real architecture in the
    overwhelming majority of cases (a user who installed an x86_64 Python on
    Windows-on-ARM, where Python.org has shipped a native ARM64 installer for
    some time, is the one host this can under-report -- tracked, not silently
    claimed complete).

    macOS IS corrected (tan-cli#294 review): the opposite direction is common
    there and worse. Rosetta silently runs the far more widely distributed
    x86_64 Python build on Apple silicon, so `platform.machine()` alone
    reported `macos-x86_64` -- a FALSE HARD REFUSAL
    (`zephyr_sdk_host_check`'s `Fail`, exit 4, "build on a Linux host") on
    hardware the pinned SDK serves natively as `macos-aarch64`.
    """
    system = platform.system().lower()
    host_os = {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(system, system)
    machine = platform.machine().lower()
    arch = _ARCH_TAGS.get(machine, machine)
    if host_os == "macos" and arch == "x86_64" and _macos_rosetta_translated():
        arch = "aarch64"
    return host_os, arch


def classify_git_core_longpaths(exit_code: int | None, stdout: str) -> bool | None:
    """The three-way verdict for a `git config --get core.longpaths`
    invocation -- the git-side counterpart to `_long_paths_enabled`'s
    registry read, split out as its own pure function (mirroring
    `tan_core::host_env::classify_git_core_longpaths`) so the exact mapping
    tan-cli#306 argues hardest about is unit-tested without needing a real
    `git` invocation for every case.

    * exit 0 -> the stdout value, parsed with git's own boolean grammar.
    * exit 1 -> `False`. `git config --get` documents this code as "the key
      is not set in any scope (system/global/local)" -- git's own default,
      and the state a fresh `HOME` is in (tan-cli#306's exact repro).
    * anything else (`git` not on PATH, a malformed config file, a
      permissions error) -> `None`: uncertain, not guessed.
    """
    if exit_code == 0:
        value = stdout.strip().lower()
        return value not in ("false", "no", "off", "0")
    if exit_code == 1:
        return False
    return None


def _git_core_longpaths() -> bool | None:
    """Read git's own EFFECTIVE `core.longpaths` (system -> global -> local
    precedence, resolved by `git config --get` itself rather than tan
    re-implementing that precedence by hand) via a real `git` subprocess.

    A SEPARATE axis from `_long_paths_enabled` on purpose (tan-cli#306): the
    registry governs manifested Win32 API calls; it does nothing for git,
    which `west update` uses for every project clone/checkout and which
    refuses a long path unless ITS OWN setting says so -- the registry read
    alone reported `pass` on a fresh `HOME` while `west update` died on
    `hal_nxp`'s `tf-psa-crypto` tree.

    Not built on this file's own `probe()`: `probe()` collapses "ran and
    exited non-zero" (exit 1, meaning "unset") and "could not run at all"
    (meaning "unknown") to the same `None`, and `classify_git_core_longpaths`
    needs to tell those apart.
    """
    try:
        out = subprocess.run(
            ["git", "config", "--get", "core.longpaths"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return classify_git_core_longpaths(out.returncode, out.stdout)


def _long_paths_enabled() -> bool | None:
    """Windows `HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem\\
    LongPathsEnabled`, via the stdlib `winreg` module (Windows-only).

    `None` off Windows (`long_paths_check` is never reached there --
    `_collect` gates the append on `os.name == "nt"`) and on any registry
    read failure OTHER than the value/subkey being absent -- an access
    denial, a value of the wrong type -- so the check can say "unknown"
    rather than guess. An absent value/subkey (`FileNotFoundError`) IS
    "disabled": that is the Windows default-off state and by far the most
    common one, matching `tan_core::host_env::classify_long_paths`.
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover -- always present on Windows CPython
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return None


#: Where the `arm-zephyr-eabi` cross compiler sits INSIDE a zephyr-sdk-1.0.1
#: root -- the version `ZEPHYR_SDK_INSTALL_VERSION` above pins and the only
#: one this file's fix hints (`zephyr_sdk_install_command`) ever name.
#:
#: tan-cli#286 third pass: the SECOND pass's blocker. `_zephyr_sdk_root_valid`
#: and `test_doctor_command.py`'s own `_plant_zephyr_sdk` fixture both
#: previously hardcoded the WRONG layout (un-prefixed `arm-zephyr-eabi/bin/`)
#: independently, so they agreed with EACH OTHER instead of with a real SDK
#: and 77 tests passed over a broken probe. Both now build from this one
#: tuple so they cannot drift back to silently matching only each other.
#:
#: The `gnu/` prefix is decisive, not guessed: a maintainer build log on the
#: exact host this check hard-failed on -- "Found assembler:
#: <home>/zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc.exe"
#: -- plus three in-repo measurements agreeing byte-for-byte:
#: `crates/tan-core/src/runners.rs`'s real-AEN801-build fixture (`gdb:
#: /zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb-py`),
#: `crates/tan-core/src/debug_launch.rs`'s resolution test (same `gdbPath`),
#: and `contract/fixtures/toolchains/toolchains.json`'s `du -sb` measurement
#: of `gnu/arm-zephyr-eabi/` (784086497 bytes) as its own line item, separate
#: from `hosttools/`.
#:
#: NOT widened to also accept the older, un-prefixed `arm-zephyr-eabi/bin/`
#: layout (0.16.x): every fix hint in this file already promises exactly
#: `--version 1.0.1`, so treating a stale sub-1.0 install as a Pass would
#: validate a toolchain this file's own advice says to replace. NOT probing
#: the SDK's own `sdk_version`/`sdk_toolchains` marker files either, tempting
#: as a layout-proof alternative would be: no measurement of either file's
#: real name, location or format exists anywhere in this repo, and guessing
#: at one is the exact unverified-brief mistake that put the wrong compiler
#: path here to begin with.
ZEPHYR_SDK_TOOLCHAIN_DIR = ("gnu", "arm-zephyr-eabi", "bin")


def _zephyr_sdk_root_valid(root: Path) -> bool:
    """`True` when `root` is an actually-installed Zephyr SDK -- not merely a
    directory that happens to be named right, or still named by a stale
    `ZEPHYR_SDK_INSTALL_DIR`. Probes the one file every downstream check
    (`west build`, `west flash`) actually needs: the `arm-zephyr-eabi` cross
    compiler, at `ZEPHYR_SDK_TOOLCHAIN_DIR`. `is_dir()` alone passes on an
    EMPTY directory -- the exact false Pass tan-cli#286 exists to fix;
    measuring the shipped thing instead of a directory-name proxy is what
    makes this port's docstring true.
    """
    exe = "arm-zephyr-eabi-gcc.exe" if os.name == "nt" else "arm-zephyr-eabi-gcc"
    try:
        return root.joinpath(*ZEPHYR_SDK_TOOLCHAIN_DIR, exe).is_file()
    except OSError:
        return False


def _zephyr_sdk_scan_roots() -> list[Path]:
    """Every directory `_zephyr_sdk_detected` scans for a `zephyr-sdk-*`
    install, besides `/opt` -- `$HOME`, `%USERPROFILE%` AND `Path.home()`,
    ALL of them, never `HOME or USERPROFILE`.

    Under Git Bash/MSYS on Windows, `HOME` is a POSIX-translated path
    (`/c/Users/dev`) while the real Zephyr SDK sits under the native
    `%USERPROFILE%` (`C:\\Users\\dev\\zephyr-sdk-1.0.1`). `or`ing the two
    picks whichever is set first and silently drops the other -- proven on a
    real host: that host HAS the SDK and `_zephyr_sdk_detected()` still
    returned `False`, a hard doctor FAIL worse than the false PASS #286
    exists to fix. `Path.home()` resolves independently of both env vars
    (POSIX `pwd`/`$HOME`; Windows `USERPROFILE` via CPython's own
    `ntpath.expanduser`) and can disagree with both, so it is scanned too,
    not assumed redundant.
    """
    roots = [Path("/opt")]
    seen: set[str] = set()
    for raw in (os.environ.get("HOME"), os.environ.get("USERPROFILE")):
        if raw and raw not in seen:
            seen.add(raw)
            roots.append(Path(raw))
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        home = None
    if home is not None and str(home) not in seen:
        roots.append(home)
    return roots


def _zephyr_sdk_detected() -> bool:
    """`True` when a Zephyr SDK toolchain is installed anywhere this host
    would resolve one from. Mirrors `crate::toolchain::resolve_toolchain_root`
    /`zephyr_sdk_detected` (not yet ported for build-plan `${TOOLCHAIN_ROOT}`
    substitution -- see `build_cmd.py`'s `toolchain_root=None` -- but doctor
    only needs the yes/no, same split the Rust module docstring draws):
    `ZEPHYR_SDK_INSTALL_DIR`, honored ONLY when the directory it names
    actually CONTAINS the toolchain (`_zephyr_sdk_root_valid` -- the variable
    is exported from a shell profile and routinely outlives the SDK it once
    pointed at, e.g. after `rm -rf ~/zephyr-sdk-0.16.5`, and an empty
    directory it never pointed at anything real for is the same failure mode
    -- trusting presence alone would report a false Pass here and the real
    failure would surface later as a raw CMake toolchain error); else any
    `zephyr-sdk*`-named directory, similarly validated, directly under
    `_zephyr_sdk_scan_roots()`. Several installs still count as detected --
    this is only doctor's yes/no, not the ambiguous-root pick the build-plan
    substitution path will need.

    Never raises: an unreadable or missing scan root is "nothing found
    there", not a doctor crash.
    """
    env_dir = os.environ.get("ZEPHYR_SDK_INSTALL_DIR")
    if env_dir and _zephyr_sdk_root_valid(Path(env_dir)):
        return True
    for root in _zephyr_sdk_scan_roots():
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("zephyr-sdk") and _zephyr_sdk_root_valid(entry):
                return True
    return False


def _probe_host_python(floor: tuple[int, int]) -> tuple[str, tuple[int, int]] | None:
    """First candidate that RUNS and clears `floor`; else the first that merely
    ran, so the too-old message can name a real version instead of "did not
    run". Mirrors `crate::util::probe_host_python`."""
    first_that_ran: tuple[str, tuple[int, int]] | None = None
    for candidate in _python_candidates():
        out = probe([*candidate, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"])
        if out is None:
            continue
        version = _parse_two(out)
        if version is None:
            continue
        entry = (" ".join(candidate), version)
        if version >= floor:
            return entry
        if first_that_ran is None:
            first_that_ran = entry
    return first_that_ran


@dataclass(frozen=True)
class ManifestLoad:
    """The result of resolving `<sdk>/metadata/bootstrap.json`.

    `is_real` is the provenance verdict as DATA, set exactly once, at the one
    return that actually read and parsed a manifest -- never re-derived by a
    caller sniffing `source`'s prose. `source` is still carried for display
    (the message names WHICH file or fallback), but nothing downstream may
    infer `is_real` from it: that used to be `source.startswith("facts from
    alp-sdk")`, which silently flips the verdict the moment this docstring's
    or `source`'s wording changes, with nothing to catch it.
    """

    facts: dict
    source: str
    error: str | None
    is_real: bool


def _load_manifest(sdk_root: str | None) -> ManifestLoad:
    """Resolve the prerequisites facts from `<sdk>/metadata/bootstrap.json`.

    A missing or malformed manifest is a WARNING with documented fallbacks, not
    a refusal: doctor's whole job is to run on a host where things are wrong,
    and a doctor that cannot start because the thing it diagnoses is broken is
    the failure mode it exists to prevent.
    """
    fallback = {
        "posix": ["git", "cmake", "python3", "ninja"],
        "windows": ["git", "cmake", "python", "ninja"],
        "pythonMinVersion": f"{FALLBACK_PYTHON_FLOOR[0]}.{FALLBACK_PYTHON_FLOOR[1]}",
        "install": {},
    }
    if sdk_root is None:
        return ManifestLoad(
            fallback,
            "tan's built-in fallback list (no alp-sdk checkout resolved)",
            None,
            is_real=False,
        )
    path = Path(sdk_root) / "metadata" / "bootstrap.json"
    text = _read_text(path)
    if text is None:
        return ManifestLoad(
            fallback,
            "tan's built-in fallback list",
            f"could not read {path}",
            is_real=False,
        )
    try:
        facts = json.loads(text)
    except ValueError as err:
        return ManifestLoad(
            fallback, "tan's built-in fallback list", f"{path} is not valid JSON: {err}", is_real=False
        )
    prerequisites = facts.get("prerequisites")
    if not isinstance(prerequisites, dict):
        return ManifestLoad(
            fallback,
            "tan's built-in fallback list",
            f"{path} has no `prerequisites` object",
            is_real=False,
        )
    west = facts.get("west")
    if isinstance(west, dict):
        prerequisites = {**prerequisites, "_pipSpec": west.get("pipSpec")}
    return ManifestLoad(prerequisites, f"facts from alp-sdk {path}", None, is_real=True)


def _manifest_floor_from_facts(facts: dict) -> tuple[int, int]:
    """The `pythonMinVersion` `facts` declares, or `FALLBACK_PYTHON_FLOOR` when
    absent/unparseable -- shared by `_collect` and `resolve_manifest_python_floor`
    so the two never parse the same field two different ways."""
    return _parse_two(str(facts.get("pythonMinVersion") or "")) or FALLBACK_PYTHON_FLOOR


def resolve_manifest_python_floor(sdk_root: str | None) -> tuple[tuple[int, int], str]:
    """`(floor, provenance)` for the SDK's OWN declared Python floor --
    `<sdk>/metadata/bootstrap.json`'s `prerequisites.pythonMinVersion` -- for
    callers gating a SPAWNED SDK interpreter (`generate`/`model`) rather than a
    Zephyr build, so they want this floor, not `_collect`'s Zephyr-composed
    effective one. The ONE reader: before this, `generate_cmd` and `model_cmd`
    each carried their own hardcoded `MIN_PYTHON = (3, 10)`, a floor that could
    drift from the manifest's -- and from each other's -- without either
    command noticing.
    """
    loaded = _load_manifest(sdk_root)
    return _manifest_floor_from_facts(loaded.facts), loaded.source


#: Generous on purpose: a real install can pull a package over the network,
#: unlike every OTHER timeout in this file (`PROBE_TIMEOUT_S`), which only
#: ever waits on a local `--version` banner. `ponytail`: one fixed ceiling,
#: no live progress reporting -- raise it, or stream output, if a real
#: install exceeds it before this is revisited.
FIX_INSTALL_TIMEOUT_S = 300


def fix_needs_sudo_check(tool: str, command: str) -> Check:
    """`doctor.fix-needs-sudo` -- ADR 0021's Tier-B refusal (tan-cli#91,
    MAINTAINER DECISION): tan never spawns `sudo` on the customer's behalf.

    Under `--format json` this process's stdio is captured end to end, so a
    `sudo` password prompt has nowhere to go -- it would hang forever rather
    than fail loudly, which is a worse outcome than refusing up front. REFUSE
    AND PRINT: name the exact command, verbatim, so it can be pasted into a
    real terminal, and stop there. `run_fix` below is the only caller, and
    only reaches this branch for a command whose first word IS literally
    `sudo` -- the manifest's own POSIX `prerequisites.install` commands are
    the one place that word appears in this codebase at all; Windows
    (`winget`, user-scope) and macOS (`brew`) never need it.
    """
    return Check(
        f"fix:{tool}",
        "warn",
        f'`--fix` will not run `{command}` for {tool}: it needs elevation '
        f'("sudo"), and tan never spawns sudo itself. Run it yourself, then '
        f"re-run `tan doctor`.",
        command,
        code="doctor.fix-needs-sudo",
    )


def fix_installed_check(tool: str, command: str) -> Check:
    """`doctor.fix-installed` -- `--fix` ran a manifest install command that
    needed no elevation (ADR 0021 Tier A), and the child process exited 0.

    Deliberately NOT a claim that `{tool}` is now on PATH: this process
    already read its own PATH at start-up (tan-cli#91), so an install that
    lands after that moment is invisible to it -- there is no same-process
    re-check to perform, honestly or otherwise. "Installed; reopen your
    shell" is the whole truth this check can tell; `hostPrerequisites`
    above still reports `{tool}` missing in THIS report, which is correct
    for THIS report.
    """
    return Check(
        f"fix:{tool}",
        "warn",
        f"`--fix` ran `{command}` for {tool}. tan cannot see a PATH change "
        f"made after it started -- open a new shell, then re-run `tan "
        f"doctor` there to confirm.",
        code="doctor.fix-installed",
    )


def fix_spawn_failed_check(tool: str, command: str, err: Exception) -> Check:
    """`doctor.fix-spawn-failed` -- `--fix` resolved `{tool}`'s install
    command on PATH (`on_path` already succeeded) but starting it raised
    (`OSError`/`ValueError`/`subprocess.SubprocessError` other than a
    timeout). Distinct from silence: without this, a customer watching
    `--fix` do nothing cannot tell "the OS refused to start it" from "tan
    never tried"."""
    return Check(
        f"fix:{tool}",
        "warn",
        f"`--fix` could not start `{command}` for {tool}: {err}. Run it "
        f"yourself, then re-run `tan doctor`.",
        command,
        code="doctor.fix-spawn-failed",
    )


def fix_failed_check(tool: str, command: str, returncode: int) -> Check:
    """`doctor.fix-failed` -- `--fix` ran `{tool}`'s install command and the
    child exited non-zero. `hostPrerequisites` above still reports `{tool}`
    missing in THIS report (same no-same-process-recheck honesty as
    `fix_installed_check`) -- this Check is the only place a customer learns
    the install itself failed, rather than merely "still missing"."""
    return Check(
        f"fix:{tool}",
        "warn",
        f"`--fix` ran `{command}` for {tool}; it exited {returncode}. Run it "
        f"yourself to see the full output, then re-run `tan doctor`.",
        command,
        code="doctor.fix-failed",
    )


def fix_timed_out_check(tool: str, command: str) -> Check:
    """`doctor.fix-timed-out` -- `{tool}`'s install command did not finish
    inside `FIX_INSTALL_TIMEOUT_S` (300s) and was killed. Without this, a
    hang here reads as up to 20 minutes of silent terminal: text-mode output
    only prints after the WHOLE report completes."""
    return Check(
        f"fix:{tool}",
        "warn",
        f"`--fix` killed `{command}` for {tool} after {FIX_INSTALL_TIMEOUT_S}s "
        f"with no result. Run it yourself, then re-run `tan doctor`.",
        command,
        code="doctor.fix-timed-out",
    )


#: Per-installer remedy for `fix_installer_not_found_check`, keyed by the first
#: word of the manifest's install command. Only two entries because only two
#: are reachable: `metadata/bootstrap.json`'s `prerequisites.install` produces
#: `brew` on macOS and `winget` on Windows, and Linux's `sudo apt-get ...` is
#: refused by `fix_needs_sudo_check` long before anything tries to resolve it
#: (see `_fallback_install_commands` in `tan.core.bootstrap`, which is
#: byte-pinned to the manifest). Anything else gets the generic remedy below
#: -- a manifest is free to name a package manager this table has never heard
#: of, and "not found" with no advice is the exact silence tan-cli#360 is
#: about.
INSTALLER_REMEDIES = {
    "brew": (
        "Homebrew is not installed -- install it from https://brew.sh, then "
        "re-run `tan doctor --fix`."
    ),
    "winget": (
        'winget comes from the App Installer package -- install "App '
        'Installer" from the Microsoft Store (Windows 10 1809 or newer), open '
        "a new shell so PATH picks it up, then re-run `tan doctor --fix`."
    ),
}


def fix_installer_not_found_check(installer: str, tools: list[str]) -> Check:
    """`doctor.fix-installer-not-found` -- tan-cli#360: `--fix` could not
    resolve the program the manifest's install command starts with, so it ran
    NO repair for the tools that command covers.

    This used to be a bare `continue` in `run_fix` -- the one outcome that
    emitted no Check at all, in a function whose whole stated invariant is
    that every entry is either run or refused and every outcome is reported.
    It is also the single most likely outcome on the hosts `--fix` exists
    for: the manifest installs with `brew` on macOS and `winget` on Windows,
    so a fresh Mac without Homebrew, or a Windows image with no usable
    winget, reported tools missing, ACCEPTED `--fix`, and then printed
    exactly the same report back with no `fix:*` line anywhere. The
    least-equipped host got the least diagnostic behaviour, and could not
    tell "nothing needed fixing" from "tan never found the installer to try
    with".

    ONE Check per installer, not per tool: a fresh Mac is missing all six
    manifest prerequisites at once and every one of them says `brew`, so
    per-tool would print the same "install Homebrew" paragraph six times over
    -- six restatements of one fact, in a report a customer is reading to
    find out what is actually wrong. `tools` therefore carries every tool
    this one absence blocked, and `run_fix` groups them.

    Named `fix:{installer}` rather than `fix:{tool}` for the same reason --
    the subject of this verdict is the installer, and one name per absent
    installer is what keeps the grouping legible in the text report. No
    `fix` field: the per-tool install commands are not runnable until the
    installer exists, and `hostPrerequisites` already carries each one
    verbatim (`data.missingPrerequisites[].command`), so repeating a
    command that cannot run would be worse than pointing at it.
    """
    named = ", ".join(tools)
    many = len(tools) > 1
    remedy = INSTALLER_REMEDIES.get(installer) or (
        f"Install `{installer}`, then re-run `tan doctor --fix`."
    )
    return Check(
        f"fix:{installer}",
        "warn",
        f"`--fix` ran no repair for {named}: the manifest installs "
        f"{'them' if many else 'it'} with `{installer}`, which is not on "
        f"PATH. {remedy} Or install {'those tools' if many else named} with a "
        f"package manager this host already has -- `hostPrerequisites` above "
        f"names {'each' if many else 'its'} exact install command.",
        code="doctor.fix-installer-not-found",
    )


def run_fix(missing: list[dict[str, str | None]]) -> list[Check]:
    """`--fix`'s ADR 0021 executor (tan-cli#91): for each tool
    `hostPrerequisites` already reported missing, either run its manifest
    install command (no elevation needed -- Tier A) or refuse and name it
    (needs `sudo` -- Tier B), never both, never neither. `missing` is that
    check's OWN structured field (`Check.missing`, `{tool, command}` pairs)
    -- never a second, independently recomputed tool/command list, so this
    can only ever act on exactly what the report already told the customer
    was wrong.

    A tool with `command=None` (the manifest names no install command for
    it) is skipped outright: nothing to run, nothing to refuse, and the
    existing `hostPrerequisites` Fail already carries the honest "install it
    yourself" advice for that case.

    Every outcome becomes a `Check` -- `fix_needs_sudo_check`/
    `fix_installed_check` on the two "acted, and it's fine" paths, (as of
    the tan-cli#91 follow-up below) `fix_spawn_failed_check`/`fix_failed_check`/
    `fix_timed_out_check` on the three "acted, and it's NOT fine" paths, and
    (tan-cli#360) `fix_installer_not_found_check` on the "could not act at
    all" one -- never a bare side effect. A customer who typed `--fix` and
    got the SAME report back used to have no way to tell "nothing needed
    fixing" from "tan tried and silently gave up": a spawn error, a non-zero
    exit, a `FIX_INSTALL_TIMEOUT_S` (300s) timeout, or an install command
    whose own program is not on PATH each used to `continue` with no trace at
    all, and text-mode output only prints after the WHOLE report completes --
    up to 20 minutes of silent terminal across four tools with nothing to
    show for it. `hostPrerequisites`'s own Fail still names the tool and its
    command either way; these Checks add the ONE fact it structurally cannot
    carry -- what `--fix` itself did about it.

    Only ever called from `doctor()`'s `--fix` branch, itself gated on
    `can_prompt` (`tan.core.consent`) -- the one place in this module that
    mutates the host rather than merely observing it, so it is confined
    exactly there, never folded into `_collect` (pure probes, see the module
    docstring).
    """
    results: list[Check] = []
    # installer -> every tool whose install command starts with it and that
    # therefore went unrepaired (tan-cli#360). Insertion-ordered, so the
    # grouped Checks appended below come out in `missing` order rather than
    # an arbitrary one.
    unresolved: dict[str, list[str]] = {}
    for entry in missing:
        tool = entry.get("tool")
        command = entry.get("command")
        if not tool or not command:
            continue
        if command.strip().startswith("sudo "):
            results.append(fix_needs_sudo_check(tool, command))
            continue
        argv = shlex.split(command)
        if not argv:
            # The one deliberately silent skip left (tan-cli#360 reviewed the
            # rest): an all-whitespace install command names no program to
            # report as absent, so there is nothing this could say beyond what
            # `hostPrerequisites`'s own Fail already says about the tool. A
            # manifest defect, not a host one.
            continue
        # `on_path`, never bare `subprocess.run([name, ...])`: the same
        # PATH-only, no-cwd-insertion resolver every other spawn in this
        # module uses (see `on_path`'s own docstring) -- a project-local
        # binary happening to share the tool's name must not be what `--fix`
        # runs with elevated-sounding trust.
        resolved_exe = on_path(argv[0])
        if resolved_exe is None:
            # tan-cli#360: collected, not silently dropped. Grouped by
            # installer because one absent `brew` blocks every macOS
            # prerequisite at once -- see `fix_installer_not_found_check`.
            unresolved.setdefault(argv[0], []).append(tool)
            continue
        argv[0] = resolved_exe
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=FIX_INSTALL_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            results.append(fix_timed_out_check(tool, command))
            continue
        except (OSError, ValueError, subprocess.SubprocessError) as err:
            results.append(fix_spawn_failed_check(tool, command, err))
            continue
        if result.returncode == 0:
            results.append(fix_installed_check(tool, command))
        else:
            results.append(fix_failed_check(tool, command, result.returncode))
    return [
        *results,
        *(fix_installer_not_found_check(i, t) for i, t in unresolved.items()),
    ]


def fix_suppressed_issue(*, non_interactive: bool, ci: bool, json_mode: bool) -> Issue:
    """`doctor.fix-suppressed` -- tan-cli#91 P1, measured against the oracle:
    `tan doctor --fix --format json` on an unhealthy host used to be a
    byte-for-byte silent no-op vs. plain `tan doctor` -- no issue, no note,
    exit code unchanged -- indistinguishable from a `--fix` that genuinely
    found nothing to do. The oracle's own equivalent refuses outright
    (`cli.parse-error`, exit 2); this port instead reports HONESTLY: `--fix`
    was requested, the `can_prompt` consent gate (`tan.core.consent`) refused
    it, and here is which of its conditions actually tripped -- not just that
    nothing happened.

    Only ever called from `doctor()`, and only when `fix` is set and
    `can_prompt` returned `False` for these same three flags -- never the
    other way around, so this can only ever explain a REAL suppression.

    The `isatty()` pair is read ONLY when `not json_mode`, mirroring
    `can_prompt`'s own short-circuit order (`... and not json_mode and
    sys.stdin.isatty() and sys.stderr.isatty()`) rather than a coincidence:
    under `--format json`, `tan.cli.main` tees `sys.stderr` through
    `_TeeStderr`, which has no `isatty()` at all -- reading it unconditionally
    here crashes this exact suppressed-fix report with
    `AttributeError: '_TeeStderr' object has no attribute 'isatty'` (measured
    against a real `tan doctor --fix --format json --ci` run). `json_mode`
    is already a complete, accurate reason on its own; there is nothing the
    tty state could add under it.
    """
    reasons = []
    if json_mode:
        reasons.append("`--format json` (no terminal to prompt on)")
    if ci:
        reasons.append("`--ci`")
    if non_interactive:
        reasons.append("`--non-interactive`")
    if not json_mode and not (sys.stdin.isatty() and sys.stderr.isatty()):
        reasons.append("no interactive terminal (stdin/stderr not a tty -- piped, redirected, or CI)")
    return Issue(
        "doctor.fix-suppressed",
        "warning",
        "`--fix` was requested but not run: " + "; ".join(reasons) + ". Re-run "
        "`tan doctor --fix` from a real, interactive terminal, without "
        "--ci/--non-interactive/--format json, to allow it.",
    )


def _collect(
    sdk_root: str | None,
    build: bool = False,
    board_yaml: str | None = None,
    project_scope: str | None = None,
    workspace_root: str = ".",
    sdk_tier: str | None = None,
    broken_global_default: str | None = None,
    on_check: Callable[[Check], None] | None = None,
) -> list[Check]:
    """Every probe, in report order. Nothing here may raise -- see the module
    docstring; `probe`/`on_path`/`_read_text` are the only three ways this
    module touches the outside world and none of them can.

    `on_check` (tan-cli streaming, change 3) fires the MOMENT each check is
    added, in the exact same order -- `doctor()`'s text branch passes a
    callback that prints that one check's block immediately, so a slow probe
    later in this function never leaves an already-answered check unprinted
    on screen. `None` (every direct test caller of this function, and JSON
    mode) skips the callback entirely and this returns exactly the batch
    `list[Check]` it always has.

    `build` (`--build`) is accepted and forwarded from `doctor()` but no
    longer changes anything here (tan-cli#290): `zephyrWorkspace`, the last
    check it used to gate, now runs unconditionally alongside `workspace`/
    `zephyrVersion` -- see `zephyr_workspace_check`'s docstring for why. Kept
    as a parameter rather than dropped so every existing direct caller (this
    file's own test suite, and the CLI's own forwarding call) keeps working
    unchanged; `alp-sdk-vscode`'s `["doctor", "--build"]` call sites keep
    working too, they just no longer see a different check list.

    `board_yaml`/`project_scope`/`workspace_root` feed the tan-cli#294/#290
    build-environment preflight (`sdk`/`boardYaml`/`workspace`/
    `westResolved`/`venvProvenance`/`zephyrVersion`/`zephyrWorkspace`) -- all
    default so every existing direct caller (this file's own test suite)
    keeps working unchanged; those checks then simply report against "no
    board.yaml"/"no workspace resolved from `.`", which is an honest verdict,
    not a skipped one. `venvProvenance` (tan-cli#292) is the exception that
    proves the rule: it emits NO check at all (not even against "no board.yaml")
    when the resolved venv carries no provenance record, which is the common
    case for a workspace alp-sdk's own `bootstrap.sh` set up.

    `boardYaml`'s severity needs one more fact: whether a project was
    actually SELECTED (`--project`/`--board-yaml` given), not merely whether
    the guessed path exists (tan-cli#294 review). `board_yaml` doubles as
    that signal here: the only way it is non-`None` while its file does NOT
    exist is an explicitly-given `--board-yaml` (`doctor()`'s own
    auto-discovery only ever sets it to a path that already `is_file()`), so
    `board_yaml is not None` is a safe proxy for "explicitly given" exactly
    where it matters -- the branch where `present` is False.

    `sdk_tier` -- the `SdkSourceTier` `resolve_sdk_root_ladder` answered
    `sdk_root` with, threaded through so `sdk_check` (tan-cli#301) can name
    it. Optional/defaulted for the same reason every other parameter here is:
    every existing direct caller keeps working, reporting `sdk` with no tier
    parenthetical rather than a guessed one.

    `broken_global_default` (tan-cli#344) -- the raw `sdkPath` a dangling
    `~/.alp/sdk-default` pointer names, computed once by the caller
    (`_broken_global_default`) and threaded straight to `sdk_check`. Optional/
    defaulted like `sdk_tier`; only changes the `sdk` check's remedy text, and
    only in the branch `sdk_root is None` already reaches.
    """
    checks: list[Check] = []

    def _add(check: Check) -> None:
        checks.append(check)
        if on_check is not None:
            on_check(check)

    # tan-cli#294 finding 2: build-environment preflight -- LEADS the report,
    # mirroring Rust's `prepend_doctor_checks(..., probe_build_preflight(...))`:
    # "can a build even start" outranks every host-tool probe below.
    #
    # tan-cli#301: a checkout discoverable from cwd that a HIGHER tier
    # outranked is surfaced too, but ONLY the discovery `sdk_check` itself
    # would have used were nothing above it configured (`discover_sdk_root`,
    # the WIDE walk `resolve_sdk_root_ladder`'s own tail already falls back
    # to) -- reusing that exact helper instead of a second, hand-rolled scan
    # is what keeps this a report-only addition: it can only ever name a
    # candidate the ladder itself already knows how to reach, never invent
    # one of its own. Skipped when the winning tier already IS discovery (or
    # nothing): there is nothing "unselected" left to name.
    unselected_candidate: str | None = None
    if sdk_root is not None and sdk_tier not in (None, "discovery", "none"):
        candidate = discover_sdk_root(Path(workspace_root))
        # `normcase` BOTH sides. `_abs_posix` is `abspath` + slash-swap and
        # deliberately does not resolve, so on Windows the SAME directory
        # spelled with different case -- a `~/.alp/sdk-default` written from a
        # differently-cased `tan sdk switch`, or a differing drive-letter case
        # -- compared unequal and the report told the user to select the SDK
        # that was already selected:
        #     alp-sdk at ...\ws\ALP-SDK (globalDefault; a checkout at
        #     ...\ws\alp-sdk was not selected -- pass --sdk-root ... to use it)
        # A report that lies is the defect class #301 exists to close, so it
        # must not be reintroduced by the fix for it. No-op on POSIX.
        if candidate is not None and os.path.normcase(
            _abs_posix(str(candidate))
        ) != os.path.normcase(_abs_posix(sdk_root)):
            unselected_candidate = str(candidate)
    # tan-cli#407: the #301 block above deliberately says nothing when the
    # winning tier already IS `discovery` -- and that is precisely the case
    # where the second checkout is not merely "unselected" but ACTIVELY in
    # use, by the four commands that take the wide ladder. Same report-only
    # discipline as #301: the candidate can only ever be one
    # `resolve_sdk_root_wide` itself would reach, never a scan of its own.
    divergent_candidate: str | None = None
    if sdk_root is not None and sdk_tier == "discovery":
        wide = resolve_sdk_root_wide(None, Path(workspace_root)).path
        # `normcase` both sides, for the reason spelled out in the #301 block
        # above: `_abs_posix` does not resolve, so on Windows one directory
        # under two spellings would be reported as two checkouts.
        if wide is not None and os.path.normcase(_abs_posix(str(wide))) != os.path.normcase(
            _abs_posix(sdk_root)
        ):
            divergent_candidate = str(wide)
    _add(
        sdk_check(
            sdk_root,
            project_scope,
            sdk_tier,
            unselected_candidate,
            broken_global_default,
            divergent_candidate,
        )
    )
    project_selected = bool(project_scope and project_scope.strip()) or board_yaml is not None
    _add(
        board_yaml_preflight_check(
            board_yaml is not None and Path(board_yaml).is_file(), project_selected
        )
    )
    workspace_path = west_workspace_dir(
        workspace_root, Path(sdk_root) if sdk_root is not None else None
    )
    _add(
        workspace_preflight_check(str(workspace_path) if workspace_path is not None else None)
    )

    # tan-cli#290: `westResolved`, right after `workspace` -- the same order
    # Rust's `build_preflight_checks` uses (`sdk`, `boardYaml`, `workspace`,
    # `westResolved`, `zephyrVersion`). The resolved binary is the SAME one
    # `tan build` would spawn (`tan.core.venv.west_program`): an absolute
    # venv path is trusted directly (`find_workspace_venv` already confirmed
    # it exists), a bare `"west"` fallback is re-checked against PATH, never
    # the reverse -- so a `westResolved` version can never be attributed to a
    # different binary than the one that answered it (tan-cli#123's exact
    # bug, reintroduced by the port and closed here).
    resolved_west = west_program(workspace_root, sdk_root)
    west_resolved_exe = (
        resolved_west if os.path.isabs(resolved_west) else on_path(resolved_west)
    )
    west_resolved_version = (
        _parse_two(probe([west_resolved_exe, "--version"]) or "")
        if west_resolved_exe is not None
        else None
    )
    _add(west_resolved_check(west_resolved_exe, west_resolved_version))

    # tan-cli#292: `venvProvenance`, right beside `westResolved` -- it is a
    # verdict on the SAME resolved venv (`find_workspace_venv`, the search
    # `west_program` itself resolves `west` through), just reading its
    # tan-written provenance record instead of probing the binary.
    venv_path = find_workspace_venv(workspace_root, sdk_root)
    venv_record: WorkspaceSdkRecord | None = None
    if venv_path is not None:
        record_text = _read_text(venv_path.parent / ".west" / "tan-workspace-sdk")
        if record_text is not None:
            venv_record = parse_workspace_sdk_record(record_text)
    provenance_check = venv_provenance_check(venv_record, sdk_root)
    if provenance_check is not None:
        _add(provenance_check)

    if workspace_path is not None:
        workspace_version = None
        version_body = _read_text(workspace_path / "zephyr" / "VERSION")
        if version_body is not None:
            workspace_version = parse_zephyr_version_file(version_body)
        sdk_pin_for_workspace = None
        if sdk_root is not None:
            west_yml_body = _read_text(Path(sdk_root) / "west.yml")
            if west_yml_body is not None:
                sdk_pin_for_workspace = parse_west_zephyr_pin(west_yml_body)
        zephyr_version_check = zephyr_version_preflight_check(
            workspace_version, sdk_pin_for_workspace
        )
        if zephyr_version_check is not None:
            _add(zephyr_version_check)
        # tan-cli#290: unconditional now, sourced from these SAME resolved
        # facts -- see `zephyr_workspace_check`'s docstring for why it still
        # earns its own check beside `zephyrVersion` rather than being
        # dropped as a duplicate.
        _add(zephyr_workspace_check(str(workspace_path), workspace_version))

    # tan-cli#294 finding 1: host-environment checks -- also unconditional
    # HOST facts (no board.yaml/workspace/SDK needed). See their docstrings.
    host_os, host_arch = _host_os_arch_tags()
    _add(zephyr_sdk_host_check(host_os, host_arch))
    if os.name == "nt":
        _add(long_paths_check(_long_paths_enabled(), _git_core_longpaths()))
    _add(
        home_path_check(os.environ.get("USERPROFILE" if os.name == "nt" else "HOME"))
    )

    loaded = _load_manifest(sdk_root)
    facts, source = loaded.facts, loaded.source
    if loaded.error is not None:
        _add(
            Check(
                "bootstrapManifest",
                "warn",
                f"metadata/bootstrap.json rejected: {loaded.error}. Falling back to "
                f"tan's built-in prerequisite list, which may not match this SDK.",
                "Update `tan` or pin an SDK whose metadata/bootstrap.json this "
                "version understands; `tan bootstrap` will refuse outright until then.",
            )
        )

    manifest_floor = _manifest_floor_from_facts(facts)
    # tan-cli#301 (second half): read the SAME resolved workspace `zephyrWorkspace`
    # reports above (`workspace_path`, from the shared `west_workspace_dir`) --
    # NOT a second, independent `$ZEPHYR_BASE` read. A stale exported
    # `$ZEPHYR_BASE` is common (Zephyr's own docs, and this command's own `tan
    # bootstrap` next-steps block, both tell a customer to export it), and
    # reading it here regardless of the resolved workspace is how one report
    # ended up citing two different Zephyrs. `$ZEPHYR_BASE` is consulted only as
    # `zephyr_python_floor`'s fallback, when no workspace resolved at all --
    # mirroring #290's fix for `zephyrWorkspace` itself.
    zephyr_source_base = (
        str(workspace_path / "zephyr")
        if workspace_path is not None
        else os.environ.get("ZEPHYR_BASE")
    )
    zephyr_floor, zephyr_source = zephyr_python_floor(zephyr_source_base)
    # The EFFECTIVE floor: the highest anything in the build chain enforces. The
    # manifest is not the authority here -- it is one of two claimants.
    effective_floor = max(manifest_floor, zephyr_floor)
    effective_source = (
        zephyr_source
        if zephyr_floor >= manifest_floor
        else "alp-sdk metadata/bootstrap.json pythonMinVersion"
    )

    python_found = _probe_host_python(effective_floor)
    _add(python_check(python_found, effective_floor, effective_source))
    skew = python_floor_skew_check(
        manifest_floor,
        effective_floor,
        effective_source,
        manifest_is_real=loaded.is_real,
    )
    if skew is not None:
        _add(skew)

    required = facts.get("windows" if os.name == "nt" else "posix")
    if not isinstance(required, list):
        required = []
    required = [t for t in required if isinstance(t, str)]
    install = facts.get("install")
    platform_key = "windows" if os.name == "nt" else ("macos" if sys.platform == "darwin" else "linux")
    per_tool = install.get(platform_key) if isinstance(install, dict) else None
    if not isinstance(per_tool, dict):
        per_tool = {}
    resolved_install = {k: v for k, v in per_tool.items() if isinstance(v, str)}
    missing_tools = [tool for tool in required if on_path(tool) is None]
    # tan-cli#294 finding 3: reintroduces tan-cli#161. Only reachable once the
    # tool list itself is clean AND a Python actually ran -- mirrors
    # `check_prerequisites`' own order (`crates/tan-cli/src/commands/
    # bootstrap/steps.rs:296-298`): presence first, `ensurepip` only after.
    venv_refusal = None
    if (
        sys.platform.startswith("linux")
        and not missing_tools
        and python_found is not None
        and not _posix_venv_capable(python_found[0].split())
    ):
        venv_refusal = posix_venv_unusable()
    _add(
        prerequisites_check(required, missing_tools, resolved_install, source, venv_refusal)
    )

    west_exe = on_path("west")
    west_version = _parse_two(probe(["west", "--version"]) or "") if west_exe else None
    # tan-cli#299 second half: feed `west_check` the SAME resolved venv path
    # `westResolved` above already computed (`resolved_west`) -- never a
    # second, independent probe -- so "absent from bare PATH, present in the
    # resolved venv" (the default post-bootstrap state) reports `pass`
    # instead of a permanent warn. Only passed when it is a real venv
    # binary (an absolute path); `west_program`'s bare-`"west"` fallback
    # carries no information `west_exe` above does not already have.
    _add(
        west_check(
            west_exe,
            west_version,
            _parse_two(str(facts.get("_pipSpec") or "")),
            resolved_west if os.path.isabs(resolved_west) else None,
        )
    )

    # Unconditional -- not gated on `build` or a resolved board.yaml/SDK. See
    # `zephyr_sdk_check`'s docstring (tan-cli#286).
    zephyr_sdk_ok = _zephyr_sdk_detected()
    _add(zephyr_sdk_check(zephyr_sdk_ok, os.environ.get("ZEPHYR_SDK_INSTALL_DIR")))
    # `sevenZip` rides beside the `zephyrSdk` Fail it unblocks and only there --
    # see `seven_zip_check`'s docstring and tan-cli#204.
    if os.name == "nt" and not zephyr_sdk_ok:
        _add(seven_zip_check(any(on_path(p) for p in SEVEN_ZIP_PROGRAMS)))

    _add(
        setools_check(
            os.environ.get("SETOOLS_DIR"),
            os.environ.get("SE_UART"),
            _has_module("fdt"),
            sys.platform.startswith("linux"),
        )
    )

    jlink_exe = next(
        (found for name in ("JLinkExe", "JLink", "JLinkGDBServerCL") if (found := on_path(name))),
        None,
    )
    # `-?` prints the banner and exits; with stdin closed it cannot sit waiting
    # for a probe that is not plugged in, and the timeout bounds it regardless.
    jlink_version = _parse_two(probe([jlink_exe, "-?"]) or "") if jlink_exe else None
    resolved_device, device_source = jlink_flash_device(sdk_root)
    _add(jlink_check(jlink_exe, jlink_version, resolved_device, device_source))

    # tan-cli#294 finding 5: LAST, mirroring `assemble_doctor_report`'s own
    # placement -- traces a report back to the SDK checkout that produced it.
    if sdk_root is not None:
        _add(sdk_provenance_check(sdk_root))

    return checks


def _has_module(name: str) -> bool:
    """Importability without importing. `find_spec` raises on a half-installed
    package (`ValueError`) or a broken meta-path finder, which must read as
    'absent', not as a doctor crash."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _generated_at() -> str:
    """`SOURCE_DATE_EPOCH` when set, so a captured envelope is reproducible --
    `tan.core.timestamp`, which NEVER raises.

    An out-of-range epoch (the MILLISECONDS case) used to throw from here, and
    the caller's own try/except then reported `doctor.internal-failure`: a
    fabricated "tan is broken" verdict on a host that was diagnosed fine.
    """
    return generated_at_iso()


def doctor(
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    build: bool = typer.Option(
        False,
        "--build",
        help="Accepted for compatibility (tan-cli#290): zephyrWorkspace, the check "
        "this used to gate, now runs unconditionally, so this flag no longer "
        "changes the check list.",
    ),
    fix: bool = typer.Option(
        False,
        "--fix",
        help="Run the manifest's own install command (ADR 0021) for any "
        "hostPrerequisites tool this host is missing, when it needs no "
        "elevation. A command that needs `sudo` is printed, never run -- tan "
        "never spawns sudo. Only in an interactive, non-CI, text-mode run "
        "(--non-interactive/--ci/--format json all disable it): a repair a "
        "human did not watch happen is not consent.",
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Never prompt, and never run --fix's repairs -- see --fix.",
    ),
    ci: bool = typer.Option(
        False, "--ci", help="CI mode: implies --non-interactive and disables --fix."
    ),
    no_color: bool = typer.Option(False, "--no-color", help="Disable ANSI colour."),
) -> None:
    """Diagnose whether this host can build and flash."""
    json_mode = output_format == "json"

    # Snapshot the RAW `--project` value before `project` is reassigned below
    # to the envelope's `Project` object -- `sdk_check`'s scoped-switch hint
    # (tan-cli#294 finding 2 / #101) needs the string, not the envelope block.
    project_scope = project

    # `util::cli_workspace_root`: `--project` joined onto the cwd, and
    # everything below (board.yaml discovery, SDK discovery, the reported
    # `project.root`) anchors on THAT -- see `build_cmd.build` for the same
    # pattern and why an unanchored `--project` builds the wrong project.
    cwd = Path.cwd()
    workspace_root = cwd if project is None else Path(os.path.join(str(cwd), project))

    # Anchor an EXPLICIT `--board-yaml` on `workspace_root`, not the real cwd,
    # BEFORE the discovery branch below -- same pattern as `build_cmd.build`
    # and `crates/tan-core/src/project.rs:198-208`'s `resolve_board_yaml_path`.
    # Left unanchored, a relative `--board-yaml` under `--project app` reports
    # (and would build/flash) the board.yaml sitting in the real cwd instead
    # of the one inside `app`.
    if board_yaml is not None and not os.path.isabs(board_yaml):
        board_yaml = os.path.join(str(workspace_root), board_yaml)
    if board_yaml is None and (workspace_root / "board.yaml").is_file():
        board_yaml = str(workspace_root / "board.yaml")
    # `--sdk-root` > `.alp/sdk-path` project pin > machine-global default >
    # the positional walk (`resolve_sdk_root_ladder`) -- no `ALP_SDK_ROOT`
    # tier (tried and reverted -- see `resolve_sdk_root_ladder`'s own
    # docstring). Previously this skipped straight from `--sdk-root` to the
    # positional walk, silently ignoring `tan init`'s own pointer in the same
    # directory.
    sdk_resolution = resolve_sdk_root_ladder(sdk_root, workspace_root)
    resolved_sdk_root = sdk_resolution.path
    sdk_tier = sdk_resolution.tier
    sdk_broken_pin = sdk_resolution.broken_project_pin
    sdk_foreign_default = sdk_resolution.foreign_global_default_for
    sdk_root = str(resolved_sdk_root) if resolved_sdk_root is not None else None
    sdk = SdkInfo(sdk_root, sdk_tier) if sdk_root is not None else None
    # tan-cli#344: a dangling `~/.alp/sdk-default` is a distinct fact from
    # "nothing configured" -- computed unconditionally (one small file read)
    # so `sdk_check` can name it in the one branch (`sdk_root is None`) where
    # the two used to print the identical sentence.
    broken_global_default = _broken_global_default()
    # Forward slashes -- the established envelope contract on this seam
    # (`build_cmd.build`, `flash_cmd._resolve_project`), not the native
    # separators `str(Path(...))` would emit on Windows.
    #
    # tan-cli#236: `boardYaml` reported only when the file really exists. An
    # explicit `--board-yaml` skips the `is_file()` discovery guard above, so
    # without this it could still name a path nothing sits at.
    project = Project.resolved(
        _abs_posix(str(workspace_root)),
        _abs_posix(board_yaml) if board_yaml is not None else None,
    )

    # Text mode streams each check the MOMENT it completes (tan-cli doctor
    # v2, change 3): `_collect` runs up to 15 subprocess probes at
    # `PROBE_TIMEOUT_S` each, and the old code printed nothing until every
    # one of them had answered -- on a host where one wedges, a blank
    # terminal names nothing. `width`/`color` are resolved ONCE, here, before
    # the first check can print, never re-resolved per check; JSON mode
    # streams nothing; still exactly one envelope, at the end. Width floored
    # at `_DOCTOR_TEXT_MIN_WIDTH` -- a 20-column terminal must not fall to
    # one word per line -- and the fallback (piped/redirected stdout) matches
    # `build_cmd`'s own `shutil.get_terminal_size` convention.
    stream = not json_mode
    if stream:
        width = max(shutil.get_terminal_size(fallback=(100, 24)).columns, _DOCTOR_TEXT_MIN_WIDTH)
        color = use_color(no_color, ci)

    def _print_check(check: Check) -> None:
        for line in render_check_lines(check.as_dict(), width, color):
            print(line, file=sys.stderr)

    try:
        checks = _collect(
            sdk_root,
            build=build,
            board_yaml=board_yaml,
            project_scope=project_scope,
            workspace_root=str(workspace_root),
            sdk_tier=sdk_tier,
            broken_global_default=broken_global_default,
            on_check=_print_check if stream else None,
        )
        # tan-cli#91 / ADR 0021: `--fix` only ever RUNS anything when a human
        # is demonstrably present. `doctor` otherwise only REPORTS; this flag
        # turns it into a machine-global, network-fetching installer, so the
        # consent gate is the feature, not decoration around it.
        #
        # Delegated to [`tan.core.consent.can_prompt`] rather than spelled out
        # inline, because spelling it out inline is exactly how this went
        # wrong: the hand-written form tested only the three FLAGS
        # (`!non_interactive && !ci && !is_json`) and omitted the two
        # `isatty()` calls, so a CI runner that redirected its output but did
        # not happen to pass `--ci` got unattended host mutation -- measured
        # under fully captured pipes, four real `winget install` runs with
        # nobody watching. The oracle's own `--non-interactive` help states
        # the missing half ("the same rule applies unasked when stdin or
        # stderr is not a terminal -- piped, redirected, or a CI runner").
        # See that module for why BOTH handles matter, and why `stdout`
        # deliberately does not.
        fix_allowed = fix and can_prompt(non_interactive=non_interactive, ci=ci, json_mode=json_mode)
        if fix_allowed:
            missing_for_fix = next(
                (c.missing for c in checks if c.name == "hostPrerequisites"), None
            )
            if missing_for_fix:
                # These checks stream too, same as every check `_collect`
                # produced above -- gated on `stream` directly (never on
                # "`fix_allowed` already implies `not json_mode`": that
                # inference only holds for the REAL `can_prompt`, and a test
                # double stubbing `can_prompt` can make `fix_allowed` true
                # under `--format json`, where `width`/`color` were never
                # resolved).
                fix_checks = run_fix(missing_for_fix)
                if stream:
                    for fix_check in fix_checks:
                        _print_check(fix_check)
                checks = [*checks, *fix_checks]
        exit_code = exit_code_for(checks)
        issues = checks_to_issues(checks)
        # tan-cli#91 P1: `--fix` requested and consent refused used to be a
        # SILENT no-op, byte-for-byte identical to plain `tan doctor` --
        # measured against the oracle (`doctor --fix --format json`, which the
        # oracle instead refuses to parse outright). SAY SO instead: name
        # every condition of `can_prompt`'s that actually tripped.
        if fix and not fix_allowed:
            issues = [
                *issues,
                fix_suppressed_issue(non_interactive=non_interactive, ci=ci, json_mode=json_mode),
            ]
        # tan-cli#294 finding 4 (#203/#210, alp-sdk-vscode#347, ADR 0021 P0a):
        # `hostPrerequisites` is the only check that ever carries a
        # `{tool, command}` pair, so it is the only place this reads from --
        # mirrors `apply_prerequisite_check`'s report-level field. `alp-sdk-
        # vscode`'s `runDependencyAction` sends `missingPrerequisites[].command`
        # to a terminal; without this key that one-click affordance silently
        # disappears on the extension side (the extension itself does not
        # crash on absence -- it feature-detects on the key, per
        # `vscodeAdapter.ts`).
        missing_prerequisites = next(
            (c.missing for c in checks if c.name == "hostPrerequisites"), None
        )
        data = {
            "generatedAt": _generated_at(),
            "summary": summarise(checks),
            "checks": [c.as_dict() for c in checks],
            "nextSteps": next_steps(checks),
            "missingPrerequisites": missing_prerequisites,
        }
    except Exception as err:  # noqa: BLE001
        # The port's most-repeated defect class: an uncaught exception escapes as
        # a raw traceback, stdout stays empty, and the extension renders nothing
        # with no error on either side. Every probe above is already guarded, so
        # anything reaching here is a tan bug -- reported as one, with an
        # envelope. INTERNAL_FAILURE, not DOCTOR_FAILURE: the host was never
        # diagnosed, and claiming it is unhealthy would be a fabricated verdict.
        exit_code = ExitCode.INTERNAL_FAILURE
        data = None
        issues = [Issue("doctor.internal-failure", "error", f"{type(err).__name__}: {err}")]

    # tan-cli#263 review: this is the "tan doctor says ready, 0 issues"
    # report -- a `.alp/sdk-path` pin that silently missed must show up here,
    # not just on a `sdk current` a suspicious operator has to think to run.
    pin_issue = project_pin_issue(sdk_broken_pin, sdk_tier)
    if pin_issue is not None:
        issues = [pin_issue, *issues]
    foreign_issue = global_default_foreign_project_issue(sdk_foreign_default)
    if foreign_issue is not None:
        issues = [foreign_issue, *issues]

    if json_mode:
        emit(Envelope("doctor", project, data, issues, exit_code, sdk=sdk))
    elif data is None:
        for issue in issues:
            print(f"{issue.severity}: {issue.message}", file=sys.stderr)
    else:
        # tan-cli#375: an Issue with no backing Check -- a suppressed `--fix`
        # (`fix_suppressed_issue`) or a broken `.alp/sdk-path` pin
        # (`project_pin_issue`, tan-cli#263) -- used to vanish here
        # completely: this branch (the non-exception path) used to print
        # only the per-check lines and the summary, never `issues`. `--fix`'s
        # consent gate (tan-cli#91) is right to suppress silently TOWARDS THE
        # HOST -- it must never mutate anything unwatched -- but silence
        # towards the CUSTOMER is a different bug: the JSON envelope already
        # carried `doctor.fix-suppressed`/`sdk.project-pin-unresolved` and
        # text mode carried neither. `checks_to_issues(checks)` already turns
        # every warn/fail Check into an Issue, and each of those already
        # rendered as its own `[  warn]`/`[  fail]` block, streamed above as
        # `_collect` produced it -- reprinting them here would duplicate the
        # whole report, so this filters down to exactly the codes no Check
        # block already named. `render_doctor_footer` places these AFTER
        # every check block (already on screen) and BEFORE the summary:
        # findable without being buried mid-report, report still ends on the
        # summary line.
        # `kebab_check_name`, not the bare `c.name`: tan-cli#461 made the
        # camelCase-to-kebab conversion the one shared way a `Check.name`
        # becomes an issue-code suffix, so the set built here must be spelled
        # the same way `checks_to_issues` spells the codes it is filtering
        # against -- otherwise every check-backed issue misses the set and
        # the whole report prints twice.
        checked_codes = {c.code or f"doctor.{kebab_check_name(c.name)}" for c in checks}
        extra_issue_lines = [
            f"{issue.severity}: {issue.message}"
            for issue in issues
            if issue.code not in checked_codes
        ]
        for line in render_doctor_footer(data["checks"], data["summary"], extra_issue_lines):
            print(line, file=sys.stderr)

    raise typer.Exit(int(exit_code))


# tan-cli#261: adds the four oracle `GlobalArgs` flags this command was still
# missing (`--all`/`--quiet`/`--target`/`--verbose`) on top of `--no-color`/
# `--non-interactive`/`--ci`, already declared and wired into `can_prompt`
# above; see `tan.core.global_flags`.
doctor = accept_global_flags(doctor)
