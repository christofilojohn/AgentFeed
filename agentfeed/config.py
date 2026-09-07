"""Runtime configuration."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "AgentFeed"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
#  Inside a packaged app the dashboard lives in the bundle, not beside the
#  source tree; the launcher points here with an environment variable.
UI_DIR = Path(os.environ.get("AGENTFEED_UI_DIR") or (PROJECT_ROOT / "ui"))


class ModelProfile(BaseModel):
    key: str
    label: str
    chat_prefer: list[str]
    embed_prefer: list[str]
    chat_model: str
    embed_model: str
    ctx: int
    enrich_concurrency: int
    enrich_word_budget: int
    notes: str


PROFILES: dict[str, ModelProfile] = {
    "36gb": ModelProfile(
        key="36gb", label="36 GB unified (M3/M4 Pro or Max)",
        #  Ollama and LM Studio name the same model differently, so list
        #  both spellings. MoE first: ~3B active parameters per token beats
        #  a dense model several times its size on tokens/second.
        chat_prefer=["qwen3:30b-a3b", "qwen3-30b-a3b-instruct-2507",
                     "qwen3-30b-a3b-instruct", "qwen3-coder-30b-a3b-instruct",
                     "qwen3:8b", "qwen/qwen3.5-9b", "qwen3-32b"],
        embed_prefer=["text-embedding-qwen3-embedding-0.6b",
                      "text-embedding-nomic-embed-text-v1.5"],
        chat_model="qwen3-30b-a3b-instruct-2507",
        embed_model="text-embedding-qwen3-embedding-0.6b",
        ctx=32768, enrich_concurrency=3, enrich_word_budget=2200,
        notes="Mixture-of-Experts models win here: only ~3B parameters are "
              "active per token, so they beat a dense 12B several times over.",
    ),
    "16gb": ModelProfile(
        key="16gb", label="16 GB unified (M1/M2/M3 base)",
        chat_prefer=["qwen3:8b", "qwen/qwen3.5-9b", "qwen3-8b", "qwen3:4b",
                     "qwen3-14b"],
        embed_prefer=["text-embedding-qwen3-embedding-0.6b",
                      "text-embedding-nomic-embed-text-v1.5"],
        chat_model="qwen3-8b",
        embed_model="text-embedding-qwen3-embedding-0.6b",
        ctx=16384, enrich_concurrency=2, enrich_word_budget=1400,
        notes="~6 GB at 4-bit, leaving room for everything else.",
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTFEED_", env_file=".env",
                                      extra="ignore")

    data_dir: Path = Path.home() / "Library" / "Application Support" / "AgentFeed"

    #  Which domain pack shapes the vocabulary. See agentfeed/domains/.
    domain: str = "generic"

    #  --- models ---
    profile: str = "36gb"
    #  Empty means auto-detect: Ollama, then LM Studio, then llama.cpp, then
    #  vLLM. Set it to pin one, or to reach a server on another machine.
    llm_base_url: str = ""
    llm_provider: str = ""      # "ollama" | "lmstudio" | ... ; "" = auto
    llm_api_key: str = "lm-studio"
    chat_model: str | None = None
    assistant_model: str | None = None
    embed_model: str | None = None
    reasoning_effort: str = ""
    assistant_reasoning_effort: str = ""
    llm_timeout: float = 300.0

    #  --- fetching ---
    user_agent: str = "AgentFeed/0.1 (+agent feed reader)"
    contact_email: str = ""
    fetch_timeout: float = 45.0
    fetch_concurrency: int = 6
    max_items_per_source: int = 60
    backfill_days: int = 21
    enable_search_sources: bool = True
    search_min_interval_s: float = 6.0

    #  --- protocol ---
    feed_id: str = "agentfeed.local"
    feed_title: str = "AgentFeed"
    public_base_url: str = "http://127.0.0.1:8770"
    #  Subscriptions are unauthenticated on localhost. Set a token to require
    #  `Authorization: Bearer <token>` on every AFP call once this is exposed
    #  beyond the machine it runs on.
    token: str = ""
    max_tokens_per_sync: int = 32000

    #  --- server ---
    host: str = "127.0.0.1"
    port: int = 8770
    daily_run_at: str = "06:30"

    @property
    def model_profile(self) -> ModelProfile:
        return PROFILES.get(self.profile, PROFILES["36gb"])

    @property
    def active_chat_model(self) -> str:
        return self.chat_model or self.model_profile.chat_model

    @property
    def active_embed_model(self) -> str:
        return self.embed_model or self.model_profile.embed_model

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agentfeed.sqlite3"

    @property
    def vector_path(self) -> Path:
        return self.data_dir / "vectors.npz"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "raw").mkdir(exist_ok=True)


settings = Settings()
