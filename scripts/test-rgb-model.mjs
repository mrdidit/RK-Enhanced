import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const source = await readFile(new URL("../src/rgbModel.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2020,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: "rgbModel.ts",
});
const model = await import(
  `data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`
);

const zones = [
  "left-270", "left-0", "left-90", "left-180",
  "right-270", "right-0", "right-90", "right-180",
].map((id, index) => ({
  id,
  color: [index, index + 10, index + 20],
  brightness: 100 + index,
}));
const request = {
  provider: "pocket-evo-v3",
  revision: "revision",
  mode: "rgb",
  lighting: {
    effect: "static",
    layout_mode: "both",
    zones,
    color: [255, 255, 255],
    brightness: 255,
    idle_color: [255, 255, 255],
    active_color: [0, 0, 255],
  },
};

const both = model.setEvoStaticGroup(request, 3, {
  color: [40, 50, 60],
  brightness: 70,
});
assert.ok(both.lighting.zones.every(zone =>
  zone.color.join(" ") === "40 50 60" && zone.brightness === 70));
assert.notDeepEqual(request.lighting.zones[0].color, both.lighting.zones[0].color);

const perStickRequest = model.cloneRgbRequest(request);
perStickRequest.lighting.layout_mode = "per-stick";
const perStick = model.setEvoStaticGroup(perStickRequest, 6, {
  color: [1, 2, 3],
});
assert.deepEqual(perStick.lighting.zones.slice(4).map(zone => zone.color),
  Array.from({ length: 4 }, () => [1, 2, 3]));
assert.notDeepEqual(perStick.lighting.zones[3].color, [1, 2, 3]);

const quadrantRequest = model.cloneRgbRequest(request);
quadrantRequest.lighting.layout_mode = "quadrants";
const quadrant = model.setEvoStaticGroup(quadrantRequest, 5, { brightness: 9 });
assert.equal(quadrant.lighting.zones[5].brightness, 9);
assert.equal(quadrant.lighting.zones[4].brightness, request.lighting.zones[4].brightness);
assert.equal(quadrant.lighting.zones[6].brightness, request.lighting.zones[6].brightness);

const collapsed = model.setEvoLayoutMode(quadrantRequest, "both", 5);
assert.ok(collapsed.lighting.zones.every(zone =>
  zone.color.join(" ") === request.lighting.zones[5].color.join(" ") &&
  zone.brightness === request.lighting.zones[5].brightness));
assert.equal(collapsed.lighting.effect, "static");

assert.deepEqual(model.calibrationRequest("revision", "save", {
  green_percent: 15,
  blue_percent: 20,
}), {
  provider: "pocket-evo-v3",
  revision: "revision",
  action: "save",
  green_percent: 15,
  blue_percent: 20,
});

assert.equal(model.rgbFailureDisposition(
  "RuntimeError: Pocket EVO RGB transport is suspended; retry after resume",
), "retry-after-resume");
assert.equal(model.rgbFailureDisposition(
  "ValueError: RGB state changed; refresh before applying",
), "clean-refresh");
assert.equal(model.rgbFailureDisposition(
  "OSError: preferences unavailable",
), "uncertain-write");

console.log("Pocket EVO RGB frontend model tests passed");
