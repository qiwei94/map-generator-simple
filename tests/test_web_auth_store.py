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
