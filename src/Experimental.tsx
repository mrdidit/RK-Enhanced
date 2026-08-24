import {
  ButtonItem, ConfirmModal, DropdownItem, Field, PanelSection, PanelSectionRow,
  showModal,
} from "@decky/ui";
import { useCallback, useEffect, useRef, useState } from "react";
import type { Ref } from "react";
import { getChargingStatus, setBatteryPolicy, setPumpProfile } from "./backend";
import type {
  BatteryLimit, BatteryPolicyStatus, ChargingCommandResult, ChargingStatus,
  PumpProfileStatus,
} from "./types";

const choice = (data: string, label: string) => ({ data, label });
const BATTERY_LIMITS = [50, 60, 70, 80, 90, 100] as const;
const BATTERY_CHOICES = [
  choice("normal", "Normal"),
  choice("bypass", "Bypass"),
  ...BATTERY_LIMITS.map(limit => choice(`limit-${limit}`, `Limit ${limit}%`)),
];
const PUMP_CHOICES = [
  choice("normal", "Qcom Normal"),
  choice("slow", "Slow 25 W"),
  choice("fast", "Fast 36 W"),
];
type ChargingControl = "battery-policy" | "pump-profile";
const GREEN = "#26de81";
const BLUE = "#45aaf2";
const YELLOW = "#fed330";
const ORANGE = "#f39c3d";
const RED = "#fc5c65";

const Heading = ({ title, headingRef, onActivate }: {
  title: string;
  headingRef?: Ref<HTMLDivElement>;
  onActivate?: () => void;
}) => <div className="rke-performance-heading-row">
  <Field ref={headingRef} className="rke-performance-heading" focusable highlightOnFocus
    bottomSeparator="none"
    label={<span className="rke-performance-heading-label"><span>{title}</span></span>}
    onActivate={onActivate} onClick={onActivate} />
</div>;

const StatusRow = ({ label, value, description, color }: {
  label: string;
  value: string;
  description?: string;
  color?: string;
}) => <PanelSectionRow><Field label={label} description={description} bottomSeparator="none">
  <span style={{
    display: "block", width: "100%", textAlign: "right", color,
    fontVariantNumeric: "tabular-nums", fontWeight: 600,
  }}>{value}</span>
</Field></PanelSectionRow>;

const batterySelection = (battery?: BatteryPolicyStatus) => {
  if (!battery?.mode) return "";
  return battery.mode === "limit" && battery.limit ? `limit-${battery.limit}` : battery.mode;
};

const pumpSelection = (pump?: PumpProfileStatus) => pump?.profile || "";

const policyLabel = (battery: BatteryPolicyStatus) => {
  if (!battery.valid) return battery.available ? "Unavailable" : "Unsupported";
  if (battery.transitional) return "Transitional/Unknown";
  if (battery.mode === "limit") return `Limit ${battery.limit}%`;
  return battery.mode === "bypass" ? "Bypass" : "Normal";
};

const pumpLabel = (pump: PumpProfileStatus) => {
  if (!pump.valid) return pump.available ? "Unavailable" : "Unsupported";
  if (pump.profile === "slow") return "Slow 25 W";
  if (pump.profile === "fast") return "Fast 36 W";
  return "Qcom Normal";
};

const phaseDisplay = (pump: PumpProfileStatus) => {
  if (pump.stale) return { value: "Stale", color: ORANGE };
  if (!pump.valid) return { value: "Unavailable", color: ORANGE };
  if (pump.phase === "off") return { value: "Off", color: BLUE };
  if (pump.phase === "starting") return { value: "Starting", color: YELLOW };
  if (pump.phase === "active") return { value: "Active", color: GREEN };
  if (pump.phase === "error") return { value: "Error", color: RED };
  return { value: "Transitional/Unknown", color: ORANGE };
};

const behaviourDisplay = (battery: BatteryPolicyStatus) => {
  if (battery.stale) return { value: "Stale", color: ORANGE };
  if (!battery.valid || !battery.charge_behaviour)
    return { value: "Unavailable", color: ORANGE };
  return battery.charge_behaviour === "inhibit-charge"
    ? { value: "Paused", color: BLUE }
    : { value: "Allowed", color: GREEN };
};

const batteryStatusDisplay = (battery: BatteryPolicyStatus) => {
  if (battery.stale) return { value: "Stale", color: ORANGE };
  const value = battery.battery_status?.trim();
  if (!battery.valid || !value) return { value: "Unavailable", color: ORANGE };
  const normalized = value.toLowerCase();
  if (normalized === "charging" || normalized === "full")
    return { value, color: GREEN };
  if (normalized === "discharging" || normalized === "not charging")
    return { value, color: BLUE };
  if (normalized.includes("error") || normalized.includes("failure"))
    return { value, color: RED };
  return { value, color: ORANGE };
};

const usbSourceDisplay = (pump: PumpProfileStatus) => {
  if (pump.stale) return { value: "Stale", color: ORANGE };
  if (!pump.valid) return { value: "Unavailable", color: ORANGE };
  if (!pump.usb_online) return { value: "Offline", color: BLUE };
  const source = pump.usb_type?.trim();
  if (!source || source === "Unknown") return { value: "Unknown", color: ORANGE };
  const labels: Record<string, string> = {
    PD_PPS: "PD-PPS",
    PD: "USB-PD",
    PD_DRP: "USB-PD DRP",
    SDP: "Standard USB",
    CDP: "Charging USB",
    DCP: "Dedicated charger",
    ACA: "Accessory charger",
    C: "USB-C",
    USB_C: "USB-C",
    Apple_Brick_ID: "Apple charger",
  };
  return {
    value: labels[source] || source.replace(/_/g, " "),
    color: source === "PD_PPS" ? GREEN : labels[source] ? BLUE : ORANGE,
  };
};

const pumpHealthDisplay = (pump: PumpProfileStatus, online?: boolean, health?: string) => {
  if (pump.stale) return { value: "Stale", color: ORANGE };
  if (!pump.valid || online === undefined || !health)
    return { value: "Unavailable", color: ORANGE };
  if (health === "Good")
    return online ? { value: "On", color: GREEN } : { value: "Off", color: BLUE };
  if (health.toLowerCase() === "unknown") return { value: health, color: ORANGE };
  return { value: `${health}${online ? " · On" : ""}`, color: RED };
};

const batteryTemperatureDisplay = (temperature: number | null) => {
  if (temperature === null || !Number.isSafeInteger(temperature))
    return { value: "Unavailable", color: ORANGE };
  const celsius = temperature / 10;
  const color = celsius < 0 || celsius >= 50 ? RED
    : celsius >= 45 ? ORANGE : celsius >= 35 ? YELLOW : GREEN;
  return { value: `${celsius.toFixed(1)}°C`, color };
};

const capturedTime = (seconds?: number) => seconds
  ? new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
  : "Unavailable";

const formatInputWatts = (microwatts: string) => {
  if (!/^\d+$/.test(microwatts)) return null;
  try {
    const value = BigInt(microwatts);
    if (value > 9223372036854775807n) return null;
    const tenths = (value + 50000n) / 100000n;
    return `${tenths / 10n}.${tenths % 10n} W`;
  } catch {
    return null;
  }
};

const inputPowerDisplay = (status: ChargingStatus, current: boolean) => {
  const input = status.pump.input_power;
  if (status.battery.stale || status.pump.stale || input?.stale)
    return { value: "Stale", description: input?.error };
  if (!current || !status.coherent || !input?.available)
    return { value: "Unavailable", description: input?.error };
  if (input.path === "offline") return { value: "Offline" };
  if (input.path === "transition") return { value: "Transitioning…" };
  if (input.path === "unavailable") return { value: "Unavailable" };
  if (!input.valid || input.microwatts === null)
    return { value: "Unavailable", description: input.error };
  const watts = formatInputWatts(input.microwatts);
  if (!watts) return { value: "Unavailable", description: input.error };
  return { value: watts };
};

const statusError = (status: BatteryPolicyStatus | PumpProfileStatus) =>
  status.refresh_error || status.error || status.transition_reason || "";

const operationMessage = (operation: ChargingCommandResult | null) => {
  if (!operation) return "";
  if (operation.timed_out) return "Request timed out; the refreshed status below is authoritative.";
  if (!operation.ok) return operation.stderr || `Command failed with exit status ${operation.exit_status}.`;
  return "Request completed; the observed status below has been refreshed.";
};

const pollDelay = (status: ChargingStatus | null) =>
  status?.pump.valid && (status.pump.phase === "starting" || status.pump.phase === "active")
    ? 1500 : 7000;

export function Experimental({ active }: { active: boolean }) {
  const [status, setStatus] = useState<ChargingStatus | null>(null);
  const [lastOperation, setLastOperation] = useState<ChargingCommandResult | null>(null);
  const [requestError, setRequestError] = useState<{ kind: ChargingControl; message: string } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [currentRefreshSucceeded, setCurrentRefreshSucceeded] = useState(false);
  const [statusSession, setStatusSession] = useState(-1);
  const generation = useRef(0);
  const busyRef = useRef(false);
  const activationSession = useRef(0);
  const previouslyActive = useRef(false);
  const topRef = useRef<HTMLDivElement>(null);
  if (active !== previouslyActive.current) {
    previouslyActive.current = active;
    if (active) activationSession.current += 1;
  }

  useEffect(() => {
    if (!active) {
      setStatus(null);
      setStatusSession(-1);
      setCurrentRefreshSucceeded(false);
      setError("");
      return;
    }
    setStatus(null);
    setStatusSession(-1);
    setCurrentRefreshSucceeded(false);
    setLastOperation(null);
    setRequestError(null);
    setError("");
    let cancelled = false;
    let timer = 0;
    const session = activationSession.current;
    const poll = async () => {
      if (busyRef.current) {
        if (!cancelled) timer = window.setTimeout(poll, 500);
        return;
      }
      const request = ++generation.current;
      let next: ChargingStatus | null = null;
      try {
        next = await getChargingStatus();
        if (!cancelled && request === generation.current) {
          setStatus(next);
          setStatusSession(session);
          setCurrentRefreshSucceeded(true);
          setError("");
        }
      } catch (reason) {
        if (!cancelled && request === generation.current) {
          setStatus(null);
          setStatusSession(-1);
          setCurrentRefreshSucceeded(false);
          setError(String(reason));
        }
      }
      if (!cancelled) timer = window.setTimeout(poll, pollDelay(next));
    };
    void poll();
    return () => {
      cancelled = true;
      generation.current += 1;
      window.clearTimeout(timer);
    };
  }, [active]);

  const refresh = useCallback(async () => {
    const request = ++generation.current;
    const session = activationSession.current;
    busyRef.current = true;
    setBusy(true);
    setCurrentRefreshSucceeded(false);
    try {
      const next = await getChargingStatus();
      if (request === generation.current) {
        setStatus(next);
        setStatusSession(session);
        setCurrentRefreshSucceeded(true);
        setError("");
      }
    } catch (reason) {
      if (request === generation.current) {
        setStatus(null);
        setStatusSession(-1);
        setCurrentRefreshSucceeded(false);
        setError(String(reason));
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, []);

  const mutate = async (kind: ChargingControl, work: () => Promise<ChargingStatus>) => {
    const request = ++generation.current;
    const session = activationSession.current;
    busyRef.current = true;
    setBusy(true);
    setCurrentRefreshSucceeded(false);
    setLastOperation(null);
    setRequestError(null);
    try {
      const next = await work();
      if (request === generation.current) {
        setStatus(next);
        setStatusSession(session);
        setCurrentRefreshSucceeded(true);
        setLastOperation(next.operation);
        setError("");
      }
    } catch (reason) {
      if (request === generation.current) {
        setStatus(null);
        setStatusSession(-1);
        setCurrentRefreshSucceeded(false);
        setRequestError({ kind, message: String(reason) });
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const changeBattery = (value: string) => {
    if (value.startsWith("limit-")) {
      const limit = Number(value.slice(6));
      if (BATTERY_LIMITS.includes(limit as BatteryLimit))
        void mutate("battery-policy", () => setBatteryPolicy("limit", limit as BatteryLimit));
      return;
    }
    if (value === "normal" || value === "bypass")
      void mutate("battery-policy", () => setBatteryPolicy(value));
  };

  const changePump = (profile: string) => {
    if (profile === "normal") {
      void mutate("pump-profile", () => setPumpProfile("normal", false));
      return;
    }
    if (profile !== "slow" && profile !== "fast") return;
    const label = profile === "slow" ? "Slow 25 W" : "Fast 36 W";
    showModal(<ConfirmModal
      strTitle={`Enable experimental ${label}?`}
      strDescription={`${label} uses the KPFE dual-pump coordinator. It returns to Qualcomm/Normal after unplug, suspend, reboot, an endpoint, interlock, or fault. Confirm this individual enable request.`}
      strOKButtonText="Enable"
      strCancelButtonText="Cancel"
      onOK={() => { void mutate("pump-profile", () => setPumpProfile(profile, true)); }} />);
  };

  const backToTop = () => {
    topRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    topRef.current?.focus();
  };

  const currentStatus = active && statusSession === activationSession.current ? status : null;
  const battery = currentStatus?.battery;
  const pump = currentStatus?.pump;
  const operation = lastOperation;
  const operationText = operationMessage(operation);
  const pairReady = Boolean(
    active && statusSession === activationSession.current &&
    currentRefreshSucceeded && currentStatus?.coherent &&
    battery?.available && battery.valid && !battery.stale && !battery.transitional &&
    pump?.available && pump.valid && !pump.stale && !pump.transitional);
  const controlsDisabled = busy || !pairReady;
  const inputPower = currentStatus
    ? inputPowerDisplay(currentStatus, pairReady && !busy) : null;
  const behaviour = currentStatus ? behaviourDisplay(currentStatus.battery) : null;
  const batteryStatus = currentStatus ? batteryStatusDisplay(currentStatus.battery) : null;
  const pumpPhase = currentStatus ? phaseDisplay(currentStatus.pump) : null;
  const usbSource = currentStatus ? usbSourceDisplay(currentStatus.pump) : null;
  const batteryTemperature = currentStatus
    ? batteryTemperatureDisplay(currentStatus.battery_temperature_deci_c) : null;
  const masterPump = currentStatus ? pumpHealthDisplay(
    currentStatus.pump, currentStatus.pump.master_online,
    currentStatus.pump.master_health) : null;
  const slavePump = currentStatus ? pumpHealthDisplay(
    currentStatus.pump, currentStatus.pump.slave_online,
    currentStatus.pump.slave_health) : null;
  const batteryOperationText = operation?.kind === "battery-policy" ? operationText
    : requestError?.kind === "battery-policy" ? requestError.message : "";
  const pumpOperationText = operation?.kind === "pump-profile" ? operationText
    : requestError?.kind === "pump-profile" ? requestError.message : "";

  return <div className="rke-experimental">
    <PanelSection>
      <Heading title="Battery Policy" headingRef={topRef} />
      <PanelSectionRow><DropdownItem label="Battery policy" bottomSeparator="none"
        disabled={controlsDisabled}
        selectedOption={batterySelection(battery)} rgOptions={BATTERY_CHOICES}
        onChange={(selected: any) => changeBattery(String(selected.data))} /></PanelSectionRow>
      {batterySelection(battery) === "limit-100" &&
        <PanelSectionRow><Field label="Limit 100 behavior" bottomSeparator="none"
          description="Stops charging at 100% and resumes at 95%." /></PanelSectionRow>}
      {batteryOperationText && <div className={operation?.kind === "battery-policy" && operation.ok ? "rke-experimental-notice" : "rke-experimental-error"}>
        <PanelSectionRow><Field label={operation?.kind === "battery-policy" && operation.ok ? "Request completed" : operation?.timed_out ? "Request timed out" : "Request refused"}
          description={batteryOperationText} bottomSeparator="none" /></PanelSectionRow>
      </div>}
      {battery && !battery.available && <div className="rke-experimental-error"><PanelSectionRow><Field
        label="Battery policy unsupported" bottomSeparator="none" /></PanelSectionRow></div>}
      {battery?.available && statusError(battery) && <div className={battery.transitional ? "rke-experimental-warning" : "rke-experimental-error"}>
        <PanelSectionRow><Field label={battery.stale ? "Battery status stale" : battery.transitional ? "Battery status transitional" : "Battery status unavailable"}
          description={statusError(battery)} bottomSeparator="none" /></PanelSectionRow>
      </div>}
    </PanelSection>

    <PanelSection>
      <Heading title="Pump Profile" />
      <PanelSectionRow><DropdownItem label="Pump profile" bottomSeparator="none"
        disabled={controlsDisabled}
        selectedOption={pumpSelection(pump)} rgOptions={PUMP_CHOICES}
        onChange={(selected: any) => changePump(String(selected.data))} /></PanelSectionRow>
      {pumpOperationText && <div className={operation?.kind === "pump-profile" && operation.ok ? "rke-experimental-notice" : "rke-experimental-error"}>
        <PanelSectionRow><Field label={operation?.kind === "pump-profile" && operation.ok ? "Request completed" : operation?.timed_out ? "Request timed out" : "Request refused"}
          description={pumpOperationText} bottomSeparator="none" /></PanelSectionRow>
      </div>}
      {pump && !pump.available && <div className="rke-experimental-error"><PanelSectionRow><Field
        label="Pump profiles unsupported" bottomSeparator="none" /></PanelSectionRow></div>}
      {pump?.available && statusError(pump) && <div className={pump.transitional ? "rke-experimental-warning" : "rke-experimental-error"}>
        <PanelSectionRow><Field label={pump.stale ? "Pump status stale" : pump.transitional ? "Pump status transitional" : "Pump status unavailable"}
          description={statusError(pump)} bottomSeparator="none" /></PanelSectionRow>
      </div>}
    </PanelSection>

    <PanelSection>
      <Heading title="Status" />
      {!currentStatus && <StatusRow label="Charging status" value={error || "Reading…"} />}
      {currentStatus && <>
        <StatusRow label="Battery policy" value={policyLabel(currentStatus.battery)} />
        <StatusRow label="Capacity" value={currentStatus.battery.capacity === undefined ? "Unavailable" : `${currentStatus.battery.capacity}%`} />
        <StatusRow label="Battery charging" value={behaviour?.value || "Unavailable"}
          color={behaviour?.color} />
        <StatusRow label="Battery status" value={batteryStatus?.value || "Unavailable"}
          color={batteryStatus?.color} />
        <StatusRow label="Pump selection" value={pumpLabel(currentStatus.pump)} />
        <StatusRow label="Pump phase" value={pumpPhase?.value || "Unavailable"}
          color={pumpPhase?.color} />
        <StatusRow label="USB source" value={usbSource?.value || "Unavailable"}
          color={usbSource?.color} />
        <StatusRow label="USB input power" value={inputPower?.value || "Unavailable"}
          description={inputPower?.description} />
        <StatusRow label="Battery temperature" value={batteryTemperature?.value || "Unavailable"}
          color={batteryTemperature?.color} />
        <StatusRow label="Master pump" value={masterPump?.value || "Unavailable"}
          color={masterPump?.color} />
        <StatusRow label="Slave pump" value={slavePump?.value || "Unavailable"}
          color={slavePump?.color} />
        {currentStatus.pump.last_end_reason && currentStatus.pump.last_end_reason !== "none" &&
          <StatusRow label="Last pump stop" value={currentStatus.pump.last_end_reason} />}
        {currentStatus.pump.last_error !== undefined && currentStatus.pump.last_error !== 0 &&
          <StatusRow label="Coordinator error" value={String(currentStatus.pump.last_error)} color="#fc5c65" />}
        <StatusRow label="Captured" value={capturedTime(Math.min(
          currentStatus.battery.captured_at || currentStatus.captured_at,
          currentStatus.pump.captured_at || currentStatus.captured_at,
        ))} description={currentStatus.battery.stale || currentStatus.pump.stale ? "One or more values are retained from the last valid status." : undefined} />
      </>}
      {currentRefreshSucceeded && currentStatus && !pairReady && <div className="rke-experimental-warning">
        <PanelSectionRow><Field label="Charging controls locked"
          description="Both current status snapshots must be valid, fresh, non-transitional, and coherent before either control can be changed."
          bottomSeparator="none" /></PanelSectionRow>
      </div>}
      {error && <div className="rke-experimental-error"><PanelSectionRow><Field
        label="Charging status failed" description={error} bottomSeparator="none" /></PanelSectionRow></div>}
      <PanelSectionRow><ButtonItem layout="below" bottomSeparator="none" disabled={busy}
        onClick={() => { void refresh(); }}>Refresh Status</ButtonItem></PanelSectionRow>
    </PanelSection>
    <Heading title="Back to top" onActivate={backToTop} />
  </div>;
}
