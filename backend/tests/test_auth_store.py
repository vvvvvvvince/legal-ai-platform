from app.services.auth_store import AuthStore


def test_password_hash_only_authenticates_original_password(tmp_path):
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("alice", "Alice", "correct-password")

    assert store.authenticate("alice", "correct-password").user_id == user.user_id
    assert store.authenticate("alice", "wrong-password") is None


def test_session_round_trip_returns_shared_workspace_identity(tmp_path):
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("alice", "Alice", "secret")
    token = store.create_session(user.user_id, 3600)

    assert store.get_identity(token).workspace_id == "shared"
    store.revoke_session(token)
    assert store.get_identity(token) is None
