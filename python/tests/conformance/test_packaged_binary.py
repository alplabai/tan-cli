# SPDX-License-Identifier: Apache-2.0
"""The packaged artifact must satisfy the extension's own probe: a single file
whose ``--version`` first line matches /^tan \\d+\\.\\d+\\.\\d+/, answering inside
the extension's 3 s budget (alp-sdk-vscode/src/alpCli/vscodeAdapter.ts:288-290).

Skips when ``dist/tan[.exe]`` is absent so the normal suite is unaffected; run
``scripts/build_binary.sh`` to produce it.
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
BINARY = PYTHON_ROOT / "dist" / ("tan.exe" if sys.platform == "win32" else "tan")
#: Where `scripts/build_binary.sh` moves an artifact that broke its ceiling, so
#: that no consumer can `cp` it -- `exit 1` alone is defeatable by a pipe.
QUARANTINE = BINARY.with_name(BINARY.name + ".oversized")

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


def test_artifact_is_a_single_file():
    # --onedir would hand the extension a directory it has no unpack step for
    # (alp-sdk-vscode/src/alpCli/download.ts:159-162 writes the body to ONE path).
    assert BINARY.is_file(), "must be --onefile: the extension cannot unpack a directory"


def test_artifact_was_built_from_a_clean_interpreter():
    # The 3 s probe below does NOT catch a dirty build: an artifact built off an
    # interpreter carrying numpy/Pillow/pywin32 measured 34349423 B and ~1.00 s
    # -- 3x the size and 2x the startup, still comfortably green. Size is the
    # only signal separating the two.
    size = BINARY.stat().st_size
    ceiling = _max_artifact_bytes()
    assert size < ceiling, (
        f"{BINARY} is {size} B against a {ceiling} B ceiling -- likely built "
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
