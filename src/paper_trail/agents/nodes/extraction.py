"""``extract_text`` and ``extract_vision`` (extraction role): the model reads the receipt into
``ExtractionOutput`` (ADR-0017).

``extract_text`` gives the text model the OCR text with the ``extraction/<version>`` prompt.
``extract_vision`` gives the vision model the stored photo alone, turned the way the OCR read
it, with its own ``vision/<version>`` prompt (no examples, no OCR text): it runs first on a poor
OCR read, or after the text attempts (agent core slice D, S3-AC-04). The photo is read from the
``ImageStore``, not through a tool: image bytes have no place on the tool wire. A retry carries
the supervisor's feedback in ``<validation_feedback>``. Every request bounds its answer
(``OLLAMA__MAX_OUTPUT_TOKENS``).

The answer is validated with the prompt's answer model (``ANSWER_MODELS``) and converted by its
``to_output()``: printed values become integer cents here, so the state always holds
``ExtractionOutput`` JSON. The attempt did not parse when the answer does not match the schema
(after the adapter's one repair), when a printed value cannot be read, or when it copied the
prompt's example receipt (the leak guard: its store, or two of its lines). A multi-buy the model
missed is reconciled from the OCR text when that makes the lines add up exactly (a NOTE event).
"""

from collections.abc import Sequence

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.schemas import ExtractionOutput, answer_model
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.domain.enums import AgentRole
from paper_trail.domain.errors import PrintedValueError
from paper_trail.domain.models import JsonValue
from paper_trail.domain.rules.normalization import is_generic_alias, normalize_alias
from paper_trail.domain.rules.reconcile import reconcile_multi_buy
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.llm import LlmOutputError, LlmRequest

EXTRACTION_PROMPT = "extraction"
VISION_PROMPT = "vision"
COPIED_LINES = 2  # example lines in one answer that make it a copy
COPIED_EXAMPLE = (
    "the answer copied the example receipt; read this receipt and answer with what it prints"
)


def extract_text_node(deps: AgentDeps) -> Node:
    return _extraction_node(deps, vision=False)


def extract_vision_node(deps: AgentDeps) -> Node:
    return _extraction_node(deps, vision=True)


def _extraction_node(deps: AgentDeps, *, vision: bool) -> Node:
    settings = deps.settings
    name, version = (
        (VISION_PROMPT, settings.prompts.vision_version)
        if vision
        else (EXTRACTION_PROMPT, settings.prompts.extraction_version)
    )
    prompt = deps.prompts.load(name, version, examples=settings.prompts.examples)
    schema = answer_model(name, version)
    examples = _guarded_texts(prompt.example_texts())
    model = settings.ollama.vision_model if vision else settings.ollama.text_model

    def extract(state: ReceiptState) -> ReceiptState:
        update: ReceiptState = (
            {"vision_attempts": state.get("vision_attempts", 0) + 1}
            if vision
            else {"text_attempts": state.get("text_attempts", 0) + 1}
        )
        images: Sequence[bytes] = (_photo(deps, state),) if vision else ()
        request = LlmRequest(
            model=model,
            tag=prompt.tag,
            messages=prompt.messages(
                state.get("ocr_text") or "", state.get("feedback", []), images
            ),
            temperature=0.0,
            max_tokens=settings.ollama.max_output_tokens,
        )
        try:
            answer = deps.llm.complete_structured(request, schema).value
            output = answer.to_output()
        except (LlmOutputError, PrintedValueError) as error:
            update["extraction"], update["parse_error"] = None, str(error)
            return update
        for note in answer.notes():
            _note(deps, "tax", note)
        output = _reconciled(deps, output, state.get("ocr_text") or "")
        if _copies(output, examples):
            update["extraction"], update["parse_error"] = None, COPIED_EXAMPLE
            return update
        update["extraction"], update["parse_error"] = output.model_dump(mode="json"), None
        return update

    return extract


def _reconciled(deps: AgentDeps, output: ExtractionOutput, ocr_text: str) -> ExtractionOutput:
    """``output`` with a multi-buy it missed taken from the OCR text (``reconcile_multi_buy``:
    exact or nothing), and a NOTE in the trace when it changed, so no fix is silent."""
    fix = reconcile_multi_buy(
        [(line.kind, line.line_total_cents) for line in output.items],
        output.subtotal_cents,
        ocr_text,
    )
    if fix is None:
        return output
    items = list(output.items)
    for position, cents in enumerate(fix.shares, start=fix.first):
        items[position] = items[position].model_copy(
            update={"quantity": "1", "unit_price_cents": cents, "line_total_cents": cents}
        )
    _note(
        deps,
        "reconcile",
        f"reconciled multi-buy {fix.detail} from OCR",
        {"detail": fix.detail, "first_line": fix.first + 1, "shares": list(fix.shares)},
    )
    return output.model_copy(update={"items": items})


def _note(
    deps: AgentDeps, name: str, summary: str, data: dict[str, JsonValue] | None = None
) -> None:
    """A NOTE trace event: code changed what the model answered, visibly."""
    deps.tracer.emit(AgentRole.EXTRACTION, TraceKind.NOTE, name, summary, data=data)


def _guarded_texts(example_texts: frozenset[str]) -> frozenset[str]:
    """The examples' store names and lines that an answer must not repeat. A generic line
    ("INSTANT SAVINGS", "DEPOSIT") is printed on real receipts too, so it is never a copy."""
    aliases = (normalize_alias(text) for text in example_texts)
    return frozenset(alias for alias in aliases if not is_generic_alias(alias))


def _copies(output: ExtractionOutput, guarded: frozenset[str]) -> bool:
    """The example's store, or at least ``COPIED_LINES`` of its lines: one line alone is no
    copy, since real receipts print the examples' products too ("BANANAS", "LIMES")."""
    if normalize_alias(output.store_name) in guarded:
        return True
    copied = sum(normalize_alias(line.raw_text) in guarded for line in output.items)
    return copied >= COPIED_LINES


def _photo(deps: AgentDeps, state: ReceiptState) -> bytes:
    """The stored photo, turned upright the way the OCR read it (a sideways photo made the
    vision model loop). The supervisor never routes a receipt without one to vision."""
    image_path = state.get("image_path")
    if image_path is None:
        msg = f"receipt {state['receipt_id']} has no photo for a vision attempt"
        raise ValueError(msg)
    return deps.images.open_upright(image_path, state.get("ocr_rotation", 0))
