from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from nexafreight.models.user import User

pytestmark = pytest.mark.asyncio


async def test_health_endpoint(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("healthy", "ok")


async def test_stub_routers_return_200(client: AsyncClient, test_user, auth_headers_factory):
    headers = auth_headers_factory(test_user)
    for path in ["/api/shipments", "/api/alerts", "/api/disruptions", "/api/decisions"]:
        resp = await client.get(path, headers=headers)
        assert resp.status_code == 200


async def test_db_creates_user_table(db_session, test_user):
    result = await db_session.execute(select(User).where(User.email == test_user.email))
    fetched = result.scalar_one()
    assert fetched.email == test_user.email


async def test_login_returns_valid_jwt(client: AsyncClient, test_user):
    resp = await client.post(
        "/api/auth/login",
        json={"email": test_user.email, "password": "operator_test_password"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body.get("role") == "OPERATOR" or body.get("user", {}).get("role") == "OPERATOR"


async def test_login_wrong_password_fails(client: AsyncClient, test_user):
    resp = await client.post(
        "/api/auth/login",
        json={"email": test_user.email, "password": "wrong_password"},
    )
    assert resp.status_code == 401
