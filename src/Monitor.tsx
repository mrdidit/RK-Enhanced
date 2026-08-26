import { Field, PanelSection, PanelSectionRow } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import type { Ref } from "react";
import {
  beginMonitorSession, endMonitorSession, getChargingStatus, getTelemetry,
  invalidateMonitorChargingStatus,
} from "./backend";
import type { ChargingStatus, MonitorEpoch, Telemetry } from "./types";

interface MonitorActivation {
  session: string;
  generation: number;
}

let lastMonitorGeneration = 0;
const newMonitorActivation = (): MonitorActivation => {
  const clockGeneration = Date.now() * 1000;
  lastMonitorGeneration = Math.max(clockGeneration, lastMonitorGeneration + 1);
  return {
    session: `monitor-${lastMonitorGeneration}-${Math.random().toString(36).slice(2)}`,
    generation: lastMonitorGeneration,
  };
};

const cpuMhz = (khz: number) => `${Math.round(khz / 1000)} MHz`;
const gpuMhz = (hz: number) => `${Math.round(hz / 1_000_000)} MHz`;
const usedMemory = (mb: number) => mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;
const severity = (value: number, warning: number, critical: number) =>
  value >= critical ? "#fc5c65" : value >= warning ? "#fed330" : "#26de81";
const temperatureColor = (value: number) => value >= 85 ? "#fc5c65"
  : value >= 70 ? "#f39c3d" : value >= 50 ? "#fed330" : "#26de81";
const batteryColor = (value: number) => value <= 15 ? "#fc5c65" : value <= 30 ? "#fed330" : "#26de81";
const clusterLabel = (index: number, cpus: string[]) => {
  if (!cpus.length) return `Cluster ${index + 1}`;
  return `Cluster ${index + 1} (${cpus.length > 1 ? `${cpus[0]}–${cpus[cpus.length - 1]}` : cpus[0]})`;
};
const duration = (seconds: number) => {
  if (seconds <= 0) return "";
  if (seconds >= 9.75 * 3600) {
    const roundedHalfHours = Math.round(seconds / 1800) / 2;
    return `${roundedHalfHours}h+`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return hours > 0 ? `${hours}h${minutes > 0 ? ` ${minutes}m` : ""}` : `${minutes}m`;
};

function Meter({ value, color = "#59bf40" }: { value: number; color?: string }) {
  const width = Math.max(0, Math.min(100, value));
  return <div style={{ height: 7, marginTop: 7, borderRadius: 5, overflow: "hidden", background: "rgba(255,255,255,.14)" }}>
    <div style={{ width: `${width}%`, height: "100%", background: color, transition: "width .35s linear" }} />
  </div>;
}

function Metric({ label, value, percent, color, detail, valueColor }: { label: string; value: string; percent?: number; color?: string; detail?: string; valueColor?: string }) {
  return <PanelSectionRow><Field label={label} bottomSeparator="none"
    description={percent === undefined ? detail : <Meter value={percent} color={color} />}>
    <span style={{ display: "block", width: "100%", textAlign: "right", color: valueColor, fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>{value}</span>
  </Field></PanelSectionRow>;
}

const Heading = ({ children, headingRef, onActivate }: {
  children: string;
  headingRef?: Ref<HTMLDivElement>;
  onActivate?: () => void;
}) =>
  <div className="rke-monitor-heading-row">
    <Field ref={headingRef} className="rke-monitor-heading" focusable highlightOnFocus
      bottomSeparator="none" label={<span className="rke-monitor-heading-label">{children}</span>}
      onActivate={onActivate} onClick={onActivate} />
  </div>;

export function Monitor({ active }: { active: boolean }) {
  const [telemetrySnapshot, setTelemetrySnapshot] = useState<{ session: string; data: Telemetry } | null>(null);
  const [chargingSnapshot, setChargingSnapshot] = useState<{ session: string; status: ChargingStatus } | null>(null);
  const [readyActivation, setReadyActivation] = useState<MonitorActivation | null>(null);
  const [error, setError] = useState("");
  const [chargingError, setChargingError] = useState("");
  const monitorActivation = useRef<MonitorActivation>({ session: "", generation: 0 });
  const previouslyActive = useRef(false);
  const acceptedChargingRevision = useRef(-1);
  const telemetryRequestGeneration = useRef(0);
  const chargingRequestGeneration = useRef(0);
  const chargingRefreshActive = useRef(false);
  const monitorTopRef = useRef<HTMLDivElement>(null);
  if (active !== previouslyActive.current) {
    previouslyActive.current = active;
    if (active) {
      monitorActivation.current = newMonitorActivation();
    }
  }
  const currentActivation = monitorActivation.current;
  const sessionReady = Boolean(
    active && readyActivation &&
    readyActivation.session === currentActivation.session &&
    readyActivation.generation === currentActivation.generation);
  const data = sessionReady && telemetrySnapshot?.session === currentActivation.session &&
    telemetrySnapshot.data.monitor_generation === currentActivation.generation
    ? telemetrySnapshot.data : null;
  const charging = sessionReady && chargingSnapshot?.session === currentActivation.session &&
    chargingSnapshot.status.monitor_generation === currentActivation.generation
    ? chargingSnapshot.status : null;
  useEffect(() => {
    if (!active) {
      setReadyActivation(null);
      return;
    }
    const activation = { ...monitorActivation.current };
    let cancelled = false;
    setReadyActivation(null);
    setTelemetrySnapshot(null);
    setChargingSnapshot(null);
    setError("");
    setChargingError("");
    acceptedChargingRevision.current = -1;
    telemetryRequestGeneration.current += 1;
    chargingRequestGeneration.current += 1;
    void beginMonitorSession(activation.session, activation.generation).then(epoch => {
      if (!cancelled && epoch.generation === activation.generation) {
        acceptedChargingRevision.current = epoch.revision;
        setReadyActivation(activation);
      }
    }).catch(reason => {
      if (!cancelled) {
        setReadyActivation(null);
        setError(String(reason));
        setChargingError(String(reason));
      }
    });
    return () => {
      cancelled = true;
      acceptedChargingRevision.current = -1;
      telemetryRequestGeneration.current += 1;
      chargingRequestGeneration.current += 1;
      chargingRefreshActive.current = false;
      void endMonitorSession(activation.session, activation.generation).catch(() => undefined);
    };
  }, [active]);
  useEffect(() => {
    if (!sessionReady || !readyActivation) return;
    const activation = { ...readyActivation };
    let cancelled = false;
    let timer = 0;
    const schedule = (delay: number) => {
      if (!cancelled) timer = window.setTimeout(refresh, delay);
    };
    const refresh = async () => {
      if (chargingRefreshActive.current) {
        schedule(200);
        return;
      }
      const requestGeneration = ++telemetryRequestGeneration.current;
      try {
        const next = await getTelemetry(activation.session, activation.generation);
        if (!cancelled && requestGeneration === telemetryRequestGeneration.current &&
          next.monitor_generation === activation.generation &&
          next.charging_revision === acceptedChargingRevision.current) {
          setTelemetrySnapshot({ session: activation.session, data: next });
          setError("");
        }
      } catch (reason) {
        if (!cancelled && requestGeneration === telemetryRequestGeneration.current) {
          setTelemetrySnapshot(null);
          setError(String(reason));
        }
      } finally {
        schedule(1000);
      }
    };
    void refresh();
    return () => {
      cancelled = true;
      telemetryRequestGeneration.current += 1;
      window.clearTimeout(timer);
    };
  }, [sessionReady, readyActivation]);
  useEffect(() => {
    if (!sessionReady || !readyActivation) {
      setChargingSnapshot(null);
      setChargingError("");
      return;
    }
    // Charging policy is session-sensitive. Do not reuse a snapshot captured
    // before this Monitor activation while the current refresh is pending.
    setChargingSnapshot(null);
    setChargingError("");
    const activation = { ...readyActivation };
    let cancelled = false;
    let timer = 0;
    const refresh = async () => {
      let next: ChargingStatus | null = null;
      const requestGeneration = ++chargingRequestGeneration.current;
      chargingRefreshActive.current = true;
      try {
        next = await getChargingStatus(activation.session, activation.generation);
        if (next.monitor_generation !== activation.generation ||
          typeof next.charging_revision !== "number") {
          throw new Error("charging status did not match the current Monitor activation");
        }
        if (!cancelled && requestGeneration === chargingRequestGeneration.current) {
          if (acceptedChargingRevision.current !== next.charging_revision) {
            acceptedChargingRevision.current = next.charging_revision;
            telemetryRequestGeneration.current += 1;
            setTelemetrySnapshot(null);
          }
          setChargingSnapshot({ session: activation.session, status: next });
          setChargingError("");
        }
      } catch (reason) {
        if (!cancelled && requestGeneration === chargingRequestGeneration.current) {
          acceptedChargingRevision.current = -1;
          telemetryRequestGeneration.current += 1;
          setTelemetrySnapshot(null);
          setChargingSnapshot(null);
          setChargingError(String(reason));
          try {
            const epoch: MonitorEpoch = await invalidateMonitorChargingStatus(
              activation.session, activation.generation);
            if (!cancelled && requestGeneration === chargingRequestGeneration.current &&
              epoch.generation === activation.generation) {
              acceptedChargingRevision.current = epoch.revision;
            }
          } catch {
            // Leave the accepted revision invalid until a current refresh succeeds.
          }
        }
      }
      if (!cancelled && requestGeneration === chargingRequestGeneration.current) {
        chargingRefreshActive.current = false;
        const fast = next?.pump.valid &&
          (next.pump.phase === "starting" || next.pump.phase === "active");
        timer = window.setTimeout(refresh, fast ? 1500 : 7000);
      }
    };
    void refresh();
    return () => {
      cancelled = true;
      chargingRequestGeneration.current += 1;
      chargingRefreshActive.current = false;
      window.clearTimeout(timer);
    };
  }, [sessionReady, readyActivation]);
  const batteryPolicy = charging?.battery;
  const batteryPolicyLabel = chargingError ? "Unavailable"
    : !batteryPolicy ? "Reading…"
      : !batteryPolicy.available ? "Unsupported"
        : !batteryPolicy.valid ? "Unavailable"
          : batteryPolicy.transitional ? "Transitional/Unknown"
            : batteryPolicy.mode === "limit" ? `Limit ${batteryPolicy.limit}%`
              : batteryPolicy.mode === "bypass" ? "Bypass" : "Normal";
  const batteryPolicyDetail = chargingError || (batteryPolicy?.available
    ? batteryPolicy.refresh_error || batteryPolicy.error || batteryPolicy.transition_reason
    : undefined);
  const batteryPolicyCurrent = Boolean(
    charging?.coherent && batteryPolicy?.available && batteryPolicy.valid &&
    !batteryPolicy.stale && !batteryPolicy.transitional);
  const batteryPolicyState = !batteryPolicyCurrent ? ""
    : batteryPolicy?.mode === "bypass"
      ? charging?.pump.usb_online === true ? "Active" : "Selected"
      : batteryPolicy?.mode === "limit"
        ? charging?.pump.usb_online !== true ? "Selected"
          : batteryPolicy.charge_behaviour === "inhibit-charge" ? "Paused"
            : batteryPolicy.battery_status?.toLowerCase() === "charging" ? "Charging" : "Active"
        : "";
  const batteryPolicyRow = <Metric label="Battery policy"
    value={`${batteryPolicyLabel}${batteryPolicyState ? ` · ${batteryPolicyState}` : ""}${batteryPolicy?.stale ? " · Stale" : ""}`}
    detail={batteryPolicyDetail} valueColor={chargingError ? "#fc5c65" : undefined} />;
  if (!data) return <div className="rke-monitor">
    <PanelSection>
      <Heading headingRef={monitorTopRef}>Live Performance</Heading>
      <PanelSectionRow><Field label={error || "Reading sensors…"} bottomSeparator="none" /></PanelSectionRow>
    </PanelSection>
    <PanelSection>
      <Heading>Power &amp; Battery</Heading>
      {batteryPolicyRow}
    </PanelSection>
  </div>;
  const logicalCpus = data.cpu_clocks.reduce((total, clock) => total + clock.cpus.length, 0);
  const oneMinuteLoad = data.load_average[0] || 0;
  const queueStatus = logicalCpus && oneMinuteLoad > logicalCpus ? "Overloaded"
    : logicalCpus && oneMinuteLoad >= logicalCpus * 0.75 ? "Busy" : "Normal";
  const bypassSelected = batteryPolicyCurrent && batteryPolicy?.mode === "bypass";
  const batteryFlowState = !data.battery_power_available ? "Unavailable"
    : data.battery_flow_watts >= 0.2 ? "Charging"
      : data.battery_flow_watts <= -0.2 ? "Discharging" : "Idle";
  const batteryFlowWatts = batteryFlowState === "Idle" ? 0 : data.battery_watts;
  const batteryFlowValue = batteryFlowState === "Unavailable" ? "Unavailable"
    : batteryFlowState === "Charging" ? `${batteryFlowWatts.toFixed(1)} W in`
      : batteryFlowState === "Discharging" ? `${batteryFlowWatts.toFixed(1)} W out`
        : "0.0 W";
  const batteryStatus = data.battery_status.toLowerCase();
  const batteryFilling = batteryFlowState === "Charging" ||
    (batteryFlowState === "Unavailable" && batteryStatus === "charging");
  const batteryDraining = batteryFlowState === "Discharging" ||
    (batteryFlowState === "Unavailable" && batteryStatus === "discharging");
  const batteryEstimateDirection = batteryFilling ? "to full"
    : batteryDraining ? "left" : "";
  const batteryEstimateValue = !batteryEstimateDirection ? "—"
    : data.battery_estimate_ready && data.battery_seconds > 0
      ? `${duration(data.battery_seconds)} ${batteryEstimateDirection}`
      : "Calculating…";
  const batteryFlowColor = batteryFlowState === "Charging" ? "#26de81"
    : batteryFlowState === "Idle" ? "#45aaf2" : undefined;
  const backToTop = () => {
    monitorTopRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    monitorTopRef.current?.focus();
  };
  return <div className="rke-monitor">
    <PanelSection>
      <Heading headingRef={monitorTopRef}>Live Performance</Heading>
      <Metric label="Thermal limit" value={data.thermal_limit}
        valueColor={data.thermal_limit === "Clear" ? "#26de81" : data.thermal_limit === "CPU + GPU" ? "#fc5c65" : "#fed330"} />
      <Metric label="CPU load" value={`${data.cpu_percent.toFixed(1)}%`} percent={data.cpu_percent} color={severity(data.cpu_percent, 70, 90)} />
      <Metric label="GPU load" value={`${data.gpu_percent.toFixed(1)}%`} percent={data.gpu_percent} color={severity(data.gpu_percent, 70, 90)} />
      <Metric label="CPU temperature" value={data.cpu_temperature ? `${data.cpu_temperature.toFixed(1)}°C` : "Unavailable"} percent={data.cpu_temperature || undefined} color={temperatureColor(data.cpu_temperature)} />
      <Metric label="CPU hotspot" value={data.cpu_hotspot_temperature ? `${data.cpu_hotspot_temperature.toFixed(1)}°C` : "Unavailable"} percent={data.cpu_hotspot_temperature || undefined} color={temperatureColor(data.cpu_hotspot_temperature)} />
      <Metric label="GPU temperature" value={data.gpu_temperature ? `${data.gpu_temperature.toFixed(1)}°C` : "Unavailable"} percent={data.gpu_temperature || undefined} color={temperatureColor(data.gpu_temperature)} />
      <Metric label="Fan" value={`${data.fan_percent}%`} percent={data.fan_percent} color="#40a9ff" />
      <Metric label="Memory used" value={usedMemory(data.memory_used_mb)} percent={data.memory_percent} color={severity(data.memory_percent, 75, 90)} />
    </PanelSection>
    <PanelSection>
      <Heading>Clocks</Heading>
      <Metric label="CPU governor" value={data.cpu_governor || "Unavailable"} />
      {data.cpu_clocks.map((clock, index) => <Metric key={clock.id}
        label={clusterLabel(index, clock.cpus)}
        value={cpuMhz(clock.frequency)}
        percent={clock.maximum ? clock.frequency * 100 / clock.maximum : 0}
        color="#45aaf2" />)}
      {data.gpu_frequency_max > 0 && <>
        <Metric label="GPU governor" value={data.gpu_governor || "Unavailable"} />
        <Metric label="GPU clock" value={gpuMhz(data.gpu_frequency)}
          percent={data.gpu_frequency * 100 / data.gpu_frequency_max} color="#45aaf2" />
      </>}
    </PanelSection>
    <PanelSection>
      <Heading>Power &amp; Battery</Heading>
      {batteryPolicyRow}
      <Metric label="Battery level" value={`${data.battery_percent}%`}
        percent={data.battery_percent} color={bypassSelected ? "#45aaf2" : batteryColor(data.battery_percent)} />
      <Metric label="Time estimate" value={batteryEstimateValue} />
      <Metric label="Battery flow" value={batteryFlowValue} valueColor={batteryFlowColor} />
    </PanelSection>
    <PanelSection>
      <Heading>Runtime</Heading>
      <Metric label="ROCKNIX cooling profile" value={data.cooling_profile || "Unavailable"} />
      <Metric label="CPU scheduler" value={data.scheduler === "lavd" ? "LAVD (sched_ext)" : "Kernel default"} />
      <Metric label="CPU queue" value={logicalCpus ? queueStatus : "Unavailable"}
        detail={logicalCpus ? `${oneMinuteLoad.toFixed(1)} / ${logicalCpus} cores` : undefined}
        valueColor={queueStatus === "Overloaded" ? "#fc5c65" : queueStatus === "Busy" ? "#fed330" : "#26de81"} />
      {error && <PanelSectionRow><Field label={error} /></PanelSectionRow>}
      <Heading onActivate={backToTop}>Back to top</Heading>
    </PanelSection>
  </div>;
}
