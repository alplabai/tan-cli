# SPDX-License-Identifier: Apache-2.0
"""The Cortex-A/Cortex-M -> OS convention (issue #95), free of `tan.planner`'s
binding requirement.

`tan.planner.topology` owns the AUTHORITATIVE version of this rule --
`_default_os_from_core_type` there is what `validate._enforce_os_matches_core_class`
gates a real build on, and it re-exports the same names this module defines so
every existing planner caller/test is unaffected by this file's existence
(`tan.planner.topology._default_os_from_core_type` and `.CLASS_RUNTIMES` are
now this module's `default_os_from_core_type`/`CLASS_RUNTIMES`, imported not
redefined).

**Why this lives OUTSIDE `tan.planner` (tan-cli#870).** `tan.planner` is a
process-global-bound package: importing ANY of its submodules requires
`tan.planner_root.bind_sdk_root(<checkout>)` to have already run, because
`tan/planner/__init__.py` -> `tan/planner/paths.py` reads `sdk_root()` at
IMPORT time and the binding is refuse-to-rebind for the rest of the process
(`tan/planner_root.py`'s own docstring). That is the right contract for `tan
build`'s single big planning pass per process. It is the WRONG contract for
`tan presets`, which is exercised by dozens of small, independent unit tests
in the SAME pytest process, each against its own disposable synthetic SDK
root: the first such test to reach a `tan.planner` import permanently binds
the whole worker process to ITS throwaway root, and every later test in that
worker that needs a DIFFERENT (often the real, `ALP_SDK_ROOT`-derived) root
then fails outright -- measured while building tan-cli#870: routing
`presets_cmd`'s SoC-type/`allowed_os` lookups through `tan.planner.topology`
turned a clean local bound suite run (5 failed, 0 errors, the 5 pre-existing
terminal-width/breadth-layer artefacts) into 292 `PlannerRootError` cascades
across `tests/parity/test_planner_emit_parity.py`, in a completely unrelated
file, purely from `tests/commands/test_presets_command.py`'s own throwaway
fixtures winning the race to bind first in their xdist worker.

So the RULE (this file) lives somewhere binding-free, and both consumers
import it: `tan.planner.topology` for the planner's own enforcement (still
combined there with `_core_os_choices`'s bound-tree schema fallback, which a
real build-time metadata root may legitimately need), and
`tan.commands.presets_cmd` for the wizard's `allowedOs` (combined there with a
plain, no-fallback read of the GIVEN checkout's own `board.schema.json` --
`presets` never has a "different bound tree" to fall back to; it is reading
exactly one checkout throughout). Neither consumer re-derives the convention;
both call this module.
"""

from __future__ import annotations

from collections.abc import Iterable

#: The two class-determined OS runtimes (issue #95): Cortex-A -> Yocto
#: (Linux), Cortex-M -> Zephyr (RTOS). These follow the core class and are NOT
#: user-selectable (see `default_os_from_core_type`); `baremetal` (no-OS) and
#: `off` (disabled) are the only values a board.yaml may set explicitly.
CLASS_RUNTIMES = ("yocto", "zephyr")


def default_os_from_core_type(core_type: str) -> str:
    """Infer default OS from a SoC's `cores[].type`.

    Convention (codified across the SoM presets pre-2026-05-18):
        cortex-a*  ->  yocto
        cortex-m*  ->  zephyr
        anything else ->  off

    Used as the fallback when a SoM preset's `topology.<core>.os` is
    omitted (the field is optional in som-preset-v1.schema.json -- M-class
    cores default to Zephyr, A-class to Yocto).

    *core_type* is typed `str` but this is the SHARED entry point both
    binding-free callers (`presets_cmd.allowed_os_lookup`) and the planner
    (`topology._default_os_from_core_type`) reach with a value read off a
    SoC JSON that a schema-invalid `--sdk-root`/`--metadata-root` tree makes
    no promise about (tan-cli#957) -- `isinstance` here, not `str`-only trust
    in the annotation, is the backstop for every caller, present or future,
    that forwards a `cores[].type` value without its OWN guard. A non-string
    normalises to the same `""` unresolved sentinel a genuinely absent
    `type` already produces, rather than raising `AttributeError` on
    `.lower()`.
    """
    t = core_type.lower() if isinstance(core_type, str) else ""
    if t.startswith("cortex-a"):
        return "yocto"
    if t.startswith("cortex-m"):
        return "zephyr"
    return "off"


def cross_class_os(core_type: str) -> set[str]:
    """The class runtime a core may NOT be set to -- the OS of the *other*
    class. A Cortex-A can't run Zephyr; a Cortex-M can't run Yocto."""
    return set(CLASS_RUNTIMES) - {default_os_from_core_type(core_type)}


def allowed_os_for_core(core_type: str, choices: Iterable[str]) -> list[str]:
    """The `os:` values valid for a core: *choices* (the checkout's own `os:`
    enum) minus the cross-class OS -- e.g. Cortex-A -> `[yocto, baremetal,
    off]`, Cortex-M -> `[zephyr, baremetal, off]`.

    `core_type == ""` is the shared UNRESOLVED sentinel both callers of this
    function use when a core's `type` could not be determined (no matching
    SoC JSON on disk, no entry for this core id in it, a synthetic/partial
    checkout, ...) -- `tan.planner.topology.core_os_topology`'s
    `soc_types.get(core_id, "")` and `tan.commands.presets_cmd.read_soms`'s
    `core_types.get(c.id, "")` both feed it here unchanged. Degrades to `[]`
    rather than the plausible-but-wrong cross-class subtraction `cross_class_os("")`
    would otherwise produce (tan-cli#914): `default_os_from_core_type("")`
    falls through to `"off"`, so an UNRESOLVED type and a genuinely
    off-class core type were indistinguishable at this call, and the
    unresolved case silently offered Bare-metal for what may be a Cortex-M
    core -- the exact defect #870 exists to close, reproduced by the very
    field meant to prevent it. An unresolved type gets an unresolved (empty)
    answer instead, for both the wizard's `presets_cmd.allowed_os_lookup` and
    the build-time `topology._allowed_os_for_core` -- the one call this
    module exists to share, so the two can never drift back apart on this.

    A non-string *core_type* is ALSO treated as unresolved (tan-cli#957),
    not just falsy ones: `default_os_from_core_type`'s own `isinstance`
    guard already stops a truthy non-string (`7`, a populated list, `True`,
    ...) from raising here, but without this function's OWN `isinstance`
    check that value would still reach `cross_class_os` truthy, fall through
    `default_os_from_core_type`'s `"off"` default the same way `""` does,
    and hand back the identical `["baremetal", "off"]` plausible-but-wrong
    guess this docstring already rejects for the empty-string case --
    the crash-vs-leak choice tan-cli#957 found in `core_type_lookup` shows
    up here too, one level in, if this guard is skipped: a caller's own
    `isinstance` at its read site is not something this shared function may
    assume every present or future caller remembered to add.
    """
    if not isinstance(core_type, str) or not core_type:
        return []
    cross = cross_class_os(core_type)
    return [o for o in choices if o not in cross]


def infer_runtime_for_core_id(core_id: str) -> str:
    """Best-effort runtime for a core id with no OS given: an `a` followed by
    a digit at a WORD start (`a55_cluster`, `cluster_a32`, `A55`) runs
    `yocto`; everything else defaults to `zephyr`.

    Word starts are the string start and any position after `_`/`-`, so
    `data55` is NOT Cortex-A. Mirrors
    `tan_core::wizard::infer_runtime_for_core_id`, and KEEP IN SYNC with
    alp-sdk-vscode's ConfiguratorView `coreSiliconClass` (same word-start
    test; the one intentional difference is the fallback, since a CLI must
    pick a concrete runtime where the IDE can offer "unknown").

    A last resort: a topology that declares `board:`/`machine:` never reaches
    it. Lives here rather than beside either caller because it had TWO
    definitions -- `tan/core/scaffold.py` and `tan/commands/presets_cmd.py` --
    and `tan/core/bootstrap.py` reached OUTWARD past the core one to import
    the command's copy (tan-cli#408). One owner, in the leaf module that
    already answers "what OS class is this core", is what the duplicated
    docstrings each said they wanted.
    """
    lowered = core_id.lower()
    word_start = True
    for i, ch in enumerate(lowered):
        if word_start and ch == "a" and i + 1 < len(lowered) and lowered[i + 1].isdigit():
            return "yocto"
        word_start = ch in "_-"
    return "zephyr"
