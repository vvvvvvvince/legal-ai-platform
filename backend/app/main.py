import os
import json
import secrets
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
from app.services.review_jobs import IdempotencyConflict, ReviewJob, ReviewJobStore, ReviewJobWorker

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
API_VERSION = "2026.08.18-chat-intake"
DEFAULT_REVIEW_JOB_DB = "data/review_jobs.sqlite3"
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
    password: str

app = FastAPI(
    title="Legal AI Platform API",
    version="0.1.0",
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
        return await call_next(request)
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


def _job_summary(job: ReviewJob) -> dict[str, object]:
    summary: dict[str, object] = {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "updated_at": job.updated_at,
        "attempt_count": job.attempt_count,
    }
    if job.status == "succeeded":
        summary["result"] = job.result
    if job.status in {"failed", "cancelled"}:
        summary["error"] = job.error
    return summary


@app.on_event("startup")
def start_review_job_worker() -> None:
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
        )
        worker.start()
        app.state.review_job_worker = worker


@app.on_event("shutdown")
def stop_review_job_worker() -> None:
    worker = getattr(app.state, "review_job_worker", None)
    if worker is not None:
        worker.stop()
    app.state.review_job_worker = None


def _identity_payload(identity: RequestIdentity) -> dict[str, str]:
    return {
        "user_id": identity.user_id,
        "username": identity.username,
        "display_name": identity.display_name,
        "workspace_id": identity.workspace_id,
    }


@app.post("/api/auth/login")
def login(request: LoginRequest, response: Response) -> dict[str, str]:
    store = AuthStore(auth_db_path())
    identity = store.authenticate(request.username, request.password)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误。")
    lifetime = max(300, int(os.getenv("SESSION_LIFETIME_SECONDS", str(8 * 3600))))
    token = store.create_session(identity.user_id, lifetime)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=lifetime,
        httponly=True,
        samesite="lax",
        secure=os.getenv("APP_ENV", "development").lower() in {"production", "prod"}
        or os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"},
        path="/",
    )
    return _identity_payload(RequestIdentity.from_user(identity))


@app.get("/api/auth/session")
def current_session(request: Request) -> dict[str, str]:
    identity = identity_from_cookie(request.cookies.get(SESSION_COOKIE_NAME))
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录。")
    return _identity_payload(identity)


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    store = AuthStore(auth_db_path())
    store.revoke_session(request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "ok"}


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
    return _job_summary(job)


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
    x_api_token: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> StreamingResponse:
    require_request_identity(x_api_token, x_tenant_id)
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
    return {"status": "recorded"}
