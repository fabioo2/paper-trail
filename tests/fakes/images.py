"""In-memory image store (implements ``paper_trail.ports.images.ImageStore``).

It does not decode or re-encode: the stored bytes are the input bytes and the hash is theirs, so
unit tests need no imaging library. It recognises images by their magic bytes, like the real
store, and lays out paths the same way (``ab/cd/<sha256>.jpg``).
"""

import hashlib

from paper_trail.domain.errors import NotFoundError, UnsupportedMediaError
from paper_trail.ports.images import StoredImage

# A stand-in PNG for unit tests: the PNG signature followed by arbitrary bytes.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def fake_png(label: str) -> bytes:
    """Distinct "image" bytes per label, accepted by FakeImageStore (never decoded)."""
    return PNG_SIGNATURE + label.encode()


def turned(rotation_degrees: int) -> bytes:
    """What ``FakeImageStore.open_upright`` appends to a photo it was asked to turn."""
    return f"|turned {rotation_degrees}".encode()


def is_image(data: bytes) -> bool:
    """JPEG, PNG, WebP or HEIC/HEIF, by magic bytes (never by file name or content type)."""
    return (
        data.startswith((b"\xff\xd8\xff", PNG_SIGNATURE))
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or data[4:12] in {b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"}
    )


class FakeImageStore:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def save(self, data: bytes) -> StoredImage:
        if not is_image(data):
            msg = "not a JPEG, PNG, WebP or HEIC image"
            raise UnsupportedMediaError(msg)
        sha256 = hashlib.sha256(data).hexdigest()
        path = f"{sha256[:2]}/{sha256[2:4]}/{sha256}.jpg"
        self.files[path] = data
        return StoredImage(
            relative_path=path, sha256=sha256, width=1, height=1, content_type="image/jpeg"
        )

    def open(self, relative_path: str) -> bytes:
        try:
            return self.files[relative_path]
        except KeyError:
            msg = f"no stored image at {relative_path!r}"
            raise NotFoundError(msg) from None

    def open_upright(self, relative_path: str, rotation_degrees: int) -> bytes:
        """``open``'s bytes; turned, with ``turned(rotation_degrees)`` appended (no decoding)."""
        data = self.open(relative_path)
        return data + turned(rotation_degrees) if rotation_degrees % 360 else data
