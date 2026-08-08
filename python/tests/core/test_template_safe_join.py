# SPDX-License-Identifier: Apache-2.0
"""`tan.planner.template`'s catalog-driven READS are contained to the SDK
root (tan-cli#494 defect 6).

`_rendered_bytes`/`render_to_envelope` used to join `record["example"]` and
every `files.user_owned` entry straight onto the bound SDK root with a raw
`Path.__truediv__`, with no schema validation on `load_catalog`'s result (the
schema's own `example`/`files.user_owned` path patterns are declared but never
enforced here) and no containment check -- so a tampered
`metadata/templates/catalog-v1.json` could make `--emit scaffold` read (and,
through `emit_scaffold`, print to stdout) an arbitrary file the `tan` process
can see: `example: ../outside`, a `files.user_owned` entry of
`../../../../outside/secret.txt`, or an absolute `/etc/hostname`. alp-sdk
closed the identical hole in its own `scripts/alp_template.py` with
`_safe_join` (cb7f64ae, alp-sdk#1126); this port never picked the fix up --
see `tests/gates/test_planner_relocation_freshness.py`'s `HAND_PORT_HASHES`
comment, which names this exact gap.

Requires a bound alp-sdk checkout (`ALP_SDK_ROOT`/`ALP_SDK_PARITY_ROOT`), same
reason as every other `tan.planner`-importing test in this tree: the package's
`__init__` eagerly reads real `metadata/registries/*` at import time, so
`tan.planner.template` cannot even be imported unbound. Skipped, loudly,
without one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _sdk_root() -> Path | None:
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


SDK = _sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner can bind "
    "a root and import (same requirement as the parity suite)",
)


@pytest.fixture(scope="module")
def render_to_envelope():
    from tan.planner_root import bind_sdk_root

    assert SDK is not None
    bind_sdk_root(SDK)
    from tan.planner.template import render_to_envelope as fn

    return fn


@pytest.fixture
def tampered_catalog(tmp_path: Path) -> Path:
    """A copy of the REAL catalog with three extra records, each tampering
    exactly one field the way tan-cli#494's own report demonstrated: an
    absolute `files.user_owned` entry, a `../`-relative one, and a `../`
    `example`. Based on the real `minimal` record so every OTHER field
    (`supported.som_skus`, `cores`, `parameters`) stays realistic."""
    assert SDK is not None
    doc = json.loads((SDK / "metadata" / "templates" / "catalog-v1.json").read_text())
    minimal = next(t for t in doc["templates"] if t["id"] == "minimal")

    def _variant(new_id: str, *, example: str | None = None, user_owned: list[str]) -> dict:
        rec = dict(minimal)
        rec["id"] = new_id
        if example is not None:
            rec["example"] = example
        rec["files"] = dict(minimal["files"])
        rec["files"]["user_owned"] = user_owned
        return rec

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET private key material\n")
    (outside / "board.yaml").write_text(
        "som:\n  sku: E1M-AEN801\npreset: e1m-evk\ncores:\n  m55_hp:\n    app: ./src\n"
    )
    # A generous, depth-independent `..` run: POSIX collapses any excess at
    # the filesystem root, so this reaches `/etc/hostname` regardless of how
    # deep the bound SDK checkout happens to be on this machine.
    escape_to_etc_hostname = "/".join([".."] * 32) + "/etc/hostname"

    doc["templates"] = [
        *doc["templates"],
        _variant("evil-abs", user_owned=["/etc/hostname"]),
        _variant("evil-rel", user_owned=[escape_to_etc_hostname]),
        _variant("evil-example", example=str(outside), user_owned=["secret.txt"]),
    ]
    catalog_path = tmp_path / "catalog-v1.json"
    catalog_path.write_text(json.dumps(doc))
    return catalog_path


@pytest.mark.parametrize("template_id", ["evil-abs", "evil-rel", "evil-example"])
def test_a_tampered_catalog_entry_cannot_read_outside_the_sdk_root(
    render_to_envelope, tampered_catalog, template_id
):
    from tan.planner.template import TemplateError

    with pytest.raises(TemplateError, match="resolves outside"):
        render_to_envelope(
            template_id, "E1M-AEN801", catalog_path=tampered_catalog, base_dir=SDK
        )


def test_an_untampered_template_still_renders(render_to_envelope):
    """The containment guard must be a no-op for every real, in-bounds
    catalog entry -- this is the regression the fix itself could introduce."""
    out = render_to_envelope("minimal", "E1M-AEN801", base_dir=SDK)
    paths = {p for p, _ in out}
    assert paths == {"CMakeLists.txt", "README.md", "board.yaml", "prj.conf", "src/main.c"}
