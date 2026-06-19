"""
utils/file_handler.py — I/O helpers for managing job output directories.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

import aiofiles

from config import settings

logger = logging.getLogger("storyforge.file_handler")

OUTPUT_DIR = Path(settings.output_dir)


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------
def get_job_dir(job_id: str) -> Path:
    """Return (and create) the root output directory for a given job."""
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def get_images_dir(job_id: str) -> Path:
    d = get_job_dir(job_id) / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_audio_dir(job_id: str) -> Path:
    d = get_job_dir(job_id) / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_subtitles_dir(job_id: str) -> Path:
    d = get_job_dir(job_id) / "subtitles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_final_dir(job_id: str) -> Path:
    d = get_job_dir(job_id) / "final"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# File-naming convention
# ---------------------------------------------------------------------------
def scene_image_path(job_id: str, scene_number: int) -> Path:
    """e.g. output/{job_id}/images/scene_001.png"""
    return get_images_dir(job_id) / f"scene_{scene_number:03d}.png"


def scene_audio_path(job_id: str, scene_number: int) -> Path:
    """e.g. output/{job_id}/audio/scene_001.mp3"""
    return get_audio_dir(job_id) / f"scene_{scene_number:03d}.mp3"


def scene_subtitle_path(job_id: str, scene_number: int) -> Path:
    """e.g. output/{job_id}/subtitles/scene_001.srt"""
    return get_subtitles_dir(job_id) / f"scene_{scene_number:03d}.srt"


def master_srt_path(job_id: str) -> Path:
    """e.g. output/{job_id}/subtitles/episode.srt — merged master subtitle file."""
    return get_subtitles_dir(job_id) / "episode.srt"


def final_video_path(job_id: str) -> Path:
    return get_final_dir(job_id) / "episode.mp4"


def final_thumbnail_path(job_id: str) -> Path:
    return get_final_dir(job_id) / "thumbnail.png"


def final_title_path(job_id: str) -> Path:
    return get_final_dir(job_id) / "title.txt"


def final_description_path(job_id: str) -> Path:
    return get_final_dir(job_id) / "description.txt"


def final_hashtags_path(job_id: str) -> Path:
    return get_final_dir(job_id) / "hashtags.txt"


def final_character_bible_path(job_id: str) -> Path:
    return get_final_dir(job_id) / "character_bible.md"


# ---------------------------------------------------------------------------
# Async I/O
# ---------------------------------------------------------------------------
async def write_bytes(path: Path, data: bytes) -> None:
    """Async write raw bytes to *path* (parent dirs must exist)."""
    async with aiofiles.open(path, "wb") as f:
        await f.write(data)
    logger.debug("Wrote %d bytes → %s", len(data), path)


async def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Async write text to *path*."""
    async with aiofiles.open(path, "w", encoding=encoding) as f:
        await f.write(text)
    logger.debug("Wrote %d chars → %s", len(text), path)


async def read_text(path: Path, encoding: str = "utf-8") -> str:
    """Async read text from *path*."""
    async with aiofiles.open(path, "r", encoding=encoding) as f:
        return await f.read()


# ---------------------------------------------------------------------------
# URL helper — public URLs use BACKEND_PUBLIC_URL when configured
# ---------------------------------------------------------------------------
def output_url(job_id: str, *relative_parts: str) -> str:
    """
    Convert a filesystem path inside the output directory to a public URL.

    When BACKEND_PUBLIC_URL is set (Cloudflare Tunnel / production), returns
    an absolute HTTPS URL.  Otherwise returns a relative path served by the
    /output static mount (local development).

    Example (production):
        output_url("abc123", "final", "episode.mp4")
        → "https://api.example.com/output/abc123/final/episode.mp4"

    Example (local):
        → "/output/abc123/final/episode.mp4"
    """
    parts = "/".join(relative_parts)
    path = f"/output/{job_id}/{parts}"
    base = settings.backend_public_url.rstrip("/")
    if base:
        return f"{base}{path}"
    return path


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def delete_job_dir(job_id: str) -> None:
    """Remove all files for a job — use with caution."""
    job_dir = OUTPUT_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
        logger.info("Deleted job directory: %s", job_dir)


# ---------------------------------------------------------------------------
# Supabase Storage & Cloudinary Upload Helpers
# ---------------------------------------------------------------------------
async def upload_to_supabase(file_path: Path, bucket: str, path_in_bucket: str) -> str | None:
    """Uploads a file to Supabase Storage and returns its public CDN URL."""
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    if not supabase_url or not supabase_key or not file_path.exists():
        return None

    import mimetypes
    import httpx

    # Normalise URL
    supabase_url = supabase_url.rstrip("/")
    url = f"{supabase_url}/storage/v1/object/{bucket}/{path_in_bucket}"

    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()

        mime_type, _ = mimetypes.guess_type(str(file_path))
        if not mime_type:
            mime_type = "application/octet-stream"

        headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": mime_type,
            "x-upsert": "true"
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, content=file_bytes, headers=headers, timeout=60.0)
            if response.status_code == 200:
                public_url = f"{supabase_url}/storage/v1/object/public/{bucket}/{path_in_bucket}"
                logger.info("Successfully uploaded %s to Supabase Storage: %s", file_path.name, public_url)
                return public_url
            else:
                logger.error("Supabase Storage upload failed for %s: %d - %s", file_path, response.status_code, response.text)
                return None
    except Exception as e:
        logger.error("Supabase Storage upload exception for %s: %s", file_path, e)
        return None


async def upload_asset(file_path: Path, resource_type: str = "auto") -> str | None:
    """
    Uploads a file to a cloud storage provider and returns its public CDN URL.
    Checks providers in this priority order:
    1. Supabase Storage (if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY/KEY are configured)
    2. Cloudinary (if CLOUDINARY_URL is configured)
    """
    if not file_path.exists():
        return None

    # 1. Try Supabase Storage first
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    if supabase_url and supabase_key:
        bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "storyforge")
        try:
            rel_path = file_path.relative_to(OUTPUT_DIR)
            path_parts = rel_path.parts
            if len(path_parts) >= 2:
                job_id = path_parts[0]
                path_in_bucket = f"jobs/{job_id}/" + "/".join(path_parts[1:])
            else:
                path_in_bucket = f"misc/{file_path.name}"
        except Exception:
            path_in_bucket = f"misc/{file_path.name}"

        url = await upload_to_supabase(file_path, bucket, path_in_bucket)
        if url:
            return url

    # 2. Try Cloudinary next
    if settings.cloudinary_url:
        def _sync_upload():
            try:
                import cloudinary
                import cloudinary.uploader

                os.environ.setdefault("CLOUDINARY_URL", settings.cloudinary_url or "")
                response = cloudinary.uploader.upload(
                    str(file_path),
                    resource_type=resource_type,
                    folder="storyforge",
                )
                url = response.get("secure_url")
                logger.info("Successfully uploaded %s to Cloudinary: %s", file_path.name, url)
                return url
            except Exception as e:
                logger.error("Cloudinary upload failed for %s: %s", file_path, e)
                return None

        loop = asyncio.get_running_loop()
        url = await loop.run_in_executor(None, _sync_upload)
        if url:
            return url

    return None

