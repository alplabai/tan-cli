# SPDX-License-Identifier: Apache-2.0
"""`tan.core.shapes` -- the shared alp-sdk-root/YAML-shape predicates.

Only `rejected_sdk_root_message` is covered here (tan-cli#497 defect 7);
`is_sdk_root` and `yaml_kind` are exercised through their five and two callers
respectively, which is where their real inputs come from.
"""
from __future__ import annotations

from tan.core.shapes import SDK_MARKER, rejected_sdk_root_message


def test_the_rejected_value_is_named_verbatim() -> None:
    """The whole defect: a user's typed `--sdk-root` vanished, so the message
    HAS to carry it. Quoted, so a path with a trailing space or an embedded
    quote is still readable as one value."""
    message = rejected_sdk_root_message("/home/u/alp-sdk-typo", "Nothing was generated.")
    assert '--sdk-root "/home/u/alp-sdk-typo"' in message


def test_the_message_names_the_marker_that_was_looked_for() -> None:
    """"Not an alp-sdk checkout" alone leaves the reader guessing WHY. The
    marker is rendered from `SDK_MARKER` rather than re-spelled, so a change to
    I-31's marker cannot leave this string claiming the old one."""
    message = rejected_sdk_root_message("/x", "Nothing was generated.")
    assert "/".join(SDK_MARKER) in message
    assert "scripts/alp_project.py" in message  # the value of I-31 today


def test_the_consequence_is_appended_verbatim() -> None:
    """Each caller owns its own "and so you got this instead" clause -- it is
    the half a reader acts on, and it differs per command."""
    assert rejected_sdk_root_message("/x", "Cannot read the pinmux table.").endswith(
        "Cannot read the pinmux table."
    )


def test_the_message_never_recommends_the_flag_the_caller_just_typed() -> None:
    """The self-defeating remediation is the defect's other half: the old
    strings answered a rejected `--sdk-root` with "Use --sdk-root". Naming the
    rejected value IS the remediation here, so no branch may reintroduce the
    advice.

    Asserted on the LOWERCASED message so a future re-wording cannot slip it
    back in under different capitalisation."""
    message = rejected_sdk_root_message("/x", "Nothing was validated.").lower()
    assert "use --sdk-root" not in message
    assert "pass --sdk-root" not in message
