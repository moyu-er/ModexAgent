// Two separate hooks for skill autocomplete, reflecting a strict separation
// of concerns:
//
//   usePoolSkillsWarmup — called when the agent's chat view opens. Fetches
//     the skill list once (unless already cached) and refreshes it on a 60s
//     timer. This is the ONLY place that triggers network requests. It does
//     not return data; it only keeps the cache warm.
//
//   usePoolSkills — called by the autocomplete dropdown. Pure cache read via
//     useSyncExternalStore: it NEVER fetches. If the cache is empty (not yet
//     warmed up, or warmup in flight), it returns [] and the autocomplete
//     shows nothing — "no cache, no suggestions" per the design.

import { useEffect, useSyncExternalStore } from "react";
import type { SkillEntry } from "../types/pool";
import {
  getPoolSkills,
  peekPoolSkills,
  refreshPoolSkills,
  subscribeSkills,
  SKILL_REFRESH_INTERVAL_MS,
} from "../lib/skillCache";

const EMPTY: SkillEntry[] = [];

/**
 * Warmup hook: fetch the skill list for (pool, mainAgent) once on mount
 * (unless the cache already has it) and refresh on a 60s interval. Fills the
 * shared cache; does not return data. Call this when the agent's chat view
 * opens. No-op when pool/mainAgent are empty.
 */
export function usePoolSkillsWarmup(
  pool: string | undefined,
  mainAgent: string | undefined,
): void {
  useEffect(() => {
    if (!pool || !mainAgent) return;
    let cancelled = false;

    // Fetch once unless cached. getPoolSkills is a no-op when the cache
    // already holds a value for this pair.
    void getPoolSkills(pool, mainAgent).catch(() => {
      // Swallowed: the cache stays empty and the autocomplete shows nothing.
      // The 60s refresh tick below will retry.
      if (!cancelled) return;
    });

    const timer = window.setInterval(() => {
      void refreshPoolSkills(pool, mainAgent).catch(() => {
        // Same as above — stale-or-empty, retry next tick.
      });
    }, SKILL_REFRESH_INTERVAL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [pool, mainAgent]);
}

/**
 * Read-only hook for the autocomplete. Returns the cached skill list for
 * (pool, mainAgent), or [] when the cache is empty. NEVER fetches — the
 * warmup hook is responsible for populating the cache. Re-renders
 * automatically when the cache is updated by the warmup.
 */
export function usePoolSkills(
  pool: string | undefined,
  mainAgent: string | undefined,
): SkillEntry[] {
  const key = pool && mainAgent ? `${pool}:${mainAgent}` : "";

  return useSyncExternalStore(
    subscribeSkills,
    () => (key ? peekPoolSkills(pool!, mainAgent!) ?? EMPTY : EMPTY),
    () => EMPTY,
  );
}
