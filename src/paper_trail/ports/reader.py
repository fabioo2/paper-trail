"""Reading a receipt photo with a hosted model (claude-extraction plan E1, V11, R2).

One call per attempt: the stored photo and the prompt in, the answer validated as ``schema``
(the agents' Pydantic answer model) out. The adapter prepares the image (upright, long edge at
most 2576 px, JPEG) and never retries: one read is one HTTP request, so whether it was billed
is known exactly; the budget wrapper retries the retryable failures (review C11).

Every failure is a ``ReaderError``. ``ReaderOutputError`` is a failed attempt (the next attempt
gets the error as feedback); every other one stops the run: the receipt is FAILED with its
checkpoint and ``process --resume`` continues it.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ReadUsage:
    input_tokens: int
    output_tokens: int


class ReaderError(Exception):
    """Base class of a read's failures; ``usage`` is set when the call was billed and says
    what it cost, ``maybe_billed`` when it may have been billed without saying (a timeout, a
    broken connection), ``retryable`` when the same read may succeed if made again (a rate
    limit, an overloaded or failing API, a lost connection, a timeout), and ``image_sha256``
    when an image was sent (of the bytes sent). A reader makes one attempt per read: retrying
    is the caller's."""

    usage: ReadUsage | None = None
    maybe_billed: bool = False
    retryable: bool = False
    image_sha256: str | None = None


class ReaderUnavailableError(ReaderError):
    """The API could not be reached, timed out, failed, or refused the key (or there is none).
    The message names ``ANTHROPIC_API_KEY`` when the key is the problem, never the key."""


class ReaderRefusedError(ReaderError):
    """The model declined to answer (stop reason ``refusal``)."""


class ReaderTruncatedError(ReaderError):
    """The answer was cut off at the token limit (stop reason ``max_tokens``)."""


class ReaderOutputError(ReaderError):
    """The answer did not match the schema; the message carries the validation text."""


class BudgetReachedError(ReaderError):
    """This month's spend has reached ``CLAUDE__MONTHLY_BUDGET_USD``: no call was made."""


@dataclass(frozen=True, slots=True)
class ReadRequest:
    model: str
    tag: str  # the prompt id, e.g. "extraction/claude-v1"; recorded in the trace
    system: str
    text: str  # the user turn after the image; a retry's carries the validation feedback
    # The stored photo as stored, sent as it is unless it exceeds the API's limits; None for a
    # text-only request (an enrichment guess).
    image: bytes | None
    max_tokens: int
    attempt: int = 1  # 1-based, across resumed runs: recorded in the trace


@dataclass(frozen=True, slots=True)
class ReadResult[M]:
    value: M
    usage: ReadUsage
    stop_reason: str
    image_sha256: str | None = None  # of the bytes sent; None: the request's image as it is


class ReceiptReader(Protocol):
    def read[M](self, request: ReadRequest, schema: type[M]) -> ReadResult[M]:
        """The answer validated as ``schema``. Raises a ``ReaderError`` subclass for every
        failure; no SDK exception escapes."""
        ...
