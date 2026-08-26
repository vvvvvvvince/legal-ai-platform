import type {
  RiskLevel,
  RiskFilter,
  LawReference,
  ReviewRisk,
  ReviewCoverage,
  ReviewConsistencyCheck,
  DocumentQuality,
  DocumentPreflightCheck,
  PartyRole,
  ReviewStyle,
  DeepReviewSettings,
  DeepReviewOutput,
  ContractOverview,
  ContractOverviewResponse,
  IntakeChatMessage,
  IntakeReviewCriteria,
  IntakeChatResponse,
  LegalResearchResponse,
  ReviewResponse,
  Modification,
  FeedbackDecision,
  PreflightDecision,
  ParagraphOption,
  RiskWithKey,
  RiskLocationCandidate,
  ReviewStage,
  IntakeConversationStep,
  DeepReviewFormSettings,
} from "../domain/reviewTypes";

const unsupportedEditorCharacters = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g;

const EMPTY_INTAKE_CRITERIA: IntakeReviewCriteria = {
  party_role: null,
  other_party_role: "",
  deal_priorities: [],
  focus_areas: [],
  review_style: "protective",
  business_context: "",
  non_negotiables: "",
  special_requirements: [],
  additional_notes: [],
};
export function apiHeaders() {
  const headers: Record<string, string> = {
    "X-Tenant-ID": import.meta.env.VITE_TENANT_ID || "local"
  };
  const apiToken = import.meta.env.VITE_API_AUTH_TOKEN;
  if (apiToken) {
    headers["X-API-Token"] = apiToken;
  }
  return headers;
}

export async function getContractOverview(file: File): Promise<ContractOverviewResponse> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch("/api/overview", {
    method: "POST",
    headers: apiHeaders(),
    body: formData
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `Contract overview request failed with status ${response.status}.`);
  }
  const payload = await response.json() as Partial<ContractOverviewResponse>;
  if (typeof payload.contract_text !== "string" || !payload.contract_text.trim() || !payload.overview) {
    throw new Error("合同概览服务未返回可用的合同文本或概览内容。");
  }
  return {
    filename: typeof payload.filename === "string" ? payload.filename : file.name,
    contract_text: payload.contract_text.replace(unsupportedEditorCharacters, ""),
    overview: {
      contract_type: typeof payload.overview.contract_type === "string" ? payload.overview.contract_type : "待确认",
      summary: typeof payload.overview.summary === "string" ? payload.overview.summary : "已读取合同，请确认我方身份与业务诉求。",
      parties: Array.isArray(payload.overview.parties) ? payload.overview.parties.filter((item): item is string => typeof item === "string") : [],
      transaction_subject: typeof payload.overview.transaction_subject === "string" ? payload.overview.transaction_subject : "待确认",
      key_terms: Array.isArray(payload.overview.key_terms) ? payload.overview.key_terms.filter((item): item is string => typeof item === "string") : [],
      dimensions: Array.isArray(payload.overview.dimensions) ? payload.overview.dimensions.flatMap((item) => {
        if (!item || typeof item !== "object" || typeof item.category !== "string") return [];
        const status = item.status === "stated" || item.status === "partial" || item.status === "not_found" ? item.status : "not_found";
        return [{ category: item.category, status, details: Array.isArray(item.details) ? item.details.filter((detail): detail is string => typeof detail === "string") : [] }];
      }) : [],
      business_flow: Array.isArray(payload.overview.business_flow) ? payload.overview.business_flow.filter((item): item is string => typeof item === "string") : [],
      party_responsibilities: Array.isArray(payload.overview.party_responsibilities) ? payload.overview.party_responsibilities.flatMap((item) => {
        if (!item || typeof item !== "object" || typeof item.party !== "string") return [];
        return [{ party: item.party, responsibilities: Array.isArray(item.responsibilities) ? item.responsibilities.filter((duty): duty is string => typeof duty === "string") : [] }];
      }) : [],
      decision_points: Array.isArray(payload.overview.decision_points) ? payload.overview.decision_points.flatMap((item) => {
        if (!item || typeof item !== "object" || typeof item.topic !== "string") return [];
        return [{ topic: item.topic, contract_position: typeof item.contract_position === "string" ? item.contract_position : "", user_question: typeof item.user_question === "string" ? item.user_question : "" }];
      }) : [],
      clarification_questions: Array.isArray(payload.overview.clarification_questions) ? payload.overview.clarification_questions.filter((item): item is string => typeof item === "string") : [],
      method: payload.overview.method === "model" ? "model" : "fallback",
      warnings: Array.isArray(payload.overview.warnings) ? payload.overview.warnings.filter((item): item is string => typeof item === "string") : []
    },
    document_quality: payload.document_quality ?? null
  };
}

export function normalizeIntakeCriteria(value: unknown): IntakeReviewCriteria {
  if (!value || typeof value !== "object") return { ...EMPTY_INTAKE_CRITERIA };
  const source = value as Record<string, unknown>;
  const list = (key: string, limit: number) => Array.isArray(source[key])
    ? source[key].filter((item): item is string => typeof item === "string" && item.trim().length > 0).slice(0, limit)
    : [];
  return {
    party_role: source.party_role === "party_a" || source.party_role === "party_b" || source.party_role === "other" ? source.party_role : null,
    other_party_role: typeof source.other_party_role === "string" ? source.other_party_role : "",
    deal_priorities: list("deal_priorities", 6),
    focus_areas: list("focus_areas", 8),
    review_style: source.review_style === "balanced" || source.review_style === "material_only" ? source.review_style : "protective",
    business_context: typeof source.business_context === "string" ? source.business_context : "",
    non_negotiables: typeof source.non_negotiables === "string" ? source.non_negotiables : "",
    special_requirements: list("special_requirements", 8),
    additional_notes: list("additional_notes", 5)
  };
}

export async function continueIntakeChat(
  overview: ContractOverviewResponse,
  messages: IntakeChatMessage[],
  criteria: IntakeReviewCriteria,
): Promise<IntakeChatResponse> {
  const response = await fetch("/api/intake/chat", {
    method: "POST",
    headers: { ...apiHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({
      contract_text: overview.contract_text,
      overview: overview.overview,
      messages: messages.map(({ role, content }) => ({ role, content })),
      criteria
    })
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `法务助手对话请求失败（${response.status}）。`);
  }
  const payload = await response.json() as Partial<IntakeChatResponse>;
  if (typeof payload.assistant_message !== "string" || !payload.assistant_message.trim()) {
    throw new Error("法务助手未返回下一步问题，请重试。");
  }
  return {
    assistant_message: payload.assistant_message.trim(),
    quick_replies: Array.isArray(payload.quick_replies)
      ? payload.quick_replies.filter((item): item is string => typeof item === "string" && item.trim().length > 0).slice(0, 4)
      : [],
    suggested_questions: Array.isArray(payload.suggested_questions)
      ? payload.suggested_questions.filter((item): item is string => typeof item === "string" && item.trim().length > 0).slice(0, 4)
      : [],
    criteria: normalizeIntakeCriteria(payload.criteria),
    ready_for_review: payload.ready_for_review === true,
    source: payload.source === "model" ? "model" : "fallback",
    warning: typeof payload.warning === "string" ? payload.warning : null
  };
}

export async function continueLegalResearch(
  messages: IntakeChatMessage[],
  contractContext?: string,
): Promise<LegalResearchResponse> {
  const response = await fetch("/api/legal-research/chat", {
    method: "POST",
    headers: { ...apiHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({
      messages: messages.slice(-12).map(({ role, content }) => ({ role, content })),
      contract_context: contractContext?.slice(0, 12_000) ?? "",
    }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `法规咨询请求失败（${response.status}）。`);
  }
  const payload = await response.json() as Partial<LegalResearchResponse>;
  if (typeof payload.assistant_message !== "string" || !payload.assistant_message.trim()) {
    throw new Error("法规咨询未返回有效回答，请重试。");
  }
  return {
    assistant_message: payload.assistant_message.trim(),
    suggested_questions: Array.isArray(payload.suggested_questions)
      ? payload.suggested_questions.filter((item): item is string => typeof item === "string" && item.trim().length > 0).slice(0, 4)
      : [],
    source: payload.source === "model" ? "model" : "fallback",
    warning: typeof payload.warning === "string" ? payload.warning : null,
  };
}

const legalResearchSignals = [
  "法规", "法条", "法律", "条例", "办法", "规定", "司法解释", "民法典", "公司法", "劳动法",
  "个人信息保护法", "数据安全法", "网络安全法", "知识产权", "法律依据", "第几条", "条文",
];

export function isLegalResearchQuestion(content: string) {
  const normalized = content.replace(/\s+/g, "");
  return legalResearchSignals.some((signal) => normalized.includes(signal));
}

export function normalizeReviewResponse(payload: unknown, fallbackFilename: string): ReviewResponse {
  if (!payload || typeof payload !== "object") {
    throw new Error("审查服务返回了无法识别的数据。");
  }

  const source = payload as Record<string, unknown>;
  if (typeof source.contract_text !== "string" || !source.contract_text.trim()) {
    throw new Error("审查服务未返回可显示的合同正文。");
  }

  if (!Array.isArray(source.risks)) {
    throw new Error("审查服务返回的风险列表格式不正确。");
  }

  const risks: ReviewRisk[] = source.risks.map((entry, index) => {
    if (!entry || typeof entry !== "object") {
      throw new Error(`第 ${index + 1} 条风险数据格式不正确。`);
    }

    const risk = entry as Record<string, unknown>;
    const level = risk.level;
    if (level !== "high" && level !== "medium" && level !== "low") {
      throw new Error(`第 ${index + 1} 条风险缺少有效等级。`);
    }

    const requiredFields = ["item", "original_text", "risk", "suggestion"] as const;
    for (const field of requiredFields) {
      if (typeof risk[field] !== "string") {
        throw new Error(`第 ${index + 1} 条风险缺少 ${field}。`);
      }
    }

    const rawLaws = risk.laws;
    const laws = Array.isArray(rawLaws)
      ? rawLaws.filter((law): law is string => typeof law === "string")
      : typeof rawLaws === "string"
        ? [rawLaws]
        : [];

    return {
      item: risk.item as string,
      level,
      original_text: risk.original_text as string,
      anchor_text: typeof risk.anchor_text === "string" ? risk.anchor_text : null,
      insert_after_text: typeof risk.insert_after_text === "string" ? risk.insert_after_text : null,
      risk: risk.risk as string,
      suggestion: risk.suggestion as string,
      laws,
      source: risk.source === "rule" || risk.source === "combined" ? risk.source : "model",
      evidence_status: risk.evidence_status === "verified" ? "verified" : "needs_manual_review",
      clause_reference: typeof risk.clause_reference === "string" ? risk.clause_reference : null,
      party_impact: typeof risk.party_impact === "string" ? risk.party_impact : null,
      negotiation_level: risk.negotiation_level === "must_modify" || risk.negotiation_level === "negotiable" || risk.negotiation_level === "internal_approval" || risk.negotiation_level === "prohibited"
        ? risk.negotiation_level
        : null,
      minimum_acceptable_text: typeof risk.minimum_acceptable_text === "string" ? risk.minimum_acceptable_text : null,
      law_references: Array.isArray(risk.law_references)
        ? risk.law_references.flatMap((entry): LawReference[] => {
            if (!entry || typeof entry !== "object") return [];
            const item = entry as Record<string, unknown>;
            if (typeof item.label !== "string") return [];
            return [{
              label: item.label,
              official_url: typeof item.official_url === "string" ? item.official_url : null,
              authority: typeof item.authority === "string" ? item.authority : null,
              effectiveness_status: typeof item.effectiveness_status === "string" ? item.effectiveness_status : null,
            }];
          })
        : []
    };
  });

  const coverage = Array.isArray(source.coverage)
    ? source.coverage.flatMap((entry): ReviewCoverage[] => {
        if (!entry || typeof entry !== "object") return [];
        const item = entry as Record<string, unknown>;
        const status = item.status;
        if (typeof item.topic !== "string" || (status !== "checked" && status !== "missing" && status !== "uncertain")) {
          return [];
        }
        return [{
          topic: item.topic,
          status,
          evidence: typeof item.evidence === "string" ? item.evidence : null,
          method: item.method === "model" || item.method === "combined" ? item.method : "rule"
        }];
      })
    : [];

  return {
    filename: typeof source.filename === "string" && source.filename ? source.filename : fallbackFilename,
    contract_type: typeof source.contract_type === "string" ? source.contract_type : null,
    contract_text: source.contract_text.replace(unsupportedEditorCharacters, ""),
    risks,
    review_status: source.review_status === "complete" || source.review_status === "partial" || source.review_status === "needs_manual_review"
      ? source.review_status
      : "needs_manual_review",
    review_summary: typeof source.review_summary === "string" ? source.review_summary : "",
    review_scope: Array.isArray(source.review_scope)
      ? source.review_scope.filter((item): item is string => typeof item === "string")
      : [],
    coverage,
    warnings: Array.isArray(source.warnings)
      ? source.warnings.filter((item): item is string => typeof item === "string")
      : [],
    review_duration_ms: typeof source.review_duration_ms === "number" ? source.review_duration_ms : null,
    policy_version: typeof source.policy_version === "string" ? source.policy_version : "2026.08",
    review_method: source.review_method === "model" || source.review_method === "rule" || source.review_method === "manual"
      ? source.review_method
      : "combined",
    manual_review_required: source.manual_review_required !== false,
    consistency_checks: Array.isArray(source.consistency_checks)
      ? source.consistency_checks.flatMap((entry): ReviewConsistencyCheck[] => {
          if (!entry || typeof entry !== "object") return [];
          const item = entry as Record<string, unknown>;
          const status = item.status;
          if (typeof item.check !== "string" || (status !== "checked" && status !== "warning" && status !== "not_applicable")) return [];
          return [{
            check: item.check,
            status,
            evidence: typeof item.evidence === "string" ? item.evidence : "",
            note: typeof item.note === "string" ? item.note : "",
          }];
        })
      : [],
    document_quality: source.document_quality && typeof source.document_quality === "object"
      ? (() => {
          const item = source.document_quality as Record<string, unknown>;
          const status = item.status;
          if (item.kind !== "docx" && item.kind !== "pdf") return null;
          if (status !== "searchable" && status !== "partial" && status !== "scanned" && status !== "not_applicable") return null;
          return {
            kind: item.kind,
            status,
            pages: typeof item.pages === "number" ? item.pages : null,
            extracted_chars: typeof item.extracted_chars === "number" ? item.extracted_chars : 0,
            average_chars_per_page: typeof item.average_chars_per_page === "number" ? item.average_chars_per_page : null,
            ocr_detected: item.ocr_detected === true,
            note: typeof item.note === "string" ? item.note : "",
          };
        })()
      : null
    , preflight_checks: Array.isArray(source.preflight_checks)
      ? source.preflight_checks.flatMap((entry): DocumentPreflightCheck[] => {
          if (!entry || typeof entry !== "object") return [];
          const item = entry as Record<string, unknown>;
          const category = item.category;
          const checkStatus = item.status;
          if (
            (category !== "structure" && category !== "scope" && category !== "punctuation" && category !== "typo")
            || (checkStatus !== "passed" && checkStatus !== "warning")
            || typeof item.title !== "string"
          ) return [];
          return [{
            category,
            title: item.title,
            status: checkStatus,
            evidence: typeof item.evidence === "string" ? item.evidence : "",
            suggestion: typeof item.suggestion === "string" ? item.suggestion : "",
            original_text: typeof item.original_text === "string" ? item.original_text : null,
            replacement_text: typeof item.replacement_text === "string" ? item.replacement_text : null,
            auto_fixable: item.auto_fixable === true,
          }];
        })
      : [],
    deep_review: source.deep_review && typeof source.deep_review === "object"
      ? (() => {
          const item = source.deep_review as Record<string, unknown>;
          const state = item.state;
          const conclusion = item.overall_conclusion;
          if ((state !== "completed" && state !== "needs_manual_review") || (conclusion !== "可签" && conclusion !== "有条件可签" && conclusion !== "不建议签" && conclusion !== "待确认")) return null;
          const toStrings = (value: unknown) => Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
          const keyFacts = Array.isArray(item.key_facts) ? item.key_facts.flatMap((entry) => {
            if (!entry || typeof entry !== "object") return [];
            const fact = entry as Record<string, unknown>;
            if (typeof fact.item !== "string") return [];
            return [{ item: fact.item, contract_term: typeof fact.contract_term === "string" ? fact.contract_term : "", conclusion: typeof fact.conclusion === "string" ? fact.conclusion : "" }];
          }) : [];
          const negotiations = Array.isArray(item.negotiation_items) ? item.negotiation_items.flatMap((entry) => {
            if (!entry || typeof entry !== "object") return [];
            const negotiation = entry as Record<string, unknown>;
            if (typeof negotiation.topic !== "string") return [];
            return [{ topic: negotiation.topic, target: typeof negotiation.target === "string" ? negotiation.target : "", minimum_acceptable: typeof negotiation.minimum_acceptable === "string" ? negotiation.minimum_acceptable : "", owner: typeof negotiation.owner === "string" ? negotiation.owner : "法务/业务确认" }];
          }) : [];
          return { state, overall_conclusion: conclusion, executive_summary: typeof item.executive_summary === "string" ? item.executive_summary : "", key_facts: keyFacts, missing_clauses: toStrings(item.missing_clauses), negotiation_items: negotiations, clarification_questions: toStrings(item.clarification_questions), settings_note: typeof item.settings_note === "string" ? item.settings_note : "" };
        })()
      : null
  };
}
