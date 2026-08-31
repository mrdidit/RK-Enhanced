import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(
  new URL("../src/frontendLightweight.ts", import.meta.url),
  "utf8",
);
const contentSource = await readFile(
  new URL("../src/Content.tsx", import.meta.url),
  "utf8",
);
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2020,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: "frontendLightweight.ts",
});
const model = await import(
  `data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`
);
const flushPromises = () => new Promise(resolve => setTimeout(resolve, 0));

assert.equal(model.sameGame(null, null), true);
assert.equal(model.sameGame(null, { appid: "1", name: "Game" }), false);
assert.equal(model.sameGame(
  { appid: "1", name: "Game" },
  { appid: "1", name: "Game" },
), true);
assert.equal(model.sameGame(
  { appid: "1", name: "Game" },
  { appid: "2", name: "Game" },
), false);
assert.equal(
  model.shouldRefreshGameState(
    { appid: "1", name: "Unsaved draft remains selected" },
    { appid: "1", name: "A later display-name value" },
  ),
  false,
  "reopening the panel on the same game must not reload and discard a draft",
);
assert.equal(model.shouldRefreshGameState(null, null), false);
assert.equal(model.shouldRefreshGameState(null, { appid: "1", name: "Game" }), true);
assert.equal(model.shouldRefreshGameState({ appid: "1", name: "Game" }, null), true);
assert.equal(model.shouldRefreshGameState(
  { appid: "1", name: "Game" },
  { appid: "2", name: "Other" },
), true);
assert.equal(model.canAcceptGameState(
  { appid: "1", name: "Game" }, "1", "1",
), true);
assert.equal(model.canAcceptGameState(
  { appid: "2", name: "New game" }, "1", "1",
), false, "a delayed response for the previous game must be discarded");
assert.equal(model.canAcceptGameState(
  { appid: "1", name: "Game" }, "1", "",
), false, "state is not accepted until the backend watcher confirms the transition");
assert.equal(model.canAcceptGameState(null, "", ""), true);
assert.equal(model.canAcceptBackendState(
  { appid: "9", name: "Game" }, "9", "", false, false,
), false, "normal state remains strictly AppID-matched");
assert.equal(model.canAcceptBackendState(
  { appid: "9", name: "Game" }, "9", "", true, false,
), true, "a legacy-plugin conflict may hydrate safe removal UI");
assert.equal(model.canAcceptBackendState(
  { appid: "9", name: "Game" }, "9", "", false, true,
), true, "a backend mutation gate may hydrate safe read-only UI");
assert.equal(model.canAcceptBackendState(
  { appid: "10", name: "New game" }, "9", "", true, true,
), false, "a blocked response for an obsolete observed game is still stale");
assert.equal(model.stateRefreshMode(false,
  { appid: "1", name: "Game" }, { appid: "1", name: "Game" }), "full");
assert.equal(model.stateRefreshMode(true,
  { appid: "1", name: "Game" }, { appid: "1", name: "Renamed" }), "metadata");
assert.equal(model.stateRefreshMode(true,
  { appid: "1", name: "Game" }, { appid: "2", name: "Other" }), "full");
assert.equal(model.safeAcceptedRefreshMode("full", false, false), "full");
assert.equal(model.safeAcceptedRefreshMode("full", true, false), "metadata",
  "blocked mismatch must not reset an existing editor to a stale preset");
assert.equal(model.safeAcceptedRefreshMode("full", true, true), "full");
assert.equal(model.isCurrentGeneration(4, 4, false), true);
assert.equal(model.isCurrentGeneration(4, 5, false), false);
assert.equal(model.isCurrentGeneration(4, 4, true), false);
const fanStatus = { fan_pwm: 81, fan_percent: 32, cooling_profile: "custom" };
assert.equal(model.sameFanStatus(fanStatus, { ...fanStatus }), true);
assert.equal(model.sameFanStatus(fanStatus, { ...fanStatus, fan_pwm: 82 }), false);
assert.equal(model.sameFanStatus(fanStatus, { ...fanStatus, fan_percent: 33 }), false);
assert.equal(model.sameFanStatus(fanStatus, { ...fanStatus, cooling_profile: "quiet" }), false);
assert.equal(model.sameFanStatus(null, null), true);
assert.equal(model.sameFanStatus(null, fanStatus), false);

assert.equal(model.shouldPollInstallProgress(false, true, true), false);
assert.equal(model.shouldPollInstallProgress(true, false, false), false);
assert.equal(model.shouldPollInstallProgress(true, true, false), true);
assert.equal(model.shouldPollInstallProgress(true, false, true), true);

assert.equal(model.formatLogContent(""), "");
assert.equal(
  model.formatLogContent(
    "[2026-08-31 20:25:10] first\n[2026-08-31T20:26:11,123] second\n",
  ),
  "[20:26] second\n[20:25] first",
);

const scheduled = [];
const cancelled = [];
const schedule = (callback, delay) => {
  const id = scheduled.length + 1;
  scheduled.push({ id, callback, delay });
  return id;
};
const cancel = timer => cancelled.push(timer);
const pending = [];
let calls = 0;
const stop = model.startCompletionPoll(() => {
  calls += 1;
  return new Promise(resolve => pending.push(resolve));
}, 2000, schedule, cancel);

await flushPromises();
assert.equal(calls, 1);
assert.equal(scheduled.length, 0, "no timer exists while the request is active");
pending.shift()();
await flushPromises();
assert.equal(scheduled.length, 1);
assert.equal(scheduled[0].delay, 2000);

scheduled[0].callback();
await flushPromises();
assert.equal(calls, 2);
assert.equal(scheduled.length, 1, "the second request must settle before scheduling again");
stop();
pending.shift()();
await flushPromises();
assert.equal(scheduled.length, 1, "stopping prevents a completed request from rescheduling");
assert.deepEqual(cancelled, [], "an already-fired timer is not cancelled again");

const readyTimers = [];
const readyCancelled = [];
const stopReady = model.startCompletionPoll(
  async () => undefined,
  500,
  (callback, delay) => {
    readyTimers.push({ callback, delay });
    return 99;
  },
  timer => readyCancelled.push(timer),
);
await flushPromises();
assert.equal(readyTimers.length, 1);
stopReady();
assert.deepEqual(readyCancelled, [99], "stopping cancels a pending timer");

assert.doesNotMatch(contentSource, /\bactivateGame\b|activate_game/);
assert.match(contentSource, /loadState\(expectedAppid, "full", isCurrent\)/,
  "boot hydration must be AppID-gated");
assert.match(contentSource, /Boolean\(next\.plugin_conflict\?\.blocked\), Boolean\(next\.mutations_blocked\)/,
  "blocked boot hydration must expose conflict-removal and safe read-only UI");
assert.match(contentSource, /observe\(true\)/,
  "each visible-panel activation must request a bounded state refresh");
const metadataStart = contentSource.indexOf("const acceptStateMetadata");
const fullStart = contentSource.indexOf("const acceptStateFull", metadataStart);
assert.ok(metadataStart >= 0 && fullStart > metadataStart);
const metadataSource = contentSource.slice(metadataStart, fullStart);
assert.doesNotMatch(metadataSource, /setSelected|setDraft|setSystemCurve/,
  "same-game metadata refresh must preserve editor state");
const fanPollStart = contentSource.indexOf("const generation = ++fanStatusGenerationRef.current");
const fanPollEnd = contentSource.indexOf("useEffect(() =>", fanPollStart);
assert.ok(fanPollStart >= 0 && fanPollEnd > fanPollStart);
const fanPollSource = contentSource.slice(fanPollStart, fanPollEnd);
assert.match(fanPollSource, /catch \(_\) \{[\s\S]*setFanStatus\(null\)/,
  "Fan RPC failure must clear freshness");
assert.ok((fanPollSource.match(/setFanStatus\(null\)/g) || []).length >= 3,
  "Fan activation, failure, and cleanup must each clear cached status");
assert.match(contentSource,
  /const fanCurveActuallyActive = fanCanApply && state\.fan_curve_active;/,
  "Fan active wording must also follow the fresh cooling-profile result");

console.log("Lightweight frontend tests passed");
