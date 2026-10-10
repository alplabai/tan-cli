# SPDX-License-Identifier: Apache-2.0
"""tan-cli#810: a real `tan` invocation must not load a dependency that
exactly one command needs.

`cli.py` static-imports every command module, so any module-scope `import` in
any `*_cmd.py` is paid by EVERY invocation -- `tan --version` included. Four
are heavy and single-use: `click.testing` (one call, on the
`--help --format json` path), `jsonschema` (one call, in `new_som_cmd`'s
`_schema_errors`), `PyYAML` (`new_som_cmd` + `kconfig_cmd`) and
`urllib.request` (`sdk_cmd`, network only). With what they pull behind them --
`attr`, `referencing`, `jsonschema_specifications`, `email`, `http`, `ssl` --
they were about a third of the startup import graph.

TWO PROBES, AND THE SECOND IS THE REAL ONE. `import tan.cli` is the cheap
question and it is NOT the rule: an adversarial review of the first version of
this file built a tree that loaded MORE modules and ran SLOWER than the
pre-fix one while every assertion here stayed green, simply by moving cost
somewhere `import tan.cli` does not reach (a Typer callback body, a
`__main__` shim, a lazily-imported module that itself imports eagerly). What
a user pays is what the PROCESS loads, so the second probe runs the actual
CLI over `--version` -- the cheapest real argv there is -- and asks the same
question of that. A regression that hides from the first probe has to hide
from both.

WHY NAMED MODULES AND NOT A COUNT. A `len(sys.modules)` ceiling looks
stricter and is worse: it moves with the Python version, the platform and any
stdlib module some unrelated import happens to pull, so it fails for reasons
that have nothing to do with this rule and gets raised until it means
nothing. The invariant is "these four are not loaded unless used".

WHY A SUBPROCESS. By the time pytest reaches this file it has imported `yaml`
and `jsonschema` itself, so an in-process check passes no matter what the CLI
does -- the test would report green while measuring the test runner.

SCOPE, stated because the first draft overstated it. This is startup cost.
It is NOT a size gate: nothing here measures the PyInstaller freeze, and the
one artifact consequence this change does have -- `click.testing` losing its
only unconditional import, so `verify_binary.sh`'s checks stop reaching it --
is covered by `tests/conformance/test_packaged_binary.py::
test_the_artifact_carries_click_testing`, against a real built binary, not
here.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

#: (module, the place that pulls it into the CLI's graph). "Sole user" is
#: about THIS graph, not the repo: `tan/planner/`'s seven modules also import
#: PyYAML at module scope, and `tan.planner.loader` imports `jsonschema` --
#: none of them is reachable from `tan.cli` (only the `tan.planner_root` /
#: `tan.planner_emit` shims are), which is exactly why deferring the command
#: -side imports is enough to keep them out.
_MUST_NOT_LOAD = (
    ("click.testing", "tan/cli.py's `--help --format json` path"),
    ("jsonschema", "tan/commands/new_som_cmd.py's `_schema_errors`"),
    ("yaml", "tan/model/perf.py, tan/commands/{new_som_cmd,kconfig_cmd}.py"),
    ("urllib.request", "tan/commands/sdk_cmd.py's release fetch"),
)

_IMPORT_ONLY = textwrap.dedent(
    """
    import sys
    import tan.cli  # noqa: F401
    print("\\n".join(sorted(sys.modules)))
    """
)

#: The same question asked of a real run. `--version` is the cheapest argv the
#: CLI has, so anything it loads is a floor every other command also pays.
_REAL_VERSION_RUN = textwrap.dedent(
    """
    import sys
    sys.argv = ["tan", "--version"]
    import tan.cli
    try:
        tan.cli.main()
    except SystemExit:
        pass
    print("\\n".join(sorted(sys.modules)))
    """
)


def _modules_loaded_by(script: str) -> set[str]:
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "the probe interpreter failed, which is a much bigger problem than "
        f"this gate's subject:\n{proc.stderr}"
    )
    return {line for line in proc.stdout.split() if not line.startswith("tan ")}


@pytest.fixture(scope="module")
def after_import() -> set[str]:
    return _modules_loaded_by(_IMPORT_ONLY)


@pytest.fixture(scope="module")
def after_version_run() -> set[str]:
    return _modules_loaded_by(_REAL_VERSION_RUN)


@pytest.mark.parametrize(("module", "sole_user"), _MUST_NOT_LOAD, ids=lambda v: v.split("/")[-1])
def test_a_single_use_dependency_does_not_load_on_a_bare_cli_import(
    module, sole_user, after_import
):
    assert module not in after_import, (
        f"`import tan.cli` pulls in `{module}`, which only {sole_user} needs "
        f"(tan-cli#810). Move the import into the function that uses it, with "
        f"a `# noqa: PLC0415` marker -- `tan/commands/monitor_cmd.py`'s "
        f"`import serial` is the in-tree pattern. If a NEW caller genuinely "
        f"needs it at module scope, say which one here and drop the entry "
        f"deliberately rather than deleting this assertion."
    )


@pytest.mark.parametrize(("module", "sole_user"), _MUST_NOT_LOAD, ids=lambda v: v.split("/")[-1])
def test_a_single_use_dependency_does_not_load_on_a_real_version_run(
    module, sole_user, after_version_run
):
    """The probe that cannot be dodged by moving cost off the import graph:
    this is a real `tan --version`, driven through `tan.cli.main`."""
    assert module not in after_version_run, (
        f"a real `tan --version` loads `{module}`, which only {sole_user} "
        f"needs (tan-cli#810). It is not enough for `import tan.cli` to stay "
        f"clean -- what a user pays is what the PROCESS loads, and every "
        f"command pays whatever `--version` does."
    )


def test_the_real_run_is_not_cheaper_than_the_import(after_import, after_version_run):
    """Anti-dodge. The two probes must stay coupled: a `--version` that
    somehow loaded FEWER modules than importing `tan.cli` would mean the run
    no longer goes through the module this gate watches, and the second probe
    would be measuring a different program from the first."""
    assert after_import <= after_version_run, sorted(after_import - after_version_run)


def test_the_probes_actually_observe_the_cli():
    """Anti-vacuity. Every assertion above is a NEGATIVE, so a probe that
    silently stopped exercising the CLI -- a renamed module, a subprocess that
    failed in a way the returncode did not show, an `argv` the entrypoint no
    longer recognises -- would report eight passes having measured nothing.
    Assert what must be PRESENT, and that the real run really ran."""
    imported = _modules_loaded_by(_IMPORT_ONLY)
    assert "tan.cli" in imported, sorted(m for m in imported if m.startswith("tan"))
    assert "typer" in imported, "tan.cli no longer imports typer; this gate watches the wrong thing"

    proc = subprocess.run(
        [sys.executable, "-c", _REAL_VERSION_RUN.replace('print("\\n".join(sorted(sys.modules)))', "")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("tan "), (
        "the `--version` probe did not print a version, so the second probe "
        f"is not exercising the CLI at all. stdout={proc.stdout!r} "
        f"stderr={proc.stderr!r}"
    )
