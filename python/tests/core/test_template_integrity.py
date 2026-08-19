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
from pathlib import Path

import pytest

from tan.net import default_ssl_context
from tan.core.scaffold import (
    IOT_STARTER_SUPPORTED_SKU,
    TEMPLATE_IDS,
    VENDORED_ROOT,
    _FAMILY_TREES,
    plan_template_files,
)

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
#: point -- see `_tag_exists`.
_TAG_API = "https://api.github.com/repos/alplabai/alp-sdk/git/ref/tags/{ref}"


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

    commit_match = _MANIFEST_COMMIT.search(manifest)
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
    request = urllib.request.Request(
        _TAG_API.format(ref=ref), headers={"Accept": "application/vnd.github+json"}
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


def _skus_for(template_id: str) -> tuple[str, ...]:
    """Every SKU whose tree this template can be asked to plan. `iot-starter`
    vendors ONE family (`tan.core.scaffold`'s own comment: any other `--som`
    is refused before planning), so asking it for the V2N SKU would silently
    re-check the Alif tree a second time and report a SKU it never supports in
    the failure message."""
    return (IOT_STARTER_SUPPORTED_SKU,) if template_id == "iot-starter" else _FAMILY_TREES


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
    `main` moves out from under it.
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


def test_the_vendored_ref_is_a_tag_alp_sdk_actually_has():
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
    exists = _tag_exists(ref)
    if exists is None:
        pytest.skip(
            f"GitHub could not be asked whether alp-sdk has tag {ref!r} (offline, "
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
        f"tag -- so those links 404 in every scaffolded project. Re-pin the links AND "
        f"the manifest at a ref that exists (an rc tag, or the final tag once cut)."
    )
