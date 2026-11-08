"""ImageStore contract, for the in-memory fake and the filesystem adapter (S2-AC-03, AC-A-02).

What the use cases rely on: saving is idempotent and content-addressed, ``open`` returns bytes
whose SHA-256 is ``StoredImage.sha256``, non-images are refused by content, and nothing outside
the store can be opened.
"""

import hashlib
from pathlib import Path

import pytest

from paper_trail.adapters.filesystem.image_store import FilesystemImageStore
from paper_trail.domain.errors import NotFoundError, UnsupportedMediaError
from paper_trail.ports.images import ImageStore
from tests.fakes.images import FakeImageStore
from tests.images import png_bytes

MAX_PIXELS = 40_000_000


@pytest.fixture(params=["fake", "filesystem"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ImageStore:
    if request.param == "fake":
        return FakeImageStore()
    return FilesystemImageStore(tmp_path / "images", max_pixels=MAX_PIXELS)


RED = png_bytes(color=(200, 30, 30))
BLUE = png_bytes(color=(30, 30, 200))


def test_saved_images_are_content_addressed(store: ImageStore) -> None:
    stored = store.save(RED)

    sha = stored.sha256
    assert len(sha) == 64
    assert stored.relative_path == f"{sha[:2]}/{sha[2:4]}/{sha}.jpg"
    assert stored.content_type == "image/jpeg"
    assert hashlib.sha256(store.open(stored.relative_path)).hexdigest() == sha


def test_saving_the_same_bytes_twice_is_idempotent(store: ImageStore) -> None:
    """S2-AC-03: identical bytes give the same StoredImage (one file)."""
    assert store.save(RED) == store.save(RED)


def test_different_images_get_different_paths(store: ImageStore) -> None:
    assert store.save(RED).relative_path != store.save(BLUE).relative_path


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.7\n1 0 obj", b"just some text, renamed to receipt.jpg", b""],
    ids=["pdf", "text", "empty"],
)
def test_non_images_are_refused_by_content(store: ImageStore, data: bytes) -> None:
    """S2-AC-02 (pulled forward): judged by magic bytes, not by name or content type."""
    with pytest.raises(UnsupportedMediaError):
        store.save(data)


@pytest.mark.parametrize(
    "path",
    ["ab/cd/" + "0" * 64 + ".jpg", "../../etc/passwd", "/etc/passwd", ""],
    ids=["unknown", "traversal", "absolute", "empty"],
)
def test_only_stored_images_can_be_opened(store: ImageStore, path: str) -> None:
    store.save(RED)

    with pytest.raises(NotFoundError):
        store.open(path)


def test_open_outside_the_root_is_refused_even_if_the_file_exists(
    store: ImageStore, tmp_path: Path
) -> None:
    """The filesystem store's root is tmp_path/images, so ../secret.txt exists on disk."""
    (tmp_path / "secret.txt").write_text("not an image", encoding="utf-8")

    with pytest.raises(NotFoundError):
        store.open("../secret.txt")


def test_upright_at_zero_degrees_is_the_stored_image(store: ImageStore) -> None:
    """The vision attempt opens the photo as the OCR read it; unturned, it is ``open``'s."""
    stored = store.save(RED)

    assert store.open_upright(stored.relative_path, 0) == store.open(stored.relative_path)


def test_upright_of_a_missing_image_is_not_found(store: ImageStore) -> None:
    with pytest.raises(NotFoundError):
        store.open_upright("ab/cd/missing.jpg", 90)
