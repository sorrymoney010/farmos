"""Audit logging middleware for FARMOS."""

import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.db.session import AsyncSessionLocal
from app.db.models.enums import AuditEventType
from app.logging import get_logger, get_request_id

logger = get_logger(__name__)


class AuditMiddleware(BaseHTTPMiddleware):
    """Middleware to log security-relevant events to audit_logs."""

    AUDIT_PATHS = {
        ("POST", "/api/v1/auth/login"): AuditEventType.USER_LOGIN,
        ("POST", "/api/v1/auth/logout"): AuditEventType.USER_LOGOUT,
        ("POST", "/api/v1/auth/register"): AuditEventType.USER_REGISTERED,
        ("POST", "/api/v1/wallets/verify-signature"): AuditEventType.WALLET_VERIFIED,
        ("POST", "/api/v1/devices/enroll"): AuditEventType.DEVICE_ENROLLED,
        ("POST", "/api/v1/admin/devices/{device_id}/quarantine"): AuditEventType.DEVICE_QUARANTINED,
        ("POST", "/api/v1/admin/devices/{device_id}/release"): AuditEventType.DEVICE_RELEASED,
    }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip audit for non-relevant paths to reduce overhead
        path = request.url.path
        method = request.method

        response = await call_next(request)

        event_type = None
        for (m, p), evt in self.AUDIT_PATHS.items():
            if method == m and path == p:
                event_type = evt
                break

        if event_type and response.status_code < 500:
            # Best-effort async audit write
            try:
                from app.db.models.audit import AuditLog  # noqa: PLC0415
                from app.db.models.core import User  # noqa: PLC0415
                from sqlalchemy import select  # noqa: PLC0415

                user_id = getattr(request.state, "user_id", None)
                async with AsyncSessionLocal() as session:
                    if user_id:
                        log = AuditLog(
                            event_type=event_type,
                            actor_type="USER",
                            actor_id=user_id,
                            target_type=None,
                            target_id=None,
                            request_id=get_request_id() or None,
                            ip_hash=None,
                            event_metadata={
                                "method": method,
                                "path": path,
                                "status_code": response.status_code,
                            },
                        )
                        session.add(log)
                        await session.commit()
            except Exception:  # noqa: BLE001
                logger.warning("audit_log_failed", exc_info=True)

        return response
