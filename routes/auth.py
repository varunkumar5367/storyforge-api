"""
routes/auth.py — Authentication routes (login, signup, session profile check).
"""

from __future__ import annotations

import uuid
import logging
from fastapi import APIRouter, HTTPException, Depends, status

from database import get_user_by_username, create_user, hash_password, verify_password, update_user_profile, update_user_password, save_analytics_event
from models.user import UserRegister, UserLogin, UserResponse, TokenResponse, UserUpdatePayload, PasswordUpdatePayload
from utils.auth_helper import create_access_token, get_current_user

logger = logging.getLogger("storyforge.routes.auth")
router = APIRouter()


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
)
async def register(payload: UserRegister) -> UserResponse:
    """Register a new user in the system. Default role is 'user'."""
    existing = await get_user_by_username(payload.username)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already taken.",
        )
    
    user_id = str(uuid.uuid4())
    pwd_hash = hash_password(payload.password)
    
    user = await create_user(user_id, payload.username, pwd_hash, "user")
    await save_analytics_event("signup", user_id=user_id, username=payload.username)
    logger.info("New user registered: %s (ID: %s)", payload.username, user_id)
    return UserResponse(
        id=user["id"],
        username=user["username"],
        role=user["role"],
        full_name=user.get("full_name", ""),
        display_name=user.get("display_name", ""),
        email=user.get("email", ""),
        phone=user.get("phone", ""),
        dob=user.get("dob", ""),
        avatar_data=user.get("avatar_data", ""),
        pollen_balance=user.get("pollen_balance", 20.0),
        last_seen=user.get("last_seen", ""),
        is_active=bool(user.get("is_active", 1)),
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login to receive a JWT access token",
)
async def login(payload: UserLogin) -> TokenResponse:
    """Authenticate and obtain a JWT access token."""
    user = await get_user_by_username(payload.username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
        )
    
    if not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
        )

    if "is_active" in user and not user["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated. Please contact your administrator.",
        )
    
    token = create_access_token(data={"sub": user["username"], "role": user["role"]})
    await save_analytics_event("login", user_id=user["id"], username=user["username"])
    logger.info("User logged in: %s", user["username"])
    return TokenResponse(access_token=token, token_type="bearer", role=user["role"])


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Retrieve current logged-in user profile details",
)
async def get_me(current_user: dict = Depends(get_current_user)) -> UserResponse:
    """Return user profile associated with current active token."""
    return UserResponse(
        id=current_user["id"],
        username=current_user["username"],
        role=current_user["role"],
        full_name=current_user.get("full_name", ""),
        display_name=current_user.get("display_name", ""),
        email=current_user.get("email", ""),
        phone=current_user.get("phone", ""),
        dob=current_user.get("dob", ""),
        avatar_data=current_user.get("avatar_data", ""),
        pollen_balance=current_user.get("pollen_balance", 20.0),
        last_seen=current_user.get("last_seen", ""),
        is_active=bool(current_user.get("is_active", 1)),
    )


@router.patch(
    "/profile",
    response_model=UserResponse,
    summary="Update current user profile fields",
)
async def update_profile(
    payload: UserUpdatePayload,
    current_user: dict = Depends(get_current_user)
) -> UserResponse:
    """Update display name, full name, email, phone, dob, or avatar."""
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if fields:
        await update_user_profile(current_user["id"], **fields)
        from database import get_user_by_id
        user = await get_user_by_id(current_user["id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        current_user = user
        
    return UserResponse(
        id=current_user["id"],
        username=current_user["username"],
        role=current_user["role"],
        full_name=current_user.get("full_name", ""),
        display_name=current_user.get("display_name", ""),
        email=current_user.get("email", ""),
        phone=current_user.get("phone", ""),
        dob=current_user.get("dob", ""),
        avatar_data=current_user.get("avatar_data", ""),
        pollen_balance=current_user.get("pollen_balance", 20.0),
        last_seen=current_user.get("last_seen", ""),
        is_active=bool(current_user.get("is_active", 1)),
    )


@router.post(
    "/change-password",
    summary="Change current user password",
)
async def change_password(
    payload: PasswordUpdatePayload,
    current_user: dict = Depends(get_current_user)
):
    """Authenticate with old password and set a new password."""
    from database import verify_password
    if not verify_password(payload.old_password, current_user["password_hash"]):
        raise HTTPException(status_code=400, detail="Incorrect old password.")
    
    new_hash = hash_password(payload.new_password)
    await update_user_password(current_user["id"], new_hash)
    return {"success": True, "message": "Password changed successfully."}


@router.post(
    "/heartbeat",
    summary="Trigger an active heartbeat ping",
)
async def heartbeat(current_user: dict = Depends(get_current_user)):
    """Ping to update user's last_seen status. Handled by JWT validator."""
    return {"success": True, "active": True}
