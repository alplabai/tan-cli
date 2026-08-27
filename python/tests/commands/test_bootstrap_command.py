# SPDX-License-Identifier: Apache-2.0
"""`tan bootstrap` -- the port's own gate.

**There are no committed fixtures for this command.** `contract/README.md` puts
`bootstrap` in neither the frozen list nor the stated-uncovered rows (the Rust
side says why: `yocto-host` fires only on a non-Linux host and
`prerequisites-missing` only when a tool is absent from PATH, so a golden would
be inert on the ubuntu CI leg). So this file IS the gate, and a green run that
never compared against the oracle would prove very little -- every envelope
pinned below was first diffed against the compiled Rust `tan bootstrap` on the
same argv in the same isolated cwd. 30 of 34 diffed cases came out
byte-identical; the four that did not are each pinned here with the reason:

* `manifest-is-a-directory`, `manifest-non-utf8`, `workspace-names-a-file`
  differ ONLY in the OS error string embedded in an otherwise-identical refusal
  (`std::io::Error` vs `OSError` rendering). Asserted by SHAPE, not by the
  language's own text.
* `python-too-old` on a host the oracle accepts is the deliberate FIX -- see
  `test_the_effective_floor_refuses_a_host_the_manifest_would_accept`.

**Hermetic.** Nothing here pip-installs, clones, or writes outside `tmp_path`.
The install steps are exercised through `--dry-run`, which records the argv it
WOULD have spawned; `test_a_dry_run_writes_nothing` is what keeps that honest.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tan.commands import bootstrap_cmd, doctor_cmd
from tan.commands.bootstrap_cmd import (
    HostPython,
    PythonFloor,
    _rebase,
    _read_board_slice,
    _scan_board_slice,
    check_prerequisites,
    default_relocation_target,
    load_facts,
    reconcile_west_manifest_path,
    resolve_python_floor,
    workspace_orphan_refusal,
)
from tan.core import atomic_write as atomic_write_mod
from tan.core.sdk_default_registry import registry_path
from tan.core.bootstrap import (
    INCOMPATIBLE,
    LINUX,
    LINUX_PM_APT,
    LINUX_PM_DNF,
    MACOS,
    MANIFEST_MISMATCH,
    OTHER,
    REUSE,
    STALE,
    WINDOWS,
    BootstrapManifestError,
    MissingPrerequisite,
    Tokens,
    WorkspaceSdkRecord,
    capture_tail,
    completion_verdict,
    decide_workspace_reuse,
    detect_host_os,
    detect_linux_pm,
    die,
    fallback_facts,
    get_manifest_path,
    hint_line,
    in_play_runtimes,
    next_steps_block,
    normalize_linux_install,
    optional_libs_block,
    parent_needs_workspace_guard,
    parse_bootstrap_manifest,
    parse_west_zephyr_pin,
    parse_workspace_sdk_record,
    parse_zephyr_version_file,
    posix_refusal,
    posix_venv_unusable,
    print_env_block,
    python_ceiling_warning,
    python_floor_skew_warning,
    python_too_old,
    reported_missing,
    resolve_workspace_target,
    resolve_zephyr_pin,
    select_linux_install,
    set_manifest_path,
    windows_python_not_runnable,
    windows_refusal,
    workspace_sdk_record_json,
    yocto_gate,
    zephyr_requirements_hint,
)
from tan.exit_codes import ExitCode

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

#: The real producer output, vendored beside the Rust consumer's own fixture.
#: Read from `contract/`, never re-typed here: a manifest fact re-spelled in a
#: test is a fact with two owners.
REAL_MANIFEST = (
    Path(__file__).resolve().parents[3] / "contract" / "fixtures" / "bootstrap" / "manifest.json"
).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def run_tan(*argv, cwd, env_extra=None):
    """A real subprocess, like the sibling command suites: that also exercises
    the argv parsing + stdout framing the extension actually shells out to."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    # A developer's real `~/.alp/sdk-default` must not decide what resolves, and
    # an ambient `$ZEPHYR_BASE` must not decide the workspace plan or the floor.
    env.pop("ZEPHYR_BASE", None)
    env.pop("SOURCE_DATE_EPOCH", None)
    # The prerequisite gate probes `python3`/`python` FROM PATH and refuses a
    # host below the EFFECTIVE floor (Zephyr's 3.12) -- so which interpreter is
    # first on PATH decides the exit code of nearly every case below. An
    # unactivated venv on Ubuntu 22.04 leaves `python3` = the system 3.10, and 19
    # cases here then failed with `bootstrap.python-too-old`, saying nothing
    # about the code under test. Pin the probed interpreter to the one running
    # the suite (>= 3.12 by pyproject's `requires-python`), exactly as CI's
    # setup-python and a venv activation both do -- the same hermeticity
    # `make_sdk(tools=...)` gives the TOOL list. The refusal itself keeps its own
    # coverage in `test_the_effective_floor_refuses_a_host_the_manifest_would_accept`.
    env["PATH"] = os.pathsep.join(
        [str(Path(sys.executable).parent), *([p] if (p := env.get("PATH")) else [])]
    )
    home = Path(cwd).parent / "fake-home"
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = env["USERPROFILE"] = str(home)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
        timeout=300,
    )


def envelope(proc):
    """THE one JSON document on stdout. Zero or two are the same break for a
    consumer that parses stdout whole -- and a traceback with an empty stdout is
    the defect class this whole port keeps re-hitting."""
    assert "Traceback" not in proc.stderr, f"an exception escaped the contract:\n{proc.stderr}"
    assert proc.stdout.strip(), f"no envelope on stdout; stderr:\n{proc.stderr}"
    return json.loads(proc.stdout)


def codes(env):
    return [i["code"] for i in env["issues"]]


def make_sdk(root: Path, *, manifest=REAL_MANIFEST, tools=None, marker=True) -> Path:
    """A minimal alp-sdk checkout under `root/ws`, with `root/ws` holding NOTHING
    else -- otherwise the workspace-parent guard fires before the gate under
    test. `tools` shrinks the prerequisite lists to names this host really has.

    All three host-keyed lists (`posix`/`macos`/`windows`) are overwritten, not
    just `posix`/`windows`: `prerequisites(MACOS)` reads its OWN manifest key
    rather than falling back to `posix` (see
    `test_macos_reads_its_own_tool_list_and_falls_back_to_posix_without_one`),
    so leaving `macos` at the real manifest's `["git", "cmake", "python3",
    "ninja"]` let a macOS run silently check a DIFFERENT tool list than the one
    the test asked for -- `tools=["tan-no-such-tool-xyz"]` never touched a macOS
    host at all, since every one of those four tools is actually on the runner.
    """
    sdk = root / "ws" / "alp-sdk"
    (sdk / "scripts").mkdir(parents=True)
    if marker:
        (sdk / "scripts" / "alp_project.py").write_text("# marker\n", encoding="utf-8")
    if manifest is not None:
        (sdk / "metadata").mkdir(parents=True)
        text = manifest
        if tools is not None:
            doc = json.loads(text)
            doc["prerequisites"]["posix"] = list(tools)
            doc["prerequisites"]["macos"] = list(tools)
            doc["prerequisites"]["windows"] = list(tools)
            text = json.dumps(doc, indent=2)
        (sdk / "metadata" / "bootstrap.json").write_text(text, encoding="utf-8")
    return sdk


#: A prerequisite every host running this suite has (git is required to clone
#: it). Lets a case reach the phases instead of stopping at a missing `ninja`,
#: which is genuinely absent on the maintainer's Windows box.
PRESENT_TOOL = "git"


# ---------------------------------------------------------------------------
# The FIX: the effective Python floor. Verified against all three sources.
# ---------------------------------------------------------------------------


def test_the_three_facts_that_compose_into_the_bug_are_all_still_true():
    """The bug is a COMPOSITION, so it is only real while all three hold.

    1. the manifest declares 3.10; 2. Zephyr's CMake demands 3.12;
    3. the Rust oracle's POSIX branch says it "cannot fail on version".

    Any one of them changing upstream turns the fix below into dead weight, and
    a stale citation is how the next reader concludes the fix was unnecessary.
    """
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    assert facts.python_min_version == (3, 10)

    assert doctor_cmd.ZEPHYR_PYTHON_FLOOR == (3, 12)

    steps = (
        Path(__file__).resolve().parents[3]
        / "crates"
        / "tan-cli"
        / "src"
        / "commands"
        / "bootstrap"
        / "steps.rs"
    )
    if steps.is_file():
        assert "this branch cannot fail on version" in steps.read_text(encoding="utf-8")


def test_bootstrap_and_doctor_derive_the_effective_floor_from_one_reader(monkeypatch, tmp_path):
    """The agreement is structural, not coincidental: `resolve_python_floor`
    calls doctor's own `zephyr_python_floor` with the same argument. A second
    floor rule is how the two commands come to disagree about one host, which is
    worse than either verdict alone.

    `zephyr_base_adopts=True` is the ADOPTED case -- the only one in which the
    `$ZEPHYR_BASE` tree's own floor is honoured at all after tan-cli#495 defect
    2; the discarded case is pinned separately below."""
    zephyr = tmp_path / "zephyr"
    (zephyr / "cmake" / "modules").mkdir(parents=True)
    (zephyr / "cmake" / "modules" / "python.cmake").write_text(
        "set(PYTHON_MINIMUM_REQUIRED 3.14)\n", encoding="utf-8"
    )
    monkeypatch.setenv("ZEPHYR_BASE", str(zephyr))

    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    floor = resolve_python_floor(facts, zephyr_base_adopts=True)
    doctor_floor, doctor_source = doctor_cmd.zephyr_python_floor(str(zephyr))

    # Read from the real file on the customer's machine, so a Zephyr bump raises
    # the floor with no tan release.
    assert floor.effective == (3, 14) == doctor_floor
    assert floor.source == doctor_source
    assert floor.manifest == (3, 10)


def test_bootstrap_reads_the_manifests_zephyr_floor_with_no_workspace_to_adopt():
    """tan-cli#606: `zephyr_base_adopts=False` is the shape every host has at
    `tan bootstrap` time -- nothing has run `west update` yet, so there is no
    `python.cmake` to read. Before this fix that always landed on the
    hardcoded `ZEPHYR_PYTHON_FLOOR` pin even though `facts` already carries
    the same fact, live, as `zephyr_python_min_version`. `REAL_MANIFEST`
    declares `zephyr.pythonMinVersion: "3.12"` -- the SAME number as the pin
    today -- so this asserts on PROVENANCE, not a value the two happen to
    share: a manifest bump to the Zephyr floor must reach this without a tan
    release, which "coincidentally equal" cannot prove.
    """
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    assert facts.zephyr_python_min_version == (3, 12)

    floor = resolve_python_floor(facts, zephyr_base_adopts=False)

    assert floor.effective == (3, 12)
    assert "zephyr.pythonMinVersion" in floor.source
    assert "tan's built-in pin" not in floor.source


def test_the_effective_floor_refuses_a_host_the_manifest_would_accept():
    """**The fix.** A 3.10 host clears the manifest's own floor and is refused
    anyway, with the frozen `python-too-old` code, because 3.12 is what Zephyr's
    CMake will enforce. The oracle refuses this on Windows only, against 3.10 --
    so on Ubuntu 22.04 (`python3` = 3.10) it accepted the host and the first
    build died inside Zephyr's configure.

    Verified for real on Ubuntu 22.04 with `python3` 3.10.12: the gate returns
    `python-too-old`. Reproduced here as a pure call so it runs on every host.
    """
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    floor = PythonFloor(effective=(3, 12), source="zephyr python.cmake", manifest=(3, 10))
    refusal = python_too_old(
        (3, 10), floor.effective, facts.install_for_host(LINUX, linux_pm=LINUX_PM_APT),
        floor_source=floor.source, manifest_floor=floor.manifest,
    )
    assert refusal.code == "python-too-old"
    assert refusal.missing == ()  # no `{tool, command}` pair can carry "yours is 3.10"
    line = refusal.lines[0]
    assert "Python 3.10 found; the SDK tooling needs >= 3.12" in line
    assert "zephyr python.cmake" in line
    # Names the SKEW, or a customer greps the manifest, reads 3.10 and concludes
    # tan is broken.
    assert "declares only 3.10" in line


#: The harness's OWN regex (`scripts/e2e-full.sh`'s Scenario-B FLOOR_CHECK,
#: verbatim), not a paraphrase of it -- a loose substring pin (a prior
#: version of this constant pinned the bare word `"found"`) is exactly the
#: defect class this whole PR is about, just relocated into the test: three
#: of six candidate rewords that break the harness's structural match (an
#: added patch component, a restructure keeping both words, a swapped
#: clause order) left a substring-only pin GREEN (tan-cli#757 review,
#: second pass). Compare `E2E_DIVERGENCE_PHRASE` above -- a five-word
#: fragment chosen because it cannot survive a reword -- this constant now
#: matches that bar by construction: it cannot pass unless the STRUCTURE the
#: harness parses (two `X.Y` pairs bracketed by "found" ... "needs >=") is
#: still there. Nothing else pins this: no workflow runs `e2e-full.sh` at
#: all (`grep -rn e2e-full.sh .github/workflows/` finds nothing), so a
#: reword would otherwise leave every CI job green while the harness's
#: regex silently stops matching. Reword the message and this fails, naming
#: the file to update.
E2E_FLOOR_CHECK_REGEX = r"Python (\d+)\.(\d+) found.*needs >= (\d+)\.(\d+)"


def test_e2e_full_sh_floor_wording_survives_in_the_refusal_message():
    """tan-cli#757 review MINOR 5 (second pass). See `E2E_FLOOR_CHECK_REGEX`
    above for why this is pinned separately from
    `test_the_effective_floor_refuses_a_host_the_manifest_would_accept`."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    floor = PythonFloor(effective=(3, 12), source="zephyr python.cmake", manifest=(3, 10))
    refusal = python_too_old(
        (3, 10), floor.effective, facts.install_for_host(LINUX, linux_pm=LINUX_PM_APT),
        floor_source=floor.source, manifest_floor=floor.manifest,
    )
    line = refusal.lines[0]
    match = re.search(E2E_FLOOR_CHECK_REGEX, line)
    assert match is not None, (
        f"scripts/e2e-full.sh's FLOOR_CHECK regex {E2E_FLOOR_CHECK_REGEX!r} no "
        f"longer matches this message. That regex is what confirms a "
        f"bootstrap.python-too-old refusal was actually EARNED (found < floor) "
        f"before honouring it as a reason to skip the rest of Scenario B -- a "
        f"reword here silently stops that check, degrading safe (every case "
        f"falls to NOPARSE, a scored failure) but not correctly. Update "
        f"scripts/e2e-full.sh's FLOOR_CHECK regex in the same change.\n\n"
        f"message was: {line!r}"
    )
    found = (int(match.group(1)), int(match.group(2)))
    effective = (int(match.group(3)), int(match.group(4)))
    assert found == (3, 10), f"regex extracted the wrong 'found' pair: {found!r}"
    assert effective == (3, 12), f"regex extracted the wrong 'floor' pair: {effective!r}"


def test_the_skew_case_suppresses_the_manifests_own_install_command():
    """`sudo apt-get install -y python3` installs 3.10 on Ubuntu 22.04 -- the
    exact version being refused. Printing the manifest's command in the skew case
    would send the customer round a loop, so it is dropped and the prose carries
    the real remedy."""
    install = parse_bootstrap_manifest(REAL_MANIFEST).install_for_host(LINUX, linux_pm=LINUX_PM_APT)
    assert install["python3"] == "sudo apt-get install -y python3"

    skewed = python_too_old(
        (3, 10), (3, 12), install, floor_source="zephyr", manifest_floor=(3, 10)
    )
    assert "apt-get" not in skewed.lines[0]
    assert "install a Python 3.12+" in skewed.lines[0]

    # No skew -> the manifest's command IS for the floor being enforced, so it
    # travels, exactly as the oracle prints it.
    agreed = python_too_old(
        (3, 9), (3, 10), install, floor_source="the manifest", manifest_floor=(3, 10)
    )
    assert "sudo apt-get install -y python3" in agreed.lines[0]


def test_the_gate_applies_the_version_floor_on_every_host_not_just_windows(monkeypatch):
    """The oracle's asymmetry IS the bug: `steps.rs` refuses below the floor on
    the Windows branch and states outright that the POSIX branch "cannot fail on
    version". Both branches refuse here."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    blank = dict.fromkeys(
        ("prerequisites_posix", "prerequisites_macos", "prerequisites_windows"), ()
    )
    facts = type(facts)(**{**vars(facts), **blank})
    floor = PythonFloor(effective=(99, 9), source="a floor no host can meet", manifest=(3, 10))

    import tan.commands.bootstrap_cmd as mod

    monkeypatch.setattr(mod, "probe_host_python", lambda _floor: HostPython(("python3",), (3, 12)))
    for host in (LINUX, MACOS, WINDOWS, OTHER):
        python, refusal = check_prerequisites(facts, host, floor)
        assert python is None, host
        assert refusal is not None and refusal.code == "python-too-old", host


def test_the_skew_warning_matches_doctors_pythonfloor_check_on_both_numbers():
    """One manifest defect, one verdict. Two commands describing it differently
    is the drift this port keeps hitting."""
    skew = python_floor_skew_warning((3, 10), (3, 12), "zephyr python.cmake")
    assert skew is not None
    code, message = skew
    assert code == "python-floor-skew"

    check = doctor_cmd.python_floor_skew_check((3, 10), (3, 12), "zephyr python.cmake")
    assert check is not None and check.status == "warn"
    for fragment in ("3.10", "3.12", "metadata/bootstrap.json"):
        assert fragment in message and fragment in check.detail

    # Agreeing floors raise nothing on either side.
    assert python_floor_skew_warning((3, 12), (3, 12), "x") is None
    assert doctor_cmd.python_floor_skew_check((3, 12), (3, 12), "x") is None


def test_neither_side_tells_the_user_to_raise_the_manifest_floor():
    """tan-cli#300. Raising `prerequisites.pythonMinVersion` was tried and
    REVERTED (alp-sdk#1078): the key is host-universal while this floor is
    Zephyr's, so raising it refuses a 3.10/3.11 host for a Yocto-only project
    that builds today.

    This is asserted because nothing asserted it before, which is exactly why
    the advice shipped in v0.5.0-rc2 -- and why it shipped on the path that
    matters most: `bootstrap` emits this WHILE REFUSING, so it is the last line
    a blocked user reads.
    """
    _, message = python_floor_skew_warning((3, 10), (3, 12), "zephyr python.cmake")
    check = doctor_cmd.python_floor_skew_check((3, 10), (3, 12), "zephyr python.cmake")

    # `doctor` splits its prose across `detail` and `fix`; `bootstrap` has one
    # string. Read whatever the user actually sees, not one field of it.
    doctor_text = f"{check.detail} {check.fix or ''}"

    for text, where in ((message, "bootstrap"), (doctor_text, "doctor")):
        assert "Raise `prerequisites.pythonMinVersion`" not in text, where
        assert "alp-sdk#1078" in text, where


def test_the_skew_warning_reaches_the_wire_even_on_a_successful_run(tmp_path):
    """The host is fine; the manifest is not. Reported on success too, or the
    fix never lands in `metadata/bootstrap.json`."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    env = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
        )
    )
    assert env["exitCode"] == 0 and env["ok"] is True
    skew = [i for i in env["issues"] if i["code"] == "bootstrap.python-floor-skew"]
    assert len(skew) == 1 and skew[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# The envelope contract
# ---------------------------------------------------------------------------


def test_the_envelope_key_set_and_sdk_omission(tmp_path):
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    env = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
        )
    )
    assert set(env) == {"command", "ok", "exitCode", "project", "sdk", "data", "issues"}
    assert env["command"] == "bootstrap"
    # `ok` is DERIVED from the exit code, never set independently.
    assert env["ok"] is (env["exitCode"] == 0)
    assert set(env["data"]) == {
        "schemaVersion", "sdkRoot", "workspaceDir", "venvDir", "zephyrBase",
        "factsFromManifest", "zephyrPin", "noPip", "noWest", "printEnv",
        "missingPrerequisites",
    }
    assert env["data"]["schemaVersion"] == "2"  # the STRING, not the number
    # `sdk.root` is ALWAYS forward-slash separated (normalised in
    # `SdkInfo.as_dict`); never assert the platform-native form here -- that
    # exact mistake shipped once.
    assert "\\" not in env["sdk"]["root"]
    assert env["sdk"]["sourceTier"] == "sdkRootFlag"
    # `data.sdkRoot` by contrast is NATIVE, so a consumer comparing it against
    # `workspaceDir` by prefix has one separator.
    assert env["data"]["sdkRoot"].startswith(env["data"]["workspaceDir"])


def test_a_relative_sdk_root_flag_resolves_absolute_everywhere_in_the_envelope(tmp_path):
    """tan-cli#217/#296: `tan bootstrap --sdk-root ./alp-sdk --format json`
    reported `data.sdkRoot` -- and everything derived from it -- exactly as
    typed. A consumer reading the envelope from any OTHER cwd (the vscode
    extension's, in particular) resolves nothing. Anchored the same way #263
    anchored `init`'s `.alp/sdk-path` pin: against the cwd THIS run actually
    used, not the string the caller typed.
    """
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    ws = sdk.parent
    env = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", "./alp-sdk", cwd=ws,
        )
    )
    assert env["exitCode"] == 0 and env["ok"] is True

    ws_abs = os.path.abspath(str(ws)).replace("\\", "/")
    for key in ("sdkRoot", "workspaceDir", "venvDir", "zephyrBase"):
        value = env["data"][key]
        assert value, f"data.{key} is empty"
        assert os.path.isabs(value), f"data.{key}={value!r} is not absolute"
        assert value.replace("\\", "/").startswith(ws_abs), key

    assert env["data"]["workspaceDir"].replace("\\", "/") == ws_abs
    assert env["data"]["sdkRoot"].replace("\\", "/") == f"{ws_abs}/alp-sdk"
    assert os.path.isabs(env["project"]["root"])
    assert Path(env["sdk"]["root"]).is_absolute()


def test_the_sdk_key_is_absent_not_null_when_nothing_resolves(tmp_path):
    empty = tmp_path / "ws"
    empty.mkdir()
    proc = run_tan("bootstrap", "--format", "json", cwd=empty)
    env = envelope(proc)
    assert proc.returncode == 2
    assert "sdk" not in env, "an absent SDK must OMIT the key, never emit null"
    assert env["project"] == {"root": None, "boardYaml": None}
    assert codes(env) == ["bootstrap.sdk-root-unresolved"]
    # Every path field is `""`, never null.
    assert env["data"]["sdkRoot"] == env["data"]["workspaceDir"] == ""
    assert env["data"]["missingPrerequisites"] is None


def test_a_broken_project_pin_is_reported_even_when_nothing_else_resolves(tmp_path):
    """tan-cli#926 -- the `bootstrap` instance of the tan-cli#900 class
    (`clean`/`presets` already had this; `examples`/`generate` got it in
    #900; `bootstrap`/`new-som` are the sixth and seventh).

    `_run` used to return its `sdk-root-unresolved` refusal the moment
    `resolved is None`, BEFORE `pin_issue`/`foreign_issue` were computed a
    few lines further down -- so a workspace whose `.alp/sdk-path` names a
    checkout that no longer exists, with no sibling for the ladder to fall
    through to either, reported `bootstrap.sdk-root-unresolved` alone. The
    customer was told the SDK root was unresolved but never that their own
    broken project pin was the reason -- `presets`/`clean` disclose it from
    the identical ladder.

    Fails against dev: `codes(env)` there is `["bootstrap.sdk-root-
    unresolved"]` alone, with no leading `sdk.project-pin-unresolved` and
    `"gone-checkout"` nowhere in the envelope."""
    ws = tmp_path / "ws"
    (ws / ".alp").mkdir(parents=True)
    (ws / ".alp" / "sdk-path").write_text(
        json.dumps({"sdkPath": str(tmp_path / "gone-checkout")})
    )
    proc = run_tan("bootstrap", "--format", "json", cwd=ws)
    env = envelope(proc)
    assert proc.returncode == 2
    assert codes(env) == ["sdk.project-pin-unresolved", "bootstrap.sdk-root-unresolved"]
    assert "gone-checkout" in env["issues"][0]["message"]
    # Still no usable checkout -- still no `sdk` block.
    assert "sdk" not in env


def test_text_mode_renders_the_broken_project_pin_warning_json_already_carries(tmp_path):
    """tan-cli#926's fix (see the test above) landed the `sdk.project-pin-
    unresolved` warning in `--format json`'s `issues[]`, but the SAME
    `sdk-root-unresolved` refusal's TEXT output (`_refusal`'s `text` is
    `list(lines)` alone -- it never saw `issues`) stayed silent about the
    broken pin: exactly the tan-cli#677 defect, recurring on this refusal path
    instead of the success path #677 originally fixed.

    Pre-fix, the JSON assertion above passes and this text assertion fails:
    `.alp/sdk-path` and the dangling `gone-checkout` value never appear in
    stderr even though the identical invocation's `--format json` carries
    them."""
    ws = tmp_path / "ws"
    (ws / ".alp").mkdir(parents=True)
    (ws / ".alp" / "sdk-path").write_text(
        json.dumps({"sdkPath": str(tmp_path / "gone-checkout")})
    )
    text = run_tan("bootstrap", cwd=ws)
    assert text.returncode == 2
    assert text.stdout == ""
    assert ".alp/sdk-path" in text.stderr, (
        f"DEFECT (tan-cli#677 recurrence): JSON carries sdk.project-pin-"
        f"unresolved but text does not render it:\n{text.stderr}"
    )
    assert "gone-checkout" in text.stderr
    assert "alp-sdk root is unresolved" in text.stderr


def test_missing_prerequisites_is_null_or_populated_but_never_an_empty_list(tmp_path):
    """`[]` would spell "checked, nothing missing" -- which is what a successful
    run reports as `null`. One fact, one spelling."""
    ok = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(make_sdk(tmp_path / "a", tools=[PRESENT_TOOL])),
            cwd=tmp_path / "a" / "ws",
        )
    )
    assert ok["data"]["missingPrerequisites"] is None

    refused = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(make_sdk(tmp_path / "b", tools=["tan-no-such-tool-xyz"])),
            cwd=tmp_path / "b" / "ws",
        )
    )
    assert refused["exitCode"] == 1  # RuntimeFailure, matching the oracle
    assert codes(refused)[-1] == "bootstrap.prerequisites-missing"
    assert refused["data"]["missingPrerequisites"] == [
        {"tool": "tan-no-such-tool-xyz", "command": None}
    ]


def test_text_mode_writes_nothing_at_all_to_stdout(tmp_path):
    """stdout is the envelope channel. One stray byte and the extension renders
    nothing, with no error on either side."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    proc = run_tan("bootstrap", "--no-west", "--no-pip", "--sdk-root", str(sdk), cwd=sdk.parent)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "bootstrap: complete." in proc.stderr
    assert "Next steps:" in proc.stderr


def test_a_refusals_text_output_is_the_issue_message_split_back_into_lines(tmp_path):
    """The envelope's issue message is `" ".join(lines)` -- which is exactly why
    `data.missingPrerequisites` exists: an install command contains the same
    spaces the join used, so the split is not recoverable."""
    sdk = make_sdk(tmp_path, tools=["tan-no-such-tool-xyz"])
    text = run_tan("bootstrap", "--no-west", "--no-pip", "--sdk-root", str(sdk), cwd=sdk.parent)
    assert text.stdout == ""
    assert "Missing required tools:" in text.stderr

    env = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
        )
    )
    message = [i for i in env["issues"] if i["code"] == "bootstrap.prerequisites-missing"][0]
    assert message["severity"] == "error"
    # The refusal's own lines are the UNPREFIXED ones. Warnings stream live above
    # them carrying the `bootstrap: ` progress prefix, exactly as the oracle's
    # `Log::warn` prints them -- the copy-pasteable refusal must not inherit it.
    refusal_lines = [
        line
        for line in text.stderr.splitlines()
        if line.strip() and not line.startswith("bootstrap: ")
    ]
    assert message["message"] == " ".join(refusal_lines)
    # The CONTRACT is that the first refusal line names the missing tools; its
    # exact shape is the HOST's, and both oracles are honoured verbatim.
    # `bootstrap.ps1` heads a per-tool list, `bootstrap.sh` puts the names inline
    # on one line -- so pinning the PowerShell rendering here failed on Linux
    # against perfectly correct POSIX output.
    assert refusal_lines[0].startswith("Missing required tools:")
    assert "tan-no-such-tool-xyz" in " ".join(refusal_lines)


def test_text_mode_renders_the_foreign_global_default_warning_json_already_carries(tmp_path):
    """tan-cli#677: `bootstrap` computes `sdk.global-default-foreign-project`
    and emits it in `issues[]` under `--format json`, but the default TEXT
    output never printed it -- while `bootstrap.python-floor-skew` from the
    same array DOES print (`_run`'s `log.warn(*skew)`). `doctor` and `init`
    both render this warning in text; `bootstrap` -- the command that WRITES
    `~/.alp/sdk-default` in the first place -- was the odd one out.

    Pre-fix, the JSON assertion below passes (the warning IS computed and
    emitted) and the text assertion fails: the warning's own vocabulary
    ("machine-global default SDK", the foreign project's own path) never
    appears in stderr even though the identical invocation's `--format json`
    carries it.
    """
    sdk = make_sdk(tmp_path / "realsdk", tools=[PRESENT_TOOL])
    other_project = tmp_path / "otherproj"
    other_project.mkdir()
    proj = tmp_path / "myproj"
    proj.mkdir()
    # `run_tan` derives HOME as `cwd.parent / "fake-home"` when no `env_extra`
    # override is given -- both invocations below use `cwd=proj`, so both read
    # the SAME pointer written here.
    home = tmp_path / "fake-home"
    (home / ".alp").mkdir(parents=True)
    pointer = home / ".alp" / "sdk-default"
    pointer.write_text(
        json.dumps({"sdkPath": str(sdk), "writtenFor": str(other_project)}),
        encoding="utf-8",
    )

    json_env = envelope(
        run_tan("bootstrap", "--dry-run", "--no-west", "--no-pip", "--format", "json", cwd=proj)
    )
    assert json_env["exitCode"] == 0
    assert "sdk.global-default-foreign-project" in codes(json_env), (
        "precondition unmet: the JSON surface must carry the warning"
    )

    text = run_tan("bootstrap", "--dry-run", "--no-west", "--no-pip", cwd=proj)
    assert text.returncode == 0
    assert "machine-global default SDK" in text.stderr, (
        f"DEFECT (tan-cli#677): JSON carries sdk.global-default-foreign-project "
        f"but text does not render it:\n{text.stderr}"
    )
    assert str(other_project) in text.stderr


@pytest.mark.parametrize(
    ("flag", "key"),
    [("--no-pip", "noPip"), ("--no-west", "noWest"), ("--print-env", "printEnv")],
)
def test_each_flag_is_reflected_in_the_payload(flag, key, tmp_path):
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    env = envelope(
        run_tan("bootstrap", flag, "--format", "json", "--sdk-root", str(sdk), cwd=sdk.parent)
    )
    assert env["data"][key] is True


@pytest.mark.parametrize(
    "flag", ["--verbose", "--no-color", "--non-interactive", "--ci", "--quiet"]
)
def test_the_globals_the_oracle_ignores_are_accepted_not_rejected(flag, tmp_path):
    """tan-cli#284 review minor (bootstrap_cmd.py:2244): `bootstrap` declared
    none of clap's `GlobalArgs` members, so each of these was a Click usage
    error at exit 2 where the oracle exits 0 -- `tan bootstrap
    --non-interactive` is the literal first-blink command in
    `.github/workflows/parity.yml` and `docs/python-release-feasibility.md`."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json",
        "--sdk-root", str(sdk), flag, cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode == 0, env


# ---------------------------------------------------------------------------
# Refusals, in the order the run applies them
# ---------------------------------------------------------------------------


def test_print_env_answers_on_a_host_that_is_still_missing_tools(tmp_path):
    """`--print-env` short-circuits BEFORE the prerequisite check, so it works on
    a machine that cannot yet bootstrap. The manifest's real tool list stands
    here (this host is missing `ninja`) and the run still exits 0."""
    sdk = make_sdk(tmp_path)
    proc = run_tan(
        "bootstrap", "--print-env", "--format", "json", "--sdk-root", str(sdk), cwd=sdk.parent
    )
    env = envelope(proc)
    assert proc.returncode == 0 and env["issues"] == []
    assert env["data"]["zephyrBase"].endswith("zephyr")


def test_print_env_and_workspace_are_refused_together(tmp_path):
    sdk = make_sdk(tmp_path)
    proc = run_tan(
        "bootstrap", "--print-env", "--workspace", str(tmp_path / "elsewhere"),
        "--format", "json", "--sdk-root", str(sdk), cwd=sdk.parent,
    )
    assert proc.returncode == 2
    assert codes(envelope(proc)) == ["bootstrap.print-env-workspace-conflict"]


@pytest.mark.parametrize(
    ("original", "mutation", "fragment"),
    [
        ('"schemaVersion": 1', '"schemaVersion": 99', "schemaVersion 99"),
        # The PREREQUISITES floor, spelled with its value: tan-cli#585's
        # re-vendor gave the fixture a SECOND `pythonMinVersion` (`zephyr`'s,
        # `3.12`) -- a key-only match would have taken the first line in the
        # file, mutating whichever field happens to come first rather than
        # the one this row names. Both are read now (tan-cli#606), and each
        # has its own row below, so a key-only match can no longer silently
        # test the wrong field.
        ('"pythonMinVersion": "3.10"', '"pythonMinVersion": "three.ten"', "is not MAJOR.MINOR"),
        # tan-cli#606: the ZEPHYR-scoped floor, previously the field "no
        # consumer here reads" -- now read by `zephyr_python_floor`'s
        # manifest fallback, so a malformed value must refuse the same way
        # `prerequisites.pythonMinVersion` above does, not silently fall
        # through.
        (
            '"pythonMinVersion": "3.12"',
            '"pythonMinVersion": "twelve.oh"',
            "zephyr.pythonMinVersion `twelve.oh` is not MAJOR.MINOR",
        ),
        ('"dirName": ".venv"', '"dirName": "../escape"', "is not a plain relative path"),
    ],
)
def test_a_present_but_unusable_manifest_is_fatal_never_a_silent_fallback(
    original, mutation, fragment, tmp_path
):
    """Falling back HERE would re-introduce hand-ported behaviour against an SDK
    that explicitly declared something else. Diffed byte-identical against the
    oracle on all three."""
    assert REAL_MANIFEST.count(original) == 1, original
    sdk = make_sdk(tmp_path, manifest=REAL_MANIFEST.replace(original, mutation))
    proc = run_tan("bootstrap", "--format", "json", "--sdk-root", str(sdk), cwd=sdk.parent)
    env = envelope(proc)
    assert proc.returncode == 2  # ValidationFailure
    assert codes(env) == ["bootstrap.manifest"]
    assert fragment in env["issues"][0]["message"]
    assert env["data"]["factsFromManifest"] is False


def test_an_absent_manifest_falls_back_but_says_so(tmp_path):
    """ABSENT is the ONLY case that falls back. A `chmod 000` manifest used to
    produce an envelope identical in every verdict-bearing field to a genuine
    legacy SDK's."""
    sdk = make_sdk(tmp_path, manifest=None)
    facts = load_facts(str(sdk))
    assert facts.from_manifest is False
    assert facts.zephyr_version == "v4.4.1"

    env = envelope(
        run_tan(
            "bootstrap", "--print-env", "--format", "json", "--sdk-root", str(sdk),
            cwd=sdk.parent,
        )
    )
    assert env["data"]["factsFromManifest"] is False
    assert env["data"]["zephyrPin"] == "4.4.1"


def test_a_manifest_that_is_present_but_unreadable_is_not_an_absent_one(tmp_path):
    """A DIRECTORY at the manifest's path is the portable stand-in for `chmod
    000`: present, unreadable, and reproducible on Windows. The oracle's own
    message text differs (its `std::io::Error` renders "Access is denied. (os
    error 5)"), so the SHAPE is asserted, not the language's string."""
    sdk = make_sdk(tmp_path, manifest=None)
    (sdk / "metadata").mkdir(exist_ok=True)
    (sdk / "metadata" / "bootstrap.json").mkdir()

    with pytest.raises(BootstrapManifestError) as caught:
        load_facts(str(sdk))
    message = str(caught.value)
    assert message.startswith("metadata/bootstrap.json could not be read: ")
    assert message != "metadata/bootstrap.json could not be read: "  # the OS reason travels


def test_a_non_utf8_manifest_is_refused_rather_than_read_as_mojibake(tmp_path):
    sdk = make_sdk(tmp_path, manifest=None)
    (sdk / "metadata").mkdir(exist_ok=True)
    (sdk / "metadata" / "bootstrap.json").write_bytes(b'{"schemaVersion": 1, "x": "\xff\xfe"}')
    with pytest.raises(BootstrapManifestError):
        load_facts(str(sdk))


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ("", "requires a non-empty path"),
        ("   ", "requires a non-empty path"),
        ("/e/foo/ws", "has a root but no drive"),
    ],
)
def test_workspace_is_validated_before_anything_touches_the_disk(value, fragment):
    """This relocates a customer's checkout, so `--workspace ""` (the classic
    unset-`$WS` shell accident) or an MSYS-style `/e/foo/ws` on Windows must
    never resolve to a guess."""
    if value.strip().startswith("/") and os.name != "nt":
        pytest.skip("a rooted path is unambiguous off Windows")
    with pytest.raises(ValueError, match=fragment):
        resolve_workspace_target(value, os.getcwd())


def test_the_workspace_parent_guard_relocates_into_alp_workspace_automatically(tmp_path):
    """tan-cli#302: the documented quickstart -- download `tan.exe`, clone
    `alp-sdk` beside it, run `tan bootstrap` -- makes tan's OWN binary the
    "other content" that used to trip this guard, turning the FIRST command in
    the product into a refusal for following the install instructions
    literally. The refusal even NAMED `<parent>/alp-workspace` as the fix
    (`default_relocation_target`'s own choice); this proves tan now performs
    that move itself, saying so plainly, rather than asking for it back."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    (sdk.parent / "unrelated.txt").write_text("x", encoding="utf-8")
    target = sdk.parent / "alp-workspace"
    new_sdk = target / sdk.name

    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json",
        "--sdk-root", str(sdk), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode == 0
    codes_seen = codes(env)
    assert "bootstrap.workspace-guard" not in codes_seen
    assert "bootstrap.workspace-relocated" in codes_seen
    message = next(i["message"] for i in env["issues"] if i["code"] == "bootstrap.workspace-relocated")
    assert bootstrap_cmd._native(str(sdk)) in message
    assert bootstrap_cmd._native(str(new_sdk)) in message
    # tan-cli#466: the "to change later" fix hint names BOTH the legacy
    # pointer AND the registry, so deleting either (or both) by hand remains
    # a safe, complete recovery -- naming only one would leave a reader
    # editing the file that was not the one that actually answered.
    #
    # A bare `"sdk-default" in message` / `"sdk-defaults.json" in message`
    # pair (the pre-#904-review shape) is VACUOUS here even with the full
    # native path prepended: "sdk-default" is a literal PREFIX of
    # "sdk-defaults.json", so ".../sdk-default" is already a substring of
    # ".../sdk-defaults.json" -- the first assert cannot fail while the
    # second passes, no matter how much identical directory prefix is added
    # to both (measured: `test_sdk_command.py`'s own sibling avoids this only
    # by using two NON-overlapping fake names, which this real, same-`.alp`-
    # directory pair can't). Asserted instead on the exact compound substring
    # `global_default_pointer_fix_hint` actually emits -- both full native
    # paths, in the "X and/or Y" order the hint joins them in -- which cannot
    # be satisfied by the registry path alone (mutation-confirmed: rewriting
    # the hint to name only the registry breaks this exact assertion, where
    # the old bare-substring pair stayed green).
    home_alp = tmp_path / "fake-home" / ".alp"
    pointer_native = bootstrap_cmd._native(home_alp / "sdk-default")
    registry_native = bootstrap_cmd._native(registry_path(home_alp))
    assert f"delete {pointer_native} and/or {registry_native} (tan falls" in message
    # The checkout really moved: gone from the old location, present (with its
    # own content) at the new one; `unrelated.txt` is untouched, still the
    # only other thing in the original parent.
    assert not sdk.exists()
    assert (new_sdk / "scripts" / "alp_project.py").is_file()
    assert (sdk.parent / "unrelated.txt").exists()
    assert sorted(p.name for p in sdk.parent.iterdir()) == ["alp-workspace", "unrelated.txt"]
    # The envelope's own paths agree with where the checkout actually ended up
    # (tan-cli#284's review majors, re-applying to the auto-relocated case).
    assert env["data"]["sdkRoot"] == bootstrap_cmd._native(str(new_sdk))
    assert env["data"]["workspaceDir"] == bootstrap_cmd._native(str(target))
    # tan-cli#185 (shared with the explicit `--workspace` path): the global
    # default SDK now points at the new location, AND (tan-cli#464) records
    # which directory's bootstrap wrote it -- the ONLY record of a relocation
    # (tan-cli#464 review): a directory-scoped project pin was tried and
    # reverted (see `bootstrap_cmd._run`'s own comment at the relocation
    # write) -- this cwd is the workspace PARENT, not a project, and a
    # bootstrap from `$HOME` would have pinned inside tan's own machine-global
    # config dir.
    pointer = tmp_path / "fake-home" / ".alp" / "sdk-default"
    assert pointer.exists()
    global_doc = json.loads(pointer.read_text(encoding="utf-8"))
    assert global_doc["sdkPath"] == str(new_sdk)
    assert global_doc["writtenFor"] == str(sdk.parent).replace("\\", "/")
    assert not (sdk.parent / ".alp" / "sdk-path").exists()  # not a project pin
    # tan-cli#466: the origin-keyed sibling, written ALONGSIDE the legacy
    # pointer, keyed by the same `written_for` origin (the workspace parent
    # bootstrap ran in).
    registry = tmp_path / "fake-home" / ".alp" / "sdk-defaults.json"
    assert registry.exists()
    registry_doc = json.loads(registry.read_text(encoding="utf-8"))
    # `.replace("\\", "/")` on the RHS too (review, #904 second round, blocker
    # 2): the registry's `sdkPath` is now written through `_to_posix` at
    # `bootstrap_cmd._write_global_sdk_registry`, forward-slashed on every
    # platform -- unlike the LEGACY pointer's `sdkPath` two lines up, which
    # keeps storing `sdk_root` native (pre-existing #464 behaviour, untouched
    # here). A bare `str(new_sdk)` on the right compared native-vs-posix and
    # only failed on the Windows shard.
    assert registry_doc[str(sdk.parent).replace("\\", "/")]["sdkPath"] == str(new_sdk).replace(
        "\\", "/"
    )


def test_write_global_sdk_registry_normalises_a_native_sdk_root_to_posix(tmp_path, monkeypatch):
    """Review, #904 second round, blocker 2, proved DIRECTLY and
    deterministically on every platform (not just reproduced on Windows CI):
    `_write_global_sdk_registry`'s `sdk_root` argument is exactly
    `str(new_root)` at its one production call site -- NATIVE rendering,
    backslashes on Windows. `_to_posix`'s own replace is a plain string
    operation with no OS dependency (`str(path).replace("\\\\", "/")`), so
    passing a Windows-shaped, backslash-laden string here reproduces the
    blocker on Linux too: pre-fix, this stored `sdk_root` byte-for-byte
    (backslashes and all); post-fix, it always renders forward-slashed,
    matching the origin KEY it sits beside in the same file.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    bootstrap_cmd._write_global_sdk_registry(
        "C:\\Users\\dev\\alp-sdk", origin="/home/u/proj"
    )

    registry_doc = json.loads(
        (home / ".alp" / "sdk-defaults.json").read_text(encoding="utf-8")
    )
    assert registry_doc["/home/u/proj"]["sdkPath"] == "C:/Users/dev/alp-sdk"


def test_a_relocating_bootstrap_leaves_a_later_doctor_able_to_find_the_sdk(tmp_path):
    """tan-cli#463: the test above proves the pointer FILE is written; this
    proves a *second, independent* `tan doctor` process -- run later, from the
    same directory, the way a customer's next terminal command actually would
    -- can still resolve the SDK from nothing but that file. The gap #463
    reported was never "the write is missing" so much as "no test ever drove
    the read side through a real subprocess boundary": `resolve_sdk_tiered`'s
    `globalDefault` tier reads `~/.alp/sdk-default` fresh on every invocation,
    so a bug in THAT read (wrong `_home_alp_dir()`, a JSON-shape mismatch
    between writer and reader, `.alp/sdk-path` wrongly outranking it) would
    pass every check above and still leave a customer's `tan doctor` reporting
    `sdk.root: None` right after a bootstrap that just told them otherwise.

    `proj/`, the workspace PARENT bootstrap ran in, is deliberately never a
    tan project of its own (no `.alp/sdk-path` is ever written there by this
    flow) -- so the tier that must answer here is the GLOBAL default, not a
    project pin `resolve_sdk_tiered` would also have checked first.
    """
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    (sdk.parent / "unrelated.txt").write_text("x", encoding="utf-8")
    new_sdk = sdk.parent / "alp-workspace" / sdk.name

    bootstrap_env = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
        )
    )
    assert bootstrap_env["exitCode"] == 0
    assert bootstrap_env["sdk"]["root"] == str(new_sdk).replace("\\", "/")
    assert not (sdk.parent / ".alp" / "sdk-path").exists()  # not a project pin

    # A FRESH process, same cwd, nothing carried over but the filesystem --
    # exactly `tan doctor` typed into the same shell a moment later.
    doctor_env = envelope(run_tan("doctor", "--format", "json", cwd=sdk.parent))
    assert "sdk" in doctor_env, "doctor lost the SDK after a relocating bootstrap"
    assert doctor_env["sdk"]["root"] == bootstrap_env["sdk"]["root"]
    assert doctor_env["sdk"]["sourceTier"] == "globalDefault"


def test_a_second_projects_relocation_no_longer_repoints_the_first(tmp_path):
    """tan-cli#464's own repro, now closed by tan-cli#466 (stage 2 of the same
    issue), driven through real, independent subprocesses (never by
    inspecting pointer bytes):

        A, right after A       sdk.root=<A's own checkout> tier=globalDefault
        (project B bootstraps and relocates)
        A (the earlier one)    sdk.root=<A's own checkout, STILL> tier=globalDefault

    Before tan-cli#464, project A silently started resolving project B's
    checkout the moment B bootstrapped -- `ok: true`, `issues: []`, because
    `~/.alp/sdk-default` is one machine-global, last-writer-wins file. #464
    (stage 1) made that DISCLOSED (`sdk.global-default-foreign-project`) but
    left the ANSWER wrong: A still resolved B's SDK, just with a warning
    attached. This test used to pin exactly that -- `current_a2` resolving
    `new_sdk_b` -- as the correct, if disclosed, outcome.

    #466 (stage 2) makes the answer correct instead: `tan bootstrap` now also
    keys `origin -> sdkPath` into `~/.alp/sdk-defaults.json`, and
    `resolve_sdk_tiered`'s `globalDefault` tier picks the DEEPEST registry key
    that CONTAINS the caller's workspace before ever consulting the shared
    single pointer. A's own bootstrap already wrote `proj_a -> new_sdk_a`
    into that registry, so A queried from `proj_a` resolves ITS OWN checkout
    no matter which project bootstrapped last -- `sourceTier` stays
    `"globalDefault"` (a registry hit IS the machine-default mechanism, keyed,
    not a new tier), but the root is A's, and the foreign warning does not
    fire, because a caller a registry entry was written FOR is by
    construction not reading someone else's answer.

    The genuinely-foreign case -- a caller no registry entry covers at all --
    still falls through to the legacy pointer and still discloses; that path
    is `test_foreign_global_default_coverage.py`'s `two_projects` fixture,
    which now queries from a location outside every registered origin for
    exactly this reason.

    A per-project pin at bootstrap time (`.alp/sdk-path` written in the
    directory bootstrap ran in) was tried and reverted on review of #464: that
    directory is bootstrap's cwd, the workspace PARENT in the quickstart, not
    a project -- a bootstrap run from `$HOME` would have pinned inside tan's
    OWN machine-global config dir. The registry keeps that same "cwd is not
    necessarily a project" shape (an origin is just a directory bootstrap ran
    in, not asserted to be a project root) but escapes the single-pointer
    contention by keying on it instead of overwriting one shared slot.

    ONE shared HOME across the whole sequence (tan-cli#463's own lesson: an
    "isolated HOME" control that resets between calls never lets the
    precondition -- a SECOND project's relocation -- actually land, and a
    probe that never triggers its own precondition proves nothing).
    """
    home = tmp_path / "shared-home"
    env_extra = {"HOME": str(home), "USERPROFILE": str(home)}

    sdk_a = make_sdk(tmp_path / "projA", tools=[PRESENT_TOOL])
    sdk_b = make_sdk(tmp_path / "projB", tools=[PRESENT_TOOL])
    proj_a, proj_b = sdk_a.parent, sdk_b.parent
    # tan-cli#302's own trigger: something besides the clone beside it (here,
    # standing in for `relocprobe4.sh`'s own `<project>/tan` wrapper) is what
    # makes the dirty-parent guard auto-relocate each checkout.
    (proj_a / "unrelated.txt").write_text("x", encoding="utf-8")
    (proj_b / "unrelated.txt").write_text("x", encoding="utf-8")
    new_sdk_a = proj_a / "alp-workspace" / sdk_a.name
    new_sdk_b = proj_b / "alp-workspace" / sdk_b.name

    bootstrap_a = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk_a), cwd=proj_a, env_extra=env_extra,
        )
    )
    relocated_a = "bootstrap.workspace-relocated" in codes(bootstrap_a)
    print(f"RELOCATED? {'YES' if relocated_a else 'NO'} (project A)")
    assert bootstrap_a["exitCode"] == 0
    assert relocated_a, "precondition unmet: A's bootstrap must actually relocate"

    # Right after A's own bootstrap: A resolves its own checkout via the
    # global default, `writtenFor` naming A itself -- no cross-project
    # warning. Compared through the envelope's top-level `sdk.root` (ALWAYS
    # posix, `SdkInfo.as_dict`), not `data.sdkPath` (raw/native on both
    # sides).
    current_a1 = envelope(
        run_tan("sdk", "current", "--format", "json", cwd=proj_a, env_extra=env_extra)
    )
    print(
        f"  A, right after A       sdk.root={current_a1['sdk']['root']!r} "
        f"tier={current_a1['data']['sourceTier']}"
    )
    assert current_a1["sdk"]["root"] == str(new_sdk_a).replace("\\", "/")
    assert current_a1["data"]["sourceTier"] == "globalDefault"
    assert "sdk.global-default-foreign-project" not in codes(current_a1)

    bootstrap_b = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk_b), cwd=proj_b, env_extra=env_extra,
        )
    )
    relocated_b = "bootstrap.workspace-relocated" in codes(bootstrap_b)
    print(f"RELOCATED? {'YES' if relocated_b else 'NO'} (project B)")
    assert bootstrap_b["exitCode"] == 0
    assert relocated_b, "precondition unmet: B's bootstrap must actually relocate"
    pointer = home / ".alp" / "sdk-default"
    print(f"  pointer now: {pointer.read_text(encoding='utf-8').strip()}")

    # A, queried again from the SAME directory: tan-cli#466's whole point is
    # that this STILL resolves A's own checkout, not B's -- the shared
    # `~/.alp/sdk-default` pointer now names B, but A's own registry entry
    # (`proj_a -> new_sdk_a`, written by A's own bootstrap above) is the
    # deepest key covering `proj_a` and answers first.
    current_a2 = envelope(
        run_tan("sdk", "current", "--format", "json", cwd=proj_a, env_extra=env_extra)
    )
    print(
        f"  A (the earlier one)    sdk.root={current_a2['sdk']['root']!r} "
        f"tier={current_a2['data']['sourceTier']}"
    )
    assert current_a2["sdk"]["root"] == str(new_sdk_a).replace("\\", "/"), (
        "DEFECT (tan-cli#466): project A stopped resolving its OWN SDK after "
        "an unrelated project B relocated its checkout"
    )
    assert current_a2["data"]["sourceTier"] == "globalDefault"
    assert "sdk.global-default-foreign-project" not in codes(current_a2), (
        "a registry entry written FOR this workspace must never be reported "
        "as a foreign global default"
    )

    # And the registry FILE itself carries both origins, each naming its own
    # relocated checkout -- the mechanism, not just the outcome.
    registry_doc = json.loads(
        (home / ".alp" / "sdk-defaults.json").read_text(encoding="utf-8")
    )
    assert registry_doc[str(proj_a).replace("\\", "/")]["sdkPath"] == str(
        new_sdk_a
    ).replace("\\", "/")
    assert registry_doc[str(proj_b).replace("\\", "/")]["sdkPath"] == str(
        new_sdk_b
    ).replace("\\", "/")


def test_a_relocating_bootstrap_updates_the_project_pin_it_resolved_through(tmp_path):
    """tan-cli#644: `bootstrap` used to leave a project's OWN `.alp/sdk-path`
    naming the vacated checkout after relocating it -- reachable any time
    `tan bootstrap --workspace <dir>` relocates a checkout inside a project
    that already has a working pin (written by an earlier `tan init`, per the
    documented `bootstrap` then `init` quickstart order), i.e. a project being
    re-bootstrapped rather than only a first run. Every later command that
    resolves the SDK through it (`build`, `sdk current`, ...) then read a pin
    the checkout no longer sat under.

    Deliberately DIFFERENT from the case pinned two tests above (a bootstrap
    run from the workspace PARENT with `--sdk-root`, no project pin in play,
    tier `sdkRootFlag`): here `--sdk-root` is NOT passed, so the SDK resolves
    through the project's own EXISTING `.alp/sdk-path` pin (tier
    `projectPin`) -- the narrow condition this fix actually rewrites, chosen
    over writing a NEW pin unconditionally (the idea tried and reverted at
    tan-cli#464, cited in that same test) for exactly the reason that revert
    gives: THIS cwd genuinely is a project, with a pin `tan init` already
    wrote and this very run already resolved through -- not an arbitrary
    workspace-parent directory that may not be a project at all.
    """
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    proj = tmp_path / "proj"
    (proj / ".alp").mkdir(parents=True)
    (proj / ".alp" / "sdk-path").write_text(
        json.dumps({"sdkPath": str(sdk), "updatedAt": "2026-01-01T00:00:00Z"}, indent=2) + "\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "new-home"

    env = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--workspace", str(workspace), cwd=proj,
        )
    )
    assert env["exitCode"] == 0
    assert env["sdk"]["sourceTier"] == "projectPin"
    new_sdk = workspace / sdk.name
    assert env["sdk"]["root"] == str(new_sdk).replace("\\", "/")
    assert "bootstrap.workspace-relocated" in codes(env)

    pin = json.loads((proj / ".alp" / "sdk-path").read_text(encoding="utf-8"))
    assert pin["sdkPath"] == str(new_sdk)

    # A FRESH process, same cwd -- exactly the customer's next command --
    # resolves the SAME checkout through the SAME tier, with no
    # `sdk.project-pin-unresolved` warning: the pin this run rewrote is what
    # a later `sdk current`/`build`/`inspect`/`validate` reads.
    later = envelope(run_tan("sdk", "current", "--format", "json", cwd=proj))
    assert later["data"]["sourceTier"] == "projectPin"
    assert later["sdk"]["root"] == str(new_sdk).replace("\\", "/")
    assert "sdk.project-pin-unresolved" not in codes(later)


def test_a_relocation_rollback_restores_the_project_pin_it_rewrote(tmp_path):
    """tan-cli#644 review: the project-pin rewrite proven above must roll back
    exactly like the global default pointer already does (tan-cli#284) when a
    LATER step -- here, venv creation -- fails after a successful relocation.
    Leaving the rewritten pin in place would name a checkout that just moved
    BACK to its original location -- the exact stale-pin defect this fix
    exists to close, just introduced by the rollback instead of by a
    completed run.
    """
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    proj = tmp_path / "proj"
    (proj / ".alp").mkdir(parents=True)
    original_pin = (
        json.dumps({"sdkPath": str(sdk), "updatedAt": "2026-01-01T00:00:00Z"}, indent=2) + "\n"
    )
    (proj / ".alp" / "sdk-path").write_text(original_pin, encoding="utf-8")
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    # Blocks `python -m venv`, the same deterministic, network-free failure
    # `test_a_relocation_is_rolled_back_when_a_later_step_fails` uses.
    (workspace / ".venv").write_text("not a directory", encoding="utf-8")

    env = envelope(
        run_tan(
            "bootstrap", "--format", "json", "--workspace", str(workspace), cwd=proj,
        )
    )
    assert env["exitCode"] != 0
    issue_codes = codes(env)
    assert "bootstrap.workspace-relocated" in issue_codes
    assert "bootstrap.workspace-relocation-rolled-back" in issue_codes
    # The checkout is back at its original location; the project pin must say
    # so too, byte-for-byte -- not merely "some path that resolves".
    assert sdk.exists()
    assert (proj / ".alp" / "sdk-path").read_text(encoding="utf-8") == original_pin


def test_a_relocation_rollback_restores_the_pin_of_a_project_nested_inside_the_checkout(
    tmp_path,
):
    """tan-cli#644 review: `test_a_relocation_rollback_restores_the_project_pin_it_rewrote`
    above places `proj` OUTSIDE the checkout, so `_rebase` never touches its
    `root` and cannot catch a restore-path bug that only a NESTED project
    triggers. Here `proj` lives INSIDE the checkout being relocated (`sdk /
    "myproj"`), so `_run` rebases `root` onto the NEW checkout path before
    `_relocate_project_pin` runs, and that rebased (post-relocation) path is
    what a naive implementation would record as `project_pin_root` for the
    later restore.

    That is wrong: `_undo_relocation` moves the checkout BACK to `old_root`
    FIRST, so by the time it calls `_restore_project_pin` the post-relocation
    path has already been vacated -- writing there raises ENOENT and the
    restore is reported as failed, exactly the stale/unresolvable pin
    tan-cli#644 exists to eliminate, just reintroduced by the rollback path
    this time. `project_pin_root` must record the PRE-relocation project root
    (mirroring how `old_project` is captured before the rebase), not the
    rebased one.

    `env_extra`'s explicit `HOME` (the `test_a_second_projects_relocation...`
    pattern above) is required here, not optional: `run_tan`'s default fake
    HOME is `cwd.parent`, which for a NESTED project sits INSIDE the checkout
    itself -- letting the checkout's own relocation drag the fake HOME (and
    the machine-global pointer under it) along for the ride, which is a
    second, unrelated confound this test does not exist to cover.

    `--project <proj>` with `cwd=tmp_path`, rather than `cwd=proj` directly,
    for the same reason: a subprocess's OWN current working directory sitting
    INSIDE the checkout being relocated makes the rename fail outright on
    Windows (a directory that is any process's cwd cannot be moved there),
    which is a Windows platform limitation this test does not exist to prove
    -- `--project` reaches the identical nested-pin codepath without pinning
    the subprocess's cwd under the checkout at all.
    """
    home = tmp_path / "shared-home"
    env_extra = {"HOME": str(home), "USERPROFILE": str(home)}
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    proj = sdk / "myproj"
    proj.mkdir()
    (proj / ".alp").mkdir()
    original_pin = (
        json.dumps({"sdkPath": str(sdk), "updatedAt": "2026-01-01T00:00:00Z"}, indent=2) + "\n"
    )
    (proj / ".alp" / "sdk-path").write_text(original_pin, encoding="utf-8")
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    # Blocks `python -m venv`, the same deterministic, network-free failure
    # `test_a_relocation_is_rolled_back_when_a_later_step_fails` uses.
    (workspace / ".venv").write_text("not a directory", encoding="utf-8")

    env = envelope(
        run_tan(
            "bootstrap", "--format", "json", "--project", str(proj),
            "--workspace", str(workspace), cwd=tmp_path,
            env_extra=env_extra,
        )
    )
    assert env["exitCode"] != 0
    issue_codes = codes(env)
    assert "bootstrap.workspace-relocated" in issue_codes
    assert "bootstrap.workspace-relocation-rolled-back" in issue_codes
    # The checkout (and the nested project inside it) is back at its original
    # location; the project pin must say so too, byte-for-byte -- not merely
    # "some path that resolves", and not silently left unrestored because the
    # write targeted a path the rollback had already vacated.
    assert sdk.exists()
    assert proj.exists()
    restored_pin = (proj / ".alp" / "sdk-path").read_text(encoding="utf-8")
    assert restored_pin == original_pin, (
        f"project pin was not restored byte-for-byte: {restored_pin!r}"
    )
    assert "the project's .alp/sdk-path pin could not be restored" not in "".join(
        i["message"] for i in env["issues"]
    )


def test_the_auto_relocation_target_refuses_when_it_already_holds_content(tmp_path):
    """tan-cli#302 non-negotiable: auto-relocating into
    `default_relocation_target`'s own `alp-workspace` choice is safe only into
    an EMPTY (or absent) directory -- silently writing into one that already
    holds something would be the exact "wrote into a directory without asking"
    hazard the parent guard exists to prevent, one level down. The realistic
    trigger is a previous attempt's partial venv, left behind by
    `rollback_relocation_after` on a retry (its own docstring: "left on disk...
    delete it by hand if you do not want it"); reproduced directly here rather
    than via a real failing venv."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    (sdk.parent / "unrelated.txt").write_text("x", encoding="utf-8")
    target = sdk.parent / "alp-workspace"
    (target / "leftover").mkdir(parents=True)

    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json",
        "--sdk-root", str(sdk), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode == 2
    assert codes(env) == ["bootstrap.workspace-guard"]
    message = env["issues"][0]["message"]
    assert "already exists" in message
    assert bootstrap_cmd._native(str(target)) in message
    assert "tan bootstrap --workspace <path>" in message
    # Nothing was moved: the checkout is exactly where it started, and the
    # pre-existing `alp-workspace/leftover` was not written into.
    assert sdk.exists()
    assert (target / "leftover").is_dir()
    assert not (target / sdk.name).exists()
    # tan-cli#284: the stale "re-run interactively" advice is gone -- this
    # port never prompts, on any run, TTY or not.
    assert "interactively" not in message


def test_print_env_agrees_with_dry_run_on_a_dirty_parent(tmp_path):
    """tan-cli#459: `--print-env` used to answer BEFORE the workspace-parent
    guard above ever ran, so on the exact dirty-parent host that guard
    relocates off of (the fixture above -- tan's own binary beside a freshly
    cloned alp-sdk, per the documented quickstart), it reported
    `data.workspaceDir`/`sdkRoot`/`zephyrBase` as the checkout's raw,
    unrelocated parent -- three exported paths a real `tan bootstrap` would
    never create -- at `ok: true`, `issues: []`: silent success for advice
    that cannot work. `--dry-run` over the IDENTICAL input already got this
    right, so this pins agreement between the two rather than a hardcoded
    path: a future change to `default_relocation_target`'s own choice must
    move both together or this test catches the divergence."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    (sdk.parent / "unrelated.txt").write_text("x", encoding="utf-8")
    target = sdk.parent / "alp-workspace"

    print_env = envelope(
        run_tan(
            "bootstrap", "--print-env", "--format", "json", "--sdk-root", str(sdk),
            cwd=sdk.parent,
        )
    )
    dry_run = envelope(
        run_tan(
            "bootstrap", "--dry-run", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
        )
    )

    assert print_env["data"]["workspaceDir"] == bootstrap_cmd._native(str(target))
    assert print_env["data"]["sdkRoot"] == bootstrap_cmd._native(str(target / sdk.name))
    assert print_env["data"]["zephyrBase"] == bootstrap_cmd._native(str(target / "zephyr"))
    assert print_env["data"]["workspaceDir"] == dry_run["data"]["workspaceDir"]
    assert print_env["data"]["sdkRoot"] == dry_run["data"]["sdkRoot"]
    assert print_env["data"]["zephyrBase"] == dry_run["data"]["zephyrBase"]
    assert "bootstrap.workspace-relocated" in codes(print_env)
    # `--print-env`'s own contract (the --workspace conflict refusal's own
    # wording): "prints what an already-resolved workspace exports and moves
    # nothing". Nothing on disk may move just because the reported paths did.
    assert sdk.exists()
    assert not target.exists()
    assert not (tmp_path / "fake-home" / ".alp" / "sdk-default").exists()


def test_print_env_agrees_with_dry_run_on_an_adopted_zephyr_base(tmp_path):
    """tan-cli#459 was only HALF fixed by projecting `--print-env` through the
    relocation guard: on the ADOPTION branch (an ambient `$ZEPHYR_BASE` this
    run would REUSE, `_select_workspace`'s own call) the guard never applies
    at all -- `guard_applies` is False exactly when adoption applies -- so
    nothing relocates, but `--print-env` still named the checkout's own,
    unrelocated `ws`, a workspace a real run never builds; `--dry-run` over
    the identical input already named the adopted `other` topdir."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])  # <tmp_path>/ws/alp-sdk
    other = tmp_path / "other"
    zephyr = other / "zephyr"
    zephyr.mkdir(parents=True)
    (zephyr / "VERSION").write_text(
        "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 1\nEXTRAVERSION =\n",
        encoding="utf-8",
    )
    (other / ".west").mkdir()
    (other / ".west" / "config").write_text(
        "[manifest]\npath = ../ws/alp-sdk\nfile = west.yml\n", encoding="utf-8"
    )

    print_env = envelope(
        run_tan(
            "bootstrap", "--print-env", "--format", "json", "--sdk-root", str(sdk),
            cwd=sdk.parent, env_extra={"ZEPHYR_BASE": str(zephyr)},
        )
    )
    dry_run = envelope(
        run_tan(
            "bootstrap", "--dry-run", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent, env_extra={"ZEPHYR_BASE": str(zephyr)},
        )
    )

    assert print_env["data"]["workspaceDir"] == bootstrap_cmd._native(str(other))
    assert print_env["data"]["workspaceDir"] == dry_run["data"]["workspaceDir"]
    assert print_env["data"]["zephyrBase"] == dry_run["data"]["zephyrBase"]
    assert print_env["data"]["sdkRoot"] == dry_run["data"]["sdkRoot"]
    # Adoption never moves the checkout -- only `--print-env`'s reported
    # `workspaceDir`/`zephyrBase` change; `sdkRoot` stays the checkout itself.
    assert print_env["data"]["sdkRoot"] == bootstrap_cmd._native(str(sdk))
    assert sdk.exists()
    assert not (tmp_path / "fake-home" / ".alp" / "sdk-default").exists()


# ---------------------------------------------------------------------------
# tan-cli#389 / tan-cli#390: the two ways bootstrap destroyed something the
# customer created. Both are gated end-to-end (a real `tan bootstrap`
# subprocess) rather than by unit call, because both defects lived in the
# COMPOSITION -- a guard that a flag short-circuited past, and a delete whose
# only saving fact was resolved after it had already run.
# ---------------------------------------------------------------------------


def _live_workspace(tmp_path: Path, *, zephyr_version: str = "4.4.1") -> tuple[Path, Path]:
    """A LIVE west workspace at `<tmp>/ws`, of the exact shape `west init -l
    alp-sdk` + `west update` leaves behind: `.west/config` naming `alp-sdk` as
    the manifest repo, and a `zephyr/` checkout carrying a VERSION file.

    `zephyr_version` picks which `_select_workspace` branch a `$ZEPHYR_BASE`
    pointed here takes: the SDK pin ("4.4.1") -> REUSE, anything else -> STALE.
    Both set `WorkspacePlan.adopted`, and both therefore repoint `paths.venv_dir`
    at THIS tree's `.venv` -- which is what makes them the same hazard.
    """
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    ws = sdk.parent
    (ws / ".west").mkdir()
    (ws / ".west" / "config").write_text(
        "[manifest]\npath = alp-sdk\nfile = west.yml\n", encoding="utf-8"
    )
    (ws / "zephyr").mkdir()
    major, minor, patch = zephyr_version.split(".")
    (ws / "zephyr" / "VERSION").write_text(
        f"VERSION_MAJOR = {major}\nVERSION_MINOR = {minor}\nPATCHLEVEL = {patch}\n"
        f"EXTRAVERSION =\n",
        encoding="utf-8",
    )
    return sdk, ws




def test_workspace_flag_still_relocates_when_no_workspace_depends_on_the_checkout(tmp_path):
    """Non-vacuity for the refusal above: `--workspace` is not being disabled,
    only stopped from orphaning a LIVE workspace. The same fixture minus the
    `.west/config` (an ordinary clone that no workspace points at) still moves,
    so a refusal that fired on everything would be caught here."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    newhome = tmp_path / "newhome"

    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json",
        "--sdk-root", str(sdk), "--workspace", str(newhome), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode == 0
    assert "bootstrap.workspace-relocated" in codes(env)
    assert (newhome / sdk.name / "scripts" / "alp_project.py").is_file()
    assert not sdk.exists()




def _venv_without_pip(root: Path) -> Path:
    """A REAL venv whose interpreter runs and whose `pip` is absent -- `uv
    venv`'s default shape, which is what tan-cli#390 was reported against,
    reproduced with the stdlib so the suite needs no uv. Hermetic: `--without-pip`
    neither downloads nor unpacks a wheel."""
    venv = root / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True, capture_output=True,
    )
    return venv








def test_find_enclosing_west_walks_ancestors_never_the_start_itself(tmp_path):
    """`west init -l` aborts the instant an ancestor `.west` turns up while
    walking UP from the topdir -- but the topdir's OWN `.west` is the ordinary
    "already initialised, reuse" case `west_phase` handles separately, so the
    walk must never flag that one."""
    root = tmp_path / "a" / "b" / "c"
    root.mkdir(parents=True)
    assert bootstrap_cmd.find_enclosing_west(root) is None

    (root / ".west").mkdir()
    assert bootstrap_cmd.find_enclosing_west(root) is None  # the start itself: not "enclosing"

    (tmp_path / "a" / ".west").mkdir()
    assert bootstrap_cmd.find_enclosing_west(root) == tmp_path / "a"


def test_an_enclosing_west_workspace_refuses_before_any_mutation(tmp_path):
    """tan-cli#284: an unrelated west workspace ABOVE the intended topdir makes
    `west init -l` abort with "already initialized in <dir>, aborting" --
    knowable up front, so it must refuse before touching anything, exactly
    like the dirty-parent guard just above.

    NOT `--no-west`: this scenario is only real on a run where `west init -l`
    would actually execute -- see the over-refusal regression test below for
    the case where it would not."""
    sdk = make_sdk(tmp_path)
    (tmp_path / ".west").mkdir()  # an ancestor of sdk.parent, the intended topdir
    before = sorted(p.name for p in sdk.parent.iterdir())

    proc = run_tan(
        "bootstrap", "--no-pip", "--format", "json",
        "--sdk-root", str(sdk), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode == 2
    assert codes(env) == ["bootstrap.enclosing-west-workspace"]
    message = env["issues"][0]["message"]
    assert "already initialized in" in message
    assert str(tmp_path) in message
    # West's own remedy ("remove this directory") is never repeated: that
    # workspace may still be in use.
    assert "do not remove it" in message
    assert sorted(p.name for p in sdk.parent.iterdir()) == before
    pointer = tmp_path / "fake-home" / ".alp" / "sdk-default"
    assert not pointer.exists()


def test_an_enclosing_west_workspace_refuses_even_under_an_explicit_workspace(tmp_path):
    """The explicit `--workspace <path>` branch never consults
    `default_relocation_target` (an override answers the dirty-parent question
    outright) -- tan-cli#284 was filed against exactly this path, where
    nothing checked for an ENCLOSING `.west` before relocating."""
    sdk = make_sdk(tmp_path)
    outer = tmp_path / "outer"
    (outer / ".west").mkdir(parents=True)
    target = outer / "inner" / "ws"

    proc = run_tan(
        "bootstrap", "--no-pip", "--format", "json",
        "--sdk-root", str(sdk), "--workspace", str(target), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode == 2
    assert codes(env) == ["bootstrap.enclosing-west-workspace"]
    assert "already initialized in" in env["issues"][0]["message"]
    assert sdk.exists()
    assert not target.exists()
    pointer = tmp_path / "fake-home" / ".alp" / "sdk-default"
    assert not pointer.exists()


def test_the_enclosing_west_guard_does_not_fire_when_west_init_will_not_run(tmp_path):
    """tan-cli#284 over-refusal, now fixed: the guard predicts what a REAL
    `west init -l` would hit, so it must not fire on a run where `west init
    -l` never executes -- `--no-west` skips it outright."""
    sdk = make_sdk(tmp_path)
    (tmp_path / ".west").mkdir()  # an ancestor of sdk.parent (the topdir)

    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json",
        "--sdk-root", str(sdk), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert "bootstrap.enclosing-west-workspace" not in codes(env)


def test_the_enclosing_west_guard_does_not_fire_when_the_topdir_reuses_its_own_west(tmp_path):
    """tan-cli#284 over-refusal, now fixed: a topdir that already holds its
    OWN `.west` takes `west_phase`'s "already initialised" branch, which runs
    only `west update` -- never `west init -l` -- so an ancestor `.west`
    further up (which only `west init -l`'s topdir-upward walk would ever
    reach) must not refuse it either.

    `--dry-run`, not `--no-west`: this keeps the rest of the run hermetic
    (nothing spawned) while still exercising the guard exactly as a real run
    would reach it -- the guard itself does not consult `dry_run`.

    The topdir's own `.west` carries a `config` (not just a bare directory):
    since tan-cli#302, a bare `.west` with no `config` is NOT `dot_west_is_
    workspace` to the parent guard (`default_relocation_target`), so it reads
    as ordinary dirty content and the guard would auto-relocate the checkout
    one directory deeper -- a different scenario from the one under test
    here, which is specifically the reuse path leaving `intended_topdir`
    unmoved."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    (tmp_path / ".west").mkdir()  # an ancestor of sdk.parent (the topdir)
    (sdk.parent / ".west").mkdir()  # the topdir's OWN -- triggers reuse, not init
    (sdk.parent / ".west" / "config").write_text(
        "[manifest]\npath = alp-sdk\n", encoding="utf-8"
    )

    proc = run_tan(
        "bootstrap", "--dry-run", "--format", "json",
        "--sdk-root", str(sdk), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert "bootstrap.enclosing-west-workspace" not in codes(env)


# ---------------------------------------------------------------------------
# tan-cli#469 -- `workspace-orphan-refused` never names a stringified `None`
# or advises dropping a `--workspace` the invocation never carried.
# ---------------------------------------------------------------------------


def test_workspace_orphan_refusal_names_the_real_destination_when_workspace_was_given(
    tmp_path,
):
    """`target is not None` only when `--workspace` was passed explicitly --
    currently the ONLY way this refusal is reached at all (`default_
    relocation_target` returns `None` the instant the parent already holds a
    `.west/config`, tan-cli#389/#390). The destination is real here, so
    "drop --workspace" is applicable advice and must stay."""
    repo_root = tmp_path / "ws" / "alp-sdk"
    source_topdir = tmp_path / "ws"
    target = tmp_path / "newhome"

    message = workspace_orphan_refusal(repo_root, source_topdir, target)

    assert "None" not in message
    assert str(target) in message
    assert "moving it to" in message
    assert "drop --workspace" in message


def test_workspace_orphan_refusal_has_no_destination_and_no_unreachable_advice(tmp_path):
    """tan-cli#469: `target is None` is the shape of call the bug was filed
    against -- no `--workspace` was passed, so there is no real destination.
    The message must say that instead of interpolating a stringified `None`,
    and must not send the reader to drop a flag they never carried."""
    repo_root = tmp_path / "ws" / "alp-sdk"
    source_topdir = tmp_path / "ws"

    message = workspace_orphan_refusal(repo_root, source_topdir, None)

    assert "None" not in message
    assert str(repo_root) in message
    assert str(source_topdir) in message
    assert "cannot be relocated" in message
    assert "This workspace is already bootstrapped" in message
    assert "re-run without --sdk-root pointing into it" in message
    assert "clone a SECOND alp-sdk checkout elsewhere" in message
    assert "drop --workspace" not in message


def test_a_relocation_is_rolled_back_when_a_later_step_fails(tmp_path):
    """tan-cli#284: relocating the checkout and repointing the global default
    SDK are never rolled back by `west`/venv creation failing on their own --
    a fallible step AFTER a successful relocation must undo both, not leave
    the checkout moved and the default SDK pointed at a workspace that was
    never finished."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    # Blocks `python -m venv` from creating the venv directory: a real,
    # deterministic, network-free failure of the first fallible step after
    # the relocation.
    (workspace / ".venv").write_text("not a directory", encoding="utf-8")

    proc = run_tan(
        "bootstrap", "--format", "json",
        "--sdk-root", str(sdk), "--workspace", str(workspace), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode != 0
    issue_codes = codes(env)
    assert "bootstrap.workspace-relocated" in issue_codes
    assert "bootstrap.workspace-relocation-rolled-back" in issue_codes
    assert "bootstrap.failed" in issue_codes
    # The checkout is back where it started, not left under `workspace`.
    assert sdk.exists()
    assert not (workspace / sdk.name).exists()
    # The global default SDK pointer is restored to "absent" (nothing existed
    # before this run).
    pointer = tmp_path / "fake-home" / ".alp" / "sdk-default"
    assert not pointer.exists()
    # tan-cli#466: the origin-keyed registry sibling this same relocation
    # would have written is restored to "absent" too -- `_undo_relocation`'s
    # `previous_registry` branch, otherwise untested (review round, #904).
    registry = tmp_path / "fake-home" / ".alp" / "sdk-defaults.json"
    assert not registry.exists()
    # tan-cli#284 majors: nothing reported in the envelope may still name the
    # vacated `elsewhere` location once the rollback succeeded -- `data.*`
    # paths and `project.root` must agree with where the checkout actually
    # ended up, not a stale value from mid-run or a re-derived guess.
    assert "elsewhere" not in (env["project"]["root"] or "")
    assert "elsewhere" not in env["data"]["workspaceDir"]
    assert "elsewhere" not in env["data"]["venvDir"]
    assert "elsewhere" not in env["data"]["sdkRoot"]
    assert env["data"]["sdkRoot"] == bootstrap_cmd._native(str(sdk))
    assert env["data"]["workspaceDir"] == bootstrap_cmd._native(str(sdk.parent))
    # The rollback message itself must not overclaim: it moved the checkout
    # back, but anything the failed step already created under `elsewhere`
    # (here, the blocking `.venv` file) is left on disk, named honestly
    # rather than asserted away.
    rollback_message = next(
        i["message"] for i in env["issues"] if i["code"] == "bootstrap.workspace-relocation-rolled-back"
    )
    assert "nothing from this run is in effect" not in rollback_message
    assert "moved it back" in rollback_message


def test_a_blocked_rollback_reports_the_checkout_as_still_relocated(tmp_path):
    """tan-cli#284 blocker: `_undo_relocation` used to discard
    `relocate_checkout`'s own `(new_root, error)` return, so a move-back that
    REFUSES -- because the vacated original path was recreated in the
    meantime -- was invisible to the caller, which then asserted the checkout
    was moved back regardless. Reproduced directly against `_undo_relocation`,
    the same way the review that found this proved it: recreate the vacated
    path before the rollback runs, and check the return value, not a printed
    claim."""
    old_root = tmp_path / "ws" / "alp-sdk"
    old_root.parent.mkdir(parents=True)
    moved_to = tmp_path / "elsewhere" / "alp-sdk"
    moved_to.parent.mkdir(parents=True)
    moved_to.mkdir()
    (moved_to / "marker").write_text("x", encoding="utf-8")
    # The vacated original path was recreated (e.g. by a retry) before the
    # rollback ran.
    old_root.mkdir()

    result = bootstrap_cmd._undo_relocation(str(old_root), moved_to, None)

    assert result.moved_back is False
    assert result.detail is not None
    assert "already exists" in result.detail
    # Nothing was moved: the checkout is still exactly where the failed run
    # left it, not half-migrated or silently vanished.
    assert moved_to.is_dir()
    assert (moved_to / "marker").exists()


def test_a_successful_move_back_with_a_failed_pointer_restore_is_not_reported_as_still_relocated(
    tmp_path, monkeypatch
):
    """tan-cli#284 review BLOCKER: `_undo_relocation` used to return a plain
    `str | None`, so "the move-back failed" and "the move-back SUCCEEDED but
    the pointer restore afterwards failed" were the same non-`None` shape --
    the caller's `else` arm collapsed them and told a customer whose checkout
    HAD moved back to "move it back by hand", naming a directory that no
    longer existed. Measured (before the fix): a plain `str`, `old_root.is_dir()
    == True`, `moved_to.exists() == False` -- exactly this permutation, which
    the review named as having no test. Forces the pointer write to fail (not
    the move) by pointing `_home_alp_dir` at a path whose PARENT does not
    exist -- cross-platform, unlike a chmod-based permission-denied repro."""
    old_root = tmp_path / "ws" / "alp-sdk"
    old_root.parent.mkdir(parents=True)
    moved_to = tmp_path / "elsewhere" / "alp-sdk"
    moved_to.parent.mkdir(parents=True)
    moved_to.mkdir()
    (moved_to / "marker").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        bootstrap_cmd, "_home_alp_dir", lambda: tmp_path / "no-such-parent" / "deep"
    )

    result = bootstrap_cmd._undo_relocation(str(old_root), moved_to, b"previous-pointer-bytes")

    # The checkout DID move back -- callers must trust `moved_back`, never
    # infer "still relocated" from `detail` being non-`None`.
    assert result.moved_back is True
    assert result.detail is not None
    assert "pointer" in result.detail
    assert old_root.is_dir()
    assert (old_root / "marker").exists()
    assert not moved_to.exists()


def test_a_registry_rollback_restores_the_previous_bytes_exactly(tmp_path, monkeypatch):
    """tan-cli#904 third round, nit: the registry rollback branch of
    `_undo_relocation` now writes via `atomic_write_bytes`
    (`tan.core.atomic_write`), matching the forward write's own
    `atomic_write_text` -- same file, same N-project blast radius, so both
    must be crash-safe, not just the forward one.

    `atomic_write_bytes`, not `atomic_write_text`, because the snapshot being
    restored is a raw byte capture (`_read_global_sdk_registry_bytes`) that
    `parse_registry` never required to be valid UTF-8 -- deliberately
    non-UTF-8 here (an invalid continuation byte) to prove the rollback can
    restore content `atomic_write_text` would raise `UnicodeDecodeError`
    reconstructing a `str` from. Restored byte-for-byte, and via the temp-
    sibling-then-`os.replace` shape, not a bare truncate-then-write."""
    old_root = tmp_path / "ws" / "alp-sdk"
    old_root.parent.mkdir(parents=True)
    moved_to = tmp_path / "elsewhere" / "alp-sdk"
    moved_to.parent.mkdir(parents=True)
    moved_to.mkdir()
    home_alp = tmp_path / "fake-home" / ".alp"
    # Realistic precondition: `~/.alp` only ever has something to roll BACK
    # to because an earlier write in the SAME run (`_write_global_sdk_
    # registry`, which itself `mkdir(parents=True)`s this directory) already
    # created it -- this rollback branch, unlike the forward write, does not
    # create the directory itself.
    home_alp.mkdir(parents=True)
    monkeypatch.setattr(bootstrap_cmd, "_home_alp_dir", lambda: home_alp)

    non_utf8_registry = b'{"/proj": {"sdkPath": "/sdk"}}\xff\xfe'
    with pytest.raises(UnicodeDecodeError):
        non_utf8_registry.decode("utf-8")  # the repro's own precondition

    result = bootstrap_cmd._undo_relocation(
        str(old_root), moved_to, None, previous_registry=non_utf8_registry
    )

    assert result.moved_back is True
    assert result.detail is None, f"the registry restore itself must not fail: {result.detail}"
    registry_file = registry_path(home_alp)
    assert registry_file.read_bytes() == non_utf8_registry
    # No leftover `.tan-tmp` sibling -- the atomic write's temp file was
    # renamed into place, not left behind.
    assert list(home_alp.glob("*.tan-tmp")) == []


def test_a_yocto_only_project_is_refused_off_linux_and_a_mixed_one_only_warns(tmp_path):
    """Refusal is deliberately narrow. A mixed board still bootstraps -- nothing
    bootstrap does is Yocto-specific and its Zephyr cores need exactly this."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    yocto = sdk / "examples" / "yocto-only"
    yocto.mkdir(parents=True)
    (yocto / "board.yaml").write_text(
        "schema_version: 2\nsom:\n  sku: E1M-X-V2N101\ncores:\n  a55_cluster: {}\n",
        encoding="utf-8",
    )
    mixed = sdk / "examples" / "mixed"
    mixed.mkdir(parents=True)
    (mixed / "board.yaml").write_text(
        "schema_version: 2\nsom:\n  sku: E1M-X-V2N101\ncores:\n"
        "  a55_cluster: {}\n  m33_sm: {}\n",
        encoding="utf-8",
    )

    def issues_for(project):
        return envelope(
            run_tan(
                "bootstrap", "--no-west", "--no-pip", "--format", "json",
                "--sdk-root", str(sdk), "--project", str(project), cwd=sdk.parent,
            )
        )

    if sys.platform.startswith("linux"):
        assert issues_for(yocto)["exitCode"] == 0
        return
    refused = issues_for(yocto)
    assert refused["exitCode"] == 2
    assert codes(refused) == ["bootstrap.yocto-host"]
    assert refused["issues"][0]["severity"] == "error"
    # The project is the RESOLVED one, not null: the verdict is DERIVED from
    # that project's board.yaml, so reporting null would say "every core here
    # targets Yocto" with no way to say which project.
    assert refused["project"]["root"].endswith("yocto-only")

    warned = issues_for(mixed)
    yocto_issues = [i for i in warned["issues"] if i["code"] == "bootstrap.yocto-host"]
    # I-73: ONE spelling at TWO severities. Promoting this would refuse a board
    # that can bootstrap its Zephyr cores; the frozen-code gate checks spelling,
    # not severity, so nothing else catches a collapse.
    assert len(yocto_issues) == 1 and yocto_issues[0]["severity"] == "warning"


def test_the_yocto_host_refusal_fires_before_the_checkout_relocates(tmp_path):
    """tan-cli#284 review MAJOR (bootstrap_cmd.py:1906, before the fix): this
    refusal used to fire AFTER `--workspace` already moved the checkout and
    repointed the global default SDK, and routed through `_refusal`'s
    fresh single-issue list, so the recorded `bootstrap.workspace-relocated`
    warning was silently dropped -- a JSON consumer got no record that a
    customer's checkout had just been relocated. `read_board_runtimes`/
    `yocto_gate` are pure reads of `board_path`/`sdk_root`, knowable before
    any write, exactly like the enclosing-`.west` guard already checked
    first -- so this must refuse BEFORE the move, leaving nothing on disk.
    Skipped on Linux, where this refusal never fires at all."""
    if sys.platform.startswith("linux"):
        pytest.skip("yocto-host never refuses on Linux")
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    yocto = sdk / "examples" / "yocto-only"
    yocto.mkdir(parents=True)
    (yocto / "board.yaml").write_text(
        "schema_version: 2\nsom:\n  sku: E1M-X-V2N101\ncores:\n  a55_cluster: {}\n",
        encoding="utf-8",
    )
    target = tmp_path / "elsewhere"

    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json",
        "--sdk-root", str(sdk), "--project", str(yocto), "--workspace", str(target),
        cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode == 2
    assert codes(env) == ["bootstrap.yocto-host"]
    # Refused BEFORE the checkout moved or the global default SDK was
    # repointed (tan-cli#284's stated contract) -- nothing rolled back after
    # the fact, because nothing happened yet.
    assert sdk.exists()
    assert not target.exists()
    pointer = tmp_path / "fake-home" / ".alp" / "sdk-default"
    assert not pointer.exists()


def test_the_prerequisites_refusal_fires_before_the_checkout_relocates(tmp_path):
    """tan-cli#284 review MAJOR (bootstrap_cmd.py:1927, before the fix): a
    missing tool refused AFTER `--workspace` already moved the checkout and
    repointed the global default SDK, with no rollback -- PATH tool presence
    is as static as the enclosing-`.west` fact the guard above already
    checks first, so this must refuse before any write too."""
    sdk = make_sdk(tmp_path, tools=["tan-no-such-tool-xyz"])
    target = tmp_path / "elsewhere"

    proc = run_tan(
        "bootstrap", "--format", "json",
        "--sdk-root", str(sdk), "--workspace", str(target), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode == 1
    assert codes(env)[-1] == "bootstrap.prerequisites-missing"
    assert "bootstrap.workspace-relocated" not in codes(env)
    assert sdk.exists()
    assert not target.exists()
    pointer = tmp_path / "fake-home" / ".alp" / "sdk-default"
    assert not pointer.exists()


# ---------------------------------------------------------------------------
# Hermetic execution: `--dry-run`
# ---------------------------------------------------------------------------


def test_a_dry_run_writes_nothing_and_reports_every_step_it_would_have_run(tmp_path):
    """The whole reason the install path is testable at all. If this ever leaks a
    `.venv` into the fixture, every other test in this file becomes a machine
    mutation."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    before = sorted(p.name for p in sdk.parent.iterdir())

    env = envelope(
        run_tan(
            "bootstrap", "--dry-run", "--format", "json", "--sdk-root", str(sdk),
            cwd=sdk.parent,
        )
    )
    assert env["exitCode"] == 0
    assert sorted(p.name for p in sdk.parent.iterdir()) == before == ["alp-sdk"]

    planned = env["data"]["plannedCommands"]
    # Order IS the contract: venv, then pip-bootstrap, then west, then the pip
    # phase. Both bootstrap scripts are the oracle for that order.
    assert "-m venv" in planned[0]
    assert planned[1].endswith("-m pip install --upgrade -q pip wheel")
    assert "pip install --upgrade -q west>=0.14.0" in planned[2]
    assert planned[3].endswith(f"init -l {sdk}")
    assert planned[4].endswith("update --narrow -o=--depth=1")
    assert planned[5].endswith("zephyr-export")
    assert planned[-2].endswith("-m pip install -q jsonschema imgtool")
    assert planned[-1].endswith(f"-m pip install -q -e {sdk}")


def test_plannedcommands_appears_only_under_dry_run(tmp_path):
    """A normal run keeps the oracle's exact `data` key set; the key appears only
    with the flag that produces it."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    normal = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
        )
    )
    assert "plannedCommands" not in normal["data"]


def test_a_dry_run_moves_nothing_and_never_writes_the_global_default_pointer(tmp_path):
    """tan-cli#323 (release blocker): the dirty-parent auto-relocation
    (tan-cli#302) used to read `--dry-run` as decoration -- it moved the
    checkout with `os.rename` and repointed `~/.alp/sdk-default` exactly as a
    real run does, then reported the move in the PAST tense, so a preview run
    looked identical to one that had actually happened. Same fixture as
    `test_the_workspace_parent_guard_relocates_into_alp_workspace_
    automatically` (an `unrelated.txt` beside the checkout, so the parent
    guard actually fires and a relocation is actually planned) with
    `--dry-run` added: the checkout must stay exactly where it started,
    `alp-workspace/` must never be created on disk, and the pointer file must
    never be written -- a flag whose entire purpose is "show me, don't do it"
    must not do it.
    """
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    (sdk.parent / "unrelated.txt").write_text("x", encoding="utf-8")
    target = sdk.parent / "alp-workspace"
    new_sdk = target / sdk.name

    env = envelope(
        run_tan(
            "bootstrap", "--dry-run", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
        )
    )
    assert env["exitCode"] == 0
    codes_seen = codes(env)
    assert "bootstrap.workspace-relocated" in codes_seen
    message = next(
        i["message"] for i in env["issues"] if i["code"] == "bootstrap.workspace-relocated"
    )
    # Conditional tense: the relocation this describes has NOT happened yet.
    assert "would move" in message
    assert "would set" in message
    assert "moved the alp-sdk" not in message

    # Nothing on disk moved: the source is untouched, the planned destination
    # was never created, and the pre-existing sibling is undisturbed.
    assert sdk.exists()
    assert (sdk / "scripts" / "alp_project.py").is_file()
    assert not target.exists()
    assert sorted(p.name for p in sdk.parent.iterdir()) == ["alp-sdk", "unrelated.txt"]

    # The global default SDK pointer was never written.
    pointer = tmp_path / "fake-home" / ".alp" / "sdk-default"
    assert not pointer.exists()

    # `data.sdkRoot`/`data.workspaceDir` still report the PLANNED destination
    # (tan-cli#323's own requirement) -- a preview that reports nothing useful
    # is not a fix, only a quieter version of the bug.
    assert env["data"]["sdkRoot"] == bootstrap_cmd._native(str(new_sdk))
    assert env["data"]["workspaceDir"] == bootstrap_cmd._native(str(target))


def test_doctor_and_bootstrap_resolve_the_same_root_on_the_quickstart_layout(tmp_path):
    """tan-cli#322: on the documented quickstart layout -- `tan.exe` and a
    freshly cloned `alp-sdk/` side by side, no `--sdk-root` -- `doctor` used
    to resolve the checkout (`tier: discovery`, via `resolve_sdk_root_ladder`'s
    fallback to the wide positional walk, which checks the CHILD `<cwd>/alp-
    sdk`) while `bootstrap` called the narrower `resolve_sdk_tiered` directly,
    which has no candidate for a child at all -- so it refused with
    `sdk-root-unresolved` and told the user to clone a checkout sitting right
    there. `make_sdk`'s own layout (`root/ws/alp-sdk`, with `root/ws` -- the
    cwd here -- holding nothing else) already IS that layout, so no extra
    fixture setup is needed to reproduce it. Both commands now route through
    `resolve_sdk_root_ladder`, so they must resolve identically."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])

    doctor_env = envelope(run_tan("doctor", "--format", "json", cwd=sdk.parent))
    assert doctor_env["sdk"]["sourceTier"] == "discovery"

    bootstrap_env = envelope(
        run_tan(
            "bootstrap", "--dry-run", "--no-west", "--no-pip", "--format", "json",
            cwd=sdk.parent,
        )
    )
    assert bootstrap_env["exitCode"] == 0
    assert "bootstrap.sdk-root-unresolved" not in codes(bootstrap_env)
    assert bootstrap_env["sdk"]["sourceTier"] == "discovery"
    # The load-bearing assertion: the SAME checkout, reported identically by
    # both commands from the identical cwd.
    assert bootstrap_env["sdk"]["root"] == doctor_env["sdk"]["root"]
    assert bootstrap_env["sdk"]["root"] == str(sdk).replace("\\", "/")


# ---------------------------------------------------------------------------
# Hostile inputs. None may produce a traceback or an empty stdout.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "epoch", ["1700000000000", "-99999999999", "not-a-number", "253402300799"]
)
def test_an_out_of_range_source_date_epoch_still_emits_one_envelope(epoch, tmp_path):
    """The most recent Critical in this port was a DOUBLE FAULT: a timestamp
    helper that throws, called from the exception guard's own recovery path,
    triggered by `SOURCE_DATE_EPOCH` in MILLISECONDS. bootstrap renders no
    timestamp in its envelope, and its one caller of `sdk_pointer_json` (which
    does) is wrapped -- this is what keeps that true."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json", "--sdk-root", str(sdk),
        cwd=sdk.parent, env_extra={"SOURCE_DATE_EPOCH": epoch},
    )
    assert envelope(proc)["command"] == "bootstrap"
    assert proc.returncode == 0


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("a YAML list", "- a\n- b\n"),
        ("a scalar cores block", "som:\n  sku: X\ncores: nope\n"),
        ("nothing at all", ""),
        ("a tab-indented mess", "som:\n\tsku: X\n"),
    ],
)
def test_a_wrong_shaped_board_yaml_proceeds_rather_than_crashing(name, body, tmp_path):
    """Unresolvable means PROCEED. `yocto_gate`'s own rule: erring toward running
    is harmless (bootstrap is idempotent), erring toward refusing bricks the
    command."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    project = sdk / "examples" / "p"
    project.mkdir(parents=True)
    (project / "board.yaml").write_text(body, encoding="utf-8")
    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json", "--sdk-root", str(sdk),
        "--project", str(project), cwd=sdk.parent,
    )
    assert envelope(proc)["exitCode"] == 0, name


def test_a_non_utf8_board_yaml_is_unresolvable_not_half_read(tmp_path):
    """board.yaml is a DECISION input, so it is read strictly. Read with
    `errors="replace"` a non-decodable file's `cores:` block still parses, and a
    Yocto-looking core id then REFUSES the run over a file nothing could read --
    a false refusal the oracle does not make."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    project = sdk / "examples" / "p"
    project.mkdir(parents=True)
    (project / "board.yaml").write_bytes(
        b"som:\n  sku: \xff\xfe\ncores:\n  a55_cluster: {}\n"
    )
    assert _read_board_slice(str(project / "board.yaml")) == (None, None, None)

    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json", "--sdk-root", str(sdk),
        "--project", str(project), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert env["exitCode"] == 0
    assert "bootstrap.yocto-host" not in codes(env)


@pytest.mark.parametrize(
    "layout",
    ["directory", "garbage", "unreadable-bytes"],
)
def test_a_broken_som_preset_never_fails_the_run(layout, tmp_path):
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    modules = sdk / "metadata" / "e1m_modules"
    modules.mkdir(parents=True)
    preset = modules / "E1M-X1.yaml"
    if layout == "directory":
        preset.mkdir()
    elif layout == "garbage":
        preset.write_text("::: not yaml [\n", encoding="utf-8")
    else:
        preset.write_bytes(b"schema_version: 1\nsku: \xff\n")
    project = sdk / "examples" / "p"
    project.mkdir(parents=True)
    (project / "board.yaml").write_text(
        "som:\n  sku: E1M-X1\ncores:\n  m33_sm: {}\n", encoding="utf-8"
    )
    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json", "--sdk-root", str(sdk),
        "--project", str(project), cwd=sdk.parent,
    )
    assert envelope(proc)["exitCode"] == 0


@pytest.mark.parametrize("shape", ["directory", "garbage", "non-utf8"])
def test_an_unusable_west_yml_falls_back_to_the_manifest_pin(shape, tmp_path):
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    if shape == "directory":
        (sdk / "west.yml").mkdir()
    elif shape == "garbage":
        (sdk / "west.yml").write_text("\x00\x01 not: [yaml\n", encoding="utf-8")
    else:
        (sdk / "west.yml").write_bytes(b"manifest:\n  projects:\n  - name: \xff\n")
    env = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
        )
    )
    assert env["data"]["zephyrPin"] == "4.4.1"


@pytest.mark.parametrize("shape", ["file", "missing", "python-cmake-is-a-directory"])
def test_a_broken_zephyr_base_never_fails_the_run(shape, tmp_path):
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    base = tmp_path / "zb"
    if shape == "file":
        base.write_text("not a directory", encoding="utf-8")
    elif shape == "python-cmake-is-a-directory":
        (base / "cmake" / "modules" / "python.cmake").mkdir(parents=True)
        (base / "VERSION").write_text("VERSION_MAJOR = 4\nVERSION_MINOR = 4\n", encoding="utf-8")
    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json", "--sdk-root", str(sdk),
        cwd=sdk.parent, env_extra={"ZEPHYR_BASE": str(base)},
    )
    assert envelope(proc)["exitCode"] == 0


def test_an_sdk_root_that_is_not_a_checkout_resolves_to_nothing(tmp_path):
    """I-31: `--sdk-root` is TERMINAL. A typo must surface as "unresolved", never
    fall through to a lower tier and silently report a DIFFERENT SDK."""
    make_sdk(tmp_path)  # a real one, as a sibling, to prove it is not adopted
    decoy = tmp_path / "not-a-checkout"
    decoy.mkdir()
    proc = run_tan("bootstrap", "--format", "json", "--sdk-root", str(decoy), cwd=tmp_path / "ws")
    assert proc.returncode == 2
    assert codes(envelope(proc)) == ["bootstrap.sdk-root-unresolved"]


def test_a_bad_format_value_is_a_usage_error_not_a_crash(tmp_path):
    sdk = make_sdk(tmp_path)
    proc = run_tan("bootstrap", "--format", "yaml", "--sdk-root", str(sdk), cwd=sdk.parent)
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr


# ---------------------------------------------------------------------------
# Pure decisions
# ---------------------------------------------------------------------------


def test_the_fallback_constants_match_the_real_manifest_field_for_field():
    """The fallback is what a customer on a RELEASED SDK actually gets, and
    `check_bootstrap_manifest.py` does not scan this repo -- so nothing but this
    holds the two in step.

    `manual_install_posix` was exempt here until tan-cli#585: the vendored
    fixture was stale, its first note ended on `tan sdk switch` (refused by
    this build), and transcribing it verbatim failed
    `test_sdk_onboarding_dead_end.py`. Re-vendoring removed the conflict at
    its source, so the exemption is gone and EVERY field is compared --
    `from_manifest` excepted, which is the parse-vs-fallback flag itself and
    differs by construction.

    `install` carried a SECOND, narrower exemption from tan-cli#760's second
    half until tan-cli#846: `REAL_MANIFEST` tracks `parity.yml`'s
    `PINNED_SDK_TAG`, and that pin sat behind alp-sdk#1471 (landed on `dev`
    @ `7a419865`), so the fixture still declared `install.linux` in the
    pre-alp-sdk#1464 FLAT shape (normalised to `{"apt": {...}}`, no `dnf`
    key -- see `normalize_linux_install`) while `_fallback_install_commands`
    was deliberately re-pinned AHEAD of that gap so a customer with no
    manifest at all still got a working `dnf` remedy on a Fedora/Rocky host.
    That exemption existed with a self-cancelling assertion attached, and
    tan-cli#846's bump to `94378a056549c7377d714a7f2b68878aca8fea01` fired
    it: the pin has caught up, the re-vendored fixture carries #1471's `dnf`
    sub-map, and the comparison is back to blanket field-for-field with
    `from_manifest` the only exemption left.
    """
    manifest = parse_bootstrap_manifest(REAL_MANIFEST)
    fallback = fallback_facts(manifest.python_min_version)
    for field in vars(manifest):
        if field == "from_manifest":
            continue
        assert getattr(fallback, field) == getattr(manifest, field), field

    # Named explicitly on top of the loop above: `install` is the one field
    # that has been exempted before, and a nested dict compares equal on the
    # loop's single `==` without saying WHICH sub-map drifted.
    assert fallback.install[MACOS] == manifest.install[MACOS]
    assert fallback.install[WINDOWS] == manifest.install[WINDOWS]
    assert fallback.install[LINUX][LINUX_PM_APT] == manifest.install[LINUX][LINUX_PM_APT]
    assert fallback.install[LINUX][LINUX_PM_DNF] == manifest.install[LINUX][LINUX_PM_DNF]


def test_no_instruction_in_the_vendored_manifest_names_a_refused_subcommand():
    """tan-cli#585 acceptance 3: the fixture cannot RE-acquire guidance for a
    subcommand this build refuses.

    Scanned over the whole file, not just the note that was wrong: `switch`
    was the one a gate happened to catch, and a future re-vendor could just as
    easily bring back `tan sdk install`. Checked against
    `sdk_cmd.NOT_PORTED_SDK_SUBCOMMANDS` rather than a literal, so a
    subcommand ADDED to that frozenset later is covered here the day it lands.
    """
    from tan.commands import sdk_cmd

    assert sdk_cmd.NOT_PORTED_SDK_SUBCOMMANDS  # else this asserts nothing

    # Both spellings: `alp` is the retired binary name, and a re-vendor could
    # bring back either form of the same dead instruction.
    refused = [
        f"{binary} sdk {sub}"
        for sub in sorted(sdk_cmd.NOT_PORTED_SDK_SUBCOMMANDS)
        for binary in ("tan", "alp")
    ]

    for phrase in refused:
        assert phrase not in REAL_MANIFEST, phrase

    # And the shipped fallback, transcribed from that same file, stays clean --
    # driven off the SAME derived list, so this half cannot fall behind the
    # frozenset while the half above tracks it.
    facts = fallback_facts((3, 10))
    for note in (*facts.manual_install_posix, *facts.manual_install_windows):
        for phrase in refused:
            assert phrase not in note, phrase


def test_the_reuse_test_compares_the_full_patch_level(tmp_path):
    """The oracle scripts truncate to MAJOR.MINOR, which is what let a `v4.4.0`
    tree satisfy a `v4.4.1` pin -- the build went green against the previous
    Zephyr AND the previous hal_alif, with nothing exiting non-zero."""
    west_yml = (
        "manifest:\n  projects:\n    - name: zephyr\n      revision: v4.4.1\n"
        "  self:\n    path: alp-sdk\n"
    )
    pin = resolve_zephyr_pin(west_yml, "v4.4.1")
    assert pin == "4.4.1"
    # west.yml LEADS, so bootstrap and `build`'s preflight cannot disagree and
    # auto-bootstrap cannot loop.
    assert parse_west_zephyr_pin(west_yml) == pin
    assert resolve_zephyr_pin(west_yml.replace("v4.4.1", "v4.9.3"), "v4.4.1") == "4.9.3"
    # A branch/SHA revision has no version to compare -> the manifest's.
    assert resolve_zephyr_pin(west_yml.replace("v4.4.1", "main"), "v4.4.1") == "4.4.1"
    assert resolve_zephyr_pin(None, "v4.6.0") == "4.6.0"

    v440 = "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 0\nEXTRAVERSION =\n"
    assert decide_workspace_reuse(v440, True, True, "4.4.1") == (STALE, "4.4.0")
    assert decide_workspace_reuse(v440, True, True, "4.4.0") == (REUSE, "4.4.0")


def test_a_foreign_manifest_is_never_stale_only_mismatched_or_ignored():
    """`west update` over someone else's workspace would drive it off alp-sdk's
    manifest, so a foreign tree is refused, never adopted."""
    v440 = "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 0\n"
    assert decide_workspace_reuse(v440, True, False, "4.4.0")[0] == MANIFEST_MISMATCH
    assert decide_workspace_reuse(v440, True, False, "4.5.0")[0] == INCOMPATIBLE
    assert decide_workspace_reuse(v440, False, True, "4.4.0")[0] == INCOMPATIBLE
    assert decide_workspace_reuse("not a version file", True, True, "4.4.0")[0] == INCOMPATIBLE
    assert parse_zephyr_version_file("VERSION_MAJOR = 4\n") is None


# tan-cli#334: `INCOMPATIBLE` is `decide_workspace_reuse`'s catch-all -- reached
# by missing on ONE axis (no readable VERSION, or no `.west/`) or on TWO at
# once (a real workspace that is both off-pin AND on a foreign manifest). The
# rejection message must still name whichever facts were actually observed,
# the way `STALE` and `MANIFEST_MISMATCH` already do for their own single-axis
# cases -- not a fixed string, so these assert by CONTENT.
V440 = "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 0\n"


def _incompatible_message(monkeypatch, tmp_path, existing_facts):
    """Drives `_select_workspace` for a canned `_existing_workspace_facts`
    triple `(version_file, top_is_west_workspace, manifest_is_sdk)` -- the
    decision + message-rendering under test, not the filesystem probing that
    `_existing_workspace_facts` covers on its own."""
    zephyr_base = tmp_path / "zephyr"
    monkeypatch.setenv("ZEPHYR_BASE", str(zephyr_base))
    monkeypatch.setattr(bootstrap_cmd, "_existing_workspace_facts", lambda _repo_root: existing_facts)
    log = bootstrap_cmd.Log(json_mode=True)
    paths = bootstrap_cmd.RunPaths(
        repo_root=tmp_path / "sdk",
        workspace_dir=tmp_path / "ws",
        venv_dir=tmp_path / "ws" / ".venv",
    )
    bootstrap_cmd._select_workspace(log, False, "4.4.1", fallback_facts((3, 12)), paths)
    assert [code for code, _ in log.warnings] == ["zephyr-base-incompatible"]
    return log.warnings[0][1]


def test_incompatible_names_the_version_and_pin_when_only_that_axis_missed(monkeypatch, tmp_path):
    """No `.west/` at the topdir, so the manifest axis was never in play -- but
    the Zephyr VERSION was readable and off the pin: name both, the way STALE
    already does for its own (same-manifest) case."""
    message = _incompatible_message(monkeypatch, tmp_path, (V440, False, False))
    assert "4.4.0" in message
    assert "4.4.1" in message


def test_incompatible_names_the_foreign_manifest_when_only_that_axis_missed(monkeypatch, tmp_path):
    """A `.west/` IS there but its manifest is not this SDK's, and no Zephyr
    VERSION could be read at all: name the manifest problem, the way
    MANIFEST_MISMATCH already does for its own (on-pin) case."""
    message = _incompatible_message(monkeypatch, tmp_path, ("not a version file", True, False))
    assert "manifest" in message
    assert "not alp-sdk's west.yml" in message


def test_incompatible_names_both_axes_when_both_missed_at_once(monkeypatch, tmp_path):
    """The reported case (tan-cli#334): a real `.west/` workspace on a real
    Zephyr checkout, but the WRONG version AND a foreign manifest together --
    misses both the STALE and the MANIFEST_MISMATCH branch, so both facts must
    survive into the catch-all rather than neither."""
    message = _incompatible_message(monkeypatch, tmp_path, (V440, True, False))
    assert "4.4.0" in message
    assert "4.4.1" in message
    assert "not alp-sdk's west.yml" in message


def test_incompatible_keeps_its_original_wording_when_genuinely_not_a_workspace(
    monkeypatch, tmp_path
):
    """No readable Zephyr VERSION and no `.west/` -- there is nothing to name,
    so the terse original wording is exactly preserved: this is the case the
    branch's comment always meant."""
    message = _incompatible_message(monkeypatch, tmp_path, ("not a version file", False, False))
    assert message == (
        f"$ZEPHYR_BASE ({tmp_path / 'zephyr'}) is not an alp-sdk Zephyr 4.4.1 west workspace -- "
        f"ignoring it and building an isolated one"
    )


def test_the_parent_guard_never_keys_off_a_directory_name(tmp_path):
    """A name list (`Downloads`/`Desktop`/...) is locale-dependent and incomplete
    by construction. The guard counts entries instead."""
    # The documented `mkdir alp && cd alp && git clone ...` flow.
    assert not parent_needs_workspace_guard(["alp-sdk"], "alp-sdk", ".venv", False)
    assert not parent_needs_workspace_guard([], "alp-sdk", ".venv", False)
    # bootstrap's OWN venv is not foreign content: a run that died between
    # `python -m venv` and the pip installs must reach the venv-recovery path.
    assert not parent_needs_workspace_guard(["alp-sdk", ".venv"], "alp-sdk", ".venv", False)
    # A nested `venv.dirName` only ever shows its FIRST component one level down.
    assert not parent_needs_workspace_guard(["alp-sdk", "tools"], "alp-sdk", "tools/.venv", False)
    # Any other entry guards, dotfiles included.
    assert parent_needs_workspace_guard(["alp-sdk", ".bashrc"], "alp-sdk", ".venv", False)
    # A CONFIRMED west workspace is sufficient on its own; nothing else is even
    # inspected.
    assert not parent_needs_workspace_guard(["alp-sdk", "Photos"], "alp-sdk", ".venv", True)


def test_a_dot_west_that_is_a_plain_file_still_guards(tmp_path):
    """A FILE, or an empty directory, named `.west` is not a workspace. Letting
    the NAME answer that was a false PROCEED -- `west init` then refused the very
    content the guard had waved through."""
    parent = tmp_path / "p"
    repo = parent / "alp-sdk"
    repo.mkdir(parents=True)
    (parent / ".west").write_text("not a workspace", encoding="utf-8")
    assert default_relocation_target(repo, parent, ".venv") == parent / "alp-workspace"

    real = tmp_path / "q"
    repo2 = real / "alp-sdk"
    repo2.mkdir(parents=True)
    (real / ".west").mkdir()
    (real / ".west" / "config").write_text("[manifest]\npath = alp-sdk\n", encoding="utf-8")
    (real / "zephyr").mkdir()
    assert default_relocation_target(repo2, real, ".venv") is None


def test_an_unreadable_parent_is_not_treated_as_confirmed_dirty(tmp_path):
    """`None`, not `[]`: an unreadable parent tells the guard nothing, and `[]`
    would read as "confirmed empty", a claim we cannot make."""
    ghost = tmp_path / "ghost"
    assert default_relocation_target(ghost / "alp-sdk", ghost, ".venv") is None


def test_runtime_resolution_routes_through_the_presets_owner():
    """ONE owner of `board:`->zephyr / `machine:`->yocto / core-id heuristic. Two
    copies is how `tan presets` and `tan bootstrap` come to disagree about which
    host can build a project."""
    topology = {"a55_cluster": "yocto", "m33_sm": "zephyr"}
    assert in_play_runtimes({"m33_sm": None}, None, topology) == ["zephyr"]
    assert in_play_runtimes({"a55_cluster": "off", "m33_sm": None}, None, topology) == ["zephyr"]
    assert in_play_runtimes({"a55_cluster": None, "m33_sm": None}, None, topology) == [
        "yocto", "zephyr"
    ]
    # No `cores:` -> a v1 top-level `os:` wins, else the whole topology.
    assert in_play_runtimes(None, "baremetal", topology) == ["baremetal"]
    assert in_play_runtimes(None, None, topology) == ["yocto", "zephyr"]
    # A core the topology does not know falls back to the id heuristic.
    assert in_play_runtimes({"a72_big": None}, None, {}) == ["yocto"]
    assert in_play_runtimes(None, None, {}) == []


def test_the_yocto_gate_refuses_only_an_entirely_yocto_project_off_linux():
    yocto_only = ["yocto"]
    for host in (WINDOWS, MACOS, OTHER):
        assert yocto_gate(yocto_only, host) == "refuse"
    assert yocto_gate(yocto_only, LINUX) == "clear"
    assert yocto_gate(["yocto", "zephyr"], WINDOWS) == "warn"
    assert yocto_gate(["zephyr"], WINDOWS) == "clear"
    # An unrecognised `os:` is UNRESOLVABLE, not a refusal.
    assert yocto_gate(["yocto", "something-else"], WINDOWS) == "warn"
    assert yocto_gate([], WINDOWS) == "clear"


def test_host_detection_maps_the_platform_strings():
    assert detect_host_os("linux") == detect_host_os("linux2") == LINUX
    assert detect_host_os("darwin") == MACOS
    assert detect_host_os("win32") == WINDOWS
    assert detect_host_os("freebsd13") == OTHER


def test_a_refusal_renders_advice_in_the_line_and_null_in_the_command():
    """A consumer renders `command` as something it can RUN, so prose there is a
    button that fails."""
    install = parse_bootstrap_manifest(REAL_MANIFEST).install_for_host(WINDOWS)
    refusal = windows_refusal(["ninja", "tan-no-such-tool-xyz"], install)
    assert refusal.code == "prerequisites-missing"
    assert refusal.lines[1] == "  ninja  ->  winget install -e --id Ninja-build.Ninja"
    assert refusal.lines[2] == (
        "  tan-no-such-tool-xyz  ->  install `tan-no-such-tool-xyz` and put it on PATH"
    )
    assert [m.command for m in refusal.missing] == [
        "winget install -e --id Ninja-build.Ninja", None
    ]
    assert hint_line("ninja", {}) == "  ninja  ->  install `ninja` and put it on PATH"


def test_every_host_gets_its_own_package_managers_command_for_one_tool():
    """Handing a macOS user Linux's `apt-get` line is the bug a `posix`-keyed
    lookup would cause."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    assert facts.install_for_host(LINUX, linux_pm=LINUX_PM_APT)["cmake"] == (
        "sudo apt-get install -y cmake"
    )
    assert facts.install_for_host(MACOS)["cmake"] == "brew install cmake"
    assert facts.install_for_host(WINDOWS)["cmake"] == "winget install -e --id Kitware.CMake"
    # A POSIX host that is neither: no manifest entry, so `null` -- never a
    # wrong-OS command.
    assert facts.install_for_host(OTHER) == {}
    # No confirmed package manager at all -- `null` for every tool, not a
    # guess (tan-cli#760's second half).
    assert facts.install_for_host(LINUX) == {}
    assert facts.install_for_host(LINUX, linux_pm=None) == {}
    # `REAL_MANIFEST` declared `install.linux` in the pre-alp-sdk#1471 FLAT
    # shape (Debian's, unconditionally) until tan-cli#846's pin bump; a `dnf`
    # query came back empty then. It now carries alp-sdk#1464/#1471's PM-keyed
    # shape, so `dnf` resolves to dnf's OWN commands -- which is the same
    # invariant either way: a Fedora host never gets Debian's line under it.
    assert facts.install_for_host(LINUX, linux_pm=LINUX_PM_DNF)["cmake"] == (
        "sudo dnf install -y cmake"
    )


def test_macos_reads_its_own_tool_list_and_falls_back_to_posix_without_one():
    """alp-sdk v0.14.0 added `xz`/`wget` to `prerequisites.posix` and a separate
    `prerequisites.macos` that omits them. Keying the list off `is_windows` hands
    macOS the POSIX list and refuses a stock macOS host -- which ships neither --
    for tools the SDK does not ask macOS for."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    assert facts.prerequisites(LINUX)[-2:] == ("xz", "wget")
    assert "xz" not in facts.prerequisites(MACOS)
    assert facts.prerequisites(WINDOWS) == ("git", "cmake", "python", "ninja")

    # An SDK predating the split declares no `macos` -- which must keep meaning
    # "read `posix`", not "no prerequisites at all".
    legacy = type(facts)(**{**vars(facts), "prerequisites_macos": ()})
    assert legacy.prerequisites(MACOS) == legacy.prerequisites(LINUX)


def test_the_posix_refusal_keeps_the_oracle_line_and_adds_the_doctor_fix_remedy():
    """Was `..._stays_one_line_with_two_spaces_before_install`, which asserted
    the refusal is exactly ONE line. tan-cli#355 deliberately makes it two, so
    that assertion now encodes the wrong intent and is inverted here rather than
    left to fail.

    What is NOT negotiable, and is still pinned byte-for-byte, is `bootstrap.sh`'s
    own first line -- including the TWO spaces before "Install", which any reflow
    would silently eat. The per-tool commands still travel in the STRUCTURED half
    only; that half of the original constraint is unchanged.

    What is added is a second line naming `tan doctor --build --fix`. The old
    wording predates tan having an installer at all; tan-cli#91 gave it one, and
    a pristine `ubuntu:24.04` showed a first-time customer being handed four
    package names with no route to them while that command sat one subcommand
    away. Withholding a remedy tan HAS, to match an oracle that never had one,
    is parity serving nobody."""
    install = parse_bootstrap_manifest(REAL_MANIFEST).install_for_host(LINUX, linux_pm=LINUX_PM_APT)
    refusal = posix_refusal(["cmake", "ninja"], install)
    assert len(refusal.lines) == 2, refusal.lines
    assert refusal.lines[0] == "Missing required tools: cmake ninja.  Install them and re-run."
    assert "  Install them" in refusal.lines[0], "the oracle's double space was reflowed away"
    assert "tan doctor --build --fix" in refusal.lines[1]
    assert [m.command for m in refusal.missing] == [
        "sudo apt-get install -y cmake", "sudo apt-get install -y ninja-build"
    ]


def test_check_prerequisites_nulls_the_command_when_the_package_manager_is_absent(monkeypatch):
    """tan-cli#760, end to end through `check_prerequisites` (the function
    `tan bootstrap` actually calls). Measured on fedora:42/archlinux:latest/
    rockylinux:9: none of the three has `apt-get`, yet alp-sdk's
    `prerequisites.install.linux` is six `sudo apt-get install -y ...` lines
    -- so on a host where NOTHING is on PATH (tools absent AND `apt-get`
    absent), every `MissingPrerequisite.command` this call produces must be
    `None`, never the unrunnable string that used to reach `alp-sdk-vscode`'s
    Fix button byte-identical to a real Debian host."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    floor = PythonFloor(effective=(3, 10), source="x", manifest=(3, 10))
    monkeypatch.setattr(bootstrap_cmd, "on_path", lambda _name: None)

    python, refusal = check_prerequisites(facts, LINUX, floor)

    assert python is None
    assert refusal is not None and refusal.code == "prerequisites-missing"
    assert refusal.missing, "the real manifest's Linux tool list must not be empty"
    assert all(m.command is None for m in refusal.missing)


def test_check_prerequisites_keeps_the_command_when_apt_get_is_confirmed(monkeypatch):
    """The other half of tan-cli#760: a real Debian/Ubuntu host DOES have
    `apt-get`, so the guard must not strip a command that can actually run
    there -- dropping every entry unconditionally would just trade one wrong
    answer (a command that never runs) for another (no command shown even
    where one works)."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    floor = PythonFloor(effective=(3, 10), source="x", manifest=(3, 10))
    # `sudo` itself must be confirmed too (tan-cli#760 review MINOR 3) -- a
    # host with `apt-get` but no `sudo` is not actually a Debian/Ubuntu one
    # this remedy works on. Every OTHER tool stays absent (`None`, matching
    # `on_path`'s real `str | None` contract -- a bare bool broke downstream
    # `is not None` presence checks) so the `missing` branch is the one this
    # test actually exercises.
    monkeypatch.setattr(
        bootstrap_cmd,
        "on_path",
        lambda name: f"/usr/bin/{name}" if name in {"apt-get", "sudo"} else None,
    )

    python, refusal = check_prerequisites(facts, LINUX, floor)

    assert python is None
    assert refusal is not None
    by_tool = {m.tool: m.command for m in refusal.missing}
    assert by_tool["cmake"] == "sudo apt-get install -y cmake"
    assert by_tool["ninja"] == "sudo apt-get install -y ninja-build"


def test_check_prerequisites_nulls_the_venv_unusable_command_when_apt_get_is_absent(
    monkeypatch,
):
    """tan-cli#760 review MAJOR 2 / Closes #765: `posix_venv_unusable()`'s
    hardcoded `sudo apt-get install -y python3-venv` reached the bootstrap
    envelope completely unguarded before this fix -- measured on a host with
    nothing on PATH, `refusal.missing` carried the RAW command. The
    structural claim "this is the ONE place that decides" was false; this
    proves the second call site is now guarded too, with NO signature
    change to `posix_venv_unusable` itself."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    floor = PythonFloor(effective=(3, 10), source="x", manifest=(3, 10))
    # Every required tool present (so the missing-tools branch is never
    # reached) EXCEPT the installer commands (`apt-get`/`sudo`), which are
    # absent -- the venv-incapable branch is the only one this test exercises.
    # `str | None`, matching `on_path`'s real contract -- a bare bool broke
    # downstream `is not None` presence checks elsewhere on this path.
    monkeypatch.setattr(
        bootstrap_cmd,
        "on_path",
        lambda name: None if name in {"apt-get", "sudo"} else f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        bootstrap_cmd, "probe_host_python", lambda _floor: HostPython(("python3",), (3, 12))
    )
    monkeypatch.setattr(bootstrap_cmd, "python_venv_capable", lambda _python: False)

    python, refusal = check_prerequisites(facts, LINUX, floor)

    assert python is None
    assert refusal is not None and refusal.code == "venv-unusable"
    assert refusal.missing == (MissingPrerequisite("python3-venv", None),)


# ---------------------------------------------------------------------------
# tan-cli#760's second half (alp-sdk#1464 / #1471): `install.linux` is now
# PACKAGE-MANAGER-keyed (`{"apt": {...}, "dnf": {...}}`), not tool-keyed, on
# an alp-sdk `dev` manifest. `REAL_MANIFEST` above still declares the
# pre-#1471 FLAT shape (it tracks `parity.yml`'s `PINNED_SDK_TAG`, a pin onto
# alp-sdk `main`, which has not merged that change yet) -- exercised as the
# OLD-MANIFEST case below. `FEDORA_AWARE_MANIFEST` carries alp-sdk `dev` @
# `7a419865`'s real `install.linux.{apt,dnf}` content, apt's six commands
# given one harmless, DISTINGUISHING marker each (`--no-install-recommends`)
# so a test cannot pass merely because tan's hardcoded apt fallback happens
# to be byte-identical to the real manifest data -- every assertion below
# that compares a resolved command against one of these marked strings is
# proof the REAL per-tool value reached the caller, not tan's own fallback
# table standing in for it unnoticed (exactly the tan-cli#760 defect: "the
# map comes back empty, and tan falls back to its byte-pinned apt table").
# ---------------------------------------------------------------------------

FEDORA_AWARE_INSTALL_LINUX = {
    LINUX_PM_APT: {
        "git": "sudo apt-get install -y --no-install-recommends git",
        "cmake": "sudo apt-get install -y --no-install-recommends cmake",
        "python3": "sudo apt-get install -y --no-install-recommends python3",
        "ninja": "sudo apt-get install -y --no-install-recommends ninja-build",
        "xz": "sudo apt-get install -y --no-install-recommends xz-utils",
        "wget": "sudo apt-get install -y --no-install-recommends wget",
    },
    LINUX_PM_DNF: {
        "git": "sudo dnf install -y git",
        "cmake": "sudo dnf install -y cmake",
        "python3": "sudo dnf install -y python3",
        # No `ninja` -- alp-sdk#1464's deliberate gap: Rocky/RHEL's default
        # repos carry no `ninja`/`ninja-build` under any name without EPEL.
        "xz": "sudo dnf install -y xz",
        "wget": "sudo dnf install -y wget",
    },
}


def _manifest_with_linux_install(pm_map: dict) -> str:
    """`REAL_MANIFEST`, with `prerequisites.install.linux` replaced -- every
    other field (schemaVersion, zephyr, venv, west, pip, ...) stays real, so
    only the one thing each test below is about actually varies."""
    doc = json.loads(REAL_MANIFEST)
    doc["prerequisites"]["install"]["linux"] = pm_map
    return json.dumps(doc)


FEDORA_AWARE_MANIFEST = _manifest_with_linux_install(FEDORA_AWARE_INSTALL_LINUX)


def _apt_get_only(name: str) -> bool:
    return name == "apt-get"


def _dnf_and_sudo_only(name: str) -> bool:
    return name in {"dnf", "sudo"}


def _nothing_on_path(_name: str) -> bool:
    return False


def test_check_prerequisites_resolves_a_dnf_command_on_a_fedora_shaped_host(monkeypatch):
    """**The core of tan-cli#760's second half.** `dnf` (and `sudo`) confirmed,
    `apt-get` absent -- the `fedora:42`/`rockylinux:9` shape from the linked
    issues. `cmake`'s command must be the manifest's REAL `dnf` line, not
    `None` (tan's pre-fix behaviour: the nested `install.linux` filtered to
    nothing, fell back to the apt table, which then got dropped by tan-cli
    #760's PATH guard since `apt-get` is absent here) and not a guessed
    `apt-get` line either.

    Verified to FAIL against pre-fix code: pre-fix, `install_for_host` has no
    `linux_pm` parameter at all and `_resolve_install_commands` cannot read
    the nested shape, so `install_for_host(LINUX)` always returns the
    fallback apt table regardless of host -- unconfirmed here (`apt-get`
    absent), every command nulls, and the assertion below fails with
    `None != "sudo dnf install -y cmake"`.
    """
    facts = parse_bootstrap_manifest(FEDORA_AWARE_MANIFEST)
    floor = PythonFloor(effective=(3, 10), source="x", manifest=(3, 10))
    monkeypatch.setattr(
        bootstrap_cmd, "on_path", lambda name: f"/usr/bin/{name}" if name in {"dnf", "sudo"} else None
    )

    python, refusal = check_prerequisites(facts, LINUX, floor)

    assert python is None
    assert refusal is not None
    by_tool = {m.tool: m.command for m in refusal.missing}
    assert by_tool["cmake"] == "sudo dnf install -y cmake"
    assert by_tool["git"] == "sudo dnf install -y git"
    # And never the apt line, marked or not -- proves this is a real `dnf`
    # resolution, not the apt sub-map leaking across package managers.
    assert "apt-get" not in by_tool["cmake"]


def test_check_prerequisites_leaves_ninjas_dnf_gap_null_never_a_guessed_package(monkeypatch):
    """The other deliberate half of the same manifest: `install.linux.dnf` has
    no `ninja` entry at all (alp-sdk#1464's Rocky/RHEL gap). `ninja` must stay
    `None` -- never `ninja-build` (apt's package name) and never a GUESSED
    `dnf`-shaped `ninja` command tan invented to fill the gap -- while `cmake`,
    on the SAME host, resolves normally. Proving both in one call is what
    makes this a `dnf`-resolution test and not just a repeat of the
    already-covered "nothing resolves" case.

    Verified to FAIL against pre-fix code for the same reason as the sibling
    test above: `cmake`'s command comes back `None`, not the real `dnf` line,
    because `apt-get` (the only PM tan's pre-fix fallback ever offers) is
    absent on this host.
    """
    facts = parse_bootstrap_manifest(FEDORA_AWARE_MANIFEST)
    floor = PythonFloor(effective=(3, 10), source="x", manifest=(3, 10))
    monkeypatch.setattr(
        bootstrap_cmd, "on_path", lambda name: f"/usr/bin/{name}" if name in {"dnf", "sudo"} else None
    )

    python, refusal = check_prerequisites(facts, LINUX, floor)

    assert python is None
    assert refusal is not None
    by_tool = {m.tool: m.command for m in refusal.missing}
    assert by_tool["cmake"] == "sudo dnf install -y cmake"
    assert by_tool["ninja"] is None
    assert "ninja-build" not in " ".join(refusal.lines)
    assert "ninja" not in (by_tool.get("ninja") or "")


def test_check_prerequisites_still_prefers_apt_when_a_manifest_carries_both(monkeypatch):
    """`detect_linux_pm` checks `apt-get` BEFORE `dnf` -- the same order
    alp-sdk's own two detectors use (`scripts/bootstrap.sh`'s `LINUX_PM`
    block, `scripts/alp_cli/doctor.py`'s `_prereq_linux_pm()`). A real Debian
    host with `apt-get` on PATH must get `apt`'s command even though this
    manifest ALSO carries `dnf` data -- the marker text
    (`--no-install-recommends`) is what proves the REAL apt sub-map was read,
    not tan's own byte-identical hardcoded fallback standing in for it.

    Verified to FAIL against pre-fix code: pre-fix, `install_for_host(LINUX)`
    returns tan's own hardcoded fallback (the nested manifest filters to
    nothing), which carries the PLAIN `sudo apt-get install -y cmake` --
    never the marked `--no-install-recommends` variant this manifest actually
    declares -- so the equality assertion below fails.
    """
    facts = parse_bootstrap_manifest(FEDORA_AWARE_MANIFEST)
    floor = PythonFloor(effective=(3, 10), source="x", manifest=(3, 10))
    monkeypatch.setattr(
        bootstrap_cmd,
        "on_path",
        lambda name: f"/usr/bin/{name}" if name in {"apt-get", "sudo"} else None,
    )

    python, refusal = check_prerequisites(facts, LINUX, floor)

    assert python is None
    assert refusal is not None
    by_tool = {m.tool: m.command for m in refusal.missing}
    assert by_tool["cmake"] == "sudo apt-get install -y --no-install-recommends cmake"
    assert "dnf" not in by_tool["cmake"]


def test_check_prerequisites_degrades_host_neutral_with_neither_package_manager(monkeypatch):
    """Neither `apt-get` nor `dnf` resolves (Alpine/musl; a `pacman` host,
    which this manifest never even carries a sub-map for) -- every Linux tool
    must null, and no package-manager-specific text may appear anywhere in the
    refusal's prose, on a manifest that in fact carries usable `dnf` data for
    a DIFFERENT host. `detect_linux_pm`/`install_for_host(..., linux_pm=None)`
    are new tan-cli#760 API surface pre-fix code does not have at all --
    calling this scenario through `check_prerequisites` (which internally
    calls both) is what makes the assertions below meaningful rather than
    coincidentally true on both sides of the fix.
    """
    facts = parse_bootstrap_manifest(FEDORA_AWARE_MANIFEST)
    floor = PythonFloor(effective=(3, 10), source="x", manifest=(3, 10))
    monkeypatch.setattr(bootstrap_cmd, "on_path", lambda _name: None)

    python, refusal = check_prerequisites(facts, LINUX, floor)

    assert python is None
    assert refusal is not None
    assert all(m.command is None for m in refusal.missing)
    prose = " ".join(refusal.lines)
    assert "apt-get" not in prose
    assert "dnf" not in prose


def test_normalize_and_select_round_trip_the_new_and_legacy_linux_shapes():
    """The pure decision functions, isolated from PATH probing entirely --
    `select_linux_install`/`normalize_linux_install`/`detect_linux_pm` do not
    exist at all pre-fix, so any of these calls is an `ImportError` there,
    not merely a wrong value.

    Covers design decision (4): a manifest whose `install.linux` is still the
    pre-alp-sdk#1471 FLAT shape is read AS `apt`'s sub-map -- correct on a
    real apt host, and never leaked to a `dnf` one. That branch used to be
    exercised straight off `REAL_MANIFEST`, with a self-cancelling guard for
    the day the fixture caught up; tan-cli#846's pin bump is that day, so the
    legacy shape now comes from a literal (`FEDORA_AWARE_INSTALL_LINUX`'s own
    `apt` sub-map, which IS what a flat `install.linux` looks like) and the
    guard is inverted onto `REAL_MANIFEST` to catch a re-vendor going
    backwards."""
    new_shape = normalize_linux_install(FEDORA_AWARE_INSTALL_LINUX)
    assert new_shape[LINUX_PM_APT]["cmake"] == (
        "sudo apt-get install -y --no-install-recommends cmake"
    )
    assert new_shape[LINUX_PM_DNF]["cmake"] == "sudo dnf install -y cmake"
    assert "ninja" not in new_shape[LINUX_PM_DNF]

    real_raw = json.loads(REAL_MANIFEST)["prerequisites"]["install"]["linux"]
    assert all(isinstance(v, dict) for v in real_raw.values()), (
        "REAL_MANIFEST went back to the FLAT shape -- a re-vendor moved the "
        "pin backwards past alp-sdk#1471, or the fixture was hand-edited"
    )
    legacy_raw = FEDORA_AWARE_INSTALL_LINUX[LINUX_PM_APT]
    legacy_shape = normalize_linux_install(legacy_raw)
    assert legacy_shape == {LINUX_PM_APT: legacy_raw}
    assert select_linux_install(legacy_shape, LINUX_PM_APT) == legacy_raw
    assert select_linux_install(legacy_shape, LINUX_PM_DNF) == {}
    assert select_linux_install(legacy_shape, None) == {}

    assert detect_linux_pm(_apt_get_only) == LINUX_PM_APT
    assert detect_linux_pm(_dnf_and_sudo_only) == LINUX_PM_DNF
    assert detect_linux_pm(_nothing_on_path) is None


def test_the_tool_less_refusals_carry_their_own_codes_and_report_null():
    """A `{tool, command}` pair cannot represent "the Python you have is 3.10", so
    these must not report under `prerequisites-missing` -- a consumer keying on
    that code would get an empty array against a fully actionable message."""
    install = parse_bootstrap_manifest(REAL_MANIFEST).install_for_host(WINDOWS)
    not_runnable = windows_python_not_runnable(install)
    assert not_runnable.code == "python-not-runnable"
    assert reported_missing(not_runnable.missing) is None
    # The package ID comes from the MANIFEST, never a second hardcoded copy.
    assert "winget install -e --id Python.Python.3.12" in not_runnable.lines[0]
    assert "Windows Store alias" in windows_python_not_runnable({}).lines[0]

    too_old = python_too_old((3, 9), (3, 10), install, floor_source="x", manifest_floor=(3, 10))
    assert too_old.code == "python-too-old"
    assert reported_missing(too_old.missing) is None

    # `venv-unusable` is the exception: python3 IS there and DID run, and a Fix
    # button needs something runnable.
    unusable = posix_venv_unusable()
    assert unusable.code == "venv-unusable"
    assert reported_missing(unusable.missing) == [
        {"tool": "python3-venv", "command": "sudo apt-get install -y python3-venv"}
    ]
    assert reported_missing(()) is None


def test_the_west_config_pointer_survives_a_rewrite_byte_for_byte():
    """`.west/config` is the topdir's ONLY manifest pointer, shared by every SDK
    version under it. Comments, other sections and the file's own CRLF must
    survive."""
    config = "# top\r\n[manifest]\r\npath = old-sdk\r\n[zephyr]\r\npath = keep-me\r\n"
    assert get_manifest_path(config) == "old-sdk"
    rewritten = set_manifest_path(config, "new-sdk")
    assert rewritten == "# top\r\n[manifest]\r\npath = new-sdk\r\n[zephyr]\r\npath = keep-me\r\n"
    # Section-scoped: a `path =` under another section is never returned.
    assert get_manifest_path("[zephyr]\npath = nope\n") is None
    assert set_manifest_path("[zephyr]\npath = nope\n", "x") is None
    # A comment line is not a key-value pair.
    assert get_manifest_path("[manifest]\n# path = commented\n") is None


def test_a_stale_manifest_pointer_is_rewritten_and_a_matching_one_is_left_alone(tmp_path):
    """The "already initialised" branch runs `west update` WITHOUT re-running
    `west init -l`, so a config left by a different SDK under the same topdir
    would silently pull the WRONG SDK's west.yml."""
    topdir = tmp_path / "top"
    (topdir / "v0.6.0").mkdir(parents=True)
    new_sdk = topdir / "v0.7.0"
    new_sdk.mkdir()
    (topdir / ".west").mkdir()
    config = topdir / ".west" / "config"
    config.write_text("[manifest]\npath = v0.6.0\n", encoding="utf-8")

    assert reconcile_west_manifest_path(str(new_sdk)) == ("rewrote", "v0.6.0", "v0.7.0")
    assert get_manifest_path(config.read_text(encoding="utf-8")) == "v0.7.0"
    assert reconcile_west_manifest_path(str(new_sdk))[0] == "already-matches"

    # No `.west/config` at all is the one SILENT case.
    lone = tmp_path / "lone" / "alp-sdk"
    lone.mkdir(parents=True)
    assert reconcile_west_manifest_path(str(lone)) == ("not-applicable", None, None)


def test_an_unreadable_west_config_is_a_failure_never_a_silent_no_op(tmp_path):
    """`west update` is about to run against whatever that unrewritten pointer
    names -- i.e. the WRONG SDK's west.yml. Reporting "nothing to do" here IS the
    silent-success bug."""
    topdir = tmp_path / "top"
    sdk = topdir / "alp-sdk"
    sdk.mkdir(parents=True)
    (topdir / ".west" / "config").mkdir(parents=True)  # present, unreadable
    outcome, _old, detail = reconcile_west_manifest_path(str(sdk))
    assert outcome == "failed" and detail


def _stale_west_config(tmp_path):
    """A topdir whose `.west/config` still points at `v0.6.0`, plus a fresh
    `v0.7.0` checkout that would reconcile onto it -- the shared setup every
    tan-cli#516 test below rewrites."""
    topdir = tmp_path / "top"
    (topdir / "v0.6.0").mkdir(parents=True)
    new_sdk = topdir / "v0.7.0"
    new_sdk.mkdir()
    (topdir / ".west").mkdir()
    config = topdir / ".west" / "config"
    config.write_text("[manifest]\npath = v0.6.0\n", encoding="utf-8")
    return new_sdk, config


def test_reconcile_west_manifest_path_fsyncs_before_replacing_the_config(tmp_path, monkeypatch):
    """tan-cli#516: the pre-fix code wrote the temp sibling with a bare
    `Path.write_text` + `os.replace` and never called `os.fsync` anywhere --
    atomic with respect to the RENAME only, so a crash between the rename
    landing and the temp's data reaching stable storage could leave
    `.west/config` -- the topdir's ONLY manifest pointer, shared by every SDK
    version under it -- renamed to its real name with truncated or missing
    content. FAILS against the unfixed code: `calls` stays empty because
    `os.fsync` is never invoked on that path."""
    new_sdk, config = _stale_west_config(tmp_path)
    real_fsync = os.fsync
    calls: list[int] = []

    def spy_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    # `monkeypatch.setattr`, not a raw `atomic_write_mod.os.fsync = ...`
    # assignment -- `os` is one shared module object, so a bare assignment
    # patches EVERY module's `os.fsync` process-wide and only unwinds if this
    # test's own `finally` runs; `monkeypatch` restores it unconditionally.
    monkeypatch.setattr(atomic_write_mod.os, "fsync", spy_fsync)
    assert reconcile_west_manifest_path(str(new_sdk)) == ("rewrote", "v0.6.0", "v0.7.0")

    assert calls, "reconcile_west_manifest_path must fsync the temp before renaming it into place"
    assert get_manifest_path(config.read_text(encoding="utf-8")) == "v0.7.0"


def test_a_west_config_fsync_failure_is_reported_and_leaves_the_original_untouched(
    tmp_path, monkeypatch
):
    """A failure INSIDE the durability sequence (`os.fsync`, after the temp's
    own buffer already holds the rewritten content but before it is durable or
    the rename happens) must surface as `"failed"`, must leave the real
    `.west/config` byte-identical -- not partially rewritten -- and must not
    leak the temp sibling. FAILS against the unfixed code for the reverse
    reason: it never calls `os.fsync` at all, so patching it to raise changes
    nothing and the write silently SUCCEEDS where this test expects a
    reported failure."""
    new_sdk, config = _stale_west_config(tmp_path)
    original = config.read_text(encoding="utf-8")

    def boom_fsync(_fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(atomic_write_mod.os, "fsync", boom_fsync)

    outcome, old, detail = reconcile_west_manifest_path(str(new_sdk))

    assert outcome == "failed" and detail
    assert old == "v0.6.0"
    assert config.read_text(encoding="utf-8") == original
    leftovers = list(config.parent.glob("*.tan-tmp"))
    assert leftovers == [], leftovers


# ---------------------------------------------------------------------------
# tan-cli#292: the `<topdir>/.west/tan-workspace-sdk` record, extended with
# venv provenance -- `workspace_sdk_record_json`/`parse_workspace_sdk_record`.
# ---------------------------------------------------------------------------


def test_workspace_sdk_record_round_trips_the_full_provenance_stamp():
    text = workspace_sdk_record_json(
        "/ws/alp-sdk", venv_dir_name=".venv", venv_layout="bin", requirements_digest="ab" * 32
    )
    assert '"sdkPath": "/ws/alp-sdk"' in text
    assert '"venvDir": ".venv"' in text
    assert '"venvLayout": "bin"' in text
    assert f'"requirementsDigest": "{"ab" * 32}"' in text

    record = parse_workspace_sdk_record(text)
    assert record == WorkspaceSdkRecord(
        sdk_path="/ws/alp-sdk",
        venv_dir_name=".venv",
        venv_layout="bin",
        requirements_digest="ab" * 32,
    )


def test_workspace_sdk_record_omits_absent_provenance_fields_rather_than_writing_null():
    """A caller with nothing to report (no venv, a hash it could not compute)
    omits the key -- mirrors `Check.as_dict`'s `skip_serializing_if`, and
    keeps a record written by an older tan indistinguishable from one whose
    caller simply had nothing new to say."""
    text = workspace_sdk_record_json("/ws/alp-sdk")
    assert "venvDir" not in text
    assert "venvLayout" not in text
    assert "requirementsDigest" not in text
    assert parse_workspace_sdk_record(text) == WorkspaceSdkRecord(sdk_path="/ws/alp-sdk")


def test_parse_workspace_sdk_record_reads_a_pre_292_two_field_record():
    """A record written before tan-cli#292 (`sdkPath` + `updatedAt` only,
    `tan.core.scaffold.sdk_pointer_json`'s shape) must still parse -- the
    provenance fields are simply absent, not a parse failure."""
    legacy = '{\n  "sdkPath": "/ws/alp-sdk",\n  "updatedAt": "2026-01-01T00:00:00Z"\n}\n'
    assert parse_workspace_sdk_record(legacy) == WorkspaceSdkRecord(sdk_path="/ws/alp-sdk")


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        "[]",
        "42",
        '{"updatedAt": "2026-01-01T00:00:00Z"}',  # no sdkPath
        '{"sdkPath": 7}',  # wrong type
        '{"sdkPath": ""}',  # empty
    ],
)
def test_parse_workspace_sdk_record_returns_none_for_anything_unusable(text):
    """Unreadable is `None`, the SAME as no record at all -- never a mismatch
    WARNING against a checkout `doctor` cannot even name."""
    assert parse_workspace_sdk_record(text) is None


def test_record_workspace_sdk_writes_the_full_venv_provenance_stamp(tmp_path):
    """`bootstrap_cmd.record_workspace_sdk` -- the IO wrapper around
    `workspace_sdk_record_json` -- hashes the requirements file it is handed
    and writes every field, given all of them."""
    topdir = tmp_path / "ws"
    topdir.mkdir()
    requirements = topdir / "zephyr" / "scripts" / "requirements-base.txt"
    requirements.parent.mkdir(parents=True)
    # `newline=""`: a hash is of RAW BYTES, and `write_text`'s platform
    # newline translation (`\n` -> `\r\n` on Windows) would otherwise make
    # the fixture's on-disk bytes -- and so its hash -- host-dependent.
    requirements.write_text("west>=0.14.0\n", encoding="utf-8", newline="")

    bootstrap_cmd.record_workspace_sdk(
        topdir,
        str(topdir / "alp-sdk"),
        venv_dir_name=".venv",
        venv_layout="bin",
        requirements_path=requirements,
    )

    record = parse_workspace_sdk_record(
        (topdir / ".west" / "tan-workspace-sdk").read_text(encoding="utf-8")
    )
    assert record.sdk_path == str(topdir / "alp-sdk")
    assert record.venv_dir_name == ".venv"
    assert record.venv_layout == "bin"
    assert record.requirements_digest == hashlib.sha256(b"west>=0.14.0\n").hexdigest()


def test_record_workspace_sdk_omits_the_digest_when_the_requirements_file_is_unreadable(
    tmp_path,
):
    """A caller can hand `record_workspace_sdk` a path that (yet) does not
    exist -- e.g. `--no-pip`, or a Zephyr module that never shipped a
    requirements file at that path -- and the sdkPath half of the record must
    still be written; the digest is simply absent, never a fabricated one."""
    topdir = tmp_path / "ws"
    topdir.mkdir()

    bootstrap_cmd.record_workspace_sdk(
        topdir,
        str(topdir / "alp-sdk"),
        venv_dir_name=".venv",
        venv_layout="bin",
        requirements_path=topdir / "zephyr" / "does-not-exist.txt",
    )

    record = parse_workspace_sdk_record(
        (topdir / ".west" / "tan-workspace-sdk").read_text(encoding="utf-8")
    )
    assert record.sdk_path == str(topdir / "alp-sdk")
    assert record.requirements_digest is None


def test_record_workspace_sdk_still_writes_the_bare_record_with_no_venv_args(tmp_path):
    """Backward-compatible call shape: a caller passing only `(topdir,
    sdk_root)` -- there is none left in this tree, but the signature must not
    force every future one to compute a hash it may not have -- still writes
    a usable record."""
    topdir = tmp_path / "ws"
    topdir.mkdir()

    bootstrap_cmd.record_workspace_sdk(topdir, str(topdir / "alp-sdk"))

    record = parse_workspace_sdk_record(
        (topdir / ".west" / "tan-workspace-sdk").read_text(encoding="utf-8")
    )
    assert record == WorkspaceSdkRecord(sdk_path=str(topdir / "alp-sdk"))


def test_the_printed_blocks_keep_their_load_bearing_whitespace():
    """Copy-pasteable shell snippets: no `bootstrap: ` prefix, and POSIX quotes a
    value only when it contains `/`."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    tokens = Tokens("/home/dev/work/alp-sdk", "/home/dev/work")
    assert print_env_block(facts, tokens, "bin", False) == [
        "# Add to your shell profile (or run before invoking the SDK):",
        "# Activate the workspace venv (west + Zephyr/SDK Python deps live here):",
        '#   source "/home/dev/work/.venv/bin/activate"',
        'export ZEPHYR_BASE="/home/dev/work/zephyr"',
        "export ZEPHYR_TOOLCHAIN_VARIANT=zephyr",
    ]
    # The fallback constants must render the SAME bytes as the manifest.
    assert print_env_block(fallback_facts((3, 10)), tokens, "bin", False) == print_env_block(
        facts, tokens, "bin", False
    )


def test_windows_env_lines_never_come_out_with_mixed_separators():
    """The workspace token is forward-slash on every OS, so an un-normalised
    Windows line printed `C:/dev/work\\.venv\\Scripts\\Activate.ps1`."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    lines = print_env_block(facts, Tokens("C:/dev/work/alp-sdk", "C:/dev/work"), "Scripts", True)
    assert lines == [
        "# Add to your PowerShell profile (or run before invoking the SDK):",
        "# Activate the workspace venv (west + Zephyr/SDK Python deps live here):",
        '#   & "C:\\dev\\work\\.venv\\Scripts\\Activate.ps1"',
        '$env:ZEPHYR_BASE = "C:\\dev\\work\\zephyr"',
        '$env:ZEPHYR_TOOLCHAIN_VARIANT = "zephyr"',
    ]
    for line in (line for line in lines if "C:" in line):
        assert "/" not in line, f"mixed separators: {line}"
    # A backslash path in (what `bootstrap.ps1` itself has) is untouched.
    assert print_env_block(
        facts, Tokens("C:\\dev\\work\\alp-sdk", "C:\\dev\\work"), "Scripts", True
    ) == lines


def test_a_changed_manifest_changes_the_rendered_output_without_a_tan_release():
    """The whole point of consuming the manifest."""
    edited = REAL_MANIFEST.replace('"dirName": ".venv"', '"dirName": ".venv-4.5"').replace(
        '"ZEPHYR_TOOLCHAIN_VARIANT": "zephyr"',
        '"ZEPHYR_TOOLCHAIN_VARIANT": "zephyr", "ZEPHYR_EXTRA": "${SDK_ROOT}/x"',
    )
    facts = parse_bootstrap_manifest(edited)
    lines = print_env_block(facts, Tokens("/ws/alp-sdk", "/ws"), "bin", False)
    assert '#   source "/ws/.venv-4.5/bin/activate"' in lines
    assert 'export ZEPHYR_EXTRA="/ws/alp-sdk/x"' in lines


def test_the_windows_manual_install_block_prints_the_manifests_note_only():
    """Appending `nativeLibHints.windows.note` too printed the Arm/Zephyr-SDK
    sentence TWICE -- once hardcoded, once from the manifest."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    lines = optional_libs_block(facts, WINDOWS)
    assert lines[0] == ""
    assert lines[1] == "bootstrap: NOT auto-installed (manual, one-time):"
    assert len(lines) == 2 + len(facts.manual_install_windows)
    assert sum("developer.arm.com" in line for line in lines) == 1
    assert not any("Git Bash / MSYS2" in line for line in lines)


def test_the_posix_hint_block_carries_the_per_os_note_and_command():
    """The install command is no longer the block's LAST line: tan-cli#495
    defect 6 appends the `manualInstallHints.posix.note` section after it, as
    the oracle does (`blocks.rs:229-245`, after `bootstrap.sh:594` vs `:638`).
    So this pins the command as the end of the NATIVE-LIBS section -- the line
    immediately before the manual-install heading -- rather than the end of the
    list, which is what it happened to be when only Windows had manual hints."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    manual_heading = "bootstrap: NOT auto-installed (manual, one-time):"

    linux = optional_libs_block(facts, LINUX)
    assert linux[1] == "bootstrap: Optional native libraries unlock the Yocto-side backends:"
    assert "  libmosquitto-dev  -> alp_mqtt_* (cleartext + TLS)" in linux
    # The blank line the manual section opens with sits between the two.
    linux_end = linux.index(manual_heading) - 2
    assert linux[linux_end].startswith("  sudo apt-get install -y libmosquitto-dev")

    macos = optional_libs_block(facts, MACOS)
    assert "brew install mosquitto pkg-config" in macos[macos.index(manual_heading) - 2]

    # `OTHER` has no hint at all -- just the not-detected line, and no
    # POSIX-specific manual section either.
    other = optional_libs_block(facts, OTHER)
    assert other[-1] == "  (OS not auto-detected; see docs/testing.md)"
    assert manual_heading not in other


def test_next_steps_routes_the_posix_build_through_tan_with_absolute_paths():
    """`$PWD` is correct only when the reader happens to be standing IN the
    checkout -- and the workspace-parent guard above this block can have just
    moved it to a sibling `alp-workspace/alp-sdk`."""
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    lines = next_steps_block(facts, Tokens("/ws/alp-sdk", "/ws"), "/ws/.venv", "bin", False)
    assert '  source "/ws/.venv/bin/activate"' in lines
    assert '  tan build --sdk-root "/ws/alp-sdk" \\' in lines
    assert '      --project "/ws/alp-sdk/examples/peripheral-io/uart-echo"' in lines
    assert "  tan doctor" in lines
    assert not any("cargo install" in line for line in lines)

    win = next_steps_block(facts, Tokens("C:/ws/alp-sdk", "C:/ws"), "C:\\ws\\.venv", "Scripts", True)
    assert '  & "C:\\ws\\.venv\\Scripts\\Activate.ps1"' in win
    assert any("-DEXTRA_ZEPHYR_MODULES=C:\\ws\\alp-sdk" in line for line in win)


def test_capture_tail_prefers_stderr_and_keeps_the_last_lines_in_order():
    """Without this the JSON envelope carried no failure reason at all -- a pip
    traceback, a "no such file" -- because only the exit status was read."""
    assert capture_tail(b"a\nb\n", b"1\n2\n3\n4\n5\n") == "2 | 3 | 4 | 5"
    assert capture_tail(b"west init failed: no such file\n", b"") == (
        "west init failed: no such file"
    )
    assert capture_tail(b"", b"") == ""
    assert capture_tail("", "   \n \n") == ""
    # Non-UTF-8 child output must not become a crash that masquerades as a host
    # problem.
    assert "\ufffd" in capture_tail(b"", b"\xff\xfe boom\n")


def test_die_appends_a_detail_only_when_there_is_one():
    """Text mode usually has none (the child's log already streamed), so the bare
    message is what the user sees there -- no dangling colon."""
    assert die("west update failed", "") == "west update failed"
    assert die("west update failed", "  \n ") == "west update failed"
    assert die("west update failed", "fatal: not a git repo") == (
        "west update failed: fatal: not a git repo"
    )


def test_force_git_long_paths_env_is_the_documented_override_triple():
    assert bootstrap_cmd.FORCE_GIT_LONG_PATHS_ENV == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.longpaths",
        "GIT_CONFIG_VALUE_0": "true",
    }


def test_runner_run_extra_env_reaches_the_real_child_process():
    """tan-cli#306: `west_phase` passes `FORCE_GIT_LONG_PATHS_ENV` as
    `extra_env` on the `west update` call specifically so every nested `git`
    subprocess it spawns inherits it. This proves the PLUMBING with a real
    child process (not just that the dict is correct) -- a subprocess that
    checks its OWN environment for the override and exits 0 only if it is
    there, so a `Runner.run` that dropped `extra_env` on the floor would fail
    here rather than only in a real `west update`."""
    runner = bootstrap_cmd.Runner(json=True)
    probe = [
        sys.executable,
        "-c",
        "import os, sys; sys.exit(0 if os.environ.get('TAN_TEST_LONGPATHS') == 'yes' else 1)",
    ]
    assert runner.run(probe, extra_env={"TAN_TEST_LONGPATHS": "yes"}) is None
    # Without it, the same probe must fail -- otherwise this test would pass
    # for the wrong reason (the variable already being set some other way).
    assert runner.run(probe) is not None


def test_the_no_pyyaml_board_scan_reads_cores_in_both_forms():
    """The frozen binary ships without PyYAML, so this fallback is THE path on
    the shipped artifact."""
    cores, top_os, sku = _scan_board_slice(
        "schema_version: 2\nsom:\n  sku: E1M-X-V2N101\ncores:\n"
        '  a55_cluster:\n    os: "off"\n  m33_sm: {}\n'
    )
    assert sku == "E1M-X-V2N101"
    assert cores == {"a55_cluster": "off", "m33_sm": None}
    assert top_os is None
    # The flow form on one line, and a v1 top-level `os:`.
    flow, top, _ = _scan_board_slice('os: baremetal\ncores:\n  m33: {os: "off"}\n')
    assert flow == {"m33": "off"} and top == "baremetal"


def test_a_relocated_checkout_rebases_only_paths_that_were_under_it():
    """A project nowhere near the checkout is returned unchanged, never
    force-rebased."""
    assert _rebase("/old/alp-sdk/examples/x", "/old/alp-sdk", "/new/alp-sdk") == (
        "/new/alp-sdk/examples/x"
    )
    assert _rebase("/old/alp-sdk", "/old/alp-sdk", "/new/alp-sdk") == "/new/alp-sdk"
    assert _rebase("/elsewhere/proj", "/old/alp-sdk", "/new/alp-sdk") == "/elsewhere/proj"
    # A sibling whose name merely STARTS with the old root must not be rebased.
    assert _rebase("/old/alp-sdk-other", "/old/alp-sdk", "/new") == "/old/alp-sdk-other"
    assert _rebase(None, "/a", "/b") is None


# ---------------------------------------------------------------------------
# tan-cli#285: exit 0 with a knowingly incomplete venv; the Python floor with
# no ceiling; the hidapi remediation hint naming the wrong OS.
# ---------------------------------------------------------------------------


def test_completion_verdict_matches_the_rust_oracles_wording_and_escape_hatch():
    """Ported from the Rust oracle's `verdict()`
    (`crates/tan-cli/src/commands/bootstrap/mod.rs`), not re-derived
    (tan-cli#220 / tan-cli#285): the wording, the named failures and the
    `--allow-partial` escape hatch are the ALREADY-SHIPPED, ALREADY TAGGED
    (`CHANGELOG.md` `[0.5.0-rc1]`) contract -- a second, independently-worded
    rule for the same decision is exactly how this port's closing line and
    its escape hatch would drift from the one already-integrated consumers
    expect."""
    lines, ok = completion_verdict([], False)
    assert lines == ["bootstrap: complete."] and ok is True
    lines, ok = completion_verdict([], True)
    assert lines == ["bootstrap: complete."] and ok is True

    lines, ok = completion_verdict(["zephyr-requirements"], False)
    assert ok is False
    joined = "\n".join(lines)
    assert "bootstrap: complete." not in joined
    assert "INCOMPLETE" in joined
    assert "zephyr-requirements" in joined
    assert "--allow-partial" in joined

    # Every blocking warning is named, not just the first -- a customer
    # fixing one and re-running should not discover the next one at a time.
    lines, _ok = completion_verdict(["zephyr-requirements", "sdk-extras"], False)
    joined = "\n".join(lines)
    assert "zephyr-requirements" in joined and "sdk-extras" in joined

    # The escape still reports success -- and still says what is missing, so
    # `--allow-partial` is an informed choice rather than a mute override.
    lines, ok = completion_verdict(["sdk-extras"], True)
    assert ok is True
    joined = "\n".join(lines)
    assert "bootstrap: complete." in joined
    assert "sdk-extras" in joined


def test_python_ceiling_warns_without_ever_refusing_a_newer_host():
    """The floor refuses (a GUARANTEED failure downstream in Zephyr's CMake);
    the ceiling only ever warns -- a hard refusal here would block a host that
    was going to bootstrap a perfectly complete venv, the same defect class the
    floor fix exists to close, mirrored onto the other edge. Lowering
    `PYTHON_CEILING_KNOWN_GOOD` to the actually-measured value does not change
    that: it only widens which hosts get told, never which ones can proceed."""
    from tan.core.bootstrap import PYTHON_CEILING_KNOWN_GOOD

    # (3, 12): what CI actually pins and measures -- not a guessed value.
    assert PYTHON_CEILING_KNOWN_GOOD == (3, 12)

    assert python_ceiling_warning(PYTHON_CEILING_KNOWN_GOOD, "/ws/.venv") is None
    older = (PYTHON_CEILING_KNOWN_GOOD[0], PYTHON_CEILING_KNOWN_GOOD[1] - 1)
    assert python_ceiling_warning(older, "/ws/.venv") is None

    newer = (PYTHON_CEILING_KNOWN_GOOD[0], PYTHON_CEILING_KNOWN_GOOD[1] + 1)
    result = python_ceiling_warning(newer, "/ws/.venv")
    assert result is not None
    code, message = result
    assert code == "python-newer-than-verified"
    assert f"{newer[0]}.{newer[1]}" in message
    assert "hidapi" in message
    assert "Not refused" in message
    # The remedy must be one that actually works: a REUSED venv keeps the
    # interpreter that created it, so "install another Python 3" alone does
    # nothing -- the message must point at deleting the venv (there is no
    # --recreate-venv) and, on Windows, choosing the interpreter explicitly.
    assert "/ws/.venv" in message
    assert "delete" in message
    assert "no --recreate-venv" in message
    assert "installing another Python 3 alongside this one does nothing" in message
    assert "Windows" in message


def test_venv_python_version_probes_the_real_interpreter_not_the_host(tmp_path):
    """`ensure_venv` may REUSE an existing venv built by a different
    interpreter than whatever `host_python` resolves today; pip installs run
    inside the VENV's own interpreter, so the ceiling check must probe that
    one, not `host_python.version` (tan-cli#285)."""
    venv = bootstrap_cmd.VenvBin(Path(sys.executable), Path(sys.executable), "bin")
    runner = bootstrap_cmd.Runner(json=True)
    probed = bootstrap_cmd._venv_python_version(venv, runner, fallback=(1, 0))
    assert probed == tuple(sys.version_info[:2])

    # Falls back when the probe cannot even be spawned -- a venv that does
    # not exist on disk (or, in real use, a genuinely broken one; the real
    # pip install a moment later surfaces its own error).
    missing = bootstrap_cmd.VenvBin(tmp_path / "nope", tmp_path / "nope", "bin")
    assert bootstrap_cmd._venv_python_version(missing, runner, fallback=(9, 9)) == (9, 9)

    # `--dry-run`: nothing was actually written to disk to probe.
    dry = bootstrap_cmd.Runner(json=True, dry_run=True)
    assert bootstrap_cmd._venv_python_version(venv, dry, fallback=(9, 9)) == (9, 9)


def test_zephyr_requirements_hint_is_gated_on_the_real_host():
    """The Windows hint names the MSVC linker error actually measured
    (`LNK1104`) and never the Linux `apt-get` line; the Linux hint stays what
    was verified on a stock ubuntu-24.04 runner. Neither host gets the other's
    unactionable, misdirecting command."""
    windows = zephyr_requirements_hint(WINDOWS)
    assert "LNK1104" in windows
    assert "apt-get" not in windows

    linux = zephyr_requirements_hint(LINUX)
    assert "apt-get" in linux
    assert "LNK1104" not in linux

    # macOS/other: no GUESSED package name -- that would just repeat the
    # wrong-OS defect against a different OS.
    other = zephyr_requirements_hint(MACOS)
    assert "apt-get" not in other
    assert "LNK1104" not in other


@pytest.mark.parametrize(
    ("forced_host", "expect_fragment", "forbid_fragment"),
    [
        (WINDOWS, "LNK1104", "apt-get"),
        (LINUX, "apt-get", "LNK1104"),
    ],
)
def test_a_pip_phase_problem_blocks_complete_and_the_zero_exit(
    monkeypatch, tmp_path, forced_host, expect_fragment, forbid_fragment
):
    """The reported defect, reproduced without a real pip/network install: the
    Zephyr requirements step reports a problem (hidapi's wheel build, as
    measured), and the run must not print `bootstrap: complete.` or exit 0 --
    and the warning must carry THIS host's remedy, not always Linux's.

    The issue must also be `severity: "error"`, not `"warning"` (tan-cli#285):
    an envelope that exits non-zero while every issue in it says `warning`
    invites a consumer to treat the whole thing as advisory."""
    outcome = _run_with_a_blocked_zephyr_requirements_install(
        monkeypatch, tmp_path, forced_host, allow_partial=False
    )

    assert outcome.exit_code == ExitCode.RUNTIME_FAILURE
    assert not any(line == "bootstrap: complete." for line in outcome.text)
    assert any("INCOMPLETE" in line for line in outcome.text)
    problems = [i for i in outcome.issues if i.code == "bootstrap.zephyr-requirements"]
    assert len(problems) == 1
    assert problems[0].severity == "error"
    assert expect_fragment in problems[0].message
    assert forbid_fragment not in problems[0].message
    assert "the venv is incomplete" in problems[0].message
    # tan-cli#285: the captured pip tail rides along in the SAME message, so
    # "look in the captured pip output" (the hint's own wording) names
    # something actually present, including in `--format json` where there
    # is no terminal output to look back at.
    assert "Captured output:" in problems[0].message


def test_allow_partial_reports_success_but_keeps_the_issue_a_warning(monkeypatch, tmp_path):
    """`--allow-partial` is an informed choice, not a mute override (tan-cli
    #220 / #285): the run reports success, but the issue stays `warning` (the
    customer was told and chose to proceed) and the closing text still names
    what did not install."""
    outcome = _run_with_a_blocked_zephyr_requirements_install(
        monkeypatch, tmp_path, WINDOWS, allow_partial=True
    )

    assert outcome.exit_code == ExitCode.SUCCESS
    assert any(line == "bootstrap: complete." for line in outcome.text)
    assert any("zephyr-requirements" in line for line in outcome.text)
    problems = [i for i in outcome.issues if i.code == "bootstrap.zephyr-requirements"]
    assert len(problems) == 1
    assert problems[0].severity == "warning"


def _run_with_a_blocked_zephyr_requirements_install(
    monkeypatch, tmp_path, forced_host, *, allow_partial: bool
):
    """Shared setup: a hermetic `_run` where the Zephyr requirements pip
    install reports a failure (hidapi's wheel build, as measured), without a
    real pip/network install."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    workspace_dir = sdk.parent
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    requirements = workspace_dir / facts.zephyr_requirements_path
    # The captured tail now rides along in the issue message (tan-cli#285),
    # so it must actually vary by host like a real failure would -- a fixture
    # that always names the Windows linker error would make the Linux case's
    # "never LNK1104" assertion fail on the appended tail, not the hint.
    captured_detail = (
        "LINK : fatal error LNK1104: cannot open file 'python314.lib'"
        if forced_host == WINDOWS
        else "error: pkg-config package 'libusb-1.0 >= 1.0.9' not found"
    )

    def fake_run(self, argv, cwd=None):  # noqa: ARG001 -- matches Runner.run's shape
        if "-r" in argv and str(requirements) in argv:
            return captured_detail
        if "venv" in argv:
            # Stand in for a real `west update` having fetched the Zephyr tree
            # (skipped here via `--no-west`) -- just the one file `pip_phase`
            # reads. Created lazily, on the FIRST spawned command, which is
            # always after the workspace-parent guard's directory-listing
            # check: creating it up front would add an extra top-level entry
            # under the workspace dir and trip that guard instead.
            requirements.parent.mkdir(parents=True, exist_ok=True)
            requirements.write_text("hidapi\n", encoding="utf-8")
        return None

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", fake_run)
    monkeypatch.setattr(bootstrap_cmd, "detect_host_os", lambda _platform: forced_host)
    monkeypatch.setattr(
        bootstrap_cmd, "probe_host_python", lambda _floor: HostPython((sys.executable,), (3, 12))
    )

    outcome, _project, _sdk_info = bootstrap_cmd._run(
        project=str(workspace_dir),
        board_yaml=None,
        sdk_root_flag=str(sdk),
        no_pip=False,
        no_west=True,
        print_env=False,
        allow_partial=allow_partial,
        workspace=None,
        dry_run=False,
        json_mode=True,
    )
    return outcome


# ---------------------------------------------------------------------------
# tan-cli#495 -- resolution order and reporting that matches what was DONE
# ---------------------------------------------------------------------------


def _foreign_zephyr_tree(root: Path, *, python_floor: str) -> Path:
    """A west topdir whose manifest is NOT this SDK and whose Zephyr is not the
    pin -- `decide_workspace_reuse`'s INCOMPATIBLE, i.e. a tree
    `_select_workspace` warns about and then IGNORES. Returns its `zephyr/`."""
    zephyr = root / "zephyr"
    (zephyr / "cmake" / "modules").mkdir(parents=True)
    (zephyr / "VERSION").write_text(
        "VERSION_MAJOR = 4\nVERSION_MINOR = 5\nPATCHLEVEL = 0\nEXTRAVERSION =\n",
        encoding="utf-8",
    )
    (zephyr / "cmake" / "modules" / "python.cmake").write_text(
        f"set(PYTHON_MINIMUM_REQUIRED {python_floor})\n", encoding="utf-8"
    )
    (root / ".west").mkdir()
    (root / ".west" / "config").write_text(
        "[manifest]\npath = someother\nfile = west.yml\n", encoding="utf-8"
    )
    return zephyr


def test_a_zephyr_base_about_to_be_discarded_does_not_set_the_python_floor(tmp_path):
    """**tan-cli#495 defect 2.** `resolve_python_floor` read `$ZEPHYR_BASE`
    unconditionally, so a tree `_select_workspace` was about to REJECT still
    decided the enforced floor.

    `3.99` is deliberately unreachable on every host, so this pins the
    REFUSAL, not a version coincidence: before the fix `tan bootstrap` exited
    1 with `bootstrap.python-too-old`, before `_select_workspace` ever ran --
    so the envelope never even said the tree was being discarded, and the
    message's remedy ("install a newer Python") named the wrong fix. The real
    one is `unset ZEPHYR_BASE`, which is what the run now does for the
    customer: it warns `zephyr-base-incompatible` and bootstraps its own
    workspace.
    """
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    zephyr = _foreign_zephyr_tree(tmp_path / "foreign", python_floor="3.99")

    env = envelope(
        run_tan(
            "bootstrap", "--dry-run", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
            env_extra={"ZEPHYR_BASE": str(zephyr)},
        )
    )

    assert env["exitCode"] == 0 and env["ok"] is True, env["issues"]
    assert "bootstrap.python-too-old" not in codes(env)
    # The tree IS reported as discarded -- the fact the refusal used to hide.
    # (That warning names the tree, deliberately; nothing else may.)
    assert "bootstrap.zephyr-base-incompatible" in codes(env)
    # No issue attributes the enforced FLOOR to a `python.cmake` inside it --
    # `bootstrap.python-floor-skew` used to, in the same envelope as the
    # warning above saying that tree was being ignored.
    floor_source = str(zephyr / "cmake" / "modules" / "python.cmake")
    for issue in env["issues"]:
        assert floor_source not in issue["message"], issue


def test_an_adopted_zephyr_base_still_sets_the_python_floor(tmp_path):
    """The other half of defect 2, so the fix is a narrowing and not a
    deletion: a tree this run REUSES is exactly the Zephyr whose CMake will
    enforce the floor at build time, so its `python.cmake` must still win.
    Without this, the same edit could have dropped `$ZEPHYR_BASE` entirely and
    reintroduced tan-cli#300's silent gap."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    other = tmp_path / "other"
    zephyr = other / "zephyr"
    (zephyr / "cmake" / "modules").mkdir(parents=True)
    (zephyr / "VERSION").write_text(
        "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 1\nEXTRAVERSION =\n",
        encoding="utf-8",
    )
    (zephyr / "cmake" / "modules" / "python.cmake").write_text(
        "set(PYTHON_MINIMUM_REQUIRED 3.99)\n", encoding="utf-8"
    )
    (other / ".west").mkdir()
    (other / ".west" / "config").write_text(
        f"[manifest]\npath = ../{sdk.parent.name}/{sdk.name}\nfile = west.yml\n",
        encoding="utf-8",
    )

    env = envelope(
        run_tan(
            "bootstrap", "--dry-run", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
            env_extra={"ZEPHYR_BASE": str(zephyr)},
        )
    )

    assert env["exitCode"] == 1 and "bootstrap.python-too-old" in codes(env), env["issues"]
    refusal = next(i for i in env["issues"] if i["code"] == "bootstrap.python-too-old")
    assert "3.99" in refusal["message"]


def test_the_venv_a_rolled_back_run_left_behind_does_not_block_the_retry(tmp_path):
    """**tan-cli#495 defect 3.** The occupied-target check tested raw
    non-emptiness, so the `.venv` `rollback_relocation_after` DELIBERATELY
    leaves under the auto-relocation target counted as "content of its own".

    The documented quickstart -- the `tan` binary beside a fresh `alp-sdk`
    clone -- therefore stopped being retryable after its single most likely
    transient failure (the network dropping during `pip install west`): run 2
    refused with `bootstrap.workspace-guard` / exit 2 and told the customer to
    hand-delete a directory tan itself had created seconds earlier. The
    module's own foreign-content predicate already exempts the checkout name
    and the venv dir; this uses it.
    """
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    (sdk.parent / "unrelated.txt").write_text("x", encoding="utf-8")
    target = sdk.parent / "alp-workspace"
    # Exactly what run 1's rollback leaves: the venv, and nothing else.
    (target / ".venv" / "bin").mkdir(parents=True)

    env = envelope(
        run_tan(
            "bootstrap", "--dry-run", "--no-west", "--no-pip", "--format", "json",
            "--sdk-root", str(sdk), cwd=sdk.parent,
        )
    )

    assert env["exitCode"] == 0, env["issues"]
    assert "bootstrap.workspace-guard" not in codes(env)
    assert "bootstrap.workspace-relocated" in codes(env)
    assert env["data"]["workspaceDir"] == bootstrap_cmd._native(str(target))
    # `--dry-run` still moves nothing.
    assert sdk.exists() and not (target / sdk.name).exists()


def test_genuinely_foreign_content_in_the_target_still_refuses(tmp_path):
    """Defect 3's negative control, held at the same severity as before: the
    exemption is `checkout_name` + the venv dir ONLY. A `.west` workspace under
    the target is NOT exempt -- proving ownership by its manifest's last path
    component alone passes for any unrelated workspace whose checkout is also
    named `alp-sdk`, which is most of them, so an earlier cut of this fix was
    reverted rather than shipped."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    (sdk.parent / "unrelated.txt").write_text("x", encoding="utf-8")
    target = sdk.parent / "alp-workspace"
    (target / ".west").mkdir(parents=True)
    (target / ".west" / "config").write_text(
        "[manifest]\npath = alp-sdk\nfile = west.yml\n", encoding="utf-8"
    )

    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json",
        "--sdk-root", str(sdk), cwd=sdk.parent,
    )
    env = envelope(proc)

    assert proc.returncode == 2
    assert codes(env) == ["bootstrap.workspace-guard"]
    assert sdk.exists() and not (target / sdk.name).exists()


def test_a_print_env_refusal_goes_to_stderr_and_leaves_stdout_empty(tmp_path):
    """**tan-cli#495 defect 4.** The terminal dispatch keyed on the `--print-env`
    FLAG, not on what happened, so every refusal computed BEFORE the
    short-circuit was written to STDOUT with stderr empty: the customer's
    terminal showed nothing at rc=2 while `env.sh` received refusal PROSE
    carrying `(` and backticks -- not shell, and backticks are command
    substitution to anything that does parse it.

    Measured on the frozen oracle (`target/debug/tan bootstrap --print-env`,
    no resolvable SDK): rc=2, stdout EMPTY, the refusal on stderr. This pins
    that split.
    """
    empty = tmp_path / "emptyproj"
    empty.mkdir()
    (tmp_path / "fake-home").mkdir(exist_ok=True)

    proc = run_tan("bootstrap", "--print-env", cwd=empty)

    assert proc.returncode == 2
    assert proc.stdout == "", f"refusal prose on stdout: {proc.stdout!r}"
    assert "alp-sdk root is unresolved" in proc.stderr


def test_print_env_still_writes_its_env_block_to_stdout(tmp_path):
    """Defect 4's positive control. The success gate must not have moved the
    whole command onto stderr: `tan bootstrap --print-env > env.sh` is the
    documented flow, and on stderr the redirect target is empty while the
    lines still appear on the terminal -- it looks like it worked and wrote
    nothing."""
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])

    proc = run_tan("bootstrap", "--print-env", "--sdk-root", str(sdk), cwd=sdk.parent)

    assert proc.returncode == 0, proc.stderr
    assert "Activate the workspace venv" in proc.stdout
    assert "ZEPHYR_BASE" in proc.stdout


def test_the_long_paths_override_is_appended_to_the_callers_git_config_chain(monkeypatch):
    """**tan-cli#495 defect 5.** `Runner._env` merged `FORCE_GIT_LONG_PATHS_ENV`
    with a bare `dict.update`, which claims index 0 and resets
    `GIT_CONFIG_COUNT` to `1`.

    A corporate host or CI runner exporting git's own documented ad hoc
    override -- a proxy at index 0 and a mirror `insteadOf` at index 1 -- lost
    BOTH: index 0 overwritten, index 1 stranded past the reset COUNT. Every
    `git clone`/`git fetch` inside `west update` then went direct, and tan died
    with `west update failed` naming github.com rather than the setting it
    deleted.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.proxy")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "http://proxy.corp:3128")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "url.https://mirror.corp/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "https://github.com/")

    env = bootstrap_cmd.Runner(json=True)._env(bootstrap_cmd.FORCE_GIT_LONG_PATHS_ENV)

    assert env is not None
    # The caller's two survive, verbatim, at their own indices...
    assert env["GIT_CONFIG_KEY_0"] == "http.proxy"
    assert env["GIT_CONFIG_VALUE_0"] == "http://proxy.corp:3128"
    assert env["GIT_CONFIG_KEY_1"] == "url.https://mirror.corp/.insteadOf"
    assert env["GIT_CONFIG_VALUE_1"] == "https://github.com/"
    # ...and tan's lands after them, with the COUNT raised to cover all three.
    assert env["GIT_CONFIG_KEY_2"] == "core.longpaths"
    assert env["GIT_CONFIG_VALUE_2"] == "true"
    assert env["GIT_CONFIG_COUNT"] == "3"


def test_the_long_paths_override_claims_slot_zero_when_the_host_has_no_chain(monkeypatch):
    """The ordinary host: nothing to preserve, so the override lands at 0 and
    the shipped `FORCE_GIT_LONG_PATHS_ENV` triple is what git sees -- byte for
    byte what tan-cli#306 established. A corrupt inherited COUNT (git itself
    fatals on one: "bogus count in GIT_CONFIG_COUNT") is treated the same way,
    rather than raising out of the step whose job is to make `west update`
    work."""
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.delenv("GIT_CONFIG_KEY_0", raising=False)
    monkeypatch.delenv("GIT_CONFIG_VALUE_0", raising=False)
    clean = bootstrap_cmd.Runner(json=True)._env(bootstrap_cmd.FORCE_GIT_LONG_PATHS_ENV)
    assert clean is not None
    for key, value in bootstrap_cmd.FORCE_GIT_LONG_PATHS_ENV.items():
        assert clean[key] == value

    monkeypatch.setenv("GIT_CONFIG_COUNT", "not-a-number")
    bogus = bootstrap_cmd.Runner(json=True)._env(bootstrap_cmd.FORCE_GIT_LONG_PATHS_ENV)
    assert bogus is not None
    assert bogus["GIT_CONFIG_COUNT"] == "1"
    assert bogus["GIT_CONFIG_KEY_0"] == "core.longpaths"


def test_real_git_reads_every_override_in_the_chain_tan_hands_it(monkeypatch, tmp_path):
    """The dict assertions above prove the shape; this proves GIT agrees, with
    a real `git config --get-all` child reading the environment `Runner._env`
    built. The reset COUNT was the half of defect 5 a shape-only check misses
    -- `GIT_CONFIG_KEY_1` stayed in the environment, simply unread."""
    git = shutil.which("git")
    if git is None:  # pragma: no cover -- git is a prerequisite of this repo
        pytest.skip("git is not on PATH")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://mirror.corp/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")

    env = bootstrap_cmd.Runner(json=True)._env(bootstrap_cmd.FORCE_GIT_LONG_PATHS_ENV)

    def read(name):
        out = subprocess.run(
            [git, "config", "--get-all", name],
            capture_output=True, text=True, cwd=str(tmp_path), env=env,
        )
        return out.stdout.strip()

    assert read("url.https://mirror.corp/.insteadOf") == "https://github.com/"
    assert read("core.longpaths") == "true"


def test_print_env_names_the_venv_layout_that_exists_not_the_hosts(tmp_path):
    """**tan-cli#495 defect 7.** `--print-env` rendered the activation hint from
    the HOST's bin-dir name, so a workspace `.venv` created under
    git-bash/Windows (a `Scripts/` layout) and then bootstrapped from a POSIX
    host printed `source "<venv>/bin/activate"` -- a path that does not exist
    -- while the SAME run's `Next steps:` block, which reads the
    existence-derived `venv.bin_dir`, printed `Scripts`. The mirror case (a
    `bin/` venv read from Windows) is the same defect the other way round;
    `Workspace.venv_bin()` is the resolver both now share.

    Severity low, and deliberately still low: the hint is emitted as a COMMENT
    (`#   source "..."`), so a redirected `env.sh` sources cleanly either way.
    The damage is confined to a human copy-pasting it.
    """
    sdk = make_sdk(tmp_path, tools=[PRESENT_TOOL])
    other_layout = "Scripts" if os.name != "nt" else "bin"
    (sdk.parent / ".venv" / other_layout).mkdir(parents=True)

    proc = run_tan("bootstrap", "--print-env", "--sdk-root", str(sdk), cwd=sdk.parent)

    assert proc.returncode == 0, proc.stderr
    # The rendered command line, not the heading above it: `#   source "..."`
    # on POSIX, `#   & "...Activate.ps1"` on Windows.
    hint = next(line for line in proc.stdout.splitlines() if line.startswith("#   "))
    assert f".venv/{other_layout}/" in hint.replace("\\", "/"), hint


def test_a_relocation_refusal_keeps_the_warnings_the_run_already_recorded(tmp_path):
    """tan-cli#491 defect 10. The relocation-failure return passed a literal
    `[]` to `_fatal` instead of `log.take_issues()`, so every warning recorded
    before that point was dropped from the envelope: the refusal carried
    `bootstrap.failed` alone.

    `bootstrap.python-floor-skew` is the warning used here because it fires on
    EVERY run against the shipped manifest (declared 3.10, effective floor
    3.12) -- the control case above pins that, so its absence here can only be
    the drop, never "this workspace happens not to warn".

    `--dry-run` so nothing is moved; the guard runs and refuses either way, and
    `<workspace>/alp-sdk` already existing is what makes `relocate_checkout`
    return its error."""
    sdk = make_sdk(tmp_path / "src" / "alp-sdk", tools=[PRESENT_TOOL])
    workspace = tmp_path / "ws"
    (workspace / "alp-sdk").mkdir(parents=True)

    env = envelope(
        run_tan(
            "bootstrap", "--no-west", "--no-pip", "--dry-run", "--format", "json",
            "--sdk-root", str(sdk), "--workspace", str(workspace),
            cwd=tmp_path,
        )
    )
    assert env["exitCode"] == 1 and env["ok"] is False, env
    assert "bootstrap.failed" in codes(env), env
    assert "already exists; refusing to relocate" in (
        [i["message"] for i in env["issues"] if i["code"] == "bootstrap.failed"][0]
    )
    skew = [i for i in env["issues"] if i["code"] == "bootstrap.python-floor-skew"]
    assert len(skew) == 1 and skew[0]["severity"] == "warning", env
    # Order is the contract `_fatal` states: the run's warnings, then the
    # `bootstrap.failed` error that ended it.
    assert codes(env)[-1] == "bootstrap.failed", env
