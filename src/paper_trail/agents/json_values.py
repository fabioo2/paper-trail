"""Reading typed values out of tool results, which are JSON (``Mapping[str, JsonValue]``)."""

from paper_trail.domain.models import JsonValue
from paper_trail.domain.rules.item_codes import is_valid_gtin
from paper_trail.domain.rules.masking import mask_pans


def as_str(value: JsonValue) -> str:
    if not isinstance(value, str):
        msg = f"expected a string, got {type(value).__name__}"
        raise TypeError(msg)
    return value


def as_int(value: JsonValue) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"expected an integer, got {type(value).__name__}"
        raise TypeError(msg)
    return value


def as_bool(value: JsonValue) -> bool:
    if not isinstance(value, bool):
        msg = f"expected a boolean, got {type(value).__name__}"
        raise TypeError(msg)
    return value


def as_optional_float(value: JsonValue) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        msg = f"expected a number, got {type(value).__name__}"
        raise TypeError(msg)
    return float(value)


def as_list(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        msg = f"expected a list, got {type(value).__name__}"
        raise TypeError(msg)
    return value


def as_dict(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        msg = f"expected an object, got {type(value).__name__}"
        raise TypeError(msg)
    return value


def as_float(value: JsonValue) -> float:
    number = as_optional_float(value)
    if number is None:
        msg = "expected a number, got NoneType"
        raise TypeError(msg)
    return number


def masked_json(value: JsonValue) -> tuple[JsonValue, int]:
    """``value`` with the card numbers in every string masked (``mask_pans``), and how many
    were masked. A ``code`` that is a valid GTIN (8, 12, 13 or 14 digits with its check digit)
    is a barcode, kept as it is even when it also passes the card number check."""
    if isinstance(value, str):
        result = mask_pans(value)
        return result.text, result.masked_count
    if isinstance(value, list):
        pairs = [masked_json(item) for item in value]
        return [item for item, _ in pairs], sum(count for _, count in pairs)
    if isinstance(value, dict):
        entries = {
            key: (item, 0) if _is_barcode(key, item) else masked_json(item)
            for key, item in value.items()
        }
        return {key: item for key, (item, _) in entries.items()}, sum(
            count for _, count in entries.values()
        )
    return value, 0


def _is_barcode(key: str, value: JsonValue) -> bool:
    return key == "code" and isinstance(value, str) and is_valid_gtin(value)
