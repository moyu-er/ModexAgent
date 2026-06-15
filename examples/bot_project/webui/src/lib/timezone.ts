/**
 * Shared timezone cache + timezone-aware time formatting.
 *
 * The zone name is fetched once from the backend (`GET /api/workspace`) and
 * cached in `localStorage` so the very first render (and offline reloads)
 * already use the last-known zone instead of the browser's local zone.
 *
 * Epoch millisecond timestamps are timezone-agnostic (an absolute instant);
 * timezone only matters when rendering a human-readable wall-clock string —
 * which is exactly what happens here.
 */

const STORAGE_KEY = "modexbot_timezone";

function loadCached(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) || "";
  } catch {
    // localStorage unavailable
    return "";
  }
}

let _tzName: string = loadCached();

/** Update the cached zone name (persisted). Called after /api/workspace. */
export function setTimezone(name: string): void {
  if (!name || name === _tzName) return;
  _tzName = name;
  try {
    localStorage.setItem(STORAGE_KEY, name);
  } catch {
    // localStorage unavailable
  }
}

/** The currently cached zone name (raw server form, e.g. ``Asia/Shanghai``). */
export function getTimezone(): string {
  return _tzName;
}

/**
 * Normalize the server-provided name into a value `Intl.DateTimeFormat`
 * accepts, or ``undefined`` to fall back to the browser's local zone.
 *
 * IANA names (``Asia/Shanghai``) work as-is. Fixed-offset names emitted by the
 * backend look like ``UTC+08:00``; Intl wants the bare ``+08:00`` form.
 */
function intlZone(): string | undefined {
  const name = _tzName;
  if (!name) return undefined;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: name });
    return name;
  } catch {
    if (/^utc/i.test(name)) {
      const stripped = name.replace(/^utc/i, "");
      try {
        new Intl.DateTimeFormat("en-US", { timeZone: stripped });
        return stripped;
      } catch {
        // fall through to browser local
      }
    }
    return undefined;
  }
}

interface TzParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
}

/** Calendar components of ``ms`` in the cached zone. */
function partsInTz(ms: number): TzParts {
  const tz = intlZone();
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const map: Partial<Record<string, number>> = {};
  for (const part of fmt.formatToParts(new Date(ms))) {
    if (part.type !== "literal") {
      map[part.type] = Number(part.value);
    }
  }
  // Some ICU builds emit "24" for midnight with hour12:false.
  const hour = map.hour === 24 ? 0 : (map.hour ?? 0);
  return {
    year: map.year ?? 0,
    month: map.month ?? 0,
    day: map.day ?? 0,
    hour,
    minute: map.minute ?? 0,
  };
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

function normalizeMs(ms: number): number {
  // Defend against accidental second-precision timestamps.
  return ms < 1e12 ? ms * 1000 : ms;
}

/** Sidebar session row: ``MM-DD HH:mm``. */
export function formatShort(ms: number): string {
  const p = partsInTz(normalizeMs(ms));
  return `${pad(p.month)}-${pad(p.day)} ${pad(p.hour)}:${pad(p.minute)}`;
}

/** Message bubble: ``HH:mm`` if the same calendar day, else ``YYYY/M/D HH:mm``. */
export function formatClock(ms: number): string {
  const p = partsInTz(normalizeMs(ms));
  const now = partsInTz(Date.now());
  const sameDay =
    p.year === now.year && p.month === now.month && p.day === now.day;
  if (sameDay) {
    return `${pad(p.hour)}:${pad(p.minute)}`;
  }
  return `${p.year}/${p.month}/${p.day} ${pad(p.hour)}:${pad(p.minute)}`;
}
