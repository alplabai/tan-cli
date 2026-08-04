# SPDX-License-Identifier: Apache-2.0
"""`scripts/verify_binary.sh` must survive its own documented invocation.

tan-cli#361: the script's usage line advertises a generic `<path-to-binary>`
and the surrounding docs use RELATIVE examples (`dist/tan/tan`), but checks
3-5 run after a `cd` into a throwaway project -- so a relative `$BIN` passed
1/5 and 2/5 and then died with `./dist/tan/tan: not found`, and a relative
`$SDK` silently became a nonexistent path under the temp dir when handed to
the binary as `--sdk-root`. Every CI call site passes `$PWD/...`, so no
release build could ever discriminate.

The binary under test is a ~30-line `sh` stub, not a real freeze: what is
being proved is the SCRIPT's path handling, and a PyInstaller freeze takes
minutes to produce and is absent on a dev box. The stub answers all five
checks and additionally asserts that the `--sdk-root` it receives is the SDK
the caller named -- the half of #361 the issue title does not mention.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_binary.sh"

#: Git Bash's `sh` is bash in POSIX mode and would happily accept a bashism,
#: so `dash` is run too wherever it exists (it ships with Git for Windows and
#: is /bin/sh on Debian) -- that is the shell the "POSIX sh, no bashisms"
#: header is actually a claim about. Neither is assumed present.
SHELLS = [s for s in ("sh", "dash") if shutil.which(s)]

pytestmark = pytest.mark.skipif(not SHELLS, reason="needs a POSIX sh on PATH")

#: Stands in for the frozen binary. Contains the literal `truststore` because
#: check 5/5 greps $BIN's own bytes for it. `$STUB_SDK_MARKER` is written in
#: by the fixture: the stub refuses a `--sdk-root` that does not contain it,
#: which is what fails if $SDK is left relative across the cd.
STUB = """#!/bin/sh
# Answers the five checks in verify_binary.sh. The name truststore appears
# here so that check 5/5's `grep -q truststore "$BIN"` finds it.
set -eu
case "${1:-}" in
--version) echo "tan 0.0.0-stub" ;;
init)
    mkdir -p src
    : >board.yaml
    echo 'int main(void) { return 0; }' >src/main.c
    echo '{"ok":true}'
    ;;
generate)
    out=; sdk=
    while [ $# -gt 0 ]; do
        case $1 in
        --help) echo "  --output PATH  where to write the emitted file"; exit 0 ;;
        --output) out=$2; shift ;;
        --sdk-root) sdk=$2; shift ;;
        esac
        shift
    done
    [ -f "$sdk/MARKER" ] || { echo "stub: --sdk-root '$sdk' is not the SDK" >&2; exit 1; }
    mkdir -p "$(dirname "$out")"
    echo "CONFIG_STUB=y" >"$out"
    echo '{"ok":true}'
    ;;
*) echo "stub: unexpected argv: $*" >&2; exit 2 ;;
esac
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """`bin/tan` (+ the `_internal/` sibling 5/5 needs) and `sdk/`, both under
    one root -- so the same tree can be addressed relatively or absolutely."""
    stub = tmp_path / "bin" / "tan"
    stub.parent.mkdir()
    stub.write_text(STUB, encoding="utf-8", newline="\n")
    stub.chmod(0o755)  # Python writes 0o644; sh would refuse to exec it
    ca = tmp_path / "bin" / "_internal" / "certifi" / "cacert.pem"
    ca.parent.mkdir(parents=True)
    ca.write_text("-- stub CA bundle --\n", encoding="utf-8", newline="\n")
    (tmp_path / "sdk").mkdir()
    (tmp_path / "sdk" / "MARKER").write_text("", encoding="utf-8", newline="\n")
    return tmp_path


def run(shell: str, workspace: Path, binary: str, sdk: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [shell, SCRIPT.as_posix(), binary, sdk],
        cwd=workspace,
        capture_output=True,
        text=True,
        # The script writes into cwd, but `mktemp -d` still needs somewhere to
        # go, and the ambient env decides which SDK a real tan would resolve.
        env={**os.environ, "ALP_SDK_ROOT": ""},
    )


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "binary, sdk",
    [
        # THE #361 CASE: fails on the unfixed script at check 3/5 with
        # `./bin/tan: not found`, after 1/5 and 2/5 have already gone green.
        ("./bin/tan", "./sdk"),
        # No `./` prefix -- a bare relative path is the other half of what the
        # usage line's `dist/tan/tan` example looks like when pasted.
        ("bin/tan", "sdk"),
        # The shape CI passes, and the only shape that ever worked.
        ("<abs>/bin/tan", "<abs>/sdk"),
        # Mixed, because the two arguments are canonicalized separately.
        ("./bin/tan", "<abs>/sdk"),
        ("<abs>/bin/tan", "./sdk"),
    ],
)
def test_relative_and_absolute_paths_both_reach_the_end(
    workspace: Path, shell: str, binary: str, sdk: str
) -> None:
    root = workspace.as_posix()
    done = run(shell, workspace, binary.replace("<abs>", root), sdk.replace("<abs>", root))
    assert done.returncode == 0, f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
    # Past check 2 is the bar the issue sets; assert the whole run anyway,
    # since a stub that satisfies 3-5 costs nothing extra and 4/5 is where a
    # relative $SDK -- the half #361's title omits -- would surface.
    assert done.stdout.rstrip().splitlines()[-1].startswith("OK: ")


@pytest.mark.parametrize("shell", SHELLS)
def test_a_missing_binary_fails_before_anything_runs(workspace: Path, shell: str) -> None:
    """Canonicalizing a path that is not there must not turn into a bare `cd`
    error from inside a command substitution."""
    done = run(shell, workspace, "./bin/nope", "./sdk")
    assert done.returncode == 1
    assert "no such binary: ./bin/nope" in done.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_a_missing_sdk_fails_before_anything_runs(workspace: Path, shell: str) -> None:
    done = run(shell, workspace, "./bin/tan", "./nope")
    assert done.returncode == 1
    assert "no such alp-sdk checkout: ./nope" in done.stderr
