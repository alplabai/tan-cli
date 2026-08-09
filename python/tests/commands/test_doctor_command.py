# SPDX-License-Identifier: Apache-2.0
"""``tan doctor`` -- the host-readiness probe, and the Python-floor gap it exists
to close.

Two halves, deliberately:

* **Pure verdicts, called directly.** Every ``*_check`` in ``doctor_cmd`` takes
  already-probed facts and returns a ``Check``. That is where the arithmetic
  lives (which floor wins, which issue code a verdict carries), and it is
  testable without a host that happens to be misconfigured. Driving these
  through a subprocess would mean asserting against whatever ``python3``/
  ``west``/``JLinkExe`` this particular developer machine has, which certifies
  nothing.
* **Framing, driven as a real subprocess.** One JSON document on stdout, the
  exit code, and the guarantee that probing a hostile environment cannot escape
  as a traceback. An in-process call exercises none of those -- same reasoning as
  ``test_build_command.py``.

The load-bearing case is ``test_python_3_10_fails_although_the_manifest_allows
_it``: ``metadata/bootstrap.json`` declares ``pythonMinVersion: "3.10"``,
``zephyr/cmake/modules/python.cmake`` sets ``PYTHON_MINIMUM_REQUIRED 3.12``, and
tan's own POSIX bootstrap branch "cannot fail on version". Ubuntu 22.04 ships
``python3`` = 3.10, so bootstrap succeeds, doctor said Pass, and the customer's
first build died inside Zephyr's CMake configure pointing at Zephyr.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands import doctor_cmd
from tan.core.bootstrap import venv_layout, workspace_sdk_record_json

#: ``python/`` -- pinned onto the child's PYTHONPATH so ``python -m tan``
#: resolves from a scratch cwd without a ``pip install``.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

#: The worktree root -- one level above ``python/`` -- so ``contract/`` fixtures
#: can be read without re-typing a repo-relative path in every test.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: In-process CLI driver, used ONLY for the `--fix` wiring tests below: they
#: need to monkeypatch `doctor_cmd`'s own module attributes (`can_prompt`,
#: `_collect`, `run_fix`) and observe the effect, which a real `run_tan`
#: subprocess cannot do (the child is a different process, and its stdin/
#: stderr are captured pipes -- never a tty -- so `can_prompt` is always
#: `False` there regardless of flags; see `test_doctor_fix_interactive_with_
#: nothing_resolvable_is_a_safe_no_op`'s own history for the same limit).
runner = CliRunner()


def _plant_zephyr_sdk(root: Path) -> None:
    """Create the one file ``_zephyr_sdk_root_valid`` actually probes, so a
    test SDK root is genuine rather than merely present -- the distinction
    finding 1 (tan-cli#286 second pass) exists to enforce.

    Builds the path from ``doctor_cmd.ZEPHYR_SDK_TOOLCHAIN_DIR`` -- the SAME
    constant the production probe reads -- rather than a second, independently
    spelled literal here: tan-cli#286 third pass's blocker was exactly that,
    this fixture and the probe each hardcoding the layout by hand and
    silently agreeing on the WRONG one, so 77 tests passed over a broken
    probe. One constant, two readers, cannot drift apart the same way again.

    The exe suffix reads ``doctor_cmd.os.name``, never this test module's own
    (real) ``os`` -- ``doctor_cmd.os`` is the name a test rebinds to flip the
    production platform branch (see ``_FixedOsName`` below), and reading the
    real module here is finding 2 (tan-cli#286 third pass): it planted the
    POSIX name while a faked-``nt`` probe looked for the ``.exe`` suffix,
    failing 2 of 3 CI legs.
    """
    exe = "arm-zephyr-eabi-gcc.exe" if doctor_cmd.os.name == "nt" else "arm-zephyr-eabi-gcc"
    bin_dir = root.joinpath(*doctor_cmd.ZEPHYR_SDK_TOOLCHAIN_DIR)
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / exe).write_text("", encoding="utf-8")


def run_tan(*argv, cwd, scrub_path=False, env_extra=None):
    """Spawn the port. ``scrub_path`` empties ``PATH`` so not one probe can
    resolve -- the hostile environment doctor is supposed to survive."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
        **(env_extra or {}),
    }
    if scrub_path:
        env["PATH"] = ""
        # Both are read by the SETOOLS check; a developer machine that exports
        # them must not change what this case observes.
        env.pop("SETOOLS_DIR", None)
        env.pop("SE_UART", None)
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
    )


# --------------------------------------------------------------------------
# The bug: the effective floor, not the declared one
# --------------------------------------------------------------------------


def test_python_3_10_fails_although_the_manifest_allows_it():
    """The exact Ubuntu 22.04 shape. 3.10 clears `pythonMinVersion` and dies at
    Zephyr's CMake configure, so doctor must refuse it here."""
    check = doctor_cmd.python_check(
        found=("python3", (3, 10)), floor=(3, 12), floor_source="zephyr"
    )
    assert check.status == "fail"
    assert check.code == "bootstrap.python-too-old"
    assert "3.10" in check.detail and "3.12" in check.detail


def test_python_3_12_passes():
    check = doctor_cmd.python_check(
        found=("python3", (3, 12)), floor=(3, 12), floor_source="zephyr"
    )
    assert check.status == "pass"


def test_no_runnable_interpreter_is_its_own_frozen_code():
    check = doctor_cmd.python_check(found=None, floor=(3, 12), floor_source="zephyr")
    assert check.status == "fail"
    assert check.code == "bootstrap.python-not-runnable"


def test_zephyr_floor_is_read_from_the_real_cmake_when_the_workspace_resolves(tmp_path):
    modules = tmp_path / "cmake" / "modules"
    modules.mkdir(parents=True)
    (modules / "python.cmake").write_text(
        "include_guard(GLOBAL)\nset(PYTHON_MINIMUM_REQUIRED 3.13)\n", encoding="utf-8"
    )
    floor, source = doctor_cmd.zephyr_python_floor(str(tmp_path))
    assert floor == (3, 13)
    assert "python.cmake" in source


def test_zephyr_floor_falls_back_to_the_pinned_constant_with_no_workspace():
    floor, source = doctor_cmd.zephyr_python_floor(None)
    assert floor == doctor_cmd.ZEPHYR_PYTHON_FLOOR == (3, 12)
    assert "built-in" in source


def test_zephyr_floor_survives_an_unreadable_cmake_file(tmp_path):
    """A directory where a file is expected, undecodable bytes, no match at all
    -- every one is a fallback, never an exception."""
    modules = tmp_path / "cmake" / "modules"
    modules.mkdir(parents=True)
    (modules / "python.cmake").mkdir()
    assert doctor_cmd.zephyr_python_floor(str(tmp_path))[0] == doctor_cmd.ZEPHYR_PYTHON_FLOOR


def test_zephyr_floor_names_the_real_workspace_when_the_cmake_file_is_missing(tmp_path):
    """tan-cli#488 defect 7: a resolved Zephyr workspace mid-`west update` (a
    `.west` workspace with no `cmake/modules/python.cmake` yet --
    `zephyr_workspace_check`'s own "legitimate, working-in-progress host
    state") used to fall back to the SAME hardcoded string as "nothing
    resolved at all": "no $ZEPHYR_BASE workspace on this host to read
    `cmake/modules/python.cmake` from" -- blaming an env var this call was
    never even given (`_collect` passes the RESOLVED workspace path here,
    never `$ZEPHYR_BASE`, once one resolves) in the same envelope that
    reported the workspace resolving fine. The fallback VALUE stays correct;
    only the PROVENANCE must now name the real cause.
    """
    workspace_zephyr = tmp_path / "zephyr"
    workspace_zephyr.mkdir()
    floor, source = doctor_cmd.zephyr_python_floor(str(workspace_zephyr))
    assert floor == doctor_cmd.ZEPHYR_PYTHON_FLOOR
    assert str(workspace_zephyr) in source
    assert "could not be read" in source
    assert "no $ZEPHYR_BASE workspace" not in source


def test_zephyr_floor_names_the_real_file_when_it_has_no_parseable_pin(tmp_path):
    """Third cause: the file resolves and reads fine, but its content has no
    `PYTHON_MINIMUM_REQUIRED` this regex can find -- a future Zephyr rename,
    say. Still not "no workspace on this host"."""
    modules = tmp_path / "cmake" / "modules"
    modules.mkdir(parents=True)
    (modules / "python.cmake").write_text("include_guard(GLOBAL)\n", encoding="utf-8")
    floor, source = doctor_cmd.zephyr_python_floor(str(tmp_path))
    assert floor == doctor_cmd.ZEPHYR_PYTHON_FLOOR
    assert "python.cmake" in source
    assert "did not declare a parseable" in source
    assert "no $ZEPHYR_BASE workspace" not in source


def test_the_manifest_declaring_a_lower_floor_is_itself_reported():
    """`doctor` must not silently paper over the skew: it says which floor is
    which, and where the ACTUAL fix belongs (a newer host interpreter, not
    the manifest -- tan-cli#300)."""
    check = doctor_cmd.python_floor_skew_check(
        manifest_floor=(3, 10), effective_floor=(3, 12), effective_source="zephyr python.cmake"
    )
    assert check is not None
    assert check.status == "warn"
    assert "3.10" in check.detail and "3.12" in check.detail
    assert "metadata/bootstrap.json" in check.detail


def test_the_skew_fix_points_at_the_reverted_alp_sdk_change_not_a_manifest_raise():
    """tan-cli#300: raising `prerequisites.pythonMinVersion` was tried and
    reverted (alp-sdk#1078) -- it gates EVERY backend, not just Zephyr's, so
    it would refuse a Yocto-only/metadata-only host that builds today. The
    `fix` text must send the reader to a real interpreter, not back to the
    manifest edit that was already rejected."""
    check = doctor_cmd.python_floor_skew_check(
        manifest_floor=(3, 10), effective_floor=(3, 12), effective_source="zephyr python.cmake"
    )
    assert check is not None
    assert "alp-sdk#1078" in (check.fix or "")
    assert "tried and reverted" in (check.fix or "")
    assert "Raise `prerequisites.pythonMinVersion`" not in (check.fix or "")


def test_no_skew_check_when_the_two_floors_agree():
    assert (
        doctor_cmd.python_floor_skew_check(
            manifest_floor=(3, 12), effective_floor=(3, 12), effective_source="x"
        )
        is None
    )


# --------------------------------------------------------------------------
# _load_manifest -- the provenance verdict, as DATA
#
# `manifest_is_real` used to be re-derived by `_collect` sniffing
# `source.startswith("facts from alp-sdk")` -- a prefix match against
# `_load_manifest`'s own f-string, silently flippable by a future reword with
# nothing to catch it. `ManifestLoad.is_real` is now set once, at the read,
# and these pin the three provenances a caller can see.
# --------------------------------------------------------------------------


def _write_bootstrap_json(root: Path, prerequisites: dict) -> Path:
    path = root / "metadata" / "bootstrap.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # `schemaVersion` is required (tan-cli#488 defect 3): `_load_manifest`
    # reads it FIRST, same as `tan.core.bootstrap.parse_bootstrap_manifest`,
    # and refuses a manifest that omits or misdeclares it.
    path.write_text(
        json.dumps({"schemaVersion": 1, "prerequisites": prerequisites}), encoding="utf-8"
    )
    return path


def test_load_manifest_resolves_and_declares_the_python_floor(tmp_path):
    _write_bootstrap_json(
        tmp_path,
        {"posix": ["git", "cmake", "python3", "ninja"], "pythonMinVersion": "3.10"},
    )
    loaded = doctor_cmd._load_manifest(str(tmp_path))
    assert loaded.is_real is True
    assert loaded.error is None
    assert loaded.source.startswith("facts from alp-sdk")
    assert loaded.facts["pythonMinVersion"] == "3.10"


def test_load_manifest_resolves_but_omits_the_python_floor(tmp_path):
    """The manifest itself is real -- `is_real` must still say so -- even though
    `pythonMinVersion` is absent and `_manifest_floor_from_facts` falls back to
    `FALLBACK_PYTHON_FLOOR` for the NUMBER. Provenance and the floor value are
    two different questions."""
    _write_bootstrap_json(tmp_path, {"posix": ["git", "cmake", "python3", "ninja"]})
    loaded = doctor_cmd._load_manifest(str(tmp_path))
    assert loaded.is_real is True
    assert loaded.error is None
    assert "pythonMinVersion" not in loaded.facts
    assert doctor_cmd._manifest_floor_from_facts(loaded.facts) == doctor_cmd.FALLBACK_PYTHON_FLOOR


def test_load_manifest_with_no_sdk_resolved_is_not_real():
    loaded = doctor_cmd._load_manifest(None)
    assert loaded.is_real is False
    assert loaded.error is None
    assert "fallback" in loaded.source
    assert loaded.facts["pythonMinVersion"] == "3.10"


def test_load_manifest_refuses_a_schema_version_tan_bootstrap_would_also_refuse(
    tmp_path,
):
    """tan-cli#488 defect 3: `_load_manifest` used to accept ANY `prerequisites`
    object regardless of `schemaVersion`, so an SDK whose manifest `tan
    bootstrap` refuses outright (a schema this build does not understand)
    still yielded `hostPrerequisites: pass`, zero tools probed, and no
    `bootstrapManifest` warning -- a clean bill of health for a manifest that
    was never actually read.
    """
    path = tmp_path / "metadata" / "bootstrap.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "prerequisites": {
                    "pythonMinVersion": "3.10",
                    "posix": [],
                    "windows": [],
                    "install": {},
                },
            }
        ),
        encoding="utf-8",
    )
    loaded = doctor_cmd._load_manifest(str(tmp_path))
    assert loaded.is_real is False
    assert loaded.error is not None
    assert "schemaVersion 2" in loaded.error
    assert "supports only 1" in loaded.error
    # The fallback tool list, not an empty one this manifest declared -- a
    # manifest tan cannot understand must probe tan's OWN built-in tools,
    # never trust a shape it never actually parsed as real.
    assert loaded.facts["posix"] == ["git", "cmake", "python3", "ninja"]


def test_load_manifest_refuses_a_manifest_missing_schema_version_entirely(tmp_path):
    """Same refusal, the other input shape: `schemaVersion` absent altogether
    (not merely a different number) -- `tan.core.bootstrap.
    parse_bootstrap_manifest` (the ONE other reader of this file) raises
    identically, and `_load_manifest` must not disagree with it."""
    path = tmp_path / "metadata" / "bootstrap.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"prerequisites": {"posix": ["git", "cmake", "python3", "ninja"]}}),
        encoding="utf-8",
    )
    loaded = doctor_cmd._load_manifest(str(tmp_path))
    assert loaded.is_real is False
    assert loaded.error is not None
    assert "schemaVersion" in loaded.error


def test_collect_warns_bootstrap_manifest_and_falls_back_on_an_unsupported_schema(
    tmp_path,
):
    """The end-to-end wire: an unsupported `schemaVersion` must surface as the
    `bootstrapManifest` warning (previously silent) with the fallback tool
    list actually probed, not a silent `hostPrerequisites: pass`."""
    path = tmp_path / "metadata" / "bootstrap.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "prerequisites": {
                    "pythonMinVersion": "3.10",
                    "posix": [],
                    "windows": [],
                    "install": {},
                },
            }
        ),
        encoding="utf-8",
    )
    checks = doctor_cmd._collect(str(tmp_path))
    manifest_warn = next((c for c in checks if c.name == "bootstrapManifest"), None)
    assert manifest_warn is not None, [c.name for c in checks]
    assert manifest_warn.status == "warn"
    assert "schemaVersion 2" in manifest_warn.detail
    # tan-cli#488 defect 2: `_load_manifest`'s schemaVersion-mismatch message
    # used to end with its own period, and this check's own `f"... rejected:
    # {loaded.error}. Falling back to ..."` appends another -- rendering
    # "...outright.. Falling back to" (a double stop) in the assembled detail.
    assert ".. Falling back to" not in manifest_warn.detail, manifest_warn.detail
    assert "outright. Falling back to" in manifest_warn.detail, manifest_warn.detail


def test_collect_reports_no_manifest_read_when_none_resolves(tmp_path, monkeypatch):
    """The end-to-end wire: with no `metadata/bootstrap.json` under `sdk_root`,
    `pythonFloor` (when it fires) must say the manifest was never consulted --
    the exact case `manifest_is_real=False` exists for. `ZEPHYR_BASE` is cleared
    so the built-in Zephyr floor (3.12), which already outranks the fallback
    manifest floor (3.10), is what makes the skew fire deterministically."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    checks = doctor_cmd._collect(str(tmp_path))
    skew = next((c for c in checks if c.name == "pythonFloor"), None)
    assert skew is not None
    assert "no alp-sdk metadata/bootstrap.json was read" in skew.detail


# --------------------------------------------------------------------------
# Prerequisites, west
# --------------------------------------------------------------------------


def test_missing_prerequisites_carry_the_frozen_code_and_the_install_commands():
    check = doctor_cmd.prerequisites_check(
        checked=["git", "cmake", "python3", "ninja"],
        missing=["ninja"],
        install={"ninja": "sudo apt-get install -y ninja-build"},
        source="alp-sdk metadata/bootstrap.json",
    )
    assert check.status == "fail"
    assert check.code == "bootstrap.prerequisites-missing"
    assert "sudo apt-get install -y ninja-build" in (check.fix or "")


def test_macos_reads_its_own_prerequisites_list_not_posixs(tmp_path, monkeypatch):
    """tan-cli#488 defect 2: a stock Mac has neither `wget` nor a standalone
    `xz`, and alp-sdk v0.14.0 declares a separate `prerequisites.macos` that
    omits both for exactly that reason. Before the fix, `_collect` selected
    the tool list with `facts.get("windows" if os.name == "nt" else
    "posix")` -- no `macos` arm at all -- so a stock Mac got the POSIX list
    (including `xz`/`wget`) wholesale and `hostPrerequisites` reported `fail`
    on a host `tan bootstrap` accepts.
    """
    monkeypatch.setattr(doctor_cmd.sys, "platform", "darwin")
    # `_FixedOsName` (not a bare `os.name` mutation) -- mutating the real,
    # process-wide `os` module makes `pathlib.Path.__new__` re-pick
    # `PosixPath` on every call anywhere in the process, including `_collect`'s
    # own `Path(sdk_root)` -- and `PosixPath` cannot be instantiated on
    # Windows at all (`NotImplementedError`), which reddened this test on the
    # `windows-latest` CI leg even though the property under test is
    # platform-independent. See `_FixedOsName`'s docstring below.
    monkeypatch.setattr(doctor_cmd, "os", _FixedOsName("posix"))
    _write_bootstrap_json(
        tmp_path,
        {
            "posix": ["git", "cmake", "python3", "ninja", "xz", "wget"],
            "macos": ["git", "cmake", "python3", "ninja"],
            "windows": ["git", "cmake", "python", "ninja"],
            "pythonMinVersion": "3.10",
            "install": {},
        },
    )
    # Every tool the MACOS list names is "present"; `xz`/`wget` (posix-only)
    # would be "missing" if the posix list were used instead -- `on_path`
    # never even gets asked about them when the fix is in place.
    present = {"git", "cmake", "python3", "ninja"}
    monkeypatch.setattr(doctor_cmd, "on_path", lambda name: f"/usr/bin/{name}" if name in present else None)

    checks = doctor_cmd._collect(str(tmp_path))
    host_prereqs = next(c for c in checks if c.name == "hostPrerequisites")
    assert host_prereqs.status == "pass", host_prereqs.detail


def test_macos_falls_back_to_posix_when_the_manifest_declares_no_macos_list(
    tmp_path, monkeypatch
):
    """A manifest predating alp-sdk v0.14.0 (`prerequisites.macos` absent
    entirely) must still read `posix` on macOS -- the behaviour every SDK had
    before that release, not a refusal."""
    monkeypatch.setattr(doctor_cmd.sys, "platform", "darwin")
    # See the comment in `test_macos_reads_its_own_prerequisites_list_not_posixs`
    # -- `_FixedOsName`, never a bare `os.name` mutation.
    monkeypatch.setattr(doctor_cmd, "os", _FixedOsName("posix"))
    _write_bootstrap_json(
        tmp_path,
        {
            "posix": ["git", "cmake", "python3", "ninja"],
            "windows": ["git", "cmake", "python", "ninja"],
            "pythonMinVersion": "3.10",
            "install": {},
        },
    )
    monkeypatch.setattr(doctor_cmd, "on_path", lambda name: f"/usr/bin/{name}")

    checks = doctor_cmd._collect(str(tmp_path))
    host_prereqs = next(c for c in checks if c.name == "hostPrerequisites")
    assert host_prereqs.status == "pass", host_prereqs.detail
    assert "git" in host_prereqs.detail


def test_west_on_bare_path_the_activated_venv_state_passes():
    """STATE 1 of tan-cli#299's four required states: the workspace venv has
    been sourced into this shell, so bare PATH sees `west` too. Unaffected by
    the tan-cli#299 second-half fix below -- `resolved` is not even consulted
    once `found` is not `None`."""
    check = doctor_cmd.west_check(found="west", version=(1, 5), floor=(1, 4))
    assert check.status == "pass"


def test_west_below_the_manifest_floor_warns_and_names_both_versions():
    """STATE 4 (part 1): a parseable-but-too-old version, on bare PATH.
    Unaffected by the tan-cli#299 second-half fix -- `resolved` is not
    consulted once `found` is not `None`."""
    check = doctor_cmd.west_check(found="west", version=(0, 13), floor=(0, 14))
    assert check.status == "warn"
    assert "0.13" in check.detail and "0.14" in check.detail


def test_west_absent_from_bare_path_but_resolved_in_the_venv_passes():
    """STATE 2 (tan-cli#299 second half): the DEFAULT state of every correct
    fresh install -- `tan bootstrap` deliberately does not put `west` on PATH
    (its own next-steps text tells the user to activate the venv afterwards).
    `west_check` now consults the SAME resolver `westResolved` uses
    (`tan.core.venv.west_program`) and reports `pass`, naming the resolved
    path, instead of the permanent `warn` a bare-PATH-only probe could not
    avoid. A warning that fires on every correct install trains users to
    ignore warnings -- the same defect as the false `fail` this replaced,
    one severity down."""
    check = doctor_cmd.west_check(
        found=None, version=None, floor=(0, 14), resolved="/ws/.venv/bin/west"
    )
    assert check.status == "pass"
    assert "/ws/.venv/bin/west" in check.detail
    assert check.fix is None
    assert doctor_cmd.exit_code_for([check]) == doctor_cmd.ExitCode.SUCCESS


def test_west_absent_from_bare_path_and_unresolvable_anywhere_still_warns_not_fails():
    """STATE 3, the one that must NOT regress: west absent from PATH AND
    unresolvable through the venv resolver either (`resolved=None`) -- a
    genuinely unbuildable host. `west` itself still only WARNs here: FAIL is
    `west_resolved_check`'s job alone (tan-cli#123's one-version-per-check
    contract applied to severity -- see `test_the_two_west_checks_split_the_
    question_and_only_one_can_be_fatal` and `test_collect_exit_code_over_
    two_REAL_trees_venv_with_west_and_without` for the check that actually
    fails this state and owns the exit code). Making BOTH checks non-fatal
    on this exact state is the regression tan-cli#299's own first attempt
    shipped: west absent everywhere produced two warns and exited 0."""
    check = doctor_cmd.west_check(found=None, version=None, floor=(0, 14), resolved=None)
    assert check.status == "warn"
    assert doctor_cmd.exit_code_for([check]) == doctor_cmd.ExitCode.SUCCESS


def test_west_present_but_unparseable_version_is_a_warning_not_a_crash():
    """STATE 4 (part 2): found on bare PATH but `--version` unparseable.
    Unaffected by the tan-cli#299 second-half fix -- `resolved` is not
    consulted once `found` is not `None`."""
    check = doctor_cmd.west_check(found="west", version=None, floor=(0, 14))
    assert check.status == "warn"


# --------------------------------------------------------------------------
# westResolved -- tan-cli#123/#290: the venv-resolved west, not bare PATH
# --------------------------------------------------------------------------


def test_west_resolved_FAILS_when_nothing_resolves_anywhere():
    """No venv west, no PATH west: NO build slice can run, so this is the check
    that fails and owns the exit code.

    It was a Warn, on the premise that `west` (bare PATH) "already fails
    outright on a totally-absent west". tan-cli#299 removed that Fail and
    falsified the premise. Measured on a real host with `west.exe` renamed out
    of the venv and off PATH, both checks Warned and `tan doctor` exited **0**
    on a machine where nothing could build -- a false refusal traded for a
    false pass. This test is the one that would have caught it.
    """
    check = doctor_cmd.west_resolved_check(None, None)
    assert check.status == "fail"
    assert check.fix == "tan bootstrap"


def test_collect_exit_code_over_two_REAL_trees_venv_with_west_and_without(tmp_path, monkeypatch):
    """The proof the unit tests below cannot give: real host states, run
    through `_collect` + `exit_code_for`, asserting the EXIT CODE.

    `west_check(found=None)` is ONE input covering three different host states
    (venv has a genuinely runnable west / venv holds a west that CANNOT run /
    no venv at all). A test that calls the check twice with the same argument
    is two inputs, not two states, and it is exactly what let five "refuses a
    host that works" defects ship green out of this file. The state that
    actually shipped broken -- venv lacks it entirely -- was constructed by no
    fixture at all.

    tan-cli#488 round 2, defect 5: this test's ORIGINAL two-state form (have a
    genuinely runnable west / have none at all) PASSES unmodified against
    `origin/dev`'s pre-fix `doctor_cmd.py` -- measured -- because neither of
    those two states is the one tan-cli#488 defects 1 and 3 actually broke.
    "have" was always a full pass and "lack" was always a full fail, on both
    the buggy and the fixed code; a test built entirely from states that never
    disagree between the two versions pins nothing. The state that DOES
    disagree -- a venv whose `west` launcher file survives but cannot be
    EXECUTED -- is added below as a third real tree, so this integration-level
    test (not just the narrower unit tests it sits beside) actually fails
    against the pre-fix code.

    So: build three real trees -- runnable west / unrunnable west / no venv at
    all -- keep PATH scrubbed in all three, and assert the exit code and both
    the `westResolved` and `west` verdicts for each.
    """
    def _tree(name: str, *, west: str | None) -> Path:
        """`west` selects the venv's west launcher: `"runnable"` (a real,
        spawnable binary), `"unrunnable"` (a 0-byte non-executable file at the
        right name -- resolves via `is_file()`, cannot actually run), or
        `None` (no `.venv` west at all)."""
        ws = tmp_path / name
        (ws / ".west").mkdir(parents=True)
        bin_dir = ws / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        bin_dir.mkdir(parents=True)
        if west == "runnable":
            # A GENUINELY runnable west (tan-cli#488 defect 1): a 0-byte,
            # non-executable file at the right name used to be enough to
            # satisfy this fixture's "builds work" state, because
            # `west_resolved_check` treated ANY resolved path as `pass`
            # regardless of whether it could actually be spawned. Now it
            # cannot.
            _write_runnable_west(bin_dir)
        elif west == "unrunnable":
            layout = venv_layout(os.name == "nt")
            (bin_dir / layout.west).write_text("", encoding="utf-8")
        return ws

    # west is on NEITHER PATH in any state -- that is the constant under test.
    monkeypatch.setattr(doctor_cmd, "on_path", lambda _name: None)

    have = _tree("have", west="runnable")
    broken = _tree("broken", west="unrunnable")
    lack = _tree("lack", west=None)

    def _run(ws: Path) -> tuple[list[doctor_cmd.Check], int]:
        checks = doctor_cmd._collect(None, workspace_root=str(ws))
        return checks, int(doctor_cmd.exit_code_for(checks))

    # STATE 1 -- the DEFAULT post-bootstrap state. `west`/`westResolved` must
    # not be why this host refuses (`_collect` here has no SDK/prerequisites
    # context, so OTHER checks can still fail the overall exit code -- that is
    # not this test's subject; only the west PAIR's own verdicts are).
    checks, _code = _run(have)
    by_name = {c.name: c.status for c in checks}
    assert by_name.get("westResolved") == "pass", "venv holds a west: westResolved must pass"
    # tan-cli#299 second half, at `_collect` level: `west` (bare-PATH-only)
    # must ALSO pass here, fed the same resolved venv path, not warn forever
    # on the default state every correct install starts in.
    assert by_name.get("west") == "pass", "west must consult the same resolved venv path"
    assert not any(i.code == "doctor.west" for i in doctor_cmd.checks_to_issues(checks)), (
        "a passing `west` check must not surface as an issue at all"
    )

    # STATE 2 -- tan-cli#488 defects 1 AND 3: the venv's `west` FILE survives
    # (`is_file()` true, so both checks resolve a path) but cannot actually be
    # spawned. Pre-fix, `westResolved` treated `version is None` as `pass`
    # unconditionally (defect 1), and `west_check`'s own
    # `found is None and resolved is not None` branch reported `pass`
    # regardless of whether `resolved` could run (defect 3) -- both checks
    # green, exit 0, on a host where the first real `west build` dies with
    # `FileNotFoundError`/`ModuleNotFoundError`.
    checks, code = _run(broken)
    by_name = {c.name: c.status for c in checks}
    assert by_name.get("westResolved") == "fail", (
        "a west that resolves but cannot run must FAIL, not pass"
    )
    assert by_name.get("west") != "pass", (
        "west must not report pass for a binary westResolved just failed on"
    )
    assert code == 4, "a host where no build slice can run must not exit 0"

    # STATE 3 -- nothing anywhere. No slice can run; must refuse.
    checks, code = _run(lack)
    by_name = {c.name: c.status for c in checks}
    assert by_name.get("westResolved") == "fail", "no west anywhere: westResolved must FAIL"
    assert code == 4, "a host where no build slice can run must not exit 0"
    # The regression tan-cli#299's first attempt shipped: both checks warning
    # here (and NEITHER failing) is exactly what let this exit 0.
    assert by_name.get("west") == "warn", "west stays a warn -- westResolved alone is fatal"
    # The failing ENVELOPE, not just the check objects -- the same assembly
    # `doctor_cmd.doctor` calls (`checks_to_issues`), mirroring the
    # `{ok, exitCode, issues}` shape `alp-sdk-vscode` actually consumes.
    issues = doctor_cmd.checks_to_issues(checks)
    assert any(
        i.code == "doctor.west-resolved" and i.severity == "error" for i in issues
    ), issues


def test_a_resolved_but_unrunnable_west_fails_not_passes(tmp_path, monkeypatch):
    """tan-cli#488 defect 1, reproduced against the REAL `_collect` wiring: a
    workspace venv whose `west` launcher FILE survives -- `tool_in_venv`'s
    `is_file()` check passes -- but cannot actually be EXECUTED. A relocated
    workspace (the state `tan bootstrap` itself models as
    `bootstrap.workspace-relocated`), a deleted `.venv/bin/python`, or (as
    planted here, portable across POSIX/Windows) a 0-byte non-executable file
    at the right name -- every one leaves `westResolved` unable to spawn the
    binary it names.

    Before the fix, `probe()` collapsed "could not be spawned" into the same
    `None` `west_resolved_check` also sees for "spawned fine but printed
    something unparseable", and treated `version is None` as `pass`
    unconditionally: `westResolved: pass`, `summary.fail == 0`, `ok: true`,
    exit 0 -- on a host where the very first real `west build` invocation
    dies with `FileNotFoundError`/`ModuleNotFoundError`. This is the check
    that must now refuse instead.
    """
    ws = tmp_path
    (ws / ".west").mkdir(parents=True)
    bin_dir = ws / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)
    (bin_dir / ("west.exe" if os.name == "nt" else "west")).write_text("", encoding="utf-8")
    monkeypatch.setattr(doctor_cmd, "on_path", lambda _name: None)

    checks = doctor_cmd._collect(None, workspace_root=str(ws))
    by_name = {c.name: c.status for c in checks}
    assert by_name.get("westResolved") == "fail", (
        "a west that resolves but cannot run must FAIL, not pass"
    )
    assert int(doctor_cmd.exit_code_for(checks)) == 4


def test_west_check_probes_the_resolved_path_not_a_bare_name_reprobe(tmp_path, monkeypatch):
    """tan-cli#488 defect 5: `west`'s version used to come from a SECOND,
    bare-name spawn (`probe(["west", "--version"])`) rather than the
    `on_path`-resolved absolute path -- reopening the exact cwd-injection gap
    `on_path` exists to close (its own docstring: a project-local
    `west.exe`/`west` at the cwd root must not be reported as this host's
    tooling). Fake `probe` so the bare name and the resolved path answer
    DIFFERENT version strings -- a real rogue-binary scenario could not do
    this so cleanly, but it proves which argv this call site actually spawns.
    """
    resolved = "/opt/onpath/west"
    monkeypatch.setattr(doctor_cmd, "on_path", lambda name: resolved if name == "west" else None)

    def _fake_probe(argv, timeout=doctor_cmd.PROBE_TIMEOUT_S):
        if argv == [resolved, "--version"]:
            return "West version: v1.5.0\n"
        if argv == ["west", "--version"]:
            return "West version: v9.99.0\n"  # the rogue binary's answer
        return None

    monkeypatch.setattr(doctor_cmd, "probe", _fake_probe)

    checks = doctor_cmd._collect(None, workspace_root=str(tmp_path))
    west = next(c for c in checks if c.name == "west")
    assert "1.5" in west.detail, west.detail
    assert "9.99" not in west.detail, west.detail
    """`west` = "is it on bare PATH, or resolvable through the same resolver
    `westResolved` uses" -- never fatal either way (tan-cli#299 second half).
    `westResolved` = "can a slice run at all", fatal when not. tan-cli#123's
    one-version-per-check contract, applied to severity: exactly one of them
    can ever own the exit code.

    Both states, not one twice -- a single-state test is what let five
    "refuses a host that works" defects ship green out of this file.
    """
    # DEFAULT post-bootstrap state: venv has west, PATH does not. Builds work.
    # `west` now PASSES here too (tan-cli#299 second half) -- fed the same
    # resolved path `westResolved` reports, never a second bare-PATH re-probe.
    assert doctor_cmd.west_check(None, None, None, "/ws/.venv/bin/west").status == "pass"
    assert doctor_cmd.west_resolved_check("/ws/.venv/bin/west", (1, 5)).status == "pass"

    # ABSENT state: nothing anywhere. Builds cannot work. `west` still only
    # WARNs -- FAIL stays `westResolved`'s alone.
    assert doctor_cmd.west_check(None, None, None).status == "warn"
    assert doctor_cmd.west_resolved_check(None, None).status == "fail"

    # `west`'s text must not predict what `westResolved` found -- asserting the
    # venv resolved one was wrong in exactly the absent state.
    detail = doctor_cmd.west_check(None, None, None).detail
    assert "actually resolves it through the workspace venv" not in detail
    assert "westResolved" in detail


def test_west_check_does_not_pass_a_resolved_but_unspawnable_west():
    """tan-cli#488 defect 3: `found is None and resolved is not None` used to
    report `pass` unconditionally -- correct only when the resolved binary can
    actually run. `westResolved` (tan-cli#488 defect 1) now FAILS the
    identical binary when it cannot be spawned; this check must not still call
    it `pass`, which is the exact headline symptom ("pass on an unrunnable
    west") surviving in the one branch defect 1's own fix did not touch.
    """
    check = doctor_cmd.west_check(None, None, None, "/ws/.venv/bin/west", False)
    assert check.status != "pass", check.detail
    assert "/ws/.venv/bin/west" in check.detail
    assert "westResolved" in check.detail

    # `resolved_ran` defaults to `True` -- every pre-existing 4-argument call
    # site (this file's own unit tests above) keeps its prior "pass" meaning.
    assert doctor_cmd.west_check(None, None, None, "/ws/.venv/bin/west").status == "pass"


def test_west_resolved_passes_and_names_the_resolved_binary_and_version():
    """The working-but-unusual state `west` (bare-PATH-only) cannot see: a
    resolved venv binary, off PATH entirely."""
    check = doctor_cmd.west_resolved_check("/ws/.venv/bin/west", (1, 5))
    assert check.status == "pass"
    assert "1.5" in check.detail
    assert "/ws/.venv/bin/west" in check.detail


def test_west_resolved_still_passes_with_no_readable_version():
    check = doctor_cmd.west_resolved_check("/ws/.venv/bin/west", None)
    assert check.status == "pass"
    assert "/ws/.venv/bin/west" in check.detail


def test_collect_reports_west_resolved_unconditionally(tmp_path):
    """tan-cli#290: unlike the old `zephyrWorkspace`, `westResolved` was
    never `--build`-gated in the first place -- confirm both modes carry it,
    mirroring `crates/tan-cli/src/commands/doctor.rs:1828`'s assertion that
    `sdk`/`workspace`/`westResolved` all land in the plain fold together."""
    for build in (False, True):
        names = {c.name for c in doctor_cmd._collect(None, build=build, workspace_root=str(tmp_path))}
        assert "westResolved" in names, (build, names)


def _write_runnable_west(bin_dir: Path) -> Path:
    """Plant a REAL, spawnable `west` under `bin_dir` -- one that a
    `subprocess.run([west_path, "--version"])` actually executes to a
    successful exit, not merely a file at the right name (tan-cli#488 defect
    1: `west_resolved_check` now FAILS a `west` that resolves but cannot
    run, so a fixture meaning "this venv's west genuinely works" must plant
    one that genuinely does).

    Shared by every test that needs a "builds work" west, factored out of
    `test_west_resolved_reproduces_and_closes_tan_cli_123`'s own fixture.
    """
    layout = venv_layout(os.name == "nt")
    west_path = bin_dir / layout.west
    if os.name == "nt":
        # A real, spawnable PE binary under the exact required name -- a text
        # file named `west.exe` is not executable at all, and
        # `test_west_resolved_reproduces_and_closes_tan_cli_123`'s own
        # `scrub_path=True` proved (by breaking it first) that a bare copy of
        # `python.exe` is NOT self-contained: with PATH empty it cannot find
        # its own `python3*.dll`/`vcruntime*.dll` via the app-directory search
        # and dies with STATUS_DLL_NOT_FOUND before printing anything. Copying
        # every DLL that sits beside the real interpreter alongside the
        # renamed copy closes that gap; `--version` then runs the copied
        # interpreter's own, always-parseable banner.
        #
        # tan-cli#297: the source must be the BASE interpreter
        # (`sys.base_prefix`), never `sys.executable` as such. When pytest
        # itself runs from a project venv (`.venv\Scripts\python.exe`),
        # `sys.executable` names a launcher stub that ships with NO sibling
        # DLLs at all (they stay in the base install) and needs its own
        # `pyvenv.cfg` to find them -- reproduced directly: copying that stub
        # elsewhere and running `--version` fails outright with "No pyvenv.cfg
        # file" (exit 106), never a parseable banner, which is exactly the
        # "west.exe version-banner assertion" failure this closes. Reading
        # `sys.base_prefix` instead is a no-op when pytest already runs from a
        # base install (this host: `sys.executable == sys.base_prefix`), and
        # resolves to the same self-contained layout either way.
        interpreter_dir = Path(sys.base_prefix)
        base_python = interpreter_dir / "python.exe"
        if not base_python.is_file():
            # A base layout with no `python.exe` (e.g. an embeddable/portable
            # install with a differently-named executable) turns this fixture
            # into a hard `FileNotFoundError` rather than a clean skip -- this
            # is a fixture-construction gap, not something the test is meant
            # to catch.
            pytest.skip(f"no base interpreter at {base_python} to build the self-contained west.exe fixture from")
        for dll in interpreter_dir.glob("*.dll"):
            shutil.copy(dll, bin_dir / dll.name)
        shutil.copy(base_python, west_path)
    else:
        west_path.write_text("#!/bin/sh\necho 'West version: v99.98.97'\n", encoding="utf-8")
        os.chmod(west_path, 0o755)
    return west_path


def test_west_resolved_reproduces_and_closes_tan_cli_123(tmp_path):
    """tan-cli#123's exact bug, reproduced then guarded: a workspace venv
    holds `west`, PATH is scrubbed empty so a bare lookup CANNOT possibly
    answer -- `westResolved` must still resolve and report a version, proving
    it came from the venv binary, never a bare-PATH re-probe. Break
    `west_resolved_check`'s wiring in `_collect` (e.g. feed it
    `on_path("west")` instead of `tan.core.venv.west_program`'s result) and
    this goes red: `westResolved` would report the same absent verdict `west`
    (bare-PATH-only) does, with PATH scrubbed.

    Also STATE 2 of tan-cli#299's second half, run through the REAL envelope
    a `tan doctor --format json` process emits (`doctor_cmd.doctor`'s own
    `checks_to_issues`/`exit_code_for`, not a hand-built `Check`): `west`
    must now PASS here too -- fed the SAME resolved venv path `westResolved`
    reports -- naming it, and it must not surface as a `doctor.west` issue
    at any severity in `data.issues`. Before this fix, this exact state (the
    default one every correct install starts in) reported `west` as a
    permanent `warn`.
    """
    layout = venv_layout(os.name == "nt")
    bin_dir = tmp_path / ".venv" / layout.bin_dir
    bin_dir.mkdir(parents=True)
    west_path = _write_runnable_west(bin_dir)

    proc = run_tan("doctor", "--format", "json", cwd=tmp_path, scrub_path=True)
    envelope = json.loads(proc.stdout)
    checks = {c["name"]: c for c in envelope["data"]["checks"]}

    resolved = checks["westResolved"]
    assert resolved["status"] == "pass", resolved
    if os.name == "nt":
        expected = ".".join(sys.version.split()[0].split(".")[:2])
    else:
        expected = "99.98"
    assert expected in resolved["detail"], resolved["detail"]

    # tan-cli#299 second half.
    west = checks["west"]
    assert west["status"] == "pass", west
    assert str(west_path) in west["detail"], west["detail"]
    assert not any(i["code"] == "doctor.west" for i in envelope["issues"]), envelope["issues"]


# --------------------------------------------------------------------------
# venvProvenance (tan-cli#292 consequences 1 and 3): the resolved venv's
# tan-written record must name the SAME SDK this report resolved against.
# --------------------------------------------------------------------------


def _plant_venv_with_record(venv_dir: Path, record_json: str | None) -> None:
    """A west-capable `.venv` at `venv_dir`, with `record_json` (if given)
    written as its sibling `.west/tan-workspace-sdk` -- the exact on-disk
    shape `bootstrap_cmd.record_workspace_sdk` produces, one level up from
    the venv itself."""
    layout = venv_layout(os.name == "nt")
    bin_dir = venv_dir / layout.bin_dir
    bin_dir.mkdir(parents=True)
    (bin_dir / layout.west).write_text("", encoding="utf-8")
    if record_json is not None:
        record_dir = venv_dir.parent / ".west"
        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / "tan-workspace-sdk").write_text(record_json, encoding="utf-8")


def test_venv_provenance_warns_when_the_record_names_a_different_sdk(tmp_path, monkeypatch):
    """Consequence 3 (`tan sdk switch` leaves the venv behind): the resolved
    venv's own record still names the SDK that last populated it, not the
    one this report resolved against."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    old_sdk = tmp_path / "sdk-v1"
    old_sdk.mkdir()
    new_sdk = tmp_path / "sdk-v2"
    new_sdk.mkdir()
    workspace = tmp_path / "ws"
    _plant_venv_with_record(
        workspace / ".venv", workspace_sdk_record_json(str(old_sdk))
    )

    checks = doctor_cmd._collect(str(new_sdk), workspace_root=str(workspace))
    check = next(c for c in checks if c.name == "venvProvenance")
    assert check.status == "warn", check.detail
    assert str(old_sdk) in check.detail
    assert str(new_sdk) in check.detail
    assert check.fix == "tan bootstrap"


def test_venv_provenance_passes_when_the_record_matches_the_resolved_sdk(tmp_path, monkeypatch):
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk = tmp_path / "alp-sdk"
    sdk.mkdir()
    workspace = tmp_path / "ws"
    _plant_venv_with_record(
        workspace / ".venv", workspace_sdk_record_json(str(sdk))
    )

    checks = doctor_cmd._collect(str(sdk), workspace_root=str(workspace))
    check = next(c for c in checks if c.name == "venvProvenance")
    assert check.status == "pass", check.detail


def test_venv_provenance_catches_a_neighbouring_tan_bootstrapped_projects_venv(
    tmp_path, monkeypatch
):
    """Consequence 1 (a neighbouring project's venv wins the upward walk):
    reproduced with the neighbour ITSELF tan-bootstrapped -- for a different
    SDK -- so it carries a record `venvProvenance` can actually compare
    against. A neighbour with no tan record at all (a bare `west init`, the
    #278 shape) is the documented, still-open gap this mechanism does not
    close; see `venv_provenance_check`'s docstring."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    other_sdk = tmp_path / "other-project" / "alp-sdk"
    other_sdk.mkdir(parents=True)
    # The neighbour's venv sits ABOVE the customer's own project in the tree,
    # so `find_workspace_venv`'s upward walk from the project reaches it
    # before any SDK-derived candidate.
    neighbour_root = tmp_path / "other-project"
    _plant_venv_with_record(
        neighbour_root / ".venv",
        workspace_sdk_record_json(str(other_sdk)),
    )
    project = neighbour_root / "nested" / "my-app"
    project.mkdir(parents=True)

    my_sdk = tmp_path / "my-sdk"
    my_sdk.mkdir()
    checks = doctor_cmd._collect(str(my_sdk), workspace_root=str(project))
    check = next(c for c in checks if c.name == "venvProvenance")
    assert check.status == "warn", check.detail
    assert str(other_sdk) in check.detail


def test_venv_provenance_emits_no_check_on_a_record_less_bootstrap_sh_workspace(
    tmp_path, monkeypatch
):
    """A workspace alp-sdk's own `bootstrap.sh` set up writes no tan record
    at all (`crates/tan-cli/src/venv.rs:25-27`) -- `venvProvenance` must not
    invent a verdict against a checkout it was never told about."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk = tmp_path / "alp-sdk"
    sdk.mkdir()
    workspace = tmp_path / "ws"
    _plant_venv_with_record(workspace / ".venv", None)  # west-capable, no record

    checks = doctor_cmd._collect(str(sdk), workspace_root=str(workspace))
    assert not any(c.name == "venvProvenance" for c in checks)


def test_venv_provenance_emits_no_check_when_no_venv_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk = tmp_path / "alp-sdk"
    sdk.mkdir()
    empty = tmp_path / "elsewhere"
    empty.mkdir()

    checks = doctor_cmd._collect(str(sdk), workspace_root=str(empty))
    assert not any(c.name == "venvProvenance" for c in checks)


def test_venv_provenance_check_unit_covers_the_no_sdk_root_case():
    """`_collect` never has "a record but no `sdk_root`" to feed the check in
    practice (the record's OWN existence implies bootstrap ran against SOME
    resolvable SDK), but `venv_provenance_check` is exercised directly here
    for the branch: nothing to compare against, so `None`, never a fabricated
    verdict."""
    record = doctor_cmd.WorkspaceSdkRecord(sdk_path="/ws/alp-sdk")
    assert doctor_cmd.venv_provenance_check(record, None) is None
    assert doctor_cmd.venv_provenance_check(None, "/ws/alp-sdk") is None
    assert doctor_cmd.venv_provenance_check(None, None) is None


# --------------------------------------------------------------------------
# zephyrSdk (tan-cli#286) -- the port had NO such check at all; the Rust
# oracle's `zephyrSdk` (crates/tan-cli/src/commands/doctor.rs::
# append_zephyr_sdk_toolchain, tan-cli#160) is unconditional in plain
# `tan doctor`, so this must be too.
# --------------------------------------------------------------------------


def test_zephyr_sdk_detected_passes():
    check = doctor_cmd.zephyr_sdk_check(True)
    assert check.status == "pass"
    assert check.fix is None


def test_zephyr_sdk_not_detected_fails_and_names_the_exact_install_command():
    check = doctor_cmd.zephyr_sdk_check(False)
    assert check.status == "fail"
    command = "west sdk install --version 1.0.1 -t arm-zephyr-eabi"
    assert command in check.detail
    assert command in (check.fix or "")


def test_zephyr_sdk_check_names_the_env_var_when_it_points_at_a_bad_directory():
    """Finding: the fail detail used to hardcode "(ZEPHYR_SDK_INSTALL_DIR
    unset)" even when the variable WAS set and simply named a directory with
    no working toolchain in it -- exactly the stale-var case the guard in
    `_zephyr_sdk_detected` exists for. It must say the var is set and wrong,
    not unset."""
    check = doctor_cmd.zephyr_sdk_check(False, env_dir="/opt/zephyr-sdk-0.16.5")
    assert "ZEPHYR_SDK_INSTALL_DIR=" in check.detail
    assert "/opt/zephyr-sdk-0.16.5" in check.detail
    assert "unset" not in check.detail


def test_zephyr_sdk_check_says_unset_only_when_it_really_is():
    check = doctor_cmd.zephyr_sdk_check(False, env_dir=None)
    assert "ZEPHYR_SDK_INSTALL_DIR unset" in check.detail


def test_zephyr_sdk_install_dir_env_wins_when_the_directory_actually_has_the_toolchain(
    tmp_path, monkeypatch
):
    _plant_zephyr_sdk(tmp_path)
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(tmp_path))
    assert doctor_cmd._zephyr_sdk_detected() is True


def test_zephyr_sdk_install_dir_pointing_at_an_empty_directory_is_not_trusted(
    tmp_path, monkeypatch
):
    """Finding 1, tan-cli#286 second pass: `Path(env_dir).is_dir()` alone
    passes on ANY directory, so an empty one named by ZEPHYR_SDK_INSTALL_DIR
    used to report a false Pass. The scan roots are pinned to an empty
    stand-in too (finding 3, third pass) -- `/opt` is one of
    `_zephyr_sdk_scan_roots`'s roots UNCONDITIONALLY, not only via
    `HOME`/`USERPROFILE`/`Path.home()`, so pinning only those three (as this
    test used to) still let the assertion flip on a host that genuinely has a
    Zephyr SDK under `/opt` -- a documented `west sdk install` default."""
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(tmp_path))
    monkeypatch.setattr(
        doctor_cmd, "_zephyr_sdk_scan_roots", lambda: [tmp_path / "not-a-real-home"]
    )
    assert doctor_cmd._zephyr_sdk_detected() is False


def test_zephyr_sdk_install_dir_env_pointing_nowhere_is_not_trusted(tmp_path, monkeypatch):
    """A stale `ZEPHYR_SDK_INSTALL_DIR` (exported once, the SDK since removed)
    must not report a false Pass -- mirrors
    `crate::toolchain::env_dir_still_exists`. The scan roots are pinned for
    the same reason, including `/opt`, as the empty-directory case above."""
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(tmp_path / "gone"))
    monkeypatch.setattr(
        doctor_cmd, "_zephyr_sdk_scan_roots", lambda: [tmp_path / "not-a-real-home"]
    )
    assert doctor_cmd._zephyr_sdk_detected() is False


def test_zephyr_sdk_detected_by_scanning_home_with_no_env_var_set(tmp_path, monkeypatch):
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    _plant_zephyr_sdk(tmp_path / "zephyr-sdk-1.0.1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert doctor_cmd._zephyr_sdk_detected() is True


def test_zephyr_sdk_detected_via_msys_home_split_from_windows_userprofile(tmp_path, monkeypatch):
    """Finding 2, tan-cli#286 second pass -- reproduced on a real host: Git
    Bash/MSYS sets `HOME` to a POSIX-translated path (`/c/Users/dev`) that is
    real but has no SDK under it, while the actual Zephyr SDK sits under the
    native `%USERPROFILE%` (`C:\\Users\\dev\\zephyr-sdk-1.0.1`). The old
    `HOME or USERPROFILE` picked `HOME` (set first) and never scanned
    `USERPROFILE` at all, so a host that HAS the SDK reported `False`. Both
    must be scanned."""
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    posix_home = tmp_path / "msys-home"  # stands in for e.g. /c/Users/dev: real, empty
    posix_home.mkdir()
    windows_profile = tmp_path / "win-profile"  # stands in for C:\Users\dev
    _plant_zephyr_sdk(windows_profile / "zephyr-sdk-1.0.1")
    monkeypatch.setenv("HOME", str(posix_home))
    monkeypatch.setenv("USERPROFILE", str(windows_profile))
    assert doctor_cmd._zephyr_sdk_detected() is True


def test_zephyr_sdk_root_valid_rejects_a_directory_with_no_compiler_in_it(tmp_path):
    assert doctor_cmd._zephyr_sdk_root_valid(tmp_path) is False


def test_zephyr_sdk_root_valid_accepts_a_real_layout(tmp_path):
    _plant_zephyr_sdk(tmp_path)
    assert doctor_cmd._zephyr_sdk_root_valid(tmp_path) is True


def test_zephyr_sdk_install_version_matches_the_real_toolchain_lock():
    """tan-cli#172's Python-side half. `contract/fixtures/toolchains/
    toolchains.json`'s own `_comment` states the rule verbatim: "A NEW
    consumer of this pin needs its own parity assertion; widening this scan
    will not reach it." Mirrored `crates/tan-core/src/host_env.rs`'s
    `zephyr_sdk_install_version_matches_the_real_toolchain_lock` until
    tan-cli#269 deleted it; this is now the ONLY assertion in the chain, so a
    bump that re-vendors the fixture without moving the constant fails HERE or
    nowhere, and `tan doctor` would otherwise silently name a stale
    `west sdk install --version`."""
    fixture = REPO_ROOT / "contract" / "fixtures" / "toolchains" / "toolchains.json"
    doc = json.loads(fixture.read_text(encoding="utf-8"))
    assert doctor_cmd.ZEPHYR_SDK_INSTALL_VERSION == doc["zephyrSdk"]["version"]


# --------------------------------------------------------------------------
# sevenZip (tan-cli#286 second pass, finding 3) -- the `zephyrSdk` Fail names
# `west sdk install` as the whole remedy, but on native Windows that command
# cannot complete without 7-Zip on PATH (`tan.core.bootstrap`'s
# `manual_install_windows` prose). Mirrors `crate::build_readiness`'s
# `sevenZip` sibling, gated exactly `probe.is_windows && !probe.zephyr_sdk`
# (tan-cli#204).
# --------------------------------------------------------------------------


def test_seven_zip_check_passes_clean_with_no_fix_when_found():
    check = doctor_cmd.seven_zip_check(True)
    assert check.status == "pass"
    assert check.fix is None


def test_seven_zip_check_names_the_pinned_install_command_when_absent():
    check = doctor_cmd.seven_zip_check(False)
    assert check.status == "warn"
    command = "winget install -e --id 7zip.7zip"
    assert command in check.detail
    assert command in (check.fix or "")
    for program in doctor_cmd.SEVEN_ZIP_PROGRAMS:
        assert program in check.detail


class _FixedOsName:
    """A stand-in for the `os` module that reports a FIXED `os.name`, proxying
    every other attribute to the real module.

    Rebinding `doctor_cmd.os` to one of these (rather than mutating
    `os.name` on the real, process-wide module object `import os` hands back
    everywhere) is the only safe way to flip an `os.name`-gated branch in a
    test: mutating the shared module crashes pytest's OWN failure-reporting on
    a real failure (`pathlib.Path.__new__` re-picks `WindowsPath`/`PosixPath`
    from `os.name` on every call, including ones pytest itself makes) --
    caught while writing this test, not theoretical.
    """

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        return getattr(os, attr)

    @property
    def name(self):
        return self._name


def test_collect_adds_seven_zip_only_on_windows_while_the_sdk_is_absent(tmp_path, monkeypatch):
    """Finding 3 (tan-cli#286 third pass): the scan roots are stubbed outright
    -- `/opt` is one of `_zephyr_sdk_scan_roots`'s roots unconditionally, so a
    developer host with a real `/opt/zephyr-sdk-*` would otherwise flip this
    to `zephyrSdk` detected and drop `sevenZip` from the check list."""
    monkeypatch.setattr(doctor_cmd, "os", _FixedOsName("nt"))
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    monkeypatch.setattr(
        doctor_cmd, "_zephyr_sdk_scan_roots", lambda: [tmp_path / "not-a-real-home"]
    )
    checks = doctor_cmd._collect(None)
    assert "sevenZip" in {c.name for c in checks}


def test_collect_omits_seven_zip_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor_cmd, "os", _FixedOsName("posix"))
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    monkeypatch.setattr(
        doctor_cmd, "_zephyr_sdk_scan_roots", lambda: [tmp_path / "not-a-real-home"]
    )
    checks = doctor_cmd._collect(None)
    assert "sevenZip" not in {c.name for c in checks}


def test_collect_omits_seven_zip_on_windows_once_the_sdk_is_detected(tmp_path, monkeypatch):
    """The permanent-noise case the gate exists to avoid: once the SDK is
    present, the extractor is irrelevant, so `sevenZip` must not linger."""
    monkeypatch.setattr(doctor_cmd, "os", _FixedOsName("nt"))
    _plant_zephyr_sdk(tmp_path)
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(tmp_path))
    checks = doctor_cmd._collect(None)
    assert "sevenZip" not in {c.name for c in checks}


def test_collect_reports_zephyr_sdk_unconditionally_with_no_board_or_sdk_resolved():
    """The load-bearing regression case: before this check existed, plain
    `tan doctor` with no SDK and no `--build` (the exact fresh-host,
    ADR-0021-Lane-1-P0a call) never mentioned a Zephyr toolchain at all --
    reverting the `_collect` wiring must fail this."""
    checks = doctor_cmd._collect(None, build=False)
    assert "zephyrSdk" in {c.name for c in checks}


def test_collect_reports_zephyr_sdk_under_build_too():
    checks = doctor_cmd._collect(None, build=True)
    assert "zephyrSdk" in {c.name for c in checks}


# --------------------------------------------------------------------------
# zephyrWorkspace -- unconditional (tan-cli#290), Warn-only (tan-cli#295):
# a version-mismatch verdict is `zephyrVersion`'s Fail to report, not this
# check's -- see the two-check `_collect` regression below.
# --------------------------------------------------------------------------


def test_zephyr_workspace_warns_when_the_dir_is_not_a_zephyr_checkout():
    """The one fact `zephyrVersion` cannot see at all (it silently skips) --
    this check's whole remaining reason to exist. Advisory: a `.west`
    workspace mid-`west update` is a legitimate, working-in-progress state,
    not a proven blocker."""
    check = doctor_cmd.zephyr_workspace_check("/ws", None)
    assert check.status == "warn"
    assert "VERSION" in check.detail


def test_zephyr_workspace_passes_whenever_version_is_readable():
    """No Fail branch (tan-cli#295 review): this check only answers whether
    the resolved workspace looks like a Zephyr checkout at all, regardless
    of whether its version matches the SDK's pin."""
    check = doctor_cmd.zephyr_workspace_check("/ws", "4.4.0")
    assert check.status == "pass"
    assert "4.4.0" in check.detail


def test_zephyr_workspace_now_runs_unconditionally_not_only_under_build(tmp_path):
    """tan-cli#290: the last of tan-cli#294's `--build`-gated checks folds
    into plain `tan doctor` -- ADR 0021 Lane 1 P0a runs PLAIN doctor as the
    very first command a customer types, and gating this fact behind
    `--build` left it invisible there. `--build` is accepted and forwarded
    (both `alp-sdk-vscode` call sites still pass it) but no longer changes
    the check-name set. A resolvable workspace is required to observe the
    check at all -- see `zephyr_workspace_check`'s "no unresolved branch"
    docstring note -- so this plants one directly at `workspace_root` (step
    1 of `tan.core.venv.west_workspace_dir`'s search).
    """
    (tmp_path / ".west").mkdir()
    zephyr = tmp_path / "zephyr"
    zephyr.mkdir()
    (zephyr / "VERSION").write_text(
        "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 1\n", encoding="utf-8"
    )
    for build in (False, True):
        names = {
            c.name for c in doctor_cmd._collect(None, build=build, workspace_root=str(tmp_path))
        }
        assert "zephyrWorkspace" in names, (build, names)


# --------------------------------------------------------------------------
# SETOOLS -- the second silent gap
# --------------------------------------------------------------------------


def test_setools_check_names_both_env_vars_the_fdt_package_and_the_alif_download():
    check = doctor_cmd.setools_check(
        setools_dir=None, se_uart=None, has_fdt=False, is_linux=True
    )
    assert check.status == "warn"
    blob = f"{check.detail} {check.fix}"
    for token in ("SETOOLS_DIR", "SE_UART", "fdt", "app-release-exec-linux"):
        assert token in blob, f"the SETOOLS check never mentions {token}"


def test_setools_dir_pointing_somewhere_without_app_gen_toc_is_reported(tmp_path):
    check = doctor_cmd.setools_check(
        setools_dir=str(tmp_path), se_uart="/dev/ttyUSB0", has_fdt=True, is_linux=True
    )
    assert check.status == "warn"
    assert "app-gen-toc" in check.detail


def test_a_fully_provisioned_setools_host_passes(tmp_path):
    (tmp_path / "app-gen-toc").write_text("", encoding="utf-8")
    (tmp_path / "app-write-mram").write_text("", encoding="utf-8")
    check = doctor_cmd.setools_check(
        setools_dir=str(tmp_path), se_uart="/dev/ttyUSB0", has_fdt=True, is_linux=True
    )
    assert check.status == "pass"


def test_setools_is_unknown_not_warn_off_linux():
    """`alif_flash.py` hard-codes `app-release-exec-linux`; there is no verdict
    to give a Windows/macOS host, and `unknown` counts in no summary bucket."""
    check = doctor_cmd.setools_check(
        setools_dir=None, se_uart=None, has_fdt=False, is_linux=False
    )
    assert check.status == "unknown"
    assert "app-release-exec-linux" in check.detail


def test_module_importable_asks_the_given_interpreter_not_tans_own():
    """tan-cli#488 defect 6: `_module_importable(python_exe, name)` must
    probe `python_exe` -- a REAL subprocess spawn, `os` (always importable)
    vs a name that certainly is not, both under `sys.executable` standing in
    for "some other interpreter"."""
    assert doctor_cmd._module_importable(sys.executable, "os") is True
    assert (
        doctor_cmd._module_importable(sys.executable, "tan_cli_488_no_such_module")
        is False
    )


def test_module_importable_falls_back_to_tans_own_interpreter_with_no_venv():
    """`python_exe is None` (no workspace venv resolved yet, e.g. before `tan
    bootstrap` has run) falls back to `_has_module`, against tan's own
    interpreter -- the best available signal before there is anything else
    to ask."""
    assert doctor_cmd._module_importable(None, "os") == doctor_cmd._has_module("os")


def test_collect_asks_the_workspace_venvs_python_for_fdt_not_tans_own(
    tmp_path, monkeypatch
):
    """The `_collect` wiring: `setools_check`'s `has_fdt` argument must come
    from `_module_importable(venv_python(workspace_root, sdk_root), "fdt")`
    -- the interpreter `west flash` actually runs `app-gen-toc` under -- never
    a bare `_has_module("fdt")` against tan's own interpreter. Proven by
    substituting a sentinel `venv_python` and recording what
    `_module_importable` was actually called with, rather than asserting on
    `has_fdt`'s boolean value alone (which cannot distinguish "asked the
    right interpreter and got False" from "asked the wrong one entirely").
    """
    sentinel = "/some/workspace/.venv/bin/python"
    monkeypatch.setattr(doctor_cmd, "venv_python", lambda *_a, **_k: sentinel)
    calls: list[tuple[str | None, str]] = []

    def _record(python_exe, name):
        calls.append((python_exe, name))
        return True

    monkeypatch.setattr(doctor_cmd, "_module_importable", _record)
    doctor_cmd._collect(None, workspace_root=str(tmp_path))
    assert (sentinel, "fdt") in calls, calls


# --------------------------------------------------------------------------
# J-Link / Flow D
# --------------------------------------------------------------------------


def test_jlink_names_the_part_number_device_profile_and_the_dll_floor():
    check = doctor_cmd.jlink_check(found="/usr/bin/JLinkExe", version=(9, 50))
    assert check.status == "pass"
    blob = f"{check.detail} {check.fix or ''}"
    assert "AE822FA0E5597LS0_M55_HE" in blob
    assert "V13" in blob


def test_jlink_below_v9_46_warns_because_flow_d_has_no_mram_loader():
    check = doctor_cmd.jlink_check(found="/usr/bin/JLinkExe", version=(9, 40))
    assert check.status == "warn"
    assert "9.46" in check.detail


def test_jlink_absent_is_a_warning_not_a_failure():
    assert doctor_cmd.jlink_check(found=None, version=None).status == "warn"


def test_jlink_present_with_an_unreadable_version_still_reports_the_requirements():
    check = doctor_cmd.jlink_check(found="/usr/bin/JLinkExe", version=None)
    assert check.status == "warn"
    assert "AE822FA0E5597LS0_M55_HE" in f"{check.detail} {check.fix or ''}"


def test_jlink_check_names_a_caller_supplied_device_not_only_the_fallback():
    """`_collect` passes the metadata-resolved profile through; a stand-in value
    proves the parameter is actually used, not shadowed by the constant."""
    check = doctor_cmd.jlink_check(
        found="/usr/bin/JLinkExe", version=(9, 50), device="SOME_OTHER_PROFILE"
    )
    assert "SOME_OTHER_PROFILE" in check.detail
    assert "AE822FA0E5597LS0_M55_HE" not in check.detail


# --------------------------------------------------------------------------
# jlink_flash_device -- resolving the AE822 profile from metadata
# --------------------------------------------------------------------------


def _write_e8_json(root: Path, variants: list[dict]) -> None:
    e8 = root / "metadata" / "socs" / "alif" / "ensemble"
    e8.mkdir(parents=True)
    (e8 / "e8.json").write_text(json.dumps({"variants": variants}), encoding="utf-8")


def test_jlink_flash_device_is_read_from_e8_json_when_an_sdk_resolves(tmp_path):
    """The real shape: two AE822 package variants, only the second carrying a
    `jlink_flash_device` -- the one variant with an MRAM loader profile."""
    _write_e8_json(
        tmp_path,
        [
            {"debug": {"jlink_device": {"m55_he": "Cortex-M55"}}},
            {"debug": {"jlink_device": {"m55_he": "Cortex-M55"},
                       "jlink_flash_device": "AE822FA0E5597LS0_M55_HE"}},
        ],
    )
    device, source = doctor_cmd.jlink_flash_device(str(tmp_path))
    assert device == "AE822FA0E5597LS0_M55_HE"
    assert "e8.json" in source


def test_jlink_flash_device_falls_back_with_no_sdk_root():
    """Case 1: nothing to read from at all -- the one wording tan-cli#310 left
    unchanged. Its own distinguishing text ("no alp-sdk checkout resolved")
    must NOT appear in either of the other two fallback causes below, or a
    doctor run with a perfectly good SDK checkout tells the user to go hunting
    for an SDK-resolution problem they do not have (the bug tan-cli#310
    found)."""
    device, source = doctor_cmd.jlink_flash_device(None)
    assert device == doctor_cmd.JLINK_AEN_DEVICE
    assert "built-in fallback" in source
    assert "no alp-sdk checkout resolved" in source


def test_jlink_flash_device_falls_back_when_no_variant_carries_the_key(tmp_path):
    """Case 3: an SDK resolved and its `e8.json` parsed fine, but no variant
    carries `debug.jlink_flash_device` -- the real state of every checkout
    since alp-sdk#1057 moved that fact to a per-board `flash_args` value
    doctor has no board selected to read. Must say THAT, not case 1's "no
    alp-sdk checkout resolved" (tan-cli#310: a checkout genuinely resolved
    here, so that sentence would be false)."""
    _write_e8_json(tmp_path, [{"debug": {"jlink_device": {"m55_he": "Cortex-M55"}}}])
    device, source = doctor_cmd.jlink_flash_device(str(tmp_path))
    assert device == doctor_cmd.JLINK_AEN_DEVICE
    assert "built-in fallback" in source
    assert "no variant carries" in source
    assert "flash_args" in source
    assert "no alp-sdk checkout resolved" not in source


def test_jlink_flash_device_survives_a_missing_or_malformed_e8_json(tmp_path):
    """Case 2: an SDK resolved but `e8.json` itself is missing / unreadable /
    malformed -- both fall back rather than raising (doctor's whole job is to
    run on a host where things are wrong), and both must say THAT, naming the
    path, not case 1's "no alp-sdk checkout resolved" (a checkout DID
    resolve; only the file under it did not read)."""
    device, source = doctor_cmd.jlink_flash_device(str(tmp_path))
    assert device == doctor_cmd.JLINK_AEN_DEVICE
    assert "missing, unreadable" in source
    assert "no alp-sdk checkout resolved" not in source
    assert str(tmp_path) in source

    (tmp_path / "metadata" / "socs" / "alif" / "ensemble").mkdir(parents=True)
    (tmp_path / "metadata" / "socs" / "alif" / "ensemble" / "e8.json").mkdir()
    device, source = doctor_cmd.jlink_flash_device(str(tmp_path))
    assert device == doctor_cmd.JLINK_AEN_DEVICE
    assert "missing, unreadable" in source
    assert "no alp-sdk checkout resolved" not in source


def test_jlink_flash_device_falls_back_when_variants_disagree(tmp_path):
    """Two variants carrying DIFFERENT `jlink_flash_device` values is
    ambiguous, not resolved: picking whichever serialises first would silently
    name the wrong part with nothing to catch it, so this must fall back and
    say why -- not pick either one."""
    _write_e8_json(
        tmp_path,
        [
            {"debug": {"jlink_flash_device": "AE822FA0E5597LS0_M55_HE"}},
            {"debug": {"jlink_flash_device": "SOME_OTHER_PART_M55_HE"}},
        ],
    )
    device, source = doctor_cmd.jlink_flash_device(str(tmp_path))
    assert device == doctor_cmd.JLINK_AEN_DEVICE
    assert "ambiguous" in source


# --------------------------------------------------------------------------
# _collect -- the production call site, not just the helpers in isolation
# --------------------------------------------------------------------------


def test_collect_wires_the_resolved_jlink_device_and_its_source_into_the_check(tmp_path):
    """Reverting `_collect` to the old hardcoded `jlink_check(jlink_exe,
    jlink_version)` call must fail THIS test: it is the only coverage of the
    production call site, not just `jlink_flash_device`/`jlink_check` in
    isolation. Also proves the source travels into the envelope (Finding 5):
    the check text must differ depending on where the profile came from."""
    _write_e8_json(tmp_path, [{"debug": {"jlink_flash_device": "STAND-IN-PROFILE"}}])
    checks = doctor_cmd._collect(str(tmp_path))
    jlink = next(c for c in checks if c.name == "jlink")
    assert "STAND-IN-PROFILE" in jlink.detail
    assert doctor_cmd.JLINK_AEN_DEVICE not in jlink.detail
    assert "e8.json" in jlink.detail


# --------------------------------------------------------------------------
# Aggregation and issue codes
# --------------------------------------------------------------------------


def test_a_single_failing_check_exits_4_never_0():
    checks = [
        doctor_cmd.Check("a", "pass", "fine"),
        doctor_cmd.Check("b", "fail", "broken"),
    ]
    assert doctor_cmd.exit_code_for(checks) == 4


def test_warnings_alone_do_not_fail_the_host():
    checks = [doctor_cmd.Check("a", "warn", "meh"), doctor_cmd.Check("b", "unknown", "?")]
    assert doctor_cmd.exit_code_for(checks) == 0


def test_unknown_is_counted_in_no_summary_bucket():
    summary = doctor_cmd.summarise(
        [
            doctor_cmd.Check("a", "pass", ""),
            doctor_cmd.Check("b", "warn", ""),
            doctor_cmd.Check("c", "fail", ""),
            doctor_cmd.Check("d", "unknown", ""),
        ]
    )
    assert summary == {"pass": 1, "warn": 1, "fail": 1}


def test_issues_default_to_doctor_dot_check_name_and_skip_passing_checks():
    issues = doctor_cmd.checks_to_issues(
        [
            doctor_cmd.Check("west", "warn", "old"),
            doctor_cmd.Check("jlink", "pass", "fine"),
            doctor_cmd.Check("setools", "unknown", "not askable"),
        ]
    )
    assert [(i.code, i.severity) for i in issues] == [("doctor.west", "warning")]


def test_the_retired_windows_unsupported_code_is_never_reused():
    source = Path(doctor_cmd.__file__).read_text(encoding="utf-8")
    assert "windows-unsupported" not in source


def test_the_three_frozen_codes_are_spelled_exactly():
    source = Path(doctor_cmd.__file__).read_text(encoding="utf-8")
    for code in (
        "bootstrap.python-too-old",
        "bootstrap.python-not-runnable",
        "bootstrap.prerequisites-missing",
    ):
        assert f'"{code}"' in source


# --------------------------------------------------------------------------
# Hostile probes -- the recurring Critical in this port
# --------------------------------------------------------------------------


def test_a_probe_that_hangs_is_killed_and_returns_none():
    assert doctor_cmd.probe([sys.executable, "-c", "import time; time.sleep(30)"], timeout=2) is None


def test_a_probe_of_a_missing_binary_returns_none():
    assert doctor_cmd.probe(["tan-doctor-no-such-binary-8f3a"], timeout=5) is None


def test_a_probe_that_exits_non_zero_returns_none():
    assert doctor_cmd.probe([sys.executable, "-c", "raise SystemExit(3)"], timeout=10) is None


def test_a_probe_emitting_undecodable_bytes_does_not_raise():
    out = doctor_cmd.probe(
        [sys.executable, "-c", r"import sys; sys.stdout.buffer.write(b'\xff\xfe ok')"],
        timeout=10,
    )
    assert out is not None and "ok" in out


def test_path_lookup_never_consults_the_current_directory(tmp_path, monkeypatch):
    """Mirrors `command_on_path`: `shutil.which` inserts `os.curdir` ahead of
    PATH on Windows, so a checkout shipping its own `west.exe` would be
    reported as the host's tooling."""
    planted = tmp_path / ("west.exe" if os.name == "nt" else "west")
    planted.write_text("", encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    assert doctor_cmd.on_path("west") is None


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_a_scrubbed_host_exits_4_with_exactly_one_envelope_and_no_traceback(tmp_path):
    proc = run_tan("doctor", "--format", "json", cwd=tmp_path, scrub_path=True)
    assert "Traceback" not in proc.stderr, proc.stderr
    assert proc.returncode == 4, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    envelope = json.loads(proc.stdout)
    assert envelope["command"] == "doctor"
    assert envelope["ok"] is False
    assert envelope["exitCode"] == 4
    # `sdk` is OMITTED when absent, never null.
    assert "sdk" not in envelope
    codes = {i["code"] for i in envelope["issues"]}
    # `west`, `cmake` and `ninja` cannot resolve with no PATH, so this one is
    # certain on every host.
    assert "bootstrap.prerequisites-missing" in codes
    # The Python verdict is host-dependent even with PATH scrubbed, in all
    # THREE directions -- so it is the CHECK's presence and vocabulary that is
    # asserted, never a particular outcome. Windows resolves `py.exe` from the
    # Windows directory regardless of PATH (CreateProcess searches it before
    # PATH), so the launcher's default interpreter decides: below the effective
    # floor gives `fail` (`bootstrap.python-too-old`), at or above it gives
    # `pass` and NO issue at all, and a POSIX host with no PATH gives `fail`
    # (`bootstrap.python-not-runnable`). Pinning any one of those makes this
    # case flip on a host change that is not a defect -- installing Python 3.12
    # beside a 3.11 was enough to move it from the second to the third.
    host_python = next(c for c in envelope["data"]["checks"] if c["name"] == "hostPython")
    assert host_python["status"] in ("pass", "fail")
    if host_python["status"] == "fail":
        assert codes & {"bootstrap.python-not-runnable", "bootstrap.python-too-old"}
    assert {"summary", "checks", "generatedAt", "nextSteps"} <= set(envelope["data"])


@pytest.mark.parametrize(
    "epoch", ["1700000000000", "99999999999", "-99999999999", "253402300799"]
)
def test_an_out_of_range_source_date_epoch_still_reports_the_host(epoch, tmp_path):
    """`data.generatedAt` is rendered INSIDE doctor's own try/except, so a
    timestamp helper that throws does not produce a traceback here -- it produces
    a WRONG VERDICT: `doctor.internal-failure` at exit 5, `data: null`, on a host
    that was diagnosed fine. The quiet half of the same defect
    `test_debug_config_command.py` covers loudly.

    Milliseconds is the realistic trigger (1700000000000 -> year 55838), and CI
    and reproducible-build environments are what set this variable. `PATH` is
    scrubbed so the exit code is the deterministic 4 of the case above rather
    than whatever this developer machine happens to have installed.
    """
    proc = run_tan(
        "doctor", "--format", "json", cwd=tmp_path, scrub_path=True,
        env_extra={"SOURCE_DATE_EPOCH": epoch},
    )
    assert "Traceback" not in proc.stderr, proc.stderr
    envelope = json.loads(proc.stdout)

    codes = {i["code"] for i in envelope["issues"]}
    assert "doctor.internal-failure" not in codes, envelope["issues"]
    assert proc.returncode == 4, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert envelope["data"] is not None
    # Shape, not value: an out-of-range epoch falls back to the wall clock.
    time.strptime(envelope["data"]["generatedAt"], "%Y-%m-%dT%H:%M:%SZ")


def test_text_mode_writes_nothing_to_stdout(tmp_path):
    proc = run_tan("doctor", cwd=tmp_path, scrub_path=True)
    assert proc.stdout == ""
    assert proc.stderr.strip() != ""


def test_an_invalid_format_is_rejected(tmp_path):
    proc = run_tan("doctor", "--format", "yaml", cwd=tmp_path)
    assert proc.returncode == 2


# --------------------------------------------------------------------------
# `project` envelope field -- posix separators, anchored on `--project`
# --------------------------------------------------------------------------


def test_project_envelope_uses_posix_separators_not_native(tmp_path):
    """`root`/`boardYaml` must be forward-slash on every platform, matching the
    oracle -- verified: `tan --project app doctor --format json` from a scratch
    tree reports `"root":"C:/Users/.../app"`. `Project(root=str(workspace_root),
    board_yaml=board_yaml)` used to emit the native `C:\\Users\\...\\app`
    (`os.path.join`/`Path` on Windows) instead."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "board.yaml").write_text("", encoding="utf-8")
    proc = run_tan(
        "--project", "app", "doctor", "--format", "json", cwd=tmp_path, scrub_path=True
    )
    envelope = json.loads(proc.stdout)
    assert envelope["project"] == {
        "root": app.as_posix(),
        "boardYaml": (app / "board.yaml").as_posix(),
    }


def test_explicit_relative_board_yaml_is_anchored_on_project_not_cwd(tmp_path):
    """`--project app doctor --board-yaml board.yaml` must resolve onto
    `<tmp_path>/app/board.yaml`, not the real cwd -- verified against the
    oracle. Before the fix, `--board-yaml` was anchored only inside the
    discovery branch (`if board_yaml is None and ...`); an EXPLICIT relative
    `--board-yaml` skipped that branch entirely and was reported verbatim
    (`'board.yaml'`, resolving against the real cwd downstream) -- the Critical
    wrong-project defect class `build_cmd.build` already guards against,
    surviving here."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "board.yaml").write_text("app board\n", encoding="utf-8")
    # A DIFFERENT board.yaml sitting in the real cwd -- the one that would
    # (wrongly) win if the anchor is missing.
    (tmp_path / "board.yaml").write_text("cwd board\n", encoding="utf-8")
    proc = run_tan(
        "--project", "app", "doctor", "--board-yaml", "board.yaml", "--format", "json",
        cwd=tmp_path, scrub_path=True,
    )
    envelope = json.loads(proc.stdout)
    assert envelope["project"] == {
        "root": app.as_posix(),
        "boardYaml": (app / "board.yaml").as_posix(),
    }


# --------------------------------------------------------------------------
# tan-cli#294 finding 1 -- host-environment checks, reintroducing tan-cli#70.
# --------------------------------------------------------------------------


def test_zephyr_sdk_host_check_distinguishes_a_served_host_from_an_unserved_one():
    served = doctor_cmd.zephyr_sdk_host_check("linux", "x86_64")
    assert served.status == "pass"
    assert served.fix is None

    unserved = doctor_cmd.zephyr_sdk_host_check("linux", "riscv64")
    assert unserved.status == "fail"
    assert unserved.fix is not None
    assert unserved.name == "zephyrSdkAvailableForHost"


def test_zephyr_sdk_host_check_names_wsl2_for_windows_on_arm_and_a_different_remedy_for_intel_mac():
    """The correction this check exists to make: routing an Intel Mac to WSL2
    is advice that cannot be followed -- the two unserved hosts must get
    DIFFERENT remedies, not one shared message."""
    windows_arm = doctor_cmd.zephyr_sdk_host_check("windows", "aarch64")
    intel_mac = doctor_cmd.zephyr_sdk_host_check("macos", "x86_64")
    assert "WSL2" in windows_arm.fix
    assert "linux-aarch64" in windows_arm.fix
    assert windows_arm.fix != intel_mac.fix
    assert "WSL" not in intel_mac.fix
    assert "Linux host" in intel_mac.fix


def test_macos_rosetta_translated_reads_the_sysctl_probe(monkeypatch):
    monkeypatch.setattr(doctor_cmd, "probe", lambda *a, **k: "1\n")
    assert doctor_cmd._macos_rosetta_translated() is True

    monkeypatch.setattr(doctor_cmd, "probe", lambda *a, **k: "0\n")
    assert doctor_cmd._macos_rosetta_translated() is False

    monkeypatch.setattr(doctor_cmd, "probe", lambda *a, **k: None)
    assert doctor_cmd._macos_rosetta_translated() is False


def test_host_os_arch_tags_corrects_x86_64_to_aarch64_under_rosetta(monkeypatch):
    """tan-cli#294 review: an Apple-silicon Mac running an x86_64 Python
    reports `macos-x86_64` from `platform.machine()` alone -- a FALSE hard
    refusal (`zephyrSdkAvailableForHost` fail, exit 4, "build on a Linux
    host") on a host the pinned SDK fully serves as `macos-aarch64`.
    Rosetta's own sysctl corrects it."""
    monkeypatch.setattr(doctor_cmd.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(doctor_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(doctor_cmd, "probe", lambda *a, **k: "1\n")
    assert doctor_cmd._host_os_arch_tags() == ("macos", "aarch64")


def test_host_os_arch_tags_leaves_a_native_intel_mac_alone(monkeypatch):
    """The other state of the same probe: a REAL Intel Mac (not translated)
    must still report `macos-x86_64` -- the unserved host this check is
    supposed to fail."""
    monkeypatch.setattr(doctor_cmd.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(doctor_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(doctor_cmd, "probe", lambda *a, **k: "0\n")
    assert doctor_cmd._host_os_arch_tags() == ("macos", "x86_64")


def test_long_paths_check_passes_only_when_both_the_registry_and_git_are_on():
    both_on = doctor_cmd.long_paths_check(True, True)
    assert both_on.status == "pass"
    assert both_on.fix is None

    # Every OTHER combination of the two axes must NOT pass -- swapping any
    # one of these to "pass" is the mutation that turns tan-cli#306 back into
    # a silent regression: a pass here is a customer-facing claim that
    # `west update` will not hit "Filename too long".
    for registry in (True, False, None):
        for git in (True, False, None):
            if registry is True and git is True:
                continue
            check = doctor_cmd.long_paths_check(registry, git)
            assert check.status != "pass", f"registry={registry} git={git}: {check}"


def test_long_paths_check_fails_when_the_registry_says_yes_and_git_does_not():
    """The exact defect tan-cli#306 reports: the registry read alone said
    `pass` while `west update`'s own `git` -- which does not consult the
    registry at all -- refused a long path. This is the one combination that
    must be `fail`, not `warn`: `git` is the first thing in the toolchain to
    touch a long path, and its own setting says no, so the break is
    certain."""
    for git in (False, None):
        check = doctor_cmd.long_paths_check(True, git)
        assert check.status == "fail", f"git={git}: {check}"
        # Fix #3 in tan-cli#306: the remedy must name the exact command,
        # verbatim.
        assert "git config --global core.longpaths true" in (check.fix or "")


def test_long_paths_check_warns_but_does_not_fail_when_git_is_on_and_the_registry_is_not():
    """Git handles long paths on its own once `core.longpaths=true` (it
    never consults the registry), so the failure this check exists to catch
    will NOT reproduce -- must not be `fail`. But other manifested tools
    (CMake, Ninja) still rely on the registry flag, so residual risk
    remains -- must not be a bare `pass` either."""
    for registry in (False, None):
        check = doctor_cmd.long_paths_check(registry, True)
        assert check.status == "warn", f"registry={registry}: {check}"
        assert check.fix is not None
        assert "git core.longpaths is true" in check.detail


def test_long_paths_check_warns_when_neither_axis_is_on():
    for registry in (False, None):
        for git in (False, None):
            check = doctor_cmd.long_paths_check(registry, git)
            assert check.status == "warn", f"registry={registry} git={git}: {check}"
            assert check.fix is not None
    off = doctor_cmd.long_paths_check(False, False)
    assert "MAX_PATH" in off.detail
    assert "New-ItemProperty" in (off.fix or "")

    unknown = doctor_cmd.long_paths_check(None, None)
    assert "could not be read" in unknown.detail or "could not be determined" in unknown.detail
    # "could not tell" must never render as a silent Pass.
    assert unknown.status != "pass"


def test_classify_git_core_longpaths_maps_exit_status_and_never_guesses():
    assert doctor_cmd.classify_git_core_longpaths(0, "true") is True
    assert doctor_cmd.classify_git_core_longpaths(0, "false") is False
    # git's own boolean grammar: case-insensitive, other spellings.
    assert doctor_cmd.classify_git_core_longpaths(0, "TRUE") is True
    assert doctor_cmd.classify_git_core_longpaths(0, "no") is False
    assert doctor_cmd.classify_git_core_longpaths(0, "1") is True
    assert doctor_cmd.classify_git_core_longpaths(0, "0") is False
    # exit 1: git's documented "not set in any scope" -- its own default, and
    # the state a fresh HOME is in.
    assert doctor_cmd.classify_git_core_longpaths(1, "") is False
    # Anything else (git missing, a malformed config) is uncertain, not
    # guessed.
    assert doctor_cmd.classify_git_core_longpaths(2, "") is None
    assert doctor_cmd.classify_git_core_longpaths(128, "") is None
    assert doctor_cmd.classify_git_core_longpaths(None, "") is None


@pytest.mark.skipif(os.name != "nt", reason="longPaths is Windows-only")
def test_long_paths_reads_gits_real_config_not_just_the_registry(tmp_path):
    """tan-cli#306's own regression, driven through the REAL CLI with a REAL
    `git` subprocess -- as far as an automated test can go without writing
    `HKLM` (the registry axis genuinely cannot be driven this way; see the
    module docstring). A fresh `HOME` with no global `.gitconfig` is the
    exact state that broke `west update` on a customer's machine, and it is
    reachable here the same way `test_zephyr_sdk_detected_via_msys_home_
    split_from_windows_userprofile` (above) reaches `_zephyr_sdk_detected`'s
    own `HOME`/`USERPROFILE` axis: both env vars are read by git itself, not
    reimplemented here, this test just controls which directory `git` sees.

    The check's overall `status` is deliberately NOT asserted -- it also
    depends on the registry flag, which THIS runner's own state decides and
    a test must not write. What is asserted is the one thing under this
    test's control either way: the DETAIL names git's real, freshly-read
    value.

    `GIT_CONFIG_NOSYSTEM=1` isolates this from whatever the CI image's own
    system-wide git config carries -- with it set, only `HOME` (verified by
    hand to outrank `USERPROFILE`/`HOMEDRIVE`+`HOMEPATH` in git's own
    global-config lookup) decides what `git config --get core.longpaths`
    sees, so both cases below are deterministic regardless of the runner.
    """

    def longpaths_detail(tag: str, gitconfig: str | None) -> str:
        home = tmp_path / f"home-{tag}"
        home.mkdir()
        if gitconfig is not None:
            (home / ".gitconfig").write_text(gitconfig, encoding="utf-8")
        work_dir = tmp_path / f"work-{tag}"
        work_dir.mkdir()

        proc = run_tan(
            "doctor",
            "--format",
            "json",
            cwd=work_dir,
            env_extra={"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"},
        )
        envelope = json.loads(proc.stdout)
        check = next(c for c in envelope["data"]["checks"] if c["name"] == "longPaths")
        return check["detail"]

    unset = longpaths_detail("unset", None)
    assert "git core.longpaths is unset or false" in unset, unset

    on = longpaths_detail("on", "[core]\n\tlongpaths = true\n")
    assert "git core.longpaths is true" in on, on


def test_home_path_check_distinguishes_a_spaced_home_from_a_clean_one():
    spaced = doctor_cmd.home_path_check(r"C:\Users\Jane Doe")
    assert spaced.status == "warn"
    assert r"C:\Users\Jane Doe" in spaced.detail
    assert spaced.status != "fail"  # degraded, not a hard blocker

    clean = doctor_cmd.home_path_check(r"C:\Users\jane")
    assert clean.status == "pass"
    assert clean.fix is None

    unset = doctor_cmd.home_path_check(None)
    assert unset.status == "warn"
    assert unset.fix is not None


def test_collect_reports_the_host_environment_trio_unconditionally(tmp_path):
    """Reverting the `_collect` wiring must fail this: these are host facts,
    not gated on `--build`, a board.yaml, or a resolved SDK."""
    checks = doctor_cmd._collect(None, workspace_root=str(tmp_path))
    names = {c.name for c in checks}
    assert "zephyrSdkAvailableForHost" in names
    assert "homePath" in names


# --------------------------------------------------------------------------
# tan-cli#294 finding 2 -- build-environment preflight, reintroducing
# tan-cli#100, #98, #159.
# --------------------------------------------------------------------------


def test_sdk_check_distinguishes_a_resolved_sdk_from_an_unresolved_one():
    resolved = doctor_cmd.sdk_check("/opt/alp-sdk", project_scope=None)
    assert resolved.status == "pass"

    unresolved = doctor_cmd.sdk_check(None, project_scope=None)
    assert unresolved.status == "fail"
    # tan-cli#305: `sdk switch` refuses in this build -- the fail detail must
    # not send the user to it, and the only thing that still resolves an SDK
    # is `--sdk-root`.
    assert "sdk switch" not in unresolved.detail
    assert "sdk install" not in unresolved.detail
    assert "--sdk-root" in unresolved.detail
    assert unresolved.fix == "--sdk-root <path>"


def test_sdk_check_names_the_project_scope_without_recommending_a_refused_switch():
    """tan-cli#101 used to hint a project-scoped `tan sdk switch <path>` here,
    reasoning a bare one reports success while leaving a `tan --project <p>
    ...` invocation failing identically. tan-cli#305: that hint is moot now
    that `sdk switch` refuses in EVERY scope -- the fix is `--sdk-root`
    regardless, so the project is named only as context, never as part of a
    broken remedy."""
    check = doctor_cmd.sdk_check(None, project_scope="examples/uart-echo")
    assert check.status == "fail"
    assert "examples/uart-echo" in check.detail
    assert "sdk switch" not in check.detail
    assert "sdk install" not in check.detail
    assert "--sdk-root" in check.detail
    assert check.fix == "--sdk-root <path>"


# --------------------------------------------------------------------------
# tan-cli#301 -- `sdk` names the winning TIER, and an unselected cwd
# candidate a higher tier outranked. No behaviour change: the tier ladder
# itself (GlobalDefault > Discovery, tan-cli#263's absolute pins) is untouched.
# --------------------------------------------------------------------------


def test_sdk_check_with_no_tier_given_keeps_the_old_bare_detail():
    """Every existing direct caller (no `tier` passed) must see byte-identical
    output -- this is a report-only addition."""
    check = doctor_cmd.sdk_check("/opt/alp-sdk", project_scope=None)
    assert check.detail == "alp-sdk at /opt/alp-sdk"


def test_sdk_check_reports_the_tier_alongside_the_root():
    check = doctor_cmd.sdk_check("/opt/alp-sdk", project_scope=None, tier="globalDefault")
    assert check.status == "pass"
    assert "alp-sdk at /opt/alp-sdk" in check.detail
    assert "globalDefault" in check.detail


def test_sdk_check_names_an_unselected_candidate_and_how_to_select_it():
    check = doctor_cmd.sdk_check(
        "/opt/alp-sdk",
        project_scope=None,
        tier="globalDefault",
        unselected_candidate="/home/dev/project/alp-sdk",
    )
    assert "was not selected" in check.detail
    assert "/home/dev/project/alp-sdk" in check.detail
    assert "--sdk-root /home/dev/project/alp-sdk" in check.detail


def _make_sdk_root(path: Path) -> Path:
    (path / "scripts").mkdir(parents=True, exist_ok=True)
    (path / "scripts" / "alp_project.py").write_text("", encoding="utf-8", newline="")
    return path


def _write_global_default_pointer(target: Path) -> None:
    """`~/.alp/sdk-default` -- `HOME`/`USERPROFILE` are already repointed at
    an isolated tmp dir by the autouse `conftest._scrub_sdk_discovery_env`
    fixture, so this cannot touch a developer's real global default."""
    home = Path(os.environ["USERPROFILE" if os.name == "nt" else "HOME"])
    pointer = home / ".alp" / "sdk-default"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps({"sdkPath": str(target), "updatedAt": "1970-01-01T00:00:00Z"}),
        encoding="utf-8",
        newline="",
    )


def test_collect_names_the_global_default_tier_and_the_unselected_child_checkout(tmp_path):
    """tan-cli#301's exact reported shape: a global-default alp-sdk wins over
    a checkout the user was standing IN (a CHILD of the workspace root), and
    the report used to say nothing about either fact -- three roots, one
    report, no way to tell which is which. `resolve_sdk_root_ladder` still
    picks the SAME globalDefault (no behaviour change); only `sdk`'s own
    detail changes."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    child_sdk = _make_sdk_root(workspace / "alp-sdk")
    global_sdk = _make_sdk_root(tmp_path / "elsewhere" / "alp-sdk")
    _write_global_default_pointer(global_sdk)

    resolution = doctor_cmd.resolve_sdk_root_ladder(None, workspace)
    assert resolution.tier == "globalDefault"
    assert str(resolution.path) == str(global_sdk)
    assert resolution.broken_project_pin is None

    checks = doctor_cmd._collect(
        str(resolution.path), workspace_root=str(workspace), sdk_tier=resolution.tier
    )
    sdk = next(c for c in checks if c.name == "sdk")
    assert sdk.status == "pass"
    assert "globalDefault" in sdk.detail
    assert str(child_sdk) in sdk.detail
    assert "was not selected" in sdk.detail
    assert f"--sdk-root {child_sdk}" in sdk.detail


def test_collect_names_no_unselected_candidate_when_discovery_itself_answered(tmp_path):
    """The tier that resolved IS discovery -- there is no "unselected"
    candidate to name; a bolder discovery walk finding the SAME checkout
    again must not read as a second, ignored one."""
    workspace = tmp_path / "ws"
    sdk = _make_sdk_root(workspace)
    checks = doctor_cmd._collect(str(sdk), workspace_root=str(workspace), sdk_tier="discovery")
    check = next(c for c in checks if c.name == "sdk")
    assert "was not selected" not in check.detail


# --------------------------------------------------------------------------
# tan-cli#344 -- a dangling `~/.alp/sdk-default` is a distinct fact from
# "nothing configured": falling through stays correct, exit 4 stays correct,
# only the `sdk` check's remedy text changes.
# --------------------------------------------------------------------------


def test_broken_global_default_is_none_when_nothing_is_configured():
    assert doctor_cmd._broken_global_default() is None


def test_broken_global_default_is_none_when_the_pointer_resolves(tmp_path):
    target = _make_sdk_root(tmp_path / "alp-sdk")
    _write_global_default_pointer(target)
    assert doctor_cmd._broken_global_default() is None


def test_broken_global_default_names_the_dangling_target(tmp_path):
    broken_target = tmp_path / "gone"
    _write_global_default_pointer(broken_target)
    assert doctor_cmd._broken_global_default() == str(broken_target)


def test_sdk_check_names_a_broken_global_default_distinctly_from_nothing_configured(
    tmp_path,
):
    """The exact tan-cli#344 defect: before this, both cases printed the
    identical `NO_SDK_NEXT_STEPS` sentence. The remedy must name the pointer
    path, offer to delete or hand-edit it (never `tan sdk switch`, which
    refuses outright per tan-cli#305), and still offer `--sdk-root`."""
    broken_target = tmp_path / "gone"

    nothing_configured = doctor_cmd.sdk_check(None, project_scope=None)
    broken_default = doctor_cmd.sdk_check(
        None, project_scope=None, broken_global_default=str(broken_target)
    )

    assert nothing_configured.status == broken_default.status == "fail"
    assert nothing_configured.detail != broken_default.detail

    assert str(broken_target) in broken_default.detail
    assert str(broken_target) not in nothing_configured.detail

    # tan-cli#305: never recommend the refused `sdk switch` subcommand.
    assert "sdk switch" not in broken_default.detail
    assert "sdk switch" not in broken_default.fix
    assert "delete" in broken_default.fix
    assert "--sdk-root" in broken_default.fix

    # The plain "nothing configured" sentence is untouched.
    assert "get an alp-sdk checkout" in nothing_configured.detail


def test_sdk_check_ignores_broken_global_default_once_something_else_resolves():
    """`broken_global_default` only matters in the `sdk_root is None` branch
    -- a resolved SDK's `pass` detail must not change shape just because a
    stale default also happens to be lying around."""
    check = doctor_cmd.sdk_check(
        "/opt/alp-sdk", project_scope=None, broken_global_default="/gone"
    )
    assert check.status == "pass"
    assert check.detail == "alp-sdk at /opt/alp-sdk"


def test_collect_names_a_broken_global_default_end_to_end(tmp_path):
    """tan-cli#344 through the whole pipeline: `resolve_sdk_root_ladder`
    falls through the broken pointer to `none` (UNCHANGED behaviour), while
    `_collect`'s `sdk` check now says why."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    broken_target = tmp_path / "gone"
    _write_global_default_pointer(broken_target)

    resolution = doctor_cmd.resolve_sdk_root_ladder(None, workspace)
    assert resolution.path is None
    assert resolution.tier == "none"
    assert resolution.broken_project_pin is None  # the GLOBAL default, not the project pin

    checks = doctor_cmd._collect(
        None,
        workspace_root=str(workspace),
        sdk_tier=resolution.tier,
        broken_global_default=doctor_cmd._broken_global_default(),
    )
    sdk = next(c for c in checks if c.name == "sdk")
    assert sdk.status == "fail"
    assert str(broken_target) in sdk.detail


def test_doctor_names_a_broken_global_default_end_to_end_via_the_cli(tmp_path):
    """Real subprocess, real envelope: the exit code (4) and the fall-through
    (no SDK selected) are both unchanged from before tan-cli#344; only the
    `sdk` check's `detail`/`fix` differ."""
    broken_target = tmp_path / "gone"
    # `_write_global_default_pointer`, not a second hand-rolled copy of the
    # same three lines -- this file already owns one (used above by
    # `test_collect_names_a_broken_global_default_end_to_end` and friends).
    _write_global_default_pointer(broken_target)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    proc = run_tan("doctor", "--format", "json", cwd=workspace)
    envelope = json.loads(proc.stdout)
    assert envelope["exitCode"] == 4
    sdk = next(c for c in envelope["data"]["checks"] if c["name"] == "sdk")
    assert sdk["status"] == "fail"
    assert str(broken_target) in sdk["detail"]
    assert "sdk switch" not in sdk["fix"]
    assert "--sdk-root" in sdk["fix"]


def test_board_yaml_preflight_check_passes_when_present_regardless_of_selection():
    assert doctor_cmd.board_yaml_preflight_check(True, project_selected=False).status == "pass"
    assert doctor_cmd.board_yaml_preflight_check(True, project_selected=True).status == "pass"


def test_board_yaml_preflight_check_warns_with_no_project_selected():
    """#100(b): plain `tan doctor` run from a freshly bootstrapped SDK
    checkout root -- no `--project`/`--board-yaml` given -- must not refuse
    the host over a board.yaml nobody asked about."""
    check = doctor_cmd.board_yaml_preflight_check(False, project_selected=False)
    assert check.status == "warn"
    assert "tan init" not in check.detail


def test_board_yaml_preflight_check_fails_when_a_project_was_explicitly_selected():
    check = doctor_cmd.board_yaml_preflight_check(False, project_selected=True)
    assert check.status == "fail"
    assert "tan init" in check.detail


def test_collect_resolves_a_dot_west_walking_up_the_project_tree(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".west").mkdir(parents=True)
    app = workspace / "app" / "nested"
    app.mkdir(parents=True)
    checks = doctor_cmd._collect(None, workspace_root=str(app))
    check = next(c for c in checks if c.name == "workspace")
    assert check.status == "pass"
    assert str(workspace) in check.detail


def test_collect_resolves_the_workspace_from_a_bare_zephyr_base_with_no_sdk_root(
    monkeypatch, tmp_path
):
    """tan-cli#294 review: a host whose ONLY Zephyr workspace is a manually
    exported `$ZEPHYR_BASE` outside both the project tree and any SDK-derived
    layout -- step 2 of the shared `tan.core.venv.west_workspace_dir` search,
    which this port's own retired `_resolve_west_workspace_dir` omitted, so
    `workspace` reported a false Fail telling the customer to bootstrap a
    SECOND workspace."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    workspace = tmp_path / "manual-workspace"
    zephyr = workspace / "zephyr"
    zephyr.mkdir(parents=True)
    (workspace / ".west").mkdir()
    monkeypatch.setenv("ZEPHYR_BASE", str(zephyr))

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    checks = doctor_cmd._collect(None, workspace_root=str(outside))
    check = next(c for c in checks if c.name == "workspace")
    assert check.status == "pass", check.detail
    assert str(workspace) in check.detail


# --------------------------------------------------------------------------
# tan-cli#301, second half: `hostPython`/`pythonFloor` must read the SAME
# resolved workspace `zephyrWorkspace` reports, not an independent
# `$ZEPHYR_BASE` re-read -- else one report can cite two different Zephyrs.
# --------------------------------------------------------------------------


def _plant_zephyr_tree(zephyr_dir: Path, python_floor: str, patchlevel: int = 1) -> None:
    """A minimal `<zephyr_dir>/{VERSION,cmake/modules/python.cmake}` -- the two
    files `zephyrWorkspace` and `zephyr_python_floor` each read."""
    zephyr_dir.mkdir(parents=True, exist_ok=True)
    (zephyr_dir / "VERSION").write_text(
        f"VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = {patchlevel}\n",
        encoding="utf-8",
    )
    modules = zephyr_dir / "cmake" / "modules"
    modules.mkdir(parents=True)
    (modules / "python.cmake").write_text(
        f"set(PYTHON_MINIMUM_REQUIRED {python_floor})\n", encoding="utf-8"
    )


def test_hostpython_and_zephyrworkspace_name_the_same_tree_over_a_stale_zephyr_base(
    tmp_path, monkeypatch
):
    """Reproduces the issue's own shape measured on a real host: a genuine
    resolved workspace (`.west` + `zephyr/`), and a stale exported
    `$ZEPHYR_BASE` pointing at an unrelated, un-workspaced tree with a
    materially different floor. Before this fix, `hostPython`/`pythonFloor`
    read `$ZEPHYR_BASE` regardless of the resolved workspace `zephyrWorkspace`
    itself reports -- one report, two different Zephyrs.
    """
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)

    workspace = tmp_path / "alp-workspace"
    (workspace / ".west").mkdir(parents=True)
    _plant_zephyr_tree(workspace / "zephyr", "3.11")

    # An un-workspaced (no `.west` at its parent), unrelated tree -- exactly
    # the "stray-zephyrproject" shape from the issue -- with a DIFFERENT
    # floor, so a report reading it instead of the resolved workspace is
    # provably wrong, not merely differently-worded.
    stray = tmp_path / "dirty" / "stray-zephyrproject" / "zephyr"
    _plant_zephyr_tree(stray, "3.14")
    monkeypatch.setenv("ZEPHYR_BASE", str(stray))

    checks = doctor_cmd._collect(None, workspace_root=str(workspace))
    zephyr_workspace = next(c for c in checks if c.name == "zephyrWorkspace")
    host_python = next(c for c in checks if c.name == "hostPython")
    python_floor = next((c for c in checks if c.name == "pythonFloor"), None)

    assert zephyr_workspace.status == "pass", zephyr_workspace.detail
    # Same tree: the resolved workspace's own topdir names both.
    assert str(workspace) in zephyr_workspace.detail
    assert str(workspace) in host_python.detail
    # Anchor on the FLOOR phrase, never on a bare `"3.14" not in detail`. This
    # detail also carries the INTERPRETER's version (`Python 3.14 (`py -3`)
    # meets the effective floor 3.11 (...)`), and CI's Windows runner really is
    # on 3.14 -- a bare substring search cannot tell "the stray tree's floor
    # leaked" from "the host happens to run that version", and asserted the
    # latter while claiming the former.
    assert "effective floor 3.11" in host_python.detail
    assert "effective floor 3.14" not in host_python.detail
    # Never the stray tree `$ZEPHYR_BASE` points at.
    assert str(stray) not in host_python.detail

    # 3.11 > the (3, 10) fallback manifest floor (no `sdk_root` resolved), so
    # `pythonFloor` fires -- and must cite the same resolved tree too.
    assert python_floor is not None, "expected pythonFloor: workspace floor 3.11 > fallback 3.10"
    assert str(workspace) in python_floor.detail
    assert str(stray) not in python_floor.detail


def test_collect_python_floor_source_falls_back_to_zephyr_base_when_no_workspace_resolves(
    tmp_path, monkeypatch
):
    """State 2 of 3: no `.west` workspace resolves anywhere (the stray tree's
    parent has no `.west`, and no `sdk_root` is given), so `zephyr_python_floor`
    falls back to a literal `$ZEPHYR_BASE` read -- and that source, not the
    workspace one, is what `hostPython` must name here."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    stray = tmp_path / "dirty" / "stray-zephyrproject" / "zephyr"
    _plant_zephyr_tree(stray, "3.13")
    monkeypatch.setenv("ZEPHYR_BASE", str(stray))

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    checks = doctor_cmd._collect(None, workspace_root=str(outside))

    workspace_check = next(c for c in checks if c.name == "workspace")
    assert workspace_check.status == "fail", workspace_check.detail  # nothing resolved

    host_python = next(c for c in checks if c.name == "hostPython")
    assert str(stray) in host_python.detail
    # `effective floor 3.13`, not a bare `"3.13" in detail` -- the detail also
    # carries the interpreter's own version, so the bare form would pass on a
    # host running 3.13 even if the floor were read from entirely the wrong
    # place. That is a FALSE PASS, and this test's whole job is provenance.
    assert "effective floor 3.13" in host_python.detail


def test_collect_python_floor_source_falls_back_to_the_built_in_pin_when_neither_resolves(
    tmp_path, monkeypatch
):
    """State 3 of 3: no workspace resolves and `$ZEPHYR_BASE` is unset --
    `zephyr_python_floor` lands on `ZEPHYR_PYTHON_FLOOR`, and says so."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    checks = doctor_cmd._collect(None, workspace_root=str(outside))

    host_python = next(c for c in checks if c.name == "hostPython")
    assert "tan's built-in pin" in host_python.detail
    assert "no $ZEPHYR_BASE workspace" in host_python.detail
    pin = f"{doctor_cmd.ZEPHYR_PYTHON_FLOOR[0]}.{doctor_cmd.ZEPHYR_PYTHON_FLOOR[1]}"
    assert f"effective floor {pin}" in host_python.detail


def test_workspace_preflight_check_distinguishes_resolved_from_absent():
    assert doctor_cmd.workspace_preflight_check("/ws").status == "pass"
    absent = doctor_cmd.workspace_preflight_check(None)
    assert absent.status == "fail"
    assert "tan bootstrap" in absent.detail


def test_zephyr_version_preflight_check_distinguishes_a_match_from_a_patch_level_drift():
    """tan-cli#98: compared at full MAJOR.MINOR.PATCH -- a truncated compare
    would let v4.4.0 read as a match against a v4.4.1 pin."""
    match = doctor_cmd.zephyr_version_preflight_check("4.4.1", "4.4.1")
    assert match.status == "pass"

    drifted = doctor_cmd.zephyr_version_preflight_check("4.4.0", "4.4.1")
    assert drifted.status == "fail"  # tan-cli#159: FAIL, not warn
    assert "4.4.0" in drifted.detail and "4.4.1" in drifted.detail


def test_zephyr_version_preflight_check_is_skipped_when_unknown():
    assert doctor_cmd.zephyr_version_preflight_check(None, "4.4.1") is None
    assert doctor_cmd.zephyr_version_preflight_check("4.4.1", None) is None


def test_collect_leads_the_report_with_the_build_preflight_and_fails_a_workspaceless_host(
    tmp_path, monkeypatch
):
    """The regression tan-cli#100 names: before this, a host with no SDK
    selected and no Zephyr workspace reported `0 failed` on every other
    check -- these now report the gap themselves, and lead the check list
    (`nextSteps` follows check order).

    `boardYaml` is asserted separately (tan-cli#294 review): this bare
    `_collect` call with no `--project`/`--board-yaml` given is EXACTLY the
    #100(b) shape -- the checkout root `tan bootstrap` tells the customer to
    run `tan doctor` from next -- so `boardYaml` must Warn here, not Fail.
    The retired version of this test asserted the Fail as if it were
    correct, which is why it shipped; see
    `test_collect_reports_board_yaml_as_fail_only_when_a_project_was_selected`
    for both states.
    """
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    checks = doctor_cmd._collect(None, board_yaml=None, workspace_root=str(tmp_path))
    names = [c.name for c in checks]
    assert names[0] == "sdk"
    assert names[1] == "boardYaml"
    assert names[2] == "workspace"
    assert next(c for c in checks if c.name == "sdk").status == "fail"
    assert next(c for c in checks if c.name == "workspace").status == "fail"


def test_collect_reports_board_yaml_as_fail_only_when_a_project_was_selected(
    tmp_path, monkeypatch
):
    """The two-state pair the single-state version above let ship broken
    (tan-cli#294 review): the SAME missing board.yaml at the SAME
    bootstrapped checkout root warns with no project selected, and fails
    once a project is explicitly named -- mirrors the Rust oracle's
    `plain_doctor_does_not_fail_at_a_checkout_root_with_no_project_selected`
    (`crates/tan-cli/src/commands/doctor.rs:1848`)."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    unselected = doctor_cmd._collect(None, board_yaml=None, workspace_root=str(tmp_path))
    assert next(c for c in unselected if c.name == "boardYaml").status == "warn"

    selected = doctor_cmd._collect(
        None, board_yaml=None, project_scope=str(tmp_path), workspace_root=str(tmp_path)
    )
    assert next(c for c in selected if c.name == "boardYaml").status == "fail"


def test_collect_resolves_a_real_workspace_and_matching_zephyr_version(tmp_path, monkeypatch):
    """The other host state: an SDK resolved, a board.yaml present, a
    workspace resolved with a Zephyr matching the SDK's pin -- `zephyrVersion`
    and `zephyrWorkspace` (tan-cli#290, both sourced from these same resolved
    facts) agree, and all five Pass."""
    # Isolate from a developer/CI shell's own `$ZEPHYR_BASE`: the shared
    # resolver now consults it (step 2) before falling back to this SDK's
    # own `<sdk-parent>` layout (step 3), which is the resolution path this
    # test actually means to exercise.
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk_root = tmp_path / "workspace" / "alp-sdk"
    sdk_root.mkdir(parents=True)
    (sdk_root / "west.yml").write_text(
        "manifest:\n  projects:\n    - name: zephyr\n      revision: v4.4.1\n",
        encoding="utf-8",
    )
    (tmp_path / "workspace" / ".west").mkdir()
    zephyr = tmp_path / "workspace" / "zephyr"
    zephyr.mkdir()
    (zephyr / "VERSION").write_text(
        "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 1\n", encoding="utf-8"
    )
    board_yaml = tmp_path / "app" / "board.yaml"
    board_yaml.parent.mkdir(parents=True)
    board_yaml.write_text("", encoding="utf-8")

    checks = doctor_cmd._collect(
        str(sdk_root), board_yaml=str(board_yaml), workspace_root=str(tmp_path / "app")
    )
    for name in ("sdk", "boardYaml", "workspace", "zephyrVersion", "zephyrWorkspace"):
        check = next(c for c in checks if c.name == name)
        assert check.status == "pass", f"{name}: {check.detail}"


def test_collect_does_not_double_report_a_zephyr_version_mismatch(tmp_path, monkeypatch):
    """Regression, tan-cli#295 review: on a Zephyr-4.4.0-vs-`v4.4.1`-pin
    host, `zephyrWorkspace` used to report the identical fact `zephyrVersion`
    already Fails on, from the same two resolved inputs, under a second
    issue code (`doctor.zephyrWorkspace` alongside `doctor.zephyrVersion`)
    with a second `nextSteps` entry for the same `tan bootstrap` remedy.
    Scoped to these two checks and to `nextSteps` entries that actually
    mention bootstrap -- not `summary.fail`/`nextSteps` as a whole -- so this
    stays independent of whatever else a bare test host does or does not
    have on PATH. This must fail pre-fix and pass post-fix."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk_root = tmp_path / "workspace" / "alp-sdk"
    sdk_root.mkdir(parents=True)
    (sdk_root / "west.yml").write_text(
        "manifest:\n  projects:\n    - name: zephyr\n      revision: v4.4.1\n",
        encoding="utf-8",
    )
    (tmp_path / "workspace" / ".west").mkdir()
    zephyr = tmp_path / "workspace" / "zephyr"
    zephyr.mkdir()
    (zephyr / "VERSION").write_text(
        "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 0\n", encoding="utf-8"
    )
    board_yaml = tmp_path / "app" / "board.yaml"
    board_yaml.parent.mkdir(parents=True)
    board_yaml.write_text("", encoding="utf-8")

    checks = doctor_cmd._collect(
        str(sdk_root), board_yaml=str(board_yaml), workspace_root=str(tmp_path / "app")
    )
    version = next(c for c in checks if c.name == "zephyrVersion")
    workspace = next(c for c in checks if c.name == "zephyrWorkspace")
    assert version.status == "fail"
    assert workspace.status == "pass"

    # `zephyrVersion`'s own fix earns exactly one `nextSteps` entry -- not the
    # extra "Run `tan bootstrap` to refresh the workspace." the removed
    # `zephyrWorkspace` Fail used to add for the identical remedy.
    steps = doctor_cmd.next_steps(checks)
    assert steps.count(version.fix) == 1, steps
    assert "Run `tan bootstrap` to refresh the workspace." not in steps


# --------------------------------------------------------------------------
# tan-cli#294 finding 3 -- `posix_venv_unusable` never reached doctor,
# reintroducing tan-cli#161.
# --------------------------------------------------------------------------


def test_posix_venv_capable_distinguishes_a_working_probe_from_a_failing_one():
    working = doctor_cmd._posix_venv_capable([sys.executable, "-c", "import sys; sys.exit(0)"])
    assert working is True
    broken = doctor_cmd._posix_venv_capable([sys.executable, "-c", "import sys; sys.exit(1)"])
    assert broken is False


def test_posix_venv_capable_fails_open_when_the_probe_cannot_launch():
    """tan-cli#294 review: `probe()` collapses "ran and exited non-zero" and
    "could not run at all" to the same `None`, and this must NOT collapse
    them the same way -- an inconclusive answer (the probe never launched)
    must not refuse the host; only a probe that actually ran and exited
    non-zero does. Mirrors Rust's `venv_capable_fails_open_when_the_probe_
    cannot_launch` (`crates/tan-cli/src/util.rs:721`)."""
    unlaunchable = doctor_cmd._posix_venv_capable(["tan-cli-no-such-interpreter-xyz"])
    assert unlaunchable is True


def test_prerequisites_check_distinguishes_a_clean_host_from_a_venv_unusable_one():
    clean = doctor_cmd.prerequisites_check(
        checked=["git", "cmake", "python3"], missing=[], install={}, source="x"
    )
    assert clean.status == "pass"
    assert clean.missing is None

    refusal = doctor_cmd.posix_venv_unusable()
    unusable = doctor_cmd.prerequisites_check(
        checked=["git", "cmake", "python3"],
        missing=[],
        install={},
        source="x",
        venv_refusal=refusal,
    )
    assert unusable.status == "fail"
    assert unusable.code == "bootstrap.venv-unusable"
    assert "ensurepip" in unusable.detail
    # tan-cli#294 finding 4: the structured pair rides on the SAME check.
    assert unusable.missing == [
        {"tool": "python3-venv", "command": "sudo apt-get install -y python3-venv"}
    ]


def test_prerequisites_check_tool_missing_outranks_venv_unusable():
    """A missing TOOL is the more urgent fact -- if `python3` itself is
    missing there is nothing to run `ensurepip` against."""
    refusal = doctor_cmd.posix_venv_unusable()
    check = doctor_cmd.prerequisites_check(
        checked=["git", "cmake", "python3"],
        missing=["cmake"],
        install={"cmake": "sudo apt-get install -y cmake"},
        source="x",
        venv_refusal=refusal,
    )
    assert check.code == "bootstrap.prerequisites-missing"
    assert "cmake" in check.detail
    assert check.missing == [
        {"tool": "cmake", "command": "sudo apt-get install -y cmake"},
        {"tool": "python3-venv", "command": "sudo apt-get install -y python3-venv"},
    ]


# --------------------------------------------------------------------------
# tan-cli#294 finding 4 -- `data.missingPrerequisites`, #203/#210,
# alp-sdk-vscode#347, ADR 0021 P0a.
# --------------------------------------------------------------------------


def test_missing_prerequisites_key_is_populated_on_a_scrubbed_host(tmp_path):
    """The end-to-end wire: with PATH scrubbed, EVERY tool is missing, so
    `data.missingPrerequisites` must be a non-empty, runnable list -- never
    absent and never `[]` masquerading as `null`."""
    proc = run_tan("doctor", "--format", "json", cwd=tmp_path, scrub_path=True)
    envelope = json.loads(proc.stdout)
    missing = envelope["data"]["missingPrerequisites"]
    assert missing, "missingPrerequisites must be populated when tools are missing"
    for entry in missing:
        assert entry["tool"]
        assert "command" in entry  # present, possibly null -- never absent


def test_missing_prerequisites_key_is_present_and_null_when_the_check_is_clean():
    check = doctor_cmd.prerequisites_check(
        checked=["git"], missing=[], install={}, source="x"
    )
    assert check.missing is None


# --------------------------------------------------------------------------
# tan-cli#294 finding 5 -- `sdkProvenance` (no numbered GH issue).
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git must be on PATH for this test")
def test_sdk_provenance_check_distinguishes_a_real_git_checkout_from_a_plain_directory(
    tmp_path,
):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        )

    plain = tmp_path / "no-git"
    plain.mkdir()
    no_checkout = doctor_cmd.sdk_provenance_check(str(plain))
    assert no_checkout.status == "pass"
    assert "no git checkout" in no_checkout.detail

    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "x")
    head = git("rev-parse", "--short", "HEAD").stdout.strip()

    real_checkout = doctor_cmd.sdk_provenance_check(str(tmp_path))
    assert real_checkout.status == "pass"
    assert head in real_checkout.detail


@pytest.mark.skipif(shutil.which("git") is None, reason="git must be on PATH for this test")
def test_sdk_provenance_check_does_not_attribute_an_enclosing_repos_commit(tmp_path):
    """tan-cli#488 defect 4: `git -C <root> ...` discovery walks UPWARD, so an
    SDK with no `.git` of its own -- an extracted release archive vendored
    inside a customer's own application repository, a setup this port
    explicitly supports -- used to answer every git query with the ENCLOSING
    repo's HEAD instead of "not a checkout". Build exactly that: an outer git
    repo, and a plain subdirectory inside it standing in for a vendored SDK.
    """
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        )

    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "outer")
    outer_head = git("rev-parse", "--short", "HEAD").stdout.strip()

    vendored_sdk = tmp_path / "vendor" / "alp-sdk"
    vendored_sdk.mkdir(parents=True)
    (vendored_sdk / "metadata").mkdir()
    (vendored_sdk / "metadata" / "sdk_version.yaml").write_text("version: 0.42.0\n", encoding="utf-8")

    check = doctor_cmd.sdk_provenance_check(str(vendored_sdk))
    assert check.status == "pass"
    assert outer_head not in check.detail, check.detail
    # No commit attributed at all -- `detail` is bare "alp-sdk <version>", not
    # "alp-sdk <version> @ <commit>", proving `_git_short_commit` answered
    # `None` rather than the enclosing repo's HEAD.
    assert "@" not in check.detail, check.detail
    assert check.detail == "alp-sdk 0.42.0", check.detail
    # `_is_own_git_checkout` unit-level, directly: proves the guard itself,
    # not just its downstream effect on the assembled detail string.
    assert doctor_cmd._is_own_git_checkout(str(vendored_sdk)) is False
    assert doctor_cmd._is_own_git_checkout(str(tmp_path)) is True


def test_sdk_provenance_check_reads_the_sdk_version_file_when_present(tmp_path):
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "sdk_version.yaml").write_text("version: 0.42.0\n", encoding="utf-8")
    check = doctor_cmd.sdk_provenance_check(str(tmp_path))
    assert "0.42.0" in check.detail


def test_collect_reports_sdk_provenance_only_when_an_sdk_resolves(tmp_path):
    without_sdk = doctor_cmd._collect(None, workspace_root=str(tmp_path))
    assert "sdkProvenance" not in {c.name for c in without_sdk}

    with_sdk = doctor_cmd._collect(str(tmp_path), workspace_root=str(tmp_path))
    assert "sdkProvenance" in {c.name for c in with_sdk}


# --------------------------------------------------------------------------
# tan-cli#91 / ADR 0021 -- `doctor --fix` runs the manifest's own install
# commands for a missing `hostPrerequisites` tool, MAINTAINER DECISION:
# REFUSE AND PRINT anything needing `sudo`, never spawn it. `run_fix` is fed
# exactly `hostPrerequisites`'s own `Check.missing` -- never a second,
# independently recomputed tool/command list.
# --------------------------------------------------------------------------


def test_fix_needs_sudo_check_names_the_command_verbatim_and_never_hints_at_running_it():
    check = doctor_cmd.fix_needs_sudo_check("git", "sudo apt-get install -y git")
    assert check.status == "warn"
    assert check.code == "doctor.fix-needs-sudo"
    assert "sudo apt-get install -y git" in check.detail
    assert check.fix == "sudo apt-get install -y git"


def test_fix_installed_check_never_claims_the_tool_is_now_on_path():
    check = doctor_cmd.fix_installed_check("ninja", "winget install -e --id Ninja-build.Ninja")
    assert check.status == "warn"
    assert check.code == "doctor.fix-installed"
    assert "winget install -e --id Ninja-build.Ninja" in check.detail
    # tan-cli#91: no same-process re-check -- the honest outcome is "reopen
    # your shell", never a claimed-verified pass.
    assert "reopen" in check.detail or "new shell" in check.detail


def test_run_fix_refuses_a_sudo_command_and_never_spawns_it(monkeypatch):
    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("run_fix must never spawn a command needing sudo")

    monkeypatch.setattr(doctor_cmd.subprocess, "run", _must_not_run)
    monkeypatch.setattr(doctor_cmd, "on_path", _must_not_run)

    results = doctor_cmd.run_fix(
        [{"tool": "git", "command": "sudo apt-get install -y git"}]
    )
    assert len(results) == 1
    assert results[0].code == "doctor.fix-needs-sudo"


def test_run_fix_runs_a_no_elevation_command_through_the_resolved_binary(monkeypatch, tmp_path):
    fake_exe = tmp_path / "winget.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        doctor_cmd, "on_path", lambda name: str(fake_exe) if name == "winget" else None
    )
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(doctor_cmd.subprocess, "run", _fake_run)

    results = doctor_cmd.run_fix(
        [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    )
    assert len(results) == 1
    assert results[0].code == "doctor.fix-installed"
    # Resolved through `on_path`, never the bare tool name -- the same
    # PATH-only resolver every other spawn in this module goes through.
    assert captured["argv"][0] == str(fake_exe)
    assert captured["argv"][1:] == ["install", "-e", "--id", "Ninja-build.Ninja"]


def test_run_fix_skips_a_tool_with_no_known_install_command():
    assert doctor_cmd.run_fix([{"tool": "gperf", "command": None}]) == []


def test_run_fix_reports_a_verdict_when_the_installer_itself_is_not_on_path(monkeypatch):
    """tan-cli#360, replacing the test that pinned the silence
    (`assert results == []`): an unresolved installer used to be a bare
    `continue` -- the ONE outcome `run_fix` emitted no Check for, in a
    function whose whole stated invariant is that every entry is run or
    refused and every outcome becomes a check.

    Reachable on exactly the hosts `--fix` exists for: the manifest installs
    with `winget` on Windows and `brew` on macOS, so a machine without one
    reported tools missing, ACCEPTED `--fix`, and printed the same report
    back with no `fix:*` line anywhere -- indistinguishable from "nothing
    needed fixing"."""
    monkeypatch.setattr(doctor_cmd, "on_path", lambda _name: None)
    results = doctor_cmd.run_fix(
        [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    )
    assert len(results) == 1
    assert results[0].code == "doctor.fix-installer-not-found"
    assert results[0].status == "warn"
    # The installer AND the tool it blocked: naming only one of the two leaves
    # the reader unable to act on it.
    assert "winget" in results[0].detail
    assert "ninja" in results[0].detail
    # The remedy, not just "not found" -- on Windows that is App Installer.
    assert "App Installer" in results[0].detail


def test_run_fix_reports_one_missing_installer_once_not_once_per_tool(monkeypatch):
    """tan-cli#360 acceptance: a fresh Mac is missing every manifest
    prerequisite at once and every one of them installs with `brew`. Per-tool
    reporting would print the same "install Homebrew" paragraph six times --
    six restatements of one fact, in a report someone is reading to find out
    what is actually wrong."""
    monkeypatch.setattr(doctor_cmd, "on_path", lambda _name: None)
    results = doctor_cmd.run_fix(
        [
            {"tool": "git", "command": "brew install git"},
            {"tool": "cmake", "command": "brew install cmake"},
            {"tool": "ninja", "command": "brew install ninja"},
        ]
    )
    assert len(results) == 1
    assert results[0].name == "fix:brew"
    # One verdict, every tool that one absence blocked named inside it.
    for tool in ("git", "cmake", "ninja"):
        assert tool in results[0].detail
    assert results[0].detail.count("brew.sh") == 1


def test_run_fix_groups_per_installer_and_still_runs_the_ones_it_can_resolve(
    monkeypatch, tmp_path
):
    """Two distinct absent installers are two distinct verdicts -- grouping
    must not collapse unrelated remedies into one -- and a tool whose
    installer DOES resolve is still repaired in the same pass, so the
    grouping cannot swallow the working path."""
    fake_exe = tmp_path / "winget.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        doctor_cmd, "on_path", lambda name: str(fake_exe) if name == "winget" else None
    )
    monkeypatch.setattr(
        doctor_cmd.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0),
    )
    results = doctor_cmd.run_fix(
        [
            {"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"},
            {"tool": "git", "command": "brew install git"},
            {"tool": "cmake", "command": "pixi global install cmake"},
        ]
    )
    assert [c.code for c in results] == [
        "doctor.fix-installed",
        "doctor.fix-installer-not-found",
        "doctor.fix-installer-not-found",
    ]
    assert [c.name for c in results[1:]] == ["fix:brew", "fix:pixi"]
    # An installer this module has never heard of still gets a remedy that
    # names it, rather than a bare "not found".
    assert "Install `pixi`" in results[2].detail


def test_doctor_fix_explains_a_missing_installer_in_both_text_and_json(monkeypatch, tmp_path):
    """tan-cli#360 acceptance: BOTH output modes must say that no repair ran.
    Driven through the real command rather than `run_fix` directly, because
    the two modes render from different places -- JSON from `data.checks` +
    `issues`, text from a `print` loop over `data.checks` to stderr -- and a
    verdict reaching only one of them is the same bug wearing a different
    hat. `can_prompt` is stubbed for the reason the section header above
    gives: `CliRunner`'s pipes are never a tty."""
    missing = [{"tool": "ninja", "command": "brew install ninja"}]
    stub_checks = [doctor_cmd.Check("hostPrerequisites", "fail", "ninja missing", missing=missing)]
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: stub_checks)
    monkeypatch.setattr(doctor_cmd, "can_prompt", lambda **k: True)
    monkeypatch.setattr(doctor_cmd, "on_path", lambda _name: None)
    monkeypatch.chdir(tmp_path)

    as_json = json.loads(runner.invoke(app, ["doctor", "--fix", "--format", "json"]).output)
    assert "doctor.fix-installer-not-found" in {i["code"] for i in as_json["issues"]}
    json_detail = next(
        c["detail"] for c in as_json["data"]["checks"] if c["name"] == "fix:brew"
    )

    # Text mode prints every check to stderr, envelope-free.
    text_detail = runner.invoke(app, ["doctor", "--fix"]).stderr

    for detail in (json_detail, text_detail):
        assert "ran no repair" in detail
        assert "brew" in detail
        assert "ninja" in detail


def test_run_fix_reports_a_check_when_the_install_command_exits_non_zero(monkeypatch, tmp_path):
    """A customer who typed `--fix` and watched it do nothing must be able to
    tell "tan tried and the install itself failed" from "tan never tried" --
    the exact silence tan-cli#91's own review flagged (a bare `continue`
    here). `hostPrerequisites` still names the tool separately; this Check
    is the only place the FAILED ATTEMPT itself is reported."""
    fake_exe = tmp_path / "winget.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(doctor_cmd, "on_path", lambda _name: str(fake_exe))
    monkeypatch.setattr(
        doctor_cmd.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1),
    )
    results = doctor_cmd.run_fix(
        [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    )
    assert len(results) == 1
    assert results[0].code == "doctor.fix-failed"
    assert results[0].name == "fix:ninja"
    assert "1" in results[0].detail


def test_run_fix_reports_a_check_when_the_spawn_itself_raises(monkeypatch, tmp_path):
    fake_exe = tmp_path / "winget.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(doctor_cmd, "on_path", lambda _name: str(fake_exe))

    def _raise(*_a, **_k):
        raise OSError("no such file or directory")

    monkeypatch.setattr(doctor_cmd.subprocess, "run", _raise)
    results = doctor_cmd.run_fix(
        [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    )
    assert len(results) == 1
    assert results[0].code == "doctor.fix-spawn-failed"
    assert "no such file or directory" in results[0].detail


def test_run_fix_reports_a_check_when_the_install_command_times_out(monkeypatch, tmp_path):
    fake_exe = tmp_path / "winget.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(doctor_cmd, "on_path", lambda _name: str(fake_exe))

    def _timeout(argv, **k):
        raise subprocess.TimeoutExpired(argv, k.get("timeout", doctor_cmd.FIX_INSTALL_TIMEOUT_S))

    monkeypatch.setattr(doctor_cmd.subprocess, "run", _timeout)
    results = doctor_cmd.run_fix(
        [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    )
    assert len(results) == 1
    assert results[0].code == "doctor.fix-timed-out"
    assert str(doctor_cmd.FIX_INSTALL_TIMEOUT_S) in results[0].detail


def test_doctor_fix_is_disabled_under_ci_non_interactive_and_json(tmp_path):
    """tan-cli#91: `--fix` must never actually attempt a repair under
    `--ci`, `--non-interactive`, or `--format json` -- the idiom this
    codebase already applies to any command that would otherwise mutate the
    host rather than merely report on it."""
    for extra in (["--ci"], ["--non-interactive"], []):
        proc = run_tan(
            "doctor", "--fix", "--format", "json", *extra, cwd=tmp_path, scrub_path=True
        )
        envelope = json.loads(proc.stdout)
        names = {c["name"] for c in envelope["data"]["checks"]}
        assert not any(n.startswith("fix:") for n in names), (extra, names)
        assert not any(
            i["code"] in ("doctor.fix-needs-sudo", "doctor.fix-installed")
            for i in envelope["issues"]
        ), (extra, envelope["issues"])


def test_doctor_fix_interactive_with_nothing_resolvable_is_a_safe_no_op(tmp_path):
    """PATH scrubbed -- every `hostPrerequisites` tool is missing AND
    unresolvable via `on_path`, so an interactive `--fix` (the guard is
    satisfied: no `--ci`, no `--non-interactive`, text mode) reaches
    `run_fix` and finds nothing it can actually run. The report must stay
    well-formed and the exit code unchanged (still 4 -- `hostPrerequisites`
    is still failing, `--fix` ran and genuinely fixed nothing)."""
    proc = run_tan("doctor", "--fix", cwd=tmp_path, scrub_path=True)
    assert "Traceback" not in proc.stderr
    assert proc.returncode == 4


# --------------------------------------------------------------------------
# The `--fix` WIRING itself. `test_doctor_fix_is_disabled_under_ci_non_
# interactive_and_json` above passes `--format json` in EVERY loop
# iteration, so `json_mode` alone already satisfies every one of its
# assertions regardless of `--ci`/`--non-interactive` -- it cannot tell a
# correct guard from `if fix and not json_mode` (ignores `--ci`/
# `--non-interactive` entirely) or `if fix or True` (guard deleted, `--fix`
# ignored). And no `run_tan` subprocess test can ever grant consent at all:
# `can_prompt`'s two `isatty()` checks read `False` off a captured pipe every
# time, flags aside -- see `tan.core.consent`. In-process, via `CliRunner`
# and monkeypatching `doctor_cmd`'s own module attributes, is the only way to
# drive BOTH the consent-granted path and the guard's flag logic without
# spawning a real install.
# --------------------------------------------------------------------------


def test_doctor_fix_invokes_run_fix_and_folds_its_checks_into_the_report_when_consent_is_granted(
    monkeypatch, tmp_path
):
    """The positive case: with consent genuinely granted, `run_fix` must
    actually be called with `hostPrerequisites`'s OWN `missing` list, and its
    resulting Checks must reach the report -- not just "the guard didn't
    crash". Fails red against `checks = [*checks, *run_fix(missing_for_fix)]`
    replaced by `pass` (the feature unwired entirely): `run_fix` would never
    be called and its Check would never reach `data.checks`/`issues`."""
    missing = [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    stub_checks = [doctor_cmd.Check("hostPrerequisites", "fail", "ninja missing", missing=missing)]
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: stub_checks)
    monkeypatch.setattr(doctor_cmd, "can_prompt", lambda **k: True)

    calls = []

    def _spy_run_fix(missing_arg, on_check=None):  # noqa: ARG001 -- streaming covered elsewhere
        calls.append(missing_arg)
        return [doctor_cmd.fix_installed_check("ninja", missing[0]["command"])]

    monkeypatch.setattr(doctor_cmd, "run_fix", _spy_run_fix)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["doctor", "--fix", "--format", "json"])
    assert calls == [missing], calls
    envelope = json.loads(result.output)
    assert envelope["exitCode"] == 4  # hostPrerequisites is still a Fail
    names = [c["name"] for c in envelope["data"]["checks"]]
    assert "fix:ninja" in names, names
    codes = [i["code"] for i in envelope["issues"]]
    assert "doctor.fix-installed" in codes, codes


def test_doctor_fix_guard_honours_ci_even_in_text_mode(monkeypatch, tmp_path):
    """The negative case, through the REAL (unmonkeypatched) `can_prompt`:
    `--fix --ci` in TEXT mode (no `--format json`) must never call `run_fix`.
    Fails red against `if fix and not json_mode` (`--ci` plays no part in
    that condition, and text mode makes `not json_mode` true) and against
    `if fix or True` (guard deleted, always runs)."""
    missing = [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    stub_checks = [doctor_cmd.Check("hostPrerequisites", "fail", "ninja missing", missing=missing)]
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: stub_checks)

    calls = []
    monkeypatch.setattr(doctor_cmd, "run_fix", lambda m: calls.append(m) or [])
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["doctor", "--fix", "--ci"])
    assert calls == [], calls


def test_doctor_fix_guard_honours_no_tty_the_same_way_ci_does(monkeypatch, tmp_path):
    """Same shape as the `--ci` case above, but for the "unasked" half of
    `can_prompt` (`tan.core.consent`): even with none of `--ci`/
    `--non-interactive`/`--format json` passed, a non-terminal stdin/stderr
    (exactly what `CliRunner`/any captured-pipe run provides, and exactly
    what tan-cli#91's own postmortem measured -- a CI runner that redirected
    output but never passed `--ci`) must refuse `--fix` the same way `--ci`
    does."""
    missing = [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    stub_checks = [doctor_cmd.Check("hostPrerequisites", "fail", "ninja missing", missing=missing)]
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: stub_checks)

    calls = []
    monkeypatch.setattr(doctor_cmd, "run_fix", lambda m: calls.append(m) or [])
    monkeypatch.chdir(tmp_path)

    # No --ci, no --non-interactive, no --format json -- only `can_prompt`'s
    # own isatty() reads (both False under CliRunner) can be refusing this.
    runner.invoke(app, ["doctor", "--fix"])
    assert calls == [], calls


# --------------------------------------------------------------------------
# tan-cli#91 P1: `--fix` suppressed must SAY SO, not silently reproduce
# plain `tan doctor`'s report -- the oracle divergence on `doctor --fix
# --format json` (oracle: exitCode 2, cli.parse-error; this port: used to be
# byte-for-byte identical to plain `tan doctor`, no issue, no note).
# --------------------------------------------------------------------------


def test_fix_suppressed_issue_names_every_condition_that_tripped():
    issue = doctor_cmd.fix_suppressed_issue(non_interactive=False, ci=True, json_mode=True)
    assert issue.code == "doctor.fix-suppressed"
    assert issue.severity == "warning"
    assert "--ci" in issue.message
    assert "--format json" in issue.message


def test_fix_suppressed_issue_never_reads_isatty_under_json_mode(monkeypatch):
    """`tan.cli.main` tees `sys.stderr` through `_TeeStderr` under
    `--format json`, which has no `isatty()` at all -- reading it
    unconditionally here is the exact `AttributeError` measured against a
    real `tan doctor --fix --format json --ci` run. `json_mode=True` must
    short-circuit past both `isatty()` reads, mirroring `can_prompt`'s own
    order, not merely happen to avoid them under THIS monkeypatch."""

    class _NoIsatty:
        def isatty(self):
            raise AttributeError("'_TeeStderr' object has no attribute 'isatty'")

    monkeypatch.setattr(doctor_cmd.sys, "stdin", _NoIsatty())
    monkeypatch.setattr(doctor_cmd.sys, "stderr", _NoIsatty())
    issue = doctor_cmd.fix_suppressed_issue(non_interactive=False, ci=False, json_mode=True)
    assert issue.code == "doctor.fix-suppressed"
    assert "--format json" in issue.message


def test_fix_suppressed_issue_does_not_crash_when_stdin_is_none(monkeypatch):
    """tan-cli#488 defect 6: `sys.stdin` itself -- not just `sys.stderr` (see
    the sibling test above) -- can be `None`: a GUI-launched/`pythonw`-style
    process, or any host that runs tan with its standard handles detached. A
    bare `sys.stdin.isatty()` there raised `AttributeError: 'NoneType' object
    has no attribute 'isatty'`. Only reachable with `json_mode=False` -- that
    is the one case the `isatty()` pair is read at all.
    """
    monkeypatch.setattr(doctor_cmd.sys, "stdin", None)
    issue = doctor_cmd.fix_suppressed_issue(non_interactive=False, ci=False, json_mode=False)
    assert issue.code == "doctor.fix-suppressed"
    assert "no interactive terminal" in issue.message


def test_fix_suppressed_issue_does_not_crash_when_stderr_is_none(monkeypatch):
    """tan-cli#488 round 5: the sibling site of the same defect, in the same
    function. Round 4 guarded `sys.stdin is not None` here (the test above)
    but left `sys.stderr.isatty()` -- the NEXT operand of the identical `and`
    chain -- unguarded, so a host whose `stdin` is real (redirected to a real
    file, `.isatty()` returns `False` there rather than raising) but whose
    `stderr` alone is `None` still raised `AttributeError: 'NoneType' object
    has no attribute 'isatty'` one operand later. `sys.stdin` is left as
    whatever pytest's own capture already replaced it with (a real object,
    never `None`) precisely so this test cannot pass by accident via the
    OTHER guard."""
    monkeypatch.setattr(doctor_cmd.sys, "stderr", None)
    issue = doctor_cmd.fix_suppressed_issue(non_interactive=False, ci=False, json_mode=False)
    assert issue.code == "doctor.fix-suppressed"
    assert "no interactive terminal" in issue.message


def test_doctor_fix_with_stderr_none_does_not_crash_before_reaching_fix_suppressed_issue(
    monkeypatch, tmp_path
):
    """tan-cli#488 round 5, the CLI-level twin of the unit test above: proves
    `can_prompt` itself (`tan.core.consent`) -- not just `fix_suppressed_issue`
    -- tolerates a `None` `sys.stderr` when `sys.stdin` genuinely IS a tty.
    `doctor()`'s `fix_allowed = fix and can_prompt(...)` (doctor_cmd.py:3467)
    calls the REAL, unmonkeypatched `can_prompt` before `fix_suppressed_issue`
    ever runs, so a guard added only inside `fix_suppressed_issue` would leave
    THIS call to crash first, caught only by `doctor()`'s outer
    `except Exception` and reported as a fabricated `doctor.internal-failure`
    (exit 5) instead of the correct `doctor.fix-suppressed` warning (exit 4)
    -- exactly the round-2 regression `can_prompt` was centralised to close,
    recurring one operand later.

    `_NamedTextIOWrapper.isatty` is forced `True` (the same technique
    `test_doctor_no_color_flag_reaches_the_render_and_suppresses_ansi` uses)
    so `CliRunner`'s captured `sys.stdin` -- which `can_prompt` must reach and
    pass before it ever reads `sys.stderr` -- answers `True`, matching the
    real detached-process shape this defect needs: a live, non-tty-refusing
    stdin with stderr alone detached. `sys.stderr` is set to `None` as a side
    effect inside `_collect`, the same pattern
    `test_doctor_fix_with_stdin_none_does_not_crash_before_reaching_fix_suppressed_issue`
    uses for `sys.stdin`, for the identical reason: `CliRunner.invoke`'s own
    `isolation()` context manager swaps `sys.stderr` for its own captured
    stream at the START of every invocation, so a monkeypatch applied before
    `invoke()` is silently overwritten before `doctor()` ever runs.

    Unlike `sys.stdin`, `sys.stderr` cannot simply stay `None` for the rest
    of the invocation: `doctor()` itself keeps writing to it afterwards (the
    summary footer), and `CliRunner.invoke`'s own `finally: sys.stderr.flush()`
    runs once the command returns -- both would raise the same `AttributeError`
    for a reason that has nothing to do with this defect. `fix_suppressed_issue`
    is the LAST consumer of the `None` stderr in `doctor()`'s call order (after
    `can_prompt`), so wrapping it to restore the real stream in a `finally`,
    once its own body has run with `sys.stderr` still `None`, isolates the
    `None` window to exactly the two calls this test targets."""
    from typer.testing import _NamedTextIOWrapper

    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)
    missing = [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    stub_checks = [doctor_cmd.Check("hostPrerequisites", "fail", "ninja missing", missing=missing)]
    real_stderr_box: list[object] = []

    def _collect_stub(*a, **k):  # noqa: ARG001
        real_stderr_box.append(doctor_cmd.sys.stderr)
        doctor_cmd.sys.stderr = None
        return stub_checks

    real_fix_suppressed_issue = doctor_cmd.fix_suppressed_issue

    def _fix_suppressed_issue_wrapper(*a, **k):
        try:
            return real_fix_suppressed_issue(*a, **k)
        finally:
            doctor_cmd.sys.stderr = real_stderr_box[0]

    monkeypatch.setattr(doctor_cmd, "_collect", _collect_stub)
    monkeypatch.setattr(doctor_cmd, "fix_suppressed_issue", _fix_suppressed_issue_wrapper)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["doctor", "--fix"])

    assert "doctor.internal-failure" not in result.stderr, result.stderr
    assert "AttributeError" not in result.stderr, result.stderr
    assert "`--fix` was requested but not run" in result.stderr, result.stderr
    assert "no interactive terminal" in result.stderr, result.stderr
    assert result.exit_code == 4, result.stderr  # hostPrerequisites is still a Fail; not 5


def test_doctor_fix_with_stdin_none_does_not_crash_before_reaching_fix_suppressed_issue(
    monkeypatch, tmp_path
):
    """tan-cli#488 round 3: the sibling test above proves `fix_suppressed_issue`
    itself tolerates `sys.stdin is None` -- but that function is only ever
    reached AFTER `doctor()`'s own `fix_allowed = fix and can_prompt(...)`
    (doctor_cmd.py:3454) has already called the UNGUARDED `can_prompt`
    (`tan.core.consent`). A `sys.stdin is None` guard added only inside
    `fix_suppressed_issue` -- and not in `can_prompt` itself -- leaves that
    earlier call to crash first, caught only by `doctor()`'s outer
    `except Exception`, which discards the whole diagnosis and reports
    `doctor.internal-failure` (exit 5) instead of the correct
    `doctor.fix-suppressed` warning. Drives the REAL, unmonkeypatched
    `can_prompt` through the actual CLI dispatch -- not a direct call to
    `fix_suppressed_issue` -- because that earlier call site is exactly what
    round 2 left unfixed. Text mode, not `--format json`: `can_prompt` short-
    circuits past both `isatty()` reads under `json_mode=True`, so the crash
    is only reachable in text mode (measured against a real
    `tan doctor --fix` run with stdin closed before exec, `0<&-`).

    Sets `sys.stdin = None` from INSIDE the stubbed `_collect` -- not before
    `runner.invoke` -- because `CliRunner.invoke`'s own isolation() context
    manager swaps `sys.stdin` for its own captured stream at the start of
    every invocation (and restores it after), so a monkeypatch applied
    before `invoke()` is silently overwritten the moment the command
    actually runs, and this test would pass whether or not the bug is
    fixed (measured). `_collect` runs and returns before `doctor()` ever
    reaches `can_prompt` (line 3454), so setting it as a side effect there
    lands exactly where a real detached-stdio host already has
    `sys.stdin is None` by the time `--fix`'s consent gate is evaluated."""
    missing = [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    stub_checks = [doctor_cmd.Check("hostPrerequisites", "fail", "ninja missing", missing=missing)]

    def _collect_stub(*a, **k):  # noqa: ARG001
        doctor_cmd.sys.stdin = None
        return stub_checks

    monkeypatch.setattr(doctor_cmd, "_collect", _collect_stub)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["doctor", "--fix"])

    assert "doctor.internal-failure" not in result.stderr, result.stderr
    assert "AttributeError" not in result.stderr, result.stderr
    assert "`--fix` was requested but not run" in result.stderr, result.stderr
    assert "no interactive terminal" in result.stderr, result.stderr
    assert result.exit_code == 4, result.stderr  # hostPrerequisites is still a Fail; not 5


def test_doctor_fix_format_json_is_no_longer_a_silent_no_op(monkeypatch, tmp_path):
    """The exact reported shape: `doctor --fix --format json` on an
    unhealthy host used to be byte-for-byte identical to plain `tan doctor`.
    Now it must carry a `doctor.fix-suppressed` issue naming why, even though
    `run_fix` itself is never called."""
    missing = [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    stub_checks = [doctor_cmd.Check("hostPrerequisites", "fail", "ninja missing", missing=missing)]
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: stub_checks)
    calls = []
    monkeypatch.setattr(doctor_cmd, "run_fix", lambda m: calls.append(m) or [])
    monkeypatch.chdir(tmp_path)

    plain = runner.invoke(app, ["doctor", "--format", "json"])
    fixed = runner.invoke(app, ["doctor", "--fix", "--format", "json"])
    assert calls == [], calls

    plain_envelope = json.loads(plain.output)
    fixed_envelope = json.loads(fixed.output)
    assert fixed_envelope["exitCode"] == plain_envelope["exitCode"] == 4
    assert not any(i["code"] == "doctor.fix-suppressed" for i in plain_envelope["issues"])
    suppressed = [i for i in fixed_envelope["issues"] if i["code"] == "doctor.fix-suppressed"]
    assert len(suppressed) == 1, fixed_envelope["issues"]
    assert "--format json" in suppressed[0]["message"]


def test_doctor_fix_suppressed_notice_reaches_text_mode_not_just_json(monkeypatch, tmp_path):
    """tan-cli#375: `--fix` suppressed by the consent gate used to explain
    itself ONLY in `--format json` -- text mode printed exactly the same
    report as plain `tan doctor`, with nothing saying `--fix` had even been
    requested. The customer typed `--fix`, got a report, and had no way to
    tell "nothing needed fixing" from "tan declined to fix anything".

    No `--ci`/`--non-interactive`/`--format json` passed -- text mode, and
    the ONLY thing suppressing `--fix` is `can_prompt`'s own `isatty()` pair
    (`tan.core.consent`), which `CliRunner`'s captured pipes always fail.
    That is deliberately the exact shape tan-cli#91's own postmortem
    measured: a run that redirected its stdio without ever passing `--ci`.

    Fails red against the pre-#375 `else:` branch (`doctor()`'s text-mode
    rendering), which printed the per-check lines then jumped straight to
    the summary -- `issues` was read only in the `data is None` (exception)
    branch, so an issues-only entry like `fix_suppressed_issue` never
    reached stderr at all outside `--format json`."""
    missing = [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    stub_checks = [doctor_cmd.Check("hostPrerequisites", "fail", "ninja missing", missing=missing)]
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: stub_checks)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["doctor", "--fix"])

    # The plain sentence, and the SPECIFIC condition that tripped -- naming
    # "not interactive" alone is useless to someone who never realised their
    # CI runner has no TTY (the issue's own wording).
    assert "`--fix` was requested but not run" in result.stderr, result.stderr
    assert "no interactive terminal (stdin/stderr not a tty" in result.stderr, result.stderr
    # Findable, not buried mid-report: after every check line, and the report
    # still ends on the summary line, not the notice.
    notice_at = result.stderr.index("`--fix` was requested but not run")
    summary_at = result.stderr.rindex("passed,")
    assert notice_at < summary_at, result.stderr
    assert result.stderr.strip().endswith("failed.")


# --------------------------------------------------------------------------
# `--no-color` must actually REACH `doctor()` (UX-polish sweep, text-layout
# task). `accept_global_flags` (`tan/core/global_flags.py`) injects and then
# DROPS `--no-color`/`--ci` for any command that doesn't declare them itself
# -- `doctor()` didn't declare `--no-color`, so `tan doctor --no-color` was
# accepted and silently ignored (never regressed by a test, because nothing
# in `doctor` used colour yet either).
# --------------------------------------------------------------------------


def test_doctor_no_color_flag_reaches_the_render_and_suppresses_ansi(monkeypatch, tmp_path):
    """`CliRunner`'s own stderr is a `BytesIO`-backed stream, never a real
    tty, so colour is already off there regardless of `--no-color` -- that
    would make this test pass for the same wrong reason the bug shipped
    with. Forcing `isatty()` True on typer's own `_NamedTextIOWrapper` (the
    class both `sys.stdout` and `sys.stderr` are swapped to for the duration
    of `runner.invoke`) is what makes the flag's effect actually observable:
    plain `tan doctor` must now carry ANSI escapes and `--no-color` must
    strip them, through the REAL CLI dispatch (argument parsing ->
    `accept_global_flags` -> `doctor()`'s body -> `tan.env.use_color` ->
    `render_doctor_lines`), not a direct call to any one of those.

    `tan.env.use_color` also short-circuits on `NO_COLOR`'s presence in the
    real environment (`no_color_requested()`, tan-cli#288 -- correctly so,
    per spec). Left alone, this test's first assertion is a coin flip on
    whoever runs it: green on a shell with no `NO_COLOR`, red -- for the
    right reason, colour genuinely suppressed -- on one that has it set
    (CI runners commonly do). `NO_COLOR` is not something this test
    controls via any argument or fixture, so it must be neutralised
    explicitly, same as `isatty` is forced explicitly below. `--ci` needs
    no equivalent treatment: `ci` reaches `use_color` only as the `--ci`
    CLI flag's own value (default `False`), never read from an env var, so
    it is already fully controlled by the argv this test passes.

    `doctor` v2's streaming (change 3): each check's block now prints
    through the `on_check` callback `_collect` is given, at the moment
    `_collect` produces it -- a stub replacing `_collect` must call that
    callback itself, or its check is never printed at all (there is no
    separate batch-print step left to fall back on)."""
    from typer.testing import _NamedTextIOWrapper

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)
    stub_checks = [doctor_cmd.Check("west", "fail", "west not found")]

    def stub_collect(*a, on_check=None, **k):
        if on_check is not None:
            for check in stub_checks:
                on_check(check)
        return stub_checks

    monkeypatch.setattr(doctor_cmd, "_collect", stub_collect)
    monkeypatch.chdir(tmp_path)

    colored = runner.invoke(app, ["doctor"])
    plain = runner.invoke(app, ["doctor", "--no-color"])

    assert "\x1b[" in colored.stderr, colored.stderr
    assert "\x1b[" not in plain.stderr, plain.stderr


# --------------------------------------------------------------------------
# Streaming (`doctor` v2 task, change 3): each check's block prints the
# MOMENT that check completes, not after every check has been computed.
# --------------------------------------------------------------------------


def test_checks_stream_incrementally_not_in_one_final_flush(monkeypatch, tmp_path):
    """Proves ORDERING/interleaving, not just final content -- a test that
    only inspects the finished stderr text would pass equally well against
    the OLD batched code (compute every check first, print everything at the
    very end) and prove nothing about this change.

    `doctor_cmd._collect` is stubbed to accept the real `on_check` callback
    `doctor()`'s text branch wires up, and to record a marker into the SAME
    event list BETWEEN its two calls -- standing in for a slow probe still
    running for the second check. `builtins.print` is wrapped (not replaced:
    it still forwards to the real `print`, so `CliRunner` still captures
    stderr normally) to append every printed line to that one list too. If
    streaming works, the first check's line is already in `events` before
    the marker; if the code regressed to batching, the marker would come
    first, or nothing would separate the two checks' output at all.
    """
    events: list[str] = []

    def fake_collect(*args, on_check=None, **kwargs):  # noqa: ARG001
        first = doctor_cmd.Check("alpha", "pass", "alpha detail")
        second = doctor_cmd.Check("beta", "pass", "beta detail")
        if on_check is not None:
            on_check(first)
        # Stand-in for the second check's own slow probe -- spelled without
        # "alpha"/"beta" so it cannot accidentally satisfy either substring
        # search below.
        events.append("MARKER-still-probing")
        if on_check is not None:
            on_check(second)
        return [first, second]

    monkeypatch.setattr(doctor_cmd, "_collect", fake_collect)
    monkeypatch.chdir(tmp_path)

    real_print = print

    def recording_print(*args, **kwargs):
        if args:
            events.append(str(args[0]))
        real_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", recording_print)

    result = runner.invoke(app, ["doctor", "--no-color"])
    assert result.exit_code == 0, result.stderr

    alpha_at = next(i for i, e in enumerate(events) if "alpha" in e)
    marker_at = events.index("MARKER-still-probing")
    beta_at = next(i for i, e in enumerate(events) if "beta" in e)
    assert alpha_at < marker_at < beta_at, events


# --------------------------------------------------------------------------
# Review finding 2: `_print_check`'s own `print` (via `_print_stream_lines`)
# is a FOURTH way `_collect` touches the outside world, and unlike
# `probe`/`on_path`/`_read_text` it CAN raise. Measured -- see
# `_print_stream_lines`'s docstring for the mechanism, not restated here --
# it is NOT a legacy-codepage stderr (`_reconfigure_stdio()` already forces
# utf-8/strict before any command runs); it is a lone surrogate in a printed
# filesystem path, a closed pipe, or a stream closed outright. This test
# forces the failure directly (a monkeypatched `print`) rather than
# reproducing any one specific trigger, so it covers the GUARD regardless of
# which of the three tripped it. Unguarded, that exception used to unwind
# through `_collect` into `doctor()`'s own `except Exception`, converting an
# ALREADY-COMPLETED diagnosis into `doctor.internal-failure` -- discarding
# real information (the probe genuinely ran and answered) over a print-layer
# hiccup.
# --------------------------------------------------------------------------


def test_a_print_failure_does_not_discard_an_already_completed_diagnosis(monkeypatch, tmp_path):
    """`builtins.print` is made to fail on exactly the rendered check-block
    line (it always starts with `"["`, see `render_check_lines`'s `prefix`),
    so the guard's own fallback note -- plain text, no leading `"["` -- still
    goes through the real `print`. Proves both halves finding 2 asks for:
    the finished `west` verdict survives (exit 0, no internal-failure), and
    the guard is not silent about WHY that check's own block did not print."""
    stub_checks = [doctor_cmd.Check("west", "pass", "west 1.2.0 found")]

    def stub_collect(*a, on_check=None, **k):
        if on_check is not None:
            for check in stub_checks:
                on_check(check)
        return stub_checks

    monkeypatch.setattr(doctor_cmd, "_collect", stub_collect)
    monkeypatch.chdir(tmp_path)

    real_print = print

    def flaky_print(*args, **kwargs):
        text = str(args[0]) if args else ""
        if text.startswith("["):
            raise UnicodeEncodeError("cp1252", text, 0, 1, "ordinal not in range(128)")
        real_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", flaky_print)

    result = runner.invoke(app, ["doctor", "--no-color"])

    # The real verdict (`west` passed) survives -- exit 0, not the fabricated
    # `INTERNAL_FAILURE` a discarded diagnosis would produce.
    assert result.exit_code == 0, result.stderr
    assert "doctor.internal-failure" not in result.stderr
    assert "could not print the 'west' block" in result.stderr


# --------------------------------------------------------------------------
# Review finding 3: `--fix`'s checks used to stream only after `run_fix`
# returned IN FULL -- the largest blank-terminal window in the whole command
# (`FIX_INSTALL_TIMEOUT_S` (300s) times up to 4 tools, against
# `PROBE_TIMEOUT_S` (15s) for the probes that already streamed). `run_fix`
# now takes the same `on_check` shape `_collect` does.
# --------------------------------------------------------------------------


def test_run_fix_streams_each_outcome_via_on_check_as_it_happens(monkeypatch, tmp_path):
    """Proves ORDERING, the same way
    `test_checks_stream_incrementally_not_in_one_final_flush` proves it for
    `_collect`: a marker recorded between two tools' `subprocess.run` calls
    must land between their two `on_check` firings, not before both (the old,
    print-after-`run_fix`-returns shape) or after both."""
    fake_exe = tmp_path / "tool.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(doctor_cmd, "on_path", lambda _name: str(fake_exe))

    events: list[str] = []

    def fake_run(argv, **k):  # noqa: ARG001
        events.append(f"RUN-{argv[-1].lstrip('-')}")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(doctor_cmd.subprocess, "run", fake_run)

    def on_check(check):
        events.append(f"CHECK-{check.name}")

    missing = [
        {"tool": "ninja", "command": "installer --ninja"},
        {"tool": "dtc", "command": "installer --dtc"},
    ]
    results = doctor_cmd.run_fix(missing, on_check=on_check)

    assert events == ["RUN-ninja", "CHECK-fix:ninja", "RUN-dtc", "CHECK-fix:dtc"], events
    assert [c.name for c in results] == ["fix:ninja", "fix:dtc"]


def test_doctor_fix_passes_a_real_on_check_to_run_fix_in_text_mode_and_none_under_json(
    monkeypatch, tmp_path
):
    """Wiring, not `run_fix`'s own streaming internals (covered above):
    `doctor()` must actually PASS a callable into `run_fix` when text mode is
    streaming (never just accept the parameter and leave it unused), and
    must NOT print `--fix` checks under `--format json` -- the same `stream`
    gate `_collect` is given, applied consistently to the second caller."""
    missing = [{"tool": "ninja", "command": "winget install -e --id Ninja-build.Ninja"}]
    stub_checks = [doctor_cmd.Check("hostPrerequisites", "fail", "ninja missing", missing=missing)]
    monkeypatch.setattr(doctor_cmd, "_collect", lambda *a, **k: stub_checks)
    monkeypatch.setattr(doctor_cmd, "can_prompt", lambda **k: True)

    seen_on_check = []

    def _spy_run_fix(missing_arg, on_check=None):  # noqa: ARG001
        seen_on_check.append(on_check)
        return []

    monkeypatch.setattr(doctor_cmd, "run_fix", _spy_run_fix)
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["doctor", "--fix", "--no-color"])
    runner.invoke(app, ["doctor", "--fix", "--format", "json"])

    assert callable(seen_on_check[0]), seen_on_check  # text mode: a real callback
    assert seen_on_check[1] is None, seen_on_check  # json mode: never streamed


# --------------------------------------------------------------------------
# tan-cli#488 defect 8 -- a deleted cwd must produce an envelope, not a
# traceback with empty stdout.
# --------------------------------------------------------------------------


def test_a_deleted_working_directory_still_produces_an_envelope(monkeypatch):
    """`doctor()`'s prologue -- `Path.cwd()` through `Project.resolved(...)`
    and the SDK-root ladder -- used to run OUTSIDE the `try`/`except` that
    turns a raise into the `doctor.internal-failure` envelope: `Path.cwd()`
    throws `FileNotFoundError` when the working directory has been deleted
    out from under the process (entirely reachable -- a cleanup script
    racing a `tan doctor` run in the same shell), and that raise used to
    unwind through typer's own traceback renderer instead: empty stdout, a
    bare non-zero exit, no error on either side of the CLI/extension seam.
    `flash_cmd`/`build_cmd`/`validate_cmd` already guard the identical shape
    (`flash_cmd.workspace_root`'s own docstring calls it "the port's
    recurring double fault").
    """
    def gone():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(doctor_cmd.Path, "cwd", staticmethod(gone))

    result = runner.invoke(app, ["doctor", "--format", "json"])
    assert result.stdout, "must never be empty stdout"
    payload = json.loads(result.stdout)
    assert payload["command"] == "doctor"
    assert payload["ok"] is False
    assert result.exit_code == payload["exitCode"] == int(doctor_cmd.ExitCode.INTERNAL_FAILURE)
    # An envelope, whatever the outcome -- both `project` keys present (null
    # is fine; typer's own traceback renderer would have produced no JSON at
    # all).
    assert set(payload["project"]) == {"root", "boardYaml"}
    assert payload["issues"], "a failure must always carry an issue"
    assert any(i["code"] == "doctor.internal-failure" for i in payload["issues"]), payload["issues"]
