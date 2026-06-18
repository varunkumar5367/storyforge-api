"""
utils/auth_helper.py — JWT token creation, validation, and FastAPI dependencies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import settings
from database import get_user_by_username

# Config — JWT_SECRET_KEY required in production; dev fallback only
SECRET_KEY = settings.effective_jwt_secret or "super_secret_key_storyforge_2026"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days token validity

security = HTTPBearer(auto_error=False)


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Generate a JWT access token containing subject data and expiry."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def verify_token(token: str) -> dict[str, Any] | None:
    """Validate a JWT token. Returns decoded payload or None if invalid."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
) -> dict:
    """
    FastAPI dependency: Extract token, verify it, and return the database user dict.
    Raises 401 if token is invalid or missing.
    """
    auth_token = None
    if credentials:
        auth_token = credentials.credentials

    if not auth_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization token is missing",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    payload = verify_token(auth_token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    username = payload.get("sub")
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject invalid",
        )
    
    user = await get_user_by_username(username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User associated with token not found",
        )
    
    if "is_active" in user and not user["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated. Please contact your administrator.",
        )
        
    # Update activity in the background or synchronously with safety
    import asyncio
    try:
        from database import DATABASE_URL, DatabaseConnection
        import time
        
        # In-memory activity cache to avoid database locking on every request
        global _last_seen_cache
        if '_last_seen_cache' not in globals():
            _last_seen_cache = {}
            
        now_ts = time.time()
        last_updated = _last_seen_cache.get(user["id"], 0)
        if now_ts - last_updated > 15: # update every 15s
            _last_seen_cache[user["id"]] = now_ts
            iso_now = datetime.now(timezone.utc).isoformat()
            
            async def run_db_update():
                try:
                    async with DatabaseConnection(DATABASE_URL) as db:
                        await db.execute("UPDATE users SET last_seen = ? WHERE id = ?", (iso_now, user["id"]))
                        await db.commit()
                except Exception as ex:
                    pass
                    
            asyncio.create_task(run_db_update())
    except Exception as ex:
        pass
        
    return user


async def get_admin_user(current_user: dict = Depends(get_current_user)) -> dict:
    """
    FastAPI dependency: Restricts access to admin role only.
    Raises 403 if user is not an admin.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden. Admin role required.",
        )
    return current_user
