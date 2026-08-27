import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import test from "node:test";

const componentUrl = new URL("../src/features/intake/LegalAssistantMark.tsx", import.meta.url);
const artworkUrl = new URL("../public/assets/legal-assistant-bot-v1.png", import.meta.url);

test("assistant mark uses the selected text-free robot artwork", async () => {
  const source = await readFile(componentUrl, "utf8");

  assert.doesNotMatch(source, />\s*律\s*</);
  assert.match(source, /legal-assistant-robot-image/);
  assert.match(source, /\/assets\/legal-assistant-bot-v1\.png/);
  assert.doesNotMatch(source, /legal-assistant-robot-head/);
});

test("assistant mark preserves the thinking state", async () => {
  const source = await readFile(componentUrl, "utf8");

  assert.match(source, /legal-assistant-mark-thinking/);
});

test("assistant artwork is optimized for immediate avatar rendering", async () => {
  const artwork = await stat(artworkUrl);

  assert.ok(artwork.size < 200_000, `expected optimized artwork, received ${artwork.size} bytes`);
});
