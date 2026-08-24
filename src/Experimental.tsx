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
  choice("normal", "Qualcomm/Normal"),
  choice("slow", "Slow 25 W"),
  choice("fast", "Fast 36 W"),
];
type ChargingControl = "battery-policy" | "pump-profile";

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
  return "Qualcomm/Normal";
};

const phaseLabel = (pump: PumpProfileStatus) => {
  if (!pump.valid) return "Unavailable";
  if (pump.phase === "off") return "Off";
  if (pump.phase === "starting") return "Starting";
  if (pump.phase === "active") return "Active";
  if (pump.phase === "error") return "Error";
  return "Transitional/Unknown";
};

const behaviourLabel = (battery: BatteryPolicyStatus) => {
  if (!battery.valid || !battery.charge_behaviour) return "Unavailable";
  return battery.charge_behaviour === "inhibit-charge" ? "Inhibit charge" : "Auto";
};

const healthLabel = (online?: boolean, health?: string) => {
  if (!health) return "Unavailable";
  return `${health} · ${online ? "Online" : "Off"}`;
};

const capturedTime = (seconds?: number) => seconds
  ? new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
  : "Unavailable";

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
      <PanelSectionRow><Field label="Limit 100 behavior" bottomSeparator="none"
        description="Stops charging at 100% and resumes at 95%." /></PanelSectionRow>
      {batteryOperationText && <div className={operation?.kind === "battery-policy" && operation.ok ? "rke-experimental-notice" : "rke-experimental-error"}>
        <PanelSectionRow><Field label={operation?.kind === "battery-policy" && operation.ok ? "Request completed" : operation?.timed_out ? "Request timed out" : "Request refused"}
          description={batteryOperationText} bottomSeparator="none" /></PanelSectionRow>
      </div>}
      {battery && !battery.available && <div className="rke-experimental-error"><PanelSectionRow><Field
        label="Battery policy unsupported" bottomSeparator="none"
        description="/usr/bin/charging_mode is unavailable or reports that this device is unsupported." /></PanelSectionRow></div>}
      {battery && statusError(battery) && <div className={battery.transitional ? "rke-experimental-warning" : "rke-experimental-error"}>
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
        label="Pump profiles unsupported" bottomSeparator="none"
        description="/usr/bin/kpfe_fast_charge is unavailable or reports that this device is unsupported." /></PanelSectionRow></div>}
      {pump && statusError(pump) && <div className={pump.transitional ? "rke-experimental-warning" : "rke-experimental-error"}>
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
        <StatusRow label="Observed behaviour" value={behaviourLabel(currentStatus.battery)} />
        <StatusRow label="Battery status" value={currentStatus.battery.battery_status || "Unavailable"} />
        <StatusRow label="Pump selection" value={pumpLabel(currentStatus.pump)} />
        <StatusRow label="Pump phase" value={phaseLabel(currentStatus.pump)}
          color={currentStatus.pump.phase === "active" ? "#26de81" : currentStatus.pump.phase === "error" ? "#fc5c65" : currentStatus.pump.phase === "starting" ? "#fed330" : undefined} />
        <StatusRow label="USB source" value={currentStatus.pump.usb_type || "Unavailable"} />
        <StatusRow label="Master pump" value={healthLabel(currentStatus.pump.master_online, currentStatus.pump.master_health)} />
        <StatusRow label="Slave pump" value={healthLabel(currentStatus.pump.slave_online, currentStatus.pump.slave_health)} />
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
