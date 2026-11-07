# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1570: `scripts/e2e-full.sh`'s `sdk list --online` canary must not
score red on GitHub's ANONYMOUS rate limit (`sdk.fetch-failed`, HTTP 403
`rate limit exceeded`) -- the shared per-IP runner quota, not the freeze --
while every other failure still does.

The harness functions are cut out of the real script and run under `bash`
against a fake `tan`, so the test breaks if the script regresses."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.gates.test_e2e_full_excerpt_shows_the_compile_error import _bash_available

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "e2e-full.sh"
FUNCS = ("ok", "bad", "note", "jget", "envelope_issues", "jrun", "jscore",
         "sdklist_rate_limited", "sdklist_canary")

pytestmark = pytest.mark.skipif(not _bash_available(), reason="needs a real bash")


def _function(lines: list[str], name: str) -> str:
    for i, line in enumerate(lines):
        if line.startswith(f"{name}()"):
            if line.rstrip().endswith("}"):
                return line
            end = next(j for j in range(i + 1, len(lines)) if lines[j] == "}")
            return "\n".join(lines[i : end + 1])
    raise AssertionError(f"e2e-full.sh has no `{name}()`")


def _envelope(rc: int, message: str) -> str:
    issues = [] if rc == 0 else [{"code": "sdk.fetch-failed", "severity": "error", "message": message}]
    return json.dumps({"ok": rc == 0, "exitCode": rc, "issues": issues, "data": {}})


def _run_canary(tmp_path: Path, rc: int, message: str) -> subprocess.CompletedProcess[str]:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    funcs = "\n".join(_function(lines, name) for name in FUNCS)
    (tmp_path / "env.json").write_text(_envelope(rc, message), encoding="utf-8")
    fake = tmp_path / "tan"
    fake.write_text(f'#!/bin/sh\necho call >> "{tmp_path.as_posix()}/calls"\n'
                    f'cat "{tmp_path.as_posix()}/env.json"\nexit {rc}\n', encoding="utf-8")
    fake.chmod(0o755)
    script = (f'set -u\nPASS=0; FAIL=0; FAILED_NAMES=""\nWORK="{tmp_path.as_posix()}"\n'
              f'TAN="{fake.as_posix()}"\nSDKLIST_RETRY_DELAY_S=0\n{funcs}\n'
              'sdklist_canary\necho "RESULT PASS=$PASS FAIL=$FAIL"\n')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)


def _calls(tmp_path: Path) -> int:
    return len((tmp_path / "calls").read_text().splitlines())


def test_an_exhausted_anonymous_rate_limit_warns_and_does_not_fail(tmp_path):
    out = _run_canary(tmp_path, 1, "HTTP Error 403: rate limit exceeded").stdout
    assert "RESULT PASS=0 FAIL=0" in out
    assert "::warning::" in out and "tan-cli#304" in out
    assert _calls(tmp_path) == 3  # retried before giving up


def test_any_other_fetch_failure_still_fails(tmp_path):
    out = _run_canary(tmp_path, 1, "<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]>").stdout
    assert "RESULT PASS=0 FAIL=1" in out
    assert "sdklist: expected exit 0, got 1" in out
    assert _calls(tmp_path) == 1  # not retried: only the quota is


def test_a_403_without_rate_limit_still_fails(tmp_path):
    out = _run_canary(tmp_path, 1, "HTTP Error 403: Forbidden").stdout
    assert "RESULT PASS=0 FAIL=1" in out


def test_success_is_scored_once(tmp_path):
    out = _run_canary(tmp_path, 0, "").stdout
    assert "RESULT PASS=1 FAIL=0" in out
    assert _calls(tmp_path) == 1
