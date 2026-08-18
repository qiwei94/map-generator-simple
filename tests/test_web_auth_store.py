"""Passwordless account, multi-identity, and quota ledger contracts."""

from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"
if str(WEBAPP) not in sys.path:
    sys.path.insert(0, str(WEBAPP))

from auth_store import AuthError, AuthStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return AuthStore(
        tmp_path / "studio.db", "test-secret", default_quota=7,
        admin_emails={"owner@example.com"},
    )


def _login(store, email="user@example.com", now=1000.0):
    store.request_email_code(email, "123456", now=now, min_interval_s=0)
    return store.verify_email_code(email, "123456", now=now + 1)


def test_email_code_auto_registers_and_creates_session(store):
    user, token = _login(store)

    restored = store.get_session_user(token, now=1002.0)

    assert restored == user
    assert user.email == "user@example.com"
    assert user.role == "user"
    assert user.quota_remaining == 7


def test_wrong_and_reused_codes_are_rejected(store):
    store.request_email_code("user@example.com", "123456", now=1000,
                             min_interval_s=0)
    with pytest.raises(AuthError, match="不正确"):
        store.verify_email_code("user@example.com", "654321", now=1001)
    store.verify_email_code("user@example.com", "123456", now=1002)
    with pytest.raises(AuthError, match="失效"):
        store.verify_email_code("user@example.com", "123456", now=1003)


def test_email_code_rate_limit(store):
    store.request_email_code("user@example.com", "123456", now=1000)

    with pytest.raises(AuthError, match="频繁"):
        store.request_email_code("user@example.com", "123456", now=1030)


def test_admin_email_gets_admin_role(store):
    user, _ = _login(store, "OWNER@example.com")

    assert user.role == "admin"


def test_quota_reservation_is_idempotent_and_refundable(store):
    user, _ = _login(store)

    first = store.reserve_quota(user.id, "job-a", 5, now=1010)
    duplicate = store.reserve_quota(user.id, "job-a", 5, now=1011)
    refunded = store.refund_quota(user.id, "job-a", now=1012)
    refunded_twice = store.refund_quota(user.id, "job-a", now=1013)

    assert first.quota_used == 5
    assert duplicate.quota_used == 5
    assert refunded.quota_used == 0
    assert refunded_twice.quota_used == 0


def test_quota_rejects_unbounded_generation(store):
    user, _ = _login(store)

    with pytest.raises(AuthError, match="额度不足"):
        store.reserve_quota(user.id, "job-big", 8, now=1010)


def test_future_wechat_unionid_can_bind_to_existing_user(store):
    user, _ = _login(store)
    store.bind_identity(user.id, "wechat", "union-123", "openid-abc")

    with pytest.raises(AuthError, match="已绑定"):
        other, _ = _login(store, "other@example.com", now=2000)
        store.bind_identity(other.id, "wechat", "union-123", "openid-other")


def test_logout_revokes_session(store):
    _, token = _login(store)
    store.revoke_session(token, now=1002)

    assert store.get_session_user(token, now=1003) is None


def test_admin_controls_quota_and_account_status(store):
    user, _ = _login(store)

    updated = store.update_user_controls(
        user.id, quota_limit=12, status="paused", now=1010)

    assert updated.quota_limit == 12
    assert updated.status == "paused"
    with pytest.raises(AuthError, match="额度"):
        store.update_user_controls(user.id, quota_limit=-1)


def test_http_email_login_sets_secure_server_session(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import server

    http_store = AuthStore(tmp_path / "http.db", "http-secret")
    monkeypatch.setattr(server, "_AUTH_STORE", http_store)
    monkeypatch.setattr(server, "AUTH_DEV_ECHO_CODE", True)
    client = TestClient(server.app)

    started = client.post("/api/auth/email/start",
                          json={"email": "web@example.com"})
    verified = client.post(
        "/api/auth/email/verify",
        json={"email": "web@example.com", "code": started.json()["dev_code"]},
    )
    me = client.get("/api/auth/me")

    assert started.status_code == 200
    assert verified.status_code == 200
    assert "studio_session=" in verified.headers["set-cookie"]
    assert "HttpOnly" in verified.headers["set-cookie"]
    assert me.json()["user"]["email"] == "web@example.com"


def _http_login(client, email):
    started = client.post("/api/auth/email/start", json={"email": email})
    assert started.status_code == 200
    verified = client.post(
        "/api/auth/email/verify",
        json={"email": email, "code": started.json()["dev_code"]},
    )
    assert verified.status_code == 200
    return verified.json()["user"]


def test_owned_jobs_charge_once_share_cache_and_stay_private(monkeypatch,
                                                              tmp_path):
    from fastapi.testclient import TestClient
    import server

    http_store = AuthStore(tmp_path / "jobs.db", "http-secret",
                           default_quota=20)
    monkeypatch.setattr(server, "_AUTH_STORE", http_store)
    monkeypatch.setattr(server, "AUTH_DEV_ECHO_CODE", True)
    monkeypatch.setattr(server, "AUTH_REQUIRED", True)
    monkeypatch.setattr(server, "WORKER_MODE", True)
    monkeypatch.setattr(server, "_save_jobs", lambda: None)
    monkeypatch.setattr(server, "_pbf_status", lambda bbox: {
        "state": "local", "pbf": "fixture.osm.pbf",
    })
    server.JOBS.clear()
    first_client = TestClient(server.app)
    second_client = TestClient(server.app)
    first_user = _http_login(first_client, "first@example.com")
    second_user = _http_login(second_client, "second@example.com")

    payload = {
        "bbox": [30.20, 120.10, 30.27, 120.18],
        "name": "同一区域",
        "prototype": "landscape",
    }
    first = first_client.post("/api/styles", json=payload)
    assert first.status_code == 200
    job_id = first.json()["job_id"]
    assert http_store.get_user(first_user["id"]).quota_used == 2
    assert second_client.get(f"/api/jobs/{job_id}").status_code == 404

    shared = second_client.post("/api/styles", json=payload)

    assert shared.status_code == 200
    assert shared.json()["job_id"] == job_id
    assert shared.json()["reused"] is True
    assert http_store.get_user(second_user["id"]).quota_used == 0
    assert second_client.get(f"/api/jobs/{job_id}").status_code == 200
    assert second_client.get("/api/jobs?mine=true").json()["jobs"][0]["id"] == job_id
    assert set(server.JOBS[job_id]["owner_ids"]) == {
        first_user["id"], second_user["id"],
    }
    server.JOBS.clear()


def test_admin_can_list_users_and_change_quota(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import server

    http_store = AuthStore(
        tmp_path / "admin.db", "http-secret", default_quota=20,
        admin_emails={"admin@example.com"},
    )
    monkeypatch.setattr(server, "_AUTH_STORE", http_store)
    monkeypatch.setattr(server, "AUTH_DEV_ECHO_CODE", True)
    monkeypatch.setattr(server, "AUTH_REQUIRED", True)
    server.JOBS.clear()
    admin_client = TestClient(server.app)
    user_client = TestClient(server.app)
    admin = _http_login(admin_client, "admin@example.com")
    user = _http_login(user_client, "user@example.com")

    forbidden = user_client.get("/api/jobs?include_log=true")
    users = admin_client.get("/api/admin/users")
    changed = admin_client.patch(
        f"/api/admin/users/{user['id']}", json={"quota_limit": 50},
    )

    assert admin["role"] == "admin"
    assert forbidden.status_code == 403
    assert users.status_code == 200
    assert {row["email"] for row in users.json()["users"]} == {
        "admin@example.com", "user@example.com",
    }
    assert changed.json()["user"]["quota_limit"] == 50
