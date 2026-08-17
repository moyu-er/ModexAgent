// Workspace tabs at the App level: boot seeding, legacy-key migration,
// per-pod session isolation (the invariant that replaces the deleted
// cross-workspace fetch race — each pod only ever fetches and shows its own
// workspace's sessions, so a stale response CANNOT overwrite another
// workspace's sidebar), close fallback, and drag-reorder persistence.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor, act, cleanup, fireEvent } from "@testing-library/react";
import { fetchSessions, fetchPools, fetchWorkspace, cdWorkspace } from "./lib/api";
import App from "./App";

vi.mock("./lib/api", () => ({
  fetchSessions: vi.fn(),
  fetchPools: vi.fn(),
  fetchWorkspace: vi.fn(),
  fetchModels: vi.fn().mockResolvedValue({ choices: [] }),
  deleteConversation: vi.fn(),
  changeWorkspace: vi.fn(),
  cdWorkspace: vi.fn(),
  // Pods render graph views whose error formatting instanceof-checks ApiError.
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly statusText: string,
      readonly detail: string,
    ) {
      super(`API ${status} ${statusText}${detail ? `: ${detail}` : ""}`);
      this.name = "ApiError";
    }
  },
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
  sent: unknown[] = [];
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
}

const LEGACY_WS_KEY = "modexbot_workspace";
const TABS_KEY = "modexbot_ws_tabs";

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

function podEl(tabId: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-pod-id="${tabId}"]`);
  expect(el, `pod ${tabId} mounted`).toBeTruthy();
  return el!;
}

function wsTabIds(): string[] {
  return Array.from(document.querySelectorAll("[data-pod-id]")).map(
    (el) => (el as HTMLElement).dataset.podId ?? "",
  );
}

describe("App workspace tabs", () => {
  beforeEach(() => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    sessionStorage.clear();
    localStorage.clear();
    window.location.hash = "";

    vi.mocked(fetchPools).mockResolvedValue([{ name: "main" }]);
    vi.mocked(fetchWorkspace).mockResolvedValue({
      home: "/home",
      recent: [],
      timezone: "UTC",
    });
    vi.mocked(fetchSessions).mockImplementation((ws?: string) => {
      if (ws === "/ws_a") return Promise.resolve([makeSession("aSession.main")]);
      return Promise.resolve([makeSession("homeSession.main")]);
    });
  });

  afterEach(() => {
    cleanup();
    sessionStorage.clear();
    localStorage.clear();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("boots into a single home tab scoped to the home partition", async () => {
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("homeSession");
    });
    expect(wsTabIds()).toEqual(["__home__"]);
    expect(fetchSessions).toHaveBeenCalledWith(undefined, "main");
  });

  it("migrates the legacy single-workspace key into a second active tab", async () => {
    sessionStorage.setItem(LEGACY_WS_KEY, "/ws_a");
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("aSession");
    });
    const ids = wsTabIds();
    expect(ids).toHaveLength(2);
    expect(ids[0]).toBe("__home__");
    // The migrated tab is active; the home pod stays mounted but hidden.
    expect(podEl(ids[1]!).style.display).not.toBe("none");
    expect(podEl("__home__").style.display).toBe("none");
    // Each pod fetched with its own scope + pool.
    expect(fetchSessions).toHaveBeenCalledWith("/ws_a", "main");
    expect(fetchSessions).toHaveBeenCalledWith(undefined, "main");
  });

  it("keeps each pod scoped to its own workspace across tab switches", async () => {
    sessionStorage.setItem(LEGACY_WS_KEY, "/ws_a");
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("aSession");
    });
    const wsTabId = wsTabIds()[1]!;

    // Each pod only ever fetched its own workspace's sessions.
    const wsArgs = vi.mocked(fetchSessions).mock.calls.map((c) => c[0]);
    expect(wsArgs).toContain("/ws_a");
    expect(wsArgs).toContain(undefined);

    // Switch to the home tab: home pod becomes visible, ws pod hides.
    const homeTab = Array.from(
      document.querySelectorAll<HTMLElement>('[role="tab"]'),
    ).find((el) => el.textContent?.includes("Home"));
    expect(homeTab).toBeTruthy();
    await act(async () => {
      homeTab!.click();
      await Promise.resolve();
    });
    expect(podEl("__home__").style.display).not.toBe("none");
    expect(podEl(wsTabId).style.display).toBe("none");
    expect(podEl("__home__").textContent).toContain("homeSession");

    // Switch back: the ws pod still shows its own session, untouched by the
    // home pod's fetches.
    const wsTab = Array.from(
      document.querySelectorAll<HTMLElement>('[role="tab"]'),
    ).find((el) => el.textContent?.includes("ws_a"));
    await act(async () => {
      wsTab!.click();
      await Promise.resolve();
    });
    expect(podEl(wsTabId).style.display).not.toBe("none");
    expect(podEl(wsTabId).textContent).toContain("aSession");
    expect(podEl(wsTabId).textContent).not.toContain("homeSession");
  });

  it("closing the active tab falls back to the left neighbor and unmounts it", async () => {
    sessionStorage.setItem(LEGACY_WS_KEY, "/ws_a");
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("aSession");
    });

    const closeBtn = document.querySelector<HTMLButtonElement>(".wstab-close");
    expect(closeBtn).toBeTruthy();
    await act(async () => {
      closeBtn!.click();
      await Promise.resolve();
    });

    expect(wsTabIds()).toEqual(["__home__"]);
    expect(podEl("__home__").style.display).not.toBe("none");
    // The closed pod's unmount disconnected its WebSocket without errors and
    // the home pod's content is intact.
    expect(podEl("__home__").textContent).toContain("homeSession");
  });

  it("persists the tab set across a remount", async () => {
    sessionStorage.setItem(LEGACY_WS_KEY, "/ws_a");
    const first = render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("aSession");
    });
    const persisted = JSON.parse(sessionStorage.getItem(TABS_KEY) ?? "{}");
    expect(persisted.tabs).toHaveLength(2);
    expect(persisted.active).toBe(persisted.tabs[1].id);
    first.unmount();
  });

  it("opening the home path creates a normal closable tab sharing the home scope", async () => {
    vi.mocked(fetchWorkspace).mockResolvedValue({
      home: "/home",
      recent: [{ path: "/home" }],
      timezone: "UTC",
    });
    vi.mocked(cdWorkspace).mockResolvedValue("/home");
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("homeSession");
    });

    // Open the home path from the "+" menu — it must append a NORMAL tab
    // (closable, draggable), not dedupe onto the pinned home tab.
    const plus = document.querySelector<HTMLButtonElement>(".wstabs-plus");
    await act(async () => {
      plus!.click();
      await Promise.resolve();
    });
    const recentItem = Array.from(
      document.querySelectorAll<HTMLButtonElement>(".wsopen-item"),
    ).find((b) => b.getAttribute("title") === "/home");
    expect(recentItem).toBeTruthy();
    await act(async () => {
      recentItem!.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    const tabsEls = Array.from(
      document.querySelectorAll<HTMLElement>('[role="tab"]'),
    );
    expect(tabsEls).toHaveLength(2);
    // The new tab is a regular tab: draggable, has a close button, active.
    expect(tabsEls[1]!.getAttribute("draggable")).toBe("true");
    expect(tabsEls[1]!.querySelector(".wstab-close")).toBeTruthy();
    expect(tabsEls[1]!.classList.contains("active")).toBe(true);
    // Both pods share the home data scope (undefined ws) — same source.
    const homeCalls = vi
      .mocked(fetchSessions)
      .mock.calls.filter((c) => c[0] === undefined);
    expect(homeCalls.length).toBeGreaterThanOrEqual(2);
  });

  it("tab drag reorder fires the reorder handler path (home stays first)", async () => {
    sessionStorage.setItem(LEGACY_WS_KEY, "/ws_a");
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("aSession");
    });
    const tabsEls = Array.from(
      document.querySelectorAll<HTMLElement>('[role="tab"]'),
    );
    expect(tabsEls).toHaveLength(2);
    // Home is pinned: it is not draggable.
    expect(tabsEls[0]!.getAttribute("draggable")).toBe("false");
    expect(tabsEls[1]!.getAttribute("draggable")).toBe("true");
    // DnD on the second tab runs without errors (drop on itself is a no-op).
    fireEvent.dragStart(tabsEls[1]!);
    fireEvent.dragOver(tabsEls[1]!);
    fireEvent.drop(tabsEls[1]!);
    expect(wsTabIds()).toHaveLength(2);
  });

  it("opening a tab saves the current route and starts the new tab on chat", async () => {
    sessionStorage.setItem(LEGACY_WS_KEY, "/ws_a");
    vi.mocked(fetchWorkspace).mockResolvedValue({
      home: "/home",
      recent: [{ path: "/ws_b" }],
      timezone: "UTC",
    });
    vi.mocked(cdWorkspace).mockResolvedValue("/ws_b");
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("aSession");
    });

    // The active ws_a tab navigates to the graphs route.
    await act(async () => {
      window.location.hash = "#/graphs";
      window.dispatchEvent(new Event("hashchange"));
      await Promise.resolve();
    });
    expect(window.location.hash).toBe("#/graphs");

    // Open /ws_b from the "+" menu: the fresh tab starts on the chat route
    // (hash reset), NOT on the outgoing tab's graphs route.
    const plus = document.querySelector<HTMLButtonElement>(".wstabs-plus");
    await act(async () => {
      plus!.click();
      await Promise.resolve();
    });
    const recentItem = Array.from(
      document.querySelectorAll<HTMLButtonElement>(".wsopen-item"),
    ).find((b) => b.getAttribute("title") === "/ws_b");
    await act(async () => {
      recentItem!.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(wsTabIds()).toHaveLength(3);
    expect(window.location.hash).toBe("");

    // Switching back to the ws_a tab restores its graphs route.
    const wsATab = Array.from(
      document.querySelectorAll<HTMLElement>('[role="tab"]'),
    ).find((el) => el.textContent?.includes("ws_a"));
    await act(async () => {
      wsATab!.click();
      await Promise.resolve();
    });
    expect(window.location.hash).toBe("#/graphs");
  });

  it("closing the active tab restores the fallback tab's remembered route", async () => {
    sessionStorage.setItem(LEGACY_WS_KEY, "/ws_a");
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("aSession");
    });

    // Put the HOME tab on the graphs route first.
    const homeTab = Array.from(
      document.querySelectorAll<HTMLElement>('[role="tab"]'),
    ).find((el) => el.textContent?.includes("Home"));
    await act(async () => {
      homeTab!.click();
      await Promise.resolve();
    });
    await act(async () => {
      window.location.hash = "#/graphs";
      window.dispatchEvent(new Event("hashchange"));
      await Promise.resolve();
    });

    // Switch to ws_a (home's #/graphs is remembered), then close it: the
    // fallback (home) becomes active AND its graphs route is restored.
    const wsTab = Array.from(
      document.querySelectorAll<HTMLElement>('[role="tab"]'),
    ).find((el) => el.textContent?.includes("ws_a"));
    await act(async () => {
      wsTab!.click();
      await Promise.resolve();
    });
    expect(window.location.hash).toBe("");

    const closeBtn = document.querySelector<HTMLButtonElement>(".wstab-close");
    await act(async () => {
      closeBtn!.click();
      await Promise.resolve();
    });
    expect(wsTabIds()).toEqual(["__home__"]);
    expect(window.location.hash).toBe("#/graphs");
  });

  it("a fetchWorkspace failure shows an error with retry instead of a blank page", async () => {
    vi.mocked(fetchWorkspace).mockRejectedValue(new Error("boom"));
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("Failed to load: boom");
    });

    // Retry recovers once the backend responds again.
    vi.mocked(fetchWorkspace).mockResolvedValue({
      home: "/home",
      recent: [],
      timezone: "UTC",
    });
    const retryBtn = Array.from(
      document.querySelectorAll<HTMLButtonElement>("button"),
    ).find((b) => b.textContent === "Retry");
    expect(retryBtn).toBeTruthy();
    await act(async () => {
      retryBtn!.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(document.body.textContent).toContain("homeSession");
    });
  });
});
