"""
config.py — Centralised environment configuration via pydantic-settings.
All URLs and secrets must come from environment variables — never hardcoded.
"""

from __future__ import annotations

import socket
# Set default socket timeout to 30s to prevent indefinite hangs on dead connections
socket.setdefaulttimeout(30.0)

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Core secrets ──────────────────────────────────────────────────────────
    groq_api_key: Optional[str] = None
    jwt_secret_key: Optional[str] = Field(default=None, validation_alias="JWT_SECRET_KEY")
    jwt_secret: Optional[str] = Field(default=None, validation_alias="JWT_SECRET")
    cloudinary_url: Optional[str] = None

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "./storyforge.db"

    # ── URLs (no hardcoded localhost / Render / Vercel values) ────────────────
    frontend_url: str = ""
    backend_public_url: str = ""
    voiceforge_url: str = ""

    # ── Storage & runtime ─────────────────────────────────────────────────────
    output_dir: str = "./output"
    env: str = "development"
    port: int = 8000

    # ── Local GPU & Models Configuration ──────────────────────────────────────
    use_local_llm: bool = False
    use_local_images: bool = False
    use_local_whisper: bool = False
    ollama_url: str = "http://localhost:11434/v1"
    ollama_model: str = "qwen2.5:7b-instruct"
    local_image_model: str = "ByteDance/SDXL-Lightning-4step"
    local_whisper_model: str = "small"

    # ── Image generation providers (optional — fallback chain) ────────────────
    gemini_api_key: Optional[str] = None
    huggingface_api_key: Optional[str] = None
    pollinations_api_key: Optional[str] = None

    # ── FFmpeg tuning (laptop hosting — 4 threads recommended for modern CPUs) ──
    ffmpeg_threads: str = "4"

    @field_validator("frontend_url", "backend_public_url", "voiceforge_url", mode="before")
    @classmethod
    def strip_trailing_slash(cls, v: object) -> object:
        if isinstance(v, str):
            return v.rstrip("/")
        return v

    @property
    def effective_jwt_secret(self) -> str:
        """Resolve JWT secret — JWT_SECRET_KEY takes precedence over JWT_SECRET."""
        return self.jwt_secret_key or self.jwt_secret or ""

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def cors_origins(self) -> list[str]:
        """Build CORS allowlist from configuration only."""
        origins: list[str] = []
        if self.frontend_url:
            origins.append(self.frontend_url)
        if not self.is_production:
            origins.extend(["http://localhost:3000", "http://127.0.0.1:3000"])
        return origins

    @property
    def cors_origin_regex(self) -> str | None:
        """Allow Vercel preview deployments in non-production only."""
        if self.is_production:
            return None
        return r"https://.*\.vercel\.app"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # Export HF_TOKEN to system environment so huggingface_hub uses it for authenticated downloads
    if s.huggingface_api_key:
        import os
        os.environ["HF_TOKEN"] = s.huggingface_api_key
    return s


settings = get_settings()
