import assert from "node:assert/strict";
import { readdir } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

const assetsDir = path.join(process.cwd(), "dist", "assets");

test("production build separates React and editor dependencies", async () => {
  const assets = await readdir(assetsDir);

  assert.ok(assets.some((asset) => asset.startsWith("react-vendor-")));
  assert.ok(assets.some((asset) => asset.startsWith("editor-vendor-")));
});
