/**
 * Safe Web Storage wrappers.
 *
 * Every access is guarded so a disabled/unavailable `localStorage` or
 * `sessionStorage` (private mode, sandbox, SSR) degrades to a no-op / default
 * instead of throwing and crashing the render that reads it.
 *
 * `localStorage` persists across sessions (theme, active pool, sidebar width);
 * `sessionStorage` is per-tab (the active workspace, which must not leak
 * between tabs of the same bot).
 */

type StorageLike = Storage;

function available(storage: StorageLike | null): storage is StorageLike {
  try {
    return storage !== null;
  } catch {
    // Accessing the property itself can throw in some sandboxed contexts.
    return false;
  }
}

/** Read a string. Returns `fallback` (default "") when missing or unavailable. */
export function storageGet(
  storage: StorageLike | null,
  key: string,
  fallback = "",
): string {
  if (!available(storage)) return fallback;
  try {
    return storage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

/** Write a string. Silently ignored when storage is unavailable. */
export function storageSet(
  storage: StorageLike | null,
  key: string,
  value: string,
): void {
  if (!available(storage)) return;
  try {
    storage.setItem(key, value);
  } catch {
    // storage unavailable or quota exceeded — ignore.
  }
}

/** Parse a stored string as an int; returns `fallback` when missing/invalid. */
export function storageGetInt(
  storage: StorageLike | null,
  key: string,
  fallback: number,
): number {
  const raw = storageGet(storage, key, "");
  if (!raw) return fallback;
  const parsed = parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}
