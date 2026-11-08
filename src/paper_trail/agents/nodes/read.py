"""``read`` (extraction role): OCR the stored image through the ``ocr_read`` tool.

The tool masks card numbers before the text enters the state (AC-A-04).
"""

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.json_values import as_int, as_optional_float, as_str
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.domain.enums import AgentRole


def read_node(deps: AgentDeps) -> Node:
    def read(state: ReceiptState) -> ReceiptState:
        result = deps.tools.call(
            AgentRole.EXTRACTION, "ocr_read", {"image_path": state.get("image_path")}
        )
        return {
            "ocr_text": as_str(result["text"]),
            "ocr_confidence": as_optional_float(result["mean_confidence"]),
            "masked_pans": as_int(result["masked_pans"]),
            "ocr_rotation": as_int(result["rotation_degrees"]),
        }

    return read
