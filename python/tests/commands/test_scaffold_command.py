# SPDX-License-Identifier: Apache-2.0
"""``tan scaffold`` -- adds one module into an EXISTING project (#260).

Driven as a real subprocess, like ``test_init_command.py``/
``test_build_command.py``: the things worth asserting are ONE JSON document on
stdout, the exit code, and that no input can replace the envelope with a
Python traceback. Every shape asserted here was measured against the frozen
Rust oracle (``target/debug/tan.exe --format json scaffold ...``) rather than
inferred from ``crates/tan-cli/src/commands/scaffold.rs`` alone -- see the
inline comments naming what was actually run.

``pytest`` gives this subprocess no controlling terminal, so ``--name``/
``--template`` are effectively ALWAYS required here regardless of whether
``--non-interactive`` is passed explicitly -- the same "no CI runner has a
TTY" fact ``tan.commands.scaffold_cmd``'s own module docstring documents for
the oracle. That is exactly the behaviour under test, not a limitation of it:
the whole point of ``--name``'s non-interactive contract is that it never
silently prompts a caller that cannot answer.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.commands.scaffold_cmd import _resolve_module_name, _resolve_template
from tan.core.module_template import MODULE_TEMPLATE_IDS

#: ``python/`` -- pinned onto the child's PYTHONPATH so ``python -m tan``
#: resolves from a scratch cwd without a ``pip install``.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

WINDOWS = os.name == "nt"


def run_tan(*argv, cwd, env_extra=None):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
        **(env_extra or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
    )


def envelope(proc):
    """The one JSON document on stdout. Fails loudly on zero or two -- both
    are the same break for a consumer that parses stdout whole."""
    assert proc.stdout.strip(), f"no envelope on stdout; stderr:\n{proc.stderr}"
    assert "Traceback" not in proc.stderr, f"an exception escaped the contract:\n{proc.stderr}"
    return json.loads(proc.stdout)


def issue(env):
    assert env["issues"], "expected at least one issue"
    return env["issues"][0]


def tree(root: Path):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _make_dir_link(link: Path, target: Path) -> bool:
    """A directory link `link` -> `target`: a Windows JUNCTION (no elevated
    privilege needed) or a POSIX symlink. `False` when the host refuses to
    make one at all. Same tradeoff `test_init_command.py`/`test_scaffold.py`
    already make for the identical reason."""
    target.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        return made.returncode == 0
    link.symlink_to(target, target_is_directory=True)
    return True


# ---------------------------------------------------------------------------
# --name is required, non-interactively
# ---------------------------------------------------------------------------


def test_missing_name_fails_validation_json(tmp_path):
    """Measured: `tan --format json scaffold` (no --name, this subprocess has
    no TTY) -> exit 2, `scaffold.name-required`. NOT a default -- unlike
    `tan init`'s `--name`, a module scaffold has no sane one."""
    proc = run_tan("scaffold", "--format", "json", cwd=tmp_path)
    env = envelope(proc)

    assert proc.returncode == 2
    assert env["ok"] is False
    assert issue(env)["code"] == "scaffold.name-required"
    assert env["project"]["root"] is None
    assert env["data"]["templateId"] == ""
    assert list(tmp_path.iterdir()) == [], "a validation failure must not touch disk"


def test_missing_name_fails_validation_text_mode(tmp_path):
    """Text mode: nothing on stdout (the envelope channel stays JSON-only),
    the human line on stderr, exit 2 -- matches the oracle's `tan scaffold`
    with no TTY attached (measured: stdout empty, stderr carries the line)."""
    proc = run_tan("scaffold", cwd=tmp_path)

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "Module name is required" in proc.stderr


def test_non_interactive_flag_reports_the_same_refusal(tmp_path):
    proc = run_tan("scaffold", "--non-interactive", "--format", "json", cwd=tmp_path)
    env = envelope(proc)
    assert proc.returncode == 2
    assert issue(env)["code"] == "scaffold.name-required"


# ---------------------------------------------------------------------------
# --preview
# ---------------------------------------------------------------------------


def test_preview_writes_nothing_at_all(tmp_path):
    proc = run_tan(
        "scaffold", "--name", "my-sensor", "--preview", "--format", "json", cwd=tmp_path
    )
    env = envelope(proc)

    assert proc.returncode == 0
    assert env["data"]["preview"] is True
    assert env["data"]["templateId"] == "sensor-driver"  # non-interactive default
    assert env["data"]["normalizedModuleName"] == "my_sensor"
    assert [c["kind"] for c in env["data"]["fileChanges"]] == ["new", "new", "new"]
    assert env["data"]["written"] == []
    assert list(tmp_path.iterdir()) == [], "--preview must not touch disk"


def test_preview_of_a_project_with_local_edits_still_answers(tmp_path):
    """The overwrite guard must stay BEHIND the preview branch -- a read-only
    question has nothing to guard (the same ordering bug `tan init` fixed;
    ``scaffold.rs`` checks `--preview` before the guard for the same reason)."""
    assert (
        run_tan(
            "scaffold", "--name", "foo", "--template", "sensor-driver",
            "--format", "json", cwd=tmp_path,
        ).returncode
        == 0
    )
    header = tmp_path / "include" / "modules" / "foo.h"
    header.write_text(header.read_text(encoding="utf-8") + "// local edit\n", encoding="utf-8")

    proc = run_tan(
        "scaffold", "--name", "foo", "--template", "sensor-driver",
        "--preview", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, "a preview must never fail on disk state"
    kinds = {c["relativePath"]: c["kind"] for c in env["data"]["fileChanges"]}
    assert kinds["include/modules/foo.h"] == "update"
    assert "// local edit" in header.read_text(encoding="utf-8"), "preview overwrote a local edit"


# ---------------------------------------------------------------------------
# Write / rerun / overwrite guard / --force
# ---------------------------------------------------------------------------


def test_write_creates_the_three_files(tmp_path):
    proc = run_tan(
        "scaffold", "--name", "my-conn", "--template", "connectivity-service",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0
    assert env["ok"] is True
    assert sorted(env["data"]["written"]) == [
        "include/modules/my_conn.h",
        "src/modules/my_conn/README.md",
        "src/modules/my_conn/my_conn.c",
    ]
    assert env["data"]["unchanged"] == []
    assert tree(tmp_path) == [
        "include/modules/my_conn.h",
        "src/modules/my_conn/README.md",
        "src/modules/my_conn/my_conn.c",
    ]


def test_rerun_with_no_changes_reports_unchanged_not_written(tmp_path):
    args = ("scaffold", "--name", "my-conn", "--template", "connectivity-service", "--format", "json")
    first = run_tan(*args, cwd=tmp_path)
    assert first.returncode == 0

    second = run_tan(*args, cwd=tmp_path)
    env = envelope(second)

    assert second.returncode == 0
    assert env["data"]["written"] == []
    assert sorted(env["data"]["unchanged"]) == [
        "include/modules/my_conn.h",
        "src/modules/my_conn/README.md",
        "src/modules/my_conn/my_conn.c",
    ]


def test_overwrite_guard_refuses_without_force(tmp_path):
    args = ("scaffold", "--name", "foo", "--format", "json")
    assert run_tan(*args, cwd=tmp_path).returncode == 0
    edited = tmp_path / "src" / "modules" / "foo" / "foo.c"
    edited.write_text(edited.read_text(encoding="utf-8") + "// hand edit\n", encoding="utf-8")

    proc = run_tan(*args, cwd=tmp_path)
    env = envelope(proc)

    assert proc.returncode == 3  # ExitCode.WRITE_FAILURE
    assert issue(env)["code"] == "scaffold.would-overwrite"
    assert env["data"]["written"] == []
    assert "// hand edit" in edited.read_text(encoding="utf-8"), "refused write must not touch disk"


def test_force_allows_the_overwrite(tmp_path):
    args = ["scaffold", "--name", "foo", "--format", "json"]
    assert run_tan(*args, cwd=tmp_path).returncode == 0
    edited = tmp_path / "src" / "modules" / "foo" / "foo.c"
    edited.write_text(edited.read_text(encoding="utf-8") + "// hand edit\n", encoding="utf-8")

    proc = run_tan(*args, "--force", cwd=tmp_path)
    env = envelope(proc)

    assert proc.returncode == 0
    assert env["data"]["written"] == ["src/modules/foo/foo.c"]
    assert "// hand edit" not in edited.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_invalid_template_reports_coded_issue(tmp_path):
    proc = run_tan(
        "scaffold", "--name", "foo", "--template", "bogus", "--format", "json", cwd=tmp_path
    )
    env = envelope(proc)

    assert proc.returncode == 2
    assert issue(env)["code"] == "scaffold.invalid-template"
    assert "bogus" in issue(env)["message"]
    assert env["project"]["root"] is None


def test_name_that_normalizes_to_empty_reports_coded_issue(tmp_path):
    proc = run_tan("scaffold", "--name", "!!!", "--format", "json", cwd=tmp_path)
    env = envelope(proc)

    assert proc.returncode == 2
    assert issue(env)["code"] == "scaffold.invalid-name"


@pytest.mark.parametrize("template_id", MODULE_TEMPLATE_IDS)
def test_every_registered_template_plans_three_files(template_id, tmp_path):
    proc = run_tan(
        "scaffold", "--name", "mod", "--template", template_id,
        "--preview", "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0, env["issues"]
    assert env["data"]["templateId"] == template_id
    assert len(env["data"]["fileChanges"]) == 3


# ---------------------------------------------------------------------------
# --destination / --project
# ---------------------------------------------------------------------------


def test_destination_flag_wins_over_project(tmp_path):
    (tmp_path / "sub").mkdir()
    proc = run_tan(
        "scaffold", "--name", "bar", "--destination", "sub", "--project", "elsewhere",
        "--format", "json", cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 0
    assert env["project"]["root"] == "sub"
    assert (tmp_path / "sub" / "include" / "modules" / "bar.h").is_file()


def test_project_flag_used_when_no_destination(tmp_path):
    (tmp_path / "sub").mkdir()
    proc = run_tan(
        "scaffold", "--name", "baz", "--project", "sub", "--format", "json", cwd=tmp_path
    )
    env = envelope(proc)

    assert proc.returncode == 0
    assert env["project"]["root"] == "sub"
    assert (tmp_path / "sub" / "include" / "modules" / "baz.h").is_file()


# ---------------------------------------------------------------------------
# The oracle's global flag set: accepted even though scaffold reads almost
# none of them (`--project` is the one exception).
# ---------------------------------------------------------------------------


def test_every_global_flag_is_accepted_and_ignored(tmp_path):
    proc = run_tan(
        "scaffold", "--name", "qux", "--preview",
        "--board-yaml", str(tmp_path / "nonexistent.yaml"),
        "--sdk-root", str(tmp_path / "nonexistent-sdk"),
        "--target", "zephyr-conf", "--all", "--verbose", "--quiet", "--no-color",
        "--non-interactive", "--ci",
        "--format", "json",
        cwd=tmp_path,
    )
    env = envelope(proc)
    assert proc.returncode == 0
    assert env["data"]["moduleName"] == "qux"


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


def test_envelope_has_every_contract_key(tmp_path):
    proc = run_tan(
        "scaffold", "--name", "shapecheck", "--preview", "--format", "json", cwd=tmp_path
    )
    env = envelope(proc)
    assert set(env.keys()) == {"command", "ok", "exitCode", "project", "data", "issues"}
    assert env["command"] == "scaffold"
    assert env["ok"] == (proc.returncode == 0)
    assert env["exitCode"] == proc.returncode
    assert "sdk" not in env  # scaffold never resolves an SDK (I-32)


# ---------------------------------------------------------------------------
# tan-cli#325: writes are confined to the project root
# ---------------------------------------------------------------------------


def test_write_refuses_through_a_symlinked_parent_directory(tmp_path):
    """`<project>/include` is a pre-existing directory link to somewhere
    outside the project. `tan.core.scaffold.write_files` (reused here, not
    reimplemented) must refuse the whole run rather than following the link
    and reporting the in-project logical path as written."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    if not _make_dir_link(project / "include", outside):
        pytest.skip("cannot create a directory link on this host")

    proc = run_tan(
        "scaffold", "--name", "esc", "--destination", str(project), "--format", "json",
        cwd=tmp_path,
    )
    env = envelope(proc)

    assert proc.returncode == 3  # ExitCode.WRITE_FAILURE
    assert env["ok"] is False
    assert issue(env)["code"] == "scaffold.write-failed"
    assert not any(outside.rglob("*")), "nothing may land outside the project through the link"


# ---------------------------------------------------------------------------
# tan-cli#496 defect 6: prompts must ride stderr, never stdout
# ---------------------------------------------------------------------------
#
# Unit-level, not the `run_tan` subprocess harness above: pytest gives that
# subprocess no controlling terminal at all, so `--name`/`--template`
# refuse before either prompt is ever reached (the module docstring's own
# "no CI runner has a TTY" point) -- there is no way to exercise the
# INTERACTIVE branch through it. `click.prompt` itself is monkeypatched to
# capture its kwargs, which proves the fix directly rather than depending
# on a real terminal/pty to observe which stream a question landed on.


def test_resolve_module_name_prompts_with_err_true(monkeypatch):
    """`click.prompt`'s own default is `err=False` -- the question would go
    to stdout, corrupting whatever a piped/redirected run is carrying there
    and leaving a real terminal on stderr blank (measured against the
    frozen oracle: `inquire::Text` renders to stderr)."""
    calls = []

    def fake_prompt(text, **kwargs):
        calls.append((text, kwargs))
        return "a-module"

    monkeypatch.setattr("tan.commands.scaffold_cmd.click.prompt", fake_prompt)
    assert _resolve_module_name(None, interactive=True) == "a-module"
    assert len(calls) == 1
    assert calls[0][1].get("err") is True


def test_resolve_template_prompts_with_err_true(monkeypatch):
    calls = []

    def fake_prompt(text, **kwargs):
        calls.append((text, kwargs))
        return MODULE_TEMPLATE_IDS[0]

    monkeypatch.setattr("tan.commands.scaffold_cmd.click.prompt", fake_prompt)
    assert _resolve_template(None, interactive=True) == MODULE_TEMPLATE_IDS[0]
    assert len(calls) == 1
    assert calls[0][1].get("err") is True
