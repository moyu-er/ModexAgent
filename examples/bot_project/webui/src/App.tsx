import { useState, useCallback, useEffect, useRef, type FC } from "react";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { useWebUIStream } from "./hooks/useWebUIStream";
import { fetchSessions, createConversation, fetchPools, fetchWorkspace } from "./lib/api";
import type { ConversationInfo } from "./types/events";
import type { PoolInfo } from "./lib/api";

const SIDEBAR_STORAGE_KEY = "modexbot_sidebar_width";
const DEFAULT_SIDEBAR_WIDTH = 260;
const MIN_SIDEBAR_WIDTH = 180;
const MAX_SIDEBAR_WIDTH = 480;

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

const App: FC = () => {
  const [conversations, setConversations] = useState<ConversationInfo[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pools, setPools] = useState<PoolInfo[]>([]);
  const [workspace, setWorkspace] = useState<string>("");
  const [sidebarWidth, setSidebarWidth] = useState<number>(loadSidebarWidth);

  const { messages, isStreaming, connect, disconnect, send } =
    useWebUIStream(selectedId);

  // Resize state
  const resizing = useRef(false);
  const resizeStartX = useRef(0);
  const resizeStartWidth = useRef(0);

  const onResizeMouseDown = useCallback(
    (e: React.MouseEvent): void => {
      e.preventDefault();
      resizing.current = true;
      resizeStartX.current = e.clientX;
      resizeStartWidth.current = sidebarWidth;
    },
    [sidebarWidth],
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
        saveSidebarWidth(sidebarWidth);
      }
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    return (): void => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
    };
  }, [sidebarWidth]);

  // Load conversations on mount
  useEffect(() => {
    fetchSessions()
      .then(setConversations)
      .catch(() => {});
  }, []);

  // Load available pools on mount
  useEffect(() => {
    fetchPools()
      .then(setPools)
      .catch(() => {});
  }, []);

  // Fetch workspace on mount and on conversation change
  useEffect(() => {
    fetchWorkspace()
      .then((info) => setWorkspace(info.cwd))
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

  const refreshConversations = useCallback((): void => {
    fetchSessions()
      .then(setConversations)
      .catch(() => {});
  }, []);

  const handleSelect = useCallback(
    (conversationId: string): void => {
      setSelectedId(conversationId);
      refreshConversations();
    },
    [refreshConversations],
  );

  const handleNew = useCallback(
    (pool: string): void => {
      createConversation(pool)
        .then((resp) => {
          setSelectedId(resp.conversation_id);
          return fetchSessions();
        })
        .then(setConversations)
        .catch(() => {});
    },
    [],
  );

  const handleSend = useCallback(
    (content: string): void => {
      send(content);
    },
    [send],
  );

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-gray-950">
      {/* Sidebar with dynamic width */}
      <div
        style={{ width: sidebarWidth }}
        className="flex-shrink-0 h-full"
      >
        <Sidebar
          conversations={conversations}
          pools={pools}
          selected={selectedId}
          workspace={workspace}
          onSelect={handleSelect}
          onNew={handleNew}
        />
      </div>

      {/* Resize handle */}
      <div
        onMouseDown={onResizeMouseDown}
        className={`w-1 flex-shrink-0 cursor-col-resize transition-colors ${
          resizing.current
            ? "bg-blue-500"
            : "bg-gray-800 hover:bg-blue-500/50"
        }`}
      />

      <main className="flex-1 flex flex-col min-w-0">
        <ChatView
          messages={messages}
          isStreaming={isStreaming}
          onSend={handleSend}
        />
      </main>
    </div>
  );
};

export default App;
