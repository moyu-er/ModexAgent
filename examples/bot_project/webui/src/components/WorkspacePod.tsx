import { useCallback, useEffect, useMemo, useRef, useState, type FC, type CSSProperties } from "react";
import { Sidebar } from "./Sidebar";
import { ChatView } from "./ChatView";
import { RenameDialog } from "./RenameDialog";
import { GraphSpecListPage } from "./graphs/GraphSpecListPage";
import { GraphSpecEditor } from "./graphs/GraphSpecEditor";
import { GraphSpecDetail } from "./graphs/GraphSpecDetail";
import { GraphInstanceDetail } from "./graphs/GraphInstanceDetail";
import { useWebUIStream } from "../hooks/useWebUIStream";
import { useSessions } from "../hooks/useSessions";
import type { WorkspaceTabStatus } from "../hooks/useWorkspaceTabs";
import type { Route } from "../hooks/useHashRoute";
import { buildTree } from "../lib/sessionTree";
import type { PoolInfo } from "../lib/api";
import type { OutgoingAttachmentRef } from "../types/attachments";

interface PendingHeroSend {
  content: string;
  files?: File[];
  providerName?: string;
  modelName?: string;
}

export interface WorkspacePodProps {
  tabId: string;
  /** Full workspace path — display (path header) only. */
  workspacePath: string;
  /** API workspace scope — "" for the home partition. Constant per pod. */
  scopeWs: string;
  /** Inactive pods render display:none but stay mounted (state preserved). */
  active: boolean;
  /** Live route when active; the pod's frozen stored route when inactive. */
  route: Route;
  navigate: (path: string) => void;
  pools: PoolInfo[];
  poolAgentMap: Record<string, string>;
  sidebarWidth: number;
  resizing: boolean;
  onResizeMouseDown: (e: React.MouseEvent) => void;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onOpenMobile: () => void;
  onReportStatus: (tabId: string, status: WorkspaceTabStatus) => void;
  /** The user's default-assistant preference (PA-07) — seeds the hero
   *  composer's new-conversation pool. */
  preferredPool?: string | null;
  /** True when this pod's path IS the saved default workspace (pin state). */
  isDefaultWorkspace?: boolean;
  /** Save this pod's path as the default workspace (preference write only). */
  onSetDefaultWorkspace?: (path: string) => void;
}

/**
 * One workspace tab's full application state — sidebar, chat/graph views,
 * its own WebSocket connection (the backend routes deltas per attached
 * session per connection, so N pods = N independent connections), and its
 * own session/pool/draft state. Inactive pods stay mounted and hidden so
 * streams, scroll positions, and selections survive tab switches.
 */
export const WorkspacePod: FC<WorkspacePodProps> = ({
  tabId,
  workspacePath,
  scopeWs,
  active,
  route,
  navigate,
  pools,
  poolAgentMap,
  sidebarWidth,
  resizing,
  onResizeMouseDown,
  mobileOpen,
  onCloseMobile,
  onOpenMobile,
  onReportStatus,
  preferredPool,
  isDefaultWorkspace = false,
  onSetDefaultWorkspace,
}) => {
  const {
    sessions,
    selectedId,
    activePool,
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
    handlePoolChange,
    onSent,
    renameSession,
    onSessionsChanged,
    newConvNonce,
  } = useSessions({ ws: scopeWs, pools, preferredPool });

  // Pool for the active session (or the hero view's selected pool). Threads
  // into useWebUIStream (session-scoped API calls) and resolves the skill
  // set for /skillName autocomplete.
  const chatPool = useMemo(() => {
    if (!selectedId) return activePool;
    return getPoolForUuid(selectedId) ?? sessions.find((x) => x.session_id === selectedId)?.pool ?? activePool;
  }, [sessions, selectedId, activePool, getPoolForUuid]);

  const {
    messages,
    isStreaming,
    isPending,
    todos,
    pendingApprovals,
    isApprovingBatch,
    isConnected,
    streamingCount,
    pendingApprovalCount,
    wsClient,
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
    scopeWs,
    onSessionCreated,
    chatPool,
    onSessionsChanged,
  );

  // Connect this pod's WebSocket on mount; drop it when the tab closes.
  useEffect(() => {
    connect();
    return (): void => {
      disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Report tab-level activity for the tab-bar status dots.
  useEffect(() => {
    onReportStatus(tabId, {
      running: streamingCount,
      pendingApprovals: pendingApprovalCount,
      connected: isConnected,
    });
  }, [tabId, streamingCount, pendingApprovalCount, isConnected, onReportStatus]);

  // Agent-node click in the graph execution viewer: jump back to chat and
  // select that session's transcript.
  const handleJumpToSession = useCallback(
    (sessionId: string): void => {
      navigate("");
      selectSession(sessionId);
    },
    [navigate, selectSession],
  );

  // Sidebar actions that imply "leave the graph view and go back to chat".
  const handleNewWithRoute = useCallback(
    (pool: string): void => {
      navigate("");
      handleNew(pool);
      onCloseMobile();
    },
    [navigate, handleNew, onCloseMobile],
  );
  const handlePoolChangeWithRoute = useCallback(
    (pool: string): void => {
      navigate("");
      handlePoolChange(pool);
      onCloseMobile();
    },
    [navigate, handlePoolChange, onCloseMobile],
  );

  // Approve every currently-pending card. Client-side loop — no new endpoint;
  // the batch runs once all requests are approved.
  const onApproveAll = useCallback((): void => {
    pendingApprovals.forEach((v) => {
      submitApproval(v.tool_call_id, "allow");
    });
  }, [pendingApprovals, submitApproval]);

  // No pool selected → no filter: the full workspace-wide session list shows.
  // A selected pool filters the list, but never erases the active chat —
  // selection state is independent of the visible tree.
  const sessionTree = useMemo(
    () =>
      buildTree(
        activePool === ""
          ? sessions
          : sessions.filter((session) => session.pool === activePool),
      ),
    [sessions, activePool],
  );

  // ── Shared rename interaction (PA-02) ─────────────────────────────────
  // ONE dialog instance + ONE edit state for both entries (sidebar tree and
  // chat header menu). A null target means closed; the dialog reads the
  // title straight from the sessions list — no second mutable title store.
  const [renameTargetId, setRenameTargetId] = useState<string | null>(null);
  const renameTarget = useMemo(
    () =>
      renameTargetId
        ? sessions.find((s) => s.session_id === renameTargetId)
        : undefined,
    [sessions, renameTargetId],
  );
  const openRename = useCallback((sessionId: string): void => {
    setRenameTargetId(sessionId);
  }, []);
  const handleRenameSave = useCallback(
    (title: string): Promise<void> => {
      if (!renameTargetId) return Promise.resolve();
      return renameSession(renameTargetId, title).then(() => {
        setRenameTargetId(null);
      });
    },
    [renameTargetId, renameSession],
  );
  const handleRenameCancel = useCallback((): void => {
    setRenameTargetId(null);
  }, []);

  // Selected session's title for the chat header (shared display rule).
  const selectedTitle = useMemo(() => {
    const meta = selectedId
      ? sessions.find((s) => s.session_id === selectedId)?.metadata
      : undefined;
    const raw = meta?.["title"];
    return typeof raw === "string" ? raw : undefined;
  }, [sessions, selectedId]);

  const isSelectedSubagent = useMemo(
    () => !!(selectedId && sessions.some((s) => s.session_id === selectedId && s.parent_session_id)),
    [selectedId, sessions],
  );

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

  // Hero-send flow: create a client-side draft (which useWebUIStream attaches
  // to the backend) and stash the payload; the effect below fires send() as
  // soon as selectedId becomes the uuid prefix.
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

  const onSelect = useCallback(
    (sessionId: string): void => {
      navigate("");
      selectSession(sessionId);
      onCloseMobile();
    },
    [navigate, selectSession, onCloseMobile],
  );

  const shellStyle: CSSProperties = active ? {} : { display: "none" };

  return (
    <div
      className="flex min-h-0 flex-1 overflow-hidden"
      style={shellStyle}
      data-pod-id={tabId}
      aria-hidden={!active}
    >
      <Sidebar
        style={{ ["--sidebar-width" as string]: `${sidebarWidth}px` }}
        sessionTree={sessionTree}
        pools={pools}
        selected={selectedId}
        workspacePath={workspacePath}
        activePool={activePool}
        isLoadingSessions={isLoadingSessions}
        mobileOpen={mobileOpen}
        onCloseMobile={onCloseMobile}
        onSelect={onSelect}
        onNew={handleNewWithRoute}
        onDelete={handleDelete}
        onPoolChange={handlePoolChangeWithRoute}
        revealSessionId={revealSessionId}
        onRenameSession={openRename}
        graphsActive={route.kind !== "chat" && route.kind !== "settings" && route.kind !== "settingsSection"}
        onOpenGraphs={() => navigate("/graphs")}
        isDefaultWorkspace={isDefaultWorkspace}
        onSetDefaultWorkspace={onSetDefaultWorkspace}
      />

      {/* Resize handle — desktop only */}
      <div
        onMouseDown={onResizeMouseDown}
        className="group relative hidden w-2 flex-shrink-0 cursor-col-resize select-none md:block"
      >
        <div
          className={`absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-colors ${
            resizing ? "bg-brand" : "bg-hairline group-hover:bg-brand"
          }`}
        />
      </div>

      <main className="flex flex-1 flex-col min-w-0">
        {route.kind === "graphs" ? (
          <GraphSpecListPage
            workspaceId={scopeWs}
            onEditSpec={(specId): void => navigate(`/graphs/${specId}`)}
          />
        ) : route.kind === "graphSpecDetail" ? (
          <GraphSpecDetail
            workspaceId={scopeWs}
            specId={route.specId}
            onBack={(): void => navigate("/graphs")}
            onEditYaml={(): void => navigate(`/graphs/${route.specId}/edit`)}
            onOpenInstance={(instanceId): void => navigate(`/graphs/instances/${instanceId}`)}
          />
        ) : route.kind === "graphSpecEdit" ? (
          <GraphSpecEditor
            workspaceId={scopeWs}
            specId={route.specId}
            onBack={(): void => navigate(`/graphs/${route.specId}`)}
            onSpecIdChanged={(newId): void => navigate(`/graphs/${newId}`)}
          />
        ) : route.kind === "graphInstance" ? (
          <GraphInstanceDetail
            workspaceId={scopeWs}
            instanceId={route.instanceId}
            wsClient={wsClient ?? undefined}
            onBack={(): void => navigate("/graphs")}
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
            workspace={scopeWs}
            onSend={handleSend}
            onHeroSend={handleHeroSend}
            onPause={pause}
            readOnly={isSelectedSubagent}
            onOpenSidebar={onOpenMobile}
            agentName={agentName}
            pool={chatPool}
            canStart={pools.some((p) => p.name === activePool)}
            sessionTitle={selectedTitle}
            onRenameSession={
              selectedId ? () => openRename(selectedId) : undefined
            }
            heroFocusNonce={newConvNonce}
          />
        )}
      </main>

      {/* The one shared rename dialog (PA-02) — sidebar + header entries. */}
      {renameTarget && (
        <RenameDialog
          sessionId={renameTarget.session_id}
          title={
            typeof renameTarget.metadata?.["title"] === "string"
              ? (renameTarget.metadata["title"] as string)
              : undefined
          }
          onSave={handleRenameSave}
          onCancel={handleRenameCancel}
        />
      )}
    </div>
  );
};
