"""
routes/download.py
───────────────────
Return download links for a completed job's output files.

GET /api/download/{job_id}  — returns URLs for all final assets
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse

from database import get_job
from utils.auth_helper import get_current_user
from models.job import JobOutputLinks
from utils.file_handler import (
    final_character_bible_path,
    final_description_path,
    final_hashtags_path,
    final_thumbnail_path,
    final_title_path,
    final_video_path,
    master_srt_path,
    output_url,
)

logger = logging.getLogger("storyforge.routes.download")
router = APIRouter()


@router.get(
    "/file/{job_id}/{file_type}",
    summary="Download video or thumbnail directly as an attachment",
)
async def download_file(job_id: str, file_type: str, current_user: dict = Depends(get_current_user)):
    """
    Directly streams the requested file with Content-Disposition attachment header to bypass CORS fetch restrictions.
    """
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    if current_user.get("role") != "admin" and job.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Access denied. You do not own this job.")

    if file_type == "video":
        path = final_video_path(job_id)
        filename = f"storyforge_{job_id[:8]}_episode.mp4"
        media_type = "video/mp4"
    elif file_type == "thumbnail":
        path = final_thumbnail_path(job_id)
        filename = f"storyforge_{job_id[:8]}_thumbnail.png"
        media_type = "image/png"
    else:
        raise HTTPException(status_code=400, detail="Invalid file type. Must be 'video' or 'thumbnail'.")

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found on disk.")

    return FileResponse(
        path=str(path),
        filename=filename,
        media_type=media_type
    )


@router.get(
    "/{job_id}",
    response_model=JobOutputLinks,
    summary="Get download links for a completed job's output files",
)
async def get_download_links(job_id: str, current_user: dict = Depends(get_current_user)) -> JobOutputLinks:
    """
    Returns static file URLs for all generated output assets.
    """
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    if current_user.get("role") != "admin" and job.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Access denied. You do not own this job.")

    if job["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job is not completed yet (status='{job['status']}', "
                f"progress={job['progress_percent']}%). "
                "Please wait for the pipeline to finish."
            ),
        )

    import json
    db_urls = {}
    if job.get("download_urls"):
        try:
            if isinstance(job["download_urls"], str):
                db_urls = json.loads(job["download_urls"])
            elif isinstance(job["download_urls"], dict):
                db_urls = job["download_urls"]
        except Exception as e:
            logger.error("Failed to parse download_urls from job %s: %s", job_id, e)

    def _get_url(key: str, path: Path, *rel_parts: str) -> str | None:
        if key in db_urls and db_urls[key]:
            return db_urls[key]
        return output_url(job_id, *rel_parts) if path.exists() else None

    return JobOutputLinks(
        job_id=job_id,
        episode_mp4=_get_url(
            "video", final_video_path(job_id), "final", "episode.mp4"
        ),
        thumbnail_png=_get_url(
            "thumbnail", final_thumbnail_path(job_id), "final", "thumbnail.png"
        ),
        character_bible_md=_get_url(
            "character_bible", final_character_bible_path(job_id), "final", "character_bible.md"
        ),
        title_txt=_get_url(
            "title", final_title_path(job_id), "final", "title.txt"
        ),
        description_txt=_get_url(
            "description", final_description_path(job_id), "final", "description.txt"
        ),
        hashtags_txt=_get_url(
            "hashtags", final_hashtags_path(job_id), "final", "hashtags.txt"
        ),
        subtitles_srt=_get_url(
            "subtitles", master_srt_path(job_id), "subtitles", "episode.srt"
        ),
        thumbnail_prompt_txt=_get_url(
            "thumbnail_prompt", final_title_path(job_id).with_name("thumbnail_prompt.txt"), "final", "thumbnail_prompt.txt"
        ),
    )

