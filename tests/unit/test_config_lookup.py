"""Product-database settings (claude-extraction plan D3, R4, V10; CE-TC-201).

Pinned: ``lookup: LookupSettings`` (``PAPER_TRAIL_LOOKUP__*``): Open*Facts on, UPCitemdb off,
a request timeout of 10 s (above 0, at most 60). The rate limits and the 30-day retry are fixed
in code, not settings.
"""

import pytest
from pydantic import ValidationError

from paper_trail.config import LookupSettings, Settings


def test_open_facts_is_on_and_upcitemdb_off_by_default() -> None:
    assert Settings().lookup == LookupSettings(open_facts=True, upcitemdb=False, timeout_s=10.0)


@pytest.mark.parametrize(
    ("variable", "value", "expected"),
    [
        ("OPEN_FACTS", "false", False),
        ("UPCITEMDB", "true", True),
        ("TIMEOUT_S", "60", 60.0),
        ("UPCITEMDB", "maybe", None),
        ("TIMEOUT_S", "0", None),
        ("TIMEOUT_S", "61", None),
    ],
)
def test_the_lookup_settings_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, expected: object
) -> None:
    """``None``: refused, the error naming the setting."""
    monkeypatch.setenv(f"PAPER_TRAIL_LOOKUP__{variable}", value)

    if expected is None:
        with pytest.raises(ValidationError, match=variable.lower()):
            Settings()
    else:
        assert getattr(Settings().lookup, variable.lower()) == expected
