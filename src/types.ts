export type FanPoint = [number, number];

export interface HardwareProfile {
  cpu_governor: string;
  cpu_min: Record<string, number>;
  cpu_max: Record<string, number>;
  gpu_governor?: string;
  gpu_min?: number;
  gpu_max?: number;
  cooling_profile: string;
  fan_curve: FanPoint[];
  cpu_scheduler: string;
}

export interface CpuPolicy {
  id: string;
  cpus: string[];
  frequencies: number[];
  boost_frequencies: number[];
  boost_enabled: boolean;
  maximum_frequencies: number[];
  governors: string[];
  current: number;
  minimum: number;
  maximum: number;
  effective_maximum: number;
}

export interface GpuCapability {
  available: boolean;
  frequencies: number[];
  governors: string[];
  current: number;
  minimum: number;
  maximum: number;
}

export type RgbMode = "off" | "battery" | "rgb";
export type RgbEffect = "static" | "breath" | "rainbow";
export type RgbColor = [number, number, number];

export interface RgbCapability {
  available: boolean;
  modes: RgbMode[];
  effects: RgbEffect[];
  shared_zone: boolean;
  max_brightness: number;
}

export interface RgbRequest {
  mode: RgbMode;
  effect: RgbEffect;
  color: RgbColor;
  brightness: number;
  correction: boolean;
}

export interface RgbState {
  supported: boolean;
  valid: boolean;
  modes: RgbMode[];
  effects: RgbEffect[];
  shared_zone: boolean;
  max_brightness: number;
  mode: RgbMode | "unknown";
  effect: RgbEffect;
  color: RgbColor;
  brightness: number;
  correction: boolean;
  error: string;
}

export interface Capabilities {
  cpu: CpuPolicy[];
  cpu_governors: string[];
  gpu: GpuCapability;
  schedulers: string[];
  fan_available: boolean;
  rgb: RgbCapability;
}

export interface State {
  capabilities: Capabilities;
  presets: Record<string, HardwareProfile>;
  game_profiles: Record<string, string>;
  steam_default: string;
  system_fan_curve: FanPoint[];
  experimental_unlocked: boolean;
  steam_default_original: HardwareProfile;
  active_preset: string;
  active_appid: string;
  effective_cooling_profile: string;
  fan_curve_active: boolean;
}

export interface Telemetry {
  monitor_generation: number;
  charging_revision: number;
  battery_percent: number;
  battery_status: string;
  battery_seconds: number;
  battery_estimate_ready: boolean;
  battery_power_available: boolean;
  battery_watts: number;
  battery_flow_watts: number;
  cpu_temperature: number;
  cpu_hotspot_temperature: number;
  gpu_temperature: number;
  cpu_percent: number;
  gpu_percent: number;
  cpu_clocks: { id: string; cpus: string[]; frequency: number; minimum: number; maximum: number }[];
  cpu_governor: string;
  gpu_frequency: number;
  gpu_frequency_max: number;
  gpu_governor: string;
  fan_pwm: number;
  fan_percent: number;
  cooling_profile: string;
  scheduler: string;
  memory_percent: number;
  memory_used_mb: number;
  memory_total_mb: number;
  load_average: number[];
  thermal_limit: string;
}

export interface ChargingCommandResult {
  command: string[];
  started: boolean;
  ok: boolean;
  timed_out: boolean;
  exit_status: number | null;
  stdout: string;
  stderr: string;
  kind?: "battery-policy" | "pump-profile";
  requested?: string;
}

interface ChargingComponentStatus {
  available: boolean;
  valid: boolean;
  stale: boolean;
  transitional: boolean;
  captured_at: number;
  error: string;
  refresh_error: string;
  transition_reason?: string;
  command: ChargingCommandResult;
}

export interface BatteryPolicyStatus extends ChargingComponentStatus {
  mode?: "normal" | "bypass" | "limit";
  limit?: number | null;
  capacity?: number;
  charge_behaviour?: "auto" | "inhibit-charge";
  start_threshold?: number;
  end_threshold?: number;
  battery_status?: string;
}

export type UsbInputPowerPath =
  "offline" | "qcom" | "dual-pump" | "transition" | "unavailable";

export interface UsbInputPowerStatus {
  available: boolean;
  valid: boolean;
  stale: boolean;
  path: UsbInputPowerPath;
  microwatts: string | null;
  error: string;
}

export interface PumpProfileStatus extends ChargingComponentStatus {
  enabled?: boolean;
  profile?: "normal" | "slow" | "fast";
  state?: "idle" | "pump-init" | "pump" | "error";
  phase?: "off" | "starting" | "active" | "error" | "transitional";
  last_error?: number;
  last_end_reason?: string;
  requested_voltage_uv?: number;
  usb_online?: boolean;
  usb_type?: string;
  charge_behaviour?: "auto" | "inhibit-charge";
  master_online?: boolean;
  master_health?: string;
  slave_online?: boolean;
  slave_health?: string;
  input_power?: UsbInputPowerStatus;
}

export interface ChargingStatus {
  captured_at: number;
  battery: BatteryPolicyStatus;
  pump: PumpProfileStatus;
  battery_temperature_deci_c: number | null;
  coherent: boolean;
  operation: ChargingCommandResult | null;
  monitor_generation?: number;
  charging_revision?: number;
}

export interface MonitorEpoch {
  generation: number;
  revision: number;
}

export type BatteryLimit = 50 | 60 | 70 | 80 | 90 | 100;

export interface GameRef { appid: string; name: string }

export interface DeviceNetworkInfo {
  ip: string;
  interface: string;
}

export interface UpdateInfo {
  installed: string;
  latest: string;
  update_available: boolean;
  previous: string;
  error: string;
}
