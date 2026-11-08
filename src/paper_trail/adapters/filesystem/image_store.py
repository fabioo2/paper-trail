"""Receipt photos on the data volume, stored once by content (PLAN 4, S2-AC-03, S2-AC-04).

Every image is re-encoded as an upright RGB JPEG without any metadata (EXIF, GPS, ICC), so
nothing but the picture is kept, and stored as ``<root>/<sha[:2]>/<sha[2:4]>/<sha>.jpg`` where
``sha`` is the SHA-256 of the stored JPEG. JPEG, PNG, WebP and HEIC/HEIF are accepted, judged by
their content; an image above ``max_pixels`` is refused before it is decoded. An iPhone JPEG is
an MPO (a JPEG primary image plus extra frames such as a depth or HDR gain map): it is a JPEG,
and like every multi-frame image only its first (primary) frame is kept.
"""

import hashlib
import io
import tempfile
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from paper_trail.domain.errors import NotFoundError, UnsupportedMediaError
from paper_trail.ports.images import StoredImage

register_heif_opener()

# Pillow's format names; "MPO" is a JPEG with extra frames (iPhone photos).
ACCEPTED_FORMATS = frozenset({"JPEG", "MPO", "PNG", "WEBP", "HEIF"})
CONTENT_TYPE = "image/jpeg"
JPEG_QUALITY = 90


class FilesystemImageStore:
    """Implements ``paper_trail.ports.images.ImageStore``."""

    def __init__(self, root: Path, *, max_pixels: int) -> None:
        self._root = root
        self._max_pixels = max_pixels

    def save(self, data: bytes) -> StoredImage:
        jpeg, width, height = _normalized_jpeg(data, self._max_pixels)
        sha256 = hashlib.sha256(jpeg).hexdigest()
        relative = f"{sha256[:2]}/{sha256[2:4]}/{sha256}.jpg"
        path = self._root / relative
        if not path.is_file():
            _write_atomically(path, jpeg)
        return StoredImage(
            relative_path=relative,
            sha256=sha256,
            width=width,
            height=height,
            content_type=CONTENT_TYPE,
        )

    def open(self, relative_path: str) -> bytes:
        path = self._resolve(relative_path)
        if path is None or not path.is_file():
            msg = f"no stored image at {relative_path!r}"
            raise NotFoundError(msg)
        return path.read_bytes()

    def open_upright(self, relative_path: str, rotation_degrees: int) -> bytes:
        data = self.open(relative_path)
        if rotation_degrees % 360 == 0:
            return data
        with Image.open(io.BytesIO(data)) as image:
            # Quarter turns with expand, as the OCR adapter turns its reads: a lossless transpose.
            turned = image.rotate(rotation_degrees, expand=True)
        return _jpeg(turned)

    def _resolve(self, relative_path: str) -> Path | None:
        """The path inside the root, or None for anything that could lead outside it."""
        candidate = Path(relative_path)
        if not relative_path or candidate.is_absolute() or ".." in candidate.parts:
            return None
        root = self._root.resolve()
        resolved = (root / candidate).resolve()
        return resolved if resolved.is_relative_to(root) else None


def _normalized_jpeg(data: bytes, max_pixels: int) -> tuple[bytes, int, int]:
    """``data`` as an upright, metadata-free RGB JPEG, with its width and height."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ACCEPTED_FORMATS:
                msg = f"{image.format} images are not supported (JPEG, PNG, WebP or HEIC)"
                raise UnsupportedMediaError(msg)
            if image.width * image.height > max_pixels:
                msg = (
                    f"the image has {image.width * image.height:,} pixels,"
                    f" more than the limit of {max_pixels:,} pixels"
                )
                raise UnsupportedMediaError(msg)
            image.seek(0)  # the primary frame of an MPO or an animation
            upright = ImageOps.exif_transpose(image)
            rgb = upright.convert("RGB")
    except UnsupportedMediaError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as error:
        msg = f"not a readable JPEG, PNG, WebP or HEIC image ({type(error).__name__})"
        raise UnsupportedMediaError(msg) from None
    return _jpeg(rgb), rgb.width, rgb.height


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as out:
        out.write(data)
        temporary = Path(out.name)
    try:
        temporary.chmod(0o644)  # like any other file on the volume
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
