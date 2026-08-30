import {
  ButtonItem, ConfirmModal, DropdownItem, Field, PanelSection, PanelSectionRow,
  showModal, SliderField, Tabs, TextField,
  useQuickAccessVisible as useLegacyQuickAccessVisible,
} from "@decky/ui";
import { useQuickAccessVisible as useLoaderQuickAccessVisible } from "@decky/api";
import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode, Ref } from "react";
import { acknowledgeInstallStatus, activateGame, assignGame, cleanOldBackups, deletePreset, getBackupCleanupInfo, getDeviceNetworkInfo, getInstallStatus, getPluginConflict, getState, getTelemetry, getUpdateInfo, installRelease, lockExperimental, removePluginConflict, renamePreset, restoreSteamDefault, savePreset, saveSystemFanCurve, setSteamDefault, unassignGame, unlockExperimental } from "./backend";
import { Experimental } from "./Experimental";
import { currentGame } from "./game";
import { chooseInstallProgress, isNewInstallTransaction } from "./installProgressModel";
import { Monitor } from "./Monitor";
import { RGB } from "./RGB";
import { Logs } from "./Logs";
import { styles } from "./styles";
import type { BackupCleanupInfo, DeviceNetworkInfo, GameRef, HardwareProfile, InstallProgress, PluginConflictStatus, State, Telemetry, UpdateInfo } from "./types";

const DEFAULT = "RK-E Default";
const useQuickAccessVisibleCompat =
  typeof useLoaderQuickAccessVisible === "function"
    ? useLoaderQuickAccessVisible
    : useLegacyQuickAccessVisible;
const option = (data: string | number, label?: string) => ({ data, label: label ?? String(data) });
const cpuMhz = (khz: number) => `${Math.round(khz / 1000)} MHz`;
const gpuMhz = (hz: number) => `${Math.round(hz / 1_000_000)} MHz`;
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const emptyConflict: PluginConflictStatus = { blocked: false, conflicts: [], message: "" };
const formatBytes = (bytes: number) => {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const exponent = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  const value = bytes / (1024 ** exponent);
  return `${value >= 10 || exponent === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[exponent]}`;
};

const installSteps = [
  { key: "downloading", label: "Downloading", phases: ["starting", "preflight", "checking-releases", "downloading"] },
  { key: "validating", label: "Validating", phases: ["validating"] },
  { key: "backing-up", label: "Backing up", phases: ["backing-up"] },
  { key: "installing", label: "Installing", phases: ["stopping-decky", "installing"] },
  { key: "reloading-decky", label: "Reloading Decky", phases: ["starting-decky"] },
  { key: "verifying", label: "Verifying", phases: ["verifying", "rolling-back"] },
] as const;
const conflictRemovalSteps = [
  { key: "checking-conflict", label: "Checking conflict", phases: ["starting", "preflight"] },
  { key: "stopping-decky", label: "Stopping Decky", phases: ["stopping-decky"] },
  { key: "removing-conflict", label: "Removing conflict", phases: ["removing-conflict"] },
  { key: "reloading-decky", label: "Reloading Decky", phases: ["starting-decky"] },
] as const;

function InstallProgressView({ status, launching, launchError, onDismiss }: {
  status: InstallProgress | null;
  launching: boolean;
  launchError: string;
  onDismiss: () => Promise<void>;
}) {
  const [dismissBusy, setDismissBusy] = useState(false);
  const [dismissError, setDismissError] = useState("");
  const phase = status?.phase || "starting";
  const steps = status?.kind === "remove-conflict" ? conflictRemovalSteps
    : phase === "removing-conflict"
      ? [...installSteps.slice(0, 3), { key: "removing-conflict", label: "Removing conflict", phases: ["removing-conflict"] }, ...installSteps.slice(3)]
      : installSteps;
  const currentIndex = steps.findIndex(step => (step.phases as readonly string[]).includes(phase));
  const terminal = Boolean(status?.terminal || launchError);
  const completed = status?.success || status?.outcome === "succeeded" || phase === "completed";
  const rolledBack = status?.rolled_back || status?.outcome === "rolled-back" || phase === "rolled-back";
  const rollingBack = phase === "rolling-back";
  const outcomeLabel = launchError ? "Installer did not start"
    : completed ? "Completed" : rolledBack ? "Rolled back" : rollingBack ? "Rolling back"
    : status?.outcome === "blocked" || phase === "blocked" ? "Blocked"
      : status?.outcome === "failed" || phase === "failed" ? "Failed" : "Installation in progress";
  const outcomeClass = completed ? " rke-install-success"
    : terminal ? " rke-install-failed" : "";

  return <div className="rke-install-blocker">
    <style>{styles}</style>
    <PanelSection>
      <PerformanceHeading title={outcomeLabel} />
      {(status?.source_version || status?.target_version) && <PanelSectionRow><Field
        label={`${status.source_version || "Unknown"} → ${status.target_version || "Unknown"}`}
        description={status.decky_version ? `Decky ${status.decky_version}` : undefined} />
      </PanelSectionRow>}
      <div className={`rke-install-phases${outcomeClass}`}>
        {steps.map((step, index) => {
          const stepCurrent = !terminal && index === currentIndex;
          const stepDone = completed || (currentIndex >= 0 && index < currentIndex)
            || (rolledBack && index <= currentIndex);
          return <div className={`rke-install-phase${stepCurrent ? " rke-install-phase-current" : ""}${stepDone ? " rke-install-phase-done" : ""}`}
            key={step.key}>
            <span className="rke-install-phase-mark">{stepDone ? "✓" : stepCurrent ? "●" : "○"}</span>
            <span>{rollingBack && step.key === "verifying" ? "Rolling back" : step.label}</span>
          </div>;
        })}
        {terminal && <div className={`rke-install-phase rke-install-phase-current${completed ? " rke-install-phase-done" : ""}`}>
          <span className="rke-install-phase-mark">{completed ? "✓" : rolledBack ? "↶" : "!"}</span>
          <span>{outcomeLabel}</span>
        </div>}
      </div>
      <PanelSectionRow><Field label={launchError || (launching && !status ? "Starting installer…" : status?.message || outcomeLabel)}
        description={launchError
          ? "No new persisted transaction appeared. Check the Installer log before retrying."
          : status?.error || (!terminal ? "Decky may briefly disappear while it reloads. Progress resumes when the frontend returns." : undefined)} />
      </PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" onClick={() => showModal(<Logs />)}>Installer log</ButtonItem></PanelSectionRow>
      {dismissError && <PanelSectionRow><Field label="Could not dismiss installer result" description={dismissError} /></PanelSectionRow>}
      {terminal && <PanelSectionRow><ButtonItem layout="below" disabled={dismissBusy} onClick={() => {
        setDismissBusy(true);
        setDismissError("");
        void onDismiss().catch(error => setDismissError(String(error))).finally(() => setDismissBusy(false));
      }}>{dismissBusy ? "Saving…" : "Continue"}</ButtonItem></PanelSectionRow>}
    </PanelSection>
  </div>;
}
function ShoulderGlyph({ side }: { side: "LB" | "RB" }) {
  return side === "LB"
    ? <svg className="rke-shoulder-glyph" viewBox="0 0 48 32" aria-hidden="true">
      <path d="M0 10.8889C0 5.97969 2.98477 2 6.66667 2H46.6667C47.403 2 48 2.79594 48 3.77778V28.6667C48 29.6485 47.403 30.4444 46.6667 30.4444H1.33333C.596954 30.4444 0 29.6485 0 28.6667V10.8889Z" fill="white" />
      <path transform="translate(8 0)" d="M15.8417 22.4445H8.32166V11.2445H10.6737V20.3325H15.8417V22.4445ZM22.901 11.2445V20.4125H25.029V22.4445H17.877V20.4125H20.517V13.8365L18.165 14.8285L17.413 13.0685L21.093 11.2445H22.901Z" fill="#0E141B" />
    </svg>
    : <svg className="rke-shoulder-glyph" viewBox="0 0 48 32" aria-hidden="true">
      <path d="M48 10.8889C48 5.97969 45.0152 2 41.3333 2H1.33333C.596952 2 0 2.79594 0 3.77778V28.6667C0 29.6485.596952 30.4444 1.33333 30.4444H46.6667C47.403 30.4444 48 29.6485 48 28.6667V10.8889Z" fill="white" />
      <path transform="translate(8 0)" d="M16.669 22.4445H14.061L11.805 18.6685H9.86897V22.4445H7.51697V11.2445H11.709C14.695 11.2445 16.189 12.4071 16.189 14.7325C16.189 16.4605 15.469 17.6178 14.029 18.2045L16.669 22.4445ZM9.86897 13.2445V16.6525H11.709C13.021 16.6525 13.677 16.0605 13.677 14.8765C13.677 13.7885 12.973 13.2445 11.565 13.2445H9.86897ZM23.7057 11.2445V20.4125H25.8337V22.4445H18.6817V20.4125H21.3217V13.8365L18.9697 14.8285L18.2177 13.0685L21.8977 11.2445H23.7057Z" fill="#0E141B" />
    </svg>;
}

const isTabFocusTarget = (target: EventTarget | null) =>
  target instanceof HTMLElement && Boolean(target.closest('[role="tablist"], [role="tab"]'));

function SelectRow({ label, value, values, format, disabled, bottomSeparator, onChange }: {
  label: string; value: string | number; values: (string | number)[];
  format?: (value: number) => string; disabled?: boolean;
  bottomSeparator?: "standard" | "thick" | "none";
  onChange: (value: any) => void;
}) {
  return <PanelSectionRow><DropdownItem label={label} disabled={disabled} selectedOption={value}
    bottomSeparator={bottomSeparator}
    rgOptions={values.map(value => option(value, typeof value === "number" && format ? format(value) : String(value)))}
    onChange={(selected: any) => onChange(selected.data)} /></PanelSectionRow>;
}

const PerformanceHeading = ({ title, detail, headingRef, onActivate }: {
  title: string;
  detail?: string;
  headingRef?: Ref<HTMLDivElement>;
  onActivate?: () => void;
}) => <div className="rke-performance-heading-row">
  <Field ref={headingRef} className="rke-performance-heading" focusable highlightOnFocus
    bottomSeparator="none"
    label={<span className="rke-performance-heading-label">
      <span>{title}</span>
      {detail && <small>{detail}</small>}
    </span>}
    onActivate={onActivate} onClick={onActivate} />
</div>;

const FrequencyLabel = ({ name, value }: { name: string; value: string }) =>
  <span className="rke-frequency-label"><span>{name}</span><span>{value}</span></span>;

export function Content() {
  const panelVisible = useQuickAccessVisibleCompat();
  const [tab, setTab] = useState("Monitor");
  const [tabBarFocused, setTabBarFocused] = useState(false);
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
  const [utility, setUtility] = useState<"Fan" | null>(null);
  const [deviceNetwork, setDeviceNetwork] = useState<DeviceNetworkInfo | null>(null);
  const [deviceNetworkError, setDeviceNetworkError] = useState(false);
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null);
  const [updateError, setUpdateError] = useState("");
  const [installProgress, setInstallProgress] = useState<InstallProgress | null>(null);
  const [installLaunching, setInstallLaunching] = useState(false);
  const [installLaunchError, setInstallLaunchError] = useState("");
  const [dismissedTransaction, setDismissedTransaction] = useState("");
  const [pluginConflict, setPluginConflict] = useState<PluginConflictStatus>(emptyConflict);
  const [backupInfo, setBackupInfo] = useState<BackupCleanupInfo | null>(null);
  const [backupError, setBackupError] = useState("");
  const [experimentalCode, setExperimentalCode] = useState("");
  const [showExperimentalUnlock, setShowExperimentalUnlock] = useState(false);
  const performanceTopRef = useRef<HTMLDivElement>(null);
  const fanTopRef = useRef<HTMLDivElement>(null);
  const installProgressRef = useRef<InstallProgress | null>(null);
  const installRefreshRef = useRef<Promise<InstallProgress> | null>(null);
  const installState = useCallback((next: State, preferred?: string) => {
    setState(next);
    if (next.plugin_conflict) setPluginConflict(next.plugin_conflict);
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
      if (next.plugin_conflict) setPluginConflict(next.plugin_conflict);
      const wanted = next.presets[next.active_preset] ? next.active_preset : DEFAULT;
      setSelected(wanted);
      setDraft(clone(next.presets[wanted]));
      setSystemCurve(clone(next.system_fan_curve));
      setGame(currentGame());
      setMessage("");
      return true;
    } catch (error) {
      setMessage(String(error));
      return false;
    }
  }, [selected]);

  const refreshInstallProgress = useCallback(() => {
    if (installRefreshRef.current) return installRefreshRef.current;
    const request = getInstallStatus().then(next => {
      const accepted = chooseInstallProgress(installProgressRef.current, next);
      installProgressRef.current = accepted;
      setInstallProgress(accepted);
      return accepted;
    });
    installRefreshRef.current = request;
    const clear = () => {
      if (installRefreshRef.current === request) installRefreshRef.current = null;
    };
    void request.then(clear, clear);
    return request;
  }, []);
  const refreshConflict = useCallback(async () => {
    const conflict = await getPluginConflict();
    setPluginConflict(conflict);
    return conflict;
  }, []);
  const refreshBackupInfo = useCallback(async () => {
    setBackupError("");
    try {
      const info = await getBackupCleanupInfo();
      setBackupInfo(info);
      return info;
    } catch (error) {
      setBackupError(String(error));
      throw error;
    }
  }, []);

  const progressBlocksUi = installLaunching || Boolean(installLaunchError)
    || Boolean(installProgress?.active)
    || Boolean(installProgress?.terminal && !installProgress.acknowledged
      && installProgress.transaction_id
      && installProgress.transaction_id !== dismissedTransaction);
  const runtimeMutationBlocked = Boolean(state?.mutations_blocked) || pluginConflict.blocked;
  const mutationBlocked = installLaunching || Boolean(installProgress?.active)
    || runtimeMutationBlocked;
  const conflictAutoRemovable = pluginConflict.blocked && pluginConflict.conflicts.length > 0
    && pluginConflict.conflicts.every(conflict => conflict.removable !== false);
  const installActionBlocked = Boolean(state?.mutations_blocked) && !pluginConflict.blocked;

  useEffect(() => {
    let cancelled = false;
    const boot = async () => {
      const running = currentGame();
      let progress: InstallProgress | null = null;
      let conflict = emptyConflict;
      try { progress = await refreshInstallProgress(); } catch (_) {}
      try { conflict = await refreshConflict(); } catch (_) {}
      if (!progress?.active && !conflict.blocked) {
        try { await activateGame(running?.appid || ""); } catch (_) {}
      }
      for (let attempt = 0; attempt < 45 && !cancelled; attempt += 1) {
        if (await load()) return;
        if (attempt < 44)
          await new Promise(resolve => window.setTimeout(resolve, 1000));
      }
    };
    void boot();
    return () => { cancelled = true; };
  }, []);
  useEffect(() => {
    let appid = game?.appid || "";
    const timer = window.setInterval(() => {
      const running = currentGame();
      const next = running?.appid || "";
      if (next === appid) return;
      appid = next;
      setGame(running);
      if (mutationBlocked) return;
      void activateGame(next).then(() => load()).catch(error => setMessage(String(error)));
    }, 2000);
    return () => window.clearInterval(timer);
  }, [game?.appid, load, mutationBlocked]);
  useEffect(() => {
    if (!panelVisible || tab !== "Fan" || progressBlocksUi) return;
    let cancelled = false;
    const refresh = () => getTelemetry().then(value => { if (!cancelled) setLive(value); }).catch(() => {});
    void refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [panelVisible, progressBlocksUi, tab]);
  useEffect(() => {
    if (tab === "Experimental" && state && !state.experimental_unlocked)
      setTab("Utils");
  }, [tab, state?.experimental_unlocked]);
  useEffect(() => {
    if (tab === "RGB" && state && !state.capabilities.rgb?.available)
      setTab("Utils");
  }, [tab, state?.capabilities.rgb?.available]);
  useEffect(() => {
    if (tab !== "Utils") return;
    setUpdateError("");
    void getUpdateInfo().then(info => {
      setUpdateInfo(info);
      setUpdateError(info.error);
    }).catch(reason => setUpdateError(String(reason)));
    void refreshBackupInfo().catch(() => {});
  }, [refreshBackupInfo, tab]);
  useEffect(() => {
    if (!panelVisible) return;
    let cancelled = false;
    const refresh = () => refreshInstallProgress().catch(() => null);
    void refresh();
    const timer = window.setInterval(() => {
      if (!cancelled && (installLaunching || installProgress?.active)) void refresh();
    }, 1000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [installLaunching, installProgress?.active, panelVisible, refreshInstallProgress]);
  useEffect(() => {
    if (!panelVisible || progressBlocksUi) return;
    let cancelled = false;
    const refresh = () => refreshConflict().catch(() => null);
    void refresh();
    const timer = window.setInterval(() => { if (!cancelled) void refresh(); }, 5000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [panelVisible, progressBlocksUi, refreshConflict]);
  useEffect(() => {
    if (!runtimeMutationBlocked) return;
    if (!["Monitor", "Utils"].includes(tab)) setTab("Utils");
  }, [runtimeMutationBlocked, tab]);
  useEffect(() => {
    if (!panelVisible || tab !== "Utils") return;
    let cancelled = false;
    setDeviceNetwork(null);
    setDeviceNetworkError(false);
    void getDeviceNetworkInfo().then(info => {
      if (!cancelled) setDeviceNetwork(info);
    }).catch(() => {
      if (!cancelled) setDeviceNetworkError(true);
    });
    return () => { cancelled = true; };
  }, [panelVisible, tab]);

  const update = (change: (profile: HardwareProfile) => void) => setDraft(current => {
    if (!current) return current;
    const next = clone(current); change(next); return next;
  });
  const run = async (work: () => Promise<unknown>, success: string) => {
    if (mutationBlocked) {
      setMessage(pluginConflict.blocked
        ? "Controls are blocked until ROCKNIX Control is removed."
        : installLaunching || installProgress?.active
          ? "Controls are blocked while installation is in progress."
          : "Controls are blocked by the backend safety gate.");
      return;
    }
    setBusy(true);
    try { await work(); setMessage(success); }
    catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  };
  const createDraft = async () => {
    if (mutationBlocked || !draft || !newName.trim()) return;
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
  if (progressBlocksUi) return <InstallProgressView
    status={installLaunchError ? null : installLaunching && installProgress?.acknowledged ? null : installProgress}
    launching={installLaunching}
    launchError={installLaunchError}
    onDismiss={async () => {
      if (installLaunchError) {
        setInstallLaunchError("");
        return;
      }
      const transactionId = installProgress?.transaction_id || "";
      if (!transactionId) throw new Error("Installer transaction is unavailable.");
      if (!await acknowledgeInstallStatus(transactionId))
        throw new Error("Backend did not acknowledge this installer result.");
      setDismissedTransaction(transactionId);
      await load();
      await refreshConflict();
    }} />;
  if (!state || !draft) return <PanelSection title="RK-Enhanced"><Field label={message} /></PanelSection>;
  const names = Object.keys(state.presets);
  const cpuPolicies = state.capabilities.cpu;
  const gpu = state.capabilities.gpu;
  const assigned = game ? state.game_profiles[game.appid] : undefined;
  const dirty = JSON.stringify(draft) !== JSON.stringify(state.presets[selected]);
  const valid = cpuPolicies.length > 0;
  const effectiveCooling = live?.cooling_profile || state.effective_cooling_profile;
  const fanCanApply = effectiveCooling === "custom";
  const boostPolicies = cpuPolicies.filter(policy =>
    policy.boost_enabled && policy.boost_frequencies.length > 0);
  const selectedBoostLimits = boostPolicies
    .map(policy => draft.cpu_max[policy.id])
    .filter((frequency, index) => boostPolicies[index].boost_frequencies.includes(frequency));
  const hardwareBoostMaximum = Math.max(0, ...boostPolicies.flatMap(policy => policy.boost_frequencies));
  const selectedBoostMaximum = Math.max(0, ...selectedBoostLimits);
  const performanceBoost = draft.cpu_governor === "performance" && selectedBoostMaximum > 0;

  const saveAndApply = () => run(
    async () => installState(await savePreset(selected, draft), selected),
    `${selected} saved and applied`,
  );
  const saveApplyControl = (fanOnly = false, noSeparators = false) => <div className="rke-save-apply">
    <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked || !valid || (fanOnly && !fanCanApply)}
      bottomSeparator={noSeparators ? "none" : undefined}
      onClick={() => void saveAndApply()}>Save &amp; Apply</ButtonItem></PanelSectionRow>
    <PanelSectionRow><Field label={fanOnly && !fanCanApply
      ? "Unavailable until ROCKNIX cooling is set to Custom."
      : `Saves and applies changes to ${selected}.`}
      bottomSeparator={noSeparators ? "none" : undefined} /></PanelSectionRow>
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
  const confirmRestoreSteam = () => showModal(<ConfirmModal strTitle="Restore RK-E Default?"
    strDescription="Replace RK-E Default with the original settings and ROCKNIX Custom curve copied during setup?"
    strOKButtonText="Restore" strCancelButtonText="Cancel"
    onOK={() => run(async () => installState(await restoreSteamDefault(), DEFAULT), "RK-E Default restored")} />);
  const startReleaseInstall = async (target: string, removeConflict: boolean) => {
    if (installActionBlocked || (pluginConflict.blocked && !removeConflict)
      || installProgress?.active || installLaunching) return;
    const baseline = installProgressRef.current;
    setDismissedTransaction("");
    setInstallLaunchError("");
    setInstallLaunching(true);
    try {
      await installRelease(target, removeConflict);
      const deadline = Date.now() + 15_000;
      let lastError = "";
      while (Date.now() < deadline) {
        try {
          const progress = await refreshInstallProgress();
          if (isNewInstallTransaction(progress, baseline)) return;
        } catch (error) {
          lastError = String(error);
        }
        await new Promise(resolve => window.setTimeout(resolve, 300));
      }
      throw new Error(`Detached installer status timed out.${lastError ? ` ${lastError}` : ""}`);
    } catch (error) {
      setInstallLaunchError(`Installer launch failed: ${String(error)}`);
    } finally {
      setInstallLaunching(false);
    }
  };
  const confirmRelease = (target: string, action: "Update" | "Reinstall" | "Downgrade" | "Restore") => {
    const unsafe = action === "Downgrade" || action === "Restore";
    const removeConflict = pluginConflict.blocked && conflictAutoRemovable;
    const conflictWarning = removeConflict
      ? " A detected ROCKNIX Control plugin will be permanently removed without a backup before RK-Enhanced is installed."
      : "";
    showModal(<ConfirmModal
    strTitle={unsafe ? `${action === "Restore" ? "Restore" : "Unsafe downgrade to"} ${target}?` : `${action} ${target}?`}
    strDescription={unsafe
      ? `Older RK-Enhanced releases may directly write, capture, or restore charging state. First select Battery Normal and Pump Qualcomm/Normal in the current Experimental tab and confirm both statuses, then hide Experimental. Downgrade only for recovery and reboot immediately afterward. Keeping the current updater does not make the older charging backend safe. The current plugin will be backed up.${conflictWarning}`
      : `This downloads ${target} from GitHub, backs up the current plugin, installs it, then reloads Decky. RK-Enhanced controls will be briefly unavailable.${conflictWarning}`}
    strOKButtonText={removeConflict ? `Remove plugin & ${action}` : unsafe ? `${action} anyway` : action} strCancelButtonText="Cancel"
    onOK={() => { void startReleaseInstall(target, removeConflict); }} />);
  };
  const confirmConflictRemoval = () => {
    if (!conflictAutoRemovable) return;
    const detected = pluginConflict.conflicts.map(conflict =>
      `${conflict.name}${conflict.version ? ` ${conflict.version}` : ""}\n${conflict.directory}`).join("\n\n");
    showModal(<ConfirmModal strTitle="Remove conflicting plugin?"
      strDescription={`This may be the original ROCKNIX Control or Rocknix Control Enhanced; both use the same plugin identity and can compete with RK-Enhanced for CPU, GPU, and fan control.\n\n${detected}\n\nRemoval is permanent. No backup will be created.`}
      strOKButtonText="Remove permanently" strCancelButtonText="Cancel"
      onOK={() => {
        const baseline = installProgressRef.current;
        setInstallLaunching(true);
        setInstallLaunchError("");
        setDismissedTransaction("");
        void removePluginConflict().then(async result => {
          setMessage(result.message || "Conflict removal started; Decky will reload");
          const deadline = Date.now() + 15_000;
          let lastError = "";
          while (Date.now() < deadline) {
            try {
              const progress = await refreshInstallProgress();
              if (isNewInstallTransaction(progress, baseline)) return;
            } catch (error) {
              lastError = String(error);
            }
            await new Promise(resolve => window.setTimeout(resolve, 300));
          }
          throw new Error(`Detached removal status timed out.${lastError ? ` ${lastError}` : ""}`);
        }).catch(error => setInstallLaunchError(`Conflict removal launch failed: ${String(error)}`))
          .finally(() => setInstallLaunching(false));
      }} />);
  };
  const confirmBackupCleanup = () => {
    if (!backupInfo?.eligible_count || backupInfo.errors?.length) return;
    showModal(<ConfirmModal strTitle="Remove old RK-E backups?"
      strDescription={`Remove ${backupInfo.eligible_count} older backup${backupInfo.eligible_count === 1 ? "" : "s"} using ${formatBytes(backupInfo.eligible_bytes)}. The newest RK-Enhanced backup will be kept. Failed-install recovery files and Decky's active rollback data will not be touched.`}
      strOKButtonText="Clean old backups" strCancelButtonText="Cancel"
      onOK={() => {
        setBusy(true);
        void cleanOldBackups().then(info => {
          setBackupInfo(info);
          setMessage(`Removed ${info.removed_count || 0} old backup${info.removed_count === 1 ? "" : "s"} · ${formatBytes(info.removed_bytes || 0)} reclaimed`);
        }).catch(error => setMessage(String(error))).finally(() => setBusy(false));
      }} />);
  };

  const presets = <div className="rke-presets">
    <PanelSection>
      <PerformanceHeading title="Preset Management" />
      <SelectRow label="Editing preset" value={selected} values={names} disabled={busy || mutationBlocked} onChange={choosePreset} />
      <PanelSectionRow><Field label="Active preset"
        description={dirty ? "The editing preset has unsaved changes." : undefined}>
        <span style={{ display: "block", width: "100%", textAlign: "right", fontWeight: 600 }}>{state.active_preset}</span>
      </Field></PanelSectionRow>
      {saveApplyControl()}
      {selected === DEFAULT && <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked}
        onClick={confirmRestoreSteam}>Restore RK-E Default</ButtonItem></PanelSectionRow>}
      <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked}
        onClick={() => setPresetForm(current => current === "new" ? null : "new")}>{presetForm === "new" ? "Cancel new preset" : "New preset"}</ButtonItem></PanelSectionRow>
      {presetForm === "new" && <>
        <PanelSectionRow><TextField label="New preset name" value={newName} onChange={event => setNewName(event.target.value)} /></PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked || !newName.trim() || names.includes(newName.trim())}
          onClick={() => void createDraft()}>Create, save &amp; apply</ButtonItem></PanelSectionRow>
      </>}
      {selected !== DEFAULT && <>
        <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked}
          onClick={() => setPresetForm(current => current === "rename" ? null : "rename")}>{presetForm === "rename" ? "Cancel rename" : "Rename preset"}</ButtonItem></PanelSectionRow>
        {presetForm === "rename" && <>
          <PanelSectionRow><TextField label="New preset name" value={renameName} onChange={event => setRenameName(event.target.value)} /></PanelSectionRow>
          <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked || !renameName.trim() || names.includes(renameName.trim())}
            onClick={() => run(async () => { const name = renameName.trim(); installState(await renamePreset(selected, name), name); setRenameName(""); setPresetForm(null); }, "Preset renamed")}>Rename</ButtonItem></PanelSectionRow>
        </>}
        <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked} onClick={confirmDelete}>Delete preset</ButtonItem></PanelSectionRow>
      </>}
      <SelectRow label="Steam default preset" value={state.steam_default} values={names} disabled={busy || mutationBlocked}
        onChange={(name: string) => run(async () => installState(await setSteamDefault(name), name), `Steam default preset set to ${name}`)} />
      {message && <PanelSectionRow><Field label={message} /></PanelSectionRow>}
    </PanelSection>

    <PanelSection>
      <PerformanceHeading title="Game Assignment" />
      <PanelSectionRow><Field label={game ? game.name : "No game running"} /></PanelSectionRow>
      {game && <SelectRow label="Game preset" value={assigned || state.steam_default} values={names} disabled={busy || mutationBlocked}
        onChange={(name: string) => run(async () => installState(await assignGame(game.appid, name), name), `${name} assigned and applied to ${game.name}`)} />}
      {game && assigned && <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked}
        onClick={() => run(async () => installState(await unassignGame(game.appid), state.steam_default), `Assignment removed; ${state.steam_default} will be used`)}>Remove assignment</ButtonItem></PanelSectionRow>}
    </PanelSection>
  </div>;

  const backToPerformanceTop = () => {
    performanceTopRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    performanceTopRef.current?.focus();
  };

  const performance = <div className="rke-performance">
    <PanelSection>
      <PerformanceHeading title="CPU" headingRef={performanceTopRef} />
      <SelectRow label="Governor" value={draft.cpu_governor} values={state.capabilities.cpu_governors}
        disabled={mutationBlocked}
        bottomSeparator="none"
        onChange={value => update(profile => { profile.cpu_governor = value; })} />
      <SelectRow label="Scheduler" value={draft.cpu_scheduler} values={state.capabilities.schedulers}
        disabled={mutationBlocked}
        bottomSeparator="none"
        onChange={value => update(profile => { profile.cpu_scheduler = value; })} />
      {boostPolicies.length > 0 && <div className={performanceBoost ? "rke-boost-warning" : "rke-boost-notice"}>
        <PanelSectionRow><Field
          bottomSeparator="none"
          label={performanceBoost ? "CPU boost may remain at maximum" : "ROCKNIX CPU boost enabled"}
          description={performanceBoost
            ? `The performance governor may hold boost clocks up to ${cpuMhz(selectedBoostMaximum)} continuously. Use schedutil for dynamic boosting.`
            : selectedBoostMaximum > 0
              ? `This preset permits boost up to ${cpuMhz(selectedBoostMaximum)}. Adaptive governors request it only when needed.`
              : `Boost is available up to ${cpuMhz(hardwareBoostMaximum)}, but this preset is capped below the boost clocks.`} />
        </PanelSectionRow>
      </div>}
      {cpuPolicies.map((policy, index) => <div key={policy.id}>
        <PerformanceHeading title={`Cluster ${index + 1}`}
          detail={`Policy ${policy.id} · cores ${policy.cpus.join(", ")}`} />
        <PanelSectionRow><SliderField
          label={<FrequencyLabel name="Min" value={cpuMhz(draft.cpu_min[policy.id])} />}
          bottomSeparator="none"
          value={Math.max(0, policy.frequencies.indexOf(draft.cpu_min[policy.id]))}
          min={0} max={policy.frequencies.length - 1} step={1}
          disabled={mutationBlocked}
          onChange={index => update(profile => {
            const value = policy.frequencies[index];
            profile.cpu_min[policy.id] = value;
            if (value > profile.cpu_max[policy.id]) profile.cpu_max[policy.id] = value;
          })} /></PanelSectionRow>
        <PanelSectionRow><SliderField
          label={<FrequencyLabel name="Max" value={`${cpuMhz(draft.cpu_max[policy.id])}${policy.boost_frequencies.includes(draft.cpu_max[policy.id]) ? " · Boost" : ""}`} />}
          bottomSeparator="none"
          value={Math.max(0, policy.maximum_frequencies.indexOf(draft.cpu_max[policy.id]))}
          min={0} max={policy.maximum_frequencies.length - 1} step={1}
          disabled={mutationBlocked}
          onChange={index => update(profile => {
            const value = policy.maximum_frequencies[index];
            profile.cpu_max[policy.id] = value;
            if (value < profile.cpu_min[policy.id]) profile.cpu_min[policy.id] = value;
          })} /></PanelSectionRow>
      </div>)}
    </PanelSection>

    {saveApplyControl(false, true)}

    <PanelSection>
      <PerformanceHeading title="GPU" />
      {!gpu.available ? <PanelSectionRow><Field label="GPU frequency control unavailable" bottomSeparator="none" /></PanelSectionRow> : <>
        <SelectRow label="Governor" value={draft.gpu_governor || ""} values={gpu.governors}
          disabled={mutationBlocked}
          bottomSeparator="none"
          onChange={value => update(profile => { profile.gpu_governor = value; })} />
        <SelectRow label="Maximum frequency" value={draft.gpu_max || gpu.maximum} values={gpu.frequencies} format={gpuMhz}
          disabled={mutationBlocked}
          bottomSeparator="none"
          onChange={value => update(profile => { profile.gpu_max = Number(value); })} />
      </>}
    </PanelSection>

    {saveApplyControl(false, true)}
    <PerformanceHeading title="Back to top" onActivate={backToPerformanceTop} />

  </div>;

  const backToFanTop = () => {
    fanTopRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    fanTopRef.current?.querySelector<HTMLElement>('[tabindex="0"], .Focusable')?.focus();
  };

  const systemFanCurveEditor = <PanelSection>
    <PerformanceHeading title="Rocknix Fan Curve" />
    <PanelSectionRow><ButtonItem layout="below" disabled={mutationBlocked}
      onClick={() => setUtility(current => current === "Fan" ? null : "Fan")}>
      {utility === "Fan" ? "Hide custom curve" : "Edit custom curve"}
    </ButtonItem></PanelSectionRow>
    {utility === "Fan" && <>
      <PanelSectionRow><Field label="Protected system curve"
        description="Edits ROCKNIX's Custom curve. Active RK-E preset curves remain independent." /></PanelSectionRow>
      {systemCurve.map(([temp, pwm], index) => <div key={index}>
        <PanelSectionRow><SliderField label={`Point ${index + 1} temperature`} description={`${Math.round(temp / 1000)}°C`}
          value={temp} min={10000 + index * 1000} max={120000 - (systemCurve.length - 1 - index) * 1000}
          step={1000} showValue disabled={mutationBlocked} onChange={value => updateSystemTemp(index, value)} /></PanelSectionRow>
        <PanelSectionRow><SliderField label={`Point ${index + 1} PWM`} description={`${Math.round(pwm * 100 / 255)}%`}
          value={pwm} min={0} max={255} step={1} showValue disabled={mutationBlocked} onChange={value => updateSystemPwm(index, value)} /></PanelSectionRow>
        {systemCurve.length > 2 && <PanelSectionRow><ButtonItem layout="below" disabled={mutationBlocked}
          onClick={() => updateSystemCurve(curve => { curve.splice(index, 1); })}>Remove point</ButtonItem></PanelSectionRow>}
      </div>)}
      {systemCurve.length < 16 && <PanelSectionRow><ButtonItem layout="below" disabled={mutationBlocked} onClick={addSystemCurvePoint}>Add point above hottest</ButtonItem></PanelSectionRow>}
      <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked || systemCurve.length < 2}
        onClick={() => run(async () => installState(await saveSystemFanCurve(systemCurve), selected), "ROCKNIX Custom fan curve saved")}>Save system fan curve</ButtonItem></PanelSectionRow>
    </>}
  </PanelSection>;

  const fan = <div className="rke-fan">
    <div ref={fanTopRef}>{saveApplyControl(true, true)}</div>
    <PanelSection>
      {!state.capabilities.fan_available && <PanelSectionRow><Field label="Fan control unavailable on this device" bottomSeparator="none" /></PanelSectionRow>}
      {state.capabilities.fan_available && <>
        <div className={!fanCanApply ? "rke-fan-warning" : ""}><PanelSectionRow><Field
          bottomSeparator="none"
          label={state.fan_curve_active ? "Preset fan curve active" : fanCanApply ? "Preset fan curve ready" : "Preset fan curve inactive"}
          description={state.fan_curve_active
            ? "ROCKNIX native fancontrol is running this preset curve."
            : fanCanApply
              ? "ROCKNIX Custom is active. Press Save & Apply to install this preset curve."
              : `ROCKNIX cooling is ${effectiveCooling || "unknown"}. In ROCKNIX Settings, set Cooling Profile to Custom. For Steam, use Per-System Advanced Configuration → Steam → Cooling Profile → Custom. Default also works when the System profile is Custom.`} /></PanelSectionRow></div>
        {draft.fan_curve.map(([temp, pwm], index) => <div key={index}>
          <PanelSectionRow><SliderField
            label={<FrequencyLabel name={`Point ${index + 1} temperature`} value={`${Math.round(temp / 1000)}°C`} />}
            bottomSeparator="none"
            value={temp} min={10000 + index * 1000} max={120000 - (draft.fan_curve.length - 1 - index) * 1000}
            step={1000} disabled={mutationBlocked} onChange={value => updateCurveTemp(index, value)} /></PanelSectionRow>
          <PanelSectionRow><SliderField label={`Point ${index + 1} PWM`} description={`${Math.round(pwm * 100 / 255)}%`}
            bottomSeparator="none"
            value={pwm} min={0} max={255} step={1} showValue disabled={mutationBlocked} onChange={value => updateCurvePwm(index, value)} /></PanelSectionRow>
          {draft.fan_curve.length > 2 && <PanelSectionRow><ButtonItem layout="below"
            disabled={mutationBlocked}
            bottomSeparator="none"
            onClick={() => update(profile => { profile.fan_curve.splice(index, 1); })}>Remove point</ButtonItem></PanelSectionRow>}
        </div>)}
        {draft.fan_curve.length < 16 && <PanelSectionRow><ButtonItem layout="below" disabled={mutationBlocked} bottomSeparator="none" onClick={addCurvePoint}>Add curve point</ButtonItem></PanelSectionRow>}
      </>}
      <PanelSectionRow><Field label="Live fan PWM" bottomSeparator="none">
        <span style={{ fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>
          {live ? `${live.fan_pwm} PWM · ${live.fan_percent}%` : "Reading…"}
        </span>
      </Field></PanelSectionRow>
    </PanelSection>
    {saveApplyControl(true, true)}
    {systemFanCurveEditor}
    <PerformanceHeading title="Back to top" onActivate={backToFanTop} />
  </div>;

  const utils = <div className="rke-utils">
    <PanelSection>
      <PanelSectionRow><Field label="Device IP"
        description={deviceNetwork?.interface ? `Interface: ${deviceNetwork.interface}` : undefined}>
        <span style={{ display: "block", width: "100%", textAlign: "right", fontWeight: 600 }}>
          {deviceNetworkError ? "Unavailable" : deviceNetwork?.ip || "Checking…"}
        </span>
      </Field></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" onClick={() => { showModal(<Logs />); }}>
        Logs
      </ButtonItem></PanelSectionRow>
      {runtimeMutationBlocked && !pluginConflict.blocked && <div className="rke-conflict-warning">
        <PanelSectionRow><Field label="RK-Enhanced controls blocked"
          description="The backend safety gate is active. Monitor, Logs, and Utils remain available; hardware controls will return after the blocking condition is cleared." /></PanelSectionRow>
      </div>}
      <PanelSectionRow><Field label="Installed release"
        description={updateInfo?.installed || "Checking…"} /></PanelSectionRow>
      <PanelSectionRow><Field label="Latest GitHub release"
        description={updateInfo?.latest || (updateError ? "Unavailable" : "Checking…")} /></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy || installActionBlocked || (pluginConflict.blocked && !conflictAutoRemovable) || !updateInfo?.latest}
        onClick={() => updateInfo?.latest && confirmRelease(updateInfo.latest,
          updateInfo.update_available ? "Update" : "Reinstall")}>{!updateInfo?.latest ? "Checking latest release…"
          : updateInfo.update_available ? `Update to ${updateInfo.latest}`
            : "Reinstall latest release"}</ButtonItem></PanelSectionRow>
      {updateInfo?.last_installed && updateInfo.last_installed_available !== false
        && updateInfo.last_installed !== updateInfo.installed
        && updateInfo.last_installed !== updateInfo.previous && <>
        <PanelSectionRow><Field label="Last installed on this device" description={updateInfo.last_installed} /></PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" disabled={busy || installActionBlocked || (pluginConflict.blocked && !conflictAutoRemovable)}
          onClick={() => confirmRelease(updateInfo.last_installed!, "Restore")}>Restore last installed · {updateInfo.last_installed}</ButtonItem></PanelSectionRow>
        <PanelSectionRow><Field label="Restore warning"
          description="Restoring an older release carries the same charging-state risks as a downgrade. Return charging controls to Normal and reboot after restoration." /></PanelSectionRow>
      </>}
      {updateInfo?.previous && <>
        <PanelSectionRow><Field label="Previous published release" description={updateInfo.previous} /></PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" disabled={busy || installActionBlocked || (pluginConflict.blocked && !conflictAutoRemovable)}
          onClick={() => confirmRelease(updateInfo.previous, "Downgrade")}>Install previous release · {updateInfo.previous}</ButtonItem></PanelSectionRow>
        <PanelSectionRow><Field label="Downgrade warning"
          description="Older releases may directly own charging state. Return Battery and Pump to Normal, confirm status, hide Experimental, then downgrade and reboot immediately." /></PanelSectionRow>
      </>}
      <PanelSectionRow><ButtonItem layout="below"
        disabled={busy || backupInfo === null || Boolean(backupInfo.errors?.length) || backupInfo.eligible_count < 1}
        onClick={confirmBackupCleanup}>Clean old RK-E backups</ButtonItem></PanelSectionRow>
      <PanelSectionRow><Field label={backupInfo?.errors?.length ? "Backup scan unavailable" : backupInfo?.eligible_count
        ? `${backupInfo.eligible_count} old backup${backupInfo.eligible_count === 1 ? "" : "s"} · ${formatBytes(backupInfo.eligible_bytes)}`
        : backupError ? "Backup scan unavailable" : backupInfo ? "No old backups to clean" : "Checking backups…"}
        description={backupInfo?.errors?.length
          ? backupInfo.errors.join(" · ")
          : "Keeps the newest RK-Enhanced backup. Recovery and active rollback files are never removed."} /></PanelSectionRow>
      {updateError && <PanelSectionRow><Field label="Update check failed" description={updateError} /></PanelSectionRow>}

      <div className="rke-experimental-utils">
        {!state.experimental_unlocked && <>
          <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked}
            onClick={() => setShowExperimentalUnlock(current => !current)}>
            {showExperimentalUnlock ? "Hide experimental unlock" : "Experimental controls"}
          </ButtonItem></PanelSectionRow>
          <PanelSectionRow><Field label="Experimental controls"
            description="Unlock the Experimental tab for supported charging hardware." /></PanelSectionRow>
          {showExperimentalUnlock && <>
            <PanelSectionRow><TextField label="Unlock code" value={experimentalCode}
              onChange={event => setExperimentalCode(event.target.value)} /></PanelSectionRow>
            <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked || !experimentalCode}
              onClick={() => run(async () => {
                installState(await unlockExperimental(experimentalCode), selected);
                setExperimentalCode(""); setShowExperimentalUnlock(false);
              }, "Experimental controls unlocked")}>Unlock</ButtonItem></PanelSectionRow>
          </>}
        </>}
        {state.experimental_unlocked && <>
          <PanelSectionRow><ButtonItem layout="below" disabled={busy || mutationBlocked}
            onClick={() => run(async () => {
              installState(await lockExperimental(), selected);
            }, "Experimental controls hidden")}>Hide experimental controls</ButtonItem></PanelSectionRow>
          <PanelSectionRow><Field label="Experimental tab enabled"
            description="Battery policy, pump profiles, and charging status are available in the Experimental tab." /></PanelSectionRow>
        </>}
      </div>
    </PanelSection>
  </div>;

  const tabContent = (content: ReactNode) => <div className="rke-content">{content}</div>;
  const monitorContent = <>
    {pluginConflict.blocked ? <PanelSection><div className="rke-conflict-warning">
      <PanelSectionRow><Field label="Conflicting plugin detected"
        description={pluginConflict.message || "ROCKNIX Control can compete with RK-Enhanced for CPU, GPU, and fan control. All RK-Enhanced controls are blocked until it is removed."} /></PanelSectionRow>
      {pluginConflict.conflicts.map(conflict => <PanelSectionRow key={conflict.directory}><Field
        label={`${conflict.name}${conflict.version ? ` ${conflict.version}` : ""}`}
        description={`${conflict.directory}${conflict.reason ? ` · ${conflict.reason}` : ""}`} /></PanelSectionRow>)}
      <PanelSectionRow><ButtonItem layout="below" disabled={busy || !conflictAutoRemovable}
        onClick={confirmConflictRemoval}>Remove conflicting plugin</ButtonItem></PanelSectionRow>
      <PanelSectionRow><Field label="Permanent removal"
        description={conflictAutoRemovable
          ? "The conflicting plugin directory will be removed without a backup."
          : "This conflict cannot be removed automatically. Follow the reason shown above and remove it manually."} /></PanelSectionRow>
    </div></PanelSection> : runtimeMutationBlocked && <PanelSection><div className="rke-conflict-warning">
      <PanelSectionRow><Field label="RK-Enhanced controls blocked"
        description="A backend safety gate is active. Monitoring remains available." /></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" onClick={() => setTab("Utils")}>Open Utils</ButtonItem></PanelSectionRow>
    </div></PanelSection>}
    <Monitor active={panelVisible && tab === "Monitor"} />
  </>;
  const availableTabs = [
    { id: "Monitor", title: "Monitor", content: tabContent(monitorContent) },
    { id: "Performance", title: "Performance", content: tabContent(performance) },
    { id: "Fan", title: "Fan Curves", content: tabContent(fan) },
    { id: "Presets", title: "Presets", content: tabContent(presets) },
    ...(state.capabilities.rgb?.available ? [{
      id: "RGB", title: "RGB",
      content: tabContent(<RGB active={panelVisible && tab === "RGB"} />),
    }] : []),
    { id: "Utils", title: "Utils", content: tabContent(utils) },
    ...(state.experimental_unlocked ? [{
      id: "Experimental", title: "Experimental",
      content: tabContent(<Experimental active={panelVisible && tab === "Experimental"} />),
    }] : []),
  ];
  const tabs = runtimeMutationBlocked
    ? availableTabs.filter(item => item.id === "Monitor" || item.id === "Utils")
    : availableTabs;
  const visibleTab = tabs.some(item => item.id === tab) ? tab : "Utils";
  const activeTitle = tabs.find(item => item.id === visibleTab)?.title || visibleTab;
  return <div className="rke-tabs"
    onFocusCapture={event => setTabBarFocused(isTabFocusTarget(event.target))}
    onBlurCapture={event => setTabBarFocused(isTabFocusTarget(event.relatedTarget))}>
    <style>{styles}</style>
    <div className="rke-tab-header" aria-hidden="true">
      <div className="rke-tab-menu">
        <ShoulderGlyph side="LB" />
        <span className={`rke-active-tab${tabBarFocused ? " rke-active-tab-focused" : ""}`}>{activeTitle}</span>
        <ShoulderGlyph side="RB" />
      </div>
    </div>
    <Tabs activeTab={visibleTab} onShowTab={setTab} tabs={tabs} />
  </div>;
}
