import type { InstallProgress } from "./types";

export type InstallProgressCursor = Pick<
  InstallProgress,
  "generation" | "transaction_id" | "updated_at" | "active" | "terminal" | "acknowledged"
>;

/** Keep installer status monotonic across delayed Decky responses. */
export const chooseInstallProgress = (
  current: InstallProgress | null,
  next: InstallProgress,
): InstallProgress => {
  if (!current) return next;
  if (next.generation > current.generation) return next;
  if (next.generation < current.generation) return current;
  if (next.transaction_id !== current.transaction_id) return current;
  if (next.updated_at > current.updated_at) return next;
  if (next.updated_at < current.updated_at) return current;
  if (current.terminal && !next.terminal) return current;
  if (current.acknowledged && !next.acknowledged) return current;
  return next;
};

export const isNewInstallTransaction = (
  progress: InstallProgress,
  baseline: InstallProgressCursor | null,
) => Boolean(progress.transaction_id) && (
  baseline === null || progress.generation > baseline.generation
);

