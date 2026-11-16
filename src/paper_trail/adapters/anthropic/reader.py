"""``ReceiptReader`` on the Claude API (claude-extraction plan EC, E1, V11).

One ``client.messages.create`` call per read: the photo, when the request has one, as a base64
image block (``prepare_image``: upright, long edge at most 2576 px, a JPEG whose base64 fits
the API's 5 MB; the trace's hash is of those bytes), then
the request's text, with the answer model's JSON schema as the structured output format
(``output_config.format``, the schema made by the SDK's ``transform_schema``, as
``messages.parse`` does). ``messages.parse`` itself is not used: it validates the answer inside
the call, so a refused or cut-off answer would raise a validation error before its stop reason
could be read. Here the stop reason is checked first: ``refusal`` is a ``ReaderRefusedError``,
``max_tokens`` a ``ReaderTruncatedError``; then the answer is validated, and one that does not
match is a ``ReaderOutputError``. Connection errors, timeouts, error statuses and a missing or
rejected key are ``ReaderUnavailableError`` (an error status with the API's error type and
message, masked); a timeout or a connection broken after it was made may have been billed
(``maybe_billed``), a refused connection or an error status was not. No SDK exception escapes.
The SDK does not retry (``max_retries=0``): one read is one HTTP request, and a retryable
failure says so (``retryable``) for the budget wrapper to retry; the timeout is
``CLAUDE__TIMEOUT_S``; no temperature is sent.
"""

import base64
import hashlib
import io
from typing import Final

import httpx2
from anthropic import (
    Anthropic,
    AnthropicError,
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    PermissionDeniedError,
    transform_schema,
)
from anthropic.types import ImageBlockParam, Message, MessageParam, TextBlockParam
from PIL import Image, ImageOps
from pydantic import TypeAdapter, ValidationError

from paper_trail.config import ClaudeSettings
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.ports.reader import (
    ReaderError,
    ReaderOutputError,
    ReaderRefusedError,
    ReaderTruncatedError,
    ReaderUnavailableError,
    ReadRequest,
    ReadResult,
    ReadUsage,
)

# The API's own resizing limit: a larger image is scaled down to this long edge anyway.
MAX_EDGE_PX = 2576
JPEG_QUALITY = 90
MIN_JPEG_QUALITY = 50
QUALITY_STEP = 10
SHRINK_STEP = 0.8  # the long edge kept per step once the quality is at its floor
# The Messages API refuses an image whose base64 text is 5 MB or more.
MAX_BASE64_BYTES = 5 * 1024 * 1024
# The SDK makes one attempt per call: whether an attempt was billed is then known exactly, and
# the budget wrapper retries and charges each attempt itself (review C11).
SDK_MAX_RETRIES = 0
RETRYABLE_STATUSES = frozenset({429})  # with every 5xx (529: overloaded)
_SERVER = 500
KEY_VARIABLE = "ANTHROPIC_API_KEY"
MEDIA_TYPE: Final = "image/jpeg"
_NO_KEY = f"no Claude API key: set {KEY_VARIABLE} in .env"


def prepare_image(image: bytes) -> bytes:
    """``image`` as the API takes it: turned upright by its EXIF orientation (no orientation
    tag left), shrunk so its long edge is at most ``MAX_EDGE_PX`` (never enlarged, aspect kept),
    as an RGB JPEG at ``JPEG_QUALITY``. While its base64 text is not under ``MAX_BASE64_BYTES``
    the quality is lowered in steps down to ``MIN_JPEG_QUALITY``, then the long edge shrunk by
    ``SHRINK_STEP`` at a time, so an over-limit image is never returned: ReaderOutputError
    when even 1 px on its long edge does not fit (nothing is sent)."""
    with Image.open(io.BytesIO(image)) as opened:
        upright = ImageOps.exif_transpose(opened).convert("RGB")
    upright.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.Resampling.LANCZOS)
    quality = JPEG_QUALITY
    while True:
        encoded = _jpeg(upright, quality)
        if _base64_size(encoded) < MAX_BASE64_BYTES:
            return encoded
        if quality > MIN_JPEG_QUALITY:
            quality = max(quality - QUALITY_STEP, MIN_JPEG_QUALITY)
            continue
        if max(upright.size) <= 1:
            msg = "the photo cannot be made small enough for the Claude API"
            raise ReaderOutputError(msg)
        edge = max(1, int(max(upright.size) * SHRINK_STEP))
        upright.thumbnail((edge, edge), Image.Resampling.LANCZOS)


def _jpeg(image: Image.Image, quality: int) -> bytes:
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def _base64_size(data: bytes) -> int:
    return 4 * ((len(data) + 2) // 3)


class AnthropicReader:
    """Implements ``paper_trail.ports.reader.ReceiptReader``. Creating one opens nothing; the
    client is made on the first read (unless one is given, as the contract tests do), so a
    missing key fails a read, not start-up."""

    def __init__(self, settings: ClaudeSettings, client: Anthropic | None = None) -> None:
        self._settings = settings
        self._client = client

    def read[M](self, request: ReadRequest, schema: type[M]) -> ReadResult[M]:
        adapter = TypeAdapter(schema)
        self._sdk()  # no key: nothing is prepared or sent
        image = None if request.image is None else prepare_image(request.image)
        sent = None if image is None else hashlib.sha256(image).hexdigest()
        try:
            response = self._create(request, image, adapter)
            usage = ReadUsage(response.usage.input_tokens, response.usage.output_tokens)
            stop_reason = response.stop_reason or "unknown"
            if stop_reason == "refusal":
                raise _billed(ReaderRefusedError(_refusal(response)), usage)
            if stop_reason == "max_tokens":
                msg = f"the answer was cut off at {request.max_tokens} tokens"
                raise _billed(ReaderTruncatedError(msg), usage)
            text = next((block.text for block in response.content if block.type == "text"), None)
            if text is None:
                msg = f"the answer has no text (stop reason {stop_reason})"
                raise _billed(ReaderOutputError(msg), usage)
            try:
                value = adapter.validate_json(text)
            except ValidationError as error:
                raise _billed(ReaderOutputError(_problems(schema, error)), usage) from None
        except ReaderError as error:
            error.image_sha256 = sent
            raise
        return ReadResult(value=value, usage=usage, stop_reason=stop_reason, image_sha256=sent)

    def _create[M](
        self, request: ReadRequest, image: bytes | None, adapter: TypeAdapter[M]
    ) -> Message:
        content: list[ImageBlockParam | TextBlockParam] = []
        if image is not None:
            data = base64.standard_b64encode(image).decode("ascii")
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": MEDIA_TYPE, "data": data},
                }
            )
        content.append({"type": "text", "text": request.text})
        messages: list[MessageParam] = [{"role": "user", "content": content}]
        try:
            return self._sdk().messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=messages,
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": transform_schema(adapter.json_schema()),
                    }
                },
            )
        except (AuthenticationError, PermissionDeniedError) as error:
            msg = (
                f"the Claude API refused the key in {KEY_VARIABLE} ({error.status_code}"
                f" {_api_error(error)})"
            )
            raise ReaderUnavailableError(msg) from None
        except APIStatusError as error:
            msg = f"the Claude API answered {error.status_code}: {_api_error(error)}"
            retryable = error.status_code in RETRYABLE_STATUSES or error.status_code >= _SERVER
            raise _classified(ReaderUnavailableError(msg), billed=False, retry=retryable) from None
        except APIConnectionError as error:
            refused = isinstance(error.__cause__, httpx2.ConnectError)
            msg = f"the Claude API cannot be reached: {type(error).__name__}"
            raise _classified(ReaderUnavailableError(msg), billed=not refused, retry=True) from None
        except AnthropicError as error:
            msg = f"the Claude call failed: {type(error).__name__}"
            raise _classified(ReaderUnavailableError(msg), billed=True, retry=False) from None

    def check(self) -> None:
        """ReaderUnavailableError when there is no key to call with (nothing is sent)."""
        if self._client is None and self._settings.api_key is None:
            raise ReaderUnavailableError(_NO_KEY)

    def _sdk(self) -> Anthropic:
        if self._client is None:
            key = self._settings.api_key
            if key is None:
                raise ReaderUnavailableError(_NO_KEY)
            self._client = Anthropic(
                api_key=key.get_secret_value(),
                timeout=self._settings.timeout_s,
                max_retries=SDK_MAX_RETRIES,
            )
        return self._client


def _billed[E: ReaderError](error: E, usage: ReadUsage) -> E:
    error.usage = usage
    return error


def _classified(
    error: ReaderUnavailableError, *, billed: bool, retry: bool
) -> ReaderUnavailableError:
    """``billed``: the request may have reached the model before it failed (a timeout, a
    connection broken after it was made), so the budget charges it; a refused connection or an
    error status never did. ``retry``: the same read may succeed if made again."""
    error.maybe_billed = billed
    error.retryable = retry
    return error


def _api_error(error: APIStatusError) -> str:
    """The API's error type and message, card numbers masked (the message may quote input)."""
    body = error.body if isinstance(error.body, dict) else {}
    found = body.get("error")
    detail: dict[str, object] = found if isinstance(found, dict) else {}
    kind = detail.get("type") or "error"
    return mask_pans(f"{kind}: {detail.get('message') or error.message}").text


def _refusal(response: Message) -> str:
    details = response.stop_details
    category = getattr(details, "category", None) if details is not None else None
    return "the model declined to read the receipt" + (f" ({category})" if category else "")


def _problems(schema: type[object], error: ValidationError) -> str:
    """Where and why the answer is wrong, without quoting the answer back."""
    problems = "; ".join(
        f"{'.'.join(str(part) for part in detail['loc']) or '(answer)'}: {detail['msg']}"
        for detail in error.errors()
    )
    # An error location can be a key the model made up, card number included.
    return mask_pans(
        f"the answer does not match {getattr(schema, '__name__', schema)}: {problems}"
    ).text
