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
export type RgbLegacyEffect = "static" | "breath" | "rainbow";
export type RgbEvoEffect = "static" | "breath" | "rgb-breath" | "rainbow" | "reactive";
export type RgbEffect = RgbLegacyEffect | RgbEvoEffect;
export type RgbColor = [number, number, number];
export type RgbProvider = "none" | "sysfs-effects" | "analog-static" |
  "pocket-evo-v3" | "htr3212-static";
export type RgbEvoLayoutMode = "both" | "per-stick" | "quadrants";

export interface RgbEvoZone {
  id: string;
  color: RgbColor;
  brightness: number;
}

export interface RgbEvoLighting {
  effect: RgbEvoEffect;
  layout_mode: RgbEvoLayoutMode;
  zones: RgbEvoZone[];
  color: RgbColor;
  brightness: number;
  idle_color: RgbColor;
  active_color: RgbColor;
}

export type RgbHtrLighting = Omit<RgbEvoLighting, "effect"> & {
  effect: "static";
};

export interface RgbEvoCalibration {
  green_percent: number;
  blue_percent: number;
}

export interface RgbCapability {
  available: boolean;
  provider: RgbProvider;
  modes: RgbMode[];
  effects: RgbEffect[];
  shared_zone: boolean;
  max_brightness: number;
}

export interface RgbLegacyRequest {
  provider: "sysfs-effects" | "analog-static";
  revision: string;
  mode: RgbMode;
  effect: RgbLegacyEffect;
  color: RgbColor;
  brightness: number;
  correction: boolean;
}

export interface RgbEvoRequest {
  provider: "pocket-evo-v3";
  revision: string;
  mode: "off" | "rgb";
  lighting: RgbEvoLighting;
}

export interface RgbHtrRequest {
  provider: "htr3212-static";
  revision: string;
  mode: "off" | "rgb";
  lighting: RgbHtrLighting;
}

export type RgbZonedRequest = RgbEvoRequest | RgbHtrRequest;
export type RgbRequest = RgbLegacyRequest | RgbZonedRequest;

export interface RgbCalibrationRequest {
  provider: "pocket-evo-v3";
  revision: string;
  action: "save" | "reset" | "raw";
  green_percent?: number;
  blue_percent?: number;
}

interface RgbStateBase {
  supported: boolean;
  valid: boolean;
  provider: RgbProvider;
  revision: string;
  modes: RgbMode[];
  effects: RgbEffect[];
  shared_zone: boolean;
  max_brightness: number;
  error: string;
}

export interface RgbUnavailableState extends RgbStateBase {
  provider: "none";
  mode: "unknown";
}

export interface RgbLegacyState extends RgbStateBase {
  provider: "sysfs-effects" | "analog-static";
  zones_differ: boolean;
  mode: RgbMode | "unknown";
  effect: RgbLegacyEffect;
  color: RgbColor;
  brightness: number;
  correction: boolean;
}

export interface RgbEvoState extends RgbStateBase {
  provider: "pocket-evo-v3";
  mode: "off" | "rgb" | "unknown";
  temporarily_gated: boolean;
  lighting: RgbEvoLighting;
  resume_lighting: RgbEvoLighting | null;
  calibration: RgbEvoCalibration;
  calibration_override: RgbEvoCalibration | null;
}

export interface RgbHtrState extends RgbStateBase {
  provider: "htr3212-static";
  mode: "off" | "rgb" | "unknown";
  modes: Array<"off" | "rgb">;
  effects: Array<"static">;
  lighting: RgbHtrLighting;
  resume_lighting: RgbHtrLighting | null;
}

export type RgbState = RgbUnavailableState | RgbLegacyState | RgbEvoState | RgbHtrState;

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
  /** Runtime safety gate. Optional while upgrading from an older backend. */
  mutations_blocked?: boolean;
  /** Exact ROCKNIX Control identity conflicts found beside RK-Enhanced. */
  plugin_conflict?: PluginConflictStatus;
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
  rke_cpu_percent: number | null;
  rke_cpu_available: boolean;
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

export interface FanStatus {
  fan_pwm: number;
  fan_percent: number;
  cooling_profile: string;
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
  /** The immediately preceding published GitHub release, not install history. */
  previous: string;
  /** Last version replaced on this device, when reliable history is available. */
  last_installed?: string;
  last_installed_available?: boolean;
  previous_published?: string;
  error: string;
}

export type InstallProgressPhase =
  | "idle"
  | "starting"
  | "preflight"
  | "checking-releases"
  | "downloading"
  | "validating"
  | "backing-up"
  | "removing-conflict"
  | "stopping-decky"
  | "installing"
  | "starting-decky"
  | "verifying"
  | "rolling-back"
  | "completed"
  | "rolled-back"
  | "blocked"
  | "failed"
  | string;

export type InstallProgressOutcome =
  | "idle"
  | "running"
  | "succeeded"
  | "failed"
  | "rolled-back"
  | "blocked"
  | string;

/** Persisted installer state; timestamps are Unix seconds. */
export interface InstallProgress {
  protocol: number;
  transaction_id: string;
  generation: number;
  active: boolean;
  terminal: boolean;
  acknowledged?: boolean;
  kind: string;
  source_version: string;
  target_version: string;
  decky_version: string;
  phase: InstallProgressPhase;
  message: string;
  outcome: InstallProgressOutcome;
  started_at: number;
  updated_at: number;
  success: boolean | null;
  rolled_back: boolean;
  error: string;
}

export interface PluginConflict {
  name: string;
  version: string;
  directory: string;
  removable?: boolean;
  reason?: string;
}

export interface PluginConflictStatus {
  blocked: boolean;
  conflicts: PluginConflict[];
  message: string;
  scan_error?: string;
}

export interface ConflictRemovalResult {
  started: boolean;
  transaction_id: string;
  message: string;
}

export interface BackupSummary {
  name: string;
  path: string;
  bytes: number;
  modified_at: number;
}

export interface BackupCleanupInfo {
  eligible_count: number;
  eligible_bytes: number;
  kept: BackupSummary | null;
  removable: BackupSummary[];
  errors?: string[];
  removed_count?: number;
  removed_bytes?: number;
}
