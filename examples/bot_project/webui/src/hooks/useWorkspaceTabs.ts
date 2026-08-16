import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { storageGet, storageSet } from "../lib/storage";

/**
 * Workspace tabs — browser-tab-style workspace switching.
 *
 * A tab is an *instance*, not a path: opening an already-open path simply
 * appends another independent tab (same-site-multiple-tabs model), so there
 * is deliberately NO dedupe logic anywhere in this hook. The home workspace
 * is a pinned tab at index 0 — never closable, never draggable.
 *
 * Tabs persist to sessionStorage (per browser tab, matching the previous
 * single-workspace storage semantics) and restore on reload. The legacy
 * single-workspace key (`modexbot_workspace`) seeds a second tab on first
 * run after the upgrade, then is abandoned.
 */

const TABS_STORAGE_KEY = "modexbot_ws_tabs";
const LEGACY_WS_STORAGE_KEY = "modexbot_workspace";

export interface WorkspaceTab {
  /** Unique instance id (NOT the path — duplicates share paths freely). */
  id: string;
  /** Full workspace path. The home tab uses the home path. */
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
  /** Empty until `home` resolves and the initial tab set is seeded. */
  tabs: WorkspaceTab[];
  activeId: string;
  /** False until the initial seed/restore ran (pods must not mount before). */
  ready: boolean;
  statuses: Record<string, WorkspaceTabStatus>;
  openWorkspace: (path: string) => void;
  closeTab: (id: string) => void;
  activateTab: (id: string) => void;
  /** Move tab `id` to index `to`. Home stays pinned at index 0. */
  reorderTab: (id: string, to: number) => void;
  reportStatus: (id: string, status: WorkspaceTabStatus) => void;
}

function genId(): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 12);
}

/**
 * The tab that takes over when `id` closes: its left neighbor. Shared by
 * the hook (active-fallback) and the App shell (hash restore) so the two
 * can never disagree. Null when `id` is unknown or pinned home (index 0).
 */
export function fallbackTabId(tabs: WorkspaceTab[], id: string): string | null {
  const idx = tabs.findIndex((t) => t.id === id);
  return idx > 0 ? tabs[idx - 1]!.id : null;
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
 * Identical paths keep identical labels (they are genuinely the same place;
 * the pod's full-path header distinguishes them).
 */
export function computeTabLabels(tabs: WorkspaceTab[], home: string): Record<string, string> {
  const bases = tabs.map((t) => (t.path === home ? "__home__" : pathBasename(t.path)));
  const distinctPathsByBase = new Map<string, Set<string>>();
  tabs.forEach((t, i) => {
    const base = bases[i]!;
    if (base === "__home__") return;
    const set = distinctPathsByBase.get(base) ?? new Set<string>();
    set.add(t.path);
    distinctPathsByBase.set(base, set);
  });
  const labels: Record<string, string> = {};
  tabs.forEach((t, i) => {
    const base = bases[i]!;
    if (base === "__home__") {
      labels[t.id] = "__home__"; // component substitutes the localized label
      return;
    }
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

export function useWorkspaceTabs(home: string): UseWorkspaceTabsResult {
  const [tabs, setTabs] = useState<WorkspaceTab[]>([]);
  const [activeId, setActiveId] = useState<string>("");
  const [ready, setReady] = useState(false);
  const [statuses, setStatuses] = useState<Record<string, WorkspaceTabStatus>>({});
  // Status reports arrive from pods on every render-ish; skip no-op writes.
  const statusesRef = useRef(statuses);
  statusesRef.current = statuses;

  // Seed once the home path resolves: restore persisted tabs, else migrate
  // the legacy single-workspace key, else just the home tab.
  useEffect(() => {
    if (!home || ready) return;
    const homeTab: WorkspaceTab = { id: "__home__", path: home };
    const persisted = readPersisted();
    if (persisted && persisted.tabs.length > 0) {
      const rest = persisted.tabs.filter((t) => t.path !== home && t.id !== "__home__");
      const restored = [homeTab, ...rest];
      setTabs(restored);
      setActiveId(
        restored.some((t) => t.id === persisted.active) ? persisted.active : homeTab.id,
      );
    } else {
      const legacy = storageGet(sessionStorage, LEGACY_WS_STORAGE_KEY, "");
      const seeded =
        legacy && legacy !== home
          ? [homeTab, { id: genId(), path: legacy }]
          : [homeTab];
      setTabs(seeded);
      setActiveId(seeded[seeded.length - 1]!.id);
    }
    setReady(true);
  }, [home, ready]);

  // Persist every change.
  useEffect(() => {
    if (!ready) return;
    storageSet(sessionStorage, TABS_STORAGE_KEY, JSON.stringify({ tabs, active: activeId }));
  }, [tabs, activeId, ready]);

  const openWorkspace = useCallback((path: string): void => {
    const id = genId();
    setTabs((prev) => [...prev, { id, path }]);
    setActiveId(id);
  }, []);

  const closeTab = useCallback((id: string): void => {
    setTabs((prev) => {
      const fallback = fallbackTabId(prev, id);
      if (fallback === null) return prev; // home (index 0) or unknown — never closable
      const next = prev.filter((t) => t.id !== id);
      setActiveId((cur) => (cur === id ? fallback : cur));
      setStatuses((s) => {
        if (!(id in s)) return s;
        const copy = { ...s };
        delete copy[id];
        return copy;
      });
      return next;
    });
  }, []);

  const activateTab = useCallback((id: string): void => {
    setActiveId((cur) => (cur === id ? cur : id));
  }, []);

  const reorderTab = useCallback((id: string, to: number): void => {
    setTabs((prev) => {
      const from = prev.findIndex((t) => t.id === id);
      if (from <= 0) return prev; // home pinned
      const clamped = Math.max(1, Math.min(prev.length - 1, to));
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
      statuses,
      openWorkspace,
      closeTab,
      activateTab,
      reorderTab,
      reportStatus,
    }),
    [tabs, activeId, ready, statuses, openWorkspace, closeTab, activateTab, reorderTab, reportStatus],
  );
}
