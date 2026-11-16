"""``POST /api/receipts`` through the fully wired app: the photo is stored by the real image store,
its receipt created PROCESSING and one ``process_receipt`` job queued (the worker's side is in
``tests/integration/test_upload_jobs_sqlite.py``).

Pinned: 202 ``{id, status, stage}`` for a new photo; 200 with the same receipt and no new job
for the same photo again; a 413 problem for a file above ``UPLOAD__MAX_BYTES`` (refused from its
Content-Length before reading, or as the body streams in); a 415 problem, with nothing stored,
for a file that is not an image or has more pixels than ``UPLOAD__MAX_PIXELS``; a 422 problem
without the ``file`` field.
"""

from collections.abc import Callable, Iterable, Iterator

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from paper_trail.bootstrap import Container, build_container, dispose
from paper_trail.domain.enums import JobKind, JobStatus
from paper_trail.interfaces.web.app import MULTIPART_OVERHEAD_BYTES, create_app
from tests.images import png_bytes
from tests.storage import stored_state
from tests.support import CopyMigrated, MakeSettings

PROBLEM_JSON = "application/problem+json"
URL = "/api/receipts"
MAX_BYTES = 1000
BOUNDARY = "limit-test"


def _upload(client: TestClient, data: bytes, name: str = "receipt.png") -> tuple[int, object]:
    response = client.post(URL, files={"file": (name, data, "image/png")})
    return response.status_code, response.json()


def _queued(container: Container) -> list[tuple[JobKind, int, JobStatus]]:
    return [
        (job.kind, job.receipt_id, job.status)
        for job in stored_state(container.engine).jobs.values()
    ]


def test_a_photo_is_stored_and_queued_once(
    migrated_client: TestClient, migrated_container: Container
) -> None:
    created = _upload(migrated_client, png_bytes())
    again = _upload(migrated_client, png_bytes(), "renamed.jpg")
    detail = migrated_client.get(f"{URL}/1").json()

    accepted = {"id": 1, "status": "processing", "stage": "reading"}
    assert created == (202, accepted)
    assert again == (200, accepted)
    assert (detail["status"], detail["stage"], detail["has_image"]) == (
        "processing",
        "reading",
        True,
    )
    assert _queued(migrated_container) == [(JobKind.PROCESS_RECEIPT, 1, JobStatus.PENDING)]
    images = migrated_container.settings.data_dir / "images"
    assert len([path for path in images.rglob("*.jpg") if path.is_file()]) == 1


@pytest.fixture
def small_limit(make_settings: MakeSettings, copy_migrated: CopyMigrated) -> Iterator[Container]:
    settings = make_settings(upload={"max_bytes": MAX_BYTES, "max_pixels": 100})
    copy_migrated(settings.database_path)
    container = build_container(settings)
    try:
        yield container
    finally:
        dispose(container)


@pytest.mark.parametrize(
    ("data", "detail"),
    [
        (b"%PDF-1.7 not a photo", "not a readable JPEG, PNG, WebP or HEIC image"),
        (png_bytes(20, 10), "more than the limit of 100 pixels"),
    ],
    ids=["not-an-image", "too-many-pixels"],
)
def test_a_file_that_is_not_an_acceptable_photo_is_a_415_problem(
    small_limit: Container, data: bytes, detail: str
) -> None:
    with TestClient(create_app(small_limit), raise_server_exceptions=False) as client:
        response = client.post(URL, files={"file": ("receipt.png", data, "image/png")})

    assert response.status_code == 415
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert (body["type"], body["status"], body["instance"]) == (
        "/problems/unsupported-media",
        415,
        URL,
    )
    assert detail in body["detail"]
    state = stored_state(small_limit.engine)
    assert (state.receipts, state.jobs) == ({}, {})


def test_an_upload_without_the_file_field_is_a_422_problem(
    migrated_client: TestClient, migrated_container: Container
) -> None:
    response = migrated_client.post(URL, data={"photo": "x"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["errors"][0]["loc"] == ["body", "file"]
    assert _queued(migrated_container) == []


def _streamed(size: int) -> Iterable[bytes]:
    """A multipart body of ``size`` bytes sent without a Content-Length (chunked)."""
    head = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="r.png"\r\n\r\n'
    yield head.encode()
    sent = len(head)
    while sent < size:
        yield b"x" * 4096
        sent += 4096


def _post_file(size: int) -> Callable[[TestClient], Response]:
    return lambda client: client.post(
        URL, files={"file": ("receipt.png", b"x" * size, "image/png")}
    )


def _post_streamed(client: TestClient) -> Response:
    return client.post(
        URL,
        content=_streamed(MAX_BYTES + MULTIPART_OVERHEAD_BYTES + 4096),
        headers={"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
    )


@pytest.mark.parametrize(
    "post",
    [_post_file(MAX_BYTES + 1), _post_file(MAX_BYTES + MULTIPART_OVERHEAD_BYTES), _post_streamed],
    ids=["file-over-the-limit", "content-length-over-the-limit", "streamed-over-the-limit"],
)
def test_an_upload_above_the_byte_limit_is_a_413_problem(
    small_limit: Container, post: Callable[[TestClient], Response]
) -> None:
    with TestClient(create_app(small_limit), raise_server_exceptions=False) as client:
        response = post(client)

    assert response.status_code == 413, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert (body["type"], body["status"]) == ("/problems/too-large", 413)
    assert body["detail"].startswith("The upload is larger than the limit of")
    state = stored_state(small_limit.engine)
    assert (state.receipts, state.jobs) == ({}, {})
