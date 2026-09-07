"""Node enrollment and signed request verification for FARMOS.

Security notes
--------------
- Enrollment tokens are generated with cryptographic randomness (secrets.token_urlsafe).
- Consumption is atomic via Redis GETDEL so a token can never be used twice and a
  failed/aborted attempt cannot "burn" a valid token by leaving it half-deleted.
- The device signature is verified by the caller BEFORE consume_enrollment_token is
  invoked, so an invalid signed request cannot consume a good token.
- Redis clients are created via an app-lifetime pool (init/close in main.lifespan) and
  handed out per-call from a connection pool; no process-global async client is reused
  across event loops.
"""

from __future__ import annotations

import base64
import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jose import jwt
from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool

from app.config import settings

_POOL: ConnectionPool | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def init_redis() -> None:
    """Create the shared Redis connection pool (called once at app startup)."""
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool.from_url(settings.REDIS_URL, decode_responses=True, max_connections=20)


async def close_redis() -> None:
    """Dispose the shared Redis connection pool (called at app shutdown)."""
    global _POOL
    if _POOL is not None:
        await _POOL.aclose()
        _POOL = None


@asynccontextmanager
async def redis_client() -> AsyncIterator[Redis]:
    """Yield a Redis client bound to the shared pool; the client is closed after use.

    We never retain an async Redis object across event loops: it is created from the
    pool on demand and `aclose()`d when the request/path finishes.
    """
    if _POOL is None:
        # Test contexts that skip lifespan startup get a throwaway pool per client.
        pool = ConnectionPool.from_url(settings.REDIS_URL, decode_responses=True, max_connections=4)
        client = Redis(connection_pool=pool)
        try:
            yield client
        finally:
            await client.aclose()
            await pool.aclose()
        return
    client = Redis(connection_pool=_POOL)
    try:
        yield client
    finally:
        await client.aclose()


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


async def issue_enrollment_token(
    owner_user_id: str,
    *,
    farm_id: str | None = None,
    expires_in_minutes: int = 15,
) -> tuple[str, datetime]:
    # Cryptographically random token (no predictable structure).
    token = f"enroll-{secrets.token_urlsafe(32)}"
    expires_at = utcnow() + timedelta(minutes=expires_in_minutes)
    async with redis_client() as redis:
        await redis.setex(
            f"farmos:enrollment:{token}",
            int(timedelta(minutes=expires_in_minutes).total_seconds()),
            json.dumps(
                {
                    "owner_user_id": owner_user_id,
                    "farm_id": farm_id,
                    "expires_at": expires_at.isoformat(),
                }
            ),
        )
    return token, expires_at


async def consume_enrollment_token(token: str) -> dict[str, Any] | None:
    """Atomically fetch-and-delete the enrollment token (Redis GETDEL).

    Returns the token payload or None if missing/expired. Because GETDEL is atomic,
    exactly one caller can consume a given token.
    """
    async with redis_client() as redis:
        raw = await redis.getdel(f"farmos:enrollment:{token}")
    if not raw:
        return None
    return json.loads(raw)


async def mark_nonce_used(scope: str, nonce: str, ttl_seconds: int = 600) -> bool:
    async with redis_client() as redis:
        return bool(await redis.set(f"farmos:nonce:{scope}:{nonce}", "1", ex=ttl_seconds, nx=True))


def ensure_timestamp_fresh(timestamp: str, *, max_skew_seconds: int = 300) -> datetime:
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid request_timestamp") from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = abs((utcnow() - ts).total_seconds())
    if delta > max_skew_seconds:
        raise ValueError("Request timestamp outside allowed skew")
    return ts


def verify_request_signature(public_key_pem: str, payload: dict[str, Any], signature_b64: str) -> None:
    import logging
    logger = logging.getLogger(" FarmosVerify")
    public_key = serialization.load_pem_public_key(public_key_pem.encode())
    signature = base64.b64decode(signature_b64)
    body = canonical_json_bytes(payload)
    logger.warning("VERIFY_DEBUG: public_key=%s", public_key_pem[:80])
    logger.warning("VERIFY_DEBUG: payload=%s", json.dumps(payload, sort_keys=True, separators=(",",":"))[:200])
    logger.warning("VERIFY_DEBUG: canonical_bytes=%s", body.decode()[:200])
    logger.warning("VERIFY_DEBUG: signature_b64=%s", signature_b64[:40])
    logger.warning("VERIFY_DEBUG: signature_raw=%s", signature.hex()[:80])
    try:
        public_key.verify(signature, body, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ValueError("Invalid device signature") from exc


def issue_node_token(device_id: str) -> str:
    expire = utcnow() + timedelta(hours=settings.NODE_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": device_id,
        "roles": ["NODE"],
        "type": "node",
        "exp": expire,
        "iat": utcnow(),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verify_node_token(token: str) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except Exception:
        return None
    if payload.get("type") != "node":
        return None
    if "NODE" not in payload.get("roles", []):
        return None
    return payload
