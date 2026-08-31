from __future__ import annotations

import os
from pathlib import Path


DEFAULT_REVIEW_JOB_FILES_DIR = "data/review_files"


def review_job_files_dir() -> Path:
    return Path(os.getenv("REVIEW_JOB_FILES_DIR", DEFAULT_REVIEW_JOB_FILES_DIR))


def _source_docx_path(job_id: str) -> Path:
    safe_id = Path(job_id).name
    return review_job_files_dir() / f"{safe_id}.docx"


def has_source_docx(job_id: str) -> bool:
    return _source_docx_path(job_id).is_file()


def save_source_docx(job_id: str, file_bytes: bytes) -> None:
    path = _source_docx_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(file_bytes)


def read_source_docx(job_id: str) -> bytes | None:
    path = _source_docx_path(job_id)
    if not path.is_file():
        return None
    return path.read_bytes()
