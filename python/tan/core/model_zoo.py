# SPDX-License-Identifier: Apache-2.0
"""The model-zoo engine behind `tan model zoo` / `tan model add` (tan-cli#1286,
ADR-0028).

alp-sdk owns the DATA (`metadata/model_zoo/<id>.yaml`, shape
`metadata/schemas/model-zoo-v1.schema.json`, validated by that repo's own
`scripts/validate_metadata.py`); tan owns the ENGINE that reads it, so this
module consumes the manifests and invents no zoo format of its own. It
re-checks only what a consumer must not take on trust (shape of the fields it
reads, the `source` oneOf, path confinement of a bundled starter, https-only
and sha256-pinned downloads) and tolerates fields it does not know, so an
SDK that adds optional fields does not break an older tan.

No zoo directory means an SDK that predates the data: `load_zoo` raises
`ZooUnavailable`, which the command turns into a refusal naming
`MIN_SDK_COMMIT`.

`validated_soms` is a hardware claim and an EMPTY list means "nothing
confirmed", never "works everywhere": `filter_by_sku` matches no entry whose
list is empty.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The alp-sdk commit that first carried `metadata/model_zoo/` (alp-sdk#2542).
#: No released tag contains it yet; named in the `zoo-unavailable` refusal.
MIN_SDK_COMMIT = "7018515f9"

#: Model source suffixes `models[].source` accepts (board.schema.json).
MODEL_SUFFIXES = (".tflite", ".onnx", ".pte")

#: Hard ceiling on a downloaded model, so a hostile or broken URL cannot fill
#: the disk.
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024

_ID_RE = re.compile(r"[a-z][a-z0-9-]*")
_BUNDLED_RE = re.compile(r"starters/[A-Za-z0-9._-]+")
_SHA256_RE = re.compile(r"[a-f0-9]{64}")
_URL_RE = re.compile(r"https://[^\s/?#@]+/\S+")
_SKU_RE = re.compile(r"E1M-(AEN[3-8][0-9]{2}|V2N[0-9]{3}|V2M[0-9]{3}|NX9[0-9]{3})")


class ZooUnavailable(Exception):
    """The bound SDK has no `metadata/model_zoo/` directory."""


class ZooFetchError(Exception):
    """A model could not be fetched or did not match its pinned hash."""

    def __init__(self, message: str, *, mismatch: bool = False) -> None:
        super().__init__(message)
        self.mismatch = mismatch


@dataclass(frozen=True)
class ZooEntry:
    id: str
    kind: str
    task: str
    description: str
    license: str
    source: dict[str, str]
    validated_soms: tuple[str, ...]
    compile: dict[str, Any] | None
    example_app: str | None

    @property
    def suffix(self) -> str:
        """`.tflite`/`.onnx`/`.pte` -- from the bundled file name or the URL path."""
        raw = self.source.get("bundled") or self.source["url"].split("?", 1)[0]
        return Path(raw).suffix.lower()


def entry_as_dict(entry: ZooEntry) -> dict[str, Any]:
    """The `data.entries[]` row `--format json` carries for one entry."""
    return {
        "id": entry.id,
        "kind": entry.kind,
        "task": entry.task,
        "description": entry.description,
        "license": entry.license,
        "source": dict(entry.source),
        "validatedSoms": list(entry.validated_soms),
        "compile": sorted(entry.compile) if entry.compile else [],
        "exampleApp": entry.example_app,
    }


def _parse_entry(doc: Any, stem: str) -> ZooEntry:
    """A `ZooEntry`, or `ValueError` naming the first thing wrong."""
    if not isinstance(doc, dict):
        raise ValueError("not a YAML mapping")
    if doc.get("schema_version") != 1 or isinstance(doc.get("schema_version"), bool):
        raise ValueError(f"unsupported schema_version {doc.get('schema_version')!r} (this tan reads 1)")
    entry_id = doc.get("id")
    if not isinstance(entry_id, str) or not _ID_RE.fullmatch(entry_id):
        raise ValueError("`id` is missing or not lowercase kebab-case")
    if entry_id != stem:
        raise ValueError(f"`id` {entry_id!r} does not match the file name")
    kind = doc.get("kind")
    if kind not in ("model", "fixture"):
        raise ValueError("`kind` must be `model` or `fixture`")
    for key in ("task", "description", "license"):
        if not isinstance(doc.get(key), str) or not doc[key]:
            raise ValueError(f"`{key}` is missing")
    src = doc.get("source")
    if not isinstance(src, dict):
        raise ValueError("`source` is missing")
    if set(src) == {"bundled"}:
        bundled = src["bundled"]
        if not isinstance(bundled, str) or not _BUNDLED_RE.fullmatch(bundled) or ".." in bundled.split("/"):
            raise ValueError("`source.bundled` must be `starters/<file>`")
        source = {"bundled": bundled}
    elif set(src) == {"url", "sha256"}:
        url, digest = src["url"], src["sha256"]
        if not isinstance(url, str) or not _URL_RE.fullmatch(url):
            raise ValueError("`source.url` must be an https:// URL without credentials")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError("`source.sha256` must be 64 lowercase hex characters")
        source = {"url": url, "sha256": digest}
    else:
        raise ValueError("`source` must be exactly {bundled} or {url, sha256}")
    soms = doc.get("validated_soms")
    if not isinstance(soms, list) or not all(isinstance(s, str) and _SKU_RE.fullmatch(s) for s in soms):
        raise ValueError("`validated_soms` must be a list of SKUs")
    comp = doc.get("compile")
    if comp is not None and not isinstance(comp, dict):
        raise ValueError("`compile` must be a mapping")
    example_app = doc.get("example_app")
    if example_app is not None and not isinstance(example_app, str):
        raise ValueError("`example_app` must be a string")
    entry = ZooEntry(
        entry_id, kind, doc["task"], doc["description"], doc["license"], source,
        tuple(soms), comp or None, example_app,
    )
    if entry.suffix not in MODEL_SUFFIXES:
        raise ValueError(f"source file type {entry.suffix or '(none)'!r} is not one of {', '.join(MODEL_SUFFIXES)}")
    return entry


def load_zoo(zoo_dir: Path) -> tuple[list[ZooEntry], list[tuple[str, str]]]:
    """`(entries sorted by id, [(file name, problem), ...])` for every
    `*.yaml` under `zoo_dir`. A bad file is reported and skipped, never fatal
    to the rest. Raises `ZooUnavailable` when `zoo_dir` does not exist."""
    import yaml  # noqa: PLC0415 (declared dependency)

    if not zoo_dir.is_dir():
        raise ZooUnavailable(str(zoo_dir))
    entries: list[ZooEntry] = []
    problems: list[tuple[str, str]] = []
    for path in sorted(zoo_dir.glob("*.yaml")):
        try:
            entries.append(_parse_entry(yaml.safe_load(path.read_text(encoding="utf-8")), path.stem))
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as err:
            problems.append((path.name, str(err)))
    entries.sort(key=lambda e: e.id)
    return entries, problems


def filter_by_sku(entries: list[ZooEntry], sku: str) -> list[ZooEntry]:
    """Entries bench-validated on exactly `sku`; an empty `validated_soms`
    matches nothing (module doc)."""
    return [e for e in entries if sku in e.validated_soms]


def fetch_url(url: str) -> bytes:
    """GET `url` over https with tan's TLS trust context, capped at
    `MAX_DOWNLOAD_BYTES`. Raises `ZooFetchError`. `urllib` is imported here,
    not at module top, so a bare `tan` start-up does not load it."""
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    from tan.net import default_ssl_context  # noqa: PLC0415

    class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            if not newurl.lower().startswith("https://"):
                raise urllib.error.URLError(f"refusing a redirect to a non-https URL: {newurl}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    try:
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=default_ssl_context()), HttpsOnlyRedirect
        )
        req = urllib.request.Request(url, headers={"User-Agent": "tan-model-zoo"})
        chunks: list[bytes] = []
        total = 0
        with opener.open(req, timeout=60) as resp:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ZooFetchError(f"{url}: larger than {MAX_DOWNLOAD_BYTES} bytes")
                chunks.append(chunk)
        return b"".join(chunks)
    except ZooFetchError:
        raise
    except (OSError, ValueError) as err:  # URLError is an OSError
        raise ZooFetchError(f"{url}: {err}") from err


def fetch_source(
    entry: ZooEntry,
    zoo_dir: Path,
    *,
    reader: Callable[[str], bytes] = fetch_url,
) -> bytes:
    """The model bytes for `entry`: a bundled starter read from inside
    `zoo_dir/starters` (confined -- a symlink out is refused), or a download
    verified against the manifest's `sha256`. Raises `ZooFetchError`; a hash
    mismatch sets `.mismatch`."""
    bundled = entry.source.get("bundled")
    if bundled is not None:
        starters = (zoo_dir / "starters").resolve()
        path = (zoo_dir / bundled).resolve()
        if path.parent != starters or not path.is_file():
            raise ZooFetchError(f"bundled starter {bundled} is missing or outside {starters}")
        try:
            return path.read_bytes()
        except OSError as err:
            raise ZooFetchError(f"{path}: {err}") from err
    data = reader(entry.source["url"])
    got = hashlib.sha256(data).hexdigest()
    if got != entry.source["sha256"]:
        raise ZooFetchError(
            f"{entry.source['url']}: sha256 {got} does not match the manifest's {entry.source['sha256']}",
            mismatch=True,
        )
    return data
