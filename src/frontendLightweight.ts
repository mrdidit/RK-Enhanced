export type ComparableGame = {
  appid: string;
  name: string;
};

export const sameGame = (
  left: ComparableGame | null,
  right: ComparableGame | null,
) => left?.appid === right?.appid && left?.name === right?.name;

export const shouldRefreshGameState = (
  observed: ComparableGame | null,
  current: ComparableGame | null,
) => observed?.appid !== current?.appid;

export const canAcceptGameState = (
  observed: ComparableGame | null,
  expectedAppid: string,
  reportedAppid: string,
) => (observed?.appid ?? "") === expectedAppid && reportedAppid === expectedAppid;

export const canAcceptBackendState = (
  observed: ComparableGame | null,
  expectedAppid: string,
  reportedAppid: string,
  conflictBlocked: boolean,
  mutationsBlocked: boolean,
) => (observed?.appid ?? "") === expectedAppid && (
  reportedAppid === expectedAppid || conflictBlocked || mutationsBlocked
);

export type StateRefreshMode = "full" | "metadata";

export const stateRefreshMode = (
  hydrated: boolean,
  observed: ComparableGame | null,
  current: ComparableGame | null,
): StateRefreshMode => !hydrated || shouldRefreshGameState(observed, current)
  ? "full"
  : "metadata";

export const safeAcceptedRefreshMode = (
  requested: StateRefreshMode,
  hydrated: boolean,
  appidMatched: boolean,
): StateRefreshMode => hydrated && !appidMatched ? "metadata" : requested;

export const isCurrentGeneration = (
  expected: number,
  current: number,
  cancelled: boolean,
) => !cancelled && expected === current;

export type ComparableFanStatus = {
  fan_pwm: number;
  fan_percent: number;
  cooling_profile: string;
};

export const sameFanStatus = (
  left: ComparableFanStatus | null,
  right: ComparableFanStatus | null,
) => left?.fan_pwm === right?.fan_pwm &&
  left?.fan_percent === right?.fan_percent &&
  left?.cooling_profile === right?.cooling_profile;

export const shouldPollInstallProgress = (
  panelVisible: boolean,
  launching: boolean,
  active: boolean,
) => panelVisible && (launching || active);

const compactTimestamp = (line: string) => line.replace(
  /^\[\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2}):\d{2}(?:,\d+)?\]/,
  "[$1]",
);

export const formatLogContent = (value: string) => {
  const log = value.trimEnd();
  return log
    ? log.split("\n").reverse().map(compactTimestamp).join("\n")
    : "";
};

type Schedule = (callback: () => void, delay: number) => number;
type Cancel = (timer: number) => void;

/** Run one request at a time and wait until it settles before scheduling more. */
export const startCompletionPoll = (
  work: () => Promise<unknown>,
  delay: number,
  schedule: Schedule = (callback, milliseconds) =>
    window.setTimeout(callback, milliseconds),
  cancelTimer: Cancel = timer => window.clearTimeout(timer),
) => {
  let cancelled = false;
  let timer: number | null = null;

  const poll = () => {
    if (cancelled) return;
    void Promise.resolve()
      .then(work)
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) timer = schedule(() => {
          timer = null;
          poll();
        }, delay);
      });
  };

  poll();
  return () => {
    cancelled = true;
    if (timer !== null) cancelTimer(timer);
  };
};
