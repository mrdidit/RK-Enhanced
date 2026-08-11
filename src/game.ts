import { Router } from "@decky/ui";
import type { GameRef } from "./types";

export function currentGame(): GameRef | null {
  const runtime = window as any;
  const running = (Router as any)?.MainRunningApp || runtime.Router?.MainRunningApp;
  if (!running?.appid) return null;
  const appid = String(running.appid);
  let name = running.display_name || running.displayName || "";
  try {
    const details = runtime.appDetailsStore?.GetAppDetails?.(Number(appid));
    name = details?.strDisplayName || details?.strName || details?.name || name;
  } catch (_) {}
  return { appid, name: name || `App ${appid}` };
}
