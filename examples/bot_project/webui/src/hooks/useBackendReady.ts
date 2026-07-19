import { useCallback, useEffect, useState } from "react";
import { fetchPools } from "../lib/api";

const POLL_INTERVAL_MS = 1000;
const MAX_ATTEMPTS = 120;

export interface UseBackendReadyResult {
  ready: boolean;
  attempts: number;
  lastError: string | null;
  /** Reset attempts/error and resume polling from attempt 1. */
  retry: () => void;
}

/**
 * Polls `/api/pools` until the backend responds, then flips `ready` to true.
 * Used as a gate so the app does not mount its data-fetching hooks until the
 * server is actually accepting requests — avoids the cold-start race where
 * `useSessions` / `useWebUIStream` fire their mount-time fetches against a
 * backend that has not finished booting and then never retry.
 *
 * `retry()` bumps `cycle`, which re-runs the polling effect from attempt 1;
 * the effect-local `stopped` flag guarantees a stale in-flight fetch from
 * the previous cycle can no longer flip state.
 */
export function useBackendReady(): UseBackendReadyResult {
  const [ready, setReady] = useState(false);
  const [attempts, setAttempts] = useState(0);
  const [lastError, setLastError] = useState<string | null>(null);
  const [cycle, setCycle] = useState(0);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = (attempt: number): void => {
      if (stopped) return;
      setAttempts(attempt);
      fetchPools()
        .then(() => {
          if (stopped) return;
          setReady(true);
          setLastError(null);
        })
        .catch((err: unknown) => {
          if (stopped) return;
          setLastError(err instanceof Error ? err.message : String(err));
          if (attempt >= MAX_ATTEMPTS) return;
          timer = setTimeout(() => tick(attempt + 1), POLL_INTERVAL_MS);
        });
    };

    tick(1);

    return (): void => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [cycle]);

  const retry = useCallback((): void => {
    setReady(false);
    setAttempts(0);
    setLastError(null);
    setCycle((c) => c + 1);
  }, []);

  return { ready, attempts, lastError, retry };
}
