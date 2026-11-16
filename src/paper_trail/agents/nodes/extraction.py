"""``extract`` (extraction role): Claude reads the stored photo into a ``ReceiptAnswer``
(claude-extraction plan TF, E1, E3, V4).

One ``ReceiptReader`` read per attempt, with the ``extraction/<version>`` prompt: attempt 1 on
``CLAUDE__EXTRACT_MODEL``, the retry on ``CLAUDE__RETRY_MODEL`` with the supervisor's feedback
in ``<validation_feedback>``. The photo is read from the ``ImageStore``, not through a tool:
image bytes have no place on the tool wire.

Card numbers in the answer, and in the error text of an answer that did not parse (it can
quote what the model wrote), are masked before they enter the state, so no checkpoint, draft,
feedback or trace holds them. The attempt did not parse when the answer does not match the schema
(``ReaderOutputError``) or a printed value cannot be read (``to_reading``): ``extraction`` is
None and ``parse_error`` says why (the retry's feedback). Every other ``ReaderError`` (refused,
cut off, unavailable, budget) propagates and stops the run with its checkpoint; the attempt is
not counted, so a resumed run makes the same read again.
"""

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.json_values import masked_json
from paper_trail.agents.nodes.supervisor import attempt_model
from paper_trail.agents.schemas import ReceiptAnswer
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.domain.errors import PrintedValueError
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.ports.reader import ReaderOutputError, ReadRequest

EXTRACTION_PROMPT = "extraction"
MAX_OUTPUT_TOKENS = 16000  # the retry model always thinks, and thinking counts here (I7)


def extract_node(deps: AgentDeps) -> Node:
    """``extract``: see the module docstring."""
    settings = deps.settings
    prompt = deps.prompts.load(EXTRACTION_PROMPT, settings.prompts.extraction_version)

    def extract(state: ReceiptState) -> ReceiptState:
        attempt = state.get("attempts", 0) + 1
        feedback = state.get("feedback", []) if attempt > 1 else []
        request = ReadRequest(
            model=attempt_model(settings.claude, attempt),
            tag=prompt.tag,
            system=prompt.system,
            text=prompt.text(feedback),
            image=deps.images.open(state["image_path"]),
            max_tokens=MAX_OUTPUT_TOKENS,
            attempt=attempt,
        )
        update: ReceiptState = {"attempts": attempt}
        try:
            answer = deps.reader.read(request, ReceiptAnswer).value
        except ReaderOutputError as error:
            update["extraction"], update["parse_error"] = None, mask_pans(str(error)).text
            return update
        masked, count = masked_json(answer.model_dump(mode="json"))
        answer = ReceiptAnswer.model_validate(masked)
        update["masked_pans"] = count
        try:
            answer.to_reading()
        except PrintedValueError as error:
            update["extraction"], update["parse_error"] = None, mask_pans(str(error)).text
            return update
        update["extraction"], update["parse_error"] = answer.model_dump(mode="json"), None
        return update

    return extract
