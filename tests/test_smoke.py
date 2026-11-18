"""Smoke tests describing current app behavior.

These act as a safety net: they encode the user-facing contract (login works,
roles are enforced, each role can read its own data) so later refactors — adding
a database, hashing passwords, migrating routes — can be verified with one
command. They assert on behavior, not on hardcoded sample values, so they should
keep passing after the mock data is replaced with real data.
"""

import httpx
import pytest

from tests.conftest import auth_header, login

ROLES = ["candidate@example.com", "hr@example.com", "admin@example.com"]


async def test_health_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


@pytest.mark.parametrize("email", ROLES)
async def test_login_succeeds_for_each_role(client: httpx.AsyncClient, email: str) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "password"},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


async def test_login_rejects_bad_password(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "hr@example.com", "password": "wrong"},
    )
    assert resp.status_code == 401


async def test_login_rejects_unknown_user(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "password"},
    )
    assert resp.status_code == 401


async def test_protected_route_requires_token(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/hr/candidates")
    assert resp.status_code in (401, 403)


async def test_hr_can_list_candidates(client: httpx.AsyncClient) -> None:
    token = await login(client, "hr@example.com")
    resp = await client.get("/api/v1/hr/candidates", headers=auth_header(token))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_admin_can_list_employees(client: httpx.AsyncClient) -> None:
    token = await login(client, "admin@example.com")
    resp = await client.get("/api/v1/admin/employees", headers=auth_header(token))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_candidate_can_read_own_profile(client: httpx.AsyncClient) -> None:
    token = await login(client, "candidate@example.com")
    resp = await client.get("/api/v1/candidate/me", headers=auth_header(token))
    assert resp.status_code == 200
    assert "email" in resp.json()


async def test_role_enforcement_candidate_denied_admin(client: httpx.AsyncClient) -> None:
    token = await login(client, "candidate@example.com")
    resp = await client.get("/api/v1/admin/employees", headers=auth_header(token))
    assert resp.status_code == 403


async def test_role_enforcement_hr_denied_admin(client: httpx.AsyncClient) -> None:
    token = await login(client, "hr@example.com")
    resp = await client.get("/api/v1/admin/employees", headers=auth_header(token))
    assert resp.status_code == 403
