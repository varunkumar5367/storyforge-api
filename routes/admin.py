"""
routes/admin.py — Admin-only endpoints for managing users, monitoring jobs, and reading logs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from database import list_users, delete_user, list_jobs, update_user_pollen_balance, get_db, list_all_pollen_requests, review_pollen_request, get_user_by_id
from models.user import UserResponse
from models.job import JobSummary
from utils.auth_helper import get_admin_user

logger = logging.getLogger("storyforge.routes.admin")
router = APIRouter()


class PollenUpdatePayload(BaseModel):
    amount: float


class RequestReviewPayload(BaseModel):
    status: str # approved | denied


@router.get(
    "/users",
    response_model=list[UserResponse],
    summary="List all registered users in the system (Admin only)",
)
async def get_all_users(admin: dict = Depends(get_admin_user)) -> list[UserResponse]:
    """Retrieve details for all users registered in the database."""
    users = await list_users()
    return [
        UserResponse(
            id=u["id"],
            username=u["username"],
            role=u["role"],
            full_name=u.get("full_name", ""),
            display_name=u.get("display_name", ""),
            email=u.get("email", ""),
            phone=u.get("phone", ""),
            dob=u.get("dob", ""),
            avatar_data=u.get("avatar_data", ""),
            pollen_balance=u.get("pollen_balance", 20.0),
            last_seen=u.get("last_seen", ""),
            is_active=bool(u.get("is_active", 1)),
        )
        for u in users
    ]


@router.post(
    "/users/{user_id}/toggle-active",
    summary="Toggle a user's active status (Admin only)",
)
async def toggle_user_active(user_id: str, admin: dict = Depends(get_admin_user)):
    """Toggle the is_active status of a user."""
    if user_id == admin["id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate your own admin account.",
        )
    
    async with get_db() as db:
        async with db.execute("SELECT is_active FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="User not found.")
            new_status = 0 if row["is_active"] else 1
            await db.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_status, user_id))
            await db.commit()
            
    return {"success": True, "is_active": bool(new_status)}


@router.get(
    "/users/{user_id}/jobs",
    response_model=list[JobSummary],
    summary="Get all jobs owned by a specific user (Admin only)",
)
async def get_user_jobs(user_id: str, admin: dict = Depends(get_admin_user)) -> list[JobSummary]:
    """Retrieve all jobs matching user_id."""
    rows = await list_jobs(limit=100, user_id=user_id)
    return [
        JobSummary(
            job_id=r["id"],
            status=r["status"],
            progress_percent=r["progress_percent"],
            story_filename=r.get("story_filename"),
            created_at=r["created_at"],
            user_id=r.get("user_id"),
            username=r.get("username"),
        )
        for r in rows
    ]


@router.patch(
    "/users/{user_id}/pollen",
    summary="Directly edit a user's pollen credit balance (Admin only)",
)
async def edit_user_pollen(user_id: str, payload: PollenUpdatePayload, admin: dict = Depends(get_admin_user)):
    """Directly override a user's pollen_balance limit."""
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    await update_user_pollen_balance(user_id, payload.amount)
    return {"success": True, "pollen_balance": payload.amount}


@router.get(
    "/pollen/requests",
    summary="List all pollen requests in the system (Admin only)",
)
async def get_all_pollen_requests(admin: dict = Depends(get_admin_user)):
    """Retrieve all submitted pollen requests."""
    requests = await list_all_pollen_requests()
    return {"success": True, "requests": requests}


@router.post(
    "/pollen/requests/{request_id}/review",
    summary="Approve or Deny a pollen request (Admin only)",
)
async def review_pollen_credits(request_id: str, payload: RequestReviewPayload, admin: dict = Depends(get_admin_user)):
    """Approve or deny a pollen request, updating user's pollen_balance if approved."""
    if payload.status not in ["approved", "denied"]:
        raise HTTPException(status_code=400, detail="Status must be 'approved' or 'denied'.")
        
    async with get_db() as db:
        async with db.execute("SELECT * FROM pollen_requests WHERE id = ?", (request_id,)) as cur:
            req = await cur.fetchone()
            if not req:
                raise HTTPException(status_code=404, detail="Request not found.")
            
            if req["status"] != "pending":
                raise HTTPException(status_code=400, detail="Request has already been reviewed.")
                
            user_id = req["user_id"]
            amount = req["amount"]
            
            # If approved, increment/update user's pollen_balance
            if payload.status == "approved":
                async with db.execute("SELECT pollen_balance FROM users WHERE id = ?", (user_id,)) as u_cur:
                    u_row = await u_cur.fetchone()
                    current_bal = u_row["pollen_balance"] if u_row else 20.0
                    new_bal = current_bal + amount
                    await db.execute("UPDATE users SET pollen_balance = ? WHERE id = ?", (new_bal, user_id))
            
            # Update request status
            await review_pollen_request(request_id, payload.status)
            await db.commit()
            
    return {"success": True, "status": payload.status}


@router.delete(
    "/users/{user_id}",
    summary="Delete a user account from the system (Admin only)",
)
async def remove_user(user_id: str, admin: dict = Depends(get_admin_user)):
    """Delete a user profile by user_id."""
    # Prevent deleting yourself
    if user_id == admin["id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own admin account.",
        )

    deleted = await delete_user(user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID '{user_id}' not found.",
        )
        
    logger.info("User deleted by admin %s: %s", admin["username"], user_id)
    return {"success": True, "message": f"User '{user_id}' deleted successfully."}


@router.get(
    "/logs",
    summary="Retrieve the last 150 lines of system logs (Admin only)",
)
async def get_system_logs(admin: dict = Depends(get_admin_user)):
    """Read and return the last 150 lines of the uvicorn/backend logs file."""
    log_path = Path("storyforge.log")
    if not log_path.exists():
        return {
            "success": True,
            "logs": ["[System logs file 'storyforge.log' has not been created yet.]"]
        }

    try:
        # Read the file and get the last 150 lines
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            last_lines = lines[-150:]
            # Clean up newlines for display
            clean_lines = [l.rstrip("\r\n") for l in last_lines]
            return {"success": True, "logs": clean_lines}
    except Exception as e:
        logger.error("Failed to read system logs: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not read logs: {str(e)}",
        )


@router.get(
    "/analytics",
    summary="Get system analytics for admin dashboard",
)
async def get_admin_analytics(admin: dict = Depends(get_admin_user)):
    from datetime import datetime, timedelta, timezone
    from database import DatabaseConnection, DATABASE_URL
    import json
    
    now = datetime.now(timezone.utc)
    one_day_ago = (now - timedelta(days=1)).isoformat()
    thirty_days_ago = (now - timedelta(days=30)).isoformat()
    
    async with DatabaseConnection(DATABASE_URL) as db:
        # ── 1. Renders ────────────────────────────────────────────────────────
        async with db.execute("SELECT COUNT(*) as count FROM analytics_renders WHERE status = 'completed'") as cur:
            row = await cur.fetchone()
            completed_count = row["count"] if row else 0
            
        async with db.execute("SELECT COUNT(*) as count FROM analytics_renders WHERE status = 'failed'") as cur:
            row = await cur.fetchone()
            failed_count = row["count"] if row else 0
            
        async with db.execute("SELECT AVG(total_duration) as avg_dur FROM analytics_renders WHERE status = 'completed'") as cur:
            row = await cur.fetchone()
            avg_duration = round(row["avg_dur"], 2) if (row and row["avg_dur"] is not None) else 0.0
            
        async with db.execute("SELECT MAX(peak_memory_mb) as max_mem, AVG(peak_memory_mb) as avg_mem FROM analytics_renders") as cur:
            row = await cur.fetchone()
            max_memory = round(row["max_mem"], 2) if (row and row["max_mem"] is not None) else 0.0
            avg_memory = round(row["avg_mem"], 2) if (row and row["avg_mem"] is not None) else 0.0

        # Step durations average
        avg_steps = {
            "analyzing": 0.0,
            "generating_images": 0.0,
            "generating_voice": 0.0,
            "generating_subtitles": 0.0,
            "composing_video": 0.0,
            "generating_metadata": 0.0,
            "generating_thumbnail": 0.0
        }
        async with db.execute("SELECT step_durations FROM analytics_renders WHERE status = 'completed'") as cur:
            rows = await cur.fetchall()
            count = len(rows)
            if count > 0:
                sums = {k: 0.0 for k in avg_steps}
                for r in rows:
                    try:
                        steps = json.loads(r["step_durations"])
                        for k in sums:
                            sums[k] += steps.get(k, 0.0)
                    except Exception:
                        pass
                for k in avg_steps:
                    avg_steps[k] = round(sums[k] / count, 2)

        # Recent renders
        async with db.execute(
            """
            SELECT id, job_id, username, total_duration, peak_memory_mb, status, created_at 
            FROM analytics_renders 
            ORDER BY created_at DESC LIMIT 10
            """
        ) as cur:
            rows = await cur.fetchall()
            recent_renders = [dict(r) for r in rows]

        # ── 2. FFmpeg Failures ────────────────────────────────────────────────
        async with db.execute(
            """
            SELECT id, job_id, username, total_duration, status, error_message, ffmpeg_cmd, ffmpeg_stderr, created_at 
            FROM analytics_renders 
            WHERE status = 'failed' AND ffmpeg_stderr IS NOT NULL AND ffmpeg_stderr != ''
            ORDER BY created_at DESC LIMIT 10
            """
        ) as cur:
            rows = await cur.fetchall()
            ffmpeg_failures = [dict(r) for r in rows]

        # ── 3. User activity & Conversion ─────────────────────────────────────
        async with db.execute("SELECT COUNT(*) as count FROM users") as cur:
            row = await cur.fetchone()
            total_users = row["count"] if row else 0
            
        async with db.execute("SELECT COUNT(*) as count FROM users WHERE last_seen > ?", (one_day_ago,)) as cur:
            row = await cur.fetchone()
            active_24h = row["count"] if row else 0
            
        async with db.execute("SELECT COUNT(*) as count FROM users WHERE last_seen > ?", (thirty_days_ago,)) as cur:
            row = await cur.fetchone()
            active_30d = row["count"] if row else 0
            
        async with db.execute("SELECT COUNT(DISTINCT user_id) as count FROM jobs WHERE status = 'completed'") as cur:
            row = await cur.fetchone()
            users_with_jobs = row["count"] if row else 0
            
        conversion_rate = round((users_with_jobs / total_users) * 100, 2) if total_users > 0 else 0.0

        # ── 4. Credits ────────────────────────────────────────────────────────
        async with db.execute("SELECT SUM(pollen_balance) as sum_bal FROM users") as cur:
            row = await cur.fetchone()
            total_credits_held = round(row["sum_bal"], 2) if (row and row["sum_bal"] is not None) else 0.0
            
        async with db.execute("SELECT SUM(credit_consumed) as sum_consumed FROM analytics_renders WHERE status = 'completed'") as cur:
            row = await cur.fetchone()
            total_credits_consumed = round(row["sum_consumed"], 2) if (row and row["sum_consumed"] is not None) else 0.0
            
        async with db.execute("SELECT SUM(amount) as sum_req FROM pollen_requests") as cur:
            row = await cur.fetchone()
            total_requested = round(row["sum_req"], 2) if (row and row["sum_req"] is not None) else 0.0
            
        async with db.execute("SELECT SUM(amount) as sum_app FROM pollen_requests WHERE status = 'approved'") as cur:
            row = await cur.fetchone()
            total_approved = round(row["sum_app"], 2) if (row and row["sum_app"] is not None) else 0.0
            
        async with db.execute("SELECT SUM(amount) as sum_den FROM pollen_requests WHERE status = 'denied'") as cur:
            row = await cur.fetchone()
            total_denied = round(row["sum_den"], 2) if (row and row["sum_den"] is not None) else 0.0

        async with db.execute(
            """
            SELECT username, SUM(credit_consumed) as consumed 
            FROM analytics_renders 
            WHERE status = 'completed' AND username IS NOT NULL 
            GROUP BY username 
            ORDER BY consumed DESC LIMIT 5
            """
        ) as cur:
            rows = await cur.fetchall()
            credits_by_user = [dict(r) for r in rows]

    return {
        "renders": {
            "completed": completed_count,
            "failed": failed_count,
            "avg_duration": avg_duration,
            "avg_steps": avg_steps,
            "max_memory": max_memory,
            "avg_memory": avg_memory,
            "recent": recent_renders
        },
        "failures": ffmpeg_failures,
        "users": {
            "total_registered": total_users,
            "active_24h": active_24h,
            "active_30d": active_30d,
            "conversion_rate": conversion_rate
        },
        "credits": {
            "total_held": total_credits_held,
            "total_consumed": total_credits_consumed,
            "total_requested": total_requested,
            "total_approved": total_approved,
            "total_denied": total_denied,
            "by_user": credits_by_user
        }
    }


# ---------------------------------------------------------------------------
# Server Control & Wake Request management
# ---------------------------------------------------------------------------
class ServerSettingsPayload(BaseModel):
    max_concurrent_tasks: int
    max_concurrent_users: int


class WakeReviewPayload(BaseModel):
    status: str  # accepted | ignored


@router.get(
    "/server-status",
    summary="Get live server status and pending wake requests (Admin only)",
)
async def get_admin_server_status(admin: dict = Depends(get_admin_user)):
    from database import get_server_status, list_wake_requests
    status = await get_server_status()
    wake_reqs = await list_wake_requests(limit=50)
    return {
        "success": True,
        "status": status,
        "wake_requests": wake_reqs
    }


@router.post(
    "/server-settings",
    summary="Update backend server settings limits (Admin only)",
)
async def update_admin_server_settings(payload: ServerSettingsPayload, admin: dict = Depends(get_admin_user)):
    from database import update_server_status
    await update_server_status(
        max_concurrent_tasks=payload.max_concurrent_tasks,
        max_concurrent_users=payload.max_concurrent_users
    )
    return {"success": True}


@router.post(
    "/wake-requests/{request_id}/review",
    summary="Approve or ignore a pending wake request (Admin only)",
)
async def review_admin_wake_request(request_id: str, payload: WakeReviewPayload, admin: dict = Depends(get_admin_user)):
    from database import review_wake_request
    if payload.status not in ["accepted", "ignored"]:
        raise HTTPException(status_code=400, detail="Status must be 'accepted' or 'ignored'.")
    reviewed = await review_wake_request(request_id, payload.status)
    return {"success": True, "reviewed": reviewed}

