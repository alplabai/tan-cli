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

**TWO** exceptions remain, both inherited verbatim from the Rust oracle and
both flagged at their definitions: ``_DEFAULT_JLINK_DEVICE`` (a part number)
and ``_DEFAULT_BASE`` (a flash base address). Do not add a third. The count
used to read "the ONE exception", naming ``_DEFAULT_JLINK_DEVICE`` and
silently passing over the address -- tan-cli#402, which also found the two
literals that miscount was keeping company with: both ``swd_probe``
success messages hardcoded a ``GD32G553`` SKU and reported it as what the run
had just programmed, whatever ``flash_args`` actually resolved to. The
messages now name the resolved device/target, so the only SKU and address
left in this module are the two named above, each reachable only when the
manifest supplied nothing. An honest count is the whole mechanism: a
docstring that undercounts its own debt is how the third one gets added
without argument.
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

#: INHERITED HARDWARE FACT, not a new one -- the second of the two this module
#: carries, flagged here by #402 after the module docstring had counted only
#: `_DEFAULT_JLINK_DEVICE` for as long as both had existed.
#: `builders.rs:16`'s `DEFAULT_BASE`, byte-identical. An ADDRESS, which
#: ADR-0017 / I-26 forbids, and it is already shipped in the Rust binary --
#: dropping it here would make the port place a raw `.bin` somewhere else than
#: the oracle on every `swd_probe` entry whose `flash_args` omits `base`. Only
#: ever reached when the manifest supplied nothing; the correct fix is for the
#: SoM preset to always supply `flash_args.base`, after which this default
#: becomes unreachable and can be deleted on BOTH sides.
_DEFAULT_BASE = "0x08000000"
#: INHERITED HARDWARE FACT, not a new one. `builders.rs:15`'s
#: `DEFAULT_JLINK_DEVICE`. This is a part number in tan, which ADR-0017 / I-26
#: forbids, and it is already shipped in the Rust binary -- changing or dropping
#: it here would make the port disagree with the oracle on every `swd_probe`
#: entry whose `flash_args` omits `jlink_device`. Kept byte-identical and
#: quarantined to this one constant; the correct fix is for the SoM preset to
#: always supply `flash_args.jlink_device` (E1M-V2N101 already does not), after
#: which this default becomes unreachable and can be deleted on BOTH sides.
_DEFAULT_JLINK_DEVICE = "GD32G553MEY7TR"
_DEFAULT_JLINK_SPEED = 4000
_JLINK_BINARIES = ("JLinkExe", "JLink")

#: tan-cli#520 REVIEW round 2, nit: the backend-registry key AND the string
#: `validate_flow_d_preflight_args`/`flow_d_preflight_script`/`_flow_d_
#: preflight` read as `method` to pick which backend a refusal names --
#: named the same way `FLOW_D_METHOD` already is, rather than a bare
#: `"swd_probe"` literal repeated at every call site with nothing tying them
#: together.
SWD_PROBE_METHOD = "swd_probe"


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


@dataclass(frozen=True)
class TargetPlan:
    targets: tuple[FlashTarget, ...]
    warnings: tuple[str, ...]
    refused: tuple[str, ...]
    #: The subset of "status not ok" refusals whose slice `status` is
    #: `"skipped"` -- i.e. `tan build` itself declined to build this slice
    #: under `executionPolicy.missingTool`/`.nullCommand` (a host with no
    #: `bitbake`, say). That was a policy decision already made and reported
    #: at build time; `flash` refusing to flash a never-built artefact is
    #: still correct (there is nothing to flash), but it must not ALSO read
    #: as a flash failure for a slice the customer's manifest already
    #: explained away. `refused` (a `"failed"`/`"pending"`/other status) is
    #: the opposite: `tan build` tried and the slice is broken or was never
    #: reconciled, which must keep failing `tan flash`. Callers surface this
    #: bucket as a WARNING and must not fold it into a failure count -- see
    #: `refused` for the error-severity, exit-code-affecting bucket.
    #:
    #: **DIVERGES from the shipped Rust oracle.** `crates/tan-core/src/
    #: flash/mod.rs`'s `plan_flash_targets` has no `refused_skipped` bucket at
    #: all -- a `status: skipped` slice/helper lands in the ONE `refused` list
    #: alongside `failed`/`pending`/anything else non-`ok`, and the CLI seeds
    #: `failed` from `refused.len()` before the dispatch loop even runs, so the
    #: oracle FAILS the run on a `status: skipped` slice exactly like any other
    #: bad status. This split (and the caller's warning-only, exit-0 treatment
    #: when something else DID flash) is a deliberate product improvement on
    #: top of the port, not a porting bug -- but the caller (`tan.commands.
    #: flash_cmd.flash`) MUST still fail the run when every match was a
    #: `refused_skipped` entry and nothing flashed (`flash.nothing-flashed`),
    #: or this bucket reintroduces the exact silent-success class `refused`
    #: exists to prevent, just inverted. `tests/parity/
    #: test_flash_oracle_parity.py` deliberately carries no `status: skipped`
    #: case for this reason -- the two implementations disagree there by
    #: design and an oracle diff would only fail.
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
      slice are the same silent-failure class. A `status: skipped` refusal is
      split into `refused_skipped` rather than `refused`: `tan build` already
      decided (via `executionPolicy`) that this slice was not supposed to build
      on this host -- e.g. no `bitbake` on an MCU-only checkout -- and that is
      not a flash failure, it is `tan flash` agreeing with a decision already
      made and reported. A genuinely broken slice (`status: failed`, or any
      other non-`ok`/non-`skipped` value) stays in `refused`.
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
                if found.status == "skipped":
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
                )
            )


    return TargetPlan(
        tuple(targets), tuple(warnings), tuple(refused), tuple(refused_skipped)
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
    `<buildDir>/build/` while the plan's `artifacts` block still reports
    `<buildDir>/zephyr/zephyr.elf`. Rust reconciles that at manifest-WRITE time
    (`build/execute/manifest.rs::resolve_zephyr_artefact`, tan's only writer of
    `output_artefact`, which stores the nested ABSOLUTE path); this port's
    `build` does not write the manifest yet, so an artefact string that still
    carries the planner's un-nested spelling would resolve to a file that is not
    there and fail the entry. Probed LAST and only when the oracle's own
    candidates all miss a real file, so it can never change a resolution the
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


#: tan-cli#519 review, MINOR: `validate_identifier`'s charset (alnum/`-`/`_`
#: per `/`-separated segment) refuses `:`, but pyOCD's own `-u`/`--uid`
#: documents an OPTIONAL `<plugin>:<uid>` form -- confirmed against a real
#: installed pyOCD 0.44.1's own `--uid` help text ("Optionally prefixed with
#: '<probe-type>:' where <probe-type> is the name of a probe plugin") and
#: `pyocd list --plugins`, whose plugin names (`cmsisdap`, `jlink`,
#: `picoprobe`, `remote`, `stlink`) are themselves plain lowercase
#: identifiers. A real DAPLink/ST-Link/J-Link UID (bare hex/decimal, no
#: prefix -- `pyocd list` on this box shows attached J-Links as `600107451`/
#: `603000869`) already passed the plain guard; only the plugin-prefixed
#: spelling was over-refused.
def validate_pyocd_uid(text: str, field_name: str = "pyocd_uid") -> None:
    """`validate_identifier`, widened by exactly one shape: a SINGLE
    `<plugin>:<uid>` split, each half charset-guarded the same way the whole
    value always was. Anything that is not that one shape (no colon, more
    than one, or an empty half either side of it) falls straight through to
    the ordinary `validate_identifier` call on the WHOLE value, which refuses
    it with the same message this field always gave -- this function adds
    acceptance for one real pyOCD shape, not a new failure mode."""
    destination = "a spawned pyocd command line argument"
    if text.count(":") == 1:
        plugin, uid = text.split(":", 1)
        if plugin and uid:
            validate_identifier(plugin, field_name, destination=destination)
            validate_identifier(uid, field_name, destination=destination)
            return
    validate_identifier(text, field_name, destination=destination)


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


#: The Jim Tcl bytes that are dangerous UNQUOTED in an OpenOCD `-c` word:
#: `[`/`]` trigger command substitution (Jim Tcl ships `exec`, so this is
#: arbitrary HOST command execution, not just an extra OpenOCD command);
#: `$` triggers variable substitution; `;` separates commands; `"`/`{`/`}`
#: change how the rest of the word is quoted/grouped. Everything else --
#: spaces, `:`, `\\`, letters, digits, `.` -- is left alone; a real artefact
#: path routinely carries all of them and none is special to Jim Tcl outside
#: an already-quoted/braced word.
_OPENOCD_TCL_UNSAFE = frozenset('[]$;"{}')


def validate_openocd_word(text: str, label: str) -> None:
    """Reject a control character or a Jim Tcl metacharacter in a value bound
    for OpenOCD's `-c` command string (tan-cli#486).

    `interface`/`target` already go through `validate_identifier` for exactly
    this reason; the flash artefact cannot, because it is a real filesystem
    path (spaces, `:`, `\\`, drive letters all legitimate) rather than a
    plain identifier. This is the path-shaped equivalent: it blocks the
    substitution/separator characters Jim Tcl treats specially in an
    unquoted word, and control characters (a smuggled newline could inject an
    extra Tcl command the same way it does in a J-Link Commander script),
    while leaving every character a real path needs untouched.
    """
    bad = sorted({c for c in text if ord(c) < 0x20 or ord(c) == 0x7F or c in _OPENOCD_TCL_UNSAFE})
    if bad:
        raise FlashPlanError(
            f"{label} = {_quoted(text)} contains {_str_list_debug(bad)}, a Jim Tcl "
            "metacharacter or control character -- refusing to interpolate it "
            "into OpenOCD's -c command string, where it could inject an extra "
            "command or trigger [...] command substitution (arbitrary host "
            "command execution)."
        )


def openocd_program_word(text: str) -> str:
    """Brace `text` for an OpenOCD `-c` command word, UNCONDITIONALLY
    (tan-cli#486 / tan-cli#487 / tan-cli#511 -- see below for why this is no
    longer conditional). Named for its original and still primary caller
    (`-c program {...} verify ...`, the flash artefact); tan-cli#519 review
    reuses it verbatim for `-c adapter usb location {...}` -- the bracing
    mechanism below is generic, not artefact-specific, and every value this
    function ever receives has already passed `validate_openocd_word`.

    `validate_openocd_word` closes the injection hole but leaves the artefact
    at the mercy of Jim Tcl's own WORD-level rules the moment it is left
    unbraced: whitespace splits an unquoted word on Tcl's normal command
    boundary (`program a.elf verify exit 0x20000000` parses as five words --
    `program`/`a.elf`/`verify`/`exit`/`0x20000000` -- so a space-only hostile
    artefact injects extra keywords with no metacharacter in sight), and
    Jim Tcl performs backslash substitution on every unbraced word regardless
    of whether it also has whitespace, silently mangling any Windows-style
    path (`C:\\Program Files\\alp\\build\\zephyr.elf` -> `C:Program` /
    `Files\\x07lp\\x08uildzephyr.elf` -- `\\a`->BEL, `\\b`->BS -- verified
    against `tclsh`). Both are real, honest-input failures: a Windows user's
    default install path (`C:\\Program Files\\...`) or an artefact whose
    directory happens to contain a space needs no attacker at all to trip
    either one.

    A matched brace pair is the fix: Jim Tcl performs NO substitution --
    command, variable, OR backslash -- on the material between braces, and
    treats the whole braced span as ONE word regardless of embedded
    whitespace. This is safe here BECAUSE `validate_openocd_word` has already
    rejected `{`/`}` (along with `[`/`]`/`$`/`;`/`"`/control characters) for
    every caller of this function -- nothing inside `text` can prematurely
    close the brace or smuggle a substitution past it.

    **Unconditional now, not conditional on whitespace/backslash (tan-cli#511
    Fable advisory).** The conditional version's own justification -- "leaves
    every already-recorded `program <path> verify ...` parity fixture
    byte-identical, since neither recorded case contains whitespace or a
    backslash" -- was never actually true: both frozen fixtures were captured
    with `oracle_fixtures.CAPTURE_PLATFORM = "win32"`
    (`tests/parity/oracle_fixtures.py:75`), so the `<ORACLE-ROOT-0>` scratch
    root each one interpolates is a native Windows path and carries
    backslashes UNCONDITIONALLY -- the predicate this docstring used to
    describe could never once observe the "no backslash" branch on the one
    platform the fixtures are actually anchored to. The Linux-green replay
    that made it LOOK preserved was an artefact of `compare()`'s own
    `normalise_scrubbed_path_separators`, which flattens `\\`->`/` in both
    sides' `entries[].message` before the diff -- plus a POSIX build root
    that never contains a literal backslash to begin with, so the predicate
    happened to answer `False` on BOTH sides there and the diff passed by
    coincidence, not because bracing was truly conditional. On a real
    Windows run -- the platform the fixtures are pinned to -- the predicate
    is unconditionally `True` (every path carries `\\`) and always was; it
    never preserved the parity it was written to protect. So the two cases
    that exercise this ("multi-segment-interface-is-allowed",
    "openocd-forced-bin-appends-base") moved OUT of the byte-diff oracle
    table entirely -- see `tests/parity/test_flash_oracle_parity.py`'s
    `CASES` for the standing divergence note -- into a bounded
    exact-difference test that pins the ONLY token allowed to move.
    Bracing every artefact, with no predicate at all, is therefore not a
    behaviour change bought at the price of losing coverage: the coverage
    the predicate was defending never existed.

    **The one glass jaw this trades in, on purpose: a value ending in an ODD
    number of backslashes.** Jim Tcl's own brace-counting (unrelated to the
    backslash-substitution rule above, which never runs on braced material)
    still walks a braced word looking for the UNESCAPED close brace, and
    counts a run of backslashes immediately before a `}` to decide whether
    that `}` is escaped. A `text` ending in an odd number of `\\` (impossible
    for a real file path -- no filesystem lets a name end in `\\`, and
    `validate_openocd_word` does not need to reject it specially) leaves the
    closing `}` this function appends looking escaped to that counting rule,
    so the brace never closes and OpenOCD reports a parse error. FAIL-SAFE
    (a parse error before anything is written, not a mis-parsed argv), and
    unreachable by any real artefact path -- documented here, not "fixed" by
    stripping the guard, because there is no guard: this is an inherent
    property of Tcl brace-counting that bracing cannot itself avoid, and a
    future reader must not mistake it for a bug in this function.
    """
    return f"{{{text}}}"


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
    #: tan-cli#520: the J-Link device profile `swd_probe`'s J-Link arm already
    #: resolved for the WRITE (`_resolve_jlink_device`), carried through so
    #: the caller's read-only DPIDR preflight can reuse it as its own connect
    #: device rather than re-reading a second `flash_args` key with a
    #: different meaning. `None` for every other backend/arm, including
    #: `swd_probe`'s own openocd/pyocd arm (no J-Link device profile exists
    #: there at all).
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


# ── swd_probe ───────────────────────────────────────────────────────────────


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


def jlink_commander_script(
    artefact: str, base: str, do_reset: bool, serial: str | None = None
) -> str:
    """The J-Link Commander script: reset/halt, load (`loadbin`+base for `.bin`,
    else `loadfile`), optional reset-and-go, quit-close.

    `serial` (tan-cli#513) is the ONLY disambiguator when a bench carries more
    than one J-Link -- `JLinkExe` selects a probe by serial alone, with no
    USB-port equivalent. When given, it is emitted as a leading
    `SelectEmuBySN {serial}` line, matching `plan_alif_mram_jlink`'s own Flow D
    shape (its `lines.append(f"SelectEmuBySN {serial}")` emit, its DPIDR
    preflight's identical emit in `flow_d_preflight_script`) byte-for-byte --
    same line text, same "first line of the script" position, same
    "absent -> no line" default. `swd_probe` (tan-cli#513 REVIEW) also passes
    `-SelectEmuBySN {serial}` in its own argv, belt-and-braces: this backend's
    `-AutoConnect 1` flag (unlike Flow D's argv, which carries no
    `-AutoConnect` and relies on its script's own leading `si SWD`/`connect`)
    means JLinkExe may start connecting to WHATEVER probe autoconnect finds
    before this script's first line is ever read, so the script-line selector
    alone is not provably ahead of the connect on every DLL version -- the
    argv selector is.

    The caller (`plan_swd_probe`) validates `serial` with the same
    `validate_identifier(..., destination=_JLINK_SERIAL_DESTINATION)` guard
    Flow D applies, before this function is ever reached; this function does
    NOT re-validate it -- unlike `artefact`, which this function DOES validate
    itself via `validate_commander_path` just below (`artefact` has no other
    single caller to trust; `serial` has exactly one, `plan_swd_probe`, which
    validates it unconditionally regardless of which arm ends up running).
    """
    validate_commander_path(artefact, "the flash artefact path")
    lines: list[str] = []
    if serial is not None:
        lines.append(f"SelectEmuBySN {serial}")
    lines += ["r", "halt"]
    # `is_raw_bin` reads the extension via `os.path.splitext` -- checked on
    # the UNQUOTED artefact, before `commander_path` may wrap it in `"..."`,
    # which would otherwise shift the extension off the string entirely.
    is_bin = is_raw_bin(artefact)
    artefact = commander_path(artefact)
    if is_bin:
        lines.append(f"loadbin {artefact}, {base}")
    else:
        lines.append(f"loadfile {artefact}")
    if do_reset:
        lines += ["r", "g"]
    lines.append("qc")
    return "\n".join(lines) + "\n"


def _resolve_jlink_device(fa: Any) -> str:
    """The J-Link device profile for `swd_probe`'s primary branch:
    `flash_args.jlink_device` when the manifest named one, else the inherited
    `_DEFAULT_JLINK_DEVICE` -- EXCEPT when `flash_args.target` is present,
    which REFUSES.

    **tan-cli#402.** `target` is the OpenOCD/pyOCD target name, read ~15 lines
    below at the point the FALLBACK branch builds its argv -- i.e. after the
    J-Link branch has already returned. A SoM preset declaring `interface:
    cmsis-dap, target: stm32h7x` and no `jlink_device` therefore reached a real
    J-Link write with the compiled-in GD32 profile, at the compiled-in
    `_DEFAULT_BASE`, on any host that merely happens to have SEGGER installed,
    with no diagnostic at all -- and `flash_plan.py`'s own `_DEFAULT_JLINK_
    DEVICE` note records that a shipped preset omits `jlink_device` today. tan
    cannot derive one spelling from the other (`stm32h7x` is not
    `STM32H747XI_M7`: an OpenOCD target names a family, a J-Link profile names
    a part), and guessing is exactly the silicon knowledge I-26 forbids -- so a
    manifest naming a DIFFERENT part gets a refusal, never a substitution.

    Scoped to `target`, and by KEY PRESENCE rather than by what it resolves to.
    `interface` names no part, so it does not trip this on its own; and a
    `target: ""` must not buy the silent GD32 default back on a quoting detail,
    the same reason `flow_d_available` reads presence. `flash_args` naming
    NEITHER key keeps the oracle's fallback untouched: that is what every
    recorded `swd_probe` parity fixture captures (`would run JLinkExe -device
    GD32G553MEY7TR ...`), so widening this to fire on an empty `flash_args`
    would diverge from the oracle on 13 fixtures at once.

    **DIVERGES from the shipped Rust oracle** (`builders.rs:243`), which has no
    such refusal and silently substitutes. Deliberate, and out of reach of
    `tests/parity/test_flash_oracle_parity.py`: no `swd_probe` case there
    declares `target` without ALSO forcing the openocd/pyocd path via
    `use_openocd`/`use_pyocd`, which skips this branch entirely.
    """
    device = fa_str_checked(fa, "jlink_device", False)
    if device is not None:
        # tan-cli#520 REVIEW round 2, MAJOR: validated HERE, at PLAN time --
        # not only inside `flow_d_preflight_script`'s own defensive repeat
        # (kept; see that call site's comment). Originally this was the ONE
        # choke point every caller of the J-Link branch passed through,
        # `--dry-run` included; round 3, finding 1 moved the primary check
        # to `plan_swd_probe`, ahead of the J-Link-vs-openocd/pyocd split
        # (this branch is reached only from the J-Link side of that split,
        # so the openocd/pyocd branch never called this function and never
        # got the guard). This call is now the redundant-but-harmless
        # defensive repeat for the J-Link branch's own choke point --
        # `validate_identifier` is pure, so re-running it here costs nothing
        # and protects a future caller of `_resolve_jlink_device` that does
        # not go through `plan_swd_probe`'s hoisted check.
        validate_identifier(device, "jlink_device")
        return device
    if _fa_has_key(fa, "target"):
        raise FlashPlanError(
            "swd_probe: flash_args.target is set but flash_args.jlink_device is "
            f"not -- refusing to fall back to the built-in {_DEFAULT_JLINK_DEVICE} "
            "profile for a SoM that named a different part. An OpenOCD/pyOCD "
            "target is not a J-Link device profile and tan does not guess one "
            "from the other. Add flash_args.jlink_device with the part-number "
            "J-Link profile, or set flash_args.use_openocd: true (or "
            "flash_args.use_pyocd: true) to take the path flash_args.target "
            "belongs to."
        )
    return _DEFAULT_JLINK_DEVICE


def plan_swd_probe(inp: FlashInputs, which: Callable[[str], bool]) -> FlashPlan:
    """`swd_probe`: J-Link (primary) / OpenOCD / pyOCD."""
    fa = inp.flash_args
    base = fa_str_checked(fa, "base", True)
    if base is not None:
        validate_address(base, "base")
    else:
        base = _DEFAULT_BASE
    do_reset = _default(fa_bool_checked(fa, "reset"), True)
    force_pyocd = _default(fa_bool_checked(fa, "use_pyocd"), False)
    force_openocd = _default(fa_bool_checked(fa, "use_openocd"), False)
    core = inp.core_id
    is_bin = is_raw_bin(inp.artefact)

    # tan-cli#513: `flash_args.jlink_serial` was accepted (it passes the #486
    # charset guard on every other backend) and then silently dropped here --
    # `swd_probe` emitted no `SelectEmuBySN` at all, so on a bench with more
    # than one J-Link this backend either failed to connect (JLinkExe with no
    # probe selected refuses to pick) or, if a connect got through anyway,
    # could not be trusted to have reached the intended board: `JLinkExe`
    # selects a probe ONLY by serial, and an OEM-cloned serial shared by two
    # different probes on the same host is a measured, not hypothetical,
    # bench condition (tan-cli#513).
    #
    # Resolved and validated HERE, unconditionally, BEFORE the J-Link-vs-
    # openocd/pyocd arm split just below -- not only inside the J-Link
    # branch (tan-cli#513 REVIEW, finding 2). Two gaps that split closed:
    # (1) a host-dependent refusal -- `--dry-run` always takes the J-Link arm
    # (see the `inp.dry_run` bypass just below), so a hostile `jlink_serial`
    # was refused under `--dry-run` and silently accepted-and-ignored on a
    # real run on an openocd-only host, the SAME manifest getting two
    # different verdicts depending on what happened to be on PATH; and
    # (2) `jlink_serial` reaching the openocd/pyocd arm at all with no
    # diagnostic -- openocd/pyocd have no probe-serial selection of their own
    # (`JLinkExe`'s `SelectEmuBySN` is a J-Link-only primitive), so silently
    # planning that arm anyway would be the exact accept-and-ignore shape
    # #513 fixed for the J-Link arm, just moved one branch over; that branch
    # refuses explicitly instead, below.
    #
    # `fa_str_checked`, not the tolerant `fa_str` (tan-cli#486 on Flow D): a
    # NUMERIC serial -- the canonical SEGGER spelling -- is a bare YAML
    # integer, which `fa_str` treats as "absent" and would silently drop the
    # line right back. `validate_identifier` closes the same newline-
    # injection hole `jlink_serial` is guarded against on Flow D
    # (`_JLINK_SERIAL_DESTINATION`) -- `serial` is interpolated verbatim into
    # `SelectEmuBySN {serial}`, a J-Link Commander script LINE, and (as of
    # tan-cli#513 REVIEW) a `-SelectEmuBySN` argv word too.
    serial = fa_str_checked(fa, "jlink_serial", False)
    if serial is not None:
        validate_identifier(serial, "jlink_serial", destination=_JLINK_SERIAL_DESTINATION)

    # tan-cli#519: the OpenOCD and pyOCD arms had NO probe-selection field at
    # all -- an ABSENT feature, not #513's accept-and-ignore shape (`serial`,
    # just above, is the ONLY selector this function used to read anywhere).
    # Deliberately TWO new fields, not one neutral one reused across all three
    # tools: `JLinkExe` selects by SERIAL only (`SelectEmuBySN`, above); OpenOCD
    # selects by USB PATH (`adapter usb location 3-4.4.3`); pyOCD has its own
    # `--uid`. A serial and a USB path are different identifiers, not two
    # spellings of one -- collapsing them would force this planner to guess
    # which shape a given manifest string names. Not hypothetical: on the
    # alplab-gw bench, two DIFFERENT attached probes (distinct USB paths,
    # distinct SW-DP IDs -- tan-cli#519) enumerate with the SAME OEM-cloned
    # serial `603000869` -- a serial cannot disambiguate them; a USB path can.
    #
    # Both charset-guarded HERE, unconditionally, ahead of the arm split --
    # mirroring `serial`'s own hoist just above, for the identical reason:
    # `--dry-run` always takes the J-Link arm (the `inp.dry_run` bypass a few
    # lines down), so a hostile value must be refused the same way under
    # `--dry-run` and on a real run, whichever arm the real run happens to take.
    #
    # `openocd_usb_location` is interpolated into an OpenOCD `-c` Tcl command
    # word (`adapter usb location <value>`, built below) -- `validate_identifier`
    # would reject the dots a real USB topology path uses (`3-4.4.3`), so this
    # is `validate_openocd_word`, the Jim-Tcl-metacharacter/control-character
    # guard #486 already gives OpenOCD's other `-c` words.
    openocd_usb_location = fa_str_checked(fa, "openocd_usb_location", False)
    if openocd_usb_location is not None:
        validate_openocd_word(
            openocd_usb_location, "flash_args.openocd_usb_location"
        )
        # tan-cli#519/#522 review round 3, MINOR: `validate_openocd_word`
        # guards the Jim Tcl/control-character charset only -- whitespace is
        # deliberately left alone there (a real artefact path needs it, see
        # that function's own docstring), so a WHITESPACE-ONLY value passed
        # straight through and reached OpenOCD as `adapter usb location {  }`,
        # an empty selector the tool would only reject at runtime, on the
        # bench. Refused at plan time instead, the same as an absent value
        # would be refused later by OpenOCD -- but before anything is spawned.
        if not openocd_usb_location.strip():
            raise FlashPlanError(
                f"flash_args.openocd_usb_location = {_quoted(openocd_usb_location)} "
                "is whitespace-only -- refusing to interpolate an empty USB-location "
                "selector into OpenOCD's `adapter usb location` command."
            )
    # `pyocd_uid` only ever reaches argv (`pyocd flash --uid <value> ...`,
    # below) -- no shell, no Tcl script -- but it is still an identifier-shaped
    # value from an untrusted manifest, so it gets the same `validate_identifier`
    # charset guard `jlink_serial` gets (widened for pyOCD's own optional
    # `<plugin>:<uid>` prefix -- see `validate_pyocd_uid`), not a bespoke
    # pass-through.
    pyocd_uid = fa_str_checked(fa, "pyocd_uid", False)
    if pyocd_uid is not None:
        validate_pyocd_uid(pyocd_uid, "pyocd_uid")

    # tan-cli#520 REVIEW round 3, finding 1: `flash_args.jlink_device`'s
    # charset guard used to live ONLY inside `_resolve_jlink_device`, which
    # is called from the J-Link branch alone -- the openocd/pyocd branch
    # below never calls it and never reads `jlink_device` at all, so a
    # hostile value there reached `ok_message`/argv unvalidated whenever a
    # real run happened to land on that branch (e.g. no J-Link binary on
    # PATH). `--dry-run` always forces the J-Link branch (the `inp.dry_run`
    # bypass a few lines down), so the SAME manifest disagreed by mode alone:
    # `--dry-run` refused, a real run on an openocd-only host reported `ok`.
    # Hoisted here, unconditionally, mirroring the `jlink_serial` guard just
    # above -- charset AND type (`fa_str_checked`, not merely `validate_
    # identifier`'s charset check), so it still doesn't change which branch
    # is taken, and a benign `jlink_device` still plans identically on the
    # openocd/pyocd branch -- but a hostile TYPE there (`true` / `-8` / a
    # list / a map) now raises `FlashPlanError` where it previously reached
    # `ok_message`/argv unvalidated (that branch has no `_DEFAULT_JLINK_
    # DEVICE` fallback to protect; it simply never read the key before).
    # Deliberate arm parity with the J-Link branch's own guard, not an
    # accident. `_resolve_jlink_device`'s own `validate_identifier` call is kept,
    # not deleted -- see its docstring -- as the defensive repeat for the
    # J-Link branch's own choke point, the same "kept" shape this file
    # already uses for `flow_d_preflight_script`'s read-device recheck.
    device_precheck = fa_str_checked(fa, "jlink_device", False)
    if device_precheck is not None:
        validate_identifier(device_precheck, "jlink_device")

    # tan-cli#520: `flash_args.expect_dpidr` shape-validated HERE,
    # unconditionally -- reusing Flow D's own `validate_flow_d_preflight_args`
    # (with `method="swd_probe"` so a refusal names the right backend) rather
    # than growing a second null/empty checker. `require_device_key=False`:
    # unlike Flow D, `swd_probe` has no SEPARATE preflight-only device field --
    # `flash_args.jlink_device` here ALREADY means the write's own `-device`
    # profile (`_resolve_jlink_device`, a few lines below), oracle-pinned on
    # its own (`tests/parity/…jlink-bin-artefact-uses-loadbin`, `jlink_device:
    # NRF_DUMMY` with no `expect_dpidr` at all) -- pairing it with
    # `expect_dpidr` the way Flow D's DIFFERENT `jlink_device` (a live-core
    # READ attach profile, distinct from Flow D's own WRITE profile
    # `jlink_flash_device`) is paired would retroactively demand a preflight
    # of every manifest that only ever set the write device, moving that
    # frozen fixture's answer -- measured: it did, before this was scoped to
    # `require_device_key=False`. `swd_probe`'s preflight is armed by
    # `expect_dpidr` ALONE; the read device is the same one already resolved
    # for the write (`_resolve_jlink_device`, passed through explicitly below
    # rather than re-read from a second `flash_args` key).
    #
    # `swd_probe` also has no confirm gate to hide a malformed `expect_dpidr`
    # behind (`planning_only` is always False here, unlike Flow D) -- so this
    # runs at PLAN time, same as Flow D's own call, so a malformed manifest
    # surfaces under `--dry-run` too, not only on a real write. The returned
    # tuple is discarded here on purpose: this call exists only to make a
    # null/empty `expect_dpidr` fail loudly now; the real, read-only preflight
    # probe (which needs a live JLinkExe) reruns `flow_d_preflight_script` --
    # and therefore this same validation -- from the same `flash_args`, at
    # write time, in `tan.commands.flash_cmd`.
    validate_flow_d_preflight_args(fa, method=SWD_PROBE_METHOD, require_device_key=False)

    # `--dry-run` is documented to bypass the required-tool PATH gate entirely;
    # without SOME bypass here this inner probe hard-failed a dry run on any
    # box without a probe tool installed, making `--dry-run` host-dependent
    # instead of a pure preview. Every `--dry-run` case that names NEITHER
    # `openocd_usb_location` NOR `pyocd_uid` keeps that unconditional bypass,
    # UNCHANGED (`_JLINK_BINARIES[0]`, no `which()` call at all) -- this is
    # also what every `tests/parity/test_flash_oracle_parity.py` `swd_probe`
    # dry-run case relies on for a REPLAY-host-independent answer (13 frozen
    # fixtures record `would run JLinkExe ...` regardless of what the replay
    # host's PATH happens to hold); widening the real `which()` probe below
    # to EVERY dry run would make those fixtures newly host-dependent on
    # whatever probe tools this box happens to have, for no reason those
    # cases care about.
    #
    # MAJOR 2 fix (tan-cli#519/#522 review), scoped to the two fields it
    # actually concerns: with NEITHER new field bypassed unconditionally as
    # above, a manifest naming `openocd_usb_location`/`pyocd_uid` still hit
    # the SAME unconditional J-Link assumption, so BOTH of this arm's
    # wrong-arm refusals (just below) fired on EVERY such preview, even on a
    # host that genuinely has openocd/pyocd and no J-Link at all, where a
    # REAL run takes neither refusal and reports `ok`. Those two fields could
    # then never be previewed at all unless the manifest also forced
    # `use_openocd`/`use_pyocd` -- and the refusal text ("this run is taking
    # the J-Link path") was flatly false for a preview that never spawns
    # anything. For exactly this pair of fields, `which()` is now consulted
    # for real, falling back to the synthetic J-Link default only when NO
    # probe tool at all is found (the one case that still needs a bypass) --
    # so a host that has openocd/pyocd for real previews the SAME arm a real
    # run on that host would take, and a `pyocd_uid`/`openocd_usb_location`
    # refusal (or lack of one) agrees between `--dry-run` and a real write on
    # the same machine.
    new_probe_selector_named = openocd_usb_location is not None or pyocd_uid is not None
    jlink: str | None = None
    if not (force_pyocd or force_openocd):
        if inp.dry_run and not new_probe_selector_named:
            jlink = _JLINK_BINARIES[0]
        else:
            jlink = next((n for n in _JLINK_BINARIES if which(n)), None)
            if jlink is None and inp.dry_run and not (which("openocd") or which("pyocd")):
                jlink = _JLINK_BINARIES[0]
    if jlink is not None:
        # tan-cli#519: the SAME wrong-arm refusal #513 gave `jlink_serial` on
        # the OTHER side of this split (below), mirrored here -- a manifest
        # naming an OpenOCD/pyOCD-only selector that then lands on the J-Link
        # arm must refuse, not silently drop it. `JLinkExe` has no USB-path or
        # `--uid` selector of its own; `jlink_serial` is its ONLY one.
        if openocd_usb_location is not None:
            raise FlashPlanError(
                "swd_probe: flash_args.openocd_usb_location is set, but this run is "
                "taking the J-Link path, which has no USB-location selector of its "
                "own -- OpenOCD's `adapter usb location` is an OpenOCD-only primitive "
                "(J-Link selects a probe by serial: flash_args.jlink_serial). Remove "
                "flash_args.openocd_usb_location, or ensure no SEGGER J-Link is on "
                "PATH and flash_args.use_openocd is set, so the manifest takes the "
                "path flash_args.openocd_usb_location belongs to."
            )
        if pyocd_uid is not None:
            raise FlashPlanError(
                "swd_probe: flash_args.pyocd_uid is set, but this run is taking the "
                "J-Link path, which has no --uid selector of its own -- pyOCD's "
                "--uid is a pyOCD-only primitive (J-Link selects a probe by serial: "
                "flash_args.jlink_serial). Remove flash_args.pyocd_uid, or ensure no "
                "SEGGER J-Link is on PATH and flash_args.use_pyocd is set, so the "
                "manifest takes the path flash_args.pyocd_uid belongs to."
            )
        device = _resolve_jlink_device(fa)
        speed = _default(fa_int_checked(fa, "jlink_speed"), _DEFAULT_JLINK_SPEED)
        return FlashPlan(
            argv=(
                jlink, "-device", device, "-if", "SWD", "-speed", str(speed),
                # tan-cli#513 REVIEW: `-SelectEmuBySN {serial}` in ARGV, not
                # only the Commander script's leading line. This arm's argv
                # carries `-AutoConnect 1` (unlike Flow D's argv, which has no
                # `-AutoConnect` and instead relies entirely on its script's
                # own leading `si SWD`/`connect` lines) -- with AutoConnect
                # armed, JLinkExe may start connecting to whatever probe
                # autoconnect finds during its own startup, BEFORE this
                # backend's script is ever read, so the script line alone is
                # not provably ahead of the connect on every DLL version. The
                # argv selector applies at parse time, ahead of any connect
                # the tool performs on its own -- order-independent within
                # argv itself, and documented SEGGER Commander behaviour, so
                # both are emitted: belt and braces, not a replacement.
                *(("-SelectEmuBySN", serial) if serial is not None else ()),
                "-AutoConnect", "1", "-ExitOnError", "1", "-NoGui", "1",
                "-CommanderScript",
            ),
            # The RESOLVED device, never a compiled-in SKU (#402). This string
            # is `data.entries[].message` -- the only per-entry human text in
            # the flash envelope, so it is what alp-sdk-vscode renders as the
            # outcome of a flash. It used to read `GD32G553 flashed via J-Link
            # ({device})`, i.e. it resolved the device from metadata and then
            # threw it away in the prose half, reporting every successful
            # `swd_probe` write as a GD32G553 whatever it had programmed.
            #
            # `@ {base}` only for a raw `.bin` (tan-cli#487, the address half
            # of #402's device fix): `jlink_commander_script` itself withholds
            # `base` from `loadfile` for an ELF/HEX (`base` is a load OFFSET,
            # meaningful only for a raw binary -- see that function and the
            # openocd/pyocd arm below), so asserting it here unconditionally
            # named an address the tool never received on every non-`.bin`
            # write -- either the compiled-in `_DEFAULT_BASE` or whatever the
            # manifest's `base` happened to be, neither of which this run used.
            ok_message=(
                f"swd_probe[{core}]: {device} flashed via J-Link @ {base}"
                if is_bin
                else f"swd_probe[{core}]: {device} flashed via J-Link"
            ),
            jlink_script=jlink_commander_script(inp.artefact, base, do_reset, serial),
            # tan-cli#520: the ALREADY-RESOLVED write device, carried through
            # so the caller's read-only DPIDR preflight (armed by
            # `flash_args.expect_dpidr` alone -- see the `require_device_key
            # =False` note above) can reuse it as the preflight's own connect
            # device, instead of a manifest needing to repeat a value under a
            # second key with a different meaning.
            preflight_device=device,
        )

    # tan-cli#513 REVIEW, finding 2: the openocd/pyocd arm has no probe-serial
    # selection of its own -- `JLinkExe`'s `SelectEmuBySN` is a J-Link-only
    # primitive, and neither `openocd`'s nor `pyocd`'s argv below reads
    # `jlink_serial` at all. Refusing here (rather than silently building the
    # plan anyway) is the same accept-and-ignore fix #513 applied to the
    # J-Link arm, extended to this one: a manifest that names a probe serial
    # and then runs on the arm that cannot honour it must say so, not report
    # `ok:true` having dropped the field. Checked unconditionally, ahead of
    # the `interface`/`target` requirement just below, so the diagnosis is
    # about the field this arm cannot use rather than a field it happens to
    # be missing.
    if serial is not None:
        raise FlashPlanError(
            "swd_probe: flash_args.jlink_serial is set, but this run is taking the "
            "openocd/pyocd path, which has no probe-serial selection of its own -- "
            "JLinkExe's SelectEmuBySN is a J-Link-only primitive. Remove "
            "flash_args.jlink_serial, or ensure a SEGGER J-Link is on PATH (and "
            "flash_args.use_openocd/use_pyocd are not forcing this path) so the "
            "manifest takes the primary path flash_args.jlink_serial belongs to."
        )

    # tan-cli#520, the same accept-and-ignore shape one field over: the
    # SW-DP IDR read-only preflight this arms is a JLinkExe-only primitive too
    # (there is no OpenOCD/pyOCD DPIDR probe wired here), so a manifest naming
    # `expect_dpidr` that then lands on this arm must refuse, not silently
    # drop the wrong-board guard. `_fa_has_key` (not the validated tuple
    # above) so a null/empty `expect_dpidr` -- already refused by the
    # `validate_flow_d_preflight_args` call above, on every arm -- is not
    # reported a second time with a different message; reaching here at all
    # means it was well-formed (present-and-valid, or absent). Unlike the
    # `jlink_serial` guard just above, this does NOT also check
    # `flash_args.jlink_device`: that key already means the WRITE's `-device`
    # profile on this arm's OWN sibling J-Link branch (`_resolve_jlink_
    # device`) and has no preflight-only meaning to protect here -- see the
    # `require_device_key=False` note on the `validate_flow_d_preflight_args`
    # call above.
    if _fa_has_key(fa, "expect_dpidr"):
        raise FlashPlanError(
            "swd_probe: flash_args.expect_dpidr is set, but this run is taking the "
            "openocd/pyocd path, which has no DPIDR preflight of its own -- the SW-DP "
            "IDR read is a JLinkExe-only primitive. Remove flash_args.expect_dpidr, or "
            "ensure a SEGGER J-Link is on PATH (and flash_args.use_openocd/use_pyocd "
            "are not forcing this path) so the manifest takes the primary path "
            "flash_args.expect_dpidr belongs to."
        )

    # tan-cli#519: mirrors the two wrong-arm refusals just above -- neither
    # OpenOCD's `adapter usb location` nor pyOCD's `--uid` is the OTHER tool's
    # primitive, so a manifest naming one and then landing on the other must
    # refuse rather than silently drop the selector (the same accept-and-ignore
    # shape #513 closed for `jlink_serial`, now closed on this side too).
    # `openocd`/`pyocd` are resolved HERE, ahead of the `interface`/`target`
    # requirement below (mirroring `serial`'s/`expect_dpidr`'s own hoist), so
    # the diagnosis is about the field this run cannot honour rather than a
    # field it happens to be missing -- and reused, unchanged, by the argv
    # build below instead of being computed a second time.
    #
    # tan-cli#519/#522 review round 3, MAJOR 2: `(inp.dry_run or which(...))`
    # treated EVERY `--dry-run` as "assume the tool is on PATH", unconditionally
    # -- the SAME unscoped bypass the J-Link resolution above had until this
    # round (`new_probe_selector_named`, defined there), just never scoped
    # here. Measured on a host with pyocd alone on PATH: a manifest naming
    # `openocd_usb_location` previewed a full `openocd -f ... -c 'adapter usb
    # location ...'` command line (`--dry-run` always saw `openocd = True`,
    # bypassed, so `chosen` was always `"openocd"`), while a REAL run on that
    # same host refuses (`which("openocd")` is `None` there, so `chosen` is
    # `"pyocd"`, and the wrong-arm refusal below fires) -- the preview an
    # operator runs BEFORE touching a board printed a command for a tool not
    # installed on that host. Scoped exactly like the J-Link resolution: keep
    # the unconditional bypass ONLY when neither new field is named (every
    # `would run JLinkExe` oracle-pinned dry-run fixture relies on reaching
    # THAT bypass first and never falls through to here at all); otherwise
    # consult `which()` for real, so `openocd`/`pyocd` -- and therefore
    # `chosen` and every refusal keyed off it -- agree between `--dry-run` and
    # a real run on the SAME host.
    if inp.dry_run and not new_probe_selector_named:
        openocd = not force_pyocd
        pyocd = not force_openocd
    else:
        openocd = not force_pyocd and bool(which("openocd"))
        pyocd = not force_openocd and bool(which("pyocd"))
    # BLOCKER fix: `openocd`/`pyocd` above test tool AVAILABILITY only. The arm
    # actually taken below is `if openocd: ... elif pyocd: ...` -- OpenOCD wins
    # whenever BOTH are on PATH, but a refusal keyed off availability alone
    # (`not pyocd`) stayed silent in exactly that case: with both tools present
    # and `pyocd_uid` set, `pyocd` was True so this guard never fired, and the
    # run then landed on the `if openocd:` branch below, which has no `--uid`
    # primitive at all -- silently dropping the probe selector on a
    # wrong-board write. This is the same accept-and-ignore defect #513/#519
    # exist to close, re-created by testing availability instead of the
    # resolved arm. `chosen` resolves the arm ONCE, with the exact same
    # if/elif precedence the argv-building code below uses, so every refusal
    # (and the argv build itself) shares one answer to "which arm is this run
    # actually taking" instead of each asking a differently-shaped question.
    chosen = "openocd" if openocd else "pyocd" if pyocd else None
    if openocd_usb_location is not None and chosen != "openocd":
        raise FlashPlanError(
            "swd_probe: flash_args.openocd_usb_location is set, but this run is not "
            "taking the OpenOCD path -- `adapter usb location` is an OpenOCD-only "
            "primitive (pyOCD selects a probe by flash_args.pyocd_uid, J-Link by "
            "flash_args.jlink_serial). Remove flash_args.openocd_usb_location, or "
            "ensure OpenOCD is on PATH and flash_args.use_pyocd is not forcing the "
            "pyOCD path, so the manifest takes the path "
            "flash_args.openocd_usb_location belongs to."
        )
    if pyocd_uid is not None and chosen != "pyocd":
        raise FlashPlanError(
            "swd_probe: flash_args.pyocd_uid is set, but this run is not taking the "
            "pyOCD path -- `--uid` is a pyOCD-only primitive (OpenOCD selects a probe "
            "by flash_args.openocd_usb_location, J-Link by flash_args.jlink_serial). "
            "Remove flash_args.pyocd_uid, or ensure pyOCD is on PATH and "
            "flash_args.use_openocd is not forcing the OpenOCD path, so the manifest "
            "takes the path flash_args.pyocd_uid belongs to."
        )

    interface = _default(fa_str_checked(fa, "interface", False), "")
    target = _default(fa_str_checked(fa, "target", False), "")
    if not interface or not target:
        raise FlashPlanError(
            "swd_probe: flash_args.interface and flash_args.target are required for "
            "the openocd/pyocd path (e.g. interface=cmsis-dap, target=gd32g553) -- "
            "or install SEGGER J-Link for the primary path."
        )
    validate_identifier(interface, "interface")
    validate_identifier(target, "target")
    if chosen == "openocd":
        validate_openocd_word(inp.artefact, "the flash artefact path")
        # `openocd_program_word` braces the artefact UNCONDITIONALLY -- see
        # its docstring (tan-cli#486 review, unconditional as of tan-cli#511)
        # for why a conditional predicate never actually preserved anything.
        # Safe here because `validate_openocd_word` just rejected every
        # character (`{`/`}` included) that could escape the brace.
        program = f"program {openocd_program_word(inp.artefact)} verify"
        if do_reset:
            program += " reset"
        # `base` is a load OFFSET, meaningful only for a raw `.bin`; ELF/HEX
        # carry their own addresses and OpenOCD's `program` proc adds a trailing
        # address to them, so passing it unconditionally shifts every section.
        program += f" exit {base}" if is_bin else " exit"
        tool = "openocd"
        argv = (
            "openocd", "-f", f"interface/{interface}.cfg",
            # tan-cli#519: the USB-path probe selector, emitted as its own `-c`
            # command BEFORE the target config/program -- OpenOCD processes
            # `-f`/`-c` in argv order, and `adapter usb location` must be set
            # before anything triggers a connect (the target config or the
            # `program` command below can). Absent entirely when the manifest
            # names no `openocd_usb_location`, matching every other optional
            # selector in this module (`jlink_serial`'s own `-SelectEmuBySN`).
            #
            # tan-cli#519 review, MINOR: braced via `openocd_program_word`,
            # the SAME unconditional bracing the artefact word gets just below
            # -- this was this module's only UNBRACED `-c` value interpolation,
            # and `validate_openocd_word` (already run on this value, above)
            # rejects Jim Tcl metacharacters and control characters but not
            # whitespace: `openocd_usb_location: "3-4.4.3 verify"` reached the
            # tool as `-c "adapter usb location 3-4.4.3 verify"`, a Tcl
            # command carrying TWO words where OpenOCD expects one (not an
            # injection -- every metacharacter is still refused -- but the
            # exact whitespace-splits-an-unquoted-word class
            # `openocd_program_word`'s own docstring names, and #511's answer
            # to that class was unconditional bracing, not a conditional
            # predicate).
            *(
                (
                    "-c",
                    f"adapter usb location {openocd_program_word(openocd_usb_location)}",
                )
                if openocd_usb_location is not None
                else ()
            ),
            "-f", f"target/{target}.cfg", "-c", program,
        )
    elif chosen == "pyocd":
        tool = "pyocd"
        parts = ["pyocd", "flash", "--target", target]
        # tan-cli#519: pyOCD's own probe-UID selector -- an argv pair, not a
        # Tcl word, so it needs no bracing/quoting the way the OpenOCD line
        # above does.
        if pyocd_uid is not None:
            parts += ["--uid", pyocd_uid]
        # pyOCD's --base-address is documented binary-only; passing it for an
        # ELF/HEX is meaningless at best and a wrong-address write at worst.
        if is_bin:
            parts += ["--base-address", base]
        parts.append(inp.artefact)
        argv = tuple(parts)
    else:
        raise FlashPlanError(
            "swd_probe: no flash tool found -- install SEGGER J-Link (preferred), "
            "or `openocd`, or `pyocd`."
        )
    # The same #402 fix as the J-Link line above, on the worse of the two: this
    # arm named `GD32G553` and echoed no device AT ALL, so nothing in the
    # message could contradict it. `target` is the only device identity this
    # path has -- it is literally what OpenOCD/pyOCD were pointed at -- so it
    # is what the line records, together with which of the two ran.
    #
    # `@ {base}` only for a raw `.bin` (tan-cli#487, the address half of #402):
    # the argv just above deliberately withholds `base` for an ELF/HEX on
    # BOTH tools (openocd's `program ... exit` with no trailing address;
    # pyOCD's `if is_bin` guard on `--base-address`), so asserting it here
    # unconditionally named an address neither tool ever received.
    return FlashPlan(
        argv=argv,
        ok_message=(
            f"swd_probe[{core}]: {target} flashed via {tool} @ {base}"
            if is_bin
            else f"swd_probe[{core}]: {target} flashed via {tool}"
        ),
    )


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
                f"{partition} via Flash Writer on {port} ({why})"
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
#: alone is metadata's statement that this silicon has one. `slot0_load_address` is
#: NOT an arming key -- it does not exist in any alp-sdk branch today (see
#: `plan_alif_mram_jlink`'s shape note) and, even once published, it only ever
#: selects the two-blob mramxip SHAPE, an ITCM-overflow exception, not whether
#: Flow D applies at all. Requiring it here would leave Flow D permanently
#: unarmed for every real AEN entry, which is the bug this comment replaces.
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
    no gate would catch. What it CAN ask is "did the SoM preset hand me a
    part-number J-Link profile for this slice?", because that arriving at all
    IS metadata's statement that this silicon has a J-Link MRAM loader.

    Consequence, stated plainly: with today's emit
    (`tan/planner/orchestrator.py::_slice_flash_recipe` returns
    `("zephyr_west_flash", {})` for every Zephyr slice) NO entry carries that
    key, so every AEN slice still takes Flow A. Arming Flow D is now a
    one-function change in THIS repo; it is deliberately NOT emulated here by
    sniffing the SKU.
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
        # headers into MRAM instead of the app image (tan-cli#311). Unlike
        # `plan_swd_probe`'s ELF/HEX fallback to `loadfile`, there is no
        # fallback here: `loadfile` ignores `slot0_load_address` entirely,
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
    expect_dpidr = fa_str_checked(fa, "expect_dpidr", False)
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
        # module. `swd_probe` is ungated for a reason that does not apply here
        # (it targets an external helper MCU's own flash).
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

    Shared by two backends, not two copies of this shape (tan-cli#520): the
    ORIGINAL caller is Flow D (`alif_mram_jlink`), and `plan_swd_probe`'s
    J-Link arm now calls this too, with `method="swd_probe"`, to give the GD32
    bridge write the identical wrong-board guard. `method` is interpolated
    ONLY into the refusal text -- it names which backend's `flash_args` a
    customer needs to fix, since both share one `flash_args` namespace and a
    manifest could otherwise not tell which entry a bare "flash_args.
    expect_dpidr" refusal was about. Defaults to `FLOW_D_METHOD` so every
    existing Flow D call site keeps its exact wording unchanged.

    `require_device_key` (default `True`, Flow D's own shape, unchanged):
    whether `expect_dpidr` must be PAIRED with a `flash_args.jlink_device`
    naming the live-core read attach profile, the way Flow D needs it
    (Flow D's `jlink_device` is DISTINCT from its write-time flash-algorithm
    profile, `jlink_flash_device`). `swd_probe` passes `False`: it has no
    separate preflight-only device field at all -- `flash_args.jlink_device`
    on THAT backend already means the write's own `-device` profile
    (`_resolve_jlink_device`), oracle-pinned on its own with no `expect_dpidr`
    anywhere near it (`tests/parity/…jlink-bin-artefact-uses-loadbin`).
    Pairing `swd_probe`'s `jlink_device` with `expect_dpidr` the way Flow D's
    DIFFERENT field is paired would retroactively demand a preflight of every
    manifest that only ever set the write device -- measured: it moved that
    frozen fixture's answer before this parameter existed. With
    `require_device_key=False`, `expect_dpidr` ALONE arms the preflight and
    the second returned slot is always `None`; the caller supplies its own
    read device (`plan_swd_probe` reuses the already-resolved write device via
    `FlashPlan.preflight_device`).

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
    expected = fa_str_checked(fa, "expect_dpidr", False)
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
    validate_identifier(read_device, "jlink_device")
    return expected, read_device


def flow_d_preflight_script(
    inp: FlashInputs, method: str = FLOW_D_METHOD, *, read_device: str | None = None
) -> tuple[str, str] | None:
    """The read-only DPIDR preflight script for a Flow D OR `swd_probe` J-Link
    plan (tan-cli#520 generalised this from Flow D-only): `(script,
    expected_id)`, or `None` when the preflight is not armed at all -- see
    `validate_flow_d_preflight_args` for every refusal shape, which this
    delegates to (passing `method` through unchanged) before building the
    script.

    `read_device` (tan-cli#520, default `None`): when the CALLER already has a
    connect device in hand -- `swd_probe` always does, via
    `FlashPlan.preflight_device`, the same device its write already resolved
    -- pass it here rather than requiring a second `flash_args` key. This
    flips `validate_flow_d_preflight_args`'s `require_device_key` to `False`,
    so `expect_dpidr` ALONE arms the preflight on that path. `None` (the
    default) keeps Flow D's own shape exactly: `jlink_device` PAIRED with
    `expect_dpidr` in `flash_args`, as it always has been.

    Run BEFORE any write, with a live-core attach profile (which the
    part-number one is not), so the caller can abort on the wrong board while
    the session is still read-only. The expected ID always comes from
    `flash_args.expect_dpidr`; the device name comes from either
    `flash_args.jlink_device` (Flow D shape) or `read_device` (`swd_probe`
    shape) depending on which was armed.
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
    SWD_PROBE_METHOD: BackendMeta(("JLinkExe", "JLink", "openocd", "pyocd"), plan_swd_probe),
    "zephyr_west_flash": BackendMeta(("west",), plan_zephyr_west_flash),
    "baremetal_cmake_flash": BackendMeta(("cmake",), plan_baremetal_cmake_flash),
    "yocto_wic_to_sd_or_emmc": BackendMeta(("bmaptool", "dd"), plan_yocto_wic),
    "yocto_wic": BackendMeta(("bmaptool", "dd"), plan_yocto_wic),
    "xspi_flashwriter": BackendMeta((), plan_xspi_flashwriter),
    FLOW_D_METHOD: BackendMeta(("JLinkExe", "JLink"), plan_alif_mram_jlink),
}


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
