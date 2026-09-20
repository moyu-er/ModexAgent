import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { storageGet, storageSet } from "../lib/storage";
import { cdWorkspace } from "../lib/api";

/**
 * Workspace tabs (PA-08) — browser-tab-style workspace switching with NO
 * pinned home and NO index-0 privilege.
 *
 * - Every tab is a normal tab: closable, draggable. Closing the last tab
 *   leaves the empty set; the App shell then shows the open-workspace
 *   entry (the user is never forced into the repo/home directory).
 * - Opening dedupes by canonical path (Windows case/separator tolerant —
 *   the server returns the canonical form; this guard also catches
 *   representation drift between recents and opened paths). Re-opening an
 *   open path ACTIVATES the existing tab instead of appending a
 *   duplicate.
 * - Seeding priority (DESIGN §3.2): persisted tabs (current tab refresh
 *   restore) → the user's default workspace → the server-provided home
 *   (better than an empty startup; never a hard-coded directory) → the
 *   empty open entry. The legacy single-workspace key migrates once, then
 *   is consumed. Seeding waits for the caller's preference fetch to settle
 *   so a slow default load is never preempted by the home fallback, and an
 *   invalid configured default falls back to home (with the error shown)
 *   instead of a blank startup.
 *
 * Tabs persist to sessionStorage (per browser tab, matching the previous
 * single-workspace storage semantics).
 */

const TABS_STORAGE_KEY = "modexbot_ws_tabs";
const LEGACY_WS_STORAGE_KEY = "modexbot_workspace";

export interface WorkspaceTab {
  /** Unique instance id (NOT the path). */
  id: string;
  /** Full workspace path (canonical server form). */
  path: string;
}

/** Live per-tab activity, reported by the pod for the status dots. */
export interface WorkspaceTabStatus {
  /** Conversations currently streaming in this tab's attached tree. */
  running: number;
  /** Pending approvals in this tab's attached tree. */
  pendingApprovals: number;
  /** This tab's WebSocket connection state (drives the brand dot). */
  connected: boolean;
}

interface PersistedTabs {
  tabs: WorkspaceTab[];
  active: string;
}

export interface UseWorkspaceTabsResult {
  /** Empty when nothing is open — the shell renders the open entry then. */
  tabs: WorkspaceTab[];
  /** "" when no tab exists. */
  activeId: string;
  /** False until the initial seed/restore ran (pods must not mount before). */
  ready: boolean;
  error: string;
  statuses: Record<string, WorkspaceTabStatus>;
  /**
   * Open (or switch to) a workspace path. Dedupes by canonical path:
   * an already-open path is ACTIVATED, not re-opened.
   */
  openWorkspace: (path: string) => void;
  /** Close any tab (none is pinned). Closing the last leaves the empty set. */
  closeTab: (id: string) => void;
  activateTab: (id: string) => void;
  /** Move tab `id` to index `to` (clamped). No pinned positions. */
  reorderTab: (id: string, to: number) => void;
  reportStatus: (id: string, status: WorkspaceTabStatus) => void;
}

function genId(): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 12);
}

/**
 * Canonical-path equality: Windows representation tolerant (case-insensitive
 * on drive letters/segments per the platform norm, forward/back slashes,
 * trailing separators). Pure-string — the server owns true canonicalization.
 */
export function sameWorkspacePath(a: string, b: string): boolean {
  const norm = (p: string): string => {
    const value = p.replace(/[\\/]+$/g, "").replace(/[\\/]/g, "/");
    // Case folding belongs to Windows paths, not POSIX server directories.
    return /^[A-Za-z]:\//.test(value) || value.startsWith("//") ? value.toLowerCase() : value;
  };
  return norm(a) === norm(b);
}

/**
 * The tab that takes over when `id` closes: its left neighbor, or the right
 * neighbor when closing the first tab. Shared by the hook and the App shell
 * (hash restore) so the two can never disagree. Null when `id` is unknown or
 * it is the last remaining tab.
 */
export function fallbackTabId(tabs: WorkspaceTab[], id: string): string | null {
  const idx = tabs.findIndex((t) => t.id === id);
  if (idx === -1) return null;
  if (tabs.length <= 1) return null;
  return (tabs[idx - 1] ?? tabs[idx + 1])!.id;
}

/** Last path segment, tolerating both separators and trailing slashes. */
export function pathBasename(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  const seg = trimmed.split(/[\\/]/).filter((s) => s.length > 0);
  return seg[seg.length - 1] ?? trimmed;
}

/** Second-to-last path segment (for duplicate-label disambiguation). */
function pathParentName(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  const seg = trimmed.split(/[\\/]/).filter((s) => s.length > 0);
  return seg.length >= 2 ? seg[seg.length - 2]! : "";
}

/**
 * Tab display labels: basename by default; when two or more tabs with
 * DIFFERENT paths share a basename, those tabs get `parent ▸ basename`.
 * (Identical paths can no longer coexist — openWorkspace dedupes.)
 */
export function computeTabLabels(tabs: WorkspaceTab[]): Record<string, string> {
  const bases = tabs.map((t) => pathBasename(t.path));
  const distinctPathsByBase = new Map<string, Set<string>>();
  tabs.forEach((t, i) => {
    const base = bases[i]!;
    const set = distinctPathsByBase.get(base) ?? new Set<string>();
    set.add(t.path);
    distinctPathsByBase.set(base, set);
  });
  const labels: Record<string, string> = {};
  tabs.forEach((t, i) => {
    const base = bases[i]!;
    const collision = (distinctPathsByBase.get(base)?.size ?? 0) > 1;
    const parent = collision ? pathParentName(t.path) : "";
    labels[t.id] = parent ? `${parent} ▸ ${base}` : base;
  });
  return labels;
}

function readPersisted(): PersistedTabs | null {
  const raw = storageGet(sessionStorage, TABS_STORAGE_KEY, "");
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as PersistedTabs;
    if (!Array.isArray(parsed.tabs) || typeof parsed.active !== "string") return null;
    const valid = parsed.tabs.every(
      (t) => typeof t?.id === "string" && typeof t?.path === "string",
    );
    return valid ? parsed : null;
  } catch {
    return null;
  }
}

export function useWorkspaceTabs(
  home: string,
  defaultWorkspace?: string | null,
  /** False until the caller's preference fetch settled (null default =
   *  genuinely unset). Gates seeding so a slow preference load is never
   *  preempted by the home fallback. Defaults to true — callers that don't
   *  track preferences seed immediately. */
  preferencesReady = true,
): UseWorkspaceTabsResult {
  const [tabs, setTabs] = useState<WorkspaceTab[]>([]);
  const [activeId, setActiveId] = useState<string>("");
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [linkedWorkspace] = useState(() => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("ws"));
  const seedAttempted = useRef(false);
  const [statuses, setStatuses] = useState<Record<string, WorkspaceTabStatus>>({});
  // True once the user has interacted with the tab set (opened or closed
  // anything) — a late-arriving default must never override that choice.
  const userTouchedRef = useRef(false);
  // Status reports arrive from pods on every render-ish; skip no-op writes.
  const statusesRef = useRef(statuses);
  statusesRef.current = statuses;

  // Every startup candidate passes through the same server open/validation
  // seam as a user-selected directory before any chat pod is mounted. The
  // seed waits for the preference fetch: an unset configured default must
  // fall through to the server home, but a SLOW default must never lose to
  // a premature home seed (preferencesReady). When NO primary candidate
  // opens (invalid restored/legacy/deeplink tabs, invalid default), a
  // finite ordered fallback tries the configured default, then the server
  // home — each distinct path exactly once, real validation errors
  // preserved — so startup is never silently empty.
  useEffect(() => {
    if (!home || !preferencesReady || userTouchedRef.current || (ready && (seedAttempted.current || !defaultWorkspace))) return;
    const seed: WorkspaceTab[] = [];
    const persisted = readPersisted();
    let initialActive = "";
    if (persisted && persisted.tabs.length > 0) {
      seed.push(...persisted.tabs);
      initialActive = persisted.tabs.some((t) => t.id === persisted.active)
        ? persisted.active
        : persisted.tabs[0]!.id;
    } else {
      const legacy = storageGet(sessionStorage, LEGACY_WS_STORAGE_KEY, "");
      if (legacy) {
        // Consume the legacy key so it seeds exactly once.
        seed.push({ id: genId(), path: legacy });
      } else if (defaultWorkspace) {
        seed.push({ id: genId(), path: defaultWorkspace });
      } else {
        seed.push({ id: genId(), path: home });
      }
      initialActive = seed[0]?.id ?? "";
    }
    if (linkedWorkspace) {
      const linked = seed.find((tab) => sameWorkspacePath(tab.path, linkedWorkspace)) ?? { id: genId(), path: linkedWorkspace };
      if (!seed.includes(linked)) seed.unshift(linked);
      initialActive = linked.id;
    }
    if (!seed.length) { setReady(true); return; }
    seedAttempted.current = true;
    let cancelled = false;
    void Promise.all(seed.map(async (tab) => {
      try { return { tab: { ...tab, path: await cdWorkspace(tab.path) }, error: "" }; }
      catch (err) { return { tab: null, error: `${tab.path}: ${String(err)}` }; }
    })).then(async (results) => {
      if (cancelled || userTouchedRef.current) return;
      const opened: WorkspaceTab[] = [];
      let active = "";
      for (const { tab } of results) {
        if (!tab) continue;
        const existing = opened.find((item) => sameWorkspacePath(item.path, tab.path));
        if (!existing) opened.push(tab);
        if (tab.id === initialActive) active = existing?.id ?? tab.id;
      }
      const errors: string[] = results.map((result) => result.error).filter(Boolean);
      if (!opened.length) {
        // Fallback candidates: the configured default, then the server
        // home — each tried once, never re-trying a primary candidate that
        // already failed above (its real error is already recorded).
        const candidates: string[] = [];
        const untried = (path: string): boolean =>
          !seed.some((t) => sameWorkspacePath(t.path, path)) &&
          !candidates.some((c) => sameWorkspacePath(c, path));
        if (defaultWorkspace && untried(defaultWorkspace)) candidates.push(defaultWorkspace);
        if (home && untried(home)) candidates.push(home);
        for (const candidate of candidates) {
          try {
            const path = await cdWorkspace(candidate);
            // A fallback await yields — the user may have opened or closed
            // a tab while it was in flight. Their explicit choice wins;
            // never commit a seed under it.
            if (cancelled || userTouchedRef.current) return;
            opened.push({ id: genId(), path });
            active = opened[0]!.id;
            break;
          } catch (err) {
            errors.push(`${candidate}: ${String(err)}`);
          }
        }
      }
      if (cancelled || userTouchedRef.current) return;
      setTabs(opened);
      setActiveId(active || opened[0]?.id || "");
      setError(errors.join("\n"));
      sessionStorage.removeItem(LEGACY_WS_STORAGE_KEY);
      setReady(true);
    });
    return () => { cancelled = true; };
  }, [home, ready, defaultWorkspace, linkedWorkspace, preferencesReady]);

  // Persist every change.
  useEffect(() => {
    if (!ready) return;
    storageSet(sessionStorage, TABS_STORAGE_KEY, JSON.stringify({ tabs, active: activeId }));
  }, [tabs, activeId, ready]);

  const openWorkspace = useCallback((path: string): void => {
    userTouchedRef.current = true;
    setError("");
    // An explicit open before the auto seed finished IS the user's startup
    // decision — the pending seed bails, so finish initialization here or
    // persistence (gated on ready) would stay disabled forever.
    if (!ready) {
      seedAttempted.current = true;
      setReady(true);
    }
    setTabs((prev) => {
      const existing = prev.find((t) => sameWorkspacePath(t.path, path));
      if (existing) {
        setActiveId((cur) => (cur === existing.id ? cur : existing.id));
        return prev;
      }
      const id = genId();
      setActiveId(id);
      return [...prev, { id, path }];
    });
  }, [ready]);

  const closeTab = useCallback((id: string): void => {
    userTouchedRef.current = true;
    setTabs((prev) => {
      if (!prev.some((t) => t.id === id)) return prev;
      const fallback = fallbackTabId(prev, id);
      setActiveId((cur) => (cur === id ? (fallback ?? "") : cur));
      setStatuses((s) => {
        if (!(id in s)) return s;
        const copy = { ...s };
        delete copy[id];
        return copy;
      });
      return prev.filter((t) => t.id !== id);
    });
  }, []);

  const activateTab = useCallback((id: string): void => {
    setActiveId((cur) => (cur === id ? cur : id));
  }, []);

  const reorderTab = useCallback((id: string, to: number): void => {
    setTabs((prev) => {
      const from = prev.findIndex((t) => t.id === id);
      if (from === -1) return prev;
      const clamped = Math.max(0, Math.min(prev.length - 1, to));
      if (clamped === from) return prev;
      const next = [...prev];
      const [moved] = next.splice(from, 1);
      next.splice(clamped, 0, moved!);
      return next;
    });
  }, []);

  const reportStatus = useCallback((id: string, status: WorkspaceTabStatus): void => {
    const cur = statusesRef.current[id];
    if (
      cur &&
      cur.running === status.running &&
      cur.pendingApprovals === status.pendingApprovals &&
      cur.connected === status.connected
    ) {
      return;
    }
    setStatuses((prev) => ({ ...prev, [id]: status }));
  }, []);

  return useMemo(
    () => ({
      tabs,
      activeId,
      ready,
      error,
      statuses,
      openWorkspace,
      closeTab,
      activateTab,
      reorderTab,
      reportStatus,
    }),
    [tabs, activeId, ready, error, statuses, openWorkspace, closeTab, activateTab, reorderTab, reportStatus],
  );
}
