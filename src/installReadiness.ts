import { getState, reportFrontendReady } from "./backend";
import { frontendBundleId } from "./frontendIntegrity";

let cancelActiveProbe: (() => void) | null = null;

export const startInstallReadinessProbe = () => {
  cancelActiveProbe?.();
  let cancelled = false;
  const cancel = () => {
    cancelled = true;
    if (cancelActiveProbe === cancel) cancelActiveProbe = null;
  };
  cancelActiveProbe = cancel;

  const run = async () => {
    try {
      for (let attempt = 0; attempt < 240 && !cancelled; attempt += 1) {
        try {
          const state = await getState();
          if (cancelled) return;
          if (!state || typeof state !== "object" || !state.presets || !state.capabilities)
            throw new Error("RK-Enhanced returned an invalid initial state");
          const ready = await reportFrontendReady(frontendBundleId);
          if (cancelled || ready !== false) return;
        } catch (_) {}
        if (attempt < 239 && !cancelled)
          await new Promise(resolve => window.setTimeout(resolve, 500));
      }
    } finally {
      if (cancelActiveProbe === cancel) cancelActiveProbe = null;
    }
  };
  void run();
  return cancel;
};
