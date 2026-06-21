import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor, cleanup } from "@testing-library/react";
import {
  fetchSessions,
  fetchPools,
  fetchWorkspace,
} from "./lib/api";
import App from "./App";

vi.mock("./lib/api", () => ({
  fetchSessions: vi.fn(),
  fetchPools: vi.fn(),
  fetchWorkspace: vi.fn(),
  deleteConversation: vi.fn(),
  changeWorkspace: vi.fn(),
}));

vi.mock("./lib/timezone", () => ({
  setTimezone: vi.fn(),
  formatShort: vi.fn((ts: number) => new Date(ts).toLocaleTimeString()),
}));

// Fake WebSocket that does nothing but open.
class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readyState = FakeWebSocket.OPEN;
  onopen: ((this: WebSocket, ev: Event) => void) | null = null;
  onclose: ((this: WebSocket, ev: CloseEvent) => void) | null = null;
  onmessage: ((this: WebSocket, ev: MessageEvent) => void) | null = null;
  onerror: ((this: WebSocket, ev: Event) => void) | null = null;
  constructor() {
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.call(this as unknown as WebSocket, new Event("open"));
    });
  }
  send(): void {}
  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
  }
  dispatchEvent(): boolean {
    return true;
  }
}

const WS_STORAGE_KEY = "modexbot_workspace";
const ACTIVE_POOL_STORAGE_KEY = "modexbot_active_pool";

function makeSession(sessionId: string) {
  return {
    session_id: sessionId,
    agent_name: sessionId.split(".")[1] || "main",
    pool: "main",
    parent_session_id: null,
    created_at: 1,
    updated_at: 1,
  };
}

describe("App workspace session list", () => {
  beforeEach(() => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    sessionStorage.clear();
    localStorage.removeItem(ACTIVE_POOL_STORAGE_KEY);

    vi.mocked(fetchPools).mockResolvedValue([{ name: "main" }]);
    vi.mocked(fetchWorkspace).mockResolvedValue({
      home: "/home",
      recent: [],
      timezone: "UTC",
    });
  });

  afterEach(() => {
    cleanup();
    sessionStorage.clear();
    localStorage.removeItem(ACTIVE_POOL_STORAGE_KEY);
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("loads sessions for the workspace stored in sessionStorage on mount", async () => {
    // The user last had workspace A open in this tab.
    sessionStorage.setItem(WS_STORAGE_KEY, "/ws_a");

    vi.mocked(fetchSessions).mockImplementation((ws?: string) => {
      if (ws === "/ws_a") {
        return Promise.resolve([makeSession("a.main")]);
      }
      // Any other ws (including home/undefined) returns a different session.
      return Promise.resolve([makeSession("h.main")]);
    });

    render(<App />);

    // Wait for the workspace-A session to appear.
    await waitFor(() => {
      expect(document.body.textContent).toContain("a.main");
    });

    // The home session should never have been rendered.
    expect(document.body.textContent).not.toContain("h.main");

    // Sanity: we did ask for the right workspace.
    expect(fetchSessions).toHaveBeenCalledWith("/ws_a", "main");
  });
});
