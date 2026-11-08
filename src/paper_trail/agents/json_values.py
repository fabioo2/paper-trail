"""Reading typed values out of tool results, which are JSON (``Mapping[str, JsonValue]``)."""

from paper_trail.domain.models import JsonValue


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
