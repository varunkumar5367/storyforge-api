"""
models/job.py — Pydantic models for Job request/response payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class JobCreateResponse(BaseModel):
    """Returned immediately after a story upload — before processing starts."""

    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    """Full status snapshot of a processing job."""

    job_id: str
    status: str = Field(
        description="One of: pending | analyzing | generating_images | "
                    "generating_voice | generating_subtitles | "
                    "composing_video | generating_metadata | "
                    "generating_thumbnail | completed | failed"
    )
    progress_percent: int = Field(ge=0, le=100)
    current_step: str | None = None
    story_filename: str | None = None
    created_at: str | None = None
    completed_at: str | None = None
    error_message: str | None = None
    scenes: list[dict[str, Any]] | None = None
    voice: str | None = Field(default="en-US-JennyNeural", description="Voice ID used for narration")
    logs: list[str] | None = Field(default=None, description="Array of real-time log statements")
    avg_scene_duration: float | None = Field(default=None, description="Average duration of a scene render in seconds")



class JobSummary(BaseModel):
    """Lightweight row used in list responses."""

    job_id: str
    status: str
    progress_percent: int
    story_filename: str | None
    created_at: str
    user_id: str | None = None
    username: str | None = None


class JobListResponse(BaseModel):
    jobs: list[JobSummary]
    total: int


class JobOutputLinks(BaseModel):
    """Download URLs returned after a job completes."""

    job_id: str
    episode_mp4: str | None = None
    thumbnail_png: str | None = None
    character_bible_md: str | None = None
    title_txt: str | None = None
    description_txt: str | None = None
    hashtags_txt: str | None = None
    subtitles_srt: str | None = None
    thumbnail_prompt_txt: str | None = None


class JobUpdatePayload(BaseModel):
    """Payload to update specific job properties."""

    story_filename: str | None = None

