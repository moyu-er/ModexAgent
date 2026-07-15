import { useEffect, useRef, useState } from "react";
import { fetchPools } from "../lib/api";

const POLL_INTERVAL_MS = 1000;
const MAX_ATTEMPTS = 120;

export interface UseBackendReadyResult {
  ready: boolean;
  attempts: number;
  lastError: string | null;
}

/**
 * Polls `/api/pools` until the backend responds, then flips `ready` to true.
 * Used as a gate so the app does not mount its data-fetching hooks until the
 * server is actually accepting requests — avoids the cold-start race where
 * `useSessions` / `useWebUIStream` fire their mount-time fetches against a
 * backend that has not finished booting and then never retry.
 */
export function useBackendReady(): UseBackendReadyResult {
  const [ready, setReady] = useState(false);
  const [attempts, setAttempts] = useState(0);
  const [lastError, setLastError] = useState<string | null>(null);
  const stoppedRef = useRef(false);

  useEffect(() => {
    stoppedRef.current = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = (attempt: number): void => {
      if (stoppedRef.current) return;
      setAttempts(attempt);
      fetchPools()
        .then(() => {
          if (stoppedRef.current) return;
          setReady(true);
          setLastError(null);
        })
        .catch((err: unknown) => {
          if (stoppedRef.current) return;
          setLastError(err instanceof Error ? err.message : String(err));
          if (attempt >= MAX_ATTEMPTS) return;
          timer = setTimeout(() => tick(attempt + 1), POLL_INTERVAL_MS);
        });
    };

    tick(1);

    return (): void => {
      stoppedRef.current = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  return { ready, attempts, lastError };
}
