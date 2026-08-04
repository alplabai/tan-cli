# SPDX-License-Identifier: Apache-2.0
"""`tan bootstrap` -- set up the SDK's build environment, natively.

The FIRST command a customer runs, on a machine with nothing set up, so its
failure modes are the first impression of the whole product. Create the
workspace venv, install west into it, `west init -l` / `west update` the Zephyr
workspace beside the alp-sdk checkout, install the Python deps.

**No `bash`, and no shelling the SDK's scripts.** Native Windows is a
first-class host, so `scripts/bootstrap.sh` + `scripts/bootstrap.ps1` are the
parity oracle for CONTROL FLOW and message strings, not a runtime dependency --
the same rule invariant **I-32** and anti-pattern **22** of
`docs/superpowers/specs/2026-07-29-tan-port-invariants.md` record for
`tan init`'s vendored scaffold tree: a command that shells the SDK acquires a
checkout dependency it deliberately does not have, invisibly to every parity
gate. The FACTS come from `<sdkRoot>/metadata/bootstrap.json`, which invariant
**I-64** names a live tan consumer contract. Decision logic lives in
`tan.core.bootstrap`; this file is probes, subprocesses and the envelope.

**The Python floor is FIXED here, not ported.** Three verified facts compose
into a silent, customer-facing failure:

1. `metadata/bootstrap.json:16` -- `"pythonMinVersion": "3.10"`.
2. Zephyr's `cmake/modules/python.cmake:14` -- `set(PYTHON_MINIMUM_REQUIRED
   3.12)` (verified on the pinned v4.4 tree; `find_package(Python3
   ${PYTHON_MINIMUM_REQUIRED} REQUIRED)` on line 41 is what aborts).
3. `crates/tan-cli/src/commands/bootstrap/steps.rs:230-234` -- the POSIX branch
   states outright *"this branch cannot fail on version"*. The Windows branch,
   `:217-223`, DOES refuse.

Ubuntu 22.04 ships `python3` = 3.10. So on the oracle today: `tan bootstrap`
succeeds, `tan doctor` reports Pass on the manifest floor, and the customer's
FIRST build dies inside Zephyr's CMake configure with an error naming Zephyr
rather than us. This port enforces the EFFECTIVE floor -- the higher of the two
-- on BOTH platforms, computed by calling `tan doctor`'s own
`zephyr_python_floor` with the same argument, so the two commands cannot
disagree by construction, and reports the skew as
`bootstrap.python-floor-skew` so the fix lands in the manifest instead of on the
customer.

**Nothing here may raise.** Seven Criticals in this port were uncaught
exceptions escaping the error contract: a raw traceback, an EMPTY stdout, and an
extension that renders nothing with no error on either side. Every filesystem
read, every subprocess and every env-var read below is guarded, and `run()`
wraps the whole command in a last-resort envelope. Every subprocess has a
timeout.

**Text mode writes to stderr only.** stdout is the envelope channel; a single
stray byte there breaks the extension silently.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer

from tan.commands.build_cmd import resolve_sdk_root_ladder
from tan.commands.doctor_cmd import (
    FALLBACK_PYTHON_FLOOR,
    _read_text,
    on_path,
    probe,
    zephyr_python_floor,
)
from tan.commands.presets_cmd import parse_som_preset, resolve_project_paths
from tan.commands.sdk_cmd import (
    NO_SDK_NEXT_STEPS,
    SDK_MARKER,
    _home_alp_dir,
    global_default_pointer_fix_hint,
    project_pin_issue,
)
from tan.core.bootstrap import (
    BOOTSTRAP_MANIFEST_REL_PATH,
    DEFAULT_WORKSPACE_DIR_NAME,
    GATE_REFUSE,
    GATE_WARN,
    LINUX,
    MANIFEST_MISMATCH,
    REUSE,
    STALE,
    WINDOWS,
    BootstrapFacts,
    BootstrapManifestError,
    PrereqFailure,
    Tokens,
    capture_tail,
    completion_verdict,
    decide_workspace_reuse,
    detect_host_os,
    die,
    fallback_facts,
    get_manifest_path,
    in_play_runtimes,
    next_steps_block,
    optional_libs_block,
    os_label,
    parent_needs_workspace_guard,
    parse_bootstrap_manifest,
    posix_python_not_runnable,
    posix_refusal,
    posix_venv_unusable,
    print_env_block,
    python_candidates,
    python_ceiling_warning,
    python_floor_skew_warning,
    python_too_old,
    reported_missing,
    resolve_workspace_target,
    resolve_zephyr_pin,
    set_manifest_path,
    venv_exe_names,
    windows_python_not_runnable,
    windows_refusal,
    workspace_sdk_record_json,
    yocto_gate,
    yocto_mixed_warning,
    yocto_only_refusal,
    zephyr_requirements_hint,
)
from tan.core.global_flags import accept_global_flags
from tan.core.scaffold import sdk_pointer_json
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

#: `data.schemaVersion`. `"2"`, a STRING: v1's `scriptPath` named
#: `<sdkRoot>/scripts/bootstrap.sh`, which this command does not run.
DATA_SCHEMA_VERSION = "2"

#: Seconds a short probe (an interpreter version, `west help`, `pip --version`)
#: may take. Generous for a cold `west` import, short enough that a hung tool
#: cannot wedge the command.
PROBE_TIMEOUT_S = 120

#: Seconds an INSTALL step may take. `west update` clones Zephyr + every HAL on
#: a cold cache and pip builds wheels from source, so this is minutes, not
#: seconds -- but it is bounded, because an unbounded child is how a CI job dies
#: at the runner's own timeout with no diagnostic.
INSTALL_TIMEOUT_S = 3600


# ---------------------------------------------------------------------------
# Guarded IO
# ---------------------------------------------------------------------------


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _native(path: Path | str) -> str:
    """`path` with this platform's separator. The resolved project paths are
    forward-slash on every OS, which would print as `C:/dev/ws\\zephyr` in the
    Windows copy-paste blocks."""
    rendered = str(path)
    return rendered.replace("/", "\\") if os.name == "nt" else rendered


def _same_directory(a: Path, b: Path) -> bool:
    """True when `a` and `b` name the same directory. `realpath` when both exist
    (the reliable answer); a lexical `normpath`+`normcase` comparison when either
    does not -- e.g. a stale config's target SDK version since pruned."""
    try:
        if a.exists() and b.exists():
            return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(
        os.path.normpath(str(b))
    )


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


#: The `pip_phase` warning codes after which the workspace cannot do what it
#: was bootstrapped for (tan-cli#220 / tan-cli#285), ported from the Rust
#: oracle's `WORKSPACE_BLOCKING`
#: (`crates/tan-cli/src/commands/bootstrap/steps.rs`). Each phase stays
#: non-fatal on its own -- the run continues and the workspace stays on disk
#: -- but `completion_verdict` refuses to call a run that hit one of these
#: `complete.` (unless `--allow-partial`).
#:
#: `pip-upgrade` is deliberately ABSENT, matching the oracle: it upgrades
#: pip/wheel themselves, and the pip that was already there still installs
#: packages -- the one genuinely cosmetic member of the phase.
#:
#: tan-cli#284 review minor (bootstrap_cmd.py:195): `west-config-reconcile-
#: failed` is also absent, so a run whose OWN warning says "`west update`
#: will resolve the manifest from whatever that pointer still names" (see
#: `reconcile_west_manifest_path`'s "failed" branch below) still prints
#: `bootstrap: complete.` at exit 0. Left as-is deliberately: it is faithful
#: to this being a byte-for-byte port of the oracle's 3-element const, so
#: adding a 4th member would be a parity DIVERGENCE, not a bug fix -- flagged
#: for the maintainer to decide, not changed unilaterally here.
WORKSPACE_BLOCKING: tuple[str, ...] = ("zephyr-requirements", "sdk-extras", "editable-install")


class Log:
    """Progress reporter. Text mode streams live to stderr (pip/west take
    minutes, so a summary at the end would look like a hang); JSON mode stays
    silent so the single stdout envelope is the only output.

    Warnings are RECORDED as well as printed. Print-only meant a JSON run where
    the Zephyr requirements, the SDK extras AND the editable install all failed
    still emitted `ok:true, exitCode:0, issues:[]` -- every non-fatal failure
    silently swallowed.
    """

    def __init__(self, json_mode: bool) -> None:
        self.json = json_mode
        self.warnings: list[tuple[str, str]] = []

    def line(self, message: str) -> None:
        """One progress line (the scripts' `info`/`ok`)."""
        if not self.json:
            _eprint(f"bootstrap: {message}")

    def warn(self, code: str, message: str) -> None:
        """Emit AND record a non-fatal warning. `code` becomes the envelope
        issue code `bootstrap.<code>`."""
        self.line(message)
        self.warnings.append((code, message))

    def blocking(self) -> list[str]:
        """The recorded warning codes that mean the WORKSPACE IS NOT USABLE,
        in the order they were raised (tan-cli#220 / tan-cli#285) -- see
        `WORKSPACE_BLOCKING`. Empty when the run only hit cosmetic problems.

        Derived from `WORKSPACE_BLOCKING` membership rather than a per-call
        `degraded=` flag: a flag is opt-in at every call site, so a future
        warn that SHOULD block defaults to not blocking unless its author
        remembers to say so -- the same fail-open shape as the defect this
        mechanism exists to close. One data set, consulted here, cannot drift
        from itself.
        """
        return [code for code, _ in self.warnings if code in WORKSPACE_BLOCKING]

    def take_issues(self, *, escalate_blocking: bool = False) -> list[Issue]:
        """Drain the recorded warnings as envelope issues. Draining is
        idempotent -- no double-reporting on a second call.

        `escalate_blocking` promotes the `WORKSPACE_BLOCKING` codes to
        `severity: "error"` -- set when they actually blocked the verdict,
        i.e. the run refused to report success over them (tan-cli#285): an
        envelope that exits non-zero while every issue in it says `warning`
        invites a consumer to treat the whole thing as advisory. Under
        `--allow-partial` they stay `warning`, because the customer was told
        and chose to proceed, and an `error` on a run that exits 0 is its own
        kind of lie.
        """
        issues = [
            Issue(
                f"bootstrap.{code}",
                "error" if escalate_blocking and code in WORKSPACE_BLOCKING else "warning",
                msg,
            )
            for code, msg in self.warnings
        ]
        self.warnings = []
        return issues


def _write_line(line: str, stream) -> None:
    """Write one line, never raising.

    A `UnicodeEncodeError` is a real possibility: the POSIX next-steps block
    carries `⏳ / 🟡 / ✅` verbatim from the oracle, and a console on a legacy
    code page cannot encode them. Losing a progress line is acceptable; killing
    the command over one is not.
    """
    try:
        print(line, file=stream)
    except (UnicodeEncodeError, OSError):
        try:
            print(line.encode("ascii", "replace").decode("ascii"), file=stream)
        except OSError:
            pass


def _eprint(line: str) -> None:
    """stderr, never stdout. Every human line this command prints goes here, so
    stdout stays free for the `--format json` envelope -- except `--print-env`,
    which is the one deliberate exception (`_outprint`)."""
    _write_line(line, sys.stderr)


def _outprint(line: str) -> None:
    """stdout -- ONLY for `--print-env`, whose entire contract is redirection.

    `tan bootstrap --print-env > env.sh`, or the `| tee` the getting-started job
    runs, capture STDOUT. Emitting the `export`/`source` block on stderr like
    every other human line leaves the redirect target EMPTY while the block
    still appears on the terminal, so it reads as working and silently writes
    nothing -- and `sh -n` passes on an empty file, because an empty shell
    script is a valid one. That combination is what had the CI step assert
    nothing at all while reporting green (tan-cli#296).

    Text mode only. Under `--format json` stdout carries exactly one envelope
    and this is never reached.
    """
    _write_line(line, sys.stdout)


# ---------------------------------------------------------------------------
# The host interpreter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostPython:
    """A host interpreter that was probed and actually RAN."""

    argv: tuple[str, ...]
    version: tuple[int, int]

    def display(self) -> str:
        """How to spell it in a message (`py -3`, `python3`, ...)."""
        return " ".join(self.argv)


def probe_host_python(minimum: tuple[int, int]) -> HostPython | None:
    """Walk `python_candidates` and take the first that RUNS and is at least
    `minimum`, falling back to the first that merely ran -- so a too-old message
    can name a real version rather than "did not run". `None` when none runs.

    "Actually runs" is the whole point on Windows: the Microsoft Store
    `python.exe` alias sits on PATH and satisfies any presence check, but
    executing it prints nothing and opens the Store. Requiring parseable output
    rejects it, and the `py -3` candidate ahead of it means a launcher-only
    machine still bootstraps.

    The version PREFERENCE is what keeps that ordering safe: `py -3` resolves to
    the launcher's default, routinely an older install than the bare `python` on
    PATH.
    """
    first_that_ran: HostPython | None = None
    for candidate in python_candidates(os.name == "nt"):
        out = probe(
            [*candidate, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            timeout=PROBE_TIMEOUT_S,
        )
        if out is None:
            continue
        version = _parse_two_dotted(out)
        if version is None:
            continue
        entry = HostPython(tuple(candidate), version)
        if version >= minimum:
            return entry
        if first_that_ran is None:
            first_that_ran = entry
    return first_that_ran


def _parse_two_dotted(raw: str) -> tuple[int, int] | None:
    """`"3.10\\n"` / `"noise\\n3.12"` -> `(3, 10)` / `(3, 12)`. The LAST
    non-empty line wins -- some interpreters print a banner first."""
    lines = [line.strip() for line in raw.strip().splitlines() if line.strip()]
    if not lines:
        return None
    major, sep, minor = lines[-1].partition(".")
    if not sep:
        return None
    try:
        return int(major.strip()), int(minor.strip())
    except ValueError:
        return None


def python_venv_capable(python: HostPython) -> bool:
    """Whether this interpreter's `venv` module can create a USABLE environment.

    `python -m venv --help` cannot tell -- argparse answers before `ensurepip`
    is touched -- so this probes the real dependency: `import ensurepip`, which
    fails fast on the Debian/Ubuntu split where `python3-venv` is a separate,
    unmet package (`python3 -m venv --help` exits 0 there while `python3 -c
    "import ensurepip"` exits 1).

    `True` when the probe cannot be run at all: this is not the check that
    should block a host on an inconclusive answer -- the real `python -m venv` a
    moment later surfaces its own error.
    """
    return probe([*python.argv, "-c", "import ensurepip"], timeout=PROBE_TIMEOUT_S) is not None


def _prereq_present(tool: str, is_windows: bool) -> bool:
    """PATH-only presence, with one deliberate widening on Windows: `python`
    counts as present when the `py` launcher is installed, because
    `python_candidates` leads with `py -3` and a launcher-only machine is a
    perfectly good Windows Python host. The oracle script checks the bare name
    only, so this can only make bootstrap SUCCEED where it would have
    refused."""
    if on_path(tool) is not None:
        return True
    return is_windows and tool == "python" and on_path("py") is not None


@dataclass(frozen=True)
class PythonFloor:
    """The floor actually enforced, and the two claimants behind it."""

    effective: tuple[int, int]
    source: str
    manifest: tuple[int, int]


def resolve_python_floor(facts: BootstrapFacts) -> PythonFloor:
    """The EFFECTIVE Python floor: the highest anything in the build chain
    enforces.

    `zephyr_python_floor` is imported from `tan.commands.doctor_cmd` and called
    with the SAME argument doctor passes it (`$ZEPHYR_BASE`, else tan's built-in
    `PYTHON_MINIMUM_REQUIRED` pin) -- not re-derived here. That is the whole
    mechanism keeping the two commands' verdicts identical: a second reader with
    its own rule is exactly the drift this port keeps hitting, and `doctor`
    reporting Pass on a host `bootstrap` refuses (or the reverse) is worse than
    either verdict alone.

    Skipped: reading the workspace's OWN `zephyr/cmake/modules/python.cmake`
    when one already exists. On the path that matters -- a fresh host, nothing
    bootstrapped -- there is no workspace to read, and a second candidate doctor
    does not consult is a way for the two to disagree. Add it when a
    bootstrapped Zephyr is found to LOWER the floor below tan's pin.
    """
    manifest_floor = facts.python_min_version
    zephyr_floor, zephyr_source = zephyr_python_floor(_env("ZEPHYR_BASE"))
    effective = max(manifest_floor, zephyr_floor)
    source = (
        zephyr_source
        if zephyr_floor >= manifest_floor
        else f"alp-sdk {BOOTSTRAP_MANIFEST_REL_PATH} pythonMinVersion"
    )
    return PythonFloor(effective, source, manifest_floor)


def check_prerequisites(
    facts: BootstrapFacts, host: str, floor: PythonFloor
) -> tuple[HostPython | None, PrereqFailure | None]:
    """`(interpreter, refusal)` -- exactly one of the two is set.

    The tool LISTS are keyed `posix`/`macos`/`windows` (`macos` optional, and
    absent means `posix`) while the install COMMANDS are keyed
    `linux`/`macos`/`windows`, so the host is resolved once here and the
    resolved map handed down: no branch can look a tool up in the wrong OS's
    table and hand a macOS user Linux's `apt-get` line.

    The version gate applies on BOTH platforms and against the EFFECTIVE floor
    -- see the module docstring. The oracle applies it on Windows only, against
    the manifest's, which is the live bug this port exists to fix.
    """
    is_windows = host == WINDOWS
    install = facts.install_for_host(host)
    missing = [
        tool for tool in facts.prerequisites(host) if not _prereq_present(tool, is_windows)
    ]
    if missing:
        refuse = windows_refusal if is_windows else posix_refusal
        return None, refuse(missing, install)

    # Probe against the floor that will actually be ENFORCED. Probing to a lower
    # bar would stop at the first candidate clearing 3.10 (`py -3`, often the
    # launcher's older default) and then fail the host for it, while a newer
    # `python` sat one candidate further down the list.
    python = probe_host_python(floor.effective)
    if python is None:
        return None, (
            windows_python_not_runnable(install) if is_windows else posix_python_not_runnable()
        )
    if python.version < floor.effective:
        return None, python_too_old(
            python.version,
            floor.effective,
            install,
            floor_source=floor.source,
            manifest_floor=floor.manifest,
        )
    # Linux-only: every install command this gate can name is apt's, and this is
    # a Debian/Ubuntu packaging split, not a general POSIX one -- macOS/BSD
    # pythons ship `ensurepip` in the base install.
    if host == LINUX and not python_venv_capable(python):
        return None, posix_venv_unusable()
    return python, None


# ---------------------------------------------------------------------------
# The spawning steps
# ---------------------------------------------------------------------------


@dataclass
class Runner:
    """Child-process launcher shared by every step."""

    json: bool
    #: Drop `$ZEPHYR_BASE` from every child. Set when workspace selection
    #: REJECTED the ambient value -- both scripts unset it so a stale tree
    #: cannot hijack `west init`.
    clear_zephyr_base: bool = False
    #: Set by `--dry-run`: log the argv and report success without spawning.
    #: The hermetic path -- the tests run every step through it.
    dry_run: bool = False
    #: Every argv a dry run would have spawned, in order. `data.plannedCommands`.
    planned: list[list[str]] = field(default_factory=list)

    def _env(self, extra_env: dict[str, str] | None = None) -> dict[str, str] | None:
        if not self.clear_zephyr_base and not extra_env:
            return None
        env = dict(os.environ)
        if self.clear_zephyr_base:
            env.pop("ZEPHYR_BASE", None)
        if extra_env:
            env.update(extra_env)
        return env

    def run(
        self,
        argv: list[str],
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> str | None:
        """Run to completion. `None` on success; otherwise a string carrying
        whatever detail is recoverable -- the captured tail in JSON mode, a
        launch error in either mode, and `""` in text mode where the child's own
        log already streamed to the terminal.

        `""` is a FAILURE, not a success: callers must test `is not None`.

        `extra_env` overlays on top of the inherited environment (or the
        `clear_zephyr_base`-filtered copy of it) -- see `force_git_long_paths`
        for the one caller that uses it.
        """
        self.planned.append(list(argv))
        if self.dry_run:
            return None
        try:
            if self.json:
                out = subprocess.run(
                    argv,
                    cwd=str(cwd) if cwd else None,
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    timeout=INSTALL_TIMEOUT_S,
                    env=self._env(extra_env),
                    check=False,
                )
                if out.returncode == 0:
                    return None
                return capture_tail(out.stdout, out.stderr)
            out = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                stdin=subprocess.DEVNULL,
                timeout=INSTALL_TIMEOUT_S,
                env=self._env(extra_env),
                check=False,
            )
            return None if out.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            return f"{argv[0]} did not finish within {INSTALL_TIMEOUT_S}s and was killed"
        except (OSError, ValueError) as err:
            # A missing binary, a path that is a DIRECTORY, an empty argv: a
            # launch failure names itself instead of escaping as a traceback.
            return f"failed to launch {argv[0]}: {err}"

    def capture(self, argv: list[str], cwd: Path | None = None) -> str:
        """Run capturing output in BOTH modes -- for the `west help` legibility
        probe, which READS the text rather than showing it. `""` for every way
        that can fail."""
        self.planned.append(list(argv))
        if self.dry_run:
            return ""
        try:
            out = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=PROBE_TIMEOUT_S,
                env=self._env(),
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return ""
        return out.stdout.decode("utf-8", "replace") + out.stderr.decode("utf-8", "replace")


@dataclass(frozen=True)
class VenvBin:
    """The venv executables, resolved against the bin directory that actually
    EXISTS on disk."""

    python: Path
    west: Path
    #: The bin sub-directory that won (`bin` or `Scripts`) -- the closing
    #: activation hint must name the real one.
    bin_dir: str


@dataclass(frozen=True)
class Workspace:
    """The resolved paths every step works against."""

    is_windows: bool
    facts: BootstrapFacts
    #: The alp-sdk checkout -- `west init -l`'s argument.
    repo_root: Path
    #: The west topdir: the checkout's PARENT (`west init -l` forces that).
    workspace_dir: Path
    venv_dir: Path

    def venv_bin(self) -> VenvBin:
        """Resolve the venv's executables by which bin directory EXISTS, POSIX
        name first -- `bootstrap.sh`'s `VBIN` assignment does exactly this.

        The bug this fixes: the presence check accepts EITHER layout (so a
        git-bash-created `Scripts/` venv is reused, not clobbered), but the
        executables used to be derived from the HOST. On a POSIX host that meant
        creation was skipped and then `bin/python` -- which does not exist -- was
        spawned, turning a reusable venv into a FATAL `pip install west (venv)
        failed`.
        """
        posix = self.facts.venv_posix_bin_dir
        windows = self.facts.venv_windows_bin_dir
        if _is_dir(self.venv_dir / posix):
            bin_dir = posix
        elif _is_dir(self.venv_dir / windows):
            bin_dir = windows
        else:
            bin_dir = self.facts.venv_bin_dir(self.is_windows)
        names = venv_exe_names(bin_dir, self.facts)
        return VenvBin(
            self.venv_dir / bin_dir / names.python,
            self.venv_dir / bin_dir / names.west,
            bin_dir,
        )

    def venv_present(self) -> bool:
        """Whether a venv interpreter already exists under EITHER layout."""
        for bin_dir in (self.facts.venv_posix_bin_dir, self.facts.venv_windows_bin_dir):
            names = venv_exe_names(bin_dir, self.facts)
            if _is_file(self.venv_dir / bin_dir / names.python):
                return True
        return False


#: `_probe_venv_pip` verdicts. Three, not two, because only ONE of them is
#: evidence that a directory is wreckage worth deleting (tan-cli#390).
PIP_USABLE = "usable"
#: pip RAN and reported itself missing or broken -- the corpse of a bootstrap
#: that died partway, which is what `ensure_venv`'s recreate exists for.
PIP_ABSENT = "absent"
#: The probe never got an answer at all: unspawnable interpreter, permission
#: error, or a `PROBE_TIMEOUT_S` timeout. NOT a verdict, and never grounds for
#: an `rmtree`.
PIP_INCONCLUSIVE = "inconclusive"


def _probe_venv_pip(venv: VenvBin, runner: Runner) -> str:
    """Three-state answer to "can this venv's own interpreter run `pip`?".

    `probe()` collapses "pip exited non-zero" and "the probe could not be run
    at all" into a single `None`, and its own docstring is explicit that `None`
    means "no answer", never "the answer is bad". `ensure_venv` nonetheless
    read that `None` as a verdict and `shutil.rmtree`'d the directory on it, so
    a venv that merely could not be spawned -- or one on a loaded machine that
    blew the 120 s timeout -- was deleted as if it had failed (tan-cli#390).

    Splitting the two keeps the recreate for the case it was written for while
    making "I don't know" incapable of authorising a delete. Deliberately does
    NOT go through `probe()`: the whole point is the returncode/exception
    distinction `probe()` erases.
    """
    if runner.dry_run:
        return PIP_USABLE
    try:
        out = subprocess.run(
            [str(venv.python), "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        # SubprocessError covers TimeoutExpired (the child is already killed by
        # `run`); ValueError catches an empty/garbage argv. None of these is a
        # statement about pip.
        return PIP_INCONCLUSIVE
    return PIP_USABLE if out.returncode == 0 else PIP_ABSENT


def _venv_python_version(
    venv: VenvBin, runner: Runner, fallback: tuple[int, int]
) -> tuple[int, int]:
    """The version `venv.python` actually resolves to, probed directly.

    NOT `host_python.version`: that only describes the interpreter that
    CREATED the venv, and pip installs run inside the venv's OWN interpreter
    -- which can be a different one on a REUSED venv (built on 3.14 last
    week; the host now resolves 3.12 first on PATH, or the reverse). Probing
    `host_python.version` for the ceiling check is a proxy that can both
    warn spuriously (a 3.12-built venv on a 3.14-default host) and miss the
    real case entirely (a 3.14-built venv on a host now defaulting to 3.12,
    tan-cli#285).

    `fallback` (the host's version) covers `--dry-run`, where nothing was
    actually written to disk to probe, and any other spawn failure: the real
    pip install a moment later surfaces its own error if the venv is
    genuinely broken, so an inconclusive probe here should not invent a
    ceiling warning that may not even apply.
    """
    if runner.dry_run:
        return fallback
    out = probe(
        [str(venv.python), "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
        timeout=PROBE_TIMEOUT_S,
    )
    if out is None:
        return fallback
    return _parse_two_dotted(out) or fallback


def ensure_venv(
    ws: Workspace, log: Log, runner: Runner, host: HostPython, *, adopted: bool = False
) -> tuple[VenvBin | None, str | None]:
    """Create (or reuse) the workspace venv and refresh `pip.bootstrapUpgrade`.

    Everything -- west, the Zephyr requirements, the SDK extras -- installs into
    this workspace-local venv, never the system interpreter / `--user` /
    `--break-system-packages`: a half-removed system `packaging` once broke
    `west init`, and a global west couples the build to the host interpreter's
    state.

    Idempotent, but only over a USABLE venv. Left alone, a partial venv makes
    every later step die exactly as it did the first time and the reporter's
    only way out is `rm -rf` by hand: a retry must either reuse a KNOWN-GOOD
    venv or start clean, never silently inherit the wreckage.

    `adopted` is that reasoning's one exception and the reason this argument
    exists (tan-cli#390). When `_select_workspace` ADOPTS a `$ZEPHYR_BASE`
    topdir it repoints `ws.venv_dir` at the USER's own `<topdir>/.venv` --
    under a comment promising never to modify their tree -- and the recreate
    below then deleted it, because `uv venv` and `python -m venv --without-pip`
    both produce a real interpreter with no pip. That directory is not
    bootstrap's to reclaim: on an adopted tree an unusable venv is a REFUSAL
    naming the path, never a delete. The caller must pass `plan.adopted`; the
    default is `False` so a workspace bootstrap built itself keeps the
    self-healing behaviour.
    """
    if not runner.dry_run:
        try:
            ws.workspace_dir.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            return None, f"could not create the workspace directory {_native(ws.workspace_dir)}: {err}"

    if ws.venv_present():
        verdict = _probe_venv_pip(ws.venv_bin(), runner)
        if verdict == PIP_USABLE:
            log.line(f"Workspace venv already present at {_native(ws.venv_dir)}")
        elif adopted:
            # tan-cli#390: this venv belongs to the user, not to bootstrap.
            # Refuse and say which directory, rather than reclaiming it.
            detail = (
                "its interpreter could not be run"
                if verdict == PIP_INCONCLUSIVE
                else "it has no usable pip"
            )
            message = (
                f"the venv at {_native(ws.venv_dir)} is not usable ({detail}), and this "
                f"workspace was ADOPTED from your existing tree -- bootstrap will not "
                f"delete a directory it did not create. Remove or repair "
                f"{_native(ws.venv_dir)} yourself, or point --workspace at a different "
                f"directory, then re-run."
            )
            log.warn("adopted-venv-unusable", message)
            return None, message
        elif verdict == PIP_INCONCLUSIVE:
            # No answer is not a verdict (tan-cli#390): reuse it and let the
            # real pip install a moment later report its own failure, rather
            # than deleting a directory on the strength of a probe that never
            # ran. Coded, so a `--format json` consumer sees why a later step
            # failed against a venv nothing vouched for.
            log.warn(
                "venv-probe-inconclusive",
                f"could not determine whether the venv at {_native(ws.venv_dir)} has a "
                f"usable pip (the probe did not run to completion) -- reusing it as-is; "
                f"if the installs below fail, remove that directory and re-run",
            )
        else:
            # PIP_ABSENT on a workspace bootstrap owns: the wreckage case the
            # recreate was written for. `log.warn`, not `log.line` -- a delete
            # that never reaches `issues[]` is invisible on the only surface
            # alp-sdk-vscode reads (tan-cli#390).
            log.warn(
                "venv-recreated",
                f"Workspace venv at {_native(ws.venv_dir)} has no usable pip (a previous "
                f"bootstrap likely failed partway) -- removing and recreating it",
            )
            if not runner.dry_run:
                try:
                    shutil.rmtree(ws.venv_dir)
                except OSError as err:
                    return None, (
                        f"failed to remove the broken venv at {_native(ws.venv_dir)}: {err}"
                    )
            failure = _create_venv(ws, log, runner, host)
            if failure is not None:
                return None, failure
    else:
        failure = _create_venv(ws, log, runner, host)
        if failure is not None:
            return None, failure

    venv = ws.venv_bin()
    upgrade = [
        str(venv.python),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "-q",
        *ws.facts.pip_bootstrap_upgrade,
    ]
    if runner.run(upgrade) is not None:
        log.warn("pip-upgrade", "pip/wheel upgrade reported a problem")
    return venv, None


def _create_venv(ws: Workspace, log: Log, runner: Runner, host: HostPython) -> str | None:
    """`python -m venv <venv_dir>`. Split out so the fresh path and the
    broken-venv recreate path run the EXACT same creation step -- a second,
    slightly different copy is how the two end up disagreeing about what
    "created" means."""
    log.line(f"Creating workspace venv at {_native(ws.venv_dir)}")
    detail = runner.run([*host.argv, "-m", "venv", str(ws.venv_dir)])
    if detail is None:
        return None
    return die(f"{host.display()} -m venv {_native(ws.venv_dir)} failed", detail)


def _west_argv(venv: VenvBin, args: list[str]) -> list[str]:
    """A `west` invocation by ABSOLUTE path to the venv's launcher, so the
    nested `west build`/`bitbake` spawns resolve the SAME west rather than
    whatever a stale PATH entry names. The caller sets `cwd` to the topdir."""
    return [str(venv.west), *args]


#: Force git's own `core.longpaths` to `true` for every subprocess `west
#: update` spawns, WITHOUT writing to any `.gitconfig` the workspace or the
#: user owns (tan-cli#306: `tan doctor`'s `longPaths` check can only WARN
#: about this -- it has no file to fix -- but `bootstrap` owns the workspace
#: it is about to `west update`, so it can make the one command that
#: actually breaks succeed outright).
#:
#: `west update` clones/checks out EACH project with its own `git`
#: subprocess, and none of those repos is `topdir` itself -- a west topdir is
#: not a git repo at all, `alp-sdk`/`zephyr`/every HAL module are. That rules
#: out both alternatives the issue named: `git -C <topdir> config` has no
#: repo to write into, and a `-c core.longpaths=true` on `west`'s OWN CLI
#: never reaches the nested `git` calls (west does not forward unrecognised
#: flags to git). `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`
#: is git's own documented ad hoc override -- it outranks every file-based
#: scope (system/global/local) without touching any of them, and it is
#: inherited by every child process `west update`'s own children spawn, so
#: it reaches every project git touches in one shot.
FORCE_GIT_LONG_PATHS_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.longpaths",
    "GIT_CONFIG_VALUE_0": "true",
}


def west_phase(
    ws: Workspace, venv: VenvBin, log: Log, runner: Runner, reuse: bool
) -> str | None:
    """Install west into the venv, then `west init -l` / `west update` /
    `west zephyr-export`, then the legibility guard. `reuse` short-circuits all
    of it (the guard included), exactly as both scripts do for a workspace
    adopted from `$ZEPHYR_BASE`. `None` on success, else the fatal message."""
    # west into the venv (NOT global / --user) so the system interpreter cannot
    # break it. `west.pipSpec` is a manifest FLOOR, not a hard pin.
    #
    # DOCUMENTED DIVERGENCE from the schema's own stance, which calls pipSpec
    # "informational today". tan deliberately DOES feed it: a declared floor
    # that nothing honours is not a floor, and it costs nothing today while
    # starting to matter the day the floor moves ahead of a stale venv.
    if not _is_file(venv.west):
        log.line("Installing west into the workspace venv")
        detail = runner.run(
            [str(venv.python), "-m", "pip", "install", "--upgrade", "-q", ws.facts.west_pip_spec]
        )
        if detail is not None:
            return die("pip install west (venv) failed", detail)

    if reuse:
        log.line(
            "Existing workspace reused -- skipping 'west init' / 'west update' (left untouched)"
        )
        return None

    workspace = _native(ws.workspace_dir)
    if not _is_dir(ws.workspace_dir / ".west"):
        log.line(
            f"Creating alp-sdk workspace at {workspace} (alp-sdk's west.yml is the "
            f"manifest; takes a few minutes)"
        )
        # `-l` makes alp-sdk the manifest repo and its parent the topdir. Zephyr
        # + HALs + extras are fetched by `west update`; alp-sdk's own
        # west-commands then expose the `alp-*` extension commands here.
        detail = runner.run(
            _west_argv(venv, [*ws.facts.west_init_args, str(ws.repo_root)]),
            cwd=ws.workspace_dir,
        )
        if detail is not None:
            return die("west init -l failed", detail)
        # Only bootstrap.sh mentions the cold-cache size on the fresh path.
        log.line(
            "Running 'west update' (shallow + narrow)"
            if ws.is_windows
            else "Running 'west update' (shallow + narrow; ~30 MB on a cold cache)"
        )
    else:
        log.line(f"alp-sdk workspace already initialised at {workspace}")
        log.line("Running 'west update' (shallow + narrow)")

    detail = runner.run(
        _west_argv(venv, list(ws.facts.west_update_args)),
        cwd=ws.workspace_dir,
        extra_env=FORCE_GIT_LONG_PATHS_ENV if ws.is_windows else None,
    )
    if detail is not None:
        return die("west update failed", detail)
    # Failure deliberately IGNORED (`|| true` in bootstrap.sh, no rc check in
    # bootstrap.ps1).
    runner.run(_west_argv(venv, list(ws.facts.west_export_args)), cwd=ws.workspace_dir)

    # Legibility guard: fail at bootstrap time -- not at first `tan build` -- if
    # the workspace manifest does not register the `alp-*` extension commands.
    # The searched-for command is a manifest fact; the scripts hardcode it a
    # second time in their own die message, which is interpolated here instead
    # so it cannot go stale.
    guard = ws.facts.west_extension_guard
    if not runner.dry_run:
        if guard not in runner.capture(_west_argv(venv, ["help"]), cwd=ws.workspace_dir):
            return (
                f"workspace at {workspace} does not register 'west {guard}' -- its "
                f"manifest is not alp-sdk's west.yml (#769). Check 'west -C {workspace} "
                f"config manifest.path'."
            )
    log.line(f"alp-* extension commands registered ('west {guard}' resolves in {workspace})")
    return None


def pip_phase(ws: Workspace, venv: VenvBin, log: Log, runner: Runner, host: str) -> None:
    """The Python dependency installs, all into the SAME venv and all NON-FATAL
    on their own (a recorded warn each) -- but three of the four are
    `WORKSPACE_BLOCKING` (tan-cli#220 / tan-cli#285): the run continues and
    the workspace stays on disk, yet it must not be reported `bootstrap:
    complete.` / exit 0 over one, unless `--allow-partial`. Matches both
    scripts only in that each phase itself stays non-fatal; the closing
    VERDICT no longer does -- see `WORKSPACE_BLOCKING`'s own docstring.

    `host` is the caller's already-detected `detect_host_os(sys.platform)`,
    threaded through rather than re-read here, so the OS this function gates
    its remediation hint on can never disagree with the one every other
    branch of `_run` already used.
    """
    requirements = ws.workspace_dir / ws.facts.zephyr_requirements_path
    # Conditional on the file EXISTING, matching the oracle -- which also means a
    # `--dry-run` over a workspace `west update` has not populated yet omits this
    # step from `plannedCommands`. Listing a command the real run would skip
    # would make the plan a lie, which is worse than an honest gap.
    if _is_file(requirements):
        log.line("Installing Zephyr Python requirements into the venv")
        argv = [str(venv.python), "-m", "pip", "install", "-q", "-r", str(requirements)]
        detail = runner.run(argv)
        if detail is not None:
            # Non-fatal, but "check manually" told the reader nothing. Measured
            # on a stock ubuntu-24.04 runner the failure is `hidapi` building
            # from source, needing pkg-config + the libusb-1.0 headers -- and
            # the workspace still LOOKS complete afterwards, so it surfaces far
            # from the cause. The remedy is OS-gated (tan-cli#285): the Linux
            # `apt-get` line was printed unconditionally before, including on
            # Windows, where it is both unactionable and wrong about the cause
            # (an MSVC linker failure, not a missing header). `detail` -- the
            # captured pip tail in JSON mode, `""` in text mode where the
            # child's own log already streamed -- is appended so the hint's
            # "look in the captured pip output" names something that is
            # actually THERE, not off-screen (tan-cli#285).
            tail = f" Captured output: {detail}" if detail else ""
            log.warn(
                "zephyr-requirements",
                "Zephyr requirements install reported a problem -- the venv is "
                f"incomplete and a later `tan init`/`tan build` may fail. "
                f"{zephyr_requirements_hint(host)}{tail}",
            )

    # SDK-side extras: alp_project.py needs jsonschema; the MCUboot dev-key
    # script needs imgtool. bootstrap.sh space-joins the list in its info line;
    # bootstrap.ps1 comma-joins it.
    extras = list(ws.facts.pip_sdk_extras)
    rendered = ", ".join(extras) if ws.is_windows else " ".join(extras)
    log.line(f"Installing alp-sdk Python extras into the venv ({rendered})")
    if runner.run([str(venv.python), "-m", "pip", "install", "-q", *extras]) is not None:
        log.warn("sdk-extras", "alp-sdk extras install reported a problem -- check manually")

    # tan's Python backend -- editable, so a `git pull` in the checkout updates
    # the backend in place.
    editable = Tokens(str(ws.repo_root), str(ws.workspace_dir)).apply(
        ws.facts.pip_editable_install
    )
    log.line(
        f"Installing the tan CLI's Python backend into the venv "
        f"(pip install -e {_native(editable)})"
    )
    argv = [str(venv.python), "-m", "pip", "install", "-q", "-e", editable]
    if runner.run(argv) is not None:
        log.warn(
            "editable-install", "alp_cli editable install reported a problem -- check manually"
        )


# ---------------------------------------------------------------------------
# `.west/config` reconciliation + the workspace sync record
# ---------------------------------------------------------------------------


def reconcile_west_manifest_path(sdk_root: str) -> tuple[str, str | None, str | None]:
    """Reconcile a stale `[manifest] path` in `<dirname(sdk_root)>/.west/config`
    to `sdk_root`. Returns `(outcome, old_rel, detail)`.

    `west init -l <sdk_root>` sets `topdir = dirname(sdk_root)` and writes
    `path = <basename>`; the "already initialised" branch runs `west update`
    WITHOUT re-running `west init -l`, so a config left behind by a DIFFERENT
    SDK checkout sharing the topdir keeps pointing at the stale SDK's
    `west.yml`.

    Non-fatal but not SILENT: `"failed"` separates "nothing to do" from "a
    rewrite was needed and did not happen", so the caller can say the workspace
    is still broken instead of reporting clean success.
    """
    sdk_path = Path(sdk_root)
    topdir = sdk_path.parent
    if str(topdir) == str(sdk_path):
        return "not-applicable", None, None
    config_path = topdir / ".west" / "config"
    # An ABSENT `.west/config` is the ordinary "no west workspace under this
    # topdir" case and the one silent outcome. Anything that EXISTS falls
    # through to the read below, which reports why it could not be used --
    # calling an unreadable config "nothing to do" is exactly the
    # silent-success bug this function exists to close.
    if not _safe_exists(config_path):
        return "not-applicable", None, None
    contents = _read_text(config_path)
    if contents is None:
        return "failed", None, f"{config_path} could not be read"
    current = get_manifest_path(contents)
    if current is None:
        # No `[manifest] path` line carries no pointer to reconcile; `west`
        # itself is what complains about that.
        return "not-applicable", None, None
    if _same_directory(topdir / current.strip(), sdk_path):
        return "already-matches", current, None
    new_rel = sdk_path.name
    if not new_rel:
        return "not-applicable", current, None
    rewritten = set_manifest_path(contents, new_rel)
    if rewritten is None:
        return "failed", current, "no [manifest] path line to rewrite"
    # Atomic replace: write a sibling temp in the same `.west/`, then rename
    # over `config`. That file is the topdir's ONLY manifest pointer, shared by
    # every SDK version under it -- a crash mid-write must not leave it
    # truncated, which would break `west` for all of them.
    tmp_path = config_path.with_name(f"config.{os.getpid()}.tan-tmp")
    try:
        tmp_path.write_text(rewritten, encoding="utf-8", newline="")
        os.replace(tmp_path, config_path)
    except OSError as err:
        # Worth naming on Windows: a `config` open in another process, or marked
        # read-only, fails the replace even though writing the temp succeeded.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return "failed", current, str(err)
    return "rewrote", current, new_rel


def _safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _hash_requirements_file(path: Path) -> str | None:
    """Lowercase-hex SHA-256 of `path`'s bytes, or `None` when it cannot be
    read -- the same "absence, not a claim" rule `record_workspace_sdk`
    already applies to the rest of this record: a hash tan could not compute
    must never be silently reported as a match OR a mismatch later."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def record_workspace_sdk(
    topdir: Path,
    sdk_root: str,
    venv_dir_name: str | None = None,
    venv_layout: str | None = None,
    requirements_path: Path | None = None,
) -> None:
    """Record that `topdir`'s workspace is now synced to `sdk_root`, after a
    `west update` that actually ran.

    Nothing else on disk answers "which SDK's manifest were these trees checked
    out from": `.west/config` names the manifest repo but is rewritten by the
    reconcile above (and by `west init`) WITHOUT the trees changing, so it
    cannot stand in for the update having happened. Best-effort -- a workspace
    that cannot record it simply keeps getting the `tan bootstrap` advice.

    `venv_dir_name`/`venv_layout`/`requirements_path` are the tan-cli#292
    venv-provenance stamp `doctor`'s `venvProvenance` check reads back:
    which venv this run populated, which bin-dir layout it created (#291),
    and a content hash of the Zephyr requirements file that populated it
    (`requirements_path`, hashed here -- the CALLER passes the path, not a
    precomputed digest, so a caller with none of this to report can simply
    omit the arguments rather than duplicating the hashing). All optional and
    independently best-effort: a caller mid-`--no-pip` or one that cannot
    determine the venv layout still gets the `sdkPath` half of the record
    written, which is strictly more than tan wrote before tan-cli#292.

    Broadly guarded on purpose: `workspace_sdk_record_json` renders a
    timestamp and can throw on an out-of-range `SOURCE_DATE_EPOCH` (the
    milliseconds case), and a best-effort record must never be the thing that
    kills the command.
    """
    try:
        digest = _hash_requirements_file(requirements_path) if requirements_path else None
        record = topdir / ".west" / "tan-workspace-sdk"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(
            workspace_sdk_record_json(sdk_root, venv_dir_name, venv_layout, digest),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 -- best-effort by contract; see the docstring
        pass


# ---------------------------------------------------------------------------
# The workspace-parent guard
# ---------------------------------------------------------------------------


def _list_entries(parent: Path) -> list[str] | None:
    """`parent`'s direct entry names, or `None` when it cannot even be read.

    `None`, not `[]`: an unreadable parent tells the guard nothing, and `[]`
    would read as "confirmed empty", which is a claim we cannot make. The real
    problem, if there is one, surfaces the first time a step tries to write
    there.
    """
    try:
        return [entry.name for entry in parent.iterdir()]
    except OSError:
        return None


def default_relocation_target(
    repo_root: Path, workspace_dir: Path, venv_dir_name: str
) -> Path | None:
    """`<workspace_dir>/alp-workspace` when the guard fires, else `None`.

    `workspace_dir == repo_root` (the rootless fallback) has no real parent to
    guard at all.
    """
    if str(workspace_dir) == str(repo_root):
        return None
    checkout_name = repo_root.name
    if not checkout_name:
        return None
    entries = _list_entries(workspace_dir)
    if entries is None:
        return None
    # A TYPED check, not a name match: `.west` is only "an existing west
    # workspace" when `west init -l` actually wrote a readable config there. A
    # plain file, or an empty directory, happening to be named `.west` is
    # foreign content like anything else.
    dot_west_is_workspace = _is_file(workspace_dir / ".west" / "config")
    if parent_needs_workspace_guard(
        entries, checkout_name, venv_dir_name, dot_west_is_workspace
    ):
        return workspace_dir / DEFAULT_WORKSPACE_DIR_NAME
    return None


def workspace_guard_target_occupied_refusal(workspace_dir: Path, target: Path) -> str:
    """tan-cli#302: the ONE case the workspace-parent guard still refuses.

    A dirty parent with no `--workspace` given now RELOCATES the checkout into
    `target` (`default_relocation_target`'s own `alp-workspace` choice)
    automatically rather than refusing and naming that same path back to the
    customer -- see the "tan-cli#302" note at the guard's call site in `_run`
    for why. That auto-relocation needs somewhere EMPTY to land, though:
    `target` already existing and already holding content of its own (a
    previous attempt's partial venv is the realistic case, per
    `rollback_relocation_after`'s own docstring) is the same "write into a
    directory without asking" hazard the guard exists to prevent in the first
    place, just one level deeper. This refusal is what still catches that one.

    Never says "re-run interactively", for the same reason tan-cli#284 settled
    on that wording originally: this port takes NO input on this decision on
    ANY run, TTY or not.
    """
    return (
        f"{_native(workspace_dir)} holds more than this checkout, and is not itself an "
        f"existing west workspace; tan would ordinarily move the checkout into "
        f"{_native(target)} automatically, but that directory already exists and already "
        f"holds content of its own, so tan is not going to write into it without asking. "
        f"Run `tan bootstrap --workspace <path>` with an empty destination, or clear out "
        f"{_native(target)} yourself and re-run."
    )


def enclosing_west_workspace_refusal(intended_topdir: Path, ancestor: Path) -> str:
    """`bootstrap.enclosing-west-workspace`: refused BEFORE the checkout is
    moved or the global default SDK is repointed (tan-cli#284).

    `west init -l` walks from the topdir upward looking for an existing
    `.west` and aborts the instant it finds one -- even when the topdir
    ITSELF is clean, which is exactly what let the workspace-parent guard
    (above) wave a run through only for `west init -l` to fail one step later
    with `"already initialized in {ancestor}, aborting."` By then the
    checkout had already been relocated and `~/.alp/sdk-default` already
    repointed at a workspace that was never going to exist -- neither
    mutation is rolled back by `west` failing on its own. An enclosing `.west`
    is a static filesystem fact, so it is knowable -- and checked here --
    before either mutation runs.

    Deliberately does NOT repeat west's own remedy ("remove this directory"):
    that `.west` may belong to a workspace the operator still depends on, and
    a command that has not looked inside it has no business suggesting its
    deletion.
    """
    return (
        f"{_native(ancestor)} already holds a west workspace (its own `.west` directory), "
        f"and west refuses to nest a second workspace inside one it finds above the topdir "
        f"-- initialising a workspace at {_native(intended_topdir)} would fail with "
        f'"already initialized in {_native(ancestor)}, aborting." That other workspace may '
        f"still be in use; do not remove it on west's say-so. Point `tan bootstrap "
        f"--workspace <path>` somewhere outside {_native(ancestor)} instead."
    )


def find_enclosing_west(start: Path) -> Path | None:
    """Walk from `start`'s PARENT upward -- never `start` itself, whose own
    `.west` is the ordinary "already initialised, reuse" case `west_phase`
    already handles by skipping straight to `west update` -- looking for a
    `.west` directory. `west init -l` performs exactly this walk from the
    topdir upward (`west.util.west_topdir`) and aborts the moment it finds
    one, so this predicts that outcome without spawning `west` at all.

    Directory PRESENCE only, matching west's own check (no `config`-file
    requirement the way `default_relocation_target`'s `dot_west_is_workspace`
    applies for its own, different purpose): west aborts on a bare `.west/`
    just the same, so a looser probe here would miss real refusals.

    `None` when nothing is found, or a directory along the way cannot even be
    probed: a permissions error tells this walk nothing, and the real problem
    -- if there is one -- surfaces naturally the first time `west` itself
    tries.
    """
    current = start.parent
    while True:
        try:
            found = (current / ".west").is_dir()
        except OSError:
            found = False
        if found:
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def relocate_checkout(
    repo_root: Path, target_parent: Path, dry_run: bool = False
) -> tuple[Path | None, str | None]:
    """Move `repo_root` to be a direct child of `target_parent`, preserving its
    basename. Returns `(new_root, error)`.

    This moves a customer's own git checkout, so it is built never to
    half-complete: a no-op when already a direct child (covers a retry after
    success and `--workspace <the-current-parent>`); an outright refusal when
    the destination exists, so nothing is ever merged into or overwritten; and
    exactly ONE `os.rename`, a single filesystem-level directory move that
    carries `.git`, uncommitted changes and untracked files in one operation. A
    cross-device target or a file locked open inside the tree fails the WHOLE
    rename, leaving the checkout exactly where it was -- there is no
    "moved half the files" state this can reach.

    `dry_run=True` (tan-cli#323) runs every check above -- the already-a-child
    no-op, the destination-exists refusal -- and returns the SAME `new_root` a
    real run would, but stops there: no `mkdir`, no `os.rename`. A preview
    flag that reports a planned destination must never be the thing that
    creates it.
    """
    if _same_directory(repo_root.parent, target_parent):
        return repo_root, None
    checkout_name = repo_root.name
    if not checkout_name:
        return None, f"{_native(repo_root)} has no final path component to relocate"
    destination = target_parent / checkout_name
    if _safe_exists(destination):
        return None, (
            f"{_native(destination)} already exists; refusing to relocate the checkout "
            f"there (nothing was moved)"
        )
    if dry_run:
        return destination, None
    try:
        target_parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        return None, f"could not create the workspace directory {_native(target_parent)}: {err}"
    try:
        os.rename(repo_root, destination)
    except OSError as err:
        return None, (
            f"could not move the checkout from {_native(repo_root)} to "
            f"{_native(destination)}: {err}{_rename_hint(err)} (the checkout was left in "
            f"place)"
        )
    return destination, None


def _rename_hint(err: OSError) -> str:
    """A one-line remedy for the two OS errors that have one; `""` for every
    other cause (permissions, a full disk), rather than guessing.

    Windows `ERROR_SHARING_VIOLATION` (32): something inside the checkout is
    open, often the invoking shell's own cwd. A cross-device move: Windows
    `ERROR_NOT_SAME_DEVICE` (17), POSIX `EXDEV` (18).
    """
    code = err.winerror if os.name == "nt" and getattr(err, "winerror", None) else err.errno
    if os.name == "nt":
        if code == 32:
            return " -- re-run from outside the checkout (e.g. `cd ..` first)"
        if code == 17:
            return " -- pick a --workspace on the same drive as the checkout"
    elif code == 18:
        return " -- pick a --workspace on the same filesystem as the checkout"
    return ""


# ---------------------------------------------------------------------------
# Reading the project's board.yaml + the SoM topology
# ---------------------------------------------------------------------------


def read_board_runtimes(board_yaml: str | None, sdk_root: str | None) -> list[str]:
    """The runtimes this project puts in play. `[]` for every way that can fail,
    which the Yocto gate treats as "unresolvable, proceed"."""
    cores, board_os, sku = _read_board_slice(board_yaml)
    topology = _read_som_topology(sku, sdk_root)
    return in_play_runtimes(cores, board_os, topology)


def _read_board_slice(
    board_yaml: str | None,
) -> tuple[dict[str, str | None] | None, str | None, str | None]:
    """`(cores, top-level os, som.sku)` out of `board.yaml`.

    PyYAML when importable, else a targeted scan -- the same bargain
    `presets_cmd._load_som_yaml` and `generate_cmd._board_sku` strike, and the
    frozen binary ships without PyYAML so the fallback is THE path there.

    Read STRICTLY (no `errors="replace"`), unlike doctor's `_read_text`. This
    file is a DECISION input, not a diagnostic: replacement characters turn a
    non-decodable board.yaml into a half-read one whose `cores:` block still
    parses, and a Yocto-looking core id then REFUSES the run on a non-Linux host
    over a file nothing could actually read. `yocto_gate`'s own rule applies --
    erring toward running is harmless, erring toward refusing bricks the command
    -- so an undecodable file is "unresolvable, proceed", which is also what the
    oracle's `read_to_string(..).ok()` produces.
    """
    if not board_yaml:
        return None, None, None
    try:
        text = Path(board_yaml).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None, None, None
    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError:
        return _scan_board_slice(text)
    try:
        doc = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 -- yaml.YAMLError and anything a loader raises
        return None, None, None
    if not isinstance(doc, dict):
        return None, None, None
    raw_cores = doc.get("cores")
    cores: dict[str, str | None] | None = None
    if isinstance(raw_cores, dict):
        cores = {}
        for core_id, entry in raw_cores.items():
            if not isinstance(core_id, str):
                continue
            value = entry.get("os") if isinstance(entry, dict) else None
            cores[core_id] = value if isinstance(value, str) else None
    som = doc.get("som")
    sku = som.get("sku") if isinstance(som, dict) else None
    top_os = doc.get("os")
    return (
        cores or None,
        top_os if isinstance(top_os, str) else None,
        sku if isinstance(sku, str) else None,
    )


def _scan_board_slice(
    text: str,
) -> tuple[dict[str, str | None] | None, str | None, str | None]:
    """The no-PyYAML reader: the top-level `os:`, `som: sku:`, and the `cores:`
    block's ids plus each one's `os:`. Deliberately not a YAML parser -- it
    answers only what the Yocto gate consumes."""
    cores: dict[str, str | None] = {}
    top_os: str | None = None
    sku: str | None = None
    section: str | None = None
    current_core: str | None = None
    core_indent = -1
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        key, sep, value = stripped.partition(":")
        key = key.strip()
        cleaned = value.strip().strip("'\"")
        if indent == 0:
            section = key
            current_core = None
            core_indent = -1
            if key == "os" and sep:
                top_os = cleaned or None
            continue
        if section == "som" and key == "sku" and sep:
            sku = cleaned or None
        elif section == "cores":
            if core_indent < 0 or indent <= core_indent:
                core_indent = indent
                current_core = key
                cores.setdefault(key, None)
                # A flow mapping on the same line: `m33_sm: {os: "off"}`.
                if "os" in cleaned:
                    inline = cleaned.strip("{}").split(",")
                    for item in inline:
                        ikey, isep, ival = item.partition(":")
                        if isep and ikey.strip() == "os":
                            cores[key] = ival.strip().strip("'\"") or None
            elif current_core is not None and key == "os" and sep:
                cores[current_core] = cleaned or None
    return cores or None, top_os, sku


def _read_som_topology(sku: str | None, sdk_root: str | None) -> dict[str, str]:
    """`{core id: runtime}` for `sku`, from the SDK metadata. Supports both
    layouts the SDK has used -- a flat `<sku>.yaml` or an `<sku>/som.yaml`
    directory. `{}` when anything is missing or unparseable, which the caller
    must treat as "unresolvable, proceed".

    Routed through `presets_cmd.parse_som_preset`, which owns the
    `board:`->zephyr / `machine:`->yocto / core-id-heuristic mapping. A second
    copy here is how `tan presets` and `tan bootstrap` would come to disagree
    about which host can build a project.
    """
    cleaned = (sku or "").strip()
    if not cleaned or not sdk_root:
        return {}
    directory = Path(sdk_root) / "metadata" / "e1m_modules"
    for candidate in (directory / f"{cleaned}.yaml", directory / cleaned / "som.yaml"):
        text = _read_text(candidate)
        if text is None:
            continue
        try:
            som = parse_som_preset(text)
        except Exception:  # noqa: BLE001 -- one bad preset must not fail the whole run
            continue
        return {core.id: core.os for core in som.cores}
    return {}


# ---------------------------------------------------------------------------
# Envelope assembly
# ---------------------------------------------------------------------------


@dataclass
class RunPaths:
    """Mutable run state the envelope reports, threaded through the phases."""

    repo_root: Path
    workspace_dir: Path
    venv_dir: Path

    def tokens(self) -> Tokens:
        """`${SDK_ROOT}` / `${WORKSPACE_DIR}` for the CURRENT paths, re-derived
        at each use so a repointed workspace is reflected."""
        return Tokens(str(self.repo_root), str(self.workspace_dir))


def _data(
    *,
    args: dict[str, bool],
    sdk_root: str,
    paths: RunPaths | None,
    facts: BootstrapFacts,
    pin: str,
    missing: list[dict[str, str | None]] | None = None,
    planned: list[list[str]] | None = None,
) -> dict[str, object]:
    """The `data` payload.

    `zephyrBase` is RENDERED FROM THE MANIFEST (`env.ZEPHYR_BASE`), never
    re-derived as `<workspaceDir>/zephyr`: if alp-sdk repoints that key the
    printed export line follows it, and a second derivation here would hand a
    consumer a path nothing else in the run agrees with. Absent key -> `""`,
    like every other unresolved path field.

    `missingPrerequisites` is an explicit `null` on every run with no missing
    tool to name -- NEVER `[]`, which would be a second spelling of the fact a
    successful run already reports as `null`.
    """
    tokens = paths.tokens() if paths else Tokens("", "")
    zephyr_base = ""
    if paths is not None:
        for key, raw in facts.env:
            if key == "ZEPHYR_BASE":
                zephyr_base = _native(tokens.apply(raw))
                break
    data: dict[str, object] = {
        "schemaVersion": DATA_SCHEMA_VERSION,
        # `_native` like the other three: a consumer comparing `sdkRoot` against
        # `workspaceDir` (prefix / dirname) needs one separator.
        "sdkRoot": _native(sdk_root) if sdk_root else "",
        "workspaceDir": _native(paths.workspace_dir) if paths else "",
        "venvDir": _native(paths.venv_dir) if paths else "",
        "zephyrBase": zephyr_base,
        "factsFromManifest": facts.from_manifest,
        "zephyrPin": pin,
        "noPip": args["no_pip"],
        "noWest": args["no_west"],
        "printEnv": args["print_env"],
        "missingPrerequisites": missing,
    }
    if planned is not None:
        # `--dry-run` only: the argv every step WOULD have spawned, in order. A
        # key that appears only under the flag that produces it, so a normal run
        # keeps the oracle's exact key set.
        data["plannedCommands"] = [" ".join(argv) for argv in planned]
    return data


@dataclass
class Outcome:
    """What the command decided: the exit code, the payload, the issues, and the
    stderr lines text mode prints."""

    exit_code: ExitCode
    data: dict[str, object] | None
    issues: list[Issue]
    text: list[str] = field(default_factory=list)


def _refusal(
    exit_code: ExitCode, code: str, lines: list[str], data: dict[str, object]
) -> Outcome:
    """A refusal before any step ran: ONE `bootstrap.<code>` issue whose message
    is `" ".join(lines)`, and those same lines as the text output (which is what
    `doctor --build --fix` and `build`'s auto-bootstrap surface).

    The join is why `data.missingPrerequisites` has to exist: an install command
    contains the same spaces the join used, so the split is not recoverable.
    """
    return Outcome(
        exit_code,
        data,
        [Issue(f"bootstrap.{code}", "error", " ".join(lines))],
        list(lines),
    )


def _fatal(message: str, data: dict[str, object], issues: list[Issue]) -> Outcome:
    """A failing STEP: the fatal message as a `bootstrap.failed` error issue, on
    top of whatever warnings the run had already recorded."""
    return Outcome(
        ExitCode.RUNTIME_FAILURE,
        data,
        [*issues, Issue("bootstrap.failed", "error", message)],
        [message],
    )


def _env(name: str) -> str | None:
    """An env var, or `None` when unset OR blank. Never raising: a decoding
    failure on an exotic value must not kill the command."""
    try:
        raw = os.environ.get(name)
    except Exception:  # noqa: BLE001 -- an environ that cannot be read is "unset"
        return None
    if raw is None or not raw.strip():
        return None
    return raw


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def load_facts(sdk_root: str) -> BootstrapFacts:
    """`<sdkRoot>/metadata/bootstrap.json`, or the fallback constants when the
    SDK predates it. Raises `BootstrapManifestError`.

    Version-skew guard: an ABSENT manifest is the legacy path and falls back; a
    manifest present but unusable -- unreadable, non-UTF-8, unparseable, or
    carrying an unsupported `schemaVersion` -- is a HARD error naming why.
    Falling back there would silently re-introduce hand-ported behaviour against
    an SDK that explicitly declared something else.

    ABSENT is the ONLY case that falls back. A `chmod 000` manifest on a dev
    tree used to produce an envelope identical in every verdict-bearing field
    (`ok:true`, `exitCode:0`, `factsFromManifest:false`, `issues:[]`) to a
    genuine legacy SDK's.
    """
    path = Path(sdk_root) / BOOTSTRAP_MANIFEST_REL_PATH
    if not _safe_exists(path):
        return fallback_facts(_manifest_absent_floor())
    try:
        # UTF-8 with NO replacement: non-UTF-8 bytes are a refusal, never
        # mojibake silently parsed as facts. `doctor`'s `_read_text` uses
        # `errors="replace"` deliberately -- it must degrade rather than refuse
        # -- and this must not.
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as err:
        # Same shape as the parse-failure message, so a consumer sees ONE
        # wording for "this manifest is here and I cannot use it", whichever way
        # it is unusable -- and the OS's own reason travels with it, because
        # "Access is denied" and "invalid start byte" are different fixes.
        raise BootstrapManifestError(
            f"{BOOTSTRAP_MANIFEST_REL_PATH} could not be read: {err}"
        ) from err
    return parse_bootstrap_manifest(text)


def _manifest_absent_floor() -> tuple[int, int]:
    """The floor `fallback_facts` records for an SDK with no manifest -- doctor's
    own `FALLBACK_PYTHON_FLOOR`, imported rather than re-spelled (see its doc
    comment: it mirrors the Rust oracle's frozen `crate::util::MIN_PYTHON`, not
    the manifest, and the two can drift). The EFFECTIVE floor is still resolved
    from this by `resolve_python_floor`, so a legacy SDK with no manifest at all
    gets the same Zephyr-aware verdict a current one does."""
    return FALLBACK_PYTHON_FLOOR


def _zephyr_base_will_adopt(pin: str, repo_root: Path) -> bool:
    """Whether `$ZEPHYR_BASE`, if set, will be ADOPTED by `_select_workspace`
    rather than ignored.

    A read-only restatement of that same detection, used ONLY to decide whether
    the workspace-parent guard applies at all: when adoption is about to repoint
    the write target at the `$ZEPHYR_BASE` topdir, a dirty `<sdkRoot>/..` is not
    a problem, because nothing is going to be written there. Both call
    `decide_workspace_reuse`, so this is a few extra filesystem reads, never a
    second source of truth for the DECISION.
    """
    facts = _existing_workspace_facts(repo_root)
    if facts is None:
        return False
    choice, _ = decide_workspace_reuse(*facts, pin)
    return choice in (REUSE, STALE)


def _existing_workspace_facts(repo_root: Path) -> tuple[str, bool, bool] | None:
    """`(VERSION body, topdir is a west workspace, manifest is this SDK)` for the
    `$ZEPHYR_BASE` tree, or `None` when there is nothing to judge.

    Reads the ENVIRONMENT VARIABLE only -- never a shell rc file -- so this
    behaves identically under bash / zsh / fish / PowerShell / WSL.
    """
    zephyr_base = _env("ZEPHYR_BASE")
    if zephyr_base is None:
        return None
    base = Path(zephyr_base)
    version_file = _read_text(base / "VERSION")
    if version_file is None:
        return None
    top = base.parent
    return (
        version_file,
        _is_dir(top / ".west"),
        _manifest_points_at(top, repo_root),
    )


def _manifest_points_at(topdir: Path, repo_root: Path) -> bool:
    """Whether `<topdir>/.west/config`'s `[manifest] path` resolves to
    `repo_root`. west and the venv are not set up yet at this point, so the
    config is read directly rather than shelling `west config manifest.path`."""
    config = _read_text(topdir / ".west" / "config")
    if config is None:
        return False
    rel = get_manifest_path(config)
    if rel is None:
        return False
    return _same_directory(topdir / rel.strip(), repo_root)


@dataclass(frozen=True)
class WorkspacePlan:
    """What workspace selection decided about `$ZEPHYR_BASE`."""

    #: Skip `west init`/`west update` entirely -- the adopted tree is already on
    #: the pinned Zephyr, so bootstrap leaves it untouched.
    reuse: bool = False
    #: A `$ZEPHYR_BASE` topdir was taken over (reused untouched OR refreshed in
    #: place), so the paths now name it rather than `<sdkRoot>/..`.
    adopted: bool = False
    #: Drop `$ZEPHYR_BASE` from every child -- set only when the ambient value
    #: was REFUSED, so a foreign tree cannot hijack `west init`.
    clear_zephyr_base: bool = False


def _select_workspace(
    log: Log, is_windows: bool, pin: str, facts: BootstrapFacts, paths: RunPaths
) -> WorkspacePlan:
    """Workspace selection over the `$ZEPHYR_BASE` tree; repoints
    `paths.workspace_dir`/`venv_dir` at it when adopted.

    Three outcomes for a tree whose manifest IS this checkout: on the pinned
    Zephyr it is reused untouched; on a DIFFERENT one it is adopted and
    refreshed by the ordinary `west update` rather than reused-and-skipped or
    abandoned for a second clone elsewhere; and a foreign-manifest tree is
    refused. Every non-reuse outcome is RECORDED as a warning, not just printed:
    a JSON consumer otherwise could not tell that its `$ZEPHYR_BASE` was
    refreshed, or refused and a second workspace built somewhere else entirely.
    """
    existing = _existing_workspace_facts(paths.repo_root)
    if existing is None:
        return WorkspacePlan()
    _, top_is_west_workspace, manifest_is_sdk = existing
    zephyr_base = _env("ZEPHYR_BASE") or ""
    top = Path(zephyr_base).parent
    var = "$env:ZEPHYR_BASE" if is_windows else "$ZEPHYR_BASE"
    choice, version = decide_workspace_reuse(*existing, pin)

    if choice == REUSE:
        # Never modify the user's tree: adopt it and skip init/update.
        paths.workspace_dir = top
        paths.venv_dir = top / facts.venv_dir_name
        log.line(
            f"Reusing compatible alp-sdk workspace from {var}: "
            f"{_native(paths.workspace_dir)} (Zephyr {version})"
        )
        return WorkspacePlan(reuse=True, adopted=True)

    if choice == STALE:
        # This IS bootstrap's own workspace, just left behind by an SDK pin bump.
        # `west update` over a diagnostic: a warning alone leaves the next build
        # green against the wrong Zephyr, which IS the defect. It is not the
        # aggressive option either -- it is byte-for-byte the command a bootstrap
        # with no $ZEPHYR_BASE set would run over this same topdir, gated on a
        # manifest that already proved the tree belongs to this SDK.
        paths.workspace_dir = top
        paths.venv_dir = top / facts.venv_dir_name
        log.warn(
            "zephyr-base-stale",
            f"{var} workspace ({_native(paths.workspace_dir)}) is on Zephyr {version} "
            f"but this alp-sdk pins {pin} -- refreshing it with 'west update' (this also "
            f"moves the other west.yml projects to their pins)",
        )
        return WorkspacePlan(adopted=True)

    if choice == MANIFEST_MISMATCH:
        log.warn(
            "zephyr-base-manifest-mismatch",
            f"{var} workspace ({_native(top)}) is a Zephyr {pin} tree but its manifest "
            f"is not alp-sdk's west.yml -- not reusing it (would leave 'west "
            f"{facts.west_extension_guard}' unknown, #769); building an alp-sdk "
            f"workspace at {_native(paths.workspace_dir)}",
        )
        return WorkspacePlan(clear_zephyr_base=True)

    # INCOMPATIBLE: the tree missed on at least one axis -- an unreadable
    # Zephyr VERSION, no .west/ topdir, or (tan-cli#334) a version skew AND a
    # foreign manifest at once, which fails BOTH the STALE and
    # MANIFEST_MISMATCH branches above and used to fall through here with
    # neither explanation. Accumulate whichever facts were actually observed
    # instead of a single "not a west workspace at all" claim, which is only
    # true when NONE of them were found. bootstrap.sh's message carries a tail
    # bootstrap.ps1's does not.
    tail = "" if is_windows else " and building an isolated one"
    found: list[str] = []
    if version:
        found.append(f"Zephyr {version} (this alp-sdk pins {pin})")
    if top_is_west_workspace and not manifest_is_sdk:
        found.append("a manifest that is not alp-sdk's west.yml")

    if found:
        detail = " and ".join(found)
        log.warn(
            "zephyr-base-incompatible",
            f"{var} ({zephyr_base}) has {detail} -- not an alp-sdk Zephyr {pin} "
            f"west workspace; ignoring it{tail}",
        )
    else:
        log.warn(
            "zephyr-base-incompatible",
            f"{var} ({zephyr_base}) is not an alp-sdk Zephyr {pin} west workspace -- "
            f"ignoring it{tail}",
        )
    return WorkspacePlan(clear_zephyr_base=True)


@dataclass(frozen=True)
class RelocationUndo:
    """Everything `rollback_relocation_after` needs to put a checkout
    relocation back exactly as it was (tan-cli#284), snapshotted immediately
    BEFORE this run's relocation mutated any of it -- never re-derived later,
    because e.g. re-deriving `workspace_dir` as `repo_root.parent` is wrong
    for a `--workspace` run over an ADOPTED `$ZEPHYR_BASE` topdir."""

    #: The checkout's location before this run moved it.
    old_root: str
    #: `~/.alp/sdk-default`'s bytes before this run overwrote it; `None` when
    #: it did not exist yet (the common first-bootstrap-ever case).
    previous_pointer: bytes | None
    #: The envelope `project` this run would have reported had it never
    #: relocated anything.
    project: Project
    workspace_dir: Path
    venv_dir: Path


def _run(  # noqa: PLR0911, PLR0912, PLR0915 -- one linear refusal ladder; see below
    *,
    project: str | None,
    board_yaml: str | None,
    sdk_root_flag: str | None,
    no_pip: bool,
    no_west: bool,
    print_env: bool,
    allow_partial: bool,
    workspace: str | None,
    dry_run: bool,
    json_mode: bool,
) -> tuple[Outcome, Project, SdkInfo | None]:
    """The whole command, as a sequence of early refusals then the three phases.

    Deliberately one long function rather than a pipeline of small ones: the
    order of the gates is the contract (a refusal must leave NOTHING on disk, so
    every validation precedes every write), and splitting it would hide that
    order behind call sites. Mirrors the oracle's `bootstrap::run` one-to-one.
    """
    is_windows = os.name == "nt"
    host = detect_host_os(sys.platform)
    log = Log(json_mode)
    flags = {"no_pip": no_pip, "no_west": no_west, "print_env": print_env}

    root, board_path = resolve_project_paths(project, board_yaml)
    # tan-cli#236: `board_yaml` is reported only when a file is really there --
    # `board_path` names where one WOULD live regardless.
    reported_project = Project.resolved(root, board_path)

    # tan-cli#322: routed through `resolve_sdk_root_ladder` -- the SAME
    # resolver `doctor` and eleven other commands call -- rather than
    # `resolve_sdk_tiered` directly. `resolve_sdk_tiered` alone only checks
    # the workspace root itself, a LATERAL `../alp-sdk` sibling, or an
    # enclosing checkout; it has no candidate for the documented quickstart
    # layout (`tan.exe` beside a freshly cloned `alp-sdk/`, a CHILD of cwd).
    # `resolve_sdk_root_ladder` falls through to the wider positional walk
    # (`discover_sdk_root`, which does check that child) exactly when the
    # narrow tiers come up empty -- the same fallback `doctor` already
    # benefits from, which is why `doctor` resolved a checkout here and a
    # bootstrap calling the narrower resolver alone did not. A second,
    # bootstrap-only copy of that fallback would be the very drift this
    # fixes: one resolver, consulted by both.
    active_path, active_tier, broken_project_pin = resolve_sdk_root_ladder(
        sdk_root_flag, Path(root)
    )
    active_is_sdk = active_path is not None and active_path.joinpath(*SDK_MARKER).exists()
    resolved = str(active_path) if active_is_sdk else None
    if resolved is None:
        return (
            _refusal(
                ExitCode.VALIDATION_FAILURE,
                "sdk-root-unresolved",
                [
                    # `tan sdk switch`/`tan sdk install` both refuse in this
                    # build (tan-cli#305, `sdk_cmd._run_not_ported`) -- naming
                    # either here left a clean host with no way forward at
                    # all. `NO_SDK_NEXT_STEPS` is the one mechanism that
                    # actually resolves an SDK, shared with `doctor_cmd`.
                    f"alp-sdk root is unresolved -- {NO_SDK_NEXT_STEPS}."
                ],
                _data(
                    args=flags,
                    sdk_root="",
                    paths=None,
                    facts=fallback_facts(_manifest_absent_floor()),
                    pin="",
                ),
            ),
            # The only refusal that predates project resolution in the oracle.
            Project(root=None, board_yaml=None),
            None,
        )
    # tan-cli#217/#296: `resolve_sdk_root_ladder` returns an explicit
    # `--sdk-root` VERBATIM (I-31, so a typo surfaces rather than silently
    # falling through to a lower tier) -- `./alp-sdk` stays `./alp-sdk`. That
    # is fine for this run's OWN filesystem calls (relative to this process's
    # real cwd), but
    # every path below is either compared by PREFIX (`sdkRoot` vs
    # `workspaceDir`) or handed to a consumer with a different cwd entirely --
    # the vscode extension included. Anchored here, once, before `sdk_root`
    # starts feeding `paths`/`SdkInfo`/the envelope, exactly as `init_cmd.
    # _resolve_sdk_root` anchors an explicit `--sdk-root` before persisting it
    # (tan-cli#263): `expanduser` first (`abspath` alone does not expand `~`),
    # then `abspath`, which is lexical only (not `Path.resolve()`), so a
    # checkout reached through a symlink keeps the name it was given and a
    # not-yet-existing path still resolves. The other three tiers
    # (`projectPin`/`globalDefault`/`discovery`) are already absolute, so this
    # is a no-op for them.
    sdk_root = os.path.abspath(os.path.expanduser(resolved))
    sdk = SdkInfo(sdk_root, active_tier)
    # tan-cli#263 review: `bootstrap` sets up a whole venv/west workspace
    # against whichever checkout this resolved -- a silently-missed
    # `.alp/sdk-path` pin belongs on the same two SUCCESS paths below
    # (`--print-env`, and the full run) as every other non-fatal notice this
    # command reports; not `log.warn`, which always prefixes `bootstrap.` and
    # would misname this shared code.
    pin_issue = project_pin_issue(broken_project_pin, active_tier)

    # `west init -l <alp-sdk>` always makes the topdir the checkout's PARENT and
    # alp-sdk itself the manifest repo, which is what registers the `alp-*`
    # extension commands. Zephyr + HALs land as its siblings.
    repo_root = Path(sdk_root)
    workspace_dir = repo_root.parent if str(repo_root.parent) != str(repo_root) else repo_root
    paths = RunPaths(repo_root, workspace_dir, workspace_dir / ".venv")

    try:
        facts = load_facts(sdk_root)
    except BootstrapManifestError as err:
        return (
            _refusal(
                ExitCode.VALIDATION_FAILURE,
                "manifest",
                [str(err)],
                _data(
                    args=flags,
                    sdk_root=sdk_root,
                    paths=paths,
                    facts=fallback_facts(_manifest_absent_floor()),
                    pin="",
                ),
            ),
            reported_project,
            sdk,
        )
    # `venv.dirName` is a manifest fact, so the venv path is only final now.
    paths.venv_dir = paths.workspace_dir / facts.venv_dir_name
    # ONE pin authority, shared with `build`'s preflight zephyrVersion check.
    pin = resolve_zephyr_pin(_read_text(paths.repo_root / "west.yml"), facts.zephyr_version)

    def payload(**extra: object) -> dict[str, object]:
        return _data(args=flags, sdk_root=sdk_root, paths=paths, facts=facts, pin=pin, **extra)

    # Set below, only if THIS run relocates the checkout. A step after the
    # relocation (`ensure_venv` / `west_phase`) that later turns out to be
    # the fallible one rolls this back rather than leaving the checkout moved
    # and the global default repointed for a bootstrap that never finished
    # (tan-cli#284).
    relocation_undo: RelocationUndo | None = None

    # `--workspace` is validated + absolutised HERE, before anything else
    # touches it: this relocates a customer's checkout, so an empty value or an
    # ambiguous drive-relative one must never resolve to a guess.
    workspace_override: Path | None = None
    if workspace is not None:
        if print_env:
            # Refused outright rather than rendering env lines for a directory
            # nothing was ever moved into.
            return (
                _refusal(
                    ExitCode.VALIDATION_FAILURE,
                    "print-env-workspace-conflict",
                    [
                        "--print-env and --workspace cannot be combined: --print-env only "
                        "prints what an already-resolved workspace exports and moves "
                        "nothing, while --workspace's whole job is choosing where the "
                        "workspace goes. Run `tan bootstrap --workspace <path>` first, "
                        "then `tan bootstrap --print-env` against the workspace that "
                        "produced."
                    ],
                    payload(),
                ),
                reported_project,
                sdk,
            )
        try:
            workspace_override = Path(resolve_workspace_target(workspace, os.getcwd()))
        except (ValueError, OSError) as err:
            return (
                _refusal(
                    ExitCode.VALIDATION_FAILURE, "workspace-invalid", [str(err)], payload()
                ),
                reported_project,
                sdk,
            )

    # `--print-env` short-circuits before any prerequisite check or venv work, so
    # it answers on a machine that is still missing cmake or ninja.
    if print_env:
        text = print_env_block(
            facts, paths.tokens(), facts.venv_bin_dir(is_windows), is_windows
        )
        print_env_issues = [pin_issue] if pin_issue is not None else []
        return (
            Outcome(ExitCode.SUCCESS, payload(), print_env_issues, text),
            reported_project,
            sdk,
        )

    # Workspace-parent guard, BEFORE any write below, so a refusal -- the one
    # case that still is one, see below -- leaves nothing on disk. A
    # `$ZEPHYR_BASE` workspace about to be ADOPTED repoints the write target
    # away from `repo_root`'s parent entirely -- a dirty parent is not a
    # problem when nothing is going to be written there. An explicit
    # `--workspace` answers the question outright regardless.
    #
    # tan-cli#302: a dirty parent with no `--workspace` given now RELOCATES
    # the checkout into `default_relocation_target`'s own `alp-workspace`
    # choice instead of refusing. The refusal this replaced named that exact
    # path back to the customer ("run `tan bootstrap --workspace <path>` --
    # for example <target>"), and the condition that trips it is tan's own
    # binary sitting beside a freshly cloned alp-sdk: the documented
    # quickstart (download `tan.exe`, clone `alp-sdk` beside it, run `tan
    # bootstrap`) followed LITERALLY, making the first command in the product
    # fail a customer for doing exactly what they were told. Typing back a
    # path tan already computed is strictly more work than tan just doing it.
    # (An alternative shape -- excluding tan's own binary from the "holds more
    # than this checkout" test -- was considered and rejected: it only helps
    # the customer whose directory is otherwise empty, and a stray `README` or
    # `.gitignore` beside the clone puts them right back in the refusal.)
    # `target` is used exactly as an explicit `--workspace <target>` would be
    # from here on: the enclosing-`.west` check just below, the relocation
    # itself further down, and its rollback on a later failure
    # (`rollback_relocation_after`) are all the SAME code, shared with that
    # path unchanged -- so tan-cli#284's invariant (refuse or relocate BEFORE
    # writing anything; never leave a half-moved checkout) covers this
    # auto-relocation exactly as it already covers an explicit one.
    #
    # NO PROMPT, ever, in this port: a prompt needs a human at a console, and
    # every caller that reaches here without `--workspace` gets the
    # non-interactive outcome -- a relocation now, rather than a refusal, but
    # still no question asked. `--format json` says so explicitly; a piped or
    # redirected stdin says so just as loudly, and the oracle hung forever on
    # exactly that before the terminal term was added.
    # Evaluated UNCONDITIONALLY, not short-circuited past (tan-cli#389). Python
    # skips the right operand of `or` once the left is true, so with
    # `--workspace` set the adoption probe never ran at all -- and the two
    # checks it feeds (`default_relocation_target`'s `.west/config` test and
    # `parent_needs_workspace_guard`) are the only places that would notice the
    # checkout is the manifest repo of a LIVE west workspace. The value is
    # still only consulted for the no-override case; naming it first is what
    # keeps the probe's side of the decision observable.
    zephyr_base_adopts = _zephyr_base_will_adopt(pin, paths.repo_root)
    guard_applies = workspace_override is not None or not zephyr_base_adopts
    target: Path | None = None
    if guard_applies:
        target = workspace_override
        if target is None:
            target = default_relocation_target(
                paths.repo_root, paths.workspace_dir, facts.venv_dir_name
            )
            if target is not None and _list_entries(target):
                # The one case that still refuses (tan-cli#302): `target` --
                # the directory the auto-relocation below would move the
                # checkout INTO -- already exists and already holds content of
                # its own. Auto-relocating there anyway would be the exact
                # "wrote into a directory without asking" hazard this guard
                # exists to prevent, one level down. An ABSENT or genuinely
                # EMPTY `target` (the ordinary case) falls straight through to
                # the relocation instead.
                return (
                    _refusal(
                        ExitCode.VALIDATION_FAILURE,
                        "workspace-guard",
                        [workspace_guard_target_occupied_refusal(paths.workspace_dir, target)],
                        payload(),
                    ),
                    reported_project,
                    sdk,
                )
        # tan-cli#284: whichever directory is about to become the west topdir
        # -- `target` whenever a relocation is happening (an explicit
        # `--workspace`, or the tan-cli#302 auto-relocation just above), else
        # the checkout's own already-clean parent -- probe its ancestors for
        # an ENCLOSING `.west` before the first mutation below. `west init -l`
        # would hit exactly this and abort; caught here, refusing leaves
        # nothing on disk, whereas discovering it after `relocate_checkout` /
        # `_write_global_sdk_pointer` below leaves the checkout moved and the
        # global default SDK repointed at a workspace that will never exist,
        # neither of which `west` failing rolls back.
        #
        # Gated on `west init -l` actually running THIS run: `--no-west`
        # skips it entirely, and a topdir that already holds its OWN `.west`
        # takes `west_phase`'s "already initialised" branch, which runs only
        # `west update` -- never `west init -l`, so its ancestor-upward walk
        # (the only thing that could ever hit an enclosing `.west`) never
        # happens either way. Without this gate the check over-refused both:
        # a `--no-west` run with an unrelated ancestor workspace, and a
        # perfectly reusable topdir that merely happened to sit under one.
        intended_topdir = target if target is not None else paths.workspace_dir
        if not no_west and not _is_dir(intended_topdir / ".west"):
            enclosing = find_enclosing_west(intended_topdir)
            if enclosing is not None:
                return (
                    _refusal(
                        ExitCode.VALIDATION_FAILURE,
                        "enclosing-west-workspace",
                        [enclosing_west_workspace_refusal(intended_topdir, enclosing)],
                        payload(),
                    ),
                    reported_project,
                    sdk,
                )

        # tan-cli#389. The guard above probes ancestors of the DESTINATION; this
        # one asks about the workspace the checkout would be taken OUT of. When
        # `paths.repo_root` is the manifest repo of a live west workspace, the
        # `os.rename` below renames it out from under a `<parent>/.west/config`
        # that still records `path = <name>`: every later `west build` /
        # `west update` / `west alp-*` in that topdir then fails, tan reports
        # the workspace as having a foreign manifest -- true only because tan
        # moved it -- and `~/.alp/sdk-default` is repointed at the new location
        # for good measure. `rollback_relocation_after` is no help: it fires
        # only when a LATER step fails, and here nothing fails.
        #
        # Checked before the host and prerequisite gates below for the same
        # reason they sit before the relocation: a refusal knowable from
        # filesystem facts alone must not depend on having got that far.
        source_topdir = paths.repo_root.parent
        if _is_file(source_topdir / ".west" / "config") and _manifest_points_at(
            source_topdir, paths.repo_root
        ):
            return (
                _refusal(
                    ExitCode.VALIDATION_FAILURE,
                    "workspace-orphan-refused",
                    [
                        f"{_native(paths.repo_root)} is the manifest repository of the "
                        f"west workspace at {_native(source_topdir)}, so moving it to "
                        f"{_native(target)} would leave that workspace pointing at "
                        f"nothing -- its .west/config would still name this checkout.",
                        "Nothing was moved and the default SDK was not changed.",
                        "Bootstrap that workspace in place (drop --workspace), or clone a "
                        "SECOND alp-sdk checkout under the directory you want and pass "
                        "--sdk-root pointing at that one.",
                    ],
                    payload(),
                ),
                reported_project,
                sdk,
            )

    # Host gate + prerequisite gate, AFTER the write-avoiding checks above
    # (whose relative order the existing test suite already pins) but BEFORE
    # the relocation WRITE below and every other write past it -- tan-cli#284
    # review majors: both refusals are knowable from facts that do not depend
    # on where the checkout ends up (`board_path`/`sdk_root` name the SAME
    # files whether read before or after a `--workspace` move; PATH tool
    # presence is as static as the enclosing-`.west` fact just checked), so
    # deciding them AFTER relocating + repointing the global default SDK --
    # only to leave both in place on refusal, or roll them back -- was
    # strictly worse than never doing either in the first place. Refuse ONLY
    # a project whose every in-play core is Yocto, on a non-Linux host: a
    # mixed board still bootstraps -- nothing here is Yocto-specific (venv +
    # west + Zephyr requirements) and its Zephyr cores need exactly this.
    runtimes = read_board_runtimes(board_path, sdk_root)
    gate = yocto_gate(runtimes, host)
    if gate == GATE_REFUSE:
        return (
            _refusal(
                ExitCode.VALIDATION_FAILURE, "yocto-host", [yocto_only_refusal()], payload()
            ),
            reported_project,
            sdk,
        )
    if gate == GATE_WARN:
        # Same spelling at severity `warning`, deliberately (I-73): promoting it
        # would refuse a board that can bootstrap its Zephyr cores.
        log.warn("yocto-host", yocto_mixed_warning())

    floor = resolve_python_floor(facts)
    skew = python_floor_skew_warning(
        floor.manifest, floor.effective, floor.source, from_manifest=facts.from_manifest
    )
    if skew is not None:
        log.warn(*skew)

    host_python, refusal = check_prerequisites(facts, host, floor)
    if refusal is not None:
        # The ONLY path that fills `missingPrerequisites`.
        return (
            Outcome(
                ExitCode.RUNTIME_FAILURE,
                payload(missing=reported_missing(refusal.missing)),
                [
                    *log.take_issues(),
                    Issue(f"bootstrap.{refusal.code}", "error", " ".join(refusal.lines)),
                ],
                list(refusal.lines),
            ),
            reported_project,
            sdk,
        )
    if host_python is None:
        # Unreachable: `check_prerequisites` sets exactly one of the two. Stated
        # as a refusal rather than an `assert` (stripped under `-O`) or a bare
        # fall-through, because the alternative is spawning `None -m venv`.
        return (
            _refusal(
                ExitCode.INTERNAL_FAILURE,
                "internal-failure",
                ["the prerequisite gate returned neither an interpreter nor a refusal"],
                payload(),
            ),
            reported_project,
            sdk,
        )

    if guard_applies and target is not None:
        # `dry_run=dry_run` (tan-cli#323): a preview run still computes and
        # validates the destination -- so `data.sdkRoot`/`data.workspaceDir`
        # below keep reporting the planned relocation -- but performs neither
        # the `mkdir` nor the `os.rename`. The `_write_global_sdk_pointer`
        # write a few lines down is gated the same way, for the same reason:
        # a flag whose entire purpose is "show me, don't do it" must not move
        # the checkout or repoint the machine-global default SDK.
        new_root, error = relocate_checkout(paths.repo_root, target, dry_run=dry_run)
        if error is not None:
            return _fatal(error, payload(), []), reported_project, sdk
        if new_root is not None and str(new_root) != str(paths.repo_root):
            old_root = sdk_root
            # Snapshotted BEFORE anything below is mutated (tan-cli#284):
            # a later rollback restores exactly these values rather than
            # RE-DERIVING them (e.g. `workspace_dir` as `repo_root.parent`
            # is wrong for a `--workspace` run over an ADOPTED
            # `$ZEPHYR_BASE` topdir -- see `rollback_relocation_after`).
            undo = RelocationUndo(
                old_root=old_root,
                previous_pointer=_read_global_sdk_pointer(),
                project=reported_project,
                workspace_dir=paths.workspace_dir,
                venv_dir=paths.venv_dir,
            )
            paths.repo_root = new_root
            paths.workspace_dir = target
            paths.venv_dir = target / facts.venv_dir_name
            sdk_root = str(new_root)
            sdk = SdkInfo(sdk_root, active_tier)
            move_verb = "would move" if dry_run else "moved"
            set_verb = "would set" if dry_run else "set"
            log.warn(
                "workspace-relocated",
                f"{move_verb} the alp-sdk checkout from {_native(old_root)} to "
                f"{_native(sdk_root)} so the west workspace "
                f"(zephyr/modules/.west/venv) stays out of "
                f"{_native(Path(old_root).parent)}, and {set_verb} it as your default SDK "
                # `tan sdk switch --global` refuses in this build (tan-cli#305) --
                # naming the pointer mechanism itself instead of a subcommand
                # keeps this true even once that changes.
                f"(to change later: {global_default_pointer_fix_hint(_native(_home_alp_dir() / 'sdk-default'))}) "
                f"(tan-cli#185)",
            )
            # The project may live INSIDE the checkout, so rebase any
            # reported path under the OLD root onto the new one -- nothing
            # downstream may keep naming a location the checkout vacated.
            root = _rebase(root, old_root, sdk_root)
            board_path = _rebase(board_path, old_root, sdk_root)
            reported_project = Project.resolved(root, board_path)
            relocation_undo = undo
            if not dry_run:
                _write_global_sdk_pointer(sdk_root)

    log.line(f"Repo root:       {_native(paths.repo_root)}")
    if is_windows:
        log.line(
            f"Workspace dir:   {_native(paths.workspace_dir)}  (west topdir; alp-sdk is "
            f"the manifest)"
        )
        log.line(f"Python:          {host_python.version[0]}.{host_python.version[1]}")
    else:
        log.line(f"Workspace dir:   {_native(paths.workspace_dir)}")
        log.line(f"Detected OS:     {os_label(host)}")
    if not facts.from_manifest:
        # A `line`, not a `warn`: on a released SDK this is the correct,
        # expected path, not a defect. But a customer on the default text UI
        # otherwise had no way to tell a run against a legacy SDK -- every pin,
        # tool list and env line from tan's hand-ported constants -- from one
        # driven by the SDK's own declared facts.
        log.line(
            f"Facts:           tan's built-in fallbacks ({BOOTSTRAP_MANIFEST_REL_PATH} "
            f"absent -- this SDK predates alp-sdk#917)"
        )
    if dry_run:
        log.line("Dry run (--dry-run): planning only, nothing will be installed or written")

    plan = _select_workspace(log, is_windows, pin, facts, paths)
    ws = Workspace(is_windows, facts, paths.repo_root, paths.workspace_dir, paths.venv_dir)
    runner = Runner(json_mode, plan.clear_zephyr_base, dry_run)

    def planned_payload(**extra: object) -> dict[str, object]:
        return payload(planned=runner.planned if dry_run else None, **extra)

    def rollback_relocation_after(step: str) -> None:
        """When THIS run relocated the checkout and `step` (a later, genuinely
        fallible step) is the one that actually failed, undo the relocation
        rather than leaving the checkout moved and the global default SDK
        repointed for a bootstrap that never finished (tan-cli#284). Reported
        HONESTLY, not silently: the earlier `workspace-relocated` warning
        already drained into this run's issues stays, and this adds a second
        one saying what actually happened to the undo -- never asserting the
        checkout moved back when `_undo_relocation` reports it did not. No-op
        when this run never relocated anything.
        """
        nonlocal sdk_root, sdk, reported_project
        if relocation_undo is None:
            return
        moved_to = paths.repo_root
        undo = _undo_relocation(
            relocation_undo.old_root, moved_to, relocation_undo.previous_pointer
        )
        if undo.moved_back:
            # The checkout itself is back; anything the failed step already
            # created UNDER the vacated target (a partial venv/west checkout
            # lives beside the checkout, not inside it, so moving the
            # checkout back does not remove it) is left on disk -- named
            # honestly rather than asserting "nothing from this run is in
            # effect", which was true of the checkout and the pointer but
            # never of whatever `step` had already written. `paths`/
            # `sdk_root`/`reported_project` are restored on THIS branch
            # regardless of `undo.detail`: the checkout really is back at
            # `old_root`, so every reported path must say so, whether or not
            # the pointer restore below it also succeeded.
            if undo.detail is None:
                log.warn(
                    "workspace-relocation-rolled-back",
                    f"{step} failed after the checkout was moved to {_native(moved_to)} -- "
                    f"moved it back to {_native(relocation_undo.old_root)} and restored "
                    f"the previous default SDK. Anything {step} already created under "
                    f"{_native(moved_to)} (a partial venv/west checkout) was left on "
                    f"disk -- delete {_native(moved_to)} by hand if you do not want it.",
                )
            else:
                # The checkout moved back; only the pointer restore that
                # follows it failed. Naming THAT failure, not "move it back
                # by hand" for a directory that is no longer there (the
                # tan-cli#284 review blocker).
                log.warn(
                    "workspace-relocation-rolled-back",
                    f"{step} failed after the checkout was moved to {_native(moved_to)} -- "
                    f"moved it back to {_native(relocation_undo.old_root)}, but "
                    f"{undo.detail}. The default SDK pointer may still name the "
                    f"vacated path -- "
                    f"{global_default_pointer_fix_hint(_native(_home_alp_dir() / 'sdk-default'))} "
                    f"(to point at {_native(relocation_undo.old_root)}).",
                )
            paths.repo_root = Path(relocation_undo.old_root)
            paths.workspace_dir = relocation_undo.workspace_dir
            paths.venv_dir = relocation_undo.venv_dir
            sdk_root = relocation_undo.old_root
            sdk = SdkInfo(sdk_root, active_tier)
            reported_project = relocation_undo.project
        else:
            # The move-back itself refused or failed (e.g. the vacated path
            # was recreated in the meantime) -- the checkout is still at
            # `moved_to`, and NOTHING here may claim otherwise.
            log.warn(
                "workspace-relocation-rolled-back",
                f"{step} failed after the checkout was moved to {_native(moved_to)} -- "
                f"could NOT move it back to {_native(relocation_undo.old_root)} "
                f"({undo.detail}); the checkout is still at {_native(moved_to)} and the "
                f"default SDK still points there. Move it back by hand, then "
                f"{global_default_pointer_fix_hint(_native(_home_alp_dir() / 'sdk-default'))} "
                f"(to point at {_native(relocation_undo.old_root)}).",
            )

    # The venv backs BOTH later phases, so it is created when either will run.
    venv: VenvBin | None = None
    if not (no_west and no_pip):
        # `plan.adopted` passed IN, not read afterwards: the only other read of
        # it sits below at the west-reconcile step, which is AFTER the venv
        # phase -- so before tan-cli#390 the delete inside `ensure_venv` had
        # already happened by the time anything consulted adoption at all.
        venv, error = ensure_venv(ws, log, runner, host_python, adopted=plan.adopted)
        if error is not None:
            rollback_relocation_after("the workspace venv setup")
            return (
                _fatal(error, planned_payload(), log.take_issues()),
                reported_project,
                sdk,
            )

    if no_west:
        log.line("Skipping west setup (--no-west)")
    elif venv is not None:
        # Reconcile a stale `.west/config` manifest.path BEFORE west
        # init/update: the "already initialised" branch runs `west update`
        # without re-running `west init -l`, so a config left over from a
        # different SDK checkout under the same topdir would silently pull the
        # WRONG SDK's west.yml. Gated on ADOPTION, not on reuse: an adopted
        # `$ZEPHYR_BASE` topdir is not the one this derives, and needs no
        # reconciling anyway since `manifest_is_sdk` already proved its pointer
        # resolves here.
        outcome, old_rel, detail = (
            ("not-applicable", None, None)
            if plan.adopted or dry_run
            else reconcile_west_manifest_path(sdk_root)
        )
        if outcome == "rewrote":
            log.warn(
                "west-config-reconciled",
                f"reconciled {paths.workspace_dir / '.west' / 'config'} manifest.path "
                f"{old_rel} -> {detail} (it named a different SDK checkout under this "
                f"topdir, #31)",
            )
        elif outcome == "failed":
            # Silent here would be the worst of the three: `west update` is
            # about to run against whatever that unrewritten pointer names --
            # i.e. the WRONG SDK's west.yml, the exact failure this call exists
            # to prevent.
            log.warn(
                "west-config-reconcile-failed",
                f"could not reconcile {paths.workspace_dir / '.west' / 'config'} "
                f"manifest.path (currently {old_rel}): {detail}; `west update` will "
                f"resolve the manifest from whatever that pointer still names",
            )
        error = west_phase(ws, venv, log, runner, plan.reuse)
        if error is not None:
            rollback_relocation_after("`west init`/`west update`")
            return (
                _fatal(error, planned_payload(), log.take_issues()),
                reported_project,
                sdk,
            )
        # `west update` just materialised this topdir's trees from THIS SDK's
        # manifest. NOT after a failed reconcile, which is the subtle one: the
        # update resolved its manifest from the STALE pointer, so `west_phase`
        # returned success over trees belonging to the OTHER SDK, and recording
        # a sync would assert the very thing that did not happen.
        if outcome != "failed" and not plan.reuse and not dry_run:
            record_workspace_sdk(
                paths.workspace_dir,
                sdk_root,
                venv_dir_name=ws.facts.venv_dir_name,
                venv_layout=venv.bin_dir,
                requirements_path=ws.workspace_dir / ws.facts.zephyr_requirements_path,
            )

    if no_pip:
        log.line("Skipping pip installs (--no-pip)")
    elif venv is not None:
        # A floor alone cannot say "too new for the ecosystem" (tan-cli#285):
        # a WARN, never a refusal -- see `python_ceiling_warning`'s own
        # docstring for why a hard ceiling here would be its own defect,
        # symmetric to the floor bug this port already fixed. Probes the
        # VENV's own interpreter, not `host_python.version` -- a REUSED venv
        # can be running a different Python than whatever the host resolves
        # today, and pip installs run inside the venv's interpreter, never
        # the host's. Placed here, right before the pip phase that could
        # actually hit it, rather than up front: skipped entirely when
        # `--no-pip` means nothing is about to be installed anyway.
        ceiling = python_ceiling_warning(
            _venv_python_version(venv, runner, host_python.version), _native(paths.venv_dir)
        )
        if ceiling is not None:
            log.warn(*ceiling)
        pip_phase(ws, venv, log, runner, host)

    # NOTE: this does NOT install the Zephyr SDK (the cross toolchains). Real
    # silicon targets need it -- run `west sdk install` from the workspace once.
    venv_bin_dir = venv.bin_dir if venv else facts.venv_bin_dir(is_windows)
    text = optional_libs_block(facts, host)
    text.append("")
    # Deliberately NOT the oracles' "Bootstrap complete." -- `doctor`'s
    # bootstrap-fix check folds this exact line into its detail, and tan's own
    # house prefix is what reads correctly there. The only reworded line.
    #
    # Gated on which `WORKSPACE_BLOCKING` codes actually fired (tan-cli#220 /
    # tan-cli#285), ported from the Rust oracle's `verdict()`: `complete.` +
    # exit 0 must never follow a step that already said the venv is
    # incomplete, unless `--allow-partial` was passed. The workspace is still
    # real and usable either way, so `next_steps_block` below is printed
    # regardless.
    blocking = log.blocking()
    closing_lines, ok = completion_verdict(blocking, allow_partial)
    text.extend(closing_lines)
    text.extend(
        next_steps_block(
            facts, paths.tokens(), _native(paths.venv_dir), venv_bin_dir, is_windows
        )
    )
    exit_code = ExitCode.SUCCESS if ok else ExitCode.RUNTIME_FAILURE
    bootstrap_issues = log.take_issues(escalate_blocking=not ok)
    if pin_issue is not None:
        bootstrap_issues = [pin_issue, *bootstrap_issues]
    return (
        Outcome(exit_code, planned_payload(), bootstrap_issues, text),
        reported_project,
        sdk,
    )


def _rebase(value: str | None, old_root: str, new_root: str) -> str | None:
    """Repoint a reported path that fell under `old_root` at `new_root`. A path
    nowhere near the checkout is returned unchanged, never force-rebased."""
    if value is None:
        return None
    old = old_root.replace("\\", "/")
    new = new_root.replace("\\", "/")
    normalised = value.replace("\\", "/")
    if normalised == old:
        return new
    if normalised.startswith(old + "/"):
        return new + normalised[len(old) :]
    return value


def _write_global_sdk_pointer(sdk_root: str) -> None:
    """Repoint `~/.alp/sdk-default` at the checkout's new location after a
    relocation.

    Without it the Quickstart's very next documented step -- `tan init` from the
    same shell, or a fresh one tomorrow -- has no sibling `../alp-sdk` left to
    auto-discover and `tan build` fails with "alp-sdk root is unresolved". A
    printed "next command" would not survive past the terminal it was printed
    into; a written pointer does. Best-effort: every resolver already tolerates
    a stale or missing pointer by falling through to the next tier.
    """
    try:
        pointer = _home_alp_dir() / "sdk-default"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(sdk_pointer_json(sdk_root), encoding="utf-8")
    except Exception:  # noqa: BLE001 -- best-effort by contract
        pass


def _read_global_sdk_pointer() -> bytes | None:
    """`~/.alp/sdk-default`'s current bytes, or `None` when absent.

    Snapshotted immediately BEFORE a relocation overwrites it (tan-cli#284),
    so a later rollback can put back exactly what was there -- `None` restores
    "absent", not an empty file, which matters for the common case of a first
    bootstrap ever run on the machine, before any pointer existed at all.
    Best-effort, like the write it guards: a snapshot that could not be taken
    degrades the rollback, not this read.
    """
    try:
        pointer = _home_alp_dir() / "sdk-default"
        return pointer.read_bytes() if pointer.is_file() else None
    except Exception:  # noqa: BLE001 -- best-effort, like _write_global_sdk_pointer
        return None


@dataclass(frozen=True)
class RelocationUndoResult:
    """`_undo_relocation`'s outcome, as TWO independent facts rather than one
    `str | None` (tan-cli#284 review blocker): whether the checkout itself
    made it back to `old_root`, and -- regardless of that -- what, if
    anything, still went wrong. A single `str | None` conflated "the
    move-back failed, the checkout is still at the vacated path" with "the
    move-back SUCCEEDED, only the pointer restore that follows it failed",
    so a caller that treated any non-`None` return as the first case told a
    customer whose checkout HAD moved back to go hand-move a directory that
    no longer existed there.
    """

    #: Whether `relocate_checkout` actually put the checkout back at
    #: `old_root`. Callers must branch on THIS, never on `detail`, to decide
    #: which location (old vs. still-vacated) to keep reporting.
    moved_back: bool
    #: `None` when nothing went wrong; otherwise a short string naming what
    #: could not be undone -- the move itself when `moved_back` is `False`,
    #: or the pointer restore that runs only after a successful move-back
    #: when `moved_back` is `True`.
    detail: str | None


def _undo_relocation(
    old_root: str, current_repo_root: Path, previous_pointer: bytes | None
) -> RelocationUndoResult:
    """Rollback of a relocation THIS run performed, for when a step after it
    -- `ensure_venv` or `west_phase` -- turns out to be the fallible one
    after all (tan-cli#284): move the checkout back to where it was, and
    restore (or remove) the global default-SDK pointer this run overwrote.

    `moved_back=False` means `relocate_checkout` itself refused -- e.g.
    because the vacated original path was recreated in the meantime, which
    `relocate_checkout`'s own already-exists guard reports as an error, not a
    silent no-op -- and the checkout is still at `current_repo_root`.
    `moved_back=True` means the checkout IS back at `old_root`, whether or
    not the pointer restore that follows it (attempted only once the move
    itself succeeded, since a pointer aimed at a checkout that did NOT
    actually move back would be worse than leaving it alone) also succeeded.
    The caller (`rollback_relocation_after`) uses `moved_back` to decide
    which location to keep reporting, and `detail` only to word WHAT failed.
    """
    _new_root, move_error = relocate_checkout(current_repo_root, Path(old_root).parent)
    if move_error is not None:
        return RelocationUndoResult(moved_back=False, detail=move_error)
    try:
        pointer = _home_alp_dir() / "sdk-default"
        if previous_pointer is None:
            pointer.unlink(missing_ok=True)
        else:
            pointer.write_bytes(previous_pointer)
    except OSError as err:
        return RelocationUndoResult(
            moved_back=True,
            detail=f"the default SDK pointer could not be restored: {err}",
        )
    return RelocationUndoResult(moved_back=True, detail=None)


def bootstrap(
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    no_pip: bool = typer.Option(False, "--no-pip", help="Skip the pip dependency installs."),
    no_west: bool = typer.Option(False, "--no-west", help="Skip the west init/update step."),
    print_env: bool = typer.Option(
        False, "--print-env", help="Print the environment-variable lines and exit."
    ),
    allow_partial: bool = typer.Option(
        False,
        "--allow-partial",
        help=(
            "Report success even when a dependency install failed and the workspace "
            "cannot build (tan-cli#220). The failures are still printed and still "
            "reported as issues -- this only changes the verdict, for the case where "
            "the missing packages are ones you know you do not need."
        ),
    ),
    workspace: str = typer.Option(
        None,
        "--workspace",
        metavar="PATH",
        help=(
            "Build the west workspace under this directory instead of the checkout's "
            "parent, moving the checkout there first if it is not already."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Resolve everything and report the commands each step would run, without "
            "installing, cloning or writing anything."
        ),
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
    verbose: bool = typer.Option(False, "--verbose", hidden=True),
    quiet: bool = typer.Option(False, "--quiet", hidden=True),
    no_color: bool = typer.Option(False, "--no-color", hidden=True),
    non_interactive: bool = typer.Option(False, "--non-interactive", hidden=True),
    ci: bool = typer.Option(False, "--ci", hidden=True),
) -> None:
    """Set up the SDK's build environment: workspace venv, west, Python deps."""
    # The five options above are clap's `GlobalArgs` members that `bootstrap.rs`
    # accepts and never reads (tan-cli#284 review minor). Declared here purely so
    # the argv SURFACE matches: `tan bootstrap --non-interactive` is the literal
    # first-blink command in `.github/workflows/parity.yml` and
    # `docs/python-release-feasibility.md` -- without these it was a Click usage
    # error at rc=2, so that first command failed before bootstrap ever ran.
    # Same port-wide gap as `clean_cmd.clean`'s identical block; not fixed here
    # for `doctor`/`init`, which still have it.
    del verbose, quiet, no_color, non_interactive, ci
    json_mode = output_format == "json"

    reported = Project(root=None, board_yaml=None)
    sdk: SdkInfo | None = None
    try:
        outcome, reported, sdk = _run(
            project=project,
            board_yaml=board_yaml,
            sdk_root_flag=sdk_root,
            no_pip=no_pip,
            no_west=no_west,
            print_env=print_env,
            allow_partial=allow_partial,
            workspace=workspace,
            dry_run=dry_run,
            json_mode=json_mode,
        )
    except Exception as err:  # noqa: BLE001
        # The port's most-repeated defect class: an uncaught exception escapes as
        # a raw traceback, stdout stays EMPTY, and the extension renders nothing
        # with no error on either side. Every probe and every read above is
        # already guarded, so anything arriving here is a tan bug -- reported as
        # one, with an envelope. Nothing in this handler may itself throw: it
        # renders no timestamp and calls no helper that reads the filesystem.
        outcome = Outcome(
            ExitCode.INTERNAL_FAILURE,
            None,
            [
                Issue(
                    "bootstrap.internal-failure",
                    "error",
                    f"bootstrap failed unexpectedly: {type(err).__name__}: {err}",
                )
            ],
            [f"bootstrap failed unexpectedly: {type(err).__name__}: {err}"],
        )

    if json_mode:
        emit(
            Envelope(
                "bootstrap", reported, outcome.data, outcome.issues, outcome.exit_code, sdk=sdk
            )
        )
    elif print_env:
        # STDOUT, not stderr. This block is meant to be redirected --
        # `tan bootstrap --print-env > env.sh` -- so on stderr the redirect
        # target is empty while the lines still appear on the terminal: it
        # looks like it worked and wrote nothing. See `_outprint`.
        for line in outcome.text:
            _outprint(line)
    else:
        for line in outcome.text:
            _eprint(line)
    raise typer.Exit(int(outcome.exit_code))


# tan-cli#261: adds the two oracle `GlobalArgs` flags this command was still
# missing (`--all`, `--target`) on top of the five already declared above
# (`--verbose`/`--quiet`/`--no-color`/`--non-interactive`/`--ci`, all
# `hidden=True` and dropped the same way); see `tan.core.global_flags`.
bootstrap = accept_global_flags(bootstrap)
