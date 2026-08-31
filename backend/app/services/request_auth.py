from __future__ import annotations

import os
import re
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException, status

from app.services.auth_store import AuthStore, UserIdentity


TENANT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
SESSION_COOKIE_NAME = "legal_ai_session"
_CURRENT_IDENTITY: ContextVar["RequestIdentity | None"] = ContextVar(
    "current_request_identity", default=None
)


@dataclass(frozen=True)
class RequestIdentity:
    user_id: str
    username: str
    display_name: str
    workspace_id: str = "shared"

    @classmethod
    def from_user(cls, user: UserIdentity) -> "RequestIdentity":
        return cls(user.user_id, user.username, user.display_name, user.workspace_id)


def auth_db_path() -> str:
    return os.getenv("AUTH_DB", os.getenv("REVIEW_JOB_DB", "data/auth.sqlite3"))


def auth_store() -> AuthStore:
    return AuthStore(auth_db_path())


def set_current_identity(identity: RequestIdentity | None):
    return _CURRENT_IDENTITY.set(identity)


def reset_current_identity(token) -> None:
    _CURRENT_IDENTITY.reset(token)


def identity_from_cookie(raw_token: str | None) -> RequestIdentity | None:
    identity = auth_store().get_identity(raw_token)
    return RequestIdentity.from_user(identity) if identity else None


def require_request_identity(
    x_api_token: str | None = None,
    x_tenant_id: str | None = None,
) -> RequestIdentity:
    """Return the middleware identity; headers are only a development fallback."""
    identity = _CURRENT_IDENTITY.get()
    if identity is not None:
        return identity
    if auth_store().has_active_users():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效，请重新登录。")

    configured_token = os.getenv("API_AUTH_TOKEN")
    if configured_token and x_api_token != configured_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API token.")
    workspace_id = (x_tenant_id or "shared").strip().lower()
    if not TENANT_ID_PATTERN.fullmatch(workspace_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid workspace identifier.")
    return RequestIdentity("system-legacy", "legacy", "Legacy", workspace_id)
