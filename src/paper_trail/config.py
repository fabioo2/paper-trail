"""Application settings, read from ``PAPER_TRAIL_*`` environment variables (PLAN 1.3).

``Settings`` is constructed once, in the CLI entry point, and passed down explicitly.
"""

from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from paper_trail.domain.rules.budget import MODEL_PRICES

DATABASE_FILENAME = "paper_trail.db"
CATALOG_DIRNAME = "catalog"
PLU_FILENAME = "plu.csv"


class WorkerSettings(BaseModel):
    """Background worker loop (``PAPER_TRAIL_WORKER__*``)."""

    model_config = ConfigDict(frozen=True)

    poll_interval_s: float = Field(default=1.0, ge=0.1, le=60.0)
    heartbeat_interval_s: float = Field(default=10.0, gt=0)
    shutdown_grace_s: float = Field(default=8.0, gt=0)


class UploadSettings(BaseModel):
    """Image intake (``PAPER_TRAIL_UPLOAD__*``)."""

    model_config = ConfigDict(frozen=True)

    max_bytes: int = Field(default=15_000_000, gt=0)
    max_pixels: int = Field(default=40_000_000, gt=0)  # decompression-bomb guard


class ClaudeSettings(BaseModel):
    """The Claude API (``PAPER_TRAIL_CLAUDE__*``, claude-extraction plan E7).

    ``api_key`` is read from ``ANTHROPIC_API_KEY`` only (``Settings`` fills it in); None: no
    key, so every receipt that needs Claude fails with a message naming that variable. The
    budget is per UTC calendar month and checked before each call; 0 stops every call.
    """

    model_config = ConfigDict(frozen=True)

    api_key: SecretStr | None = None
    extract_model: str = Field(default="claude-haiku-5-5", min_length=1)
    retry_model: str = Field(default="claude-opus-5-5", min_length=1)
    # Opus can take minutes for 16k output tokens; an abandoned call is still billed.
    timeout_s: float = Field(default=300.0, gt=0, le=600)
    monthly_budget_usd: Decimal = Field(default=Decimal("5.00"), ge=0)

    @field_validator("extract_model", "retry_model")
    @classmethod
    def _priced(cls, model: str, info: ValidationInfo) -> str:
        """Every call's cost is known: a model without a price cannot be configured."""
        if model not in MODEL_PRICES:
            variable = env_var_name(("claude", str(info.field_name)))
            msg = f"{variable}: {model!r} has no price; use one of {', '.join(MODEL_PRICES)}"
            raise ValueError(msg)
        return model


class GraphSettings(BaseModel):
    """The receipt graph's limits (``PAPER_TRAIL_GRAPH__*``, ADR-0008)."""

    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=2, ge=1, le=2)  # Claude reads: extract, then retry model
    feedback: bool = True  # retries carry the validation issues (False: experiment E1 control)
    enrichment: bool = True  # describe the products a receipt created (False: skip the stage)


class CatalogSettings(BaseModel):
    """Product codes and catalogs (``PAPER_TRAIL_CATALOG__*``, barcode-lookup plan 6).

    ``enabled`` false sends coded lines the way of uncoded ones (``process --no-catalog``);
    matching still asks no model. ``plu_path`` defaults to ``<data_dir>/catalog/plu.csv``
    (``Settings`` fills it in, so it is never None there)."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    plu_path: Path | None = None


class LookupSettings(BaseModel):
    """Online product databases asked for a barcode (``PAPER_TRAIL_LOOKUP__*``,
    claude-extraction plan D3, V10). Only the barcode is sent. ``open_facts`` asks Open Food
    Facts, then Open Beauty Facts and Open Products Facts; ``upcitemdb`` asks UPCitemdb's free
    trial endpoint last (off by default: its terms do not clearly allow keeping its answers)."""

    model_config = ConfigDict(frozen=True)

    open_facts: bool = True
    upcitemdb: bool = False
    timeout_s: float = Field(default=10.0, gt=0, le=60)


class PromptSettings(BaseModel):
    """Prompt versions per agent (``PAPER_TRAIL_PROMPTS__*``, agent-core plan 2.5)."""

    model_config = ConfigDict(frozen=True)

    extraction_version: str = "v3"  # v3: Claude reads the photo item by item (E1)


class ToolsSettings(BaseModel):
    """How the agents call their tools (``PAPER_TRAIL_TOOLS__*``, PLAN 7, ADR-0013)."""

    model_config = ConfigDict(frozen=True)

    transport: Literal["inprocess", "mcp"] = "inprocess"


class McpSettings(BaseModel):
    """The standalone MCP tool server (``PAPER_TRAIL_MCP__*``, PLAN 7): what
    ``paper-trail tools --transport mcp`` talks to. ``paper-trail process --transport mcp``
    serves its run's tools itself and uses only ``timeout_s``.

    ``timeout_s`` bounds one tool call, connecting included: ``Settings`` refuses one that is
    not above ``CLAUDE__TIMEOUT_S``.
    """

    model_config = ConfigDict(frozen=True)

    url: AnyHttpUrl = AnyHttpUrl("http://mcp:8000")
    timeout_s: float = Field(default=330.0, gt=0)  # connect plus answer, per tool call


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
    # The SDK's own variable, without the PAPER_TRAIL_ prefix; moved into ``claude.api_key``.
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias="ANTHROPIC_API_KEY", exclude=True, repr=False
    )
    claude: ClaudeSettings = Field(default=ClaudeSettings(), validate_default=True)
    graph: GraphSettings = GraphSettings()
    prompts: PromptSettings = PromptSettings()
    catalog: CatalogSettings = Field(default=CatalogSettings(), validate_default=True)
    lookup: LookupSettings = LookupSettings()
    tools: ToolsSettings = ToolsSettings()
    mcp: McpSettings = McpSettings()

    @field_validator("database_path", mode="before")
    @classmethod
    def _default_database_path(cls, value: object, info: ValidationInfo) -> object:
        if value is None and "data_dir" in info.data:
            return info.data["data_dir"] / DATABASE_FILENAME
        return value

    @field_validator("catalog", mode="after")
    @classmethod
    def _default_plu_path(cls, value: CatalogSettings, info: ValidationInfo) -> CatalogSettings:
        if value.plu_path is None and "data_dir" in info.data:
            plu_path = info.data["data_dir"] / CATALOG_DIRNAME / PLU_FILENAME
            return value.model_copy(update={"plu_path": plu_path})
        return value

    @field_validator("claude", mode="after")
    @classmethod
    def _claude_api_key(cls, value: ClaudeSettings, info: ValidationInfo) -> ClaudeSettings:
        """Only ``ANTHROPIC_API_KEY`` holds the key: ``PAPER_TRAIL_CLAUDE__API_KEY`` is not read."""
        return value.model_copy(update={"api_key": info.data.get("anthropic_api_key")})

    @field_validator("log_format", mode="before")
    @classmethod
    def _default_log_format(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            return "console" if info.data.get("env") == "dev" else "json"
        return value

    @model_validator(mode="after")
    def _mcp_outlasts_claude(self) -> Self:
        """An MCP call must not give up before a Claude call made behind it could finish."""
        if self.mcp.timeout_s <= self.claude.timeout_s:
            msg = (
                f"PAPER_TRAIL_MCP__TIMEOUT_S ({self.mcp.timeout_s:g}) must be greater than"
                f" PAPER_TRAIL_CLAUDE__TIMEOUT_S ({self.claude.timeout_s:g})"
            )
            raise ValueError(msg)
        return self


def env_var_name(location: tuple[str | int, ...]) -> str:
    """The environment variable behind a validation error location, e.g. ``("worker", "x")``."""
    return "PAPER_TRAIL_" + "__".join(str(part) for part in location).upper()
