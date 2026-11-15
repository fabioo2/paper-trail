"""``paper-trail process`` with the slice D stages on (the production defaults): the vision
attempt (AC-D-01, AC-D-03, ``--no-vision``) and enrichment by lookup, end to end.

Real composition root, SQLite, Tesseract for the photo, the real Ollama adapters for chat and
embeddings; the model is a scripted fake Ollama on 127.0.0.1, so the vision request is checked
on the wire: the vision model and the stored photo as base64 on the last message.

Pinned (docs/tests/agent-core-D.md):
- ``GRAPH__MAX_VISION_ATTEMPTS`` defaults to 2 (1 before extraction accuracy) and
  ``GRAPH__ENRICHMENT`` to true; ``--no-vision``
  sets the vision attempts to 0. The trace's ``config`` records ``max_vision_attempts`` and
  ``enrichment``; ``totals.vision_attempts`` counts the vision attempts.
- The vision request's image is the photo as stored by the image store (EXIF stripped), read
  back from ``<data dir>/images/<receipts.image_path>``. The trace file never holds it.
- Product codes (PC-TC-89): enrichment asks no model. For a receipt of PLU lines the terminal
  shows the matching NOTE (``new 2 (plu 2)``) and one ``tool describe_from_catalog`` row per new
  product (agent ``supervisor``); the only model request is the extraction.
"""

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from paper_trail.config import OllamaSettings
from tests.e2e.agent_cli import (
    StartOllama,
    configure_cli,
    fake_ollama_env,
    invoke,
    rows,
    run_database,
    trace_files,
)
from tests.extractions import (
    COSTCO_OCR,
    L3,
    L4,
    LOBLAWS,
    PLU_CSV,
    extraction,
    printed_extraction,
    printed_inconsistent_extraction,
)
from tests.images import PRINTED_RECEIPTS

RUN = "d"
VISION_MODEL = OllamaSettings().vision_model


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return configure_cli(monkeypatch, tmp_path, slice_d=True)


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartOllama]:
    with fake_ollama_env(monkeypatch) as start:
        yield start


@pytest.fixture
def photo(tmp_path: Path) -> Path:
    path = tmp_path / "costco.jpg"
    path.write_bytes(PRINTED_RECEIPTS[0].render())
    return path


@pytest.fixture
def receipt_text(tmp_path: Path) -> Path:
    path = tmp_path / "costco.txt"
    path.write_text(COSTCO_OCR, encoding="utf-8")
    return path


def _process(*args: str) -> tuple[int, str]:
    result = invoke("process", "--no-color", "--run", RUN, *args)
    return result.exit_code, result.output


def _trace(data_dir: Path) -> tuple[str, dict[str, Any]]:
    (path,) = trace_files(data_dir, RUN)
    text = path.read_text(encoding="utf-8")
    document: dict[str, Any] = json.loads(text)
    return text, document


def test_ac_d_01_an_unreadable_photo_gets_its_vision_attempts_with_the_stored_image(
    data_dir: Path, ollama: StartOllama, photo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendered photo reads well (OCR confidence above a GRAPH__VISION_FIRST_BELOW of 0.65,
    the default until 2026-11-08), so the text attempts come first; otherwise production
    defaults: extraction/v2, two vision attempts."""
    monkeypatch.setenv("PAPER_TRAIL_GRAPH__VISION_FIRST_BELOW", "0.65")
    fake = ollama(
        printed_inconsistent_extraction(),
        printed_inconsistent_extraction(),
        then=printed_inconsistent_extraction(line=1),
    )

    code, output = _process(str(photo))

    assert code == 0, output  # parked for review, not failed
    assert "NEEDS_REVIEW" in output
    assert fake.calls == 4
    models = [request["model"] for request in fake.chat_requests]
    assert models[2:] == [VISION_MODEL, VISION_MODEL]
    assert VISION_MODEL not in models[:2]
    vision_messages = fake.chat_requests[2]["messages"]
    assert all("images" not in message for message in vision_messages[:-1])
    (encoded,) = vision_messages[-1]["images"]
    ((image_path,),) = rows(run_database(data_dir, RUN), "SELECT image_path FROM receipts")
    stored = (data_dir / "images" / image_path).read_bytes()
    assert base64.b64decode(encoded) == stored
    text, document = _trace(data_dir)
    assert document["config"]["max_vision_attempts"] == 2
    assert document["totals"]["vision_attempts"] == 2
    assert encoded not in text


def test_no_vision_skips_the_vision_attempt(
    data_dir: Path, ollama: StartOllama, photo: Path
) -> None:
    fake = ollama(then=printed_inconsistent_extraction())

    code, output = _process("--no-vision", str(photo))

    assert code == 0, output
    assert fake.calls == 2
    assert VISION_MODEL not in {request["model"] for request in fake.chat_requests}
    _, document = _trace(data_dir)
    assert document["config"]["max_vision_attempts"] == 0
    assert document["totals"]["vision_attempts"] == 0


def test_ac_d_03_a_text_file_never_gets_a_vision_attempt(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    fake = ollama(then=printed_inconsistent_extraction())

    code, output = _process("--text-file", str(receipt_text))

    assert code == 0, output
    assert fake.calls == 2
    assert all(
        "images" not in message for request in fake.chat_requests for message in request["messages"]
    )
    _, document = _trace(data_dir)
    assert document["totals"]["vision_attempts"] == 0


def _plu_line(raw_text: str, amount: str) -> dict[str, Any]:
    return {
        "raw_text": raw_text,
        "description": raw_text.title(),
        "quantity": "1",
        "unit_price": amount,
        "amount": amount,
        "kind": "item",
    }


def test_new_products_are_described_from_the_plu_table_and_shown_on_the_terminal(
    data_dir: Path, ollama: StartOllama, tmp_path: Path
) -> None:
    plu = data_dir / "catalog" / "plu.csv"
    plu.parent.mkdir(parents=True, exist_ok=True)
    plu.write_text(PLU_CSV, encoding="utf-8")
    receipt = tmp_path / "loblaws.txt"
    receipt.write_text(f"{LOBLAWS}\n{L3} 2.49\n{L4} 2.50\nTOTAL 4.99\n", encoding="utf-8")
    answer = printed_extraction(
        store_name=LOBLAWS,
        subtotal=None,
        taxes=[],
        total="4.99",
        items=[_plu_line(L3, "2.49"), _plu_line(L4, "2.50")],
    )
    fake = ollama(answer)

    code, output = _process("--text-file", str(receipt))

    assert code == 0, output
    assert fake.calls == 1
    assert "enrichment/v1" not in output
    assert "new 2 (plu 2)" in output
    describe_rows = [line for line in output.splitlines() if "describe_from_catalog" in line]
    assert len(describe_rows) == 2
    assert all("supervisor" in line for line in describe_rows)
    products = rows(
        run_database(data_dir, RUN),
        "SELECT canonical_name, category, tags FROM products ORDER BY id",
    )
    assert products == [
        ("Test Apple", "produce", "[]"),
        ("Organic Test Apple", "produce", '["organic"]'),
    ]
    _, document = _trace(data_dir)
    assert document["config"]["enrichment"] is True
    assert len(fake.embed_requests) == 1  # the two confirmed new products, in save_matches


# --- EA-AC-11: v2 by default, v1 still runs end to end (experiment E1's comparison) --------------


def _status(data_dir: Path) -> str:
    ((status,),) = rows(run_database(data_dir, RUN), "SELECT status FROM receipts")
    return str(status)


def test_ea_ac_11_the_default_extraction_prompt_is_v2(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    fake = ollama(printed_extraction())

    code, output = _process("--text-file", str(receipt_text))

    assert code == 0, output
    assert fake.chat_requests[0]["format"]["title"] == "PrintedExtraction"
    _, document = _trace(data_dir)
    assert document["config"]["prompt_versions"]["extraction"] == "v2"
    assert _status(data_dir) == "completed"


def test_ea_ac_11_prompt_version_v1_still_runs_the_v1_schema(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    fake = ollama(extraction())

    code, output = _process("--prompt-version", "v1", "--text-file", str(receipt_text))

    assert code == 0, output
    assert fake.chat_requests[0]["format"]["title"] == "ExtractionOutput"
    _, document = _trace(data_dir)
    assert document["config"]["prompt_versions"]["extraction"] == "v1"
    assert _status(data_dir) == "completed"
