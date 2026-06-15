"""
models/user.py — Pydantic models for user registration, login, and profile.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, description="Unique username")
    password: str = Field(..., min_length=6, max_length=100, description="Plaintext password")


class UserLogin(BaseModel):
    username: str = Field(...)
    password: str = Field(...)


class UserResponse(BaseModel):
    id: str
    username: str
    role: str
    full_name: str = ""
    display_name: str = ""
    email: str = ""
    phone: str = ""
    dob: str = ""
    avatar_data: str = ""
    pollen_balance: float = 20.0
    last_seen: str = ""
    is_active: bool = True

    class Config:
        from_attributes = True


class UserUpdatePayload(BaseModel):
    full_name: str | None = None
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    dob: str | None = None
    avatar_data: str | None = None


class PasswordUpdatePayload(BaseModel):
    old_password: str
    new_password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
