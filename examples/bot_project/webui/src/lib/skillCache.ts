// Process-lifetime skill cache, keyed by `${pool}:${mainAgent}`.
//
// Rationale: the skill set for a (pool, main-agent) pair is stable across
// sessions and only changes when an admin assigns/unassigns a skill on disk
// (followed by a restart prompt). So one fetch per pair per app session is
// enough; a 60s background refresh (driven by usePoolSkills) picks up disk
// changes without per-keystroke or per-message requests.
//
// This module is a singleton by design — multiple components / sessions
// reading the same (pool, main-agent) share one cached list and one in-flight
// promise. The cache is NOT per-session: switching conversations within the
// same pool's main agent reuses the cached entry instantly.

import type { SkillEntry } from "../types/pool";
import { listAgentSkills } from "./skillsApi";

const REFRESH_INTERVAL_MS = 60_000;

function cacheKey(pool: string, mainAgent: string): string {
  return `${pool}:${mainAgent}`;
}

interface CacheEntry {
  skills: SkillEntry[];
  /** Monotonic timestamp (ms) of the last successful fetch. */
  fetchedAt: number;
}

// Module-level singleton: lives for the lifetime of the page, shared across
// every consumer. Not exported as an object on purpose — callers go through
// the functions below so we can evolve the shape without touching call sites.
const cache = new Map<string, CacheEntry>();

// In-flight fetch promises keyed by cache key. While a fetch for a given pair
// is running, concurrent callers await the same promise instead of issuing a
// second network request.
const inflight = new Map<string, Promise<SkillEntry[]>>();

// Subscribers notified whenever the cache changes (a fetch/refresh completed).
// Read-only consumers (the autocomplete) subscribe to re-render without ever
// issuing a fetch themselves.
const listeners = new Set<() => void>();

function notify(): void {
  for (const fn of listeners) fn();
}

/**
 * Return the cached skill list for (pool, mainAgent), fetching it on first
 * access. Subsequent calls for the same pair return the cached value
 * synchronously (no re-fetch). Use {@link refreshPoolSkills} to force a
 * re-fetch, or let {@link usePoolSkills} refresh on a timer.
 *
 * Returns an empty list (and does not throw) when pool/mainAgent are empty
 * or the fetch fails — the composer simply shows no suggestions.
 */
export async function getPoolSkills(
  pool: string,
  mainAgent: string,
): Promise<SkillEntry[]> {
  if (!pool || !mainAgent) return [];
  const key = cacheKey(pool, mainAgent);

  const hit = cache.get(key);
  if (hit) return hit.skills;

  // No cached value — fetch (deduped against any concurrent fetch).
  return runFetch(pool, mainAgent);
}

/**
 * Force a re-fetch for (pool, mainAgent), updating the cache. Concurrent
 * callers share one in-flight promise. Safe to call when a fetch is already
 * running (the running one wins). No-op for empty pool/mainAgent.
 */
export async function refreshPoolSkills(
  pool: string,
  mainAgent: string,
): Promise<SkillEntry[]> {
  if (!pool || !mainAgent) return [];
  return runFetch(pool, mainAgent);
}

/** Synchronously read the cached list without triggering a fetch (or `null`). */
export function peekPoolSkills(pool: string, mainAgent: string): SkillEntry[] | null {
  if (!pool || !mainAgent) return null;
  return cache.get(cacheKey(pool, mainAgent))?.skills ?? null;
}

/**
 * Subscribe to cache changes. The callback fires after every successful
 * fetch/refresh. Read-only consumers use this to re-render when the cache is
 * updated by the warmup hook, without fetching themselves. Returns an
 * unsubscribe function.
 */
export function subscribeSkills(callback: () => void): () => void {
  listeners.add(callback);
  return () => listeners.delete(callback);
}

/** Whether a cached entry exists for the pair (does not trigger a fetch). */
export function hasPoolSkills(pool: string, mainAgent: string): boolean {
  if (!pool || !mainAgent) return false;
  return cache.has(cacheKey(pool, mainAgent));
}

/** Drop every cached entry. Intended for tests; not used at runtime. */
export function clearSkillCache(): void {
  cache.clear();
  inflight.clear();
  notify();
}

/** The refresh interval the hook polls on. Exported for tests. */
export const SKILL_REFRESH_INTERVAL_MS = REFRESH_INTERVAL_MS;

async function runFetch(pool: string, mainAgent: string): Promise<SkillEntry[]> {
  const key = cacheKey(pool, mainAgent);
  const existing = inflight.get(key);
  if (existing) return existing;

  const p = (async (): Promise<SkillEntry[]> => {
    try {
      const skills = await listAgentSkills(pool, mainAgent);
      cache.set(key, { skills, fetchedAt: Date.now() });
      notify();
      return skills;
    } catch {
      // On failure, leave any prior cached value intact (stale > empty) and
      // return whatever we currently have so the UI keeps working. A future
      // refresh tick will retry.
      return cache.get(key)?.skills ?? [];
    } finally {
      inflight.delete(key);
    }
  })();

  inflight.set(key, p);
  return p;
}
