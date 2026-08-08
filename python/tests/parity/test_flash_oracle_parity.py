# SPDX-License-Identifier: Apache-2.0
"""Diff the Python ``tan flash`` against the shipped Rust ``tan flash`` on
identical inputs in identical isolated cwds.

**This file is the only gate on this command.** ``contract/README.md`` states
outright that no committed envelope fixture covers ``flash``, so unlike every
other ported command there is no golden to check the port against -- a green
unit-test run that never compared against the oracle would prove almost nothing.
Each case below plants a ``system-manifest.yaml``, runs BOTH binaries with the
same argv, and diffs the whole envelope key by key (``oracle.ENVELOPE``).

**Nothing here touches hardware.** Every case reaches a decision before any
device is programmed: a refusal, a skip, a confirm-gated no-op, a ``--dry-run``
preview, a missing tool, an unresolvable artefact, or -- in the one spawning case
-- ``dd`` failing to open a file that does not exist. There is no board attached
and none is required.

Two message texts are structurally out of reach and are diffed by CODE + EXIT
rather than byte-for-byte; both are called out on the case that covers them.
"""
import os
import re
import shutil
import subprocess

import pytest

from . import oracle_fixtures
from .oracle import ENVELOPE, compare, missing_for_live, rust_binary, rust_run

RUST = rust_binary()

pytestmark = pytest.mark.skipif(
    missing_for_live(RUST),
    reason="TAN_PARITY_LIVE=1 needs a Rust tan; set TAN_RUST_BINARY",
)

#: A manifest with one Zephyr slice whose `flash_method`/`flash_args` the case
#: substitutes. `status: ok` so it is not refused before dispatch.
SLICE = """schema_version: 1
hw_info: {{sku: E1M-V2N101}}
slices:
- {{core_id: c1, os: zephyr, output_artefact: a.elf, status: ok,
   flash_method: {method}, flash_args: {args}}}
helper_mcus: []
boot_order: []
"""

def _slice(method, args="{}"):
    return SLICE.format(method=method, args=args)


#: `(id, manifest-or-None, extra argv)`. `--sdk-root ./sdk` and `--format json`
#: sit AFTER the subcommand: clap accepts a `global = true` flag on either side
#: and the port declares both as command-local options, so this is the one
#: position both parsers agree on. A `None` manifest writes no file at all.
CASES = [
    # ── command-level refusals ──────────────────────────────────────────────
    ("manifest-missing", None, []),
    ("sdk-root-invalid", _slice("swd_probe"), ["--sdk-root", "./nowhere"]),
    # `schema_version: 2` -- the version handshake, refused rather than read as
    # if it were v1.
    ("schema-version-2", "schema_version: 2\nslices: []\n", []),
    # ── selection ───────────────────────────────────────────────────────────
    ("no-slices", "schema_version: 1\nslices: []\n", []),
    ("core-filter-matches-nothing", _slice("swd_probe"), ["--dry-run", "--core", "nope"]),
    (
        "boot-order-order-and-both-warnings",
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: b, os: zephyr, output_artefact: b.elf, status: ok,
   flash_method: zephyr_west_flash}
- {core_id: a, os: zephyr, output_artefact: a.elf, status: ok,
   flash_method: zephyr_west_flash}
helper_mcus: []
boot_order: [{core: a}, {core: ghost}]
""",
        ["--dry-run"],
    ),
    (
        "empty-boot-order-sorts-and-helpers-last",
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: m33_sm, os: zephyr, output_artefact: m.elf, status: ok,
   flash_method: zephyr_west_flash}
- {core_id: a55_cluster, os: yocto, output_artefact: a.wic, status: ok,
   flash_method: yocto_wic, flash_args: {target: /dev/sdb}}
helper_mcus:
- {name: gd32_bridge, chip: gd32g553, firmware_path: g.bin, flash_method: swd_probe,
   flash_args: {}}
boot_order: []
""",
        ["--dry-run"],
    ),
    (
        "slice-status-not-ok-is-refused",
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: a, os: zephyr, output_artefact: a.elf, status: failed,
   flash_method: swd_probe}
helper_mcus: []
boot_order: []
""",
        ["--dry-run"],
    ),
    # No `status: skipped` case here, deliberately: the Python port's
    # `TargetPlan.refused_skipped` (see its docstring in `tan.core.flash_plan`)
    # treats a skipped slice as a warning that alone never fails the run, while
    # the oracle's `plan_flash_targets` refuses (and fails) it exactly like
    # `status: failed` above -- the two implementations disagree there BY
    # DESIGN, so diffing this case would only ever fail. Pinned instead in
    # `tests/commands/test_flash_command.py`.
    # ── skips that must never fail the run ──────────────────────────────────
    # No `helper-no-flash-method` / `helper-update-channel-is-not-a-flash-
    # target` / `flash-args-tbd-mapping` / `flash-args-tbd-bare-string` cases
    # here, and that is NOT an oversight -- see tan-cli#487 defect 7. All four
    # (plus `missing-tool-skips-with-flag`, moved out of the required-tool-gate
    # section below for the identical reason) reach `_flash_entry`, which
    # correctly skips the ONE target (`rc=-1`, `status: skipped`, the real
    # per-entry reason in `entries[0].message`), but the aggregate check after
    # `_flash_entry`'s loop only knows about `plan.refused`/
    # `plan.refused_skipped` -- planner-level buckets an entry-level skip
    # never populates -- so it fell through to "nothing matched the requested
    # filters" on a run that carries NO `--core`/`--helper` at all and DID
    # match exactly one real target. The shipped Rust oracle has the SAME
    # shape (verified by running it: `flash.nothing-matched` on all five,
    # `exitCode 0`) -- the two implementations diverge here BY DESIGN. Pinned
    # instead, with the correct code/message, in
    # `tests/commands/test_flash_command.py`. Do not "restore parity" by
    # moving these back.
    # No `output_artefact`/`firmware_path: TBD` case here, and that is NOT an
    # oversight -- see #222. The `flash_args: TBD` shape used to be diffed
    # here (both sides skipped it identically) until it moved out under the
    # tan-cli#487 defect 7 divergence above; the ARTEFACT sibling below was
    # ALWAYS a deliberate divergence of its own, for an unrelated reason. The
    # shipped Rust has the alp-sdk hole (`flash/mod.rs:307`'s
    # `.filter(|s| !s.is_empty())`, and `TBD` is not empty): run the oracle on a
    # helper with `firmware_path: TBD` and it reports `status: ok`, exit 0, and
    # `would run JLinkExe ... -CommanderScript <generated.jlink>` whose script
    # `loadfile`s `<build_root>\TBD` -- i.e. on a host with a real J-Link it
    # spawns a flasher against a placeholder. The port REFUSES that (a failed
    # entry, under `--dry-run` too), so diffing it here would only ever go red.
    # Pinned in `tests/commands/test_flash_command.py` instead, with an
    # audit-hook proof that nothing is spawned. Do not "restore parity".
    # ── the strict flash_args accessors ─────────────────────────────────────
    ("quoted-bool-is-refused", _slice("zephyr_west_flash", '{erase: "true"}'), ["--dry-run"]),
    ("quoted-int-is-refused", _slice("swd_probe", '{jlink_speed: "8000"}'), ["--dry-run"]),
    ("negative-base-is-refused", _slice("swd_probe", "{base: -8}"), ["--dry-run"]),
    ("sequence-base-is-refused", _slice("swd_probe", "{base: [1, 2]}"), ["--dry-run"]),
    ("bare-int-base-round-trips-to-hex", _slice("swd_probe", "{base: 134217728}"), ["--dry-run"]),
    ("explicit-zero-speed-means-default", _slice("swd_probe", "{jlink_speed: 0}"), ["--dry-run"]),
    ("reset-false-is-honoured", _slice("swd_probe", "{reset: false}"), ["--dry-run"]),
    # ── injection guards ────────────────────────────────────────────────────
    (
        "tcl-metacharacter-in-interface-is-refused",
        _slice("swd_probe", '{use_openocd: true, interface: "a;b", target: t}'),
        ["--dry-run"],
    ),
    (
        "traversal-in-target-is-refused",
        _slice("swd_probe", '{use_openocd: true, interface: cmsis-dap, target: "../../x"}'),
        ["--dry-run"],
    ),
    # No `multi-segment-interface-is-allowed` case here, deliberately
    # (tan-cli#486 / tan-cli#487 / tan-cli#511 -- MAINTAINER-AUTHORISED
    # divergence, not a port defect to chase back to parity). It used to pin
    # the openocd `-c program <artefact> verify ...` word BYTE-IDENTICAL to
    # the oracle's own unbraced rendering, on the strength of
    # `openocd_program_word`'s old whitespace-or-backslash predicate leaving
    # a plain path unbraced. That predicate never actually preserved the
    # parity it claimed to: both frozen fixtures are captured on
    # `oracle_fixtures.CAPTURE_PLATFORM = "win32"`
    # (`tests/parity/oracle_fixtures.py:75`), so the `<ORACLE-ROOT-0>`
    # scratch root each one interpolates is a native Windows path and
    # carries a backslash UNCONDITIONALLY -- on the one platform these
    # fixtures are actually anchored to, the predicate could never once
    # observe its own "no backslash" branch. It only LOOKED preserved on a
    # Linux replay, where `compare()`'s `normalise_scrubbed_path_separators`
    # flattens `\\`->`/` in `entries[].message` before the diff and the
    # locally-built path never contains a literal backslash to begin with --
    # both sides answer `False` there by coincidence, not because bracing
    # was genuinely conditional.
    #
    # Left unbraced, the oracle's own rendering is the exact defect
    # `openocd_program_word`'s docstring documents (measured against real
    # `tclsh`, not inferred): unbraced, `C:\Program Files\alp\build\
    # zephyr.elf` splits into `program` / `C:Program` / `Files\x07lp\
    # x08uildzephyr.elf` (`\a`->BEL, `\b`->BS), so `program` receives the
    # filename `C:Program`; and a space-bearing artefact
    # `/build/evil.elf verify exit 0x20000000` injects extra Tcl keywords,
    # rendering `program /build/evil.elf verify exit 0x20000000 verify
    # reset exit`. Both are honest-input failures -- a Windows user's
    # default install path, no attacker required -- so the port now braces
    # every artefact unconditionally (`openocd_program_word`, tan-cli#511)
    # and no longer matches the oracle's raw interpolation byte-for-byte.
    # Standing instruction: do not "restore parity" here while the oracle
    # still interpolates the artefact raw into an unquoted Jim Tcl word --
    # that would be reintroducing the defect, not fixing a port bug. Pinned
    # instead, as a BOUNDED exact-difference case, in
    # `test_openocd_program_word_diverges_from_the_oracle_by_exactly_the_
    # brace` below.
    #
    # No `openocd-forced-bin-appends-base` case here either, for the
    # identical reason -- it exercises the same `program {...}` word, just
    # on the `.bin`/`base` arm. Also pinned in that same bounded test.
    ("newline-in-base-is-refused", _slice("swd_probe", '{base: "0x8000\\n r"}'), ["--dry-run"]),
    # ── per-backend argv ────────────────────────────────────────────────────
    ("no-artefact-real-run-fails", "\n".join(
        [
            "schema_version: 1",
            "hw_info: {sku: S}",
            "slices:",
            "- {core_id: c1, os: zephyr, status: ok, flash_method: zephyr_west_flash,"
            " flash_args: {}}",
            "helper_mcus: []",
            "boot_order: []",
            "",
        ]
    ), []),
    ("no-artefact-dry-run-previews", "\n".join(
        [
            "schema_version: 1",
            "hw_info: {sku: S}",
            "slices:",
            "- {core_id: c1, os: zephyr, status: ok, flash_method: zephyr_west_flash,"
            " flash_args: {}}",
            "helper_mcus: []",
            "boot_order: []",
            "",
        ]
    ), ["--dry-run"]),
    ("west-runner-and-erase", _slice("zephyr_west_flash", "{runner: openocd, erase: true}"),
     ["--dry-run"]),
    ("west-build-dir-from-zephyr-subdir", """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: c1-zephyr/zephyr/zephyr.elf, status: ok,
   flash_method: zephyr_west_flash, flash_args: {}}
helper_mcus: []
boot_order: []
""", ["--dry-run"]),
    ("cmake-jobs-and-config", _slice("baremetal_cmake_flash", "{target: prog, jobs: 4, config: Rel}"),
     ["--dry-run"]),
    ("jlink-bin-artefact-uses-loadbin", """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.bin, status: ok, flash_method: swd_probe,
   flash_args: {base: "0x08000000", jlink_device: NRF_DUMMY, jlink_speed: 1000}}
helper_mcus: []
boot_order: []
""", ["--dry-run"]),
    ("pyocd-forced-elf-omits-base",
     _slice("swd_probe", "{use_pyocd: true, interface: cmsis-dap, target: t}"), ["--dry-run"]),
    ("openocd-missing-interface-and-target",
     _slice("swd_probe", "{use_openocd: true}"), ["--dry-run"]),
    # ── the confirm gate (I-30) ─────────────────────────────────────────────
    ("yocto-unconfirmed-is-planned-not-ok",
     _slice("yocto_wic", "{target: /dev/sdb}"), []),
    ("yocto-target-must-be-a-device", _slice("yocto_wic", "{target: ./oops}"), ["--dry-run"]),
    ("yocto-target-is-required", _slice("yocto_wic", "{}"), ["--dry-run"]),
    ("yocto-alias-method-resolves",
     _slice("yocto_wic_to_sd_or_emmc", "{target: /dev/sdb}"), ["--dry-run"]),
    ("xspi-unconfirmed-is-planned",
     _slice("xspi_flashwriter", "{flash_partition: mtd1, port: COM3, baud: 921600}"), []),
    ("xspi-partition-must-be-mtd0-or-mtd1",
     _slice("xspi_flashwriter", "{flash_partition: mtdX}"), ["--dry-run"]),
    ("xspi-confirmed-is-hw-gated",
     _slice("xspi_flashwriter", "{flash_partition: mtd0, confirm: true}"), []),
    # ── the required-tool gate ──────────────────────────────────────────────
    ("missing-tool-fails", _slice("zephyr_west_flash"), []),
    # No `missing-tool-skips-with-flag` case here -- moved out for tan-cli#487
    # defect 7; see the divergence note above `helper-no-flash-method`.
    # ── --build-root ────────────────────────────────────────────────────────
    ("explicit-relative-build-root", _slice("swd_probe"), ["--dry-run", "--build-root", "build"]),
    # ── flag combinations a user gets wrong ─────────────────────────────────
    # `--core` guards the helper loop and `--helper` guards the slice loop, so
    # BOTH together select nothing at all rather than "one of each".
    (
        "core-and-helper-together-select-nothing",
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.elf, status: ok, flash_method: swd_probe}
helper_mcus:
- {name: h1, chip: x, firmware_path: f.bin, flash_method: swd_probe, flash_args: {}}
boot_order: []
""",
        ["--dry-run", "--core", "c1", "--helper", "h1"],
    ),
    # A `--core` filter must SUPPRESS the "no boot_order entry" warning: it
    # deliberately narrows the slice set, so warning about the ones it excluded
    # would fire on every correct use.
    (
        "core-filter-suppresses-the-missing-boot-order-warning",
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: a, os: zephyr, output_artefact: a.elf, status: ok, flash_method: swd_probe}
- {core_id: b, os: zephyr, output_artefact: b.elf, status: ok, flash_method: swd_probe}
helper_mcus: []
boot_order: [{core: a}, {core: b}]
""",
        ["--dry-run", "--core", "a"],
    ),
    # A `--helper` filter suppresses it too, and skips every slice.
    (
        "helper-filter-suppresses-slices-entirely",
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: a, os: zephyr, output_artefact: a.elf, status: ok, flash_method: swd_probe}
helper_mcus:
- {name: h1, chip: x, firmware_path: f.bin, flash_method: swd_probe, flash_args: {}}
boot_order: [{core: a}]
""",
        ["--dry-run", "--helper", "h1"],
    ),
    # A boot_order step that is not a mapping, and one with no `core` key: both
    # dropped silently rather than crashing the walk.
    (
        "malformed-boot-order-steps-are-dropped",
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: a, os: zephyr, output_artefact: a.elf, status: ok, flash_method: swd_probe}
helper_mcus: []
boot_order: ["just-a-string", {notcore: a}, {core: ""}, {core: a}]
""",
        ["--dry-run"],
    ),
    # An `off` core with no flash_method reaches the target list and skips.
    (
        "off-core-skips-without-failing-the-run",
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: idle, os: "off", status: ok}
- {core_id: a, os: zephyr, output_artefact: a.elf, status: ok, flash_method: swd_probe}
helper_mcus: []
boot_order: []
""",
        ["--dry-run"],
    ),
    # A slice with an empty `core_id` is dropped from the step list entirely.
    (
        "empty-core-id-is-dropped",
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: "", os: zephyr, output_artefact: a.elf, status: ok, flash_method: swd_probe}
helper_mcus: []
boot_order: []
""",
        ["--dry-run"],
    ),
    # An ABSOLUTE artefact bypasses build_root/sdk_root resolution entirely.
    (
        "absolute-artefact-passes-through",
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: a, os: zephyr, output_artefact: /abs/tree/zephyr/zephyr.elf, status: ok,
   flash_method: zephyr_west_flash, flash_args: {}}
helper_mcus: []
boot_order: []
""",
        ["--dry-run"],
    ),
    # A slice carrying an explicit `build_dir` overrides the artefact-derived one.
    (
        "explicit-build-dir-wins-over-the-derived-one",
        _slice("zephyr_west_flash", "{build_dir: /elsewhere/bd, hex_file: /h/x.hex}"),
        ["--dry-run"],
    ),
    # `hw_info` absent entirely -- `sku` reads as "" and nothing crashes.
    (
        "no-hw-info-block",
        """schema_version: 1
slices:
- {core_id: a, os: zephyr, output_artefact: a.elf, status: ok, flash_method: swd_probe}
boot_order: []
""",
        ["--dry-run"],
    ),
]


@pytest.fixture
def work_dir(tmp_path_factory):
    """A fresh isolated directory holding a fake SDK root (just the loader
    marker, which is all `has_loader_script` checks) and an empty `build/`.

    `tmp_path_factory`, not a shared dir: each case must not see another's
    manifest, and `--sdk-root ./sdk` must resolve relative to THIS cwd. No
    `board.yaml` is written -- both sides report where one WOULD live."""
    root = tmp_path_factory.mktemp("flash-parity")
    (root / "build").mkdir()
    (root / "sdk" / "scripts").mkdir(parents=True)
    (root / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    return root


#: Cases whose MANIFEST plants a POSIX-absolute path (``/abs/tree/...``) that
#: the two platforms then resolve to genuinely different absolute paths --
#: Windows re-anchors a root-relative path onto the current drive, so the
#: frozen answer says ``C:/abs/tree`` where a POSIX run says ``/abs/tree``.
#: That is not a separator artefact (:func:`oracle_fixtures.
#: normalise_scrubbed_path_separators` already covers those, and the path is
#: outside the scrubbed scratch root anyway) and it is not a port defect: both
#: renderings are correct on their own OS. A fixture frozen on
#: ``CAPTURE_PLATFORM`` simply cannot answer for the other one here.
#:
#: Exactly one case, arrived at by MEASUREMENT rather than by reading the
#: manifests: `explicit-build-dir-wins-over-the-derived-one` also embeds an
#: absolute path and compares clean on POSIX, because its value never reaches
#: the diffed surface. (The `flash-args-tbd-*` pair used to be a second such
#: example; they moved out of `CASES` entirely under tan-cli#487 defect 7 --
#: see the divergence note above `helper-no-flash-method` -- so they no
#: longer need mentioning here either.) Do not widen this to "every case
#: with a leading slash" -- that skips cases that are really comparing.
_HOST_ANCHORED_ABSOLUTE_CASES = frozenset({"absolute-artefact-passes-through"})


#: Case IDs whose expected message (or whose pass/fail shape) depends on a
#: LIVE tool-presence probe against PATH -- either `plan_yocto_wic`'s own
#: `which("bmaptool")`/`which("dd")` (`tan/core/flash_plan.py:1002-1056`), or
#: the required-tool gate `tool_gate` (`flash_plan.py:1524-1545`, reached via
#: `doctor_cmd.on_path` at `flash_cmd.py:879-882`). The frozen fixture
#: recorded whichever tool inventory the CAPTURE host happened to have --
#: `dd` present/`bmaptool` absent for the three yocto cases, `west` absent
#: for the `zephyr_west_flash` one below -- so replaying on a host with
#: a DIFFERENT inventory (this box, for one: real `dd`, no `bmaptool`, and a
#: broken but PATH-resolvable `west` shim) makes the port pick/find a
#: different tool and diffs on text that is not a port bug at all
#: (tan-cli#313).
#:
#: `tool_gate` is bypassed ONLY under `--dry-run` or an empty `requires`
#: (its own docstring) -- it is LIVE for every other case that reaches it.
#: That is NOT every other case in `CASES`, though: two more non-dry-run
#: cases never reach `tool_gate` at all, refused by an earlier check in
#: `flash_cmd.py`'s dispatch order (traced directly, not inferred):
#:   * `no-artefact-real-run-fails` -- fails the empty-artefact check
#:     BEFORE `tool_gate` (`flash_cmd.py:856-860`).
#:   * `sdk-root-invalid` -- refused at SDK-root resolution, before the
#:     manifest is even read (`flash_cmd.py:1163-1175`), nowhere near a
#:     backend or `tool_gate`.
#: See `_pin_tool_inventory` for why the four pinned below DO need it.
_TOOL_PROBE_PINNED_CASES = frozenset(
    {
        "empty-boot-order-sorts-and-helpers-last",
        "yocto-unconfirmed-is-planned-not-ok",
        "yocto-alias-method-resolves",
        "missing-tool-fails",
    }
)


def _pin_tool_inventory(work_dir) -> str:
    """A scratch PATH holding exactly one stand-in tool, `dd` -- matching what
    the fixture's capture host had (`dd`, no `bmaptool`, no `west`) -- for
    `python_env_overrides` to hand to the PYTHON side alone (tan-cli#313).

    Serves two different probes with the one stub dir: `plan_yocto_wic`'s
    `which("bmaptool")`/`which("dd")` (the three yocto case IDs), where `dd`
    being FOUND is the point, and `tool_gate`'s `west` probe (the one
    remaining `missing-tool-*` case ID, `missing-tool-fails` --
    `missing-tool-skips-with-flag` moved out under tan-cli#487 defect 7),
    where `dd`'s presence is irrelevant and `west` simply not being anywhere
    on this replaced PATH is what the frozen "none found" answer needs.

    Replaces PATH outright rather than prepending: `doctor_cmd.on_path`
    (what `plan_yocto_wic`'s and `tool_gate`'s `which` callables both
    resolve to) walks every directory looking for a name match, so
    prepending a `dd`-only directory ahead of the replay host's real PATH
    would still let a REAL `bmaptool` or `west` further down it be found --
    which is exactly the host-dependence this fixes. The stub's own content
    is never read: every case that pins this is a `--dry-run` preview, an
    unconfirmed "would run" plan, or the required-tool gate's own refusal/
    skip message, so `dd` is only ever named in a message, never spawned.
    `chmod` is a no-op on Windows (no execute bit there), where
    `os.access(path, os.X_OK)` accepts any existing file -- covered by
    `doctor_cmd.on_path`'s own docstring.
    """
    stub_dir = work_dir / "tool-stub"
    stub_dir.mkdir()
    dd_stub = stub_dir / "dd"
    dd_stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    dd_stub.chmod(0o755)
    return str(stub_dir)


@pytest.mark.parametrize("case_id, manifest, extra", CASES, ids=[c[0] for c in CASES])
def test_flash_matches_the_rust_oracle(case_id, manifest, extra, work_dir):
    if case_id in _HOST_ANCHORED_ABSOLUTE_CASES and not oracle_fixtures.REPLAY_IS_CAPTURE_PLATFORM:
        pytest.skip(
            f"{case_id} plants a POSIX-absolute artefact path; the frozen answer "
            f"was captured on {oracle_fixtures.CAPTURE_PLATFORM}, which anchors it "
            f"to a DRIVE (`C:/abs/tree`) -- a real OS difference, not a port defect"
        )
    if manifest is not None:
        (work_dir / "build" / "system-manifest.yaml").write_text(
            manifest, encoding="utf-8", newline=""
        )
    python_env_overrides = (
        {"PATH": _pin_tool_inventory(work_dir)} if case_id in _TOOL_PROBE_PINNED_CASES else None
    )
    result = compare(
        _argv(extra),
        work_dir,
        surface=ENVELOPE,
        home=work_dir,
        python_env_overrides=python_env_overrides,
    )
    assert result.matches, f"{case_id}: " + "; ".join(result.diffs)


def _argv(extra, app_path="."):
    """`flash` argv with the default `--sdk-root ./sdk`, unless the case supplies
    its own. Repeating the flag is NOT a way to override it: clap rejects a
    duplicate outright (exit 2, `cli.parse-error`) while Click takes the last
    one, so a case built that way would compare two parsers rather than two
    flash implementations."""
    base = [] if "--sdk-root" in extra else ["--sdk-root", "./sdk"]
    return ["flash", *base, "--format", "json", *extra, app_path]


def _dd_matches_the_captured_implementation() -> bool:
    """Is the `dd` on PATH the one whose stderr the fixture froze?

    This is the only case that compares a THIRD-PARTY tool's message, so the
    frozen answer is valid only where that tool words its failure the same
    way. GNU coreutils `dd` (the capture host's, via ubuntu CI too) says ``dd:
    failed to open '<path>': No such file or directory``; macOS ships a BSD
    `dd` that says ``dd: <path>: No such file or directory``. Neither is
    wrong, and neither is tan's text.

    Probed rather than keyed off `sys.platform`: what matters is the
    implementation, not the OS -- a Linux host with busybox, or a mac with
    coreutils on PATH, both get the right answer this way. BSD `dd` has no
    `--version` and exits non-zero, so the check is a natural fit.
    """
    try:
        proc = subprocess.run(
            ["dd", "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "coreutils" in proc.stdout.lower()


def test_a_real_spawn_diffs_including_the_captured_failure_tail(work_dir):
    """The one case that actually SPAWNS a flash tool.

    `ALP_FLASH_FORCE=1` arms the confirm gate, so `yocto_wic` stops planning and
    runs `dd if=<a-file-that-does-not-exist> of=/dev/sdb`. `dd` fails to open the
    source before it opens the target, so nothing is written anywhere -- and the
    envelope then carries the CAPTURED stderr tail, which is the only way to
    exercise `_capture_tail` / `_execute_message` against the oracle at all.

    Skipped where `dd` is absent: the two sides would then agree on a DIFFERENT
    message (the no-tool refusal), which is real parity but not this test's
    subject.

    Also skipped where the local `dd` is not the implementation the fixture
    captured -- see `_dd_matches_the_captured_implementation`.

    Also skipped where `bmaptool` IS present: `plan_yocto_wic`'s
    `if bmaptool or (planning_only and not dd)` (`flash_plan.py:1023-1026`)
    picks `bmaptool` whenever it is found on PATH, unconditionally --
    `planning_only` narrows nothing here, since the check is a bare `or`.
    This is an ordinary Yocto dev host (`apt install bmap-tools` is the
    documented way to get the preferred tool), not an exotic one, and
    unlike the yocto CASES above this test is not in `_TOOL_PROBE_PINNED_CASES`
    and cannot be: it actually SPAWNS the resolved tool (that is the test's
    whole subject, its captured stderr tail), and a `dd` stand-in that only
    NAMES the tool would defeat that -- pinning would need a stub `dd`
    faithful enough to reproduce the fixture's exact captured failure text,
    which is just `_dd_matches_the_captured_implementation`'s own job
    restated. Measured: with a `bmaptool` stub on PATH the python side spawns
    it (exits `rc=1`, no captured tail) while the frozen fixture still names
    `dd`'s tail -- tan-cli#313 at this call site too.
    """
    if shutil.which("dd") is None:
        pytest.skip("no `dd` on PATH; nothing to spawn")
    if not _dd_matches_the_captured_implementation():
        pytest.skip(
            "the local `dd` words its open failure differently from the one the "
            "fixture captured (BSD `dd: <path>: No such file` vs GNU `dd: failed "
            "to open '<path>': No such file`); the tail this test compares is the "
            "SPAWNED TOOL's text, not tan's, so that diff is not a port defect"
        )
    if shutil.which("bmaptool") is not None:
        pytest.skip(
            "a host with bmaptool on PATH plans that tool instead of dd "
            "(flash_plan.py:1023-1026 prefers it unconditionally); this test's "
            "subject is the SPAWNED dd's captured failure tail, not bmaptool's"
        )
    (work_dir / "build" / "system-manifest.yaml").write_text(
        _slice("yocto_wic", "{target: /dev/sdb}"), encoding="utf-8", newline=""
    )
    previous = os.environ.get("ALP_FLASH_FORCE")
    os.environ["ALP_FLASH_FORCE"] = "1"
    try:
        result = compare(
            ["flash", "--sdk-root", "./sdk", "--format", "json", "."],
            work_dir,
            surface=ENVELOPE,
            home=work_dir,
        )
    finally:
        if previous is None:
            os.environ.pop("ALP_FLASH_FORCE", None)
        else:
            os.environ["ALP_FLASH_FORCE"] = previous
    assert result.matches, "; ".join(result.diffs)


#: Every case above passes `app_path = "."`, where the workspace root and the app
#: directory are the SAME path -- which hid a real bug: `project.root` and SDK
#: discovery anchor on the WORKSPACE root (cwd + the global `--project`) while
#: `app_path` is the flash-local positional feeding only `build_root`. An
#: app_path-anchored port reports `cwd/app` as `project.root` and hunts for the
#: SDK one level too deep, and no `.` case can see it. These are the cases that
#: separate the two anchors.
SUBDIR_CASES = [
    ("subdir-app-path-explicit-sdk", ["--dry-run", "--sdk-root", "./sdk"]),
    ("subdir-app-path-discovered-sdk", ["--dry-run"]),
    ("subdir-app-path-explicit-build-root", ["--dry-run", "--build-root", "app/build"]),
]


@pytest.mark.parametrize("case_id, extra", SUBDIR_CASES, ids=[c[0] for c in SUBDIR_CASES])
def test_workspace_root_and_app_path_are_separate_anchors(case_id, extra, tmp_path_factory):
    root = tmp_path_factory.mktemp("flash-subdir")
    (root / "app" / "build").mkdir(parents=True)
    # The SDK sits beside the CWD, which is what `discover_sdk_root` probes --
    # NOT inside `app/`, so an app_path-anchored resolver misses it entirely.
    (root / "sdk" / "scripts").mkdir(parents=True)
    (root / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (root / "alp-sdk" / "scripts").mkdir(parents=True)
    (root / "alp-sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (root / "app" / "build" / "system-manifest.yaml").write_text(
        _slice("swd_probe"), encoding="utf-8", newline=""
    )
    result = compare(_argv(extra, "app"), root, surface=ENVELOPE, home=root)
    assert result.matches, f"{case_id}: " + "; ".join(result.diffs)


def test_format_json_before_the_subcommand_is_accepted(work_dir):
    """clap makes `--format` `global = true`, so the oracle takes it on either
    side of the subcommand name. Refusing the pre-subcommand position on THIS
    command would mean a customer's flash silently does not run, so `flash` is in
    `cli._HONOURS_ROOT_FORMAT`."""
    (work_dir / "build" / "system-manifest.yaml").write_text(
        _slice("swd_probe"), encoding="utf-8", newline=""
    )
    argv = ["--format", "json", "flash", "--sdk-root", "./sdk", "--dry-run", "."]
    result = compare(argv, work_dir, surface=ENVELOPE, home=work_dir)
    assert result.matches, "; ".join(result.diffs)


def test_unknown_method_diverges_by_exactly_the_flow_d_registry_key(work_dir):
    """The one DELIBERATE envelope divergence, bounded to a single token.

    `alif_mram_jlink` (Flow D) is a backend this port registers and the shipped
    Rust does not, so the "Available: [...]" list in the unknown-`flash_method`
    refusal is one entry longer. Asserted as an EXACT set difference rather than
    waved through: `registry_keys()` feeds a customer-facing message, and a
    second unexpected key -- or a MISSING one, which would mean a ported backend
    silently vanished from the registry -- must fail here.
    """
    (work_dir / "build" / "system-manifest.yaml").write_text(
        _slice("bogus_thing"), encoding="utf-8", newline=""
    )
    from .oracle import _run, python_command  # noqa: PLC0415 -- test-only seam

    argv = _argv([])
    rust_code, rust_out = rust_run(argv, work_dir, work_dir)
    py_code, py_out = _run(python_command(), argv, work_dir, work_dir)
    assert rust_code == py_code == 1
    assert _available(py_out) - _available(rust_out) == {"alif_mram_jlink"}
    assert _available(rust_out) - _available(py_out) == set()
    # Everything OUTSIDE the message must still be identical.
    for side in (rust_out, py_out):
        entry = side["data"]["entries"][0]
        assert (entry["kind"], entry["id"], entry["method"], entry["status"], entry["rc"]) == (
            "slice", "c1", "bogus_thing", "failed", 1
        )
        assert [i["code"] for i in side["issues"]] == ["flash.entry-failed"]


def _available(payload):
    """The method names out of an unknown-`flash_method` refusal message."""
    message = payload["data"]["entries"][0]["message"]
    listed = message.split("Available: [", 1)[1].rstrip("]")
    return {token.strip().strip('"') for token in listed.split(",")}


#: `(case_id, manifest, extra)` for the two cases `openocd_program_word`'s
#: unconditional bracing (tan-cli#511) moved OUT of the byte-diff `CASES`
#: table above -- see the standing divergence note there for why. Kept as
#: their own list (not folded into `CASES`) because they need a DIFFERENT
#: comparator: not "match byte-for-byte" but "match except this one word".
_OPENOCD_BRACE_DIVERGENCE_CASES = [
    (
        "multi-segment-interface-is-allowed",
        _slice(
            "swd_probe",
            "{use_openocd: true, interface: ftdi/olimex-arm-usb-ocd-h, target: gd32g553}",
        ),
        ["--dry-run"],
    ),
    (
        "openocd-forced-bin-appends-base",
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.bin, status: ok, flash_method: swd_probe,
   flash_args: {use_openocd: true, interface: cmsis-dap, target: gd32g553}}
helper_mcus: []
boot_order: []
""",
        ["--dry-run"],
    ),
]

#: The exact `-c program <word>` artefact token, isolated so the assertion
#: below can wrap ONLY it -- not "somewhere in the message a `{` appeared",
#: which would also pass for a brace that landed on the wrong word entirely.
_PROGRAM_WORD_RE = re.compile(r"(?<=-c program )(\S+)(?= verify)")


def _braced_program_word(message: str) -> str:
    """`message` with its unbraced oracle `-c program <word>` wrapped in
    `{...}`, matching what `openocd_program_word` now always does. The
    oracle's own word never contains whitespace (that is the whole reason it
    was left unbraced under the old predicate), so `\\S+` captures it whole."""
    return _PROGRAM_WORD_RE.sub(lambda m: "{" + m.group(1) + "}", message, count=1)


@pytest.mark.parametrize(
    "case_id, manifest, extra",
    _OPENOCD_BRACE_DIVERGENCE_CASES,
    ids=[c[0] for c in _OPENOCD_BRACE_DIVERGENCE_CASES],
)
def test_openocd_program_word_diverges_from_the_oracle_by_exactly_the_brace(
    case_id, manifest, extra, work_dir
):
    """The ONE bounded envelope divergence unconditional bracing introduces
    (tan-cli#511), in the shape of `test_unknown_method_diverges_by_exactly_
    the_flow_d_registry_key` above: run BOTH sides for real (frozen replay
    for rust, exactly like every other case in this module) and assert the
    whole envelope matches EXCEPT that the python `-c program` word is the
    oracle's own word wrapped in `{...}` -- and that nothing else moved.
    `multi-segment-interface-is-allowed` and `openocd-forced-bin-appends-
    base` used to be plain `CASES` entries (byte-diffed whole); they moved
    here when bracing went unconditional -- see the standing divergence note
    above `CASES` -- so a SECOND, unexpected drift in either envelope still
    fails this suite, which is the whole point of a bounded exact-difference
    test over simply dropping the coverage."""
    (work_dir / "build" / "system-manifest.yaml").write_text(
        manifest, encoding="utf-8", newline=""
    )
    from .oracle import _run, normalise_path_separators, python_command  # noqa: PLC0415

    argv = _argv(extra)
    roots = (work_dir, work_dir)
    r_code, r_out = rust_run(argv, work_dir, work_dir, scrub_roots=roots)
    p_code, p_out = _run(python_command(), argv, work_dir, work_dir)
    p_out = oracle_fixtures.scrub(p_out, *roots)
    r_out = normalise_path_separators(r_out)
    p_out = normalise_path_separators(p_out)
    r_out = oracle_fixtures.normalise_scrubbed_path_separators(r_out)
    p_out = oracle_fixtures.normalise_scrubbed_path_separators(p_out)

    assert r_code == p_code == 0, f"{case_id}: rust={r_code} python={p_code}"

    r_entries = r_out["data"]["entries"]
    p_entries = p_out["data"]["entries"]
    assert len(r_entries) == len(p_entries) == 1, f"{case_id}: {r_entries} vs {p_entries}"
    r_message = r_entries[0]["message"]
    p_message = p_entries[0]["message"]
    assert p_message == _braced_program_word(r_message), (
        f"{case_id}: expected ONLY the -c program word to gain braces; "
        f"rust={r_message!r} python={p_message!r}"
    )

    # Everything OUTSIDE that one message must still be identical -- blank
    # both sides' message (already proven equal-modulo-brace above) and diff
    # the WHOLE remaining envelope.
    r_out["data"]["entries"][0]["message"] = None
    p_out["data"]["entries"][0]["message"] = None
    assert r_out == p_out, f"{case_id}: unexpected divergence beyond the brace: {r_out!r} != {p_out!r}"


#: Cases whose ENVELOPE cannot match byte-for-byte, with the reason. Diffed on
#: exit code + issue CODE only -- which is what `contract/README.md` freezes
#: (codes are matched with `===`; messages are prose with no contract over
#: them). Listed rather than dropped so the gap stays visible.
MESSAGE_ONLY_DIVERGENT = [
    # serde_yaml's parse-error text ("did not find expected ',' or ']' at line 2
    # column 1, while parsing a flow sequence at line 1 column 4") is its own
    # renderer's; PyYAML cannot reproduce it and reproducing it by hand would be
    # a parser rewrite for a prose string.
    ("malformed-yaml", "a: [unclosed\n", [], "flash.manifest-invalid"),
    # Same class: the shape error a serde struct reports vs a hand-rolled reader.
    ("slice-missing-os", "schema_version: 1\nslices:\n- {core_id: c1}\n", [],
     "flash.manifest-invalid"),
]


@pytest.mark.parametrize(
    "case_id, manifest, extra, code",
    MESSAGE_ONLY_DIVERGENT,
    ids=[c[0] for c in MESSAGE_ONLY_DIVERGENT],
)
def test_manifest_shape_errors_agree_on_code_and_exit(case_id, manifest, extra, code, work_dir):
    (work_dir / "build" / "system-manifest.yaml").write_text(
        manifest, encoding="utf-8", newline=""
    )
    argv = _argv(extra)
    result = compare(argv, work_dir, surface=ENVELOPE, home=work_dir)
    # The whole-envelope diff is EXPECTED to fail here, and only on `issues`
    # (the message) -- never on the exit code, never on `data`.
    offending = {d.split(":", 1)[0] for d in result.diffs}
    assert offending <= {"issues"}, f"{case_id}: unexpected divergence: {result.diffs}"
    from .oracle import _run, python_command  # noqa: PLC0415 -- test-only seam

    sides = {
        "rust": rust_run(argv, work_dir, work_dir),
        "python": _run(python_command(), argv, work_dir, work_dir),
    }
    for name, (exit_code, payload) in sides.items():
        assert exit_code == 1, f"{case_id}: {name} exited {exit_code}"
        assert [i["code"] for i in payload["issues"]] == [code], f"{case_id}: {name} {payload['issues']}"
