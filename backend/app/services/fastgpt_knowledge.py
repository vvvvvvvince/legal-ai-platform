"""Read-only FastGPT knowledge-base retrieval.

This integration is optional.  It never writes to FastGPT and failure is
non-blocking so contract review remains available through the local RAG path.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


_DATASETS = (
    ("法务知识库", "FASTGPT_LEGAL_DATASET_ID"),
    ("法规法条知识库", "FASTGPT_LAWS_DATASET_ID"),
)


def _base_url() -> str:
    return os.getenv("FASTGPT_BASE_URL", "").strip().rstrip("/")


def is_fastgpt_knowledge_enabled() -> bool:
    return bool(
        _base_url()
        and os.getenv("FASTGPT_API_KEY", "").strip()
        and all(os.getenv(env_name, "").strip() for _, env_name in _DATASETS)
    )


def _response_rows(payload: Any) -> list[dict[str, Any]]:
    """Tolerate FastGPT response-shape differences across deployed versions."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("list", "data", "searchData", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            rows = _response_rows(value)
            if rows:
                return rows
    return []


def _content(row: dict[str, Any]) -> str:
    for key in ("content", "text", "chunk", "answer", "q", "question"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def retrieve_fastgpt_knowledge(query_text: str, *, limit: int = 3) -> list[dict[str, Any]]:
    """Return compact, attributable snippets from the two configured datasets."""
    query = query_text.strip()
    if not query or not is_fastgpt_knowledge_enabled():
        return []

    timeout = float(os.getenv("FASTGPT_TIMEOUT_SECONDS", "12"))
    verify_tls = os.getenv("FASTGPT_VERIFY_TLS", "true").lower() in {"1", "true", "yes"}
    per_dataset = max(1, min(limit, int(os.getenv("FASTGPT_KNOWLEDGE_LIMIT", str(limit)))))
    headers = {"Authorization": f"Bearer {os.environ['FASTGPT_API_KEY'].strip()}"}
    results: list[dict[str, Any]] = []
    # trust_env=False avoids unintentionally sending internal FastGPT traffic
    # through a desktop/system proxy.
    with httpx.Client(timeout=timeout, verify=verify_tls, trust_env=False) as client:
        for dataset_name, env_name in _DATASETS:
            dataset_id = os.environ[env_name].strip()
            try:
                response = client.post(
                    f"{_base_url()}/core/dataset/searchTest",
                    headers=headers,
                    json={
                        "datasetId": dataset_id,
                        "text": query,
                        "limit": per_dataset,
                        "similarity": 0,
                        "searchMode": "mixedRecall",
                        "usingReRank": True,
                        "datasetSearchUsingExtensionQuery": False,
                    },
                )
                response.raise_for_status()
            except (httpx.HTTPError, TypeError, ValueError):
                continue
            try:
                rows = _response_rows(response.json())
            except ValueError:
                continue
            for row in rows:
                content = _content(row)
                if not content:
                    continue
                results.append(
                    {
                        "dataset_name": dataset_name,
                        "dataset_id": dataset_id,
                        "title": str(row.get("name") or row.get("sourceName") or row.get("collectionName") or dataset_name),
                        "content": content[:1800],
                        "score": row.get("score") or row.get("similarity") or row.get("qScore"),
                    }
                )
    return results[: limit * len(_DATASETS)]


def format_fastgpt_knowledge_for_prompt(snippets: list[dict[str, Any]]) -> str:
    if not snippets:
        return "未配置或未检索到 FastGPT 知识库参考。"
    return "\n".join(
        f"{index}. 【{item['dataset_name']}｜{item['title']}】{item['content']}"
        for index, item in enumerate(snippets, start=1)
    )
