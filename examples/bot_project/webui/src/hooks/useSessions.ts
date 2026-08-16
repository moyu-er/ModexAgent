import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteConversation,
  fetchSessions,
  type PoolInfo,
} from "../lib/api";
import { storageGet, storageSet } from "../lib/storage";
import type { ConversationInfo } from "../types/events";

/**
 * Per-workspace-pod session state (workspace-tabs architecture).
 *
 * Each workspace tab mounts one pod with one ``useSessions`` instance whose
 * ``ws`` scope is constant for the pod's lifetime ("" = home partition).
 * The old global workspace-switching machinery (handleWorkspaceChanged,
 * handleGoHome, recents, epoch-guarded cross-workspace races) is gone —
 * switching workspaces is now a tab switch handled entirely above this hook.
 *
 * Behavioral invariants preserved from the original global implementation:
 *  - ``fetchEpochRef``: a monotonic counter captured per fetch; its ``.then``
 *    discards any response whose epoch no longer matches (stale pool-switch
 *    overwrite guard — pools still switch within a pod).
 *  - ``draftIdsRef``: empty drafts are reused across "New Conversation" clicks
 *    and survive pool switches; cleared on first send.
 */

function poolStorageKey(ws: string): string {
  return `modexbot_active_pool:${ws || "__home__"}`;
}

function loadActivePool(ws: string): string {
  // Fall back to the legacy global key so the upgrade preserves the user's
  // last pool choice for the first pod that opens.
  return (
    storageGet(localStorage, poolStorageKey(ws), "") ||
    storageGet(localStorage, "modexbot_active_pool", "main")
  );
}

function saveActivePool(ws: string, pool: string): void {
  storageSet(localStorage, poolStorageKey(ws), pool);
}

export interface UseSessionsOptions {
  /** API workspace scope for this pod — "" means the home partition. */
  ws: string;
  /** Global pool list (fetched once at the app level and passed down). */
  pools: PoolInfo[];
}

export interface UseSessionsResult {
  sessions: ConversationInfo[];
  selectedId: string | null;
  activePool: string;
  isLoadingSessions: boolean;
  revealSessionId: string | null;
  /** Resolve the pool for a client-side pending (uuid-prefix) session. */
  getPoolForUuid: (uuid: string) => string | undefined;
  /** Promote a pending uuid-prefix session to its backend-assigned full id. */
  handleSessionReady: (uuidPrefix: string, fullSessionId: string) => void;
  /** Sidebar reorder trigger when a non-selected session starts a turn. */
  onSessionActivity: (sessionId: string) => void;
  /** Insert a freshly-spawned subagent session into the tree. */
  onSessionCreated: (sessionId: string, parentSessionId: string | null) => void;
  /** Select a session (also used as the sidebar onSelect target). */
  selectSession: (sessionId: string) => void;
  handleNew: (pool: string) => void;
  /** Create a client-side draft session for the hero-send flow. Returns the
   *  uuid prefix so the caller can correlate with onSessionReady. The draft
   *  is NOT inserted into the sidebar list — it appears once
   *  handleSessionReady inserts the promoted full session id (the backend
   *  emits conversation_created only for subagents, not main-agent sessions). */
  createDraftForSend: (pool: string) => string;
  handleDelete: (sessionId: string) => void;
  handlePoolChange: (pool: string) => void;
  /** Clear draft tracking + bump updated_at once the user sends a message. */
  onSent: (sessionId: string | null) => void;
  /** Bumped on every "New Conversation" click; ChatView replays the hero
   *  focus + pulse feedback on change. */
  newConvNonce: number;
}

export function useSessions({ ws, pools }: UseSessionsOptions): UseSessionsResult {
  const [sessions, setSessions] = useState<ConversationInfo[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activePool, setActivePool] = useState<string>(() => loadActivePool(ws));
  const [isLoadingSessions, setIsLoadingSessions] = useState<boolean>(false);
  // Monotonic counter bumped on every "New Conversation" click. ChatView
  // watches it to replay the hero composer focus + acknowledgment pulse even
  // when selectedId was already null (a bare setSelectedId(null) is a React
  // no-op in that case, so without the nonce the click would feel dead).
  const [newConvNonce, setNewConvNonce] = useState<number>(0);
  // Monotonic counter incremented on every pool switch. Each fetchSessions
  // call captures the current value; its .then() compares it to the latest —
  // if they differ, the response is stale (the user has since switched pools)
  // and is discarded. Without this guard, a slow fetch from the OLD pool can
  // resolve after the NEW pool's fetch and overwrite the sidebar.
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
      // The backend emits conversation_created only for subagents, not for
      // main-agent sessions — so we must insert the new session into the
      // sidebar list here for it to appear and highlight. A backend
      // fetchSessions refresh will reconcile any missing fields.
      setSessions((prev) => {
        if (prev.some((s) => s.session_id === fullSessionId)) return prev;
        const agentName = fullSessionId.split(".")[1] || "main";
        const now = Date.now();
        return [
          ...prev,
          {
            session_id: fullSessionId,
            agent_name: agentName,
            pool: pool || "main",
            parent_session_id: null,
            created_at: now,
            updated_at: now,
          },
        ];
      });
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

  // Persist activePool changes (per-workspace key).
  useEffect(() => {
    saveActivePool(ws, activePool);
  }, [ws, activePool]);

  // Load sessions on mount and re-fetch on pool change. The ws scope is
  // constant for this pod's lifetime, so no workspace race handling remains.
  useEffect(() => {
    const epoch = fetchEpochRef.current;
    fetchSessions(ws || undefined, activePool)
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
  }, [ws, activePool]);

  // Validate the restored/persisted pool against the global pool list.
  useEffect(() => {
    if (pools.length > 0 && !pools.some((p) => p.name === activePool)) {
      const first = pools[0];
      if (first) {
        setActivePool(first.name);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pools]);

  const refreshSessions = useCallback((): void => {
    const epoch = fetchEpochRef.current;
    fetchSessions(ws || undefined, activePool)
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
  }, [ws, activePool]);

  useEffect(() => {
    refreshSessionsRef.current = refreshSessions;
  }, [refreshSessions]);

  // Clear the debounced tree-refresh timer on unmount.
  useEffect(() => {
    return (): void => {
      if (treeRefreshTimerRef.current) {
        clearTimeout(treeRefreshTimerRef.current);
        treeRefreshTimerRef.current = null;
      }
    };
  }, []);

  const selectSession = useCallback(
    (sessionId: string): void => {
      setSelectedId(sessionId);
      refreshSessions();
    },
    [refreshSessions],
  );

  const handleNew = useCallback(
    (pool: string): void => {
      // New Conversation now means "return to the hero view" — no sidebar
      // placeholder, no client-side draft. The real session is created only
      // when the user actually sends a message from the hero composer
      // (createDraftForSend below). Repeated clicks are idempotent because
      // the hero view is the same regardless of how many times it's hit.
      void pool;
      setSelectedId(null);
      setNewConvNonce((n) => n + 1);
    },
    [],
  );

  // Create a client-side draft session for the hero-send flow: generate a
  // uuid prefix, register it in pendingRef/draftIdsRef so useWebUIStream
  // attaches it to the backend, and select it. Deliberately does NOT insert
  // a placeholder into `sessions` — the sidebar only shows the new
  // conversation once handleSessionReady inserts it (the backend emits
  // conversation_created only for subagents, not main-agent sessions).
  // Returns the uuid prefix so the caller can correlate with onSessionReady.
  const createDraftForSend = useCallback(
    (pool: string): string => {
      const uuidPrefix = crypto.randomUUID().replace(/-/g, "").slice(0, 12);
      pendingRef.current.set(uuidPrefix, pool);
      draftIdsRef.current.set(uuidPrefix, pool);
      setSelectedId(uuidPrefix);
      return uuidPrefix;
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
      const pool =
        sessions.find((s) => s.session_id === sessionId)?.pool ?? activePool;
      deleteConversation(sessionId, ws || undefined, pool)
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
    [selectedId, ws, sessions, activePool],
  );

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
      fetchSessions(ws || undefined, pool)
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
    [ws],
  );

  const onSent = useCallback((sessionId: string | null): void => {
    if (!sessionId) return;
    // The session is now real — clear draft tracking so subsequent
    // "New Conversation" clicks create a fresh empty draft.
    draftIdsRef.current.delete(sessionId);
    // Bump updated_at now so the sidebar (sorted by updated_at desc)
    // immediately moves this conversation to the top, instead of waiting
    // for the backend to refresh.
    const now = Date.now();
    setSessions((prev) =>
      prev.map((s) =>
        s.session_id === sessionId ? { ...s, updated_at: now } : s,
      ),
    );
  }, []);

  return {
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
    newConvNonce,
  };
}
