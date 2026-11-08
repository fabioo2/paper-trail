"""The Tesseract adapter, running the real ``tesseract`` of the dev image (S2-AC-07, AC-A-03).

Pinned (docs/tests/agent-core-A.md): ``TesseractOcr(settings: OcrSettings)``; ``read(image)``
returns ``OcrResult(text, mean_confidence, rotation_degrees)`` with the confidence as a fraction
in 0..1 (None when no word was found) and the rotation the text was read at
(docs/tests/ocr-rotation.md); a run longer than ``ocr.timeout_s`` or bytes that are not an image
raise ``OcrError``. The receipts are rendered by ``tests/images.py``.
"""

import io
import re

import pytest
from PIL import Image

from paper_trail.adapters.tesseract.ocr import TesseractOcr
from paper_trail.config import OcrSettings
from paper_trail.ports.ocr import OcrError
from tests.images import PRINTED_RECEIPTS, PrintedReceipt, png_bytes


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).upper()


@pytest.fixture
def ocr() -> TesseractOcr:
    return TesseractOcr(OcrSettings())


@pytest.mark.parametrize("printed", PRINTED_RECEIPTS, ids=lambda receipt: receipt.store_name)
def test_s2_ac_07_reads_store_name_and_total(ocr: TesseractOcr, printed: PrintedReceipt) -> None:
    """S2-AC-07: non-empty text containing the store name and the total, for three receipts."""
    result = ocr.read(printed.render())

    text = _normalized(result.text)
    assert printed.store_name in text
    assert printed.total in text
    assert result.mean_confidence is not None
    assert 0.0 < result.mean_confidence <= 1.0


def test_an_upright_receipt_is_read_without_rotation(ocr: TesseractOcr) -> None:
    assert ocr.read(PRINTED_RECEIPTS[0].render()).rotation_degrees == 0


def test_a_sideways_receipt_is_read_upright(ocr: TesseractOcr) -> None:
    """docs/tests/ocr-rotation.md: a receipt lying sideways (no word at all upright, measured)
    is read at the rotation that puts it upright."""
    printed = PRINTED_RECEIPTS[0]
    buffer = io.BytesIO()
    with Image.open(io.BytesIO(printed.render())) as upright:
        upright.rotate(90, expand=True, fillcolor="white").save(buffer, format="PNG")

    result = ocr.read(buffer.getvalue())

    text = _normalized(result.text)
    assert printed.store_name in text
    assert printed.total in text
    assert result.rotation_degrees == 270  # the image was turned 90 CCW; 270 more CCW is upright
    assert result.mean_confidence is not None
    assert result.mean_confidence >= OcrSettings().rotation_min_confidence


def test_a_blank_image_has_no_text_and_no_confidence(ocr: TesseractOcr) -> None:
    result = ocr.read(png_bytes(400, 200, (255, 255, 255)))

    assert result.text.strip() == ""
    assert result.mean_confidence is None


def test_s2_ac_07_a_timeout_is_an_ocr_error() -> None:
    """S2-AC-07: a run past the timeout raises OcrError (the job retries with backoff)."""
    slow = TesseractOcr(OcrSettings(timeout_s=0.000_001))

    with pytest.raises(OcrError, match=r"timed out|timeout"):
        slow.read(PRINTED_RECEIPTS[0].render())


def test_bytes_that_are_not_an_image_are_an_ocr_error(ocr: TesseractOcr) -> None:
    with pytest.raises(OcrError):
        ocr.read(b"%PDF-1.7 not an image")
