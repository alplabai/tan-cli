# SPDX-License-Identifier: Apache-2.0
"""Alp Platform release manifest (`alp-platform-v1`): version grammar, schema
validation, workspace-pin lookup and the `tan doctor` report. Pure -- no
network, no writes (tan-cli#1571; cross-repo contract alp-sdk-internal#78).

A platform release is a dated, bench-validated set of component versions. This
module only READS and REPORTS: nothing here (or anywhere in `tan platform`)
flashes firmware, and the bridge firmware versions are informational.
"""
from __future__ import annotations

import datetime
import hashlib
import re
import reprlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPR = reprlib.Repr()
_REPR.maxstring = 80
_REPR.maxother = 80

SCHEMA = "alp-platform-v1"
REPO = "alplabai/alp-platform"
PIN_REL = (".alp", "platform.yaml")
#: The five owned components, in the order `data.platform.components` reports.
COMPONENTS = (
    "alp-sdk",
    "tan-cli",
    "alp-sdk-vscode",
    "gd32-bridge-firmware",
    "cc3501e-bridge-firmware",
)
_TOP_KEYS = frozenset(
    {"schema", "platform", "released", "components", "recorded", "validated_hw", "bench_evidence"}
)
_REQUIRED_TOP = _TOP_KEYS - {"bench_evidence"}
_RECORDED_KEYS = ("zephyr", "hal_alif", "yocto", "renesas_bsp", "renesas_ai_sdk")

_VERSION_RE = re.compile(r"^(\d{2})\.([1-9]|1[0-2])\.(0|[1-9]\d*)(-rc[1-9]\d*)?$")
_PADDED_RE = re.compile(r"^(\d{2})\.0(\d)(\.|$)")
_LINE_RE = re.compile(r"^(\d{2})\.([1-9]|1[0-2])$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
_HW_REV_RE = re.compile(r"^[0-9]{4}-r[1-9][0-9]*$")
_GPE_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


class PlatformError(ValueError):
    """A version string or manifest the contract refuses. The message is the
    user-facing text."""


@dataclass(frozen=True)
class PlatformVersion:
    yy: int
    month: int
    micro: int
    rc: int | None = None

    @property
    def line(self) -> str:
        return f"{self.yy:02d}.{self.month}"

    def __str__(self) -> str:
        rc = f"-rc{self.rc}" if self.rc is not None else ""
        return f"{self.line}.{self.micro}{rc}"

    def sort_key(self) -> tuple[int, int, int, float]:
        # `-rcN` sorts before the same GA, rcN by N.
        return (self.yy, self.month, self.micro, float("inf") if self.rc is None else self.rc)

    def __lt__(self, other: PlatformVersion) -> bool:
        return self.sort_key() < other.sort_key()


def parse_version(text: str) -> PlatformVersion:
    """`YY.M.MICRO[-rcN]`, month never zero-padded. A padded month is refused
    with the unpadded spelling named, never silently normalised."""
    m = _VERSION_RE.match(text)
    if m is None:
        padded = _PADDED_RE.match(text)
        if padded is not None:
            fixed = f"{padded.group(1)}.{padded.group(2)}{text[padded.end(2):]}"
            raise PlatformError(
                f"platform version {_show(text)} has a zero-padded month; "
                f"write it unpadded: {fixed!r}."
            )
        raise PlatformError(
            f"platform version {_show(text)} is not YY.M.MICRO[-rcN] "
            "(two-digit year, month 1-12 without zero padding, e.g. 27.2.0)."
        )
    rc = int(m.group(4)[3:]) if m.group(4) else None
    return PlatformVersion(int(m.group(1)), int(m.group(2)), int(m.group(3)), rc)


def parse_line(text: str) -> str | None:
    """`YY.M` -> itself when it is a valid bare line, else `None`."""
    return text if _LINE_RE.match(text) else None


def parse_gpe(text: str, what: str) -> tuple[int, ...]:
    if not isinstance(text, str) or not _GPE_RE.match(text):
        raise PlatformError(f"{what} {_show(text)} must be four dot-separated integers (A.B.C.D).")
    return tuple(int(p) for p in text.split("."))


def release_asset_url(version: PlatformVersion) -> str:
    return f"https://github.com/{REPO}/releases/download/platform-v{version}/platform.yaml"


def latest_ga_for_line(line: str, release_tags: list[str]) -> PlatformVersion | None:
    """Highest GA MICRO of `line` among `platform-v<YY.M>.<n>` tags; rc tags
    and anything off-grammar are ignored."""
    best: PlatformVersion | None = None
    for tag in release_tags:
        if not tag.startswith("platform-v"):
            continue
        try:
            v = parse_version(tag[len("platform-v") :])
        except PlatformError:
            continue
        if v.rc is None and v.line == line and (best is None or best < v):
            best = v
    return best


@dataclass(frozen=True)
class Manifest:
    version: PlatformVersion
    components: dict[str, dict[str, Any]]
    data: dict[str, Any]

    def pinned(self, name: str) -> str:
        return str(self.components[name]["version"])


def validate_manifest(data: Any) -> Manifest:
    """Validate a parsed `platform.yaml` against `alp-platform-v1`. Unknown
    TOP-LEVEL keys are refused; unknown keys inside `components` entries are
    ignored (additive per-component fields stay compatible)."""
    if not isinstance(data, dict):
        raise PlatformError("platform manifest must be a YAML mapping.")
    schema = data.get("schema")
    if schema != SCHEMA:
        raise PlatformError(
            f"unsupported platform manifest schema {_show(schema)}; this tan supports {SCHEMA!r}."
        )
    unknown = sorted(str(k) for k in data if k not in _TOP_KEYS)
    if unknown:
        raise PlatformError(f"platform manifest has unknown top-level key(s): {', '.join(unknown)}.")
    missing = sorted(_REQUIRED_TOP - set(data))
    if missing:
        raise PlatformError(f"platform manifest is missing required key(s): {', '.join(missing)}.")
    platform = data["platform"]
    if not isinstance(platform, str):
        raise PlatformError(f"platform must be a quoted-safe string like 27.2.0, got {_show(platform)}.")
    version = parse_version(platform)
    released = data["released"]
    if isinstance(released, datetime.date):
        pass
    elif not (isinstance(released, str) and _is_iso_date(released)):
        raise PlatformError(f"released must be an ISO date (YYYY-MM-DD), got {_show(released)}.")
    comps = data["components"]
    _check_components(comps)
    if not isinstance(data["recorded"], dict) or not all(k in data["recorded"] for k in _RECORDED_KEYS):
        raise PlatformError(f"recorded must be a mapping with: {', '.join(_RECORDED_KEYS)}.")
    if not isinstance(data["validated_hw"], list):
        raise PlatformError("validated_hw must be a list (may be empty).")
    for hw in data["validated_hw"]:
        rev = hw.get("rev") if isinstance(hw, dict) else None
        if not isinstance(hw.get("som") if isinstance(hw, dict) else None, str) or not (
            isinstance(rev, str) and _HW_REV_RE.match(rev)
        ):
            raise PlatformError(
                f"validated_hw entry {_show(hw)} needs a som and a rev like '2626-r2' "
                "(<board_datecode>-<rev key>, as in the module EEPROM hw_rev)."
            )
    bench = data.get("bench_evidence")
    if bench is not None and not isinstance(bench, str):
        raise PlatformError("bench_evidence must be a URL string or null.")
    return Manifest(version, comps, data)


def _check_components(comps: Any) -> None:
    if not isinstance(comps, dict) or set(comps) != set(COMPONENTS):
        raise PlatformError(f"components must have exactly these keys: {', '.join(COMPONENTS)}.")
    for name in COMPONENTS:
        entry = comps[name]
        ver = entry.get("version") if isinstance(entry, dict) else None
        if not isinstance(ver, str) or not _SEMVER_RE.match(ver):
            raise PlatformError(
                f"components.{name}.version {_show(ver)} must be a SemVer string X.Y.Z[-pre] "
                "with no leading 'v'."
            )
    cc = comps["cc3501e-bridge-firmware"]
    stamp = parse_gpe(cc.get("gpe_stamp"), "components.cc3501e-bridge-firmware.gpe_stamp")
    floor = parse_gpe(cc.get("gpe_floor"), "components.cc3501e-bridge-firmware.gpe_floor")
    if stamp <= floor:
        raise PlatformError(
            f"cc3501e-bridge-firmware gpe_stamp {cc['gpe_stamp']} is at or below gpe_floor "
            f"{cc['gpe_floor']}: flashing a GPE stamp at or below the floor permanently "
            "bricks the CC3501E bridge. Refusing this manifest."
        )


def _is_iso_date(text: str) -> bool:
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))


#: A real manifest is about 1 KiB; anything far larger is not one.
MAX_MANIFEST_BYTES = 64 * 1024


def _show(value: Any) -> str:
    """Bounded repr for error messages: manifest values come from the network,
    and an unbounded repr of a crafted value can be enormous."""
    return _REPR.repr(value)


def load_manifest_text(text: str) -> Manifest:
    import yaml  # noqa: PLC0415

    if len(text.encode("utf-8", "replace")) > MAX_MANIFEST_BYTES:
        raise PlatformError(f"platform manifest is larger than {MAX_MANIFEST_BYTES} bytes; refusing it.")

    class _NoAliasLoader(yaml.SafeLoader):
        """A manifest has no use for YAML anchors/aliases; refusing them
        removes alias-amplification ("billion laughs") inputs outright."""

        def compose_node(self, parent, index):  # type: ignore[override]
            if self.check_event(yaml.AliasEvent):
                raise PlatformError("platform manifest uses a YAML alias; aliases are not allowed.")
            return super().compose_node(parent, index)

    try:
        data = yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 -- SafeLoader subclass
    except yaml.YAMLError as err:
        raise PlatformError(f"platform manifest is not valid YAML: {err}") from err
    return validate_manifest(data)


# -- workspace pin (contract section 4) --------------------------------------


def pin_dir_for_sdk(sdk_root: str | Path) -> Path | None:
    """`<west topdir>/.alp`: walk from the SDK root up to the first directory
    holding `.west/`. `None` when there is none."""
    root = Path(sdk_root).resolve()
    for d in (root, *root.parents):
        if (d / ".west").is_dir():
            return d / PIN_REL[0]
    return None


def pin_path_for_sdk(sdk_root: str | Path) -> Path:
    """Read-side lookup: the west topdir's pin, else `<sdk root>/.alp/platform.yaml`."""
    d = pin_dir_for_sdk(sdk_root)
    return (d if d is not None else Path(sdk_root) / PIN_REL[0]) / PIN_REL[1]


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def same_version(a: str, b: str) -> bool:
    """Leading `v` ignored; the full string, `-rc`/pre suffix included, compared."""
    return a.removeprefix("v") == b.removeprefix("v")


def platform_report(
    pin_bytes: bytes | None, installed: dict[str, str | None]
) -> tuple[dict[str, Any] | None, str, str]:
    """`(data.platform, check status, check detail)` for `tan doctor`
    (contract section 6). `pin_bytes` is the pin file's content, `None` when
    there is no pin; an unreadable/invalid pin is a `fail` with `data` null."""
    if pin_bytes is None:
        return None, "pass", "no platform pinned"
    try:
        manifest = load_manifest_text(pin_bytes.decode("utf-8"))
    except (PlatformError, UnicodeDecodeError) as err:
        return None, "fail", f"the workspace platform pin is invalid: {err}"
    rows = []
    for name in COMPONENTS:
        pinned = manifest.pinned(name)
        have = installed.get(name)
        status = "unknown" if have is None else ("match" if same_version(pinned, have) else "mismatch")
        rows.append({"name": name, "pinned": pinned, "installed": have, "status": status})
    data = {
        "version": str(manifest.version),
        "line": manifest.version.line,
        "manifestSha256": sha256_hex(pin_bytes),
        "components": rows,
    }
    off = [f"{r['name']} {r['installed']} != {r['pinned']}" for r in rows if r["status"] == "mismatch"]
    if off:
        return data, "warn", f"Alp Platform {data['version']}; off-manifest: " + "; ".join(off) + "."
    return data, "pass", f"Alp Platform {data['version']}; no known component is off-manifest."

