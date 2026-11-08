"""What the receipt graph's nodes work with (wired by the composition root or the tests)."""

from dataclasses import dataclass

from paper_trail.agents.prompts import PromptLibrary
from paper_trail.config import Settings
from paper_trail.ports.clock import Clock
from paper_trail.ports.images import ImageStore
from paper_trail.ports.llm import LlmPort
from paper_trail.ports.tools import ToolGateway
from paper_trail.ports.trace import Tracer


@dataclass(frozen=True, slots=True)
class AgentDeps:
    llm: LlmPort
    tools: ToolGateway
    tracer: Tracer
    clock: Clock
    settings: Settings
    prompts: PromptLibrary
    images: ImageStore  # the vision attempt reads the photo here, never through a tool
