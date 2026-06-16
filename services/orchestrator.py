"""
services/orchestrator.py
─────────────────────────
The master pipeline controller for StoryForge AI.

Responsibilities
────────────────
• Accept a job_id + story_text and run the full 7-step pipeline.
• After every step, write the updated state (progress %, status, scenes blob)
  to SQLite via database.update_job().
• On any FATAL step failure: mark the job as "failed" with the error message
  and stop — but KEEP all files already produced so partial output survives.
• On NON-FATAL step failures (subtitles, metadata, thumbnail): log a warning
  and continue — the video can still be completed.
• After the final step, compute download URLs for every output file that
  actually exists on disk and store them in the database as a JSON blob.
• Expose a single public function `start_pipeline()` that launches the
  pipeline as an asyncio background task and returns immediately —
  the HTTP response goes back to the client before any work starts.

Progress milestones
───────────────────
  queued              →   0 %
  analyzing           →  15 %     (after story_analyzer completes)
  per-scene block     →  15–65 %  (image + voice + subtitle per scene)
  composing_video     →  85 %     (after video_composer completes)
  generating_metadata →  95 %     (after metadata_generator completes)
  generating_thumbnail→  98 %     (after thumbnail_generator completes)
  completed           → 100 %

SQLite columns written
──────────────────────
  status            TEXT   — one of the step names above or "failed"/"completed"
  progress_percent  INT    — 0-100
  current_step      TEXT   — human-readable step label (same as status while running)
  scenes            TEXT   — JSON blob of [Scene dicts] — updated after each step
  character_memory  TEXT   — JSON blob set after step 1
  error_message     TEXT   — set on failure
  completed_at      TEXT   — ISO-8601 UTC timestamp set when done or failed
  download_urls     TEXT   — JSON blob of { video, thumbnail, srt, title, … }
                             set only on success at 100 %
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from database import update_job
from services.image_generator import generate_image_for_scene
from services.metadata_generator import generate_metadata
from services.story_analyzer import analyze_story
from services.subtitle_generator import (
    finalize_master_subtitles,
    generate_subtitle_for_scene,
)
from services.thumbnail_generator import generate_thumbnail
from services.video_composer import compose_video
from services.voice_generator import (
    DEFAULT_VOICE,
    TTS_INTER_CALL_DELAY_SECS,
    TTS_TIMEOUT_SECS,
    generate_voice_for_scene,
    warmup_voiceforge,
)
from utils.file_handler import (
    final_character_bible_path,
    final_description_path,
    final_hashtags_path,
    final_thumbnail_path,
    final_title_path,
    final_video_path,
    master_srt_path,
    output_url,
    write_text,
)

logger = logging.getLogger("storyforge.orchestrator")

# Global semaphore to restrict pipeline concurrency to 1 job at a time
concurrency_semaphore = asyncio.Semaphore(1)

# ─────────────────────────────────────────────────────────────────────────────
# Progress milestones
# ─────────────────────────────────────────────────────────────────────────────

# Each entry: (status_label, progress_percent_AFTER_completion, is_fatal_on_failure)
_STEPS: list[tuple[str, int, bool]] = [
    ("analyzing",             15, True),   # story must parse or there's nothing to do
    ("generating_images",     65, False),  # per-scene image failures are non-fatal
    ("generating_voice",      65, True),   # no audio = no video
    ("generating_subtitles",  65, False),  # subtitles optional — continue without
    ("composing_video",       85, True),   # video itself is the core deliverable
    ("generating_metadata",   95, False),  # nice-to-have; continue without
    ("generating_thumbnail",  98, False),  # nice-to-have; continue without
]

# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def start_pipeline(job_id: str, story_text: str) -> asyncio.Task:
    """
    Launch the full pipeline as a non-blocking asyncio background task.

    The task is attached to the running event loop so it outlives the HTTP
    request that created it.  The caller (FastAPI route) returns immediately
    while the pipeline runs asynchronously.

    Args:
        job_id:     Unique job identifier (already inserted into the DB).
        story_text: Raw story content from the uploaded .txt file.

    Returns:
        The asyncio.Task object (caller may ignore it — it is self-contained).
    """
    task = asyncio.create_task(
        _run_pipeline(job_id, story_text),
        name=f"pipeline-{job_id}",
    )
    # Attach a done-callback so uncaught exceptions from the task
    # are at least logged even if nothing is awaiting the task.
    task.add_done_callback(_pipeline_task_done_callback)
    logger.info("Pipeline task created | job=%s", job_id)
    return task


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline execution
# ─────────────────────────────────────────────────────────────────────────────


async def _run_pipeline(job_id: str, story_text: str) -> None:
    """
    Execute the full StoryForge pipeline for *job_id*, step by step.

    All exceptions are caught and written to the database — the job status
    is always left in a terminal state ("completed" or "failed") so clients
    that poll /api/status/{job_id} can always determine the outcome.
    """
    from utils.locks import get_job_lock
    lock = get_job_lock(job_id)
    async with concurrency_semaphore:
        async with lock:
            await _run_pipeline_impl(job_id, story_text)


async def _run_pipeline_impl(job_id: str, story_text: str) -> None:
    logger.info("═══ Pipeline START | job=%s ═══", job_id)
    elapsed_start = _now()

    # Running state threaded through the pipeline
    scenes: list[dict[str, Any]] = []
    character_memory: dict[str, Any] = {}
    metadata: dict[str, str] = {}    # title / description / hashtags

    try:
        # ── Transition to "queued" / "starting" ───────────────────────────────
        await _set_status(job_id, "analyzing", 0,
                          log="Starting pipeline …")

        # ══════════════════════════════════════════════════════════════════════
        # Step 1 — Story Analysis  (0 % → 15 %)
        # ══════════════════════════════════════════════════════════════════════
        await _set_status(job_id, "analyzing", 0,
                          log="Step 1/7 — Analysing story …")

        result = await analyze_story(story_text)

        if not result["success"]:
            await _fail(job_id, f"Story analysis failed: {result['error']}")
            return

        scenes = result["data"]["scenes"]
        character_memory = result["data"]["character_memory"]

        # Limit checks for non-admin users
        from database import get_user_by_id, count_user_images_last_hour, get_job
        job = await get_job(job_id)
        user_id = job.get("user_id") if job else None
        is_admin = False
        if user_id:
            user = await get_user_by_id(user_id)
            if user and user.get("role") == "admin":
                is_admin = True

        if not is_admin:
            if len(scenes) > 20:
                await _fail(
                    job_id,
                    f"Limit exceeded: Story has {len(scenes)} scenes. Non-admin users are capped at 20 scenes/images per story."
                )
                return
            
            if user_id:
                used_last_hour = await count_user_images_last_hour(user_id)
                if used_last_hour + len(scenes) > 20:
                    await _fail(
                        job_id,
                        f"Limit reached: Generating this story ({len(scenes)} scenes) would exceed your hourly cap of 20 images (you have used {used_last_hour} in the last hour)."
                    )
                    return

        # Generate and save the Character Bible markdown
        try:
            bible_md = _generate_character_bible_markdown(character_memory)
            await write_text(final_character_bible_path(job_id), bible_md)
            logger.info("Character Bible saved for job %s.", job_id)
        except Exception as e:
            logger.warning("Failed to save character bible: %s", e)

        await _progress(
            job_id,
            status="analyzing",
            progress=15,
            scenes=scenes,
            character_memory=character_memory,
            log=f"Step 1 ✓ — {len(scenes)} scenes extracted "
                f"| mood={result['data'].get('mood', '?')} "
                f"| locations={len(result['data'].get('locations', []))}",
        )

        # ══════════════════════════════════════════════════════════════════════
        # Steps 2–4 — Per-scene streaming (image → voice → subtitle)
        # Progress: 15 % → 65 % (50 % spread evenly across scenes)
        # ══════════════════════════════════════════════════════════════════════
        total_scenes = len(scenes)
        scene_block_start = 15
        scene_block_end = 65
        scene_block_size = scene_block_end - scene_block_start

        from database import get_job
        job = await get_job(job_id)
        voice = job.get("voice", DEFAULT_VOICE) if job else DEFAULT_VOICE

        wakeup_ok, wakeup_error = await warmup_voiceforge()
        if not wakeup_ok:
            await _fail(job_id, f"VoiceForge unreachable: {wakeup_error}")
            return

        subtitle_results: list = []

        await _set_status(
            job_id,
            "generating_images",
            scene_block_start,
            log=f"Steps 2–4/7 — Per-scene image + voice + subtitle ({total_scenes} scenes) …",
        )

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(TTS_TIMEOUT_SECS),
            follow_redirects=True,
        ) as voice_client:
            for idx, raw_scene in enumerate(scenes):
                scene_num = idx + 1
                scene_label = f"{scene_num:03d}"

                # ── Pause Check ──
                from database import get_job
                job_check = await get_job(job_id)
                if job_check and job_check.get("status") == "paused":
                    logger.info("[job=%s] Pipeline PAUSED by user. Entering sleep loop.", job_id)
                    while True:
                        await asyncio.sleep(2)
                        job_check = await get_job(job_id)
                        if not job_check:
                            return  # Job was deleted
                        if job_check.get("status") == "failed":
                            logger.info("[job=%s] Pipeline CANCELLED during pause.", job_id)
                            return
                        if job_check.get("status") != "paused":
                            logger.info("[job=%s] Pipeline RESUMED.", job_id)
                            break

                def _scene_progress(step: int) -> int:
                    """step 1=image, 2=voice, 3=subtitle (all steps for this scene)."""
                    fraction = (idx + step / 3) / total_scenes
                    return scene_block_start + int(fraction * scene_block_size)

                # ── 2a. Image for this scene ──────────────────────────────────
                await _set_status(
                    job_id,
                    "generating_images",
                    _scene_progress(0),
                    log=f"[job={job_id} | scene={scene_label} | {scene_num}/{total_scenes}] "
                        f"Generating image …",
                )

                scenes[idx] = await generate_image_for_scene(
                    job_id, scenes[idx], character_memory
                )
                image_ok = scenes[idx].get("image_path") is not None

                await _progress(
                    job_id,
                    status="generating_images",
                    progress=_scene_progress(1),
                    scenes=scenes,
                )

                # ── 3a. Voice for this scene ──────────────────────────────────
                if idx > 0:
                    await asyncio.sleep(TTS_INTER_CALL_DELAY_SECS)

                await _set_status(
                    job_id,
                    "generating_voice",
                    _scene_progress(1),
                    log=f"[job={job_id} | scene={scene_label} | {scene_num}/{total_scenes}] "
                        f"Generating voice …",
                )

                scenes[idx] = await generate_voice_for_scene(
                    voice_client, job_id, scenes[idx], voice=voice
                )
                audio_ok = scenes[idx].get("audio_path") is not None

                await _progress(
                    job_id,
                    status="generating_voice",
                    progress=_scene_progress(2),
                    scenes=scenes,
                )

                # ── 4a. Subtitles for this scene ──────────────────────────────
                await _set_status(
                    job_id,
                    "generating_subtitles",
                    _scene_progress(2),
                    log=f"[job={job_id} | scene={scene_label} | {scene_num}/{total_scenes}] "
                        f"Transcribing subtitles …",
                )

                scenes[idx], sub_result = await generate_subtitle_for_scene(
                    job_id, scenes[idx]
                )
                if sub_result is not None:
                    subtitle_results.append(sub_result)
                subs_ok = scenes[idx].get("subtitle_path") is not None

                image_tag = "✓" if image_ok else "✗"
                audio_tag = "✓" if audio_ok else "✗"
                subs_tag = "✓" if subs_ok else "✗"
                msg = f"Scene {scene_label} complete: image {image_tag} | audio {audio_tag} | subtitles {subs_tag}"
                logger.info(
                    "[job=%s | scene=%s | %d/%d] %s",
                    job_id,
                    scene_label,
                    scene_num,
                    total_scenes,
                    f"image {image_tag} | audio {audio_tag} | subtitles {subs_tag}"
                )
                await _append_log(job_id, msg)

                await _progress(
                    job_id,
                    status="generating_subtitles",
                    progress=_scene_progress(3),
                    scenes=scenes,
                )

        # ── Build master episode.srt after all scenes ─────────────────────────
        master_srt = await finalize_master_subtitles(
            job_id, scenes, subtitle_results
        )
        failed_imgs = [
            s["scene_number"]
            for s in scenes
            if not s.get("image_path")
        ]
        failed_subs = [
            s["scene_number"]
            for s in scenes
            if not s.get("subtitle_path")
        ]

        await _progress(
            job_id,
            status="generating_subtitles",
            progress=scene_block_end,
            scenes=scenes,
            log=(
                f"Steps 2–4 ✓ — per-scene pipeline complete "
                f"| master_srt={master_srt}"
                + (f" | {len(failed_imgs)} image(s) failed" if failed_imgs else "")
                + (f" | {len(failed_subs)} subtitle(s) failed" if failed_subs else "")
            ),
        )

        # ══════════════════════════════════════════════════════════════════════
        # Step 5 — Video Composition  (65 % → 85 %)
        # ══════════════════════════════════════════════════════════════════════
        await _set_status(job_id, "composing_video", 65,
                          log="Step 5/7 — Composing episode.mp4 with FFmpeg …")

        result = await compose_video(job_id, scenes)

        if not result["success"]:
            ffmpeg_detail = result.get("ffmpeg_stderr", "")[-800:]
            err_msg = f"Video composition failed: {result['error']}"
            if ffmpeg_detail:
                err_msg += f"\n\nFFmpeg stderr:\n{ffmpeg_detail}"
            await _fail(job_id, err_msg)
            return

        video_duration = result["data"].get("duration_secs", 0.0)
        scene_count = result["data"].get("scene_count", len(scenes))

        await _progress(
            job_id,
            status="composing_video",
            progress=85,
            scenes=scenes,
            log=f"Step 5 ✓ — episode.mp4 created "
                f"| duration={video_duration:.1f}s | scenes={scene_count}",
        )

        # ══════════════════════════════════════════════════════════════════════
        # Step 6 — Metadata Generation  (85 % → 95 %)  [NON-FATAL]
        # ══════════════════════════════════════════════════════════════════════
        await _set_status(job_id, "generating_metadata", 85,
                          log="Step 6/7 — Generating YouTube metadata …")

        result = await generate_metadata(job_id, story_text, scenes)

        if result["success"]:
            metadata = result["data"]
            log_msg = (
                f"Step 6 ✓ — title='{metadata.get('title', '')[:60]}'"
            )
        else:
            metadata = {}
            log_msg = (
                f"Step 6 ⚠ — Metadata generation failed (non-fatal): "
                f"{result.get('error', 'unknown')}"
            )
            logger.warning(log_msg)

        await _progress(
            job_id,
            status="generating_metadata",
            progress=95,
            scenes=scenes,
            log=log_msg,
        )

        # ══════════════════════════════════════════════════════════════════════
        # Step 7 — Thumbnail Generation  (95 % → 98 %)  [NON-FATAL]
        # ══════════════════════════════════════════════════════════════════════
        await _set_status(job_id, "generating_thumbnail", 95,
                          log="Step 7/7 — Creating YouTube thumbnail …")

        result = await generate_thumbnail(
            job_id,
            scenes,
            title=metadata.get("title", ""),
        )

        if result["success"]:
            log_msg = "Step 7 ✓ — thumbnail.png created"
        else:
            log_msg = (
                f"Step 7 ⚠ — Thumbnail generation failed (non-fatal): "
                f"{result.get('error', 'unknown')}"
            )
            logger.warning(log_msg)

        await _progress(
            job_id,
            status="generating_thumbnail",
            progress=98,
            scenes=scenes,
            log=log_msg,
        )

        # ══════════════════════════════════════════════════════════════════════
        # Completion — compute download URLs and finalise DB record
        # ══════════════════════════════════════════════════════════════════════
        download_urls = await _build_download_urls(job_id)
        elapsed_secs = (
            datetime.now(timezone.utc) - elapsed_start
        ).total_seconds()

        await update_job(
            job_id,
            status="completed",
            progress_percent=100,
            current_step="done",
            scenes=scenes,
            completed_at=_utc_now(),
            download_urls=download_urls,          # new column — see note below
        )

        logger.info(
            "═══ Pipeline COMPLETE | job=%s | %.0fs elapsed ═══\n"
            "  video   : %s\n"
            "  thumbnail: %s\n"
            "  srt      : %s",
            job_id,
            elapsed_secs,
            download_urls.get("video", "—"),
            download_urls.get("thumbnail", "—"),
            download_urls.get("subtitles", "—"),
        )

    except asyncio.CancelledError:
        # Task was cancelled externally (server shutdown, etc.)
        logger.warning("Pipeline task cancelled | job=%s", job_id)
        await _fail(job_id, "Pipeline task was cancelled (server shutdown?).")
        raise   # re-raise so asyncio handles it correctly

    except Exception as exc:
        # Catch-all for any unhandled exception anywhere in the pipeline
        tb = traceback.format_exc()
        logger.exception("Unhandled exception in pipeline | job=%s", job_id)
        await _fail(
            job_id,
            f"Unexpected error: {exc}\n\nTraceback:\n{tb[-2000:]}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Download URL builder
# ─────────────────────────────────────────────────────────────────────────────


async def _build_download_urls(job_id: str) -> dict[str, Any]:
    """
    Build a dict of download URLs for every output file.
    If Cloudinary is configured (CLOUDINARY_URL in environment), uploads assets to the CDN.
    Otherwise, falls back to the local static mount URLs (/output/{job_id}/...).
    """
    from utils.file_handler import (
        final_video_path,
        final_thumbnail_path,
        final_character_bible_path,
        master_srt_path,
        final_title_path,
        final_description_path,
        final_hashtags_path,
        get_images_dir,
        get_audio_dir,
        get_subtitles_dir,
        output_url,
        upload_asset,
    )

    urls: dict[str, Any] = {}
    upload_tasks = []
    keys_and_paths = []

    def add_upload_item(key, path, fallback_url, resource_type):
        if path.exists():
            keys_and_paths.append((key, path, fallback_url, resource_type))
            upload_tasks.append(upload_asset(path, resource_type))

    # Add final deliverables
    add_upload_item("video", final_video_path(job_id), output_url(job_id, "final", "episode.mp4"), "video")
    add_upload_item("thumbnail", final_thumbnail_path(job_id), output_url(job_id, "final", "thumbnail.png"), "image")
    add_upload_item("character_bible", final_character_bible_path(job_id), output_url(job_id, "final", "character_bible.md"), "raw")
    add_upload_item("subtitles", master_srt_path(job_id), output_url(job_id, "subtitles", "episode.srt"), "raw")
    add_upload_item("title", final_title_path(job_id), output_url(job_id, "final", "title.txt"), "raw")
    add_upload_item("description", final_description_path(job_id), output_url(job_id, "final", "description.txt"), "raw")
    add_upload_item("hashtags", final_hashtags_path(job_id), output_url(job_id, "final", "hashtags.txt"), "raw")

    # Run the uploads in parallel
    results = await asyncio.gather(*upload_tasks, return_exceptions=True)

    for (key, path, fallback_url, _), uploaded_url in zip(keys_and_paths, results):
        if isinstance(uploaded_url, str) and uploaded_url:
            urls[key] = uploaded_url
        else:
            urls[key] = fallback_url

    # Now handle per-scene assets (images, audio, subtitles)
    images_dir = get_images_dir(job_id)
    audio_dir = get_audio_dir(job_id)
    subtitles_dir = get_subtitles_dir(job_id)

    scene_upload_tasks = []
    scene_keys_and_paths = []

    for img_path in sorted(images_dir.glob("scene_*.png")):
        stem = img_path.stem
        scene_n = stem.replace("scene_", "")

        # Image
        scene_keys_and_paths.append((scene_n, "image", img_path, output_url(job_id, "images", img_path.name), "image"))
        scene_upload_tasks.append(upload_asset(img_path, "image"))

        # Audio
        audio_path = audio_dir / f"{stem}.mp3"
        if audio_path.exists():
            scene_keys_and_paths.append((scene_n, "audio", audio_path, output_url(job_id, "audio", audio_path.name), "video"))
            scene_upload_tasks.append(upload_asset(audio_path, "video"))

        # Subtitle
        srt_path = subtitles_dir / f"{stem}.srt"
        if srt_path.exists():
            scene_keys_and_paths.append((scene_n, "subtitle", srt_path, output_url(job_id, "subtitles", srt_path.name), "raw"))
            scene_upload_tasks.append(upload_asset(srt_path, "raw"))

    scene_results = await asyncio.gather(*scene_upload_tasks, return_exceptions=True)

    scene_entries: dict[str, dict[str, str]] = {}
    for (scene_n, field, _, fallback_url, _), uploaded_url in zip(scene_keys_and_paths, scene_results):
        if scene_n not in scene_entries:
            scene_entries[scene_n] = {"scene": scene_n}
        
        if isinstance(uploaded_url, str) and uploaded_url:
            scene_entries[scene_n][field] = uploaded_url
        else:
            scene_entries[scene_n][field] = fallback_url

    if scene_entries:
        urls["scenes"] = sorted(scene_entries.values(), key=lambda x: x["scene"])

    return urls



# ─────────────────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _append_log(job_id: str, message: str) -> None:
    if not message:
        return
    from database import get_job
    job = await get_job(job_id)
    if not job:
        return
    logs_raw = job.get("logs")
    logs = []
    if logs_raw:
        try:
            logs = json.loads(logs_raw) if isinstance(logs_raw, str) else logs_raw
            if not isinstance(logs, list):
                logs = []
        except Exception:
            logs = []
    timestamped = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {message}"
    logs.append(timestamped)
    await update_job(job_id, logs=logs)


async def _set_status(
    job_id: str,
    status: str,
    progress: int,
    *,
    log: str = "",
) -> None:
    """
    Write status + progress to the DB and log a message.
    Called BEFORE starting each step to show "in-progress" state to the client.
    """
    if log:
        logger.info("[job=%s] %s", job_id, log)
        await _append_log(job_id, log)
    await update_job(
        job_id,
        status=status,
        current_step=status,
        progress_percent=progress,
    )


async def _progress(
    job_id: str,
    *,
    status: str,
    progress: int,
    scenes: list[dict] | None = None,
    character_memory: dict | None = None,
    log: str = "",
) -> None:
    """
    Write the post-step progress snapshot to the DB.
    Called AFTER a step completes successfully with the updated scenes blob.
    """
    if log:
        logger.info("[job=%s | %d%%] %s", job_id, progress, log)
        await _append_log(job_id, log)

    fields: dict[str, Any] = {
        "status":           status,
        "current_step":     status,
        "progress_percent": progress,
    }
    if scenes is not None:
        fields["scenes"] = scenes
    if character_memory is not None:
        fields["character_memory"] = character_memory

    await update_job(job_id, **fields)


async def _fail(job_id: str, error_message: str) -> None:
    """
    Mark the job as failed.  Completed files are preserved on disk.
    """
    # Truncate to avoid blowing up the SQLite TEXT column with giant FFmpeg logs
    truncated = error_message[:4000]
    if len(error_message) > 4000:
        truncated += "\n… (truncated)"

    logger.error("[job=%s] FAILED: %s", job_id, truncated[:300])
    await _append_log(job_id, f"FAILED: {truncated[:500]}")
    await update_job(
        job_id,
        status="failed",
        current_step="failed",
        error_message=truncated,
        completed_at=_utc_now(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Task lifecycle
# ─────────────────────────────────────────────────────────────────────────────


def _pipeline_task_done_callback(task: asyncio.Task) -> None:
    """
    Called by asyncio when the pipeline task finishes (success, failure, or cancel).
    Logs any exception that escaped _run_pipeline's own try/except — this
    is a safety net, not the primary error handler.
    """
    if task.cancelled():
        logger.warning("Pipeline task '%s' was cancelled.", task.get_name())
        return

    exc = task.exception()
    if exc:
        logger.error(
            "Pipeline task '%s' raised an unhandled exception: %s",
            task.get_name(),
            exc,
            exc_info=exc,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Time utilities
# ─────────────────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _now() -> datetime:
    """Return the current UTC datetime object (for elapsed-time tracking)."""
    return datetime.now(timezone.utc)


def _generate_character_bible_markdown(character_memory_dict: dict[str, Any]) -> str:
    """Generate a clean markdown document for the Character Bible."""
    chars = character_memory_dict.get("characters", [])
    if not chars:
        return "# Character Bible\n\nNo characters extracted from this story."

    lines = ["# Character Bible", ""]
    for char in chars:
        lines.append(f"## {char.get('name', 'Unnamed Character')} ({char.get('role', 'unknown').capitalize()})")
        lines.append(f"- **Gender**: {char.get('gender', 'unknown')}")
        lines.append(f"- **Age**: {char.get('age', 'unknown')}")
        lines.append(f"- **Hair**: {char.get('hair', 'unknown')}")
        lines.append(f"- **Eyes**: {char.get('eyes', 'unknown')}")
        lines.append(f"- **Body Type**: {char.get('body_type', 'unknown')}")
        lines.append(f"- **Signature Clothing**: {char.get('clothing', 'unknown')}")
        lines.append(f"- **Facial Features**: {char.get('facial_features', 'unknown')}")
        if char.get('personality'):
            lines.append(f"- **Personality**: {char.get('personality')}")
        lines.append("")

    return "\n".join(lines)


async def cleanup_old_jobs():
    """Query and purge DB jobs and output folders older than 12 hours."""
    from database import DATABASE_URL, delete_job, DatabaseConnection
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    import shutil
    
    threshold = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    logger.info("Running background cleaner loop. Purging jobs older than %s", threshold)
    
    try:
        async with DatabaseConnection(DATABASE_URL) as db:
            async with db.execute("SELECT id FROM jobs WHERE created_at < ?", (threshold,)) as cur:
                rows = await cur.fetchall()
                job_ids = [row["id"] for row in rows]
                
        for job_id in job_ids:
            try:
                logger.info("Cleaning up job %s (older than 12 hours)", job_id)
                # Delete directory
                job_dir = Path("output") / job_id
                if job_dir.exists():
                    shutil.rmtree(job_dir, ignore_errors=True)
                # Delete from database
                await delete_job(job_id)
            except Exception as e:
                logger.error("Error cleaning up job %s: %s", job_id, e)
    except Exception as e:
        logger.error("Error fetching old jobs for cleanup: %s", e)


async def cleanup_old_jobs_loop():
    """Clean up loop running every hour."""
    while True:
        try:
            await cleanup_old_jobs()
        except Exception as e:
            logger.error("Error in cleanup_old_jobs_loop: %s", e)
        await asyncio.sleep(3600)
