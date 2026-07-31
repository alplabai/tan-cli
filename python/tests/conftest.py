# SPDX-License-Identifier: Apache-2.0
"""Repo-wide test isolation for the SDK discovery ladder.

`resolve_sdk_root_ladder` / `resolve_sdk_tiered` (`tan/commands/build_cmd.py`,
`tan/commands/sdk_cmd.py`) read the REAL process environment
(`ALP_SDK_ROOT`) and the REAL `~/.alp/sdk-default` pointer (`HOME` on POSIX,
`USERPROFILE` on Windows) with no isolation of their own -- by design, so a
developer's actual shell state is what a live `tan` run resolves against.
Left unscrubbed here, the same reads make the suite non-hermetic: a
developer who follows the documented `export ALP_SDK_ROOT=<checkout>`
onboarding, or who has ever run `tan sdk switch --global`, gets a DIFFERENT
SDK resolved than a clean CI runner would, and a test asserting "nothing
resolves" observes a real checkout instead.

Autouse, function-scoped, and applied to every test in this tree -- both
in-process calls (`resolve_sdk_root_ladder` et al., called directly) and the
`run_tan` subprocess helpers (`test_build_command.py` and friends), which
copy `os.environ` at spawn time and so inherit whatever `monkeypatch` leaves
in the real environment.
"""
import os
from pathlib import Path

import pytest

# Typer renders every `--help` through Rich, and BOTH of Rich's inputs are
# ambient rather than code under test:
#
# * COLOUR. Rich emits an option name as TWO styled segments --
#   `\x1b[1;36m-\x1b[0m\x1b[1;36m-core\x1b[0m` -- so `"--core" in output` is
#   False the moment styling is on, whatever the width.
# * WIDTH. A narrow console folds an option name or wraps an asserted phrase
#   mid-string (`curated │ │ starter set`).
#
# Neither is decided by the host's OS: `typer.rich_utils` forces a terminal
# whenever `GITHUB_ACTIONS`, `FORCE_COLOR` or `PY_COLORS` is set, and takes its
# width from the tty. So a help assertion is green on a bare dev shell and red
# under Actions -- on ubuntu, windows AND macos alike.
#
# Pinned here with typer's OWN two knobs, at conftest IMPORT time and not from
# a fixture: `typer.rich_utils` reads both exactly once, at ITS import, which
# happens when the first test module imports the CLI -- after this file is
# imported, before any fixture has run. Subprocess harnesses inherit them
# through `{**os.environ}` and the child re-reads them the same way, so
# in-process `CliRunner` and spawned `python -m tan` agree.
#
# Deliberately NOT `NO_COLOR`: `tan size` and `tan faultdecode` read that
# themselves to decide their own colour, and tests pin both arms of that
# decision (`tests/core/test_size_model.py`). These two touch help rendering
# only.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
os.environ["TERMINAL_WIDTH"] = "200"


def sdk_root() -> Path | None:
    """The alp-sdk checkout this suite's real-SDK-gated tests bind against:
    whichever of ``ALP_SDK_PARITY_ROOT`` / ``ALP_SDK_ROOT`` first names a real
    checkout.

    Call this at MODULE import time (``SDK = sdk_root()`` at a test module's
    top level), never from inside a test body or a function-scoped fixture --
    `_scrub_sdk_discovery_env` below deletes ``ALP_SDK_ROOT`` from the process
    environment before every test function runs, so a call made after that
    point always sees it already gone. Import happens once at collection,
    before any fixture executes, which is what makes the module-level call
    see the developer's real environment.
    """
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


#: The REAL process environment, captured at collection time -- i.e. while
#: this conftest module is first imported, before `_scrub_sdk_discovery_env`
#: below has run for any test. A test that hands an environment to a
#: subprocess it expects to behave like the developer's own shell (e.g. a
#: real `west build` in `test_native_sim_e2e.py`) must build that environment
#: from THIS, never from `os.environ` read inside a test body/fixture --
#: `_scrub_sdk_discovery_env` has by then already repointed `HOME`/
#: `USERPROFILE` at a pytest tmp dir and deleted `ALP_SDK_ROOT`.
REAL_ENVIRON: dict[str, str] = dict(os.environ)


@pytest.fixture(autouse=True)
def _scrub_sdk_discovery_env(tmp_path_factory, monkeypatch):
    monkeypatch.delenv("ALP_SDK_ROOT", raising=False)
    # `SOURCE_DATE_EPOCH` wins over the clock in `tan.core.timestamp`, so a
    # developer or CI image that exports it (reproducible-build setups do)
    # changes every `generatedAt`/`updatedAt` this suite observes -- and a
    # value in MILLISECONDS renders a 5-digit year that `%Y` cannot parse,
    # which is what `tests/core/test_timestamp.py` exists to document. The
    # tests that WANT one set it themselves.
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
