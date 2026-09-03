import { formatApiErrorDetail } from "./errorDetails";

export type ReviewJobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export type ReviewJobRequest = {
  filename?: string;
  contract_text?: string;
  settings?: unknown;
  document_quality?: unknown;
};

export type ReviewJob = {
  job_id: string;
  job_type: string;
  status: ReviewJobStatus;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at: string;
  attempt_count: number;
  filename?: string | null;
  created_by_display_name?: string | null;
  has_source_docx?: boolean;
  request?: ReviewJobRequest;
  result?: unknown;
  error?: string | null;
};

export type ReviewModificationSuperseded = {
  modification_id: string;
  actor_display_name: string;
  modification: ReviewModificationPayload;
};

export type ReviewModificationPayload = {
  item?: string;
  risk_key?: string;
  original: string;
  modified: string;
  revision_id?: string;
  anchor_text?: string | null;
  insert_after_text?: string | null;
  paragraph_context?: string | null;
};

export type ReviewModification = {
  modification_id: string;
  job_id: string;
  status: "active" | "superseded" | "reverted";
  modification: ReviewModificationPayload;
  actor_user_id: string;
  actor_display_name: string;
  created_at: string;
  updated_at: string;
  superseded?: ReviewModificationSuperseded | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

export function normalizeReviewJob(payload: unknown): ReviewJob {
  if (!isRecord(payload) || typeof payload.job_id !== "string") {
    throw new Error("审查任务返回了无法识别的数据。");
  }
  const status = payload.status;
  if (status !== "queued" && status !== "running" && status !== "succeeded" && status !== "failed" && status !== "cancelled") {
    throw new Error("审查任务返回了无效状态。");
  }
  return {
    job_id: payload.job_id,
    job_type: typeof payload.job_type === "string" ? payload.job_type : "deep_review",
    status,
    created_at: typeof payload.created_at === "string" ? payload.created_at : "",
    started_at: typeof payload.started_at === "string" ? payload.started_at : null,
    finished_at: typeof payload.finished_at === "string" ? payload.finished_at : null,
    updated_at: typeof payload.updated_at === "string" ? payload.updated_at : "",
    attempt_count: typeof payload.attempt_count === "number" ? payload.attempt_count : 0,
    filename: typeof payload.filename === "string" ? payload.filename : null,
    created_by_display_name: typeof payload.created_by_display_name === "string" ? payload.created_by_display_name : null,
    has_source_docx: payload.has_source_docx === true,
    request: isRecord(payload.request) ? {
      filename: typeof payload.request.filename === "string" ? payload.request.filename : undefined,
      contract_text: typeof payload.request.contract_text === "string" ? payload.request.contract_text : undefined,
      settings: payload.request.settings,
      document_quality: payload.request.document_quality,
    } : undefined,
    result: payload.result,
    error: typeof payload.error === "string" ? payload.error : null,
  };
}

export function normalizeReviewModification(payload: unknown): ReviewModification {
  if (!isRecord(payload) || typeof payload.modification_id !== "string" || typeof payload.job_id !== "string") {
    throw new Error("修改记录返回了无法识别的数据。");
  }
  const status = payload.status;
  const modification = payload.modification;
  if ((status !== "active" && status !== "superseded" && status !== "reverted") || !isRecord(modification)
    || typeof modification.original !== "string" || typeof modification.modified !== "string"
    || typeof payload.actor_user_id !== "string" || typeof payload.actor_display_name !== "string") {
    throw new Error("修改记录返回了无效状态。");
  }
  return {
    modification_id: payload.modification_id,
    job_id: payload.job_id,
    status,
    modification: {
      ...(typeof modification.item === "string" ? { item: modification.item } : {}),
      ...(typeof modification.risk_key === "string" ? { risk_key: modification.risk_key } : {}),
      original: modification.original,
      modified: modification.modified,
      ...(typeof modification.revision_id === "string" ? { revision_id: modification.revision_id } : {}),
      ...(typeof modification.anchor_text === "string" ? { anchor_text: modification.anchor_text } : {}),
      ...(typeof modification.insert_after_text === "string" ? { insert_after_text: modification.insert_after_text } : {}),
      ...(typeof modification.paragraph_context === "string" ? { paragraph_context: modification.paragraph_context } : {}),
    },
    actor_user_id: payload.actor_user_id,
    actor_display_name: payload.actor_display_name,
    created_at: typeof payload.created_at === "string" ? payload.created_at : "",
    updated_at: typeof payload.updated_at === "string" ? payload.updated_at : "",
    superseded: isRecord(payload.superseded)
      && typeof payload.superseded.modification_id === "string"
      && typeof payload.superseded.actor_display_name === "string"
      && isRecord(payload.superseded.modification)
      && typeof payload.superseded.modification.original === "string"
      && typeof payload.superseded.modification.modified === "string"
      ? {
        modification_id: payload.superseded.modification_id,
        actor_display_name: payload.superseded.actor_display_name,
        modification: payload.superseded.modification as ReviewModificationPayload,
      }
      : null,
  };
}

export function shouldPollReviewJob(status: ReviewJobStatus): boolean {
  return status === "queued" || status === "running";
}

export async function waitForReviewJob(
  jobId: string,
  options: { intervalMs?: number; signal?: AbortSignal; onUpdate?: (job: ReviewJob) => void } = {},
): Promise<ReviewJob> {
  const intervalMs = Math.max(250, options.intervalMs ?? 2000);
  while (true) {
    if (options.signal?.aborted) throw new DOMException("Review job polling was cancelled.", "AbortError");
    const job = await getReviewJob(jobId);
    options.onUpdate?.(job);
    if (!shouldPollReviewJob(job.status)) return job;
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(resolve, intervalMs);
      options.signal?.addEventListener("abort", () => {
        window.clearTimeout(timer);
        reject(new DOMException("Review job polling was cancelled.", "AbortError"));
      }, { once: true });
    });
  }
}

export function apiHeaders(): Record<string, string> {
  return {};
}

export async function createReviewJob(request: unknown, idempotencyKey?: string): Promise<ReviewJob> {
  const response = await fetch("/api/review-jobs", {
    method: "POST",
    headers: { ...apiHeaders(), "Content-Type": "application/json", ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}) },
    body: JSON.stringify(request),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new Error(formatApiErrorDetail(payload?.detail, `审查任务创建失败（${response.status}）。`));
  }
  return normalizeReviewJob(await response.json());
}

export async function cancelReviewJob(jobId: string): Promise<ReviewJob> {
  const response = await fetch(`/api/review-jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    headers: apiHeaders(),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new Error(formatApiErrorDetail(payload?.detail, `审查任务取消失败（${response.status}）。`));
  }
  return normalizeReviewJob(await response.json());
}

export async function listReviewJobs(): Promise<ReviewJob[]> {
  const response = await fetch("/api/review-jobs", { headers: apiHeaders() });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new Error(formatApiErrorDetail(payload?.detail, "读取审查记录失败。"));
  }
  const payload = await response.json();
  if (!Array.isArray(payload)) throw new Error("审查记录返回了无法识别的数据。");
  return payload.map(normalizeReviewJob);
}

export async function uploadReviewJobSourceDocx(jobId: string, file: File): Promise<void> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`/api/review-jobs/${encodeURIComponent(jobId)}/source-docx`, {
    method: "PUT",
    headers: apiHeaders(),
    body: formData,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new Error(formatApiErrorDetail(payload?.detail, "保存原始合同失败。"));
  }
}

export async function downloadReviewJobSourceDocx(jobId: string, filename: string): Promise<File> {
  const response = await fetch(`/api/review-jobs/${encodeURIComponent(jobId)}/source-docx`, {
    headers: apiHeaders(),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new Error(formatApiErrorDetail(payload?.detail, "读取原始合同失败。"));
  }
  const blob = await response.blob();
  const safeName = filename.toLowerCase().endsWith(".docx") ? filename : `${filename.replace(/\.[^.]+$/, "")}.docx`;
  return new File([blob], safeName, { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
}

export async function getReviewJob(jobId: string): Promise<ReviewJob> {
  const response = await fetch(`/api/review-jobs/${encodeURIComponent(jobId)}`, {
    headers: apiHeaders(),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new Error(formatApiErrorDetail(payload?.detail, `审查任务查询失败（${response.status}）。`));
  }
  return normalizeReviewJob(await response.json());
}

async function readReviewModification(response: Response, fallback: string): Promise<ReviewModification> {
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new Error(formatApiErrorDetail(payload?.detail, fallback));
  }
  return normalizeReviewModification(await response.json());
}

export async function saveReviewModification(jobId: string, modification: ReviewModificationPayload): Promise<ReviewModification> {
  const response = await fetch(`/api/review-jobs/${encodeURIComponent(jobId)}/modifications`, {
    method: "POST",
    headers: { ...apiHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(modification),
  });
  return readReviewModification(response, "保存修改记录失败。");
}

export async function listReviewModifications(jobId: string): Promise<ReviewModification[]> {
  const response = await fetch(`/api/review-jobs/${encodeURIComponent(jobId)}/modifications`, { headers: apiHeaders() });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
    throw new Error(formatApiErrorDetail(payload?.detail, "读取修改记录失败。"));
  }
  const payload = await response.json();
  if (!Array.isArray(payload)) throw new Error("修改记录返回了无法识别的数据。");
  return payload.map(normalizeReviewModification);
}

export async function revertReviewModification(jobId: string, modificationId: string): Promise<ReviewModification> {
  const response = await fetch(
    `/api/review-jobs/${encodeURIComponent(jobId)}/modifications/${encodeURIComponent(modificationId)}/revert`,
    { method: "POST", headers: apiHeaders() },
  );
  return readReviewModification(response, "撤销修改记录失败。");
}
