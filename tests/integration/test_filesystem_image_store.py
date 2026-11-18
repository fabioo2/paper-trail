"""The filesystem image store with real Pillow and pillow-heif (S2-AC-03, S2-AC-04, AC-A-02).

Pinned (docs/tests/agent-core-A.md): ``FilesystemImageStore(root, *, max_pixels)`` writes
``<root>/<sha[:2]>/<sha[2:4]>/<sha>.jpg``; the hash is of the stored (re-encoded) JPEG; the
stored file has no EXIF at all, the orientation applied, RGB mode; ``StoredImage.width`` and
``height`` are the stored image's; an image above ``max_pixels`` raises
``UnsupportedMediaError`` whose message mentions pixels, before it is decoded in full.
"""

import hashlib
import io
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image, ImageOps

from paper_trail.adapters.filesystem import image_store
from paper_trail.adapters.filesystem.image_store import FilesystemImageStore
from paper_trail.domain.errors import UnsupportedMediaError
from tests.images import (
    GPS_IFD,
    animated_bytes,
    decode,
    heic_bytes,
    iphone_mpo_with_exif,
    landscape_jpeg_with_exif,
    png_bytes,
    rgba_png_bytes,
    webp_bytes,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "images"


@pytest.fixture
def store(root: Path) -> FilesystemImageStore:
    return FilesystemImageStore(root, max_pixels=40_000_000)


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_s2_ac_03_stored_as_jpeg_named_by_its_hash(store: FilesystemImageStore, root: Path) -> None:
    stored = store.save(png_bytes())

    (path,) = _files(root)
    data = path.read_bytes()
    assert path == root / stored.relative_path
    assert hashlib.sha256(data).hexdigest() == stored.sha256
    assert path.name == f"{stored.sha256}.jpg"
    assert data.startswith(b"\xff\xd8\xff")
    assert (stored.width, stored.height) == (64, 32)


def test_s2_ac_03_exif_is_stripped_and_orientation_applied(
    store: FilesystemImageStore,
) -> None:
    """S2-AC-03: no EXIF (GPS absent); the EXIF-rotated fixture is stored upright."""
    original = decode(landscape_jpeg_with_exif())
    assert original.getexif().get_ifd(GPS_IFD)  # the fixture really carries a location

    stored = store.save(landscape_jpeg_with_exif())

    image = decode(store.open(stored.relative_path))
    assert dict(image.getexif()) == {}
    assert "exif" not in image.info
    assert image.size == (20, 60)  # rotated 90 degrees: portrait now
    assert (stored.width, stored.height) == (20, 60)
    top, bottom = image.getpixel((10, 5)), image.getpixel((10, 55))
    assert isinstance(top, tuple)
    assert isinstance(bottom, tuple)
    assert sum(top) < 100 < sum(bottom)  # the black band is on top


def test_s2_ac_03_identical_bytes_store_one_file(store: FilesystemImageStore, root: Path) -> None:
    first = store.save(landscape_jpeg_with_exif())
    second = store.save(landscape_jpeg_with_exif())

    assert first == second
    assert len(_files(root)) == 1


@pytest.mark.parametrize(
    "make",
    [png_bytes, rgba_png_bytes, webp_bytes, heic_bytes],
    ids=["png", "png-rgba", "webp", "heic"],
)
def test_s2_ac_04_png_webp_and_heic_are_stored_as_jpeg(
    store: FilesystemImageStore, make: Callable[[], bytes]
) -> None:
    stored = store.save(make())

    image = decode(store.open(stored.relative_path))
    assert image.format == "JPEG"
    assert image.mode == "RGB"
    assert stored.content_type == "image/jpeg"


def test_s2_ac_02_too_many_pixels_is_refused(root: Path) -> None:
    """S2-AC-02 (pulled forward): a decompression-bomb guard with a clear message."""
    small_limit = FilesystemImageStore(root, max_pixels=1000)

    with pytest.raises(UnsupportedMediaError, match="pixels"):
        small_limit.save(png_bytes(100, 100))

    assert _files(root) == []


def test_a_truncated_image_is_refused(store: FilesystemImageStore) -> None:
    with pytest.raises(UnsupportedMediaError):
        store.save(png_bytes()[:40])


# --- iPhone photos are MPO: a JPEG primary image plus extra frames (depth, HDR gain map) ----------


def test_an_iphone_mpo_is_stored_as_its_primary_jpeg_frame(store: FilesystemImageStore) -> None:
    """Pillow reports iPhone JPEGs as "MPO"; only frame 0 is kept, upright, without EXIF."""
    original = decode(iphone_mpo_with_exif())
    assert (original.format, getattr(original, "n_frames", 1)) == ("MPO", 2)

    stored = store.save(iphone_mpo_with_exif())

    image = decode(store.open(stored.relative_path))
    assert image.format == "JPEG"
    assert getattr(image, "n_frames", 1) == 1
    assert image.mode == "RGB"
    assert (stored.width, stored.height) == (image.width, image.height) == (20, 60)
    assert not image.getexif()
    assert "exif" not in image.info
    top, bottom = image.getpixel((10, 5)), image.getpixel((10, 55))
    assert isinstance(top, tuple) and isinstance(bottom, tuple)
    assert max(top) < 60  # the primary frame's black band, on top once upright
    assert min(bottom) > 200  # ... and its white, never the secondary frame's blue


def test_an_animated_webp_is_still_stored_as_its_first_frame(
    store: FilesystemImageStore,
) -> None:
    stored = store.save(animated_bytes("WEBP"))

    image = decode(store.open(stored.relative_path))
    assert image.format == "JPEG"
    assert (stored.width, stored.height) == (48, 24)
    pixel = image.getpixel((24, 12))
    assert isinstance(pixel, tuple)
    assert pixel[1] > pixel[2]  # the first frame's green, not the second frame's blue


def test_an_animated_gif_is_still_refused(store: FilesystemImageStore) -> None:
    with pytest.raises(UnsupportedMediaError, match="GIF"):
        store.save(animated_bytes("GIF"))


def _stored_before(data: bytes) -> bytes:
    """What the store wrote before decoding was made lean: a turned copy, then an RGB copy."""
    with Image.open(io.BytesIO(data)) as image:
        image.seek(0)
        rgb = ImageOps.exif_transpose(image).convert("RGB")
    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=90, optimize=True)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "make",
    [
        png_bytes,
        rgba_png_bytes,
        landscape_jpeg_with_exif,
        iphone_mpo_with_exif,
        webp_bytes,
        heic_bytes,
        lambda: _encoded(Image.new("P", (40, 30), 3), "PNG"),
        lambda: _encoded(Image.new("LA", (40, 30), (90, 128)), "PNG"),
    ],
)
def test_a_photo_stored_before_is_stored_as_the_same_bytes_again(
    store: FilesystemImageStore, root: Path, make: Callable[[], bytes]
) -> None:
    """The stored JPEG's hash finds a photo uploaded again: its bytes must not change."""
    data = make()

    stored = store.save(data)

    assert (root / stored.relative_path).read_bytes() == _stored_before(data)


def _encoded(image: Image.Image, image_format: str) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def test_two_uploads_at_once_are_decoded_one_after_the_other(
    store: FilesystemImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoding = 0
    most = 0
    lock = threading.Lock()
    normalized = image_store._normalized_jpeg

    def counted(data: bytes, max_pixels: int) -> tuple[bytes, int, int]:
        nonlocal decoding, most
        with lock:
            decoding += 1
            most = max(most, decoding)
        threading.Event().wait(0.05)
        try:
            return normalized(data, max_pixels)
        finally:
            with lock:
                decoding -= 1

    monkeypatch.setattr(image_store, "_normalized_jpeg", counted)
    start = threading.Barrier(4)

    def upload(index: int) -> None:
        start.wait()
        store.save(_encoded(Image.new("RGB", (32 + index, 16), "white"), "PNG"))

    threads = [threading.Thread(target=upload, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert (most, decoding) == (1, 0)


MEASURE_PEAK = """
import sys
from pathlib import Path
from paper_trail.adapters.filesystem.image_store import FilesystemImageStore

def kib(field):
    with open("/proc/self/status") as status:
        return next(int(line.split()[1]) for line in status if line.startswith(field))

data = Path(sys.argv[1]).read_bytes()
before = kib("VmRSS:")
FilesystemImageStore(Path(sys.argv[2]), max_pixels=40_000_000).save(data)
print((kib("VmHWM:") - before) * 1024)
"""


def test_a_40_megapixel_rgba_png_is_stored_within_the_web_containers_memory(
    root: Path, tmp_path: Path
) -> None:
    """Measured in a process of its own (its peak resident memory, ``VmHWM``: unlike
    ``ru_maxrss`` it does not include the forking test process): decoding, converting and
    re-encoding a photo at the pixel limit, as RGBA (the costliest), grows it by about 300 MB
    (it was about 580 MB), inside the web container's 512 MB beside its ~125 MB at rest."""
    gradient = Image.linear_gradient("L").resize((7300, 5479))
    photo = tmp_path / "photo.png"
    Image.merge("RGBA", [gradient, gradient, gradient, gradient.rotate(90)]).save(
        photo, format="PNG", compress_level=1
    )
    del gradient

    result = subprocess.run(  # noqa: S603 - this interpreter, a script of this module
        [sys.executable, "-c", MEASURE_PEAK, str(photo), str(root)],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    assert int(result.stdout) < 340 * 2**20
