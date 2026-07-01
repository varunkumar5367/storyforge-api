"""
routes/analyze.py
──────────────────
POST /api/analyze/upload
  • Accepts a .txt file upload
  • Creates a new job in the database
  • Spawns the full pipeline as a background task
  • Returns the job_id immediately

This is the primary entry-point for the StoryForge pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile, Form, Depends

from database import create_job, count_user_images_last_hour
from models.job import JobCreateResponse
from services.orchestrator import start_pipeline
from utils.auth_helper import get_current_user

logger = logging.getLogger("storyforge.routes.analyze")
router = APIRouter()

MAX_FILE_SIZE_BYTES = 500_000  # 500 KB — generous for a story .txt


@router.post(
    "/upload",
    response_model=JobCreateResponse,
    summary="Upload a story .txt file and start the video pipeline",
)
async def upload_story(
    file: UploadFile = File(..., description="Plain-text story file (.txt)"),
    voice: str = Form(default="en-US-JennyNeural", description="Voice ID to use for narration"),
    image_model: str = Form(default="ByteDance/SDXL-Lightning-4step", description="Image generation model ID"),
    current_user: dict = Depends(get_current_user),
) -> JobCreateResponse:
    """
    Upload a story .txt file to begin the automated video generation pipeline.

    The pipeline runs asynchronously. Poll GET /api/status/{job_id} for progress.
    When status == 'completed', call GET /api/download/{job_id} for download links.
    """
    # Validate file type
    if not file.filename or not file.filename.lower().endswith(".txt"):
        raise HTTPException(
            status_code=400,
            detail="Only plain-text (.txt) files are accepted.",
        )

    # Read and size-check the file
    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size is {MAX_FILE_SIZE_BYTES // 1000} KB.",
        )

    story_text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not story_text:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Limit check: 1500 words for non-admin
    word_count = len(story_text.split())
    if current_user.get("role") != "admin":
        if word_count > 1500:
            raise HTTPException(
                status_code=400,
                detail=f"Limit exceeded: Stories are capped at 1500 words for free tier users (your story has {word_count} words).",
            )
        
        # Limit check: hourly images generated limit
        used_images = await count_user_images_last_hour(current_user["id"])
        if used_images >= 20:
            raise HTTPException(
                status_code=429,
                detail=f"Limit reached: You have already generated {used_images} images in the last hour. Your cap of 20 images refreshes hourly.",
            )

    # Concurrency Cap check
    from database import get_server_status
    server_stats = await get_server_status()
    active_tasks = server_stats.get("active_tasks", 0)
    max_tasks = server_stats.get("max_concurrent_tasks", 1)
    if active_tasks >= max_tasks:
        raise HTTPException(
            status_code=429,
            detail=f"The server is at maximum concurrent capacity ({active_tasks}/{max_tasks} active tasks). Please try again after current tasks complete.",
        )

    # Create job record
    job_id = str(uuid.uuid4())
    await create_job(job_id, story_text, file.filename, voice, current_user["id"], image_model)
    logger.info("New job created: %s | user=%s | file=%s | chars=%d | voice=%s | model=%s", job_id, current_user["username"], file.filename, len(story_text), voice, image_model)

    # Launch pipeline as a named asyncio.Task — returns immediately
    start_pipeline(job_id, story_text)
    logger.info("Pipeline task launched for job %s.", job_id)

    return JobCreateResponse(
        job_id=job_id,
        status="pending",
        message="Story uploaded. Pipeline is running — poll /api/status/{job_id} for progress.",
    )
