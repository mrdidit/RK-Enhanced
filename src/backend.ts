import { call } from "@decky/api";
import type { BatteryLimit, ChargingStatus, HardwareProfile, MonitorEpoch, State, Telemetry, UpdateInfo } from "./types";

export const getState = () => call<[], State>("get_state");
export const beginMonitorSession = (session: string, generation: number) =>
  call<[string, number], MonitorEpoch>("begin_monitor_session", session, generation);
export const endMonitorSession = (session: string, generation: number) =>
  call<[string, number], MonitorEpoch>("end_monitor_session", session, generation);
export const invalidateMonitorChargingStatus = (session: string, generation: number) =>
  call<[string, number], MonitorEpoch>("invalidate_monitor_charging_status", session, generation);
export const getTelemetry = (monitorSession?: string, monitorGeneration?: number) => monitorSession === undefined
  ? call<[], Telemetry>("get_telemetry")
  : call<[string, number], Telemetry>("get_telemetry", monitorSession, monitorGeneration!);
export const getChargingStatus = (monitorSession?: string, monitorGeneration?: number) => monitorSession === undefined
  ? call<[], ChargingStatus>("get_charging_status")
  : call<[string, number], ChargingStatus>("get_charging_status", monitorSession, monitorGeneration!);
type BatteryPolicyRequest = ["normal" | "bypass"] | ["limit", BatteryLimit];
export const setBatteryPolicy = (...request: BatteryPolicyRequest) =>
  call<BatteryPolicyRequest, ChargingStatus>("set_battery_policy", ...request);
export const setPumpProfile = (profile: "normal" | "slow" | "fast", experimentalRiskConfirmed: boolean) =>
  call<["normal" | "slow" | "fast", boolean], ChargingStatus>("set_pump_profile", profile, experimentalRiskConfirmed);
export const unlockExperimental = (code: string) => call<[string], State>("unlock_experimental", code);
export const lockExperimental = () => call<[], State>("lock_experimental");
export const getLog = () => call<[], string>("get_log");
export const clearLog = () => call<[], boolean>("clear_log");
export const getUpdateInfo = () => call<[], UpdateInfo>("get_update_info");
export const installRelease = (version: string) => call<[string], boolean>("install_release", version);
export const applyProfile = (profile: HardwareProfile) => call<[HardwareProfile], boolean>("apply_profile", profile);
export const savePreset = (name: string, profile: HardwareProfile) => call<[string, HardwareProfile], State>("save_preset", name, profile);
export const restoreSteamDefault = () => call<[], State>("restore_steam_default");
export const renamePreset = (oldName: string, newName: string) => call<[string, string], State>("rename_preset", oldName, newName);
export const deletePreset = (name: string) => call<[string], State>("delete_preset", name);
export const assignGame = (appid: string, preset: string) => call<[string, string], State>("assign_game", appid, preset);
export const setSteamDefault = (preset: string) => call<[string], State>("set_steam_default", preset);
export const saveSystemFanCurve = (curve: HardwareProfile["fan_curve"]) => call<[HardwareProfile["fan_curve"]], State>("save_system_fan_curve", curve);
export const unassignGame = (appid: string) => call<[string], State>("unassign_game", appid);
export const activateGame = (appid: string) => call<[string], { applied: boolean; preset: string }>("activate_game", appid);
