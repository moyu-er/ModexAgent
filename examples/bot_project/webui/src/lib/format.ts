/** Human-readable byte formatting shared by the composer and attachment cards. */

/** Format a byte count as a human-readable string (512 B, 2.0 KB, 5.0 MB, 3.4 GB, …). */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

/**
 * Format a model context-window limit as the short badge form ("128k").
 * Binary units — model limits are conventionally powers of two (65536 →
 * "64k", 131072 → "128k"); small limits render verbatim.
 */
export function formatContextLimit(tokens: number): string {
  if (!Number.isFinite(tokens) || tokens <= 0) return "—";
  if (tokens < 1024) return String(tokens);
  const k = tokens / 1024;
  if (k < 1024) return `${Math.round(k)}k`;
  return `${(k / 1024).toFixed(k / 1024 >= 10 ? 0 : 1)}m`;
}
