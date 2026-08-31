import sqlite3

from app.services.auth_store import AuthStore


def test_password_hash_only_authenticates_original_password(tmp_path):
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("alice", "Alice", "correct-password")

    assert store.authenticate("alice", "correct-password").user_id == user.user_id
    assert store.authenticate("alice", "wrong-password") is None


def test_session_round_trip_returns_shared_workspace_identity(tmp_path):
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("alice", "Alice", "secret-123")
    token = store.create_session(user.user_id, 3600)

    assert store.get_identity(token).workspace_id == "shared"
    store.revoke_session(token)
    assert store.get_identity(token) is None


def test_login_failures_are_rate_limited_and_success_can_clear_them(tmp_path):
    store = AuthStore(tmp_path / "auth.sqlite3")
    for _ in range(5):
        store.record_login_failure("alice", "127.0.0.1", window_seconds=300)

    assert store.login_allowed("alice", "127.0.0.1", max_attempts=5, window_seconds=300) is False
    store.clear_login_failures("alice", "127.0.0.1")
    assert store.login_allowed("alice", "127.0.0.1", max_attempts=5, window_seconds=300) is True


def test_session_cleanup_removes_expired_and_revoked_rows(tmp_path):
    path = tmp_path / "auth.sqlite3"
    store = AuthStore(path)
    user = store.create_user("alice", "Alice", "secret-123")
    expired = store.create_session(user.user_id, 3600)
    revoked = store.create_session(user.user_id, 3600)
    store.revoke_session(revoked)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE token_hash IS NOT NULL AND revoked_at IS NULL")

    assert store.cleanup_sessions() == 2
    assert store.get_identity(expired) is None
