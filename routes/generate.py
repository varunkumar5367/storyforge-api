"""
routes/generate.py
───────────────────
Provides endpoints to manually trigger individual pipeline steps.
Useful for debugging and re-running a specific stage without restarting the job.

Endpoints:
  POST /api/generate/images/{job_id}     — re-run image generation
  POST /api/generate/voices/{job_id}     — re-run voice generation
  POST /api/generate/subtitles/{job_id}  — re-run subtitle generation
  POST /api/generate/video/{job_id}      — re-run video composition
  POST /api/generate/metadata/{job_id}   — re-run metadata generation
  POST /api/generate/thumbnail/{job_id}  — re-run thumbnail generation
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends
from pydantic import BaseModel

from database import get_job, update_job
from utils.auth_helper import get_current_user
from services.image_generator import generate_images
from services.voice_generator import generate_voices
from services.subtitle_generator import generate_subtitles
from services.video_composer import compose_video
from services.metadata_generator import generate_metadata
from services.thumbnail_generator import generate_thumbnail

logger = logging.getLogger("storyforge.routes.generate")
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _load_job_or_404(job_id: str, user: dict) -> dict:
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if user.get("role") != "admin" and job.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Access denied. You do not own this job.")
    return job


def _ensure_job_idle(job: dict):
    status = job.get("status")
    if status not in ("completed", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Job is currently running (status: '{status}'). Please wait for it to finish."
        )


def _parse_scenes(job: dict) -> list[dict]:
    scenes_raw = job.get("scenes")
    if not scenes_raw:
        raise HTTPException(
            status_code=409,
            detail="Job has no scenes yet. Run the story analyzer first.",
        )
    return json.loads(scenes_raw) if isinstance(scenes_raw, str) else scenes_raw


def _parse_character_memory(job: dict) -> dict:
    cm_raw = job.get("character_memory") or "{}"
    return json.loads(cm_raw) if isinstance(cm_raw, str) else cm_raw


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/images/{job_id}", summary="Re-run image generation for a job")
async def regenerate_images(job_id: str, background_tasks: BackgroundTasks, current_user: dict = Depends(get_current_user)):
    from utils.locks import get_job_lock
    lock = get_job_lock(job_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Job is currently processing another task. Please wait.")

    job = await _load_job_or_404(job_id, current_user)
    _ensure_job_idle(job)
    scenes = _parse_scenes(job)
    character_memory = _parse_character_memory(job)

    async def _run():
        async with lock:
            result = await generate_images(job_id, scenes, character_memory)
            if result["success"]:
                await update_job(job_id, scenes=result["data"]["scenes"])

    background_tasks.add_task(_run)
    return {"job_id": job_id, "message": "Image generation re-triggered."}


@router.post("/voices/{job_id}", summary="Re-run voice generation for a job")
async def regenerate_voices(job_id: str, background_tasks: BackgroundTasks, current_user: dict = Depends(get_current_user)):
    from utils.locks import get_job_lock
    lock = get_job_lock(job_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Job is currently processing another task. Please wait.")

    job = await _load_job_or_404(job_id, current_user)
    _ensure_job_idle(job)
    scenes = _parse_scenes(job)
    voice = job.get("voice", "en-US-JennyNeural")

    async def _run():
        async with lock:
            result = await generate_voices(job_id, scenes, voice=voice)
            if result["success"]:
                await update_job(job_id, scenes=result["data"]["scenes"])

    background_tasks.add_task(_run)
    return {"job_id": job_id, "message": "Voice generation re-triggered."}


@router.post("/subtitles/{job_id}", summary="Re-run subtitle generation for a job")
async def regenerate_subtitles(job_id: str, background_tasks: BackgroundTasks, current_user: dict = Depends(get_current_user)):
    from utils.locks import get_job_lock
    lock = get_job_lock(job_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Job is currently processing another task. Please wait.")

    job = await _load_job_or_404(job_id, current_user)
    _ensure_job_idle(job)
    scenes = _parse_scenes(job)

    async def _run():
        async with lock:
            result = await generate_subtitles(job_id, scenes)
            if result["success"]:
                await update_job(job_id, scenes=result["data"]["scenes"])

    background_tasks.add_task(_run)
    return {"job_id": job_id, "message": "Subtitle generation re-triggered."}


@router.post("/video/{job_id}", summary="Re-run video composition for a job")
async def regenerate_video(job_id: str, background_tasks: BackgroundTasks, current_user: dict = Depends(get_current_user)):
    from utils.locks import get_job_lock
    lock = get_job_lock(job_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Job is currently processing another task. Please wait.")

    job = await _load_job_or_404(job_id, current_user)
    _ensure_job_idle(job)
    scenes = _parse_scenes(job)

    async def _run():
        async with lock:
            await compose_video(job_id, scenes)

    background_tasks.add_task(_run)
    return {"job_id": job_id, "message": "Video composition re-triggered."}


@router.post("/metadata/{job_id}", summary="Re-run metadata generation for a job")
async def regenerate_metadata(job_id: str, background_tasks: BackgroundTasks, current_user: dict = Depends(get_current_user)):
    from utils.locks import get_job_lock
    lock = get_job_lock(job_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Job is currently processing another task. Please wait.")

    job = await _load_job_or_404(job_id, current_user)
    _ensure_job_idle(job)
    scenes = _parse_scenes(job)
    story_text = job.get("story_text", "")

    async def _run():
        async with lock:
            await generate_metadata(job_id, story_text, scenes)

    background_tasks.add_task(_run)
    return {"job_id": job_id, "message": "Metadata generation re-triggered."}


class ThumbnailUpdatePayload(BaseModel):
    title: str | None = None
    scene_number: int | None = None
    prompt: str | None = None


@router.post("/thumbnail/{job_id}", summary="Re-run thumbnail generation for a job")
async def regenerate_thumbnail(
    job_id: str,
    payload: ThumbnailUpdatePayload,
    current_user: dict = Depends(get_current_user),
):
    from utils.locks import get_job_lock
    lock = get_job_lock(job_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Job is currently processing another task. Please wait.")

    job = await _load_job_or_404(job_id, current_user)
    _ensure_job_idle(job)
    scenes = _parse_scenes(job)

    from services.orchestrator import _append_log

    async with lock:
        title_text = payload.title
        if title_text is None:
            from utils.file_handler import final_title_path
            try:
                tp = final_title_path(job_id)
                if tp.exists():
                    title_text = tp.read_text(encoding="utf-8").strip()
            except Exception:
                title_text = ""

        custom_image_path = None
        if payload.prompt is not None:
            from utils.file_handler import get_final_dir
            prompt_file = get_final_dir(job_id) / "thumbnail_prompt.txt"
            bg_image_file = get_final_dir(job_id) / "custom_thumb_bg.png"
            if payload.prompt.strip():
                existing_prompt = ""
                if prompt_file.exists():
                    try:
                        existing_prompt = prompt_file.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass
                
                if payload.prompt.strip() != existing_prompt or not bg_image_file.exists():
                    try:
                        prompt_file.write_text(payload.prompt.strip(), encoding="utf-8")
                    except Exception:
                        pass
                    
                    from services.image_generator import _download_with_retry
                    import httpx
                    
                    await _append_log(job_id, f"🎨 Generating custom thumbnail background with prompt: {payload.prompt}")
                    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0), follow_redirects=True) as client:
                        image_bytes, provider, error = await _download_with_retry(
                            client, prompt=payload.prompt.strip(), seed=12345, scene_number=999
                        )
                    if error:
                        await _append_log(job_id, f"❌ Failed to generate custom background image: {error}")
                    else:
                        custom_image_path = bg_image_file
                        custom_image_path.write_bytes(image_bytes)
                        await _append_log(job_id, f"✅ Custom background image generated successfully via {provider}.")
                else:
                    if bg_image_file.exists():
                        custom_image_path = bg_image_file
            else:
                # Clear custom background
                if prompt_file.exists():
                    try:
                        prompt_file.unlink()
                    except Exception:
                        pass
                if bg_image_file.exists():
                    try:
                        bg_image_file.unlink()
                    except Exception:
                        pass
        else:
            from utils.file_handler import get_final_dir
            bg_image_file = get_final_dir(job_id) / "custom_thumb_bg.png"
            if bg_image_file.exists():
                custom_image_path = bg_image_file
        
        await generate_thumbnail(
            job_id,
            scenes,
            title=title_text or "",
            scene_number=payload.scene_number,
            custom_image_path=custom_image_path
        )

    return {"job_id": job_id, "message": "Thumbnail generation completed."}


class SceneUpdatePayload(BaseModel):
    image_prompt: str | None = None
    text: str | None = None
    location: str | None = None
    mood: str | None = None
    regenerate_image: bool = False
    regenerate_voice: bool = False


async def run_single_scene_regeneration(
    job_id: str,
    scene_number: int,
    payload: SceneUpdatePayload,
):
    from utils.locks import get_job_lock
    lock = get_job_lock(job_id)
    async with lock:
        from database import get_job, update_job
        import json
        from pathlib import Path
        
        job = await get_job(job_id)
        if not job:
            return
        
        scenes = json.loads(job["scenes"]) if isinstance(job["scenes"], str) else job["scenes"]
        character_memory = json.loads(job["character_memory"]) if isinstance(job["character_memory"], str) else job["character_memory"]
        voice = job.get("voice", "en-US-JennyNeural")
        
        from services.orchestrator import _append_log, _utc_now, _build_download_urls
        await _append_log(job_id, f"🔧 Initiating single-scene update for Scene {scene_number}...")
        
        target_idx = None
        for idx, scene in enumerate(scenes):
            if scene["scene_number"] == scene_number:
                target_idx = idx
                break
                
        if target_idx is None:
            await _append_log(job_id, f"❌ Scene {scene_number} not found in job.")
            return
            
        scene = scenes[target_idx]
        
        if payload.image_prompt is not None:
            scene["image_prompt"] = payload.image_prompt
        if payload.text is not None:
            scene["text"] = payload.text
            scene["narration"] = payload.text
        if payload.location is not None:
            scene["location"] = payload.location
        if payload.mood is not None:
            scene["mood"] = payload.mood
            
        await update_job(job_id, scenes=scenes)
        
        if payload.regenerate_image:
            await _append_log(job_id, f"🎨 Regenerating image for Scene {scene_number}...")
            from services.image_generator import generate_image_for_scene
            scenes[target_idx] = await generate_image_for_scene(job_id, scene, character_memory)
            await update_job(job_id, scenes=scenes)
            await _append_log(job_id, f"🎨 Image for Scene {scene_number} regenerated successfully.")
            
        if payload.regenerate_voice:
            await _append_log(job_id, f"🎙️ Regenerating narration voice for Scene {scene_number}...")
            import httpx
            from services.voice_generator import generate_voice_for_scene, warmup_voiceforge, TTS_TIMEOUT_SECS
            
            wakeup_ok, wakeup_error = await warmup_voiceforge()
            if not wakeup_ok:
                await _append_log(job_id, f"❌ VoiceForge wakeup failed: {wakeup_error}")
                return
                
            async with httpx.AsyncClient(timeout=TTS_TIMEOUT_SECS, follow_redirects=True) as voice_client:
                scenes[target_idx] = await generate_voice_for_scene(voice_client, job_id, scenes[target_idx], voice=voice)
                
            await update_job(job_id, scenes=scenes)
            await _append_log(job_id, f"🎙️ Narration voice for Scene {scene_number} regenerated.")
            
            await _append_log(job_id, f"✍️ Transcribing subtitles for Scene {scene_number}...")
            from services.subtitle_generator import generate_subtitle_for_scene
            scenes[target_idx], _ = await generate_subtitle_for_scene(job_id, scenes[target_idx])
            await update_job(job_id, scenes=scenes)
            await _append_log(job_id, f"✍️ Subtitles for Scene {scene_number} regenerated.")

        await _append_log(job_id, "✍️ Re-composing master subtitles (episode.srt)...")
        from services.subtitle_generator import finalize_master_subtitles, _Cue, _SceneSubtitleResult, scene_subtitle_path
        
        subtitle_results = []
        for s in scenes:
            s_num = s["scene_number"]
            srt_file = Path(scene_subtitle_path(job_id, s_num))
            
            if srt_file.exists():
                try:
                    content = srt_file.read_text("utf-8")
                    blocks = content.strip().split("\n\n")
                    cues = []
                    for block in blocks:
                        lines = block.strip().split("\n")
                        if len(lines) >= 3:
                            idx = int(lines[0].strip())
                            times = lines[1].split(" --> ")
                            def parse_ts(t):
                                pts = t.replace(",", ".").split(":")
                                return float(pts[0])*3600 + float(pts[1])*60 + float(pts[2])
                            start = parse_ts(times[0].strip())
                            end = parse_ts(times[1].strip())
                            text = "\n".join(lines[2:])
                            cues.append(_Cue(index=idx, start=start, end=end, text=text))
                    subtitle_results.append(_SceneSubtitleResult(
                        scene_number=s_num,
                        success=True,
                        cues=cues,
                        path=str(srt_file),
                        audio_duration=s.get("duration_hint") or 10.0
                    ))
                except Exception as e:
                    logger.error("Failed to parse SRT for Scene %d: %s", s_num, e)
                    
        await finalize_master_subtitles(job_id, scenes, subtitle_results)
        
        await _append_log(job_id, "🎬 Re-composing final video (episode.mp4)...")
        from services.video_composer import compose_video
        video_res = await compose_video(job_id, scenes)
        if not video_res["success"]:
            await _append_log(job_id, f"❌ Video composition failed: {video_res.get('error')}")
            await update_job(job_id, status="failed", error_message=video_res.get("error"))
            return
            
        await _append_log(job_id, "📝 Re-generating YouTube metadata...")
        from services.metadata_generator import generate_metadata
        await generate_metadata(job_id, job.get("story_text", ""), scenes)
        
        await _append_log(job_id, "🖼️ Re-generating YouTube thumbnail...")
        from services.thumbnail_generator import generate_thumbnail
        await generate_thumbnail(job_id, scenes, title=scenes[0].get("title", ""))
        
        download_urls = await _build_download_urls(job_id)
        await update_job(
            job_id,
            status="completed",
            progress_percent=100,
            current_step="done",
            scenes=scenes,
            completed_at=_utc_now(),
            download_urls=download_urls,
        )
        await _append_log(job_id, "🎉 Single-scene updates and video re-composition completed successfully!")


@router.post("/scene/{job_id}/{scene_number}", summary="Update details and regenerate a single scene")
async def regenerate_single_scene(
    job_id: str,
    scene_number: int,
    payload: SceneUpdatePayload,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    from utils.locks import get_job_lock
    lock = get_job_lock(job_id)
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Another process is currently running for this job. Please wait for it to complete."
        )

    job = await _load_job_or_404(job_id, current_user)
    _ensure_job_idle(job)
    # Set status to processing so UI knows it's working
    await update_job(job_id, status="processing", progress_percent=50)
    
    background_tasks.add_task(
        run_single_scene_regeneration,
        job_id,
        scene_number,
        payload
    )
    return {"job_id": job_id, "message": f"Single-scene update initiated for scene {scene_number}."}
