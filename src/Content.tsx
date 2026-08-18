import {
  ButtonItem, ConfirmModal, DropdownItem, Field, PanelSection, PanelSectionRow,
  showModal, SliderField, Tabs, TextField,
} from "@decky/ui";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { activateGame, assignGame, deletePreset, getState, getTelemetry, getUpdateStatus, reinstallLatestRelease, renamePreset, restoreSteamDefault, savePreset, saveSystemFanCurve, setSteamDefault, unassignGame } from "./backend";
import { currentGame } from "./game";
import { Monitor } from "./Monitor";
import { Logs } from "./Logs";
import { styles } from "./styles";
import type { GameRef, HardwareProfile, State, Telemetry } from "./types";

const DEFAULT = "Steam Default";
const option = (data: string | number, label?: string) => ({ data, label: label ?? String(data) });
const cpuMhz = (khz: number) => `${Math.round(khz / 1000)} MHz`;
const gpuMhz = (hz: number) => `${Math.round(hz / 1_000_000)} MHz`;
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const SectionHeading = ({ children }: { children: string }) =>
  <div className="rke-section-heading">{children}</div>;

function ShoulderGlyph({ side }: { side: "LB" | "RB" }) {
  return side === "LB"
    ? <svg className="rke-shoulder-glyph" viewBox="0 0 32 32" aria-hidden="true">
      <path d="M0 10.8889C0 5.97969 2.98477 2 6.66667 2H30.6667C31.403 2 32 2.79594 32 3.77778V28.6667C32 29.6485 31.403 30.4444 30.6667 30.4444H1.33333C.596954 30.4444 0 29.6485 0 28.6667V10.8889Z" fill="white" />
      <path d="M15.8417 22.4445H8.32166V11.2445H10.6737V20.3325H15.8417V22.4445ZM22.901 11.2445V20.4125H25.029V22.4445H17.877V20.4125H20.517V13.8365L18.165 14.8285L17.413 13.0685L21.093 11.2445H22.901Z" fill="#0E141B" />
    </svg>
    : <svg className="rke-shoulder-glyph" viewBox="0 0 32 32" aria-hidden="true">
      <path d="M32 10.8889C32 5.97969 29.0152 2 25.3333 2H1.33333C.596952 2 0 2.79594 0 3.77778V28.6667C0 29.6485.596952 30.4444 1.33333 30.4444H30.6667C31.403 30.4444 32 29.6485 32 28.6667V10.8889Z" fill="white" />
      <path d="M16.669 22.4445H14.061L11.805 18.6685H9.86897V22.4445H7.51697V11.2445H11.709C14.695 11.2445 16.189 12.4071 16.189 14.7325C16.189 16.4605 15.469 17.6178 14.029 18.2045L16.669 22.4445ZM9.86897 13.2445V16.6525H11.709C13.021 16.6525 13.677 16.0605 13.677 14.8765C13.677 13.7885 12.973 13.2445 11.565 13.2445H9.86897ZM23.7057 11.2445V20.4125H25.8337V22.4445H18.6817V20.4125H21.3217V13.8365L18.9697 14.8285L18.2177 13.0685L21.8977 11.2445H23.7057Z" fill="#0E141B" />
    </svg>;
}

function SelectRow({ label, value, values, format, disabled, onChange }: {
  label: string; value: string | number; values: (string | number)[];
  format?: (value: number) => string; disabled?: boolean; onChange: (value: any) => void;
}) {
  return <PanelSectionRow><DropdownItem label={label} disabled={disabled} selectedOption={value}
    rgOptions={values.map(value => option(value, typeof value === "number" && format ? format(value) : String(value)))}
    onChange={(selected: any) => onChange(selected.data)} /></PanelSectionRow>;
}

export function Content() {
  const rootRef = useRef<HTMLDivElement>(null);
  const [tab, setTab] = useState("Monitor");
  const [state, setState] = useState<State | null>(null);
  const [selected, setSelected] = useState(DEFAULT);
  const [draft, setDraft] = useState<HardwareProfile | null>(null);
  const [game, setGame] = useState<GameRef | null>(currentGame());
  const [message, setMessage] = useState("Loading RK-Enhanced…");
  const [busy, setBusy] = useState(false);
  const [newName, setNewName] = useState("");
  const [renameName, setRenameName] = useState("");
  const [presetForm, setPresetForm] = useState<"new" | "rename" | null>(null);
  const [live, setLive] = useState<Telemetry | null>(null);
  const [systemCurve, setSystemCurve] = useState<HardwareProfile["fan_curve"]>([]);
  const [utility, setUtility] = useState<"Logs" | "Fan" | null>(null);
  const [updateStatus, setUpdateStatus] = useState("");

  const installState = useCallback((next: State, preferred?: string) => {
    setState(next);
    const wanted = preferred && next.presets[preferred] ? preferred
      : next.presets[selected] ? selected : next.active_preset;
    setSelected(wanted);
    setDraft(clone(next.presets[wanted]));
    setSystemCurve(clone(next.system_fan_curve));
  }, [selected]);

  const load = useCallback(async () => {
    try {
      const next = await getState();
      setState(next);
      const wanted = next.presets[next.active_preset] ? next.active_preset : DEFAULT;
      setSelected(wanted);
      setDraft(clone(next.presets[wanted]));
      setSystemCurve(clone(next.system_fan_curve));
      setGame(currentGame());
      setMessage("");
    } catch (error) { setMessage(String(error)); }
  }, [selected]);

  useEffect(() => {
    const boot = async () => {
      const running = currentGame();
      try { await activateGame(running?.appid || ""); } catch (_) {}
      await load();
    };
    void boot();
  }, []);
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const disableTabFocus = () => root.querySelectorAll<HTMLElement>('[role="tablist"], [role="tab"]')
      .forEach(element => {
        element.tabIndex = -1;
        element.setAttribute("data-force-navigable", "false");
        element.classList.remove("Focusable");
      });
    disableTabFocus();
    const observer = new MutationObserver(disableTabFocus);
    observer.observe(root, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let appid = game?.appid || "";
    const timer = window.setInterval(() => {
      const running = currentGame();
      const next = running?.appid || "";
      if (next === appid) return;
      appid = next;
      setGame(running);
      void activateGame(next).then(() => load()).catch(error => setMessage(String(error)));
    }, 2000);
    return () => window.clearInterval(timer);
  }, [game?.appid, load]);
  useEffect(() => {
    if (tab !== "Fan") return;
    let cancelled = false;
    const refresh = () => getTelemetry().then(value => { if (!cancelled) setLive(value); }).catch(() => {});
    void refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [tab]);
  useEffect(() => {
    if (tab !== "Utils") return;
    void getUpdateStatus().then(setUpdateStatus).catch(() => {});
  }, [tab]);

  const update = (change: (profile: HardwareProfile) => void) => setDraft(current => {
    if (!current) return current;
    const next = clone(current); change(next); return next;
  });
  const run = async (work: () => Promise<unknown>, success: string) => {
    setBusy(true);
    try { await work(); setMessage(success); }
    catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  };
  const createDraft = async () => {
    if (!draft || !newName.trim()) return;
    const name = newName.trim();
    setBusy(true);
    try {
      const next = await savePreset(name, draft);
      installState(next, name);
      setNewName("");
      setPresetForm(null);
      setMessage(`Created, saved and applied: ${name}`);
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  };

  if (!state || !draft) return <PanelSection title="RK-Enhanced"><Field label={message} /></PanelSection>;
  const names = Object.keys(state.presets);
  const cpuPolicies = state.capabilities.cpu;
  const gpu = state.capabilities.gpu;
  const assigned = game ? state.game_profiles[game.appid] : undefined;
  const dirty = JSON.stringify(draft) !== JSON.stringify(state.presets[selected]);
  const valid = cpuPolicies.length > 0;

  const saveAndApply = () => run(
    async () => installState(await savePreset(selected, draft), selected),
    `${selected} saved and applied`,
  );
  const saveApplyControl = () => <div className="rke-save-apply">
    <PanelSectionRow><ButtonItem layout="below" disabled={busy || !valid}
      onClick={() => void saveAndApply()}>Save &amp; Apply</ButtonItem></PanelSectionRow>
    <PanelSectionRow><Field label={`Saves and applies changes to ${selected}.`} /></PanelSectionRow>
  </div>;

  const choosePreset = (name: string) => {
    setSelected(name); setDraft(clone(state.presets[name])); setRenameName(""); setPresetForm(null); setMessage("");
  };
  const updateSystemCurve = (change: (curve: HardwareProfile["fan_curve"]) => void) =>
    setSystemCurve(current => { const next = clone(current); change(next); return next; });
  const addSystemCurvePoint = () => updateSystemCurve(curve => {
    const last = curve[curve.length - 1];
    if (last && last[0] < 120000) curve.push([Math.min(120000, last[0] + 10000), last[1]]);
  });
  const updateSystemTemp = (index: number, requested: number) => updateSystemCurve(curve => {
    const minimum = 10000 + index * 1000;
    const maximum = 120000 - (curve.length - 1 - index) * 1000;
    curve[index][0] = Math.max(minimum, Math.min(maximum, requested));
    for (let i = index + 1; i < curve.length; i++) curve[i][0] = Math.max(curve[i][0], curve[i - 1][0] + 1000);
    for (let i = index - 1; i >= 0; i--) curve[i][0] = Math.min(curve[i][0], curve[i + 1][0] - 1000);
  });
  const updateSystemPwm = (index: number, value: number) => updateSystemCurve(curve => {
    curve[index][1] = value;
    for (let i = index + 1; i < curve.length; i++) if (curve[i][1] < value) curve[i][1] = value;
    for (let i = index - 1; i >= 0; i--) if (curve[i][1] > value) curve[i][1] = value;
  });
  const addCurvePoint = () => update(profile => {
    const curve = profile.fan_curve;
    const last = curve[curve.length - 1];
    if (last && last[0] < 120000) {
      curve.push([Math.min(120000, last[0] + 10000), last[1]]);
      return;
    }
    let gapIndex = 0;
    for (let index = 1; index < curve.length; index++)
      if (curve[index][0] - curve[index - 1][0] > curve[gapIndex + 1][0] - curve[gapIndex][0]) gapIndex = index - 1;
    if (curve[gapIndex + 1][0] - curve[gapIndex][0] > 1000) {
      const low = curve[gapIndex], high = curve[gapIndex + 1];
      curve.splice(gapIndex + 1, 0, [Math.round((low[0] + high[0]) / 2000) * 1000, Math.round((low[1] + high[1]) / 2)]);
    }
  });
  const updateCurveTemp = (index: number, requested: number) => update(profile => {
    const curve = profile.fan_curve;
    const minimum = 10000 + index * 1000;
    const maximum = 120000 - (curve.length - 1 - index) * 1000;
    curve[index][0] = Math.max(minimum, Math.min(maximum, requested));
    for (let i = index + 1; i < curve.length; i++) curve[i][0] = Math.max(curve[i][0], curve[i - 1][0] + 1000);
    for (let i = index - 1; i >= 0; i--) curve[i][0] = Math.min(curve[i][0], curve[i + 1][0] - 1000);
  });
  const updateCurvePwm = (index: number, value: number) => update(profile => {
    const curve = profile.fan_curve;
    curve[index][1] = value;
    for (let i = index + 1; i < curve.length; i++) if (curve[i][1] < value) curve[i][1] = value;
    for (let i = index - 1; i >= 0; i--) if (curve[i][1] > value) curve[i][1] = value;
  });
  const confirmDelete = () => showModal(<ConfirmModal strTitle="Delete preset?"
    strDescription={`Delete “${selected}” and remove all of its game assignments?`}
    strOKButtonText="Delete" strCancelButtonText="Cancel"
    onOK={() => run(async () => { const next = await deletePreset(selected); installState(next, DEFAULT); }, "Preset deleted")} />);
  const confirmRestoreSteam = () => showModal(<ConfirmModal strTitle="Restore Steam Default?"
    strDescription="Replace Steam Default with the original ROCKNIX settings copied during setup?"
    strOKButtonText="Restore" strCancelButtonText="Cancel"
    onOK={() => run(async () => installState(await restoreSteamDefault(), DEFAULT), "Steam Default restored")} />);
  const confirmReinstall = () => showModal(<ConfirmModal strTitle="Reinstall latest RK-Enhanced?"
    strDescription="This closes Steam and any running game, downloads the newest GitHub release, backs up the current plugin, installs it, then relaunches Steam."
    strOKButtonText="Reinstall" strCancelButtonText="Cancel"
    onOK={() => run(async () => {
      await reinstallLatestRelease();
      setUpdateStatus("Update started. Steam will close shortly…");
    }, "Update started; Steam will restart")} />);

  const presets = <div className="rke-presets">
    <PanelSection>
      <SectionHeading>Game Assignment</SectionHeading>
      <PanelSectionRow><Field label={game ? game.name : "No game running"}
        description={game
          ? assigned ? `Assigned: ${assigned}` : `Uses ${state.steam_default}`
          : `Steam uses ${state.steam_default}`} /></PanelSectionRow>
      <SelectRow label="Steam default preset" value={state.steam_default} values={names} disabled={busy}
        onChange={(name: string) => run(async () => installState(await setSteamDefault(name), name), `Steam default preset set to ${name}`)} />
      {game && <PanelSectionRow><ButtonItem layout="below" disabled={busy}
        onClick={() => run(async () => installState(await assignGame(game.appid, selected), selected), `${selected} assigned to ${game.name}`)}>Assign selected preset</ButtonItem></PanelSectionRow>}
      {game && assigned && <PanelSectionRow><ButtonItem layout="below" disabled={busy}
        onClick={() => run(async () => installState(await unassignGame(game.appid), state.steam_default), `Assignment removed; ${state.steam_default} will be used`)}>Remove assignment</ButtonItem></PanelSectionRow>}
    </PanelSection>

    <PanelSection>
      <SelectRow label="Editing preset" value={selected} values={names} disabled={busy} onChange={choosePreset} />
      <PanelSectionRow><Field label={dirty ? "Unsaved changes" : selected === state.active_preset ? "Active preset" : "Saved preset"} /></PanelSectionRow>
      {saveApplyControl()}
      {selected === DEFAULT && <PanelSectionRow><ButtonItem layout="below" disabled={busy}
        onClick={confirmRestoreSteam}>Restore original Steam Default</ButtonItem></PanelSectionRow>}
      <PanelSectionRow><ButtonItem layout="below" disabled={busy}
        onClick={() => setPresetForm(current => current === "new" ? null : "new")}>{presetForm === "new" ? "Cancel new preset" : "New preset"}</ButtonItem></PanelSectionRow>
      {presetForm === "new" && <>
        <PanelSectionRow><TextField label="New preset name" value={newName} onChange={event => setNewName(event.target.value)} /></PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" disabled={busy || !newName.trim() || names.includes(newName.trim())}
          onClick={() => void createDraft()}>Create, save &amp; apply</ButtonItem></PanelSectionRow>
      </>}
      {selected !== DEFAULT && <>
        <PanelSectionRow><ButtonItem layout="below" disabled={busy}
          onClick={() => setPresetForm(current => current === "rename" ? null : "rename")}>{presetForm === "rename" ? "Cancel rename" : "Rename preset"}</ButtonItem></PanelSectionRow>
        {presetForm === "rename" && <>
          <PanelSectionRow><TextField label="New preset name" value={renameName} onChange={event => setRenameName(event.target.value)} /></PanelSectionRow>
          <PanelSectionRow><ButtonItem layout="below" disabled={busy || !renameName.trim() || names.includes(renameName.trim())}
            onClick={() => run(async () => { const name = renameName.trim(); installState(await renamePreset(selected, name), name); setRenameName(""); setPresetForm(null); }, "Preset renamed")}>Rename</ButtonItem></PanelSectionRow>
        </>}
        <div className="rke-action-button"><PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={confirmDelete}>Delete preset</ButtonItem></PanelSectionRow></div>
      </>}
      {message && <PanelSectionRow><Field label={message} /></PanelSectionRow>}
    </PanelSection>

  </div>;

  const performance = <div className="rke-performance">
    <PanelSection>
      <SectionHeading>CPU</SectionHeading>
      <SelectRow label="Governor" value={draft.cpu_governor} values={state.capabilities.cpu_governors}
        onChange={value => update(profile => { profile.cpu_governor = value; })} />
      {cpuPolicies.map((policy, index) => <div key={policy.id}>
        <div className="rke-cluster-heading">
          <div>Cluster {index + 1}</div>
          <small>Policy {policy.id} · cores {policy.cpus.join(", ")}</small>
        </div>
        <PanelSectionRow><SliderField label="Min" description={cpuMhz(draft.cpu_min[policy.id])}
          value={Math.max(0, policy.frequencies.indexOf(draft.cpu_min[policy.id]))}
          min={0} max={policy.frequencies.length - 1} step={1}
          onChange={index => update(profile => {
            const value = policy.frequencies[index];
            profile.cpu_min[policy.id] = value;
            if (value > profile.cpu_max[policy.id]) profile.cpu_max[policy.id] = value;
          })} /></PanelSectionRow>
        <PanelSectionRow><SliderField label="Max" description={cpuMhz(draft.cpu_max[policy.id])}
          value={Math.max(0, policy.frequencies.indexOf(draft.cpu_max[policy.id]))}
          min={0} max={policy.frequencies.length - 1} step={1}
          onChange={index => update(profile => {
            const value = policy.frequencies[index];
            profile.cpu_max[policy.id] = value;
            if (value < profile.cpu_min[policy.id]) profile.cpu_min[policy.id] = value;
          })} /></PanelSectionRow>
      </div>)}
      <SelectRow label="Scheduler" value={draft.cpu_scheduler} values={state.capabilities.schedulers}
        onChange={value => update(profile => { profile.cpu_scheduler = value; })} />
    </PanelSection>

    {saveApplyControl()}

    <PanelSection>
      <SectionHeading>GPU</SectionHeading>
      {!gpu.available ? <PanelSectionRow><Field label="GPU frequency control unavailable" /></PanelSectionRow> : <>
        <SelectRow label="Governor" value={draft.gpu_governor || ""} values={gpu.governors}
          onChange={value => update(profile => { profile.gpu_governor = value; })} />
        <SelectRow label="Maximum frequency" value={draft.gpu_max || gpu.maximum} values={gpu.frequencies} format={gpuMhz}
          onChange={value => update(profile => { profile.gpu_max = Number(value); })} />
      </>}
    </PanelSection>

    {saveApplyControl()}

  </div>;

  const fan = <div>
    {saveApplyControl()}
    <PanelSection>
      <SelectRow label="ROCKNIX cooling profile" value={draft.cooling_profile} values={state.capabilities.cooling_profiles}
        disabled={!state.capabilities.fan_available}
        onChange={value => update(profile => { profile.cooling_profile = value; })} />
      {!state.capabilities.fan_available && <PanelSectionRow><Field label="Fan control unavailable on this device" /></PanelSectionRow>}
      {draft.cooling_profile === "custom" && <>
        <PanelSectionRow><Field label={`${selected} custom curve`}
          description="Saved inside this preset. It temporarily replaces fancontrol.conf while this preset is active." /></PanelSectionRow>
        {draft.fan_curve.map(([temp, pwm], index) => <div key={index}>
          <PanelSectionRow><SliderField label={`Point ${index + 1} temperature`} description={`${Math.round(temp / 1000)}°C`}
            value={temp} min={10000 + index * 1000} max={120000 - (draft.fan_curve.length - 1 - index) * 1000}
            step={1000} showValue onChange={value => updateCurveTemp(index, value)} /></PanelSectionRow>
          <PanelSectionRow><SliderField label={`Point ${index + 1} PWM`} description={`${Math.round(pwm * 100 / 255)}%`}
            value={pwm} min={0} max={255} step={1} showValue onChange={value => updateCurvePwm(index, value)} /></PanelSectionRow>
          {draft.fan_curve.length > 2 && <PanelSectionRow><ButtonItem layout="below"
            onClick={() => update(profile => { profile.fan_curve.splice(index, 1); })}>Remove point</ButtonItem></PanelSectionRow>}
        </div>)}
        {draft.fan_curve.length < 16 && <PanelSectionRow><ButtonItem layout="below" onClick={addCurvePoint}>Add curve point</ButtonItem></PanelSectionRow>}
      </>}
      <PanelSectionRow><Field label="Live fan PWM">
        <span style={{ fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>
          {live ? `${live.fan_pwm} PWM · ${live.fan_percent}%` : "Reading…"}
        </span>
      </Field></PanelSectionRow>
    </PanelSection>
    {saveApplyControl()}
  </div>;

  const utils = <div className="rke-utils">
    <PanelSection>
      <PanelSectionRow><ButtonItem layout="below" onClick={() => setUtility(current => current === "Logs" ? null : "Logs")}>
        {utility === "Logs" ? "Hide logs" : "Logs"}
      </ButtonItem></PanelSectionRow>
      {utility === "Logs" && <Logs />}
      <PanelSectionRow><ButtonItem layout="below" onClick={() => setUtility(current => current === "Fan" ? null : "Fan")}>
        {utility === "Fan" ? "Hide ROCKNIX Custom fan curve" : "ROCKNIX Custom fan curve"}
      </ButtonItem></PanelSectionRow>
      {utility === "Fan" && <>
        <PanelSectionRow><Field label="Native ROCKNIX custom profile"
          description="Edits /storage/.config/fancontrol.conf" /></PanelSectionRow>
        {systemCurve.map(([temp, pwm], index) => <div key={index}>
          <PanelSectionRow><SliderField label={`Point ${index + 1} temperature`} description={`${Math.round(temp / 1000)}°C`}
            value={temp} min={10000 + index * 1000} max={120000 - (systemCurve.length - 1 - index) * 1000}
            step={1000} showValue onChange={value => updateSystemTemp(index, value)} /></PanelSectionRow>
          <PanelSectionRow><SliderField label={`Point ${index + 1} PWM`} description={`${Math.round(pwm * 100 / 255)}%`}
            value={pwm} min={0} max={255} step={1} showValue onChange={value => updateSystemPwm(index, value)} /></PanelSectionRow>
          {systemCurve.length > 2 && <PanelSectionRow><ButtonItem layout="below"
            onClick={() => updateSystemCurve(curve => { curve.splice(index, 1); })}>Remove point</ButtonItem></PanelSectionRow>}
        </div>)}
        {systemCurve.length < 16 && <PanelSectionRow><ButtonItem layout="below" onClick={addSystemCurvePoint}>Add point above hottest</ButtonItem></PanelSectionRow>}
        <PanelSectionRow><ButtonItem layout="below" disabled={busy || systemCurve.length < 2}
          onClick={() => run(async () => installState(await saveSystemFanCurve(systemCurve), selected), "ROCKNIX Custom fan curve saved")}>Save system fan curve</ButtonItem></PanelSectionRow>
      </>}
      <PanelSectionRow><Field label="GitHub releases"
        description="github.com/mrdidit/RK-Enhanced/releases" /></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy}
        onClick={confirmReinstall}>Reinstall latest release</ButtonItem></PanelSectionRow>
      {updateStatus && <PanelSectionRow><Field label={updateStatus} /></PanelSectionRow>}
    </PanelSection>
  </div>;

  const tabContent = (content: ReactNode) => <div className="rke-content">{content}</div>;
  const tabs = [
    { id: "Monitor", title: "Monitor", content: tabContent(<Monitor active={tab === "Monitor"} />) },
    { id: "Performance", title: "Performance", content: tabContent(performance) },
    { id: "Fan", title: "Fan Curves", content: tabContent(fan) },
    { id: "Presets", title: "Presets", content: tabContent(presets) },
    { id: "Utils", title: "Utils", content: tabContent(utils) },
  ];
  const activeTitle = tabs.find(item => item.id === tab)?.title || tab;
  return <div ref={rootRef} className="rke-tabs"><style>{styles}</style>
    <div className="rke-tab-header" aria-hidden="true">
      <div className="rke-tab-menu">
        <ShoulderGlyph side="LB" />
        <span className="rke-active-tab">{activeTitle}</span>
        <ShoulderGlyph side="RB" />
      </div>
    </div>
    <Tabs activeTab={tab} onShowTab={setTab} tabs={tabs} />
  </div>;
}
