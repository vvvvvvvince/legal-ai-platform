import type { DeepReviewOutput, LocalReviewReference, ReviewStage } from "../../domain/reviewTypes";

export function ReviewPanel({ deepReview, localReferences = [], reviewStage }: { deepReview?: DeepReviewOutput | null; localReferences?: LocalReviewReference[]; reviewStage: ReviewStage }) {
  if (reviewStage !== "modification" || !deepReview) return null;
  return (
    <section className="deep-review-result" aria-label="深度审查结论">
      <div className="deep-review-heading"><div><strong>深度审查结论：{deepReview.overall_conclusion}</strong><span>{deepReview.settings_note}</span></div><b>已完成</b></div>
      <p>{deepReview.executive_summary}</p>
      {deepReview.key_facts.length ? <details open><summary>关键条款与结论</summary><div className="deep-result-list">{deepReview.key_facts.map((fact, index) => <article key={`${fact.item}-${index}`}><b>{fact.item}</b><span>{fact.contract_term}</span><small>{fact.conclusion}</small></article>)}</div></details> : null}
      {deepReview.missing_clauses.length ? <details><summary>需补充的条款</summary><ul>{deepReview.missing_clauses.map((item) => <li key={item}>{item}</li>)}</ul></details> : null}
      {deepReview.negotiation_items.length ? <details><summary>谈判清单</summary><div className="deep-result-list">{deepReview.negotiation_items.map((item, index) => <article key={`${item.topic}-${index}`}><b>{item.topic} · {item.owner}</b><span>目标：{item.target}</span><small>底线：{item.minimum_acceptable}</small></article>)}</div></details> : null}
      {deepReview.clarification_questions.length ? <details><summary>待业务确认</summary><ul>{deepReview.clarification_questions.map((item) => <li key={item}>{item}</li>)}</ul></details> : null}
      {localReferences.length ? <details><summary>本地审核依据（{localReferences.length} 项）</summary><div className="deep-result-list">{localReferences.map((reference, index) => <article key={`${reference.reference_type}-${reference.reference_id}-${index}`}><b>{reference.reference_type === "approved_rule" ? "正式规则" : reference.reference_type === "approved_sop" ? "已批准 SOP" : "历史习惯参考"} · {reference.reference_id || reference.title}</b><span>{reference.title}</span><small>{reference.summary || reference.authority_note}</small><small>{reference.source_file}{reference.source_locator ? ` · ${reference.source_locator}` : ""}</small></article>)}</div></details> : null}
    </section>
  );
}
