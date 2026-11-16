"""The ``ReceiptReader`` port on Claude (claude-extraction plan EC, D1, R3, V11; CE-TC-017..020).

``AnthropicReader`` runs over the real SDK client with a canned in-memory transport
(``tests/anthropic_api.py``): no socket is opened and no key is real. The SDK's retry pauses are
patched out, so no test sleeps.

- CE-TC-017, C5: ``prepare_image`` on synthetic Pillow images: always re-encoded, upright,
  long edge at most 2576 px, JPEG q90, under 5 MB once base64-encoded.
- CE-TC-018: one read is one request of the agreed shape (model, image then text, the answer
  schema as the output format, ``CLAUDE__TIMEOUT_S``, no SDK retries: C11).
- CE-TC-019: every SDK outcome maps to one port outcome; no SDK exception escapes.
- CE-TC-020: ``ScriptedReader`` (the graph suites' fake) and the adapter honour one contract.
"""

import base64
import contextlib
import hashlib
import io
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import httpx2
import pytest
from PIL import Image
from pydantic import SecretStr

from paper_trail.adapters.anthropic.reader import (
    JPEG_QUALITY,
    KEY_VARIABLE,
    AnthropicReader,
    prepare_image,
)
from paper_trail.agents.schemas import ReceiptAnswer
from paper_trail.agents.tracing import TracingReader
from paper_trail.config import ClaudeSettings
from paper_trail.ports.reader import (
    ReaderError,
    ReaderOutputError,
    ReaderRefusedError,
    ReaderTruncatedError,
    ReaderUnavailableError,
    ReadRequest,
    ReadUsage,
    ReceiptReader,
)
from tests.anthropic_api import (
    API_KEY,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    CannedApi,
    Reply,
    error_response,
    message,
)
from tests.extractions import answer
from tests.factories import MASKED_TEST_PAN, TEST_PAN
from tests.fakes.clock import FakeClock
from tests.fakes.reader import ScriptedReader
from tests.fakes.trace import FakeTracer

TIMEOUT_S = 42.0  # not the default (60): proves the setting reaches the request
MODEL = "claude-haiku-5-5"
FEEDBACK = "items add up to 25.46 but the subtotal is 25.55"
EXIF_ORIENTATION = 0x0112
BILLED = ReadUsage(input_tokens=INPUT_TOKENS, output_tokens=OUTPUT_TOKENS)


@pytest.fixture(autouse=True)
def _no_retry_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("anthropic._base_client.time.sleep", lambda _seconds: None)


def _jpeg(image: Image.Image, **save: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", **save)
    return buffer.getvalue()


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


PHOTO = _jpeg(Image.new("RGB", (60, 40), "white"))


def _request(text: str = "Read this receipt.") -> ReadRequest:
    return ReadRequest(
        model=MODEL,
        tag="extraction/claude-v1",
        system="You transcribe receipts.",
        text=text,
        image=PHOTO,
        max_tokens=16000,
    )


def _reader(api: CannedApi) -> AnthropicReader:
    return AnthropicReader(ClaudeSettings(timeout_s=TIMEOUT_S), client=api.client())


# --- CE-TC-017: image preparation -----------------------------------------------------------


def _opened(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def test_c5_even_an_upright_jpeg_within_the_api_limits_is_re_encoded() -> None:
    """Ruling C5 (supersedes R14's "sent unchanged"): the reader always re-encodes."""
    stored = _jpeg(Image.new("RGB", (1200, 900), "white"), quality=75)

    prepared = prepare_image(stored)

    assert (prepared != stored, _opened(prepared).size) == (True, (1200, 900))


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((4000, 3000), (2576, 1932)),
        ((3000, 4000), (1932, 2576)),
        ((8001, 100), (2576, 32)),
        ((1200, 900), (1200, 900)),
    ],
    ids=["landscape shrunk", "portrait shrunk", "above the api edge", "small kept"],
)
def test_c5_the_long_edge_is_at_most_2576_px(
    size: tuple[int, int], expected: tuple[int, int]
) -> None:
    prepared = _opened(prepare_image(_jpeg(Image.new("RGB", size, "white"))))

    assert (prepared.format, prepared.mode, prepared.size) == ("JPEG", "RGB", expected)


def test_c5_a_large_noisy_photo_fits_the_api_limit_once_base64_encoded() -> None:
    """Noise barely compresses: at quality 90 this photo would be over 5 MB in base64."""
    noise = Image.frombytes("RGB", (2576, 1932), os.urandom(2576 * 1932 * 3))

    prepared = prepare_image(_png(noise))

    assert len(base64.b64encode(prepared)) < 5 * 1024 * 1024
    assert _opened(prepared).size == (2576, 1932)


def test_c13_a_photo_still_too_large_at_quality_50_is_shrunk_until_it_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling C13: lowering the quality stops at 50; then the long edge shrinks in 20 % steps.
    The limit is lowered so a small noise image stands for a huge photo."""
    limit = 150_000
    monkeypatch.setattr("paper_trail.adapters.anthropic.reader.MAX_BASE64_BYTES", limit)
    noise = Image.frombytes("RGB", (800, 600), os.urandom(800 * 600 * 3))
    at_quality_50 = _jpeg(noise, quality=50)
    assert len(base64.b64encode(at_quality_50)) >= limit

    prepared = prepare_image(_png(noise))

    width, height = _opened(prepared).size
    assert len(base64.b64encode(prepared)) < limit
    assert (width < 800, round(width / height, 2)) == (True, round(800 / 600, 2))


def test_ce_tc_017_exif_orientation_is_applied_and_dropped() -> None:
    """Orientation 6 (stored turned 90 degrees counter-clockwise): the top-left red block of the
    stored pixels ends up top-right, and no orientation tag is left."""
    stored = Image.new("RGB", (300, 200), "white")
    stored.paste((255, 0, 0), (0, 0, 60, 60))
    exif = Image.Exif()
    exif[EXIF_ORIENTATION] = 6

    prepared = _opened(prepare_image(_jpeg(stored, exif=exif.tobytes())))

    assert prepared.size == (200, 300)
    assert prepared.getexif().get(EXIF_ORIENTATION) is None
    red, green, blue = prepared.getpixel((200 - 30, 30))  # type: ignore[misc]
    assert (red > 200, green < 60, blue < 60) == (True, True, True)


def test_ce_tc_017_a_png_with_alpha_becomes_an_rgb_jpeg() -> None:
    prepared = _opened(prepare_image(_png(Image.new("RGBA", (50, 30), (0, 128, 0, 100)))))

    assert (prepared.format, prepared.mode, prepared.size) == ("JPEG", "RGB", (50, 30))


def test_c5_the_jpeg_is_saved_at_quality_90() -> None:
    """JPEG quality is read back as the quantization tables Pillow writes for it."""
    image = Image.new("RGB", (120, 80), "white")
    reference = _opened(_jpeg(image, quality=JPEG_QUALITY))

    prepared = _opened(prepare_image(_png(image)))

    assert JPEG_QUALITY == 90
    assert prepared.quantization == reference.quantization  # type: ignore[attr-defined]


# --- CE-TC-018: the request (the contract tests hand the adapter a client; the timeout and
# retry tests let it build its own, as production does) --------------------------------


def test_ce_tc_018_one_read_is_one_request_of_the_agreed_shape() -> None:
    api = CannedApi(message(answer()))

    _reader(api).read(_request(), ReceiptAnswer)

    body = api.body()
    (turn,) = body["messages"]
    image_block, text_block = turn["content"]
    output_format = body["output_config"]["format"]
    assert len(api.requests) == 1
    assert api.requests[0].url.path == "/v1/messages"
    assert (body["model"], body["max_tokens"], body["system"]) == (
        MODEL,
        16000,
        "You transcribe receipts.",
    )
    assert turn["role"] == "user"
    assert image_block["type"] == "image"
    assert {key: image_block["source"][key] for key in ("type", "media_type")} == {
        "type": "base64",
        "media_type": "image/jpeg",
    }
    assert base64.b64decode(image_block["source"]["data"]) == prepare_image(PHOTO)
    assert text_block == {"type": "text", "text": "Read this receipt."}
    assert output_format["type"] == "json_schema"
    assert set(output_format["schema"]["properties"]) == set(ReceiptAnswer.model_fields)
    assert "temperature" not in body


def _built_reader(api: CannedApi, monkeypatch: pytest.MonkeyPatch) -> AnthropicReader:
    """The adapter making its own client from the settings, as in production; only the
    transport under that client is the canned one."""
    monkeypatch.setattr("paper_trail.adapters.anthropic.reader.Anthropic", api.client)
    return AnthropicReader(ClaudeSettings(api_key=SecretStr(API_KEY), timeout_s=TIMEOUT_S))


def test_ce_tc_018_the_timeout_is_claude_timeout_s(monkeypatch: pytest.MonkeyPatch) -> None:
    api = CannedApi(message(answer()))

    _built_reader(api, monkeypatch).read(_request(), ReceiptAnswer)

    timeout = api.requests[0].extensions["timeout"]
    assert {key: timeout[key] for key in ("connect", "read", "write")} == {
        "connect": TIMEOUT_S,
        "read": TIMEOUT_S,
        "write": TIMEOUT_S,
    }


def test_c11_one_read_is_one_http_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ruling C11 (supersedes the SDK's 2 retries): the client is built with ``max_retries=0``
    so each attempt is billed or not on its own; ``BudgetedReader`` retries."""
    api = CannedApi(then=error_response(500, "api_error", "Internal server error"))

    with pytest.raises(ReaderUnavailableError):
        _built_reader(api, monkeypatch).read(_request(), ReceiptAnswer)

    assert len(api.requests) == 1


def test_ce_tc_018_a_retry_read_carries_the_feedback_text() -> None:
    api = CannedApi(message(answer()), message(answer()))
    reader = _reader(api)

    reader.read(_request(), ReceiptAnswer)
    reader.read(_request(f"Read this receipt.\n{FEEDBACK}"), ReceiptAnswer)

    texts = [api.body(index)["messages"][0]["content"][1]["text"] for index in (0, 1)]
    assert [FEEDBACK in text for text in texts] == [False, True]


# --- CE-TC-019: every outcome ---------------------------------------------------------------


def test_ce_tc_019_a_parsed_answer_comes_back_with_its_usage_and_stop_reason() -> None:
    api = CannedApi(message(answer()))

    result = _reader(api).read(_request(), ReceiptAnswer)

    assert (result.value, result.usage, result.stop_reason) == (
        ReceiptAnswer.model_validate(answer()),
        BILLED,
        "end_turn",
    )


FAILURES: list[tuple[str, Callable[[], Reply], type[ReaderError], ReadUsage | None]] = [
    (
        "refusal",
        lambda: message(text="I can't help with that.", stop_reason="refusal"),
        ReaderRefusedError,
        BILLED,
    ),
    (
        "max_tokens",
        lambda: message(text='{"store_name": "COSTCO", "items": [{', stop_reason="max_tokens"),
        ReaderTruncatedError,
        BILLED,
    ),
    ("500", lambda: error_response(500, "api_error", "Internal"), ReaderUnavailableError, None),
    ("529", lambda: error_response(529, "overloaded_error", "Busy"), ReaderUnavailableError, None),
    ("connection", lambda: httpx2.ConnectError("connection refused"), ReaderUnavailableError, None),
    ("timeout", lambda: httpx2.ReadTimeout("timed out"), ReaderUnavailableError, None),
    (
        "schema",
        lambda: message({"store_name": "COSTCO", "total": 26.83}),
        ReaderOutputError,
        BILLED,
    ),
]  # fmt: skip


@pytest.mark.parametrize(
    ("reply", "error", "usage"),
    [failure[1:] for failure in FAILURES],
    ids=[failure[0] for failure in FAILURES],
)
def test_ce_tc_019_every_failure_is_one_port_error(
    reply: Callable[[], Reply], error: type[ReaderError], usage: ReadUsage | None
) -> None:
    """Each failure is exactly its port error, never a raw SDK one; a billed answer (refusal,
    truncation, schema failure) carries its usage for the budget."""
    api = CannedApi(then=reply())

    with pytest.raises(ReaderError) as raised:
        _reader(api).read(_request(), ReceiptAnswer)

    assert (type(raised.value), raised.value.usage) == (error, usage)


def test_ce_tc_019_a_schema_failure_carries_the_validation_text() -> None:
    api = CannedApi(message({"store_name": "COSTCO", "total": 26.83}))

    with pytest.raises(ReaderOutputError) as raised:
        _reader(api).read(_request(), ReceiptAnswer)

    assert "items" in str(raised.value)
    assert "total" in str(raised.value)


def test_ce_tc_019_a_rejected_key_names_the_variable_never_the_key() -> None:
    api = CannedApi(error_response(401, "authentication_error", "invalid x-api-key"))

    with pytest.raises(ReaderUnavailableError) as raised:
        _reader(api).read(_request(), ReceiptAnswer)

    assert KEY_VARIABLE in str(raised.value)
    assert API_KEY not in str(raised.value)
    assert API_KEY not in repr(raised.value.args)
    assert len(api.requests) == 1


def test_ce_tc_019_no_key_fails_the_read_not_start_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    reader = AnthropicReader(ClaudeSettings())

    with pytest.raises(ReaderUnavailableError, match=KEY_VARIABLE):
        reader.read(_request(), ReceiptAnswer)


# --- CE-TC-020: one contract for the fake and the adapter ----------------------------------

OUTCOMES: list[tuple[str, Any, Reply]] = [
    ("answer", answer(), message(answer())),
    (
        "refused",
        ReaderRefusedError("the model declined"),
        message(text="I can't help with that.", stop_reason="refusal"),
    ),
    (
        "truncated",
        ReaderTruncatedError("cut off"),
        message(text='{"store_name": "COS', stop_reason="max_tokens"),
    ),
    (
        "unavailable",
        ReaderUnavailableError("down"),
        error_response(503, "api_error", "Service unavailable"),
    ),
    ("output", {"store_name": "COSTCO"}, message({"store_name": "COSTCO"})),
]


@pytest.fixture(params=["scripted", "anthropic"])
def make_reader(request: pytest.FixtureRequest) -> Iterator[Callable[[str], ReceiptReader]]:
    def make(outcome: str) -> ReceiptReader:
        scripted, canned = next((s, c) for name, s, c in OUTCOMES if name == outcome)
        if request.param == "scripted":
            return ScriptedReader(then=scripted)
        return _reader(CannedApi(then=canned))

    yield make


def test_ce_tc_020_the_same_receipt_comes_back(make_reader: Callable[[str], ReceiptReader]) -> None:
    result = make_reader("answer").read(_request(), ReceiptAnswer)

    assert result.value == ReceiptAnswer.model_validate(answer())


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ("refused", ReaderRefusedError),
        ("truncated", ReaderTruncatedError),
        ("unavailable", ReaderUnavailableError),
        ("output", ReaderOutputError),
    ],
)
def test_ce_tc_020_the_same_error_for_each_outcome(
    make_reader: Callable[[str], ReceiptReader], outcome: str, error: type[ReaderError]
) -> None:
    with pytest.raises(ReaderError) as raised:
        make_reader(outcome).read(_request(), ReceiptAnswer)

    assert type(raised.value) is error


# --- R18: the trace hashes the bytes actually sent ----------------------------------------------


def _sent_sha256(api: CannedApi) -> str:
    (turn,) = api.body()["messages"]
    image_block = turn["content"][0]
    return hashlib.sha256(base64.b64decode(image_block["source"]["data"])).hexdigest()


@pytest.mark.parametrize(
    "reply",
    [lambda: message(answer()), lambda: message(text="No.", stop_reason="refusal")],
    ids=["answered", "refused"],
)
def test_r18_a_resized_image_is_traced_by_the_hash_of_the_bytes_sent(
    reply: Callable[[], httpx2.Response],
) -> None:
    stored = _jpeg(Image.new("RGB", (8001, 100), "white"))
    api = CannedApi(reply())
    tracer = FakeTracer(FakeClock())
    reader = TracingReader(_reader(api), tracer, receipt_id=1)

    with tracer.bind(1), contextlib.suppress(ReaderRefusedError):
        reader.read(replace(_request(), image=stored), ReceiptAnswer)

    (event,) = tracer.events(1)
    assert event.data["image_sha256"] == _sent_sha256(api)
    assert _sent_sha256(api) != hashlib.sha256(stored).hexdigest()


def test_c5_an_unavailable_api_error_carries_its_type_and_masked_message() -> None:
    api = CannedApi(
        then=error_response(500, "api_error", f"internal error near {TEST_PAN} in the request")
    )

    with pytest.raises(ReaderUnavailableError) as raised:
        _reader(api).read(_request(), ReceiptAnswer)

    message = str(raised.value)
    assert ("api_error" in message, MASKED_TEST_PAN in message, TEST_PAN in message) == (
        True,
        True,
        False,
    )


def test_c15_an_image_that_cannot_fit_stops_with_an_output_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling C15: at 1 px on its long edge the shrinking stops; the read fails as an output
    error (the next attempt or a person takes over) instead of looping."""
    monkeypatch.setattr("paper_trail.adapters.anthropic.reader.MAX_BASE64_BYTES", 1)
    image = _png(Image.new("RGB", (64, 48), "white"))
    raised: list[BaseException] = []

    def prepare() -> None:
        try:
            prepare_image(image)
        except BaseException as error:
            raised.append(error)

    worker = threading.Thread(target=prepare, daemon=True)
    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive(), "prepare_image is still shrinking after 30 s"
    assert [type(error) for error in raised] == [ReaderOutputError]
