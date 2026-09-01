"""Integration tests for FARMOS Phase 1."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

pytest_plugins = ["pytest_asyncio"]


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"


def test_register_and_login(client):
    email = f"test-{uuid4().hex}@farmos.example"
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "ChangeMe123!", "full_name": "Test"},
    )
    assert reg.status_code == 201, reg.text
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "ChangeMe123!"},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    assert "access_token" in body
    assert body["roles"] == ["CUSTOMER"]


def test_me(client):
    email = f"me-{uuid4().hex}@farmos.example"
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "ChangeMe123!"},
    )
    assert reg.status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "ChangeMe123!"},
    )
    token = login.json()["access_token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email
