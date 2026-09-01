"""Basic unit tests for FARMOS Phase 1."""

import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(backend_dir))


def test_imports():
    from app.config import settings  # noqa: F401
    from app.db.models.enums import RoleName, DeviceStatus, JobStatus  # noqa: F401
    from app.security.signatures import hash_password, verify_password, create_access_token, verify_token  # noqa: F401
    from app.logging import setup_logging, get_logger  # noqa: F401
    assert True


def test_password_hash():
    from app.security.signatures import hash_password, verify_password
    h = hash_password("testpass123")
    assert verify_password("testpass123", h)
    assert not verify_password("wrong", h)


def test_jwt_roundtrip():
    from app.security.signatures import create_access_token, verify_token
    token = create_access_token(subject="user-123", roles=["OWNER", "ADMIN"])
    payload = verify_token(token, token_type="access")
    assert payload["sub"] == "user-123"
    assert "OWNER" in payload["roles"]
    assert "ADMIN" in payload["roles"]


def test_role_enum():
    from app.db.models.enums import RoleName
    assert RoleName.OWNER == "OWNER"
    assert RoleName.ADMIN == "ADMIN"
    assert RoleName.PROVIDER == "PROVIDER"
