"""Runtime configuration, loaded from environment variables and ``backend/.env``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent

ProviderChoice = Literal["auto", "gemini", "openai_compat", "none"]

# Model used when a provider is picked but no model name is configured.
DEFAULT_MODELS = {
    ("gemini", "chat"): "gemini-2.5-flash",
    ("gemini", "vision"): "gemini-2.5-flash",
    ("gemini", "embedding"): "gemini-embedding-001",
    # Defaults target Groq's OpenAI-compatible API. For xAI Grok set
    # OPENAI_COMPAT_BASE_URL=https://api.x.ai/v1 and LLM_MODEL / VLM_MODEL.
    ("openai_compat", "chat"): "llama-3.3-70b-versatile",
    ("openai_compat", "vision"): "meta-llama/llama-4-scout-17b-16e-instruct",
    ("openai_compat", "embedding"): "text-embedding-3-small",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Farm identity -----------------------------------------------------
    app_name: str = "GAIA Farm Intelligence"
    farm_name: str = "My farm"
    farmer_name: str = "Farmer"
    farm_timezone: str = "Asia/Kolkata"
    farm_region: str = "India"  # added to web searches for locally relevant results
    data_dir: Path = BACKEND_DIR / "data"

    # --- Storage -------------------------------------------------------------
    # "embedded"  -> local PostgreSQL + pgvector started via pgserver (no Docker)
    # postgresql://user:pass@host:5432/db -> external PostgreSQL with pgvector
    # sqlite:///path.db -> lightweight fallback (tests); vector search runs in Python
    database_url: str = "embedded"

    # --- Security ------------------------------------------------------------
    api_key: str | None = None  # when set, app/web calls must send X-API-Key
    device_key: str = "gaia-rover-dev-key"  # rover telemetry must send X-Device-Key
    cors_origins: list[str] = ["*"]

    # --- Generative AI providers ---------------------------------------------
    llm_provider: ProviderChoice = "auto"
    llm_model: str | None = None
    vlm_provider: ProviderChoice = "auto"
    vlm_model: str | None = None
    embedding_provider: Literal["auto", "gemini", "openai_compat", "hashing"] = "auto"
    embedding_model: str | None = None
    embedding_dim: int = 768

    gemini_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY")
    )
    gemini_thinking_budget: int | None = None
    openai_compat_base_url: str = "https://api.groq.com/openai/v1"
    openai_compat_api_key: str | None = None
    # Sent as "reasoning_effort" to thinking models (Groq qwen3: none|default, xAI: low|high,
    # Ollama: none to disable thinking). Unset = provider default.
    openai_compat_reasoning_effort: str | None = None
    llm_timeout_seconds: float = 90.0

    # --- Web search (open-ended chat + diagnosis agent) ----------------------
    search_provider: Literal["auto", "tavily", "duckduckgo", "none"] = "auto"
    tavily_api_key: str | None = None
    search_max_results: int = 5

    # --- Sensor ingestion ----------------------------------------------------
    window_minutes: int = 15
    ingest_grace_seconds: int = 90
    ingest_interval_seconds: int = 60
    scheduler_enabled: bool = True
    llm_digest_enabled: bool = False

    # --- Retrieval / chat ----------------------------------------------------
    rag_top_k: int = 6
    chat_history_turns: int = 6

    # --- Diagnosis -----------------------------------------------------------
    max_upload_mb: int = 12
    vlm_max_image_px: int = 1600
    agent_max_rounds: int = 2
    agent_queries_per_round: int = 3

    # --- Rover ---------------------------------------------------------------
    rover_base_url: str = "http://gaia-rover.local"
    rover_stream_url: str | None = None
    rover_timeout_seconds: float = 5.0

    @field_validator("window_minutes")
    @classmethod
    def _window_divides_day(cls, v: int) -> int:
        if v <= 0 or 1440 % v:
            raise ValueError("window_minutes must evenly divide 1440 (e.g. 5, 10, 15, 30, 60)")
        return v

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def resolved_rover_stream_url(self) -> str:
        if self.rover_stream_url:
            return self.rover_stream_url
        base = self.rover_base_url.rstrip("/")
        scheme, _, rest = base.partition("://")
        host = rest.split("/")[0].split(":")[0]
        return f"{scheme}://{host}:81/stream"


@lru_cache
def get_settings() -> Settings:
    return Settings()
