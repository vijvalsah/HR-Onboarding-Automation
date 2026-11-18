"""Admin route tests against the database-backed implementation."""

import httpx

from tests.conftest import auth_header, login


async def _admin(client: httpx.AsyncClient) -> dict[str, str]:
    return auth_header(await login(client, "admin@example.com"))


async def test_list_employees(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    resp = await client.get("/api/v1/admin/employees", headers=h)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert {"employee_id", "full_name", "department", "start_date"}.issubset(rows[0])


async def test_inventory(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    resp = await client.get("/api/v1/admin/assets/inventory", headers=h)
    assert resp.status_code == 200
    assert len(resp.json()) == 3


async def test_list_users_sorted_by_email(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    resp = await client.get("/api/v1/admin/users", headers=h)
    assert resp.status_code == 200
    emails = [u["email"] for u in resp.json()]
    assert emails == sorted(emails)


async def test_role_change_persists_and_audits(client: httpx.AsyncClient) -> None:
    h = await _admin(client)

    # Count audit logs before.
    before = await client.get("/api/v1/admin/audit-logs", headers=h)
    n_before = len(before.json())

    # Promote the HR user to admin.
    patch = await client.patch(
        "/api/v1/admin/users/hr-1/role",
        headers=h,
        json={"role": "admin"},
    )
    assert patch.status_code == 200
    assert patch.json()["role"] == "admin"

    # Role persisted in the users listing.
    users = await client.get("/api/v1/admin/users", headers=h)
    hr_user = next(u for u in users.json() if u["user_id"] == "hr-1")
    assert hr_user["role"] == "admin"

    # A new audit row was recorded, newest-first.
    after = await client.get("/api/v1/admin/audit-logs", headers=h)
    assert len(after.json()) == n_before + 1
    assert after.json()[0]["action"] == "admin.role_change"
    assert "hr -> admin" in after.json()[0]["detail"]


async def test_role_change_unknown_user_404(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    resp = await client.patch(
        "/api/v1/admin/users/nobody/role", headers=h, json={"role": "hr"}
    )
    assert resp.status_code == 404
