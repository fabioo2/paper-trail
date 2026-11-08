"""Image fixtures built with Pillow at test time (no binary files in the repository).

Used by the integration suites of the real image store and the Tesseract adapter, and by the
CLI tests. Receipts are rendered as clean black-on-white text in Pillow's scalable default font,
large enough for Tesseract to read reliably.
"""

import io
from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

ORIENTATION = 0x0112  # EXIF tag
GPS_IFD = 0x8825  # EXIF tag of the GPS block
ROTATE_90_CW = 6  # EXIF orientation: the camera was turned; display rotated 90 degrees clockwise


def _encode(image: Image.Image, image_format: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


def solid(
    width: int = 64, height: int = 32, color: tuple[int, int, int] = (200, 30, 30)
) -> Image.Image:
    return Image.new("RGB", (width, height), color)


def encode_png(image: Image.Image) -> bytes:
    return _encode(image, "PNG")


def png_bytes(
    width: int = 64, height: int = 32, color: tuple[int, int, int] = (200, 30, 30)
) -> bytes:
    return _encode(solid(width, height, color), "PNG")


def rgba_png_bytes() -> bytes:
    return _encode(Image.new("RGBA", (40, 20), (10, 120, 200, 128)), "PNG")


def webp_bytes() -> bytes:
    return _encode(solid(48, 24, (30, 160, 60)), "WEBP")


def heic_bytes() -> bytes:
    from pillow_heif import register_heif_opener  # noqa: PLC0415 - only the HEIC fixture needs it

    register_heif_opener()
    return _encode(solid(48, 24, (60, 60, 180)), "HEIF")


def landscape_jpeg_with_exif() -> bytes:
    """A 60x20 JPEG whose EXIF says "rotate 90 degrees clockwise" and that carries a GPS block.

    Its left third is black, so after the orientation is applied the black band is on top.
    """
    image = solid(60, 20, (255, 255, 255))
    ImageDraw.Draw(image).rectangle((0, 0, 19, 19), fill=(0, 0, 0))
    exif = Image.Exif()
    exif[ORIENTATION] = ROTATE_90_CW
    exif[GPS_IFD] = {1: "N", 2: (43.0, 39.0, 0.0), 3: "W", 4: (79.0, 23.0, 0.0)}
    return _encode(image, "JPEG", exif=exif, quality=95)


def iphone_mpo_with_exif() -> bytes:
    """An iPhone-style MPO: a 60x20 JPEG primary frame (EXIF: rotate 90 degrees clockwise, a
    GPS block, left third black) plus a 16x16 blue secondary frame (a depth or gain map)."""
    primary = solid(60, 20, (255, 255, 255))
    ImageDraw.Draw(primary).rectangle((0, 0, 19, 19), fill=(0, 0, 0))
    exif = Image.Exif()
    exif[ORIENTATION] = ROTATE_90_CW
    exif[GPS_IFD] = {1: "N", 2: (43.0, 39.0, 0.0), 3: "W", 4: (79.0, 23.0, 0.0)}
    secondary = solid(16, 16, (0, 0, 255))
    return _encode(primary, "MPO", save_all=True, append_images=[secondary], exif=exif, quality=95)


def animated_bytes(image_format: str) -> bytes:
    """A two-frame 48x24 animation (green, then blue) in ``image_format``."""
    frames = [solid(48, 24, (30, 160, 60)), solid(48, 24, (0, 0, 255))]
    return _encode(frames[0], image_format, save_all=True, append_images=frames[1:])


@dataclass(frozen=True, slots=True)
class PrintedReceipt:
    store_name: str
    total: str
    lines: tuple[str, ...]

    def render(self) -> bytes:
        return receipt_jpeg([self.store_name, *self.lines, f"TOTAL {self.total}"])


PRINTED_RECEIPTS = (
    PrintedReceipt("COSTCO WHOLESALE", "26.83", ("KS ORG EGGS 24CT 8.99", "SUBTOTAL 25.55")),
    PrintedReceipt("FRESHCO", "14.27", ("BANANAS 1.36", "WHOLE MILK 4L 6.49")),
    PrintedReceipt("SHOPPERS DRUG MART", "9.48", ("TOOTHPASTE 4.99", "DENTAL FLOSS 3.99")),
)


def receipt_jpeg(lines: Sequence[str]) -> bytes:
    """A receipt photo stand-in: each line of text on a white page, as a JPEG."""
    font = ImageFont.load_default(size=40)
    width, line_height, margin = 1000, 60, 40
    image = Image.new("RGB", (width, margin * 2 + line_height * len(lines)), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((margin, margin + index * line_height), line, fill=(0, 0, 0), font=font)
    return _encode(image, "JPEG", quality=95)


def decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image
