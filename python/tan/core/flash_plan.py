# SPDX-License-Identifier: Apache-2.0
"""Pure planning for ``tan flash`` -- the decision + argv-building half.

Port of ``crates/tan-core/src/flash/`` (``mod.rs`` / ``args.rs`` /
``builders.rs`` / ``registry.rs`` / ``storage.rs``) plus the manifest reader in
``crates/tan-core/src/system_manifest.rs``. Every string, argv, filter and
per-backend command shape lives here with NO IO; the subprocess / filesystem /
temp-file half is ``tan.commands.flash_cmd``.

The flow mirrors ``alp_flash.dispatch`` + ``_flash_entry``: walk the manifest's
``boot_order`` (or the sorted slice ``core_id``s when empty), map each step to
its slice, append the helper MCUs after, then dispatch each entry's
``flash_method`` to a backend plan-builder.

**Strict ``flash_args`` reading.** A whole ``flash_args`` that is not a mapping
(the AEN701 helper's ``flash_args: TBD`` string) reads as an empty map -- but a
sub-key that IS present is read STRICTLY: every behaviour-affecting bool/int
(``erase``, ``use_openocd``, ``reset``, ``base``, ``baud``, ...) goes through a
``_checked`` accessor that hard-errors on a wrong-type scalar rather than
silently defaulting, since a wrong flash is worse than a refused one. Do not
reintroduce a tolerant bool/int reader here.

**No hardware facts (I-26 / ADR-0017).** Nothing in this module names a SKU, an
address, a pin, an I2C address, a probe serial or a vendor branch. Every such
value arrives in ``flash_args``, passed through from alp-sdk ``metadata/``.
tan-cli#732 removed the module's last two exceptions along with the
``swd_probe`` backend they existed for -- ``_DEFAULT_JLINK_DEVICE`` (a part
number) and ``_DEFAULT_BASE`` (a flash base address), both inherited verbatim
from the Rust oracle and reachable only from ``plan_swd_probe`` when a
manifest omitted the corresponding ``flash_args`` key. Neither default has any
other caller. If a future backend needs one, restate the count here rather
than adding it silently -- a docstring that undercounts its own debt is how
the next one gets added without argument.
"""
from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from typing import Any, Callable

from tan.core.pending import PENDING_PLACEHOLDER as PENDING_SENTINEL, is_pending_placeholder

#: The system-manifest schema major this command consumes. A different value is
#: REFUSED rather than read as if it were v1 -- mirrors
#: `system_manifest.rs::SYSTEM_MANIFEST_SCHEMA_VERSION`.
SYSTEM_MANIFEST_SCHEMA_VERSION = 1

_DEFAULT_JLINK_SPEED = 4000
_JLINK_BINARIES = ("JLinkExe", "JLink")


class ManifestError(Exception):
    """`build/system-manifest.yaml` could not be consumed. `message` is the
    human text; the caller pairs it with `flash.manifest-invalid`."""


class FlashPlanError(Exception):
    """A backend refused to build a plan -- the `Err(String)` arm of every
    `plan_*` builder in `builders.rs`/`storage.rs`. The message is reported
    verbatim as the entry's `message`."""


# ── manifest reading ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Slice:
    """One per-core image from the manifest's `slices[]`. Tolerant reader: only
    the fields `tan flash` consumes are modeled and unknown additive-v1 keys are
    ignored, per the stability policy in `system_manifest.rs`."""

    core_id: str
    os: str
    status: str = ""
    output_artefact: str | None = None
    flash_method: str | None = None
    flash_args: Any = None


@dataclass(frozen=True)
class HelperMcu:
    """One on-module helper MCU from `helper_mcus[]`."""

    name: str
    firmware_path: str | None = None
    flash_method: str | None = None
    flash_args: Any = None
    update_channel: str | None = None
    #: tan-cli#611. WHO may invoke `flash_method`, and WHEN -- the fact neither
    #: `update_channel` (how a device is updated in the FIELD) nor
    #: `flash_method` (by what transport) carries. See [`FLASH_POLICY_FACTORY`]
    #: for the whole argument; `None` means the SoM preset declared nothing and
    #: every gate below behaves exactly as it did before this field existed.
    flash_policy: str | None = None


@dataclass(frozen=True)
class Manifest:
    sku: str = ""
    slices: tuple[Slice, ...] = ()
    helper_mcus: tuple[HelperMcu, ...] = ()
    boot_order: tuple[Any, ...] = ()


def _opt_str(raw: Any) -> str | None:
    """A manifest string field, or `None`. A non-string scalar reads as absent
    rather than being coerced: `serde` would have failed the whole document, and
    `str(4)` here would silently invent a path/method name."""
    return raw if isinstance(raw, str) else None


def parse_system_manifest(text: str) -> Manifest:
    """Parse + version-guard a `system-manifest.yaml` document.

    Raises `ManifestError` for: PyYAML unavailable, malformed YAML, a non-mapping
    document, a `schema_version` that is not 1, or a `slices[]`/`helper_mcus[]`
    entry missing a field the Rust struct declares non-`Option` (`core_id`/`os`
    for a slice, `name`/`chip` for a helper) -- serde fails the ENTIRE parse in
    that last case, so a partial read here would flash against a manifest the
    oracle rejects.

    tan ships no YAML dependency of its own (`python/pyproject.toml`), so PyYAML
    is imported lazily. Its absence is FATAL here, unlike in `debug-config`
    where the manifest is a best-effort enrichment: `flash` cannot pick a target
    or an artefact without it, and silently flashing nothing would be the worse
    outcome.
    """
    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError as err:
        raise ManifestError(
            "reading a system-manifest needs PyYAML, which is not importable "
            f"({err}); install it (`pip install pyyaml`) or run tan from a "
            "bootstrapped workspace"
        ) from err
    try:
        doc = yaml.safe_load(text)
    except Exception as err:  # noqa: BLE001 -- the SDK's output, not ours
        raise ManifestError(f"system-manifest is not valid YAML: {err}") from err
    if doc is None or not isinstance(doc, dict):
        raise ManifestError(
            "system-manifest is not valid YAML: expected a mapping at the "
            f"document root, got {type(doc).__name__}"
        )
    version = doc.get("schema_version")
    if version != SYSTEM_MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"unsupported system-manifest schema_version {version} (this CLI "
            f"consumes v{SYSTEM_MANIFEST_SCHEMA_VERSION}); upgrade the CLI or "
            "the SDK so the versions match"
        )

    hw_info = doc.get("hw_info")
    sku = ""
    if isinstance(hw_info, dict) and isinstance(hw_info.get("sku"), str):
        sku = hw_info["sku"]

    slices: list[Slice] = []
    for raw in _seq(doc.get("slices")):
        if not isinstance(raw, dict):
            raise ManifestError("system-manifest is not valid YAML: slices[] entry is not a mapping")
        core_id, os_name = raw.get("core_id"), raw.get("os")
        if not isinstance(core_id, str) or not isinstance(os_name, str):
            raise ManifestError(
                "system-manifest is not valid YAML: every slices[] entry needs a "
                "string `core_id` and `os`"
            )
        slices.append(
            Slice(
                core_id=core_id,
                os=os_name,
                status=raw["status"] if isinstance(raw.get("status"), str) else "",
                output_artefact=_opt_str(raw.get("output_artefact")),
                flash_method=_opt_str(raw.get("flash_method")),
                flash_args=raw.get("flash_args"),
            )
        )

    helpers: list[HelperMcu] = []
    for raw in _seq(doc.get("helper_mcus")):
        if not isinstance(raw, dict):
            raise ManifestError(
                "system-manifest is not valid YAML: helper_mcus[] entry is not a mapping"
            )
        name, chip = raw.get("name"), raw.get("chip")
        if not isinstance(name, str) or not isinstance(chip, str):
            raise ManifestError(
                "system-manifest is not valid YAML: every helper_mcus[] entry needs "
                "a string `name` and `chip`"
            )
        helpers.append(
            HelperMcu(
                name=name,
                firmware_path=_opt_str(raw.get("firmware_path")),
                flash_method=_opt_str(raw.get("flash_method")),
                flash_args=raw.get("flash_args"),
                update_channel=_opt_str(raw.get("update_channel")),
                flash_policy=_opt_str(raw.get("flash_policy")),
            )
        )

    return Manifest(
        sku=sku,
        slices=tuple(slices),
        helper_mcus=tuple(helpers),
        boot_order=tuple(_seq(doc.get("boot_order"))),
    )


def _seq(raw: Any) -> list[Any]:
    """A manifest list field. `#[serde(default)]` means a missing key is an
    empty list; a key present with a NON-list value is a shape error serde would
    reject, so it is not silently treated as empty here either -- `[]` is
    returned only for genuinely absent/null."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError(
            f"system-manifest is not valid YAML: expected a sequence, got {type(raw).__name__}"
        )
    return raw


# ── target selection ────────────────────────────────────────────────────────

SLICE = "slice"
HELPER = "helper"


@dataclass(frozen=True)
class FlashTarget:
    """One manifest entry selected for flashing, in dispatch order."""

    kind: str
    id: str
    flash_method: str | None
    flash_args: Any
    output_artefact: str | None = None
    firmware_path: str | None = None
    update_channel: str | None = None
    #: tan-cli#611 -- carried from `HelperMcu.flash_policy`; always `None` for a
    #: slice, which is an application core and a genuine customer flash target.
    flash_policy: str | None = None


@dataclass(frozen=True)
class TargetPlan:
    targets: tuple[FlashTarget, ...]
    warnings: tuple[str, ...]
    refused: tuple[str, ...]
    #: The subset of "status not ok" refusals that are explained away rather
    #: than a real build problem: a slice `status: "skipped"` -- i.e. `tan
    #: build` itself declined to build this slice under `executionPolicy.
    #: missingTool`/`.nullCommand` (a host with no `bitbake`, say) -- or a
    #: slice declared `os: "off"` in `board.yaml` (tan-cli#699), which `tan
    #: build`'s `iter_buildable_slices` never touches at all, so its manifest
    #: entry never advances off whatever status the plan-time emit left it at
    #: (`pending`, today). Both were decisions already made before `tan
    #: flash` ever ran; refusing to flash a never-built
    #: artefact is still correct (there is nothing to flash), but it must not
    #: ALSO read as a flash failure for a slice the manifest already
    #: explained away. `refused` (a `"failed"`/`"pending"`/other status on a
    #: slice that is NOT `os: "off"`) is the opposite: `tan build` tried and
    #: the slice is broken or was never reconciled, which must keep failing
    #: `tan flash`. Callers surface this bucket as a WARNING and must not
    #: fold it into a failure count -- see `refused` for the error-severity,
    #: exit-code-affecting bucket.
    #:
    #: **DIVERGED from the Rust oracle, before the oracle was retired.**
    #: `crates/tan-core/src/flash/mod.rs`'s `plan_flash_targets` had no
    #: `refused_skipped` bucket at all -- a `status: skipped` slice/helper
    #: landed in the ONE `refused` list alongside `failed`/`pending`/anything
    #: else non-`ok`, and the CLI seeded `failed` from `refused.len()` before
    #: the dispatch loop even ran, so the oracle FAILED the run on a
    #: `status: skipped` slice exactly like any other bad status -- and had no
    #: `os: "off"` carve-out either, so it also failed on that shape. This
    #: split (and the caller's
    #: warning-only, exit-0 treatment when something else DID flash) was a
    #: deliberate product improvement on top of the port, not a porting bug --
    #: but the caller (`tan.commands.flash_cmd.flash`) MUST still fail the run
    #: when every match was a `refused_skipped` entry and nothing flashed
    #: (`flash.nothing-flashed`), or this bucket reintroduces the exact
    #: silent-success class `refused` exists to prevent, just inverted.
    #: `crates/` and `tests/parity/test_flash_oracle_parity.py` (which
    #: deliberately carried no `status: skipped` nor `os: "off"` case, for the
    #: reasons above) were both deleted in 2883cdf -- there is no longer a
    #: second implementation to diff against.
    refused_skipped: tuple[str, ...] = ()



def plan_flash_targets(
    manifest: Manifest, core: str | None = None, helper: str | None = None
) -> TargetPlan:
    """Build the ordered flash target list + any `boot_order` warnings/refusals.

    - Empty `boot_order`: one step per slice `core_id`, sorted ascending.
    - Non-empty `boot_order`: walked in order; a step naming a `core_id` not in
      `slices` is dropped and surfaced as a warning.
    - A slice whose `status` is not `ok` is REFUSED, not flashed and not silently
      dropped: `overlay_run_results` PRESERVES the plan-time `output_artefact`
      when a later run has no artefact for that core, so a run-1 success followed
      by a run-2 failure/skip leaves run-1's elf on disk under a manifest
      reporting a broken slice. Flashing that stale elf and silently dropping the
      slice are the same silent-failure class. A `status: skipped` refusal, or
      one for a slice declared `os: "off"` in `board.yaml` (tan-cli#699), is
      split into `refused_skipped` rather than `refused`: `tan build` already
      decided (via `executionPolicy`, or by design for an `off` core) that
      this slice was never going to build -- e.g. no `bitbake` on an MCU-only
      checkout, or a core that is deliberately off -- and that is not a flash
      failure, it is `tan flash` agreeing with a decision already made.
      `os: "off"` is checked on the slice's `os` field, not its
      `status`: an `off` core's manifest entry never advances off whatever
      status the plan-time emit left it at, so it must be recognised
      regardless of that value. A genuinely broken or unbuilt slice
      (`status: failed`, `status: pending` on an `os` that is NOT `"off"`, or
      any other non-`ok`/non-`skipped` value) stays in `refused`.
    - Helpers always come AFTER all slices.
    - `core` flashes only that slice and skips every helper; `helper` skips every
      slice and flashes only that helper.

    Callers MUST surface both `refused` and `refused_skipped`: those entries
    never enter `targets`, so a caller that only reports `targets`/`warnings`
    would show a clean run while a stale/never-built artefact stayed unflashed.
    Only `refused` (not `refused_skipped`) may fail the overall run -- see
    `TargetPlan.refused_skipped`.
    """
    targets: list[FlashTarget] = []
    warnings: list[str] = []
    refused: list[str] = []
    refused_skipped: list[str] = []

    def find_slice(cid: str) -> Slice | None:
        # Non-empty core_id only, matching the Python dict-comprehension guard
        # `alp_flash` used and the `!s.core_id.is_empty()` filter in Rust.
        for s in manifest.slices:
            if s.core_id and s.core_id == cid:
                return s
        return None

    if not manifest.boot_order:
        steps = sorted(s.core_id for s in manifest.slices if s.core_id)
    else:
        steps = []
        for step in manifest.boot_order:
            if not isinstance(step, dict):
                continue
            named = step.get("core")
            if isinstance(named, str) and named:
                steps.append(named)

    # A slice present in `slices` but never named by a `boot_order` step used to
    # be dropped with NO warning at all -- a heterogeneous system silently
    # flashed a strict subset of its cores and reported success. Only warn on the
    # unfiltered default run: `--core` deliberately narrows the slice set and
    # `--helper` deliberately suppresses every slice.
    if manifest.boot_order and helper is None and core is None:
        for s in manifest.slices:
            if s.core_id and s.core_id not in steps:
                warnings.append(f"flash: slice '{s.core_id}' has no boot_order entry; not flashed")

    if helper is None:
        for cid in steps:
            if core is not None and cid != core:
                continue
            found = find_slice(cid)
            if found is None:
                warnings.append(
                    f"flash: boot_order references core '{cid}' not in slices; skipping"
                )
                continue
            if not slice_should_flash(found.status):
                # `found.os == "off"` is checked BEFORE `found.status ==
                # "skipped"` below on purpose: an off core never receives a
                # `SliceRunResult` (see the comment inside this branch), so
                # its `status` can only ever be the plan-time `pending`
                # default -- never `failed` -- making this ordering safe
                # today. If that ever changed and an off core's manifest
                # entry could carry `status: failed`, checking `os` first
                # would silently downgrade a real failure to a warning; the
                # `os` check would need to move after the `status` checks
                # (or gain its own `status == "failed"` guard) at that point.
                if found.os == "off":
                    # tan-cli#699: `board.yaml` declares this core `os: "off"`
                    # -- `tan build`'s `iter_buildable_slices` (`tan/planner/
                    # orchestrator.py:36`) correctly skips it, so it never
                    # receives a `SliceRunResult` and the manifest's plan-time
                    # `status: pending` default is never overlaid to anything
                    # else. That is NOT a policy skip (`executionPolicy` never
                    # ran for this core) and NOT an incomplete/failed build --
                    # it is the manifest correctly recording a core that is
                    # declared off, so `tan build` never builds it. (The
                    # manifest can still name an `app:`/`board:`/`toolchain:`
                    # for an off core -- `board.yaml` may declare those
                    # fields per-core independent of `os:` -- so this must
                    # not be read as "no app, no board".)
                    # Checked on `found.os`, not `found.status`: unlike the
                    # `skipped` branch below, this must catch the slice
                    # regardless of which never-advanced status the emitter
                    # happens to have left behind.
                    refused_skipped.append(
                        f"flash: slice '{found.core_id}' is declared `os: \"off\"` "
                        "in board.yaml -- tan build never builds a core "
                        "declared off, by design. There is nothing to flash; "
                        "this is expected, not an error."
                    )
                elif found.status == "skipped":
                    # A policy decision `tan build` already made and reported
                    # (`executionPolicy.missingTool`/`.nullCommand`), not a
                    # broken build -- "stale, rebuild it" is wrong on both
                    # counts: nothing was ever built, so nothing is stale, and
                    # rebuilding ON THIS HOST hits the same policy skip again.
                    refused_skipped.append(
                        f"flash: slice '{found.core_id}' build status is 'skipped' -- "
                        "tan build already declined to build it under executionPolicy "
                        "(a missing tool or a null command on this host); there is "
                        "nothing to flash. Rebuilding on this same host will skip it "
                        "again -- it needs a host where that tool resolves."
                    )
                else:
                    refused.append(
                        f"flash: slice '{found.core_id}' build status is "
                        f"'{found.status}' (not 'ok'); refusing to flash its artefact "
                        "-- it may be stale from a previous successful build. "
                        "Rebuild it first."
                    )
                continue
            targets.append(
                FlashTarget(
                    kind=SLICE,
                    id=found.core_id,
                    flash_method=found.flash_method,
                    flash_args=found.flash_args,
                    output_artefact=found.output_artefact,
                )
            )

    if core is None:
        for h in manifest.helper_mcus:
            if not h.name:
                continue
            if helper is not None and h.name != helper:
                continue
            targets.append(
                FlashTarget(
                    kind=HELPER,
                    id=h.name,
                    flash_method=h.flash_method,
                    flash_args=h.flash_args,
                    firmware_path=h.firmware_path,
                    update_channel=h.update_channel,
                    flash_policy=h.flash_policy,
                )
            )


    return TargetPlan(
        tuple(targets), tuple(warnings), tuple(refused), tuple(refused_skipped)
    )


# ── who may flash a helper, and when (tan-cli#611) ──────────────────────────

#: A genuine customer flash target. Explicit rather than merely-absent so a SoM
#: preset can SAY so; absent is treated the same way (see [`helper_flash_gate`]),
#: because every preset written before this field existed means exactly that.
FLASH_POLICY_CUSTOMER = "customer"

#: Programmed by Alp Lab in production. Never a customer flash target, and there
#: is no recovery path declared for it.
#:
#: This is the fact tan-cli#611 found nothing carried. `update_channel` answers
#: how a device is updated in the FIELD; `flash_method` answers by what
#: TRANSPORT it is written. Neither answers WHO may write it, so `flash` was
#: inferring the first from the second: it honoured `update_channel` only for a
#: helper that declared no `flash_method` at all, which mirrored an alp-sdk
#: schema rule making the two mutually exclusive. That XOR is the modelling
#: defect -- a helper can legitimately have BOTH an OTA channel for the normal
#: case and a flash method for the abnormal one -- and this field is what
#: replaces the inference.
FLASH_POLICY_FACTORY = "factory"

#: Programmed by Alp Lab in production, and customer-flashable ONLY to recover a
#: bricked device, with Alp Lab-supplied binaries.
#:
#: An unconditional skip would be WRONG for this value: it would remove the one
#: path that matters at the one moment it matters. So the gate declines it on an
#: ordinary run and keeps it reachable through a DELIBERATE action -- see
#: [`helper_flash_gate`]'s `recovery_armed`/`helper_filter` pair, which together
#: mean a recovery write cannot be reached by a bare `tan flash`.
FLASH_POLICY_RECOVERY_ONLY = "recovery_only"

#: Every policy this CLI understands, in the order the diagnostics list them.
FLASH_POLICIES = (
    FLASH_POLICY_CUSTOMER,
    FLASH_POLICY_FACTORY,
    FLASH_POLICY_RECOVERY_ONLY,
)

#: How a policy-declined helper is named in the human transcript.
_PRODUCTION_PROGRAMMED = "is programmed by Alp Lab in production"


def _channel_clause(channel: str) -> str:
    """The `update_channel` sentence, or nothing.

    "Mention the update channel only where one exists" is the point: the old
    message asserted an OTA mechanism as the REASON a helper was skipped, which
    is only ever true where a channel is declared -- and is not the reason even
    then. The reason is who programs it; the channel is a separate fact about
    its life afterwards."""
    return f" Field updates arrive over update_channel: {channel}." if channel else ""


def _under_declared_skip_message(kind: str, entry_id: str, method: str, channel: str) -> str:
    """A helper carrying BOTH halves and no `flash_policy`.

    Legal only once the upstream XOR is relaxed, and under-declared when it
    happens: the preset author gave the entry both an update channel and a flash
    method, and said nothing about who may write it. Declining is fail-safe AND
    visible -- silently flashing it would be the tan-cli#611 defect one field
    over."""
    return (
        f"flash: {kind} '{entry_id}' declares both flash_method '{method}' and "
        f"update_channel '{channel}' but no flash_policy, so nothing says who may "
        f"flash it; skipping. Add flash_policy ({' / '.join(FLASH_POLICIES)}) to "
        "the helper_firmware entry in the SoM preset."
    )


def _recovery_only_skip_message(
    kind: str, entry_id: str, channel: str, *, recovery_armed: bool
) -> str:
    """The decline for a `recovery_only` helper this run may not write.

    The tail names the EXACT re-run rather than the flag alone, and distinguishes
    the two ways to be here: `--recover` not given at all, versus given on a run
    that was never narrowed to this entry. An operator whose device is bricked
    has no second channel to work anything out from."""
    if recovery_armed:
        tail = (
            " --recover was given but this run is not narrowed to it; a recovery "
            "flash must name its single target. Re-run with "
            f"`--helper {entry_id} --recover`."
        )
    else:
        tail = (
            " To recover a bricked device deliberately, re-run with "
            f"`--helper {entry_id} --recover`."
        )
    return (
        f"flash: {kind} '{entry_id}' {_PRODUCTION_PROGRAMMED} and is "
        "customer-flashable only to recover a bricked device, with Alp "
        f"Lab-supplied binaries; skipping.{_channel_clause(channel)}{tail}"
    )


def helper_flash_gate(
    target: FlashTarget,
    *,
    recovery_armed: bool = False,
    helper_filter: str | None = None,
) -> str | None:
    """The `flash_policy` decision for one target: a skip message, or `None` to
    let dispatch continue. Pure -- no IO, no `flash_args`, no PATH.

    Called ABOVE `_flash_entry`'s `if not raw_method:` guard, which is the whole
    of tan-cli#611's tan-side half: the old code consulted the only non-customer
    declaration there was (`update_channel`) exclusively INSIDE that guard, so a
    helper declaring both it and a `flash_method` had the declaration silently
    dropped and was flashed like any other target.

    `recovery_armed` (`--recover`) and `helper_filter` (`--helper NAME`) are BOTH
    required to reach a `recovery_only` write: the flag alone would let a
    whole-manifest run sweep a recovery helper in. An UNRECOGNISED policy skips
    -- a restriction this build does not understand must not become permission."""
    policy = (target.flash_policy or "").strip()
    channel = (target.update_channel or "").strip()
    method = (target.flash_method or "").strip()
    kind, entry_id = target.kind, target.id

    if not policy:
        if method and channel:
            return _under_declared_skip_message(kind, entry_id, method, channel)
        # Every shape that predates this field, unchanged -- including the
        # `update_channel`-without-`flash_method` skip, still worded by
        # `_flash_entry` exactly as before.
        return None
    if policy == FLASH_POLICY_CUSTOMER:
        return None
    if policy == FLASH_POLICY_FACTORY:
        return (
            f"flash: {kind} '{entry_id}' {_PRODUCTION_PROGRAMMED}, not a customer "
            f"flash target; skipping.{_channel_clause(channel)}"
        )
    if policy == FLASH_POLICY_RECOVERY_ONLY:
        if recovery_armed and helper_filter == entry_id:
            return None
        return _recovery_only_skip_message(
            kind, entry_id, channel, recovery_armed=recovery_armed
        )
    return (
        f"flash: {kind} '{entry_id}' declares flash_policy '{policy}', which this "
        f"tan does not recognise (known: {', '.join(FLASH_POLICIES)}); refusing to "
        "treat an unrecognised restriction as permission; skipping. Upgrade tan, "
        "or correct the SoM preset."
    )


def slice_should_flash(status: str) -> bool:
    """A slice is flashed iff it built successfully. `image_bundle.rs::
    slice_should_bundle` -- the same one-line predicate, shared on purpose so
    `flash` and `image` can never disagree about which artefacts are real."""
    return status == "ok"


# ── path helpers ────────────────────────────────────────────────────────────


def is_rust_absolute(path: str) -> bool:
    """`Path::is_absolute()` semantics, NOT `os.path.isabs`.

    On Windows Rust requires BOTH a prefix (drive/UNC) and a root, so a
    rooted-but-driveless `/dev/sdb` or `\\x` is RELATIVE and `base.join(p)`
    discards part of `base`. `os.path.isabs("/dev/sdb")` answered True on
    Windows until Python 3.13 and False from 3.13 on -- so reaching for it would
    make artefact resolution differ from the oracle AND differ between two
    supported interpreters on the same host.
    """
    if os.name == "nt":
        drive, rest = os.path.splitdrive(path)
        return bool(drive) and rest[:1] in ("\\", "/")
    return path.startswith("/")


def resolve_artefact_path(
    artefact: str,
    build_root: str,
    sdk_root: str | None,
    is_file: Callable[[str], bool],
) -> str:
    """Resolve a manifest artefact string to a path. Absolute strings pass
    through; a relative string tries `build_root/artefact`, then
    `sdk_root/artefact`, then west's NESTED `build_root/build/artefact`, and
    falls back to the `build_root` candidate. `is_file` is injected to keep this
    pure.

    The first two candidates and the fallback are `flash/mod.rs::
    resolve_artefact_path` verbatim. The third is the consumer half of **I-18**:
    the planner emits `west build` with NO `-d`, so west's tree lands at
    `<buildDir>/build/`. Before tan-cli#560 (alp-sdk d00dbdc1) the plan's
    `artifacts` block still reported the un-nested `<buildDir>/zephyr/
    zephyr.elf`; as of that pin the planner's own in-process output already
    carries the nested path, but an older cached plan, the stale-SDK alp-sdk
    subprocess fallback, or a hand-authored manifest can still arrive
    un-nested -- this candidate still catches those. Rust reconciles the
    nested case at manifest-WRITE time (`build/execute/manifest.rs::
    resolve_zephyr_artefact`, tan's only writer of `output_artefact`, which
    stores the nested ABSOLUTE path); this port's `build` does not write the
    manifest yet, so an un-nested artefact string would resolve to nothing
    without this candidate. Probed LAST, only when the oracle's own
    candidates all miss a real file, so it never changes a resolution the
    oracle already makes -- an absolute artefact never reaches it at all.
    """
    if is_rust_absolute(artefact):
        return artefact
    cand_build = os.path.join(build_root, artefact)
    if sdk_root is None:
        return cand_build
    if is_file(cand_build):
        return cand_build
    cand_sdk = os.path.join(sdk_root, artefact)
    if is_file(cand_sdk):
        return cand_sdk
    cand_nested = os.path.join(build_root, "build", artefact)
    if is_file(cand_nested):
        return cand_nested
    return cand_build


# ── flash_args accessors ────────────────────────────────────────────────────


def _fa_get(value: Any, key: str) -> Any:
    """A `flash_args` sub-key, or `None` when `flash_args` is not a mapping.
    Mirrors `args.rs::fa_get`'s `v.as_mapping()?`: the AEN701 helper's
    `flash_args: TBD` string reads as an empty map, not an error."""
    if not isinstance(value, dict):
        return None
    return value.get(key)


def _fa_has_key(value: Any, key: str) -> bool:
    """Whether `flash_args` is a mapping that carries `key` AT ALL --
    independent of what it resolves to. `_fa_get`/`fa_str_checked` collapse a
    present-but-null value and a genuinely-absent key to the same `None`,
    which is right for every OPTIONAL field but wrong for one that must
    distinguish "not selected" from "selected with a malformed value" (see
    `slot0_load_address` in `plan_alif_mram_jlink`, and `expect_dpidr` /
    `jlink_device` in `flow_d_preflight_script`)."""
    return isinstance(value, dict) and key in value


def _yaml_debug(value: Any) -> str:
    """`serde_yaml::Value`'s `{:?}` rendering, so the strict accessors' refusal
    messages match the oracle byte for byte (`String("true")`, `Number(1)`,
    `Bool(true)`, `Sequence [Number(1), Number(2)]`). Verified against the
    shipped binary; the messages ship to the customer and to the extension's
    issue list, and a diff harness that has to special-case them stops being
    able to prove anything about the rest of the envelope."""
    if value is None:
        return "Null"
    if isinstance(value, bool):
        return f"Bool({'true' if value else 'false'})"
    if isinstance(value, str):
        return f'String("{value}")'
    if isinstance(value, (int, float)):
        return f"Number({value})"
    if isinstance(value, list):
        return "Sequence [" + ", ".join(_yaml_debug(v) for v in value) + "]"
    if isinstance(value, dict):
        body = ", ".join(f"{_yaml_debug(k)}: {_yaml_debug(v)}" for k, v in value.items())
        return "Mapping {" + body + "}"
    return f"String(\"{value}\")"


def fa_str(value: Any, key: str) -> str | None:
    """A non-empty string sub-key; `None` when absent, empty, or non-string."""
    raw = _fa_get(value, key)
    if isinstance(raw, str) and raw:
        return raw
    return None


def fa_bool_checked(value: Any, key: str) -> bool | None:
    """Strict bool accessor for every behaviour-affecting `flash_args` bool
    (`reset`, `erase`, `use_openocd`, `use_pyocd`, `confirm`, ...).

    A quoted `"false"` is NOT a bool, and a tolerant reader would read it as
    absent, apply the caller's default and program the OPPOSITE of what was
    written. `None` only for genuinely absent/null; any other shape raises."""
    raw = _fa_get(value, key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    raise FlashPlanError(
        f"flash_args.{key} must be a bare boolean (true/false, unquoted; got "
        f"{_yaml_debug(raw)}) -- refusing to silently fall back to a default -- "
        "this plans a real flash write."
    )


def fa_int_checked(value: Any, key: str) -> int | None:
    """Strict int accessor (`jlink_speed`, `baud`, `jobs`, `speed`).

    `0`-means-absent semantics are preserved from the oracle: an explicit `0`
    yields `None`, i.e. "use the default". `bool` is checked BEFORE `int` --
    Python's `True` IS an `int`, so an unguarded `isinstance(raw, int)` would
    accept `jobs: true` and emit `-j 1`."""
    raw = _fa_get(value, key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise FlashPlanError(_int_refusal(key, raw))
    if isinstance(raw, int):
        return raw if raw != 0 else None
    raise FlashPlanError(_int_refusal(key, raw))


def _int_refusal(key: str, raw: Any) -> str:
    return (
        f"flash_args.{key} must be a bare number (unquoted; got {_yaml_debug(raw)}) "
        "-- refusing to silently fall back to a default -- this plans a real "
        "flash write."
    )


def fa_str_checked(value: Any, key: str, as_hex_address: bool) -> str | None:
    """Strict string accessor for fields where falling back to a baked-in default
    is dangerous -- a flash base address, an OpenOCD interface/target name that
    gets interpolated into a spawned command.

    `fa_str` treats ANY non-string value -- including the bare YAML integer an
    unquoted `base: 0x08000000` resolves to -- as "absent", so the caller
    silently substitutes the default and programs real silicon at the wrong
    address with no warning. This returns `None` only for genuinely
    absent/null/empty, round-trips a bare non-negative number back into a string
    (hex for an address field, decimal otherwise), and refuses every other shape.

    A NEGATIVE number is refused outright rather than formatted: Rust's
    `n as u64` sign-extends `-8` into `0xFFFFFFFFFFFFFFF8`, which
    `validate_address` (a pure charset check) then ACCEPTS as a plausible
    address and the J-Link/OpenOCD command interpolates verbatim.
    """
    raw = _fa_get(value, key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        # Guarded before the int arm for the same reason as `fa_int_checked`:
        # `True` is an `int`, and `base: true` must not resolve to `0x00000001`.
        raise FlashPlanError(_str_refusal(key, raw))
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, int):
        if raw < 0:
            raise FlashPlanError(
                f"flash_args.{key} = {raw} is negative; refusing to interpret it as "
                "an address/count -- this plans a real flash write."
            )
        return f"0x{raw:08X}" if as_hex_address else str(raw)
    raise FlashPlanError(_str_refusal(key, raw))


def _str_refusal(key: str, raw: Any) -> str:
    return (
        f"flash_args.{key} must be a quoted string (got {_yaml_debug(raw)}); "
        "refusing to silently fall back to a default -- this plans a real flash write."
    )


def is_pending(value: Any) -> bool:
    """Whether a manifest SCALAR is the SDK's unfilled-field sentinel.

    **The one definition for the whole flash path (#222).** Every guard in this
    area used to test for EMPTY, and empty is the one thing a `TBD` placeholder
    is not -- so an unfilled field behaved exactly like a filled one, and
    whether that ended in a loud refusal or a spawned flasher came down to
    whether the particular consumer happened to validate against a closed set.
    `flash_method: TBD` hit the backend registry and failed safely;
    `output_artefact`/`firmware_path: TBD` hit nothing at all, resolved to
    `<build_root>/TBD` and reached a real J-Link write. Route every new
    manifest-derived field through THIS, never through a fresh `== "TBD"`.

    Trimmed before comparing -- a YAML `device: "  TBD  "` is the same unfilled
    field -- but deliberately NOT case-folded and NOT a substring test:
    `TBD-1234-XYZ` is a plausible part number and `flash_args.build_dir:
    /opt/TBDtool/x` a plausible path, and refusing either would block a
    legitimate flash. `tbd` lowercase is not the sentinel alp-sdk emits;
    widening to it means widening the SDK's convention first, in one place,
    not here.

    The comparison is the single `tan.core.pending.is_pending_placeholder`
    definition (#276): the neutral module with no flash- or image-bundle
    machinery behind it, so `tan.core.size` (and `pinmux`, once ported) can
    read the same rule without pulling flash internals in. `PENDING_SENTINEL`
    stays the name this module exports -- `flash_cmd` and the flash tests
    already spell it that way -- but it is now an alias for
    `pending.PENDING_PLACEHOLDER`, not a second definition. `tan image`'s own
    `image_bundle.PENDING_SENTINEL` is still a separate `"TBD"` literal;
    pointing it at the same module too is a follow-up outside flash_plan.py.
    """
    return is_pending_placeholder(value)


def flash_args_has_tbd(value: Any) -> bool:
    """Whether `flash_args` carries an unresolved `TBD` ANYWHERE -- a bare `TBD`
    scalar, or a mapping/sequence value that trims to `TBD`.

    Deliberately broader than a single-key check: a `TBD` anywhere means the
    entry is not finalised yet under the SDK's pending-placeholder convention.
    Do not narrow this back to a set of known keys. Recurses into mapping VALUES
    and sequence elements, not mapping keys: every accessor here reads by a
    known key name, so a key literally named `TBD` selects nothing and cannot
    reach an argv.

    This covers `flash_args` ONLY. The sibling artefact fields
    (`output_artefact`/`firmware_path`) are NOT part of `flash_args` and are
    guarded separately at the point of use -- see `is_pending`.
    """
    if isinstance(value, str):
        return is_pending(value)
    if isinstance(value, dict):
        return any(flash_args_has_tbd(v) for v in value.values())
    if isinstance(value, list):
        return any(flash_args_has_tbd(v) for v in value)
    return False


# ── validators ──────────────────────────────────────────────────────────────


def validate_identifier(
    text: str, field_name: str, *, destination: str = "a spawned command / OpenOCD Tcl script"
) -> None:
    """Reject anything that is not a plain identifier, or a `/`-separated path
    of plain identifier segments.

    `interface`/`target` are interpolated verbatim into an OpenOCD
    `-f <name>.cfg` path and a `-c` Tcl command string, so an unrestricted value
    is a path-traversal + Tcl-injection primitive into a process routinely run
    with device-flashing privileges. Multi-segment is allowed because OpenOCD
    ships interface configs in subdirectories (`ftdi/olimex-arm-usb-ocd-h`).

    Rust composes `path_guard::is_plain_relative` with a per-segment charset
    check. The charset alone is EQUIVALENT here and is what is implemented: the
    only shapes `is_plain_relative` adds are absolute/rooted/drive-prefixed and
    `.`/`..`, and every one of those carries a character (`/` leading -> an empty
    segment, `:`, `\\`, `.`) the charset already rejects. Cross-checked against
    the oracle on `a;b`, `../x`, `/x`, `\\x`, `C:/x`, `a//b`, `.`.

    `destination` lets a caller whose value never goes near OpenOCD say so.
    The default names OpenOCD because `interface`/`target` -- the callers
    that shaped this message originally -- legitimately reach its `-c` Tcl
    string; `jlink_serial` (tan-cli#486 review) does not, and reusing the
    default gave a captured refusal envelope the sentence "refusing to
    interpolate it into a spawned command / OpenOCD Tcl script" for a value
    that only ever reaches a J-Link Commander `SelectEmuBySN` line.
    """
    segments = text.split("/")
    ok = bool(text) and all(
        seg and all(c.isascii() and (c.isalnum() or c in "-_") for c in seg) for seg in segments
    )
    if not ok:
        raise FlashPlanError(
            f"flash_args.{field_name} = {_quoted(text)} is not a plain identifier or "
            "'/'-separated path of plain identifiers (letters, digits, '-', '_' per "
            f"segment) -- refusing to interpolate it into {destination}."
        )


def validate_address(text: str, field_name: str) -> None:
    """A flash base address must be purely hex digits, with an optional `0x`/`0X`.

    `base` is interpolated verbatim into a J-Link Commander script LINE and an
    OpenOCD `-c` Tcl command string -- both line/command-oriented interpreters,
    so a newline (or `;`, `[`, `]`) inside `base` runs arbitrary extra commands
    against whatever silicon is attached.
    """
    digits = text
    for prefix in ("0x", "0X"):
        if digits.startswith(prefix):
            digits = digits[len(prefix) :]
            break
    if not digits or not all(c in "0123456789abcdefABCDEF" for c in digits):
        raise FlashPlanError(
            f"flash_args.{field_name} = {_quoted(text)} is not a plain hex/decimal "
            "address -- refusing to interpolate it into a J-Link/OpenOCD command."
        )


def validate_commander_path(text: str, label: str) -> None:
    """Reject a control character (`\\x00`-`\\x1f`, or DEL `\\x7f`) or a literal
    `"` in a value bound for a J-Link Commander script LINE -- `loadbin`/
    `loadfile`/`verifybin`'s path argument, or `SelectEmuBySN`'s serial
    (tan-cli#486).

    Deliberately narrower than `validate_identifier`: this guards a real
    filesystem path (`atoc`, the flash artefact), which legitimately carries
    spaces, `:`, `\\`, and drive letters -- rejecting those would turn a real
    Windows-style path into a refusal. What must never reach the script is a
    literal newline/CR: `commander_path`'s conditional quoting only stops
    SEGGER's whitespace tokeniser from splitting a spaced path into two
    tokens, it does nothing to stop an embedded newline from ending the
    quoted string's own Commander LINE and starting a new, attacker-chosen
    one -- quoting controls tokenisation within a line, not where lines end.
    A tab/formfeed/vertical-tab is technically a control character too but is
    already forced through the quoting path by `commander_path`'s `c.isspace()`
    check; rejecting the narrower "line-ending" set here (which still catches
    every one of them, since `isspace()` control chars are also `< 0x20`)
    keeps this guard doing ONE job instead of duplicating that quoting
    decision.

    `"` is rejected too (tan-cli#486 review): a value carrying BOTH a `"` and
    a space defeats `commander_path`'s own conditional quoting from the
    inside -- `/b/a" halt "z.bin` renders `loadbin "/b/a" halt "z.bin", 0x8000`,
    where the embedded quote closes the wrapper early and `halt` reads back
    as a bare token mid-line, exactly what this function's own claim to
    "control tokenisation within a line" promises will not happen. `"` is a
    reserved character in a Windows filename and vanishingly rare on POSIX,
    so rejecting it costs a real path nothing.
    """
    if any(ord(c) < 0x20 or ord(c) == 0x7F or c == '"' for c in text):
        raise FlashPlanError(
            f"{label} = {_quoted(text)} contains a control character or a "
            'literal \'"\' -- refusing to interpolate it into a J-Link '
            "Commander script line, where either would let it end the "
            "current line/quoted token and start a new, unintended one "
            "regardless of quoting."
        )


#: `char::escape_debug`'s named escapes, which is what Rust's `{:?}` for a
#: `&str` emits. Applied in ONE pass -- escaping `\\` up front and then
#: re-scanning would revisit the backslashes it just added.
_DEBUG_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\t": "\\t",
    "\r": "\\r",
    "\n": "\\n",
}


def _quoted(text: str) -> str:
    """Rust's `{s:?}` for a `&str`.

    Not just `"` and `\\`: Rust escapes control characters too, so a `base`
    containing a real newline renders as `"0x8000\\n r"` -- ONE line -- and not
    as a refusal message split across two. These messages are exactly the ones
    reporting an injection attempt (`validate_address`/`validate_identifier`
    exist to catch a newline smuggled into a J-Link Commander script line), so a
    diagnostic that itself breaks across lines is the worst possible rendering:
    a reader sees a truncated message and the offending bytes on their own line.
    Caught by the oracle diff, not by review.
    """
    rendered = [
        _DEBUG_ESCAPES.get(char)
        or (char if char.isprintable() else f"\\u{{{ord(char):x}}}")
        for char in text
    ]
    return '"' + "".join(rendered) + '"'


def is_raw_bin(artefact: str) -> bool:
    """Whether an artefact is a raw binary (needs an explicit load address), as
    opposed to ELF/HEX which carry their own. Passing a load offset for a
    non-`.bin` artefact shifts every section by that offset and writes outside
    the intended flash region."""
    return os.path.splitext(artefact)[1].lower() == ".bin"


# ── the plan + backend registry ─────────────────────────────────────────────


@dataclass(frozen=True)
class FlashPlan:
    """A built flash plan: the argv, the success message, whether it is
    planning-only (never spawns real device IO), and -- for the J-Link path --
    the Commander script the caller must materialise to a temp file."""

    argv: tuple[str, ...]
    ok_message: str
    planning_only: bool = False
    jlink_script: str | None = None
    #: tan-cli#520: a backend's own already-resolved WRITE device, carried
    #: through so the caller's read-only DPIDR preflight can reuse it as its
    #: own connect device rather than re-reading a second `flash_args` key
    #: with a different meaning. `None` for every backend/arm that has no such
    #: device to offer -- today that is every registered backend; the field
    #: exists for a future J-Link-arm backend that needs it (`swd_probe`, the
    #: original setter, was removed by tan-cli#732).
    preflight_device: str | None = None


@dataclass(frozen=True)
class BackendMeta:
    """A registered backend: the tool-gate `requires` list + its plan-builder."""

    requires: tuple[str, ...]
    build: Callable[["FlashInputs", Callable[[str], bool]], FlashPlan]


@dataclass(frozen=True)
class FlashInputs:
    """Everything a backend plan-builder consumes. Injected by the CLI layer."""

    artefact: str
    flash_args: Any
    core_id: str
    sku: str
    dry_run: bool = False
    #: The env half of the confirm gate (`ALP_FLASH_FORCE=1`). The per-entry
    #: `flash_args.confirm` is OR-ed in by the gated builders, so the effective
    #: gate is `flash_args.confirm OR ALP_FLASH_FORCE=1`.
    force_confirm: bool = False


#: The one place the confirm gate's remedy is written (tan-cli#719). Three
#: sites used to compose their own "not run" note and only ONE of them named
#: `ALP_FLASH_FORCE=1` -- the mechanism that actually works -- so the other two
#: pointed the reader at a manifest key and stayed silent about the env var.
#: Most-specific-first, matching the SETOOLS resolution message's shape.
CONFIRM_REMEDY = (
    "to actually flash, most-specific first: `--confirm` on the command line, "
    "`ALP_FLASH_FORCE=1` in the environment, or `flash_args.confirm: true` in "
    "the manifest"
)


def confirm_gate_note(why: str) -> str:
    """The parenthetical every confirm-gated preview appends.

    `why` is `dry-run` (an explicit preview, nothing to remedy) or the
    confirm-gate reason, which carries `CONFIRM_REMEDY`.
    """
    if why == "dry-run":
        return why
    return f"{why} -- {CONFIRM_REMEDY}"


def backend_for(method: str) -> BackendMeta | None:
    """Resolve a `flash_method` string to its backend metadata, or `None`."""
    return _REGISTRY.get(method)


def registry_keys() -> list[str]:
    """The registered method names, sorted -- for the "Available: ..." error."""
    return sorted(_REGISTRY)


def registry_keys_debug() -> str:
    """`{:?}` of a `Vec<&str>`, for the unknown-method message."""
    return _str_list_debug(registry_keys())


def _str_list_debug(items) -> str:
    return "[" + ", ".join(_quoted(i) for i in items) + "]"


# ── J-Link Commander script quoting (shared by Flow D) ──────────────────────


def commander_path(path: str) -> str:
    """A path as it should be interpolated into a J-Link Commander script
    line -- quoted when it CONTAINS whitespace, unchanged otherwise
    (tan-cli#369). SEGGER's Commander splits an unquoted line on whitespace,
    so an unquoted `loadbin C:\\Program Files\\alif\\setools\\build\\
    AppTocPackage.bin, <address>` silently truncates to `C:\\Program` --
    `-ExitOnError 1` turns that into a loud SEGGER parse error rather than a
    mis-write, which is the only reason it was not a blocker. tan generates
    every Commander script now, so tan owns making its own filenames parse
    back correctly. Quoting is CONDITIONAL, not unconditional, so the
    overwhelmingly common no-space path -- every already-measured
    oracle/bench script -- renders byte-identical to before this fix.
    """
    return f'"{path}"' if any(c.isspace() for c in path) else path


def _default(value, fallback):
    """`Option::unwrap_or`. Spelled out because `value or fallback` is WRONG for
    every falsy-but-present value this module reads -- `reset: false`,
    `jlink_speed` legitimately absent-as-0, `interface: ""`."""
    return fallback if value is None else value


# ── zephyr_west_flash / baremetal_cmake_flash ───────────────────────────────


def zephyr_build_dir(artefact: str) -> str:
    """The Zephyr build dir derived from the artefact: `parent.parent` when the
    artefact sits directly in a `zephyr/` subdirectory, else `parent`.

    Checks the PARENT DIRECTORY NAME, never the artefact's basename: an
    MCUboot-signed (`zephyr.signed.hex`) or sysbuild (`merged.hex`) output still
    lands in `<build_dir>/zephyr/` under a different name, and a basename
    allowlist sent those one directory too deep -- `west flash --build-dir
    <that>` then failed with no CMakeCache.txt there.

    `os.path.dirname`, not `Path.parent`: it slices the string and preserves
    whatever separators the joined path already mixes (a native `build_root` +
    a `/`-authored manifest artefact), exactly as Rust's `Path::parent` does.
    """
    parent = os.path.dirname(artefact)
    if os.path.basename(parent).lower() == "zephyr":
        return os.path.dirname(parent)
    return parent


def plan_zephyr_west_flash(inp: FlashInputs, which: Callable[[str], bool]) -> FlashPlan:
    """`zephyr_west_flash`: `west flash --build-dir <d> [--runner <r>] [--erase]
    [--hex-file <h>]`.

    `runner` is OPTIONAL -- when absent, `--runner` is omitted and `west flash`
    falls back to the board.cmake default runner (on an AEN board that is
    `alif_flash`, i.e. Flow A over the SE-UART).
    """
    del which  # this backend probes nothing
    fa = inp.flash_args
    runner = fa_str(fa, "runner")
    build_dir = _default(fa_str(fa, "build_dir"), zephyr_build_dir(inp.artefact))
    argv = ["west", "flash", "--build-dir", build_dir]
    if runner is not None:
        argv += ["--runner", runner]
    if _default(fa_bool_checked(fa, "erase"), False):
        argv.append("--erase")
    hex_file = fa_str(fa, "hex_file")
    if hex_file is not None:
        argv += ["--hex-file", hex_file]
    return FlashPlan(
        argv=tuple(argv),
        ok_message=(
            f"zephyr_west_flash[{inp.core_id}]: programmed via "
            f"{runner if runner is not None else 'board-default runner'}"
        ),
    )


def plan_baremetal_cmake_flash(inp: FlashInputs, which: Callable[[str], bool]) -> FlashPlan:
    """`baremetal_cmake_flash`: `cmake --build <d> --target <t> [--config <c>] [-j N]`."""
    del which
    fa = inp.flash_args
    build_dir = _default(fa_str(fa, "build_dir"), os.path.dirname(inp.artefact))
    target = _default(fa_str(fa, "target"), "flash")
    argv = ["cmake", "--build", build_dir, "--target", target]
    config = fa_str(fa, "config")
    if config is not None:
        argv += ["--config", config]
    jobs = fa_int_checked(fa, "jobs")
    if jobs is not None:
        argv += ["-j", str(jobs)]
    return FlashPlan(
        argv=tuple(argv),
        ok_message=f"baremetal_cmake_flash[{inp.core_id}]: target `{target}` ok",
    )


# ── storage backends ────────────────────────────────────────────────────────

PIPE = "|"

#: The directory a `yocto_wic` flash target must resolve beneath (tan-cli#487,
#: porting alp-sdk's `_DEV_ROOT` -- security fix 3aa65cd7 / #1112, the half of
#: that commit tan-cli#486's sibling-hardening pass dropped). A module
#: constant, not a literal in the check, so a test can exercise the real
#: resolution logic without touching a host's actual `/dev`.
_DEV_ROOT = "/dev"

#: The two registry keys `plan_yocto_wic` answers for -- exported so
#: `tan.commands.flash_cmd` can scope its write-time block-device gate
#: (tan-cli#487, see that module's `_yocto_wic_block_device_refusal`) to
#: exactly this backend without re-typing the strings.
YOCTO_WIC_METHODS = ("yocto_wic_to_sd_or_emmc", "yocto_wic")

#: Suffixes `plan_yocto_wic` RECOGNISES as a real compression codec it
#: cannot decompress (tan-cli#487, defect 2, shape 2) -- named verbatim from
#: the issue's own examples. Deliberately NOT "every suffix that isn't gz/
#: xz/wic": an unrecognised suffix might be a customer's own naming
#: convention for a genuinely uncompressed artefact, and refusing THAT would
#: trade a silent raw-dd for a false-positive refusal on a legitimate plain
#: `.wic`.
_KNOWN_UNSUPPORTED_COMPRESSION_SUFFIXES = ("zst", "bz2", "lzo")


def _resolve_dev_root(target: str) -> str | None:
    """LEXICAL canonicalization of `target`: `posixpath.normpath` on a
    POSIX-normalized copy of the string -- collapses a `..` traversal
    (`/dev/../home/u/x`) on every host, including Windows, where
    `pathlib`/`os.path` would instead reinterpret the string as an NT path.
    Pure -- no filesystem access -- so safe to run unconditionally, including
    for a `--dry-run` preview of a target that is not plugged in yet.

    Returns the normalized path when it resolves at or beneath `_DEV_ROOT`,
    else `None`. The caller keeps using the ORIGINAL `target` string for the
    refusal message and for the argv actually spawned -- never this return
    value -- so a target that is itself a symlink (`/dev/by-id/mmc-foo`)
    still reaches `dd`/`bmaptool` under the name the caller asked for.

    Ported from alp-sdk's `_resolve_dev_root` (`scripts/flash_backends/
    yocto_wic.py`) MINUS its real-filesystem symlink-chase layer: this
    module is pure by convention (see its own docstring's "NO IO"), so that
    half lives in `tan.commands.flash_cmd`'s IO side instead, as a SEPARATE,
    write-time-only block-device `stat` gate
    (`_yocto_wic_block_device_refusal`) -- see that function's docstring for
    why, and for the one shape this lexical-only layer alone cannot catch (a
    real, existing regular file lexically living under a real `/dev/`
    subtree, e.g. `/dev/shm/<name>`)."""
    normalized = posixpath.normpath(target.replace(os.sep, "/") if os.sep != "/" else target)
    root = _DEV_ROOT.rstrip("/")
    # Strictly BENEATH the root, not equal to it -- `_DEV_ROOT` itself is not
    # a device (matches the old bare `startswith("/dev/")` check, which also
    # rejected the bare string `"/dev"`: it has no trailing slash to match).
    if normalized.startswith(root + "/"):
        return normalized
    return None


def plan_yocto_wic(inp: FlashInputs, which: Callable[[str], bool]) -> FlashPlan:
    """`yocto_wic_to_sd_or_emmc` / `yocto_wic`: bmaptool (preferred) or dd to a
    raw `/dev/` block device. Compressed images pipe `gunzip`/`xz` into `dd`.
    Planning-only unless the confirm gate is armed."""
    fa = inp.flash_args
    target = fa_str(fa, "target")
    if target is None:
        raise FlashPlanError("yocto_wic: flash_args.target is required (e.g. /dev/sdb)")
    if _resolve_dev_root(target) is None:
        raise FlashPlanError(
            f"yocto_wic: refusing target '{target}' -- must start with /dev/ to avoid "
            "clobbering a regular file. Set flash_args.target to a real block device."
        )
    artefact = inp.artefact
    compress = fa_str(fa, "compress")
    suffix = os.path.splitext(artefact)[1].lstrip(".").lower()
    confirm = inp.force_confirm or _default(fa_bool_checked(fa, "confirm"), False)
    planning_only = inp.dry_run or not confirm

    bmaptool = which("bmaptool")
    dd = which("dd")
    if bmaptool or (planning_only and not dd):
        # tan-cli#487 review finding 2: `compress` is a `dd`-fallback-only
        # concern -- bmaptool decompresses natively and never reads it -- so
        # the vocabulary refusals below must not fire here. Before this fix
        # they ran unconditionally, ahead of tool selection, so a stock Yocto
        # `IMAGE_FSTYPES` artefact like `core-image.wic.zst` hard-refused a
        # `bmaptool copy core-image.wic.zst /dev/sdb` even on a bmap-tools
        # host -- a live regression on the arm this module's own docstring
        # calls "preferred".
        argv: tuple[str, ...] = ("bmaptool", "copy", artefact, target)
    elif dd:
        # `compress` is validated HERE, not before tool selection -- see the
        # bmaptool arm above. The suffix-refusal message below does NOT
        # suggest `flash_args.compress` as a fix for an UNSUPPORTED codec:
        # that used to send the operator straight into the compress-value
        # refusal a few lines down (the exact same codec, now explicit,
        # is still not "gz"|"xz") -- a dead-end loop tan-cli#487's review
        # caught.
        if compress is None:
            if suffix in ("gz", "xz"):
                compress = suffix
            elif suffix in _KNOWN_UNSUPPORTED_COMPRESSION_SUFFIXES:
                # tan-cli#487, defect 2, shape 2: no `compress` key at all,
                # but a suffix (`.wic.zst`, `.wic.bz2`, `.wic.lzo`) this
                # backend RECOGNISES as a real codec it just cannot
                # decompress via `dd` -- the old auto-detect missed it (only
                # "gz"/"xz" are recognised), so `compress` silently resolved
                # to `None`, which reads as "genuinely uncompressed" and
                # raw-`dd`s the compressed stream. An UNRECOGNISED suffix
                # (anything else) is left alone -- there is no way to tell
                # "genuinely uncompressed" from "a codec this list simply
                # does not know about" for those, and guessing wrong in THAT
                # direction would refuse a legitimate plain `.wic`.
                raise FlashPlanError(
                    f"yocto_wic: artefact suffix '.{suffix}' looks compressed but "
                    'is not supported by the dd fallback -- the vocabulary is '
                    '"gz" | "xz". Decompress the artefact first, or install '
                    "bmaptool (preferred -- it decompresses natively)."
                )
            # else: no `compress` key and an unrecognised/absent suffix --
            # genuinely uncompressed, `compress` stays `None`.
        elif compress not in ("gz", "xz"):
            # tan-cli#487, defect 2, shape 1 (an explicit out-of-vocabulary
            # `compress`) and shape 3 (that same bad explicit value SHADOWING
            # a suffix that would have auto-detected correctly -- a genuinely
            # `.wic.gz` artefact with `compress: zst` never even reaches the
            # `if compress is None` branch above, so the bad explicit value
            # wins and the auto-detect that WOULD have been right never
            # runs). Both used to fall through to the `else` branch below and
            # raw-`dd` the still-compressed stream onto the block device --
            # silently, `ok:true`, exit 0, and the board then silently does
            # not boot. That `else` branch is the CORRECT path for a
            # genuinely uncompressed `.wic`; it must never be reached for a
            # value the manifest actually SET to something this backend
            # cannot decompress. The documented vocabulary is "gz" | "xz" |
            # None (alp-sdk `scripts/flash_backends/yocto_wic.py:46`).
            raise FlashPlanError(
                f"yocto_wic: flash_args.compress '{compress}' is not supported -- the "
                'vocabulary is "gz" | "xz" (omit the key to auto-detect from the '
                "artefact suffix instead)."
            )
        bs = _default(fa_str(fa, "bs"), "4M")
        dd_cmd = ["dd", f"of={target}", f"bs={bs}", "conv=fsync", "status=progress"]
        if compress == "gz":
            if which("gunzip"):
                dcmp = ["gunzip", "-c", artefact]
            elif which("gzip"):
                dcmp = ["gzip", "-dc", artefact]
            else:
                raise FlashPlanError(
                    "yocto_wic: compressed .wic.gz fallback needs `gunzip` or `gzip` on PATH."
                )
            argv = tuple([*dcmp, PIPE, *dd_cmd])
        elif compress == "xz":
            if not which("xz"):
                raise FlashPlanError(
                    "yocto_wic: compressed .wic.xz fallback needs `xz` on PATH."
                )
            argv = tuple(["xz", "-dc", artefact, PIPE, *dd_cmd])
        else:
            argv = (
                "dd", f"if={artefact}", f"of={target}", f"bs={bs}",
                "conv=fsync", "status=progress",
            )
    else:
        raise FlashPlanError(
            "yocto_wic: neither `bmaptool` nor `dd` is on PATH; install bmaptool "
            "(preferred -- sparse aware) via `apt install bmap-tools` or run on a "
            "host with coreutils."
        )
    return FlashPlan(
        argv=argv,
        ok_message=f"yocto_wic[{inp.core_id}]: programmed {target}",
        planning_only=planning_only,
    )


def plan_xspi_flashwriter(inp: FlashInputs, which: Callable[[str], bool]) -> FlashPlan:
    """`xspi_flashwriter`: Renesas Flash Writer over SCIF. Planning-only unless
    confirmed; the confirmed real write is HW-gated and fails today."""
    del which
    fa = inp.flash_args
    partition = _default(fa_str(fa, "flash_partition"), "")
    if partition not in ("mtd0", "mtd1"):
        raise FlashPlanError(
            "xspi_flashwriter: flash_args.flash_partition must be 'mtd0' (bl2) or 'mtd1' (fip)"
        )
    port = _default(fa_str(fa, "port"), "<port>")
    writer = _default(fa_str(fa, "flash_writer"), "<flash_writer.mot>")
    baud = _default(fa_int_checked(fa, "baud"), 115200)
    artefact_name = os.path.basename(inp.artefact)
    argv = (
        "flash-writer-scif", f"port={port}", f"writer={writer}", f"baud={baud}",
        f"partition={partition}", f"artefact={artefact_name}",
    )
    confirm = inp.force_confirm or _default(fa_bool_checked(fa, "confirm"), False)
    if inp.dry_run or not confirm:
        why = "dry-run" if inp.dry_run else "flash_args.confirm is false"
        return FlashPlan(
            argv=argv,
            ok_message=(
                f"xspi_flashwriter[{inp.core_id}]: would write {artefact_name} -> xSPI "
                f"{partition} via Flash Writer on {port} ({confirm_gate_note(why)})"
            ),
            planning_only=True,
        )
    raise FlashPlanError(
        "xspi_flashwriter: the real SCIF write is HW-gated and not yet validated on "
        "silicon (bench shelved). Run with --dry-run; see docs/provisioning.md."
    )


# ── Flow D: J-Link direct MRAM write ────────────────────────────────────────

#: The `flash_args` key that ARMS Flow D. Only the part-number device profile
#: is required: without it J-Link has no MRAM loader at all, so its presence
#: alone is metadata's statement that this silicon has one. `slot0_load_address`
#: is NOT an arming key -- `tan/planner/orchestrator.py`/`loader.py` DO emit it
#: today (alp-sdk#1374/tan-cli#353) for AEN slot0-XIP cores, but even where
#: present it only ever selects the two-blob mramxip SHAPE, an ITCM-overflow
#: exception, not whether Flow D applies at all. Requiring it here would leave
#: Flow D permanently unarmed for every real AEN entry, which is the bug this
#: comment replaces.
FLOW_D_KEYS = ("jlink_flash_device",)
FLOW_D_METHOD = "alif_mram_jlink"

#: `jlink_serial`'s `validate_identifier(..., destination=...)` override
#: (tan-cli#486 review): it is interpolated into a J-Link Commander
#: `SelectEmuBySN` line, never an OpenOCD Tcl script -- the default
#: `destination` names the wrong interpreter for this one field.
_JLINK_SERIAL_DESTINATION = "a J-Link Commander script line (SelectEmuBySN)"


def flow_d_available(flash_args: Any) -> bool:
    """Whether the manifest armed Flow D for this entry, i.e. supplied every
    key in `FLOW_D_KEYS`. Purely a data question -- see `select_flash_method`.

    KEY PRESENCE, deliberately -- not "resolves to a non-null/non-empty
    string": an `is not None` check collapses a present-but-null
    `jlink_flash_device:` (bare YAML null) to "absent" and SILENTLY routes the
    entry to Flow A over the SE-UART instead, with no diagnostic at all.
    Transport must never be decided by a quoting detail. Using `_fa_has_key`
    arms Flow D on presence alone, so a present-but-null/malformed value still
    reaches `plan_alif_mram_jlink`, which turns it into the loud refusal it
    already produces for every other malformed Flow D field -- not a silent
    Flow A fallback. `fa_str_checked` itself only raises on a genuinely
    malformed (wrong-type) value; for present-but-null it quietly returns
    `None` same as for absent, so it is `plan_alif_mram_jlink`'s own explicit
    `_fa_has_key` re-check on that `None` (distinguishing "present but
    null/empty" from "absent") that decides the present-but-null case, not
    `fa_str_checked`'s own check.
    """
    return all(_fa_has_key(flash_args, key) for key in FLOW_D_KEYS)


def select_flash_method(target: FlashTarget) -> str | None:
    """The `flash_method` actually dispatched for `target` -- **Flow D by
    default, Flow A as the fallback.**

    Two host paths put a signed image into MRAM on an Alif Ensemble part. Both
    need the SETOOLS `app-gen-toc` step to sign the ATOC; they differ only in
    TRANSPORT, and the transport is the part tan owns:

    * **Flow A** -- `zephyr_west_flash` with no runner, so `west flash` picks the
      board.cmake default (`alif_flash`) and burns over the SE-UART. Needs a
      dedicated 1.8 V-capable USB-UART, which the bench runbook calls the #1
      trap.
    * **Flow D** -- `alif_mram_jlink`: J-Link straight over SWD, no SE-UART. Same
      blob(s), same addresses, ~0.16 s, and the bench's day-to-day default
      (`docs/aen-bench-bringup.md`: "Flow D is the day-to-day default now").

    The switch is made **entirely from data**, never from silicon knowledge: a
    `zephyr_west_flash` entry whose `flash_args` carries `FLOW_D_KEYS` is
    dispatched as Flow D instead. tan cannot ask "is this an AEN MRAM part?" --
    that would put a SKU or an address in tan, which ADR-0017 / I-26 forbid and
    no gate would catch. What it CAN ask is "does this slice's SoC variant
    publish a part-number J-Link profile in its `debug:` block?", because
    that arriving at all IS metadata's statement that this silicon has a
    J-Link MRAM loader.

    Consequence, stated plainly: `tan/planner/loader.py` already resolves `jlink_flash_device`
    (and, where present, the paired `expect_dpidr`/`jlink_device` preflight) from the SoC variant's
    `debug:` block -- selected via the SoM preset's `silicon_variant` -- and
    `tan/planner/orchestrator.py::_slice_flash_recipe` copies them onto the emitted `flash_args`.
    `slot0_load_address` rides alongside them in the same `args` dict but is sourced differently on
    purpose: `loader.py::_resolve_slot0_load_address` reads it from the SoM preset's `memory_map:`,
    NOT from that `debug:` block -- it is SDK/module build POLICY, not a silicon fact
    (alp-sdk#1069), so two SoMs on the same part can pick different slot0 windows. So a Zephyr
    slice on a SoC variant that publishes a part-number J-Link profile already carries
    `FLOW_D_KEYS` on emit and dispatches to Flow D, not Flow A. A slice on a variant with no such
    profile still emits `args={}` and stays on Flow A, which is correct: that silicon has no J-Link
    MRAM loader for tan to arm. That is most of today's shipped Alif metadata (13 Ensemble variants
    total; only 2 carry `jlink_flash_device`, and of those only 1 also carries `expect_dpidr`) -- a
    fact about what's published today, not an invariant of the emitter itself.
    """
    method = target.flash_method or None
    if method == "zephyr_west_flash" and flow_d_available(target.flash_args):
        return FLOW_D_METHOD
    return method


def parse_atoc_start_address(text: str) -> str | None:
    """The ATOC package's MRAM placement out of an `app-gen-toc`
    `app-package-map.txt` report -- the LAST `APP Package Start Address:`
    line's last field, mirroring every bench script's own
    ``awk '/APP Package Start Address:/{print $NF}' app-package-map.txt | tail
    -1`` byte for byte (last match wins: a re-signed re-run APPENDS a fresh
    block rather than truncating the file, per
    `scripts/bench/aen/flash-jlink.sh`/`flash-jlink-mramxip.sh`/
    `flash-update-log-dual.sh`). `None` when the marker never appears -- an
    empty, foreign or not-yet-signed file, not a malformed one; the caller
    decides what that means.

    **tan-cli#373.** `tan.core.setools.sign_slot0` -- the one place this repo
    WRITES that report -- never deletes it for exactly this reason: an
    earlier version did, which destroyed every prior entry (this SETOOLS
    install's whole accumulated sign history) the moment a soft-failing
    re-sign recreated the file holding only its own block.

    **This is a BUILD-TIME output, never plan-time metadata.** `app-gen-toc`
    writes the address fresh at signing time and the runbook says outright it
    SHIFTS per build/config -- no field under `metadata/**` can express it, so
    parsing this report is the only correct source. See `plan_alif_mram_jlink`
    for the required/optional split this feeds; the actual file read happens
    in `tan.commands.flash_cmd` (IO), never here.
    """
    address: str | None = None
    for line in text.splitlines():
        if "APP Package Start Address:" not in line:
            continue
        fields = line.split()
        if fields:
            address = fields[-1]
    return address


def is_elf_artefact(artefact: str) -> bool:
    """Extension-based, mirroring `is_raw_bin`'s own convention: no extension,
    `.elf`, or `.out` (case-insensitive) -- the three "plausibly ELF" shapes
    #367(a) named and #353 agreed are safe to resolve automatically to a
    same-stem sibling `.bin`. Covers the Zephyr build's own known-good
    `zephyr.elf`/`zephyr.bin` pair AND a toolchain output named bare (`app`)
    or `.out` with no `.elf` suffix. Every other shape (a `.hex` carries its
    own load addresses) is NOT, even when a same-stem `.bin` happens to sit
    beside it: that could be an unrelated image, and resolving it silently
    would flash something the manifest never named.

    **tan-cli#373.** A prior version of this function accepted `.elf` only --
    a narrowing of #367(a)'s own three-shape decision that nothing flagged,
    since no test exercised the other two.
    """
    return os.path.splitext(artefact)[1].lower() in ("", ".elf", ".out")


def resolve_slot0_binary(artefact: str, is_file: Callable[[str], bool]) -> str | None:
    """The ONE definition of "what raw `.bin` does this slot0 write/sign
    actually mean" -- shared by [`plan_alif_mram_jlink`] (the `loadbin`/
    `verifybin` pair) and `tan.commands.flash_cmd
    ._resolve_flow_d_atoc_via_setools` (the SETOOLS `app-gen-toc` sign
    input), via [`validate_flow_d_shape`], so the two can never resolve
    DIFFERENT files for the same entry. **#367's root cause**: they used to
    each carry their own copy of this logic, and neither actually applied the
    ELF-only restriction its own comment/tests claimed -- a `.hex` resolved
    to a same-stem sibling `.bin` exactly like an ELF did, silently flashing
    a different artefact than the manifest named.

    Already a raw `.bin` -> returned unchanged. A plausibly-ELF artefact
    ([`is_elf_artefact`]: no extension, `.elf`, `.out`) with a real
    same-directory, same-stem `.bin` -> that sibling. Every other shape -- a
    `.hex`, a plausibly-ELF artefact with no sibling, anything else -- ->
    `None`; the caller decides how to phrase the refusal.
    """
    if is_raw_bin(artefact):
        return artefact
    if not is_elf_artefact(artefact):
        return None
    sibling = os.path.splitext(artefact)[0] + ".bin"
    return sibling if is_file(sibling) else None


@dataclass(frozen=True)
class FlowDShape:
    """Everything about a Flow D entry that is knowable WITHOUT `atoc`/
    `atoc_address` -- i.e. before SETOOLS may need to sign one. `artefact` is
    already resolved via [`resolve_slot0_binary`] when `app_address` is set;
    callers must use THIS value, not the raw input artefact, for both the
    SETOOLS sign input and the final loadbin/verifybin.
    """

    device: str
    app_address: str | None
    artefact: str


def validate_flow_d_shape(fa: Any, artefact: str, is_file: Callable[[str], bool]) -> FlowDShape:
    """Validate + resolve every Flow D input that does NOT depend on `atoc`/
    `atoc_address` -- the part-number device profile and, when
    `slot0_load_address` selects the mramxip shape, the artefact itself.

    **The single source `tan.commands.flash_cmd._flash_entry` now calls
    BEFORE ever considering a SETOOLS auto-sign or a `--dry-run` preview
    (#366).** `atoc`/`atoc_address` are legitimately still absent at that
    point -- SETOOLS is what is about to produce them -- so this cannot
    validate the WHOLE entry; splitting out exactly the half that CAN be
    checked early means a manifest that would fail here can no longer report
    `ok:true` on a SETOOLS `--dry-run` preview just because the failure used
    to live only in the (until-then unreached) second half of
    `plan_alif_mram_jlink`.

    Also called BY `plan_alif_mram_jlink` itself -- there remains exactly ONE
    implementation of these checks; calling it twice on the real-write path
    is pure/side-effect-free, so the repeat costs nothing.
    """
    device = fa_str_checked(fa, "jlink_flash_device", False)
    if device is None:
        if _fa_has_key(fa, "jlink_flash_device"):
            raise FlashPlanError(
                f"{FLOW_D_METHOD}: flash_args.jlink_flash_device is present but "
                "null/empty -- refusing to write MRAM with no part-number J-Link "
                "device profile; the generic profile has none. It is a per-variant "
                "metadata fact (socs/**/*.json `variants[].debug.jlink_flash_device`); "
                "tan does not guess a part number."
            )
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: flash_args.jlink_flash_device is required -- only the "
            "part-number J-Link device profile unlocks the MRAM loader, and the "
            "generic profile has none. It is a per-variant metadata fact "
            "(socs/**/*.json `variants[].debug.jlink_flash_device`); tan does not "
            "guess a part number."
        )
    validate_identifier(device, "jlink_flash_device")
    # OPTIONAL -- selects the mramxip two-blob shape when present; the default
    # single-ATOC-blob shape (flash-jlink.sh) needs no app-address write at
    # all, since the ATOC embeds the app. `None` only for genuinely absent; a
    # present-but-malformed value still raises below, never silently reverts
    # to the default shape.
    #
    # `fa_str_checked` alone cannot tell "key absent" from "key present with a
    # null/empty-string value" -- both collapse to `None` (`raw or None` at
    # line ~571). A key that IS present must still refuse when it resolves to
    # `None`: a `slot0_load_address: ""` or a bare `slot0_load_address:` (YAML
    # null) must never silently pick the default shape, exactly like any other
    # malformed value.
    app_address = fa_str_checked(fa, "slot0_load_address", True)
    if app_address is None and _fa_has_key(fa, "slot0_load_address"):
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: flash_args.slot0_load_address is present but "
            "null/empty -- refusing to silently select the default "
            "single-ATOC-blob shape. Remove the key entirely to use the default "
            "shape, or supply the app's real MRAM address to select the mramxip "
            "two-blob shape."
        )
    resolved_artefact = artefact
    if app_address is not None:
        validate_address(app_address, "slot0_load_address")
        # The mramxip shape `loadbin`s the app blob at an explicit MRAM
        # address -- correct ONLY for a raw `.bin`. `loadbin`ing anything else
        # (e.g. `zephyr.elf`) at that address writes the artefact's own
        # headers into MRAM instead of the app image (tan-cli#311). There is no
        # ELF/HEX fallback to `loadfile` here: it ignores `slot0_load_address` entirely,
        # which would silently place the app wherever the ELF's own load
        # addresses say rather than where this flow demands -- a refusal is
        # the safer failure. tan-cli#353 resolves a real ELF/sibling-`.bin`
        # pair rather than refusing over something resolvable;
        # [`resolve_slot0_binary`] is the ONE place that decides which shapes
        # qualify (#367).
        resolved = resolve_slot0_binary(artefact, is_file)
        if resolved is None:
            if is_elf_artefact(artefact):
                detail = (
                    "No sibling "
                    f"{os.path.basename(os.path.splitext(artefact)[0] + '.bin')} "
                    "was found beside it either."
                )
            else:
                detail = (
                    "Only a plausibly-ELF artefact's (no extension, .elf, .out) "
                    "same-stem sibling .bin is resolved automatically -- a .hex "
                    "(or any other shape) is not, even with a same-stem .bin "
                    "beside it, since that could be an unrelated image."
                )
            raise FlashPlanError(
                f"{FLOW_D_METHOD}: flash_args.slot0_load_address is set but the "
                f"artefact {artefact} is not a raw .bin -- refusing to loadbin "
                "it at slot0_load_address, which would write the artefact's own "
                f"headers into MRAM instead of the app image. {detail} Point the "
                "build's output_artefact at the slot0-linked zephyr.bin for the "
                "mramxip shape."
            )
        resolved_artefact = resolved
    # tan-cli#373: `jlink_speed`/`confirm` do not depend on `atoc`/`atoc_address`
    # either, so they belong in the "everything checkable early" half this
    # function IS -- but #366 only moved `jlink_flash_device`/
    # `slot0_load_address` here, leaving these two still validated only deep
    # inside `plan_alif_mram_jlink`. That left #366's own fix narrowed, not
    # closed: `_resolve_flow_d_atoc_via_setools`'s `--dry-run` preview short-
    # circuits BEFORE `plan_alif_mram_jlink` ever runs (measured: a manifest
    # with `jlink_speed: "fast"` and SETOOLS resolving reported `ok:true`
    # under `--dry-run`), and on a REAL (confirmed) run the SETOOLS auto-sign
    # itself -- spawning `app-gen-toc`, writing into the customer's install --
    # happens before `plan_alif_mram_jlink` ever gets a chance to refuse.
    # Validating (and discarding the result -- `plan_alif_mram_jlink` still
    # applies its own default) here closes that gap regardless of which path
    # the entry takes afterward.
    fa_int_checked(fa, "jlink_speed")
    fa_bool_checked(fa, "confirm")
    # tan-cli#486 review: `jlink_serial` is the same class of "checkable
    # early" field as `jlink_speed`/`confirm` just above -- it does not
    # depend on `atoc`/`atoc_address` either -- but was left validated only
    # deep inside `plan_alif_mram_jlink`/`flow_d_preflight_script`, which is
    # exactly the #373 gap those two comments describe: a hostile
    # `jlink_serial` (e.g. embedded newlines forming extra Commander
    # commands) reported `ok:true` from `tan flash --dry-run`, and on a real
    # confirmed run reached the customer's SETOOLS install via
    # `_resolve_flow_d_atoc_via_setools` -- which runs BEFORE this
    # function -- before ever being refused. Validating (and discarding the
    # result, like the two lines above) here closes that gap regardless of
    # which path the entry takes afterward; the late call sites keep their
    # own check too, defensively, since it is pure and costs nothing to repeat.
    serial = fa_str_checked(fa, "jlink_serial", False)
    if serial is not None:
        validate_identifier(serial, "jlink_serial", destination=_JLINK_SERIAL_DESTINATION)
    return FlowDShape(device=device, app_address=app_address, artefact=resolved_artefact)


def plan_alif_mram_jlink(inp: FlashInputs, which: Callable[[str], bool]) -> FlashPlan:
    """Flow D: burn the signed ATOC into MRAM over SWD with J-Link's built-in
    Alif MRAM loader, verify it, then PIN-reset so the Secure Enclave boot ROM
    boots the image -- the same blob(s) at the same addresses SETOOLS writes
    over the SE-UART, so no re-signing and no keys.

    **Two shapes, selected from data, matching the two bench scripts they
    port** (`scripts/bench/aen/flash-jlink.sh` / `flash-jlink-mramxip.sh`):

    * **Default -- single ATOC blob.** The day-to-day flow
      (`flash-jlink.sh`): the ATOC is self-contained (an ITCM-load package,
      its own embedded load address set by `app-gen-toc`), so ONE
      `loadbin`/`verifybin` of `atoc` at `atoc_address` is the whole write.
      This is what runs whenever `flash_args` omits `slot0_load_address`.
    * **mramxip -- two blobs.** The ITCM-overflow exception
      (`flash-jlink-mramxip.sh`), for an app LINKED into MRAM slot0 (built
      with `CONFIG_USE_DT_CODE_PARTITION=y`, a per-app-build opt-in tan does
      not set): the app blob itself also needs writing, to `slot0_load_address`,
      ahead of the ATOC. This activates only when `flash_args.slot0_load_address`
      is present -- tan cannot detect the Kconfig opt-in from here, so a
      manifest that arms the mramxip shape must supply the address that
      proves it was built that way.

    **Every identifier is read from `flash_args`; none is baked in.** Required
    in both shapes:

    * `jlink_flash_device` -- the PART-NUMBER device profile. Only this unlocks
      the loader; with a generic `Cortex-M55` profile there is no loader and
      `loadbin` to MRAM does nothing useful. It is also the wrong profile for
      attaching to a live core, which is why it is a distinct metadata key
      (`jlink_flash_device`, not `jlink_device`) on the SoC spec.
    * `atoc` + `atoc_address` -- the signed ATOC blob and its MRAM placement.
      The address SHIFTS per build/config and the runbook says outright not to
      hardcode it -- it is a BUILD-TIME output of the signing step, never a
      metadata fact, so this function still requires it as a plain
      `flash_args` value and REFUSES when it is absent. tan does NOT run
      `app-gen-toc` here either way: signing is common to both flows and
      belongs to whatever produced the ATOC. What changed is only WHO fills
      `atoc_address` in before this function runs -- `tan.commands.flash_cmd`
      resolves it from `flash_args.atoc_map` (an `app-package-map.txt` path)
      via `parse_atoc_start_address` when the manifest supplies that instead
      of a baked-in address, so a customer's manifest never has to hardcode a
      value that changes every build. `atoc` itself is read here VERBATIM --
      this module has no filesystem access to resolve it against -- so
      `tan.commands.flash_cmd` also anchors it on `build_root`/`sdk_root`
      (`resolve_artefact_path`, the same resolver `output_artefact`/
      `atoc_map` use) before this function ever sees it; a caller that skips
      that step hands this function a path relative to WHATEVER the eventual
      spawn's cwd turns out to be, not the build root.

    Optional, mramxip-only:

    * `slot0_load_address` -- where the slot0-linked app itself sits, so the SE boots
      it in place rather than loading it out of the ATOC. Present but
      malformed is still a loud refusal, never a silent fall-back to the
      default shape -- a quoting detail must never decide which shape burns.

    Absent a required identifier this REFUSES. There is no default to fall
    back to: a guessed MRAM address is a write to the wrong place on a part
    whose Secure Enclave then boots whatever is there.

    Confirm-gated (`flash_args.confirm` OR `ALP_FLASH_FORCE=1`) like the other
    two persistent-device backends -- see the `planning_only` note below.
    """
    fa = inp.flash_args
    shape = validate_flow_d_shape(fa, inp.artefact, os.path.isfile)
    device, app_address, artefact = shape.device, shape.app_address, shape.artefact

    atoc = fa_str(fa, "atoc")
    atoc_address = fa_str_checked(fa, "atoc_address", True)
    if atoc is None or atoc_address is None:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: flash_args.atoc (the signed ATOC blob) and "
            "flash_args.atoc_address are both required. Both flows burn the SAME "
            "signed ATOC -- sign it with the SETOOLS `app-gen-toc` step and pass the "
            "blob plus the placement its own report prints; the addresses shift per "
            "build and must not be hardcoded."
        )
    validate_address(atoc_address, "atoc_address")

    speed = _default(fa_int_checked(fa, "jlink_speed"), _DEFAULT_JLINK_SPEED)
    # Probe serial: the ONLY disambiguator when a bench carries more than one
    # J-Link. No default -- a bench-wide serial can be shared by two probes that
    # differ only by USB path, and a silent default can select the wrong board.
    # `fa_str_checked`, not the tolerant `fa_str` (tan-cli#486): a NUMERIC
    # serial -- the canonical SEGGER spelling -- is a bare YAML integer, which
    # `fa_str` treats as "absent" and silently drops the SelectEmuBySN line
    # on a bench with more than one probe attached. `validate_identifier`
    # then closes the same newline-injection hole `jlink_flash_device` above
    # is already guarded against: `serial` is interpolated verbatim into
    # `SelectEmuBySN {serial}`, a J-Link Commander script LINE. Also already
    # validated by `validate_flow_d_shape` above (tan-cli#486 review, #373
    # gap) -- repeated here defensively; the check is pure and this is the
    # value actually used below.
    serial = fa_str_checked(fa, "jlink_serial", False)
    if serial is not None:
        validate_identifier(serial, "jlink_serial", destination=_JLINK_SERIAL_DESTINATION)
    # The expected SW-DP IDR. When the manifest supplies one, the Commander
    # script connects with the READ profile first and the caller ABORTS unless
    # that ID appears -- writing MRAM on the wrong attached board is the one
    # unrecoverable mistake this path can make. A hardware value, so it comes
    # from data: tan neither knows nor invents an IDR.
    # tan-cli#795: `as_hex_address=True` -- an unquoted YAML `expect_dpidr:
    # 0x0BE12477` must round-trip back to `0x0BE12477`, the way `base` /
    # `atoc_address` / `slot0_load_address` already do, not decay to the
    # decimal string `199304311` and refuse the CORRECT board.
    expect_dpidr = fa_str_checked(fa, "expect_dpidr", True)
    if expect_dpidr is not None:
        validate_address(expect_dpidr, "expect_dpidr")

    jlink = _JLINK_BINARIES[0] if inp.dry_run else next(
        (n for n in _JLINK_BINARIES if which(n)), None
    )
    if jlink is None:
        raise FlashPlanError(
            f"{FLOW_D_METHOD}: needs SEGGER J-Link on PATH (JLinkExe/JLink), on a "
            "V9.46+ DLL with the probe on matched firmware -- the built-in Alif MRAM "
            "loader ships with the DLL and older ones cannot connect with the "
            "part-number device profile."
        )

    # Two-blob mramxip shape only when `slot0_load_address` armed it; otherwise the
    # default single-ATOC-blob shape (flash-jlink.sh) writes nothing for the
    # app -- the ATOC already embeds it. See the docstring's "two shapes" note.
    lines: list[str] = []
    if serial is not None:
        lines.append(f"SelectEmuBySN {serial}")
    else:
        # tan-cli#353: no serial pinned, so this script selects no probe. Fine
        # on a single-probe host; on a bench with several J-Links JLinkExe
        # cannot choose and answers "Connecting to J-Link ...FAILED: Cannot
        # connect to the probe/programmer." -- measured on the AEN bench, which
        # carries three. Recorded here so the failure diagnosis can SAY that
        # instead of leaving the user with SEGGER's bare sentence; the plan
        # itself is unchanged, because refusing would break every correct
        # single-probe host.
        pass
    # tan-cli#486: quoting (below) is not escaping -- a newline embedded in
    # either path still ends the quoted string's own Commander LINE and
    # starts a new, attacker-chosen one. Validated on the UNQUOTED value,
    # same as `jlink_commander_script`'s own artefact guard.
    validate_commander_path(artefact, "the flash artefact path")
    validate_commander_path(atoc, "flash_args.atoc")
    # Quoted (tan-cli#369) ONLY for these Commander-script lines, when either
    # actually contains whitespace -- `artefact`/`atoc` themselves are left
    # unquoted for `ok_message` and every other use above.
    commander_artefact = commander_path(artefact)
    commander_atoc = commander_path(atoc)
    lines += ["si SWD", f"speed {speed}", f"device {device}", "connect"]
    if app_address is not None:
        # `artefact`, not `inp.artefact`: the tan-cli#353 sibling resolution
        # above may have swapped an ELF for its real raw `.bin`, and the
        # write must use what was RESOLVED or the guard would be decorative.
        lines.append(f"loadbin {commander_artefact} {app_address}")
    lines.append(f"loadbin {commander_atoc} {atoc_address}")
    if app_address is not None:
        lines.append(f"verifybin {commander_artefact} {app_address}")
    lines += [
        f"verifybin {commander_atoc} {atoc_address}",
        # PIN reset (RSetType 2), then run: the Secure Enclave boot ROM re-reads
        # and boots the ATOC, exactly as it does after an SE-UART burn. A core
        # reset would leave the SE out of the loop.
        "RSetType 2",
        "r",
        "g",
        "exit",
    ]
    argv = (
        jlink, "-device", device, "-if", "SWD", "-speed", str(speed),
        "-ExitOnError", "1", "-NoGui", "1", "-CommanderScript",
    )
    confirm = inp.force_confirm or _default(fa_bool_checked(fa, "confirm"), False)
    ok_message = (
        f"{FLOW_D_METHOD}[{inp.core_id}]: app -> {app_address}, signed ATOC -> "
        f"{atoc_address} via J-Link ({device}); verified and PIN-reset"
        if app_address is not None
        else (
            f"{FLOW_D_METHOD}[{inp.core_id}]: signed ATOC (app embedded) -> "
            f"{atoc_address} via J-Link ({device}); verified and PIN-reset"
        )
    )
    return FlashPlan(
        argv=argv,
        ok_message=ok_message,
        # `planning_only` -- and therefore the `planned` status + the
        # `flash.confirm-required` warning -- for an UNCONFIRMED run, matching
        # `yocto_wic`/`xspi_flashwriter`. This is a NEW backend, so nothing in
        # the oracle is being diverged from, and it is the only backend in the
        # registry that persistently programs on-die MRAM: a `tan flash` in a
        # fresh customer's checkout must not silently reprogram an attached
        # module. A backend targeting an external helper MCU's own flash
        # rather than persistent on-die storage would not need this gate.
        planning_only=inp.dry_run or not confirm,
        jlink_script="\n".join(lines) + "\n",
    )


def validate_flow_d_preflight_args(
    flash_args: Any, method: str = FLOW_D_METHOD, *, require_device_key: bool = True
) -> tuple[str | None, str | None]:
    """The presence/pairing/shape checks for the read-only DPIDR preflight,
    returning `(expect_dpidr, jlink_device)` -- both `None` (opted out) or
    both set (validated), UNLESS `require_device_key` is `False` (see below),
    in which case the second slot is always `None`. Raises `FlashPlanError`
    for every half-armed or malformed shape; never touches a J-Link binary or
    builds the Commander script, so the CALLER decides when to run it.
    `flow_d_preflight_script` (write-path) runs it then builds the script
    from the same values; `tan.commands.flash_cmd` also runs it PLAN-TIME,
    before the confirm/dry-run gate, so a half-armed or malformed manifest
    surfaces as a `flash.entry-failed` issue in the planned envelope too --
    not only at real-write time.

    Written to be shared by more than one backend, not one copy per backend
    (tan-cli#520): the caller is Flow D (`alif_mram_jlink`) today, with
    `method` interpolated ONLY into the refusal text -- it names which
    backend's `flash_args` a customer needs to fix, since a manifest could
    otherwise not tell which entry a bare "flash_args.expect_dpidr" refusal
    was about. Defaults to `FLOW_D_METHOD` so every existing Flow D call site
    keeps its exact wording unchanged. `swd_probe`'s J-Link arm was the second
    caller (`method="swd_probe"`, `require_device_key=False`) until tan-cli#732
    removed the backend; the parameter stays for a future J-Link-arm backend
    with the same shape.

    `require_device_key` (default `True`, Flow D's own shape, unchanged):
    whether `expect_dpidr` must be PAIRED with a `flash_args.jlink_device`
    naming the live-core read attach profile, the way Flow D needs it
    (Flow D's `jlink_device` is DISTINCT from its write-time flash-algorithm
    profile, `jlink_flash_device`). A backend whose `flash_args.jlink_device`
    already means the write's own `-device` profile -- `swd_probe` was one --
    passes `False`: pairing that field with `expect_dpidr` the way Flow D's
    DIFFERENT field is paired would retroactively demand a preflight of every
    manifest that only ever set the write device. With
    `require_device_key=False`, `expect_dpidr` ALONE arms the preflight and
    the second returned slot is always `None`; the caller supplies its own
    read device via `FlashPlan.preflight_device`.

    `None`/`None` (or `expect_dpidr`/`None` under `require_device_key=False`)
    only for a genuinely ABSENT `expect_dpidr` -- the documented, test-pinned
    way to opt out of the preflight entirely. A key that IS present must
    still refuse when it resolves to `None` (`fa_str_checked` alone cannot
    tell "absent" from "present but null/empty"; see `_fa_has_key`'s
    docstring): silently treating it as absent would drop the SW-DP IDR check
    -- the one guard standing between a wrong-board attach and an MRAM/GD32
    write -- with no diagnostic at all.
    """
    fa = flash_args
    # tan-cli#795: `as_hex_address=True` -- same round-trip fix as
    # `plan_alif_mram_jlink` above; a bare YAML integer must not decay to a
    # decimal string that then fails to match the probe's hex-formatted banner
    # on the CORRECT board.
    expected = fa_str_checked(fa, "expect_dpidr", True)
    if expected is None and _fa_has_key(fa, "expect_dpidr"):
        raise FlashPlanError(
            f"{method}: flash_args.expect_dpidr is present but null/empty -- "
            "refusing to silently skip the pre-write SW-DP IDR check. Remove the key "
            "entirely to skip the preflight, or supply the board's real expected ID."
        )
    if not require_device_key:
        if expected is None:
            return None, None
        validate_address(expected, "expect_dpidr")
        _validate_expect_dpidr_width(expected, method)
        return expected, None
    read_device = fa_str_checked(fa, "jlink_device", False)
    if read_device is None and _fa_has_key(fa, "jlink_device"):
        raise FlashPlanError(
            f"{method}: flash_args.jlink_device is present but null/empty -- "
            "refusing to silently skip the pre-write SW-DP IDR check. Remove the key "
            "entirely to skip the preflight, or supply the live-core read device "
            "profile."
        )
    # Half-armed by a genuinely ABSENT partner key (not a null one -- that is
    # the two checks above): supplying `expect_dpidr` is the manifest's
    # unambiguous statement that it wanted the wrong-board guard armed, and
    # the reverse holds for `jlink_device`. Silently returning `None` here
    # would drop the SW-DP IDR check with no diagnostic at all, immediately
    # before the one write this backend's own docstring calls unrecoverable.
    if (expected is None) != (read_device is None):
        present_key, absent_key = (
            ("expect_dpidr", "jlink_device")
            if expected is not None
            else ("jlink_device", "expect_dpidr")
        )
        raise FlashPlanError(
            f"{method}: flash_args.{present_key} is present but flash_args."
            f"{absent_key} is not -- refusing to silently skip the pre-write SW-DP "
            "IDR check. Supply both flash_args.expect_dpidr and flash_args."
            "jlink_device to arm the preflight, or remove both to skip it entirely."
        )
    if expected is None or read_device is None:
        return None, None
    validate_address(expected, "expect_dpidr")
    _validate_expect_dpidr_width(expected, method)
    validate_identifier(read_device, "jlink_device")
    return expected, read_device


def _validate_expect_dpidr_width(expected: str, method: str) -> None:
    """Refuse an `expect_dpidr` that is not a full 32-bit / 8-hex-digit SW-DP
    ID (tan-cli#795). `validate_address` (above) stays a generic charset check
    of any length -- `base` and friends share it -- so this width rule lives
    here instead, narrowly scoped to the one field this preflight arms off of,
    and runs at the SAME PLAN-TIME call site as `validate_address` itself:
    Flow D calls `validate_flow_d_preflight_args` before any write, via
    `tan.commands.flash_cmd`, so a truncated value surfaces under `--dry-run`
    too, not only when a real J-Link probe reads a banner.

    Without this, a truncated value like `0x2477`, or `0x477` (ARM's own
    JEP106 designer field, shared by every ARM SW-DP), would match any ARM
    board's banner and silently disarm the wrong-board guard.
    """
    digits = expected[2:] if expected[:2] in ("0x", "0X") else expected
    if len(digits) == 8:
        return
    raise FlashPlanError(
        f"{method}: flash_args.expect_dpidr = {_quoted(expected)} is not a full "
        "32-bit SW-DP ID (8 hex digits) -- refusing to arm the wrong-board "
        "guard with a value short enough to match more than one board (ARM's "
        "own JEP106 designer field, 0x477, is shared by every ARM SW-DP)."
    )


def flow_d_preflight_script(
    inp: FlashInputs, method: str = FLOW_D_METHOD, *, read_device: str | None = None
) -> tuple[str, str] | None:
    """The read-only DPIDR preflight script for a Flow D J-Link plan (tan-cli
    #520 generalised this out of a Flow D-only shape so a second backend
    could reuse it -- see `read_device` below): `(script, expected_id)`, or
    `None` when the preflight is not armed at all -- see
    `validate_flow_d_preflight_args` for every refusal shape, which this
    delegates to (passing `method` through unchanged) before building the
    script.

    `read_device` (tan-cli#520, default `None`): for a CALLER that already has
    a connect device in hand via `FlashPlan.preflight_device` -- the same
    device its own write already resolved -- pass it here rather than
    requiring a second `flash_args` key. This flips
    `validate_flow_d_preflight_args`'s `require_device_key` to `False`, so
    `expect_dpidr` ALONE arms the preflight on that path. `None` (the default)
    keeps Flow D's own shape exactly: `jlink_device` PAIRED with
    `expect_dpidr` in `flash_args`, as it always has been. `swd_probe`'s
    J-Link arm was the one caller that passed a `read_device` until tan-cli
    #732 removed the backend; the parameter stays for the next one.

    Run BEFORE any write, with a live-core attach profile (which the
    part-number one is not), so the caller can abort on the wrong board while
    the session is still read-only. The expected ID always comes from
    `flash_args.expect_dpidr`; the device name comes from either
    `flash_args.jlink_device` (Flow D shape) or the caller-supplied
    `read_device`, depending on which was armed.
    """
    expected, paired_device = validate_flow_d_preflight_args(
        inp.flash_args, method, require_device_key=read_device is None
    )
    if expected is None:
        return None
    read_device = read_device if read_device is not None else paired_device
    if read_device is None:
        return None
    # tan-cli#520 REVIEW, BLOCKER 1: `read_device` -- whichever source it came
    # from -- is charset-validated HERE, unconditionally, before it reaches
    # the script line below. A CALLER-supplied `read_device` (`swd_probe`'s
    # `FlashPlan.preflight_device`, i.e. `_resolve_jlink_device`'s return
    # value) was NEVER validated anywhere: `_resolve_jlink_device` returns
    # `flash_args.jlink_device` verbatim, which was safe while it only ever
    # reached ARGV (a list element, newline-inert) via the write -- this
    # preflight instead interpolates it into a Commander script LINE
    # (`device {read_device}`, a few lines down), the exact injection surface
    # `validate_identifier` already closes for Flow D's own PAIRED
    # `jlink_device` inside `validate_flow_d_preflight_args`'s
    # `require_device_key=True` branch. Without this, an embedded newline in
    # `flash_args.jlink_device` could splice EXTRA Commander lines (a
    # `loadfile`/`r`/`g` ahead of `connect`) into this READ-ONLY preflight,
    # running BEFORE the mismatch abort this function exists to run first --
    # the same class of hole #486 closed for `jlink_serial` on Flow D, now
    # closed here for the device name on both callers (re-validating Flow
    # D's own already-checked `paired_device` here too is redundant but
    # harmless -- `validate_identifier` is pure).
    validate_identifier(read_device, "jlink_device")
    fa = inp.flash_args
    speed = _default(fa_int_checked(fa, "jlink_speed"), _DEFAULT_JLINK_SPEED)
    lines = []
    # `fa_str_checked` + `validate_identifier`, not the tolerant `fa_str`
    # (tan-cli#486) -- same fix as `plan_alif_mram_jlink`'s write script, and
    # more load-bearing HERE: an injected line prefixed onto this READ-ONLY
    # preflight (see the module docstring's "the identity is confirmed while
    # the session is still read-only" comment in `flash_cmd`) would run
    # before the wrong-board abort ever gets a chance to fire. Also already
    # validated by `validate_flow_d_shape`, which `_flash_entry` runs before
    # this preflight (tan-cli#486 review) -- repeated here defensively.
    serial = fa_str_checked(fa, "jlink_serial", False)
    if serial is not None:
        validate_identifier(serial, "jlink_serial", destination=_JLINK_SERIAL_DESTINATION)
        lines.append(f"SelectEmuBySN {serial}")
    lines += ["si SWD", f"speed {speed}", f"device {read_device}", "connect", "exit"]
    return "\n".join(lines) + "\n", expected


_REGISTRY: dict[str, BackendMeta] = {
    "zephyr_west_flash": BackendMeta(("west",), plan_zephyr_west_flash),
    "baremetal_cmake_flash": BackendMeta(("cmake",), plan_baremetal_cmake_flash),
    "yocto_wic_to_sd_or_emmc": BackendMeta(("bmaptool", "dd"), plan_yocto_wic),
    "yocto_wic": BackendMeta(("bmaptool", "dd"), plan_yocto_wic),
    "xspi_flashwriter": BackendMeta((), plan_xspi_flashwriter),
    FLOW_D_METHOD: BackendMeta(("JLinkExe", "JLink"), plan_alif_mram_jlink),
}


# ── tan-cli#609: which methods the wrong-board SW-DP ID guard covers ────────

#: Every registered `flash_method`, mapped to whether **tan itself composes
#: the J-Link Commander session that performs the write** and can therefore
#: run the read-only SW-DP IDR preflight (`flow_d_preflight_script`) ahead of
#: it. `True` is the guard's coverage set.
#:
#: A TABLE rather than an inline `method == ...` test at the one call site,
#: because tan-cli#609 measured what the inline form costs. `flash_cmd` used to
#: gate the unarmed-guard ADVISORY on a single hardcoded method; the AEN
#: dispatches Flow D (`alif_mram_jlink`), so a real MRAM write on
#: `e1m-aen-evk-01` (2026-08-10, `origin/dev` `a9062ea`) emitted `ISSUES = []`
#: -- no guard AND no signal -- on a bench where one J-Link serial is
#: OEM-cloned across two probes and `JLinkExe` selects by serial alone. The
#: advisory tracked whichever method someone had last wired it to, not the
#: methods that can actually write.
#:
#: `False` is NOT a safety claim about a method. It says only that
#: `flash_args.expect_dpidr` would have nothing to arm there, because tan does
#: not build that method's probe session: `zephyr_west_flash` hands the job to
#: `west flash`, whose runner composes its own command line (a J-Link runner
#: among them); `baremetal_cmake_flash` hands it to a CMake target; the
#: `yocto_wic*` pair and `xspi_flashwriter` address a block device and a
#: serial port, neither probe-selected. An advisory on any of those would tell
#: an operator to set a key that does nothing.
#:
#: `tests/gates/test_dpidr_guard_coverage.py` pins these keys to `_REGISTRY`'s,
#: so a NEW backend cannot dispatch until someone has decided which side it is
#: on. That pin is what makes the `.get(..., False)` default in
#: `dpidr_preflight_possible` a closed question rather than a silent omission.
DPIDR_GUARD_COVERAGE: dict[str, bool] = {
    FLOW_D_METHOD: True,
    "zephyr_west_flash": False,
    "baremetal_cmake_flash": False,
    "yocto_wic": False,
    "yocto_wic_to_sd_or_emmc": False,
    "xspi_flashwriter": False,
}


def dpidr_preflight_possible(method: str, preflight_device: str | None) -> bool:
    """Whether tan CAN run its read-only SW-DP IDR preflight for this entry --
    i.e. whether `flash_args.expect_dpidr` would arm a real check here. Pure.

    The method being on the `True` side of `DPIDR_GUARD_COVERAGE` is the whole
    test today. `preflight_device` stays a parameter for call-site symmetry
    with `dpidr_preflight_unarmed`, which threads the same `FlashPlan` field
    through -- tan-cli#732 removed `swd_probe`, the one backend whose plan
    builder could take an arm where a preflight is impossible even though the
    METHOD is covered (`preflight_device is None` on its openocd/pyocd arm).
    Flow D has no such split (it is J-Link by construction) and always passes
    `preflight_device=None`; a future backend with its own split arm would
    restate that second condition here rather than inventing a new predicate.
    """
    return DPIDR_GUARD_COVERAGE.get(method, False)


def dpidr_preflight_unarmed(
    method: str, flash_args: Any, preflight_device: str | None
) -> bool:
    """Whether this write went ahead with a wrong-board guard that COULD have
    run and was not armed -- the condition `flash.dpidr-preflight-unarmed`
    reports. Pure.

    `expect_dpidr` PRESENT is the whole test, via `_fa_has_key` rather than
    `fa_str_checked`: a present-but-null key is a refusal
    (`validate_flow_d_preflight_args`), never a quiet opt-out, so treating it
    as absent here would report the wrong thing about a manifest that already
    cannot flash. Flow D additionally requires `expect_dpidr` to be PAIRED
    with `jlink_device`, but a half-armed pair refuses before any write, so by
    the time an entry reaches a caller of this function "present" and "armed"
    coincide on both covered methods.

    Ask this only of a REAL, CONFIRMED write. It says nothing about a
    `--dry-run` or an unconfirmed preview, neither of which writes anything
    for a wrong-board guard to have protected.
    """
    return dpidr_preflight_possible(method, preflight_device) and not _fa_has_key(
        flash_args, "expect_dpidr"
    )


# ── the required-tool gate ──────────────────────────────────────────────────

PROCEED = "proceed"
SKIP = "skip"
FAIL = "fail"


@dataclass(frozen=True)
class ToolGate:
    outcome: str
    message: str = ""


def tool_gate(
    requires,
    dry_run: bool,
    skip_missing: bool,
    kind: str,
    entry_id: str,
    method: str,
    which: Callable[[str], bool],
) -> ToolGate:
    """A backend is usable when AT LEAST ONE of `requires` is on PATH. Bypassed
    entirely under `--dry-run`, and for a backend with an empty `requires`."""
    if dry_run or not requires:
        return ToolGate(PROCEED)
    if any(which(t) for t in requires):
        return ToolGate(PROCEED)
    msg = (
        f"flash: {kind} '{entry_id}' backend '{method}' needs one of "
        f"{_str_list_debug(requires)} on PATH; none found."
    )
    if skip_missing:
        return ToolGate(SKIP, f"{msg} (skipped via --skip-missing-tools)")
    return ToolGate(FAIL, msg)


# ── argv display ────────────────────────────────────────────────────────────


def display_argv(plan: FlashPlan) -> str:
    """The would-run display string; a J-Link plan shows a `<generated.jlink>`
    placeholder for the temp Commander script (which does not exist yet, and
    whose name carries a pid + nanosecond stamp that must never reach a
    golden)."""
    parts = list(plan.argv)
    if plan.jlink_script is not None:
        parts.append("<generated.jlink>")
    return " ".join(parts)
