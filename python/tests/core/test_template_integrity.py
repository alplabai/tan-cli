# SPDX-License-Identifier: Apache-2.0
"""Every file a template REFERENCES is a file that template actually WRITES
(tan-cli#379).

`tan init` lays a template's files into a customer's empty directory. Nothing
downstream ever re-checks that the project it just wrote is self-consistent,
so a template could -- and, for `iot-starter`, DID -- ship a README linking a
file that was never vendored, and a `testcase.yaml` whose `extra_args:
EXTRA_CONF_FILE=native_sim.conf` named that same missing file. The scaffold
came out of `alp-sdk --emit scaffold`, whose envelope is the catalog's
`files.user_owned` list; `native_sim.conf` was absent from that list even
though the example directory carried it, so the emit never produced it and
neither vendored tree ever got it. Both byte-parity gates were GREEN
throughout: `tests/parity/scaffold_byte_parity.py` compared the vendored tree
against that same short emit, and the frozen Rust oracle has the identical
omission, so `tests/parity/test_scaffold_content_oracle_parity.py` compared
one incomplete tree against another.

That is the gap this file closes, and it is deliberately checked against the
PLANNER's output (`plan_template_files`) rather than the vendored directory:

* it covers `minimal-app` too, tan's own hand-generated template with no
  vendored tree at all (`tan.core.scaffold`'s `_minimal_app_files`), so a
  future dangling reference there is caught by the same rule;
* it is the bytes `tan init` really writes, including `board.yaml` after
  `retarget_board_yaml_som` -- not a directory listing that happens to feed
  it.

Two rules, both about the SAME class of defect (a name with no file behind
it), which is why they live in one file rather than two:

1. every RELATIVE Markdown link in a planned `.md` resolves to a planned path;
2. every `EXTRA_CONF_FILE=<name>` written anywhere in the template resolves to
   a planned path -- Zephyr fails the build outright on a missing overlay, so
   this one is a broken `west build`, not just a broken doc link.

A third rule, same class of defect one repo over (tan-cli#384): every ABSOLUTE
`https://github.com/alplabai/alp-sdk/blob/<ref>/...` link -- what the SDK emit
rewrites cross-directory links INTO -- pins `<ref>` at the ref this tree is
vendored from. Their targets cannot be resolved from a planned file set, but
their REF can, and that is where the failure was: the v0.15.0-rc1 re-vendor
shipped 40 links pinned at `v0.15.0`, a tag alp-sdk has never cut (the SDK's
link renderer drops the `-rc1` suffix), so every one of them 404s in a
customer's freshly scaffolded README.

That rule has TWO halves, and the second is the one #384 actually asks for.
Consistency with `MANIFEST.md` is not existence: a re-vendor could update both
the links and the manifest to the same ref and still ship 40 dead links. So
`test_the_vendored_ref_is_a_tag_alp_sdk_actually_has` asks GitHub, through the
EXACT-ref endpoint and no other -- see `_tag_exists`, where the whole trap
lives.
"""
import os
import re
import urllib.error
import urllib.request
import warnings
from pathlib import Path

import pytest

from tan.net import default_ssl_context
from tan.core.scaffold import (
    TEMPLATE_SUPPORTED_SKUS,
    TEMPLATE_IDS,
    VENDORED_ROOT,
    _FAMILY_TREES,
    _family_bucket,
    plan_template_files,
)
from tan.core.scaffold_selftest_identity import _AEN_TREE_SOC_REF, _SOC_IDENTITY_PLACEHOLDER
from tests.conftest import sdk_root

#: `[text](target)`. Non-greedy on the label so an `[a](x) ... [b](y)` line
#: yields two matches, not one spanning both.
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

#: `EXTRA_CONF_FILE=<value>`, as passed on a `west build -- -D...` line or in
#: twister's `extra_args:`. The lookbehind rejects Zephyr's per-image
#: `<image>_EXTRA_CONF_FILE=` spelling, which names a DIFFERENT variable.
_EXTRA_CONF = re.compile(r"(?<![\w])EXTRA_CONF_FILE=(\S+)")

#: A value `_EXTRA_CONF` captured that this check can resolve statically. The
#: same variable is legitimately written in forms that name no file at all:
#: `${_alp_generated}` (a CMake variable holding a BUILD-dir path, in every
#: template's `CMakeLists.txt`) and the literal `EXTRA_CONF_FILE=...` used as
#: prose ellipsis in a comment. Requiring a real Kconfig-fragment extension
#: skips exactly those and nothing else.
_RESOLVABLE_CONF = re.compile(r"^[\w./-]+\.(?:conf|overlay)$")

#: The `<ref>` of an absolute alp-sdk link, `blob/` (file) or `tree/` (dir).
_SDK_LINK_REF = re.compile(r"https://github\.com/alplabai/alp-sdk/(?:blob|tree)/([^/]+)/")

#: `MANIFEST.md`'s `- Ref:` line -- the ONE machine-readable statement of where
#: this tree came from. Keep that line's shape if the manifest is reworded.
_MANIFEST_REF = re.compile(r"^- Ref: `([^`]+)`", re.M)

#: `MANIFEST.md`'s `- Commit:` line -- where the vendored BYTES came from, as
#: distinct from `- Ref:`, which is the ref the rendered LINKS name. Nothing
#: read this line until tan-cli#821: it had asserted since 2026-08-09 that its
#: commit was "the same commit `parity.yml`'s `PINNED_SDK_TAG` now names",
#: which stopped being true four days later when the vendor point moved and
#: this line did not. Unread provenance is provenance that drifts.
#: Anchored to the FIRST `- Commit:` after `## Source`, not to the first in
#: the file: MANIFEST.md is an append-only re-vendor log, tan-cli#851 already
#: demonstrated inserting a bullet above the live one, and a bare `re.search`
#: would silently read a historical entry the day someone adds one higher up.
_MANIFEST_COMMIT = re.compile(r"^- Commit: \*\*`([0-9a-f]{7,40})`\*\*", re.M)

#: The same fact, stated a second time in the `## Source` summary -- short sha
#: on one line, full sha in the parenthetical below it.
_MANIFEST_VENDOR_POINT = re.compile(
    r"^- \*\*Current vendor point \(all templates\):\*\* \*\*`([0-9a-f]{7,40})`\*\*\s*\n"
    r"\s*\(`([0-9a-f]{40})`",
    re.M,
)

#: A ref that must NEVER exist as an alp-sdk tag, hardcoded rather than
#: derived from the live vendor ref. The original control derived it by
#: stripping `ref`'s pre-release suffix (`v0.15.0-rc1` -> `v0.15.0`) -- exactly
#: the pair GitHub's PREFIX-matching plural endpoint confuses, which is why
#: that shape existed at all. But alp-sdk went on to cut a real `v0.15.0` tag
#: (2026-08), so the SAME derivation now produces a ref that legitimately
#: EXISTS -- the control would silently stop asserting anything, exactly the
#: self-disabling failure mode a "negative control" exists to prevent. `v0.15`
#: (no patch component) keeps the same hazard shape without depending on the
#: live ref: it is a real PREFIX of both `v0.15.0` and `v0.15.0-rc1` (so the
#: plural endpoint still 200s on it, prefix-matching one or the other), while
#: alp-sdk's own tagging convention is always full `MAJOR.MINOR.PATCH`
#: (optionally `-rcN`) -- a bare `MAJOR.MINOR` has never been cut and would
#: break that convention if it ever were. Re-verified against the live repo:
#: singular `/git/ref/tags/v0.15` -> 404 (correctly absent); plural
#: `/git/refs/tags/v0.15` -> 200, prefix-matching `v0.15.0` (still a hazard).
#: Go stale only if alp-sdk cuts a literal `v0.15` tag, or once the vendored
#: line moves far enough (v0.16+) that `v0.15` stops being adjacent to
#: anything real -- if so, pick a fresh dead-but-plausible-prefix ref from the
#: new neighbourhood rather than re-deriving one from `_vendor_ref()`.
_DEAD_CONTROL_REF = "v0.15"


#: GitHub's **exact**-ref endpoint. Singular `ref`, and that is the entire
#: point -- see `_tag_exists`. `{qualified}` is a full `tags/<name>` or
#: `heads/<name>`: since alp-sdk#1535 the vendored ref can legitimately be the
#: `main` BRANCH (see `_ref_exists`), which the tags path would 404 on.
_REF_API = "https://api.github.com/repos/alplabai/alp-sdk/git/ref/{qualified}"

#: The one non-tag ref this tree may be vendored at -- alp-sdk#1535's
#: degradation target when the SDK's declared `v<version>` tag is not cut.
_MAIN_REF = "main"


def _vendor_ref() -> str:
    """The alp-sdk ref the vendored tree is captured from, read from the
    provenance manifest rather than hardcoded here, so a re-vendor updates the
    expected link ref and the recorded one together or fails."""
    manifest = (VENDORED_ROOT / "MANIFEST.md").read_text(encoding="utf-8")
    match = _MANIFEST_REF.search(manifest)
    assert match, f"no '- Ref: `<ref>`' line in {VENDORED_ROOT / 'MANIFEST.md'}"
    return match.group(1)


def test_the_manifest_states_one_vendor_point_not_two():
    """tan-cli#821(b): `MANIFEST.md` records the vendor commit TWICE -- once as
    the `## Source` summary's "Current vendor point" bullet, once as the
    `- Commit:` line in the re-vendor log. Nothing compared them, so they
    disagreed for six days: `- Commit:` still named `f30f4d4b` after
    tan-cli#714 moved the tree to `d00dbdc1`. The same line also asserted an
    identity with `parity.yml`'s `PINNED_SDK_TAG` which had already broken the
    day the line was written -- tan-cli#593 moved the pin 21h44m later -- so
    the two halves went stale independently and neither had a reader.

    Worst case is bounded -- a maintainer re-runs `--emit scaffold` against the
    wrong checkout, gets a bogus byte diff, and `scaffold_byte_parity.py` says
    so within one round -- but the cost is a wasted round every time, and the
    file is the only record of where these bytes came from.
    """
    manifest = (VENDORED_ROOT / "MANIFEST.md").read_text(encoding="utf-8")

    source = manifest[manifest.index("## Source") :]
    commit_match = _MANIFEST_COMMIT.search(source)
    assert commit_match, (
        "no '- Commit: **`<sha>`**' line in MANIFEST.md. If the line was "
        "reworded, keep its shape or update _MANIFEST_COMMIT in the same "
        "change -- this is the only reader it has."
    )
    point_match = _MANIFEST_VENDOR_POINT.search(manifest)
    assert point_match, (
        "no '- **Current vendor point (all templates):** **`<short>`**' bullet "
        "followed by its full sha in MANIFEST.md; same rule as above."
    )

    commit, short, full = commit_match.group(1), point_match.group(1), point_match.group(2)
    assert commit == short, (
        f"MANIFEST.md disagrees with itself about where the vendored bytes came "
        f"from: '- Commit:' says {commit!r}, 'Current vendor point' says "
        f"{short!r}. Re-vendoring moves BOTH, or the next person emits against "
        f"the wrong checkout and spends a round on a byte diff that was never "
        f"a real divergence (tan-cli#821)."
    )
    assert full.startswith(short), (
        f"the vendor-point bullet's short sha {short!r} is not a prefix of the "
        f"full sha {full!r} on the line below it."
    )


#: English number words `MANIFEST.md`'s prose spells its `DELIBERATE_EDITS`
#: count out in (digits nowhere in that sentence) -- `enumerate` gives each
#: word its natural value (`"zero"` -> 0, ..., `"twenty"` -> 20) with no
#: hand-kept mapping to drift.
_NUMBER_WORDS = {
    word: value
    for value, word in enumerate((
        "zero", "one", "two", "three", "four", "five", "six", "seven",
        "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
        "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
        "twenty-one", "twenty-two", "twenty-three", "twenty-four",
        "twenty-five", "twenty-six", "twenty-seven", "twenty-eight",
        "twenty-nine", "thirty", "thirty-one",
    ))
}

#: MANIFEST.md's own claim of how many `DELIBERATE_EDITS` entries are live --
#: "The `DELIBERATE_EDITS` table below currently carries **twenty** live
#: entries (counted from `scaffold_byte_parity.py`'s own `DELIBERATE_EDITS`
#: dict, not by hand)". The sentence SAYS it is not hand-counted; before
#: tan-cli#932's review round nothing had ever actually run the count it
#: claims to be.
_MANIFEST_DELIBERATE_EDIT_COUNT = re.compile(
    r"`DELIBERATE_EDITS` table below currently carries \*\*(\S+)\*\* live entries"
)


def _load_scaffold_byte_parity():
    """Load `tests/parity/scaffold_byte_parity.py` -- the standalone gate
    script, at the REPO ROOT, outside `python/`'s own `pythonpath` and never
    otherwise imported by this suite -- by file path rather than by package
    import (mirrors `tests/gates/test_module_size_budget.py`'s own
    `spec_from_file_location` loading of `regen_module_size_budget.py`, the
    established idiom in this tree for reaching a script outside the
    package).

    The script itself does `from _sdk_checkout import ...` -- a BARE sibling
    import that only resolves because Python auto-adds a `__main__` script's
    own directory to `sys.path[0]`, which `spec_from_file_location` does not
    do on its own. Its directory goes onto `sys.path` for the duration of
    `exec_module` (and is popped after, even on failure) to reproduce that,
    same as running `python3 tests/parity/scaffold_byte_parity.py` would."""
    import importlib.util
    import sys

    path = VENDORED_ROOT.parents[3] / "tests" / "parity" / "scaffold_byte_parity.py"
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(
            "_scaffold_byte_parity_under_test", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


def test_the_manifest_deliberate_edit_count_matches_the_table():
    """tan-cli#932 review round: `MANIFEST.md`'s "currently carries
    **twenty** live entries" sentence, and the itemised 1-9 enumeration
    right below it, are both prose a human keeps in sync by hand against
    `scaffold_byte_parity.py`'s real `DELIBERATE_EDITS` dict -- the sentence
    SAYS it counts "not by hand", but nothing had ever run that count until
    this test existed. Measured (before this same change fixed both): the
    itemised enumeration summed to 19 -- it was missing the
    `multicore-mailbox`/`E1M-AEN801` `blocked_caveat` entry entirely, which
    also had no numbered write-up anywhere in the 1-8 list -- one short of
    the twenty the bold sentence claimed and `DELIBERATE_EDITS` itself
    carries. This test pins the COUNT only (the itemised enumeration's own
    arithmetic is prose, not a second machine-checked fact); it is what
    keeps the next entry added or retired from rotting the sentence
    silently, the way this one did.
    """
    manifest = (VENDORED_ROOT / "MANIFEST.md").read_text(encoding="utf-8")
    match = _MANIFEST_DELIBERATE_EDIT_COUNT.search(manifest)
    assert match, (
        "no \"DELIBERATE_EDITS table below currently carries **<N>** live "
        "entries\" sentence in MANIFEST.md. If it was reworded, keep this "
        "shape or update _MANIFEST_DELIBERATE_EDIT_COUNT in the same change."
    )
    word = match.group(1)
    assert word in _NUMBER_WORDS, (
        f"MANIFEST.md's deliberate-edit count {word!r} is not a number word "
        f"this test recognises -- extend _NUMBER_WORDS in the same change."
    )
    stated = _NUMBER_WORDS[word]

    module = _load_scaffold_byte_parity()
    actual = len(module.DELIBERATE_EDITS)
    assert stated == actual, (
        f"MANIFEST.md claims {word!r} ({stated}) live DELIBERATE_EDITS "
        f"entries; scaffold_byte_parity.py's DELIBERATE_EDITS dict actually "
        f"has {actual}. Whoever added or retired an entry needs to update "
        f"the sentence (and its itemised enumeration right below it) in the "
        f"same change."
    )


def _tag_exists(ref: str) -> bool | None:
    """Does `refs/tags/<ref>` exist in alp-sdk? `None` when GitHub could not be
    asked at all (offline, rate-limited, 5xx) -- the caller SKIPS on that
    rather than inventing an answer.

    **`/git/ref/tags/` (singular), never `/git/refs/tags/` (plural).** The
    plural endpoint PREFIX-matches and returns a one-element array, so
    `.../refs/tags/v0.15.0` answers `200` with `refs/tags/v0.15.0-rc1` in the
    body -- measured. A gate built on it calls every one of tan-cli#384's 40
    dead links healthy, which is exactly the shape of not having a gate. The
    singular endpoint 404s on that same ref. `test_the_vendored_ref_is_a_tag_
    alp_sdk_actually_has` re-proves that distinction on every run instead of
    trusting this comment.

    Authenticates opportunistically: unauthenticated GitHub API is 60 req/hour
    per IP, which a shared CI runner exhausts routinely, and an exhausted quota
    here means a permanent SKIP -- a gate that never runs. `GITHUB_TOKEN` is
    present by default in Actions; a workflow still has to pass it through to
    the step for this to take effect.
    """
    return _exact_ref_exists(f"tags/{ref}")


def _exact_ref_exists(qualified: str) -> bool | None:
    """`_tag_exists`'s transport, with the `tags/`/`heads/` prefix left to the
    caller. Split out for `_ref_exists`; every note on `_tag_exists` about the
    singular endpoint, the 404-vs-rate-limit distinction and opportunistic
    auth applies here unchanged, because this IS that code."""
    request = urllib.request.Request(
        _REF_API.format(qualified=qualified),
        headers={"Accept": "application/vnd.github+json"},
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=15, context=default_ssl_context()) as response:
            return response.status == 200
    except urllib.error.HTTPError as e:
        # 404 is the real answer "no such tag"; 403/429 is a spent rate limit
        # and 5xx is GitHub, neither of which says anything about the ref.
        return False if e.code == 404 else None
    except (urllib.error.URLError, OSError):
        return None


def _ref_exists(ref: str) -> bool | None:
    """Does `ref` resolve in alp-sdk at all -- as a tag, or as `main`?

    alp-sdk#1535 made the scaffold emit degrade to `main` when the SDK's
    declared `v<version>` tag has not actually been cut, so `main` is now a
    ref this tree can legitimately be vendored at and it is NOT a tag. It is
    answered through the same singular exact-ref endpoint against `heads/`
    rather than exempted from the check -- an exemption would be a hole
    exactly where tan-cli#384's 40 dead links came through."""
    if ref == _MAIN_REF:
        return _exact_ref_exists(f"heads/{ref}")
    return _tag_exists(ref)


SDK = sdk_root()


def _catalogued_som_skus() -> tuple[str, ...] | None:
    """Every SoM SKU alp-sdk's own metadata catalogue ships a manifest for --
    one `metadata/e1m_modules/<SKU>.yaml` per SKU, so the SKU IS the
    filename stem; no field inside the YAML needs parsing. `None` without a
    bound SDK checkout (`ALP_SDK_ROOT`/`ALP_SDK_PARITY_ROOT`) rather than a
    hardcoded fallback list -- `_skus_for` below falls back to `_FAMILY_TREES`
    in that case, so an unbound run still exercises the two SKUs it always
    has and this only WIDENS coverage when a checkout is available.

    tan-cli#932 review round: `_skus_for` used to answer `_FAMILY_TREES`
    (two SKUs) for every unrestricted template, so `E1M-V2N102`/`E1M-V2M101`/
    `E1M-V2M102` and every `E1M-AEN301`..`E1M-AEN701` were never a `CASES`
    entry at all -- the exact-token guard below (`_foreign_sku_hits`) could
    not have caught #932's own defect class one SKU over, because the SKU it
    would have caught it on was never checked."""
    if SDK is None:
        return None
    root = SDK / "metadata" / "e1m_modules"
    return tuple(sorted(p.stem for p in root.glob("*.yaml")))


CATALOGUED_SKUS = _catalogued_som_skus()

if CATALOGUED_SKUS is None:
    # Degraded, not skipped: `_skus_for` and `_foreign_sku_hits` both still
    # run, just against `_FAMILY_TREES`'s two SKUs instead of the SDK's full
    # eleven -- a silent narrowing a contributor's local unbound run would
    # otherwise never learn about (tan-cli#946 review round: "the
    # `CATALOGUED_SKUS or _FAMILY_TREES` fallback emits no warning, so a
    # contributor's local unbound run is degraded with no signal").
    warnings.warn(
        "test_template_integrity: no ALP_SDK_ROOT/ALP_SDK_PARITY_ROOT bound -- "
        "SKU-catalogue coverage (CASES, _foreign_sku_hits) is degraded to "
        f"_FAMILY_TREES's {_FAMILY_TREES!r} instead of the SDK's full catalogue",
        stacklevel=1,
    )


def _skus_for(template_id: str) -> tuple[str, ...]:
    """Every SKU whose tree this template can be asked to plan.

    `iot-starter`/`multicore-mailbox` vendor (or the SDK catalogue accepts)
    exactly one family each -- `TEMPLATE_SUPPORTED_SKUS` -- so asking either
    for a SKU outside it would silently re-check the same tree a second time
    and report a SKU it never supports in the failure message.

    Every other template reads the full catalogued SKU set (falling back to
    `_FAMILY_TREES` when no SDK is bound). `minimal-app` is tan's one
    vendor-neutral template (`tan.core.scaffold`'s own docstring: "scaffolds
    every SKU") and gets all of them, `E1M-NX9101` included. Every vendored
    template is filtered through `_family_bucket`, the SAME lookup
    `plan_template_files` itself refuses an unsupported family through
    (`UnsupportedSomError`) -- `E1M-NX9101` has no vendored tree
    (`_SOM_FAMILIES`'s `("E1M-NX9", "m33", None)` row) and asking a
    vendored template to plan it is not a defect this file is testing for,
    it is the refusal `test_scaffold.py` covers directly."""
    restricted = TEMPLATE_SUPPORTED_SKUS.get(template_id)
    if restricted is not None:
        return restricted
    all_skus = CATALOGUED_SKUS or _FAMILY_TREES
    if template_id == "minimal-app":
        return all_skus
    return tuple(sku for sku in all_skus if _family_bucket(sku) is not None)


def _cases():
    for template_id in TEMPLATE_IDS:
        for sku in _skus_for(template_id):
            yield pytest.param(template_id, sku, id=f"{template_id}::{sku}")


CASES = list(_cases())


def _planned(template_id: str, sku: str) -> dict[str, str]:
    return {f.relative_path: f.content for f in plan_template_files(template_id, sku)}


def _resolves(target: str, paths: set[str]) -> bool:
    """`target` names a planned file, or a directory some planned file sits
    under (`src/`, `boards/` -- a directory is never itself a planned path)."""
    target = target.rstrip("/")
    return target in paths or any(p.startswith(target + "/") for p in paths)


@pytest.mark.parametrize("template_id,sku", CASES)
def test_every_relative_markdown_link_resolves_to_a_planned_file(template_id, sku):
    """tan-cli#379's user-visible half: `iot`'s README linked
    `[native_sim.conf](native_sim.conf)` at a file `tan init` never wrote."""
    planned = _planned(template_id, sku)
    paths = set(planned)
    dangling = []
    for name, content in planned.items():
        if not name.endswith(".md"):
            continue
        for target in _MD_LINK.findall(content):
            # Anchors, mail links and anything with a scheme point outside the
            # planned tree; `#frag` on an otherwise-relative target is not part
            # of the filename.
            if target.startswith(("#", "mailto:")) or "://" in target:
                continue
            relative = target.split("#", 1)[0]
            if relative and not _resolves(relative, paths):
                dangling.append(f"{name} -> {target}")
    assert not dangling, (
        f"--template {template_id} --som {sku}: relative link(s) with no file behind "
        f"them: {dangling}; planned files: {sorted(paths)}"
    )


@pytest.mark.parametrize("template_id,sku", CASES)
def test_every_extra_conf_file_named_by_a_template_is_a_planned_file(template_id, sku):
    """tan-cli#379's build-breaking half: `iot`'s README build command AND its
    `testcase.yaml` `extra_args` both passed `EXTRA_CONF_FILE=native_sim.conf`
    for a file the scaffold did not contain. Zephyr's `kconfig.cmake` errors
    out on a missing overlay, so this shipped a documented `west build` and a
    committed twister scenario that could not run at all."""
    planned = _planned(template_id, sku)
    paths = set(planned)
    missing = []
    for name, content in planned.items():
        for raw in _EXTRA_CONF.findall(content):
            # `EXTRA_CONF_FILE` takes a `;`-separated list; strip the quoting a
            # YAML scalar or a shell line puts around it before splitting.
            for value in raw.strip("\"'`,").split(";"):
                if _RESOLVABLE_CONF.match(value) and value not in paths:
                    missing.append(f"{name} -> {value}")
    assert not missing, (
        f"--template {template_id} --som {sku}: EXTRA_CONF_FILE names(s) with no planned "
        f"file behind them: {missing}; planned files: {sorted(paths)}"
    )


#: `(template_id, sku, foreign_sku) -> expected occurrence count` for a
#: DELIBERATE cross-SKU mention -- without this, the guard below would flag
#: `iot-starter`'s own "E1M-V2N101 is deliberately not supported" exclusion
#: note (both in its README and its `board.yaml` comment), which names the
#: sibling SKU ON PURPOSE to explain why `--som E1M-V2N101` refuses this
#: template. Keyed on the exact COUNT, not mere membership (tan-cli#946
#: round-2 review): a bare allowlist entry makes the pair invisible outright,
#: so an unrelated NEW cross-SKU mention added on top of a legitimate one --
#: same `(template_id, sku, foreign_sku)`, one extra occurrence -- would
#: silently pass instead of changing the count and demanding its own review.
#: Measured against the pre-fix (membership-only) guard: appending `/* Note:
#: E1M-V2M101 also works here. */` to `edge-ai/E1M-V2N101/src/main.c` left it
#: at 53 passed; the count form below reds on exactly that plant, and still
#: reds on a wholly un-allowlisted token (e.g. `E1M-NX9101`).
#:
#: The other twelve entries (tan-cli#946 review round, widening
#: `_foreign_sku_hits` off `CATALOGUED_SKUS` instead of the bare
#: `_FAMILY_TREES`) are all `edge-ai-starter`'s DEEPX DX-M1 pointer, in two
#: independent groups sharing one shape (name a DEEPX-equipped sibling SKU
#: to steer a customer toward it), not one:
#:
#: * the `E1M-AEN801` tree's `README.md`/`board.yaml` both tell an Alif
#:   customer to re-scaffold at `E1M-V2M101` for the DEEPX path
#:   (tan-cli#814) -- six entries, each counting 2 (one mention per file),
#:   one per AEN-family SKU that renders this tree (`E1M-AEN301`..`E1M-AEN801`);
#: * the `E1M-V2N101` tree's `README.md` names BOTH `E1M-V2M101` and
#:   `E1M-V2M102` as DEEPX-equipped (tan-cli#946 -- see
#:   `scaffold_byte_parity.py`'s `DELIBERATE_EDITS` entry
#:   `deepx_v2m102_scope` for why: the single-target-SKU wording this
#:   replaced was misleading for an `E1M-V2M102` customer) -- six entries,
#:   one per (sku, foreign) pair where the sentence's own SKU differs from
#:   the token: `E1M-V2N101`/`E1M-V2N102` each see both `E1M-V2M101` and
#:   `E1M-V2M102` as foreign, 1 mention each (they carry neither); `E1M-V2M101`
#:   sees `E1M-V2M102` as foreign and vice versa, 1 mention each (each is the
#:   sentence's OWN SKU for one of the two mentions, never for both).
#:
#: `iot-starter`'s `E1M-AEN801` entry counts 3: the exclusion note appears
#: once in `board.yaml`'s comment and twice in `README.md`.
_ALLOWED_CROSS_SKU_MENTIONS: dict[tuple[str, str, str], int] = {
    ("iot-starter", "E1M-AEN801", "E1M-V2N101"): 3,
    # tan-cli#996/#1001 (the 722320a1 re-vendor): alp-sdk's own DEEPX-note
    # rewrite (see MANIFEST.md's "Current vendor point" bullet) made the
    # sentence SKU-neutral for BOTH SKU pairs, not just the V2M101/V2M102
    # pair the entries below already covered -- "The DEEPX DX-M1 NPU is
    # populated on E1M-V2M101/E1M-V2M102 -- not on E1M-V2N101/E1M-V2N102,
    # the same PCB without it." Every planned `edge-ai-starter` tree now
    # names whichever of E1M-V2N101/E1M-V2N102 is not its own SKU once (both,
    # for a tree that is neither). A real cross-reference (explaining what
    # does and does not carry DEEPX), not a substitution gap; alp-sdk's own
    # rewrite is what retired the `deepx_v2m_note`/README.md and
    # `deepx_v2m102_scope` DELIBERATE_EDITS entries (entries 5 and 10 in
    # MANIFEST.md's "Deliberate edits on top of the emit").
    ("edge-ai-starter", "E1M-AEN301", "E1M-V2N101"): 1,
    ("edge-ai-starter", "E1M-AEN301", "E1M-V2N102"): 1,
    ("edge-ai-starter", "E1M-AEN401", "E1M-V2N101"): 1,
    ("edge-ai-starter", "E1M-AEN401", "E1M-V2N102"): 1,
    ("edge-ai-starter", "E1M-AEN501", "E1M-V2N101"): 1,
    ("edge-ai-starter", "E1M-AEN501", "E1M-V2N102"): 1,
    ("edge-ai-starter", "E1M-AEN601", "E1M-V2N101"): 1,
    ("edge-ai-starter", "E1M-AEN601", "E1M-V2N102"): 1,
    ("edge-ai-starter", "E1M-AEN701", "E1M-V2N101"): 1,
    ("edge-ai-starter", "E1M-AEN701", "E1M-V2N102"): 1,
    ("edge-ai-starter", "E1M-AEN801", "E1M-V2N101"): 1,
    ("edge-ai-starter", "E1M-AEN801", "E1M-V2N102"): 1,
    ("edge-ai-starter", "E1M-V2M101", "E1M-V2N101"): 1,
    ("edge-ai-starter", "E1M-V2M101", "E1M-V2N102"): 1,
    ("edge-ai-starter", "E1M-V2M102", "E1M-V2N101"): 1,
    ("edge-ai-starter", "E1M-V2M102", "E1M-V2N102"): 1,
    ("edge-ai-starter", "E1M-V2N101", "E1M-V2N102"): 1,
    ("edge-ai-starter", "E1M-V2N102", "E1M-V2N101"): 1,
    # Same rewrite, the other new mention: the AEN trees' `board.yaml`
    # comment still carries the OLD `deepx_v2m_note` DELIBERATE_EDITS text
    # ("Flip som.sku ... to E1M-V2M101"), which is why E1M-V2M101's count
    # below is unchanged at 2 (README + board.yaml) -- but the README's own
    # new SKU-neutral sentence adds a first-ever E1M-V2M102 mention (1, from
    # README.md alone) that the old "Flip ... to E1M-V2M101" sentence never
    # named.
    ("edge-ai-starter", "E1M-AEN301", "E1M-V2M102"): 1,
    ("edge-ai-starter", "E1M-AEN401", "E1M-V2M102"): 1,
    ("edge-ai-starter", "E1M-AEN501", "E1M-V2M102"): 1,
    ("edge-ai-starter", "E1M-AEN601", "E1M-V2M102"): 1,
    ("edge-ai-starter", "E1M-AEN701", "E1M-V2M102"): 1,
    ("edge-ai-starter", "E1M-AEN801", "E1M-V2M102"): 1,
    ("edge-ai-starter", "E1M-AEN301", "E1M-V2M101"): 2,
    ("edge-ai-starter", "E1M-AEN401", "E1M-V2M101"): 2,
    ("edge-ai-starter", "E1M-AEN501", "E1M-V2M101"): 2,
    ("edge-ai-starter", "E1M-AEN601", "E1M-V2M101"): 2,
    ("edge-ai-starter", "E1M-AEN701", "E1M-V2M101"): 2,
    ("edge-ai-starter", "E1M-AEN801", "E1M-V2M101"): 2,
    ("edge-ai-starter", "E1M-V2N101", "E1M-V2M101"): 1,
    ("edge-ai-starter", "E1M-V2N101", "E1M-V2M102"): 1,
    ("edge-ai-starter", "E1M-V2N102", "E1M-V2M101"): 1,
    ("edge-ai-starter", "E1M-V2N102", "E1M-V2M102"): 1,
    ("edge-ai-starter", "E1M-V2M101", "E1M-V2M102"): 1,
    ("edge-ai-starter", "E1M-V2M102", "E1M-V2M101"): 1,
}


def _foreign_sku_hits(sku: str, planned: dict[str, str]) -> dict[str, int]:
    """`{foreign_sku: occurrence count}` for every OTHER CATALOGUED SKU's
    exact token found anywhere in `planned`'s content -- word-bounded
    (`(?<![\\w-])...(?![\\w-])`) so `E1M-V2N101` can never accidentally
    match as a substring of a longer or shorter sibling SKU, and counts
    every occurrence rather than stopping at the first, so a partially
    substituted file (tan-cli#932's `README.md`: the SoM SKU line was fixed,
    the SoC identity lines were not) is reported precisely rather than
    merely flagged.

    Iterates `CATALOGUED_SKUS or _FAMILY_TREES` -- the same fallback
    `_skus_for` uses -- not the bare two-entry `_FAMILY_TREES` (tan-cli#946
    review round). `_FAMILY_TREES` alone made this guard blind to any SKU
    token outside `E1M-AEN801`/`E1M-V2N101` even after `CASES` itself was
    widened to the full catalogue: `_skus_for` iterates the wide list to pick
    which `(template_id, sku)` pairs to PLAN, but this function separately
    named the narrow list to pick which foreign tokens to LOOK for, so a
    9-SKU blind spot survived the widening that was supposed to close it.
    Measured: inserting `Note: on E1M-V2N102 the power rail answers
    differently.` into `diagnostics/E1M-V2N101/README.md` left this test at
    53 passed under the narrow list; the same mutation reds it once this
    list is the catalogue."""
    hits: dict[str, int] = {}
    for foreign in (CATALOGUED_SKUS or _FAMILY_TREES):
        if foreign == sku:
            continue
        pattern = re.compile(r"(?<![\w-])" + re.escape(foreign) + r"(?![\w-])")
        count = sum(len(pattern.findall(content)) for content in planned.values())
        if count:
            hits[foreign] = count
    return hits


@pytest.mark.parametrize("template_id,sku", CASES)
def test_no_planned_file_names_a_different_skus_exact_token(template_id, sku):
    """tan-cli#932 companion guard, the issue's own suggested fix: assert
    that no vendored `<template>/<sku>` tree contains a SKU TOKEN belonging
    to a DIFFERENT SKU -- word-bounded on the SKU string itself, nothing
    else. Before tan-cli#932's fix, `diagnostics/E1M-V2N101`'s `src/main.c`
    was never SKU-substituted at all (still `E1M-AEN801`, byte-identical to
    that SKU's own tree) and its `README.md` was substituted on the SoM SKU
    line only, leaving the sample serial and both `SoC identity:` lines
    unresolved -- six wrong bytes, declared as six `DELIBERATE_EDITS`
    entries (`MANIFEST.md`'s "Deliberate edits on top of the emit", entry
    8). A customer running the real self-test on real V2N101 hardware saw
    output contradicting what the scaffold told them to expect, and the
    natural reading of that mismatch is "my board failed", not "the README
    is wrong".

    **This guard is red on 2 of those six, not all six.** Only `src/main.c`'s
    two SKU-token lines (`(real hardware, E1M-AEN801):` and `SoM identity:
    E1M-AEN801 rev r1 ...`) NAME a sibling SKU's exact token, so only those
    two are this test's business -- mutation-measured: reverting `README.md`
    AND `src/main.c`'s SoC-identity/serial bytes to their pre-fix values
    while leaving the SKU-token lines fixed leaves this test GREEN (it has
    nothing to see: `alif:ensemble:e8` and `AEN0000123` name no SKU token at
    all); reverting only the two SKU-token lines reds it, naming
    `{'E1M-AEN801': 2}`. The other four entries -- the placeholder serial and
    both `SoC identity:` lines, in each file -- are caught only by
    `scaffold_byte_parity.py`'s `DELIBERATE_EDITS` `un_edit` half (each
    `un_edit_*` is `xfail(strict=True)`-strict: it MUST find its declared
    wrong bytes to undo, so reverting any of those four to a THIRD, un-
    declared value fails there, just not here). "A substitution bug anywhere
    in the plan path is caught too" would overstate this test's actual
    reach; it is not claimed.

    Checked against the PLANNER's real output (`plan_template_files`, the
    same source `_planned` above reads for the link-integrity tests), not
    the vendored tree directly -- so a SKU-TOKEN substitution bug anywhere in
    the plan path (`retarget_board_yaml_som`/`retarget_selftest_som_identity`
    included, not just a vendoring gap) is caught, and -- since tan-cli#932's
    review round widened `CASES` off the metadata catalogue (`_skus_for`
    above) -- a future SKU added to that catalogue is covered with no one
    having to remember to extend this test by hand."""
    planned = _planned(template_id, sku)
    hits = _foreign_sku_hits(sku, planned)
    drifted = {}
    for foreign, count in list(hits.items()):
        allowed = _ALLOWED_CROSS_SKU_MENTIONS.get((template_id, sku, foreign))
        if allowed is None:
            continue
        if allowed == count:
            del hits[foreign]
        else:
            # An allowlisted pair whose occurrence COUNT no longer matches --
            # an extra (or missing) mention on top of the declared one, not a
            # wholly new foreign token. Reported separately so the failure
            # reads as "the allowlisted count drifted", not "a new gap".
            drifted[foreign] = f"expected {allowed}, found {count}"
    assert not hits, (
        f"--template {template_id} --som {sku}: names a sibling SKU's exact "
        f"token {hits} -- either a real substitution gap (tan-cli#932), an "
        f"allowlisted pair whose occurrence count drifted ({drifted or 'none'}) "
        f"and needs its own review, or a legitimate cross-reference that needs "
        f"declaring (with its exact count) in _ALLOWED_CROSS_SKU_MENTIONS"
    )


#: Only `board-diagnostics` (the `diagnostics` tree) prints a `SoC identity:`
#: line at all -- filtered from `CASES` rather than a second `_cases()`-style
#: generator so a future SKU/template CASES gains is automatically covered
#: here too, with no second list to keep in sync.
DIAGNOSTICS_CASES = [param for param in CASES if param.values[0] == "board-diagnostics"]

#: The literal `SoC identity: <ref>` line the AEN tree's OWN representative
#: SKU (`_FAMILY_TREES[0]`, `E1M-AEN801`) carries -- imported from the
#: source module rather than re-typed here, so a future re-vendor that
#: changes the tree's captured SoC ref moves this test's expectation too
#: instead of leaving a stale hardcoded copy behind.
_AEN_FOREIGN_SOC_LINE = f"SoC identity: {_AEN_TREE_SOC_REF}"


@pytest.mark.parametrize("template_id,sku", DIAGNOSTICS_CASES)
def test_no_planned_soc_identity_line_asserts_a_different_soms_value(template_id, sku):
    """tan-cli#952: `retarget_selftest_som_identity` (tan-cli#946) corrected
    the `SoM identity:`/`Real hardware (...)` lines for every SKU sharing a
    vendored tree, but deliberately left the `SoC identity:` line alone --
    correct for the V2N/V2M family (one shared SoC) but wrong for AEN,
    whose SKUs are different Ensemble variants. Before this fix, `--som
    E1M-AEN301` scaffolded a README/`src/main.c` whose `SoM identity:` line
    correctly said `E1M-AEN301` sitting directly beside a `SoC identity:`
    line that still said `E1M-AEN801`'s own value -- a HALF-corrected
    identity block, which tan-cli#952 argues is MORE misleading than the
    fully-uncorrected block that preceded #946: nothing left in the block
    signals that the SoC line belongs to another SoM.

    `test_no_planned_file_names_a_different_skus_exact_token` (the existing
    cross-SKU guard) cannot see this defect at all: a SoC ref is not a SKU
    token, so it never appears in `CATALOGUED_SKUS` and the word-bounded
    SKU-token scan has nothing to match. This is a SEPARATE assertion for
    exactly that reason.

    Checks the PROPERTY (`retarget_selftest_soc_identity`'s fix is a
    neutralize, not a per-SKU retarget -- see that function's own docstring
    for why a ground-truth table was rejected: it would be a second,
    `test_no_new_hardware_facts.py`-violating copy of a fact
    `metadata/e1m_modules/<SKU>.yaml` already owns), not a rendered value:
    for the tree's OWN SKU the foreign-looking line must be exactly the
    real captured text (a regression there means an unrelated re-vendor or
    a scoping bug ate the tree's real content); for every OTHER SKU that
    line must be ABSENT and the disclosed placeholder must be PRESENT in
    its place -- "absent" alone would also pass a mutant that deleted the
    whole line outright, which is not this fix's shape.
    """
    planned = _planned(template_id, sku)
    combined = "\n".join(planned.values())
    tree = _family_bucket(sku)
    has_foreign_line = _AEN_FOREIGN_SOC_LINE in combined
    if tree != _FAMILY_TREES[0]:
        # Not the AEN tree at all (V2N/V2M) -- `_AEN_FOREIGN_SOC_LINE` never
        # appeared in this tree's content for ANY SKU sharing it, including
        # its own representative SKU (`E1M-V2N101`), so there is nothing this
        # test can check here beyond the trivial negative. The general
        # cross-SKU guard (`test_no_planned_file_names_a_different_skus_exact_
        # token`) already covers this tree's own defect class.
        assert not has_foreign_line, (
            f"--template {template_id} --som {sku}: unexpectedly contains the "
            f"AEN tree's {_AEN_FOREIGN_SOC_LINE!r} line -- this SKU renders "
            f"the {tree} tree, which should never contain AEN content at all"
        )
        return
    if sku == tree:
        # `E1M-AEN801`, the AEN tree's own representative SKU: its line is
        # real captured content and must stay untouched.
        assert has_foreign_line, (
            f"--template {template_id} --som {sku}: this SKU IS the vendored "
            f"AEN tree's own representative SKU, so its {_AEN_FOREIGN_SOC_LINE!r} "
            "line should be untouched -- it is missing (or was garbled) instead"
        )
        return
    assert not has_foreign_line, (
        f"--template {template_id} --som {sku}: names {tree}'s own "
        f"{_AEN_FOREIGN_SOC_LINE!r} value as if it were {sku}'s own "
        "(tan-cli#952: a half-corrected identity block -- the SoM line is "
        "right, the SoC line still asserts a different SoM's silicon)"
    )
    # Every other AEN sibling (`E1M-AEN301`..`E1M-AEN701`): the foreign line
    # must be NEUTRALIZED, not merely deleted.
    assert _SOC_IDENTITY_PLACEHOLDER in combined, (
        f"--template {template_id} --som {sku}: the foreign SoC-identity "
        "line was removed but the disclosed placeholder "
        f"({_SOC_IDENTITY_PLACEHOLDER!r}) is nowhere in the scaffolded "
        "output -- the line appears to have been deleted outright rather "
        "than neutralized"
    )


def test_a_documented_extra_conf_file_build_is_not_clobbered_by_the_generated_conf():
    """tan-cli#379's other half: naming the overlay is not enough, the build
    has to actually let it win.

    Zephyr merges `EXTRA_CONF_FILE` in list order, last assignment of a symbol
    wins. `iot`'s CMakeLists.txt APPENDED the generated `alp.conf`, so a
    caller's `-DEXTRA_CONF_FILE=native_sim.conf` landed BEFORE it (measured:
    `native_sim.conf;<build-dir>/generated/alp.conf`) and the emitted
    `CONFIG_MBEDTLS=y` overrode the `=n` the overlay exists to set. The
    documented native_sim build and `testcase.yaml`'s `extra_args` were both
    no-ops against the one symbol they were written for.

    Scoped to a template that DOCUMENTS an explicit `-DEXTRA_CONF_FILE=`
    build line (`_EXTRA_CONF` matching some planned file's content), not to
    "ships any .conf/.overlay" (tan-cli#501 review finding 1): a file that
    rides in via `boards/<board>.conf` -- Zephyr's own board-dir
    auto-discovery, e.g. `sensor`/`diagnostics`'s
    `boards/native_sim_native_64.conf` -- joins `CONF_FILE`, not
    `EXTRA_CONF_FILE`, and is unaffected by this ordering question no matter
    which way the CMakeLists' `EXTRA_CONF_FILE` list is built (MEASURED: a
    real CMake configure against Zephyr v4.4.1 produced the identical merge
    order and identical `.config` under both PREPEND and APPEND for that
    class of file). Only a caller-supplied `-DEXTRA_CONF_FILE=<file>` -- the
    `iot` scenario -- actually races the generated `alp.conf` for last-write.

    Pinned here because the fix is a hand-edit on top of a GENERATED tree
    (`vendored/MANIFEST.md`, "Deliberate edits on top of the emit") -- the next
    re-vendor re-emits the appending version, and the byte-parity gate will
    argue for it.
    """
    clobbered = []
    for param in CASES:
        template_id, sku = param.values
        planned = _planned(template_id, sku)
        cmake = planned.get("CMakeLists.txt", "")
        documents_extra_conf_file = any(
            _EXTRA_CONF.search(content)
            for name, content in planned.items()
            if name != "CMakeLists.txt"
        )
        if documents_extra_conf_file and "list(APPEND EXTRA_CONF_FILE" in cmake:
            clobbered.append(f"{template_id}/{sku}")
    assert not clobbered, (
        "generated conf APPENDED after a caller's own documented -DEXTRA_CONF_FILE=, so "
        f"that file cannot override it: {clobbered}"
    )


@pytest.mark.parametrize("template_id,sku", CASES)
def test_every_alp_sdk_link_pins_the_ref_the_tree_is_vendored_from(template_id, sku):
    """tan-cli#384: 40 links across seven READMEs pinned `v0.15.0`, which
    alp-sdk has never tagged -- the emit renders the link ref from the SDK's
    VERSION, and the tree was vendored from the PRE-RELEASE `v0.15.0-rc1`. A
    scaffolded README's "Further reading" section was 40/40 dead.

    Pinning at the vendor ref rather than a floating `main` is deliberate: the
    scaffold's prose describes the API at the commit it was captured from, and
    `main` moves out from under it. That preference is the EMIT's to express,
    not this gate's -- alp-sdk#1535 makes `_docs_ref()` fall back to `main`
    when the declared `v<version>` tag has not been cut, and since
    tan-cli#846's pin bump this tree is vendored in exactly that state. So the
    ref checked here is whatever `MANIFEST.md` records, tag or `main`; what
    this gate enforces is that all 40 links agree with it, which is the half
    that catches a half-finished re-vendor.
    """
    expected = _vendor_ref()
    wrong = sorted(
        {
            f"{name} -> {ref}"
            for name, content in _planned(template_id, sku).items()
            for ref in _SDK_LINK_REF.findall(content)
            if ref != expected
        }
    )
    assert not wrong, (
        f"--template {template_id} --som {sku}: alp-sdk link(s) pinned at a ref this "
        f"tree is not vendored from ({expected}, per vendored/MANIFEST.md): {wrong}"
    )


def test_the_vendored_ref_is_one_alp_sdk_actually_has():
    """tan-cli#384's acceptance criterion 2, and the half consistency cannot
    reach: the ref every shipped link is pinned at has to EXIST.

    The test above only proves the links and `MANIFEST.md` agree. They agreed
    on `v0.15.0` too -- an emit-rendered tag alp-sdk never cut -- and 40 links
    404ed anyway. Only GitHub can answer this one, so this is the one network
    check in the suite; it SKIPS visibly when GitHub cannot be reached, naming
    why, never a silent pass.

    The second assertion is a permanent negative control, not decoration --
    `_DEAD_CONTROL_REF` (see its own comment for why it is a hardcoded,
    plausible-prefix ref rather than one re-derived from the live vendor ref
    on every run). It is precisely the shape GitHub's PLURAL ref endpoint
    confuses: `/git/refs/tags/v0.15` prefix-matches `v0.15.0`/`v0.15.0-rc1` and
    answers 200. If `_tag_exists` is ever "simplified" onto that endpoint, this
    control turns True and the run reds -- which is the only way to keep
    proving the gate can still fail for a ref that does not exist.
    """
    ref = _vendor_ref()
    exists = _ref_exists(ref)
    if exists is None:
        pytest.skip(
            f"GitHub could not be asked whether alp-sdk has ref {ref!r} (offline, "
            f"rate-limited, or 5xx). This is a SKIP about reachability, not a pass: "
            f"the shipped templates' 40 version-pinned links were NOT verified."
        )

    assert _DEAD_CONTROL_REF != ref, (
        f"the hardcoded dead-control ref {_DEAD_CONTROL_REF!r} now equals the live "
        f"vendor ref -- a control must never test itself; pick a different dead ref "
        f"(see _DEAD_CONTROL_REF's comment) before this assertion means anything"
    )
    control_exists = _tag_exists(_DEAD_CONTROL_REF)
    assert control_exists is not True, (
        f"negative control failed: {_DEAD_CONTROL_REF!r} reports as an existing "
        f"alp-sdk tag. Either _tag_exists is querying the PREFIX-matching plural "
        f"endpoint (/git/refs/tags/, which answers 200 for {_DEAD_CONTROL_REF!r} by "
        f"prefix-matching a real tag) and this gate can no longer detect a dead ref "
        f"at all, or alp-sdk really did cut a tag literally named "
        f"{_DEAD_CONTROL_REF!r} -- in which case pick a fresh dead-but-plausible-"
        f"prefix ref (see _DEAD_CONTROL_REF's comment) and re-point this control."
    )

    assert exists, (
        f"vendored/MANIFEST.md pins this tree at {ref!r}, and every version-pinned "
        f"link in the shipped template READMEs points there, but alp-sdk has no such "
        f"ref -- so those links 404 in every scaffolded project. Re-pin the links AND "
        f"the manifest at a ref that exists (an rc tag, the final tag once cut, or "
        f"`{_MAIN_REF}`, which is what alp-sdk#1535's emit degrades to when the "
        f"declared tag is not cut)."
    )
