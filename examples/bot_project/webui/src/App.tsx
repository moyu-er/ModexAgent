import { useState, useCallback, useEffect, useMemo, useRef, type FC } from "react";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { useWebUIStream } from "./hooks/useWebUIStream";
import { fetchSessions, fetchPools, fetchWorkspace, deleteConversation, changeWorkspace, fetchRecentWorkspaces } from "./lib/api";
import { setTimezone } from "./lib/timezone";
import type { ConversationInfo } from "./types/events";
import type { PoolInfo, RecentWorkspaceEntry } from "./lib/api";

const ACTIVE_POOL_STORAGE_KEY = "modexbot_active_pool";
const SIDEBAR_WIDTH_KEY = "modexbot_sidebar_width";
const DEFAULT_SIDEBAR_WIDTH = 260;
const MIN_SIDEBAR_WIDTH = 200;
const MAX_SIDEBAR_WIDTH = 480;

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
    const stored = localStorage.getItem(SIDEBAR_WIDTH_KEY);
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
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(width));
  } catch {
    // localStorage unavailable
  }
}

// ── Tree helpers ──────────────────────────────────────────────────────────

function computeDisplayName(sessionId: string, parentId?: string): string {
  if (parentId) {
    let i = 0;
    while (i < sessionId.length && i < parentId.length && sessionId[i] === parentId[i]) {
      i++;
    }
    while (i > 0 && sessionId[i - 1] !== ".") {
      i--;
    }
    return sessionId.slice(i) || sessionId;
  }
  return sessionId;
}

interface TreeNode {
  session_id: string;
  displayName: string;
  pool: string;
  parent_session_id: string | null;
  created_at?: number;
  updated_at?: number;
  children: TreeNode[];
}

function buildTree(sessions: ConversationInfo[]): TreeNode[] {
  const childrenMap = new Map<string, ConversationInfo[]>();
  const roots: ConversationInfo[] = [];

  for (const s of sessions) {
    if (s.parent_session_id) {
      const siblings = childrenMap.get(s.parent_session_id) || [];
      siblings.push(s);
      siblings.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
      childrenMap.set(s.parent_session_id, siblings);
    } else {
      roots.push(s);
    }
  }
  roots.sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));

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
  const [sidebarMobileOpen, setSidebarMobileOpen] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => loadSidebarWidth());

  // uuidPrefix → pool, for client-side empty session generation.
  // Cleared once the backend echoes a real session id via the attached event.
  const pendingRef = useRef<Map<string, string>>(new Map());
  // Empty drafts (both pre- and post-attach promotion): id → pool.
  // Clicking "New Conversation" reuses the current draft instead of spawning
  // another one.  Cleared when the user sends the first message.  Survives
  // pool switches so switching back to a pool where a draft was left open
  // still reuses it rather than creating a duplicate.
  const draftIdsRef = useRef<Map<string, string>>(new Map());
  const refreshSessionsRef = useRef<(() => void) | null>(null);
  const treeRefreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const getPoolForUuid = useCallback((uuid: string): string | undefined => {
    return pendingRef.current.get(uuid);
  }, []);

  const handleSessionReady = useCallback(
    (uuidPrefix: string, fullSessionId: string): void => {
      // Transfer the pool association from the bare prefix to the stable id.
      const pool = pendingRef.current.get(uuidPrefix);
      pendingRef.current.delete(uuidPrefix);
      // The backend responded with a stable session id; track it so
      // subsequent "New" clicks reuse this still-empty draft rather than
      // creating a fresh one.
      draftIdsRef.current.delete(uuidPrefix);
      draftIdsRef.current.set(fullSessionId, pool || "main");
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

  // A non-selected session (e.g. a subagent) just started a turn: bump its
  // updated_at so the sidebar re-sorts it to the top of its group immediately,
  // then debounce a refresh so any brand-new session also appears in the tree.
  const onSessionActivity = useCallback((sid: string): void => {
    const now = Date.now();
    setSessions((prev) =>
      prev.some((s) => s.session_id === sid)
        ? prev.map((s) => (s.session_id === sid ? { ...s, updated_at: now } : s))
        : prev,
    );
    if (treeRefreshTimerRef.current) return;
    treeRefreshTimerRef.current = setTimeout(() => {
      treeRefreshTimerRef.current = null;
      refreshSessionsRef.current?.();
    }, 600);
  }, []);

  const { messages, isStreaming, isPending, connect, disconnect, send } =
    useWebUIStream(selectedId, getPoolForUuid, handleSessionReady, onSessionActivity);

  const sessionTree = useMemo(() => buildTree(sessions), [sessions]);

  const isSelectedSubagent = useMemo(
    () => !!(selectedId && sessions.some((s) => s.session_id === selectedId && s.parent_session_id)),
    [selectedId, sessions],
  );

  // Sidebar resize state — refs keep the drag smooth without re-registering listeners.
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
        // Cache the configured timezone for readable-time rendering. The
        // shared module persists it to localStorage, so later reloads use it
        // before the first fetch completes.
        if (info.timezone) {
          setTimezone(info.timezone);
        }
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
      if (treeRefreshTimerRef.current) {
        clearTimeout(treeRefreshTimerRef.current);
        treeRefreshTimerRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refreshSessions = useCallback((): void => {
    fetchSessions(workspace || undefined, activePool)
      .then((loaded) => {
        setSessions((prev) => {
          // Keep any client-side drafts that the backend doesn't know about
          // yet so they don't vanish from the sidebar on refresh.  Drafts are
          // pool-scoped (cleared on pool/workspace switch), so this only
          // applies to the active pool.
          const draftEntries = prev.filter(
            (s) => draftIdsRef.current.has(s.session_id),
          );
          const nonDraft = loaded.filter(
            (s) => !draftIdsRef.current.has(s.session_id),
          );
          return [...draftEntries, ...nonDraft];
        });
      })
      .catch(() => {});
  }, [workspace, activePool]);

  useEffect(() => {
    refreshSessionsRef.current = refreshSessions;
  }, [refreshSessions]);

  const handleSelect = useCallback(
    (sessionId: string): void => {
      setSelectedId(sessionId);
      setSidebarMobileOpen(false);
      refreshSessions();
    },
    [refreshSessions],
  );

  const handleNew = useCallback(
    (pool: string): void => {
      // Reuse the existing empty draft for this pool if one already exists
      // (bare uuid prefix or promoted session id).  Repeated clicks on
      // "New Conversation" must not spawn a stack of unsaved sessions.
      for (const [draftId, draftPool] of draftIdsRef.current) {
        if (draftPool === pool) {
          setSelectedId(draftId);
          setSessions((prev) => {
            if (prev.some((s) => s.session_id === draftId)) {
              return prev;
            }
            const now = Date.now();
            return [
              {
                session_id: draftId,
                agent_name: "…",
                pool,
                parent_session_id: null,
                created_at: now,
                updated_at: now,
              },
              ...prev,
            ];
          });
          return;
        }
      }

      // No empty draft for this pool yet — create a client-side stub.
      const uuidPrefix = crypto.randomUUID().replace(/-/g, "").slice(0, 12);
      pendingRef.current.set(uuidPrefix, pool);
      draftIdsRef.current.set(uuidPrefix, pool);
      setSelectedId(uuidPrefix);
      const now = Date.now();
      // Discard any stale "…" placeholders from a prior session (belt-and-
      // suspenders — draftIdsRef tracking should prevent duplicates, but
      // cleanup ensures we never show two "…" entries).
      setSessions((prev) => [
        {
          session_id: uuidPrefix,
          agent_name: "…",
          pool,
          parent_session_id: null,
          created_at: now,
          updated_at: now,
        },
        ...prev.filter(
          (s) => s.agent_name !== "…" || draftIdsRef.current.has(s.session_id),
        ),
      ]);
    },
    [],
  );

  const handleDelete = useCallback(
    (sessionId: string): void => {
      draftIdsRef.current.delete(sessionId);
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
      draftIdsRef.current.clear();
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
        draftIdsRef.current.clear();
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
      // Clear all draft/pending state — switching pools is a fresh context.
      pendingRef.current.clear();
      draftIdsRef.current.clear();
      fetchSessions(workspace || undefined, pool)
        .then(setSessions)
        .catch(() => {});
    },
    [workspace],
  );

  const handleSend = useCallback(
    (content: string): void => {
      // The session is now real — clear draft tracking so subsequent
      // "New Conversation" clicks create a fresh empty draft.
      if (selectedId) {
        draftIdsRef.current.delete(selectedId);
        // Bump updated_at now so the sidebar (sorted by updated_at desc)
        // immediately moves this conversation to the top, instead of waiting
        // for the backend to refresh.
        const now = Date.now();
        setSessions((prev) =>
          prev.map((s) =>
            s.session_id === selectedId ? { ...s, updated_at: now } : s,
          ),
        );
      }
      send(content);
    },
    [send, selectedId],
  );

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-page-bg-light dark:bg-page-bg-dark">
      <Sidebar
        style={{ ["--sidebar-width" as string]: `${sidebarWidth}px` }}
        sessionTree={sessionTree}
        pools={pools}
        selected={selectedId}
        workspace={workspace}
        isHome={isHome}
        activePool={activePool}
        recentWorkspaces={recentWorkspaces}
        mobileOpen={sidebarMobileOpen}
        onCloseMobile={() => setSidebarMobileOpen(false)}
        onSelect={handleSelect}
        onNew={handleNew}
        onDelete={handleDelete}
        onWorkspaceChanged={handleWorkspaceChanged}
        onGoHome={handleGoHome}
        onPoolChange={handlePoolChange}
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
              ? "bg-ai-brand-light dark:bg-ai-brand-dark"
              : "bg-divider-light dark:bg-divider-dark group-hover:bg-ai-brand-light/50 dark:group-hover:bg-ai-brand-dark/50"
          }`}
        />
      </div>

      <main className="flex flex-1 flex-col min-w-0">
        <ChatView
          messages={messages}
          isStreaming={isStreaming}
          isPending={isPending}
          onSend={handleSend}
          readOnly={isSelectedSubagent}
          onOpenSidebar={() => setSidebarMobileOpen(true)}
        />
      </main>

      {sidebarMobileOpen && (
        <div
          className="fixed inset-0 z-30 bg-overlay-light dark:bg-overlay-dark md:hidden"
          onClick={() => setSidebarMobileOpen(false)}
          aria-hidden="true"
        />
      )}
    </div>
  );
};

export default App;
