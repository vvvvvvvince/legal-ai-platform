import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = process.cwd();
const outDir = path.join(root, ".test-dist");
const utilSource = path.join(root, "src", "reviewUtils.ts");
const utilOutput = path.join(outDir, "reviewUtils.js");
const jobsSource = path.join(root, "src", "api", "reviewJobs.ts");
const jobsOutput = path.join(outDir, "reviewJobs.js");
const errorDetailsSource = path.join(root, "src", "api", "errorDetails.ts");
// TypeScript preserves the extensionless import in reviewJobs.js. Node's ESM
// resolver therefore needs this exact extensionless test fixture name.
const errorDetailsOutput = path.join(outDir, "errorDetails");
const testFile = path.join(root, "tests", "review-utils.test.mjs");
const jobsTestFile = path.join(root, "tests", "review-job-utils.test.mjs");
const assistantMarkTestFile = path.join(root, "tests", "legal-assistant-mark.test.mjs");
const viteChunksTestFile = path.join(root, "tests", "vite-chunks.test.mjs");

const ts = await import(pathToFileURL(path.join(root, "node_modules", "typescript", "lib", "typescript.js")).href);

await rm(outDir, { recursive: true, force: true });
await mkdir(outDir, { recursive: true });

const sourceText = await readFile(utilSource, "utf8");
const transpiled = ts.transpileModule(sourceText, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020
  },
  fileName: "reviewUtils.ts"
});

await writeFile(utilOutput, transpiled.outputText, "utf8");

const jobsSourceText = await readFile(jobsSource, "utf8");
const jobsTranspiled = ts.transpileModule(jobsSourceText, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020
  },
  fileName: "reviewJobs.ts"
});
await writeFile(jobsOutput, jobsTranspiled.outputText, "utf8");

const errorDetailsText = await readFile(errorDetailsSource, "utf8");
const errorDetailsTranspiled = ts.transpileModule(errorDetailsText, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020
  },
  fileName: "errorDetails.ts"
});
await writeFile(errorDetailsOutput, errorDetailsTranspiled.outputText, "utf8");

try {
  await import(pathToFileURL(testFile).href);
  await import(pathToFileURL(jobsTestFile).href);
  await import(pathToFileURL(assistantMarkTestFile).href);
  await import(pathToFileURL(viteChunksTestFile).href);
} finally {
  await rm(outDir, { recursive: true, force: true });
}
