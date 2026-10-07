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
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
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

#: Total wall-clock budget for one download.
MAX_DOWNLOAD_SECONDS = 300.0

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


def _check_compile(comp: Any) -> None:
    """`compile` must be `{backend: {key: str | int | [str|int, ...]}}` -- the
    only shape `models[].compile` takes. Checked shallowly (no recursion), so a
    self-referencing YAML alias is rejected here as a plain type error."""
    if comp is None:
        return
    if not isinstance(comp, dict):
        raise ValueError("`compile` must be a mapping")

    def scalar(v: Any) -> bool:
        return isinstance(v, (str, int)) and not isinstance(v, bool)

    for backend, opts in comp.items():
        if not isinstance(backend, str) or not isinstance(opts, dict) or not opts:
            raise ValueError("`compile` must map backend ids to non-empty mappings")
        for key, value in opts.items():
            ok = scalar(value) or (isinstance(value, list) and value and all(scalar(v) for v in value))
            if not isinstance(key, str) or not ok:
                raise ValueError(f"`compile.{backend}.{key}` must be a string, integer or list of them")


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
    _check_compile(comp)
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
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError, RecursionError) as err:
            problems.append((path.name, str(err)))
    entries.sort(key=lambda e: e.id)
    return entries, problems


def filter_by_sku(entries: list[ZooEntry], sku: str) -> list[ZooEntry]:
    """Entries bench-validated on exactly `sku`; an empty `validated_soms`
    matches nothing (module doc)."""
    return [e for e in entries if sku in e.validated_soms]


#: Largest read from the socket at a time (never an unbounded `read()`).
READ_CHUNK_BYTES = 64 * 1024

#: Redirects followed before giving up.
MAX_REDIRECTS = 5


def iter_url(
    url: str,
    *,
    max_seconds: float = MAX_DOWNLOAD_SECONDS,
    _schemes: tuple[str, ...] = ("https://",),
) -> Iterator[bytes]:
    """Chunks (<= 64 KiB) of a GET with tan's TLS trust context.

    Hardened against a hostile server: only identity encoding is accepted (a
    gzip/deflate body is refused, and `Accept-Encoding: identity` is sent, so
    nothing expands transparently); a `Content-Length` over `MAX_DOWNLOAD_BYTES`
    is refused before reading; `read1` returns after one receive, so the total
    wall-clock deadline is checked between every chunk (a slow drip cannot
    hide inside one long read); redirects are capped at `MAX_REDIRECTS` and may
    only stay on https. The body is never trusted to match its
    `Content-Length`: `fetch_source` still counts every byte against the cap.
    `urllib` is imported here so a bare `tan` start-up does not load it."""
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    from tan.net import default_ssl_context  # noqa: PLC0415

    deadline = time.monotonic() + max_seconds

    class LimitedRedirect(urllib.request.HTTPRedirectHandler):
        max_redirections = MAX_REDIRECTS

        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            if not newurl.lower().startswith(_schemes):
                raise urllib.error.URLError(f"refusing a redirect to a non-https URL: {newurl}")
            if time.monotonic() > deadline:
                raise urllib.error.URLError(f"not complete within {max_seconds:g} s")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    try:
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=default_ssl_context()), LimitedRedirect
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "tan-model-zoo", "Accept-Encoding": "identity"}
        )
        with opener.open(req, timeout=max(0.1, min(30.0, max_seconds))) as resp:
            encoding = (resp.headers.get("Content-Encoding") or "identity").strip().lower()
            if encoding != "identity":
                raise ZooFetchError(f"{url}: refusing Content-Encoding {encoding!r}")
            declared = resp.headers.get("Content-Length")
            if declared and declared.strip().isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                raise ZooFetchError(f"{url}: Content-Length {declared} exceeds {MAX_DOWNLOAD_BYTES} bytes")
            while True:
                if time.monotonic() > deadline:
                    raise ZooFetchError(f"{url}: not complete within {max_seconds:g} s")
                chunk = resp.read1(READ_CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
    except ZooFetchError:
        raise
    except (OSError, ValueError) as err:  # URLError is an OSError
        raise ZooFetchError(f"{url}: {err}") from err


@dataclass(frozen=True)
class StagedModel:
    """A verified model sitting in a temp file the caller must publish or unlink."""

    path: Path
    sha256: str
    size: int


def bundled_chunks(zoo_dir: Path, bundled: str) -> Iterator[bytes]:
    starters = (zoo_dir / "starters").resolve()
    path = (zoo_dir / bundled).resolve()
    if path.parent != starters or not path.is_file():
        raise ZooFetchError(f"bundled starter {bundled} is missing or outside {starters}")
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                yield chunk
    except OSError as err:
        raise ZooFetchError(f"{path}: {err}") from err


def closing_iter(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Iterate `chunks`, closing it (generator `.close()`, which releases the
    HTTP connection) when the consumer stops early -- a tripped cap or deadline."""
    it = iter(chunks)
    try:
        yield from it
    finally:
        close = getattr(it, "close", None)
        if close is not None:
            close()


def fetch_source(
    entry: ZooEntry,
    zoo_dir: Path,
    *,
    tmp_dir: Path,
    reader: Callable[[str], Iterable[bytes]] = iter_url,
    max_seconds: float = MAX_DOWNLOAD_SECONDS,
) -> StagedModel:
    """Stream the model for `entry` into a temp file in `tmp_dir` (the
    destination directory, so publishing is a same-filesystem link), hashing as
    it goes -- never buffering the model in memory. A download is capped in
    size and total wall-clock time and must hash to the manifest's `sha256`;
    a bundled starter is read from inside `zoo_dir/starters` (confined). The
    temp file is removed on any failure. Raises `ZooFetchError`; a hash
    mismatch sets `.mismatch`."""
    bundled = entry.source.get("bundled")
    chunks = closing_iter(bundled_chunks(zoo_dir, bundled) if bundled else reader(entry.source["url"]))
    label = bundled or entry.source["url"]
    digest = hashlib.sha256()
    size = 0
    deadline = time.monotonic() + max_seconds
    fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, prefix=".tan-zoo-", suffix=".part")
    try:
        # `mkstemp` makes a 0600 file; the published model should carry the
        # mode an ordinary new file would (0666 & ~umask).
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp_name, 0o666 & ~mask)
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                if size + len(chunk) > MAX_DOWNLOAD_BYTES:
                    raise ZooFetchError(f"{label}: larger than {MAX_DOWNLOAD_BYTES} bytes")
                size += len(chunk)
                if time.monotonic() > deadline:
                    raise ZooFetchError(f"{label}: not complete within {max_seconds:g} s")
                digest.update(chunk)
                out.write(chunk)
        chunks.close()
        got = digest.hexdigest()
        if not bundled and got != entry.source["sha256"]:
            raise ZooFetchError(
                f"{label}: sha256 {got} does not match the manifest's {entry.source['sha256']}",
                mismatch=True,
            )
    except OSError as err:
        chunks.close()
        Path(tmp_name).unlink(missing_ok=True)
        raise ZooFetchError(f"{label}: {err}") from err
    except BaseException:
        chunks.close()
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return StagedModel(Path(tmp_name), got, size)
