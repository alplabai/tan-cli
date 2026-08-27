# SPDX-License-Identifier: Apache-2.0
"""The one spelling of "tan accepts this option and does nothing with it"
(tan-cli#886).

`--help` marks an option inert in four structurally different situations, and
until this module existed the difference lived only in free prose that varied
per site: "Accepted by other commands; not implemented for `build` yet
(tan-cli#427)", "Accepted for compatibility (tan-cli#290)", "(unused:
faultdecode is HW-free)", "(unused; see below)". Four meanings, six spellings,
two of which said nothing at all once the option was read out of its
surrounding help block.

**Why that is a defect and not a style nit.** `alp-sdk-vscode` records tan's
whole option surface into a golden (`test/golden/tan-surface/surface.json`)
and renders the inert ones for the customer. Only ONE of the four kinds will
ever start acting; the other three are permanent by design. Telling someone
"not implemented yet, see tan-cli#427" about `doctor --build` invites them to
wait for a flag that is never going to act. With nothing structured to read,
the consumer's only options were to pattern-match tan's prose -- fragile, and
a condition pinned to one spelling is blind to the other five -- or to
hand-maintain its own copy of a table tan already knows
(alp-sdk-vscode#577 did the latter).

**The marker.** Every inert option's help ends with a single whitespace-free
token, `(inert:KIND)` or `(inert:KIND:REF)`:

    --plan        Accepted by other commands; not implemented for `build`
                  yet. (inert:deferred:tan-cli#427)
    --build       Accepted for compatibility: ... (inert:compatibility:tan-cli#290)
    --project     Project root. Not read: ... (inert:not-applicable)

Three properties of that shape are deliberate, and each rules out a rendering
defect measured on the real `--help`:

1. **Parentheses, not square brackets.** Typer runs this app with
   `rich_markup_mode="rich"`, so help text is rich MARKUP: a `[inert:deferred]`
   marker parses as a style tag and renders as *nothing at all* -- measured, the
   whole token vanishes from `tan build --help`. `\\[` escapes it, but only
   while the markup mode stays `rich`, so the escape is a second thing to keep
   true. Parentheses need neither.
2. **No whitespace inside the token.** Rich wraps help text at spaces. A
   `(inert: deferred, ref: tan-cli#427)` marker splits across two lines in a
   narrow terminal and a consumer's regex has to re-join them first; this one
   cannot split.
3. **The ref lives in the marker, not a second time in the prose.** The prose
   the twelve `build` flags shared already ended `(tan-cli#427).`; keeping both
   would be two places to keep true for one fact.

**`deferred` is the only kind that requires a ref**, and `inert_help` refuses
to render one without it. That requirement is the whole point of publishing
the kind: `deferred` means "an upstream issue tracks its arrival", so a
`deferred` marker naming no issue is a promise with nothing behind it. The
permanent kinds MAY carry a ref (`compatibility` and `parity` both name the
issue that explains the history) and a consumer must not read the presence of
a ref as "this will arrive" -- the KIND is what says that, never the ref.
"""
from __future__ import annotations

import re

#: An upstream issue tracks this option's arrival: it is accepted now, it does
#: nothing now, and it is expected to start acting. The ONLY kind that is not
#: permanent, and the only one that requires a ref.
DEFERRED = "deferred"

#: Kept so an existing caller's command line keeps parsing, after the
#: behaviour it used to select stopped being conditional. Permanent.
COMPATIBILITY = "compatibility"

#: Accepted only because a sibling surface accepts it -- the v0.4.1 oracle's
#: clap `GlobalArgs` are `global = true`, so every verb parses all ten
#: (tan-cli#261). Permanent; see `tan.core.global_flags`.
PARITY = "parity"

#: Structurally meaningless for THIS command -- there is no behaviour for it
#: to select, no matter what tan implements later. Permanent.
NOT_APPLICABLE = "not-applicable"

#: The closed vocabulary. A consumer switches on exactly these; anything else
#: is a tan bug, not a value to fall back on.
INERT_KINDS: tuple[str, ...] = (DEFERRED, COMPATIBILITY, PARITY, NOT_APPLICABLE)

#: The kinds that will never start acting. Stated as its own tuple rather than
#: as "everything except DEFERRED" so adding a fifth kind forces a decision
#: about which side it lands on instead of inheriting one.
PERMANENT_KINDS: tuple[str, ...] = (COMPATIBILITY, PARITY, NOT_APPLICABLE)

#: How the marker is read back OUT of rendered `--help` text -- published in
#: `contract/README.md` for the extension, and used by this repo's own gate
#: (`tests/gates/test_inert_option_markers.py`) so both sides read one regex.
MARKER_RE = re.compile(r"\(inert:(?P<kind>[a-z-]+)(?::(?P<ref>[^)\s]+))?\)")

#: The only ref spelling tan emits. `tan-cli#N` rather than a full URL: it is
#: what every other cross-reference in this CLI's prose already uses, and it
#: survives the line-wrapping rule above (a URL would not be shorter).
REF_RE = re.compile(r"^tan-cli#[1-9][0-9]*$")


def inert_help(prose: str, kind: str, ref: str | None = None) -> str:
    """Render `prose` with the inert marker appended.

    Raises `ValueError` -- at IMPORT time, since every call site is a
    `typer.Option(...)` default evaluated when its module loads -- rather than
    emitting a marker a consumer cannot switch on. A malformed marker that
    only fails a test run would still have shipped in someone's branch;
    failing the import fails the CLI itself.
    """
    if kind not in INERT_KINDS:
        raise ValueError(
            f"unknown inert kind {kind!r}; expected one of {', '.join(INERT_KINDS)}. "
            "The vocabulary is closed on purpose -- a consumer switches on it "
            "(tan-cli#886). Adding a kind means adding it to INERT_KINDS, to "
            "PERMANENT_KINDS or not, and to contract/README.md."
        )
    if kind == DEFERRED and ref is None:
        raise ValueError(
            "a deferred option must name the issue tracking its arrival: "
            "`deferred` is the one kind that promises the flag will start "
            "acting, and a promise with no issue behind it is exactly what "
            "tan-cli#886 asked to make impossible. Use a permanent kind "
            f"({', '.join(PERMANENT_KINDS)}) if nothing tracks it."
        )
    if ref is not None and not REF_RE.match(ref):
        raise ValueError(
            f"inert ref {ref!r} is not of the form 'tan-cli#123'. One spelling "
            "keeps the consumer's extraction regex (MARKER_RE) exact."
        )
    stripped = prose.strip()
    if not stripped:
        raise ValueError(
            "inert help needs prose of its own: the marker says WHICH KIND of "
            "inert, never what the option would have meant. An option whose "
            "help is only a marker reads as nameless in `--help`."
        )
    marker = f"(inert:{kind})" if ref is None else f"(inert:{kind}:{ref})"
    return f"{stripped} {marker}"
