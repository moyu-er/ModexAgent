import { useState, useCallback, useEffect, useMemo, useRef, type FC } from "react";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { useWebUIStream } from "./hooks/useWebUIStream";
import { fetchSessions, fetchPools, fetchWorkspace, deleteConversation, changeWorkspace } from "./lib/api";
import { setTimezone } from "./lib/timezone";
import type { ConversationInfo } from "./types/events";
import type { PoolInfo } from "./lib/api";

const ACTIVE_POOL_STORAGE_KEY = "modexbot_active_pool";
const SIDEBAR_WIDTH_KEY = "modexbot_sidebar_width";
const DEFAULT_SIDEBAR_WIDTH = 260;
const MIN_SIDEBAR_WIDTH = 200;
const MAX_SIDEBAR_WIDTH = 480;

const WS_STORAGE_KEY = "modexbot_workspace";

function loadWorkspace(home: string): string {
  try {
    const s = sessionStorage.getItem(WS_STORAGE_KEY);
    if (s) return s;
  } catch {
    // sessionStorage unavailable
  }
  return home;
}

function saveWorkspace(ws: string): void {
  try {
    sessionStorage.setItem(WS_STORAGE_KEY, ws);
  } catch {
    // sessionStorage unavailable
  }
}

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
  const [home, setHome] = useState<string>("");
  const [workspace, setWorkspace] = useState<string>("");
  const [isHome, setIsHome] = useState<boolean>(true);
  const [recentWorkspaces, setRecentWorkspaces] = useState<{ path: string }[]>([]);
  const [sidebarMobileOpen, setSidebarMobileOpen] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => loadSidebarWidth());
  const [isLoadingSessions, setIsLoadingSessions] = useState<boolean>(false);
  const [workspaceVersion, setWorkspaceVersion] = useState<number>(0);
  // Monotonic counter incremented on every workspace/pool switch. Each
  // fetchSessions call captures the current value; its .then() compares it
  // to the latest — if they differ, the response is stale (the user has
  // since switched to another workspace/pool) and is discarded. Without this
  // guard, a slow fetch from the OLD workspace can resolve after the NEW
  // workspace's fetch and overwrite the sidebar with stale sessions.
  const fetchEpochRef = useRef<number>(0);

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
                updated_at: Date.now(),
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
  // Also reveal it in the tree so the SessionTree cascades-expands its
  // ancestor chain (parent, grandparent, … up to the root).
  const [revealSessionId, setRevealSessionId] = useState<string | null>(null);
  const onSessionActivity = useCallback((sid: string): void => {
    const now = Date.now();
    setSessions((prev) =>
      prev.some((s) => s.session_id === sid)
        ? prev.map((s) => (s.session_id === sid ? { ...s, updated_at: now } : s))
        : prev,
    );
    setRevealSessionId(sid);
    if (treeRefreshTimerRef.current) return;
    treeRefreshTimerRef.current = setTimeout(() => {
      treeRefreshTimerRef.current = null;
      refreshSessionsRef.current?.();
    }, 600);
  }, []);

  // A new subagent session was spawned: insert it into the sidebar tree
  // immediately (even before the backend refresh) so the user sees it right
  // away, and reveal its ancestor chain.
  const onSessionCreated = useCallback(
    (sid: string, parentSessionId: string | null): void => {
      const now = Date.now();
      setSessions((prev) => {
        if (prev.some((s) => s.session_id === sid)) {
          return prev;
        }
        const parent = prev.find((s) => s.session_id === parentSessionId);
        const pool = parent?.pool || "main";
        const agentName = sid.split(".")[1] || "unknown";
        return [
          ...prev,
          {
            session_id: sid,
            agent_name: agentName,
            pool,
            parent_session_id: parentSessionId,
            created_at: now,
            updated_at: now,
          },
        ];
      });
      setRevealSessionId(sid);
      // Refresh immediately so the backend authoritative record fills in any
      // missing fields (created_at, etc.) and the tree stays consistent.
      refreshSessionsRef.current?.();
    },
    [],
  );

  const { messages, isStreaming, isPending, todos, connect, disconnect, send, pause } =
    useWebUIStream(
      selectedId,
      getPoolForUuid,
      handleSessionReady,
      onSessionActivity,
      // Home is its own workspace partition; pass ws only for non-home so home
      // reads/writes use the canonical home dir (matching the no-ws behavior).
      isHome ? "" : workspace,
      onSessionCreated,
    );

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

  // Load sessions once the workspace is known, and re-fetch when workspace
  // or active pool changes.  Waiting for `home` prevents the mount-time race
  // where we fetched home's sessions before sessionStorage/workspace info was
  // available, which displayed the wrong workspace's conversations.
  useEffect(() => {
    if (!home) {
      return;
    }
    const epoch = fetchEpochRef.current;
    fetchSessions(workspace || undefined, activePool)
      .then((loaded) => {
        if (fetchEpochRef.current !== epoch) return;
        setSessions((prev) => {
          const draftEntries = prev.filter(
            (s) => draftIdsRef.current.has(s.session_id),
          );
          const nonDraft = loaded.filter(
            (s) => !draftIdsRef.current.has(s.session_id),
          );
          return [...draftEntries, ...nonDraft];
        });
      })
      .catch((err) => {
        console.error("Failed to load sessions:", err);
      });
  }, [home, workspace, activePool]);

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
      .catch((err) => {
        console.error("Failed to load pools:", err);
      });
  }, []);

  // Fetch workspace info on mount (home, recent, timezone, initial ws)
  useEffect(() => {
    fetchWorkspace()
      .then((info) => {
        setHome(info.home);
        setRecentWorkspaces(info.recent);
        const initial = loadWorkspace(info.home);
        setWorkspace(initial);
        setIsHome(initial === info.home);
        if (info.timezone) {
          setTimezone(info.timezone);
        }
      })
      .catch((err) => {
        console.error("Failed to load workspace info:", err);
      });
  }, []);

  // Refresh recentWorkspaces after a workspace switch (not on every conv change)
  useEffect(() => {
    if (workspaceVersion === 0) return;
    fetchWorkspace()
      .then((info) => {
        setRecentWorkspaces(info.recent);
      })
      .catch((err) => {
        console.error("Failed to refresh recent workspaces:", err);
      });
  }, [workspaceVersion]);

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
    const epoch = fetchEpochRef.current;
    fetchSessions(workspace || undefined, activePool)
      .then((loaded) => {
        if (fetchEpochRef.current !== epoch) return;
        setSessions((prev) => {
          const draftEntries = prev.filter(
            (s) => draftIdsRef.current.has(s.session_id),
          );
          const nonDraft = loaded.filter(
            (s) => !draftIdsRef.current.has(s.session_id),
          );
          return [...draftEntries, ...nonDraft];
        });
      })
      .catch((err) => {
        console.error("Failed to refresh sessions:", err);
      });
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
              ...prev,
              {
                session_id: draftId,
                agent_name: "…",
                pool,
                parent_session_id: null,
                created_at: now,
                updated_at: now,
              },
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
        ...prev.filter(
          (s) => s.agent_name !== "…" || draftIdsRef.current.has(s.session_id),
        ),
        {
          session_id: uuidPrefix,
          agent_name: "…",
          pool,
          parent_session_id: null,
          created_at: now,
          updated_at: now,
        },
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
      deleteConversation(sessionId, isHome ? undefined : (workspace || undefined))
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
    [selectedId, workspace, isHome],
  );

  const handleWorkspaceChanged = useCallback(
    (cwd: string): void => {
      const cwdStr = typeof cwd === "string" ? cwd : String(cwd);
      // Cancel any pending debounced refreshSessions from a stale workspace
      // — without this, a 600ms-debounced fetch from the OLD workspace could
      // resolve AFTER this fetch and overwrite the sidebar with stale sessions.
      if (treeRefreshTimerRef.current) {
        clearTimeout(treeRefreshTimerRef.current);
        treeRefreshTimerRef.current = null;
      }
      fetchEpochRef.current += 1;
      const epoch = fetchEpochRef.current;
      setWorkspace(cwdStr);
      setIsHome(cwdStr === home);
      saveWorkspace(cwdStr);
      setSelectedId(null);
      pendingRef.current.clear();
      draftIdsRef.current.clear();
      setWorkspaceVersion((v) => v + 1);
      setSessions([]);
      setIsLoadingSessions(true);
      fetchSessions(cwdStr, activePool)
        .then((loaded) => {
          if (fetchEpochRef.current !== epoch) return;
          setSessions(loaded);
        })
        .catch((err) => {
          console.error("Failed to load sessions after workspace change:", err);
        })
        .finally(() => {
          if (fetchEpochRef.current === epoch) {
            setIsLoadingSessions(false);
          }
        });
    },
    [activePool, home],
  );

  const handleGoHome = useCallback(async (): Promise<void> => {
    try {
      const result = await changeWorkspace("");
      if (result.success) {
        handleWorkspaceChanged(result.cwd);
      } else {
        alert(result.notice || "Failed to return home");
      }
    } catch {
      alert("Network error");
    }
  }, [handleWorkspaceChanged]);

  const handlePoolChange = useCallback(
    (pool: string): void => {
      if (treeRefreshTimerRef.current) {
        clearTimeout(treeRefreshTimerRef.current);
        treeRefreshTimerRef.current = null;
      }
      fetchEpochRef.current += 1;
      const epoch = fetchEpochRef.current;
      setActivePool(pool);
      setSelectedId(null);
      pendingRef.current.clear();
      draftIdsRef.current.clear();
      setSessions([]);
      setIsLoadingSessions(true);
      fetchSessions(workspace || undefined, pool)
        .then((loaded) => {
          if (fetchEpochRef.current !== epoch) return;
          setSessions(loaded);
        })
        .catch((err) => {
          console.error("Failed to load sessions after pool change:", err);
        })
        .finally(() => {
          if (fetchEpochRef.current === epoch) {
            setIsLoadingSessions(false);
          }
        });
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
        isLoadingSessions={isLoadingSessions}
        mobileOpen={sidebarMobileOpen}
        onCloseMobile={() => setSidebarMobileOpen(false)}
        onSelect={handleSelect}
        onNew={handleNew}
        onDelete={handleDelete}
        onWorkspaceChanged={handleWorkspaceChanged}
        onGoHome={handleGoHome}
        onPoolChange={handlePoolChange}
        revealSessionId={revealSessionId}
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
          todos={todos}
          sessionId={selectedId}
          onSend={handleSend}
          onPause={pause}
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
