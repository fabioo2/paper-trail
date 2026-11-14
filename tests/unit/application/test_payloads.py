"""``DraftLinePayload`` carries a person's discount link through tool JSON and checkpoints
(discounts-and-paid-prices plan, R7; TC-85, TC-86).

Pinned: ``applies_to_position: StrictInt | None = None``; ``from_line`` / ``to_line`` carry it
both ways; a payload written before the field existed (no key) reads as None.
"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from paper_trail.application.payloads import DraftLinePayload
from tests.factories import discount_line

# P8 PAYLOAD_LINKS: a DISCOUNT line at position 6 unlinked, linked to 0, linked to 5.
PAYLOAD_LINKS = [None, 0, 5]


def _payload_json(**changes: Any) -> dict[str, Any]:
    dumped = DraftLinePayload.from_line(discount_line(6, "TPD/1362394", -400)).model_dump(
        mode="json"
    )
    dumped.update(changes)
    return dumped


@pytest.mark.parametrize("applies_to", PAYLOAD_LINKS)
def test_tc_85_a_line_round_trips_with_its_link(applies_to: int | None) -> None:
    line = discount_line(6, "TPD/1362394", -400, applies_to)

    assert DraftLinePayload.from_line(line).to_line() == line


def test_tc_86_a_payload_without_the_key_reads_as_unlinked() -> None:
    written_before = _payload_json()
    del written_before["applies_to_position"]

    line = DraftLinePayload.model_validate_json(json.dumps(written_before)).to_line()

    assert line == discount_line(6, "TPD/1362394", -400)


@pytest.mark.parametrize("value", ["0", 0.0, True], ids=["string", "float", "bool"])
def test_tc_86_a_link_that_is_not_an_integer_is_refused(value: object) -> None:
    with pytest.raises(ValidationError) as raised:
        DraftLinePayload.model_validate_json(json.dumps(_payload_json(applies_to_position=value)))

    assert [error["loc"] for error in raised.value.errors()] == [("applies_to_position",)]
