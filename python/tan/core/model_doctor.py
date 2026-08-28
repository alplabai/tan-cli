# SPDX-License-Identifier: Apache-2.0
"""Pure logic for `tan model doctor`: one row per compiler-backend toolchain.

Under alp-sdk ADR-0028 the model engine (`tan.model.build`, the adapters
under `tan.model.adapters`) moved OUT of alp-sdk and into tan, so tan is now
the customer's only diagnostic surface for the NPU compiler toolchains
`tan model build` shells out to. When a compile silently produces nothing,
this is what turns "nothing happened" into "`dxcom` is not on PATH; it is
license-gated and Linux-only" -- that is the whole job.

**This module never spawns anything and never touches `metadata/`.** Every
fact it renders comes from an already-computed `available`/`version` pair the
caller (`tan.commands.model_cmd`) obtained by calling each registered
adapter's own `is_available()` -- `shutil.which(...)`/an env-var directory
check in every adapter today (`tan/model/adapters/{cpu,ethos_u,drpai,
deepx,executorch}.py`) -- and, for the one backend with a non-spawning
version probe (`ethos_u`'s `importlib.metadata` read), a version string. No
function here calls `subprocess.run`/`Popen`, imports an adapter, or reads a
SoM/SoC file: `doctor` must be safe to run on a completely broken host, and
"safe" here specifically means it cannot invoke a compiler as a side effect
of asking whether one is installed.

`data.optional[]` (`optional_row`) is the one row shape that is NOT a
"can tan compile for this backend" verdict: an unavailable OPTIONAL
prerequisite -- today the vendor vela `.ini` a licensed customer points
`ALP_VELA_CONFIG` at -- is not a fault and must not read as one. It is kept out
of `backends[]` for that reason; see `optional_row`.

`registry_backends` still reads the *shape* of `tan.model.build._ADAPTERS`
(which backends the registry actually declares, in what order) rather than
hard-coding the four-entry list a snapshot of that registry happens to be
today -- so a fifth adapter (or a genuinely distinct `"executorch"` backend,
should one ever replace `ExecutorchAdapter`'s current `backend == "cpu"`)
shows up here automatically instead of silently going unprobed.
"""
from __future__ import annotations

from dataclasses import dataclass

from tan.model.adapters import CompilerAdapter

#: The external binary/script each backend's adapter actually spawns to
#: compile a model -- `None` for a backend with no external tool at all.
#: `cpu`'s two adapters (`CpuAdapter`, `ExecutorchAdapter`) are both pure
#: byte passthroughs (`tan/model/adapters/cpu.py`,
#: `tan/model/adapters/executorch.py`) -- neither spawns anything, so `cpu`
#: reports no tool.
#:
#: `drpai`'s entry names the vendor TUTORIAL SCRIPT the adapter invokes
#: (`compile_onnx_model_quant.py`, `tan/model/adapters/drpai.py`'s own
#: `cmd = ["python3", str(script), ...]`), not the `python3` interpreter --
#: `python3` is present on essentially every host and naming it would tell a
#: customer nothing they can act on; the toolchain checkout the script lives
#: inside (`$ALP_DRPAI_TVM_HOME/tutorials/...`) is the thing that is
#: actually absent.
BACKEND_TOOLS: dict[str, str | None] = {
    "cpu": None,
    "ethos_u": "vela",
    "drpai": "compile_onnx_model_quant.py",
    "deepx_dxm1": "dxcom",
}

#: Actionable unavailability guidance, keyed by backend -- surfaced ONLY when
#: that backend's `available` is False (`backend_row` below). Each string is
#: derived from what that backend's adapter actually checks in
#: `is_available()`; never an install instruction this repo cannot support.
_UNAVAILABLE_REASONS: dict[str, str] = {
    # VelaAdapter.is_available(): shutil.which("vela") is not None
    # (tan/model/adapters/ethos_u.py). This repo's own pinned path is the
    # `model-compile` extra (`pyproject.toml`: `ethos-u-vela>=3.9`), which
    # carries the floor -- name it instead of a bare `pip install
    # ethos-u-vela` that could drift from that pin.
    "ethos_u": "vela not on PATH; pip install alp-tan[model-compile]",
    # deepx_dxm1's row is NOT `DeepxAdapter.is_available()` (see
    # `_deepx_dxm1_status` in `tan.commands.model_cmd`): that adapter method
    # ORs in a second arm -- `ALP_DEEPX_SDK_HOME` pointing at a directory --
    # that `DeepxAdapter.compile()` never reads (it always shells the bare
    # `dxcom` off PATH), so gating on it reported this row green on hosts
    # where the next `model build` immediately raised `FileNotFoundError:
    # 'dxcom'`. This default reason covers the case neither PATH nor the env
    # var is set; the ALP_DEEPX_SDK_HOME-set-but-no-dxcom case gets its own
    # caveat reason from `_deepx_dxm1_status`, passed through `backend_row`'s
    # `reason` override below. Its module doc: the dx-com wheel "is not
    # redistributable" and is "verified -- Linux x86_64, CPython 3.12" only,
    # so "license-gated, Linux-only" is what a customer needs to know before
    # chasing a `pip install` that does not exist for them.
    "deepx_dxm1": "dxcom not on PATH; license-gated, Linux-only",
    # drpai's row is likewise NOT the bare `DrpaiAdapter.is_available()`
    # (see `_drpai_status` in `tan.commands.model_cmd`): that adapter method
    # only checks `ALP_DRPAI_TVM_HOME` names a directory, never that the
    # vendor tutorial script `DrpaiAdapter.compile()` actually spawns
    # (`tutorials/compile_onnx_model_quant.py`) exists under it, so an
    # unpacked-but-unbuilt toolchain tree reported green here too. This
    # default reason covers the case the env var isn't set at all; the
    # var-set-but-script-missing case gets its own caveat reason from
    # `_drpai_status`. Its module doc: the toolchain is "large and
    # account-/source-gated ... so it is NOT bundled" -- there is no
    # pip/apt install to name here either.
    "drpai": (
        "ALP_DRPAI_TVM_HOME not set to a built rzv_drp-ai_tvm install; "
        "account-gated toolchain, not bundled"
    ),
}


@dataclass(frozen=True)
class BackendRow:
    """One backend's toolchain verdict -- the wire shape `tan model doctor`
    reports per backend under `data.backends[]`."""

    backend: str
    tool: str | None
    available: bool
    version: str | None
    reason: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "tool": self.tool,
            "available": self.available,
            "version": self.version,
            "reason": self.reason,
        }


def backend_row(
    backend: str, *, available: bool, version: str | None, reason: str | None = None
) -> BackendRow:
    """Assemble one row from an ALREADY-PROBED `available`/`version` pair.

    Pure -- no IO here at all, real or otherwise; the caller did the probing.
    `reason` is populated only when `available` is False (never a "why it
    works" note on a green row); `version` is discarded when `available` is
    False even if the caller passed one in, since an unavailable backend has
    no running toolchain for a version to describe.

    `reason` is an explicit override for `_UNAVAILABLE_REASONS[backend]`'s
    default, used when the caller's own probe (`_deepx_dxm1_status`/
    `_drpai_status` in `tan.commands.model_cmd`) found a more specific
    situation than "nothing at all is set" -- e.g. the toolchain env var IS
    set but the thing `compile()` actually needs under/via it is still
    absent. `None` (the default) falls back to the generic per-backend
    reason, same as before this parameter existed.
    """
    fallback_reason = reason if reason is not None else _UNAVAILABLE_REASONS.get(backend)
    return BackendRow(
        backend=backend,
        tool=BACKEND_TOOLS.get(backend),
        available=available,
        version=version if available else None,
        reason=None if available else fallback_reason,
    )


def optional_row(
    backend: str, *, tool: str, available: bool, reason: str | None
) -> BackendRow:
    """One OPTIONAL prerequisite's row -- the same five keys as `backend_row`,
    reported under `data.optional[]` rather than `data.backends[]`.

    A SEPARATE LIST, not a fifth backend row, because the two answer different
    questions and a consumer keyed on `backends[]` reads one row per backend.
    `available: false` there means "tan cannot compile for this backend at
    all"; here it means "this backend works, and a vendor-tuned enhancement is
    not installed". Rendering the second as the first would tell an unlicensed
    customer their toolchain is broken when it is complete and correct: without
    a vendor `.ini` vela uses Arm's own built-in system config, which is what
    the arena/SRAM figures tan reports already describe.

    `version` is always None: this is a data file, not a toolchain, and there
    is no version of it to probe without reading a customer's proprietary file.
    `reason` is dropped when available, exactly as `backend_row` drops it --
    and it is REQUIRED from the caller when it is not, because
    `_UNAVAILABLE_REASONS[backend]` answers the other question (`ethos_u`'s
    entry is about `vela` not being on PATH) and must never fall through to
    this row."""
    return BackendRow(
        backend=backend,
        tool=tool,
        available=available,
        version=None,
        reason=None if available else reason,
    )


def registry_backends(adapters: list[CompilerAdapter]) -> list[str]:
    """Distinct `backend` values across `adapters`, in first-seen registry
    order -- mirrors `build_model`'s own `by_backend` grouping
    (`tan/model/build.py`): a backend with MORE THAN ONE adapter (`cpu`
    today: `CpuAdapter` + `ExecutorchAdapter`, both `backend == "cpu"`)
    reports as one row, not two -- `doctor` answers "can tan compile for
    cpu", not "how many adapter classes register under cpu"."""
    seen: list[str] = []
    for adapter in adapters:
        if adapter.backend not in seen:
            seen.append(adapter.backend)
    return seen
