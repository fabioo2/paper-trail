"""Keyset paging: PageRequest and the opaque cursor (S1-AC-08)."""

import base64
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paper_trail.domain.errors import DomainValidationError
from paper_trail.domain.paging import Page, PageRequest, decode_cursor, encode_cursor

utc_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),  # noqa: DTZ001 - hypothesis bounds are naive by contract
    max_value=datetime(2100, 1, 1),  # noqa: DTZ001
    timezones=st.just(UTC),
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_s1_ac_08_page_request_defaults() -> None:
    """S1-AC-08: limit defaults to 20, no cursor."""
    page = PageRequest()

    assert page.limit == 20
    assert page.cursor is None


@pytest.mark.parametrize("limit", [1, 50, 100])
def test_s1_ac_08_page_request_accepts_limits_1_to_100(limit: int) -> None:
    """S1-AC-08: the bounds are inclusive."""
    assert PageRequest(limit=limit).limit == limit


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
def test_s1_ac_08_page_request_rejects_limits_outside_1_to_100(limit: int) -> None:
    """S1-AC-08: limit outside 1..100 raises DomainValidationError."""
    with pytest.raises(DomainValidationError):
        PageRequest(limit=limit)


def test_s1_ac_08_cursor_round_trips() -> None:
    """S1-AC-08: decode_cursor(encode_cursor(t, i)) == (t, i), microseconds included."""
    moment = datetime(2026, 11, 6, 14, 30, 5, 123456, tzinfo=UTC)

    assert decode_cursor(encode_cursor(moment, 42)) == (moment, 42)


def test_s1_ac_08_cursor_is_opaque_urlsafe_base64() -> None:
    """S1-AC-08 / PLAN 3.2: the cursor is URL-safe base64 of "<created_at_iso>|<id>"."""
    moment = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)

    cursor = encode_cursor(moment, 7)

    decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
    timestamp, _, identifier = decoded.rpartition("|")
    assert datetime.fromisoformat(timestamp) == moment
    assert identifier == "7"
    assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")


def test_s1_ac_08_decoded_time_is_utc() -> None:
    """Pinned: a cursor encoded from a non-UTC aware time decodes to the same instant in UTC."""
    eastern = timezone(timedelta(hours=-5))
    moment = datetime(2026, 11, 6, 9, 0, tzinfo=eastern)

    decoded, _ = decode_cursor(encode_cursor(moment, 1))

    assert decoded == moment
    assert decoded.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "not base64 at all!",
        "%%%",
        _b64("no separator"),
        _b64("2026-11-06T14:30:00+00:00|"),
        _b64("2026-11-06T14:30:00+00:00|seven"),
        _b64("yesterday|7"),
        _b64("2026-11-06T14:30:00|7"),  # naive timestamps are not ours
        _b64("2026-11-06T14:30:00+00:00|-1"),
        base64.urlsafe_b64encode(b"\xff\xfe|1").decode(),
    ],
)
def test_s1_ac_08_garbage_cursors_raise_validation_error(cursor: str) -> None:
    """S1-AC-08: anything that is not a cursor we issued raises DomainValidationError."""
    with pytest.raises(DomainValidationError):
        decode_cursor(cursor)


def test_s1_ac_08_page_holds_items_and_next_cursor() -> None:
    """Page is a frozen generic container."""
    page = Page(items=(1, 2), next_cursor=None)

    assert page.items == (1, 2)
    assert page.next_cursor is None


@given(moment=utc_datetimes, identifier=st.integers(min_value=1, max_value=2**63 - 1))
def test_s1_ac_08_property_cursor_round_trip(moment: datetime, identifier: int) -> None:
    """Property: every (UTC time, positive id) pair survives the cursor."""
    assert decode_cursor(encode_cursor(moment, identifier)) == (moment, identifier)


def test_s1_ac_08_cursor_times_must_be_aware() -> None:
    """A naive time has no instant, so it cannot be a cursor position."""
    with pytest.raises(ValueError, match="timezone-aware"):
        encode_cursor(datetime(2026, 11, 6, 14, 30), 1)  # noqa: DTZ001 - the point of the test
