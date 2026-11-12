"""Runtime configuration, read from environment variables / `.env`."""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- General -----------------------------------------------------------
    timezone: str = "Europe/Bucharest"
    data_dir: Path = ROOT / "data"
    # Safety switch: while true, nothing is sent to Facebook/Instagram. The
    # whole flow (generate, approve, schedule) still runs and logs what it
    # would have published.
    dry_run: bool = True
    generate_time: time = time(8, 0)
    publish_time: time = time(18, 30)
    publish_targets: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["facebook", "instagram"])
    max_publish_attempts: int = 3

    # --- Claude ------------------------------------------------------------
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-5-5"
    claude_effort: str = "medium"
    claude_review_model: str = "claude-haiku-5-5"
    # Server-side refusal fallback (Claude API only).
    claude_fallbacks: bool = True

    # --- Stock photos (Thursday posts) -------------------------------------
    # Either key works; Pixabay is used if both are set.
    pixabay_api_key: str = ""
    pexels_api_key: str = ""

    # --- Telegram (approval) ----------------------------------------------
    telegram_bot_token: str = ""
    # Comma-separated chat ids allowed to approve. Empty = locked; the bot
    # answers /start with the chat id so you can fill this in.
    telegram_allowed_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- Meta Graph API ----------------------------------------------------
    meta_access_token: str = ""  # System User token, never expires
    meta_page_id: str = ""
    meta_ig_user_id: str = ""
    meta_graph_version: str = "v23.0"

    # --- Media hosting -----------------------------------------------------
    storage_backend: str = "local"  # "r2" in production, "local" for dev
    media_public_base_url: str = "https://media.abonix.net"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "abonix-social"

    @field_validator("publish_targets", "telegram_allowed_chat_ids", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_url(self) -> str:
        return f"sqlite:///{(self.data_dir / 'social.db').as_posix()}"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.media_dir.mkdir(parents=True, exist_ok=True)
    return s


CONTENT_DIR = ROOT / "app" / "content"
TEMPLATES_DIR = ROOT / "templates"
ASSETS_DIR = ROOT / "assets"
