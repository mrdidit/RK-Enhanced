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
  governors: string[];
  current: number;
  minimum: number;
  maximum: number;
}

export interface GpuCapability {
  available: boolean;
  frequencies: number[];
  governors: string[];
  current: number;
  minimum: number;
  maximum: number;
}

export interface Capabilities {
  cpu: CpuPolicy[];
  cpu_governors: string[];
  gpu: GpuCapability;
  cooling_profiles: string[];
  schedulers: string[];
  fan_available: boolean;
}

export interface State {
  capabilities: Capabilities;
  presets: Record<string, HardwareProfile>;
  game_profiles: Record<string, string>;
  steam_default: string;
  system_fan_curve: FanPoint[];
  steam_default_original: HardwareProfile;
  active_preset: string;
  active_appid: string;
}

export interface Telemetry {
  battery_percent: number;
  battery_status: string;
  bypass_charging: boolean;
  battery_seconds: number;
  battery_estimate_ready: boolean;
  battery_watts: number;
  battery_flow_watts: number;
  cpu_temperature: number;
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

export interface GameRef { appid: string; name: string }
