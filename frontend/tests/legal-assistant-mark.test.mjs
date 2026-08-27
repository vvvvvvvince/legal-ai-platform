import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const componentUrl = new URL("../src/features/intake/LegalAssistantMark.tsx", import.meta.url);

test("assistant mark uses a text-free robot structure", async () => {
  const source = await readFile(componentUrl, "utf8");

  assert.doesNotMatch(source, />\s*律\s*</);
  assert.match(source, /legal-assistant-robot-head/);
  assert.match(source, /legal-assistant-robot-eye/);
  assert.match(source, /legal-assistant-robot-smile/);
});

test("assistant mark preserves the thinking state", async () => {
  const source = await readFile(componentUrl, "utf8");

  assert.match(source, /legal-assistant-mark-thinking/);
});
