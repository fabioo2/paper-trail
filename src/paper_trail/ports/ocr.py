"""Optical character recognition of a receipt photo (PLAN 4, ADR-0010)."""

from dataclasses import dataclass
from typing import Protocol


class OcrError(Exception):
    """The image could not be read: not an image, or the OCR timed out or failed."""


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str  # unmasked: the caller masks card numbers before the text goes anywhere
    mean_confidence: float | None  # 0..1 over the recognised words; None when there were none
    rotation_degrees: int = 0  # how far the image was turned to read it (0, 90, 180 or 270)


class OcrPort(Protocol):
    def read(self, image: bytes) -> OcrResult: ...
