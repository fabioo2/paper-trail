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
    CATALOG = auto()  # the code printed on the line names a product (ADR-0018)
    ALIAS = auto()
    VECTOR = auto()
    LLM = auto()
    NEW = auto()


class CodeKind(StrEnum):
    """What the leading code of a receipt line is (barcode-lookup plan section 2)."""

    GTIN = auto()
    PLU = auto()
    IN_STORE = auto()  # a store's own scale or counter code, or a GS1 restricted-circulation one
    STORE_ITEM = auto()  # a store's item number (every Costco code)
    NONE = auto()  # digits that are no product code (a coupon, a 9-10 digit number)


class CodeScheme(StrEnum):
    """How a code is stored in ``product_codes``: barcodes and PLUs name one product at every
    store; store codes only at their store."""

    GTIN = auto()
    PLU = auto()
    STORE = auto()


class CodeSource(StrEnum):
    """Who attached a code to a product. A person's source replaces another product's mapping;
    a machine's never does (``PERSON_CODE_SOURCES``)."""

    MANUAL = auto()
    IMPORT = auto()
    ADOPTED = auto()
    CORRECTION = auto()
    REVIEW = auto()
    RECEIPT = auto()
    OFF = auto()
    PLU = auto()
    BACKFILL = auto()


class FieldSource(StrEnum):
    """Where a product's name, brand, size, category or tags came from (ADR-0018)."""

    RECEIPT_TEXT = auto()
    PERSON = auto()
    PLU = auto()
    OFF = auto()
    OFF_LLM = auto()
    LEGACY_MODEL = auto()  # written by a model before 0006; never confirmed
