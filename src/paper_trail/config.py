"""Application settings, read from ``PAPER_TRAIL_*`` environment variables (PLAN 1.3).

``Settings`` is constructed once, in the CLI entry point, and passed down explicitly.
"""

from pathlib import Path
from typing import Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

DATABASE_FILENAME = "paper_trail.db"


class WorkerSettings(BaseModel):
    """Background worker loop (``PAPER_TRAIL_WORKER__*``)."""

    model_config = ConfigDict(frozen=True)

    poll_interval_s: float = Field(default=1.0, ge=0.1, le=60.0)
    heartbeat_interval_s: float = Field(default=10.0, gt=0)
    shutdown_grace_s: float = Field(default=8.0, gt=0)


class UploadSettings(BaseModel):
    """Image intake (``PAPER_TRAIL_UPLOAD__*``)."""

    model_config = ConfigDict(frozen=True)

    max_pixels: int = Field(default=40_000_000, gt=0)  # decompression-bomb guard


# The reads after a low-confidence upright one, degrees counter-clockwise, in order (ADR-0010).
OCR_RETRY_ROTATIONS: tuple[int, ...] = (90, 180, 270)


class OcrSettings(BaseModel):
    """Tesseract (``PAPER_TRAIL_OCR__*``, ADR-0010)."""

    model_config = ConfigDict(frozen=True)

    lang: str = "eng"
    # 6: one uniform block of text; keeps the right-hand price column of a crumpled receipt
    # that 4 (a single column of variable sizes) dropped (ADR-0010 amendment, ADR-0017).
    psm: int = Field(default=6, ge=0, le=13)
    timeout_s: float = Field(default=30.0, gt=0)  # per tesseract run
    # Below this mean confidence the image is also read at OCR_RETRY_ROTATIONS; 0: never.
    rotation_min_confidence: float = Field(default=0.6, ge=0, le=1)

    @property
    def worst_case_s(self) -> float:
        """The longest one read may take: every Tesseract run it can make, each at ``timeout_s``."""
        retries = len(OCR_RETRY_ROTATIONS) if self.rotation_min_confidence > 0 else 0
        return self.timeout_s * (1 + retries)


class OllamaSettings(BaseModel):
    """The LLM host (``PAPER_TRAIL_OLLAMA__*``, ADR-0009, ADR-0016)."""

    model_config = ConfigDict(frozen=True)

    base_url: AnyHttpUrl | None = None  # None: not configured, the LLM is unavailable
    probe_timeout_s: float = Field(default=2.0, gt=0)
    request_timeout_s: float = Field(default=180.0, gt=0)
    text_model: str = "qwen2.5:7b-instruct"
    vision_model: str = "qwen2.5vl:7b"
    embed_model: str = "nomic-embed-text"
    keep_alive: str = "5m"
    # Ollama's default context is 4096 tokens, too small for the few-shot extraction prompt.
    # One value for every request: changing it between requests reloads the model.
    num_ctx: int = Field(default=8192, gt=0)
    # Most tokens one extraction answer may take (num_predict): a model that loops on a hard
    # photo stops here instead of filling the context (ADR-0017 amendment).
    max_output_tokens: int = Field(default=4096, gt=0)

    @model_validator(mode="after")
    def _output_fits_the_context(self) -> Self:
        if self.max_output_tokens >= self.num_ctx:
            msg = (
                f"PAPER_TRAIL_OLLAMA__MAX_OUTPUT_TOKENS ({self.max_output_tokens}) must be less"
                f" than PAPER_TRAIL_OLLAMA__NUM_CTX ({self.num_ctx}): the prompt needs room too"
            )
            raise ValueError(msg)
        return self


class GraphSettings(BaseModel):
    """The receipt graph's limits (``PAPER_TRAIL_GRAPH__*``, ADR-0008)."""

    model_config = ConfigDict(frozen=True)

    max_text_attempts: int = Field(default=2, ge=1)
    max_vision_attempts: int = Field(default=2, ge=0)  # photo reads; a retry gets its own issues
    # A photo whose OCR mean confidence is below this goes to vision first; 1.0 (the default
    # since 2026-11-08): every photo; 0: never (ADR-0017).
    vision_first_below: float = Field(default=1.0, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    feedback: bool = True  # retries carry the validation issues (False: experiment E1 control)
    enrichment: bool = True  # describe the products a receipt created (False: skip the stage)


class PromptSettings(BaseModel):
    """Prompt versions per agent (``PAPER_TRAIL_PROMPTS__*``, agent-core plan 2.5)."""

    model_config = ConfigDict(frozen=True)

    extraction_version: str = "v2"  # v2: values as printed (ADR-0017); v1: integer cents
    vision_version: str = "v1"
    matching_version: str = "v1"
    enrichment_version: str = "v1"
    examples: bool = True  # include the few-shot example pairs


class MatchingSettings(BaseModel):
    """Product matching by embedding distance (``PAPER_TRAIL_MATCHING__*``, PLAN 6).

    Cosine distances (0 = same direction, 2 = opposite): a nearest product at most
    ``auto_link_max_distance`` away is linked; one at most ``review_max_distance`` away is
    confirmed by the model; anything further is a new product. Equal values leave no
    confirmation band.
    """

    model_config = ConfigDict(frozen=True)

    auto_link_max_distance: float = Field(default=0.15, ge=0.0, le=2.0)
    review_max_distance: float = Field(default=0.30, ge=0.0, le=2.0)

    @model_validator(mode="after")
    def _band_is_ordered(self) -> Self:
        if self.review_max_distance < self.auto_link_max_distance:
            msg = "REVIEW_MAX_DISTANCE must not be below AUTO_LINK_MAX_DISTANCE"
            raise ValueError(msg)
        return self


class ToolsSettings(BaseModel):
    """How the agents call their tools (``PAPER_TRAIL_TOOLS__*``, PLAN 7, ADR-0013)."""

    model_config = ConfigDict(frozen=True)

    transport: Literal["inprocess", "mcp"] = "inprocess"


class McpSettings(BaseModel):
    """The standalone MCP tool server (``PAPER_TRAIL_MCP__*``, PLAN 7): what
    ``paper-trail tools --transport mcp`` talks to. ``paper-trail process --transport mcp``
    serves its run's tools itself and uses only ``timeout_s``.

    ``timeout_s`` bounds one tool call, connecting included, so it must outlast the slowest
    tool: ``Settings`` refuses one that is not above the OCR worst case (every rotation run,
    ``OcrSettings.worst_case_s``) and the model request timeout.
    """

    model_config = ConfigDict(frozen=True)

    url: AnyHttpUrl = AnyHttpUrl("http://mcp:8000")
    timeout_s: float = Field(default=240.0, gt=0)  # connect plus answer, per tool call


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PAPER_TRAIL_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    env: Literal["dev", "test", "prod"] = "prod"
    data_dir: Path = Path("/data")
    database_path: Path = Field(default=None, validate_default=True)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Unset means console in dev and JSON everywhere else (see _default_log_format).
    log_format: Literal["json", "console"] = Field(default=None, validate_default=True)
    host: str = "0.0.0.0"  # noqa: S104 - bind address inside the container
    http_port: int = Field(default=8000, ge=1, le=65535)
    static_dir: Path = Path("/app/static")
    build_sha: str | None = None
    worker: WorkerSettings = WorkerSettings()
    upload: UploadSettings = UploadSettings()
    ocr: OcrSettings = OcrSettings()
    ollama: OllamaSettings = OllamaSettings()
    graph: GraphSettings = GraphSettings()
    prompts: PromptSettings = PromptSettings()
    matching: MatchingSettings = MatchingSettings()
    tools: ToolsSettings = ToolsSettings()
    mcp: McpSettings = McpSettings()

    @field_validator("database_path", mode="before")
    @classmethod
    def _default_database_path(cls, value: object, info: ValidationInfo) -> object:
        if value is None and "data_dir" in info.data:
            return info.data["data_dir"] / DATABASE_FILENAME
        return value

    @field_validator("log_format", mode="before")
    @classmethod
    def _default_log_format(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return "console" if info.data.get("env") == "dev" else "json"
        return value

    @model_validator(mode="after")
    def _mcp_outlasts_its_tools(self) -> Self:
        """An MCP call waits for its tool: ``ocr_read`` on Tesseract, the matching tools on the
        embedding model. A shorter MCP timeout would give up on a tool that is still working."""
        slowest = max(self.ocr.worst_case_s, self.ollama.request_timeout_s)
        if self.mcp.timeout_s <= slowest:
            runs = 1 + len(OCR_RETRY_ROTATIONS)
            msg = (
                f"PAPER_TRAIL_MCP__TIMEOUT_S ({self.mcp.timeout_s:g}) must be greater than"
                f" the OCR worst case (PAPER_TRAIL_OCR__TIMEOUT_S per Tesseract run, {runs} runs"
                " while PAPER_TRAIL_OCR__ROTATION_MIN_CONFIDENCE > 0) and"
                f" PAPER_TRAIL_OLLAMA__REQUEST_TIMEOUT_S (the slowest is {slowest:g})"
            )
            raise ValueError(msg)
        return self


def env_var_name(location: tuple[str | int, ...]) -> str:
    """The environment variable behind a validation error location, e.g. ``("worker", "x")``."""
    return "PAPER_TRAIL_" + "__".join(str(part) for part in location).upper()
