import {
  ButtonItem, DropdownItem, Field, PanelSection, PanelSectionRow,
  SliderField, ToggleField,
} from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import type { Ref } from "react";
import { getRgbState, setRgbCalibration, setRgbState } from "./backend";
import {
  calibrationRequest, cloneEvoLighting, cloneRgbColor, cloneRgbRequest,
  rgbFailureDisposition, sameEvoCalibration, sameEvoLighting,
  setEvoLayoutMode, setEvoStaticGroup,
} from "./rgbModel";
import type {
  RgbColor, RgbEffect, RgbEvoCalibration, RgbEvoEffect, RgbEvoLayoutMode,
  RgbEvoRequest, RgbLegacyEffect, RgbLegacyRequest, RgbMode, RgbRequest, RgbState,
  RgbZonedRequest,
} from "./types";

const MODE_LABELS: Record<RgbMode, string> = {
  off: "Off",
  battery: "Battery",
  rgb: "RGB",
};
const LEGACY_EFFECT_LABELS: Record<RgbLegacyEffect, string> = {
  static: "Static",
  breath: "Breath",
  rainbow: "Rainbow",
};
const EVO_EFFECT_LABELS: Record<RgbEvoEffect, string> = {
  static: "Static",
  breath: "Breath",
  "rgb-breath": "RGB Breath",
  rainbow: "Rainbow",
  reactive: "Reactive",
};
const LAYOUT_LABELS: Record<RgbEvoLayoutMode, string> = {
  both: "Both rings",
  "per-stick": "Per stick",
  quadrants: "Quadrants",
};
const QUADRANT_LABELS = ["270° Left", "0° Top", "90° Right", "180° Bottom"];
const HTR3212_QUADRANT_LABELS = ["Upper left", "Upper right", "Lower right", "Lower left"];
const option = <T extends string>(data: T, label: string) => ({ data, label });

const isZonedProvider = (provider: RgbState["provider"] | RgbRequest["provider"]):
  provider is "pocket-evo-v3" | "htr3212-static" =>
  provider === "pocket-evo-v3" || provider === "htr3212-static";
type RgbEvoState = Extract<RgbState, { provider: "pocket-evo-v3" }>;
type RgbHtrState = Extract<RgbState, { provider: "htr3212-static" }>;
type RgbZonedState = RgbEvoState | RgbHtrState;
const isZonedState = (state: RgbState): state is RgbZonedState =>
  isZonedProvider(state.provider);
const isZonedRequest = (request: RgbRequest): request is RgbZonedRequest =>
  isZonedProvider(request.provider);

const isLegacyEffect = (effect: RgbEffect): effect is RgbLegacyEffect =>
  effect === "static" || effect === "breath" || effect === "rainbow";
const isEvoEffect = (effect: RgbEffect): effect is RgbEvoEffect =>
  effect === "static" || effect === "breath" || effect === "rgb-breath" ||
  effect === "rainbow" || effect === "reactive";

function defaultNonOffLighting(state: RgbEvoState): RgbEvoState["lighting"];
function defaultNonOffLighting(state: RgbHtrState): RgbHtrState["lighting"];
function defaultNonOffLighting(state: RgbZonedState): RgbEvoState["lighting"] {
  const lighting = cloneEvoLighting(state.lighting);
  lighting.effect = "static";
  lighting.layout_mode = "both";
  lighting.zones = lighting.zones.map(zone => ({
    ...zone,
    color: cloneRgbColor(lighting.color),
    brightness: lighting.brightness,
  }));
  return lighting;
}

const requestFromState = (state: RgbState): RgbRequest | null => {
  if (!(state.supported && state.valid && state.provider !== "none" &&
      state.mode !== "unknown")) return null;
  if (state.provider === "htr3212-static") {
    const lighting = state.mode === "off"
      ? state.resume_lighting || defaultNonOffLighting(state)
      : state.lighting;
    return {
      provider: state.provider,
      revision: state.revision,
      mode: state.mode,
      lighting: cloneEvoLighting(lighting),
    };
  }
  if (state.provider === "pocket-evo-v3") {
    const lighting = state.mode === "off"
      ? state.resume_lighting || defaultNonOffLighting(state)
      : state.lighting;
    return {
      provider: state.provider,
      revision: state.revision,
      mode: state.mode,
      lighting: cloneEvoLighting(lighting),
    };
  }
  return {
    provider: state.provider,
    revision: state.revision,
    mode: state.mode,
    effect: state.effect,
    color: cloneRgbColor(state.color),
    brightness: state.brightness,
    correction: state.correction,
  };
};

const sameRequest = (left: RgbRequest | null, right: RgbRequest | null) => {
  if (left === right) return true;
  if (!left || !right || !(left.provider === right.provider &&
      left.revision === right.revision && left.mode === right.mode)) return false;
  if (left.provider === "pocket-evo-v3" && right.provider === "pocket-evo-v3")
    return sameEvoLighting(left.lighting, right.lighting);
  if (left.provider === "htr3212-static" && right.provider === "htr3212-static")
    return sameEvoLighting(left.lighting, right.lighting);
  if (isZonedRequest(left) || isZonedRequest(right)) return false;
  return left.effect === right.effect && left.brightness === right.brightness &&
    left.correction === right.correction &&
    left.color.every((value, index) => value === right.color[index]);
};

const hexColour = ([red, green, blue]: RgbColor) =>
  `#${[red, green, blue].map(value => value.toString(16).padStart(2, "0")).join("").toUpperCase()}`;

const Heading = ({ title, headingRef, onActivate }: {
  title: string;
  headingRef?: Ref<HTMLDivElement>;
  onActivate?: () => void;
}) => <div className="rke-performance-heading-row">
  <Field ref={headingRef} className="rke-performance-heading" focusable highlightOnFocus
    bottomSeparator="none"
    label={<span className="rke-performance-heading-label">{title}</span>}
    onActivate={onActivate} onClick={onActivate} />
</div>;

const ValueLabel = ({ name, value }: { name: string; value: string }) =>
  <span className="rke-frequency-label"><span>{name}</span><span>{value}</span></span>;

const ColourEditor = ({
  color, brightness, maxBrightness, min = 0, disabled,
  onColorChange, onBrightnessChange,
}: {
  color: RgbColor;
  brightness?: number;
  maxBrightness: number;
  min?: number;
  disabled: boolean;
  onColorChange: (color: RgbColor) => void;
  onBrightnessChange?: (brightness: number) => void;
}) => <>
  <PanelSectionRow><Field label="Colour" bottomSeparator="none">
    <span className="rke-rgb-colour-value">
      <span>{hexColour(color)}</span>
      <span className="rke-rgb-swatch" style={{ backgroundColor: hexColour(color) }} />
    </span>
  </Field></PanelSectionRow>
  {(["Red", "Green", "Blue"] as const).map((name, index) =>
    <PanelSectionRow key={name}><SliderField
      label={<ValueLabel name={name} value={String(color[index])} />}
      bottomSeparator="none" disabled={disabled}
      value={color[index]} min={0} max={255} step={1}
      minimumDpadGranularity={1}
      onChange={value => {
        const next = cloneRgbColor(color);
        next[index] = value;
        onColorChange(next);
      }} />
    </PanelSectionRow>)}
  {brightness !== undefined && onBrightnessChange && <PanelSectionRow><SliderField
    label={<ValueLabel name="Brightness"
      value={`${Math.round(brightness * 100 / maxBrightness)}%`} />}
    bottomSeparator="none" disabled={disabled}
    value={brightness} min={min} max={maxBrightness} step={1}
    minimumDpadGranularity={1}
    onChange={onBrightnessChange} />
  </PanelSectionRow>}
</>;

const LegacyLightingToggle = ({
  draft, disabled, onChange,
}: {
  draft: RgbLegacyRequest;
  disabled: boolean;
  onChange: (checked: boolean) => void;
}) => <PanelSectionRow><ToggleField label="Stick lighting"
  description="Independent of the system battery indicator."
  bottomSeparator="none" disabled={disabled}
  checked={draft.mode === "rgb"} onChange={onChange} />
</PanelSectionRow>;

export function RGB({ active }: { active: boolean }) {
  const [status, setStatus] = useState<RgbState | null>(null);
  const [saved, setSaved] = useState<RgbRequest | null>(null);
  const [draft, setDraft] = useState<RgbRequest | null>(null);
  const [savedCalibration, setSavedCalibration] = useState<RgbEvoCalibration | null>(null);
  const [draftCalibration, setDraftCalibration] = useState<RgbEvoCalibration | null>(null);
  const [evoTargetIndex, setEvoTargetIndex] = useState(0);
  const [reactiveTarget, setReactiveTarget] = useState<"idle" | "active">("idle");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<false | "lighting" | "calibration">(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [refreshRequest, setRefreshRequest] = useState(0);
  const generation = useRef(0);
  const busyRef = useRef(false);
  const activeRef = useRef(active);
  const needsRefreshAfterStaleApply = useRef(false);
  const topRef = useRef<HTMLDivElement>(null);
  const lightingDirty = !sameRequest(saved, draft);
  const calibrationDirty = !sameEvoCalibration(savedCalibration, draftCalibration);
  const dirtyRef = useRef(lightingDirty || calibrationDirty);
  activeRef.current = active;
  dirtyRef.current = lightingDirty || calibrationDirty;

  const adoptState = (next: RgbState) => {
    const request = requestFromState(next);
    setStatus(next);
    setSaved(request);
    setDraft(request ? cloneRgbRequest(request) : null);
    if (next.provider === "pocket-evo-v3") {
      const calibration = { ...next.calibration };
      setSavedCalibration(calibration);
      setDraftCalibration({ ...calibration });
    } else {
      setSavedCalibration(null);
      setDraftCalibration(null);
    }
    setEvoTargetIndex(0);
    return request;
  };

  useEffect(() => {
    const requestGeneration = ++generation.current;
    if (!active) {
      setLoading(false);
      return;
    }
    // Preserve an unfinished controller edit across Quick Access visibility changes.
    // A write completed outside its original activation is authoritative instead.
    const forceRefresh = needsRefreshAfterStaleApply.current;
    if (dirtyRef.current && !forceRefresh) return;
    needsRefreshAfterStaleApply.current = false;
    setStatus(null);
    setSaved(null);
    setDraft(null);
    setSavedCalibration(null);
    setDraftCalibration(null);
    setLoading(true);
    setMessage("");
    setError("");
    void getRgbState().then(next => {
      if (requestGeneration !== generation.current) return;
      const request = adoptState(next);
      setError(request ? "" : next.error || "RGB status is unavailable.");
    }).catch(reason => {
      if (requestGeneration !== generation.current) return;
      setError(String(reason));
    }).finally(() => {
      if (requestGeneration === generation.current) setLoading(false);
    });
    return () => {
      if (requestGeneration === generation.current) generation.current += 1;
    };
  }, [active, refreshRequest]);

  const replaceDraft = (change: (request: RgbRequest) => RgbRequest) => {
    setMessage("");
    setDraft(current => current ? change(cloneRgbRequest(current)) : current);
  };
  const updateLegacy = (change: (request: RgbLegacyRequest) => void) =>
    replaceDraft(request => {
      if (isZonedRequest(request)) return request;
      change(request);
      return request;
    });
  const updateZoned = (change: (request: RgbZonedRequest) => RgbZonedRequest | void) =>
    replaceDraft(request => {
      if (!isZonedRequest(request)) return request;
      return change(request) || request;
    });
  const updateEvo = (change: (request: RgbEvoRequest) => RgbEvoRequest | void) =>
    replaceDraft(request => {
      if (request.provider !== "pocket-evo-v3") return request;
      return change(request) || request;
    });
  const reload = () => {
    if (!active || busyRef.current) return;
    needsRefreshAfterStaleApply.current = true;
    setRefreshRequest(current => current + 1);
  };

  const beginOperation = () => {
    if (!active || busyRef.current) return false;
    busyRef.current = true;
    setMessage("");
    setError("");
    return true;
  };
  const finishOperation = (requestGeneration: number) => {
    busyRef.current = false;
    setBusy(false);
    if (requestGeneration !== generation.current && activeRef.current)
      setRefreshRequest(current => current + 1);
  };

  const reconcileFailedOperation = async (
    reason: unknown,
    requestGeneration: number,
    attemptedLighting?: RgbRequest,
    attemptedCalibration?: RgbEvoCalibration,
    persistedLighting?: RgbRequest | null,
    persistedCalibration?: RgbEvoCalibration | null,
  ) => {
    const failure = String(reason);
    try {
      // A sysfs error may occur after the driver changed its cached state,
      // and a guarded rollback may deliberately yield to another writer.
      // Always replace the stale pre-operation snapshot with a fresh complete
      // provider read before reporting the failure.
      const next = await getRgbState();
      if (requestGeneration !== generation.current) {
        needsRefreshAfterStaleApply.current = true;
        return;
      }
      const actual = adoptState(next);
      const disposition = rgbFailureDisposition(failure);
      const retryAfterResume = disposition === "retry-after-resume";
      const preMutationRejection = disposition === "clean-refresh";
      if (retryAfterResume && actual && attemptedLighting &&
          actual.provider === attemptedLighting.provider) {
        const retry = cloneRgbRequest(attemptedLighting);
        retry.revision = actual.revision;
        setDraft(retry);
      }
      if (retryAfterResume && next.provider === "pocket-evo-v3" &&
          attemptedCalibration) {
        setDraftCalibration({ ...attemptedCalibration });
      }
      if (!retryAfterResume && !preMutationRejection && actual && persistedLighting &&
          actual.provider === persistedLighting.provider) {
        // Keep the last known persisted request as the comparison baseline,
        // but rebase its optimistic token onto the complete fresh snapshot.
        // If rollback failed (or yielded to an external writer), the active
        // native state is therefore visibly dirty and can be saved again.
        const baseline = cloneRgbRequest(persistedLighting);
        baseline.revision = actual.revision;
        setSaved(baseline);
      }
      if (!retryAfterResume && !preMutationRejection &&
          next.provider === "pocket-evo-v3" &&
          persistedCalibration) {
        setSavedCalibration({ ...persistedCalibration });
      }
      setError(failure);
    } catch (refreshReason) {
      if (requestGeneration === generation.current) {
        setError(`${failure} Actual RGB state could not be refreshed: ${String(refreshReason)}`);
      } else {
        needsRefreshAfterStaleApply.current = true;
      }
    }
  };

  const applyLighting = async () => {
    if (!draft || calibrationDirty || !beginOperation()) return;
    const requestGeneration = generation.current;
    const attempted = cloneRgbRequest(draft);
    const persisted = saved ? cloneRgbRequest(saved) : null;
    setBusy("lighting");
    try {
      const next = await setRgbState(attempted);
      if (requestGeneration !== generation.current) {
        needsRefreshAfterStaleApply.current = true;
        return;
      }
      const applied = adoptState(next);
      if (!applied) {
        setError(next.error || "RGB settings could not be applied.");
        return;
      }
      setMessage("RGB settings saved and applied");
    } catch (reason) {
      await reconcileFailedOperation(
        reason, requestGeneration, attempted, undefined, persisted);
    } finally {
      finishOperation(requestGeneration);
    }
  };

  const applyCalibration = async (action: "save" | "reset" | "raw") => {
    if (lightingDirty || !draftCalibration || draft?.provider !== "pocket-evo-v3" ||
        !beginOperation()) return;
    const requestGeneration = generation.current;
    const attempted = { ...draftCalibration };
    const persisted = savedCalibration ? { ...savedCalibration } : null;
    setBusy("calibration");
    try {
      const next = await setRgbCalibration(calibrationRequest(
        draft.revision,
        action,
        attempted,
      ));
      if (requestGeneration !== generation.current) {
        needsRefreshAfterStaleApply.current = true;
        return;
      }
      const applied = adoptState(next);
      if (!applied || next.provider !== "pocket-evo-v3") {
        setError(next.error || "RGB calibration could not be applied.");
        return;
      }
      setMessage(action === "reset" ? "Calibration reset to Pocket EVO defaults" :
        action === "raw" ? "Raw RGB calibration saved" : "RGB calibration saved");
    } catch (reason) {
      await reconcileFailedOperation(
        reason, requestGeneration, undefined, attempted, undefined, persisted);
    } finally {
      finishOperation(requestGeneration);
    }
  };

  const modes = status?.modes || [];
  const effects = status?.effects || [];
  const analogStatic = status?.provider === "analog-static";
  const evoStatus = status?.provider === "pocket-evo-v3" ? status : null;
  const zonedStatus = status && isZonedState(status) ? status : null;
  const legacyDraft = draft && !isZonedRequest(draft) ? draft : null;
  const zonedDraft = draft && isZonedRequest(draft) ? draft : null;
  const evoDraft = draft?.provider === "pocket-evo-v3" ? draft : null;
  const htrDraft = draft?.provider === "htr3212-static" ? draft : null;
  const maxBrightness = Math.max(1, status?.max_brightness || 255);
  const lightingLocked = Boolean(busy || !active || calibrationDirty);
  const calibrationLocked = Boolean(busy || !active || lightingDirty);
  const calibrationNeedsSave = Boolean(draftCalibration &&
    !sameEvoCalibration(evoStatus?.calibration_override || null, draftCalibration));
  const selectedZone = zonedDraft?.lighting.zones[Math.min(
    Math.max(evoTargetIndex, 0),
    Math.max(0, zonedDraft.lighting.zones.length - 1),
  )];
  const backToTop = () => {
    topRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    topRef.current?.focus();
  };

  return <div className="rke-rgb">
    <PanelSection>
      <Heading title={analogStatic ? "Stick RGB" : "RGB Control"} headingRef={topRef} />
      {loading && !draft && <PanelSectionRow><Field label="Reading RGB settings…"
        bottomSeparator="none" /></PanelSectionRow>}

      {legacyDraft && <>
        {analogStatic
          ? <LegacyLightingToggle draft={legacyDraft} disabled={Boolean(busy || !active)}
            onChange={checked => updateLegacy(request => {
              request.mode = checked ? "rgb" : "off";
              request.effect = "static";
            })} />
          : <PanelSectionRow><DropdownItem label="LED Color" bottomSeparator="none"
            disabled={Boolean(busy || !active)}
            selectedOption={legacyDraft.mode}
            rgOptions={modes.map(mode => option(mode, MODE_LABELS[mode]))}
            onChange={(selected: any) => updateLegacy(request => { request.mode = selected.data; })} />
          </PanelSectionRow>}
        {status?.provider === "analog-static" && status?.zones_differ &&
          <PanelSectionRow><Field label="Saved ring colours differ"
            description="Showing the right-stick colour. Save & Apply uses one colour for both rings."
            bottomSeparator="none" />
          </PanelSectionRow>}
        {legacyDraft.mode === "rgb" && <>
          {effects.length > 1 && <PanelSectionRow><DropdownItem
            label="Effect" bottomSeparator="none" disabled={Boolean(busy || !active)}
            selectedOption={legacyDraft.effect}
            rgOptions={effects.filter(isLegacyEffect).map(effect =>
              option(effect, LEGACY_EFFECT_LABELS[effect]))}
            onChange={(selected: any) => updateLegacy(request => {
              request.effect = selected.data;
            })} />
          </PanelSectionRow>}
          {legacyDraft.effect !== "rainbow" && <>
            <ColourEditor color={legacyDraft.color}
              brightness={legacyDraft.effect === "static" ? legacyDraft.brightness : undefined}
              maxBrightness={maxBrightness} min={analogStatic ? 1 : 0}
              disabled={Boolean(busy || !active)}
              onColorChange={color => updateLegacy(request => { request.color = color; })}
              onBrightnessChange={brightness => updateLegacy(request => {
                request.brightness = brightness;
              })} />
            <PanelSectionRow><ToggleField label="Colour correction"
              description="When red is used, green and blue output are reduced to 80%."
              bottomSeparator="none" disabled={Boolean(busy || !active)}
              checked={legacyDraft.correction}
              onChange={checked => updateLegacy(request => { request.correction = checked; })} />
            </PanelSectionRow>
          </>}
          {legacyDraft.effect === "rainbow" && <PanelSectionRow><Field
            label="MCU-controlled effect" bottomSeparator="none"
            description="Rainbow has no adjustable colour or brightness." />
          </PanelSectionRow>}
        </>}
      </>}

      {zonedDraft && zonedStatus && <>
        <PanelSectionRow><DropdownItem label="Stick lighting" bottomSeparator="none"
          disabled={lightingLocked}
          selectedOption={zonedDraft.mode}
          rgOptions={[option("off", "Off"), option("rgb", "RGB")]}
          onChange={(selected: any) => updateZoned(request => {
            request.mode = selected.data;
          })} />
        </PanelSectionRow>
        {evoStatus?.temporarily_gated && <div className="rke-rgb-warning">
          <PanelSectionRow><Field label="RGB output temporarily suspended"
            description="ROCKNIX has gated the LEDs. Saved changes will appear when output resumes."
            bottomSeparator="none" />
          </PanelSectionRow>
        </div>}
        {zonedDraft.mode === "rgb" && <>
          {evoDraft && <PanelSectionRow><DropdownItem label="Effect" bottomSeparator="none"
            disabled={lightingLocked}
            selectedOption={evoDraft.lighting.effect}
            rgOptions={effects.filter(isEvoEffect).map(effect =>
              option(effect, EVO_EFFECT_LABELS[effect]))}
            onChange={(selected: any) => updateEvo(request => {
              request.lighting.effect = selected.data;
            })} />
          </PanelSectionRow>}

          {zonedDraft.lighting.effect === "static" && <>
            <PanelSectionRow><DropdownItem label="Layout" bottomSeparator="none"
              disabled={lightingLocked}
              selectedOption={zonedDraft.lighting.layout_mode}
              rgOptions={(Object.keys(LAYOUT_LABELS) as RgbEvoLayoutMode[]).map(layout =>
                option(layout, LAYOUT_LABELS[layout]))}
              onChange={(selected: any) => updateZoned(request => {
                const layout = selected.data as RgbEvoLayoutMode;
                setEvoTargetIndex(layout === "per-stick" && evoTargetIndex >= 4 ? 4 : 0);
                return setEvoLayoutMode(request, layout, evoTargetIndex);
              })} />
            </PanelSectionRow>
            {zonedDraft.lighting.layout_mode === "both" && <PanelSectionRow><Field
              label="Target" description="Both stick rings" bottomSeparator="none" />
            </PanelSectionRow>}
            {zonedDraft.lighting.layout_mode === "per-stick" && <PanelSectionRow><DropdownItem
              label="Target" bottomSeparator="none" disabled={lightingLocked}
              selectedOption={evoTargetIndex < 4 ? "left" : "right"}
              rgOptions={[option("left", "Left stick"), option("right", "Right stick")]}
              onChange={(selected: any) => setEvoTargetIndex(selected.data === "right" ? 4 : 0)} />
            </PanelSectionRow>}
            {zonedDraft.lighting.layout_mode === "quadrants" && <>
              <PanelSectionRow><DropdownItem label="Stick" bottomSeparator="none"
                disabled={lightingLocked}
                selectedOption={evoTargetIndex < 4 ? "left" : "right"}
                rgOptions={[option("left", "Left stick"), option("right", "Right stick")]}
                onChange={(selected: any) => setEvoTargetIndex(
                  (selected.data === "right" ? 4 : 0) + evoTargetIndex % 4,
                )} />
              </PanelSectionRow>
              <PanelSectionRow><DropdownItem label="Quadrant" bottomSeparator="none"
                disabled={lightingLocked}
                selectedOption={String(evoTargetIndex % 4)}
                rgOptions={(htrDraft ? HTR3212_QUADRANT_LABELS : QUADRANT_LABELS)
                  .map((label, index) => option(String(index), label))}
                onChange={(selected: any) => setEvoTargetIndex(
                  (evoTargetIndex < 4 ? 0 : 4) + Number(selected.data),
                )} />
              </PanelSectionRow>
            </>}
            {selectedZone && <ColourEditor color={selectedZone.color}
              brightness={selectedZone.brightness} maxBrightness={maxBrightness}
              disabled={lightingLocked}
              onColorChange={color => updateZoned(request =>
                setEvoStaticGroup(request, evoTargetIndex, { color }))}
              onBrightnessChange={brightness => updateZoned(request =>
                setEvoStaticGroup(request, evoTargetIndex, { brightness }))} />}
          </>}

          {evoDraft && evoDraft.lighting.effect === "breath" && <ColourEditor
            color={evoDraft.lighting.color} brightness={evoDraft.lighting.brightness}
            maxBrightness={maxBrightness} disabled={lightingLocked}
            onColorChange={color => updateEvo(request => {
              request.lighting.color = color;
            })}
            onBrightnessChange={brightness => updateEvo(request => {
              request.lighting.brightness = brightness;
            })} />}

          {evoDraft && evoDraft.lighting.effect === "rgb-breath" && <PanelSectionRow><SliderField
            label={<ValueLabel name="Brightness"
              value={`${Math.round(evoDraft.lighting.brightness * 100 / maxBrightness)}%`} />}
            bottomSeparator="none" disabled={lightingLocked}
            value={evoDraft.lighting.brightness} min={0} max={maxBrightness} step={1}
            minimumDpadGranularity={1}
            onChange={brightness => updateEvo(request => {
              request.lighting.brightness = brightness;
            })} />
          </PanelSectionRow>}

          {evoDraft && evoDraft.lighting.effect === "rainbow" && <PanelSectionRow><Field
            label="MCU-controlled effect" bottomSeparator="none"
            description="Rainbow is generated by the controller and has no adjustable settings." />
          </PanelSectionRow>}

          {evoDraft && evoDraft.lighting.effect === "reactive" && <>
            <PanelSectionRow><DropdownItem label="Reactive colour" bottomSeparator="none"
              disabled={lightingLocked} selectedOption={reactiveTarget}
              rgOptions={[option("idle", "Idle"), option("active", "Active")]}
              onChange={(selected: any) => setReactiveTarget(selected.data)} />
            </PanelSectionRow>
            <ColourEditor
              color={reactiveTarget === "idle"
                ? evoDraft.lighting.idle_color : evoDraft.lighting.active_color}
              brightness={evoDraft.lighting.brightness} maxBrightness={maxBrightness}
              disabled={lightingLocked}
              onColorChange={color => updateEvo(request => {
                if (reactiveTarget === "idle") request.lighting.idle_color = color;
                else request.lighting.active_color = color;
              })}
              onBrightnessChange={brightness => updateEvo(request => {
                request.lighting.brightness = brightness;
              })} />
          </>}
        </>}
        {calibrationDirty && <PanelSectionRow><Field label="Unsaved calibration changes"
          description="Save calibration before changing or applying lighting."
          bottomSeparator="none" />
        </PanelSectionRow>}
      </>}

      {draft && <>
        <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none"
          disabled={Boolean(busy || loading || !active || calibrationDirty ||
            !status?.supported || !status.valid || (zonedDraft && !lightingDirty))}
          onClick={() => { void applyLighting(); }}>Save &amp; Apply</ButtonItem></PanelSectionRow>
        {lightingDirty && <PanelSectionRow><Field label="Unsaved RGB changes"
          bottomSeparator="none" /></PanelSectionRow>}
      </>}

      {evoDraft && evoStatus && draftCalibration && <>
        <Heading title="Colour calibration" />
        <PanelSectionRow><Field label="Pocket EVO calibration"
          description="Saved separately from lighting. Pure green, blue and cyan remain unchanged."
          bottomSeparator="none" />
        </PanelSectionRow>
        <PanelSectionRow><Field label="Saved calibration override"
          description={evoStatus.calibration_override
            ? `${evoStatus.calibration_override.green_percent}% green · ${evoStatus.calibration_override.blue_percent}% blue`
            : "None — the kernel default is used after reboot."}
          bottomSeparator="none" />
        </PanelSectionRow>
        <PanelSectionRow><SliderField
          label={<ValueLabel name="Green in mixed colours"
            value={`${draftCalibration.green_percent}%`} />}
          bottomSeparator="none" disabled={calibrationLocked}
          value={draftCalibration.green_percent} min={0} max={100} step={1}
          minimumDpadGranularity={1}
          onChange={green_percent => {
            setMessage("");
            setDraftCalibration(current => current ? { ...current, green_percent } : current);
          }} />
        </PanelSectionRow>
        <PanelSectionRow><SliderField
          label={<ValueLabel name="Blue in mixed colours"
            value={`${draftCalibration.blue_percent}%`} />}
          bottomSeparator="none" disabled={calibrationLocked}
          value={draftCalibration.blue_percent} min={0} max={100} step={1}
          minimumDpadGranularity={1}
          onChange={blue_percent => {
            setMessage("");
            setDraftCalibration(current => current ? { ...current, blue_percent } : current);
          }} />
        </PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none"
          disabled={Boolean(calibrationLocked || !calibrationNeedsSave)}
          onClick={() => { void applyCalibration("save"); }}>Save calibration</ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none"
          disabled={calibrationLocked}
          onClick={() => { void applyCalibration("reset"); }}>Reset calibration</ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none"
          disabled={calibrationLocked}
          onClick={() => { void applyCalibration("raw"); }}>Use raw RGB</ButtonItem>
        </PanelSectionRow>
        {lightingDirty && <PanelSectionRow><Field label="Unsaved lighting changes"
          description="Save lighting before changing calibration."
          bottomSeparator="none" />
        </PanelSectionRow>}
      </>}

      {message && <div className="rke-rgb-notice"><PanelSectionRow><Field
        label={message} bottomSeparator="none" /></PanelSectionRow></div>}
      {error && <div className="rke-rgb-error"><PanelSectionRow><Field
        label={draft ? "RGB change not saved" : "RGB control unavailable"}
        description={error}
        bottomSeparator="none" /></PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none"
          disabled={Boolean(busy || loading || !active)}
          onClick={reload}>Reload current RGB state</ButtonItem></PanelSectionRow>
      </div>}
    </PanelSection>
    <Heading title="Back to top" onActivate={backToTop} />
  </div>;
}
