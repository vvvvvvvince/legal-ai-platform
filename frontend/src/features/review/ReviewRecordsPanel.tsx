import { useEffect, useState } from "react";
import { listReviewJobs, type ReviewJob } from "../../api/reviewJobs";

const statusLabels: Record<ReviewJob["status"], string> = {
  queued: "排队中",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function formatWhen(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

type ReviewRecordsPanelProps = {
  open: boolean;
  onClose: () => void;
  onRecover: (jobId: string) => void;
  recoveringJobId?: string | null;
};

export function ReviewRecordsPanel({ open, onClose, onRecover, recoveringJobId }: ReviewRecordsPanelProps) {
  const [jobs, setJobs] = useState<ReviewJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    void listReviewJobs()
      .then((items) => {
        if (!cancelled) setJobs(items);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "读取审查记录失败。");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [open]);

  if (!open) return null;

  return (
    <div className="review-records-overlay" role="dialog" aria-modal="true" aria-label="审查记录">
      <button className="review-records-backdrop" type="button" aria-label="关闭审查记录" onClick={onClose} />
      <section className="review-records-panel">
        <header className="review-records-header">
          <div>
            <span className="result-context-label">共享工作区</span>
            <h2>审查记录</h2>
            <p>查看同事提交的深度审查任务，点击已完成记录可恢复审查结果与修改。</p>
          </div>
          <button className="secondary-button" type="button" onClick={onClose}>关闭</button>
        </header>

        {loading ? (
          <div className="loading-stack" aria-busy="true">
            <div className="skeleton-line skeleton-title" />
            <div className="skeleton-line" />
            <div className="skeleton-line" />
          </div>
        ) : null}
        {error ? <p className="error-message">{error}</p> : null}

        {!loading && !error && jobs.length === 0 ? (
          <p className="review-records-empty">暂无审查记录。完成一次深度审查后，记录会出现在这里。</p>
        ) : null}

        <ul className="review-records-list">
          {jobs.map((job) => {
            const canRecover = job.status === "succeeded";
            const busy = recoveringJobId === job.job_id;
            return (
              <li key={job.job_id} className={`review-records-item review-records-item-${job.status}`}>
                <div className="review-records-item-main">
                  <strong>{job.filename ?? "未命名合同"}</strong>
                  <span className={`review-records-status review-records-status-${job.status}`}>{statusLabels[job.status]}</span>
                </div>
                <div className="review-records-item-meta">
                  <span>提交人：{job.created_by_display_name ?? "未知"}</span>
                  <span>更新：{formatWhen(job.updated_at)}</span>
                  {job.has_source_docx ? <span>已保存 Word 原件</span> : null}
                </div>
                {canRecover ? (
                  <button
                    className="primary-button review-records-open-btn"
                    type="button"
                    disabled={Boolean(recoveringJobId)}
                    onClick={() => onRecover(job.job_id)}
                  >
                    {busy ? "正在恢复…" : "打开审查"}
                  </button>
                ) : (
                  <p className="review-records-hint">
                    {job.status === "failed" ? (job.error ?? "任务执行失败。") : "任务尚未完成，完成后可恢复工作区。"}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}
