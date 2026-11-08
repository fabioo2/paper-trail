"""``OcrPort`` on the Tesseract command line (ADR-0010, S2-AC-07).

The image is prepared once with Pillow (orientation, grayscale, autocontrast, small images
scaled up), then ``tesseract`` writes the text and its word confidences (TSV) in one run, bounded
by ``ocr.timeout_s``. The text is returned unmasked: the ``ocr_read`` tool masks it.

Rotation fallback (ADR-0010 amendment 2026-11-08): a phone photo can be stored with the receipt
lying sideways, which Tesseract's own orientation detection refuses. When the upright read's mean
confidence is below ``ocr.rotation_min_confidence`` (or it found no words), the prepared image is
also read turned 90, 180 and 270 degrees counter-clockwise, and the most confident read wins.
"""

import csv
import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from paper_trail.config import OCR_RETRY_ROTATIONS, OcrSettings
from paper_trail.ports.ocr import OcrError, OcrResult

register_heif_opener()

TESSERACT = "tesseract"
MIN_WIDTH = 1000  # narrower images (small photos, thumbnails) are scaled up to this width
_PERCENT = 100.0


class TesseractOcr:
    """Implements ``paper_trail.ports.ocr.OcrPort``."""

    def __init__(self, settings: OcrSettings) -> None:
        self._settings = settings

    def read(self, image: bytes) -> OcrResult:
        """Read upright; below the confidence threshold, also read at each other rotation.

        Rotations are counter-clockwise (``rotation_degrees`` 90: the image was turned a quarter
        turn to the left), tried in the order of ``OCR_RETRY_ROTATIONS`` (90, 180, 270). The
        highest mean confidence wins; ties keep the earlier read, a read without words never
        beats one with words, and when no rotation finds words the upright read is returned.
        Each run has its own timeout.
        """
        prepared = _prepared(image)
        best = self._run(prepared, rotation_degrees=0)
        if _confident(best, self._settings.rotation_min_confidence):
            return best
        for degrees in OCR_RETRY_ROTATIONS:
            # Quarter turns with expand: Pillow transposes, losslessly, instead of resampling.
            turned = prepared.rotate(degrees, expand=True)
            candidate = self._run(turned, rotation_degrees=degrees)
            if _more_confident(candidate, best):
                best = candidate
        return best

    def _run(self, image: Image.Image, *, rotation_degrees: int) -> OcrResult:
        """One ``tesseract`` run over the image, bounded by ``ocr.timeout_s``."""
        with tempfile.TemporaryDirectory(prefix="paper-trail-ocr-") as workdir:
            base = Path(workdir) / "out"
            command = [
                TESSERACT,
                "stdin",
                str(base),
                "-l",
                self._settings.lang,
                "--psm",
                str(self._settings.psm),
                "txt",
                "tsv",
            ]
            try:
                subprocess.run(  # noqa: S603 - fixed argv; the image goes in on stdin
                    command,
                    input=_png(image),
                    capture_output=True,
                    timeout=self._settings.timeout_s,
                    check=True,
                )
            except subprocess.TimeoutExpired:
                msg = f"tesseract timed out after {self._settings.timeout_s} s"
                raise OcrError(msg) from None
            except (subprocess.CalledProcessError, OSError) as error:
                msg = f"tesseract failed: {type(error).__name__}"
                raise OcrError(msg) from None
            text = base.with_suffix(".txt").read_text(encoding="utf-8")
            confidence = _mean_confidence(base.with_suffix(".tsv").read_text(encoding="utf-8"))
        return OcrResult(text=text, mean_confidence=confidence, rotation_degrees=rotation_degrees)


def _confident(result: OcrResult, threshold: float) -> bool:
    """Good enough to skip the rotations? A zero threshold accepts any read, even a wordless one."""
    if threshold == 0:
        return True
    return result.mean_confidence is not None and result.mean_confidence >= threshold


def _more_confident(candidate: OcrResult, best: OcrResult) -> bool:
    """Strictly more confident (ties keep ``best``); a read without words never wins."""
    if candidate.mean_confidence is None:
        return False
    return best.mean_confidence is None or candidate.mean_confidence > best.mean_confidence


def _prepared(data: bytes) -> Image.Image:
    """ADR-0010 preprocessing: upright, grayscale, autocontrast, at least MIN_WIDTH wide."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            gray = ImageOps.autocontrast(ImageOps.exif_transpose(image).convert("L"))
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as error:
        msg = f"not a readable image ({type(error).__name__})"
        raise OcrError(msg) from None
    if gray.width < MIN_WIDTH:
        height = round(gray.height * MIN_WIDTH / gray.width)
        gray = gray.resize((MIN_WIDTH, height), Image.Resampling.LANCZOS)
    return gray


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _mean_confidence(tsv: str) -> float | None:
    """The mean confidence of the recognised words, as a fraction; None without words."""
    confidences = [
        float(row["conf"])
        for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
        if row.get("text", "").strip() and float(row.get("conf") or -1) >= 0
    ]
    if not confidences:
        return None
    return sum(confidences) / len(confidences) / _PERCENT
