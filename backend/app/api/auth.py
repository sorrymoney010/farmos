"""Authentication endpoints for FARMOS."""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.core import User, UserRole, Role
from app.db.models.enums import RoleName, UserStatus
from app.db.session import get_db
from app.security.signatures import create_access_token, create_refresh_token, hash_password, verify_password
from app.logging import get_logger
from app.dependencies import get_current_user

logger = get_logger(__name__)
router = APIRouter()


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    roles: list[str]


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    req: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    if len(req.password) < settings.PASSWORD_MIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters",
        )
    result = await db.execute(select(User).where(User.email == req.email))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    user = User(
        email=req.email,
        password_hash=hash_password(req.password),
        full_name=req.full_name,
        status=UserStatus.ACTIVE,
        is_superuser=False,
    )
    db.add(user)
    await db.flush()
    customer_role_result = await db.execute(
        select(Role).where(Role.name == RoleName.CUSTOMER)
    )
    customer_role = customer_role_result.scalar_one_or_none()
    if customer_role:
        db.add(UserRole(user_id=user.id, role_id=customer_role.id))
    await db.commit()
    logger.info("user_registered", user_id=str(user.id), email=user.email)
    return await _build_token_response(user, db)


@router.post("/login", response_model=TokenResponse)
async def login(
    req: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(req.password, user.password_hash):
        logger.warning("login_failed", email=req.email)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if user.status.value != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account inactive")
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info("user_login", user_id=str(user.id))
    return await _build_token_response(user, db)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    req: RefreshRequest,
    db: AsyncSession = Depends(get_db),
):
    from app.security.signatures import verify_token  # noqa: PLC0415
    payload = verify_token(req.refresh_token, token_type="refresh")
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.status.value != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return await _build_token_response(user, db)


@router.post("/logout")
async def logout(
    current_user=Depends(get_current_user),
):
    logger.info("user_logout", user_id=str(current_user.id))
    return {"detail": "Logged out"}


@router.get("/me")
async def me(current_user=Depends(get_current_user)):
    return {
        "user_id": str(current_user.id),
        "email": current_user.email,
        "full_name": current_user.full_name,
        "status": current_user.status.value,
    }


async def _build_token_response(user: User, db: AsyncSession) -> TokenResponse:
    roles_result = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id)
    )
    role_names = [role.value for role in roles_result.scalars().all()]
    access = create_access_token(subject=str(user.id), roles=role_names)
    refresh = create_refresh_token(subject=str(user.id))
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user_id=str(user.id),
        email=user.email,
        roles=role_names,
    )
