"""What the models must answer (S3-AC-11, PLAN 6, ADR-0009, ADR-0017).

The schema is sent to Ollama as ``format`` and the answer is validated against it. Extraction
``extraction/v1``: no extra fields, integer cents, a positive decimal-string quantity, a
three-letter currency; the date stays a string here and is parsed in code
(``ExtractedReceipt.to_draft``). ``extraction/v2`` and ``vision/v1``: every number and the date
as printed (strings), converted in code by ``PrintedExtraction.to_output``. Matching:
"same" or "different" for one line and the candidates it was offered. Enrichment: a product's
name, brand, size, one category of the fixed enum (S4-AC-05) and tags.
"""

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from paper_trail.application.payloads import (
    CurrencyCode,
    ExtractedLine,
    ExtractedReceipt,
    Last4,
    ProductEnrichment,
)
from paper_trail.domain.enums import LineKind
from paper_trail.domain.errors import PrintedValueError
from paper_trail.domain.money import Money
from paper_trail.domain.rules.printed import (
    LineAmount,
    PricedLine,
    TaxReading,
    is_item_reference,
    line_kind,
    line_quantity,
    parse_amount,
    parse_quantity,
    parse_tax,
    parse_unit_price,
    printed_tax,
    share_amounts,
)


class ExtractionOutput(ExtractedReceipt):
    """The receipt as read, plus how sure the model is of its reading (0..1)."""

    confidence: float = Field(ge=0.0, le=1.0)

    def receipt_json(self) -> dict[str, Any]:
        """The receipt without the confidence: the ``check_arithmetic`` tool's input."""
        return self.model_dump(mode="json", exclude={"confidence"})

    def to_output(self) -> Self:
        """Already integer cents (``extraction/v1``)."""
        return self

    def notes(self) -> list[str]:
        """Nothing is set aside: the answer is the output."""
        return []


class PrintedLine(BaseModel):
    """One printed line, every value copied as printed; signs and cents are computed in code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_text: StrictStr = Field(description="the line's text as printed, without the amount")
    description: StrictStr = Field(description="a readable product name, without item codes")
    quantity: StrictStr | None = Field(
        description='only from an "N @ price" or weight line, e.g. "2" or "0.460 kg"; else null'
    )
    unit_price: StrictStr | None = Field(
        description='as printed on an "N @ price" or weight line, e.g. "1.29" or "$3.31/kg"'
    )
    amount: StrictStr | None = Field(
        description='the line\'s amount exactly as printed, e.g. "1.29", "4.00-", "17.08 A"'
    )
    kind: LineKind


class PrintedExtraction(BaseModel):
    """The receipt as printed (``extraction/v2``, ``vision/v1``, ADR-0017): the model copies,
    ``to_output`` computes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store_name: StrictStr
    purchased_on: StrictStr | None = Field(description='the date as printed, e.g. "26/11/02"')
    currency: CurrencyCode
    subtotal: StrictStr | None
    taxes: list[StrictStr] = Field(description="every tax amount as printed")
    total: StrictStr
    payment_last4: Last4 | None
    items: list[PrintedLine]
    confidence: float = Field(ge=0.0, le=1.0)

    def notes(self) -> list[str]:
        """What ``to_output`` decided beyond copying, for the trace (call after ``to_output``
        succeeded): taxes ignored because the printed subtotal is the printed total, and taxes
        computed from a printed base and rate."""
        if not self.taxes:
            return []
        if self.subtotal is not None and parse_amount(self.subtotal) == parse_amount(self.total):
            return [f"tax {', '.join(self.taxes)} ignored: subtotal equals total"]
        readings = (parse_tax(tax) for tax in self.taxes)
        return [
            f"tax {Money(reading.cents, self.currency).amount_text} computed from"
            f" {reading.computed_from}"
            for reading in readings
            if reading.computed_from is not None
        ]

    def to_output(self) -> ExtractionOutput:
        """The answer in integer cents. Every unreadable value is named in one
        ``PrintedValueError`` that quotes the printed text (the next attempt's feedback)."""
        problems: list[str] = []

        def amount(field: str, text: str) -> int:
            return _read(problems, field, parse_amount, text, fallback=0)

        def quantity(field: str, text: str | None) -> Decimal:
            return _read(problems, field, parse_quantity, text, fallback=Decimal(1))

        total = amount("total", self.total)
        subtotal = None if self.subtotal is None else amount("subtotal", self.subtotal)
        taxes = [
            _read(problems, "tax", parse_tax, tax, fallback=TaxReading(0)).cents
            for tax in self.taxes
        ]
        amounts: list[LineAmount] = []
        for number, line in enumerate(self.items, start=1):
            unit = parse_unit_price(line.unit_price)
            cents = None if line.amount is None else amount(f"line {number} amount", line.amount)
            read = quantity(f"line {number} quantity", line.quantity)
            amounts.append(LineAmount(line_quantity(read, unit, cents), unit, cents))
        priced: list[PricedLine] = []
        try:
            priced = share_amounts(amounts)
        except PrintedValueError as error:
            problems.append(str(error))
        if problems:
            raise PrintedValueError("; ".join(problems))
        return ExtractionOutput(
            store_name=self.store_name,
            purchased_on=self.purchased_on,
            currency=self.currency,
            subtotal_cents=subtotal,
            tax_cents=printed_tax(subtotal, total, taxes),
            total_cents=total,
            payment_last4=self.payment_last4,
            items=[
                _extracted_line(line, numbers)
                for line, numbers in zip(self.items, priced, strict=True)
            ],
            confidence=self.confidence,
        )


def _read[T, V](
    problems: list[str], field: str, parse: Callable[[V], T], text: V, *, fallback: T
) -> T:
    """``parse(text)``; when it cannot be read, the problem is noted against ``field`` and
    ``fallback`` stands in, so that every unreadable value is reported at once."""
    try:
        return parse(text)
    except PrintedValueError as error:
        problems.append(f"{field} {error}")
        return fallback


def _extracted_line(line: PrintedLine, numbers: PricedLine) -> ExtractedLine:
    printed_kind = LineKind.DISCOUNT if is_item_reference(line.raw_text) else line.kind
    kind, cents = line_kind(printed_kind, numbers.cents)
    unit = numbers.unit_price_cents
    return ExtractedLine(
        raw_text=line.raw_text,
        description=line.description,
        quantity=str(numbers.quantity),
        # A discount's unit price carries its sign, so that quantity x unit price = amount.
        unit_price_cents=None if unit is None else (-abs(unit) if cents < 0 else unit),
        line_total_cents=cents,
        kind=kind,
    )


class ExtractionAnswer(Protocol):
    """What an extraction prompt's model answers: anything that converts to the output."""

    def to_output(self) -> ExtractionOutput: ...

    def notes(self) -> list[str]:
        """What the conversion set aside, recorded as trace notes (never silent)."""
        ...


# The answer model of each extraction prompt id ("<name>/<version>", examples or not).
ANSWER_MODELS: Mapping[str, type[ExtractionAnswer]] = {
    "extraction/v1": ExtractionOutput,
    "extraction/v2": PrintedExtraction,
    "vision/v1": PrintedExtraction,
}


def answer_model(name: str, version: str) -> type[ExtractionAnswer]:
    """The answer model of the prompt ``name/version``; LookupError when there is none."""
    prompt_id = f"{name}/{version}"
    try:
        return ANSWER_MODELS[prompt_id]
    except KeyError:
        msg = f"no answer model for the prompt {prompt_id}"
        raise LookupError(msg) from None


class MatchDecision(BaseModel):
    """Is the receipt line one of the offered products? (the matching model's answer)"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    line_position: StrictInt = Field(ge=0)
    decision: Literal["same", "different"]
    product_id: StrictInt | None  # the offered product it is, for "same"
    confidence: float = Field(ge=0.0, le=1.0)


class EnrichmentOutput(ProductEnrichment):
    """What a new product is, plus how sure the model is (0..1); below
    ``GRAPH__MIN_CONFIDENCE`` the product is left as it is."""

    confidence: float = Field(ge=0.0, le=1.0)

    def enrichment_json(self) -> dict[str, Any]:
        """The answer without the confidence: the ``save_enrichment`` tool's ``enrichment``."""
        return self.model_dump(mode="json", exclude={"confidence"})
