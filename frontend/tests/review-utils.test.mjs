import assert from "node:assert/strict";
import test from "node:test";

import {
  applySavedModifications,
  createIdempotencyKey,
  describeSourceDocxFailure,
  editDistance,
  extractHeadingCandidate,
  findFuzzyMatch,
  getParagraphMatchScore,
  getSimilarity,
  isMissingClause,
  MISSING_SENTINEL,
  normalizeMatchText,
  stripClausePrefix
} from "../.test-dist/reviewUtils.js";

test("createIdempotencyKey uses randomUUID when available", () => {
  assert.equal(createIdempotencyKey({ randomUUID: () => "uuid-from-browser" }), "uuid-from-browser");
});

test("createIdempotencyKey falls back to a UUID from getRandomValues", () => {
  const key = createIdempotencyKey({
    getRandomValues: (values) => {
      values.fill(0);
      return values;
    },
  });
  assert.equal(key, "00000000-0000-4000-8000-000000000000");
});

test("createIdempotencyKey still returns a UUID without Web Crypto", () => {
  const originalRandom = Math.random;
  Math.random = () => 0.5;
  try {
    assert.equal(createIdempotencyKey({}), "80808080-8080-4080-8080-808080808080");
  } finally {
    Math.random = originalRandom;
  }
});

test("editDistance computes small punctuation-safe distance", () => {
  assert.equal(editDistance("Taxes.", "Taxes"), 1);
});

test("getSimilarity treats close phrases as high similarity", () => {
  assert.ok(getSimilarity("Counterparts clause", "Counterparts clause.") > 0.9);
});

test("normalizeMatchText strips punctuation noise", () => {
  assert.equal(normalizeMatchText("Section 10: Notices."), "section 10 notices");
});

test("stripClausePrefix removes numbering prefixes", () => {
  assert.equal(stripClausePrefix("(j) Counterparts. This Agreement"), "Counterparts. This Agreement");
  assert.equal(stripClausePrefix("10. Notices"), "Notices");
});

test("extractHeadingCandidate returns short clause heading", () => {
  assert.equal(extractHeadingCandidate("(j) Counterparts. This Agreement"), "Counterparts");
});

test("getParagraphMatchScore favors matching headings despite numbering differences", () => {
  const score = getParagraphMatchScore(
    "(j) Counterparts. This Agreement may be executed in one or more counterparts.",
    "Counterparts"
  );
  assert.ok(score >= 0.93);
});

test("findFuzzyMatch resolves heading-only query to the right paragraph", () => {
  const fullText = [
    "(i) Representation by Counsel. Each party acts on its own judgment.",
    "(j) Counterparts. This Agreement may be executed in one or more counterparts.",
    "11. Notices. All notices must be in writing."
  ].join("\n");

  const match = findFuzzyMatch(fullText, "Counterparts", 0.8);
  assert.ok(match);
  assert.equal(match?.matchedText, "Counterparts");
  assert.ok(typeof match?.from === "number" && match.from > 0);
});

test("findFuzzyMatch tolerates punctuation and wording drift", () => {
  const fullText = "合同份数：一式两份。\n签订地点：未约定。";
  const match = findFuzzyMatch(fullText, "合同份数：一式两份", 0.7);
  assert.ok(match);
  assert.equal(match?.matchedText, "合同份数：一式两份");
  assert.equal(match?.from, 0);
});

test("isMissingClause recognizes sentinel variations", () => {
  assert.equal(isMissingClause(MISSING_SENTINEL), true);
  assert.equal(isMissingClause("缺失该约定"), true);
  assert.equal(isMissingClause("Counterparts"), false);
});

test("applySavedModifications restores replacement revision marks", () => {
  const restored = applySavedModifications(
    "甲方应按期付款。\n乙方应按期交货。",
    [{ original: "按期付款", modified: "验收后付款", revision_id: "m1", paragraph_context: "甲方应按期付款。" }],
  );
  assert.equal(restored.correctedText, "甲方应验收后付款。\n乙方应按期交货。");
  assert.match(restored.revisionHtml, /<del class="del-mark" data-revision-id="m1">按期付款<\/del>/);
  assert.match(restored.revisionHtml, /<ins class="ins-mark" data-revision-id="m1">验收后付款<\/ins>/);
  assert.equal(restored.appliedCount, 1);
  assert.equal(restored.skippedCount, 0);
});

test("applySavedModifications inserts missing clauses after the saved anchor", () => {
  const restored = applySavedModifications(
    "第一条 付款。\n第二条 交付。",
    [{ original: MISSING_SENTINEL, modified: "第三条 保密。", insert_after_text: "第一条 付款。" }],
  );
  assert.equal(restored.correctedText, "第一条 付款。\n第三条 保密。\n第二条 交付。");
  assert.match(restored.revisionHtml, /<ins class="ins-mark"[^>]*>第三条 保密。<\/ins>/);
});

test("applySavedModifications skips ambiguous replacements", () => {
  const restored = applySavedModifications(
    "按期付款。\n按期付款。",
    [{ original: "按期付款", modified: "验收后付款" }],
  );
  assert.equal(restored.correctedText, "按期付款。\n按期付款。");
  assert.equal(restored.skippedCount, 1);
  assert.equal(restored.appliedCount, 0);
});

test("describeSourceDocxFailure keeps the shared-export consequence visible", () => {
  assert.match(describeSourceDocxFailure("网络中断"), /原始 Word 未能保存到共享工作区（网络中断）/);
});
