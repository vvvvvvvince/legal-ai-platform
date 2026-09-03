import os
import json
import secrets
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.schemas.review import (
    ContractOverviewResponse,
    DeepReviewRequest,
    DocumentQuality,
    IntakeChatRequest,
    IntakeChatResponse,
    LegalResearchRequest,
    LegalResearchResponse,
    ReviewFeedback,
    ReviewModificationInput,
    ReviewResponse,
    TextReviewRequest,
)
from app.services.docx_modifier import modify_docx_inplace, parse_modifications
from app.services.docx_parser import extract_docx_text
from app.services.pdf_parser import extract_pdf_document
from app.services.knowledge_import import (
    KnowledgeImportConflict,
    KnowledgeImportError,
    import_snapshot,
)
from app.services.openai_review import review_contract_text
from app.services.deep_review import review_contract_deeply
from app.services.contract_overview import create_contract_overview
from app.services.intake_chat import continue_intake_chat
from app.services.legal_research_chat import continue_legal_research_chat
from app.services.review_report import render_review_report
from app.services.auth_store import AuthStore
from app.services.request_auth import (
    SESSION_COOKIE_NAME,
    RequestIdentity,
    auth_db_path,
    identity_from_cookie,
    require_request_identity,
    reset_current_identity,
    set_current_identity,
)
from app.services.review_job_files import has_source_docx, read_source_docx, save_source_docx
from app.services.review_jobs import IdempotencyConflict, ModificationSaveResult, ReviewJob, ReviewJobStore, ReviewJobWorker, ReviewModification
from app.services.local_review_memory import local_review_context

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
API_VERSION = "2026.08.18-chat-intake"
DEFAULT_REVIEW_JOB_DB = "data/review_jobs.sqlite3"
DEFAULT_PROVIDER_BASE_URL = os.getenv("BAILIAN_BASE_URL")
DEFAULT_PROVIDER_API_KEY = os.getenv("DASHSCOPE_API_KEY")
MAX_KNOWLEDGE_SNAPSHOT_BYTES = int(
    os.getenv("MAX_KNOWLEDGE_SNAPSHOT_BYTES", str(250 * 1024 * 1024))
)
REVIEW_SCOPE_NAMES = {
    "基础质量与合同框架",
    "主体与签约权限", "合同成立与效力", "标的与价格", "付款与发票", "交付与验收",
    "质量与售后", "违约与责任", "解除与终止", "知识产权", "保密与数据",
    "合规与许可", "通知与送达", "争议解决", "附件与文本一致性",
}
PDF_QUALITY_NOTES = {
    "searchable": "PDF 文本可搜索，已完成文本提取。",
    "partial": "PDF 仅部分页面识别出文本，可能存在漏审，需要人工复核。",
    "scanned": "PDF 疑似扫描件，当前仅识别到少量文本，需要 OCR 后复核。",
}


class LoginRequest(BaseModel):
    username: str
    phone: str = ""
    password: str


class ModelConfigRequest(BaseModel):
    model: str


class ModelProfileCreateRequest(BaseModel):
    display_name: str
    model_id: str
    base_url: str
    api_key: str


def bootstrap_users_from_environment(auth: AuthStore) -> None:
    """Create the first three accounts in a fresh production volume only.

    The JSON is read only from the private runtime environment, never returned
    by an API or written to logs.  Once users exist, the value is ignored.
    """
    raw = os.getenv("AUTH_BOOTSTRAP_USERS_JSON", "").strip()
    if auth.has_active_users() or not raw:
        return
    try:
        entries = json.loads(raw)
        users = [
            (entry["username"], entry["display_name"], entry.get("phone"), entry["password"], bool(entry.get("is_admin")))
            for entry in entries
        ]
        auth.replace_active_users(users)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("AUTH_BOOTSTRAP_USERS_JSON is invalid; production startup is blocked.") from exc


def model_profiles() -> dict[str, dict[str, str]]:
    """Named non-default providers; values stay server-side and are never returned."""
    base_url = os.getenv("QWEN3_8_27B_BASE_URL", "").strip()
    api_key = os.getenv("QWEN3_8_27B_API_KEY", "").strip()
    profiles = {"Qwen3.8-27B": {"base_url": base_url, "api_key": api_key}} if base_url and api_key else {}
    stored = AuthStore(auth_db_path()).get_setting("custom_model_profiles")
    try:
        custom_profiles = json.loads(stored) if stored else []
    except json.JSONDecodeError:
        custom_profiles = []
    if isinstance(custom_profiles, list):
        for item in custom_profiles:
            if not isinstance(item, dict):
                continue
            model_id, custom_url, custom_key = item.get("model_id"), item.get("base_url"), item.get("api_key")
            if all(isinstance(value, str) and value for value in (model_id, custom_url, custom_key)):
                profiles[model_id] = {"base_url": custom_url, "api_key": custom_key}
    return profiles


def allowed_models() -> list[str]:
    return ["qwen3.7-flash", *model_profiles().keys()]


def session_cookie_secure() -> bool:
    """Default to secure cookies in production, with an explicit local override."""
    explicit = os.getenv("SESSION_COOKIE_SECURE", "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    return os.getenv("APP_ENV", "development").lower() in {"production", "prod"}


def activate_model(model: str) -> None:
    profile = model_profiles().get(model)
    if profile:
        os.environ["BAILIAN_BASE_URL"] = profile["base_url"]
        os.environ["DASHSCOPE_API_KEY"] = profile["api_key"]
    else:
        if DEFAULT_PROVIDER_BASE_URL:
            os.environ["BAILIAN_BASE_URL"] = DEFAULT_PROVIDER_BASE_URL
        if DEFAULT_PROVIDER_API_KEY:
            os.environ["DASHSCOPE_API_KEY"] = DEFAULT_PROVIDER_API_KEY
    os.environ["BAILIAN_MODEL"] = model


@asynccontextmanager
async def review_job_lifespan(application: FastAPI):
    auth = AuthStore(auth_db_path())
    bootstrap_users_from_environment(auth)
    saved_model = auth.get_setting("active_chat_model")
    if saved_model:
        activate_model(saved_model)
    auth.cleanup_sessions()
    auth.cleanup_operation_logs()
    if os.getenv("APP_ENV", "development").lower() in {"production", "prod"} and not auth.has_active_users():
        raise RuntimeError("No active users configured; run scripts/bootstrap_users.py before production startup.")
    store = _review_job_store()
    store.recover_running_jobs()
    store.cleanup_expired(int(job_runtime_config()["retention_days"]))
    enabled = os.getenv("REVIEW_JOB_WORKER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    if enabled:
        worker = ReviewJobWorker(
            store,
            _execute_deep_review_job,
            poll_seconds=float(os.getenv("REVIEW_JOB_POLL_SECONDS", "1")),
            concurrency=max(1, int(os.getenv("REVIEW_JOB_WORKER_CONCURRENCY", "2"))),
            lease_seconds=float(os.getenv("REVIEW_JOB_LEASE_SECONDS", "120")),
            heartbeat_interval=float(os.getenv("REVIEW_JOB_HEARTBEAT_SECONDS", "30")),
        )
        worker.start()
        application.state.review_job_worker = worker
    try:
        yield
    finally:
        worker = getattr(application.state, "review_job_worker", None)
        if worker is not None:
            worker.stop()
        application.state.review_job_worker = None


app = FastAPI(
    title="Legal AI Platform API",
    version="0.1.0",
    lifespan=review_job_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # The export view uses these response headers to report how many requested
    # redlines were safely written.  Exposing them keeps the UI accurate even
    # when it is served from an allowed development origin instead of the API
    # origin itself.
    expose_headers=[
        "X-Review-Requested-Modifications",
        "X-Review-Applied-Modifications",
        "X-Review-Skipped-Modifications",
    ],
)


@app.middleware("http")
async def attach_request_identity(request: Request, call_next):
    if request.url.path.startswith("/api/auth") or request.url.path == "/health":
        return await call_next(request)
    store = AuthStore(auth_db_path())
    identity = identity_from_cookie(request.cookies.get(SESSION_COOKIE_NAME))
    if store.has_active_users() and identity is None:
        return JSONResponse({"detail": "请先登录。"}, status_code=status.HTTP_401_UNAUTHORIZED)
    token = set_current_identity(identity)
    try:
        response = await call_next(request)
        # Read/poll requests are frequent during a long review.  Recording
        # every one floods the audit log and adds avoidable SQLite contention.
        if identity is not None and request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
            store.log_operation(identity, f"{request.method} {request.url.path}", f"HTTP {response.status_code}")
        return response
    finally:
        reset_current_identity(token)


def job_runtime_config() -> dict[str, object]:
    return {
        "path": os.getenv("REVIEW_JOB_DB", DEFAULT_REVIEW_JOB_DB),
        "retention_days": max(0, int(os.getenv("REVIEW_JOB_RETENTION_DAYS", "7"))),
    }


def _review_job_store() -> ReviewJobStore:
    config = job_runtime_config()
    path = str(config["path"])
    current = getattr(app.state, "review_job_store", None)
    if current is None or str(current.path) != str(Path(path)):
        current = ReviewJobStore(path)
        app.state.review_job_store = current
    return current


def _execute_deep_review_job(request: dict[str, object]) -> dict[str, object]:
    parsed = DeepReviewRequest.model_validate(request)
    result = review_contract_deeply(
        contract_text=parsed.contract_text,
        filename=parsed.filename,
        settings=parsed.settings,
    )
    return result.model_dump(mode="json")


def _job_summary(job: ReviewJob, *, include_request: bool = False) -> dict[str, object]:
    request = job.request if isinstance(job.request, dict) else {}
    filename = request.get("filename") if isinstance(request.get("filename"), str) else None
    summary: dict[str, object] = {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "updated_at": job.updated_at,
        "attempt_count": job.attempt_count,
        "filename": filename,
        "created_by_display_name": job.created_by_display_name,
        "has_source_docx": has_source_docx(job.job_id),
    }
    if include_request:
        summary["request"] = {
            "filename": filename,
            "contract_text": request.get("contract_text"),
            "settings": request.get("settings"),
            "document_quality": request.get("document_quality"),
        }
    if job.status == "succeeded":
        summary["result"] = job.result
    if job.status in {"failed", "cancelled"}:
        summary["error"] = job.error
    return summary


def _modification_summary(
    modification: ReviewModification,
    superseded: ReviewModification | None = None,
) -> dict[str, object]:
    summary = {
        "modification_id": modification.modification_id,
        "job_id": modification.job_id,
        "status": modification.status,
        "risk_key": modification.risk_key,
        "modification": modification.payload,
        "actor_user_id": modification.actor_user_id,
        "actor_display_name": modification.actor_display_name,
        "created_at": modification.created_at,
        "updated_at": modification.updated_at,
    }
    if superseded is not None:
        summary["superseded"] = {
            "modification_id": superseded.modification_id,
            "actor_display_name": superseded.actor_display_name,
            "modification": superseded.payload,
        }
    return summary


def _identity_payload(identity: RequestIdentity) -> dict[str, object]:
    return {
        "user_id": identity.user_id,
        "username": identity.username,
        "phone": identity.phone,
        "display_name": identity.display_name,
        "workspace_id": identity.workspace_id,
        "is_admin": identity.is_admin,
    }


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, object]:
    store = AuthStore(auth_db_path())
    client_key = request.client.host if request.client else "unknown"
    max_attempts = max(1, int(os.getenv("LOGIN_MAX_ATTEMPTS", "5")))
    window_seconds = max(60, int(os.getenv("LOGIN_WINDOW_SECONDS", "300")))
    if not store.login_allowed(
        payload.username,
        client_key,
        max_attempts=max_attempts,
        window_seconds=window_seconds,
    ):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="登录尝试过多，请稍后再试。")
    identity = store.authenticate(payload.username, payload.phone, payload.password)
    if identity is None:
        store.record_login_failure(payload.username, client_key, window_seconds=window_seconds)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误。")
    store.clear_login_failures(payload.username, client_key)
    lifetime = max(300, int(os.getenv("SESSION_LIFETIME_SECONDS", str(8 * 3600))))
    token = store.create_session(identity.user_id, lifetime)
    store.log_operation(identity, "登录", "登录成功")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=lifetime,
        httponly=True,
        samesite="lax",
        secure=session_cookie_secure(),
        path="/",
    )
    return _identity_payload(RequestIdentity.from_user(identity))


@app.get("/api/auth/session")
def current_session(request: Request) -> dict[str, object]:
    identity = identity_from_cookie(request.cookies.get(SESSION_COOKIE_NAME))
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录。")
    return _identity_payload(identity)


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    store = AuthStore(auth_db_path())
    identity = store.get_identity(request.cookies.get(SESSION_COOKIE_NAME))
    if identity:
        store.log_operation(identity, "退出登录", "用户主动退出")
    store.revoke_session(request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "ok"}


@app.get("/api/admin/operation-logs")
def operation_logs(limit: int = 200) -> list[dict[str, str]]:
    identity = require_request_identity()
    if not identity.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可查看操作日志。")
    return AuthStore(auth_db_path()).list_operation_logs(limit)


@app.get("/api/admin/model-config")
def get_model_config() -> dict[str, object]:
    identity = require_request_identity()
    if not identity.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可查看模型配置。")
    active = os.getenv("BAILIAN_MODEL", "qwen-max")
    return {"active_model": active, "allowed_models": allowed_models()}


@app.get("/api/model")
def current_model() -> dict[str, str]:
    require_request_identity()
    return {"active_model": os.getenv("BAILIAN_MODEL", "qwen-max")}


@app.put("/api/admin/model-config")
def update_model_config(payload: ModelConfigRequest) -> dict[str, object]:
    identity = require_request_identity()
    if not identity.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可切换模型。")
    model = payload.model.strip()
    if model not in allowed_models() or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", model):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="模型不在允许的切换列表中。")
    AuthStore(auth_db_path()).set_setting("active_chat_model", model)
    activate_model(model)
    AuthStore(auth_db_path()).log_operation(identity, "切换大模型", f"已切换为 {model}")
    return {"active_model": model, "allowed_models": allowed_models()}


@app.post("/api/admin/model-config/profiles")
def add_model_profile(payload: ModelProfileCreateRequest) -> dict[str, object]:
    identity = require_request_identity()
    if not identity.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可新增模型。")
    display_name = payload.display_name.strip()
    model_id = payload.model_id.strip()
    base_url = payload.base_url.strip().rstrip("/")
    api_key = payload.api_key.strip()
    parsed_url = urlparse(base_url)
    if not display_name or len(display_name) > 80 or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", model_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="模型名称或模型 ID 格式不正确。")
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or parsed_url.username or parsed_url.password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="接口地址必须是完整的 http 或 https 地址。")
    if len(api_key) < 8 or len(api_key) > 500:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="API Key 格式不正确。")
    store = AuthStore(auth_db_path())
    raw = store.get_setting("custom_model_profiles")
    try:
        profiles = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        profiles = []
    if not isinstance(profiles, list):
        profiles = []
    profiles = [item for item in profiles if isinstance(item, dict) and item.get("model_id") != model_id]
    profiles.append({"display_name": display_name, "model_id": model_id, "base_url": base_url, "api_key": api_key})
    store.set_setting("custom_model_profiles", json.dumps(profiles, ensure_ascii=False))
    store.log_operation(identity, "新增大模型", f"已新增 {display_name}（{model_id}）")
    return {"active_model": os.getenv("BAILIAN_MODEL", "qwen-max"), "allowed_models": allowed_models()}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "api_version": API_VERSION}


@app.get("/api/system-status")
def system_status(
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> dict[str, object]:
    """Return configuration health without exposing credentials or document data."""
    require_request_identity(x_api_token, x_tenant_id)

    def configured_url(name: str) -> dict[str, object]:
        value = os.getenv(name, "")
        parsed = urlparse(value)
        return {"endpoint_configured": bool(value), "host": parsed.netloc or None}

    return {
        "status": "ok",
        "api_version": API_VERSION,
        "review_model": {
            "configured": bool(os.getenv("DASHSCOPE_API_KEY") and os.getenv("BAILIAN_MODEL")),
            "model": os.getenv("BAILIAN_MODEL") or None,
            **configured_url("BAILIAN_BASE_URL"),
        },
        "knowledge_base": {
            "configured": bool(os.getenv("QDRANT_COLLECTION")),
            "collection": os.getenv("QDRANT_COLLECTION") or None,
            **configured_url("QDRANT_URL"),
        },
        "pdf_parser": configured_url("PDF_PARSE_URL"),
        "reranker": {"enabled": os.getenv("RERANK_ENABLED", "true").lower() in {"1", "true", "yes"}, **configured_url("RERANK_URL")},
    }


def _require_admin_token(x_admin_token: str | None) -> None:
    configured_token = os.getenv("ADMIN_API_TOKEN")
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Knowledge import is not configured. Set ADMIN_API_TOKEN first.",
        )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, configured_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token.",
        )


def _parse_contract_document(file_bytes: bytes, filename: str) -> tuple[str, DocumentQuality]:
    """Parse an already size-validated contract consistently for every route."""
    is_pdf = filename.lower().endswith(".pdf")
    try:
        if is_pdf:
            parsed_pdf = extract_pdf_document(file_bytes, filename)
            contract_text = parsed_pdf.text
            quality = DocumentQuality(
                kind="pdf",
                status=parsed_pdf.status,
                pages=parsed_pdf.pages,
                extracted_chars=parsed_pdf.extracted_chars,
                average_chars_per_page=parsed_pdf.average_chars_per_page,
                ocr_detected=parsed_pdf.ocr_detected,
                note=PDF_QUALITY_NOTES.get(
                    parsed_pdf.status,
                    "PDF 文本质量未知，审查结论需要人工复核。",
                ),
            )
        else:
            contract_text = extract_docx_text(file_bytes)
            quality = DocumentQuality(
                kind="docx",
                status="not_applicable",
                note="DOCX 使用原生文本解析。",
            )
    except Exception as exc:
        suffix = Path(filename).suffix.lower() or "document"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to parse {suffix} file: {exc}",
        ) from exc

    if not contract_text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No readable text was found in the document.",
        )
    return contract_text, quality


@app.post("/api/admin/knowledge/snapshots", status_code=status.HTTP_201_CREATED)
async def import_knowledge_snapshot(
    file: UploadFile = File(...),
    tenant_id: str = Form(...),
    knowledge_type: str = Form(...),
    x_admin_token: str | None = Header(default=None),
) -> dict[str, int | str]:
    _require_admin_token(x_admin_token)

    if not file.filename or not file.filename.lower().endswith(".snapshot"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .snapshot files are supported.",
        )

    snapshot_bytes = await file.read(MAX_KNOWLEDGE_SNAPSHOT_BYTES + 1)
    if len(snapshot_bytes) > MAX_KNOWLEDGE_SNAPSHOT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Snapshot file exceeds the configured size limit.",
        )

    try:
        result = await run_in_threadpool(
            import_snapshot,
            snapshot_bytes,
            file.filename,
            tenant_id,
            knowledge_type,
        )
    except KnowledgeImportConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except KnowledgeImportError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {
        "collection_name": result.collection_name,
        "bytes_received": result.bytes_received,
    }


@app.post("/api/review-jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_review_job(
    request: DeepReviewRequest,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    identity = require_request_identity(x_api_token, x_tenant_id)
    try:
        job = _review_job_store().create_job(
            tenant_id=identity.workspace_id,
            job_type="deep_review",
            request=request.model_dump(mode="json"),
            created_by_user_id=identity.user_id,
            created_by_display_name=identity.display_name,
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _job_summary(job)


@app.post("/api/review-jobs/{job_id}/cancel")
async def cancel_review_job(
    job_id: str,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> dict[str, object]:
    identity = require_request_identity(x_api_token, x_tenant_id)
    job = _review_job_store().request_cancel(job_id, identity.workspace_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review job not found.")
    return _job_summary(job)


@app.get("/api/review-jobs")
async def list_review_jobs(
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> list[dict[str, object]]:
    identity = require_request_identity(x_api_token, x_tenant_id)
    return [_job_summary(job) for job in _review_job_store().list_jobs(identity.workspace_id)]


@app.get("/api/review-jobs/{job_id}")
async def get_review_job(
    job_id: str,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> dict[str, object]:
    identity = require_request_identity(x_api_token, x_tenant_id)
    job = _review_job_store().get_job(job_id, identity.workspace_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review job not found.")
    return _job_summary(job, include_request=True)


@app.put("/api/review-jobs/{job_id}/source-docx", status_code=status.HTTP_204_NO_CONTENT)
async def upload_review_job_source_docx(
    job_id: str,
    file: UploadFile = File(...),
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> Response:
    identity = require_request_identity(x_api_token, x_tenant_id)
    if _review_job_store().get_job(job_id, identity.workspace_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review job not found.")
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only .docx files are supported.")
    file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if not file_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Uploaded file must be 50 MB or smaller.")
    await run_in_threadpool(save_source_docx, job_id, file_bytes)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/review-jobs/{job_id}/source-docx")
async def download_review_job_source_docx(
    job_id: str,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> StreamingResponse:
    identity = require_request_identity(x_api_token, x_tenant_id)
    job = _review_job_store().get_job(job_id, identity.workspace_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review job not found.")
    file_bytes = await run_in_threadpool(read_source_docx, job_id)
    if file_bytes is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source document not found for this review job.")
    request = job.request if isinstance(job.request, dict) else {}
    filename = request.get("filename") if isinstance(request.get("filename"), str) else "contract.docx"
    if not filename.lower().endswith(".docx"):
        filename = f"{Path(filename).stem}.docx"
    return StreamingResponse(
        iter([file_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/review-jobs/{job_id}/modifications")
async def list_review_job_modifications(
    job_id: str,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> list[dict[str, object]]:
    identity = require_request_identity(x_api_token, x_tenant_id)
    if _review_job_store().get_job(job_id, identity.workspace_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review job not found.")
    return [
        _modification_summary(modification)
        for modification in _review_job_store().list_modifications(job_id, identity.workspace_id)
    ]


@app.post("/api/review-jobs/{job_id}/modifications", status_code=status.HTTP_201_CREATED)
async def save_review_job_modification(
    job_id: str,
    modification: ReviewModificationInput,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> dict[str, object]:
    identity = require_request_identity(x_api_token, x_tenant_id)
    try:
        save_result = _review_job_store().save_modification(
            job_id,
            identity.workspace_id,
            modification.model_dump(exclude_none=True),
            actor_user_id=identity.user_id,
            actor_display_name=identity.display_name,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review job not found.") from exc
    return _modification_summary(save_result.saved, save_result.superseded)


@app.post("/api/review-jobs/{job_id}/modifications/{modification_id}/revert")
async def revert_review_job_modification(
    job_id: str,
    modification_id: str,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> dict[str, object]:
    identity = require_request_identity(x_api_token, x_tenant_id)
    reverted = _review_job_store().revert_modification(
        modification_id,
        identity.workspace_id,
        job_id=job_id,
        actor_user_id=identity.user_id,
        actor_display_name=identity.display_name,
    )
    if reverted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review modification not found.")
    return _modification_summary(reverted)


@app.post("/api/review", response_model=ReviewResponse)
async def review_contract(
    file: UploadFile = File(...),
    review_scope: str | None = Form(default=None),
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> ReviewResponse:
    require_request_identity(x_api_token, x_tenant_id)
    if not file.filename or not file.filename.lower().endswith((".docx", ".pdf")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .docx and .pdf files are supported.",
        )

    file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Uploaded file must be 50 MB or smaller.",
        )

    contract_text, document_quality = _parse_contract_document(file_bytes, file.filename)

    selected_scope = None
    if review_scope:
        try:
            decoded_scope = json.loads(review_scope)
            if isinstance(decoded_scope, list):
                selected_scope = list(dict.fromkeys(item for item in decoded_scope if isinstance(item, str)))
                unknown_scopes = [item for item in selected_scope if item not in REVIEW_SCOPE_NAMES]
                if unknown_scopes:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="review_scope contains unsupported review topics.",
                    )
                if not selected_scope:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Select at least one review topic.",
                    )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="review_scope must be a JSON array.",
                )
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="review_scope must be a JSON array.",
            )

    review_kwargs = {"contract_text": contract_text, "filename": file.filename}
    if selected_scope is not None:
        review_kwargs["selected_scope"] = selected_scope
    review = await run_in_threadpool(review_contract_text, **review_kwargs)
    review.contract_text = contract_text
    review.document_quality = document_quality
    if document_quality.status in {"partial", "scanned"}:
        review.warnings.append(document_quality.note)
        review.manual_review_required = True
        if review.review_status == "complete":
            review.review_status = "partial"
    return review


@app.post("/api/overview", response_model=ContractOverviewResponse)
async def contract_overview(
    file: UploadFile = File(...),
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> ContractOverviewResponse:
    """Parse the upload and return a neutral overview before review settings."""
    require_request_identity(x_api_token, x_tenant_id)
    if not file.filename or not file.filename.lower().endswith((".docx", ".pdf")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only .docx and .pdf files are supported.")
    file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if not file_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Uploaded file must be 50 MB or smaller.")
    contract_text, document_quality = _parse_contract_document(file_bytes, file.filename)
    overview = await run_in_threadpool(create_contract_overview, contract_text)
    return ContractOverviewResponse(filename=file.filename, contract_text=contract_text, overview=overview, document_quality=document_quality)


@app.post("/api/intake/chat", response_model=IntakeChatResponse)
async def continue_intake_conversation(
    request: IntakeChatRequest,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> IntakeChatResponse:
    """Turn a free-form pre-review chat into later deep-review settings."""
    require_request_identity(x_api_token, x_tenant_id)
    return await run_in_threadpool(continue_intake_chat, request)


@app.post("/api/legal-research/chat", response_model=LegalResearchResponse)
async def continue_legal_research_conversation(
    request: LegalResearchRequest,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> LegalResearchResponse:
    """Answer general legal questions without changing the contract review plan."""
    require_request_identity(x_api_token, x_tenant_id)
    return await run_in_threadpool(continue_legal_research_chat, request)


@app.post("/api/review/text", response_model=ReviewResponse)
async def review_contract_text_stage(
    request: TextReviewRequest,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> ReviewResponse:
    """Run the substantive stage against the user's preflight-corrected text."""
    require_request_identity(x_api_token, x_tenant_id)
    selected_scope = list(dict.fromkeys(request.review_scope))
    unknown_scopes = [item for item in selected_scope if item not in REVIEW_SCOPE_NAMES]
    if unknown_scopes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="review_scope contains unsupported review topics.",
        )
    if not selected_scope:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select at least one review topic.",
        )

    review = await run_in_threadpool(
        review_contract_text,
        contract_text=request.contract_text,
        filename=request.filename,
        selected_scope=selected_scope,
    )
    review.contract_text = request.contract_text
    return review


@app.post("/api/review/deep", response_model=ReviewResponse)
async def review_contract_deep_stage(
    request: DeepReviewRequest,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> ReviewResponse:
    require_request_identity(x_api_token, x_tenant_id)
    review = await run_in_threadpool(
        review_contract_deeply,
        contract_text=request.contract_text,
        filename=request.filename,
        settings=request.settings,
    )
    review.document_quality = request.document_quality
    if request.document_quality and request.document_quality.status in {"partial", "scanned"}:
        review.warnings = list(dict.fromkeys([
            *review.warnings,
            request.document_quality.note or "文档文本提取不完整，审查结论需要结合原件人工复核。",
        ]))
        review.manual_review_required = True
        if review.review_status == "complete":
            review.review_status = "partial"
    return review


@app.post("/api/export")
async def export_reviewed_contract(
    file: UploadFile = File(...),
    modifications: str = Form(...),
    export_mode: str = Form(default="tracked"),
    review_job_id: str | None = Form(default=None),
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> StreamingResponse:
    identity = require_request_identity(x_api_token, x_tenant_id)
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .docx files are supported.",
        )

    file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Uploaded file must be 50 MB or smaller.",
        )

    try:
        parsed_modifications = parse_modifications(modifications)
        saved_authors: dict[str, str] = {}
        if review_job_id:
            if _review_job_store().get_job(review_job_id, identity.workspace_id) is None:
                raise ValueError("review_job_id does not belong to this workspace")
            saved_authors = {
                modification.modification_id: modification.actor_display_name
                for modification in _review_job_store().list_modifications(review_job_id, identity.workspace_id)
            }
        for modification in parsed_modifications:
            modification_id = modification.get("modification_id")
            modification["author_display_name"] = (
                saved_authors.get(modification_id)
                if isinstance(modification_id, str)
                else None
            ) or identity.display_name
        if export_mode not in {"tracked", "final"}:
            raise ValueError("export_mode must be 'tracked' or 'final'.")
        export_result = await run_in_threadpool(
            modify_docx_inplace,
            file_bytes,
            parsed_modifications,
            export_mode == "tracked",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to export reviewed .docx file: {exc}",
        ) from exc

    return StreamingResponse(
        iter([export_result.content]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": (
                'attachment; filename="reviewed_contract.docx"'
                if export_mode == "tracked"
                else 'attachment; filename="final_contract.docx"'
            ),
            "X-Review-Requested-Modifications": str(export_result.requested),
            "X-Review-Applied-Modifications": str(export_result.applied),
            "X-Review-Skipped-Modifications": str(export_result.skipped),
        },
    )


@app.post("/api/report", response_class=HTMLResponse)
async def export_review_report(
    review: ReviewResponse = Body(...),
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> HTMLResponse:
    require_request_identity(x_api_token, x_tenant_id)
    return HTMLResponse(render_review_report(review))


@app.post("/api/review/feedback", status_code=status.HTTP_201_CREATED)
async def record_review_feedback(
    feedback: ReviewFeedback,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> dict[str, str]:
    identity = require_request_identity(x_api_token, x_tenant_id)
    path = Path(os.getenv("REVIEW_FEEDBACK_LOG", "logs/review_feedback.jsonl"))
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    record = {
        **feedback.model_dump(),
        "tenant_id": identity.workspace_id,
        "actor_user_id": identity.user_id,
        "actor_display_name": identity.display_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    # Keep human feedback separate from approved rules and SOP material. A
    # personal-memory record is created only after an explicit second flag;
    # neither path can update approved_rules.jsonl.
    feedback_dir = Path(os.getenv("HUMAN_FEEDBACK_DIR", str(Path(__file__).resolve().parents[3] / "data" / "human_feedback")))
    feedback_dir.mkdir(parents=True, exist_ok=True)
    feedback_path = feedback_dir / "feedback.jsonl"
    with feedback_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if feedback.eligible_for_personal_memory and feedback.personal_memory_confirmed:
        personal_memory_path = feedback_dir / "personal_memory.jsonl"
        with personal_memory_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**record, "memory_scope": "personal_only"}, ensure_ascii=False) + "\n")
    return {"status": "recorded"}


@app.get("/api/review/history")
async def get_local_review_history(
    case_id: str | None = None,
    suggestion_id: str | None = None,
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> dict[str, object]:
    """Expose only local, traceable history metadata for human review."""
    require_request_identity(x_api_token, x_tenant_id)
    query = (case_id or suggestion_id or "").strip()
    if not query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="case_id or suggestion_id is required.")
    context = await run_in_threadpool(local_review_context, query, limit=8)
    matches = [case for case in context["historical_cases"] if case["case_id"] == query]
    if not matches and case_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Historical case not found.")
    return {
        "query": query,
        "historical_cases": matches or context["historical_cases"],
        "notice": "历史案例仅反映过往审核习惯，须由人工确认，不构成正式公司规则。",
    }
