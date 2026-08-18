import { Field, PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { useEffect, useState } from "react";
import { getTelemetry, setBypassCharging } from "./backend";
import type { Telemetry } from "./types";

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
  return <PanelSectionRow><Field focusable highlightOnFocus label={label}
    description={percent === undefined ? detail : <Meter value={percent} color={color} />}>
    <span style={{ display: "block", width: "100%", textAlign: "right", color: valueColor, fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>{value}</span>
  </Field></PanelSectionRow>;
}

const Heading = ({ children }: { children: string }) =>
  <div style={{ width: "100%", textAlign: "center", fontSize: 18, fontWeight: 700, padding: "10px 0 4px" }}>{children}</div>;

export function Monitor({ active }: { active: boolean }) {
  const [data, setData] = useState<Telemetry | null>(null);
  const [error, setError] = useState("");
  const [bypassBusy, setBypassBusy] = useState(false);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    const refresh = async () => {
      try {
        const next = await getTelemetry();
        if (!cancelled) { setData(next); setError(""); }
      } catch (reason) { if (!cancelled) setError(String(reason)); }
    };
    void refresh();
    const timer = window.setInterval(refresh, 1000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [active]);
  if (!data) return <PanelSection title="Live Monitor"><Field label={error || "Reading sensors…"} /></PanelSection>;
  const logicalCpus = data.cpu_clocks.reduce((total, clock) => total + clock.cpus.length, 0);
  const oneMinuteLoad = data.load_average[0] || 0;
  const queueStatus = logicalCpus && oneMinuteLoad > logicalCpus ? "Overloaded"
    : logicalCpus && oneMinuteLoad >= logicalCpus * 0.75 ? "Busy" : "Normal";
  const bypassCharging = data.bypass_charging;
  const bypassHolding = bypassCharging && Math.abs(data.battery_flow_watts) < 0.2;
  const bypassDischarging = bypassCharging && data.battery_flow_watts <= -0.2;
  const bypassFilling = bypassCharging && data.battery_flow_watts >= 0.2;
  const toggleBypass = async (enabled: boolean) => {
    setBypassBusy(true);
    try {
      await setBypassCharging(enabled);
      setData(await getTelemetry());
      setError("");
    } catch (reason) { setError(String(reason)); }
    finally { setBypassBusy(false); }
  };
  return <div className="rke-monitor">
    <PanelSection>
      <Metric label={bypassHolding ? "Bypass charging" : bypassDischarging ? "Battery remaining" : bypassFilling ? "Battery until full" : data.battery_status === "Charging" ? "Battery until full" : "Battery remaining"}
        value={bypassHolding
          ? `${data.battery_percent}%`
          : data.battery_estimate_ready && data.battery_seconds > 0
            ? `${duration(data.battery_seconds)} · ${data.battery_percent}%`
            : "Calculating…"}
        percent={data.battery_percent} color={bypassCharging ? "#45aaf2" : batteryColor(data.battery_percent)} />
      <PanelSectionRow><ToggleField label="Bypass charging" checked={bypassCharging}
        disabled={bypassBusy} onChange={enabled => void toggleBypass(enabled)} /></PanelSectionRow>
      <Metric label={bypassCharging ? "Battery flow" : data.battery_status === "Charging" ? "Charging power" : "Power draw"}
        value={bypassHolding ? "Holding charge" : data.battery_watts > 0
          ? `${bypassFilling ? "Charging · " : bypassDischarging ? "Drawing · " : ""}${data.battery_watts.toFixed(1)} W`
          : "Unavailable"}
        valueColor={bypassHolding ? "#45aaf2" : undefined} />
      <Metric label="Thermal limit" value={data.thermal_limit}
        valueColor={data.thermal_limit === "Clear" ? "#26de81" : data.thermal_limit === "CPU + GPU" ? "#fc5c65" : "#fed330"} />
      <Heading>Live Performance</Heading>
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
      {data.gpu_frequency_max > 0 && <Metric label={`GPU · ${data.gpu_governor || "unknown"}`}
        value={gpuMhz(data.gpu_frequency)} percent={data.gpu_frequency * 100 / data.gpu_frequency_max} color="#45aaf2" />}
    </PanelSection>
    <PanelSection>
      <Heading>Runtime</Heading>
      <Metric label="Cooling profile" value={data.cooling_profile || "Unavailable"} />
      <Metric label="CPU scheduler" value={data.scheduler === "lavd" ? "LAVD (sched_ext)" : "Kernel default"} />
      <Metric label="CPU queue" value={logicalCpus ? queueStatus : "Unavailable"}
        detail={logicalCpus ? `${oneMinuteLoad.toFixed(1)} / ${logicalCpus} cores` : undefined}
        valueColor={queueStatus === "Overloaded" ? "#fc5c65" : queueStatus === "Busy" ? "#fed330" : "#26de81"} />
      {error && <PanelSectionRow><Field label={error} /></PanelSectionRow>}
    </PanelSection>
  </div>;
}
