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
import shutil

import pytest

from .oracle import ENVELOPE, compare, rust_binary

RUST = rust_binary()

pytestmark = pytest.mark.skipif(
    RUST is None, reason="no Rust tan to diff against; set TAN_RUST_BINARY"
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

#: A manifest with one helper MCU and no slices.
HELPER = """schema_version: 1
hw_info: {{sku: E1M-AEN801}}
slices: []
helper_mcus:
- {{name: h1, chip: cc3501e, firmware_path: fw.bin, flash_method: {method},
   flash_args: {args}}}
boot_order: []
"""


def _slice(method, args="{}"):
    return SLICE.format(method=method, args=args)


def _helper(method, args="{}"):
    return HELPER.format(method=method, args=args)


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
    # ── skips that must never fail the run ──────────────────────────────────
    ("helper-no-flash-method", _helper('""'), []),
    (
        "helper-update-channel-is-not-a-flash-target",
        """schema_version: 1
hw_info: {sku: S}
slices: []
helper_mcus:
- {name: cc3501e_otp, chip: cc3501e, firmware_path: fw.bin,
   update_channel: alp_ota_spi_otp}
boot_order: []
""",
        [],
    ),
    ("flash-args-tbd-mapping", _helper("swd_probe", "{mode: TBD, device: TBD}"), []),
    ("flash-args-tbd-bare-string", _helper("swd_probe", "TBD"), ["--dry-run"]),
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
    (
        "multi-segment-interface-is-allowed",
        _slice(
            "swd_probe",
            "{use_openocd: true, interface: ftdi/olimex-arm-usb-ocd-h, target: gd32g553}",
        ),
        ["--dry-run"],
    ),
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
    ("openocd-forced-bin-appends-base", """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: c1, os: zephyr, output_artefact: a.bin, status: ok, flash_method: swd_probe,
   flash_args: {use_openocd: true, interface: cmsis-dap, target: gd32g553}}
helper_mcus: []
boot_order: []
""", ["--dry-run"]),
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
    ("missing-tool-skips-with-flag", _slice("zephyr_west_flash"), ["--skip-missing-tools"]),
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


@pytest.mark.parametrize("case_id, manifest, extra", CASES, ids=[c[0] for c in CASES])
def test_flash_matches_the_rust_oracle(case_id, manifest, extra, work_dir):
    if manifest is not None:
        (work_dir / "build" / "system-manifest.yaml").write_text(
            manifest, encoding="utf-8", newline=""
        )
    result = compare(_argv(extra), work_dir, surface=ENVELOPE, home=work_dir)
    assert result.matches, f"{case_id}: " + "; ".join(result.diffs)


def _argv(extra, app_path="."):
    """`flash` argv with the default `--sdk-root ./sdk`, unless the case supplies
    its own. Repeating the flag is NOT a way to override it: clap rejects a
    duplicate outright (exit 2, `cli.parse-error`) while Click takes the last
    one, so a case built that way would compare two parsers rather than two
    flash implementations."""
    base = [] if "--sdk-root" in extra else ["--sdk-root", "./sdk"]
    return ["flash", *base, "--format", "json", *extra, app_path]


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
    """
    if shutil.which("dd") is None:
        pytest.skip("no `dd` on PATH; nothing to spawn")
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
    rust_code, rust_out = _run([RUST], argv, work_dir, work_dir)
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

    for command in ([RUST], python_command()):
        exit_code, payload = _run(command, argv, work_dir, work_dir)
        assert exit_code == 1, f"{case_id}: {command} exited {exit_code}"
        assert [i["code"] for i in payload["issues"]] == [code], f"{case_id}: {payload['issues']}"
