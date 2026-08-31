import assert from "node:assert/strict";
import test from "node:test";

import { normalizeReviewJob, normalizeReviewModification, shouldPollReviewJob } from "../.test-dist/reviewJobs.js";

test("normalizes a succeeded review job", () => {
  const job = normalizeReviewJob({ job_id: "j1", status: "succeeded", result: { risks: [] } });
  assert.equal(job.status, "succeeded");
  assert.deepEqual(job.result, { risks: [] });
});

test("polling stops at terminal states", () => {
  assert.equal(shouldPollReviewJob("queued"), true);
  assert.equal(shouldPollReviewJob("running"), true);
  assert.equal(shouldPollReviewJob("succeeded"), false);
  assert.equal(shouldPollReviewJob("failed"), false);
});

test("normalizes a shared modification with its server-side author", () => {
  const modification = normalizeReviewModification({
    modification_id: "m1",
    job_id: "j1",
    status: "active",
    modification: { original: "先付款", modified: "验收后付款" },
    actor_user_id: "user-a",
    actor_display_name: "甲同事",
  });

  assert.equal(modification.modification_id, "m1");
  assert.equal(modification.actor_display_name, "甲同事");
  assert.equal(modification.modification.modified, "验收后付款");
});
