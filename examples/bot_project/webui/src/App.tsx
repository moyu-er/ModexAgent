import { useState, useCallback, useEffect, useRef, type FC } from "react";
import { SettingsPage } from "./components/settings/SettingsPage";
import { ToastProvider, useToast } from "./components/ToastContext";
import { WorkspaceTabBar } from "./components/WorkspaceTabBar";
import { WorkspacePod } from "./components/WorkspacePod";
import { WorkspaceEntry } from "./components/WorkspaceEntry";
import { useBackendReady } from "./hooks/useBackendReady";
import { useHashRoute, parseHash } from "./hooks/useHashRoute";
import { useWorkspaceTabs, fallbackTabId, sameWorkspacePath } from "./hooks/useWorkspaceTabs";
import BootScreen from "./components/BootScreen";
import { DISPERSE_MS } from "./lib/particles";
import { storageGetInt, storageSet } from "./lib/storage";
import { cdWorkspace, fetchPools, fetchWorkspace, type PoolInfo } from "./lib/api";
import { listPools } from "./lib/poolApi";
import {
  fetchPreferences,
  savePreferences,
  type PersonalAssistantPreferences,
} from "./lib/preferencesApi";
import { setTimezone } from "./lib/timezone";
import { useT } from "./i18n";

const SIDEBAR_WIDTH_KEY = "modexbot_sidebar_width";
const DEFAULT_SIDEBAR_WIDTH = 260;
const MIN_SIDEBAR_WIDTH = 200;
const MAX_SIDEBAR_WIDTH = 480;

function loadSidebarWidth(): number {
  const parsed = storageGetInt(localStorage, SIDEBAR_WIDTH_KEY, DEFAULT_SIDEBAR_WIDTH);
  if (parsed >= MIN_SIDEBAR_WIDTH && parsed <= MAX_SIDEBAR_WIDTH) {
    return parsed;
  }
  return DEFAULT_SIDEBAR_WIDTH;
}

function saveSidebarWidth(width: number): void {
  storageSet(localStorage, SIDEBAR_WIDTH_KEY, String(width));
}

const AppShell: FC = () => {
  const t = useT();
  const { show } = useToast();

  // ── Global (workspace-independent) data, fetched once ─────────────────
  const [home, setHome] = useState<string>("");
  const [recentWorkspaces, setRecentWorkspaces] = useState<{ path: string }[]>([]);
  const [pools, setPools] = useState<PoolInfo[]>([]);
  const [poolAgentMap, setPoolAgentMap] = useState<Record<string, string>>({});
  const [workspaceError, setWorkspaceError] = useState<string>("");
  // Personal-assistant preferences (PA-06): default workspace + default
  // pool. Loaded once; updated in place whenever any surface saves.
  const [prefs, setPrefs] = useState<PersonalAssistantPreferences | null>(null);

  const loadWorkspace = useCallback((): void => {
    fetchWorkspace()
      .then((info) => {
        setHome(info.home);
        setRecentWorkspaces(info.recent);
        if (info.timezone) {
          setTimezone(info.timezone);
        }
        setWorkspaceError("");
      })
      .catch((err) => {
        console.error("Failed to load workspace info:", err);
        setWorkspaceError(err instanceof Error ? err.message : String(err));
      });
  }, []);

  useEffect(() => {
    loadWorkspace();
    // A failed preference fetch still counts as settled: the app must boot
    // (default unset → home fallback), never hang on the seed gate.
    fetchPreferences().then(setPrefs).catch(() => setPrefs({ defaultWorkspace: null, defaultPool: "" }));
  }, [loadWorkspace]);

  useEffect(() => {
    fetchPools()
      .then(setPools)
      .catch((err) => {
        console.error("Failed to load pools:", err);
      });
  }, []);

  // Pool → main_agent_name map, so a pod's hero view (no session selected)
  // can still resolve the main agent for skill autocomplete.
  useEffect(() => {
    let cancelled = false;
    listPools()
      .then((loaded) => {
        if (cancelled) return;
        const m: Record<string, string> = {};
        for (const p of loaded) m[p.name] = p.root_agent_name;
        setPoolAgentMap(m);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshRecents = useCallback((): void => {
    fetchWorkspace()
      .then((info) => setRecentWorkspaces(info.recent))
      .catch(() => {});
  }, []);

  // ── Workspace tabs ─────────────────────────────────────────────────────
  // preferencesReady gates seeding so the server home fallback never
  // preempts a slow-configured default workspace (PA-08 startup race).
  const tabs = useWorkspaceTabs(home, prefs?.defaultWorkspace ?? null, prefs !== null);
  const { route, navigate, setNavigationGuard } = useHashRoute();
  // Per-tab hash memory: EVERY transition that changes the active tab saves
  // the outgoing tab's hash and restores the incoming tab's, so each pod
  // keeps its own chat/graph route. Closed tabs' entries are pruned.
  const podHashesRef = useRef<Record<string, string>>({});

  const activateTab = useCallback(
    (id: string): void => {
      if (id === tabs.activeId) return;
      podHashesRef.current[tabs.activeId] = window.location.hash;
      tabs.activateTab(id);
      const stored = podHashesRef.current[id] ?? "";
      if (window.location.hash !== stored) {
        window.location.hash = stored;
      }
    },
    [tabs],
  );

  // Opening a workspace dedupes by canonical path (PA-08): an already-open
  // path activates its tab; a fresh path appends one. The recent list path
  // runs cd first (backend registration + recents bump); the browse modal
  // already ran cd itself. Both paths save the outgoing tab's hash and
  // reset to the chat route for the fresh tab.
  const openRecent = useCallback(
    (path: string): void => {
      cdWorkspace(path)
        .then((cwd) => {
          podHashesRef.current[tabs.activeId] = window.location.hash;
          tabs.openWorkspace(cwd);
          if (window.location.hash) {
            window.location.hash = "";
          }
          refreshRecents();
        })
        .catch((err: unknown) => {
          const message = err instanceof Error ? err.message : t("workspace.networkError");
          show({ message, tone: "error" });
        });
    },
    [tabs, show, t, refreshRecents],
  );

  const openBrowsed = useCallback(
    (path: string): void => {
      podHashesRef.current[tabs.activeId] = window.location.hash;
      tabs.openWorkspace(path);
      if (window.location.hash) {
        window.location.hash = "";
      }
      refreshRecents();
    },
    [tabs, refreshRecents],
  );

  // Closing the active tab restores the fallback tab's remembered route
  // (fallbackTabId is the same neighbor rule the hook uses); the closed
  // tab's entry is pruned either way.
  const closeTab = useCallback(
    (id: string): void => {
      const fallback = fallbackTabId(tabs.tabs, id);
      const closingActive = id === tabs.activeId;
      delete podHashesRef.current[id];
      tabs.closeTab(id);
      if (closingActive) {
        if (fallback) {
          const stored = podHashesRef.current[fallback] ?? "";
          if (window.location.hash !== stored) {
            window.location.hash = stored;
          }
        } else if (window.location.hash) {
          // Closing the LAST tab lands on the open-workspace entry (no
          // pinned home to fall back to) — clear any stale graph route.
          window.location.hash = "";
        }
      }
    },
    [tabs],
  );

  // "Set as default workspace" (PA-08): a preference write only — never
  // touches ScopeRegistry.home, running roots, or open tabs.
  const setDefaultWorkspace = useCallback(
    (path: string): void => {
      const base: PersonalAssistantPreferences = {
        defaultWorkspace: path,
        defaultPool: prefs?.defaultPool ?? "default",
      };
      savePreferences(base)
        .then((saved) => {
          setPrefs(saved);
        })
        .catch(() => {
          show({
            message: t("tabs.setDefaultError"),
            tone: "error",
          });
        });
    },
    [prefs, show, t],
  );

  const handlePreferencesChanged = useCallback(
    (next: PersonalAssistantPreferences): void => {
      setPrefs(next);
    },
    [],
  );

  // ── Shared chrome state ────────────────────────────────────────────────
  const [sidebarMobileOpen, setSidebarMobileOpen] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => loadSidebarWidth());
  const resizing = useRef(false);
  const resizeStartX = useRef(0);
  const resizeStartWidth = useRef(0);
  const sidebarWidthRef = useRef(sidebarWidth);
  sidebarWidthRef.current = sidebarWidth;

  const onResizeMouseDown = useCallback((e: React.MouseEvent): void => {
    e.preventDefault();
    resizing.current = true;
    resizeStartX.current = e.clientX;
    resizeStartWidth.current = sidebarWidthRef.current;
  }, []);

  useEffect(() => {
    const onMouseMove = (e: MouseEvent): void => {
      if (!resizing.current) return;
      const delta = e.clientX - resizeStartX.current;
      const newWidth = Math.min(
        MAX_SIDEBAR_WIDTH,
        Math.max(MIN_SIDEBAR_WIDTH, resizeStartWidth.current + delta),
      );
      setSidebarWidth(newWidth);
    };
    const onMouseUp = (): void => {
      if (resizing.current) {
        resizing.current = false;
        saveSidebarWidth(sidebarWidthRef.current);
      }
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    return (): void => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!tabs.ready) {
    // fetchWorkspace failed after the backend-ready gate: never brick the
    // page on a transient error — offer a retry instead of a blank screen.
    if (!workspaceError) return null;
    return (
      <div className="flex h-[100dvh] w-screen items-center justify-center bg-canvas">
        <div className="flex flex-col items-center gap-3">
          <p className="text-sm text-mute">{t("common.failedToLoad", { error: workspaceError })}</p>
          <button type="button" className="btn-primary" onClick={loadWorkspace}>
            {t("common.retry")}
          </button>
        </div>
      </div>
    );
  }

  // The standalone settings page (PA-11) replaces the pods + tab bar while
  // active. Pods stay MOUNTED (display:none) underneath so streams,
  // attachments and approvals keep flowing through a settings visit.
  const settingsRoute =
    route.kind === "settings" || route.kind === "settingsSection";

  return (
    <div className="flex h-[100dvh] w-screen flex-col overflow-hidden bg-canvas">
      {!settingsRoute && (
        <WorkspaceTabBar
          tabs={tabs.tabs}
          activeId={tabs.activeId}
          statuses={tabs.statuses}
          home={home}
          recentWorkspaces={recentWorkspaces}
          defaultWorkspace={prefs?.defaultWorkspace ?? null}
          onOpenWorkspace={openBrowsed}
          onOpenRecent={openRecent}
          onActivate={activateTab}
          onClose={closeTab}
          onReorder={tabs.reorderTab}
          onOpenSettings={() => navigate("/settings")}
          onSetDefaultWorkspace={setDefaultWorkspace}
        />
      )}

      {!settingsRoute && tabs.error && <p role="alert" className="whitespace-pre-wrap break-all px-4 py-2 text-sm text-danger">{t("common.failedToLoad", { error: tabs.error })}</p>}
      {!settingsRoute && tabs.tabs.length === 0 && (
        <WorkspaceEntry onOpen={() => document.querySelector<HTMLButtonElement>(".wstabs-plus")?.click()} />
      )}

      {tabs.tabs.map((tab) => {
        const isActive = tab.id === tabs.activeId && !settingsRoute;
        const scopeWs = tab.path;
        const podRoute = isActive
          ? route
          : parseHash(podHashesRef.current[tab.id] ?? "");
        return (
          <WorkspacePod
            key={tab.id}
            tabId={tab.id}
            workspacePath={tab.path}
            scopeWs={scopeWs}
            active={isActive}
            route={podRoute.kind === "settings" || podRoute.kind === "settingsSection" ? { kind: "chat" } : podRoute}
            navigate={navigate}
            pools={pools}
            poolAgentMap={poolAgentMap}
            sidebarWidth={sidebarWidth}
            resizing={resizing.current}
            onResizeMouseDown={onResizeMouseDown}
            mobileOpen={isActive && sidebarMobileOpen}
            onCloseMobile={() => setSidebarMobileOpen(false)}
            onOpenMobile={() => setSidebarMobileOpen(true)}
            onReportStatus={tabs.reportStatus}
            preferredPool={prefs?.defaultPool ?? null}
            isDefaultWorkspace={
              prefs?.defaultWorkspace != null &&
              sameWorkspacePath(tab.path, prefs.defaultWorkspace)
            }
            onSetDefaultWorkspace={setDefaultWorkspace}
          />
        );
      })}

      {settingsRoute && (
        <SettingsPage
          section={route.kind === "settingsSection" ? route.section : undefined}
          navigate={navigate}
          onExit={() => navigate("")}
          onPreferencesChanged={handlePreferencesChanged}
          setNavigationGuard={setNavigationGuard}
        />
      )}

      {sidebarMobileOpen && !settingsRoute && (
        <div
          className="fixed inset-0 z-30 bg-overlay md:hidden"
          onClick={() => setSidebarMobileOpen(false)}
          aria-hidden="true"
        />
      )}
    </div>
  );
};

const AppInner: FC = () => (
  <ToastProvider>
    <AppShell />
  </ToastProvider>
);

const App: FC = () => {
  const { ready, attempts, lastError, retry } = useBackendReady();
  // Boot → app handoff (DESIGN.md §7): when the backend flips ready, the app
  // mounts underneath with a one-time fade/stagger while BootScreen plays the
  // disperse on top; BootScreen unmounts after the disperse window.
  const [bootDone, setBootDone] = useState(false);

  useEffect(() => {
    if (!ready) return;
    // Under prefers-reduced-motion the particle disperse is a static frame
    // (no animation), so the 800ms DISPERSE_MS hold is dead time — skip it
    // and unmount BootScreen near-instantly. A 1-frame delay keeps React's
    // mount/unmount ordering stable (app-enter + boot-exit in the same paint
    // would otherwise flash an empty intermediate frame).
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const hold = reduced ? 16 : DISPERSE_MS;
    const timer = setTimeout(() => setBootDone(true), hold);
    return (): void => clearTimeout(timer);
  }, [ready]);

  return (
    <>
      {ready && (
        <div className="app-enter">
          <AppInner />
        </div>
      )}
      {!bootDone && (
        <BootScreen
          attempts={attempts}
          lastError={lastError}
          exiting={ready}
          onRetry={retry}
        />
      )}
    </>
  );
};

export default App;
