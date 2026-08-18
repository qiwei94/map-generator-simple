# -*- coding: utf-8 -*-
"""Durable passwordless accounts and quota ledger for Studio.

The implementation intentionally uses stdlib SQLite in WAL mode for the
single-host MVP.  All mutations use explicit transactions, so the API and the
single compute worker can safely share it.  The schema keeps authentication
identities separate from users, allowing WeChat UnionID to be bound later
without creating a second account.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class AuthError(ValueError):
    """User-facing authentication or quota failure."""


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    role: str
    status: str
    quota_limit: int
    quota_used: int
    quota_period: str

    @property
    def quota_remaining(self) -> int:
        return max(0, self.quota_limit - self.quota_used)


class AuthStore:
    def __init__(self, path: Path, secret: str, default_quota: int = 20,
                 admin_emails: set[str] | None = None):
        if not secret:
            raise ValueError("auth secret must not be empty")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secret = secret.encode("utf-8")
        self.default_quota = int(default_quota)
        self.admin_emails = {
            self.normalize_email(value) for value in (admin_emails or set())
        }
        self._init_schema()

    @staticmethod
    def normalize_email(value: str) -> str:
        email = (value or "").strip().lower()
        if len(email) > 254 or not _EMAIL_RE.match(email):
            raise AuthError("邮箱格式不正确")
        return email

    @staticmethod
    def _period(now: float | None = None) -> str:
        return time.strftime("%Y-%m", time.gmtime(now or time.time()))

    def _digest(self, purpose: str, value: str) -> str:
        return hmac.new(
            self.secret, f"{purpose}:{value}".encode("utf-8"), hashlib.sha256,
        ).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL DEFAULT 'user',
                    status TEXT NOT NULL DEFAULT 'active',
                    quota_limit INTEGER NOT NULL,
                    quota_used INTEGER NOT NULL DEFAULT 0,
                    quota_period TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_identities (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    provider_uid TEXT NOT NULL,
                    provider_openid TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(provider, provider_uid)
                );
                CREATE TABLE IF NOT EXISTS email_codes (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    requested_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    consumed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_email_codes_lookup
                    ON email_codes(email, requested_at DESC);
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE TABLE IF NOT EXISTS quota_ledger (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    job_id TEXT NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(user_id, job_id, reason)
                );
            """)

    def request_email_code(self, email: str, code: str, *,
                           now: float | None = None, ttl_s: int = 600,
                           min_interval_s: int = 60) -> None:
        email = self.normalize_email(email)
        now = now or time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                "SELECT requested_at FROM email_codes WHERE email=? "
                "ORDER BY requested_at DESC LIMIT 1", (email,),
            ).fetchone()
            if latest and now - latest["requested_at"] < min_interval_s:
                raise AuthError("验证码发送过于频繁，请稍后再试")
            conn.execute(
                "INSERT INTO email_codes "
                "(id,email,code_hash,requested_at,expires_at) VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, email, self._digest("email-code", code),
                 now, now + ttl_s),
            )
            conn.commit()

    def verify_email_code(self, email: str, code: str, *,
                          now: float | None = None,
                          session_ttl_s: int = 30 * 86400) -> tuple[AuthUser, str]:
        email = self.normalize_email(email)
        now = now or time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM email_codes WHERE email=? AND consumed_at IS NULL "
                "ORDER BY requested_at DESC LIMIT 1", (email,),
            ).fetchone()
            if not row or row["expires_at"] < now:
                raise AuthError("验证码已失效，请重新获取")
            if row["attempts"] >= 5:
                raise AuthError("验证码错误次数过多，请重新获取")
            if not hmac.compare_digest(
                    row["code_hash"], self._digest("email-code", code)):
                conn.execute(
                    "UPDATE email_codes SET attempts=attempts+1 WHERE id=?",
                    (row["id"],),
                )
                conn.commit()
                raise AuthError("验证码不正确")
            conn.execute("UPDATE email_codes SET consumed_at=? WHERE id=?",
                         (now, row["id"]))
            user_row = conn.execute(
                "SELECT * FROM users WHERE email=?", (email,),
            ).fetchone()
            if not user_row:
                user_id = uuid.uuid4().hex
                role = "admin" if email in self.admin_emails else "user"
                period = self._period(now)
                conn.execute(
                    "INSERT INTO users "
                    "(id,email,role,status,quota_limit,quota_used,quota_period,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (user_id, email, role, "active", self.default_quota, 0,
                     period, now, now),
                )
                conn.execute(
                    "INSERT INTO auth_identities "
                    "(id,user_id,provider,provider_uid,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (uuid.uuid4().hex, user_id, "email", email, now),
                )
            else:
                user_id = user_row["id"]
                if user_row["status"] != "active":
                    raise AuthError("账号已暂停，请联系管理员")
            token = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO auth_sessions "
                "(token_hash,user_id,created_at,expires_at) VALUES (?,?,?,?)",
                (self._digest("session", token), user_id, now,
                 now + session_ttl_s),
            )
            conn.commit()
        user = self.get_user(user_id, now=now)
        if user is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("created user disappeared")
        return user, token

    def get_session_user(self, token: str, *,
                         now: float | None = None) -> AuthUser | None:
        if not token:
            return None
        now = now or time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM auth_sessions WHERE token_hash=? "
                "AND revoked_at IS NULL AND expires_at>?",
                (self._digest("session", token), now),
            ).fetchone()
        return self.get_user(row["user_id"], now=now) if row else None

    def revoke_session(self, token: str, *, now: float | None = None) -> None:
        if not token:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE token_hash=?",
                (now or time.time(), self._digest("session", token)),
            )

    def get_user(self, user_id: str, *,
                 now: float | None = None) -> AuthUser | None:
        now = now or time.time()
        period = self._period(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM users WHERE id=?",
                               (user_id,)).fetchone()
            if not row:
                conn.rollback()
                return None
            if row["quota_period"] != period:
                conn.execute(
                    "UPDATE users SET quota_used=0,quota_period=?,updated_at=? "
                    "WHERE id=?", (period, now, user_id),
                )
                row = conn.execute("SELECT * FROM users WHERE id=?",
                                   (user_id,)).fetchone()
            conn.commit()
        return AuthUser(
            id=row["id"], email=row["email"], role=row["role"],
            status=row["status"], quota_limit=row["quota_limit"],
            quota_used=row["quota_used"], quota_period=row["quota_period"],
        )

    def reserve_quota(self, user_id: str, job_id: str, amount: int, *,
                      now: float | None = None) -> AuthUser:
        if amount <= 0:
            raise ValueError("quota amount must be positive")
        now = now or time.time()
        period = self._period(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM users WHERE id=?",
                               (user_id,)).fetchone()
            if not row or row["status"] != "active":
                raise AuthError("账号不可用")
            used = row["quota_used"] if row["quota_period"] == period else 0
            existing = conn.execute(
                "SELECT 1 FROM quota_ledger WHERE user_id=? AND job_id=? "
                "AND reason='reserve'", (user_id, job_id),
            ).fetchone()
            if existing:
                conn.commit()
                return self.get_user(user_id, now=now)  # type: ignore[return-value]
            if row["role"] != "admin" and used + amount > row["quota_limit"]:
                raise AuthError(
                    f"本月生成额度不足，还剩 {max(0, row['quota_limit']-used)}",
                )
            conn.execute(
                "UPDATE users SET quota_used=?,quota_period=?,updated_at=? "
                "WHERE id=?", (used + amount, period, now, user_id),
            )
            conn.execute(
                "INSERT INTO quota_ledger "
                "(id,user_id,job_id,delta,reason,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (uuid.uuid4().hex, user_id, job_id, amount, "reserve", now),
            )
            conn.commit()
        return self.get_user(user_id, now=now)  # type: ignore[return-value]

    def refund_quota(self, user_id: str, job_id: str, *,
                     now: float | None = None) -> AuthUser:
        now = now or time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reserve = conn.execute(
                "SELECT delta FROM quota_ledger WHERE user_id=? AND job_id=? "
                "AND reason='reserve'", (user_id, job_id),
            ).fetchone()
            refunded = conn.execute(
                "SELECT 1 FROM quota_ledger WHERE user_id=? AND job_id=? "
                "AND reason='refund'", (user_id, job_id),
            ).fetchone()
            if reserve and not refunded:
                conn.execute(
                    "UPDATE users SET quota_used=MAX(0,quota_used-?),updated_at=? "
                    "WHERE id=?", (reserve["delta"], now, user_id),
                )
                conn.execute(
                    "INSERT INTO quota_ledger "
                    "(id,user_id,job_id,delta,reason,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex, user_id, job_id, -reserve["delta"],
                     "refund", now),
                )
            conn.commit()
        return self.get_user(user_id, now=now)  # type: ignore[return-value]

    def bind_identity(self, user_id: str, provider: str, provider_uid: str,
                      provider_openid: str | None = None,
                      *, now: float | None = None) -> None:
        """Bind a future OAuth identity, e.g. WeChat UnionID, to one user."""
        provider = provider.strip().lower()
        provider_uid = provider_uid.strip()
        if not provider or not provider_uid:
            raise AuthError("第三方身份无效")
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO auth_identities "
                    "(id,user_id,provider,provider_uid,provider_openid,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex, user_id, provider, provider_uid,
                     provider_openid, now or time.time()),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthError("该登录身份已绑定其他账号") from exc

    def list_users(self) -> list[AuthUser]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM users ORDER BY created_at").fetchall()
        return [user for row in rows
                if (user := self.get_user(row["id"])) is not None]

    def update_user_controls(self, user_id: str, *,
                             quota_limit: int | None = None,
                             status: str | None = None,
                             now: float | None = None) -> AuthUser:
        """Admin control for bounded quota and account suspension."""
        if quota_limit is not None and not 0 <= quota_limit <= 100_000:
            raise AuthError("额度必须在 0 到 100000 之间")
        if status is not None and status not in ("active", "paused"):
            raise AuthError("账号状态无效")
        assignments = []
        values: list[object] = []
        if quota_limit is not None:
            assignments.append("quota_limit=?")
            values.append(quota_limit)
        if status is not None:
            assignments.append("status=?")
            values.append(status)
        if not assignments:
            user = self.get_user(user_id, now=now)
            if user is None:
                raise AuthError("账号不存在")
            return user
        assignments.append("updated_at=?")
        values.append(now or time.time())
        values.append(user_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE users SET {','.join(assignments)} WHERE id=?", values,
            )
            if cursor.rowcount != 1:
                raise AuthError("账号不存在")
        user = self.get_user(user_id, now=now)
        if user is None:  # pragma: no cover - update invariant
            raise AuthError("账号不存在")
        return user


def store_from_env(root: Path) -> AuthStore:
    path = Path(os.environ.get("STUDIO_DB_PATH", root / "data" / "studio.db"))
    secret = os.environ.get("AUTH_SECRET", "")
    if not secret:
        # Development-only stable secret. Production activation refuses this in
        # server.py when AUTH_REQUIRED is enabled.
        secret = "studio-development-secret-change-before-production"
    admins = {
        value.strip().lower()
        for value in os.environ.get("ADMIN_EMAILS", "").split(",")
        if value.strip()
    }
    return AuthStore(
        path, secret,
        default_quota=int(os.environ.get("DEFAULT_MONTHLY_QUOTA", "20")),
        admin_emails=admins,
    )
