#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Capture alp-sdk's planner emits as a frozen golden fixture.

tan-cli#509. The byte-parity suite in `tests/parity/test_planner_emit_parity.py`
compares two live implementations: alp-sdk's `scripts/alp_orchestrate/` and
tan's relocated `tan/planner/**`. tan-cli#270 deletes the alp-sdk side. The
suite does not merely go advisory at that point -- it becomes UNRUNNABLE, and
`test_planner_parity_actually_ran.py` (tan-cli#500) then reds the job rather
than letting 775 cases skip green.

That is not a small loss. Measured coverage of `tan/planner/**` (3998
statements): 83% with the suite, 27% without, with `zephyr_board.py` (261
statements), `project_emit/dts.py` (219), `west_libs.py` (95),
`native_sim.py` (61) and `hw_info.py` (57) all falling to 0%. A two-line
mutation to `tan/planner/partition.py` that halved every emitted partition
`base_kib` produced an IDENTICAL suite result with the package absent.

This tool freezes the alp-sdk side's OUTPUT, so the comparison survives the
deletion of its source. The gate stops answering "have the two implementations
diverged" -- after #270 there is only one -- and starts answering "does
`tan/planner/**` still emit what the implementation it was relocated from
emitted".

WHY THE OUTPUT AND NOT THE SOURCE
----------------------------------

The obvious alternative is a second pinned `actions/checkout` of alp-sdk at a
pre-#270 ref; `parity.yml` already does exactly that three times over
(`alp-sdk-planner-audit`, `alp-sdk-hand-port-audit`,
`alp-sdk-strict-loaders-audit`). It was measured working: 768 passed, 8 skipped
against `94378a056549c7377d714a7f2b68878aca8fea01`.

It was rejected for one reason. Today a parity failure has a repair path --
port the delta into tan, bump the pin. After #270 there is no later alp-sdk ref
that still carries a planner, so a source-pinned oracle can never move again,
and the first DELIBERATE change to tan's emitted output would red the gate
permanently. A gate with no legitimate repair path gets deleted under pressure.

Frozen output has one: regenerate, and review the diff. That diff is the point.
It shows a reviewer the exact bytes that changed, in DTS and JSON and C, rather
than a pin SHA moving by one line and hiding an arbitrary behaviour change.
That is the same trade `contract/` made for the Rust oracle.

THE HOST-PATH TRAP THIS TOOL REFUSES TO SHIP
---------------------------------------------

`emit_build_plan` writes the board.yaml it was handed into the plan's
`"boardYaml"` field, as an ABSOLUTE path. Captured naively, 99 of the 100
`build-plan` goldens embed the capture machine's own directory layout, and the
fixture then passes for whoever generated it and fails for everyone else -- on
the FIRST CI run, where it reads as "the change is broken" rather than "the
fixture is host-bound".

So the capture normalises the SDK root to `_SDK_TOKEN` and then REFUSES to
write anything if a host path survives that substitution. Measured at
`94378a05`, the SDK root is the only volatile content in all 700 emits: after
normalisation there are zero remaining absolute paths, timestamps, epoch
seconds, memory addresses, temp directories or usernames. The refusal exists
so that a future emitter which starts embedding something else fails here,
loudly, instead of producing a fixture that only its author can pass.

USAGE
-----

    python scripts/capture_planner_oracle.py \\
        --sdk /path/to/alp-sdk \\
        --sdk-ref 94378a056549c7377d714a7f2b68878aca8fea01

`--sdk-ref` may be omitted when the checkout is a real git tree; it is REQUIRED
for an exported tree (`git archive`) that carries no `.git`. It is recorded in
PROVENANCE.txt and is the only thing that says which implementation these bytes
came from.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: The emit modes captured, in the order `test_planner_emit_parity.py` lists
#: them. Kept as a literal rather than imported from the test so that the
#: fixture is reproducible from this file alone.
MODES = (
    "build-plan",
    "system-manifest",
    "ipc-contract-h",
    "dts-reservations",
    "dts-partitions",
    "storage-mounts-c",
    "tfm-sysbuild-conf",
)

#: Natural extension per mode, so a regenerated fixture diffs as DTS and JSON
#: and C in a review rather than as an undifferentiated blob. The review
#: surface IS the justification for freezing output instead of source, so it
#: is worth a lookup table.
_EXTENSION = {
    "build-plan": ".json",
    "system-manifest": ".yaml",
    "ipc-contract-h": ".h",
    "dts-reservations": ".dtsi",
    "dts-partitions": ".dtsi",
    "storage-mounts-c": ".c",
    "tfm-sysbuild-conf": ".conf",
}

#: What an absolute SDK path is replaced by. The regression test applies the
#: identical substitution to tan's live output before comparing, so this string
#: is a contract between the two and must not be changed on one side alone.
SDK_TOKEN = "<SDK>"

#: What `"sdkCommit"` is replaced by, for the same contract reason.
#:
#: `emit_build_plan` fills that field from `git rev-parse --short HEAD` on the
#: bound checkout (`tan/planner/buildplan.py:365-366`, emitted at `:643`), and
#: `--short` picks its own length from the repository's object count. Measured:
#: the SAME commit rendered `"94378a05"` (8) from a local worktree and
#: `"94378a0"` (7) from CI's `fetch-depth: 1` checkout, and an exported tree
#: with no `.git` at all rendered `null`. Three answers, one commit, none of
#: them a property of the planner -- so freezing any of them makes the fixture
#: a statement about how the checkout was made.
#:
#: The identity this field gestures at is not lost, it is checked properly
#: instead: `test_the_bound_checkout_is_the_ref_the_fixture_was_captured_from`
#: compares the FULL 40-character HEAD against PROVENANCE.txt, which no
#: abbreviation length can make ambiguous.
SDK_COMMIT_TOKEN = "<SDK_COMMIT>"

#: `"sdkCommit": "94378a0",` / `"sdkCommit": null,` -- both spellings.
_SDK_COMMIT_FIELD = re.compile(r'("sdkCommit":\s*)(?:"[0-9a-f]+"|null)')

#: Patterns that must not survive normalisation. Each one is a way a golden
#: could become host-bound or time-bound; see the module docstring.
_VOLATILE = (
    ("absolute path", re.compile(r"(?<!\w)/(?:Users|home|private|tmp|var)/")),
    ("windows path", re.compile(r"[A-Za-z]:\\\\")),
    ("iso timestamp", re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")),
    ("epoch seconds", re.compile(r"\b1[7-9]\d{8}\b")),
    ("memory address", re.compile(r"0x7f[0-9a-f]{8,}")),
    ("temp directory", re.compile(r"pytest-of-|/T/tmp")),
    # The one that got through the first capture. It is listed as volatile
    # rather than left to the substitution above so that a RENAMED field, or a
    # second `git rev-parse` result appearing somewhere new, fails here instead
    # of being frozen: the first fixture captured `"sdkCommit": null` off an
    # exported tree and reded 99 of 100 build-plan cases on CI's real clone.
    ("raw sdkCommit", re.compile(r'"sdkCommit":\s*(?:"[0-9a-f]+"|null)')),
)


def normalise(text: str, sdk: Path) -> str:
    """Replace every spelling of the SDK root in ``text`` with `SDK_TOKEN`.

    :param text: one emit, as the planner produced it.
    :param sdk: the checkout the planner was bound to.
    :return: the same text with the capture host's layout removed.

    Both the POSIX and the native spelling are substituted: `emit_build_plan`
    writes `Path(board_yaml).as_posix()` for `boardYaml`, as does every other
    path field in the plan, so the POSIX spelling is what actually appears --
    but other text captured alongside it (an exception message, a warning
    built from `str(e)`) is free to carry the native, backslashed spelling on
    Windows. Substituting both spellings unconditionally is what makes a
    Windows capture and a Linux capture of the same tree agree, without
    having to prove which fields are POSIX-normalised and which are not.
    """
    for spelling in (sdk.as_posix(), str(sdk)):
        text = text.replace(spelling, SDK_TOKEN)
    return _SDK_COMMIT_FIELD.sub(rf'\1"{SDK_COMMIT_TOKEN}"', text)


def find_volatile(text: str) -> str | None:
    """Return the name of the first volatile pattern ``text`` still matches."""
    for name, pattern in _VOLATILE:
        if pattern.search(text):
            return name
    return None


def render(upstream, board: Path, mode: str) -> tuple[str, str]:
    """``(kind, text)`` for one board and mode -- 'ok', or the exception class.

    An exception is a RESULT, not a skip, exactly as in
    `test_planner_emit_parity.py::_render`: a relocation that quietly stopped
    emitting a mode must not read as parity. The two implementations of this
    function are deliberately duplicated rather than shared, because the test
    must be able to fail when the capture tool is wrong.
    """
    try:
        project = upstream.load_board_yaml(board)
    except Exception as err:  # noqa: BLE001 -- capturing failures on purpose
        return (f"load:{type(err).__name__}", str(err))
    try:
        if mode == "build-plan":
            return ("ok", upstream.emit_build_plan(
                project, board_yaml=board, build_root=Path("build")))
        emitter = {
            "system-manifest": upstream.emit_system_manifest,
            "ipc-contract-h": upstream.emit_ipc_contract_h,
            "dts-reservations": upstream.emit_dts_reservations,
            "dts-partitions": upstream.emit_dts_partitions,
            "storage-mounts-c": upstream.emit_storage_mounts_c,
            "tfm-sysbuild-conf": upstream.emit_tfm_sysbuild_conf,
        }[mode]
        return ("ok", emitter(project))
    except Exception as err:  # noqa: BLE001
        return (f"emit:{type(err).__name__}", str(err))


def resolve_ref(sdk: Path, given: str | None) -> str:
    """The alp-sdk ref these bytes came from, or exit explaining why not.

    :param sdk: the checkout being captured.
    :param given: an explicit ``--sdk-ref``, which always wins.
    :return: a ref string for PROVENANCE.txt.
    :raises SystemExit: when neither a ``--sdk-ref`` nor a git tree is
        available. A fixture whose provenance is unknown is worse than none --
        nothing downstream could ever tell what it froze.
    """
    if given:
        return given
    result = subprocess.run(
        ["git", "-C", str(sdk), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"{sdk} is not a git checkout and no --sdk-ref was given, so the "
            "captured bytes could not be attributed to an alp-sdk commit. "
            "Pass --sdk-ref explicitly (an exported tree carries no .git)."
        )
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze alp-sdk's planner emits as a golden fixture."
    )
    parser.add_argument("--sdk", type=Path, required=True,
                        help="alp-sdk checkout that still ships scripts/alp_orchestrate/")
    parser.add_argument("--sdk-ref", default=None,
                        help="the alp-sdk commit being captured (required for an exported tree)")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parents[1]
                        / "tests" / "fixtures" / "planner_oracle",
                        help="fixture root to (re)write")
    args = parser.parse_args(argv)

    sdk: Path = args.sdk.resolve()
    planner = sdk / "scripts" / "alp_orchestrate" / "__init__.py"
    if not planner.is_file():
        raise SystemExit(
            f"{planner} is missing -- this checkout no longer ships the planner, "
            "so there is nothing to freeze. Point --sdk at a pre-tan-cli#270 ref."
        )
    ref = resolve_ref(sdk, args.sdk_ref)

    sys.path.insert(0, str(sdk / "scripts"))
    import alp_orchestrate  # noqa: PLC0415 -- resolvable only after the path insert

    boards = sorted((sdk / "examples").rglob("board.yaml"))
    if not boards:
        raise SystemExit(f"{sdk}/examples has no board.yaml -- nothing to capture.")

    captured: list[tuple[Path, str]] = []
    errors = 0
    for board in boards:
        relative = board.parent.relative_to(sdk).as_posix()
        for mode in MODES:
            kind, text = render(alp_orchestrate, board, mode)
            body = normalise(text, sdk)
            offender = find_volatile(body)
            if offender is not None:
                raise SystemExit(
                    f"refusing to write the fixture: {relative} --emit {mode} still "
                    f"carries a {offender} after normalisation. A golden with host- "
                    "or time-dependent bytes passes only for whoever captured it. "
                    "Extend normalise()/_VOLATILE in this file, or make the emitter "
                    "stop embedding it, then capture again."
                )
            if kind == "ok":
                target = Path(relative) / (mode + _EXTENSION[mode])
            else:
                target = Path(relative) / (mode + ".error")
                body = f"{kind}\n{body}"
                errors += 1
            captured.append((target, body))

    emits = args.out / "emits"
    if emits.exists():
        shutil.rmtree(emits)
    for target, body in captured:
        destination = emits / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        # `newline=""` so a capture on Windows writes the same LF bytes a
        # capture on Linux does; the emitters already produce LF internally.
        with open(destination, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)

    total_bytes = sum(len(b.encode("utf-8")) for _, b in captured)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "PROVENANCE.txt").write_text(
        _provenance(ref, sdk, len(boards), len(captured), errors, total_bytes),
        encoding="utf-8", newline="",
    )
    print(f"captured {len(captured)} emits over {len(boards)} boards "
          f"({errors} error-contract) = {total_bytes:,} B, from alp-sdk {ref}")
    return 0


def _provenance(ref: str, sdk: Path, boards: int, emits: int,
                errors: int, total_bytes: int) -> str:
    return f"""\
THE FROZEN alp-sdk PLANNER ORACLE (tan-cli#509)
===============================================

alp-sdk ref   {ref}
boards        {boards}
emits         {emits}  ({errors} of them error-contract, captured as .error)
bytes         {total_bytes:,}
modes         {" ".join(MODES)}

WHAT THESE BYTES ARE. The output of alp-sdk's `scripts/alp_orchestrate/` at the
ref above, rendered over every `board.yaml` under that checkout's `examples/`,
for every mode listed. They are the last independent implementation tan's
planner can be checked against: tan-cli#270 deletes `scripts/alp_orchestrate/`
from alp-sdk, after which tan is the only implementation left and a live
comparison has no counterparty.

HOW TO REGENERATE. Check out alp-sdk at a ref that still ships the planner --
the one above unless there is a reason to move -- and run, from `python/`:

    python scripts/capture_planner_oracle.py --sdk <checkout> --sdk-ref <ref>

An exported tree (`git archive`) has no `.git`, so `--sdk-ref` is required
there. The tool refuses to write if any emit still carries a host path, a
timestamp or another volatile value after normalisation.

WHEN TO REGENERATE, AND WHAT THE DIFF MEANS. Whenever
`tests/parity/test_planner_oracle_regression.py` goes red for a change you
believe is CORRECT. The resulting diff is not noise to be waved through -- it
is the whole reason this fixture holds output rather than a pinned source ref.
It shows, in DTS and JSON and C, exactly which emitted bytes moved.

Two shapes reach it and they are not the same:

  * `tan/planner/**` changed. The diff is the behaviour change, in full. Review
    it as you would review the emitted artefact itself.
  * `PINNED_SDK_TAG` moved and the new `metadata/**` changes what tan emits.
    That is tan-cli#485's mechanism made visible: tan binds alp-sdk's DATA late
    and its LOGIC early, so a new metadata field lands instantly while the rule
    interpreting it is hand-carried. The diff is the signal that was missing
    when `CONFIG_ALP_SDK_CHIP_NONE=y` reached `dev`.

The paths in `boardYaml` are `{SDK_TOKEN}`-normalised; see
`capture_planner_oracle.py` for why, and for the refusal that keeps it true.
"""


if __name__ == "__main__":
    sys.exit(main())
