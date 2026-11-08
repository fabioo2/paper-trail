"""Errors raised by the domain and use cases; the web layer maps them to problem details."""


class DomainError(Exception):
    """Base class for every expected, user-facing failure."""


class NotFoundError(DomainError):
    """The requested entity does not exist."""


class ConflictError(DomainError):
    """The request conflicts with the current state of an entity."""


class UnsupportedMediaError(DomainError):
    """A file that is not an image Paper Trail can read (judged by content, not by name)."""


class DomainValidationError(DomainError):
    """The input is well-formed but violates a business rule."""


class InvalidMoneyError(DomainValidationError):
    """An amount that is not integer cents, or a currency that is not an ISO 4217 code."""


class CurrencyMismatchError(DomainValidationError):
    """Amounts in different currencies were combined."""


class InvalidQuantityError(DomainValidationError):
    """A line quantity that is not a positive, finite number."""


class PrintedValueError(DomainValidationError):
    """A value the model copied from the receipt that cannot be read as printed (ADR-0017).

    The message names the field and quotes the printed text only, never an invented number:
    it goes back to the model as the next attempt's feedback.
    """
