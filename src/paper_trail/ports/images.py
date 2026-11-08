"""Receipt photos, stored once by content (PLAN 4, S2-AC-03)."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredImage:
    relative_path: str  # "<sha[:2]>/<sha[2:4]>/<sha>.jpg" under the image root
    sha256: str  # of the stored bytes
    width: int
    height: int
    content_type: str


class ImageStore(Protocol):
    def save(self, data: bytes) -> StoredImage:
        """Store an image (idempotent: the same bytes give the same StoredImage).

        Raises UnsupportedMediaError for bytes that are not a readable image.
        """
        ...

    def open(self, relative_path: str) -> bytes:
        """The stored bytes; NotFoundError for anything that is not a stored image."""
        ...

    def open_upright(self, relative_path: str, rotation_degrees: int) -> bytes:
        """The stored image as the OCR read it: turned ``rotation_degrees`` counter-clockwise
        (``OcrResult.rotation_degrees``, a multiple of 90) as a JPEG; ``open``'s bytes at 0.
        NotFoundError as ``open``."""
        ...
