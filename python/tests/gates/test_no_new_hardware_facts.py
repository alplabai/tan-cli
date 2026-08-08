# SPDX-License-Identifier: Apache-2.0
"""ADR-0017 / invariant I-26 gate: `tan` must not learn a hardware fact.

Every hardware fact -- a SKU, a part number, a register or I2C address, a pin
name, a vendor-specific Kconfig symbol -- lives ONCE under alp-sdk's
`metadata/**`, and downstream files are generated from it. The moment `tan`
carries one, there is a second source of truth and the unification that alp-sdk
exists to provide is broken.

This is an **allowlist** gate rather than a ban, because some of these literals
are legitimate: `tan explain` is a teaching surface whose prose deliberately
names real parts to the customer. What is unacceptable is a NEW one appearing
unnoticed. So the gate pins the set that exists today, each with a reason, and
fails on anything else -- the debt stays visible and capped, and the next one has
to be argued for in this file rather than slipping in.

Written because the planner relocation (`alp_orchestrate` -> `tan/planner`)
carried literals across and nothing existed to catch the next one.
"""

from __future__ import annotations

import pathlib
import re

#: A hardware fact, narrowly: SoM SKUs, Alif/GD32 part numbers, the 7-bit I2C
#: address field, and vendor-specific Kconfig symbols. Deliberately NOT a generic
#: hex match -- ordinary constants, sizes and masks are not hardware facts and
#: would drown the signal.
PATTERNS = (
    re.compile(r"E1M-[A-Z0-9]+"),
    re.compile(r"AE822[A-Z0-9]*"),
    re.compile(r"GD32G[A-Z0-9]*"),
    re.compile(r"addr_7bit"),
    re.compile(r"CONFIG_ALP_SDK_WIFI_[A-Z0-9]+"),
)

#: file -> why its CODE literals are tolerated. Comments and docstrings are
#: stripped before matching, so an entry here means real executable code.
#: A file not listed here may contain no match at all.
#:
#: Every entry is DEBT unless marked OK. The value of this gate is that the list
#: cannot grow silently.
ALLOWED: dict[str, str] = {
    "explain_cmd.py": "OK: customer-facing prose; naming real parts is the feature",
    "hw_info.py": (
        "OK: EMITTED customer-facing prose, not a resolved fact. The `E1M-AEN801` "
        "mention is inside the comment block `_emit_hw_info_h` WRITES INTO the "
        "generated `<alp/hw_info.h>`, as the worked example for why "
        "CONFIG_BOARD/CONFIG_BOARD_TARGET differ per slice while "
        "ALP_HW_BUILD_PRIMARY_CORE may not (\"the HE slice's CONFIG_BOARD is "
        "\\\"alp_e1m_aen801_m55_he\\\"\"). It steers nothing: no branch reads it, and "
        "every value the header actually emits is resolved from the bound SDK's "
        "metadata. Ported byte-for-byte from alp-sdk#1279 -- this file mirrors "
        "`scripts/alp_project_emit/hw_info.py`, so paraphrasing the example to "
        "dodge this gate would make the next port a hand-merge AND would break "
        "the emit-parity byte comparison, since the text is output."
    ),
    "bootstrap.py": "OK: guidance prose naming the bridge a customer may build",
    "scaffold.py": (
        "DEBT (largest): DEFAULT_SOM_SKU, IOT_STARTER_SUPPORTED_SKU, _FAMILY_TREES "
        "and sku.startswith(('E1M-V2N','E1M-V2M')) branching -- tan picks a template "
        "tree by SKU FAMILY, which is the vendor branching I-26 forbids. Retires when "
        "the template catalogue declares its family mapping in metadata."
    ),
    "renode_sim.py": (
        "DEBT: WIRED_CONSOLE_SKUS hardcodes E1M-AEN801 -- the SKUs whose retired-Python "
        "`_SIM_BOARD_PROFILES` console was a wired hardware UART rather than the "
        "`ram_console_buf` RAM ring. Landed with the tan-cli#77 --sim-mode port. It is a "
        "real vendor fact in tan and the gate is right to flag it; it is allowlisted "
        "rather than dropped because deleting it would make the silent-UART warning "
        "claim the firmware printed nothing, when the truth is that the wired-console "
        "path is deferred. Retires when the sim descriptor's console kind is read from "
        "the SoM preset instead of a SKU list -- the same fix `scaffold.py` below waits on."
    ),
    "models.py": (
        "DEBT: a literal 7-bit I2C address -- the clearest breach in the tree. Rode "
        "along with the planner relocation; belongs in metadata."
    ),
    "kconfig.py": (
        "DEBT: a hardcoded vendor Kconfig symbol. Emitting Kconfig is the planner's "
        "job, but WHICH symbol a given part needs is a hardware fact."
    ),
    "doctor_cmd.py": (
        "DEBT (partial): `jlink_flash_device()` now resolves the AE822 profile from "
        "metadata/socs/alif/ensemble/e8.json variants[].debug.jlink_flash_device at "
        "runtime when an SDK checkout resolves. JLINK_AEN_DEVICE remains as the "
        "FALLBACK for a doctor run with no --sdk-root, kept byte-identical to today's "
        "metadata value; retires once doctor refuses to run without a resolved SDK."
    ),
    "flash_plan.py": (
        "DEBT: _DEFAULT_JLINK_DEVICE, inherited byte-identically from crates/tan-cli "
        "builders.rs -- a pre-existing I-26 breach in the Rust, kept faithful by the "
        "port rather than fixed silently."
    ),
    "new_som_cmd.py": (
        "MIXED. `DEFAULT_BOARD = \"E1M-EVK\"` is DEBT: a UI default only, not a "
        "value the file trusts -- every accepted --default-board (including this "
        "unedited default) is cross-checked against metadata/boards/*.yaml's real "
        "`name:` values before anything renders, so a stale literal here fails "
        "LOUD (`default board 'E1M-EVK' does not match any name: in "
        "metadata/boards/`) instead of silently shipping a wrong one -- unlike an "
        "address or pin name, this one cannot drift into a silently-wrong "
        "artifact. The remaining five hits (E1M-AEN801/E1M-V2N102/E1M-NX9101) are "
        "OK: teaching prose embedded in the GENERATED skeleton's comments, "
        "pointing a vendor at real committed example presets for 'the two core "
        "shapes' / pad_routes / helper_firmware conventions -- inherited "
        "byte-identical from the alp-sdk original (scripts/alp_cli/new_som.py) so "
        "the generated output stays diffable against it; naming real examples is "
        "the point, the same category explain_cmd.py and bootstrap.py are OK for."
    ),
    "pinmux_cmd.py": (
        "OK: `_FAMILY_PREFIX_TABLE` maps an E1M-* SKU prefix to its "
        "metadata/pinmux/<family>.yaml stem (the family this command's own "
        "table lookup is FOR), and the `--sku` option's help text names a real "
        "example SKU -- the same 'naming real parts is the feature' category "
        "explain_cmd.py/bootstrap.py are OK for, not a fact tan decides "
        "anything from silently."
    ),
    "zephyr_board.py": (
        "DEBT: two `E1M-EVK` mentions inside EMITTED devicetree prose (the generated "
        "`-pinctrl.dtsi` and `.dts` say which carrier wires the console). Not a fact "
        "tan decides anything from -- it is template text in a generator that "
        "relocated from alp-sdk's scripts/gen_zephyr_board.py, and it is byte-pinned "
        "against alp-sdk's committed zephyr/boards/alp/ tree by that repo's own "
        "tests/scripts/test_gen_zephyr_board.py, so rewording it here would break a "
        "merge-blocking gate over there. No metadata field can express it either: "
        "`emit_zephyr_board(sku, core_id, metadata_root)` is never told which carrier "
        "the SoM is mounted on. Retires when the carrier prose is promoted into a "
        "metadata field -- the same condition that module's docstring already records "
        "for the hand-authored `board.cmake` it deliberately does not generate."
    ),
}

TAN = pathlib.Path(__file__).resolve().parents[2] / "tan"
_FENCES = ('"' * 3, "'" * 3)


def _code_only(text: str) -> str:
    """Blank out comments and triple-quoted blocks.

    Crude, but conservative in the safe direction: a docstring that survives
    stripping yields a FALSE POSITIVE (a human looks), never a false negative
    (something slipping through unseen).
    """
    out: list[str] = []
    in_doc = False
    for line in text.splitlines():
        stripped = line.strip()
        fences = sum(stripped.count(f) for f in _FENCES)
        if in_doc:
            if fences:
                in_doc = False
            continue
        if stripped.startswith("#"):
            continue
        if fences == 1:
            in_doc = True
            continue
        if fences >= 2:
            continue
        out.append(line.split("  #")[0])
    return "\n".join(out)


def test_no_unallowlisted_hardware_fact_in_tan():
    offenders: list[str] = []
    for path in sorted(TAN.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        text = _code_only(path.read_text(encoding="utf-8", errors="replace"))
        for pattern in PATTERNS:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(
                    f"{path.relative_to(TAN.parent)} (stripped line {line_no}): "
                    f"{match.group(0)}"
                )
    assert not offenders, (
        "A hardware fact appeared in `tan`. Each one lives once under alp-sdk's\n"
        "metadata/** and must be resolved at runtime -- ADR-0017, invariant I-26.\n"
        "Resolve it from metadata, or if it is genuinely prose, add the file to\n"
        "ALLOWED in this test WITH a reason:\n  " + "\n  ".join(offenders)
    )


def test_the_allowlist_has_no_stale_entries():
    """An entry whose file no longer matches is debt that got paid -- delete it,
    so the list stays an accurate picture of what actually remains."""
    stale: list[str] = []
    for name in ALLOWED:
        hits = list(TAN.rglob(name))
        if not hits:
            stale.append(f"{name} (file gone)")
            continue
        text = _code_only(hits[0].read_text(encoding="utf-8", errors="replace"))
        if not any(p.search(text) for p in PATTERNS):
            stale.append(f"{name} (no longer contains a hardware fact in code)")
    assert not stale, "Stale ALLOWED entries -- remove them: " + ", ".join(stale)
