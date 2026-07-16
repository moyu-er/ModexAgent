import { useState, useCallback, useEffect, useMemo, useRef, type FC } from "react";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { SettingsView } from "./components/settings/SettingsView";
import { ToastProvider } from "./components/ToastContext";
import { useWebUIStream } from "./hooks/useWebUIStream";
import { useSessions } from "./hooks/useSessions";
import { useBackendReady } from "./hooks/useBackendReady";
import BootScreen from "./components/BootScreen";
import { buildTree } from "./lib/sessionTree";
import { storageGetInt, storageSet } from "./lib/storage";
import type { OutgoingAttachmentRef } from "./types/attachments";

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
    handleDelete,
    handleWorkspaceChanged,
    handleGoHome,
    handlePoolChange,
    onSent,
  } = useSessions();

  // Home is its own workspace partition; pass ws only for non-home so home
  // reads/writes use the canonical home dir (matching the no-ws behavior).
  const streamWs = isHome ? "" : workspace;

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

  // Display name of the selected session's owning agent (main or a subagent).
  // Sourced from the session list — no backend change. Undefined when no
  // session is open, in which case the chat header shows no label. The "…"
  // sentinel is the session-list placeholder for not-yet-resolved agent names
  // (fresh drafts); treat it as unknown so it never leaks into the header.
  const agentName = useMemo(() => {
    const s = sessions.find((x) => x.session_id === selectedId);
    return s && s.agent_name !== "…" ? s.agent_name : undefined;
  }, [sessions, selectedId]);

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

  const [view, setView] = useState<"chat" | "settings">("chat");

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
      <div className="flex h-screen w-screen flex-col overflow-hidden bg-canvas">
        {/* Top status bar — brand · workspace · pool */}
        <div className="statusline" role="contentinfo" aria-label="Session status">
          <span className="brand" title={isConnected ? "Connected" : "Disconnected"}>
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
          onOpenSettings={() => setView("settings")}
        />

        {/* Resize handle — desktop only */}
        <div
          onMouseDown={onResizeMouseDown}
          className="group relative hidden w-2 flex-shrink-0 cursor-col-resize select-none md:block"
          title="Drag to resize sidebar"
        >
          <div
            className={`absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-colors ${
              resizing.current
                ? "bg-link"
                : "bg-hairline group-hover:bg-link/50"
            }`}
          />
        </div>

        <main className="flex flex-1 flex-col min-w-0">
          {view === "settings" ? (
            <SettingsView onExit={() => setView("chat")} />
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
              onPause={pause}
              readOnly={isSelectedSubagent}
              onOpenSidebar={() => setSidebarMobileOpen(true)}
              agentName={agentName}
            />
          )}
        </main>

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
  const { ready, attempts, lastError } = useBackendReady();
  if (!ready) {
    return <BootScreen attempts={attempts} lastError={lastError} />;
  }
  return <AppInner />;
};

export default App;
