"""No INFO+ log line carries receipt content, prompts, model output or the API key (AC-A-14,
S2-AC-12, ADR-0011; claude-extraction plan R7, CE-TC-041).

A sentinel is planted in Claude's answer; the run goes through a retry, review and resolution
with DEBUG logging on; no line at INFO or above may contain a sentinel, any part of the prompt
Claude was sent, or the receipt-specific feedback. The Anthropic key never appears anywhere: not
in the settings' repr or dumps, the run configuration a trace records, an error or any log line
(at any level) of a read the API refused.
"""

import json
import logging
from typing import Any

import httpx2
import pytest
from anthropic import Anthropic

from paper_trail.adapters.anthropic.reader import AnthropicReader
from paper_trail.agents.schemas import ReceiptAnswer
from paper_trail.bootstrap.agents import run_config
from paper_trail.config import Settings
from paper_trail.observability import configure_logging
from paper_trail.ports.reader import ReaderUnavailableError, ReadRequest
from tests.agent_harness import agent_settings, build_harness
from tests.anthropic_api import message
from tests.extractions import L3, LOBLAWS, coded_answer, inconsistent_answer
from tests.factories import receipt_draft
from tests.fakes.catalog import TEST_APPLE, FakeCatalog
from tests.fakes.reader import ScriptedReader
from tests.images import png_bytes
from tests.support import MakeSettings, require_id

OUTPUT_SENTINEL = "SENTINEL-LLM-91C2"
KEY = "sk-ant-api03-SENTINEL-KEY-5D1E"
QUIET_LEVELS = {"debug", "notset"}
# What a catalog.* event may carry besides structlog's own keys: codes and ids, never text.
CATALOG_LOG_KEYS = {
    "event",
    "level",
    "timestamp",
    "logger",
    "scheme",
    "store_key",
    "code",
    "product_id",
    "existing_product_id",
    "refused_product_id",
    "path",
    "reason",
}


def _lines(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def _loud(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [line for line in lines if str(line.get("level", "")).lower() not in QUIET_LEVELS]


def test_ac_a_14_no_receipt_content_at_info_or_above(
    make_settings: MakeSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings(log_level="DEBUG", log_format="json"))
    first = inconsistent_answer()
    first["items"][0]["name"] = f"Eggs {OUTPUT_SENTINEL}"
    first["items"][0]["printed_text"] = f"KS ORG EGGS {OUTPUT_SENTINEL}"
    reader = ScriptedReader(first, then=inconsistent_answer())
    harness = build_harness(reader)
    graph = harness.process_image().graph
    assert graph is not None
    harness.resolve(require_id(graph.review_id), receipt_draft())

    lines = _lines(capsys.readouterr().out)

    retry = reader.requests[1]
    forbidden = [
        OUTPUT_SENTINEL,
        "KS SOURDOUGH",
        retry.system.strip().splitlines()[0][:40],
        *[line for line in retry.text.splitlines() if "9.89" in line or "25.46" in line],
    ]
    loud = _loud(lines)
    assert loud, "the run should log something at INFO (e.g. receipt.recorded)"
    for line in loud:
        text = json.dumps(line)
        for needle in forbidden:
            assert needle not in text, (needle, line.get("event"))


def test_ac_a_14_the_catalog_logs_codes_and_ids_only(
    make_settings: MakeSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    """PC-TC-84: Claude's answer, the line's printed text, the PLU entry's name and the product
    it names never reach a line at INFO or above; ``catalog.*`` events carry codes and ids."""
    configure_logging(make_settings(log_level="DEBUG", log_format="json"))
    answer = coded_answer([L3], store_name=LOBLAWS)
    answer["items"][0]["name"] = f"Apple {OUTPUT_SENTINEL}"
    harness = build_harness(
        ScriptedReader(answer),
        settings=agent_settings(enrichment=True),
        catalog=FakeCatalog({"3101": TEST_APPLE}),
    )
    graph = harness.process_image("sentinel-photo").graph
    assert graph is not None
    (item,) = harness.receipt(graph.receipt_id).items
    assert harness.product(require_id(item.product_id)).category is not None  # described

    lines = _lines(capsys.readouterr().out)

    loud = _loud(lines)
    assert loud
    forbidden = (OUTPUT_SENTINEL, "sentinel-photo", "Test Apple", "TST APPLE")
    for line in loud:
        text = json.dumps(line)
        for needle in forbidden:
            assert needle not in text, (needle, line.get("event"))
    for line in lines:
        if str(line.get("event", "")).startswith("catalog."):
            assert set(line) <= CATALOG_LOG_KEYS, line


# --- CE-TC-041: the key never leaks -------------------------------------------------------------


def _keyed_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    settings = Settings(env="test")
    assert settings.claude.api_key is not None
    assert settings.claude.api_key.get_secret_value() == KEY
    return settings


def test_ce_tc_041_settings_never_show_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _keyed_settings(monkeypatch)

    shown = [
        repr(settings),
        str(settings),
        repr(settings.claude),
        settings.model_dump_json(),
        json.dumps(settings.model_dump(mode="json")),
        repr(run_config(settings)),
    ]

    for text in shown:
        assert KEY not in text


def _refusing_client(requests: list[httpx2.Request]) -> Anthropic:
    """A real SDK client whose transport answers every request 401, the way the API answers a
    revoked key; no socket is opened."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        body = {
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        }
        return httpx2.Response(401, json=body)

    return Anthropic(
        api_key=KEY,
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(answer)),
    )


def test_ce_tc_041_a_rejected_key_is_named_never_shown(
    monkeypatch: pytest.MonkeyPatch, make_settings: MakeSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every log line at every level, the error and its chain: the variable's name, never the
    key (the key does travel in the request's header, which is where it belongs)."""
    settings = _keyed_settings(monkeypatch)
    configure_logging(make_settings(log_level="DEBUG", log_format="json"))
    requests: list[httpx2.Request] = []
    reader = AnthropicReader(settings.claude, client=_refusing_client(requests))
    request = ReadRequest(
        model=settings.claude.extract_model,
        tag="extraction/test",
        system="Read the receipt.",
        text="",
        image=png_bytes(),
        max_tokens=100,
    )

    with pytest.raises(ReaderUnavailableError) as raised:
        reader.read(request, ReceiptAnswer)

    assert requests, "the read reached the (fake) API"
    assert "ANTHROPIC_API_KEY" in str(raised.value)
    chain: list[BaseException] = []
    error: BaseException | None = raised.value
    while error is not None and error not in chain:
        chain.append(error)
        error = error.__cause__ or error.__context__
    for link in chain:
        assert KEY not in str(link)
        assert KEY not in repr(link)
    captured = capsys.readouterr()
    assert KEY not in captured.out
    assert KEY not in captured.err


THIRD_PARTY_LOGGERS = ("anthropic", "httpx2", "httpx", "httpcore")


def test_c4_a_debug_read_through_the_sdk_logs_no_image_and_no_key(
    monkeypatch: pytest.MonkeyPatch,
    make_settings: MakeSettings,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ruling C4: the SDK and HTTP client loggers stay at WARNING whatever the app's level, so
    their DEBUG request dumps (headers, the base64 photo) are never written."""
    settings = _keyed_settings(monkeypatch)
    configure_logging(make_settings(log_level="DEBUG", log_format="json"))
    caplog.set_level(logging.DEBUG)
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return message(coded_answer([L3]))

    client = Anthropic(
        api_key=KEY,
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(answer)),
    )
    request = ReadRequest(
        model=settings.claude.extract_model,
        tag="extraction/test",
        system="Read the receipt.",
        text="",
        image=png_bytes(),
        max_tokens=100,
    )

    AnthropicReader(settings.claude, client=client).read(request, ReceiptAnswer)

    (turn,) = json.loads(sent[0].content)["messages"]
    image_base64 = turn["content"][0]["source"]["data"]
    captured = capsys.readouterr()
    logged = [captured.out, captured.err, *(record.getMessage() for record in caplog.records)]
    assert [image_base64[:64] in text or KEY in text for text in logged] == [False] * len(logged)
    assert [logging.getLogger(name).getEffectiveLevel() for name in THIRD_PARTY_LOGGERS] == [
        logging.WARNING
    ] * len(THIRD_PARTY_LOGGERS)
