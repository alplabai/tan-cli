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
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.commands import doctor_cmd
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
)
from tan.core.bootstrap import (
    INCOMPATIBLE,
    LINUX,
    MACOS,
    MANIFEST_MISMATCH,
    OTHER,
    REUSE,
    STALE,
    WINDOWS,
    BootstrapManifestError,
    Tokens,
    capture_tail,
    decide_workspace_reuse,
    detect_host_os,
    die,
    fallback_facts,
    get_manifest_path,
    hint_line,
    in_play_runtimes,
    next_steps_block,
    optional_libs_block,
    parent_needs_workspace_guard,
    parse_bootstrap_manifest,
    parse_west_zephyr_pin,
    parse_zephyr_version_file,
    posix_refusal,
    posix_venv_unusable,
    print_env_block,
    python_floor_skew_warning,
    python_too_old,
    reported_missing,
    resolve_workspace_target,
    resolve_zephyr_pin,
    set_manifest_path,
    windows_python_not_runnable,
    windows_refusal,
    yocto_gate,
)

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
    worse than either verdict alone."""
    zephyr = tmp_path / "zephyr"
    (zephyr / "cmake" / "modules").mkdir(parents=True)
    (zephyr / "cmake" / "modules" / "python.cmake").write_text(
        "set(PYTHON_MINIMUM_REQUIRED 3.14)\n", encoding="utf-8"
    )
    monkeypatch.setenv("ZEPHYR_BASE", str(zephyr))

    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    floor = resolve_python_floor(facts)
    doctor_floor, doctor_source = doctor_cmd.zephyr_python_floor(str(zephyr))

    # Read from the real file on the customer's machine, so a Zephyr bump raises
    # the floor with no tan release.
    assert floor.effective == (3, 14) == doctor_floor
    assert floor.source == doctor_source
    assert floor.manifest == (3, 10)


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
        (3, 10), floor.effective, facts.install_for_host(LINUX),
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


def test_the_skew_case_suppresses_the_manifests_own_install_command():
    """`sudo apt-get install -y python3` installs 3.10 on Ubuntu 22.04 -- the
    exact version being refused. Printing the manifest's command in the skew case
    would send the customer round a loop, so it is dropped and the prose carries
    the real remedy."""
    install = parse_bootstrap_manifest(REAL_MANIFEST).install_for_host(LINUX)
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
    ("mutation", "fragment"),
    [
        ('"schemaVersion": 99', "schemaVersion 99"),
        ('"pythonMinVersion": "three.ten"', "is not MAJOR.MINOR"),
        ('"dirName": "../escape"', "is not a plain relative path"),
    ],
)
def test_a_present_but_unusable_manifest_is_fatal_never_a_silent_fallback(
    mutation, fragment, tmp_path
):
    """Falling back HERE would re-introduce hand-ported behaviour against an SDK
    that explicitly declared something else. Diffed byte-identical against the
    oracle on all three."""
    key = mutation.split(":")[0]
    original = [line for line in REAL_MANIFEST.splitlines() if key in line][0].strip().rstrip(",")
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


def test_the_workspace_parent_guard_refuses_and_leaves_nothing_on_disk(tmp_path):
    """`west init -l` forces the topdir to be the checkout's PARENT, so a clone
    into `~/Downloads` sprays zephyr/modules/.west/venv there where no
    `.gitignore` can reach it."""
    sdk = make_sdk(tmp_path)
    (sdk.parent / "unrelated.txt").write_text("x", encoding="utf-8")
    before = sorted(p.name for p in sdk.parent.iterdir())

    proc = run_tan(
        "bootstrap", "--no-west", "--no-pip", "--format", "json",
        "--sdk-root", str(sdk), cwd=sdk.parent,
    )
    env = envelope(proc)
    assert proc.returncode == 2
    assert codes(env) == ["bootstrap.workspace-guard"]
    message = env["issues"][0]["message"]
    # Names BOTH remedies: this refusal is inherited by `tan build` and `tan
    # doctor --build --fix`, neither of which has a `--workspace` of its own.
    assert "tan bootstrap --workspace <path>" in message
    assert "dedicated directory" in message
    assert sorted(p.name for p in sdk.parent.iterdir()) == before


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
    holds the two in step."""
    manifest = parse_bootstrap_manifest(REAL_MANIFEST)
    fallback = fallback_facts(manifest.python_min_version)
    for field in vars(manifest):
        if field == "from_manifest":
            continue
        assert getattr(fallback, field) == getattr(manifest, field), field


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
    assert facts.install_for_host(LINUX)["cmake"] == "sudo apt-get install -y cmake"
    assert facts.install_for_host(MACOS)["cmake"] == "brew install cmake"
    assert facts.install_for_host(WINDOWS)["cmake"] == "winget install -e --id Kitware.CMake"
    # A POSIX host that is neither: no manifest entry, so `null` -- never a
    # wrong-OS command.
    assert facts.install_for_host(OTHER) == {}


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


def test_the_posix_refusal_stays_one_line_with_two_spaces_before_install():
    """`bootstrap.sh`'s wording, byte-for-byte. It names the tools and nothing
    else; the per-tool commands travel in the STRUCTURED half only."""
    install = parse_bootstrap_manifest(REAL_MANIFEST).install_for_host(LINUX)
    refusal = posix_refusal(["cmake", "ninja"], install)
    assert refusal.lines == ("Missing required tools: cmake ninja.  Install them and re-run.",)
    assert [m.command for m in refusal.missing] == [
        "sudo apt-get install -y cmake", "sudo apt-get install -y ninja-build"
    ]


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
    facts = parse_bootstrap_manifest(REAL_MANIFEST)
    linux = optional_libs_block(facts, LINUX)
    assert linux[1] == "bootstrap: Optional native libraries unlock the Yocto-side backends:"
    assert "  libmosquitto-dev  -> alp_mqtt_* (cleartext + TLS)" in linux
    assert linux[-1].startswith("  sudo apt-get install -y libmosquitto-dev")
    assert "brew install mosquitto pkg-config" in optional_libs_block(facts, MACOS)[-1]
    # `OTHER` has no hint at all -- just the not-detected line.
    assert optional_libs_block(facts, OTHER)[-1] == (
        "  (OS not auto-detected; see docs/testing.md)"
    )


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
