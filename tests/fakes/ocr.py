"""In-memory OCR (implements ``paper_trail.ports.ocr.OcrPort``): returns a fixed text.

The text is returned unmasked on purpose, as Tesseract would return it: masking is the
``ocr_read`` tool's job (AC-A-04), so a card number in this text must never get further.
``rotation_degrees`` stands for the rotation the adapter chose for a sideways photo
(docs/tests/ocr-rotation.md); a test sets it on ``harness.ocr`` before processing.
"""

from paper_trail.ports.ocr import OcrResult


class FakeOcr:
    def __init__(
        self, text: str, mean_confidence: float | None = 0.87, rotation_degrees: int = 0
    ) -> None:
        self.text = text
        self.mean_confidence = mean_confidence
        self.rotation_degrees = rotation_degrees
        self.images: list[bytes] = []

    def read(self, image: bytes) -> OcrResult:
        self.images.append(image)
        return OcrResult(
            text=self.text,
            mean_confidence=self.mean_confidence,
            rotation_degrees=self.rotation_degrees,
        )
