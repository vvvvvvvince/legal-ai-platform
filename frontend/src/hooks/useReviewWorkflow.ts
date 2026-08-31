import { useCallback, useEffect, useState } from "react";
import {
  cancelReviewJob,
  createReviewJob,
  getReviewJob,
  shouldPollReviewJob,
  uploadReviewJobSourceDocx,
  waitForReviewJob,
  type ReviewJob,
} from "../api/reviewJobs";
import type { ContractOverviewResponse, DeepReviewFormSettings } from "../domain/reviewTypes";
import { describeSourceDocxFailure } from "../reviewUtils";

const STORAGE_KEY = "legal-ai-review-job";

export function useReviewWorkflow() {
  const [activeJob, setActiveJob] = useState<ReviewJob | null>(null);

  useEffect(() => {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    try {
      const saved = JSON.parse(raw) as { job_id?: string };
      if (!saved.job_id) return;
      void getReviewJob(saved.job_id)
        .then((job) => {
          setActiveJob(job);
          if (job.status === "succeeded" || job.status === "failed" || job.status === "cancelled") window.localStorage.removeItem(STORAGE_KEY);
        })
        .catch(() => window.localStorage.removeItem(STORAGE_KEY));
    } catch {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  }, []);

  const submitDeepReview = useCallback(async (
    overview: ContractOverviewResponse,
    settings: DeepReviewFormSettings,
    sourceFile?: File | null,
  ) => {
    const idempotencyKey = crypto.randomUUID();
    const queued = await createReviewJob({
      filename: overview.filename,
      contract_text: overview.contract_text,
      settings,
      document_quality: overview.document_quality ?? null,
    }, idempotencyKey);
    setActiveJob(queued);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
      job_id: queued.job_id,
      filename: overview.filename,
      idempotency_key: idempotencyKey,
    }));
    let sourceDocxWarning: string | null = null;
    if (sourceFile && sourceFile.name.toLowerCase().endsWith(".docx")) {
      try {
        await uploadReviewJobSourceDocx(queued.job_id, sourceFile);
      } catch (uploadError) {
        sourceDocxWarning = describeSourceDocxFailure(uploadError instanceof Error ? uploadError.message : null);
      }
    }
    const completed = await waitForReviewJob(queued.job_id, { onUpdate: setActiveJob });
    window.localStorage.removeItem(STORAGE_KEY);
    return { job: completed, sourceDocxWarning };
  }, []);

  const cancelActiveJob = useCallback(async () => {
    if (!activeJob || !shouldPollReviewJob(activeJob.status)) return;
    setActiveJob(await cancelReviewJob(activeJob.job_id));
  }, [activeJob]);

  const selectJob = useCallback((job: ReviewJob | null) => {
    setActiveJob(job);
  }, []);

  return { activeJob, submitDeepReview, cancelActiveJob, selectJob };
}
