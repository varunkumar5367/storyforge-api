"""
routes/status.py
─────────────────
Endpoints for polling job progress and listing all jobs.

GET /api/status/{job_id}  — full status of a single job
GET /api/status/          — paginated list of all jobs
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import httpx

from config import settings
from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import FileResponse
from database import get_job, list_jobs, delete_job, update_job, count_user_images_last_hour
from models.job import JobListResponse, JobStatusResponse, JobSummary, JobUpdatePayload
from utils.auth_helper import get_current_user

logger = logging.getLogger("storyforge.routes.status")
router = APIRouter()


@router.get(
    "/voice/sample/{voice_id}",
    summary="Get or generate voice preview sample",
)
async def get_voice_sample(voice_id: str):
    """
    Generate or get a cached 2-second audio sample greeting for the specified voice_id.
    """
    import re
    if not re.match(r"^[a-zA-Z0-9\-]+$", voice_id):
        raise HTTPException(status_code=400, detail="Invalid voice_id format.")

    output_dir = Path(settings.output_dir)
    samples_dir = output_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    sample_path = samples_dir / f"{voice_id}.mp3"

    if sample_path.exists():
        return FileResponse(sample_path)

    # Otherwise, generate via VoiceForge or locally using edge-tts
    text = "Hello! This is a preview of my voice. I hope you like it."
    
    try:
        import edge_tts
    except ImportError:
        edge_tts = None

    if edge_tts is not None:
        try:
            logger.info("Generating voice preview locally using edge-tts: %s", voice_id)
            communicate = edge_tts.Communicate(text, voice_id)
            audio_data = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data += chunk["data"]
            if audio_data:
                sample_path.write_bytes(audio_data)
                return FileResponse(sample_path)
        except Exception as e:
            logger.warning("Local edge-tts preview failed, falling back to VoiceForge API: %s", e)

    voiceforge_url = settings.voiceforge_url
    if not voiceforge_url:
        raise HTTPException(
            status_code=503,
            detail="Voice preview unavailable: configure VOICEFORGE_URL or ensure edge-tts is installed.",
        )
    voiceforge_url = voiceforge_url.rstrip("/")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload = {
                "text": text,
                "voice": voice_id,
                "speed": 1.0,
                "pitch": 0,
            }
            resp = await client.post(f"{voiceforge_url}/api/tts", data=payload)
            if resp.status_code == 200 and resp.content:
                sample_path.write_bytes(resp.content)
                return FileResponse(sample_path)
            else:
                raise HTTPException(status_code=502, detail=f"VoiceForge TTS preview failed with status {resp.status_code}")
    except Exception as e:
        logger.error("Failed to generate voice sample for %s: %s", voice_id, e)
        raise HTTPException(status_code=500, detail=f"Voice sample generation failed: {str(e)}")


@router.get(
    "/pollen/balance",
    summary="Get Pollinations balance and remaining images estimate",
)
async def get_pollen_balance(current_user: dict = Depends(get_current_user)):
    """
    For non-admin, returns remaining hourly picture budget.
    For admin, queries gen.pollinations.ai/account/balance.
    """
    if current_user.get("role") != "admin":
        used = await count_user_images_last_hour(current_user["id"])
        budget = current_user.get("pollen_balance", 20.0)
        remaining = max(0.0, budget - used)
        return {
            "success": True,
            "pollen": float(remaining),
            "images_left": int(remaining),
            "message": f"Free tier hourly budget: {remaining:.4f} of {budget:.4f} images remaining (refreshes hourly)."
        }

    key = settings.pollinations_api_key
    if not key:
        return {
            "success": True,
            "pollen": None,
            "images_left": None,
            "message": "No API key configured (using unauthenticated IP-based limit)."
        }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://gen.pollinations.ai/account/balance",
                headers={"Authorization": f"Bearer {key}"}
            )
            
            if resp.status_code == 200:
                data = resp.json()
                balance = data.get("balance", 0.0)
                # 0.00175 pollen per Flux Schnell image
                images_left = int(balance / 0.00175) if balance > 0 else 0
                return {
                    "success": True,
                    "pollen": balance,
                    "images_left": images_left
                }
            elif resp.status_code == 403:
                data = resp.json().get("error", {})
                msg = data.get("message", "API key is missing permissions.")
                return {
                    "success": False,
                    "error_type": "FORBIDDEN",
                    "error": msg,
                    "message": "Please go to enter.pollinations.ai, create a new key and make sure you check 'account:usage' scope."
                }
            else:
                return {
                    "success": False,
                    "error_type": "HTTP_ERROR",
                    "error": f"Pollinations returned status code {resp.status_code}",
                    "message": resp.text[:200]
                }
    except Exception as e:
        return {
            "success": False,
            "error_type": "EXCEPTION",
            "error": str(e),
            "message": "Could not connect to Pollinations API."
        }


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    summary="Get the current status of a job",
)
async def get_job_status(job_id: str, current_user: dict = Depends(get_current_user)) -> JobStatusResponse:
    """
    Poll this endpoint to track pipeline progress.
    """
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    # Concurrency control: non-admins can only see their own jobs
    if current_user.get("role") != "admin" and job.get("user_id") != current_user["id"]:
        raise HTTPException(
            status_code=403,
            detail="Access denied. You do not own this job.",
        )

    # Deserialise JSON fields
    scenes = job.get("scenes")
    if isinstance(scenes, str):
        try:
            scenes = json.loads(scenes)
        except json.JSONDecodeError:
            scenes = None

    logs = job.get("logs")
    if isinstance(logs, str):
        try:
            logs = json.loads(logs)
        except json.JSONDecodeError:
            logs = []
    elif logs is None:
        logs = []

    from database import get_average_scene_duration
    avg_scene_dur = await get_average_scene_duration()

    return JobStatusResponse(
        job_id=job["id"],
        status=job["status"],
        progress_percent=job["progress_percent"],
        current_step=job.get("current_step"),
        story_filename=job.get("story_filename"),
        created_at=job.get("created_at"),
        completed_at=job.get("completed_at"),
        error_message=job.get("error_message"),
        scenes=scenes,
        voice=job.get("voice", "en-US-JennyNeural"),
        logs=logs,
        avg_scene_duration=avg_scene_dur,
    )



@router.get(
    "/",
    response_model=JobListResponse,
    summary="List all jobs (most recent first)",
)
async def list_all_jobs(
    limit: int = Query(default=20, ge=1, le=100, description="Max results to return"),
    current_user: dict = Depends(get_current_user),
) -> JobListResponse:
    """Return a paginated list of all jobs ordered by creation time (newest first)."""
    user_id_filter = current_user["id"]
    rows = await list_jobs(limit=limit, user_id=user_id_filter)
    summaries = [
        JobSummary(
            job_id=r["id"],
            status=r["status"],
            progress_percent=r["progress_percent"],
            story_filename=r.get("story_filename"),
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return JobListResponse(jobs=summaries, total=len(summaries))


@router.delete(
    "/{job_id}",
    summary="Delete a job and its generated files",
)
async def delete_job_route(job_id: str, current_user: dict = Depends(get_current_user)):
    """
    Delete a job from the database and remove its generated files from disk.
    """
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    if current_user.get("role") != "admin" and job.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Access denied. You do not own this job.")
        
    # Delete from database
    deleted = await delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete job from database.")
        
    # Delete output directory if it exists
    try:
        output_dir = Path(settings.output_dir) / job_id
        if output_dir.exists():
            shutil.rmtree(output_dir)
            logger.info("Deleted output files for job %s.", job_id)
    except Exception as e:
        logger.error("Failed to delete output files for job %s: %s", job_id, e)
        
    return {"success": True, "message": f"Job '{job_id}' deleted successfully."}


@router.patch(
    "/{job_id}",
    response_model=JobStatusResponse,
    summary="Update details of a job (e.g. story filename)",
)
async def update_job_route(job_id: str, payload: JobUpdatePayload, current_user: dict = Depends(get_current_user)) -> JobStatusResponse:
    """
    Update details of a job, currently supporting renaming the story filename.
    """
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    if current_user.get("role") != "admin" and job.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Access denied. You do not own this job.")

    update_data = {}
    if payload.story_filename is not None:
        update_data["story_filename"] = payload.story_filename

    if update_data:
        await update_job(job_id, **update_data)
        logger.info("Updated job %s: %s", job_id, update_data)

    # Fetch updated job
    updated_job = await get_job(job_id)

    # Deserialise JSON fields
    scenes = updated_job.get("scenes")
    if isinstance(scenes, str):
        try:
            scenes = json.loads(scenes)
        except json.JSONDecodeError:
            scenes = None

    logs = updated_job.get("logs")
    if isinstance(logs, str):
        try:
            logs = json.loads(logs)
        except json.JSONDecodeError:
            logs = []
    elif logs is None:
        logs = []

    return JobStatusResponse(
        job_id=updated_job["id"],
        status=updated_job["status"],
        progress_percent=updated_job["progress_percent"],
        current_step=updated_job.get("current_step"),
        story_filename=updated_job.get("story_filename"),
        created_at=updated_job.get("created_at"),
        completed_at=updated_job.get("completed_at"),
        error_message=updated_job.get("error_message"),
        scenes=scenes,
        voice=updated_job.get("voice", "en-US-JennyNeural"),
        logs=logs,
    )


@router.post("/pause/{job_id}", summary="Pause a running job")
async def pause_job(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if job.get("status") in ("completed", "failed"):
        raise HTTPException(status_code=400, detail="Cannot pause a completed or failed job.")
    if current_user.get("role") != "admin" and job.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Access denied. You do not own this job.")
    
    from services.orchestrator import _append_log
    await update_job(job_id, status="paused", current_step="paused")
    await _append_log(job_id, "Generation PAUSED by user request.")
    return {"success": True, "message": "Job paused."}


@router.post("/resume/{job_id}", summary="Resume a paused or failed job")
async def resume_job(job_id: str, current_user: dict = Depends(get_current_user)):
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    
    status = job.get("status")
    if status not in ("paused", "failed"):
        raise HTTPException(status_code=400, detail="Job is not paused or failed.")
        
    if current_user.get("role") != "admin" and job.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Access denied. You do not own this job.")
    
    from services.orchestrator import _append_log, start_pipeline
    
    if status == "failed":
        await update_job(
            job_id,
            status="queued",
            current_step="queued",
            progress_percent=0,
            error_message=None
        )
        await _append_log(job_id, "Generation RETRIED / RESUMED by user request.")
        start_pipeline(job_id, job.get("story_text") or "")
        return {"success": True, "message": "Job restarted successfully."}
    else:
        await update_job(job_id, status="generating_images", current_step="generating_images")
        await _append_log(job_id, "Generation RESUMED by user request.")
        return {"success": True, "message": "Job resumed."}


from pydantic import BaseModel

class PollenRequestPayload(BaseModel):
    amount: float
    message: str


@router.post(
    "/pollen/request",
    summary="Request pollen credits from the admin",
)
async def request_pollen_credits(payload: PollenRequestPayload, current_user: dict = Depends(get_current_user)):
    """Submit a new pollen request to the admin."""
    # Verify profile is complete before allowing request:
    # Check if full_name and email are populated. Display name defaults to username so it's always there.
    if not current_user.get("full_name") or not current_user.get("email"):
        raise HTTPException(
            status_code=400,
            detail="You must complete your profile (Full Name and Email) before requesting pollen credits."
        )
        
    import uuid
    from database import create_pollen_request
    req_id = str(uuid.uuid4())
    req = await create_pollen_request(req_id, current_user["id"], payload.amount, payload.message)
    return {"success": True, "request": req}


@router.get(
    "/pollen/requests",
    summary="List current user's pollen requests",
)
async def get_user_requests(current_user: dict = Depends(get_current_user)):
    """Retrieve pollen requests submitted by current user."""
    from database import list_user_pollen_requests
    reqs = await list_user_pollen_requests(current_user["id"])
    return {"success": True, "requests": reqs}

