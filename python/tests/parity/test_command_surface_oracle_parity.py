# SPDX-License-Identifier: Apache-2.0
"""The verbs nothing compared against the oracle at all (tan-cli#409 section C).

Eight of the 32 registered subcommands had no oracle comparison anywhere in
this package -- `bootstrap`, `completion`, `doctor`, `examples`, `inspect`,
`kconfig`, `scaffold`, `trace`. `doctor` and `inspect` were reachable only
INDIRECTLY, as sections of the support-bundle payload, and both of those cases
are themselves live-only (`test_support_bundle_oracle_parity.py`), so that
indirect coverage disappears with `crates/` too. `trace` -- the bundle's third
section -- was not compared even indirectly.

That is not an academic gap. `kconfig` is the live feed for alp-sdk-vscode's
`prj.conf` symbol LSP, `examples` backs its example picker, and
`inspect`/`doctor`/`trace` are the three sections its diagnostics surface
reads out of the support bundle. A renamed key or a dropped field in any of
them reached the extension unmeasured.

**What is compared here, and what is not.** Every case below picks the verb's
REFUSAL or static path -- the surface both binaries produce with no SDK, no
workspace and no network -- because that is what can be frozen and replayed
forever. The full-answer paths need a bound SDK (`examples`, `kconfig`) or
mutate the machine (`bootstrap`), and `oracle_fixtures/PARITY-COVERAGE.txt`
records them as still-uncovered rather than pretending a refusal covers them.

`doctor` is deliberately absent, and the ledger says why: its envelope is
decided by the HOST (its check list reads this machine's `ZEPHYR_BASE`, tool
inventory and, on Windows, its registry), so a frozen answer would be a false
red everywhere but the capture host -- the same reason the support-bundle pair
cannot be frozen off-Windows. Measured while writing this file, the two sides
ALSO disagree structurally on that verb (the oracle emits `data.targetKind`
and `data.server`, the port emits neither, and the two spell `generatedAt`
differently: `1970-01-01T00:00:00.000Z` against `1970-01-01T00:00:00Z`) --
recorded in the ledger as a finding rather than pinned here as expected, since
pinning it would make the divergence look decided.

Frozen replay by default, like every other module here: the rust side is a
committed fixture (`oracle_fixtures.resolve`), so these keep discriminating
after tan-cli#269. `TAN_PARITY_LIVE=1` re-runs both binaries for real.
"""
import re

import pytest

from . import oracle_fixtures
from .oracle import _run, compare, missing_for_live, python_command, rust_binary, rust_run

RUST = rust_binary()

pytestmark = pytest.mark.skipif(
    missing_for_live(RUST),
    reason="TAN_PARITY_LIVE=1 needs a Rust tan; set TAN_RUST_BINARY or run `cargo build`",
)


@pytest.fixture
def work_dir(tmp_path):
    """A scratch cwd nested under its OWN parent -- `discover_workspace_sdk`
    probes the cwd's PARENT for a sibling `alp-sdk/`, so running directly in
    `tmp_path` would let the `home` directory beside it decide whether either
    binary finds an SDK. Same shape `test_support_bundle_oracle_parity.py`
    uses, for the same reason."""
    work = tmp_path / "root"
    work.mkdir()
    return work


def _both_sides(argv: list[str], work_dir, tmp_path) -> tuple[int, dict, int, dict]:
    """`(rust_code, rust_envelope, python_code, python_envelope)`, both sides
    scrubbed of the scratch roots.

    For the cases that do NOT match key-for-key: `compare()` reports a
    divergence as a diff list, which is the right shape when the answer is
    "these must be identical" and the wrong one when the answer is "these
    differ in exactly this named way, and nowhere else". The known-divergence
    cases below need the two envelopes in hand to say that.
    """
    home = tmp_path / "home"
    r_code, r_out = rust_run(argv, work_dir, home)
    p_code, p_out = _run(python_command(), argv, work_dir, home)
    return r_code, r_out, p_code, oracle_fixtures.scrub(p_out, work_dir, home)


#: The one place each shell's script names every subcommand. Applied to BOTH
#: sides, so a divergence cannot hide in an extraction that only understands
#: one of them: bash and fish each carry a single space-separated list, zsh
#: carries one `'<verb>:<description>'` row per line inside `commands=( ... )`.
_COMMAND_LIST_RE = {
    "bash": re.compile(r'local commands="([^"]+)"'),
    "fish": re.compile(r"__fish_use_subcommand' -a '([^']+)'"),
}
_ZSH_COMMAND_ROW_RE = re.compile(r"^\s*'([a-z][a-z0-9-]*):[^']*'\s*$", re.MULTILINE)


def _completion_verbs(script: str, shell: str) -> list[str]:
    if shell == "zsh":
        return _ZSH_COMMAND_ROW_RE.findall(script)
    match = _COMMAND_LIST_RE[shell].search(script)
    assert match is not None, f"no {shell} command list found in:\n{script}"
    return match.group(1).split()


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_offers_the_same_verbs_as_the_oracle(shell, work_dir, tmp_path):
    """Every subcommand, in order, on both sides.

    This is the load-bearing half of `completion`: the script names all 32
    verbs as literal text, so a verb registered in one implementation and not
    the other -- or renamed in one -- shows up here rather than as a shell
    that completes a command which no longer exists.
    `tests/commands/test_completion_command.py` asserts against the port ALONE
    and cannot see it.

    Not a whole-script compare: the `--format` VALUE lists genuinely differ
    (see the case below), and a byte compare would report that one known
    divergence three times over and take this check down with it.
    """
    argv = ["completion", "--shell", shell]
    _r_code, r_out, _p_code, p_out = _both_sides(argv, work_dir, tmp_path)
    oracle_verbs = _completion_verbs(r_out["__raw__"], shell)
    # Non-vacuity first, and it is not ceremony: three per-shell extractions
    # feed the equality below, and an extraction that stopped matching would
    # return `[]` for BOTH sides and compare equal -- a comparator answering
    # "same" for two documents it never read, which is the one thing this
    # harness must never do (`oracle.narrow_plan` records the same trap).
    # The count is the oracle's, and the oracle is frozen, so it moves only
    # when someone re-captures against a different binary.
    assert len(oracle_verbs) == 32
    assert {"validate", "build", "kconfig", "examples"} <= set(oracle_verbs)
    assert _completion_verbs(p_out["__raw__"], shell) == oracle_verbs


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_format_values_are_a_declared_divergence(shell, work_dir, tmp_path):
    """The port completes two `--format` values the oracle does not, and only
    after `validate` (tan-cli#403). The oracle offers `text json` for every
    command, unconditionally.

    Declared here rather than narrowed away: this is the shell-visible half of
    a format surface the extension also drives, so a change on EITHER side --
    the oracle growing the values, or the port dropping them -- has to be seen
    by something. `oracle_fixtures/PARITY-COVERAGE.txt` carries the same fact
    in prose.
    """
    argv = ["completion", "--shell", shell]
    _r_code, r_out, _p_code, p_out = _both_sides(argv, work_dir, tmp_path)
    rust_script, port_script = r_out["__raw__"], p_out["__raw__"]
    for value in ("diagnostic-v1", "sarif"):
        assert value not in rust_script
        assert value in port_script
    assert "text json" in rust_script and "text json" in port_script


def test_trace_without_an_sdk_matches_the_oracle(work_dir, tmp_path):
    """`trace` is the support bundle's third section and was compared NOWHERE,
    not even indirectly. Its no-SDK refusal is identical on both sides --
    envelope, `data` skeleton (`workflow: cli.trace`, empty `decisions`), issue
    code, severity AND message -- so it is pinned whole."""
    result = compare(["trace", "--format", "json"], work_dir, home=tmp_path / "home")
    assert result.matches, result.diffs


def test_scaffold_without_a_name_matches_the_oracle(work_dir, tmp_path):
    """`--non-interactive` is what makes this comparable at all: `scaffold`
    with no `--name` PROMPTS on a terminal, and the refusal is only reached
    when prompting is off. Both binaries take that branch identically, down to
    the `scaffold.name-required` message, so the whole envelope is pinned --
    including the empty `data` skeleton (`fileChanges`, `written`, `unchanged`)
    the extension reads back after a real scaffold."""
    result = compare(
        ["scaffold", "--format", "json", "--non-interactive"], work_dir, home=tmp_path / "home"
    )
    assert result.matches, result.diffs


def test_inspect_matches_the_oracle(work_dir, tmp_path):
    """The whole `resolvedValues` table: keys, order, values, `source` and
    `detail` strings.

    This is the surface alp-sdk-vscode's diagnostics view reads, and until now
    it was compared only as a nested section of the support-bundle payload --
    i.e. only on the two cases that vanish with `crates/`.

    Separator normalisation is FORCED on both sides here (see
    `oracle_fixtures.normalise_scrubbed_path_separators`), which no other case
    in this package needs. `boardYamlPath` is `<scratch root>/board.yaml`, and
    the store's automatic rule keys off `CAPTURE_PLATFORM` -- correct for the
    win32-captured majority, wrong for a key captured on POSIX, which would
    then diff `/` against a Windows replay's `\\`. The value being normalised
    is a redacted scratch root this harness itself created, so the separator in
    it is the recording host's and never a behaviour of either binary.
    """
    r_code, r_out, p_code, p_out = _both_sides(["inspect", "--format", "json"], work_dir, tmp_path)
    r_out = oracle_fixtures.normalise_scrubbed_path_separators(r_out, force=True)
    p_out = oracle_fixtures.normalise_scrubbed_path_separators(p_out, force=True)
    assert (r_code, p_code) == (0, 0)
    assert r_out == p_out


def test_bootstrap_without_an_sdk_is_a_known_divergence_from_the_oracle(work_dir, tmp_path):
    """`bootstrap` mutates the machine -- venv, west workspace, pip -- so its
    REFUSAL is the whole comparable surface, and it is the one an unconfigured
    user hits first.

    Everything a machine reads AGREES: exit code 2, `ok`, the whole `data`
    skeleton down to `schemaVersion: "2"` and `missingPrerequisites: null`, and
    the issue's `code`/`severity`. Only the remedy WORDING differs -- the
    oracle points at `tan sdk switch`/`tan sdk install`, the port at a `git
    clone` -- which this suite treats as allowed and therefore pins literally
    on both sides rather than narrowing away (the rule
    `test_run_oracle_parity.py` states for its own pair: a message that is
    allowed to differ is still pinned, so a change to EITHER side is seen).
    """
    argv = ["bootstrap", "--format", "json"]
    r_code, r_out, p_code, p_out = _both_sides(argv, work_dir, tmp_path)
    assert r_code == p_code == 2
    assert {**r_out, "issues": []} == {**p_out, "issues": []}
    assert [(i["code"], i["severity"]) for i in r_out["issues"]] == [
        ("bootstrap.sdk-root-unresolved", "error")
    ]
    assert [(i["code"], i["severity"]) for i in p_out["issues"]] == [
        ("bootstrap.sdk-root-unresolved", "error")
    ]
    assert r_out["issues"][0]["message"] == (
        "alp-sdk root is unresolved. Use --sdk-root, pin one with `tan sdk switch "
        "<version|path>`, or run `tan sdk install <version>` first."
    )
    assert p_out["issues"][0]["message"] == (
        "alp-sdk root is unresolved -- get an alp-sdk checkout (`git clone "
        "https://github.com/alplabai/alp-sdk`), then point tan at it with "
        "`--sdk-root <path>`."
    )


def test_kconfig_without_an_sdk_is_a_known_divergence_from_the_oracle(work_dir, tmp_path):
    """`kconfig` is the live feed for alp-sdk-vscode's `prj.conf` symbol LSP,
    and answering at all needs a bootstrapped Zephyr workspace -- so the
    no-SDK refusal is the surface that can be frozen without one.

    `data.schemaVersion` is the integer `1` here, not the string `"1"` other
    verbs use, on BOTH sides: that is a real (and matching) quirk of this
    envelope, and comparing `data` whole is what keeps it pinned. As with
    `bootstrap`, only the remedy wording differs.
    """
    argv = ["kconfig", "--format", "json"]
    r_code, r_out, p_code, p_out = _both_sides(argv, work_dir, tmp_path)
    assert r_code == p_code == 2
    assert {**r_out, "issues": []} == {**p_out, "issues": []}
    assert r_out["data"] == {"schemaVersion": 1, "board": "", "core": "", "symbols": []}
    assert [(i["code"], i["severity"]) for i in r_out["issues"]] == [
        ("kconfig.no-sdk-root", "error")
    ]
    assert [(i["code"], i["severity"]) for i in p_out["issues"]] == [
        ("kconfig.no-sdk-root", "error")
    ]
    assert r_out["issues"][0]["message"].startswith("no alp-sdk checkout found")
    assert p_out["issues"][0]["message"].startswith("no alp-sdk checkout found")


def test_examples_without_an_sdk_is_a_known_divergence_from_the_oracle(work_dir, tmp_path):
    """`examples` backs the extension's example picker. With no SDK bound,
    both sides answer `ok: true`, exit 0 and an EMPTY catalogue -- and the port
    additionally emits `examples.sdk-root-unresolved` as a WARNING, which the
    oracle does not emit at all.

    Pinned as a divergence rather than narrowed away, because unlike the
    wording differences above this one is machine-VISIBLE: a consumer
    iterating `issues[]` sees an entry on the port that the oracle never sent.
    It is defensible -- an empty picker with no explanation is the failure the
    warning prevents -- but it is a decision, and this is the assertion that
    makes changing it (in either direction) visible instead of silent.
    """
    argv = ["examples", "--format", "json"]
    r_code, r_out, p_code, p_out = _both_sides(argv, work_dir, tmp_path)
    assert r_code == p_code == 0
    assert {**r_out, "issues": []} == {**p_out, "issues": []}
    assert r_out["data"] == {"schemaVersion": "1", "examples": []}
    assert r_out["issues"] == []
    assert [(i["code"], i["severity"]) for i in p_out["issues"]] == [
        ("examples.sdk-root-unresolved", "warning")
    ]
