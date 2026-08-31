import type {
  RgbCalibrationRequest, RgbColor, RgbEvoCalibration, RgbEvoLayoutMode,
  RgbEvoLighting, RgbRequest, RgbZonedRequest,
} from "./types";

export const cloneRgbColor = (color: RgbColor): RgbColor => [...color];

export const cloneEvoLighting = <T extends RgbEvoLighting>(lighting: T): T => ({
  ...lighting,
  color: cloneRgbColor(lighting.color),
  idle_color: cloneRgbColor(lighting.idle_color),
  active_color: cloneRgbColor(lighting.active_color),
  zones: lighting.zones.map(zone => ({
    ...zone,
    color: cloneRgbColor(zone.color),
  })),
}) as T;

export const cloneRgbRequest = <T extends RgbRequest>(request: T): T => (
  request.provider === "pocket-evo-v3" || request.provider === "htr3212-static"
    ? { ...request, lighting: cloneEvoLighting(request.lighting) }
    : { ...request, color: cloneRgbColor(request.color) }
) as T;

const sameColor = (left: RgbColor, right: RgbColor) =>
  left.every((value, index) => value === right[index]);

export const sameEvoLighting = (left: RgbEvoLighting, right: RgbEvoLighting) =>
  left.effect === right.effect &&
  left.layout_mode === right.layout_mode &&
  left.brightness === right.brightness &&
  sameColor(left.color, right.color) &&
  sameColor(left.idle_color, right.idle_color) &&
  sameColor(left.active_color, right.active_color) &&
  left.zones.length === right.zones.length &&
  left.zones.every((zone, index) => {
    const other = right.zones[index];
    return Boolean(other && zone.id === other.id && zone.brightness === other.brightness &&
      sameColor(zone.color, other.color));
  });

export const sameEvoCalibration = (
  left: RgbEvoCalibration | null,
  right: RgbEvoCalibration | null,
) => left === right || Boolean(left && right &&
  left.green_percent === right.green_percent && left.blue_percent === right.blue_percent);

export type RgbFailureDisposition = "retry-after-resume" | "clean-refresh" | "uncertain-write";

export const rgbFailureDisposition = (message: string): RgbFailureDisposition => {
  if (message.includes("transport is suspended; retry after resume"))
    return "retry-after-resume";
  if (message.includes("state changed; refresh before applying") ||
      message.includes("provider changed; refresh before applying"))
    return "clean-refresh";
  return "uncertain-write";
};

export const evoZoneIndexes = (
  layout: RgbEvoLayoutMode,
  targetIndex: number,
  zoneCount: number,
) => {
  if (layout === "both") return Array.from({ length: zoneCount }, (_, index) => index);
  if (layout === "per-stick") {
    const start = targetIndex < 4 ? 0 : 4;
    return Array.from({ length: Math.min(4, Math.max(0, zoneCount - start)) }, (_, index) => start + index);
  }
  return targetIndex >= 0 && targetIndex < zoneCount ? [targetIndex] : [];
};

export const setEvoStaticGroup = <T extends RgbZonedRequest>(
  request: T,
  targetIndex: number,
  change: { color?: RgbColor; brightness?: number },
): T => {
  const next = cloneRgbRequest(request);
  for (const index of evoZoneIndexes(
    next.lighting.layout_mode,
    targetIndex,
    next.lighting.zones.length,
  )) {
    if (change.color) next.lighting.zones[index].color = cloneRgbColor(change.color);
    if (change.brightness !== undefined) next.lighting.zones[index].brightness = change.brightness;
  }
  return next;
};

export const setEvoLayoutMode = <T extends RgbZonedRequest>(
  request: T,
  layoutMode: RgbEvoLayoutMode,
  sourceIndex: number,
): T => {
  const next = cloneRgbRequest(request);
  if (!next.lighting.zones.length) {
    next.lighting.layout_mode = layoutMode;
    return next;
  }
  const safeSource = Math.min(Math.max(sourceIndex, 0), next.lighting.zones.length - 1);
  if (layoutMode === "both") {
    const source = next.lighting.zones[safeSource];
    for (const zone of next.lighting.zones) {
      zone.color = cloneRgbColor(source.color);
      zone.brightness = source.brightness;
    }
  } else if (layoutMode === "per-stick") {
    for (const start of [0, 4]) {
      const source = next.lighting.zones[start];
      if (!source) continue;
      for (const zone of next.lighting.zones.slice(start, start + 4)) {
        zone.color = cloneRgbColor(source.color);
        zone.brightness = source.brightness;
      }
    }
  }
  next.lighting.layout_mode = layoutMode;
  return next;
};

export const calibrationRequest = (
  revision: string,
  action: RgbCalibrationRequest["action"],
  calibration?: RgbEvoCalibration,
): RgbCalibrationRequest => ({
  provider: "pocket-evo-v3",
  revision,
  action,
  ...(action === "save" && calibration ? {
    green_percent: calibration.green_percent,
    blue_percent: calibration.blue_percent,
  } : {}),
});
