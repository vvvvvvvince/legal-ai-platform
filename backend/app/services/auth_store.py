from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    username: str
    display_name: str
    workspace_id: str = "shared"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("password must contain at least 8 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$16384$8$1${}${}".format(
        salt.hex(), digest.hex()
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class AuthStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    workspace_id TEXT NOT NULL DEFAULT 'shared',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL REFERENCES users(user_id),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    client_key TEXT NOT NULL,
                    attempted_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup
                    ON login_attempts(username, client_key, attempted_at);
                """
            )

    @staticmethod
    def _identity(row: sqlite3.Row | None) -> UserIdentity | None:
        if row is None:
            return None
        return UserIdentity(
            user_id=row["user_id"],
            username=row["username"],
            display_name=row["display_name"],
            workspace_id=row["workspace_id"],
        )

    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        workspace_id: str = "shared",
    ) -> UserIdentity:
        normalized = username.strip().lower()
        if not normalized or not display_name.strip():
            raise ValueError("username and display_name are required")
        now = _now().isoformat()
        user_id = secrets.token_hex(16)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(
                    user_id, username, display_name, password_hash,
                    workspace_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    normalized,
                    display_name.strip(),
                    _hash_password(password),
                    workspace_id,
                    now,
                    now,
                ),
            )
        return UserIdentity(user_id, normalized, display_name.strip(), workspace_id)

    def authenticate(self, username: str, password: str) -> UserIdentity | None:
        normalized = username.strip().lower()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? AND is_active = 1",
                (normalized,),
            ).fetchone()
        if row is None or not _verify_password(password, row["password_hash"]):
            return None
        return self._identity(row)

    def has_active_users(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM users WHERE is_active = 1 LIMIT 1"
            ).fetchone()
        return row is not None

    def create_session(self, user_id: str, lifetime_seconds: int) -> str:
        if lifetime_seconds <= 0:
            raise ValueError("session lifetime must be positive")
        raw_token = secrets.token_urlsafe(32)
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(
                    session_id, token_hash, user_id, created_at, expires_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    secrets.token_hex(16),
                    hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                    user_id,
                    now.isoformat(),
                    (now + timedelta(seconds=lifetime_seconds)).isoformat(),
                    now.isoformat(),
                ),
            )
        return raw_token

    def get_identity(self, raw_token: str | None) -> UserIdentity | None:
        if not raw_token:
            return None
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        now = _now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT u.*
                FROM sessions s JOIN users u ON u.user_id = s.user_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL
                  AND s.expires_at > ? AND u.is_active = 1
                """,
                (token_hash, now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            touch_before = (now - timedelta(minutes=5)).isoformat()
            connection.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ? AND last_seen_at < ?",
                (now.isoformat(), token_hash, touch_before),
            )
        return self._identity(row)

    def revoke_session(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (_now().isoformat(), token_hash),
            )

    def record_login_failure(
        self,
        username: str,
        client_key: str,
        *,
        window_seconds: int = 300,
    ) -> None:
        now = _now()
        cutoff = (now - timedelta(seconds=window_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
            connection.execute(
                "INSERT INTO login_attempts(username, client_key, attempted_at) VALUES (?, ?, ?)",
                (username.strip().lower(), client_key, now.isoformat()),
            )

    def login_allowed(
        self,
        username: str,
        client_key: str,
        *,
        max_attempts: int = 5,
        window_seconds: int = 300,
    ) -> bool:
        cutoff = (_now() - timedelta(seconds=window_seconds)).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS total FROM login_attempts
                   WHERE username = ? AND client_key = ? AND attempted_at >= ?""",
                (username.strip().lower(), client_key, cutoff),
            ).fetchone()
        return int(row["total"]) < max_attempts

    def clear_login_failures(self, username: str, client_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM login_attempts WHERE username = ? AND client_key = ?",
                (username.strip().lower(), client_key),
            )

    def cleanup_sessions(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                (_now().isoformat(),),
            )
        return cursor.rowcount
