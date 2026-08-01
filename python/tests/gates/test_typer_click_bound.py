# SPDX-License-Identifier: Apache-2.0
"""tan-cli#293: `pyproject.toml`'s `typer` bound must stay TIGHT and TRUE.

Two ways this drifts, and both are gated here:

  * the declared range widens (a stray edit, or a lazy `>=` re-added) and a
    resolver picks a typer this port was never run against -- `tan/cli.py`
    would then be exercising an unmeasured private surface on a customer's
    first `pip install ./python`, the exact release-blocker this gate exists
    to catch before it ships;
  * the range stays put but the private surface it assumes quietly changes
    shape from WITHIN the declared range (a typer patch release), which a
    bound alone cannot catch -- only running the actual mechanism can.

`_emit_help_envelope` (`tan/cli.py`) drives `typer.main.get_command()`'s
result through the PUBLIC `click.testing.CliRunner`. Measured (see
`pyproject.toml`'s dependency comment): that result is a `TyperCommand`
descending from `typer._click.core.Command` -- typer's PRIVATE vendored click
fork, not the public `click` package -- for every typer this port declares
support for (`>=0.26`). It works because the vendored `UsageError` still
surfaces as a `SystemExit` that `CliRunner.invoke()` catches generically, not
because the two implementations are actually compatible.
"""

from __future__ import annotations

import importlib.metadata
import pathlib
import re
import tomllib

import click
import typer
from click.testing import CliRunner
from typer.main import get_command

PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"

#: `typer>=X.Y,<A.B` -- both a floor AND a cap, deliberately. A bare
#: `typer>=X.Y` with no comma-separated cap is the tan-cli#293 defect itself:
#: fail loudly rather than silently skip the upper-bound check.
_BOUND_RE = re.compile(r"^typer>=(\d+)\.(\d+),<(\d+)\.(\d+)$")


def _typer_requirement() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    for spec in data["project"]["dependencies"]:
        if spec.split(">=", 1)[0].split("<", 1)[0].strip() == "typer":
            return spec
    raise AssertionError("no `typer` entry in [project].dependencies at all")


def _installed_typer_version() -> tuple[int, int, int]:
    raw = importlib.metadata.version("typer")
    # typer has always released plain `X.Y.Z`; a shape that does not parse
    # that way is itself worth knowing about rather than silently coercing.
    parts = raw.split(".")[:3]
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts))  # type: ignore[return-value]


def test_typer_dependency_declares_both_a_floor_and_a_cap():
    spec = _typer_requirement()
    match = _BOUND_RE.match(spec)
    assert match, (
        f"pyproject.toml declares `{spec}`, which this regex does not "
        "recognise as `typer>=X.Y,<A.B`. If the range shape changed on "
        "purpose, update `_BOUND_RE` here in the same change -- an "
        "unbounded `typer>=X.Y` is exactly the tan-cli#293 defect this gate "
        "exists to catch, so a shape it cannot parse must fail, not skip."
    )


def test_installed_typer_is_within_the_declared_bound():
    spec = _typer_requirement()
    match = _BOUND_RE.match(spec)
    assert match, f"unparseable typer requirement {spec!r}; see the test above"
    lo = (int(match.group(1)), int(match.group(2)), 0)
    hi = (int(match.group(3)), int(match.group(4)), 0)
    installed = _installed_typer_version()
    assert lo <= installed < hi, (
        f"installed typer=={'.'.join(map(str, installed))} is outside the "
        f"declared `{spec}`. Either the test environment drifted (reinstall "
        "against pyproject as declared), or pyproject was bumped without "
        "re-measuring `tan/cli.py`'s use of typer's private `_click` fork "
        "against the new version -- do that measurement (see this file's "
        "module docstring and pyproject's dependency comment) before "
        "widening the range."
    )


def test_typer_command_still_descends_from_the_private_click_fork():
    """The structural fact `pyproject.toml`'s comment measures: `get_command()`
    returns a `TyperCommand` built on typer's OWN `typer._click.core.Command`,
    not the public `click.core.Command`. If a future in-range typer drops the
    vendored fork and returns to building on public click, that is GOOD news
    (the mixing this gate exists to watch goes away) but it is still a change
    this port's reasoning was not written against -- fail so it gets a look,
    not a silent pass.
    """
    from typer._click.core import Command as VendoredCommand  # noqa: PLC0415

    app = typer.Typer(add_completion=False)

    @app.command()
    def _probe() -> None:  # pragma: no cover - never invoked
        pass

    cmd = get_command(app)
    assert VendoredCommand in type(cmd).__mro__, (
        "`get_command()` no longer returns a command built on "
        "`typer._click.core.Command`. This is the exact private surface "
        "`tan/cli.py::_emit_help_envelope` and this gate assume -- if typer "
        "changed shape, re-read `_emit_help_envelope`'s docstring and "
        "confirm the public `click.testing.CliRunner` still drives whatever "
        "`get_command()` now returns before touching this assertion."
    )


def test_cli_runner_still_catches_the_vendored_usage_error_as_a_plain_exit():
    """The BEHAVIOURAL half of the same fact: the public `click.testing.
    CliRunner` invoking a vendored-fork `TyperCommand` must still yield a
    plain, catchable exit -- no unhandled exception -- for both the success
    and usage-error paths `_emit_help_envelope` relies on. A structural MRO
    check alone could pass while the actual catch silently stopped working.
    """
    app = typer.Typer(add_completion=False)

    @app.command()
    def _probe() -> None:
        typer.echo("ok")

    cmd = get_command(app)
    runner = CliRunner()

    help_result = runner.invoke(cmd, ["--help"], prog_name="tan")
    assert help_result.exit_code == 0, help_result.output
    assert help_result.exception is None

    usage_result = runner.invoke(cmd, ["--not-a-real-flag"], prog_name="tan")
    assert usage_result.exit_code != 0
    assert usage_result.output.strip(), (
        "a usage error produced no captured output for _emit_help_envelope "
        "to fold into the JSON envelope's `data.message`"
    )


def test_the_public_click_package_is_still_a_distinct_import():
    """Canary for the OTHER half of the pyproject comment: `click` stays
    declared because `tan/cli.py` and `tan/commands/new_som_cmd.py` import the
    PUBLIC package directly, not because typer still depends on it. If typer
    ever re-vendors nothing and `click` stops resolving to a real, importable
    distribution, `CliRunner` above would already be undefined -- this just
    pins the assumption in one place with an explicit name.
    """
    assert click.__name__ == "click"
