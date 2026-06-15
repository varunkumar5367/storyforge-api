"""
config.py — Centralised environment configuration via pydantic-settings.
"""

from __future__ import annotations

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Image generation providers (all optional — fallback chain handles missing keys)
    gemini_api_key: Optional[str] = None
    huggingface_api_key: Optional[str] = None
    pollinations_api_key: Optional[str] = None


settings = Settings()
