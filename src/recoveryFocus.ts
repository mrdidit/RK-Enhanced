import { Navigation } from "@decky/ui";
import {
  consumeAutomaticRecoveryFocusRequest,
  reportAutomaticRecoveryFocusResult,
} from "./backend";

const APP_ID_PATTERN = /^\d+$/;
const STEAM_UI_SETTLE_MS = 3000;
const STEAM_UI_READY_TIMEOUT_MS = 15000;
const STEAM_UI_READY_INTERVAL_MS = 500;
const FOCUS_CONFIRM_TIMEOUT_MS = 1500;
const MAX_NAVIGATION_ATTEMPTS = 3;

type RunningApp = { appid?: unknown };

type RecoverySteamUIStore = {
  RunningApps?: RunningApp[];
  SetRunningApp?: (appid: number) => void;
  NavigateToRunningApp?: (force?: boolean) => void;
  CloseSideMenus?: () => void;
};

type FocusRegistration = { unregister: () => void };

type RecoveryResult =
  | "confirmed"
  | "navigation-dispatched"
  | "steam-ui-unavailable"
  | "selection-failed"
  | "navigation-failed";

const sleep = (milliseconds: number) =>
  new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));

function getRecoveryStore(): RecoverySteamUIStore | null {
  try {
    return ((window as unknown as { SteamUIStore?: RecoverySteamUIStore })
      .SteamUIStore ?? null);
  } catch (_) {
    return null;
  }
}

function storeContainsApp(store: RecoverySteamUIStore, appid: number): boolean {
  try {
    return Array.isArray(store.RunningApps) && store.RunningApps.some((app) => {
      const candidate = typeof app?.appid === "number"
        ? app.appid
        : Number(String(app?.appid ?? ""));
      return Number.isSafeInteger(candidate) && candidate === appid;
    });
  } catch (_) {
    return false;
  }
}

async function waitForRecoveryStore(appid: number): Promise<RecoverySteamUIStore | null> {
  await sleep(STEAM_UI_SETTLE_MS);
  const deadline = performance.now() + STEAM_UI_READY_TIMEOUT_MS;

  while (performance.now() <= deadline) {
    const store = getRecoveryStore();
    if (
      store
      && typeof store.SetRunningApp === "function"
      && storeContainsApp(store, appid)
    ) {
      return store;
    }
    await sleep(STEAM_UI_READY_INTERVAL_MS);
  }
  return null;
}

function watchForFocusedApp(appid: number): {
  promise: Promise<boolean>;
  registration: FocusRegistration;
} | null {
  try {
    const focusUi = window.SteamClient?.System?.UI;
    if (typeof focusUi?.RegisterForFocusChangeEvents !== "function") return null;

    let resolveFocus: (focused: boolean) => void = () => {};
    const promise = new Promise<boolean>((resolve) => {
      resolveFocus = resolve;
    });
    const registration = focusUi.RegisterForFocusChangeEvents((event) => {
      if (Number(event?.focusedApp?.appid) === appid) resolveFocus(true);
    });
    if (!registration || typeof registration.unregister !== "function") return null;
    return { promise, registration };
  } catch (_) {
    return null;
  }
}

async function dispatchResume(
  store: RecoverySteamUIStore,
  appid: number,
): Promise<RecoveryResult> {
  const focusWatch = watchForFocusedApp(appid);
  try {
    try {
      store.SetRunningApp!(appid);
    } catch (_) {
      return "selection-failed";
    }

    try {
      if (typeof store.NavigateToRunningApp === "function") {
        store.NavigateToRunningApp();
      } else {
        // The exact AppID was selected above, so this route cannot select a
        // different running game on older Steam UI builds.
        Navigation.Navigate("/apprunning");
      }
      store.CloseSideMenus?.();
    } catch (_) {
      return "navigation-failed";
    }

    if (!focusWatch) return "navigation-dispatched";
    const confirmed = await Promise.race([
      focusWatch.promise,
      sleep(FOCUS_CONFIRM_TIMEOUT_MS).then(() => false),
    ]);
    return confirmed ? "confirmed" : "navigation-dispatched";
  } finally {
    try {
      focusWatch?.registration.unregister();
    } catch (_) {}
  }
}

async function resumeExistingGame(appid: number): Promise<RecoveryResult> {
  const store = await waitForRecoveryStore(appid);
  if (!store) return "steam-ui-unavailable";

  let result: RecoveryResult = "navigation-dispatched";
  for (let attempt = 0; attempt < MAX_NAVIGATION_ATTEMPTS; attempt += 1) {
    if (!storeContainsApp(store, appid)) return "steam-ui-unavailable";
    result = await dispatchResume(store, appid);
    if (result !== "navigation-dispatched") return result;
    if (attempt + 1 < MAX_NAVIGATION_ATTEMPTS) {
      await sleep(STEAM_UI_READY_INTERVAL_MS);
    }
  }
  return result;
}

export async function restoreAutomaticRecoveryGameFocus(): Promise<void> {
  try {
    const appid = await consumeAutomaticRecoveryFocusRequest();
    if (typeof appid !== "string" || !APP_ID_PATTERN.test(appid)) return;

    const numericAppid = Number(appid);
    if (!Number.isSafeInteger(numericAppid) || numericAppid <= 0) return;

    const result = await resumeExistingGame(numericAppid);
    try {
      await reportAutomaticRecoveryFocusResult(appid, result);
    } catch (_) {}
  } catch (_) {
    // Recovery focus is best-effort and must never prevent the plugin loading.
  }
}
