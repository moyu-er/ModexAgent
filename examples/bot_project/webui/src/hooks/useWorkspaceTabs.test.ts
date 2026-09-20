// useWorkspaceTabs (PA-08) — no pinned home, canonical-path dedupe,
// close-to-empty, restore priority.
//
// Contract (DESIGN §3.2 / tickets PA-08):
//  - There is NO pinned home tab and no index-0 privilege: every tab is
//    closable and draggable; closing the last tab leaves the empty set and
//    the App shows the open-workspace entry.
//  - openWorkspace dedupes by canonical path (Windows case + separator
//    insensitive): re-opening an open path ACTIVATES the existing tab.
//  - Seeding priority on mount: persisted tabs (current tab restore) first;
//    else the user's default workspace; else the empty set. The legacy
//    single-workspace key still migrates once.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
vi.mock("../lib/api", () => ({ cdWorkspace: vi.fn(async (path: string) => path) }));
import {
  useWorkspaceTabs,
  computeTabLabels,
  pathBasename,
  sameWorkspacePath,
  fallbackTabId,
} from "./useWorkspaceTabs";

const HOME = "F:\\home\\bot_project";
const WS_A = "F:\\tool\\pythonProject\\ModexAgent";
const WS_B = "D:\\projects\\side-project";

describe("pathBasename", () => {
  it("returns the last segment for windows and posix paths", () => {
    expect(pathBasename("F:\\tool\\pythonProject\\ModexAgent")).toBe("ModexAgent");
    expect(pathBasename("/home/user/project")).toBe("project");
    expect(pathBasename("D:\\projects\\side-project\\")).toBe("side-project");
  });
});

describe("sameWorkspacePath", () => {
  it("matches identical paths", () => {
    expect(sameWorkspacePath(WS_A, WS_A)).toBe(true);
  });

  it("is Windows representation tolerant (case + slashes + trailing)", () => {
    expect(sameWorkspacePath("F:\\Tool\\PythonProject\\modexagent", WS_A)).toBe(true);
    expect(sameWorkspacePath("F:/tool/pythonProject/ModexAgent/", WS_A)).toBe(true);
  });

  it("distinguishes different paths", () => {
    expect(sameWorkspacePath(WS_A, WS_B)).toBe(false);
  });
});

describe("computeTabLabels", () => {
  it("uses basenames (no home special-case)", () => {
    const labels = computeTabLabels([{ id: "a", path: WS_A }]);
    expect(labels["a"]).toBe("ModexAgent");
  });

  it("disambiguates colliding basenames from DIFFERENT paths with parent dir", () => {
    const labels = computeTabLabels([
      { id: "a", path: "F:\\work\\webui" },
      { id: "b", path: "D:\\play\\webui" },
    ]);
    expect(labels["a"]).toBe("work ▸ webui");
    expect(labels["b"]).toBe("play ▸ webui");
  });
});

describe("fallbackTabId", () => {
  it("returns the left neighbor (any tab, no pinned home)", () => {
    const tabs = [
      { id: "x", path: WS_A },
      { id: "y", path: WS_B },
    ];
    expect(fallbackTabId(tabs, "y")).toBe("x");
    // Closing the first tab falls to the (new) first — right neighbor.
    expect(fallbackTabId(tabs, "x")).toBe("y");
    expect(fallbackTabId(tabs, "zzz")).toBeNull();
  });

  it("returns null for the last remaining tab", () => {
    expect(fallbackTabId([{ id: "only", path: WS_A }], "only")).toBeNull();
  });
});

describe("useWorkspaceTabs", () => {
  beforeEach(() => {
    sessionStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  it("seeds the server home when no default workspace and nothing is persisted", async () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.tabs.map((t) => t.path)).toEqual([HOME]);
    expect(result.current.activeId).toBe(result.current.tabs[0]!.id);
  });

  it("holds seeding until preferences settle so a late default wins over home", async () => {
    const { result, rerender } = renderHook(
      ({ ready }: { ready: boolean }) => useWorkspaceTabs(HOME, undefined, ready),
      { initialProps: { ready: false } },
    );
    // Preference fetch in flight: nothing seeded yet (not even home).
    expect(result.current.ready).toBe(false);
    expect(result.current.tabs).toEqual([]);
    // The fetch resolves with a configured default → that wins.
    rerender({ ready: true });
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.tabs.map((t) => t.path)).toEqual([HOME]);
    expect(result.current.activeId).toBe(result.current.tabs[0]!.id);
  });

  it("an early user-opened workspace is never overridden by a late default", () => {
    const { result, rerender } = renderHook(
      ({ def }: { def: string | null }) => useWorkspaceTabs(HOME, def, true),
      { initialProps: { def: null as string | null } },
    );
    act(() => result.current.openWorkspace(WS_B));
    rerender({ def: WS_A });
    expect(result.current.tabs.map((t) => t.path)).toEqual([WS_B]);
  });

  it("seeds the default workspace when nothing is persisted", async () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME, WS_A));
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.tabs.map((t) => t.path)).toEqual([WS_A]);
    expect(result.current.activeId).toBe(result.current.tabs[0]!.id);
  });

  it("falls back to the server home with the REAL validation error when the configured default is invalid", async () => {
    const { cdWorkspace } = await import("../lib/api");
    vi.mocked(cdWorkspace).mockImplementation(async (path: string) => {
      if (path === "/gone") throw new Error("Directory no longer exists");
      return path;
    });
    const { result } = renderHook(() => useWorkspaceTabs(HOME, "/gone"));
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.tabs.map((t) => t.path)).toEqual([HOME]);
    expect(result.current.error).toContain("/gone");
    // The server's reason surfaced verbatim — no invented sentinel text.
    expect(result.current.error).toContain("Directory no longer exists");
    expect(result.current.error).not.toContain("unavailable");
  });

  it("shows the error and the empty entry when the default AND home are invalid", async () => {
    const { cdWorkspace } = await import("../lib/api");
    vi.mocked(cdWorkspace).mockImplementation(async (path: string) => {
      if (path === "/gone") throw new Error("Directory no longer exists");
      if (path === HOME) throw new Error("home vanished");
      return path;
    });
    const { result } = renderHook(() => useWorkspaceTabs(HOME, "/gone"));
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.tabs).toEqual([]);
    expect(result.current.error).toContain("/gone");
    expect(result.current.error).toContain("home vanished");
  });

  it("falls back to the configured default when ALL restored tabs are invalid", async () => {
    const { cdWorkspace } = await import("../lib/api");
    vi.mocked(cdWorkspace).mockImplementation(async (path: string) => {
      if (path === WS_B || path === "/dead") throw new Error("no such directory");
      return path;
    });
    sessionStorage.setItem(
      "modexbot_ws_tabs",
      JSON.stringify({
        tabs: [
          { id: "t1", path: WS_B },
          { id: "t2", path: "/dead" },
        ],
        active: "t1",
      }),
    );
    const { result } = renderHook(() => useWorkspaceTabs(HOME, WS_A));
    await waitFor(() => expect(result.current.ready).toBe(true));
    // Both restored tabs were invalid → the configured default opened.
    expect(result.current.tabs.map((t) => t.path)).toEqual([WS_A]);
    expect(result.current.activeId).toBe(result.current.tabs[0]!.id);
    // The restored tabs' real errors are preserved (home was NOT tried —
    // a candidate opened, so no home tab is added).
    expect(result.current.error).toContain(WS_B);
    expect(result.current.error).toContain("/dead");
    expect(result.current.error).not.toContain(HOME);
  });

  it("falls back to the server home when ALL restored tabs are invalid and no default is configured", async () => {
    const { cdWorkspace } = await import("../lib/api");
    vi.mocked(cdWorkspace).mockImplementation(async (path: string) => {
      if (path === WS_B) throw new Error("no such directory");
      return path;
    });
    sessionStorage.setItem(
      "modexbot_ws_tabs",
      JSON.stringify({ tabs: [{ id: "t1", path: WS_B }], active: "t1" }),
    );
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.tabs.map((t) => t.path)).toEqual([HOME]);
    expect(result.current.error).toContain("no such directory");
  });

  it("an early user open DURING the home fallback is preserved and persisted", async () => {
    const { cdWorkspace } = await import("../lib/api");
    let releaseHome!: () => void;
    const homeAttempt = new Promise<void>((resolve) => { releaseHome = resolve; });
    vi.mocked(cdWorkspace).mockImplementation(async (path: string) => {
      if (path === "/gone") throw new Error("Directory no longer exists");
      if (path === HOME) await homeAttempt; // fallback in flight
      return path;
    });
    const { result } = renderHook(() => useWorkspaceTabs(HOME, "/gone"));
    // While the invalid-default → home fallback is still awaiting, the
    // user explicitly opens a workspace.
    act(() => result.current.openWorkspace(WS_B));
    act(() => releaseHome());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    // The user's tab survived; no seed overwrote it; initialization is
    // finished and the tab set is persisted.
    expect(result.current.tabs.map((t) => t.path)).toEqual([WS_B]);
    expect(result.current.ready).toBe(true);
    const persisted = JSON.parse(sessionStorage.getItem("modexbot_ws_tabs") ?? "null");
    expect(persisted?.tabs.map((t: { path: string }) => t.path)).toEqual([WS_B]);
    expect(persisted?.active).toBe(result.current.activeId);
  });

  it("restores persisted tabs over the default workspace", async () => {
    sessionStorage.setItem(
      "modexbot_ws_tabs",
      JSON.stringify({
        tabs: [
          { id: "t1", path: WS_B },
          { id: "t2", path: WS_A },
        ],
        active: "t2",
      }),
    );
    const { result } = renderHook(() => useWorkspaceTabs(HOME, WS_B));
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.tabs.map((t) => t.path)).toEqual([WS_B, WS_A]);
    expect(result.current.activeId).toBe("t2");
  });

  it("migrates the legacy single-workspace key once", async () => {
    sessionStorage.setItem("modexbot_workspace", WS_A);
    const { result } = renderHook(() => useWorkspaceTabs(HOME, WS_B));
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.tabs.map((t) => t.path)).toEqual([WS_A]);
    expect(result.current.activeId).toBe(result.current.tabs[0]!.id);
    // The legacy key is consumed.
    expect(sessionStorage.getItem("modexbot_workspace")).toBeNull();
  });

  it("openWorkspace dedupes by canonical path and activates the existing tab", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.openWorkspace(WS_A));
    act(() => result.current.openWorkspace(WS_B));
    const aId = result.current.tabs[0]!.id;
    act(() => result.current.openWorkspace("F:/TOOL/pythonProject/modexagent"));
    expect(result.current.tabs).toHaveLength(2);
    expect(result.current.activeId).toBe(aId);
  });

  it("closeTab removes any tab; closing the last leaves the empty set", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.openWorkspace(WS_A));
    act(() => result.current.openWorkspace(WS_B));
    const aId = result.current.tabs[0]!.id;
    act(() => result.current.closeTab(result.current.tabs[1]!.id));
    expect(result.current.tabs.map((t) => t.path)).toEqual([WS_A]);
    act(() => result.current.closeTab(aId));
    expect(result.current.tabs).toEqual([]);
    expect(result.current.activeId).toBe("");
  });

  it("closing the active tab activates its fallback; closing inactive keeps selection", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.openWorkspace(WS_A));
    act(() => result.current.openWorkspace(WS_B));
    const aId = result.current.tabs[0]!.id;
    const bId = result.current.tabs[1]!.id;
    act(() => result.current.closeTab(bId));
    expect(result.current.activeId).toBe(aId);

    act(() => result.current.openWorkspace(WS_B));
    const b2Id = result.current.tabs[1]!.id;
    act(() => result.current.closeTab(aId));
    expect(result.current.activeId).toBe(b2Id);
  });

  it("reorderTab moves any tab to any index (no pinned index 0)", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.openWorkspace(WS_A));
    act(() => result.current.openWorkspace(WS_B));
    const bId = result.current.tabs[1]!.id;
    act(() => result.current.reorderTab(bId, 0));
    expect(result.current.tabs.map((t) => t.path)).toEqual([WS_B, WS_A]);
  });

  it("persists and restores tabs + active across a remount", async () => {
    const first = renderHook(() => useWorkspaceTabs(HOME));
    await waitFor(() => expect(first.result.current.ready).toBe(true));
    act(() => first.result.current.openWorkspace(WS_A));
    act(() => first.result.current.openWorkspace(WS_B));
    const aId = first.result.current.tabs.find((t) => t.path === WS_A)!.id;
    act(() => first.result.current.activateTab(aId));
    first.unmount();

    const second = renderHook(() => useWorkspaceTabs(HOME));
    await waitFor(() => expect(second.result.current.ready).toBe(true));
    // The home seed (committed before the opens) plus the user-opened tabs,
    // in open order — restored verbatim.
    expect(second.result.current.tabs.map((t) => t.path)).toEqual([HOME, WS_A, WS_B]);
    expect(second.result.current.activeId).toBe(aId);
  });

  it("reportStatus stores per-tab status and drops it on close", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.openWorkspace(WS_A));
    const tabId = result.current.tabs[0]!.id;
    const status = { running: 2, pendingApprovals: 1, connected: true };
    act(() => result.current.reportStatus(tabId, status));
    expect(result.current.statuses[tabId]).toEqual(status);
    const before = result.current.statuses;
    act(() => result.current.reportStatus(tabId, { ...status }));
    expect(result.current.statuses).toBe(before); // no-op write skipped
    act(() => result.current.closeTab(tabId));
    expect(result.current.statuses[tabId]).toBeUndefined();
  });
});
