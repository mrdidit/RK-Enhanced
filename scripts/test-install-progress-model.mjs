import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(
  new URL("../src/installProgressModel.ts", import.meta.url),
  "utf8",
);
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2020,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: "installProgressModel.ts",
});
const model = await import(
  `data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`
);

const progress = (changes = {}) => ({
  protocol: 1,
  transaction_id: "11111111-1111-4111-8111-111111111111",
  generation: 4,
  active: true,
  terminal: false,
  kind: "update",
  source_version: "beta.9",
  target_version: "beta.10",
  decky_version: "v3",
  phase: "downloading",
  message: "Downloading",
  outcome: "running",
  started_at: 100,
  updated_at: 101,
  success: null,
  rolled_back: false,
  error: "",
  acknowledged: false,
  ...changes,
});

const current = progress();
assert.equal(model.chooseInstallProgress(current, progress({ generation: 3 })), current);
assert.equal(model.chooseInstallProgress(current, progress({
  transaction_id: "22222222-2222-4222-8222-222222222222",
})), current);
assert.equal(model.chooseInstallProgress(current, progress({ updated_at: 100 })), current);

const newer = progress({
  generation: 5,
  transaction_id: "22222222-2222-4222-8222-222222222222",
  updated_at: 102,
});
assert.equal(model.chooseInstallProgress(current, newer), newer);
assert.equal(model.isNewInstallTransaction(newer, current), true);
assert.equal(model.isNewInstallTransaction(current, current), false);

const terminal = progress({
  active: false,
  terminal: true,
  phase: "completed",
  outcome: "succeeded",
  success: true,
});
assert.equal(model.chooseInstallProgress(terminal, current), terminal);
const acknowledged = { ...terminal, acknowledged: true };
assert.equal(model.chooseInstallProgress(acknowledged, terminal), acknowledged);

console.log("Installer progress frontend model tests passed");
