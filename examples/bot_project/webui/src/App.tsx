import { useState, useCallback, useEffect, useMemo, useRef, type FC } from "react";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { SettingsModal } from "./components/settings/SettingsView";
import { ToastProvider } from "./components/ToastContext";
import { useWebUIStream } from "./hooks/useWebUIStream";
import { useSessions } from "./hooks/useSessions";
import { useBackendReady } from "./hooks/useBackendReady";
import { useHashRoute } from "./hooks/useHashRoute";
import { GraphConfigPage } from "./components/graphs/GraphConfigPage";
import { GraphSpecEditor } from "./components/graphs/GraphSpecEditor";
import { GraphExecutionViewer } from "./components/graphs/GraphExecutionViewer";
import { GraphListPage } from "./components/graphs/GraphListPage";
import BootScreen from "./components/BootScreen";
import { DISPERSE_MS } from "./lib/particles";
import { buildTree } from "./lib/sessionTree";
import { storageGetInt, storageSet } from "./lib/storage";
import { listPools } from "./lib/poolApi";
import type { OutgoingAttachmentRef } from "./types/attachments";
import { useT } from "./i18n";
import { LogoMarkIcon } from "./components/ui/icons";

interface PendingHeroSend {
  content: string;
  files?: File[];
  providerName?: string;
  modelName?: string;
}

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

const AppInner: FC = () => {
  const t = useT();
  const {
    sessions,
    selectedId,
    pools,
    activePool,
    workspace,
    isHome,
    recentWorkspaces,
    isLoadingSessions,
    revealSessionId,
    getPoolForUuid,
    handleSessionReady,
    onSessionActivity,
    onSessionCreated,
    selectSession,
    handleNew,
    createDraftForSend,
    handleDelete,
    handleWorkspaceChanged,
    handleGoHome,
    handlePoolChange,
    onSent,
    newConvNonce,
  } = useSessions();

  // Home is its own workspace partition; pass ws only for non-home so home
  // reads/writes use the canonical home dir (matching the no-ws behavior).
  const streamWs = isHome ? "" : workspace;

  const { route, navigate } = useHashRoute();

  // Agent-node click in the graph execution viewer: session id is
  // "{node_id}.{node_name}" (AgentNode._ensure_session). Jump back to chat
  // and select that session's transcript.
  const handleJumpToSession = useCallback(
    (sessionId: string): void => {
      navigate("");
      selectSession(sessionId);
    },
    [navigate, selectSession],
  );

  const {
    messages,
    isStreaming,
    isPending,
    todos,
    pendingApprovals,
    isApprovingBatch,
    isConnected,
    submitApproval,
    connect,
    disconnect,
    send,
    pause,
  } = useWebUIStream(
    selectedId,
    getPoolForUuid,
    handleSessionReady,
    onSessionActivity,
    streamWs,
    onSessionCreated,
  );

  // Approve every currently-pending card. Client-side loop — no new endpoint;
  // the batch runs once all requests are approved.
  const onApproveAll = useCallback((): void => {
    pendingApprovals.forEach((v) => submitApproval(v.tool_call_id, "allow"));
  }, [pendingApprovals, submitApproval]);

  const sessionTree = useMemo(() => buildTree(sessions), [sessions]);

  const isSelectedSubagent = useMemo(
    () => !!(selectedId && sessions.some((s) => s.session_id === selectedId && s.parent_session_id)),
    [selectedId, sessions],
  );

  // Pool → main_agent_name map, fetched once so the hero view (no session
  // selected) can still resolve the main agent for skill autocomplete.
  const [poolAgentMap, setPoolAgentMap] = useState<Record<string, string>>({});
  useEffect(() => {
    let cancelled = false;
    listPools()
      .then((pools) => {
        if (cancelled) return;
        const m: Record<string, string> = {};
        for (const p of pools) m[p.name] = p.main_agent_name;
        setPoolAgentMap(m);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Three-tier fallback: session list → infer from id suffix → active pool's
  // main agent (via poolAgentMap). Keeps the header populated through the
  // hero-send → attach window and gives the hero view a real agent name for
  // skill autocomplete.
  const agentName = useMemo(() => {
    if (!selectedId) {
      return (activePool && poolAgentMap[activePool]) || undefined;
    }
    const s = sessions.find((x) => x.session_id === selectedId);
    if (s && s.agent_name !== "…" && s.agent_name) return s.agent_name;
    const parts = selectedId.split(".");
    if (parts.length >= 2) return parts[1] || "main";
    return activePool || "main";
  }, [sessions, selectedId, activePool, poolAgentMap]);

  // Pool for the active session (or the hero view's active pool). Used to
  // resolve the skill set for /skillName autocomplete.
  const chatPool = useMemo(() => {
    if (!selectedId) return activePool;
    return sessions.find((x) => x.session_id === selectedId)?.pool ?? activePool;
  }, [sessions, selectedId, activePool]);

  const handleSend = useCallback(
    (
      content: string,
      attachments?: OutgoingAttachmentRef[],
      providerName?: string,
      modelName?: string,
    ): void => {
      onSent(selectedId);
      send(content, attachments, providerName, modelName);
    },
    [onSent, send, selectedId],
  );

  // Hero-send flow: when the user submits from the no-session hero composer,
  // create a client-side draft (which useWebUIStream attaches to the backend)
  // and stash the payload. The effect below fires send() as soon as
  // selectedId becomes the uuid prefix — send() adds the optimistic message
  // immediately and queues the ws send for the `attached` handler to flush
  // with the real session id. This bridges the async gap between "user
  // pressed send" and "backend attached + ready to receive".
  const pendingHeroSendRef = useRef<PendingHeroSend | null>(null);

  const handleHeroSend = useCallback(
    (
      content: string,
      files?: File[],
      providerName?: string,
      modelName?: string,
    ): void => {
      createDraftForSend(activePool);
      pendingHeroSendRef.current = { content, files, providerName, modelName };
    },
    [createDraftForSend, activePool],
  );

  useEffect((): void => {
    const pending = pendingHeroSendRef.current;
    if (!pending || !selectedId) return;
    pendingHeroSendRef.current = null;
    onSent(selectedId);
    send(pending.content, undefined, pending.providerName, pending.modelName, pending.files);
  }, [selectedId, onSent, send]);

  const [settingsOpen, setSettingsOpen] = useState<boolean>(false);

  // ── Sidebar resize (refs keep the drag smooth without re-registering listeners)
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

  // Connect WebSocket on mount
  useEffect(() => {
    connect();
    return (): void => {
      disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onSelect = useCallback(
    (sessionId: string): void => {
      selectSession(sessionId);
      setSidebarMobileOpen(false);
    },
    [selectSession],
  );

  return (
    <ToastProvider>
      <div className="flex h-[100dvh] w-screen flex-col overflow-hidden bg-canvas">
        {/* Top status bar — logo mark · brand wordmark · workspace · pool (§8) */}
        <div className="statusline" role="contentinfo" aria-label={t("chat.sessionStatus")}>
          <span className="brand" title={isConnected ? t("chat.connected") : t("chat.disconnected")}>
            <span className="brand-mark" aria-hidden="true">
              <LogoMarkIcon className="h-4 w-4" />
            </span>
            <span className={isConnected ? "dot-signal" : "dot-dim"} aria-hidden="true" />
            ModexBot
          </span>
          <span className="v" title={workspace || "—"}>
            {workspace || "—"}
          </span>
          <span className="v">{activePool || "default"}</span>
        </div>

        <div className="flex min-h-0 flex-1 overflow-hidden">
        <Sidebar
          style={{ ["--sidebar-width" as string]: `${sidebarWidth}px` }}
          sessionTree={sessionTree}
          pools={pools}
          selected={selectedId}
          workspace={workspace}
          isHome={isHome}
          activePool={activePool}
          recentWorkspaces={recentWorkspaces}
          isLoadingSessions={isLoadingSessions}
          mobileOpen={sidebarMobileOpen}
          onCloseMobile={() => setSidebarMobileOpen(false)}
          onSelect={onSelect}
          onNew={handleNew}
          onDelete={handleDelete}
          onWorkspaceChanged={handleWorkspaceChanged}
          onGoHome={handleGoHome}
          onPoolChange={handlePoolChange}
          revealSessionId={revealSessionId}
          onOpenSettings={() => setSettingsOpen(true)}
          graphsActive={route.kind !== "chat"}
          onOpenGraphs={() => navigate("/graphs")}
        />

        {/* Resize handle — desktop only */}
        <div
          onMouseDown={onResizeMouseDown}
          className="group relative hidden w-2 flex-shrink-0 cursor-col-resize select-none md:block"
          title={t("chat.dragResizeSidebar")}
        >
          <div
            className={`absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-colors ${
              resizing.current
                ? "bg-brand"
                : "bg-hairline group-hover:bg-brand"
            }`}
          />
        </div>

        <main className="flex flex-1 flex-col min-w-0">
          {route.kind === "graphs" ? (
            <GraphConfigPage
              workspaceId={streamWs}
              onEditSpec={(specId): void => navigate(`/graphs/${specId}/edit`)}
              onOpenInstances={(): void => navigate("/graphs/instances")}
            />
          ) : route.kind === "graphSpecEdit" ? (
            <GraphSpecEditor
              workspaceId={streamWs}
              specId={route.specId}
              onBack={(): void => navigate("/graphs")}
              onRun={(instanceId): void => navigate(`/graphs/instances/${instanceId}`)}
            />
          ) : route.kind === "graphInstances" ? (
            <GraphListPage
              workspaceId={streamWs}
              onOpenInstance={(instanceId): void => navigate(`/graphs/instances/${instanceId}`)}
              onBack={(): void => navigate("/graphs")}
            />
          ) : route.kind === "graphInstance" ? (
            <GraphExecutionViewer
              workspaceId={streamWs}
              instanceId={route.instanceId}
              onBack={(): void => navigate("/graphs/instances")}
              onJumpToSession={handleJumpToSession}
            />
          ) : (
          <ChatView
            messages={messages}
            isStreaming={isStreaming}
            isPending={isPending}
            todos={todos}
            pendingApprovals={pendingApprovals}
            isApprovingBatch={isApprovingBatch}
            submitApproval={submitApproval}
            onApproveAll={onApproveAll}
            sessionId={selectedId}
            workspace={streamWs}
            onSend={handleSend}
            onHeroSend={handleHeroSend}
            onPause={pause}
            readOnly={isSelectedSubagent}
            onOpenSidebar={() => setSidebarMobileOpen(true)}
            agentName={agentName}
            pool={chatPool}
            heroFocusNonce={newConvNonce}
          />
          )}
        </main>

        <SettingsModal
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
        />

        {sidebarMobileOpen && (
          <div
            className="fixed inset-0 z-30 bg-overlay md:hidden"
            onClick={() => setSidebarMobileOpen(false)}
            aria-hidden="true"
          />
        )}
        </div>
      </div>
    </ToastProvider>
  );
};

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
