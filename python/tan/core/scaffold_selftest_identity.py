# SPDX-License-Identifier: Apache-2.0
"""SoM/SoC-identity retargeting for `tan.core.scaffold` -- the phrases
currently live only in README.md / src/main.c, but every `retarget_*`
function here runs UNCONDITIONALLY over `scaffold._vendored_files`'s
per-file loop, the same as `retarget_selftest_som_identity`'s own
no-SKU-list, general-anchored design: they are never gated to those two
paths in code, only narrowed by which phrases they anchor on
(`_selftest_som_identity_edits`, `# Example for <SKU>:`, the literal
`_AEN_TREE_SOC_REF` value) happening to appear nowhere else in the vendored
trees today. A future vendored file that happens to contain one of those
same anchor phrases for an unrelated reason would be rewritten too, not
skipped.

Split out of `scaffold.py` itself (tan-cli#932 review round) purely to keep
that module under its recorded size budget -- every function here is called
from exactly one place, `scaffold._vendored_files`'s per-file loop, and carry
no dependency on anything else in this package. Not a new layer of
abstraction: it is the same `retarget_*` functions that would otherwise
live beside `retarget_board_yaml_som`/`retarget_board_yaml_cores`, moved
verbatim because the module they were written next to was already at its
line cap. `retarget_selftest_soc_identity` (tan-cli#952) is the one function
added here since the split rather than moved -- the SoC-identity half of
`retarget_selftest_som_identity`'s own gap, deliberately left for a
follow-up by tan-cli#946's docstring.
"""

from __future__ import annotations

#: Anchored substring pairs `(source-text, target-text)` factory, keyed on
#: `source_sku` -- the three shapes `diagnostics/<family>/README.md` and
#: `src/main.c` use to name the tree's own SKU in their "what a real board
#: prints" documentation (tan-cli#932 review round). Anchored to the whole
#: phrase around the token, not the bare SKU string: a bare
#: `content.replace(source_sku, sku)` would also rewrite `# Example for
#: <SKU>:` immediately above a `west build -b alp_e1m_<sku>_...` line that
#: does NOT get renamed (that Zephyr board target is a real, separately
#: vendored artefact -- e.g. no `alp_e1m_v2n102_m33_sm` board exists at all --
#: so relabelling the comment above it while leaving the command itself
#: pointed at the tree's OWN board would tell a V2N102 customer the
#: `v2n101` target below is theirs). Anchoring to these three phrases means
#: only the SoM-identity documentation moves; the build instructions, which
#: this function never touches, keep naming the tree's real vendored board.
#: The `# Example for <SKU>:` comment ITSELF is a separate, narrower edit --
#: see `retarget_example_build_target_comment` below.
def _selftest_som_identity_edits(source_sku: str, sku: str) -> tuple[tuple[str, str], ...]:
    return (
        (f"SoM identity: {source_sku} rev", f"SoM identity: {sku} rev"),
        (f"Real hardware ({source_sku},", f"Real hardware ({sku},"),
        (f"(real hardware, {source_sku})", f"(real hardware, {sku})"),
    )


def retarget_selftest_som_identity(content: str, sku: str, source_sku: str) -> str:
    """Rewrite `diagnostics`' "what a real board prints" documentation onto
    `sku` -- the SoM-identity half of tan-cli#932 that the issue's own fix
    left uncovered.

    #932 hand-substituted `diagnostics/E1M-V2N101`'s vendored bytes so the
    tree is correct FOR THAT SKU (its `SoM identity:`/`Real hardware (...)`
    lines name `E1M-V2N101`, its `SoC identity:` lines name
    `renesas:rzv2n:n44`). But E1M-V2N101/E1M-V2N102/E1M-V2M101/E1M-V2M102 are
    "the same PCB, variant-populated" (`scaffold._SOM_FAMILIES`) and all four
    render this ONE tree -- so `--som E1M-V2M101` scaffolded a project whose
    `board.yaml` correctly said `sku: E1M-V2M101` (`retarget_board_yaml_som`)
    but whose README and `src/main.c` still told the customer to expect
    `SoM identity: E1M-V2N101 ...` from their own selftest binary. Measured:
    `tan init --template board-diagnostics --som E1M-V2M101` before this fix.

    A per-SKU substitution table -- a fifth (sixth, ...) hand-written
    `un_edit_*` entry per new SKU -- is exactly the shape that produced the
    original bug: #932 fixed the SKU line for `E1M-V2N101` alone, because
    nothing generalised the fix to the family. This is the general form
    instead: it fires for ANY `sku != source_sku` sharing a tree (every
    E1M-AEN* SKU sharing `E1M-AEN801`'s tree gets the identical treatment,
    not just the two families named in #932's own issue), reads no SKU list
    of its own, and costs nothing to extend -- a fifth SKU added to
    `scaffold._SOM_FAMILIES` needs no matching edit here.

    Deliberately narrow in WHAT it rewrites (see `_selftest_som_identity_edits`):
    only the SoM-identity phrase, never the `SoC identity:` line -- that is
    `retarget_selftest_soc_identity`'s job, a SEPARATE function (added
    tan-cli#952) rather than a fourth entry in
    `_selftest_som_identity_edits`, because it NEUTRALIZES rather than
    substitutes (see its own docstring for why) and needs its own anchor
    (`_AEN_TREE_SOC_REF`) and its own no-op shape (V2N/V2M share one SoC, so
    it never fires for that family by construction; AEN's SKUs are
    different Ensemble variants, so it fires for every non-source AEN SKU).
    This function never touches the placeholder serial either
    (`<factory-serial>`/`AEN0000123` name no real SKU, so there is nothing
    to retarget). NO-OP when `sku == source_sku`, matching
    `retarget_board_yaml_cores`'s convention -- byte-exact passthrough for
    the tree's own two representative SKUs, so neither vendored-capture
    fixture nor the byte-parity gate's `DELIBERATE_EDITS` needs to change.
    """
    if sku == source_sku:
        return content
    for old, new in _selftest_som_identity_edits(source_sku, sku):
        content = content.replace(old, new)
    return content


#: What a non-source SKU's `SoC identity:` line reads after neutralizing.
#: Mirrors `retarget_example_build_target_comment`'s wording on purpose --
#: same disclosure shape ("this template's own vendored X -- substitute
#: your SoM's Y"), same reason: this module carries NO per-SKU SoC-ref
#: table (see `retarget_selftest_soc_identity`'s docstring for why one was
#: rejected), so it cannot assert a correct replacement value, only flag
#: the tree's own value as not-yours.
_SOC_IDENTITY_PLACEHOLDER = (
    "this template's own vendored SoC -- substitute your SoM's Ensemble variant"
)

#: The AEN family's own `SoC identity:` value, literal in the vendored
#: `E1M-AEN801` tree today. This is the ONE anchor `retarget_selftest_soc_
#: identity` needs -- not a SKU-keyed fact table, ADR-0017/I-26 territory
#: `test_no_new_hardware_facts.py` enforces (measured: a `dict[sku, soc_ref]`
#: version of this fix reds that gate, `tan/core/scaffold_selftest_identity.py
#: (stripped line N): E1M-AEN301` etc., one finding per SKU literal). Used
#: purely to DETECT and ERASE the tree's own value for every OTHER SKU, never
#: to assert what a different SKU's real value is -- the same "anchor a
#: phrase, don't assert a fact" shape `_selftest_som_identity_edits` and
#: `retarget_example_build_target_comment` already use. Naturally a no-op
#: against the V2N/V2M tree, which never contains this literal at all, so no
#: `source_sku` branch is needed to keep this scoped to the AEN family.
_AEN_TREE_SOC_REF = "alif:ensemble:e8"


def retarget_selftest_soc_identity(content: str, sku: str, source_sku: str) -> str:
    """Neutralize `diagnostics`' "what a real board prints" `SoC identity:`
    line(s) for every SKU that is NOT the tree's own -- the half of
    tan-cli#932's family widening that tan-cli#946 deliberately left
    uncovered (tracked as tan-cli#952).

    `retarget_selftest_som_identity` above fixes the `SoM identity:`/`Real
    hardware (...)` lines for every SKU sharing a vendored tree, but never
    touches `SoC identity:` -- correct for the V2N/V2M family (one shared
    SoC) but wrong for AEN: `E1M-AEN301`..`E1M-AEN701` render
    `E1M-AEN801`'s tree and, before this function, kept ITS `SoC identity:
    alif:ensemble:e8` line verbatim even though `E1M-AEN301`'s own silicon is
    a DIFFERENT Ensemble variant. Measured: `tan init --template
    board-diagnostics --som E1M-AEN301` before this fix printed a `SoM
    identity: E1M-AEN301` line (tan-cli#946 already corrected that half)
    directly beside a `SoC identity: alif:ensemble:e8` line -- a
    HALF-corrected identity block, which tan-cli#952 argues reads as MORE
    misleading than the fully uncorrected block on `dev` before #946: a
    reader can no longer tell the SoC line is foreign just by noticing the
    whole block belongs to another SoM.

    Neutralizes rather than retargets -- tan-cli#952's own issue weighed
    both shapes and flagged this one "probably right". A retarget (assert
    `sku`'s own SoC ref) needs a SKU -> SoC-ref fact table this SDK-free
    package is not allowed to carry: ADR-0017/invariant I-26, enforced by
    `test_no_new_hardware_facts.py`, exists precisely so `tan` never becomes
    a second source of truth for a fact `metadata/e1m_modules/<SKU>.yaml`'s
    `silicon:` key already owns (measured: a `dict[sku, soc_ref]` version of
    this fix reds that gate, one finding per SKU literal). This function
    asserts nothing about what `sku`'s real SoC is -- it only erases the
    OTHER SKU's value, the same disclose-don't-invent choice
    `retarget_example_build_target_comment` already made for the build-target
    comment (and for the identical reason: several sibling SKUs reaching that
    comment have no board of their own at all, so a guessed replacement would
    sometimes be provably wrong, not merely unverified).

    NO-OP when `sku == source_sku` (byte-exact passthrough for the tree's
    own representative SKU, matching every other `retarget_*` here) and,
    by construction, for the V2N/V2M tree (see `_AEN_TREE_SOC_REF`'s own
    comment -- its `SoC identity:` line is unconditionally correct for
    every SKU that shares that tree, so nothing here should ever touch it).
    """
    if sku == source_sku:
        return content
    return content.replace(
        f"SoC identity: {_AEN_TREE_SOC_REF}",
        f"SoC identity: <{_SOC_IDENTITY_PLACEHOLDER}>",
    )


def retarget_example_build_target_comment(content: str, sku: str, source_sku: str) -> str:
    """Neutralize `# Example for <source_sku>:` -- the comment `minimal`/
    `sensor`/`diagnostics` READMEs put directly above their one real-silicon
    `west build -b alp_e1m_<source_sku>_..._sm/...` line -- for every OTHER
    SKU sharing the tree.

    Discovered widening `test_template_integrity.py`'s `CASES` to the full
    SKU catalogue (tan-cli#932 review round): with `retarget_selftest_som_
    identity` deliberately leaving this comment alone (see its own docstring
    -- the Zephyr board target below it is a real, separate artefact this
    SDK-free module cannot safely rename; several sibling SKUs, e.g.
    `E1M-V2N102`, have no board of their own at all, and one that DOES --
    `E1M-V2M101` -- is not something this module can discover), the comment
    still read "Example for E1M-V2N101" while scaffolding `E1M-V2M101`. Not
    the SoM-identity gap tan-cli#932 is about (nothing here claims a real
    board printed anything), but the SAME shape of claim: a specific SKU
    named beside a fact that is not that SKU's.

    Relabelling the comment onto `sku` would be worse, not better: it would
    claim the UNCHANGED command below it (still `source_sku`'s real board)
    is `sku`'s own. So this drops the SKU-specific claim rather than moving
    it -- but going fully generic (bare `# Example:`) would ALSO lose real
    information the customer needs: without any qualifier, a `sku` whose own
    `alp_e1m_<sku>_..._sm` target genuinely exists (e.g. `E1M-V2M101`, unlike
    several other siblings sharing this tree such as `E1M-V2N102`) has no
    way to tell, from the comment alone, that the line below it is a
    DIFFERENT SoM's board target rather than a generic placeholder.

    "Substitute your SoM's" (tan-cli#946 round-1 review's own suggested
    wording) is wrong for HALF the SKUs that reach this line: measured at
    `zephyr/boards/alp/`, only `e1m_v2n101_m33_sm` and `e1m_v2m101_m33_sm`
    exist -- `E1M-V2N102` and `E1M-V2M102` have no board tree of their own AT
    ALL (the fact this module's own docstring above cites), so telling those
    customers to "substitute your SoM's" points at a target that does not
    exist. Rewritten to qualify that instead: `# Example (this template's
    own vendored board target -- substitute your SoM's, where one exists):`,
    true for every SKU that reaches this line, with the one real vendored
    board target it introduces left exactly as documented. NO-OP when
    `sku == source_sku` (byte-exact passthrough for the tree's own two
    representative SKUs, same convention as every retarget_* above).
    """
    if sku == source_sku:
        return content
    return content.replace(
        f"# Example for {source_sku}:",
        "# Example (this template's own vendored board target -- substitute "
        "your SoM's, where one exists):",
    )
