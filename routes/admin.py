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
