# SPDX-License-Identifier: Apache-2.0
"""Gate: `test_flash_oracle_parity.json`'s recorded values stay win32-native
(tan-cli#511).

This is the second time a frozen oracle recording was silently altered
instead of merely re-keyed. `PROVENANCE.txt` records the first (2026-08-03,
tan-cli#376: a rename that left the stored ANSWER byte-identical, exactly as
a rename must). The round that shipped as `aaf452e` did the opposite by
accident: while moving two cases out of `test_flash_oracle_parity.py`'s
byte-diff `CASES` table (a real, authorised fix -- see that module's own
`# No <case> here, deliberately` comments), it ALSO rewrote their recorded
`buildRoot` and `message` values from the oracle's actual win32-native
`<ORACLE-ROOT-0>\\.\\build` shape to a POSIX-shaped `<ORACLE-ROOT-0>/./build`
the win32 capture never produced. Nothing caught it locally, because
`oracle_fixtures.normalise_scrubbed_path_separators` SHORT-CIRCUITS on the
capture platform (`REPLAY_IS_CAPTURE_PLATFORM`, `oracle_fixtures.py:264`) --
so on Linux/macOS CI the doctored `/` was silently flattened right back to
`/` (a no-op) and compared equal to itself. The PR's Windows job caught it
downstream, the hard way: `3 failed` against live win32 `\\` output. This
module is the guard against a third instance landing the same way, checkable
on ANY host with no live oracle -- and, unlike the win32 job that caught
`aaf452e`, before a push rather than after one. (CI does have a Windows
runner: `parity.yml`'s `python-tests-shard` matrix `os:` carries
`windows-latest`. It just never runs THIS suite -- those shards pass
`--ignore=tests/gates`, so `tests/gates` is ubuntu-only by the recorded
decision in `ci.yml`'s comment above its `python` job. tan-cli#1153.)

## After tan-cli#269 this gate matters MORE, not less

`crates/` is deleted and the replay/capture machinery named above
(`oracle_fixtures.py`, `normalise_scrubbed_path_separators`,
`REPLAY_IS_CAPTURE_PLATFORM`) went with the parity suite that used it -- kept
in the account above because they are how the `aaf452e` mutation stayed
invisible, not because they still exist. The store itself survives, moved to
`tests/fixtures/oracle_captures/` and read through `tests.oracle_captures`.
What changed is the cost of a bad edit: while a binary existed, a doctored
recording could be caught downstream by a live win32 run or simply
re-captured. Neither is possible now. A hand-edited value is unrecoverable and
indistinguishable from a real one, so this file is the only thing standing
between the store and a fabricated oracle answer.

## Why this checks ONLY `test_flash_oracle_parity.json`

The obvious generalisation -- walk every `*.json` under `oracle_fixtures/`
and demand every `<ORACLE-ROOT-N>`/`<ROOT>`-anchored substring be
backslash-shaped -- was tried first, here, and measured to be WRONG. Several
other stores legitimately mix conventions within the very same envelope:

* `test_clean_parity.json` prefixes its scratch tree with a FIXED,
  test-authored `/proj` child name (that module's own `_scrub`, which
  predates `oracle_fixtures.scrub()`) regardless of capture platform --
  `<ROOT>/proj\\build` is a real, correct, currently-committed win32-captured
  value: `/proj` from the test harness, `\\build` from the oracle. A blanket
  "no `/` after the token" rule flags 40+ entries in this one file alone that
  are not defects.
* `project.boardYaml` / `project.root` / `sdk.root` / `data.boardYamlPath`
  render forward-slash-shaped EVEN ON A GENUINE WIN32 CAPTURE -- verified
  directly against `platform_bound.json`'s `resolved-sdk::win32` key (a real
  windows-latest CI capture): `"boardYaml": "<ORACLE-ROOT-0>/board.yaml"`
  sits beside `"outputPath": "<ORACLE-ROOT-2>\\rust-bundle\\...json"` in the
  SAME envelope. These are apparently rendered as logical/schema paths
  independent of host, not `os.path.join` output; a blanket rule would flag
  them as false regressions on every win32-governed file that carries one.
* A `--build-root` value passed verbatim on the CLI (e.g.
  `subdir-app-path-explicit-build-root`'s `app/build`) is echoed into
  `buildRoot` UNMODIFIED after the first, OS-native join:
  `<ORACLE-ROOT-0>\\app/build` -- the `\\` proves the join was native, the
  literal `/build` after it is the user's own argument text, not a rendering
  bug.

Auditing every field of every file to tell "always-POSIX schema field" from
"OS-native join" from "test-harness-fixed literal" apart is real, non-trivial
source-reading work this fix does not have the room to do safely for files
it did not touch. Getting it wrong in either direction is worse than not
gating those files at all: a false positive gets "fixed" by loosening the
gate, which is how a gate stops gating.

`test_flash_oracle_parity.json` is different, and checkable exactly:
`buildRoot` is `_abs_join(app_dir, "build")` (`flash_cmd.py:173-181`,
`_abs_join = os.path.join`, deliberately NOT `pathlib` -- see that function's
own docstring), and every message that embeds a build artefact path embeds
that SAME `buildRoot` string plus a filename tan itself controls (`a.elf`,
`a.bin`, `system-manifest.yaml`, ...) -- never raw user text after the first
join. That makes one invariant sound file-wide: the character immediately
following any `<ORACLE-ROOT-N>` token, wherever the token appears in this
file, is the join separator of the platform that produced it -- `\\` on this
store's `CAPTURE_PLATFORM` ("win32"). Whatever comes AFTER that first
character is not policed here (the `app/build`-style pass-through case
above lives in this very file), which is what keeps this gate from
false-positiving on that legitimate case while still catching the mutation:
both doctored values changed the character RIGHT AFTER the token, from `\\`
to `/`.

Verified to catch the `aaf452e` mutation specifically: reintroducing the two
doctored `/`-shaped `buildRoot`/`message` values in a scratch copy of this
file and pointing this test at that copy reds, naming exactly those two
keys.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tests import oracle_captures

_FIXTURE_PATH = oracle_captures.CAPTURES_DIR / "test_flash_oracle_parity.json"

#: The character immediately following a scrub token, captured but not
#: required to be anything in particular by the regex itself -- absence (the
#: token is the last thing in the string) is not a match and is not policed.
_TOKEN_NEXT_CHAR_RE = re.compile(r"<(?:ORACLE-ROOT-\d+|ROOT)>(.)")

#: `oracle_captures.CAPTURE_PLATFORM` restated as the literal separator this
#: file's joins must produce -- imported as a value, not hardcoded, so this
#: gate cannot silently drift from the constant it is actually checking.
_EXPECTED_NEXT_CHAR = "\\" if oracle_captures.CAPTURE_PLATFORM == "win32" else "/"


def _iter_token_next_chars(payload: object):
    if isinstance(payload, str):
        yield from _TOKEN_NEXT_CHAR_RE.finditer(payload)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_token_next_chars(item)
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from _iter_token_next_chars(value)


def _check(store: dict) -> list[str]:
    violations = []
    for key, entry in store.items():
        for match in _iter_token_next_chars(entry):
            got = match.group(1)
            if got != _EXPECTED_NEXT_CHAR:
                violations.append(
                    f"{key} -- expected {_EXPECTED_NEXT_CHAR!r} right after the scrub "
                    f"token, got {got!r} in {match.string!r}"
                )
    return violations


def test_flash_oracle_fixture_separators_are_win32_native():
    """Every scrubbed-root token in `test_flash_oracle_parity.json` is
    immediately followed by the win32 join separator. A forward slash there
    is the tan-cli#511 shape: a recorded win32 answer quietly rewritten to a
    POSIX shape the real capture never produced."""
    store = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    violations = _check(store)
    assert violations == [], (
        "test_flash_oracle_parity.json separator convention violated (restore "
        "from the last known-good win32 capture, matching by fixture NAME if "
        "the key was also renamed -- do not hand-edit the value):\n  "
        + "\n  ".join(violations)
    )


def test_the_mutation_that_shipped_in_aaf452e_would_be_caught():
    """Self-check: reintroduce the exact `aaf452e` mutation in a scratch copy
    of the store and confirm this gate reds on it, naming both doctored
    keys. Proves the check above is not vacuously green."""
    store = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    mutated_keys = [
        "tests/parity/test_flash_oracle_parity.py::"
        "test_openocd_program_word_diverges_from_the_oracle_by_exactly_the_brace"
        "[multi-segment-interface-is-allowed]#0",
        "tests/parity/test_flash_oracle_parity.py::"
        "test_openocd_program_word_diverges_from_the_oracle_by_exactly_the_brace"
        "[openocd-forced-bin-appends-base]#0",
    ]
    scratch = json.loads(json.dumps(store))  # deep copy
    for key in mutated_keys:
        assert key in scratch, f"fixture key moved or was renamed: {key}"
        text = json.dumps(scratch[key])
        # The exact rewrite aaf452e made: win32-native `\.\build` -> POSIX
        # `/./build`, applied to the whole entry so it reaches both
        # `data.buildRoot` and the `data.entries[].message` copy.
        mutated_text = text.replace("<ORACLE-ROOT-0>\\\\.\\\\build", "<ORACLE-ROOT-0>/./build")
        assert mutated_text != text, f"mutation did not apply to {key}"
        scratch[key] = json.loads(mutated_text)

    violations = _check(scratch)
    violated_keys = {v.split(" -- ", 1)[0] for v in violations}
    assert violated_keys == set(mutated_keys), (
        f"expected the mutation to red exactly the two doctored keys, got: {violated_keys}"
    )
