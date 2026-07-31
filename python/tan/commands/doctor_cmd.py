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

**`--build` is accepted, real, and additive -- not the Rust oracle's second,
disjoint check vocabulary.** Measured against a real `tan.exe`, plain `tan
doctor` and `tan doctor --build` run two almost entirely different check
lists (debug-readiness vs. zephyr/yocto/baremetal build-readiness -- compare
`tan doctor`'s `workspaceRoot`/`codeLLDBExtension`/`lldb` against `tan doctor
--build`'s `git`/`cmake`/`ninja`/`dtc`/`gperf`/`vendorToolchain`/...). Byte-
parity with BOTH of those lists is a second command's worth of new checks,
not a flag gap -- and this port's own check list (`hostPython`/
`hostPrerequisites`/`west`/`setools`/`jlink`) is ALREADY build/flash-oriented
by design (see above), unlike the Rust oracle's PLAIN `doctor`. So `--build`
here means what it says on this port's own terms: it turns on ONE extra,
genuinely-probed check plain `tan doctor` does not run --
`zephyrWorkspace`, whether `$ZEPHYR_BASE` resolves to a real Zephyr checkout
matching alp-sdk's `west.yml` pin -- rather than either silently doing
nothing (the anti-pattern this whole command exists to avoid) or
re-deriving the oracle's second, disjoint vocabulary from scratch. Both
`alp-sdk-vscode` call sites (`["doctor", "--build"]`,
`["doctor", "--build", "--fix"]`) still run this port's normal checks PLUS
`zephyrWorkspace`.

`--fix` is a separate, NOT-yet-ported flag gap (it is not part of this one):
the oracle's `--build --fix` auto-repairs a missing Zephyr workspace by
running `tan bootstrap`, and nothing here does that yet.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import typer

from tan.commands.build_cmd import _abs_posix, resolve_sdk_root_ladder
from tan.core.bootstrap import parse_west_zephyr_pin, parse_zephyr_version_file
from tan.core.timestamp import generated_at_iso
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode

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
    """

    name: str
    status: str
    detail: str
    fix: str | None = None
    code: str | None = None

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
    `JLINK_AEN_DEVICE` is only the FALLBACK for a host with no SDK checkout
    resolved yet, mirroring `zephyr_python_floor`'s shape -- kept byte-identical
    to today's metadata value so a doctor run with no `--sdk-root` still names
    the right part instead of a stale one.

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
    if sdk_root:
        path = Path(sdk_root) / "metadata" / "socs" / "alif" / "ensemble" / "e8.json"
        text = _read_text(path)
        if text is not None:
            try:
                doc = json.loads(text)
            except ValueError:
                doc = None
            if isinstance(doc, dict):
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
        f"tan's built-in fallback {JLINK_AEN_DEVICE} -- no alp-sdk checkout "
        "resolved to read metadata/socs/alif/ensemble/e8.json "
        "variants[].debug.jlink_flash_device from"
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
    are supported. Saying which number came from which file is the whole value:
    the fix belongs in `metadata/bootstrap.json`, not on the customer's machine.

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
            f"Raise `prerequisites.pythonMinVersion` to {_fmt(effective_floor)} in "
            f"alp-sdk's metadata/bootstrap.json (and re-run its "
            f"scripts/check_bootstrap_manifest.py drift gate)."
        )
    else:
        claim = (
            f"no alp-sdk metadata/bootstrap.json was read (no SDK checkout resolved, "
            f"or this SDK predates it), so tan's own built-in floor {_fmt(manifest_floor)} "
            f"is standing in"
        )
        fix = (
            "Resolve or pin an alp-sdk checkout (`tan sdk switch <version|path>`) so its "
            "own metadata/bootstrap.json pythonMinVersion is read instead of tan's "
            "built-in floor."
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
    checked: list[str], missing: list[str], install: dict[str, str], source: str
) -> Check:
    """`hostPrerequisites` -- the manifest's own tool list, on PATH.

    Mirrors `tan_core::bootstrap::doctor_prerequisite_check`, including that the
    per-tool install commands come from the manifest rather than being spelled
    here: they are per-platform facts alp-sdk owns.
    """
    if not missing:
        return Check(
            "hostPrerequisites", "pass", f"{', '.join(checked)} present ({source})."
        )
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
    )


def west_check(
    found: str | None, version: tuple[int, int] | None, floor: tuple[int, int] | None
) -> Check:
    """`west` -- present, and at the manifest's floor.

    `Fail` when absent: `west` is how every slice of every plan is executed, so
    without it nothing builds. Only WARN on an old or unreadable version -- west
    is forward-compatible in practice and refusing a host on a version string we
    could not parse is a worse failure than letting the real invocation report
    its own.
    """
    if found is None:
        return Check(
            "west",
            "fail",
            "`west` is not on PATH; every build slice is executed through it.",
            "Run `tan bootstrap`, or activate the workspace venv it created "
            "(its `bin`/`Scripts` directory holds the `west` launcher).",
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


def zephyr_workspace_check(
    zephyr_base: str | None, version_text: str | None, sdk_pin: str | None
) -> Check:
    """`zephyrWorkspace` -- `--build`-only: does `$ZEPHYR_BASE` actually
    resolve to a Zephyr checkout, and does its version match what alp-sdk's
    own `west.yml` pins?

    Nothing else in this command asks. `hostPrerequisites`/`west` only check
    that the TOOLS needed to build are on PATH -- neither confirms a Zephyr
    tree is actually there to build against, or that it is the right one.
    `--build` exists to dig one level deeper than plain `tan doctor` does
    (see the module docstring); this is that extra level. WARN, never FAIL:
    every other check already fails outright when a workspace is entirely
    absent (`hostPrerequisites`/`west`), so this stays advisory -- a stale or
    unresolved Zephyr pin is fixed by `tan bootstrap`, not a reason to refuse
    the whole preflight.
    """
    if zephyr_base is None:
        return Check(
            "zephyrWorkspace",
            "warn",
            "no $ZEPHYR_BASE Zephyr workspace resolved.",
            "Run `tan bootstrap` to create one.",
        )
    if version_text is None:
        return Check(
            "zephyrWorkspace",
            "warn",
            f"$ZEPHYR_BASE=`{zephyr_base}` does not look like a Zephyr checkout "
            f"(no readable VERSION file).",
            "Run `tan bootstrap`, or point $ZEPHYR_BASE at a real Zephyr checkout.",
        )
    if sdk_pin is not None and version_text != sdk_pin:
        return Check(
            "zephyrWorkspace",
            "warn",
            f"Zephyr {version_text} at $ZEPHYR_BASE=`{zephyr_base}` does not match "
            f"alp-sdk's pinned {sdk_pin} (from west.yml).",
            "Run `tan bootstrap` to reuse or refresh a matching workspace.",
        )
    return Check(
        "zephyrWorkspace", "pass", f"Zephyr {version_text} at $ZEPHYR_BASE=`{zephyr_base}`."
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


def checks_to_issues(checks: list[Check]) -> list[Issue]:
    """Warn/fail checks become issues; `unknown` raises none (it is not a
    problem, the question was simply not askable). The code is the check's own
    when it has one -- the frozen `bootstrap.*` spellings -- else Rust's
    `doctor.<checkName>` convention."""
    return [
        Issue(
            check.code or f"doctor.{check.name}",
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


def _collect(sdk_root: str | None, build: bool = False) -> list[Check]:
    """Every probe, in report order. Nothing here may raise -- see the module
    docstring; `probe`/`on_path`/`_read_text` are the only three ways this
    module touches the outside world and none of them can.

    `build` (`--build`) appends `zephyrWorkspace` on top of the checks plain
    `tan doctor` already runs -- see `zephyr_workspace_check`'s doc comment
    for why that one check, and only that one, is gated on the flag rather
    than always running.
    """
    checks: list[Check] = []

    loaded = _load_manifest(sdk_root)
    facts, source = loaded.facts, loaded.source
    if loaded.error is not None:
        checks.append(
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
    zephyr_floor, zephyr_source = zephyr_python_floor(os.environ.get("ZEPHYR_BASE"))
    # The EFFECTIVE floor: the highest anything in the build chain enforces. The
    # manifest is not the authority here -- it is one of two claimants.
    effective_floor = max(manifest_floor, zephyr_floor)
    effective_source = (
        zephyr_source
        if zephyr_floor >= manifest_floor
        else "alp-sdk metadata/bootstrap.json pythonMinVersion"
    )

    checks.append(
        python_check(_probe_host_python(effective_floor), effective_floor, effective_source)
    )
    skew = python_floor_skew_check(
        manifest_floor,
        effective_floor,
        effective_source,
        manifest_is_real=loaded.is_real,
    )
    if skew is not None:
        checks.append(skew)

    required = facts.get("windows" if os.name == "nt" else "posix")
    if not isinstance(required, list):
        required = []
    required = [t for t in required if isinstance(t, str)]
    install = facts.get("install")
    platform_key = "windows" if os.name == "nt" else ("macos" if sys.platform == "darwin" else "linux")
    per_tool = install.get(platform_key) if isinstance(install, dict) else None
    if not isinstance(per_tool, dict):
        per_tool = {}
    checks.append(
        prerequisites_check(
            required,
            [tool for tool in required if on_path(tool) is None],
            {k: v for k, v in per_tool.items() if isinstance(v, str)},
            source,
        )
    )

    west_exe = on_path("west")
    west_version = _parse_two(probe(["west", "--version"]) or "") if west_exe else None
    checks.append(west_check(west_exe, west_version, _parse_two(str(facts.get("_pipSpec") or ""))))

    if build:
        zephyr_base = os.environ.get("ZEPHYR_BASE")
        version_text = None
        if zephyr_base:
            body = _read_text(Path(zephyr_base) / "VERSION")
            if body is not None:
                version_text = parse_zephyr_version_file(body)
        sdk_pin = None
        if sdk_root is not None:
            west_yml = _read_text(Path(sdk_root) / "west.yml")
            if west_yml is not None:
                sdk_pin = parse_west_zephyr_pin(west_yml)
        checks.append(zephyr_workspace_check(zephyr_base, version_text, sdk_pin))

    checks.append(
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
    checks.append(jlink_check(jlink_exe, jlink_version, resolved_device, device_source))

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
        help="Also run the build-readiness check (zephyrWorkspace: is $ZEPHYR_BASE a real, SDK-matching Zephyr checkout).",
    ),
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """Diagnose whether this host can build and flash."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

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
    resolved_sdk_root, sdk_tier = resolve_sdk_root_ladder(sdk_root, workspace_root)
    sdk_root = str(resolved_sdk_root) if resolved_sdk_root is not None else None
    sdk = SdkInfo(sdk_root, sdk_tier) if sdk_root is not None else None
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

    try:
        checks = _collect(sdk_root, build=build)
        exit_code = exit_code_for(checks)
        issues = checks_to_issues(checks)
        data = {
            "generatedAt": _generated_at(),
            "summary": summarise(checks),
            "checks": [c.as_dict() for c in checks],
            "nextSteps": next_steps(checks),
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

    if json_mode:
        emit(Envelope("doctor", project, data, issues, exit_code, sdk=sdk))
    else:
        for check in (data or {}).get("checks", []):
            fix = f"\n    fix: {check['fix']}" if "fix" in check else ""
            print(f"[{check['status']:>7}] {check['name']}: {check['detail']}{fix}", file=sys.stderr)
        if data is None:
            for issue in issues:
                print(f"{issue.severity}: {issue.message}", file=sys.stderr)
        else:
            s = data["summary"]
            print(
                f"\n{s['pass']} passed, {s['warn']} warning(s), {s['fail']} failed.",
                file=sys.stderr,
            )
    raise typer.Exit(int(exit_code))
