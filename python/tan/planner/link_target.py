# SPDX-License-Identifier: Apache-2.0
"""`diagnostics.link:` -- the image link target (tan-cli#1350, ADR-0026).

`link: itcm` turns a board.yaml into an AEN Flow C image: linked into the
M55-HE ITCM (base 0x0, global window 0x58000000) for `tan flash --ram`, which
loads the ELF there and reads `ram_console_buf`.  Without it the only route
was hand-copying alp-sdk's `scripts/bench/aen/aen-flowc-itcm.{conf,overlay}`
into the app.

The planner owns the retarget (ADR-0026): it renders the same two halves the
bench pair carries -- the Kconfig half and the devicetree half, BOTH required
(the conf alone still links into MRAM, the overlay alone leaves
`CONFIG_FLASH_LOAD_OFFSET` at the slot0 offset) -- as two extra per-slice
config artefacts layered AFTER the slice's `alp.conf`.

Pure: no IO, no SDK read.  HE-only by design: the knob retargets the M55-HE
slice only, and is refused when the project has no M55-HE app of its own (an
M55-HP-only project included), on any SKU but AEN801/AEN803, with a sysbuild
(`boot:`) project, and with an explicit non-RAM console (Flow C produces zero
UART bytes; the only observable is the RAM console buffer).
"""

from __future__ import annotations

from typing import Any

from .models import BoardProject, OrchestratorError, Slice
from .secure import emit_sysbuild_conf, emit_tfm_sysbuild_conf

UNSUPPORTED_CODE = "build.link-itcm-unsupported"
CONSOLE_CONFLICT_CODE = "build.link-itcm-console-conflict"

#: Build-dir file names of the two extra artefacts (siblings of `alp.conf`).
CONF_NAME = "alp-link-itcm.conf"
OVERLAY_NAME = "alp-link-itcm.overlay"

_HE_CORE_ID = "m55_he"
#: The SKUs the Flow C ITCM retarget is proven on (E8: AEN801 / AEN803, bench
#: 2026-07..10). Other Alif Ensemble SKUs have different ITCM maps / SoC
#: variants and are refused until someone proves them.
_PROVEN_SKUS = ("E1M-AEN801", "E1M-AEN803")
#: The schema enum already rejects anything else; the checks below that read
#: the value defensively exist for hand-built projects that skip the schema.
_LINK_VALUES = ("auto", "itcm")


class LinkTargetError(OrchestratorError):
    """A `diagnostics.link:` refusal carrying its tan issue code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def link_target(diagnostics: dict[str, Any] | None) -> str:
    """The `diagnostics.link:` value (`auto` when unset). The `.strip().lower()`
    is defensive only: the schema enum is lowercase, exact."""
    raw = (diagnostics or {}).get("link")
    return "auto" if raw is None else str(raw).strip().lower()


def is_itcm_link(diagnostics: dict[str, Any] | None) -> bool:
    return link_target(diagnostics) == "itcm"


def applies_to(diagnostics: dict[str, Any] | None, slice_: Slice) -> bool:
    """Whether `link: itcm` retargets THIS slice: the Zephyr M55-HE only.

    The knob is project-wide in board.yaml, but a Flow C image is one core's
    ITCM image: the other slices of the project (the AEN stock M55-HP shim,
    the A32 Linux image) build exactly as without it.
    """
    return (is_itcm_link(diagnostics) and slice_.os == "zephyr"
            and slice_.core_id == _HE_CORE_ID)


def check_link_target(project: BoardProject) -> None:
    """Refuse `link: itcm` anywhere but an AEN project with an M55-HE app.

    Raises `LinkTargetError`; returns None for `auto`/unset.
    """
    target = link_target(project.diagnostics)
    if target == "auto":
        return
    if target not in _LINK_VALUES:
        raise LinkTargetError(
            UNSUPPORTED_CODE,
            f"diagnostics.link: '{target}' is not one of "
            f"{', '.join(_LINK_VALUES)}.")
    if project.sku not in _PROVEN_SKUS:
        raise LinkTargetError(
            UNSUPPORTED_CODE,
            f"diagnostics.link: itcm is proven on the Alif Ensemble E8 "
            f"M55-HE ({', '.join(_PROVEN_SKUS)}); {project.sku} is not "
            f"supported. Remove `diagnostics.link:` for this SoM.")
    declared = (project.raw.get("cores") or {}) if isinstance(
        project.raw, dict) else {}
    he_decl = declared.get(_HE_CORE_ID)
    he = project.cores.get(_HE_CORE_ID)
    if (not isinstance(he_decl, dict) or not he_decl.get("app")
            or he is None or he.os != "zephyr"):
        others = sorted(
            c for c, d in declared.items()
            if c != _HE_CORE_ID and isinstance(d, dict) and d.get("app"))
        hint = (f" The project's own app is on {', '.join(others)}; the "
                f"M55-HP has a separate ITCM window (0x50000000) and is not "
                f"supported -- move the app to `cores.{_HE_CORE_ID}`."
                if others else "")
        raise LinkTargetError(
            UNSUPPORTED_CODE,
            f"diagnostics.link: itcm links the M55-HE ITCM image, but "
            f"`cores.{_HE_CORE_ID}` has no `os: zephyr` app of its own."
            f"{hint}")
    if emit_sysbuild_conf(project) or emit_tfm_sysbuild_conf(project):
        raise LinkTargetError(
            UNSUPPORTED_CODE,
            "diagnostics.link: itcm cannot be combined with a sysbuild "
            "project (`boot:` / `ota:` / TF-M) -- a RAM-run image has no "
            "MCUboot slot.")
    console = str(project.diagnostics.get("console") or "auto").strip().lower()
    if console not in ("auto", "ram"):
        raise LinkTargetError(
            CONSOLE_CONFLICT_CODE,
            f"diagnostics.link: itcm needs the RAM console (a Flow C "
            f"RAM-run produces zero UART bytes), but diagnostics.console is "
            f"'{console}'. Use `console: ram` or leave it `auto`.")


def apply_link_target(project: BoardProject) -> None:
    """`check_link_target`, then mark the retargeted slice (`Slice.link_target`)
    so the manifest/flash recipe can refuse an MRAM write of it."""
    check_link_target(project)
    for sl in project.cores.values():
        if applies_to(project.diagnostics, sl):
            sl.link_target = "itcm"


#: Floor for the Flow C RAM-console buffer (bytes).
ITCM_RAM_CONSOLE_MIN_SIZE = 16384


def itcm_conf(app_ram_console_size: int = 0) -> str:
    """Kconfig half of the retarget (+ the bench-proven RAM-run settings).

    `app_ram_console_size` is the `CONFIG_RAM_CONSOLE_BUFFER_SIZE` the app's own
    `prj.conf` sets (0 when none): this conf is layered AFTER it, so a bare
    16384 would shrink a larger app-set buffer and wrap its console
    (tan-cli#1401). The size is max(16384, app value).
    """
    size = max(ITCM_RAM_CONSOLE_MIN_SIZE, app_ram_console_size)
    return (
        "# Flow C (AEN M55-HE ITCM RAM-run) link retarget -- board.yaml\n"
        "# `diagnostics.link: itcm`.  NOT for an image you will flash to MRAM:\n"
        "# it links at 0x0.  Kconfig half; the devicetree half is\n"
        f"# {OVERLAY_NAME} -- BOTH are required.\n"
        "#\n"
        "# Stop deriving the link offset from the DT code-partition and force\n"
        "# the ITCM base (undoes the AEN board defconfig's slot0 default and a\n"
        "# literal CONFIG_FLASH_LOAD_OFFSET an app may hard-code).\n"
        "CONFIG_USE_DT_CODE_PARTITION=n\n"
        "CONFIG_FLASH_LOAD_OFFSET=0x0\n"
        "# The E8 D-cache maintenance loop hangs on this silicon.\n"
        "CONFIG_DCACHE=n\n"
        "# ram_console_out() WRAPS: a buffer smaller than the app's output\n"
        "# reads back as two interleaved points in the run.\n"
        f"CONFIG_RAM_CONSOLE_BUFFER_SIZE={size}\n"
    )


def itcm_overlay() -> str:
    """Devicetree half: retarget the ROM region to ITCM."""
    return (
        "/*\n"
        " * Flow C (AEN M55-HE ITCM RAM-run) devicetree retarget -- board.yaml\n"
        " * `diagnostics.link: itcm`.  Moves zephyr,flash to the ITCM and drops\n"
        " * the code-partition so the image links at 0x0.  Kconfig half:\n"
        f" * {CONF_NAME} -- BOTH are required.  NOT for an MRAM-flashed image.\n"
        " */\n"
        "\n"
        "/ {\n"
        "\tchosen {\n"
        "\t\t/* path-ref form: the pointer form makes FLASH_SIZE=0 and\n"
        "\t\t * overflows the link */\n"
        "\t\tzephyr,flash = &itcm;\n"
        "\t\t/delete-property/ zephyr,code-partition;\n"
        "\t};\n"
        "};\n"
    )


def extra_config_artefacts(project: BoardProject,
                           slice_: Slice) -> list[tuple[str, str]]:
    """`(name, contents)` of the extra per-slice artefacts, in layer order.

    Empty unless `link: itcm` and this is the Zephyr M55-HE slice.
    """
    if not applies_to(project.diagnostics, slice_):
        return []
    # Lazy: kconfig imports this module (the shared reader of the app's prj.conf).
    from .kconfig import _app_ram_console_size  # noqa: PLC0415

    return [(CONF_NAME, itcm_conf(_app_ram_console_size(project, slice_))),
            (OVERLAY_NAME, itcm_overlay())]
