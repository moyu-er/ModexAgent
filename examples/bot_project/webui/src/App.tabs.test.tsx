// Workspace shell at the App level (PA-08): seeding priority (persisted →
// default → open entry), canonical-path dedupe on open, close-to-empty
// landing on the open-workspace entry, per-pod session isolation, and the
// standalone settings hash route (PA-11).

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor, act, cleanup } from "@testing-library/react";
import { fetchSessions, fetchPools, fetchWorkspace, cdWorkspace } from "./lib/api";
import { fetchPreferences, savePreferences } from "./lib/preferencesApi";
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

vi.mock("./lib/preferencesApi", () => ({
  fetchPreferences: vi.fn().mockResolvedValue({
    defaultWorkspace: null,
    defaultPool: "main",
  }),
  savePreferences: vi.fn(async (prefs: { defaultWorkspace: string | null; defaultPool: string }) => prefs),
}));

vi.mock("./lib/poolApi", () => ({
  listPools: vi.fn().mockResolvedValue([]),
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

function tabs(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[role="tab"]'));
}

describe("App workspace shell (PA-08)", () => {
  beforeEach(() => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    sessionStorage.clear();
    localStorage.clear();
    window.location.hash = "";

    vi.mocked(fetchPools).mockReset().mockResolvedValue([{ name: "main" }]);
    vi.mocked(fetchWorkspace).mockReset().mockResolvedValue({
      home: "/home",
      recent: [],
      timezone: "UTC",
    });
    vi.mocked(cdWorkspace).mockReset().mockImplementation(async (path) => path);
    vi.mocked(fetchPreferences)
      .mockReset()
      .mockResolvedValue({ defaultWorkspace: null, defaultPool: "main" });
    vi.mocked(fetchSessions).mockReset().mockImplementation((ws?: string) => {
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

  it("boots into the server home with no default and nothing persisted", async () => {
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("homeSession");
    });
    expect(wsTabIds()).toHaveLength(1);
    expect(fetchSessions).toHaveBeenCalledWith("/home");
    expect(fetchSessions).not.toHaveBeenCalledWith(undefined);
  });

  it("a failed preference fetch still boots into the server home", async () => {
    vi.mocked(fetchPreferences).mockRejectedValue(new Error("prefs down"));
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("homeSession");
    });
    expect(wsTabIds()).toHaveLength(1);
  });

  it("seeds the default workspace as the single tab when preferences name one", async () => {
    vi.mocked(fetchPreferences).mockResolvedValue({
      defaultWorkspace: "/ws_a",
      defaultPool: "main",
    });
    // The home fetch is ALSO in flight — the default must still win (no
    // premature home seed over a late configured default).
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("aSession");
    });
    expect(wsTabIds()).toHaveLength(1);
    expect(fetchSessions).toHaveBeenCalledWith("/ws_a");
    expect(fetchSessions).not.toHaveBeenCalledWith("/home");
  });

  it("falls back to the server home (with an error) when the default is invalid", async () => {
    vi.mocked(fetchPreferences).mockResolvedValue({ defaultWorkspace: "/gone", defaultPool: "main" });
    vi.mocked(cdWorkspace).mockImplementation(async (path) => {
      if (path === "/gone") throw new Error("Directory no longer exists");
      return path;
    });
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("homeSession");
    });
    expect(wsTabIds()).toHaveLength(1);
    await waitFor(() => {
      expect(document.body.textContent).toContain("/gone");
    });
  });

  it("a delayed preference fetch still overrides the home fallback (startup race)", async () => {
    let resolvePrefs!: (v: { defaultWorkspace: string | null; defaultPool: string }) => void;
    vi.mocked(fetchPreferences).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePrefs = resolve;
        }),
    );
    render(<App />);
    // Home is known but seeding holds for the preference fetch.
    await act(async () => {
      await Promise.resolve();
    });
    expect(fetchSessions).not.toHaveBeenCalled();
    await act(async () => {
      resolvePrefs({ defaultWorkspace: "/ws_a", defaultPool: "main" });
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(document.body.textContent).toContain("aSession");
    });
    expect(fetchSessions).not.toHaveBeenCalledWith("/home");
  });

  it("validates an unavailable default before mounting a chat pod", async () => {
    vi.mocked(fetchPreferences).mockResolvedValue({ defaultWorkspace: "/gone", defaultPool: "main" });
    vi.mocked(cdWorkspace).mockRejectedValue(new Error("Directory no longer exists"));
    render(<App />);
    await waitFor(() => expect(document.body.textContent).toContain("Directory no longer exists"));
    expect(document.querySelector("[data-testid='workspace-entry']")).toBeTruthy();
    expect(fetchSessions).not.toHaveBeenCalled();
  });

  it("opens the explicit workspace link before restored tabs and the default", async () => {
    sessionStorage.setItem(TABS_KEY, JSON.stringify({ tabs: [{ id: "t1", path: "/home" }], active: "t1" }));
    window.history.replaceState(null, "", "/#/chat?ws=%2Fws_a");
    vi.mocked(cdWorkspace).mockImplementation(async (path) => path);
    render(<App />);
    await waitFor(() => {
      const active = tabs().find((tab) => tab.getAttribute("aria-selected") === "true");
      expect(active?.textContent).toContain("ws_a");
    });
    expect(fetchSessions).toHaveBeenCalledWith("/ws_a");
  });

  it("restores persisted tabs over the default workspace (refresh restore wins)", async () => {
    sessionStorage.setItem(
      TABS_KEY,
      JSON.stringify({ tabs: [{ id: "t1", path: "/home" }], active: "t1" }),
    );
    vi.mocked(fetchPreferences).mockResolvedValue({
      defaultWorkspace: "/ws_a",
      defaultPool: "main",
    });
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("homeSession");
    });
    // The default workspace did NOT open a tab — restore wins.
    expect(wsTabIds()).toEqual(["t1"]);
    expect(fetchSessions).not.toHaveBeenCalledWith("/ws_a");
  });

  it("opening an already-open path activates its tab instead of appending (dedupe)", async () => {
    sessionStorage.setItem(
      TABS_KEY,
      JSON.stringify({ tabs: [{ id: "h", path: "/home" }], active: "h" }),
    );
    vi.mocked(fetchWorkspace).mockResolvedValue({
      home: "/home",
      recent: [{ path: "/ws_a" }],
      timezone: "UTC",
    });
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("homeSession");
    });

    const plus = document.querySelector<HTMLButtonElement>(".wstabs-plus");
    await act(async () => {
      plus!.click();
      await Promise.resolve();
    });
    const recentItem = Array.from(
      document.querySelectorAll<HTMLButtonElement>(".wsopen-item"),
    ).find((b) => b.getAttribute("title") === "/ws_a");
    expect(recentItem).toBeTruthy();
    await act(async () => {
      recentItem!.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(wsTabIds()).toHaveLength(2);
    const firstId = wsTabIds()[1]!;

    // Re-open the SAME path via the menu: dedupe activates the existing tab.
    await act(async () => {
      plus!.click();
      await Promise.resolve();
    });
    const again = Array.from(
      document.querySelectorAll<HTMLButtonElement>(".wsopen-item"),
    ).find((b) => b.getAttribute("title") === "/ws_a");
    await act(async () => {
      again!.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(wsTabIds()).toHaveLength(2);
    expect(document.querySelector(".wstab.active")?.getAttribute("data-tab-id")).toBe(firstId);
  });

  it("closing the last tab lands on the open-workspace entry", async () => {
    sessionStorage.setItem(
      TABS_KEY,
      JSON.stringify({ tabs: [{ id: "t1", path: "/home" }], active: "t1" }),
    );
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("homeSession");
    });

    const closeBtn = document.querySelector<HTMLButtonElement>(".wstab-close");
    expect(closeBtn).toBeTruthy();
    await act(async () => {
      closeBtn!.click();
      await Promise.resolve();
    });
    expect(wsTabIds()).toEqual([]);
    await waitFor(() => {
      expect(document.querySelector("[data-testid='workspace-entry']")).toBeTruthy();
    });
  });

  it("keeps each pod scoped to its own workspace across tab switches", async () => {
    sessionStorage.setItem(
      TABS_KEY,
      JSON.stringify({
        tabs: [
          { id: "h", path: "/home" },
          { id: "a", path: "/ws_a" },
        ],
        active: "a",
      }),
    );
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("aSession");
    });

    const homeTab = tabs().find((el) => el.textContent?.includes("home"));
    expect(homeTab).toBeTruthy();
    await act(async () => {
      homeTab!.click();
      await Promise.resolve();
    });
    expect(podEl("h").style.display).not.toBe("none");
    expect(podEl("h").textContent).toContain("homeSession");
    expect(podEl("h").textContent).not.toContain("aSession");
  });

  it("hash route #/settings opens the standalone settings page and pods stay mounted", async () => {
    sessionStorage.setItem(
      TABS_KEY,
      JSON.stringify({ tabs: [{ id: "h", path: "/home" }], active: "h" }),
    );
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("homeSession");
    });

    await act(async () => {
      window.location.hash = "#/settings";
      window.dispatchEvent(new Event("hashchange"));
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(document.querySelector("[data-testid='settings-shell']")).toBeTruthy();
    });
    // The settings gear route hides the tab bar; the pod stays mounted
    // (streams keep flowing through a settings visit).
    expect(document.querySelector(".wstabs")).toBeNull();
    expect(podEl("h").style.display).toBe("none");

    // Back to chat restores the shell.
    await act(async () => {
      window.location.hash = "";
      window.dispatchEvent(new Event("hashchange"));
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(document.querySelector(".wstabs")).toBeTruthy();
    });
    expect(podEl("h").style.display).not.toBe("none");
  });

  it("sidebar pin writes the current path as the default workspace preference", async () => {
    sessionStorage.setItem(
      TABS_KEY,
      JSON.stringify({ tabs: [{ id: "h", path: "/home" }], active: "h" }),
    );
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("homeSession");
    });

    await act(async () => {
      const pins = document.querySelectorAll<HTMLButtonElement>(
        'button[aria-label="Set as default workspace"]',
      );
      for (const b of Array.from(pins)) b.click();
      await Promise.resolve();
    });
    expect(savePreferences).toHaveBeenCalledWith({
      defaultWorkspace: "/home",
      defaultPool: "main",
    });
  });

  it("a fetchWorkspace failure shows an error with retry instead of a blank page", async () => {
    vi.mocked(fetchWorkspace).mockRejectedValue(new Error("boom"));
    render(<App />);
    await waitFor(() => {
      expect(document.body.textContent).toContain("Failed to load: boom");
    });

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
      // Retry recovered: home seeds the first tab (no blank page).
      expect(document.body.textContent).toContain("homeSession");
    });
  });
});
