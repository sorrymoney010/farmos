"""Node enrollment and signed request verification for FARMOS."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jose import jwt
from redis.asyncio import Redis

from app.config import settings

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def get_redis() -> Redis:
    # Do NOT cache at module scope: a cached client binds to the first asyncio
    # event loop it touches and raises "Event loop is closed" when later used
    # from a different loop (e.g. across TestClient sessions / request cycles).
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


async def issue_enrollment_token(
    owner_user_id: str,
    *,
    farm_id: str | None = None,
    expires_in_minutes: int = 15,
) -> tuple[str, datetime]:
    token = f"enroll-{base64.urlsafe_b64encode(canonical_json_bytes({'u': owner_user_id, 't': utcnow().isoformat()})).decode().rstrip('=')[:40]}"
    expires_at = utcnow() + timedelta(minutes=expires_in_minutes)
    redis = await get_redis()
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
    redis = await get_redis()
    key = f"farmos:enrollment:{token}"
    raw = await redis.get(key)
    if not raw:
        return None
    await redis.delete(key)
    return json.loads(raw)


async def mark_nonce_used(scope: str, nonce: str, ttl_seconds: int = 600) -> bool:
    redis = await get_redis()
    key = f"farmos:nonce:{scope}:{nonce}"
    return bool(await redis.set(key, "1", ex=ttl_seconds, nx=True))


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
    public_key = serialization.load_pem_public_key(public_key_pem.encode())
    signature = base64.b64decode(signature_b64)
    body = canonical_json_bytes(payload)
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
