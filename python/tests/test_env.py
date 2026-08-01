# SPDX-License-Identifier: Apache-2.0
import pytest

from tan.env import no_color_requested


def test_unset_does_not_request_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert no_color_requested() is False


@pytest.mark.parametrize("value", ["", "0", "false", "1", "true", "anything"])
def test_any_set_value_including_empty_requests_no_color(monkeypatch, value):
    """PRESENCE, not truthiness -- `NO_COLOR=` (set, empty) is the exact case
    that drifted between the two hand-rolled copies this module replaces."""
    monkeypatch.setenv("NO_COLOR", value)
    assert no_color_requested() is True
