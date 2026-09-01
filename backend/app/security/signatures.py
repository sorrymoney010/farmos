"""Security utilities for FARMOS."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt
from passlib.context import CryptContext

from app.config import settings


# Password hashing
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    """Hash a password."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    return pwd_context.verify(plain_password, hashed_password)


# JWT tokens
def create_access_token(
    subject: str,
    roles: list[str],
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create a JWT access token."""
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    
    to_encode = {
        "sub": subject,
        "roles": roles,
        "exp": expire,
        "type": "access",
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(subject: str, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT refresh token."""
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )
    
    to_encode = {
        "sub": subject,
        "exp": expire,
        "type": "refresh",
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except jwt.JWTError:
        return {}


def verify_token(token: str, token_type: str = "access") -> Optional[dict]:
    """Verify a token and return payload if valid."""
    payload = decode_token(token)
    if not payload:
        return None
    if payload.get("type") != token_type:
        return None
    return payload


# Nonce generation
def generate_nonce(length: int = 32) -> str:
    """Generate a cryptographically secure nonce."""
    return secrets.token_urlsafe(length)


def generate_challenge_message(
    account_type: str,
    account_id: str,
    wallet_address: str,
    nonce: str,
) -> str:
    """Generate the wallet verification challenge message."""
    return f"""FARMOS Wallet Verification

Account:
{account_type}-{account_id}

Wallet:
{wallet_address}

Nonce:
{nonce}

Purpose:
Verify wallet ownership.

This signature does not authorize a transfer."""


# Hash utilities
def sha256_hash(data: str) -> str:
    """Generate SHA-256 hash of string data."""
    return hashlib.sha256(data.encode()).hexdigest()


def verify_sha256(data: str, expected_hash: str) -> bool:
    """Verify data against expected SHA-256 hash."""
    return secrets.compare_digest(sha256_hash(data), expected_hash)