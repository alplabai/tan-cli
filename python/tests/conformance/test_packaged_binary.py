# SPDX-License-Identifier: Apache-2.0
"""The packaged artifact must satisfy the extension's own probe: ``--version``
first line matches /^tan \\d+\\.\\d+\\.\\d+/, answering inside the extension's
3 s budget (alp-sdk-vscode/src/alpCli/vscodeAdapter.ts:288-290).

From tan-cli#349 the shipped shape is a PyInstaller --onedir freeze, archived
for release, not a single raw binary: ``dist/tan/`` is the unpacked folder
(``dist/tan/tan[.exe]`` + ``dist/tan/_internal/``) and ``dist/tan.zip`` /
``dist/tan.tar.gz`` is the archive `scripts/build_binary.sh` actually ships.
Running ``--version`` against the already-unpacked folder (rather than
unpacking the archive first) is deliberate: it is what install.sh hands the
launcher script it writes, and what the extension's own unpack step (a
SEPARATE unit of #349, on the alp-sdk-vscode side) will hand the resolved
binary path -- the archive itself is inert until something has unpacked it,
and testing that unpack step is not this file's job.

Skips when neither ``dist/tan/tan[.exe]`` nor a quarantined archive is present
so the normal suite is unaffected; run ``scripts/build_binary.sh`` to produce
them.
"""
import json
import re
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import pytest

PYTHON_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = PYTHON_ROOT / "dist" / "tan"
BINARY = DIST_DIR / ("tan.exe" if sys.platform == "win32" else "tan")
ARCHIVE_EXT = "zip" if sys.platform == "win32" else "tar.gz"
ARCHIVE = PYTHON_ROOT / "dist" / f"tan.{ARCHIVE_EXT}"
#: Where `scripts/build_binary.sh` moves the ARCHIVE (not the onedir folder)
#: when it breaks its ceiling, so that no consumer can `cp` it -- `exit 1`
#: alone is defeatable by a pipe.
QUARANTINE = ARCHIVE.with_name(ARCHIVE.name + ".oversized")

pytestmark = pytest.mark.skipif(
    not BINARY.exists() and not QUARANTINE.exists(),
    reason="run scripts/build_binary.sh first",
)


def _ceilings() -> dict[str, int]:
    """The ceilings as `scripts/build_binary.sh` sees them.

    READ from `scripts/artifact_ceilings.env`, never copied: this constant and
    the one in the build script were two copies of 15000000, and the pair
    rejected a correct aarch64 musl build (15277408 B) with neither copy
    pointing at the other. The file is `KEY=VALUE` so `sh` can source it, which
    makes it parseable here in four lines.
    """
    values = {}
    for line in (PYTHON_ROOT / "scripts" / "artifact_ceilings.env").read_text(
        encoding="utf-8"
    ).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = int(value)
    return values


def _max_artifact_bytes() -> int:
    """The ceiling for THIS host's libc. musl links libc statically and runs
    ~1.0-1.4 MB larger than its glibc twin, so one number cannot cover both.

    `sysconfig`'s host triplet is the interpreter's own build target
    (`x86_64-pc-linux-musl` on Alpine), which is what decides the frozen
    artifact's size; `platform.libc_ver()` looks for glibc strings and answers
    `('', '')` on musl, so it cannot be used for this. The `ld-musl-*` probe
    covers a build of CPython whose triplet is unset.
    """
    ceilings = _ceilings()
    triplet = (sysconfig.get_config_var("HOST_GNU_TYPE") or "").lower()
    is_musl = "musl" in triplet or any(Path("/lib").glob("ld-musl-*"))
    return ceilings[
        "TAN_MAX_ARTIFACT_BYTES_MUSL" if is_musl else "TAN_MAX_ARTIFACT_BYTES_DEFAULT"
    ]


@pytest.fixture(scope="session", autouse=True)
def _refuse_a_quarantined_build():
    """A quarantined artifact fails EVERY test in this module, loudly.

    `dist/` is gitignored build output, so the state "the build broke its
    ceiling" would otherwise read as "no binary here yet" and skip the whole
    module -- the same silence the pipe bug produced.
    """
    if QUARANTINE.exists():
        pytest.fail(
            f"{QUARANTINE} exists ({QUARANTINE.stat().st_size} B): the build "
            f"broke its ceiling and was quarantined. Fix the build (or the "
            f"ceiling in scripts/artifact_ceilings.env) and rebuild.",
            pytrace=False,
        )


def test_artifact_ships_as_onedir_plus_one_archive():
    # tan-cli#349: --onedir, not --onefile -- the folder IS the deliverable now,
    # and the archive built from it (never the folder itself) is the one thing
    # every downstream consumer (checksums.txt, install.sh, the extension's own
    # unpack step) deals with as a single file.
    assert DIST_DIR.is_dir(), f"{DIST_DIR} must be the --onedir folder"
    assert BINARY.is_file(), f"{BINARY} missing from the onedir folder"
    if not QUARANTINE.exists():
        assert ARCHIVE.is_file(), f"{ARCHIVE} missing -- build_binary.sh archives dist/tan/ into one file"


def test_artifact_was_built_from_a_clean_interpreter():
    # The 3 s probe below does NOT catch a dirty build: an artifact built off an
    # interpreter carrying numpy/Pillow/pywin32 measured 34349423 B and ~1.00 s
    # -- 3x the size and 2x the startup, still comfortably green. Size is the
    # only signal separating the two. Measured over the ARCHIVE (what
    # `scripts/build_binary.sh` ceiling-checks and what actually ships), not the
    # unpacked folder -- see that script's own comment on why.
    size = ARCHIVE.stat().st_size
    ceiling = _max_artifact_bytes()
    assert size < ceiling, (
        f"{ARCHIVE} is {size} B against a {ceiling} B ceiling -- likely built "
        f"from a dirty interpreter that pulled in modules tan never imports; "
        f"see scripts/build_binary.sh. If the venv is clean, measure and edit "
        f"scripts/artifact_ceilings.env (both readers share it)."
    )


def test_both_ceilings_are_defined_and_ordered():
    """The parse itself is load-bearing: a typo in `artifact_ceilings.env` --
    the file the build script SOURCES -- would otherwise surface here as a
    KeyError inside another test, or not at all if the file grew a third key
    nobody reads. musl must be the larger of the two; if that ever inverts, one
    of the numbers was edited without the measurement behind it."""
    ceilings = _ceilings()

    assert set(ceilings) == {
        "TAN_MAX_ARTIFACT_BYTES_DEFAULT",
        "TAN_MAX_ARTIFACT_BYTES_MUSL",
    }, ceilings
    assert (
        ceilings["TAN_MAX_ARTIFACT_BYTES_MUSL"]
        > ceilings["TAN_MAX_ARTIFACT_BYTES_DEFAULT"]
    ), "musl links libc statically and is the larger artifact"
    # Both must stay well under the 34349423 B dirty-interpreter measurement, or
    # the ceiling has stopped telling a dirty build from a legitimate one.
    assert max(ceilings.values()) < 34_349_423 * 0.6, ceilings


def test_version_line_matches_the_extension_regex():
    out = subprocess.run(
        [str(BINARY), "--version"], capture_output=True, text=True, encoding="utf-8"
    )
    first = out.stdout.splitlines()[0]
    assert re.match(r"^tan \d+\.\d+\.\d+", first), f"got: {first!r}"


def test_version_probe_completes_within_the_3s_budget():
    start = time.monotonic()
    subprocess.run(
        [str(BINARY), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=3,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"--version took {elapsed:.2f}s; extension probe timeout is 3s"
    print(f"\nstartup: {elapsed:.3f}s")


def test_version_probe_stays_within_the_onedir_budget():
    """A dedicated, TIGHT regression gate for tan-cli#349 -- separate from the
    3 s test above, which merely enforces the extension's actual probe
    timeout and is loose enough that a --onefile-style regression could land
    and still pass it silently: measured on THIS host, a --onefile build of
    the same commit answers --version in 0.833-0.875 s (10 runs, mean
    0.855 s) -- comfortably under the 3 s test, so that test alone would not
    have caught the regression even on the machine sitting right here. (Only
    macOS actually missed the 3 s budget outright, on the unsigned
    re-extracted-dylib verification cost: 13.25-19.74 s, measured on the
    published v0.5.0-rc4 asset -- this suite has no macOS runner to reproduce
    that number with.)

    Threshold is picked from THIS repo's own onedir measurement, checked
    against the same host's --onefile build rather than inferred:

      onedir  (this build):  10 runs, 0.274-0.340 s, mean 0.294 s
      onefile (same commit): 10 runs, 0.833-0.875 s, mean 0.855 s

    0.6 s sits in the ~0.49 s gap between them -- about 1.8x the onedir
    ceiling above its own worst run (room for a slower/loaded CI box) while
    staying a comfortable 0.23 s under the onefile floor, so a build that
    silently reverts to --onefile fails THIS test on this very platform, not
    only on macOS's much larger margin. Re-verified directly against a real
    --onefile build of this binary before picking the number (the tan-cli#323
    lesson: prove a regression gate against the known-bad build, don't infer
    it would fail from a different platform's numbers). A future change that
    reintroduces per-invocation extraction trips this gate long before it
    would threaten the extension's own 3 s timeout, or even get near it --
    which is the point: the 3 s test alone was not tight enough to have
    caught the original regression, and the e2e harness (getting-started.yml)
    asserts correctness only, never speed.
    """
    start = time.monotonic()
    subprocess.run(
        [str(BINARY), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 0.6, (
        f"--version took {elapsed:.2f}s -- over the 0.6s onedir budget "
        f"(measured local baseline: 0.27-0.34s over 10 runs; a --onefile "
        f"build of the same commit measured 0.83-0.88s, well above this "
        f"threshold). This is the tan-cli#349 regression gate: something in "
        f"this build re-added per-invocation extraction cost -- check "
        f"scripts/build_binary.sh is still building --onedir, not --onefile."
    )
    print(f"\nonedir startup: {elapsed:.3f}s")


def test_the_artifact_carries_its_scaffold_templates(tmp_path):
    """`tan init`'s vendored scaffold trees are DATA, so PyInstaller's static
    import graph does not reach them -- they ship only because
    `scripts/build_binary.sh` passes `--add-data`. Nothing in the source-run
    suite can notice that flag going missing, and the symptom in the field is a
    customer's FIRST command failing.

    `zephyr-app` deliberately: it is the non-interactive default AND vendored, so
    this is the literal `tan init` path. `--preview` keeps it read-only.
    """
    out = subprocess.run(
        [str(BINARY), "init", "--template", "zephyr-app", "--preview", "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
    )
    envelope = json.loads(out.stdout)
    codes = [issue["code"] for issue in envelope.get("issues", [])]

    # `dist/` is gitignored build output, so it can easily be an artefact built
    # before this command existed. That is a STALE BINARY, not a packaging
    # regression, and it has its own signature (Click rejects the unknown
    # subcommand). The failure this test is for looks nothing like it:
    # `init.template-unreadable`, exit 5, from a build that dropped --add-data.
    if out.returncode == 2 and "cli.parse-error" in codes:
        pytest.skip("dist/ predates `tan init` -- rerun scripts/build_binary.sh")

    assert out.returncode == 0, envelope.get("issues")
    assert len(envelope["data"]["fileChanges"]) >= 6
    assert list(tmp_path.iterdir()) == [], "--preview must not touch disk"


def test_the_artifact_carries_click_testing(tmp_path):
    """`tan --help --format json` must not be a dead path in a frozen build.

    tan-cli#810 moved `from click.testing import CliRunner` out of `cli.py`'s
    module scope and into `_emit_help_envelope`, which is the ONLY production
    use of `click.testing` in the package. That took it off every path
    `verify_binary.sh` exercises -- its `--version` check used to be what
    proved the module was in the freeze, and after #810 it no longer touches
    it. `build_binary.sh` passes no `--hidden-import` at all, so a freeze that
    dropped `click.testing` would pass all five of those checks and fail first
    at a customer, on the one flag combination the extension uses.

    The repo has been here before: deferring `import serial` came with
    `test_the_artifact_carries_pyserial` below for exactly this reason. This
    is that test for the other deferral. `--help` under `--format json` is the
    only argv that reaches `CliRunner`, so it is the only argv that proves it.
    """
    out = subprocess.run(
        [str(BINARY), "--help", "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
        timeout=30,
    )
    assert "ModuleNotFoundError" not in out.stderr, (
        "the frozen artifact is missing a module on the `--help --format json` "
        f"path -- `click.testing` is the candidate this test exists for:\n{out.stderr}"
    )
    assert out.stdout.strip(), f"no envelope on stdout; stderr:\n{out.stderr}"
    envelope = json.loads(out.stdout)
    assert envelope["command"] == "cli"
    assert envelope["ok"] is True, envelope.get("issues")
    assert envelope["data"]["message"].startswith("Usage: tan"), envelope["data"]


def test_the_artifact_carries_pyserial(tmp_path):
    """`tan monitor` must not be a dead command in a frozen build.

    pyserial is an EXTRA (`[project.optional-dependencies] monitor`), so the
    freeze only carries it if the build venv installed `.[monitor]` -- and
    `release.yml`, the workflow a `v*` tag actually runs, froze a bare `.` while
    `python-binaries.yml` froze the extra. Every published asset would have
    advertised `tan monitor` in `--help` and then refused to run it, with a hint
    the holder of a --onefile binary cannot act on.

    Both outcomes here exit non-zero on a runner with no serial hardware, so the
    return code proves nothing and the ISSUE CODE is the whole signal:
    `monitor.pyserial-missing` means the extra was dropped;
    `monitor.no-port` means pyserial loaded and enumerated an empty bus, which
    is the pass. Deliberately no `--port`: opening a real device is not this
    test's business, and the enumeration alone touches pyserial.
    """
    out = subprocess.run(
        [str(BINARY), "monitor", "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
        timeout=30,
    )
    envelope = json.loads(out.stdout)
    codes = [issue["code"] for issue in envelope.get("issues", [])]

    # Same stale-binary escape as above: a `dist/` predating `tan monitor`
    # reports Click's unknown-subcommand parse error, not a missing extra.
    if out.returncode == 2 and "cli.parse-error" in codes:
        pytest.skip("dist/ predates `tan monitor` -- rerun scripts/build_binary.sh")

    assert "monitor.pyserial-missing" not in codes, (
        "the frozen binary has no pyserial: the build venv installed `.` instead "
        'of `".[monitor]"`. See python/scripts/build_binary.sh. Issues: '
        f"{envelope.get('issues')}"
    )
