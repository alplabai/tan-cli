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
(`boot:`) project, and with an explicit `alp`/`linux`/`none` console.

`console: uart` is accepted (tan-cli#1374).  The #1374 bench (e1m-aen-evk-02,
AEN803, 2026-10-07) ran a UART5 shell RAM-run with ONLY `zephyr,flash = &itcm;`,
the code-partition delete, `CONFIG_USE_DT_CODE_PARTITION=n` and
`CONFIG_FLASH_LOAD_OFFSET=0x0` hand-added -- no `CONFIG_DCACHE=n`, so the
D-cache stayed at its board default.  A `uart` image therefore gets exactly
that retarget.  The D-cache hang recorded by #1360 concerns the RAM-console
path, which still gets `CONFIG_DCACHE=n` and the 16 KiB buffer (`ram`/`auto`).
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
#: `diagnostics.console:` values a Flow C image accepts: the RAM console (the
#: default; read over SWD) or the board UART (a RAM-run shell, tan-cli#1374).
_CONSOLE_VALUES = ("auto", "ram", "uart")


class LinkTargetError(OrchestratorError):
    """A `diagnostics.link:` refusal carrying its tan issue code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _console(diagnostics: dict[str, Any] | None) -> str:
    return str((diagnostics or {}).get("console") or "auto").strip().lower()


def uses_ram_console(diagnostics: dict[str, Any] | None) -> bool:
    """Whether an ITCM image uses the RAM console (`auto` is promoted to it):
    True for `auto`/`ram`, False for `uart` and anything else."""
    return _console(diagnostics) in ("auto", "ram")


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
    console = _console(project.diagnostics)
    if console not in _CONSOLE_VALUES:
        raise LinkTargetError(
            CONSOLE_CONFLICT_CODE,
            f"diagnostics.link: itcm supports the RAM console or the board "
            f"UART, but diagnostics.console is '{console}'. Use "
            f"`console: ram` (or leave it `auto`) to read the RAM console "
            f"buffer, or `console: uart` for a RAM-run UART shell.")


def apply_link_target(project: BoardProject) -> None:
    """`check_link_target`, then mark the retargeted slice (`Slice.link_target`)
    so the manifest/flash recipe can refuse an MRAM write of it."""
    check_link_target(project)
    for sl in project.cores.values():
        if applies_to(project.diagnostics, sl):
            sl.link_target = "itcm"


def itcm_conf(ram_console: bool = True) -> str:
    """Kconfig half of the retarget.

    The retarget lines are always emitted.  The bench-proven RAM-console
    settings (`CONFIG_DCACHE=n`, the 16 KiB buffer) only when `ram_console`;
    a `console: uart` image leaves both as the board defaults them.
    """
    retarget = (
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
    )
    if not ram_console:
        return retarget
    return retarget + (
        "# The E8 D-cache maintenance loop hangs on this silicon.\n"
        "CONFIG_DCACHE=n\n"
        "# ram_console_out() WRAPS: a buffer smaller than the app's output\n"
        "# reads back as two interleaved points in the run.\n"
        "CONFIG_RAM_CONSOLE_BUFFER_SIZE=16384\n"
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
    return [(CONF_NAME, itcm_conf(uses_ram_console(project.diagnostics))),
            (OVERLAY_NAME, itcm_overlay())]
