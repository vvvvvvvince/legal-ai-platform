"""Read-only local sources for hybrid contract review.

Approved rules and SOP material are separated from historical edits so a past
project decision can never silently become a mandatory company requirement.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


def _data_root() -> Path:
    configured = os.getenv("LOCAL_REVIEW_DATA_ROOT")
    if configured:
        return Path(configured)
    # .../法务/legal-ai-platform-git/backend/app/services -> .../法务
    return Path(__file__).resolve().parents[4]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


@lru_cache(maxsize=1)
def _approved_rules() -> list[dict[str, Any]]:
    return [
        rule for rule in _read_jsonl(_data_root() / "data" / "approved_rules.jsonl")
        if str(rule.get("status", "")).lower() == "approved"
    ]


@lru_cache(maxsize=1)
def _history_cases() -> list[dict[str, Any]]:
    return _read_jsonl(_data_root() / "data" / "aggregated" / "review_cases_aggregated.jsonl")


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    latin = set(re.findall(r"[a-z][a-z0-9_-]{2,}", lowered))
    # Chinese two-character terms make a small, dependency-free local matcher.
    compact = re.sub(r"\s+", "", re.sub(r"[^\u4e00-\u9fff]", "", text))
    chinese = {compact[index:index + 2] for index in range(max(0, len(compact) - 1))}
    return latin | chinese


def _case_score(query_terms: set[str], case: dict[str, Any]) -> int:
    corpus = " ".join(str(case.get(key) or "") for key in ("section_title", "original_clause", "revised_clause", "comment"))
    return len(query_terms & _terms(corpus))


def local_review_context(contract_text: str, *, limit: int = 4) -> dict[str, Any]:
    """Return compact, source-traceable local evidence without any network call."""
    root = _data_root()
    sop_path = root / "references" / "contract_review_sop_v1.md"
    sop_text = sop_path.read_text(encoding="utf-8") if sop_path.is_file() else ""
    sop_approved = "已批准" in sop_text and "生效" in sop_text
    rules = _approved_rules()
    query_terms = _terms(contract_text)
    ranked = sorted(
        ((-_case_score(query_terms, case), index, case) for index, case in enumerate(_history_cases())),
        key=lambda row: (row[0], row[1]),
    )
    cases = []
    for negative_score, _, case in ranked:
        score = -negative_score
        if score <= 0 or len(cases) >= limit:
            continue
        cases.append({
            "case_id": str(case.get("aggregate_id") or case.get("document_id") or ""),
            "section_title": str(case.get("section_title") or "未标注条款标题"),
            "original_clause": str(case.get("original_clause") or "")[:900],
            "revised_clause": str(case.get("revised_clause") or "")[:900],
            "comment": str(case.get("comment") or "")[:300],
            "source_file": str(case.get("source_file") or ""),
            "source_locator": str((case.get("source_locators") or [""])[0]),
            "match_score": score,
        })
    return {
        "approved_rules": [
            {
                "rule_id": str(rule.get("rule_id") or ""),
                "rule_name": str(rule.get("rule_name") or ""),
                "rule_type": str(rule.get("rule_type") or "mandatory"),
                "trigger_conditions": str(rule.get("trigger_conditions") or "")[:500],
                "required_action": str(rule.get("required_action") or "")[:500],
                "source_file": str(root / "data" / "approved_rules.jsonl"),
            }
            for rule in rules
        ],
        "sop": {
            "approved": sop_approved,
            "source_file": str(sop_path),
            "excerpt": sop_text[:1_500] if sop_approved else "",
        },
        "historical_cases": cases,
    }


def format_local_context_for_model(context: dict[str, Any]) -> str:
    """Label authority clearly before optional use in a hybrid model prompt."""
    rules = context.get("approved_rules", [])
    cases = context.get("historical_cases", [])
    lines = ["本地审核依据（仅供辅助，来源均可追溯）："]
    if rules:
        lines.append("【正式规则：已批准，优先适用】")
        for rule in rules[:8]:
            lines.append(f"- {rule['rule_id']} {rule['rule_name']}：触发={rule['trigger_conditions']}；处理={rule['required_action']}")
    if cases:
        lines.append("【历史审核习惯参考：不是强制规则】")
        for case in cases:
            lines.append(f"- {case['case_id']}：{case['section_title']}；历史修改={case['revised_clause'][:280]}；来源={case['source_file']}#{case['source_locator']}")
    if not rules and not cases:
        lines.append("未检索到与当前合同相关的本地依据；不得虚构来源。")
    return "\n".join(lines)
