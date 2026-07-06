import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor, act, cleanup } from "@testing-library/react";
import {
  fetchSessions,
  fetchPools,
  fetchWorkspace,
  changeWorkspace,
} from "./lib/api";
import App from "./App";

vi.mock("./lib/api", () => ({
  fetchSessions: vi.fn(),
  fetchPools: vi.fn(),
  fetchWorkspace: vi.fn(),
  fetchModels: vi.fn().mockResolvedValue({ choices: [] }),
  deleteConversation: vi.fn(),
  changeWorkspace: vi.fn(),
  browseWorkspace: vi.fn(),
}));

vi.mock("./lib/timezone", () => ({
  setTimezone: vi.fn(),
  formatShort: vi.fn((ts: number) => new Date(ts).toLocaleTimeString()),
}));

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
  sent: any[] = [];
  constructor() {
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.call(this as unknown as WebSocket, new Event("open"));
    });
  }
  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }
  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
  }
  dispatchEvent(): boolean {
    return true;
  }
  pushEnvelope(ev: Record<string, unknown>): void {
    const msg = new MessageEvent("message", { data: JSON.stringify(ev) });
    this.onmessage?.call(this as unknown as WebSocket, msg);
  }
}

const WS_STORAGE_KEY = "modexbot_workspace";
const ACTIVE_POOL_STORAGE_KEY = "modexbot_active_pool";

function makeSession(sessionId: string, pool = "main") {
  return {
    session_id: sessionId,
    agent_name: sessionId.split(".")[1] || "main",
    pool,
    parent_session_id: null,
    created_at: 1,
    updated_at: 1,
  };
}

describe("App workspace switch race condition", () => {
  let wsInstance: FakeWebSocket;

  beforeEach(() => {
    vi.stubGlobal(
      "WebSocket",
      class extends FakeWebSocket {
        constructor() {
          super();
          (globalThis as any).__wsInstance = this;
          wsInstance = this;
        }
      },
    );
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

  it("discards stale fetchSessions response when workspace switches mid-fetch", async () => {
    // Start on workspace A.
    sessionStorage.setItem(WS_STORAGE_KEY, "/ws_a");

    // Controllable promises so we can resolve them in a specific order.
    let resolveA: (v: any[]) => void = () => {};
    let resolveHome: (v: any[]) => void = () => {};
    const fetchSessionsA = new Promise<any[]>((r) => { resolveA = r; });
    const fetchSessionsHome = new Promise<any[]>((r) => { resolveHome = r; });

    let callCount = 0;
    vi.mocked(fetchSessions).mockImplementation((ws?: string) => {
      callCount++;
      if (callCount === 1) {
        return Promise.resolve([makeSession("aSession.main")]);
      }
      if (callCount === 2) {
        return Promise.resolve([makeSession("aSession.main")]);
      }
      if (ws === "/ws_a") return fetchSessionsA;
      if (ws === "/home") return fetchSessionsHome;
      return Promise.resolve([]);
    });

    vi.mocked(changeWorkspace).mockImplementation(async (path: string) => {
      return { success: true, cwd: path === "" ? "/home" : path, notice: "" };
    });

    render(<App />);

    // Wait for ws_a sessions to show.
    await waitFor(() => {
      expect(document.body.textContent).toContain("aSession");
    });

    // Simulate: a non-selected session starts streaming (onSessionActivity fires).
    // This sets up the 600ms debounced refreshSessions.
    act(() => {
      wsInstance.pushEnvelope({
        event_type: "model_content_delta",
        session_id: "otherSession.main",
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: {},
        payload: { text: "x", turn_id: "t1" },
        timestamp: Date.now(),
      });
    });

    // Advance fake timers to trigger the 600ms debounce.
    vi.useFakeTimers();
    act(() => { vi.advanceTimersByTime(700); });
    vi.useRealTimers();

    // The debounced refreshSessions has now fired fetchSessions("/ws_a").
    // It's pending (fetchSessionsA not resolved yet).

    // User clicks Home → handleGoHome → changeWorkspace("") → handleWorkspaceChanged("/home").
    // We drive it by calling the WorkspaceBrowser's onGoHome through the sidebar.
    // Since we can't click easily, we simulate by calling changeWorkspace directly
    // and then invoking handleWorkspaceChanged via re-render with sessionStorage.
    //
    // Actually, the Sidebar "Home" button calls onGoHome directly. Let's click it.
    const homeButtons = Array.from(
      document.querySelectorAll("button"),
    ).filter((b) => b.textContent?.includes("Home"));
    // The sidebar Home button only shows when !isHome. Since we're on ws_a, it shows.
    expect(homeButtons.length).toBeGreaterThan(0);

    // Click the sidebar Home button (triggers handleGoHome).
    await act(async () => {
      homeButtons[0]!.click();
      // Allow changeWorkspace mock to resolve.
      await Promise.resolve();
    });

    // handleWorkspaceChanged("/home") has now fired fetchSessions("/home").
    // It's pending (fetchSessionsHome not resolved yet).

    // THE RACE: resolve the FRESH fetch (home) FIRST, then the STALE fetch (ws_a).
    // Without the epoch guard, the stale ws_a response resolves last and
    // overwrites the sidebar with the old workspace's sessions.
    await act(async () => {
      resolveHome([makeSession("homeSession.main")]);
      await Promise.resolve();
      resolveA([makeSession("aSession.main")]);
      await Promise.resolve();
    });

    // After both resolve, the sidebar must show homeSession, NOT aSession.
    await waitFor(() => {
      const text = document.body.textContent || "";
      expect(text).toContain("homeSession");
      expect(text).not.toContain("aSession");
    });
  });
});
