"""Is a receipt complete enough to be saved as COMPLETED? (ADR-0017, 2026-11-08)

Adding up is not enough: a receipt without a purchase date yields no price observation, and
one without a store name matches no alias. The extraction graph checks both beside the
arithmetic, with messages the model can act on in its next attempt.
"""

from datetime import date

from paper_trail.domain.rules.arithmetic import ArithmeticIssue, IssueCode

COPY_THE_DATE = "copy the date exactly as printed"


def check_completeness(
    store_name: str, date_text: str | None, purchased_on: date | None
) -> tuple[ArithmeticIssue, ...]:
    """The issues that keep the receipt from being complete: a blank store name, and no
    purchase date (``date_text`` as read, ``purchased_on`` what it parsed to up to today)."""
    issues = []
    if not store_name.strip():
        message = "no store name was read: copy the store name printed at the top"
        issues.append(ArithmeticIssue(IssueCode.MISSING_STORE, message, None, None, None))
    if purchased_on is None:
        message = (
            f"no purchase date was read: {COPY_THE_DATE}"
            if date_text is None or not date_text.strip()
            else f'the purchase date "{date_text}" is not a date up to today: {COPY_THE_DATE}'
        )
        issues.append(ArithmeticIssue(IssueCode.MISSING_DATE, message, None, None, None))
    return tuple(issues)
