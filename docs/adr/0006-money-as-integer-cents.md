# 6. Money as integer minor units, quantities as Decimal

Date: 2026-11-06

## Status

Accepted

## Context

Arithmetic validation is a core guardrail. Floating point cannot represent cents exactly, and receipts include weighed items (0.452 kg) and negative lines (discounts, coupons).

## Decision

- `Money(amount_cents: int, currency: str)` value object; currency is ISO 4217, default `CAD`, one currency per receipt. Mixing currencies raises `CurrencyMismatchError`.
- Quantities are `decimal.Decimal`, stored as TEXT through an SQLAlchemy `TypeDecorator` and serialised as strings in JSON.
- Rounding is `ROUND_HALF_UP` to the cent, applied only in `domain/rules`.
- The API exposes money as `{ "amount_cents": int, "currency": str }`; the frontend formats with `Intl.NumberFormat`.
- LLM output schemas use integer cents too, so the model never emits floats for money.

## Consequences

- Exact arithmetic checks with explicit, documented tolerances (per-line rounding of quantity x unit price).
- Slightly more verbose API payloads.
