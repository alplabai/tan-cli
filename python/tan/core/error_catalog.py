# SPDX-License-Identifier: Apache-2.0
"""The alp-sdk error/diagnostic catalogue, as `tan explain --code` reads it.

Port of the lookup half of alp-sdk's `scripts/alp_cli/explain.py` (ADR-0020
end-state B: the verb moves to `tan`, it does not disappear). That command is
the only reader of `<sdk>/metadata/error-catalog.json` today, and retiring it
without this port would leave every `ALP-Bxxx` landing page and every
`ALP_ERR_*` enum comment unreachable from any CLI.

**The source is the generated catalogue, not the markdown.** The task this
was written for described `explain.py` as reading `docs/diagnostics/ALP-B*.md`
and the `ALP_ERR_*` enum directly. Measured against alp-sdk `origin/dev`, it
does not: it reads `metadata/error-catalog.json` and nothing else
(`explain.py:24-25,39-42`). Those two sources are what
`scripts/gen_error_catalog.py` reads, and that generator stays in alp-sdk --
it is a `pr-generated-files.yml` drift gate over alp-sdk's own headers and
docs, not a user command, so ADR-0020 does not move it. tan reading the
generated JSON is also the only shape compatible with I-26/ADR-0017: tan
learns no hardware fact and parses no C header, it reads one JSON file out of
the bound checkout the same way `presets` reads `metadata/e1m_modules/`.

Pure apart from the one file read, so the lookup, the suggestion list and the
rendered field order are unit-testable without a checkout on disk.
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path

#: `<sdk_root>/metadata/error-catalog.json`, as path segments -- the same
#: shape `tan.core.shapes.SDK_MARKER` uses so a caller may join it either way.
CATALOG_RELATIVE = ("metadata", "error-catalog.json")

#: Rendered field order and label, verbatim from `explain.py`'s `_FIELD_ORDER`.
#: A field the catalogue omits (an `api-error` carries no `cause`/`fix`) is
#: SKIPPED, never rendered empty -- the generator's own "never fabricate a
#: field that has no source" rule, preserved on the reading side.
FIELD_ORDER: tuple[tuple[str, str], ...] = (
    ("summary", "summary"),
    ("cause", "cause"),
    ("fix", "fix"),
    ("doc", "doc"),
)

#: `difflib.get_close_matches` tuning, verbatim from `explain.py:87-88`. Kept
#: as named constants because the SUGGESTION LIST is user-visible output: a
#: different cutoff silently changes what a typo is answered with.
SUGGESTION_LIMIT = 5
SUGGESTION_CUTOFF = 0.4


class CatalogUnreadable(Exception):
    """The bound checkout has no readable `metadata/error-catalog.json`.

    Raised for an absent file, an unreadable one, malformed JSON, and a
    document whose top level is not an object -- one exception for all four
    because the caller's answer is the same in every case: name the path it
    looked at and say the checkout cannot answer, rather than guessing which
    of alp-sdk's several possible states produced it.
    """

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


def catalog_path(sdk_root: Path | str) -> Path:
    """`<sdk_root>/metadata/error-catalog.json`."""
    return Path(os.path.join(str(sdk_root), *CATALOG_RELATIVE))


def load_codes(sdk_root: Path | str) -> dict[str, dict]:
    """The `{code: entry}` map from the bound checkout's catalogue.

    Raises `CatalogUnreadable`; never returns a partial map. A document that
    parses but carries no `codes` key yields `{}` rather than raising, exactly
    as `explain.py`'s `data.get("codes", {})` does -- an empty catalogue is a
    catalogue in which every code is unknown, which the miss path already
    reports honestly.

    Every VALUE in `codes` is also checked here, not just `codes` itself: a
    catalogue shaped `{"codes": {"ALP-B003": "not an object"}}` (also a list,
    also `null`) otherwise reaches `lookup`, matches on `ALP-B003`, and
    `resolve_code`'s `entry = codes[key]` then hands a non-dict to
    `summary_line`/`detail_lines`, both of which call `entry.get(...)`.
    Measured with this guard removed: `AttributeError: 'str' object has no
    attribute 'get'`, surfacing as `explain.internal-failure` at exit 5 for a
    checkout problem the reader could fix -- the same exit-1-not-exit-5
    concern `codes` itself is guarded for, just one dereference deeper.
    """
    path = catalog_path(sdk_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CatalogUnreadable(
            path,
            "error catalog not found -- run `python3 scripts/gen_error_catalog.py` "
            "in the alp-sdk checkout",
        ) from None
    except (OSError, UnicodeDecodeError) as err:
        raise CatalogUnreadable(path, f"error catalog is unreadable: {err}") from None
    try:
        data = json.loads(raw)
    except ValueError as err:
        raise CatalogUnreadable(path, f"error catalog is not valid JSON: {err}") from None
    if not isinstance(data, dict):
        raise CatalogUnreadable(path, "error catalog is not a JSON object")
    codes = data.get("codes", {})
    if not isinstance(codes, dict):
        raise CatalogUnreadable(path, "error catalog's `codes` is not a JSON object")
    for code, entry in codes.items():
        if not isinstance(entry, dict):
            raise CatalogUnreadable(
                path, f"error catalog entry '{code}' is not a JSON object"
            )
    return codes


def normalise(code: str) -> str:
    """Canonicalise user input for matching -- `explain.py`'s `_normalise`.

    Case-insensitive and whitespace-trimmed, so `alp-b003`, ` ALP-B003 ` and
    `alp_err_no_backend` all resolve.
    """
    return code.strip().upper()


def lookup(codes: dict[str, dict], query: str) -> tuple[str | None, list[str]]:
    """`(matched_key, suggestions)` for `query` against `codes`.

    On a hit the suggestion list is empty; on a miss the key is `None` and the
    suggestions are `explain.py`'s own `difflib` shortlist, mapped back to the
    catalogue's real spelling. Both halves are returned together because the
    caller needs exactly one of them and deciding which is this function's job.
    """
    by_upper = {normalise(key): key for key in codes}
    wanted = normalise(query)
    key = by_upper.get(wanted)
    if key is not None:
        return key, []
    close = difflib.get_close_matches(
        wanted, list(by_upper), n=SUGGESTION_LIMIT, cutoff=SUGGESTION_CUTOFF
    )
    return None, [by_upper[match] for match in close]


def summary_line(entry: dict) -> str:
    """`<CODE> (<kind>)` -- the header `explain.py`'s `_render` paints cyan.

    ONE space, where `explain.py` writes two (`f"{code}  ({kind})"`): tan's
    `explain` summary is `"<label> (<id>)"` for every other selector kind and a
    per-mode spacing rule inside one command is a worse surface than a
    one-character divergence. The CODE and the KIND are verbatim.
    """
    return f"{entry.get('code', '')} ({entry.get('kind', '')})".strip()


def detail_lines(entry: dict) -> list[str]:
    """`<label>: <value>` per populated field, in `FIELD_ORDER`.

    The values are verbatim catalogue text -- no re-wrapping, no markdown
    stripping, no truncation. `explain_cmd` prefixes each with `- ` and wraps
    it exactly as it wraps a template's detail lines.
    """
    return [
        f"{label}: {entry[key]}" for key, label in FIELD_ORDER if entry.get(key)
    ]
