# SPDX-License-Identifier: Apache-2.0
"""Whether a SoM's hw_rev -- explicit, or (absent that) its DEFAULT -- is
buildable, read directly off the alp-sdk metadata tree, without importing
`tan.planner` (see `tan/core/example_catalog.py`'s own docstring for why:
`tan.planner.paths` binds `REPO = sdk_root()` at import time, so pulling it
into `init_cmd` before an SDK is even resolved raises `PlannerRootError`).

tan-cli#743. `tan init --template minimal-app --som E1M-NX9101` exits 0 and
scaffolds a project whose FIRST `tan validate` hard-errors::

    sdk-compat: SoM E1M-NX9101 hw_rev 'r1' exists but is not buildable
    (status: 'tbd').

`init` never writes an explicit `hw_rev:` into a scaffolded `minimal-app`/
vendored-template board.yaml, so the value `validate` refuses there is the
one the SDK loader falls back to -- `hw_rev or
som_preset.get("default_hw_rev")` (`scripts/alp_orchestrate/loader.py:1241`
and siblings) -- i.e. the SoM preset's OWN `default_hw_rev:`. `validate` is
not wrong here: the fact is real and correctly published
(`metadata/e1m_modules/E1M-NX9101.yaml`'s `status: {preliminary: true,
partial_hw_config: true}`, and its family's
`metadata/e1m_modules/imx93/hw-revisions.yaml`'s `r1: {status: tbd}`). The
defect is that `init` stays silent about a fact it could have read from the
exact same preset it already resolved -- so this module reads that one fact
(the SKU's effective hw_rev + the family's declared status for it) so `init`
can warn about it instead.

tan-cli#1008 review majors 1+2: an `--from-example`/`--topology` copy can
carry an EXPLICIT `hw_rev:` of its own (`tan/core/scaffold.vendored_som`
reads it off the planned board.yaml) -- the original example's own revision,
or one that survived an INTRA-family retarget (`retarget_board_yaml_som`
only drops the sibling `hw_rev:` on a CROSS-family retarget -- review round
4 -- since within one family the value is still a real, declared revision
for the new SKU too). `hw_rev_not_buildable`'s caller passes that explicit
value through when there is one, so the pair this module judges (and the
warning names) is always the one actually written to disk, never a value
inferred from `--som`/`DEFAULT_SOM_SKU` alone.

WHY A WARNING AND NOT A REFUSAL: `minimal-app` is deliberately the one
template every OTHER template's own refusal message names as the escape
hatch -- "Use --template minimal-app, which is vendor-neutral and scaffolds
any SoM" (`tan/core/scaffold.py`'s vendored-scaffold refusal text). If this
check also refused, a SoM in `status: preliminary` would have no self-contained
scaffolding path at all -- worse than the silent defect it replaces. This
mirrors `example_catalog.unsupported_som`'s own precedent (warn, still
write) for the identical reason: refusing there "made `tan init
--from-example` unusable for nearly the whole AEN family, which is worse
than the original defect".

Deliberately duplicates the tiny SKU -> family-directory map
`scripts/alp_project_loader._sku_family` also carries (`AEN`/`V2N`/`V2M`/
`NX9` -> `aen`/`v2n`/`v2n-m1`/`imx93`) and the buildable-status set
`scripts/alp_orchestrate/sdk_compat._NOT_BUILDABLE_STATUSES` (`reserved`,
`tbd`, or a missing `status:` key) rather than importing either: neither
module is on `sys.path` for a `tan init --template` invocation, which reads
`--sdk-root` only as a metadata SOURCE, never as an importable package.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

#: Mirrors `scripts/alp_project_loader._SKU_FAMILY` / `_sku_family` verbatim.
_SKU_FAMILY = re.compile(r"^E1M-(AEN|V2N|V2M|NX9)")
_FAMILY_DIR = {"AEN": "aen", "V2N": "v2n", "V2M": "v2n-m1", "NX9": "imx93"}

#: Mirrors `scripts/alp_orchestrate/sdk_compat._NOT_BUILDABLE_STATUSES`
#: verbatim (alp-sdk #1025): `reserved`, `tbd`, and a missing `status:` key
#: are all refused; every other declared status is buildable.
_NOT_BUILDABLE_STATUSES = frozenset({"reserved", "tbd"})


class HwRevNotBuildable:
    """The hw_rev a scaffolded board.yaml resolves to (its own explicit
    `hw_rev:`, or -- absent that -- the SoM preset's `default_hw_rev:`), and
    why `tan validate` will refuse it.

    `has_buildable_alternative` -- tan-cli#1008 review minor -- is whether
    the SAME family table declares at least one OTHER hw_rev whose status
    permits a build: the "or until board.yaml names a buildable `hw_rev:`
    explicitly" remedy is only real advice when this is true. imx93 today
    publishes exactly one hw_rev for E1M-NX9101 (`r1`, `status: tbd`), so
    that clause was unconditionally offered on the one SKU it currently
    fires for, and following it (adding `hw_rev: r1` -- the only revision
    the family HAS) reproduces the identical refusal, rc 2, every time."""

    __slots__ = ("sku", "hw_rev", "status", "has_buildable_alternative")

    def __init__(
        self, sku: str, hw_rev: str, status: Optional[str], has_buildable_alternative: bool
    ) -> None:
        self.sku = sku
        self.hw_rev = hw_rev
        self.status = status
        self.has_buildable_alternative = has_buildable_alternative


def _safe_load_mapping(path: Path) -> Optional[dict]:
    """`yaml.safe_load(path)`, or None for every reason a caller here must
    treat as "nothing to check" rather than an error: absent, unreadable,
    unparseable, or parsed to something other than a mapping.

    Imports PyYAML lazily, inside the call, not at module scope: this module
    is reached from `init_cmd`, which `tan/cli.py` static-imports on EVERY
    invocation, and `tests/gates/test_cli_import_is_lean.py` (tan-cli#810)
    pins PyYAML as loaded only by `new_som_cmd`/`kconfig_cmd`'s single-use
    paths -- a module-scope `import yaml` here would load it on `tan
    --version` too.
    """
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else None


def hw_rev_not_buildable(
    sdk_root: Path, sku: str, hw_rev: Optional[str] = None
) -> Optional[HwRevNotBuildable]:
    """Whether `hw_rev` -- or, when `None`/empty, `sku`'s own
    `default_hw_rev:` -- is buildable per its family's `hw-revisions.yaml`.
    Returns None for "nothing to report" -- covering every reason a caller
    here must NOT warn over, deliberately mirroring
    `example_catalog.unsupported_som`'s own contract:

      * `sku` doesn't match the known SoM SKU pattern at all,
      * no SoM preset file for `sku` in this checkout,
      * `hw_rev` is `None`/empty and the preset declares no (or a malformed)
        `default_hw_rev:` to fall back to,
      * no family `hw-revisions.yaml` table (an in-development family with
        nothing to check against yet),
      * the family table doesn't declare `hw_rev` as a key at all --
        existence is `revision_known()`'s question
        (`tan/planner/sdk_compat.py`), not this one's; an unknown revision
        has no status to have read, so there is nothing here to judge,
      * the declared status permits a build.

    Never raises: a scaffold must not fail because a metadata file could
    not be read.
    """
    match = _SKU_FAMILY.match(sku)
    family_dir = _FAMILY_DIR.get(match.group(1)) if match else None
    if family_dir is None:
        return None

    preset = _safe_load_mapping(sdk_root / "metadata" / "e1m_modules" / f"{sku}.yaml")
    if preset is None:
        return None
    if not hw_rev:
        hw_rev = preset.get("default_hw_rev")
    if not isinstance(hw_rev, str) or not hw_rev:
        return None

    table = _safe_load_mapping(
        sdk_root / "metadata" / "e1m_modules" / family_dir / "hw-revisions.yaml"
    )
    if table is None:
        return None
    revisions = table.get("hw_revisions")
    if not isinstance(revisions, dict):
        return None
    entry = revisions.get(hw_rev)
    if not isinstance(entry, dict):
        # Mirrors `alp_orchestrate/sdk_compat.revision_buildable` verbatim: a
        # key present but not a dict (None, a bare string, ...) is a
        # malformed entry, not an absent one -- it has no status, so it is
        # not buildable. Only a truly absent key stays "nothing to judge"
        # (unknown, `revision_known()`'s question).
        if hw_rev not in revisions:
            return None
        return HwRevNotBuildable(sku, hw_rev, None, _has_other_buildable(revisions, hw_rev))

    status = entry.get("status")
    if isinstance(status, str) and status not in _NOT_BUILDABLE_STATUSES:
        return None
    return HwRevNotBuildable(
        sku, hw_rev, status if isinstance(status, str) else None,
        _has_other_buildable(revisions, hw_rev),
    )


def _has_other_buildable(revisions: dict, hw_rev: str) -> bool:
    """Whether `revisions` (the family's `hw_revisions:` map) declares any
    key OTHER than `hw_rev` whose `status:` permits a build -- the fact that
    decides whether "or name a buildable `hw_rev:` explicitly" is real
    advice or a dead end (tan-cli#1008 review minor)."""
    return any(
        other_rev != hw_rev
        and isinstance(other_entry, dict)
        and isinstance(other_entry.get("status"), str)
        and other_entry.get("status") not in _NOT_BUILDABLE_STATUSES
        for other_rev, other_entry in revisions.items()
    )
