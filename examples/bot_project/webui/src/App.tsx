import { useState, useCallback, useEffect, useMemo, useRef, type FC } from "react";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { useWebUIStream } from "./hooks/useWebUIStream";
import { fetchSessions, fetchPools, fetchWorkspace, deleteConversation, changeWorkspace, fetchRecentWorkspaces } from "./lib/api";
import type { ConversationInfo } from "./types/events";
import type { PoolInfo, RecentWorkspaceEntry } from "./lib/api";

const SIDEBAR_STORAGE_KEY = "modexbot_sidebar_width";
const ACTIVE_POOL_STORAGE_KEY = "modexbot_active_pool";
const DEFAULT_SIDEBAR_WIDTH = 260;
const MIN_SIDEBAR_WIDTH = 180;
const MAX_SIDEBAR_WIDTH = 720;

function loadActivePool(): string {
  try {
    const stored = localStorage.getItem(ACTIVE_POOL_STORAGE_KEY);
    if (stored) {
      return stored;
    }
  } catch {
    // localStorage unavailable
  }
  return "main";
}

function saveActivePool(pool: string): void {
  try {
    localStorage.setItem(ACTIVE_POOL_STORAGE_KEY, pool);
  } catch {
    // localStorage unavailable
  }
}

function loadSidebarWidth(): number {
  try {
    const stored = localStorage.getItem(SIDEBAR_STORAGE_KEY);
    if (stored) {
      const parsed = parseInt(stored, 10);
      if (parsed >= MIN_SIDEBAR_WIDTH && parsed <= MAX_SIDEBAR_WIDTH) {
        return parsed;
      }
    }
  } catch {
    // localStorage unavailable
  }
  return DEFAULT_SIDEBAR_WIDTH;
}

function saveSidebarWidth(width: number): void {
  try {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, String(width));
  } catch {
    // localStorage unavailable
  }
}

// ── Tree helpers ──────────────────────────────────────────────────────────

function computeDisplayName(sessionId: string, parentId?: string): string {
  if (parentId) {
    // Strip the longest common prefix between parent and child session_ids,
    // backing up to the last dot boundary.  This handles the case where the
    // subagent has a different agent name than its parent:
    //   parent:  "abc.coding"
    //   child:   "abc.reviewer.ee11"
    //   common:  "abc."     → display: "reviewer.ee11"
    let i = 0;
    while (i < sessionId.length && i < parentId.length && sessionId[i] === parentId[i]) {
      i++;
    }
    while (i > 0 && sessionId[i - 1] !== ".") {
      i--;
    }
    return sessionId.slice(i) || sessionId;
  }
  // Root session: show conversation ID (first segment) — original behavior
  return sessionId.split(".")[0] || sessionId;
}

interface TreeNode {
  session_id: string;
  displayName: string;
  pool: string;
  parent_session_id: string | null;
  created_at?: number;
  children: TreeNode[];
}

function buildTree(sessions: ConversationInfo[]): TreeNode[] {
  const childrenMap = new Map<string, ConversationInfo[]>();
  const roots: ConversationInfo[] = [];

  for (const s of sessions) {
    if (s.parent_session_id) {
      const siblings = childrenMap.get(s.parent_session_id) || [];
      siblings.push(s);
      siblings.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
      childrenMap.set(s.parent_session_id, siblings);
    } else {
      roots.push(s);
    }
  }
  // Sort roots by last update descending (most recent first)
  roots.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));

  function toTreeNode(s: ConversationInfo, parentId?: string): TreeNode {
    return {
      ...s,
      displayName: computeDisplayName(s.session_id, parentId),
      pool: s.pool || "main",
      parent_session_id: s.parent_session_id,
      children: (childrenMap.get(s.session_id) || []).map((c) =>
        toTreeNode(c, s.session_id),
      ),
    };
  }

  return roots.map((r) => toTreeNode(r));
}

const App: FC = () => {
  const [sessions, setSessions] = useState<ConversationInfo[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pools, setPools] = useState<PoolInfo[]>([]);
  const [activePool, setActivePool] = useState<string>(() => loadActivePool());
  const [workspace, setWorkspace] = useState<string>("");
  const [isHome, setIsHome] = useState<boolean>(true);
  const [recentWorkspaces, setRecentWorkspaces] = useState<RecentWorkspaceEntry[]>([]);
  const [sidebarWidth, setSidebarWidth] = useState<number>(loadSidebarWidth);

  // uuidPrefix → pool, for client-side empty session generation
  const pendingRef = useRef<Map<string, string>>(new Map());

  const getPoolForUuid = useCallback((uuid: string): string | undefined => {
    return pendingRef.current.get(uuid);
  }, []);

  const handleSessionReady = useCallback(
    (uuidPrefix: string, fullSessionId: string): void => {
      pendingRef.current.delete(uuidPrefix);
      setSelectedId(fullSessionId);
      setSessions((prev) =>
        prev.map((s) =>
          s.session_id === uuidPrefix
            ? {
                ...s,
                session_id: fullSessionId,
                agent_name:
                  fullSessionId.split(".")[1] || "main",
              }
            : s,
        ),
      );
    },
    [],
  );

  const { messages, isStreaming, isPending, connect, disconnect, send } =
    useWebUIStream(selectedId, getPoolForUuid, handleSessionReady);

  // Build session tree from flat list (memoized — only recompute on sessions change)
  const sessionTree = useMemo(() => buildTree(sessions), [sessions]);

  // Determine if selected session is a subagent (readOnly view)
  const isSelectedSubagent = useMemo(
    () => !!(selectedId && sessions.some((s) => s.session_id === selectedId && s.parent_session_id)),
    [selectedId, sessions],
  );

  // Resize state — use refs for the drag to avoid re-registering listeners per pixel
  const resizing = useRef(false);
  const resizeStartX = useRef(0);
  const resizeStartWidth = useRef(0);
  const sidebarWidthRef = useRef(sidebarWidth);
  sidebarWidthRef.current = sidebarWidth;

  const onResizeMouseDown = useCallback(
    (e: React.MouseEvent): void => {
      e.preventDefault();
      resizing.current = true;
      resizeStartX.current = e.clientX;
      resizeStartWidth.current = sidebarWidthRef.current;
    },
    [],
  );

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

  // Persist activePool changes.
  useEffect(() => {
    saveActivePool(activePool);
  }, [activePool]);

  // Load sessions on mount (filtered by current workspace and pool)
  useEffect(() => {
    fetchSessions(workspace || undefined, activePool)
      .then(setSessions)
      .catch(() => {});
  }, []);

  // Load available pools on mount
  useEffect(() => {
    fetchPools()
      .then((loaded) => {
        setPools(loaded);
        if (loaded.length > 0 && !loaded.some((p) => p.name === activePool)) {
          const first = loaded[0];
          if (first) {
            setActivePool(first.name);
          }
        }
      })
      .catch(() => {});
  }, []);

  // Fetch workspace on mount and on conversation change
  useEffect(() => {
    fetchWorkspace()
      .then((info) => {
        setWorkspace(info.cwd);
        setIsHome(info.is_home);
      })
      .catch(() => {});
    fetchRecentWorkspaces()
      .then(setRecentWorkspaces)
      .catch(() => {});
  }, [selectedId]);

  // Connect WebSocket on mount
  useEffect(() => {
    connect();
    return (): void => {
      disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refreshSessions = useCallback((): void => {
    fetchSessions(workspace || undefined, activePool)
      .then(setSessions)
      .catch(() => {});
  }, [workspace, activePool]);

  const handleSelect = useCallback(
    (sessionId: string): void => {
      setSelectedId(sessionId);
      refreshSessions();
    },
    [refreshSessions],
  );

  const handleNew = useCallback(
    (pool: string): void => {
      const uuidPrefix = crypto.randomUUID().replace(/-/g, "").slice(0, 12);
      // Remove old pending entries, keep all real sessions
      for (const [uuid, p] of pendingRef.current) {
        if (p === pool) pendingRef.current.delete(uuid);
      }
      pendingRef.current.set(uuidPrefix, pool);
      setSelectedId(uuidPrefix);
      setSessions((prev) => [
        // New pending session at top, then all real sessions sorted by time
        { session_id: uuidPrefix, agent_name: "…", pool, parent_session_id: null },
        ...prev.filter((s) => s.agent_name !== "…"),
      ]);
    },
    [],
  );

  const handleDelete = useCallback(
    (sessionId: string): void => {
      // Pending (empty) session: local delete only, no backend call
      if (pendingRef.current.has(sessionId)) {
        pendingRef.current.delete(sessionId);
        setSessions((prev) =>
          prev.filter((s) => s.session_id !== sessionId),
        );
        if (selectedId === sessionId) {
          setSelectedId(null);
        }
        return;
      }
      deleteConversation(sessionId)
        .then(() => {
          setSessions((prev) =>
            prev.filter((s) => s.session_id !== sessionId),
          );
          if (selectedId === sessionId) {
            setSelectedId(null);
          }
        })
        .catch((err) => {
          console.error("Failed to delete conversation:", err);
        });
    },
    [selectedId],
  );

  const handleWorkspaceChanged = useCallback(
    (cwd: string): void => {
      setWorkspace(cwd);
      setSelectedId(null);
      pendingRef.current.clear();
      fetchWorkspace()
        .then((info) => setIsHome(info.is_home))
        .catch(() => {});
      fetchRecentWorkspaces()
        .then(setRecentWorkspaces)
        .catch(() => {});
      fetchSessions(cwd, activePool)
        .then(setSessions)
        .catch(() => {});
    },
    [activePool],
  );

  const handleGoHome = useCallback(async (): Promise<void> => {
    try {
      const result = await changeWorkspace("");
      if (result.success) {
        setWorkspace(result.cwd);
        setIsHome(true);
        setSelectedId(null);
        pendingRef.current.clear();
        fetchSessions(result.cwd, activePool)
          .then(setSessions)
          .catch(() => {});
      } else {
        alert(result.notice || "Failed to return home");
      }
    } catch {
      alert("Network error");
    }
  }, [activePool]);

  const handlePoolChange = useCallback(
    (pool: string): void => {
      setActivePool(pool);
      setSelectedId(null);
      pendingRef.current.clear();
      fetchSessions(workspace || undefined, pool)
        .then(setSessions)
        .catch(() => {});
    },
    [workspace],
  );

  const handleSend = useCallback(
    (content: string): void => {
      send(content);
    },
    [send],
  );

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-ink-950">
      {/* Sidebar with dynamic width */}
      <div
        style={{ width: sidebarWidth }}
        className="flex-shrink-0 h-full"
      >
        <Sidebar
          sessionTree={sessionTree}
          pools={pools}
          selected={selectedId}
          workspace={workspace}
          isHome={isHome}
          activePool={activePool}
          recentWorkspaces={recentWorkspaces}
          onSelect={handleSelect}
          onNew={handleNew}
          onDelete={handleDelete}
          onWorkspaceChanged={handleWorkspaceChanged}
          onGoHome={handleGoHome}
          onPoolChange={handlePoolChange}
        />
      </div>

      {/* Resize handle — invisible 8px hit area with a 1px visible bar */}
      <div
        onMouseDown={onResizeMouseDown}
        className="group relative w-2 flex-shrink-0 cursor-col-resize select-none"
        title="Drag to resize sidebar"
      >
        <div
          className={`absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-colors ${
            resizing.current
              ? "bg-brand-500"
              : "bg-white/[0.06] group-hover:bg-brand-500/50"
          }`}
        />
      </div>

      <main className="flex-1 flex flex-col min-w-0">
        <ChatView
          messages={messages}
          isStreaming={isStreaming}
          isPending={isPending}
          onSend={handleSend}
          readOnly={isSelectedSubagent}
        />
      </main>
    </div>
  );
};

export default App;
