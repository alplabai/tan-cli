# SPDX-License-Identifier: Apache-2.0
"""The ``.alp/debug-launch-provenance.json`` sidecar ``tan debug-config``
reads and writes alongside ``.vscode/launch.json`` -- a content-hash record
of which ``configFiles``/``setupCommands`` list entries a PRIOR
tan-authored write itself produced, so the next run's merge
(``debug_launch._merge_list_by_identity``) can tell "tan wrote this, safe to
update or retract" apart from "the customer's, leave it alone" without
relying on POSITION. tan-cli#518, the gap tan-cli#489's own docstring names
as a "Known, accepted limitation" (``debug_launch.py``'s
``_merge_list_by_identity``).

tan-cli#1020 review widened this sidecar to a THIRD field, ``loadFiles``
(tan-cli#945) -- the same content-hash record, read by the same
:meth:`LaunchProvenance.hashes_for`, but consumed by a DIFFERENT merge rule
(``debug_launch._merge_load_files``, not ``_merge_list_by_identity``): a
``loadFiles`` this run cannot prove it wrote is left untouched WHOLESALE
(never appended-to, unlike ``configFiles``/``setupCommands``), because it
names one deliberate artefact list -- possibly an explicit ``[]`` for
attach-only -- not a set of independently-owned entries. This module itself
needed no change for that: `hashes_for`/`updated`/`record` are already
generic over the field name.

## Why content hash, and why a sidecar at all

The design decision (tan-cli#518, 2026-08-28) rules out an in-file marker:
``launch.json`` is schema-validated by VS Code against the debug adapter's
own contract, and an unknown property inside a ``configurations[]`` entry
draws a permanent squiggle in the customer's editor -- "that warning is
ours, ignore it" is not something a tool should ever require of its own
output. So the record lives beside ``launch.json``, in ``.alp/`` (the same
directory ``tan init``'s own ``sdk-path`` pointer already occupies --
``init_cmd.py``, ``bootstrap_cmd._project_pin_file``), never inside it.
``launch.json`` itself is byte-for-byte unaffected by this module: no new
key, no comment, nothing ``jsonc_splice``'s span-preserving edits need to
know about.

Position was the ORIGINAL provenance proxy (tan-cli#489's anchor-relative
placement) and it has exactly one failure mode: a customer's own hand-added
entry that happens to fall into the position tan is about to reuse gets
silently overwritten, indistinguishable from tan's own prior output sitting
there. A NAME would have the same flaw one layer up -- ``configFiles``
entries have no name at all, and ``setupCommands``' own ``text`` field is
exactly the identity ``_list_item_identity`` already matches by (matching by
identity already never reaches position; only an UNMATCHED entry does).
Content hash is the one thing that is stable across reordering and a
reformatting round-trip (see ``content_hash`` below) yet changes the instant
a human -- or a formatter with different defaults -- touches the actual
value.

## The asymmetry this whole module exists to preserve

Every desync degrades the SAME direction: a missing file, corrupt JSON, a
schema this build does not recognise, an entry whose hash was never
recorded, a stale record left over from a run that resolved something
different -- all of them read as "not tan's", never as "tan's, go ahead".
:func:`load` never raises; every failure returns :func:`empty`, the record
that owns nothing. A customer who deletes
``.alp/debug-launch-provenance.json`` outright gets exactly that: tan
forgets what it wrote and starts leaving every existing list entry alone
(``debug_launch._merge_list_by_identity``'s position-based pass degrades to
append-only without a confirmed hash) -- a mildly stale entry the customer
can delete in one keystroke, never a deleted customer edit. The SIDECAR
re-establishes itself from the very next write, so the MECHANISM's
degradation is one run, not permanent -- but a list entry it left stranded
during that one run (appended beside a value it could not prove was its
own) is not retroactively cleaned up once the sidecar heals; it stays in
``launch.json`` until the customer deletes it themselves. See
``debug_launch.sdk_identity_stranded_appends`` (surfaced by
``debug_config_cmd.py`` as ``debug-config.sdk-identity-appended``) for the
disclosure that names it rather than leaving it silent.

**``loadFiles`` heals the same way for the same reason, but only from ONE
specific starting point** (tan-cli#1020 re-review): a sidecar loss re-derives
provenance the instant the field itself is next observed absent from the
existing entry -- the pre-#945 upgrade shape, and the only case where there
is provably nothing to protect (``debug_launch._merge_configuration``'s
key-absent branch records what it just wrote, exactly like a brand-new
entry). A sidecar lost while the KEY WAS ALREADY PRESENT -- a genuinely
tan-authored value, or a customer's own -- cannot re-derive provenance from
observation alone, because ``_merge_load_files``'s protect branch (unlike
``configFiles``/``setupCommands``'s append) makes no write of its own to
attribute: recording ownership of an untouched value the sidecar merely
forgot would be indistinguishable, to the very next run, from recording
ownership of a customer's own untouched value, which is the exact silent-
overwrite failure tan-cli#518/#1020's protect rule exists to prevent. So
that narrower case stays protected -- and disclosed, every run, via
``debug-config.load-files-preserved`` -- until the customer's own edit (or a
value that happens to already match the fresh resolution) lets it agree
again; it does not "heal" the way an appended `configFiles` entry's
provenance does.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Sidecar schema version. A `load` that sees anything else treats the whole
#: file as unrecognised -- the safe direction for a shape a future (or past)
#: build cannot confidently parse; see `load`.
SCHEMA_VERSION = 1

#: `<workspace_root>/.alp/debug-launch-provenance.json` -- beside `tan
#: init`'s own `.alp/sdk-path` (`init_cmd.py`, `bootstrap_cmd._project_pin_
#: file`), never inside `.vscode/launch.json` itself.
SIDECAR_RELATIVE_PATH = (".alp", "debug-launch-provenance.json")


def sidecar_path(workspace_root: str | Path) -> Path:
    """The sidecar path for a workspace root, following the same
    `.alp/<file>` convention `tan init`/`tan sdk` already use."""
    return Path(workspace_root).joinpath(*SIDECAR_RELATIVE_PATH)


def content_hash(value: Any) -> str:
    """A stable hash of a `configFiles`/`setupCommands` list ENTRY's
    semantic content. `json.dumps(..., sort_keys=True)` canonicalises key
    order before hashing, so re-serialising an unchanged `setupCommands`
    dict with a different key order (an external formatter, a hand-edit
    that only reordered `text`/`ignoreFailures`) hashes IDENTICAL to what
    tan itself wrote, and is never mistaken for a customer edit -- the exact
    property the issue names: "re-serialisation with different key order or
    whitespace must not read as an edit". Whitespace never reaches the hash
    at all, because this hashes the PARSED value, not raw text.
    """
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class LaunchProvenance:
    """The parsed sidecar: per-configuration-name, per-list-field sets of
    content hashes tan itself wrote the last time a run touched that field.

    Immutable from the outside -- :meth:`updated` is the only way a caller
    (`debug_launch.create_launch_json_write_plan`) ever changes one, and it
    always returns a NEW record rather than mutating `self`, so a
    `LaunchJsonWritePlan` never rewrites the `LaunchProvenance` its own
    caller is still holding a reference to.
    """

    _by_config: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    def hashes_for(self, configuration_name: str, list_field: str) -> frozenset[str]:
        """The hashes tan recorded for `list_field` (`configFiles` /
        `setupCommands`) the last time it wrote `configuration_name` --
        empty when the configuration, the field, or the whole sidecar was
        never recorded (or could not be read; see `load`). Empty is the
        safe default everywhere this is called from: it is exactly the
        "nothing is ours" set."""
        return frozenset(self._by_config.get(configuration_name, {}).get(list_field, ()))

    def record(self, configuration_name: str, list_field: str, entries: list[Any]) -> None:
        """Replace `list_field`'s recorded hash set for `configuration_name`
        with the hashes of exactly `entries` -- MUTATES `self`; only
        :meth:`updated` (which always operates on a fresh copy) calls this
        directly."""
        config = self._by_config.setdefault(configuration_name, {})
        config[list_field] = [content_hash(entry) for entry in entries]

    def updated(
        self, configuration_name: str, owned_entries: dict[str, list[Any]]
    ) -> LaunchProvenance:
        """A NEW record equal to `self` except `configuration_name`'s fields
        named in `owned_entries` are replaced by the hashes of exactly
        those entries -- the list entries THIS run identified as
        tan-authored (matched, positionally placed, or freshly appended;
        never an entry `debug_launch._merge_list_by_identity`'s pass 3
        left untouched, and never a field the run did not touch at all --
        that field, absent from `owned_entries`, keeps whatever `self`
        already had for it).

        A field this run DID touch is fully REPLACED, never merged with its
        own stale entries: an entry that vanishes from this run's draft
        resolution must not leave a phantom hash behind for something
        unrelated to accidentally match against later. This is the
        pure-functional twin of :meth:`record` -- `create_launch_json_write_
        plan` never mutates the `provenance` object a caller handed it.
        """
        result = LaunchProvenance(
            {name: dict(fields) for name, fields in self._by_config.items()}
        )
        for list_field, entries in owned_entries.items():
            result.record(configuration_name, list_field, entries)
        return result

    def to_json(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "configurations": {
                name: dict(fields) for name, fields in self._by_config.items() if fields
            },
        }


def empty() -> LaunchProvenance:
    """The record that owns nothing -- what every unreadable, missing, or
    schema-unrecognised sidecar degrades to. tan-cli#518's whole point: a
    desync must read as "not tan's", never as "everything is tan's"."""
    return LaunchProvenance()


def load(content: str | None) -> LaunchProvenance:
    """Parse a sidecar's raw text into a :class:`LaunchProvenance`. Never
    raises -- anything this cannot confidently parse (`None`, empty, not
    JSON, not an object, a `configurations` that is not an object, a
    `schemaVersion` this build does not recognise, or a per-field value that
    is not a list of strings) returns :func:`empty` rather than guessing,
    per this module's own asymmetry (see the module docstring). A caller
    that could not even READ the file (permission denied, an I/O error) is
    expected to pass `None` here for exactly the same reason -- this
    function draws no distinction between "absent" and "unreadable"; both
    mean "nothing is ours".
    """
    if not content:
        return empty()
    try:
        document = json.loads(content)
    except json.JSONDecodeError:
        return empty()
    if not isinstance(document, dict):
        return empty()
    if document.get("schemaVersion") != SCHEMA_VERSION:
        return empty()
    configurations = document.get("configurations")
    if not isinstance(configurations, dict):
        return empty()
    by_config: dict[str, dict[str, list[str]]] = {}
    for name, fields in configurations.items():
        if not isinstance(name, str) or not isinstance(fields, dict):
            continue
        clean_fields: dict[str, list[str]] = {}
        for field_name, hashes in fields.items():
            if (
                isinstance(field_name, str)
                and isinstance(hashes, list)
                and all(isinstance(h, str) for h in hashes)
            ):
                clean_fields[field_name] = hashes
        if clean_fields:
            by_config[name] = clean_fields
    return LaunchProvenance(by_config)


def render(record: LaunchProvenance) -> str:
    """The sidecar's own pretty-printed bytes, trailing newline included --
    the same two-space-indent shape `jsonc_splice.pretty_json` uses for
    `launch.json`'s own fresh-file fallback, so a customer who opens either
    file in `.alp`/`.vscode` sees the same formatting convention. Unlike
    `launch.json`, this file is tan's own bookkeeping -- there is no
    customer content in it to preserve byte-for-byte, so it is always a
    plain whole-file re-serialise; nothing here ever needs `jsonc_splice`.
    """
    return json.dumps(record.to_json(), indent=2, sort_keys=True) + "\n"
