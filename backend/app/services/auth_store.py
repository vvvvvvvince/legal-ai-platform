from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    username: str
    phone: str
    display_name: str
    workspace_id: str = "shared"
    is_admin: bool = False


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


def _normalize_phone(phone: str) -> str:
    """Return a consistently stored phone number without exposing it in logs."""
    normalized = re.sub(r"[\s()-]", "", phone.strip())
    if normalized.startswith("00"):
        normalized = "+" + normalized[2:]
    if not re.fullmatch(r"\+?[0-9]{6,20}", normalized):
        raise ValueError("phone must contain 6 to 20 digits, optionally beginning with +")
    return normalized


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
                    phone TEXT,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    workspace_id TEXT NOT NULL DEFAULT 'shared',
                    is_admin INTEGER NOT NULL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS operation_logs (
                    log_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operation_logs_occurred_at
                    ON operation_logs(occurred_at DESC);
                CREATE TABLE IF NOT EXISTS system_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Existing local databases predate phone-based login.  SQLite does
            # not support ADD COLUMN IF NOT EXISTS, so make this migration
            # idempotent for both old and new installations.
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
            if "phone" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN phone TEXT")
            if "is_admin" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique "
                "ON users(phone) WHERE phone IS NOT NULL"
            )

    @staticmethod
    def _identity(row: sqlite3.Row | None) -> UserIdentity | None:
        if row is None:
            return None
        return UserIdentity(
            user_id=row["user_id"],
            username=row["username"],
            phone=row["phone"] or "",
            display_name=row["display_name"],
            workspace_id=row["workspace_id"],
            is_admin=bool(row["is_admin"]),
        )

    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        phone: str | None = None,
        workspace_id: str = "shared",
        is_admin: bool = False,
    ) -> UserIdentity:
        normalized = username.strip().lower()
        normalized_phone = _normalize_phone(phone) if phone else ""
        if not normalized or not display_name.strip():
            raise ValueError("username and display_name are required")
        if not is_admin and not normalized_phone:
            raise ValueError("phone is required for non-admin users")
        now = _now().isoformat()
        user_id = secrets.token_hex(16)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(
                    user_id, username, phone, display_name, password_hash,
                    workspace_id, is_admin, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    normalized,
                    normalized_phone,
                    display_name.strip(),
                    _hash_password(password),
                    workspace_id,
                    int(is_admin),
                    now,
                    now,
                ),
            )
        return UserIdentity(user_id, normalized, normalized_phone, display_name.strip(), workspace_id, is_admin)

    def authenticate(self, username: str, phone: str, password: str) -> UserIdentity | None:
        normalized = username.strip().lower()
        try:
            normalized_phone = _normalize_phone(phone) if phone else ""
        except ValueError:
            return None
        with self._connect() as connection:
            if normalized_phone:
                row = connection.execute(
                    "SELECT * FROM users WHERE username = ? AND phone = ? AND is_active = 1",
                    (normalized, normalized_phone),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM users WHERE username = ? AND is_admin = 1 AND is_active = 1",
                    (normalized,),
                ).fetchone()
        if row is None or not _verify_password(password, row["password_hash"]):
            return None
        return self._identity(row)

    def set_password(self, username: str, password: str) -> None:
        """Reset an existing account password without ever storing it in plain text."""
        normalized = username.strip().lower()
        encoded = _hash_password(password)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ? AND is_active = 1",
                (encoded, _now().isoformat(), normalized),
            )
        if cursor.rowcount != 1:
            raise ValueError("active user not found")

    def replace_active_users(self, users: list[tuple[str, str, str | None, str, bool]]) -> list[UserIdentity]:
        """Deactivate old users, revoke their sessions, then create the supplied accounts.

        Deactivation preserves audit references on review records while making
        old accounts unable to authenticate.
        """
        if len(users) != 3:
            raise ValueError("exactly three replacement users are required")
        normalized_users: list[tuple[str, str, str, str, bool]] = []
        seen_usernames: set[str] = set()
        seen_phones: set[str] = set()
        for username, display_name, phone, password, is_admin in users:
            normalized_username = username.strip().lower()
            normalized_phone = _normalize_phone(phone) if phone else ""
            if not normalized_username or not display_name.strip():
                raise ValueError("username and display_name are required")
            if not is_admin and not normalized_phone:
                raise ValueError("phone is required for non-admin users")
            if normalized_username in seen_usernames or normalized_phone in seen_phones:
                raise ValueError("new usernames and phone numbers must each be unique")
            _hash_password(password)  # validate the password before any account is changed
            seen_usernames.add(normalized_username)
            seen_phones.add(normalized_phone)
            normalized_users.append((normalized_username, display_name.strip(), normalized_phone, password, is_admin))
        now = _now().isoformat()
        created: list[UserIdentity] = []
        with self._connect() as connection:
            # Replace identifying values on retired accounts too, so a later
            # account may safely reuse a username or phone number.
            retired = connection.execute("SELECT user_id FROM users WHERE is_active = 1").fetchall()
            for row in retired:
                connection.execute(
                    "UPDATE users SET username = ?, phone = NULL, display_name = ?, is_active = 0, updated_at = ? WHERE user_id = ?",
                    (f"retired-{row['user_id']}", "已停用用户", now, row["user_id"]),
                )
            connection.execute("UPDATE sessions SET revoked_at = ? WHERE revoked_at IS NULL", (now,))
            for username, display_name, phone, password, is_admin in normalized_users:
                user_id = secrets.token_hex(16)
                connection.execute(
                    """INSERT INTO users(user_id, username, phone, display_name, password_hash, workspace_id, is_admin, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'shared', ?, ?, ?)""",
                    (user_id, username, phone or None, display_name, _hash_password(password), int(is_admin), now, now),
                )
                created.append(UserIdentity(user_id, username, phone, display_name, "shared", is_admin))
        return created

    def log_operation(self, identity: UserIdentity, action: str, detail: str = "") -> None:
        """Append metadata only; password, phone, contract and request data are excluded."""
        # Audit logging must never make a contract review fail because SQLite
        # is briefly locked by another request.
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO operation_logs(log_id, user_id, username, display_name, action, detail, occurred_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (secrets.token_hex(16), identity.user_id, identity.username, identity.display_name, action[:120], detail[:500], _now().isoformat()),
                )
        except sqlite3.Error:
            return

    def cleanup_operation_logs(self, keep: int = 5000) -> int:
        """Bound audit-table growth while retaining the most recent records."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM operation_logs WHERE log_id NOT IN (SELECT log_id FROM operation_logs ORDER BY occurred_at DESC LIMIT ?)",
                (max(100, keep),),
            )
        return cursor.rowcount

    def list_operation_logs(self, limit: int = 200) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT username, display_name, action, detail, occurred_at FROM operation_logs ORDER BY occurred_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_setting(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT setting_value FROM system_settings WHERE setting_key = ?", (key,)
            ).fetchone()
        return str(row["setting_value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO system_settings(setting_key, setting_value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value, updated_at = excluded.updated_at""",
                (key, value, _now().isoformat()),
            )

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
