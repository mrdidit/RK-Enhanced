import {
  ButtonItem, DropdownItem, Field, PanelSection, PanelSectionRow,
  SliderField, ToggleField,
} from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import type { Ref } from "react";
import { getRgbState, setRgbState } from "./backend";
import type { RgbColor, RgbEffect, RgbMode, RgbRequest, RgbState } from "./types";

const MODE_LABELS: Record<RgbMode, string> = {
  off: "Off",
  battery: "Battery",
  rgb: "RGB",
};
const EFFECT_LABELS: Record<RgbEffect, string> = {
  static: "Static",
  breath: "Breath",
  rainbow: "Rainbow",
};
const option = <T extends string>(data: T, label: string) => ({ data, label });

const requestFromState = (state: RgbState): RgbRequest | null =>
  state.supported && state.valid && state.provider !== "none" && state.mode !== "unknown" ? {
    provider: state.provider,
    revision: state.revision,
    mode: state.mode,
    effect: state.effect,
    color: [...state.color],
    brightness: state.brightness,
    correction: state.correction,
  } : null;

const sameRequest = (left: RgbRequest | null, right: RgbRequest | null) =>
  left === right || Boolean(left && right && left.provider === right.provider &&
    left.revision === right.revision && left.mode === right.mode && left.effect === right.effect &&
    left.brightness === right.brightness && left.correction === right.correction &&
    left.color.every((value, index) => value === right.color[index]));

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

export function RGB({ active }: { active: boolean }) {
  const [status, setStatus] = useState<RgbState | null>(null);
  const [saved, setSaved] = useState<RgbRequest | null>(null);
  const [draft, setDraft] = useState<RgbRequest | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [refreshRequest, setRefreshRequest] = useState(0);
  const generation = useRef(0);
  const busyRef = useRef(false);
  const activeRef = useRef(active);
  const needsRefreshAfterStaleApply = useRef(false);
  const topRef = useRef<HTMLDivElement>(null);
  const dirty = !sameRequest(saved, draft);
  const dirtyRef = useRef(dirty);
  activeRef.current = active;
  dirtyRef.current = dirty;

  useEffect(() => {
    const requestGeneration = ++generation.current;
    if (!active) {
      setLoading(false);
      return;
    }
    // Keep an unfinished controller edit when Quick Access or this tab was hidden.
    // A write completed outside its original activation is the sole exception:
    // its current backend state is authoritative over that stale draft.
    const forceRefresh = needsRefreshAfterStaleApply.current;
    if (dirtyRef.current && !forceRefresh) return;
    needsRefreshAfterStaleApply.current = false;
    setStatus(null);
    setSaved(null);
    setDraft(null);
    setLoading(true);
    setMessage("");
    setError("");
    void getRgbState().then(next => {
      if (requestGeneration !== generation.current) return;
      const request = requestFromState(next);
      setStatus(next);
      setSaved(request);
      setDraft(request);
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

  const update = (change: (request: RgbRequest) => void) => {
    setMessage("");
    setDraft(current => {
      if (!current) return current;
      const next: RgbRequest = { ...current, color: [...current.color] };
      change(next);
      return next;
    });
  };
  const updateColor = (index: number, value: number) => update(request => {
    request.color[index] = value;
  });
  const reload = () => {
    if (!active || busyRef.current) return;
    needsRefreshAfterStaleApply.current = true;
    setRefreshRequest(current => current + 1);
  };
  const apply = async () => {
    if (!active || !draft || busyRef.current) return;
    const requestGeneration = generation.current;
    busyRef.current = true;
    setBusy(true);
    setMessage("");
    setError("");
    try {
      const next = await setRgbState({ ...draft, color: [...draft.color] });
      if (requestGeneration !== generation.current) {
        needsRefreshAfterStaleApply.current = true;
        return;
      }
      const applied = requestFromState(next);
      setStatus(next);
      if (!applied) {
        setError(next.error || "RGB settings could not be applied.");
        return;
      }
      setSaved(applied);
      setDraft(applied);
      setMessage("RGB settings saved and applied");
    } catch (reason) {
      if (requestGeneration === generation.current) setError(String(reason));
      else needsRefreshAfterStaleApply.current = true;
    } finally {
      busyRef.current = false;
      setBusy(false);
      if (requestGeneration !== generation.current && activeRef.current)
        setRefreshRequest(current => current + 1);
    }
  };

  const modes = status?.modes || [];
  const effects = status?.effects || [];
  const analogStatic = status?.provider === "analog-static";
  const maxBrightness = Math.max(1, status?.max_brightness || 255);
  const showColour = draft?.mode === "rgb" && draft.effect !== "rainbow";
  const backToTop = () => {
    topRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    topRef.current?.focus();
  };

  return <div className="rke-rgb">
    <PanelSection>
      <Heading title={analogStatic ? "Stick RGB" : "RGB Control"} headingRef={topRef} />
      {loading && !draft && <PanelSectionRow><Field label="Reading RGB settings…"
        bottomSeparator="none" /></PanelSectionRow>}
      {draft && <>
        {analogStatic
          ? <PanelSectionRow><ToggleField label="Stick lighting"
            description="Independent of the system battery indicator."
            bottomSeparator="none" disabled={busy || !active}
            checked={draft.mode === "rgb"}
            onChange={checked => update(request => {
              request.mode = checked ? "rgb" : "off";
              request.effect = "static";
            })} />
          </PanelSectionRow>
          : <PanelSectionRow><DropdownItem label="LED Color" bottomSeparator="none"
            disabled={busy || !active}
            selectedOption={draft.mode}
            rgOptions={modes.map(mode => option(mode, MODE_LABELS[mode]))}
            onChange={(selected: any) => update(request => { request.mode = selected.data; })} />
          </PanelSectionRow>}
        {analogStatic && status?.zones_differ && <PanelSectionRow><Field
          label="Saved ring colours differ"
          description="Showing the right-stick colour. Save & Apply uses one colour for both rings."
          bottomSeparator="none" />
        </PanelSectionRow>}
        {draft.mode === "rgb" && <>
          {effects.length > 1 && <PanelSectionRow><DropdownItem label="Effect" bottomSeparator="none"
              disabled={busy || !active}
              selectedOption={draft.effect}
              rgOptions={effects.map(effect => option(effect, EFFECT_LABELS[effect]))}
              onChange={(selected: any) => update(request => { request.effect = selected.data; })} />
            </PanelSectionRow>}
          {showColour && <>
            <PanelSectionRow><Field label="Colour" bottomSeparator="none">
              <span className="rke-rgb-colour-value">
                <span>{hexColour(draft.color)}</span>
                <span className="rke-rgb-swatch" style={{ backgroundColor: hexColour(draft.color) }} />
              </span>
            </Field></PanelSectionRow>
            {(["Red", "Green", "Blue"] as const).map((name, index) =>
              <PanelSectionRow key={name}><SliderField
                label={<ValueLabel name={name} value={String(draft.color[index])} />}
                bottomSeparator="none" disabled={busy || !active}
                value={draft.color[index]} min={0} max={255} step={1}
                minimumDpadGranularity={1}
                onChange={value => updateColor(index, value)} />
              </PanelSectionRow>)}
            {draft.effect === "static" && <PanelSectionRow><SliderField
              label={<ValueLabel name="Brightness"
                value={`${Math.round(draft.brightness * 100 / maxBrightness)}%`} />}
              bottomSeparator="none" disabled={busy || !active}
              value={draft.brightness} min={analogStatic ? 1 : 0} max={maxBrightness} step={1}
              minimumDpadGranularity={1}
              onChange={value => update(request => { request.brightness = value; })} />
            </PanelSectionRow>}
            <PanelSectionRow><ToggleField label="Colour correction"
              description="When red is used, green and blue output are reduced to 80%."
              bottomSeparator="none" disabled={busy || !active}
              checked={draft.correction}
              onChange={checked => update(request => { request.correction = checked; })} />
            </PanelSectionRow>
          </>}
          {draft.effect === "rainbow" && <PanelSectionRow><Field
            label="MCU-controlled effect" bottomSeparator="none"
            description="Rainbow has no adjustable colour or brightness." />
          </PanelSectionRow>}
        </>}
        <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none"
          disabled={busy || loading || !active || !status?.supported || !status.valid}
          onClick={() => { void apply(); }}>Save &amp; Apply</ButtonItem></PanelSectionRow>
        {dirty && <PanelSectionRow><Field label="Unsaved RGB changes"
          bottomSeparator="none" /></PanelSectionRow>}
      </>}
      {message && <div className="rke-rgb-notice"><PanelSectionRow><Field
        label={message} bottomSeparator="none" /></PanelSectionRow></div>}
      {error && <div className="rke-rgb-error"><PanelSectionRow><Field
        label="RGB control unavailable" description={error}
        bottomSeparator="none" /></PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none"
          disabled={busy || loading || !active}
          onClick={reload}>Reload current RGB state</ButtonItem></PanelSectionRow>
      </div>}
    </PanelSection>
    <Heading title="Back to top" onActivate={backToTop} />
  </div>;
}
