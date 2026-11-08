"""Enumerations of the domain. Values are the lowercase member names, used verbatim in the
database (as CHECK-constrained TEXT) and in the HTTP API (PLAN 1.2).

Adding a member to an enum stored in the database requires a migration that widens its CHECK
constraint (``tests/integration/test_schema_matches_metadata.py`` fails until it exists).
"""

from enum import StrEnum, auto


class Category(StrEnum):
    PRODUCE = auto()
    MEAT_SEAFOOD = auto()
    DAIRY_EGGS = auto()
    BAKERY = auto()
    DELI_PREPARED = auto()
    PANTRY = auto()
    FROZEN = auto()
    BEVERAGES = auto()
    SNACKS_CANDY = auto()
    ALCOHOL = auto()
    HOUSEHOLD = auto()
    PERSONAL_CARE = auto()
    HEALTH_PHARMACY = auto()
    BABY = auto()
    PET = auto()
    ELECTRONICS = auto()
    CLOTHING = auto()
    HOME_GARDEN = auto()
    AUTOMOTIVE_FUEL = auto()
    RESTAURANT = auto()
    SERVICES = auto()
    OTHER = auto()


class LineKind(StrEnum):
    """What a receipt line is. Only DISCOUNT lines carry negative totals."""

    ITEM = auto()
    DISCOUNT = auto()
    DEPOSIT = auto()
    FEE = auto()


class Unit(StrEnum):
    EACH = auto()
    G = auto()
    KG = auto()
    ML = auto()
    L = auto()
    LB = auto()
    OZ = auto()


class ReceiptStatus(StrEnum):
    PROCESSING = auto()
    NEEDS_REVIEW = auto()
    COMPLETED = auto()
    FAILED = auto()


class ProcessingStage(StrEnum):
    """Where a receipt is in the pipeline; reported live from Stage 2 (SSE)."""

    QUEUED = auto()
    WAITING_FOR_GPU = auto()
    READING = auto()
    EXTRACTING = auto()
    VALIDATING = auto()
    MATCHING = auto()
    ENRICHING = auto()
    SAVING = auto()
    DONE = auto()
    NEEDS_REVIEW = auto()
    FAILED = auto()


class JobKind(StrEnum):
    PROCESS_RECEIPT = auto()


class JobStatus(StrEnum):
    PENDING = auto()
    RUNNING = auto()
    SUCCEEDED = auto()
    FAILED = auto()


class ReviewReason(StrEnum):
    VALIDATION_FAILED = auto()
    LOW_CONFIDENCE = auto()
    EXTRACTION_FAILED = auto()
    UNMATCHED_PRODUCT = auto()


class ReviewStatus(StrEnum):
    OPEN = auto()
    RESOLVED = auto()
    DISMISSED = auto()


class AliasSource(StrEnum):
    """Who taught Paper Trail an alias; a USER_CORRECTION is never overwritten by a machine."""

    IMPORT = auto()
    EXTRACTED = auto()
    MATCHED = auto()
    USER_CORRECTION = auto()


class CorrectionKind(StrEnum):
    PRODUCT_LINK = auto()
    CATEGORY = auto()
    FIELD = auto()


class AgentRole(StrEnum):
    """Who calls a tool or emits a trace event; the tool allowlists are per role (ADR-0013)."""

    SUPERVISOR = auto()
    EXTRACTION = auto()
    VALIDATION = auto()
    MATCHING = auto()
    ENRICHMENT = auto()


class MatchSource(StrEnum):
    """How a line was linked to its product, in matching order (PLAN 6)."""

    MEMORY = auto()  # a person's correction
    ALIAS = auto()
    VECTOR = auto()
    LLM = auto()
    NEW = auto()
